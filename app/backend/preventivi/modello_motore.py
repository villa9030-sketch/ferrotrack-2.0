"""Modelli addestrati del motore di riconoscimento (scelta del contorno e "sicuro").

Due modelli, addestrati sull'archivio Lantek (banco_motore/agente_I) e
esportati come alberi in JSON (modello_motore.json): QUI non si usa
scikit-learn, solo Python.

- RANKER: per ogni contorno candidato una probabilita' "e' il pezzo"; puo'
  cambiare la scelta del detector quando lo distacca abbastanza.
- SICURO: per ogni disegno la probabilita' che la geometria letta sia giusta,
  calibrata sulle cartelle d'ordine tenute fuori dall'addestramento; sopra la
  soglia il disegno e' "sicuro" (pulizia 'auto'), sempre insieme alle regole
  di sicurezza fisse (fori trapano dubbi, contorni stretti, foglio intero...).

Le caratteristiche (feature) si calcolano QUI sia per il banco (dump per
l'addestramento) sia per l'import vero: stesse formule, stessi numeri.

Modo (config 'motore_modello' o variabile d'ambiente FT_MOTORE_MODELLO):
- 'off'  (predefinito): niente, il motore resta quello a regole;
- 'dump': calcola e restituisce le feature, NON cambia nessuna decisione;
- 'on'  : ranker e modello del sicuro decidono.
"""
from __future__ import annotations

import json
import logging
import math
import os

logger = logging.getLogger(__name__)

PERCORSO_MODELLO = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'modello_motore.json')
MAX_CANDIDATI = 30          # candidati valutati per disegno (primi per score + contorni esterni)


def modo(cfg: dict | None) -> str:
    m = (cfg or {}).get('motore_modello') or os.environ.get('FT_MOTORE_MODELLO') or 'off'
    m = str(m).lower()
    return m if m in ('off', 'dump', 'on') else 'off'


# ─────────────────────────────────────────────────────────────────────────
# Feature dei candidati
# ─────────────────────────────────────────────────────────────────────────
FEAT_CAND = [
    'log_area', 'area_rel_max', 'area_rel_sel', 'log_lato_max', 'log_lato_min', 'aspetto',
    'rettangolarita', 'convessita', 'log_vertici', 'frac_ortogonale', 'frac_iso', 'frac_obliqua',
    'n_cerchi', 'n_inner', 'n_inner_tondi', 'livello_alto', 'n_contenitori', 'rettangolo',
    'quote_sul', 'lati_quotati', 'quote_frac', 'n_quote', 'testi_dentro', 'testi_frac',
    'striscia', 'cx_rel', 'cy_rel', 'dist_bordo_rel', 'angolo_basso_dx',
    'score', 'rank', 'score_gap', 'score_rel', 'scelto_base', 'contiene_sel', 'dentro_sel',
    'n_copie', 'n_rivali', 'aperti_dentro', 'aperti_col_dentro', 'tratteggi_dentro',
    'n_candidati', 'n_top', 'rank_area_top',
]


def _frazioni_lati(poly) -> tuple[float, float, float]:
    """Frazione del perimetro su lati dritti ortogonali, a 30/60 gradi
    (assonometria) e obliqui; i tratti corti (archi spezzati) non contano."""
    try:
        xy = list(poly.exterior.coords)
    except Exception:
        return 0.0, 0.0, 0.0
    per = poly.length or 1.0
    soglia = 0.01 * per
    ort = iso = obl = 0.0
    for (x1, y1), (x2, y2) in zip(xy, xy[1:]):
        L = math.hypot(x2 - x1, y2 - y1)
        if L < soglia:
            continue
        a = math.degrees(math.atan2(y2 - y1, x2 - x1)) % 90.0
        if a < 1.0 or a > 89.0:
            ort += L
        elif abs(a - 30.0) < 1.5 or abs(a - 60.0) < 1.5:
            iso += L
        else:
            obl += L
    return ort / per, iso / per, obl / per


def _punti_dentro(poly, arr) -> int:
    if arr is None or not len(arr):
        return 0
    import shapely as shp
    x1, y1, x2, y2 = poly.bounds
    m = (arr[:, 0] >= x1) & (arr[:, 0] <= x2) & (arr[:, 1] >= y1) & (arr[:, 1] <= y2)
    if not m.any():
        return 0
    return int(shp.contains_xy(poly, arr[m, 0], arr[m, 1]).sum())


def _mezzi(aperti) -> list:
    out = []
    for t in aperti or []:
        try:
            v = t[2]
            out.append(v[len(v) // 2] if len(v) > 2 else ((t[0][0] + t[1][0]) / 2, (t[0][1] + t[1][1]) / 2))
        except Exception:
            continue
    return out


def feature_candidati(candidati: list, scored: list, best_idx: int, base: dict) -> dict:
    """{'idx': [indici in candidati], 'X': [[feature]], 'geo': [[area, w, h]]}"""
    import numpy as np
    from .dxf_polygon_detector_v3 import (
        _Indice, _prep_buf, _quote_sul_contorno, _lati_quotati, _n_testi_dentro,
        _is_rectangle_like, _striscia, foro_tondo,
    )
    if not candidati or not scored:
        return {'idx': [], 'X': [], 'geo': []}
    quote = base.get('quote') or []
    testi = base.get('testi') or []
    n_q, n_t = len(quote), len(testi)
    ind = _Indice(candidati)
    preps = [_prep_buf(c) for c in candidati]
    top = [i for i, c in enumerate(candidati) if not ind.contenitori(c, preps)]
    top_set = set(top)
    rank = {s['idx']: r for r, s in enumerate(scored)}
    sc = {s['idx']: s for s in scored}
    best_score = scored[0]['score'] if scored else 0.0
    # sottoinsieme: i primi per score, i contorni esterni piu' grandi, il scelto
    top_area = sorted(top, key=lambda i: -candidati[i].area)
    rank_area_top = {i: r for r, i in enumerate(top_area)}
    scelti = []
    for i in [best_idx] + [s['idx'] for s in scored[:10]] + top_area[:25]:
        if i not in scelti:
            scelti.append(i)
    scelti = scelti[:MAX_CANDIDATI]

    sel = candidati[best_idx]
    pp_sel = preps[best_idx]
    max_area = max(c.area for c in candidati) or 1.0
    xs0 = [c.bounds for c in candidati]
    X0 = min(b[0] for b in xs0)
    Y0 = min(b[1] for b in xs0)
    X1 = max(b[2] for b in xs0)
    Y1 = max(b[3] for b in xs0)
    W, H = max(X1 - X0, 1e-6), max(Y1 - Y0, 1e-6)
    ap = _mezzi(base.get('aperti'))
    apc = _mezzi(base.get('aperti_col'))
    trt = base.get('punti_tratteggi') or []
    ap = np.asarray(ap, dtype=float).reshape(-1, 2)
    apc = np.asarray(apc, dtype=float).reshape(-1, 2)
    trt = np.asarray(trt, dtype=float).reshape(-1, 2)
    top_dims = [(candidati[i].area, sorted((candidati[i].bounds[2] - candidati[i].bounds[0],
                                            candidati[i].bounds[3] - candidati[i].bounds[1]))) for i in top]

    righe, geo = [], []
    for i in scelti:
        p = candidati[i]
        s = sc.get(i)
        f = s['features'] if s else {}
        x1, y1, x2, y2 = p.bounds
        w, h = x2 - x1, y2 - y1
        a = p.area
        lmax, lmin = max(w, h), min(w, h)
        try:
            conv = a / (p.convex_hull.area or a)
        except Exception:
            conv = 1.0
        ort, iso, obl = _frazioni_lati(p)
        contenuti = ind.contenuti(p, preps[i])
        n_tondi = sum(1 for c in contenuti if foro_tondo(c))
        n_cont = 0
        if i not in top_set:
            b = ind.bb
            msk = ((ind.aree > a) & (b[:, 0] <= x1 + 0.05) & (b[:, 1] <= y1 + 0.05)
                   & (b[:, 2] >= x2 - 0.05) & (b[:, 3] >= y2 - 0.05))
            n_cont = sum(1 for j in np.nonzero(msk)[0] if j != i and preps[j].contains(p))
        nq = _quote_sul_contorno(p, quote)
        lq = _lati_quotati(p, quote)
        nt = _n_testi_dentro(p, testi)
        cx, cy = ((x1 + x2) / 2 - X0) / W, ((y1 + y2) / 2 - Y0) / H
        dist_b = min(x1 - X0, y1 - Y0, X1 - x2, Y1 - y2) / max(W, H)
        dims = sorted((w, h))
        n_copie = sum(1 for (aa, dd) in top_dims
                      if abs(aa - a) <= 0.005 * max(aa, a) and abs(dd[0] - dims[0]) <= 1 and abs(dd[1] - dims[1]) <= 1) \
            - (1 if i in top_set else 0)
        n_riv = sum(1 for (aa, dd) in top_dims if aa >= 0.3 * a) - (1 if i in top_set else 0)
        score = s['score'] if s else 0.0
        righe.append([
            math.log10(max(a, 1e-3)), a / max_area, a / (sel.area or 1.0),
            math.log10(max(lmax, 1e-3)), math.log10(max(lmin, 1e-3)), lmin / lmax if lmax else 0.0,
            a / (w * h) if w * h > 0 else 0.0, conv, math.log10(max(len(p.exterior.coords), 1)),
            ort, iso, obl,
            float(f.get('n_circles', 0)), float(f.get('n_inner', 0)), float(n_tondi),
            1.0 if i in top_set else 0.0, float(min(n_cont, 5)), 1.0 if _is_rectangle_like(p) else 0.0,
            float(nq), float(lq), nq / n_q if n_q else 0.0, float(n_q), float(nt), nt / n_t if n_t else 0.0,
            1.0 if _striscia(p) else 0.0, cx, cy, dist_b, 1.0 if (cx > 0.65 and cy < 0.3) else 0.0,
            score, float(rank.get(i, 99)), score - best_score, score / best_score if best_score else 0.0,
            1.0 if i == best_idx else 0.0,
            1.0 if (i != best_idx and a > sel.area and preps[i].contains(sel)) else 0.0,
            1.0 if (i != best_idx and a < sel.area and pp_sel.contains(p)) else 0.0,
            float(max(n_copie, 0)), float(max(n_riv, 0)),
            float(_punti_dentro(p, ap)), float(_punti_dentro(p, apc)), float(_punti_dentro(p, trt)),
            float(len(candidati)), float(len(top)), float(rank_area_top.get(i, 99)),
        ])
        geo.append([round(a, 2), round(w, 3), round(h, 3)])
    return {'idx': scelti, 'X': [[round(v, 5) for v in r] for r in righe], 'geo': geo}


# ─────────────────────────────────────────────────────────────────────────
# Feature del disegno (per il modello del sicuro)
# ─────────────────────────────────────────────────────────────────────────
_DIAG = ['conf_scelta', 'conf_falde', 'n_falde', 'n_attaccati', 'n_scritte', 'n_scritte_dubbie',
         'n_segni', 'n_svas', 'n_intagli', 'n_quote', 'quote_su_contorno', 'lati_quotati',
         'testi_dentro', 'n_top', 'rivale_rel', 'n_fori_piccoli_non_tondi', 'n_fori', 'n_fori_tondi',
         'foro_min_mm', 'n_fori_lt2', 'n_fori_lt4', 'area_fori_rel']
_PUL = ['pul_n_taglio', 'pul_n_piega', 'pul_n_marcatura', 'pul_n_simboli_tolti', 'pul_copertura',
        'pul_n_lung_marcatura_mm']

FEAT_DIS = (
    ['d_' + k for k in _DIAG]
    + ['scala_dubbia', 'scala_da_quote', 'scala_diversa', 'conf', 'conf_detector', 'n_pezzi',
       'log_area', 'log_lato_max', 'log_lato_min', 'n_avvisi',
       'dim_cart_presente', 'dim_cart_ok', 'peso_presente', 'peso_dev', 'peso_rapporto',
       'sp_conf', 'sp_discorde', 'sp_n_fonti', 'sp_indip', 'mat_conf',
       'n_fori_trapano', 'fori_trapano_dubbio', 'fori_stretti',
       'pul_presente'] + _PUL
    + ['pul_n_avvisi', 'pulizia_ok', 'tr_n', 'tr_lung', 'tr_facce', 'tr_bordo_curvo',
       'regole_ok', 'mot_nessun_riscontro', 'mot_lettere', 'mot_attaccati', 'mot_leggero',
       'mot_linee', 'mot_piega', 'mot_scala',
       'r_ok', 'r_p_sel', 'r_p_best', 'r_margine', 'r_sel_top1', 'r_n']
)


def _f(x, d=0.0):
    try:
        v = float(x)
        return v if v == v else d
    except (TypeError, ValueError):
        return d


def feature_disegno(geo: dict, ind: dict, pulizia_ok: bool, rank: dict | None) -> list:
    geo = geo or {}
    ind = ind or {}
    d = ind.get('diag') or geo.get('diagnostica') or {}
    v = [_f(d.get(k), -1.0) if k == 'foro_min_mm' else _f(d.get(k)) for k in _DIAG]
    w, h = _f(geo.get('bbox_width_mm')), _f(geo.get('bbox_height_mm'))
    sc = _f(ind.get('scala') or geo.get('scala_unita_mm'), 1.0)
    from . import sicurezza_import as si
    r = si.rapporto_peso(ind) if ind else None
    v += [
        _f(d.get('scala_dubbia')), _f(d.get('scala_da_quote'), 1.0), 1.0 if abs(sc - 1.0) > 1e-6 else 0.0,
        _f(geo.get('confidence')), _f(ind.get('conf_detector'), _f(geo.get('confidence'))),
        _f(geo.get('n_pezzi_rilevati'), 1.0),
        math.log10(max(_f(geo.get('area_dm2')) * 1e4, 1e-3)),
        math.log10(max(max(w, h), 1e-3)), math.log10(max(min(w, h), 1e-3)),
        float(len(geo.get('warnings') or [])),
        1.0 if ind.get('dim_cartiglio') else 0.0, 1.0 if ind.get('dim_cartiglio_ok') else 0.0,
        1.0 if r is not None else 0.0, abs(r - 1.0) if r is not None else -1.0, r if r is not None else -1.0,
        _f(ind.get('sp_conf')), 1.0 if ind.get('sp_discorde') else 0.0, float(len(ind.get('sp_fonti') or [])),
        1.0 if ind.get('sp_indipendente') else 0.0, _f(ind.get('mat_conf')),
        _f(ind.get('n_fori_trapano')), 1.0 if ind.get('fori_trapano_dubbio') else 0.0,
        _f(ind.get('fori_stretti_non_tondi')),
        1.0 if 'pul_n_taglio' in ind else 0.0,
    ]
    v += [_f(ind.get(k), -1.0) for k in _PUL]
    tr = geo.get('tratti_aperti') or {}
    tr_n = sum(x[0] for k, x in tr.items() if not k.startswith('_') and k not in ('facce', 'facce_c') and isinstance(x, list))
    tr_l = sum(x[1] for k, x in tr.items() if not k.startswith('_') and k not in ('facce', 'facce_c') and isinstance(x, list))
    ok_regole, motivi = si.decidi_sicuro(ind) if ind else (False, [])
    mot = ' '.join(motivi)
    rk = rank or {}
    v += [
        float(len(ind.get('pul_avvisi') or [])), 1.0 if pulizia_ok else 0.0,
        float(tr_n), float(tr_l), _f((tr.get('facce') or [0])[0]), _f((tr.get('bordo2~') or [0, 0.0])[1]),
        1.0 if ok_regole else 0.0, 1.0 if 'riscontro' in mot else 0.0, 1.0 if 'lettera' in mot else 0.0,
        1.0 if 'attaccati' in mot else 0.0, 1.0 if 'peso calcolato' in mot else 0.0,
        1.0 if 'non chiuse' in mot else 0.0, 1.0 if 'linee di piega' in mot else 0.0,
        1.0 if 'in scala' in mot else 0.0,
        1.0 if rk else 0.0, _f(rk.get('p_sel'), -1.0), _f(rk.get('p_best'), -1.0), _f(rk.get('margine'), -1.0),
        _f(rk.get('sel_top1'), -1.0), _f(rk.get('n'), 0.0),
    ]
    return [round(x, 5) for x in v]


# ─────────────────────────────────────────────────────────────────────────
# Valutazione dei modelli (alberi esportati in JSON)
# ─────────────────────────────────────────────────────────────────────────
_MODELLO: dict | None = None


def carica(percorso: str | None = None) -> dict:
    global _MODELLO
    if _MODELLO is None or percorso:
        try:
            with open(percorso or PERCORSO_MODELLO, encoding='utf-8') as fh:
                m = json.load(fh)
        except Exception as e:      # noqa: BLE001
            logger.warning('modello motore non caricato: %s', e)
            m = {}
        if percorso:
            return m
        _MODELLO = m
    return _MODELLO


def _albero(t: dict, x: list) -> float:
    f, s, l, r, v = t['f'], t['s'], t['l'], t['r'], t['v']
    n = 0
    while l[n] >= 0:
        n = l[n] if x[f[n]] <= s[n] else r[n]
    return v[n]


def grezzo(m: dict, x: list) -> float:
    """Somma degli alberi (log-odds) per il vettore x."""
    return m['init'] + m['lr'] * sum(_albero(t, x) for t in m['alberi'])


def probabilita(m: dict, x: list, calibrata: bool = False) -> float:
    """Probabilita' del modello; calibrata=True la passa per la calibrazione
    isotonica (fold tenuti fuori): solo da mostrare, la soglia e' sulla grezza."""
    z = grezzo(m, x)
    p = 1.0 / (1.0 + math.exp(-max(-50.0, min(50.0, z))))
    cal = m.get('calib')
    if cal and calibrata:
        xs, ys = cal['x'], cal['y']
        if p <= xs[0]:
            return ys[0]
        if p >= xs[-1]:
            return ys[-1]
        lo, hi = 0, len(xs) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if xs[mid] <= p:
                lo = mid
            else:
                hi = mid
        t = (p - xs[lo]) / ((xs[hi] - xs[lo]) or 1.0)
        return ys[lo] + t * (ys[hi] - ys[lo])
    return p


def valuta_ranker(fc: dict, best_idx: int) -> dict | None:
    """Probabilita' del ranker per ogni candidato: {'p': [...], 'best': idx,
    'p_sel', 'p_best', 'margine' (prima - seconda), 'sel_top1', 'n'}."""
    m = (carica() or {}).get('ranker')
    if not m or not fc or not fc.get('X'):
        return None
    ps = [probabilita(m, x) for x in fc['X']]
    ordine = sorted(range(len(ps)), key=lambda k: -ps[k])
    k0 = ordine[0]
    sel_k = fc['idx'].index(best_idx) if best_idx in fc['idx'] else None
    p1 = ps[k0]
    p2 = ps[ordine[1]] if len(ordine) > 1 else 0.0
    return {'p': [round(p, 4) for p in ps], 'best': fc['idx'][k0], 'p_best': p1,
            'p_sel': ps[sel_k] if sel_k is not None else 0.0, 'margine': p1 - p2,
            'sel_top1': 1.0 if fc['idx'][k0] == best_idx else 0.0, 'n': len(ps)}


def scelta_ranker(rk: dict | None, best_idx: int) -> int:
    """Indice del candidato da prendere: quello del ranker se supera la scelta
    del detector di almeno il margine del modello, altrimenti quello del detector."""
    m = (carica() or {}).get('ranker') or {}
    if not rk or rk['best'] == best_idx:
        return best_idx
    if rk['p_best'] - rk['p_sel'] >= float(m.get('margine_cambio', 1.0)) and rk['p_best'] >= float(m.get('p_min_cambio', 1.0)):
        return rk['best']
    return best_idx


def prob_sicuro(x: list, calibrata: bool = False) -> float | None:
    m = (carica() or {}).get('sicuro')
    if not m:
        return None
    return probabilita(m, x, calibrata)


def soglia_sicuro() -> float:
    """Soglia sulla probabilita' grezza. Predefinita: 99,5% di precisione sui
    fold tenuti fuori. FT_MOTORE_PRECISIONE=0.99 / 0.998 sceglie un altro punto
    della curva (0.99: circa +60% di sicuri, con gli stessi errori delle regole)."""
    m = (carica() or {}).get('sicuro') or {}
    alt = os.environ.get('FT_MOTORE_PRECISIONE')
    if alt and str(alt) in (m.get('soglie') or {}):
        return float(m['soglie'][str(alt)])
    return float(m.get('soglia', 2.0))


def politica_sicuro() -> str:
    """'sostituisce' (il modello decide da solo) o 'promuove' (aggiunge sicuri a quelli a regole)."""
    return str(((carica() or {}).get('sicuro') or {}).get('politica') or 'sostituisce')
