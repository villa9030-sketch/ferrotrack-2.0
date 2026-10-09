"""Tipo di disegno: che cosa c'e' nel foglio, prima ancora di quale contorno.

Su 100 disegni "indecisi" di clienti non DECA (revisione di Stefano, 09/10/2026)
il problema non era scegliere il contorno ma capire il TIPO di disegno:
  - SINGOLO        un pezzo piano da tagliare (il caso normale);
  - PIU_PEZZI      un foglio con piu' pezzi piani diversi da tagliare;
  - DA_SVILUPPARE  pezzo piegato disegnato solo nelle viste piegate / 3D,
                   senza lo sviluppo (lo sviluppa a mano Stefano);
  - NON_LASER      assieme, distinta, profili/tubi, pezzi lavorati di macchina.

Qui:
  - `caratteristiche(...)`: numeri sul foglio (viste, candidati, quote, testi
    chiave contati - mai il testo -, linee nascoste/isometriche, pallini e
    tabelle della distinta). Tutto spiegabile con una regola di disegno.
  - `classifica(feat)`: un albero di decisione piccolo, addestrato sui disegni
    rivisti da Stefano + i disegni che tornano con Lantek (SINGOLO); i pesi
    sono in tipo_disegno_modello.json (niente sklearn in produzione).
  - `pezzi_del_foglio(...)`: per PIU_PEZZI, tutti i pezzi piani del foglio,
    con le copie identiche contate come quantita'.
"""
from __future__ import annotations

import bisect
import json
import logging
import math
import os
import re

logger = logging.getLogger(__name__)

TIPI = ('SINGOLO', 'PIU_PEZZI', 'DA_SVILUPPARE', 'NON_LASER')

AVVISO_TIPO = {
    'DA_SVILUPPARE': 'Manca lo sviluppo: va sviluppato',
    'NON_LASER': 'Assieme o pezzo non da laser',
    'PIU_PEZZI': 'Nel foglio ci sono piu\' pezzi da tagliare',
}

# Parole chiave contate nei testi (solo il conteggio: il testo non esce mai).
# Ogni gruppo ha una ragione di disegno:
_PAROLE = {
    # il disegnatore scrive "sviluppo" accanto alla vista distesa
    'sviluppo': r'SVILUPP|DEVELOP|FLAT\s*PATTERN|ABWICK|DEPLIE|DESARROLL',
    # assiemi e distinte: il foglio descrive piu' componenti
    'assieme': r'ASSIEM|ASSEMBL|\bASSY\b|ZUSAMMENB|GRUPPO|COMPLESSIVO',
    'distinta': r'DISTINTA|\bBOM\b|PARTS\s*LIST|STUCKLIST|STÜCKLIST|ELENCO\s+PART|LISTA\s+COMP',
    'pos': r'^\s*(POS\.?|ITEM|RIF\.?|PART\s*N)',
    'qta': r'Q\.?\s*T[AÀ]\b|\bQTY\b|QUANTIT|\bMENGE\b|\bN\.?\s*PZ',
    'saldat': r'SALDAT|SALDAR|\bWELD|SCHWEI',
    # profili, tubi, getti, lavorazioni di macchina: non sono lamiere da laser
    'profilo': r'\bTUBO|\bTUBE\b|\bIPE\s*\d|\bUPN\s*\d|\bHE[ABM]\s*\d|ANGOLAR|SCATOLAT|PROFILAT|TONDO\s*\d|\bBARRA',
    'macchina': r'TORNIT|FRESAT|RETTIFIC|\bRa\s*\d|ALESAT|FILETTATUR[AE]\s+INT|BRONZ|FUSIONE|\bGETTO',
    'lamiera': r'LAMIER|\bSHEET|\bBLECH|\bLAM\.|\bSP\.?\s*\d|SPESSORE|THK',
    'piega': r'PIEG|\bBEND|\bKANT|\bR\s*INT|RAGGIO\s+INT',
    'iso': r'ISOMETR|ASSONOM|PROSPETT|\b3D\b',
    'sezione': r'SEZ\.|SEZIONE|SECTION|SCHNITT',
}
_RE_PAROLE = {k: re.compile(v, re.I) for k, v in _PAROLE.items()}
_RE_NUMERO_PALLINO = re.compile(r'^\s*\d{1,3}\s*$')

_TIPI_3D = {'3DFACE', '3DSOLID', 'MESH', 'REGION', 'BODY', 'SURFACE', 'POLYFACE'}
MAX_ENTITA = 120000          # fogli enormi: il conteggio si ferma qui


def _testo(e) -> str:
    try:
        t = e.dxftype()
        if t == 'MTEXT':
            return e.plain_text()
        if t in ('TEXT', 'ATTRIB', 'ATTDEF'):
            return e.dxf.get('text', '') or ''
    except Exception:
        pass
    return ''


def _scansione_entita(msp, scala: float, cfg: dict) -> dict:
    """Un passaggio su tutte le entita' (blocchi espansi, anche annotazioni)."""
    from .dxf_polygon_detector_v3 import entita_espanse, linea_tratteggiata, colore_effettivo, _layer_piega
    f = float(scala or 1.0)
    colori_piega = set(cfg.get('dxf_colori_piega', [2]))
    c = {'n_ent': 0, 'n_line': 0, 'n_arc': 0, 'n_circle': 0, 'n_poly': 0, 'n_spline': 0,
         'n_ellipse': 0, 'n_hatch': 0, 'n_dim': 0, 'n_dim_ang': 0, 'n_dim_rad': 0,
         'n_leader': 0, 'n_insert': 0, 'n_testi': 0, 'n_3d': 0, 'n_tab': 0,
         'n_tratt': 0, 'n_piega': 0, 'n_z': 0}
    L = {'orto': 0.0, 'iso': 0.0, 'altro': 0.0}
    lw = set()
    layers = set()
    testi = []          # (x, y, h, s)
    cerchi = []         # (x, y, r)
    segmenti = []       # (x0, y0, x1, y1) mm, linee continue
    archi = []          # (x, y, r) mm
    parole = {k: 0 for k in _PAROLE}
    n = 0
    for e in entita_espanse(msp, solo_geometria=False):
        n += 1
        if n > MAX_ENTITA:
            break
        t = e.dxftype()
        c['n_ent'] += 1
        try:
            layers.add(str(e.dxf.get('layer', '0')).lower())
            w = e.dxf.get('lineweight', -1)
            if w is not None and w >= 0:
                lw.add(int(w))
        except Exception:
            pass
        if t in ('TEXT', 'MTEXT', 'ATTRIB'):
            s = _testo(e)
            if not s.strip():
                continue
            c['n_testi'] += 1
            for k, rx in _RE_PAROLE.items():
                if rx.search(s):
                    parole[k] += 1
            try:
                p = e.dxf.insert
                h = float(e.dxf.get('height', 0) or e.dxf.get('char_height', 0) or 0)
                if t == 'TEXT' and e.dxf.hasattr('align_point') and (e.dxf.get('halign', 0) or e.dxf.get('valign', 0)):
                    p = e.dxf.align_point
                testi.append((float(p.x) * f, float(p.y) * f, h * f, s.strip()))
            except Exception:
                pass
            continue
        if t in ('DIMENSION', 'ARC_DIMENSION'):
            c['n_dim'] += 1
            try:
                dt = int(e.dxf.get('dimtype', 0)) & 7
                if dt in (2, 5) or t == 'ARC_DIMENSION':
                    c['n_dim_ang'] += 1
                elif dt in (3, 4):
                    c['n_dim_rad'] += 1
            except Exception:
                pass
            continue
        if t in ('LEADER', 'MULTILEADER', 'MLEADER'):
            c['n_leader'] += 1
            continue
        if t == 'ACAD_TABLE':
            c['n_tab'] += 1
            continue
        if t in _TIPI_3D:
            c['n_3d'] += 1
            continue
        if t == 'HATCH':
            c['n_hatch'] += 1
            continue
        if t not in ('LINE', 'ARC', 'CIRCLE', 'LWPOLYLINE', 'POLYLINE', 'SPLINE', 'ELLIPSE'):
            continue
        tratteggiata = False
        try:
            if linea_tratteggiata(e):
                c['n_tratt'] += 1
                tratteggiata = True
            if colore_effettivo(e) in colori_piega or _layer_piega(e.dxf.get('layer', '')):
                c['n_piega'] += 1
        except Exception:
            pass
        if t == 'LINE':
            c['n_line'] += 1
            try:
                s, en = e.dxf.start, e.dxf.end
                if abs(s.z) > 1e-6 or abs(en.z) > 1e-6:
                    c['n_z'] += 1
                dx, dy = en.x - s.x, en.y - s.y
                lung = math.hypot(dx, dy) * f
                if lung <= 0:
                    continue
                if not tratteggiata:
                    segmenti.append((s.x * f, s.y * f, en.x * f, en.y * f))
                a = math.degrees(math.atan2(dy, dx)) % 90.0
                if a < 0.5 or a > 89.5:
                    L['orto'] += lung
                elif abs(a - 30.0) < 1.0 or abs(a - 60.0) < 1.0:
                    L['iso'] += lung
                else:
                    L['altro'] += lung
            except Exception:
                pass
        elif t == 'ARC':
            c['n_arc'] += 1
            if not tratteggiata:
                try:
                    cc = e.dxf.center
                    archi.append((float(cc.x) * f, float(cc.y) * f, float(e.dxf.radius) * f))
                except Exception:
                    pass
        elif t == 'CIRCLE':
            c['n_circle'] += 1
            try:
                cc = e.dxf.center
                cerchi.append((float(cc.x) * f, float(cc.y) * f, float(e.dxf.radius) * f))
            except Exception:
                pass
        elif t in ('LWPOLYLINE', 'POLYLINE'):
            c['n_poly'] += 1
            if t == 'LWPOLYLINE' and not tratteggiata:
                try:
                    pts = list(e.get_points('xyb'))
                    if e.closed and pts:
                        pts.append(pts[0])
                    for (x0, y0, b0), (x1, y1, _b) in zip(pts, pts[1:]):
                        if abs(b0) < 1e-9 and (x0, y0) != (x1, y1):
                            segmenti.append((x0 * f, y0 * f, x1 * f, y1 * f))
                except Exception:
                    pass
        elif t == 'SPLINE':
            c['n_spline'] += 1
        elif t == 'ELLIPSE':
            c['n_ellipse'] += 1
    tot = sum(L.values())
    c['l_tot_m'] = round(tot / 1000.0, 2)
    c['fr_iso'] = round(L['iso'] / tot, 4) if tot else 0.0
    c['fr_obliquo'] = round(L['altro'] / tot, 4) if tot else 0.0
    ng = c['n_line'] + c['n_arc'] + c['n_circle'] + c['n_poly'] + c['n_spline'] + c['n_ellipse']
    c['n_geom'] = ng
    c['fr_tratt'] = round(c['n_tratt'] / ng, 4) if ng else 0.0
    c['fr_piega'] = round(c['n_piega'] / ng, 4) if ng else 0.0
    c['n_lw'] = len(lw)
    c['n_layer'] = len(layers)
    for k, v in parole.items():
        c['kw_' + k] = v
    c.update(_pallini_e_tabelle(testi, cerchi))
    c.update(_spessore_nelle_viste(segmenti, archi))
    return c


SPESSORE_VISTA = (0.4, 6.0)      # distanza tra le due linee di un bordo di lamiera visto di fianco


def _spessore_nelle_viste(segmenti: list, archi: list) -> dict:
    """Pezzo disegnato PIEGATO: nelle viste ogni bordo di lamiera e' una
    coppia di linee parallele a distanza = spessore (0,4-6 mm) e ogni piega una
    coppia di archi concentrici con raggi che differiscono dello spessore.
    In uno sviluppo (il pezzo disteso) queste coppie quasi non ci sono.
    - fr_doppie: frazione della lunghezza di linea che ha una parallela vicina
      (sovrapposta per almeno meta') a distanza 0,4-6 mm;
    - n_archi_piega: archi con un arco concentrico a raggio +- 0,4-6 mm."""
    out = {'fr_doppie': 0.0, 'l_doppie_m': 0.0, 'n_archi_piega': 0, 'fr_archi_piega': 0.0}
    lo, hi = SPESSORE_VISTA
    if segmenti and len(segmenti) <= 60000:
        gruppi: dict = {}
        tot = 0.0
        for x0, y0, x1, y1 in segmenti:
            L = math.hypot(x1 - x0, y1 - y0)
            if L < 2.0:
                continue
            tot += L
            a = math.atan2(y1 - y0, x1 - x0) % math.pi
            k = round(math.degrees(a) * 2) % 360          # mezzo grado
            ux, uy = math.cos(a), math.sin(a)
            off = -x0 * uy + y0 * ux                       # distanza dalla retta per l'origine
            t0, t1 = sorted((x0 * ux + y0 * uy, x1 * ux + y1 * uy))
            gruppi.setdefault(k, []).append((off, t0, t1, L))
        dopp = 0.0
        for k, lst in gruppi.items():
            lst.sort()
            offs = [x[0] for x in lst]
            for i, (off, t0, t1, L) in enumerate(lst):
                trovato = False
                j = bisect.bisect_left(offs, off - hi)
                visti = 0
                while j < len(lst) and offs[j] <= off + hi and visti < 200:
                    o2, s0, s1, L2 = lst[j]
                    if j != i and abs(o2 - off) >= lo:
                        if min(t1, s1) - max(t0, s0) >= 0.5 * min(L, L2):
                            trovato = True
                            break
                    j += 1
                    visti += 1
                if trovato:
                    dopp += L
        if tot > 0:
            out['fr_doppie'] = round(dopp / tot, 4)
            out['l_doppie_m'] = round(dopp / 1000.0, 3)
    if archi and len(archi) <= 20000:
        g: dict = {}
        for x, y, r in archi:
            g.setdefault((round(x, 1), round(y, 1)), []).append(r)
        n = 0
        for rr in g.values():
            if len(rr) < 2:
                continue
            for i, r in enumerate(rr):
                if any(lo <= abs(r - r2) <= hi for j, r2 in enumerate(rr) if j != i):
                    n += 1
        out['n_archi_piega'] = n
        out['fr_archi_piega'] = round(n / len(archi), 4)
    return out


def _pallini_e_tabelle(testi: list, cerchi: list) -> dict:
    """Pallini della distinta (un numero dentro un cerchietto) e righe di
    tabella (almeno 4 testi sulla stessa riga): sono il segno di un assieme."""
    out = {'n_pallini': 0, 'n_righe_tab': 0, 'n_num_soli': 0}
    num = [(x, y, h) for x, y, h, s in testi if _RE_NUMERO_PALLINO.match(s)]
    out['n_num_soli'] = len(num)
    if num and cerchi:
        # griglia dei centri dei cerchi
        g: dict = {}
        for x, y, r in cerchi:
            if r <= 0:
                continue
            g.setdefault((round(x / 20.0), round(y / 20.0)), []).append((x, y, r))
        visti = set()
        for x, y, h in num:
            kx, ky = round(x / 20.0), round(y / 20.0)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for cx, cy, r in g.get((kx + dx, ky + dy), ()):
                        # testo dentro il cerchio e cerchio poco piu' grande del testo
                        if math.hypot(x - cx, y - cy) <= 0.8 * r and (h <= 0 or r <= 4.0 * h + 1.0):
                            visti.add((round(cx, 1), round(cy, 1)))
        out['n_pallini'] = len(visti)
    if len(testi) >= 8:
        righe: dict = {}
        for x, y, h, s in testi:
            k = round(y / max(1.0, h or 2.5))
            righe.setdefault(k, []).append(x)
        out['n_righe_tab'] = sum(1 for v in righe.values() if len(v) >= 4)
    return out


def _viste(polys_top: list, aperti: list, ingombro: float) -> dict:
    """Gruppi di disegno separati (viste): ingombri dei contorni di primo
    livello e dei tratti aperti, uniti se distano meno del 2% del foglio."""
    boxes = [p.bounds for p in polys_top]
    for s, e, v in aperti[:20000]:
        xs = [p[0] for p in v]
        ys = [p[1] for p in v]
        boxes.append((min(xs), min(ys), max(xs), max(ys)))
    if not boxes:
        return {'n_viste': 0, 'n_viste_10': 0, 'vista_max_rel': 0.0}
    d = max(1.0, 0.02 * ingombro)
    # unione per sovrapposizione degli ingombri allargati (union-find su griglia)
    n = len(boxes)
    par = list(range(n))

    def find(i):
        while par[i] != i:
            par[i] = par[par[i]]
            i = par[i]
        return i
    cell = max(d * 4, ingombro / 50.0)
    grid: dict = {}
    for i, (x0, y0, x1, y1) in enumerate(boxes):
        for gx in range(int((x0 - d) // cell), int((x1 + d) // cell) + 1):
            for gy in range(int((y0 - d) // cell), int((y1 + d) // cell) + 1):
                grid.setdefault((gx, gy), []).append(i)
    for lst in grid.values():
        if len(lst) > 400:
            lst = lst[:400]
        for a in range(len(lst)):
            i = lst[a]
            bi = boxes[i]
            for b in range(a + 1, len(lst)):
                j = lst[b]
                bj = boxes[j]
                if bi[0] - d <= bj[2] and bj[0] - d <= bi[2] and bi[1] - d <= bj[3] and bj[1] - d <= bi[3]:
                    ri, rj = find(i), find(j)
                    if ri != rj:
                        par[ri] = rj
    gruppi: dict = {}
    for i, b in enumerate(boxes):
        r = find(i)
        g = gruppi.get(r)
        gruppi[r] = b if g is None else (min(g[0], b[0]), min(g[1], b[1]), max(g[2], b[2]), max(g[3], b[3]))
    aree = sorted(((g[2] - g[0]) * (g[3] - g[1]) for g in gruppi.values()), reverse=True)
    amax = aree[0] if aree else 0.0
    return {'n_viste': sum(1 for a in aree if a >= 0.01 * amax),
            'n_viste_10': sum(1 for a in aree if a >= 0.10 * amax),
            'vista_max_rel': round(amax / (ingombro ** 2), 4) if ingombro > 0 else 0.0}


def _profilo_sottile(p) -> bool:
    """Contorno sottile e piegato (L, U, Z): la vista di fianco di una lamiera
    piegata; larghezza media 2A/P <= 6 mm e meno di meta' del rettangolo minimo."""
    try:
        if p.length < 40.0:
            return False
        w = 2.0 * p.area / p.length
        if w > 6.0:
            return False
        r = p.minimum_rotated_rectangle
        return r.area > 0 and p.area < 0.5 * r.area
    except Exception:
        return False


def _striscia(p) -> bool:
    x0, y0, x1, y1 = p.bounds
    return min(x1 - x0, y1 - y0) <= 6.0 and max(x1 - x0, y1 - y0) >= 20.0


def caratteristiche(doc, msp, base: dict, candidati: list, top_level: list, outer, scelto,
                    copia_identica, cfg: dict | None = None, conf_scelta: float = 0.0,
                    profilo_piegato: bool = False) -> dict:
    """Numeri del foglio per decidere il tipo di disegno."""
    cfg = cfg or {}
    scala = base.get('scala') or 1.0
    feat = {}
    try:
        feat.update(_scansione_entita(msp, scala, cfg))
    except Exception as e:     # noqa: BLE001
        logger.debug('tipo disegno, scansione: %s', e)
    polys = base.get('polys') or []
    try:
        xs0 = [p.bounds for p in polys] or [outer.bounds]
        X0 = min(b[0] for b in xs0)
        Y0 = min(b[1] for b in xs0)
        X1 = max(b[2] for b in xs0)
        Y1 = max(b[3] for b in xs0)
        ingombro = max(X1 - X0, Y1 - Y0)
    except Exception:
        ingombro = 0.0
    feat['foglio_mm'] = round(ingombro, 1)
    a_out = outer.area if outer is not None else 0.0
    ob = outer.bounds
    feat['pezzo_lato_max'] = round(max(ob[2] - ob[0], ob[3] - ob[1]), 1)
    feat['pezzo_lato_min'] = round(min(ob[2] - ob[0], ob[3] - ob[1]), 1)
    feat['pezzo_su_foglio'] = round(feat['pezzo_lato_max'] / ingombro, 4) if ingombro else 1.0
    feat['n_polys'] = len(polys)
    feat['n_cand'] = len(candidati)
    feat['n_top'] = len(top_level)
    tops = [c for c in top_level if c is not outer and c is not scelto]
    aree = sorted((c.area for c in tops), reverse=True)
    feat['n_top_3'] = sum(1 for a in aree if a >= 0.03 * a_out)
    feat['n_top_10'] = sum(1 for a in aree if a >= 0.10 * a_out)
    feat['n_top_30'] = sum(1 for a in aree if a >= 0.30 * a_out)
    feat['n_top_100'] = sum(1 for a in aree if a >= 1.0 * a_out)
    feat['rivale_rel'] = round(aree[0] / a_out, 4) if aree and a_out > 0 else 0.0
    grandi = [c for c in tops if c.area >= 0.30 * a_out]
    feat['n_copie'] = sum(1 for c in grandi if copia_identica(outer, c))
    feat['n_strisce'] = sum(1 for c in top_level if _striscia(c))
    try:
        from .dxf_polygon_detector_v3 import _Indice
        sig = [c for c in top_level if c.area >= 0.03 * a_out]
        if len(sig) <= 300:
            # viste che si accavallano (assieme: i pezzi si coprono) e
            # contorni grandi con fori dentro (piu' pezzi nel foglio)
            n_inc = 0
            for i, a in enumerate(sig):
                for b in sig[i + 1:]:
                    try:
                        if a.intersects(b) and not a.touches(b):
                            n_inc += 1
                    except Exception:
                        pass
            feat['n_incroci'] = n_inc
            ind = _Indice(candidati)
            feat['n_top_con_fori'] = sum(1 for c in sig if c.area >= 0.10 * a_out and ind.contenuti(c))
    except Exception as e:     # noqa: BLE001
        logger.debug('tipo disegno, incroci: %s', e)
    feat['n_profili'] = sum(1 for c in polys if _profilo_sottile(c))
    feat['profilo_piegato'] = int(bool(profilo_piegato))
    feat['conf_scelta'] = round(float(conf_scelta or 0), 3)
    try:
        from .dxf_polygon_detector_v3 import _is_rectangle_like
        feat['pezzo_rett'] = int(bool(_is_rectangle_like(outer)))
        a3 = [c.area for c in top_level if c.area >= 0.03 * a_out]
        feat['fr_pezzo'] = round(a_out / sum(a3), 4) if a3 else 1.0
        l_chiusi = sum(p.length for p in polys)
        l_aperti = sum(math.dist(v[i], v[i + 1]) for _s, _e, v in (base.get('aperti') or [])[:50000]
                       for i in range(len(v) - 1))
        feat['fr_chiuso'] = round(l_chiusi / (l_chiusi + l_aperti), 4) if l_chiusi + l_aperti > 0 else 0.0
    except Exception as e:     # noqa: BLE001
        logger.debug('tipo disegno, pezzo: %s', e)
    ng = feat.get('n_geom') or 0
    feat['fr_ellissi'] = round((feat.get('n_ellipse') or 0) / ng, 4) if ng else 0.0
    # quote: sul pezzo scelto e nel foglio
    from .dxf_polygon_detector_v3 import _quote_sul_contorno, _lati_quotati, _n_testi_dentro
    quote = base.get('quote') or []
    feat['n_quote'] = len(quote)
    feat['quote_sul_pezzo'] = _quote_sul_contorno(outer, quote)
    feat['fr_quote_pezzo'] = round(feat['quote_sul_pezzo'] / len(quote), 3) if quote else 0.0
    feat['lati_quotati'] = _lati_quotati(outer, quote)
    feat['testi_dentro'] = _n_testi_dentro(outer, base.get('testi'))
    # fori del pezzo scelto
    try:
        from .dxf_polygon_detector_v3 import _prep_buf, _contiene
        pp = _prep_buf(outer)
        dentro = [c for c in candidati if c is not outer and _contiene(outer, c, pp)]
        feat['n_dentro'] = len(dentro)
    except Exception:
        feat['n_dentro'] = 0
    try:
        gr = gruppi_pezzi(candidati, top_level, base.get('testi'), copia_identica)
        feat['_gruppi'] = gr          # tolto prima di uscire (serve a pezzi_del_foglio)
        a0 = gr[0]['poly'].area if gr else 0.0
        feat['n_pezzi_foglio'] = len(gr)
        feat['n_pezzi_tot'] = sum(g['quantita'] for g in gr)
        feat['n_pezzi_30'] = sum(1 for g in gr if g['poly'].area >= 0.30 * a0)
        feat['n_pezzi_fori'] = sum(1 for g in gr if g['fori'])
        feat['q_max'] = max((g['quantita'] for g in gr), default=0)
    except Exception as e:     # noqa: BLE001
        logger.debug('tipo disegno, pezzi: %s', e)
    try:
        feat.update(_viste(top_level, base.get('aperti') or [], ingombro))
    except Exception as e:     # noqa: BLE001
        logger.debug('tipo disegno, viste: %s', e)
    return feat


# ---------------------------------------------------------------------------
# Classificatore: albero di decisione esportato in JSON
# ---------------------------------------------------------------------------
_MODELLO = None
_MODELLO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tipo_disegno_modello.json')


def _modello():
    global _MODELLO
    if _MODELLO is None:
        try:
            with open(_MODELLO_PATH, encoding='utf-8') as fh:
                _MODELLO = json.load(fh)
        except Exception as e:     # noqa: BLE001
            logger.warning('modello tipo disegno non caricato: %s', e)
            _MODELLO = {}
    return _MODELLO


def _albero(nodo: dict, feat: dict):
    while 'foglia' not in nodo:
        v = feat.get(nodo['f'])
        v = 0.0 if v is None else float(v)
        nodo = nodo['si'] if v <= nodo['t'] else nodo['no']
    return nodo['foglia']


def classifica(feat: dict) -> tuple[str, float, dict]:
    """(tipo, confidenza, probabilita' per classe). Senza modello: SINGOLO."""
    m = _modello()
    if not m or 'albero' not in m:
        return 'SINGOLO', 0.0, {}
    prob = dict(_albero(m['albero'], feat))
    tipo = max(prob, key=prob.get)
    if tipo != 'SINGOLO' and prob[tipo] < float(m.get('soglia', 0.5)):
        # foglia incerta: resta il comportamento di sempre (pezzo singolo)
        tipo = 'SINGOLO'
    return tipo, round(float(prob[tipo]), 3), {k: round(float(v), 3) for k, v in prob.items()}


# ---------------------------------------------------------------------------
# Piu' pezzi nello stesso foglio
# ---------------------------------------------------------------------------
MIN_PEZZO_REL = 0.02         # un pezzo e' almeno il 2% del piu' grande (sotto: simboli, frecce)


def _stessa_misura(a: float, b: float) -> bool:
    return abs(a - b) <= max(1.0, 0.01 * max(a, b))


def _vista_di(b, a) -> bool:
    """`b` e' un'altra VISTA del pezzo `a` (proiezione ortogonale): le viste
    stanno allineate in orizzontale o in verticale, hanno in comune la misura
    lungo quell'asse (larghezza della pianta = larghezza del prospetto) e, per
    una lamiera, la vista di lato e' sottile (l'altra misura e' l'altezza
    delle ali: al massimo il 35% del pezzo). Due pezzi della stessa altezza
    messi in fila restano due pezzi."""
    ax0, ay0, ax1, ay1 = a.bounds
    bx0, by0, bx1, by1 = b.bounds
    aw, ah, bw, bh = ax1 - ax0, ay1 - ay0, bx1 - bx0, by1 - by0
    if _stessa_misura(aw, bw) and abs(ax0 - bx0) <= max(1.0, 0.01 * aw) and bh <= 0.35 * ah:
        return True
    if _stessa_misura(ah, bh) and abs(ay0 - by0) <= max(1.0, 0.01 * ah) and bw <= 0.35 * aw:
        return True
    return False


def gruppi_pezzi(candidati: list, top_level: list, testi: list, copia_identica) -> list:
    """Pezzi piani del foglio: contorni di primo livello, non strisce di
    fianco, non profili piegati, non caselle con scritte senza fori, non piu'
    piccoli del 2% del piu' grande, non viste di un pezzo gia' preso
    (_vista_di); con i loro fori. Le copie identiche diventano un pezzo solo
    con quantita'. [{'poly', 'fori', 'quantita'}], dal piu' grande."""
    from .dxf_polygon_detector_v3 import (_Indice, _prep_buf, _riduci_fori_annidati,
                                          _n_testi_dentro, _is_rectangle_like)
    if not top_level:
        return []
    amax = max(c.area for c in top_level)
    ind = _Indice(candidati)
    scelti = []
    for c in sorted(top_level, key=lambda p: -p.area)[:200]:
        if c.area < MIN_PEZZO_REL * amax or _striscia(c) or _profilo_sottile(c):
            continue
        x0, y0, x1, y1 = c.bounds
        if min(x1 - x0, y1 - y0) < 3.0:
            continue
        pp = _prep_buf(c)
        dentro = ind.contenuti(c, pp)
        if not dentro and _is_rectangle_like(c) and _n_testi_dentro(c, testi) > 0:
            continue        # casella con scritte: cella di tabella, non pezzo
        # fori = contorni dentro non contenuti in altri contorni dentro (primo livello)
        ind_d = _Indice(dentro)
        pr = [_prep_buf(o) for o in dentro]
        fori = [o for o in dentro if not ind_d.contenitori(o, pr)]
        fori, _sv = _riduci_fori_annidati(fori)
        scelti.append((c, fori))
    gruppi: list = []
    for c, fori in scelti:
        for g in gruppi:
            if len(g['fori']) == len(fori) and copia_identica(g['poly'], c):
                g['quantita'] += 1
                break
        else:
            if any(_vista_di(c, g['poly']) for g in gruppi):
                continue
            gruppi.append({'poly': c, 'fori': fori, 'quantita': 1})
    return gruppi


def pezzi_del_foglio(candidati: list, top_level: list, testi: list, scala: float,
                     copia_identica, gruppi: list | None = None) -> list:
    """Per PIU_PEZZI: ogni pezzo con contorno, fori, area, perimetro, ingombro
    (coordinate nelle unita' del disegno, misure in mm) e quantita'."""
    from .dxf_polygon_detector_v3 import _misure
    if gruppi is None:
        gruppi = gruppi_pezzi(candidati, top_level, testi, copia_identica)
    out = []
    for g in gruppi:
        m = _misure(g['poly'], g['fori'], scala)
        b = g['poly'].bounds
        out.append({
            'quantita': g['quantita'],
            'area_dm2': m['area_dm2'], 'area_lorda_dm2': m['area_lorda_dm2'],
            'perimetro_taglio_m': m['perimetro_taglio_m'], 'n_pierce': m['n_pierce'],
            'n_fori': m['n_fori'],
            'bbox_width_mm': m['bbox_width_mm'], 'bbox_height_mm': m['bbox_height_mm'],
            'bbox': [round(v / scala, 3) for v in b],
            'contorno': [[round(x / scala, 3), round(y / scala, 3)] for x, y in g['poly'].exterior.coords],
            'fori': [[[round(x / scala, 3), round(y / scala, 3)] for x, y in f.exterior.coords]
                     for f in g['fori']],
        })
    return out
