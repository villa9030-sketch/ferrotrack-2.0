"""DXF Polygon Detector v3 — Shapely-based con confidence + candidati per UI manuale.

Riscrittura completa che sostituisce l'algoritmo custom del v2 con Shapely
(libreria industriale per operazioni geometriche 2D). Vantaggi:

- Polygon.area / .length / .contains sono robusti e testati su 10+ anni di prod
- Nested holes detection via `.contains()` diretto invece di raycasting custom
- Difference / union / buffer disponibili se serve refinement
- Prepared geometries per performance su tante contains() queries

Output arricchito rispetto al v2:
- Lista completa candidati con score/rank
- Confidence globale (0-1) — se bassa, frontend chiede conferma manuale
- Per ogni candidato: bbox, area, n_holes, geometry_json (per rendering overlay)

Unità: tutti i calcoli interni sono in MILLIMETRI (il DXF viene scalato secondo
$INSUNITS). Le COORDINATE restituite (candidates.geometry, candidates.bbox,
dxf_bbox_mm) e quelle ricevute (click, regione) restano invece nelle unità del
disegno, perché il frontend le sovrappone all'SVG del DXF originale. Per i DXF
in mm (il 99% dei casi) le due cose coincidono.

Helper condivisi (usati anche da pick_part e dxf_scanner):
- `entita_espanse`   — itera il modelspace espandendo gli INSERT (blocchi)
- `colore_effettivo` — colore reale dell'entità (risolve BYLAYER/BYBLOCK)
- `scala_unita_mm`   — fattore unità disegno → mm da $INSUNITS

API pubblica: `detect_pezzo_geometry_v3(path, config) -> dict`
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any

import ezdxf
from ezdxf.path import make_path

try:
    from shapely.geometry import Polygon, Point, MultiPolygon
    from shapely.geometry.polygon import orient
    from shapely.prepared import prep
    from shapely.validation import make_valid
    _HAS_SHAPELY = True
except ImportError:
    _HAS_SHAPELY = False

logger = logging.getLogger(__name__)


# Formati foglio ISO standard (mm) — larghezza x altezza (ordinato)
FORMATI_FOGLIO_ISO_MM = [
    (1189.0, 841.0), (841.0, 594.0), (594.0, 420.0),
    (420.0, 297.0), (297.0, 210.0),
]

# Layer che tipicamente contengono cartiglio / annotazioni (case insensitive).
# 'pieg'/'bend'/'fold'/'bieg': layer delle LINEE DI PIEGA — sono marcature, non
# contorni di taglio (spesso colorate BYLAYER, quindi il filtro colore non basta).
LAYER_DA_ESCLUDERE_PATTERNS = (
    'dim', 'quote', 'quota', 'text', 'testo', 'annot',
    'hatch', 'tratteggio', 'cartiglio', 'frame', 'border',
    'cornice', 'logo', 'symb', 'mark', 'note', 'tit', 'format',
    'pieg', 'bend', 'fold', 'bieg',
)

# Layer delle linee di piega (sottoinsieme dei precedenti, usato dallo scanner)
LAYER_PIEGA_PATTERNS = ('pieg', 'bend', 'fold', 'bieg')

# INSERT non più in elenco: i blocchi vengono ESPANSI (vedi entita_espanse)
TIPI_ANNOTAZIONE = {
    'DIMENSION', 'MTEXT', 'TEXT', 'ATTRIB', 'ATTDEF',
    'LEADER', 'MULTILEADER', 'HATCH', 'IMAGE', 'VIEWPORT',
    'ARC_DIMENSION', 'TOLERANCE', 'WIPEOUT',
}

# Blocchi di ANNOTAZIONE (note, tabelle fori, cartigli, simboli): non si espandono
# come geometria di taglio. I testi al loro interno invece si leggono (cartiglio).
# 'sw_' = blocchi annotazione esportati da SolidWorks (SW_NOTE, SW_TABLEANNOTATION,
# SW_DATUMORIGIN, ...), verificati sui DXF reali in _test_input/regression.
_BLOCCHI_ANNOTAZIONE_PATTERNS = (
    'sw_', 'note', 'nota', 'table', 'tabell', 'title', 'titol', 'cartig',
    'logo', 'datum', 'symb', 'simbol', 'format', 'border', 'frame', 'cornice',
    'revis', 'weld', 'sald', 'rugos', 'rough', 'arrow', 'frecc',
)
MAX_PROFONDITA_BLOCCHI = 8

# Unità $INSUNITS gestite: codice → (fattore → mm, nome). Le unità dichiarate
# si accettano solo se danno un disegno plausibile (vedi scala_unita_mm);
# altrimenti l'header è sbagliato (tipico: geometria in mm ma $INSUNITS=6 metri,
# default di ezdxf e di alcuni esportatori) → si assumono mm con warning.
_UNITA_DXF = {
    1: (25.4, 'pollici'),
    2: (304.8, 'piedi'),
    5: (10.0, 'centimetri'),
    6: (1000.0, 'metri'),
    14: (100.0, 'decimetri'),
}
# Estensione plausibile del disegno convertito in mm (pezzo lamiera, al massimo
# con la cornice di un foglio A0): 5 mm … 13 m
_ESTENSIONE_MM_PLAUSIBILE = (5.0, 13000.0)
# $INSUNITS=6 (metri) è creduto solo se l'estensione in metri è plausibile per un
# pezzo di lamiera (0.005–6 m) E letta come mm sarebbe assurdamente piccola (<5).
_METRI_PLAUSIBILI = (0.005, 6.0)
_MM_IMPLAUSIBILE = 5.0

# Tolleranze
# BUG FIX #5: TOL_ENDPOINT_MM ora ADATTIVO in base alla dimensione del DXF.
# Prima era fisso a 0.5mm → falliva su DXF con gap 0.6-1.5mm tipici di
# SolidWorks/AutoCAD (round-off doppia precisione + import/export tra formati).
# Alzarlo globalmente a 2mm però rompeva pezzi piccoli (chain spurie).
# Formula: `max(0.5, min_bbox_dim * 0.002)` — 0.2% del lato più corto, minimo 0.5mm.
# Per un pezzo 50×50mm → tol=0.5mm. Per un pezzo 1000×500mm → tol=1mm.
# Per un pezzo 2000×1500mm → tol=3mm.
TOL_ENDPOINT_MM = 0.5  # default (usato se DXF bbox non calcolabile)
TOL_CARTIGLIO_PCT = 0.05
FLATTEN_DISTANCE_MM = 0.2
MIN_VERTICI_CHAIN = 4
# 2 entità bastano (es. mezza ELLIPSE/ARC + LINE = contorno a "D"): gli artefatti
# a-b-a (linea sovrapposta a sé stessa) hanno < 4 vertici o area nulla e cadono
# comunque, e i tratti duplicati sono rimossi prima (_dedup_aperti).
MIN_SEGMENTI_CHAIN = 2
DEDUP_TOL_MM = 0.01                  # entità duplicate/sovrapposte (stesso tratto disegnato 2 volte)
TOL_CONTENIMENTO_MM = 0.05           # bordo coincidente ammesso nei test di contenimento


def _adaptive_tol(dxf_min_dim_mm: float) -> float:
    """Ritorna tolleranza endpoint chain-walking scalata al DXF.
    Usa 0.2% del lato più corto del bbox globale, con floor 0.5mm e ceiling 3mm."""
    if dxf_min_dim_mm <= 0:
        return TOL_ENDPOINT_MM
    return max(0.5, min(3.0, dxf_min_dim_mm * 0.002))
MIN_AREA_MM2 = 1.0                   # sotto questa area = artefatto/rumore
MIN_LARGHEZZA_MM = 0.3               # contorni più sottili = marcature, non tagli
RATIO_RETTANGOLO_PURO = 0.95         # ratio area/bbox_area ≥ questo = rettangolo
MAX_VERTICI_RETTANGOLO = 12
MIN_BBOX_CORNICE_MM = 200.0          # (legacy, non più usato come criterio da solo)
MIN_CONTENUTI_CORNICE = 6            # (legacy, non più usato come criterio da solo)
CORNICE_TOCCO_MM = 0.5               # contenuto a ≤ N mm dal bordo = cella cartiglio
FORO_AREA_MAX_REL = 0.05             # un "foro tipico" occupa ≤ 5% del contenitore
PEZZI_CONFRONTABILI_REL = 0.30       # altro contorno ≥ 30% dell'area = pezzo alternativo

# Confidence thresholds
CONF_ALTA = 0.85
CONF_MEDIA = 0.6
CONF_BASSA = 0.3


# ============================================================================
# Unità, blocchi, colori (helper condivisi)
# ============================================================================

def _estensione_raw(doc) -> float:
    """Lato maggiore dell'estensione del modelspace, in unità disegno (cache sul doc)."""
    cached = getattr(doc, '_ft_estensione_raw', None)
    if cached is not None:
        return cached
    est = 0.0
    try:
        from ezdxf import bbox as _bbox
        bb = _bbox.extents(doc.modelspace(), fast=True)
        if bb.has_data:
            est = max(bb.extmax.x - bb.extmin.x, bb.extmax.y - bb.extmin.y)
    except Exception:
        est = 0.0
    try:
        doc._ft_estensione_raw = est
    except Exception:
        pass
    return est


def scala_unita_mm(doc) -> tuple[float, str | None]:
    """Fattore di conversione unità disegno → mm letto da $INSUNITS.

    Returns (fattore, warning|None). 0 (unitless) → 1.0 + warning; 4 (mm) → 1.0.
    Se le unità dichiarate danno un pezzo implausibile (> ~6 m) l'header è
    ritenuto sbagliato e si assumono mm (con warning).
    """
    cached = getattr(doc, '_ft_scala_mm', None)
    if cached is not None:
        return cached
    try:
        u = int(doc.header.get('$INSUNITS', 0) or 0)
    except Exception:
        u = 0
    if u == 4:
        res = (1.0, None)
    elif u == 0:
        res = (1.0, 'Unità del disegno non specificate ($INSUNITS=0): assunti millimetri — verificare le quote')
    elif u not in _UNITA_DXF:
        res = (1.0, f'Unità del disegno non gestite ($INSUNITS={u}): assunti millimetri — verificare le quote')
    else:
        f, nome = _UNITA_DXF[u]
        est = _estensione_raw(doc)
        if u == 6:
            plausibile = (_METRI_PLAUSIBILI[0] <= est <= _METRI_PLAUSIBILI[1]
                          and est < _MM_IMPLAUSIBILE)
        else:
            plausibile = (est <= 0 or
                          _ESTENSIONE_MM_PLAUSIBILE[0] <= est * f <= _ESTENSIONE_MM_PLAUSIBILE[1])
        if not plausibile:
            res = (1.0, f'Il DXF dichiara unità "{nome}" ma le misure ({est:g}) sono '
                        f'plausibili solo in millimetri: assunti millimetri')
        else:
            res = (f, f'Disegno in {nome}: misure convertite in mm (×{f:g})')
    try:
        doc._ft_scala_mm = res
    except Exception:
        pass
    return res


def _blocco_annotazione(insert) -> bool:
    """True se l'INSERT richiama un blocco di annotazione (nota, tabella, simbolo)."""
    if getattr(insert, '_ft_annotazione', False):
        return True
    name = str(insert.dxf.get('name', '') or '').lower()
    if name.startswith('*d') or name.startswith('*t'):
        return True  # blocchi anonimi di quote/tabelle
    return any(p in name for p in _BLOCCHI_ANNOTAZIONE_PATTERNS)


def entita_espanse(layout, solo_geometria: bool = True, _depth: int = 0):
    """Itera le entità di `layout` espandendo gli INSERT (blocchi), anche annidati.

    Usa `virtual_entities()` di ezdxf: gestisce scala, rotazione, specchiatura,
    MINSERT e blocchi annidati. Convenzioni DXF applicate alle entità del blocco:
    layer '0' → layer dell'INSERT, colore BYBLOCK → colore dell'INSERT.

    solo_geometria=True: salta i blocchi di annotazione (note, tabelle fori,
    cartiglio, simboli) — per detector/pick_part/lavorazioni.
    solo_geometria=False: espande TUTTO e restituisce anche gli ATTRIB degli
    INSERT — per leggere i testi del cartiglio (spesso dentro un blocco). Le
    entità nate da blocchi di annotazione hanno `_ft_annotazione = True`.
    """
    for e in layout:
        if e.dxftype() != 'INSERT':
            yield e
            continue
        if _depth >= MAX_PROFONDITA_BLOCCHI:
            continue
        annot = _blocco_annotazione(e)
        if solo_geometria and annot:
            continue
        if not solo_geometria:
            try:
                for a in e.attribs:
                    if annot:
                        a._ft_annotazione = True
                    yield a
            except Exception:
                pass
        try:
            refs = list(e.multi_insert()) if getattr(e, 'mcount', 1) > 1 else [e]
        except Exception:
            refs = [e]
        layer = e.dxf.get('layer', '0')
        colore = e.dxf.get('color', 256)
        for ref in refs:
            try:
                virt = list(ref.virtual_entities())
            except Exception as ex:
                logger.debug('espansione INSERT %s fallita: %s', e.dxf.get('name'), ex)
                continue
            for ve in virt:
                try:
                    if ve.dxf.get('layer', '0') == '0':
                        ve.dxf.layer = layer
                    if ve.dxf.get('color', 256) == 0:
                        ve.dxf.color = colore
                    if annot:
                        ve._ft_annotazione = True
                except Exception:
                    pass
            yield from entita_espanse(virt, solo_geometria, _depth + 1)


def colore_effettivo(entity) -> int:
    """Colore ACI reale: colore esplicito, altrimenti colore del layer (BYLAYER).
    BYBLOCK non risolto (fuori da un blocco) → 7."""
    try:
        c = int(entity.dxf.get('color', 256))
    except Exception:
        c = 256
    if c == 256:
        try:
            lay = entity.doc.layers.get(entity.dxf.get('layer', '0'))
            c = abs(int(lay.dxf.color))
        except Exception:
            c = 7
    if c == 0:
        c = 7
    return c


def _layer_piega(layer_name: str) -> bool:
    n = (layer_name or '').strip().lower()
    return bool(n) and any(p in n for p in LAYER_PIEGA_PATTERNS)


# ============================================================================
# Layer / entity filtering
# ============================================================================

def _layer_da_escludere(layer_name: str) -> bool:
    n = (layer_name or '').strip().lower()
    if not n:
        return False
    return any(p in n for p in LAYER_DA_ESCLUDERE_PATTERNS)


def _entity_color_excluded(entity, colori_esclusi: set[int]) -> bool:
    """True se il colore EFFETTIVO (anche BYLAYER) è tra quelli esclusi."""
    try:
        return colore_effettivo(entity) in colori_esclusi
    except Exception:
        return False


def _flatten_entity(entity, distance: float = FLATTEN_DISTANCE_MM) -> list[tuple[float, float]] | None:
    try:
        p = make_path(entity)
        if len(p) == 0:
            return None
        verts = [(v.x, v.y) for v in p.flattening(distance)]
        return verts if len(verts) >= 2 else None
    except Exception:
        return None


def _is_closed_geom(verts: list[tuple[float, float]], tol: float = TOL_ENDPOINT_MM) -> bool:
    if len(verts) < 3:
        return False
    return math.hypot(verts[-1][0] - verts[0][0], verts[-1][1] - verts[0][1]) <= tol


# ============================================================================
# Estrazione poligoni raw
# ============================================================================

def _extract_polygons(msp, colori_esclusi: set[int], scala: float = 1.0) -> dict:
    """Raccoglie i contorni dal layout (INSERT espansi), già scalati in mm.

    Returns dict:
        chiusi, aperti          — geometria "normale" (list[verts] / list[(s, e, verts)])
        chiusi_col, aperti_col  — entità con colore piega/saldatura (effettivo)
        centri_cerchi           — centri (mm) dei CIRCLE normali (per lo scoring)
    """
    out = {'chiusi': [], 'aperti': [], 'chiusi_col': [], 'aperti_col': [], 'centri_cerchi': []}
    f = float(scala or 1.0)
    dist = FLATTEN_DISTANCE_MM / f  # deflessione costante in mm anche per DXF in pollici

    def _sc(verts):
        if f == 1.0:
            return [(float(x), float(y)) for x, y in verts]
        return [(x * f, y * f) for x, y in verts]

    for entity in entita_espanse(msp, solo_geometria=True):
        et = entity.dxftype()
        if et in TIPI_ANNOTAZIONE:
            continue
        try:
            if _layer_da_escludere(entity.dxf.layer):
                continue
        except AttributeError:
            pass
        col = _entity_color_excluded(entity, colori_esclusi)
        chiusi = out['chiusi_col'] if col else out['chiusi']
        aperti = out['aperti_col'] if col else out['aperti']

        if et == 'LINE':
            try:
                s = entity.dxf.start
                e = entity.dxf.end
                a, b = (s.x * f, s.y * f), (e.x * f, e.y * f)
                if a != b:
                    aperti.append((a, b, [a, b]))
            except AttributeError:
                pass
            continue
        if et not in ('CIRCLE', 'ELLIPSE', 'LWPOLYLINE', 'POLYLINE', 'SPLINE', 'ARC'):
            continue
        verts = _flatten_entity(entity, dist)
        if verts is None:
            continue
        verts = _sc(verts)
        if et == 'CIRCLE':
            if len(verts) >= 3:
                chiusi.append(verts)
                if not col:
                    try:
                        c = entity.ocs().to_wcs(entity.dxf.center)
                        out['centri_cerchi'].append((c.x * f, c.y * f))
                    except Exception:
                        pass
            continue
        if et in ('LWPOLYLINE', 'POLYLINE'):
            try:
                closed_flag = bool(entity.closed) if et == 'LWPOLYLINE' else bool(getattr(entity, 'is_closed', False))
            except Exception:
                closed_flag = False
            if closed_flag or _is_closed_geom(verts):
                chiusi.append(verts)
            else:
                aperti.append((verts[0], verts[-1], verts))
            continue
        if et == 'ARC':
            try:
                sweep = (entity.dxf.end_angle - entity.dxf.start_angle) % 360.0
            except Exception:
                sweep = 0.0
            if sweep >= 359.9 or _is_closed_geom(verts):
                chiusi.append(verts)
            else:
                aperti.append((verts[0], verts[-1], verts))
            continue
        # SPLINE / ELLIPSE: chiusi se gli estremi coincidono, altrimenti tratti
        # aperti da concatenare (prima le ELLIPSE aperte venivano scartate).
        if _is_closed_geom(verts):
            chiusi.append(verts)
        else:
            aperti.append((verts[0], verts[-1], verts))

    return out


def _dedup_aperti(opens: list, tol: float = DEDUP_TOL_MM) -> tuple[list, int]:
    """Rimuove tratti aperti duplicati/sovrapposti (stessi estremi e stessa forma
    entro `tol`, in qualsiasi verso). Un contorno disegnato due volte altrimenti
    rompe il chain walking o raddoppia il perimetro."""
    if len(opens) < 2:
        return list(opens), 0
    cell = max(tol, 1e-6) * 4
    grid: dict = {}
    kept: list = []
    n_dup = 0

    def _k(p):
        return (math.floor(p[0] / cell), math.floor(p[1] / cell))

    def _near(p, q):
        return abs(p[0] - q[0]) <= tol and abs(p[1] - q[1]) <= tol

    def _baricentro(v):
        n = len(v)
        return (sum(x for x, _ in v) / n, sum(y for _, y in v) / n)

    for item in opens:
        s, e, v = item
        c = _baricentro(v)
        kx, ky = _k(s)
        dup = False
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((kx + dx, ky + dy), ()):
                    s2, e2, v2 = kept[j]
                    if not ((_near(s, s2) and _near(e, e2)) or (_near(s, e2) and _near(e, s2))):
                        continue
                    if _near(c, _baricentro(v2)):
                        dup = True
                        break
                if dup:
                    break
            if dup:
                break
        if dup:
            n_dup += 1
            continue
        kept.append(item)
        idx = len(kept) - 1
        grid.setdefault(_k(s), []).append(idx)
        if _k(e) != _k(s):
            grid.setdefault(_k(e), []).append(idx)
    return kept, n_dup


def _dedup_poligoni(polys: list, tol: float = 0.05) -> tuple[list, int]:
    """Rimuove poligoni duplicati (stesso contorno disegnato 2 volte, anche come
    entità diverse: es. CIRCLE + LWPOLYLINE a 2 bulge). Tiene il primo."""
    if len(polys) < 2:
        return list(polys), 0
    kept: list = []
    grid: dict = {}
    cell = 1.0
    n_dup = 0
    h_max = FLATTEN_DISTANCE_MM + tol
    for p in polys:
        b = p.bounds
        k = (math.floor(b[0] / cell), math.floor(b[1] / cell))
        dup = False
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((k[0] + dx, k[1] + dy), ()):
                    q = kept[j]
                    qb = q.bounds
                    if any(abs(b[i] - qb[i]) > tol for i in range(4)):
                        continue
                    if abs(p.area - q.area) > 0.005 * max(p.area, q.area) + tol:
                        continue
                    try:
                        if p.hausdorff_distance(q) > h_max:
                            continue
                    except Exception:
                        continue
                    dup = True
                    break
                if dup:
                    break
            if dup:
                break
        if dup:
            n_dup += 1
            continue
        kept.append(p)
        grid.setdefault(k, []).append(len(kept) - 1)
    return kept, n_dup


def _chain_polygons(opens: list, tol: float = TOL_ENDPOINT_MM) -> list[list[tuple[float, float]]]:
    """Assembla open segments in poligoni chiusi via walk endpoint.

    Join endpoint per DISTANZA reale ≤ tol (indice a griglia + ricerca nelle 9
    celle vicine), non per arrotondamento a griglia: due estremi a 0.02mm ma
    a cavallo di una cella prima non si agganciavano.
    """
    if not opens:
        return []

    cell = max(tol, 1e-6)

    def _cella(pt):
        return (math.floor(pt[0] / cell), math.floor(pt[1] / cell))

    grid: dict = {}
    for i, (s, e, _v) in enumerate(opens):
        grid.setdefault(_cella(s), []).append((i, 'start', s))
        grid.setdefault(_cella(e), []).append((i, 'end', e))

    def _vicini(pt):
        cx, cy = _cella(pt)
        res = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for (i, kind, q) in grid.get((cx + dx, cy + dy), ()):
                    d = math.hypot(q[0] - pt[0], q[1] - pt[1])
                    if d <= tol:
                        res.append((d, i, kind))
        res.sort(key=lambda r: r[0])
        return res

    visited = set()
    polys = []

    for seed in range(len(opens)):
        if seed in visited:
            continue
        chain = []
        cur = seed
        orient_dir = 'forward'
        start_pt = opens[seed][0]
        last_pt = start_pt
        local = set()

        for _ in range(len(opens) + 5):
            if cur in local:
                break
            local.add(cur)
            s, e, v = opens[cur]
            if orient_dir == 'forward':
                chain.extend(v if not chain else v[1:])
                nxt_pt = e
            else:
                rev = list(reversed(v))
                chain.extend(rev if not chain else rev[1:])
                nxt_pt = s
            last_pt = nxt_pt

            if len(chain) >= MIN_VERTICI_CHAIN and math.hypot(last_pt[0] - start_pt[0], last_pt[1] - start_pt[1]) <= tol:
                if len(local) >= MIN_SEGMENTI_CHAIN:
                    visited.update(local)
                    polys.append(chain)
                break

            nxt_idx = None
            nxt_ori = 'forward'
            for _d, ci, ct in _vicini(last_pt):
                if ci in local or ci in visited:
                    continue
                nxt_idx = ci
                nxt_ori = 'forward' if ct == 'start' else 'backward'
                break
            if nxt_idx is None:
                break
            cur = nxt_idx
            orient_dir = nxt_ori

    return polys


# ============================================================================
# Shapely wrapping
# ============================================================================

def _to_shapely(verts: list[tuple[float, float]]):
    """Costruisce Polygon Shapely; se non valido, tenta make_valid."""
    if len(verts) < 3:
        return None
    try:
        poly = Polygon(verts)
        if not poly.is_valid:
            poly = make_valid(poly)
            if hasattr(poly, 'geoms'):
                # MultiPolygon → prendi il più grande
                polys = [g for g in poly.geoms if g.geom_type == 'Polygon']
                if not polys:
                    return None
                poly = max(polys, key=lambda g: g.area)
            if poly.geom_type != 'Polygon':
                return None
            # contorno pieno: eventuali "buchi" creati da make_valid non sono fori
            poly = Polygon(poly.exterior)
        if poly.area < MIN_AREA_MM2:
            return None
        # Striscia più sottile del taglio laser (larghezza media 2·A/P < 0.3mm):
        # è una marcatura/incisione o un artefatto, non un contorno da tagliare
        # (20R201N0401: 4 strisce 86×0.1mm contate come fori → perimetro +0.69m).
        if poly.length > 0 and 2.0 * poly.area / poly.length < MIN_LARGHEZZA_MM:
            return None
        return poly
    except Exception as e:
        logger.debug("shapely polygon creation failed: %s", e)
        return None


def _contiene(outer, other, prep_outer=None) -> bool:
    """Contenimento VERO di poligono (non del representative_point, che per un
    rettangolo con foro centrale cade dentro il foro). Bordo coincidente ammesso
    entro TOL_CONTENIMENTO_MM."""
    if other is outer or other.area >= outer.area * 0.999:
        return False
    ob, xb, t = outer.bounds, other.bounds, TOL_CONTENIMENTO_MM
    if xb[0] < ob[0] - t or xb[1] < ob[1] - t or xb[2] > ob[2] + t or xb[3] > ob[3] + t:
        return False
    try:
        pp = prep_outer if prep_outer is not None else prep(outer.buffer(TOL_CONTENIMENTO_MM))
        return pp.contains(other)
    except Exception:
        return False


def _prep_buf(poly):
    return prep(poly.buffer(TOL_CONTENIMENTO_MM))


def _is_iso_format(poly) -> bool:
    """True se bbox del poligono coincide con formato foglio ISO."""
    minx, miny, maxx, maxy = poly.bounds
    w = maxx - minx
    h = maxy - miny
    w_max = max(w, h)
    h_min = min(w, h)
    for ws, hs in FORMATI_FOGLIO_ISO_MM:
        if (abs(w_max - ws) / ws <= TOL_CARTIGLIO_PCT and
                abs(h_min - hs) / hs <= TOL_CARTIGLIO_PCT):
            return True
    return False


def _is_rectangle_like(poly, ratio_min: float = RATIO_RETTANGOLO_PURO) -> bool:
    """True se area/bbox_area ≥ ratio_min (forma quasi rettangolare)."""
    minx, miny, maxx, maxy = poly.bounds
    bbox_area = (maxx - minx) * (maxy - miny)
    if bbox_area <= 0:
        return False
    return (poly.area / bbox_area) >= ratio_min


def _foro_tipico(p, contenitore) -> bool:
    """Un foro di una lamiera è piccolo rispetto al pezzo (≤ 5% dell'area)."""
    return p.area <= FORO_AREA_MAX_REL * contenitore.area


def _is_cornice_cartiglio(poly, all_polys: list, min_bbox_mm: float = MIN_BBOX_CORNICE_MM,
                          min_contenuti: int = MIN_CONTENUTI_CORNICE) -> bool:
    """Cornice/cartiglio = rettangolo che contiene ALTRO oltre ai propri fori.

    BUG FIX D1: prima bastavano ≥3 poligoni contenuti (con bbox ≥200mm) o ≥6
    contenuti → una piastra 300×200 con 4 fori veniva scartata come cornice e si
    quotava un foro Ø10. Ora un rettangolo è cornice solo se:
      a) contiene un "pezzo": un contorno non-foro (>5% dell'area) che a sua volta
         contiene altri contorni (il pezzo con i suoi fori dentro la cornice), OPPURE
      b) contiene contorni che TOCCANO il suo bordo (celle del cartiglio attaccate
         alla cornice: i fori di una lamiera non toccano mai il bordo), OPPURE
      c) ha formato foglio ISO e contiene almeno un contorno non-foro.
    Una lamiera con soli fori (piccoli, staccati dal bordo) resta un pezzo.
    """
    if not _is_rectangle_like(poly):
        return False
    pp = _prep_buf(poly)
    contenuti = [o for o in all_polys if _contiene(poly, o, pp)]
    if not contenuti:
        return False
    bordo = poly.exterior
    for o in contenuti:
        if o.area < 0.9 * poly.area and bordo.distance(o) <= CORNICE_TOCCO_MM:
            return True  # b) cella attaccata alla cornice
    grandi = [o for o in contenuti if not _foro_tipico(o, poly)]
    for o in grandi:
        po = _prep_buf(o)
        if any(_contiene(o, x, po) for x in contenuti if x is not o):
            return True  # a) c'è un pezzo con fori dentro
    if grandi and _is_iso_format(poly):
        return True  # c) foglio ISO con dentro un disegno
    return False


def _separa_cartiglio(all_polys: list) -> tuple[list, int]:
    """Divide i poligoni in (candidati pezzo, n_scartati come cornice/cartiglio).

    Oltre alle cornici (_is_cornice_cartiglio) scarta i contorni che TOCCANO il
    bordo di una cornice: sono le celle del cartiglio / le strisce d'intestazione
    (un pezzo non è mai disegnato attaccato alla cornice del foglio).
    """
    cornici = [p for p in all_polys if _is_cornice_cartiglio(p, all_polys)]
    if not cornici:
        return list(all_polys), 0
    ids_cornici = {id(c) for c in cornici}
    candidati = []
    n = len(cornici)
    for p in all_polys:
        if id(p) in ids_cornici:
            continue
        if any(c.exterior.distance(p) <= CORNICE_TOCCO_MM for c in cornici):
            n += 1
            continue
        candidati.append(p)
    return candidati, n


def _riduci_fori_annidati(inners: list) -> tuple[list, int]:
    """Contorni interni annidati in altri contorni interni.

    - Concentrici (svasatura: passante + smusso) → il laser taglia SOLO il
      passante (il più piccolo); lo smusso è lavorazione successiva, contata come
      svasatura dallo scanner. Coerente con pick_part._holes_inside.
    - Non concentrici (isola dentro un foro) → si tiene il foro esterno: l'isola
      cade con lo sfrido, non è un taglio del pezzo.
    Returns (fori_tenuti, n_svasature_scartate).
    """
    if len(inners) < 2:
        return list(inners), 0
    drop = set()
    n_svas = 0
    order = sorted(range(len(inners)), key=lambda k: -inners[k].area)
    for i in order:
        if i in drop:
            continue
        a = inners[i]
        pa = _prep_buf(a)
        for j in order:
            if j == i or j in drop or i in drop:
                continue
            b = inners[j]
            if not _contiene(a, b, pa):
                continue
            r_b = (b.area / math.pi) ** 0.5
            if a.centroid.distance(b.centroid) <= max(0.5, 0.15 * r_b):
                drop.add(i)   # a è lo smusso della svasatura → resta il passante b
                n_svas += 1
            else:
                drop.add(j)   # b è un'isola dentro il foro a
    return [p for k, p in enumerate(inners) if k not in drop], n_svas


# ============================================================================
# Confidence scoring
# ============================================================================

def _score_candidate(poly, all_polys: list, circles_centri: list) -> dict:
    """Calcola score/features di un candidato pezzo.

    Score composto da:
    - n_circles: n° CIRCLE contenuti (fori del pezzo) — segnale forte
    - n_inner: n° altri poligoni contenuti (fori/dettagli) — contenimento vero
    - area_rel: area relativa (rispetto al max)
    - is_rectangle: penalità se rettangolo puro (potrebbe essere cornice)
    """
    prep_poly = prep(poly)
    pp = _prep_buf(poly)
    n_circles = sum(1 for cx, cy in circles_centri if prep_poly.contains(Point(cx, cy)))
    n_inner = sum(1 for other in all_polys if _contiene(poly, other, pp))
    return {
        'n_circles': n_circles,
        'n_inner': n_inner,
        'area': poly.area,
        'is_rectangle': _is_rectangle_like(poly),
        'bbox': poly.bounds,
        'perimeter': poly.length,
    }


def _pick_outer_with_confidence(candidates: list, all_polys: list, circles_centri: list) -> tuple[int, float, list]:
    """Sceglie l'outer con score composito + confidence globale.

    Confidence (BUG FIX D10): prima dipendeva solo dal distacco di score tra i
    primi due candidati, e i FORI stessi erano candidati → una piastra con 1 foro
    dava 0.128 e finiva in selezione manuale. Ora si guarda la STRUTTURA:
    i contorni "di primo livello" (non contenuti in altri candidati). Un solo
    contorno esterno con i suoi fori dentro = caso chiaro = confidence alta.
    Più contorni esterni di dimensione confrontabile = ambiguo.

    Returns:
        (idx_best, confidence, all_scored)
    """
    if not candidates:
        return -1, 0.0, []

    max_area = max(c.area for c in candidates)
    scored = []
    for i, poly in enumerate(candidates):
        f = _score_candidate(poly, all_polys, circles_centri)
        # Score composito (higher = più probabile pezzo). L'AREA pesa di più dei
        # cerchi contenuti: prima (cerchi ×10, area ×5) un simbolo di proiezione
        # 12×12 con un cerchio batteva la lamiera liscia 49×326 (CPPBPA0044).
        # 1. Area relativa = 0-20 punti
        # 2. CIRCLE contenuti = +2 ciascuno (max 10)
        # 3. Sub-poligoni contenuti = +1 ciascuno (max 10)
        # 4. Rettangolo puro VUOTO = -3 (potrebbe essere una cella/cornice)
        area_rel = (f['area'] / max_area) if max_area > 0 else 0
        score = (
            area_rel * 20.0
            + min(f['n_circles'], 10) * 2.0
            + min(f['n_inner'], 10) * 1.0
            + (-3.0 if f['is_rectangle'] and f['n_inner'] == 0 else 0.0)
        )
        scored.append({'idx': i, 'poly': poly, 'features': f, 'score': score})

    scored.sort(key=lambda x: x['score'], reverse=True)
    best = scored[0]
    best_idx = best['idx']
    outer = best['poly']

    # Contorni di primo livello: non contenuti in nessun altro candidato
    preps = [(_prep_buf(c), c) for c in candidates]
    top_level = [c for c in candidates
                 if not any(o is not c and _contiene(o, c, pp) for pp, o in preps)]
    if not any(c is outer for c in top_level):
        # il migliore è DENTRO un altro contorno: scelta dubbia
        confidence = 0.4
    else:
        altri = [c for c in top_level if c is not outer]
        rapporto = max((c.area for c in altri), default=0.0) / outer.area if outer.area > 0 else 1.0
        if not altri:
            confidence = 0.95
        elif rapporto < 0.10:
            confidence = 0.9      # altri contorni piccoli (viste laterali, simboli)
        elif rapporto < PEZZI_CONFRONTABILI_REL:
            confidence = 0.75
        else:
            # altro contorno di dimensione confrontabile: decide lo score
            ids_altri = {id(a) for a in altri}
            gap = best['score'] - max(s['score'] for s in scored if id(s['poly']) in ids_altri)
            confidence = 0.65 if gap >= 10.0 else 0.45

    return best_idx, round(confidence, 3), scored


def _poligoni_documento(doc, cfg: dict) -> dict:
    """Pipeline comune: contorni (mm) del documento, dedup, colori, chain walking.

    Returns dict: polys, centri_cerchi, scala, warnings, n_raw, n_dup.
    """
    colori_esclusi = set(cfg.get('dxf_colori_piega', [2])) | set(cfg.get('dxf_colori_saldatura', [1]))
    warnings: list[str] = []
    scala, w_unita = scala_unita_mm(doc)
    if w_unita:
        warnings.append(w_unita)
    msp = doc.modelspace()

    raw = _extract_polygons(msp, colori_esclusi, scala)
    opens, n_dup_a = _dedup_aperti(raw['aperti'])
    opens_col, _ = _dedup_aperti(raw['aperti_col'])

    # Chain walking: tolleranza adattiva basata sul bbox del DXF
    # (BUG FIX #5 — piccoli pezzi 0.5mm, grandi pezzi fino 3mm)
    _dxf_min_dim = 0
    try:
        _all_verts = []
        for _v in raw['chiusi'] + [o[2] for o in opens]:
            _all_verts.extend(_v)
        if _all_verts:
            _xs = [_v[0] for _v in _all_verts]
            _ys = [_v[1] for _v in _all_verts]
            _dxf_min_dim = min(max(_xs) - min(_xs), max(_ys) - min(_ys))
    except Exception:
        pass
    _tol = _adaptive_tol(_dxf_min_dim)
    chained = _chain_polygons(opens, tol=_tol)
    n_raw = len(raw['chiusi']) + len(chained)

    polys = [p for p in (_to_shapely(v) for v in raw['chiusi'] + chained) if p is not None]

    # BUG FIX D7: il colore "saldatura/piega" non deve cancellare il CONTORNO del
    # pezzo. Le marcature piega/saldatura sono linee aperte; un contorno chiuso
    # colorato che è il più grande (o racchiude la geometria) resta geometria.
    if raw['chiusi_col'] or opens_col:
        chained_col = _chain_polygons(opens_col, tol=_tol)
        polys_col = [p for p in (_to_shapely(v) for v in raw['chiusi_col'] + chained_col) if p is not None]
        if polys_col:
            if not polys:
                polys = polys_col
                n_raw += len(polys_col)
                warnings.append('Contorno disegnato solo con colori piega/saldatura: usato come geometria di taglio')
            else:
                max_a = max(p.area for p in polys)
                usati = 0
                for pc in polys_col:
                    pp = _prep_buf(pc)
                    if pc.area >= max_a * 0.999 or any(_contiene(pc, p, pp) for p in polys):
                        polys.append(pc)
                        usati += 1
                if usati:
                    n_raw += usati
                    warnings.append(f'{usati} contorno/i chiuso/i col colore piega/saldatura trattati come contorno del pezzo')

    polys, n_dup_p = _dedup_poligoni(polys)
    n_dup = n_dup_a + n_dup_p
    if n_dup:
        warnings.append(f'{n_dup} entità duplicate/sovrapposte ignorate')
    return {
        'polys': polys, 'centri_cerchi': raw['centri_cerchi'], 'scala': scala,
        'warnings': warnings, 'n_raw': n_raw, 'n_dup': n_dup,
    }


def contorno_pezzo_mm(doc, cfg: dict | None = None):
    """Contorno esterno (Polygon Shapely in mm) del pezzo scelto dal detector,
    o None se la scelta è incerta (confidence < 0.5). Usato dallo scanner
    lavorazioni per sapere cosa sta DENTRO il pezzo (svasature, saldature)."""
    if not _HAS_SHAPELY:
        return None
    cached = getattr(doc, '_ft_contorno_pezzo', False)
    if cached is not False:
        return cached
    outer = None
    try:
        base = _poligoni_documento(doc, cfg or {})
        candidati, _n = _separa_cartiglio(base['polys'])
        if candidati:
            idx, conf, _sc = _pick_outer_with_confidence(candidati, candidati, base['centri_cerchi'])
            outer = candidati[idx] if idx >= 0 and conf >= 0.5 else None
    except Exception as e:
        logger.debug('contorno_pezzo_mm fallito: %s', e)
        outer = None
    try:
        doc._ft_contorno_pezzo = outer
    except Exception:
        pass
    return outer


def _dxf_bbox_raw(msp):
    try:
        from ezdxf.bbox import extents
        bb = extents(msp)
        if bb.has_data:
            return [round(bb.extmin.x, 3), round(bb.extmin.y, 3),
                    round(bb.extmax.x, 3), round(bb.extmax.y, 3)]
    except Exception:
        pass
    return None


def _misure(outer, inners, scala: float) -> dict:
    """Area netta / perimetro / pierce dall'outer + fori (tutto in mm)."""
    area_outer_mm2 = outer.area
    area_netta_mm2 = max(0.0, area_outer_mm2 - sum(p.area for p in inners))
    perim_totale_mm = outer.length + sum(p.length for p in inners)
    minx, miny, maxx, maxy = outer.bounds
    n_pierce = 1 + len(inners)
    return {
        'area_dm2': round(area_netta_mm2 / 10000.0, 4),
        'area_lorda_dm2': round(area_outer_mm2 / 10000.0, 4),
        'perimetro_taglio_m': round(perim_totale_mm / 1000.0, 4),
        'n_pierce': n_pierce,
        'n_forature': n_pierce,   # = pierce (1 contorno esterno + fori), come v2/v1
        'n_fori': len(inners),    # soli fori interni
        'n_inner': len(inners),
        'bbox_width_mm': round(maxx - minx, 2),
        'bbox_height_mm': round(maxy - miny, 2),
        'scala_unita_mm': scala,
    }


def _raw_coords(coords, scala: float):
    """mm interni → unità disegno (per overlay sull'SVG del DXF originale)."""
    if scala == 1.0:
        return [[round(x, 2), round(y, 2)] for x, y in coords]
    return [[round(x / scala, 4), round(y / scala, 4)] for x, y in coords]


# ============================================================================
# API pubblica
# ============================================================================

def detect_pezzo_geometry_v3(path: str, config: dict | None = None) -> dict:
    """Estrae geometria pezzo da DXF con Shapely + confidence + candidati.

    Returns:
        {
            area_dm2, area_lorda_dm2, perimetro_taglio_m, n_pierce, n_inner,
            n_forature (= n_pierce: 1 contorno esterno + fori), n_fori (soli fori),
            bbox_width_mm, bbox_height_mm,
            confidence: 0-1,
            confidence_label: 'alta' | 'media' | 'bassa' | 'nessuna',
            needs_manual_select: bool,
            n_pezzi_rilevati: int (contorni esterni di dimensione confrontabile),
            candidates: [
                {idx, area_dm2, perimetro_m, bbox: [minx,miny,maxx,maxy],
                 n_circles, n_inner, score, geometry: [[x,y], ...]},
                ...
            ],
            selected_candidate_idx: int,
            warnings: [str],
        }
    """
    if not _HAS_SHAPELY:
        return _empty_result(['Shapely non installato — installalo con: pip install shapely'])

    cfg = config or {}

    try:
        doc = ezdxf.readfile(path)
    except Exception as e:
        return _empty_result([f'DXF non leggibile: {e}'])

    msp = doc.modelspace()

    # ---- 1-3. Poligoni (mm): estrazione con INSERT espansi, dedup, chain walking
    base = _poligoni_documento(doc, cfg)
    all_polys = base['polys']
    scala = base['scala']
    warnings: list[str] = list(base['warnings'])
    circles_centri = base['centri_cerchi']

    if not all_polys:
        if base['n_raw'] == 0:
            return _empty_result(warnings + ['Nessun poligono chiuso rilevato'])
        return _empty_result(warnings + ['Poligoni non validi (area < 1 mm² o self-intersect)'])

    # ---- 5. Filtra cartiglio (ISO + cornici custom)
    # Un rettangolo ISO VUOTO non è più scartato a priori: potrebbe essere una
    # lamiera A4/A3; è cornice solo se contiene un disegno (vedi _is_cornice_cartiglio)
    candidati, cartiglio_count = _separa_cartiglio(all_polys)

    if not candidati:
        return _empty_result(warnings + [f'Solo {cartiglio_count} cornici/cartigli rilevati, nessun pezzo'])

    # ---- 6. Pick best outer + confidence
    best_idx, confidence, all_scored = _pick_outer_with_confidence(candidati, candidati, circles_centri)
    outer = candidati[best_idx]

    # ---- 7. Inner holes (contenuti VERAMENTE nell'outer — BUG FIX D2) + svasature (D4)
    pp_outer = _prep_buf(outer)
    inners = [p for i, p in enumerate(candidati) if i != best_idx and _contiene(outer, p, pp_outer)]
    inners, n_svas = _riduci_fori_annidati(inners)

    # ---- 8. Calcoli finali
    mis = _misure(outer, inners, scala)

    # Più pezzi nello stesso disegno: segnala invece di sceglierne uno in silenzio
    preps = [(_prep_buf(o), o) for o in candidati]
    top_level = [c for c in candidati
                 if not any(o is not c and _contiene(o, c, pp) for pp, o in preps)]
    n_pezzi = 1 + sum(1 for c in top_level
                      if c is not outer and c.area >= PEZZI_CONFRONTABILI_REL * outer.area)
    if n_pezzi > 1:
        warnings.append(f'{n_pezzi} contorni esterni di dimensioni confrontabili nel disegno: '
                        f'verificare quale pezzo quotare')
    if n_svas:
        warnings.append(f'{n_svas} svasatura/e: tagliato solo il foro passante')

    # ---- 9. Confidence label
    if confidence >= CONF_ALTA:
        conf_label = 'alta'
        needs_manual = False
    elif confidence >= CONF_MEDIA:
        conf_label = 'media'
        needs_manual = False
    elif confidence >= CONF_BASSA:
        conf_label = 'bassa'
        needs_manual = True
        warnings.append(f'Confidence bassa ({confidence:.0%}) — verificare selezione pezzo')
    else:
        conf_label = 'nessuna'
        needs_manual = True
        warnings.append(f'Nessuna confidenza sul pezzo detectato — selezione manuale richiesta')

    # ---- 10. Costruisci lista candidati per UI (top N per score)
    # Coordinate nelle unità del DISEGNO (overlay sull'SVG), misure in mm.
    candidates_out = []
    for c in all_scored[:20]:  # max 20 candidati (per non appesantire UI)
        p = c['poly']
        # Discretizza geometria per rendering overlay SVG (max 200 punti)
        coords = list(p.exterior.coords)
        if len(coords) > 200:
            step = len(coords) // 200
            coords = coords[::step]
        candidates_out.append({
            'idx': c['idx'],
            'area_dm2': round(p.area / 10000.0, 4),
            'perimetro_m': round(p.length / 1000.0, 4),
            'bbox': [round(v / scala, 4 if scala != 1.0 else 2) for v in p.bounds],
            'n_circles': c['features']['n_circles'],
            'n_inner': c['features']['n_inner'],
            'score': round(c['score'], 2),
            'is_rectangle': c['features']['is_rectangle'],
            'is_selected': c['idx'] == best_idx,
            'geometry': _raw_coords(coords, scala),
        })

    return {
        **mis,
        'confidence': confidence,
        'confidence_label': conf_label,
        'needs_manual_select': needs_manual,
        'n_pezzi_rilevati': n_pezzi,
        'candidates': candidates_out,
        'selected_candidate_idx': best_idx,
        'poligoni_grezzi': base['n_raw'],
        'poligoni_cartiglio_rimossi': cartiglio_count,
        'entita_duplicate_rimosse': base['n_dup'],
        'tipo_disegno': 'v3_shapely',
        'warnings': warnings,
        # Bbox globale DXF (unità disegno) — usato dal frontend per scale SVG→mm
        'dxf_bbox_mm': _dxf_bbox_raw(msp),
        '_engine': 'shapely-' + __import__('shapely').__version__,
    }


def _empty_result(warnings: list[str]) -> dict:
    return {
        'area_dm2': 0.0, 'area_lorda_dm2': 0.0, 'perimetro_taglio_m': 0.0,
        'n_pierce': 0, 'n_forature': 0, 'n_fori': 0, 'n_inner': 0,
        'bbox_width_mm': 0.0, 'bbox_height_mm': 0.0,
        'confidence': 0.0, 'confidence_label': 'nessuna',
        'needs_manual_select': True, 'n_pezzi_rilevati': 0,
        'candidates': [], 'selected_candidate_idx': -1,
        'poligoni_grezzi': 0, 'poligoni_cartiglio_rimossi': 0,
        'tipo_disegno': 'vuoto', 'warnings': warnings,
    }


def compute_geometry_from_region(path: str, region_bbox: tuple[float, float, float, float],
                                  config: dict | None = None) -> dict:
    """Calcola area/perim/n_pierce prendendo tutti i poligoni chiusi la cui
    bbox interseca (o è contenuta) nella region_bbox utente.

    Il pattern d'uso è: l'utente disegna col mouse un rettangolo attorno al
    pezzo nella preview DXF. Il backend prende tutti i contorni chiusi dentro
    quella regione, identifica outer (area max) e inner (contenuti nell'outer),
    calcola area netta + perimetro + n_pierce.

    Args:
        path: percorso DXF
        region_bbox: (minx, miny, maxx, maxy) in coord DXF (unità del disegno)
        config: dict optional (colori esclusi)

    Returns:
        dict compat con `detect_pezzo_geometry_v3` (senza candidates).
    """
    if not _HAS_SHAPELY:
        return _empty_result(['Shapely non installato'])
    cfg = config or {}

    try:
        doc = ezdxf.readfile(path)
    except Exception as e:
        return _empty_result([f'DXF non leggibile: {e}'])

    base = _poligoni_documento(doc, cfg)
    scala = base['scala']
    all_polys = base['polys']

    # Region utente come Polygon (unità disegno → mm)
    minx, miny, maxx, maxy = [float(v) * scala for v in region_bbox]
    if minx == maxx or miny == maxy:
        return _empty_result(['Regione degenere (larghezza o altezza = 0)'])
    from shapely.geometry import box
    region = box(min(minx, maxx), min(miny, maxy), max(minx, maxx), max(miny, maxy))

    # Filtra poligoni la cui bbox interseca la region utente
    # (`intersects` è più tollerante di `within` per selezioni approssimative)
    in_region = [p for p in all_polys if p.intersects(region)]

    # Filtri cartiglio in selezione manuale:
    # 1. Formati ISO standard (A4/A3/...)
    # 2. Bbox molto più grande della region utente (soglia 2x per lato o 3x area)
    #    → cornice/cartiglio esterno alla vera intenzione dell'utente
    region_w = abs(maxx - minx)
    region_h = abs(maxy - miny)
    region_area = region.area
    MAX_BBOX_RATIO = 2.0    # bbox width/height max 2x region
    MAX_AREA_RATIO = 3.0    # area max 3x region
    def _too_big(p):
        minx_p, miny_p, maxx_p, maxy_p = p.bounds
        pw = maxx_p - minx_p
        ph = maxy_p - miny_p
        if pw > region_w * MAX_BBOX_RATIO or ph > region_h * MAX_BBOX_RATIO:
            return True
        if p.area > region_area * MAX_AREA_RATIO:
            return True
        return False
    in_region = [p for p in in_region if not _is_cornice_cartiglio(p, all_polys) and not _too_big(p)]

    if not in_region:
        return _empty_result(['Nessun contorno chiuso trovato nella regione selezionata. Prova a disegnare un\'area più ampia (o meno ampia se hai selezionato l\'intero disegno).'])

    # Centri CIRCLE nella region (per scoring)
    circles_centri_in_region = [(cx, cy) for cx, cy in base['centri_cerchi']
                                if region.contains(Point(cx, cy))]

    # Outer = poligono che contiene PIÙ CIRCLE (segnale forte pezzo con fori)
    # Tie-break su area (per casi senza fori)
    def _score(p):
        prep_p = prep(p)
        n_circ = sum(1 for cx, cy in circles_centri_in_region if prep_p.contains(Point(cx, cy)))
        return (n_circ, p.area)
    outer = max(in_region, key=_score)
    pp = _prep_buf(outer)
    inners = [p for p in in_region if p is not outer and _contiene(outer, p, pp)]
    inners, _n_svas = _riduci_fori_annidati(inners)

    mis = _misure(outer, inners, scala)
    return {
        **mis,
        'confidence': 1.0,
        'confidence_label': 'manuale',
        'needs_manual_select': False,
        'candidates': [],
        'selected_candidate_idx': -1,
        'poligoni_grezzi': base['n_raw'],
        'poligoni_cartiglio_rimossi': 0,
        'tipo_disegno': 'v3_region_manual',
        'warnings': list(base['warnings']) + [f'Regione manuale utente: {len(in_region)} contorni trovati (1 outer + {len(inners)} interni)'],
        '_engine': 'shapely-region',
    }


def compute_geometry_from_point(path: str, x_mm: float, y_mm: float,
                                config: dict | None = None) -> dict:
    """Pattern 'Detect Part' Lantek-style: click su un ELEMENTO del contorno
    esterno del pezzo → sistema chain-walka il perimetro completo + trova
    tutta la geometria interna.

    A differenza di 'contains(point)', qui l'utente clicca SU UNA LINEA (bordo
    visibile), non nel vuoto interno del pezzo. Vantaggi:
    - Non serve trovare un punto interno (in pezzi con molti fori è difficile)
    - Funziona anche se il chain walking non chiude perfettamente il contorno
      (basta essere abbastanza vicini a un edge conosciuto)
    - Gesto naturale (sottolineare un bordo col mouse)

    Args:
        path: percorso DXF
        x_mm, y_mm: punto cliccato in coord DXF (unità del disegno)
        config: dict optional colori esclusi

    Strategia:
    1. Estrae tutti i poligoni chiusi (native + chain walking)
    2. Per ogni poligono (skip cartigli ISO), calcola la distanza minima tra
       il click point e l'exterior boundary
    3. Filtra quelli con distanza <= TOL_EDGE_MM (click abbastanza vicino a un bordo)
    4. Sort: (distanza in bucket da 2mm asc, area asc)
       → il più vicino, tie-break sul più piccolo (evita cartiglio esterno)
    5. Se nessuno entro tolleranza, fallback a "contains" per compat
    6. Trova gli inner: poligoni contenuti nell'outer scelto

    Returns: dict compat con detect_pezzo_geometry_v3
    """
    if not _HAS_SHAPELY:
        return _empty_result(['Shapely non installato'])
    cfg = config or {}

    try:
        doc = ezdxf.readfile(path)
    except Exception as e:
        return _empty_result([f'DXF non leggibile: {e}'])
    msp = doc.modelspace()

    base = _poligoni_documento(doc, cfg)
    scala = base['scala']
    all_polys = base['polys']
    click_pt = Point(float(x_mm) * scala, float(y_mm) * scala)

    # Escludi cartigli (ISO con disegno dentro / cornici)
    non_cartiglio = _separa_cartiglio(all_polys)[0]

    if not non_cartiglio:
        return _empty_result([
            f'Nessun contorno rilevato (solo cartigli). '
            f'Verifica che il DXF contenga geometria di taglio valida.'
        ])

    # ---- Strategia PRIMARIA: click SU un edge del bordo esterno ----
    # Tolleranza adattiva: 2% della dimensione minima del DXF globale, clamp [5, 30] mm
    try:
        from ezdxf.bbox import extents
        bb = extents(msp)
        if bb.has_data:
            dxf_w = (bb.extmax.x - bb.extmin.x) * scala
            dxf_h = (bb.extmax.y - bb.extmin.y) * scala
            min_dim = min(dxf_w, dxf_h)
        else:
            min_dim = 200.0
    except Exception:
        min_dim = 200.0
    TOL_EDGE_MM = max(5.0, min(30.0, min_dim * 0.02))

    candidates_with_dist = []
    for p in non_cartiglio:
        d = p.exterior.distance(click_pt)
        if d <= TOL_EDGE_MM:
            candidates_with_dist.append((d, p))

    if candidates_with_dist:
        # Bucket distanza da 2mm — poi tie-break su area (più piccolo = pezzo, non cartiglio)
        candidates_with_dist.sort(key=lambda item: (int(item[0] / 2.0), item[1].area))
        outer = candidates_with_dist[0][1]
        strategia = f'edge-click (dist={candidates_with_dist[0][0]:.1f}mm, tol={TOL_EDGE_MM:.1f}mm)'
    else:
        # ---- Fallback: click DENTRO il pezzo (contains) ----
        contengono_click = [p for p in non_cartiglio if p.contains(click_pt) or p.touches(click_pt)]
        if not contengono_click:
            return _empty_result([
                f'Nessun contorno vicino a ({x_mm:.1f}, {y_mm:.1f}) — tolleranza {TOL_EDGE_MM:.1f}mm. '
                f'Clicca SU una linea del bordo esterno del pezzo.'
            ])
        outer = min(contengono_click, key=lambda p: p.area)
        strategia = 'contains-fallback'

    # Inner: tutti i poligoni contenuti nell'outer (fori, dettagli, sub-contorni)
    pp = _prep_buf(outer)
    inners = [p for p in all_polys if p is not outer and _contiene(outer, p, pp)]
    inners, _n_svas = _riduci_fori_annidati(inners)

    mis = _misure(outer, inners, scala)
    return {
        **mis,
        'confidence': 1.0,
        'confidence_label': 'manuale',
        'needs_manual_select': False,
        'candidates': [],
        'selected_candidate_idx': -1,
        'poligoni_grezzi': base['n_raw'],
        'poligoni_cartiglio_rimossi': 0,
        'tipo_disegno': 'v3_point_click',
        'warnings': list(base['warnings']) + [f'Trova pezzo ({strategia}): {len(inners)} contorni interni trovati.'],
        # dxf_bbox_mm per compat con frontend scale factor (unità disegno)
        'dxf_bbox_mm': _dxf_bbox_raw(msp),
        '_engine': 'shapely-point',
    }


def compute_geometry_from_candidate(path: str, candidate_idx: int,
                                    config: dict | None = None) -> dict:
    """Ricalcola area/perim/n_pierce assumendo che l'utente ha scelto candidate_idx
    come outer del pezzo (invece del top-scored automatico).

    Usato dall'endpoint POST /dxf/<file>/select-polygon.
    """
    r = detect_pezzo_geometry_v3(path, config)
    if not r.get('candidates') or candidate_idx < 0:
        return r
    # Cerca candidato per idx
    cand = next((c for c in r['candidates'] if c['idx'] == candidate_idx), None)
    if not cand:
        r['warnings'] = r.get('warnings', []) + [f'Candidato {candidate_idx} non trovato']
        return r
    if not _HAS_SHAPELY:
        return r
    cfg = config or {}
    try:
        doc = ezdxf.readfile(path)
    except Exception:
        return r
    base = _poligoni_documento(doc, cfg)
    scala = base['scala']
    # Ricostruisci Polygon shapely (geometry è in unità disegno → mm)
    outer_poly = Polygon([(x * scala, y * scala) for x, y in cand['geometry']])
    if not outer_poly.is_valid:
        outer_poly = make_valid(outer_poly)
        if hasattr(outer_poly, 'geoms'):
            outer_poly = max((g for g in outer_poly.geoms if g.geom_type == 'Polygon'), key=lambda g: g.area)
    # La geometria del candidato è decimata per la UI: se esiste il poligono
    # originale corrispondente lo uso (area/perimetro esatti)
    all_polys = _separa_cartiglio(base['polys'])[0]
    for p in all_polys:
        if abs(p.area - outer_poly.area) <= 0.01 * p.area and p.hausdorff_distance(outer_poly) <= 1.0:
            outer_poly = p
            break

    pp = _prep_buf(outer_poly)
    # Trova inners: poligoni contenuti in outer che NON siano l'outer stesso
    inners = [p for p in all_polys if p is not outer_poly and _contiene(outer_poly, p, pp)]
    inners, _n_svas = _riduci_fori_annidati(inners)
    mis = _misure(outer_poly, inners, scala)

    r_out = dict(r)
    r_out.update(mis)
    r_out['selected_candidate_idx'] = candidate_idx
    r_out['confidence'] = 1.0
    r_out['confidence_label'] = 'manuale'
    r_out['needs_manual_select'] = False
    r_out['warnings'] = r_out.get('warnings', []) + [f'Selezione manuale utente: candidato #{candidate_idx}']
    return r_out
