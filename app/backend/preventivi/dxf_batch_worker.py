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
import math
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
                cleaned_info['cleanup_stats'] = _stats_pulizia(r)
                logger.info('[%s] cleanup auto ok: %d/%d entità (%s)',
                            filename, r['entities_copied'], r['entities_source'],
                            cleaned_info['cleaned_status'])
            else:
                cleaned_info['cleanup_reason'] = f"pulizia non eseguita: {r.get('error')}"
                logger.info('[%s] cleanup fallito: %s', filename, r.get('error'))
    except Exception as e:
        logger.warning('[%s] cleanup pipeline error: %s', filename, e)
    return cleaned_info


def _stats_pulizia(r: dict) -> dict:
    return {
        'entities_copied': r['entities_copied'],
        'entities_source': r['entities_source'],
        'tolerance_mm': r['tolerance_mm'],
        'bbox_w_mm': r.get('w_mm'),
        'bbox_h_mm': r.get('h_mm'),
        'warnings': r.get('warnings') or [],
        **{k: r[k] for k in ('n_taglio', 'n_piega', 'n_marcatura', 'n_simboli_tolti',
                             'copertura', 'n_lung_marcatura_mm', 'n_fori_trapano',
                             'n_trapano_tolti') if k in r},
    }


def _decisione_modello(dxf_path, geo, cartiglio, spessore, dim_info, cleaned_info, conf_detector,
                       dxf_cfg, filename):
    """Modello addestrato del "sicuro" (modello_motore), solo se attivo
    (config 'motore_modello' / FT_MOTORE_MODELLO = 'dump' o 'on').

    - Se la pulizia non e' partita (scelta incerta) la si prova lo stesso "in
      ombra" per avere gli stessi indizi dei disegni puliti.
    - 'dump': calcola feature e probabilita', non cambia niente.
    - 'on': sicuro = probabilita' >= soglia del modello E nessuna regola fissa
      contraria (fori trapano dubbi, contorni stretti, foglio intero, pulizia
      fallita, contorno cambiato dal ranker, misure dalla descrizione)."""
    from . import modello_motore as mm
    md = mm.modo(dxf_cfg)
    if md == 'off' or not geo:
        return geo, cleaned_info
    info = dict(geo.get('_modello') or {})
    try:
        from . import dxf_cleanup
        from . import sicurezza_import as si
        ombra = None
        ind = dict((cleaned_info or {}).get('indizi') or {})
        pulito = bool(cleaned_info.get('cleaned_dxf_filename'))
        motivo = str(cleaned_info.get('cleanup_reason') or '')
        descr = geo.get('_source') == 'cartiglio_descrizione'
        if not pulito and not descr and float(geo.get('area_dm2') or 0) > 0 \
                and not motivo.startswith('pulizia non eseguita'):
            base_p, ext_p = os.path.splitext(dxf_path)
            ombra = base_p + '_cleaned' + ext_p
            r = dxf_cleanup.save_cleaned_dxf_pezzo(dxf_path, ombra, geo, dxf_cfg)
            if r.get('success'):
                st = _stats_pulizia(r)
                ci = {**cleaned_info, 'cleanup_stats': st}
                ind = si.raccogli_indizi(dxf_path, geo, cartiglio, spessore, dim_info, ci, conf_detector)
                info['ombra_stats'] = st
            else:
                if os.path.exists(ombra):
                    os.remove(ombra)
                ombra = None
        x = mm.feature_disegno(geo, ind, pulito or ombra is not None, info.get('rank'))
        p = mm.prob_sicuro(x)
        info['p_sicuro'] = p
        if p is not None:
            info['p_calibrata'] = round(mm.prob_sicuro(x, calibrata=True), 4)
        fisse = []
        if ind.get('fori_trapano_dubbio'):
            fisse.append('fori piccoli: spessore da confermare per decidere laser o trapano')
        if ind.get('fori_stretti_non_tondi'):
            fisse.append('contorni non tondi sotto il minimo laser')
        if descr:
            fisse.append('misure dalla descrizione del cartiglio')
        if motivo.startswith('area_ratio') or motivo.startswith('no detector'):
            fisse.append(motivo)
        if not pulito and ombra is None:
            fisse.append('pulizia non riuscita')
        if info.get('cambiato'):
            fisse.append('contorno cambiato dal modello')
        if not ind or ind.get('errore'):
            fisse.append('indizi non calcolati')
        info['fisse'] = fisse
        if md == 'dump':
            info['xd'] = x
            info['pulito_base'] = pulito
            if ombra and os.path.exists(ombra):
                os.remove(ombra)
            info.pop('ombra_stats', None)
            return {**geo, '_modello': info}, cleaned_info
        sicuro = p is not None and p >= mm.soglia_sicuro() and not fisse
        if sicuro:
            if not pulito:
                cleaned_info = {**cleaned_info, 'cleaned_dxf_filename': os.path.basename(ombra),
                                'cleanup_stats': info.get('ombra_stats'), 'indizi': ind}
            cleaned_info = {**cleaned_info, 'cleaned_status': 'auto',
                            'cleanup_reason': f"sicuro per il modello (probabilita' {p:.3f})"}
            conf = max(float(geo.get('confidence') or 0), 0.75)
            geo = {**geo, 'confidence': conf, 'needs_manual_select': False,
                   'confidence_label': 'alta (modello)' if conf >= 0.85 else 'media (modello)'}
        else:
            if ombra and os.path.exists(ombra):
                os.remove(ombra)
            if cleaned_info.get('cleaned_status') == 'auto':
                testo = '; '.join(fisse) or f"probabilita' del modello {p if p is not None else 0:.3f}"
                geo = {**geo, 'confidence': min(float(geo.get('confidence') or 0), si.CONF_DA_VERIFICARE),
                       'confidence_label': 'media (da verificare)',
                       'warnings': list(geo.get('warnings') or []) + [f'Da verificare: {testo}']}
                cleaned_info = {**cleaned_info, 'cleaned_status': 'auto_review',
                                'cleanup_reason': f'da verificare: {testo}'}
        info.pop('ombra_stats', None)
        return {**geo, '_modello': info}, cleaned_info
    except Exception as e:      # noqa: BLE001 - il modello non deve rompere l'import
        logger.warning('[%s] modello sicuro: %s', filename, e)
        return geo, cleaned_info


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


def _controllo_sicuro(dxf_path, geo, cartiglio, spessore, dim_info, cleaned_info, conf_detector):
    """Un disegno con pulizia 'auto' resta SICURO solo se gli indizi
    indipendenti (sicurezza_import.decidi_sicuro) tornano; altrimenti la
    confidenza scende sotto 0,7 e la pulizia diventa 'auto_review'
    (da verificare). Il contorno non cambia."""
    try:
        from . import sicurezza_import as si
        ind = si.raccogli_indizi(dxf_path, geo, cartiglio, spessore, dim_info, cleaned_info, conf_detector)
        cleaned_info = {**cleaned_info, 'indizi': ind}
        if cleaned_info.get('cleaned_status') != 'auto' or not geo:
            return geo, cleaned_info
        ok, motivi = si.decidi_sicuro(ind)
        if ok:
            return geo, cleaned_info
        testo = '; '.join(motivi)
        geo = {**geo, 'confidence': min(float(geo.get('confidence') or 0), si.CONF_DA_VERIFICARE),
               'confidence_label': 'media (da verificare)',
               'warnings': list(geo.get('warnings') or []) + [f'Da verificare: {testo}']}
        cleaned_info = {**cleaned_info, 'cleaned_status': 'auto_review',
                        'cleanup_reason': f"da verificare: {testo}"}
    except Exception as e:      # noqa: BLE001 - il controllo non deve rompere l'import
        logger.warning('controllo sicuro %s: %s', dxf_path, e)
    return geo, cleaned_info


def _descrizione_plausibile(dxf_path: str, dim_info: dict) -> bool:
    """Il rettangolo Lunghezza x Larghezza letto nel cartiglio descrive il
    pezzo disegnato? Entrambe le misure devono comparire come QUOTE del
    disegno: il disegnatore quota lo sviluppo che descrive. Altrimenti e' un
    altro numero del cartiglio (115 dm2 su un pezzo di 48 x 58)."""
    try:
        import ezdxf
        from .dxf_polygon_detector_v3 import scala_unita_mm, _quote_mm
        dx = float(dim_info.get('dim_x_mm') or 0)
        dy = float(dim_info.get('dim_y_mm') or 0)
        if dx <= 0 or dy <= 0:
            return False
        doc = ezdxf.readfile(dxf_path)
        msp = doc.modelspace()
        f, _ = scala_unita_mm(doc)
        vals = {v for q in _quote_mm(msp, f) for v in q['vals']}
        if not all(any(abs(v - d) <= 0.15 for v in vals) for d in (dx, dy)):
            return False
        return True
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════
# Fori da trapano. Regola di Stefano (08/10/2026): il laser taglia un foro
# solo se il diametro e' almeno 2/3 dello spessore, arrotondato per DIFETTO al
# millimetro (10 mm -> Ø6, 12 -> Ø8, 8 -> Ø5, 5 -> Ø3, 3 -> Ø2); i fori piu'
# piccoli si fanno dopo al trapano. I fori filettati si tagliano al laser al
# diametro del preforo (il cerchio intero disegnato) e poi si filettano.
# ═══════════════════════════════════════════════════════════════════
FORO_LASER_SU_SPESSORE = 2.0 / 3.0      # diametro minimo tagliabile / spessore
CONF_SPESSORE_TRAPANO = 0.7             # spessore abbastanza sicuro per decidere
TOLL_DIAMETRO_MM = 0.05                 # un Ø6 disegnato 5,98 resta un Ø6


def diametro_min_laser(spessore_mm) -> int | None:
    """Diametro minimo (mm interi) che il laser taglia su questo spessore."""
    try:
        t = float(spessore_mm)
    except (TypeError, ValueError):
        return None
    if not t or t <= 0:
        return None
    return int(math.floor(FORO_LASER_SU_SPESSORE * t + 1e-6))


def fori_sotto_soglia(fori_tondi: list, spessore_mm) -> list:
    """I fori tondi (geometria['fori_tondi']) che su questo spessore vanno al trapano."""
    dmin = diametro_min_laser(spessore_mm)
    if not dmin:
        return []
    return [f for f in (fori_tondi or []) if float(f.get('d_mm') or 0) < dmin - TOLL_DIAMETRO_MM]


def applica_fori_trapano(geo: dict | None, spessore: dict | None) -> dict | None:
    """Con lo spessore sicuro (>= 0,7) toglie dal taglio i fori tondi sotto i
    2/3 dello spessore: area += area del foro, perimetro -= circonferenza, un
    innesco e un foro in meno; li elenca in geometria['fori_trapano'].
    Con lo spessore incerto non cambia la geometria: se per almeno una delle
    letture dello spessore un foro andrebbe al trapano, lo segna in
    geometria['fori_trapano_dubbio'] (il controllo del sicuro lo manda in
    revisione). I contorni piccoli NON tondi restano al laser, ma se sono piu'
    stretti del minimo laser (per una lettura dello spessore) il pezzo va
    verificato: geometria['fori_stretti_non_tondi'] = quanti."""
    if not geo or geo.get('_source') == 'cartiglio_descrizione'             or not (geo.get('fori_tondi') or geo.get('fori_non_tondi_mm')):
        return geo
    sp = spessore or {}
    t = sp.get('spessore_mm')
    conf = float(sp.get('confidence') or 0)
    fori = geo.get('fori_tondi') or []
    stretti = geo.get('fori_non_tondi_mm') or []
    if t and conf >= CONF_SPESSORE_TRAPANO:
        dmin = diametro_min_laser(t)
        n_stretti = sum(1 for w in stretti if dmin and w < dmin - TOLL_DIAMETRO_MM)
        if n_stretti:
            # contorno non tondo piu' stretto del minimo laser: niente trapano,
            # e il laser non lo taglia bene: lo decide una persona
            geo = {**geo, 'fori_stretti_non_tondi': n_stretti}
        trapano = fori_sotto_soglia(fori, t)
        if not trapano:
            return geo
        a_mm2 = sum(float(f.get('area_mm2') or 0) for f in trapano)
        p_mm = sum(float(f.get('perim_mm') or 0) for f in trapano)
        n = len(trapano)
        geo = dict(geo)
        geo['area_dm2'] = round(float(geo.get('area_dm2') or 0) + a_mm2 / 10000.0, 4)
        geo['perimetro_taglio_m'] = round(max(0.0, float(geo.get('perimetro_taglio_m') or 0) - p_mm / 1000.0), 4)
        for k in ('n_pierce', 'n_forature', 'n_fori', 'n_inner'):
            if isinstance(geo.get(k), int):
                geo[k] = max(0, geo[k] - n)
        geo['fori_trapano'] = [{'d_mm': round(float(f['d_mm']), 2), 'x': f['x'], 'y': f['y']} for f in trapano]
        diam = sorted({round(float(f['d_mm']), 1) for f in trapano})
        geo['warnings'] = list(geo.get('warnings') or []) + [
            f"{n} fori Ø{'/'.join(f'{d:g}' for d in diam)} da fare al trapano "
            f"(sotto 2/3 dello spessore {float(t):g} mm: il laser taglia da Ø{diametro_min_laser(t)})"]
        return geo
    # spessore incerto o mancante: le letture possibili
    letture = {float(x['spessore_mm']) for x in (sp.get('fonti') or []) if x and x.get('spessore_mm')}
    if t:
        letture.add(float(t))
    if not letture:
        letture = set(SPESSORI_PLAUSIBILI_SENZA_LETTURA)
    if any(fori_sotto_soglia(fori, x) for x in letture):
        geo = {**geo, 'fori_trapano_dubbio': True}
    n_stretti = max((sum(1 for w in stretti if w < (diametro_min_laser(x) or 0) - TOLL_DIAMETRO_MM)
                     for x in letture), default=0)
    if n_stretti:
        geo = {**geo, 'fori_stretti_non_tondi': n_stretti}
    return geo


# Nessuna lettura dello spessore: i fori che andrebbero al trapano gia' su uno
# spessore comune (fino a 3 mm) lasciano il dubbio laser/trapano.
SPESSORI_PLAUSIBILI_SENZA_LETTURA = (3.0,)


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
        # Descrizione "45x12 sp.3" / cartiglio tabellare: misure e spessore
        dim_info = dxf_scanner.estrai_dimensioni_da_descrizione_cartiglio(dxf_path)
        altre = []
        if dim_info and dim_info.get('spessore_mm'):
            altre.append({
                'spessore_mm': dim_info['spessore_mm'],
                'confidence': dim_info.get('confidence', 0) or 0,
                'source': dim_info.get('source') or 'cartiglio_descrizione',
                'warnings': [],
                'details': {'raw': dim_info.get('raw_text')},
            })
        # Fallback cartiglio-descrizione: parsing testuale "45x12 sp.3".
        # - area/perimetro solo se detector geometrico è debole
        # - spessore anche se il detector area è OK ma spessore diretto null
        #   (es. 20PA00690: detector area OK, ma spessore diretto null e
        #   cartiglio contiene 'Sp.3' → deve popolare spessore)
        # Il contorno trovato ha proprio le misure scritte nel cartiglio: la
        # scelta non e' dubbia anche se nel foglio ci sono altre viste di
        # dimensioni simili (191700034-00: la vista isometrica la rendeva
        # "incerta" e l'area diventava il rettangolo 140x100 = 1,40 dm²
        # invece della piastra a L da 0,50).
        # Con i fori dubbi (scritte forse tagliate, fori spezzati) il cartiglio
        # conferma il contorno ma non cosa si taglia dentro: confidenza sotto
        # la soglia del "sicuro", senza perdere il contorno trovato.
        conf_detector = (geo or {}).get('confidence')
        if geo and _misure_come_cartiglio(geo, dim_info) and geo.get('confidence', 0) < 0.75:
            c_conf = 0.65 if (geo.get('fori_dubbi') or geo.get('dubbi_fori')) else 0.75
            geo = {**geo, 'confidence': max(c_conf, geo.get('confidence', 0) or 0),
                   'confidence_label': 'media (misure del cartiglio)',
                   'needs_manual_select': False,
                   'warnings': list(geo.get('warnings') or []) + [
                       f"Contorno confermato dalle misure del cartiglio "
                       f"({dim_info['dim_x_mm']:g} x {dim_info['dim_y_mm']:g} mm)"]}
        geo_weak = geo and (geo.get('confidence', 0) < 0.5 or geo.get('area_dm2', 0) < 0.01)
        if dim_info and dim_info.get('area_dm2') and geo_weak \
                and not _descrizione_plausibile(dxf_path, dim_info):
            # Le misure scritte nel cartiglio non sono quote del disegno: area
            # non credibile (archivio: cosi' era giusta 1 volta su 119)
            geo = {**geo, 'warnings': list(geo.get('warnings') or []) + [
                f"Misure {dim_info.get('dim_x_mm'):g} x {dim_info.get('dim_y_mm'):g} lette nel cartiglio "
                f"ma non quotate nel disegno: non usate per l'area"]}
            dim_info = {**dim_info, 'area_dm2': None}
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
        # Tutte le fonti di spessore insieme (testo, cella, piatto, vista
        # laterale, peso/area, nome file, descrizione/tabellare): scegli_spessore
        # le confronta, concordi = sicuro, discordi = da confermare. Con area
        # incerta peso/area non vota. Calcolato DOPO la conferma del contorno con le
        # misure del cartiglio: un contorno confermato non e' piu' incerto.
        area_incerta = bool(geo and geo.get('needs_manual_select'))
        spessore = dxf_scanner.estrai_spessore_da_cartiglio(
            dxf_path,
            area_dm2=(geo or {}).get('area_dm2'),
            materiale=mat_for_calc,
            area_incerta=area_incerta,
            vista=(geo or {}).get('spessore_vista'),
            altre_fonti=altre,
        )

        # ---- Fori da trapano (servono lo spessore): fuori dal taglio ----
        try:
            geo = applica_fori_trapano(geo, spessore)
        except Exception as e:      # noqa: BLE001
            logger.warning('[%s] fori da trapano: %s', filename, e)

        # ---- Auto-cleanup DXF (Fase 1a) ----
        cleaned_info = _esegui_cleanup(dxf_path, geo, filename, dxf_cfg)
        geo, cleaned_info = _controllo_sicuro(dxf_path, geo, cartiglio, spessore, dim_info,
                                              cleaned_info, conf_detector)
        geo, cleaned_info = _decisione_modello(dxf_path, geo, cartiglio, spessore, dim_info,
                                               cleaned_info, conf_detector, dxf_cfg, filename)

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
