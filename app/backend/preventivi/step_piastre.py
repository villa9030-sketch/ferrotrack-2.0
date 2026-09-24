"""STEP file plate (piastre) analysis.

Identifica i corpi in lamiera (spessore uniforme): piastre piane, gusset,
lamiere piegate (sviluppo) e calandrate. Per ogni corpo calcola spessore,
area sviluppata e peso.

Metodo (geometria condivisa in step_parser, coordinate gia' in mm):
- spessore = la MINIMA distanza tra coppie di facce antiparallele con
  materiale in mezzo, che si sovrappongono in proiezione e coprono una parte
  consistente dell'area (non la coppia di area massima: una U con ali vicine
  dava come spessore la luce interna);
- facce "di lato" = facce piane in coppia a distanza t + cilindri coassiali
  con raggi che differiscono di t (pieghe/calandrature);
- area sviluppata = (somma aree facce di lato) / 2  (= (area totale - area
  dei bordi sottili) / 2, fibra neutra a meta' spessore). Le aree dei piani
  sono esatte (poligono dagli EDGE_LOOP ordinati, fori sottratti), quelle dei
  cilindri r*theta*L;
- peso dal volume B-rep (teorema della divergenza) quando calcolabile,
  altrimenti area x spessore x densita'.
"""

import logging
import math

from .step_parser import (
    carica_entita, GeometriaStep, v_dot, v_sub, v_norm, v_cross, v_len,
    punto_in_poligono_2d, distanza_retta,
)

logger = logging.getLogger(__name__)

SPESSORE_MAX_MM = 100.0


def _loops_2d(f, o, u, v):
    out = []
    for lp in f['loops']:
        pts = lp['punti']
        if len(pts) >= 3:
            out.append([(v_dot(v_sub(p, o), u), v_dot(v_sub(p, o), v)) for p in pts])
    return out


def _sovrapposizione(fa, fb, griglia=16):
    """Area (mm2) della sovrapposizione in proiezione di due facce piane."""
    o, u, v = fa['origin'], fa['u'], fa['v']
    la = _loops_2d(fa, o, u, v)
    lb = _loops_2d(fb, o, u, v)
    if not la or not lb:
        return 0.0
    ua = [p[0] for p in la[0]]
    va = [p[1] for p in la[0]]
    ub = [p[0] for p in lb[0]]
    vb = [p[1] for p in lb[0]]
    u0, u1 = max(min(ua), min(ub)), min(max(ua), max(ub))
    v0, v1 = max(min(va), min(vb)), min(max(va), max(vb))
    if u1 <= u0 or v1 <= v0:
        return 0.0
    box = (u1 - u0) * (v1 - v0)
    if box < 0.2 * min(fa['area'], fb['area']):
        return box  # sicuramente sotto soglia, inutile campionare
    dentro = 0
    for i in range(griglia):
        for j in range(griglia):
            p = (u0 + (i + 0.5) * (u1 - u0) / griglia, v0 + (j + 0.5) * (v1 - v0) / griglia)
            if punto_in_poligono_2d(p, la, tol=0) and punto_in_poligono_2d(p, lb, tol=0):
                dentro += 1
    return box * dentro / float(griglia * griglia)


def _dimensioni_faccia(f):
    """Larghezza x altezza della faccia nel suo piano, orientate sul lato
    rettilineo piu' lungo del contorno (non sull'asse X mondo)."""
    pts = f['loops'][0]['punti'] if f['loops'] else f['punti']
    if len(pts) < 2:
        return 0.0, 0.0
    best, u = 0.0, f['u']
    for i in range(len(pts)):
        d = v_sub(pts[(i + 1) % len(pts)], pts[i])
        ln = v_len(d)
        if ln > best:
            best, u = ln, v_norm(d)
    v = v_norm(v_cross(f['normal'], u))
    us = [v_dot(p, u) for p in pts]
    vs = [v_dot(p, v) for p in pts]
    w, h = max(us) - min(us), max(vs) - min(vs)
    return max(w, h), min(w, h)


def analizza_corpo_lamiera(geo, bid, densita=7.85):
    """Analizza un corpo: ritorna dict piastra, {'scarto': motivo} o None."""
    fs = geo.facce(bid)
    if not fs:
        return None
    area_tot = sum(f['area'] for f in fs)
    if area_tot <= 0:
        return None
    piani = sorted((f for f in fs if f['tipo'] == 'PLANE' and f['area'] > 1e-6),
                   key=lambda f: f['area'], reverse=True)[:80]
    cil = [f for f in fs if f['tipo'] == 'CYL' and f['area'] > 1e-6 and f['punti']]

    # --- coppie di facce opposte con materiale in mezzo ---
    coppie = []  # (d, id_a, id_b, area_coperta)
    for i in range(len(piani)):
        fa = piani[i]
        na = v_norm(fa['normal'])
        for j in range(i + 1, len(piani)):
            fb = piani[j]
            nb = v_norm(fb['normal'])
            if v_dot(na, nb) > -0.995:
                continue
            d = v_dot(v_sub(fa['origin'], fb['origin']), na)
            if d < 0.2 or d > SPESSORE_MAX_MM + 0.5:
                continue
            ov = _sovrapposizione(fa, fb)
            if ov < 0.2 * min(fa['area'], fb['area']):
                continue
            coppie.append((d, fa['id'], fb['id'], ov))
    for i in range(len(cil)):
        ca = cil[i]
        aa = v_norm(ca['axis'])
        for j in range(i + 1, len(cil)):
            cb = cil[j]
            if abs(v_dot(aa, v_norm(cb['axis']))) < 0.9995:
                continue
            if distanza_retta(cb['origin'], ca['origin'], aa) > 0.05 + 0.002 * ca['raggio']:
                continue
            d = abs(ca['raggio'] - cb['raggio'])
            if d < 0.2 or d > SPESSORE_MAX_MM + 0.5:
                continue
            z0 = max(ca['z_min'], cb['z_min'])
            z1 = min(ca['z_max'], cb['z_max'])
            lz = min(ca['z_max'] - ca['z_min'], cb['z_max'] - cb['z_min'])
            if lz <= 0 or (z1 - z0) < 0.5 * lz:
                continue
            coppie.append((d, ca['id'], cb['id'], min(ca['area'], cb['area'])))
    if not coppie:
        return None

    # --- spessore: la distanza MINIMA che copre una parte consistente dell'area ---
    def _tol(d):
        return max(0.05, 0.02 * d)
    spessore = None
    for d in sorted({round(c[0], 2) for c in coppie}):
        cov = sum(c[3] for c in coppie if abs(c[0] - d) <= _tol(d))
        if cov >= 0.15 * area_tot:
            spessore = d
            break
    if spessore is None:
        return {'scarto': 'nessuna coppia di facce dominante'}
    in_t = [c for c in coppie if abs(c[0] - spessore) <= _tol(spessore)]
    spessore = sum(c[0] * c[3] for c in in_t) / max(sum(c[3] for c in in_t), 1e-9)
    ids_lato = {c[1] for c in in_t} | {c[2] for c in in_t}
    per_id = {f['id']: f for f in fs}
    area_lati = sum(per_id[i]['area'] for i in ids_lato)
    area_bordi = area_tot - area_lati
    if area_lati < area_bordi:
        return {'scarto': f'facce di spessore {spessore:.1f}mm non dominanti (blocco pieno?)'}

    # --- corpo cavo tipo tubo (non riconosciuto dai tubolari): non e' lamiera ---
    if geo.genere_corpo(bid) >= 1:
        dirs = []
        for i in ids_lato:
            f = per_id[i]
            dirs.append(('P', v_norm(f['normal'])) if f['tipo'] == 'PLANE' else ('C', v_norm(f['axis'])))
        assi = [d for k, d in dirs if k == 'C']
        normali = [d for k, d in dirs if k == 'P']
        for n1 in normali:
            for n2 in normali:
                c = v_cross(n1, n2)
                if v_len(c) > 0.5:
                    assi.append(v_norm(c))
                    break
            if len(assi) > 0:
                break
        pts = geo.punti_corpo(bid)
        for a in assi[:1]:
            ok = all(abs(v_dot(d, a)) < 0.05 if k == 'P' else abs(v_dot(d, a)) > 0.999
                     for k, d in dirs)
            if ok and pts:
                pr = [v_dot(p, a) for p in pts]
                u = v_norm(v_cross(a, (1.0, 0.0, 0.0) if abs(a[0]) < 0.9 else (0.0, 1.0, 0.0)))
                w = v_cross(a, u)
                sez = max(max(v_dot(p, u) for p in pts) - min(v_dot(p, u) for p in pts),
                          max(v_dot(p, w) for p in pts) - min(v_dot(p, w) for p in pts))
                if max(pr) - min(pr) >= 1.5 * sez:
                    return {'scarto': 'corpo cavo tipo tubo non riconosciuto come profilo'}

    # --- pieghe: coppie di cilindri coassiali a distanza t (una per piega) ---
    assi_piega = []
    for c in in_t:
        fa = per_id[c[1]]
        if fa['tipo'] != 'CYL':
            continue
        fb = per_id[c[2]]
        if max(fa.get('copertura', 0), fb.get('copertura', 0)) >= 2 * math.pi * 0.97:
            continue  # calandrato a 360: non e' una piega
        a = v_norm(fa['axis'])
        if not any(abs(v_dot(a, q[1])) > 0.9995 and distanza_retta(fa['origin'], q[0], q[1]) < 0.1
                   for q in assi_piega):
            assi_piega.append((fa['origin'], a))
    n_pieghe = len(assi_piega)

    area_mm2 = area_lati / 2.0
    avvisi = []
    volume = geo.volume_corpo(bid)
    if volume and volume > 0:
        t_eq = volume / max(area_mm2, 1e-9)
        if abs(t_eq / spessore - 1.0) > 0.15:
            avvisi.append(f'area_da_volume: sviluppo incoerente col volume '
                          f'(sp. equivalente {t_eq:.2f} vs {spessore:.2f}mm)')
            area_mm2 = volume / spessore
        peso_kg = volume * densita * 1e-6
    else:
        peso_kg = area_mm2 * spessore * densita * 1e-6

    f_max = max((per_id[i] for i in ids_lato if per_id[i]['tipo'] == 'PLANE'),
                key=lambda f: f['area'], default=None)
    larghezza, altezza = _dimensioni_faccia(f_max) if f_max else (0.0, 0.0)
    all_pts = geo.punti_corpo(bid)
    bbox_min = tuple(min(p[k] for p in all_pts) for k in range(3)) if all_pts else (0, 0, 0)
    bbox_max = tuple(max(p[k] for p in all_pts) for k in range(3)) if all_pts else (0, 0, 0)
    return {
        'spessore_mm': round(spessore, 1),
        'area_dm2': round(area_mm2 / 10000.0, 3),
        'larghezza_mm': round(larghezza, 1),
        'altezza_mm': round(altezza, 1),
        'peso_kg': round(peso_kg, 2),
        'n_pieghe': n_pieghe,
        'sviluppo': n_pieghe > 0,
        'volume_mm3': round(volume, 1) if volume else None,
        'body_id': bid,
        'bbox_min': bbox_min,
        'bbox_max': bbox_max,
        'avvisi': avvisi,
    }


def analizza_step_piastre(step_path: str, densita: float = 7.85,
                          escludi_body_ids=None) -> dict:
    """Analizza file STEP per rilevare piastre / lamiere (anche piegate).

    Args:
        step_path: Path al file STEP.
        densita: Densita' materiale in kg/dm3 (default: 7.85 per acciaio).
        escludi_body_ids: id dei corpi gia' riconosciuti come tubolari
            (step_tubolari 'body_ids_tubi'): non vanno contati anche come piastre.

    Returns:
        dict con 'piastre': lista (spessore_mm, area_dm2, peso_kg, n_pieghe,
        sviluppo, body_id, ...), 'peso_totale_kg', 'avvisi', 'errore'.
        Valori per UNA istanza di ciascun corpo.
    """
    try:
        entities, info = carica_entita(step_path)
    except (IOError, OSError) as e:
        return {'piastre': [], 'peso_totale_kg': 0, 'avvisi': [], 'errore': str(e)}

    geo = GeometriaStep(entities)
    body_ids = geo.corpi()
    avvisi = list(info['avvisi'])
    if not body_ids:
        return {'piastre': [], 'peso_totale_kg': 0, 'avvisi': avvisi,
                'errore': 'Nessun corpo solido trovato'}

    escludi = set(escludi_body_ids or [])
    piastre_rilevate = []
    for bid in body_ids:
        if bid in escludi:
            continue
        try:
            p = analizza_corpo_lamiera(geo, bid, densita)
        except Exception as e:  # un corpo anomalo non deve bloccare l'import
            logger.warning("analisi piastra corpo #%s fallita: %s", bid, e)
            continue
        if p is None:
            continue
        if 'scarto' in p:
            avvisi.append(f"corpo #{bid} non quotato come piastra: {p['scarto']}")
            continue
        piastre_rilevate.append(p)

    peso_totale = round(sum(p['peso_kg'] for p in piastre_rilevate), 2)
    return {
        'piastre': piastre_rilevate,
        'peso_totale_kg': peso_totale,
        'avvisi': avvisi,
        'unita': info['unita'],
        'errore': None if piastre_rilevate else 'Nessuna piastra rilevata'
    }


def calcola_costo_piastre(analisi: dict, config: dict, materiale: str = "acciaio") -> dict:
    """Calcola il costo delle piastre rilevate.

    Args:
        analisi: dict da analizza_step_piastre().
        config: dict di configurazione applicazione.
        materiale: 'acciaio', 'inox', 'alluminio'.

    Returns:
        dict con breakdown costi per piastra.
    """
    tabella = config.get('prezzo_dm2', {})
    prezzi_mat = tabella.get(materiale, tabella.get('acciaio', {}))

    # Converti chiavi stringa a float per matching
    spessori_disponibili = []
    for k, v in prezzi_mat.items():
        try:
            spessori_disponibili.append((float(k), v))
        except ValueError:
            continue
    spessori_disponibili.sort(key=lambda x: x[0])

    def _find_prezzo(spessore_mm):
        if not spessori_disponibili:
            return 0.0, 0.0
        # Trova lo spessore piu' vicino
        best = min(spessori_disponibili, key=lambda x: abs(x[0] - spessore_mm))
        return best[1], best[0]

    dettaglio = []
    totale = 0.0
    for piastra in analisi.get('piastre', []):
        prezzo_dm2, sp_match = _find_prezzo(piastra['spessore_mm'])
        costo = round(piastra['area_dm2'] * prezzo_dm2, 2)
        totale += costo
        dettaglio.append({
            'spessore_mm': piastra['spessore_mm'],
            'spessore_matchato': sp_match,
            'area_dm2': piastra['area_dm2'],
            'prezzo_dm2': prezzo_dm2,
            'costo': costo,
            'peso_kg': piastra['peso_kg'],
            'larghezza_mm': piastra.get('larghezza_mm', 0),
            'altezza_mm': piastra.get('altezza_mm', 0),
        })

    return {
        'dettaglio_piastre': dettaglio,
        'costo_materiale': round(totale, 2),
        'totale': round(totale, 2),
        'materiale': materiale,
    }
