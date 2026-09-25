"""Worker functions per import DXF batch parallelizzato.

Ogni funzione è top-level (serializzabile per multiprocessing/threading) e
esegue una singola unità di parsing. L'endpoint batch le chiama in pool.

Design:
- process_single_dxf: legge il file, calcola hash, cache lookup, se miss
  fa parsing completo (scansiona + detector + cartiglio + spessore) e cache put.
- Ritorna sempre un dict serializzabile.
- Errori non fanno crash del worker: catturati e ritornati come {success: False}.

Perché ThreadPool e non ProcessPool:
- Flask dev server ha già `threaded=True`, quindi thread interni sono nativi.
- ezdxf e Shapely rilasciano il GIL su chiamate C-level → parallelismo effettivo
  anche con ThreadPool.
- ProcessPool su Windows richiede spawn (lento all'avvio) + serializzazione
  pickle di grossi oggetti.
- Cache SHA256 SQLite: SQLite è thread-safe per default (`check_same_thread=False`).
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def _esegui_cleanup(dxf_path: str, geo: dict | None, filename: str,
                    dxf_cfg: dict | None = None) -> dict:
    """Auto-cleanup DXF (Fase 1a): se il detector ha alta confidence e sanity
    check ok, salva DXF pulito (solo contorno esterno + fori tagliati del pezzo
    scelto dal detector) accanto all'originale come <name>_cleaned.dxf. Il
    commerciale poi lo verifica nella griglia review post-import.

    BUG FIX: prima si usava save_cleaned_dxf (cluster per prossimità pensato
    per il drag manuale) col bbox del detector → spesso il pulito era la
    cornice del foglio o un solo foro. Ora save_cleaned_dxf_pezzo usa il
    contorno esatto del detector e verifica le misure prima di scrivere; se la
    verifica fallisce il pulito NON viene creato (cleanup_reason col motivo).

    Rieseguito anche sui cache hit: il file pulito va creato nella cartella del
    preventivo CORRENTE (il payload cachato puntava a quello di un altro)."""
    cleaned_info = {'cleaned_dxf_filename': None, 'cleaned_status': None,
                    'cleanup_reason': None, 'cleanup_stats': None}
    try:
        from . import dxf_cleanup
        proceed, reason = dxf_cleanup.should_cleanup(geo)
        cleaned_info['cleanup_reason'] = reason
        if proceed and (geo or {}).get('_source') == 'cartiglio_descrizione':
            # misure dalla descrizione del cartiglio, non da un contorno disegnato
            cleaned_info['cleanup_reason'] = 'geometria da descrizione cartiglio: pulizia saltata'
        elif proceed:
            base, ext = os.path.splitext(dxf_path)
            cleaned_path = base + '_cleaned' + ext
            r = dxf_cleanup.save_cleaned_dxf_pezzo(dxf_path, cleaned_path, geo, dxf_cfg)
            if r.get('success'):
                cleaned_info['cleaned_dxf_filename'] = os.path.basename(cleaned_path)
                # 'auto' se confidence alta, 'auto_review' se media
                conf = float((geo or {}).get('confidence', 0) or 0)
                cleaned_info['cleaned_status'] = 'auto' if conf >= 0.7 else 'auto_review'
                cleaned_info['cleanup_stats'] = {
                    'entities_copied': r['entities_copied'],
                    'entities_source': r['entities_source'],
                    'tolerance_mm': r['tolerance_mm'],
                    'bbox_w_mm': r.get('w_mm'),
                    'bbox_h_mm': r.get('h_mm'),
                    'warnings': r.get('warnings') or [],
                }
                logger.info('[%s] cleanup auto ok: %d/%d entità (%s)',
                            filename, r['entities_copied'], r['entities_source'],
                            cleaned_info['cleaned_status'])
            else:
                cleaned_info['cleanup_reason'] = f"pulizia non eseguita: {r.get('error')}"
                logger.info('[%s] cleanup fallito: %s', filename, r.get('error'))
    except Exception as e:
        logger.warning('[%s] cleanup pipeline error: %s', filename, e)
    return cleaned_info


def _misure_come_cartiglio(geo: dict, dim_info: dict | None) -> bool:
    """L'ingombro del contorno scelto coincide (in un verso o nell'altro,
    entro 1 mm o l'1%) con Lunghezza x Larghezza del cartiglio?"""
    if not dim_info:
        return False
    try:
        a = sorted((float(geo.get('bbox_width_mm') or 0), float(geo.get('bbox_height_mm') or 0)))
        b = sorted((float(dim_info.get('dim_x_mm') or 0), float(dim_info.get('dim_y_mm') or 0)))
    except (TypeError, ValueError):
        return False
    if min(a) <= 0 or min(b) <= 0:
        return False
    return all(abs(x - y) <= max(1.0, 0.01 * y) for x, y in zip(a, b))


def process_single_dxf(dxf_path: str, filename: str, dxf_cfg: dict) -> dict:
    """Esegue il pipeline completo di parsing su un singolo file DXF.

    Usato sia dall'import batch sia dall'import singolo (stessa logica, stessi
    risultati).

    Args:
        dxf_path: percorso del file già salvato su disco
        filename: nome file salvato (per audit/log e chiave cache: lo spessore
                  può venire dal nome)
        dxf_cfg: config estrazione (colori piega/saldatura, tolleranze)

    Returns:
        dict payload (stesso schema di POST /import-dxf single):
        {success, filename, lavorazioni, geometria, cartiglio, spessore,
         _cache_hit?, error?}
    """
    from . import dxf_cache, dxf_scanner
    from .dxf_polygon_detector_v3 import detect_pezzo_geometry_v3

    # ---- Cache lookup (chiave: contenuto + nome file + config + Gemini) ----
    try:
        cache_key = dxf_cache.chiave_cache(dxf_cache.hash_file(dxf_path), filename, dxf_cfg)
        cached = dxf_cache.get(cache_key)
    except Exception as e:
        logger.warning('[%s] cache lookup fail: %s', filename, e)
        cache_key = None
        cached = None
    if cached and cached.get('payload'):
        payload = dict(cached['payload'])
        payload.pop('_parser_version', None)
        payload['filename'] = filename
        payload['_cache_hit'] = True
        # Il DXF pulito va rigenerato per QUESTO preventivo (il nome cachato
        # puntava alla cartella del preventivo che ha popolato la cache)
        payload['cleanup'] = _esegui_cleanup(dxf_path, payload.get('geometria'), filename, dxf_cfg)
        return payload

    # ---- Parsing completo ----
    try:
        pieghe, sald_ml, fil, svas = dxf_scanner.scansiona_dxf_dettagli(dxf_path, dxf_cfg)
        try:
            geo = detect_pezzo_geometry_v3(dxf_path, dxf_cfg)
            if not geo or geo.get('area_dm2', 0) == 0:
                geo = dxf_scanner.estrai_geometria_taglio(dxf_path, dxf_cfg)
        except Exception as _e:
            logger.warning('[%s] detector v3 fallito: %s', filename, _e)
            geo = dxf_scanner.estrai_geometria_taglio(dxf_path, dxf_cfg)
        cartiglio = dxf_scanner.estrai_materiale_da_cartiglio(dxf_path)
        mat_for_calc = cartiglio.get('materiale') if cartiglio.get('confidence', 0) >= 0.5 else None
        area_incerta = bool(geo and geo.get('needs_manual_select'))
        spessore = dxf_scanner.estrai_spessore_da_cartiglio(
            dxf_path,
            area_dm2=(geo or {}).get('area_dm2'),
            materiale=mat_for_calc,
            area_incerta=area_incerta,
        )
        # Se l'area del detector è inaffidabile, abbatto la confidenza
        # dello spessore (dipende dall'area) — solo per la stima peso/area.
        if area_incerta and spessore.get('source') == 'peso_area':
            spessore = {**spessore, 'confidence': min(spessore.get('confidence', 0), 0.4)}

        # Fallback cartiglio-descrizione: parsing testuale "45x12 sp.3".
        # - area/perimetro solo se detector geometrico è debole
        # - spessore anche se il detector area è OK ma spessore diretto null
        #   (es. 20PA00690: detector area OK, ma spessore diretto null e
        #   cartiglio contiene 'Sp.3' → deve popolare spessore)
        dim_info = dxf_scanner.estrai_dimensioni_da_descrizione_cartiglio(dxf_path)
        # Il contorno trovato ha proprio le misure scritte nel cartiglio: la
        # scelta non e' dubbia anche se nel foglio ci sono altre viste di
        # dimensioni simili (191700034-00: la vista isometrica la rendeva
        # "incerta" e l'area diventava il rettangolo 140x100 = 1,40 dm²
        # invece della piastra a L da 0,50).
        if geo and _misure_come_cartiglio(geo, dim_info) and geo.get('confidence', 0) < 0.75:
            geo = {**geo, 'confidence': 0.75, 'confidence_label': 'media (misure del cartiglio)',
                   'needs_manual_select': False,
                   'warnings': list(geo.get('warnings') or []) + [
                       f"Contorno confermato dalle misure del cartiglio "
                       f"({dim_info['dim_x_mm']:g} x {dim_info['dim_y_mm']:g} mm)"]}
        geo_weak = geo and (geo.get('confidence', 0) < 0.5 or geo.get('area_dm2', 0) < 0.01)
        if dim_info and dim_info.get('area_dm2') and geo_weak:
            logger.info('[%s] cartiglio fallback area: %s', filename, dim_info.get('raw_text'))
            geo = {
                **(geo or {}),
                'area_dm2': dim_info['area_dm2'],
                'perimetro_taglio_m': dim_info['perimetro_taglio_m'],
                'n_forature': max(
                    dim_info.get('n_forature', 0),
                    (geo or {}).get('n_forature', 0),
                ),
                'confidence': dim_info['confidence'],
                'confidence_label': 'media (cartiglio)',
                'needs_manual_select': False,
                '_source': 'cartiglio_descrizione',
                '_raw_text': dim_info['raw_text'],
                '_dim_x_mm': dim_info['dim_x_mm'],
                '_dim_y_mm': dim_info['dim_y_mm'],
            }
        # Spessore dal cartiglio (descrizione "sp.3" o tabella Lunghezza/Larghezza/
        # Sp.) confrontato con quello già stimato. Regola in scegli_spessore:
        # testo etichettato > peso/area > nome file > cartiglio tabellare; se
        # discordano vince il più affidabile e resta un warning con entrambi i
        # valori (prima il tabellare 1.0mm sostituiva in silenzio il 3.0 da
        # peso/area su 20R201N0401). Con area incerta peso/area va in coda.
        if dim_info and dim_info.get('spessore_mm'):
            dim_cand = {
                'spessore_mm': dim_info['spessore_mm'],
                'confidence': dim_info.get('confidence', 0) or 0,
                'source': dim_info.get('source') or 'cartiglio_descrizione',
                'warnings': [],
                'details': {'raw': dim_info.get('raw_text')},
            }
            prima = spessore.get('spessore_mm')
            spessore = dxf_scanner.scegli_spessore(
                [spessore, dim_cand], area_incerta=area_incerta)
            if spessore.get('spessore_mm') != prima:
                logger.info('[%s] spessore da cartiglio: %s mm (%s) al posto di %s',
                            filename, spessore.get('spessore_mm'), spessore.get('source'), prima)

        # ---- Auto-cleanup DXF (Fase 1a) ----
        cleaned_info = _esegui_cleanup(dxf_path, geo, filename, dxf_cfg)

        payload = {
            'success': True,
            'filename': filename,
            'lavorazioni': {
                'pieghe': pieghe, 'saldatura_ml': sald_ml,
                'filettatura_pz': fil, 'svasatura_pz': svas,
            },
            'geometria': geo,
            'cartiglio': cartiglio,
            'spessore': spessore,
            'cleanup': cleaned_info,
        }
        # Cache put (best effort). NON si mette in cache un materiale rimasto
        # vuoto solo perché Gemini non era disponibile / ha dato errore: al
        # prossimo import potrebbe essere riconosciuto.
        if cache_key and not cartiglio.get('_llm_non_disponibile'):
            try:
                dxf_cache.put(cache_key, filename, payload)
            except Exception as e:
                logger.warning('[%s] cache put fail: %s', filename, e)
        return payload
    except Exception as e:
        logger.exception('[%s] parsing fallito', filename)
        return {'success': False, 'filename': filename, 'error': str(e)}
