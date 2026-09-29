"""Verifica di coerenza di un pezzo: fonti INDIPENDENTI dal riconoscimento.

Non tocca il riconoscimento del contorno: legge soltanto e restituisce i dati
con cui il preventivatore confronta quello che ha calcolato.

- peso scritto nel cartiglio del DXF (estrai_peso_da_cartiglio);
- se nel preventivo c'e' lo STEP dello stesso pezzo ed e' una lamiera a corpo
  unico: spessore, area sviluppata, pieghe (step_piastre) e in piu' perimetro
  di taglio e inneschi.

Perimetro e inneschi dallo STEP: le facce "di bordo" (quelle non appaiate a
spessore t) sono le superfici tagliate dal laser, quindi
perimetro = area delle facce di bordo / t; ogni gruppo di facce di bordo
collegate da spigoli comuni e' un contorno chiuso (esterno, fori, asole) =
un innesco. Verificato su R4 S2808034B (3,42 m, 8 contorni = 1 + 4 asole +
3 fori) e sulle coppie DXF+STEP dell'archivio.

Studio che giustifica il controllo peso (246 lamiere DXF+STEP): quando l'area
letta e' sbagliata il peso calcolato si discosta dal cartiglio nel 99% dei
casi; quando e' giusta resta entro il 15% nel 92%.
"""
from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict

from .step_parser import carica_entita, GeometriaStep, v_norm, v_dot, v_sub, distanza_retta
from . import step_piastre as _sp

logger = logging.getLogger(__name__)

_CACHE: 'OrderedDict[tuple, dict]' = OrderedDict()
_CACHE_MAX = 256
_LOCK = threading.Lock()


def _in_cache(chiave, calcola):
    with _LOCK:
        if chiave in _CACHE:
            _CACHE.move_to_end(chiave)
            return _CACHE[chiave]
    val = calcola()
    with _LOCK:
        _CACHE[chiave] = val
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return val


def _firma(path):
    try:
        st = os.stat(path)
        return (path, st.st_mtime, st.st_size)
    except OSError:
        return (path, 0, 0)


def trova_step(cartella: str, *nomi: str) -> str | None:
    """STEP nella cartella del preventivo con lo stesso nome (senza estensione,
    maiuscole indifferenti) di uno dei `nomi` (codice pezzo, nome del DXF)."""
    chiavi = set()
    for n in nomi:
        if not n:
            continue
        b = os.path.splitext(os.path.basename(str(n)))[0].lower()
        if b.endswith('_cleaned'):
            b = b[:-len('_cleaned')]
        if b:
            chiavi.add(b)
    if not chiavi or not os.path.isdir(cartella):
        return None
    for f in os.listdir(cartella):
        b, e = os.path.splitext(f)
        if e.lower() in ('.step', '.stp') and b.lower() in chiavi:
            return os.path.join(cartella, f)
    return None


def _perimetro_e_contorni(geo, bid, spessore):
    """Facce di bordo (non appaiate a spessore t): area/t = perimetro di taglio,
    gruppi collegati = contorni chiusi. Stessa logica di appaiamento di
    step_piastre.analizza_corpo_lamiera."""
    fs = geo.facce(bid)
    piani = sorted((f for f in fs if f['tipo'] == 'PLANE' and f['area'] > 1e-6),
                   key=lambda f: f['area'], reverse=True)[:80]
    cil = [f for f in fs if f['tipo'] == 'CYL' and f['area'] > 1e-6 and f['punti']]
    tol = max(0.05, 0.02 * spessore)
    lato = set()
    for i in range(len(piani)):
        fa = piani[i]
        na = v_norm(fa['normal'])
        for fb in piani[i + 1:]:
            if v_dot(na, v_norm(fb['normal'])) > -0.995:
                continue
            d = v_dot(v_sub(fa['origin'], fb['origin']), na)
            if abs(d - spessore) > tol:
                continue
            if _sp._sovrapposizione(fa, fb) < 0.2 * min(fa['area'], fb['area']):
                continue
            lato.update((fa['id'], fb['id']))
    for i in range(len(cil)):
        ca = cil[i]
        aa = v_norm(ca['axis'])
        for cb in cil[i + 1:]:
            if abs(v_dot(aa, v_norm(cb['axis']))) < 0.9995:
                continue
            if distanza_retta(cb['origin'], ca['origin'], aa) > 0.05 + 0.002 * ca['raggio']:
                continue
            if abs(abs(ca['raggio'] - cb['raggio']) - spessore) > tol:
                continue
            z0, z1 = max(ca['z_min'], cb['z_min']), min(ca['z_max'], cb['z_max'])
            lz = min(ca['z_max'] - ca['z_min'], cb['z_max'] - cb['z_min'])
            if lz <= 0 or (z1 - z0) < 0.5 * lz:
                continue
            lato.update((ca['id'], cb['id']))
    bordi = [f for f in fs if f['id'] not in lato]
    if not bordi or spessore <= 0:
        return None, None
    padre = {f['id']: f['id'] for f in bordi}

    def radice(x):
        while padre[x] != x:
            padre[x] = padre[padre[x]]
            x = padre[x]
        return x
    per_spigolo = {}
    for f in bordi:
        for e in f.get('edges') or []:
            per_spigolo.setdefault(e, []).append(f['id'])
    for ids in per_spigolo.values():
        for k in ids[1:]:
            padre[radice(k)] = radice(ids[0])
    perim_mm = sum(f['area'] for f in bordi) / spessore
    return perim_mm / 1000.0, len({radice(f['id']) for f in bordi})


def dati_step_lamiera(step_path: str, densita: float = 7.85) -> dict | None:
    """Dati di lamiera di uno STEP a corpo unico, o None (assieme, non lamiera)."""
    def calcola():
        try:
            ent, _info = carica_entita(step_path)
            geo = GeometriaStep(ent)
            corpi = geo.corpi()
            if len(corpi) != 1:
                return {'ok': False, 'motivo': f'{len(corpi)} corpi (assieme)'}
            p = _sp.analizza_corpo_lamiera(geo, corpi[0], densita)
            if not p or 'scarto' in p:
                return {'ok': False, 'motivo': (p or {}).get('scarto') or 'non e\' una lamiera'}
            perim, contorni = _perimetro_e_contorni(geo, corpi[0], p['spessore_mm'])
            return {
                'ok': True,
                'file': os.path.basename(step_path),
                'spessore_mm': p['spessore_mm'],
                'area_dm2': p['area_dm2'],
                'n_pieghe': p['n_pieghe'],
                'perimetro_taglio_m': round(perim, 4) if perim else None,
                'inneschi': contorni,
                'avvisi': p.get('avvisi') or [],
            }
        except Exception as e:  # uno STEP illeggibile non deve rompere la verifica
            logger.warning('verifica STEP %s fallita: %s', step_path, e)
            return {'ok': False, 'motivo': 'STEP non leggibile'}
    return _in_cache(('step',) + _firma(step_path) + (densita,), calcola)


def peso_cartiglio(dxf_path: str) -> dict:
    def calcola():
        try:
            from .dxf_scanner import estrai_peso_da_cartiglio
            r = estrai_peso_da_cartiglio(dxf_path) or {}
            return {'peso_kg': r.get('peso_kg'), 'confidence': r.get('confidence') or 0.0,
                    'testo': (r.get('peso_raw') or '')[:60]}
        except Exception as e:
            logger.warning('peso cartiglio %s fallito: %s', dxf_path, e)
            return {'peso_kg': None, 'confidence': 0.0, 'testo': ''}
    return _in_cache(('peso',) + _firma(dxf_path), calcola)


def _scala_quote(doc) -> float:
    """Scala del disegno dalle quote: valore scritto / lunghezza disegnata
    (mediana delle quote lineari). 1.0 se non ci sono quote leggibili."""
    import re
    import statistics
    rapp = []
    for d in doc.modelspace().query('DIMENSION'):
        if (d.dimtype & 7) not in (0, 1):
            continue
        try:
            m = d.get_measurement()
            m = m if isinstance(m, (int, float)) else m.magnitude
        except Exception:
            continue
        blk = doc.blocks.get(d.dxf.geometry) if d.dxf.hasattr('geometry') else None
        for e in (blk or []):
            if e.dxftype() in ('MTEXT', 'TEXT'):
                s = (e.plain_text() if e.dxftype() == 'MTEXT' else e.dxf.text).replace(',', '.').strip()
                if re.fullmatch(r'\d+(\.\d+)?', s) and m > 1e-6 and float(s) > 0:
                    rapp.append(float(s) / m)
    if not rapp:
        return 1.0
    s = statistics.median(rapp)
    return s if 0.05 < s < 50 else 1.0


def proponi_contorno(dxf_path: str, spessore: float, densita: float, peso_kg: float,
                     config: dict | None = None, n_semi: int = 40) -> dict:
    """Cerca nel disegno il contorno chiuso che pesa quanto dice il cartiglio.

    Si "clicca" in automatico (pick_candidates, lo stesso strumento del clic
    nel CAD) sui tratti piu' lunghi del disegno, si raccolgono i contorni e si
    tiene quello col peso piu' vicino; lo si propone solo se sta entro il 5%.
    E' un SUGGERIMENTO: l'operatore lo vede e lo conferma. Il riconoscimento
    automatico non cambia.

    Archivio (187 pezzi letti male con peso nel cartiglio): proposta nel 56%
    dei casi, giusta 9 volte su 10 (le sbagliate: pochi punti % su pezzi piccoli).
    """
    import math
    import time
    from . import pick_part as pp
    t0 = time.time()
    if not (spessore > 0 and densita > 0 and peso_kg > 0):
        return {'trovato': False, 'motivo': 'servono spessore, materiale e peso del cartiglio'}
    cfg = config or {}
    colori = set(cfg.get('dxf_colori_piega', [2])) | set(cfg.get('dxf_colori_saldatura', [1]))
    doc = pp._leggi_dxf(dxf_path)
    segs = pp._segments_all(doc.modelspace(), colori)
    lung = sorted(segs, key=lambda s: -math.hypot(s[1][0] - s[0][0], s[1][1] - s[0][1]))
    semi, visti = [], []
    for s in lung:
        mx, my = (s[0][0] + s[1][0]) / 2, (s[0][1] + s[1][1]) / 2
        if any(math.hypot(mx - a, my - b) < 5 for a, b in visti):
            continue
        visti.append((mx, my))
        semi.append((mx, my))
        if len(semi) >= n_semi:
            break
    candidati = {}
    for x, y in semi:
        try:
            r = pp.pick_candidates(dxf_path, x, y, cfg)
        except Exception:
            continue
        for c in (r.get('candidates') or []):
            a = c.get('area_dm2') or 0
            if a > 0 and c.get('outer_xy'):
                candidati.setdefault(round(a, 3), c)
    scala = _scala_quote(doc)
    migliore, err, s_usata = None, None, 1.0
    for c in candidati.values():
        for s in {1.0, scala}:
            peso = c['area_dm2'] * s * s * spessore / 100 * densita
            e = abs(peso / peso_kg - 1)
            if err is None or e < err:
                migliore, err, s_usata = c, e, s
    out = {'trovato': False, 'candidati': len(candidati), 'secondi': round(time.time() - t0, 1)}
    if migliore is None or err > 0.05:
        out['motivo'] = 'nessun contorno del disegno ha il peso del cartiglio'
        return out
    # forma semplificata per la miniatura (coordinate del disegno)
    try:
        from shapely.geometry import Polygon
        poly = Polygon(migliore['outer_xy'])
        tol = max(poly.bounds[2] - poly.bounds[0], poly.bounds[3] - poly.bounds[1]) / 600.0
        esterno = [[round(x, 2), round(y, 2)] for x, y in poly.simplify(tol).exterior.coords]
        fori = []
        for h in (migliore.get('holes_xy') or [])[:120]:
            hp = Polygon(h).simplify(tol)
            fori.append([[round(x, 2), round(y, 2)] for x, y in hp.exterior.coords])
    except Exception:
        esterno, fori = migliore['outer_xy'], migliore.get('holes_xy') or []
    k = s_usata
    out.update({
        'trovato': True,
        'area_dm2': round(migliore['area_dm2'] * k * k, 4),
        'perimetro_taglio_m': round((migliore.get('perimetro_taglio_m') or 0) * k, 4),
        'n_forature': migliore.get('n_forature'),
        'bbox_width_mm': round((migliore.get('bbox_width_mm') or 0) * k, 1),
        'bbox_height_mm': round((migliore.get('bbox_height_mm') or 0) * k, 1),
        'peso_kg': round(migliore['area_dm2'] * k * k * spessore / 100 * densita, 4),
        'peso_cartiglio_kg': peso_kg,
        'scarto_peso': round(err, 4),
        'scala': round(k, 3),
        'esterno': esterno,
        'fori': fori,
    })
    return out


def quote_disegno(dxf_path: str) -> list:
    """Valori delle quote lineari del disegno (testo scritto, mm veri), unici."""
    def calcola():
        import re
        try:
            from .pick_part import _leggi_dxf
            doc = _leggi_dxf(dxf_path)
        except Exception:
            return []
        val = set()
        for d in doc.modelspace().query('DIMENSION'):
            if (d.dimtype & 7) not in (0, 1):          # lineari e allineate
                continue
            blk = doc.blocks.get(d.dxf.geometry) if d.dxf.hasattr('geometry') else None
            for e in (blk or []):
                if e.dxftype() in ('MTEXT', 'TEXT'):
                    s = (e.plain_text() if e.dxftype() == 'MTEXT' else e.dxf.text).replace(',', '.').strip()
                    if re.fullmatch(r'\d+(\.\d+)?', s) and 0 < float(s) < 1e5:
                        val.add(round(float(s), 2))
        return sorted(val)[:300]
    return _in_cache(('quote',) + _firma(dxf_path), calcola)


def ingombro_letto(dxf_path: str, config: dict | None) -> dict | None:
    """Area e ingombro del contorno che l'import sceglie da solo (stessa
    funzione e stessa configurazione, in sola lettura)."""
    def calcola():
        try:
            from .dxf_scanner import estrai_geometria_taglio
            g = estrai_geometria_taglio(dxf_path, config or {})
            if not (g.get('area_dm2') or 0) > 0:
                return None
            return {'area_dm2': round(g['area_dm2'], 4), 'bbox_w_mm': round(g.get('bbox_width_mm') or 0, 2),
                    'bbox_h_mm': round(g.get('bbox_height_mm') or 0, 2)}
        except Exception:
            return None
    return _in_cache(('letto',) + _firma(dxf_path), calcola)


def verifica_pezzo(cartella: str, dxf_filename: str | None, codice: str | None,
                   config: dict | None = None) -> dict:
    """Dati indipendenti per il controllo di coerenza di un pezzo."""
    out = {'versione': 2, 'peso_cartiglio': None, 'step': None, 'quote': [], 'ingombro_letto': None}
    dxf_path = os.path.join(cartella, os.path.basename(dxf_filename)) if dxf_filename else None
    if dxf_path and os.path.exists(dxf_path):
        out['peso_cartiglio'] = peso_cartiglio(dxf_path)
        out['quote'] = quote_disegno(dxf_path)
        if out['quote']:
            out['ingombro_letto'] = ingombro_letto(dxf_path, config)
    stp = trova_step(cartella, codice, dxf_filename)
    if stp:
        out['step'] = dati_step_lamiera(stp)
    return out
