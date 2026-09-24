"""pick_part — geometria pezzo DETERMINISTICA via Shapely polygonize.

Sostituisce l'auto-detect euristico (`detect_pezzo_geometry_v3`, che sul pezzo
reale 20PA00693 sbagliava l'area di 13,6×) con la ricetta raccomandata dalla
ricerca tecnica (piano fail-safe, sezione 3):

    flatten entità → node (unary_union) → polygonize → faccia scelta dal click

Filosofia (piano fail-safe):
- NESSUNA euristica di scelta automatica del contorno. La faccia del pezzo è
  scelta dal CLICK dell'operatore sul bordo (modello Lantek "Detect Part").
- Una volta scelta la faccia, area/perimetro/fori sono MATEMATICA PURA
  (shoelace / Shapely .length / .contains). Zero stima, zero silenzioso.
- Se il click non seleziona nessuna faccia valida → il chiamante BLOCCA e
  chiede all'operatore (mai un numero inventato).

Differenza chiave vs il vecchio chain-walking O(n!):
- `unary_union` fa il NODING robusto (spezza ai veri incroci) invece del walk
  endpoint fragile con tolleranze arbitrarie.
- `polygonize` costruisce tutte le facce del planar arrangement in modo testato.

API pubblica:
    pick_part_from_click(path, click_x_mm, click_y_mm, config) -> dict
    polygonize_faces(path, config) -> list[dict]   # per probe/debug e per il viewer
"""
from __future__ import annotations

import logging
import math
import os
from typing import Any

import ezdxf
from ezdxf.path import make_path

try:
    import shapely
    from shapely.geometry import Polygon, Point, LineString, MultiLineString
    from shapely.ops import polygonize, unary_union
    from shapely.validation import make_valid
    _HAS_SHAPELY = True
except ImportError:
    _HAS_SHAPELY = False

# Griglia di snap prima del polygonize: chiude i MICRO-GAP (tipici tra arco e linea
# di un'asola, es. 0,1mm) che altrimenti impediscono al loop di chiudersi → foro
# perso. 0,1mm è sotto la tolleranza di taglio, quindi non altera la geometria.
_SNAP_GRID_MM = 0.1

logger = logging.getLogger(__name__)

# Riuso delle costanti/pattern dal detector esistente per coerenza di filtro
from .dxf_polygon_detector_v3 import (
    LAYER_DA_ESCLUDERE_PATTERNS,
    TIPI_ANNOTAZIONE,
    FORMATI_FOGLIO_ISO_MM,
    FLATTEN_DISTANCE_MM,
    MIN_AREA_MM2,
    TOL_CARTIGLIO_PCT,
    _layer_da_escludere,
    _entity_color_excluded,
    _dedup_poligoni,
    _riduci_fori_annidati,
    entita_espanse,
    scala_unita_mm,
)
try:
    from shapely.prepared import prep
except ImportError:
    pass


def _campi_forature(holes) -> dict:
    """Conteggi forature uniformi con v3/scanner/preventivo (BUG FIX D14):
    n_forature = PIERCE = 1 contorno esterno + fori (prima qui era solo i fori
    e il percorso manuale perdeva un innesco); n_fori = soli fori interni."""
    n = len(holes)
    return {'n_forature': 1 + n, 'n_pierce': 1 + n, 'n_fori': n}

# UNITÀ: tutta la pipeline di pick_part lavora in MILLIMETRI. Il DXF viene
# scalato secondo $INSUNITS (pollici, cm, m…) già in estrazione, e anche il
# viewer CAD (geometry_json) riceve coordinate in mm: click, contorni
# (outer_xy/holes_xy) e DXF canonico sono quindi coerenti in mm.


def _scala(msp) -> float:
    """Fattore unità disegno → mm del documento di `msp`."""
    try:
        return scala_unita_mm(msp.doc)[0]
    except Exception:
        return 1.0


def _cache_doc(msp, chiave, fn):
    """Memoizza calcoli costosi (facce, fori) per documento: pick_candidates
    chiama _holes_inside per ogni loop candidato."""
    doc = getattr(msp, 'doc', None)
    store = getattr(doc, '_ft_cache_pick', None) if doc is not None else None
    if store is None and doc is not None:
        store = {}
        try:
            doc._ft_cache_pick = store
        except Exception:
            store = None
    if store is not None and chiave in store:
        return store[chiave]
    val = fn()
    if store is not None:
        store[chiave] = val
    return val


# ── Estrazione segmenti ────────────────────────────────────────────────────

def _entity_segments(entity, distance: float = FLATTEN_DISTANCE_MM, scala: float = 1.0) -> list[tuple]:
    """Flatten una entità DXF in una lista di segmenti [(x1,y1,x2,y2), ...] in mm.

    Ritorna lista vuota se l'entità non è flattabile (es. TEXT).
    """
    try:
        p = make_path(entity)
        if len(p) == 0:
            return []
        pts = [(v.x * scala, v.y * scala) for v in p.flattening(distance / scala)]
    except Exception:
        # LINE non ha make_path in alcune versioni: fallback manuale
        if entity.dxftype() == 'LINE':
            try:
                s = entity.dxf.start
                e = entity.dxf.end
                return [(s.x * scala, s.y * scala, e.x * scala, e.y * scala)]
            except Exception:
                return []
        return []
    segs = []
    for a, b in zip(pts, pts[1:]):
        if a != b:
            segs.append((a[0], a[1], b[0], b[1]))
    return segs


def _entita_chiusa(entity) -> bool:
    """True se l'entità è un contorno chiuso reale (cerchio, polilinea chiusa…)."""
    et = entity.dxftype()
    if et in ('CIRCLE', 'ELLIPSE'):
        if et == 'ELLIPSE':
            try:
                sweep = (entity.dxf.end_param - entity.dxf.start_param) % (2 * math.pi)
                return sweep == 0 or sweep >= 2 * math.pi - 1e-6
            except Exception:
                return True
        return True
    if et in ('LWPOLYLINE', 'POLYLINE'):
        try:
            return bool(entity.closed) if et == 'LWPOLYLINE' else bool(getattr(entity, 'is_closed', False))
        except Exception:
            return False
    if et == 'SPLINE':
        return bool(getattr(entity, 'closed', False))
    if et == 'ARC':
        try:
            return (entity.dxf.end_angle - entity.dxf.start_angle) % 360.0 >= 359.9
        except Exception:
            return False
    return False


def _poly_da_segmenti(segs):
    """Poligono dal loop di segmenti di un'entità chiusa (None se degenere)."""
    pts = [(s[0], s[1]) for s in segs]
    if len(pts) < 3:
        return None
    try:
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = make_valid(poly)
            if hasattr(poly, 'geoms'):
                cand = [g for g in poly.geoms if g.geom_type == 'Polygon']
                if not cand:
                    return None
                poly = max(cand, key=lambda g: g.area)
        return poly if poly.geom_type == 'Polygon' and poly.area >= MIN_AREA_MM2 else None
    except Exception:
        return None


def _collect_segments(msp, colori_esclusi: set[int]) -> list[tuple]:
    """Raccoglie tutti i segmenti geometrici (mm), escludendo annotazioni/cartiglio.

    Esclude: tipi annotazione (TEXT/DIMENSION/...), layer cartiglio/quote/note/
    piega, entità di colore escluso (pieghe/saldature configurabili, colore
    EFFETTIVO anche BYLAYER). I blocchi (INSERT) vengono espansi.

    Eccezione colore (BUG FIX D7): un contorno CHIUSO colorato piega/saldatura che
    racchiude la geometria (tipico: contorno esterno disegnato in rosso) resta
    geometria del pezzo — le marcature piega/saldatura sono linee aperte.
    """
    def _calcola():
        scala = _scala(msp)
        segs = []
        chiusi_normali = []
        colorati = []   # (segmenti, poligono) dei contorni chiusi colorati
        colorati_aperti = []
        for entity in entita_espanse(msp, solo_geometria=True):
            if entity.dxftype() in TIPI_ANNOTAZIONE:
                continue
            try:
                if _layer_da_escludere(entity.dxf.layer):
                    continue
            except AttributeError:
                pass
            if _entity_color_excluded(entity, colori_esclusi):
                s = _entity_segments(entity, scala=scala)
                if _entita_chiusa(entity):
                    colorati.append((s, _poly_da_segmenti(s)))
                else:
                    colorati_aperti.extend(s)
                continue
            s = _entity_segments(entity, scala=scala)
            if _entita_chiusa(entity):
                pc = _poly_da_segmenti(s)
                if pc is not None:
                    chiusi_normali.append(pc)
            segs.extend(s)
        nessuna_normale = not segs
        if colorati:
            if segs:
                xs = [v for sg in segs for v in (sg[0], sg[2])]
                ys = [v for sg in segs for v in (sg[1], sg[3])]
                bb_norm = (min(xs), min(ys), max(xs), max(ys))
            for s, pc in colorati:
                if nessuna_normale:
                    segs.extend(s)
                    continue
                if pc is None:
                    continue
                b = pc.bounds
                racchiude_tutto = (b[0] <= bb_norm[0] + 0.01 and b[1] <= bb_norm[1] + 0.01 and
                                   b[2] >= bb_norm[2] - 0.01 and b[3] >= bb_norm[3] - 0.01)
                if racchiude_tutto or any(pc.contains(h) for h in chiusi_normali):
                    segs.extend(s)
        if nessuna_normale and colorati_aperti:
            # disegno tutto col colore piega/saldatura: è comunque il pezzo
            segs.extend(colorati_aperti)
        return segs
    return _cache_doc(msp, ('segs', frozenset(colori_esclusi)), _calcola)


def _is_iso_format_bounds(bounds) -> bool:
    """True se il bbox coincide con un formato foglio ISO (cartiglio)."""
    minx, miny, maxx, maxy = bounds
    w = maxx - minx
    h = maxy - miny
    w_max, h_min = max(w, h), min(w, h)
    for ws, hs in FORMATI_FOGLIO_ISO_MM:
        if (abs(w_max - ws) / ws <= TOL_CARTIGLIO_PCT and
                abs(h_min - hs) / hs <= TOL_CARTIGLIO_PCT):
            return True
    return False


# ── Pipeline polygonize ────────────────────────────────────────────────────

def _build_faces(path: str, colori_esclusi: set[int]) -> list:
    """flatten → node → polygonize. Ritorna lista di Polygon Shapely (facce).

    Solleva RuntimeError con messaggio esplicito se il DXF non è leggibile o
    Shapely non è installato (il chiamante deve BLOCCARE, non inventare).
    """
    if not _HAS_SHAPELY:
        raise RuntimeError('Shapely non installato')
    try:
        doc = ezdxf.readfile(path)
    except Exception as e:
        raise RuntimeError(f'DXF non leggibile: {e}')
    return _faces_from_msp(doc.modelspace(), colori_esclusi)


def _faces_from_msp(msp, colori_esclusi: set[int]) -> list:
    """flatten → node → polygonize a partire da un modelspace già aperto.
    Ritorna le facce Shapely (poligoni). Vuoto se niente segmenti."""
    if not _HAS_SHAPELY:
        return []
    return _cache_doc(msp, ('faces', frozenset(colori_esclusi)),
                      lambda: _calcola_facce(msp, colori_esclusi))


def _calcola_facce(msp, colori_esclusi: set[int]) -> list:
    segs = _collect_segments(msp, colori_esclusi)
    if not segs:
        return []
    lines = [LineString([(x1, y1), (x2, y2)]) for (x1, y1, x2, y2) in segs]
    mls = MultiLineString(lines)
    # Chiudi i micro-gap (arco↔linea nelle asole) snappando a griglia fine, poi noda.
    try:
        mls = shapely.set_precision(mls, _SNAP_GRID_MM)
    except Exception:
        pass
    # NODING: unary_union spezza i segmenti ai veri incroci (robusto, testato)
    noded = unary_union(mls)
    faces = [f for f in polygonize(noded) if f.area >= MIN_AREA_MM2]
    fixed = []
    for f in faces:
        if not f.is_valid:
            f = make_valid(f)
            if hasattr(f, 'geoms'):
                cand = [g for g in f.geoms if getattr(g, 'area', 0) >= MIN_AREA_MM2
                        and g.geom_type == 'Polygon']
                if not cand:
                    continue
                f = max(cand, key=lambda g: g.area)
        if f.geom_type == 'Polygon' and f.area >= MIN_AREA_MM2:
            fixed.append(f)
    return fixed


def _geometry_from_outer(outer, all_faces: list) -> dict:
    """Dato il poligono outer scelto, calcola area netta/perimetro/fori.

    MATEMATICA PURA: nessuna euristica. I fori sono le facce strettamente
    contenute nel CONTORNO ESTERNO dell'outer. area netta = area outer − aree fori.

    BUG FIX D2: prima il contenimento era testato col representative_point, che
    per un rettangolo cade al centro (dentro un eventuale foro centrale), e l'area
    della faccia polygonize (già al netto dei fori) veniva di nuovo decurtata.
    """
    outer = Polygon(outer.exterior)
    pp = prep(outer.buffer(0.05))
    holes = [Polygon(f.exterior) for f in all_faces
             if f is not outer and f.area < outer.area * 0.999 and pp.contains(f)]
    holes, _n = _dedup_poligoni(holes)
    holes, _n_svas = _riduci_fori_annidati(holes)

    area_lorda_mm2 = outer.area
    area_fori_mm2 = sum(h.area for h in holes)
    area_netta_mm2 = area_lorda_mm2 - area_fori_mm2

    perim_outer_mm = outer.exterior.length
    perim_fori_mm = sum(h.exterior.length for h in holes)
    perim_taglio_mm = perim_outer_mm + perim_fori_mm

    minx, miny, maxx, maxy = outer.bounds
    return {
        'success': True,
        'area_dm2': area_netta_mm2 / 10000.0,       # mm² → dm²
        'area_lorda_dm2': area_lorda_mm2 / 10000.0,
        'perimetro_taglio_m': perim_taglio_mm / 1000.0,  # mm → m
        'perimetro_outer_m': perim_outer_mm / 1000.0,
        **_campi_forature(holes),
        'bbox_width_mm': maxx - minx,
        'bbox_height_mm': maxy - miny,
        'source': 'manual-click-polygonize',
        'warnings': [],
    }


def pick_part_from_click(path: str, click_x_mm: float, click_y_mm: float,
                         config: dict | None = None) -> dict:
    """Seleziona il pezzo dal click sul bordo (modello Lantek 'Detect Part').

    Semantica: l'operatore clicca SUL contorno (o dentro il corpo) del pezzo.
    Tra tutte le facce del polygonize (esclusi i cartigli ISO), sceglie quella
    più pertinente al click:
      1. Se il click cade DENTRO una faccia → la più piccola che lo contiene
         (evita di prendere un frame che racchiude tutto).
      2. Altrimenti → la faccia il cui bordo è più vicino al click.

    Ritorna dict con area_dm2/perimetro_taglio_m/n_forature/bbox (matematica
    pura) oppure {success: False, error, warnings} se nessuna faccia valida
    (il chiamante DEVE bloccare e chiedere all'operatore).
    """
    cfg = config or {}
    colori_esclusi = set(cfg.get('dxf_colori_piega', [2])) | set(cfg.get('dxf_colori_saldatura', [1]))

    try:
        faces = _build_faces(path, colori_esclusi)
    except RuntimeError as e:
        return {'success': False, 'error': str(e), 'warnings': [str(e)]}

    if not faces:
        return {'success': False, 'error': 'Nessuna faccia chiusa trovata nel DXF',
                'warnings': ['polygonize non ha prodotto facce: contorno aperto o solo annotazioni']}

    # Escludi cartigli ISO
    candidate = [f for f in faces if not _is_iso_format_bounds(f.bounds)]
    if not candidate:
        candidate = faces  # tutti ISO? improbabile — non filtrare

    click = Point(click_x_mm, click_y_mm)

    # 1. Facce che contengono il click → la più PICCOLA (evita frame globale)
    containing = [f for f in candidate if f.contains(click)]
    if containing:
        outer = min(containing, key=lambda f: f.area)
    else:
        # 2. Faccia col bordo più vicino al click
        outer = min(candidate, key=lambda f: f.exterior.distance(click))
        # Guardia: se il click è lontanissimo dal bordo scelto, è sospetto
        d = outer.exterior.distance(click)
        diag = math.hypot(outer.bounds[2] - outer.bounds[0],
                          outer.bounds[3] - outer.bounds[1])
        if diag > 0 and d > 0.5 * diag:
            return {'success': False,
                    'error': f'Click troppo lontano da qualsiasi contorno (dist {d:.1f}mm)',
                    'warnings': ['il click non è vicino a nessun pezzo — riprova sul bordo']}

    return _geometry_from_outer(outer, faces)


def genera_dxf_canonico(outer_xy: list, holes_xy: list, out_path: str) -> dict:
    """Genera un DXF PULITO contenente SOLO il contorno confermato + i fori.

    È il file canonico di produzione: byte-identico a ciò che è stato
    preventivato (garanzia preventivato≡prodotto) e già pronto per il nesting
    Lantek (niente cartiglio/quote/viste da ripulire).

    outer_xy: [[x,y], ...] contorno esterno (mm DXF).
    holes_xy: [[[x,y], ...], ...] contorni dei fori.
    Ritorna {success, sha256, path} oppure {success: False, error}.
    """
    import hashlib
    try:
        # units=4: millimetri dichiarati ($INSUNITS). Il default di ezdxf.new è 6
        # (metri): il file canonico verrebbe riletto 1000× più grande.
        doc = ezdxf.new('R2010', units=4)
        msp = doc.modelspace()
        if outer_xy and len(outer_xy) >= 3:
            msp.add_lwpolyline([(p[0], p[1]) for p in outer_xy], close=True,
                               dxfattribs={'layer': 'PEZZO'})
        for hole in (holes_xy or []):
            if hole and len(hole) >= 3:
                msp.add_lwpolyline([(p[0], p[1]) for p in hole], close=True,
                                   dxfattribs={'layer': 'FORI'})
        doc.saveas(out_path)
        with open(out_path, 'rb') as fp:
            digest = hashlib.sha256(fp.read()).hexdigest()
        return {'success': True, 'sha256': digest, 'path': out_path}
    except Exception as e:
        logger.exception('genera_dxf_canonico fallita')
        return {'success': False, 'error': str(e)}


# ── Geometria per il viewer CAD (polilinee in mm DXF) ───────────────────────

def _entity_polyline(entity, distance: float = FLATTEN_DISTANCE_MM, scala: float = 1.0):
    """Flatten una entità in UNA polilinea (lista di punti, mm) invece di segmenti."""
    try:
        p = make_path(entity)
        if len(p) == 0:
            return None
        pts = [(round(v.x * scala, 3), round(v.y * scala, 3)) for v in p.flattening(distance / scala)]
        return pts if len(pts) >= 2 else None
    except Exception:
        if entity.dxftype() == 'LINE':
            try:
                s, e = entity.dxf.start, entity.dxf.end
                return [(round(s.x * scala, 3), round(s.y * scala, 3)),
                        (round(e.x * scala, 3), round(e.y * scala, 3))]
            except Exception:
                return None
        return None


# Spessori lamiera REALMENTE tagliati (Marco). Unica verità per snap + cross-check.
# 0.8 e 2.5 aggiunti: senza, una lamiera 0.8 veniva arrotondata a 1 e una 2.5 a 2/3.
SPESSORI_STOCK = [0.8, 1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 12, 15]


def _snap_stock(v):
    """Arrotonda uno spessore al valore di stock più vicino."""
    if not v or v <= 0:
        return v
    return min(SPESSORI_STOCK, key=lambda s: abs(s - v))


def _text_item(e):
    """Estrae un testo (TEXT/MTEXT/ATTRIB) come {x,y,s,h,rot,anchor,baseline} per
    il viewer. Pulisce i codici DXF (%%d, \\U+XXXX, formattazione MTEXT).

    IMPORTANTE: rispetta l'ALLINEAMENTO. Un TEXT centrato/allineato ha il punto
    reale in `align_point` (non `insert`); un MTEXT è ancorato secondo il suo
    `attachment_point`. Senza questo, i valori nelle celle del cartiglio finiscono
    spostati e si sovrappongono. Restituiamo anche anchor/baseline così il viewer
    posiziona il testo esattamente come nel CAD.
    """
    import re as _re
    et = e.dxftype()
    try:
        s = e.plain_text() if et == 'MTEXT' else (e.dxf.text or '')
    except Exception:
        s = getattr(e.dxf, 'text', '') or ''
    s = _re.sub(r'\\U\+([0-9A-Fa-f]{4})', lambda m: chr(int(m.group(1), 16)), s)
    s = s.replace('%%d', '°').replace('%%c', 'Ø').replace('%%p', '±')
    s = _re.sub(r'\\[A-Za-z][^;]*;', '', s)
    s = _re.sub(r'[{}]', '', s).strip()
    if not s:
        return None

    h = 0.0
    for attr in ('height', 'char_height'):
        try:
            h = float(getattr(e.dxf, attr, 0) or 0)
        except Exception:
            h = 0.0
        if h:
            break
    if not h:
        h = 2.5
    try:
        rot = float(getattr(e.dxf, 'rotation', 0) or 0)
    except Exception:
        rot = 0.0

    anchor, baseline = 'start', 'alphabetic'
    tx = ty = None

    if et == 'MTEXT':
        try:
            ins = e.dxf.insert
            tx, ty = float(ins.x), float(ins.y)
        except Exception:
            return None
        # attachment_point 1..9: 1=TL 2=TC 3=TR 4=ML 5=MC 6=MR 7=BL 8=BC 9=BR
        ap = int(getattr(e.dxf, 'attachment_point', 1) or 1)
        anchor = {1: 'start', 4: 'start', 7: 'start',
                  2: 'middle', 5: 'middle', 8: 'middle',
                  3: 'end', 6: 'end', 9: 'end'}.get(ap, 'start')
        baseline = {1: 'hanging', 2: 'hanging', 3: 'hanging',
                    4: 'middle', 5: 'middle', 6: 'middle',
                    7: 'alphabetic', 8: 'alphabetic', 9: 'alphabetic'}.get(ap, 'hanging')
    else:  # TEXT / ATTRIB / ATTDEF
        halign = int(getattr(e.dxf, 'halign', 0) or 0)  # 0 L,1 C,2 R,3 ALIGNED,4 MIDDLE,5 FIT
        valign = int(getattr(e.dxf, 'valign', 0) or 0)  # 0 BASELINE,1 BOTTOM,2 MIDDLE,3 TOP
        # Con allineamento non-default il punto reale è align_point (code 11)
        pt = None
        if halign != 0 or valign != 0:
            try:
                ap = e.dxf.align_point
                if ap is not None and (abs(ap.x) > 1e-9 or abs(ap.y) > 1e-9):
                    pt = (float(ap.x), float(ap.y))
            except Exception:
                pt = None
        if pt is None:
            try:
                ins = e.dxf.insert
                pt = (float(ins.x), float(ins.y))
            except Exception:
                return None
        tx, ty = pt
        anchor = {0: 'start', 1: 'middle', 2: 'end', 3: 'start', 4: 'middle', 5: 'start'}.get(halign, 'start')
        baseline = {0: 'alphabetic', 1: 'alphabetic', 2: 'middle', 3: 'hanging'}.get(valign, 'alphabetic')

    return {'x': round(tx, 2), 'y': round(ty, 2), 's': s[:80], 'h': round(h, 2),
            'rot': round(rot, 1), 'anchor': anchor, 'baseline': baseline}


# Discretizzazione FINE dedicata al DISPLAY del CAD (curve/archi lisci a qualsiasi
# zoom). Più fine del FLATTEN_DISTANCE_MM usato dal detection/batch: qui è un solo
# file alla volta, quindi possiamo permetterci la massima risoluzione visiva.
_DISPLAY_FLATTEN_MM = 0.05


def geometry_json(path: str, config: dict | None = None) -> dict:
    """Estrae tutta la geometria disegnabile come polilinee in mm DXF, per il
    viewer CAD interno. Ritorna {extents, polylines} dove ogni polilinea ha
    {pts, kind}: kind='geo' (contorno/geometria, cliccabile) o 'annot'
    (cartiglio/quote/testo, mostrato in grigio ma non parte del pezzo).

    Il viewer mostra TUTTO (come Lantek) così l'operatore vede il disegno
    completo e sa dove cliccare. Il follow-contour filtra le annotazioni.
    Le curve sono discretizzate a `_DISPLAY_FLATTEN_MM` (alta risoluzione visiva).
    """
    try:
        doc = ezdxf.readfile(path)
    except Exception as e:
        return {'error': f'DXF non leggibile: {e}', 'extents': None, 'polylines': []}
    msp = doc.modelspace()
    # Coordinate del viewer in MILLIMETRI (DXF in pollici/cm/m scalati): così
    # click, contorni tracciati e DXF canonico sono tutti in mm.
    scala, w_unita = scala_unita_mm(doc)

    def _testo_mm(t):
        if t and scala != 1.0:
            t['x'] = round(t['x'] * scala, 2)
            t['y'] = round(t['y'] * scala, 2)
            t['h'] = round(t['h'] * scala, 2)
        return t

    polylines = []
    texts = []
    minx = miny = float('inf')
    maxx = maxy = float('-inf')
    # INSERT espansi (anche i blocchi di annotazione, mostrati in grigio)
    for entity in entita_espanse(msp, solo_geometria=False):
        et = entity.dxftype()
        if et in ('MTEXT', 'TEXT', 'ATTRIB', 'ATTDEF'):
            t = _testo_mm(_text_item(entity))   # ora il testo (cartiglio/quote) lo mostriamo
            if t:
                texts.append(t)
            continue
        is_annot = et in TIPI_ANNOTAZIONE or getattr(entity, '_ft_annotazione', False)
        try:
            if _layer_da_escludere(entity.dxf.layer):
                is_annot = True
        except AttributeError:
            pass
        # DIMENSION: esplodi la geometria (linee/frecce) + i numeri di quota
        if et == 'DIMENSION':
            try:
                for ve in entity.virtual_entities():
                    if ve.dxftype() in ('MTEXT', 'TEXT'):
                        t = _testo_mm(_text_item(ve))
                        if t:
                            texts.append(t)
                        continue
                    pts = _entity_polyline(ve, distance=_DISPLAY_FLATTEN_MM, scala=scala)
                    if pts:
                        polylines.append({'pts': pts, 'kind': 'annot'})
                        for x, y in pts:
                            minx, miny = min(minx, x), min(miny, y)
                            maxx, maxy = max(maxx, x), max(maxy, y)
            except Exception:
                pass
            continue
        pts = _entity_polyline(entity, distance=_DISPLAY_FLATTEN_MM, scala=scala)
        if not pts:
            continue
        polylines.append({'pts': pts, 'kind': 'annot' if is_annot else 'geo'})
        for x, y in pts:
            minx, miny = min(minx, x), min(miny, y)
            maxx, maxy = max(maxx, x), max(maxy, y)

    # estendi il bounding box anche ai testi (così il fit li include)
    for t in texts:
        minx, miny = min(minx, t['x']), min(miny, t['y'])
        maxx, maxy = max(maxx, t['x']), max(maxy, t['y'])

    if minx == float('inf'):
        return {'error': 'Nessuna geometria', 'extents': None, 'polylines': []}

    # Dati dal cartiglio per PRE-COMPILARE il CAD (l'operatore conferma sempre):
    #  - peso: ancora del cross-check peso calcolato vs dichiarato
    #  - materiale + spessore: suggerimento (deterministico dove possibile)
    peso_cartiglio = None
    peso_conf = 0.0
    mat_cartiglio = ''
    mat_conf = 0.0
    sp_cartiglio = None
    sp_conf = 0.0
    sp_source = 'none'
    try:
        from .dxf_scanner import (estrai_peso_da_cartiglio,
                                  estrai_materiale_da_cartiglio,
                                  estrai_spessore_da_cartiglio,
                                  estrai_dimensioni_da_descrizione_cartiglio)
        pc = estrai_peso_da_cartiglio(path)
        if pc and pc.get('peso_kg'):
            peso_cartiglio = pc['peso_kg']
            peso_conf = pc.get('confidence', 0.0)
        mc = estrai_materiale_da_cartiglio(path)
        if mc and mc.get('materiale'):
            mat_cartiglio = mc['materiale']
            mat_conf = mc.get('confidence', 0.0)
        # spessore: senza area confermata usa filename/descrizione (no calcolo fisico)
        sc = estrai_spessore_da_cartiglio(path)
        if sc and sc.get('spessore_mm'):
            sp_cartiglio = sc['spessore_mm']
            sp_conf = sc.get('confidence', 0.0)
            sp_source = sc.get('source', 'none')
        # Fallback: leggi lo spessore dalla DESCRIZIONE del cartiglio ("...sp.3"),
        # che spesso c'è anche quando il campo "Sp." dedicato manca.
        if not sp_cartiglio:
            dim = estrai_dimensioni_da_descrizione_cartiglio(path) or {}
            if dim.get('spessore_mm'):
                sp_cartiglio = float(dim['spessore_mm'])
                sp_conf = dim.get('confidence', 0.7) or 0.7
                sp_source = 'descrizione'
        # CONFERMA DAL DISEGNO: cerca tra le quote (DIMENSION) un valore uguale a
        # uno spessore-lamiera standard. Prendere una quota "a caso" e' inaffidabile
        # (troppi numeri), ma se il DISEGNO contiene una quota = spessore cartiglio
        # → due fonti indipendenti concordano (fail-safe: confidenza alta). Se il
        # cartiglio non ha lo spessore e c'e' UNA sola quota-standard piccola, la
        # proponiamo (bassa confidenza, l'operatore verifica).
        # Arrotonda lo spessore del cartiglio allo stock reale (1-1.5-2-3-4-5-6-8-10-12-15)
        if sp_cartiglio:
            sp_cartiglio = _snap_stock(float(sp_cartiglio))
        try:
            qstd = set()
            for e in msp.query('DIMENSION'):
                try:
                    m = float(e.get_measurement()) * scala   # quota in mm
                except Exception:
                    continue
                if 0.3 <= m <= 25:
                    for s in SPESSORI_STOCK:
                        if abs(m - s) < 0.06:
                            qstd.add(s)
                            break
            if sp_cartiglio and any(abs(sp_cartiglio - q) < 0.06 for q in qstd):
                sp_conf = max(sp_conf, 0.95)
                sp_source = (sp_source + '+disegno') if sp_source not in ('none', '') else 'disegno'
            elif not sp_cartiglio and len(qstd) == 1:
                sp_cartiglio = next(iter(qstd))
                sp_conf = 0.55
                sp_source = 'quota-disegno'
        except Exception:
            pass
    except Exception:
        pass

    return {
        'extents': [minx, miny, maxx, maxy],
        'polylines': polylines,
        'texts': texts,
        'peso_cartiglio_kg': peso_cartiglio,
        'peso_cartiglio_conf': peso_conf,
        'cartiglio_materiale': mat_cartiglio,
        'cartiglio_materiale_conf': mat_conf,
        'cartiglio_spessore_mm': sp_cartiglio,
        'cartiglio_spessore_conf': sp_conf,
        'cartiglio_spessore_source': sp_source,
        'spessori_stock': SPESSORI_STOCK,
        'scala_unita_mm': scala,
        'warnings': [w_unita] if w_unita else [],
    }


# ── Contour follower (tracciamento stile Lantek Detect Part) ────────────────
#
# Validato su casi reali (app/tests/validate_contour_follow.py): dato un click
# sul contorno del pezzo, segue la catena di segmenti scegliendo sempre la
# continuazione più dritta. Le witness-line delle quote (perpendicolari) vengono
# ignorate. Area risultante entro ~0,1% di Lantek sui casi a contorno pulito.
# Sui bivi ambigui / disegni multi-pezzo l'operatore guida col click (UI Fase 2).

_FOLLOW_TOL_MM = 0.15   # tolleranza snap nodi del grafo
_FOLLOW_MAX_STEPS = 300000


def _segments_all(msp, colori_esclusi: set[int]) -> list[tuple]:
    """Come _collect_segments ma ritorna coppie di punti (a, b) invece di 4-tuple."""
    return [((x1, y1), (x2, y2)) for (x1, y1, x2, y2) in _collect_segments(msp, colori_esclusi)]


def _build_graph(segs, tol=_FOLLOW_TOL_MM):
    def key(p):
        return (round(p[0] / tol), round(p[1] / tol))
    nodes, adj = {}, {}
    for a, b in segs:
        ka, kb = key(a), key(b)
        nodes.setdefault(ka, a)
        nodes.setdefault(kb, b)
        if ka == kb:
            continue
        adj.setdefault(ka, set()).add(kb)
        adj.setdefault(kb, set()).add(ka)
    return nodes, adj, key


def _walk_straightest(nodes, adj, ka, kb, max_steps=_FOLLOW_MAX_STEPS):
    """Cammina la catena scegliendo la continuazione più dritta a ogni nodo.
    Ritorna (lista_chiavi_nodi, chiuso: bool)."""
    loop = [ka, kb]
    prev, cur = ka, kb
    for _ in range(max_steps):
        if cur == ka and len(loop) > 3:
            return loop, True
        pa, pc = nodes[prev], nodes[cur]
        din = math.atan2(pc[1] - pa[1], pc[0] - pa[0])
        best, bestturn = None, None
        for nb in adj.get(cur, ()):
            if nb == prev:
                continue
            pn = nodes[nb]
            dout = math.atan2(pn[1] - pc[1], pn[0] - pc[0])
            turn = abs((dout - din + math.pi) % (2 * math.pi) - math.pi)
            if bestturn is None or turn < bestturn:
                bestturn, best = turn, nb
        if best is None:
            return loop, False
        loop.append(best)
        prev, cur = cur, best
    return loop, False


def follow_contour_from_click(path: str, click_x_mm: float, click_y_mm: float,
                              config: dict | None = None) -> dict:
    """Traccia il contorno del pezzo dal click (modello Lantek Detect Part).

    Segue la catena di segmenti dal segmento più vicino al click, scegliendo
    la continuazione più dritta a ogni nodo (ignora le diramazioni delle quote).
    Se chiude un loop → calcola area netta/perimetro/fori (matematica pura).

    Ritorna {success, area_dm2, perimetro_taglio_m, n_forature, bbox, outer_xy,
    holes_xy, source} oppure {success: False, error} se non chiude (il chiamante
    BLOCCA e chiede all'operatore di riprovare/guidare).
    """
    if not _HAS_SHAPELY:
        return {'success': False, 'error': 'Shapely non installato'}
    cfg = config or {}
    colori_esclusi = set(cfg.get('dxf_colori_piega', [2])) | set(cfg.get('dxf_colori_saldatura', [1]))
    try:
        doc = ezdxf.readfile(path)
    except Exception as e:
        return {'success': False, 'error': f'DXF non leggibile: {e}'}
    msp = doc.modelspace()

    segs = _segments_all(msp, colori_esclusi)
    if not segs:
        return {'success': False, 'error': 'Nessuna geometria nel DXF'}

    nodes, adj, key = _build_graph(segs)

    # Segmento più vicino al click
    click = Point(click_x_mm, click_y_mm)
    best_seg, best_d = None, None
    for a, b in segs:
        d = LineString([a, b]).distance(click)
        if best_d is None or d < best_d:
            best_d, best_seg = d, (a, b)
    if best_seg is None:
        return {'success': False, 'error': 'Nessun segmento vicino al click'}

    ka, kb = key(best_seg[0]), key(best_seg[1])
    if ka == kb:
        return {'success': False, 'error': 'Segmento degenere sotto il click'}

    # Prova entrambe le direzioni; tieni il loop chiuso di area maggiore
    outer = None
    for a0, b0 in ((ka, kb), (kb, ka)):
        loop, closed = _walk_straightest(nodes, adj, a0, b0)
        if not closed or len(loop) < 4:
            continue
        try:
            poly = Polygon([nodes[k] for k in loop])
            if not poly.is_valid:
                poly = make_valid(poly)
                if hasattr(poly, 'geoms'):
                    cand = [g for g in poly.geoms if g.geom_type == 'Polygon']
                    if not cand:
                        continue
                    poly = max(cand, key=lambda g: g.area)
            if poly.geom_type != 'Polygon' or poly.area < MIN_AREA_MM2:
                continue
        except Exception:
            continue
        if outer is None or poly.area > outer.area:
            outer = poly

    if outer is None:
        return {'success': False,
                'error': 'Il contorno non si chiude da questo click',
                'warnings': ['click su un bordo diverso del pezzo, o guida con waypoint']}

    # Rete di sicurezza: se copre quasi tutto il foglio è la cornice, non il pezzo
    if _sembra_cornice(outer, msp, colori_esclusi):
        return {'success': False,
                'error': 'Sembra la cornice del foglio, non il pezzo',
                'warnings': ['hai cliccato il bordo del disegno — clicca sul contorno del PEZZO']}

    # Fori: SOLO entità chiuse reali (cerchi/asole), non facce da linee di piega
    holes = _holes_inside(msp, outer, colori_esclusi)

    area_netta = (outer.area - sum(h.area for h in holes)) / 10000.0
    perim_taglio = (outer.exterior.length + sum(h.exterior.length for h in holes)) / 1000.0
    minx, miny, maxx, maxy = outer.bounds
    return {
        'success': True,
        'area_dm2': area_netta,
        'area_lorda_dm2': outer.area / 10000.0,
        'perimetro_taglio_m': perim_taglio,
        **_campi_forature(holes),
        'bbox_width_mm': maxx - minx,
        'bbox_height_mm': maxy - miny,
        'outer_xy': list(outer.exterior.coords),
        'holes_xy': [list(h.exterior.coords) for h in holes],
        'source': 'manual-click-follow',
        'warnings': [],
    }


def _closed_entity_polygons(msp, colori_esclusi: set[int]) -> list:
    """Poligoni Shapely SOLO da entità chiuse reali (CIRCLE, ELLIPSE, polilinee/
    spline chiuse, ARC a 360°). Serve per i FORI: un foro è un contorno chiuso
    di materiale rimosso, NON una faccia creata da una linea di piega aperta che
    attraversa il pezzo. Usare le facce del polygonize per i fori sottrae per
    errore le linee di piega (bug pezzi piegati).

    INSERT espansi, coordinate in mm, duplicati rimossi (un cerchio disegnato
    due volte non è due fori)."""
    def _calcola():
        scala = _scala(msp)
        out = []
        for entity in entita_espanse(msp, solo_geometria=True):
            et = entity.dxftype()
            if et in TIPI_ANNOTAZIONE:
                continue
            try:
                if _layer_da_escludere(entity.dxf.layer):
                    continue
            except AttributeError:
                pass
            if _entity_color_excluded(entity, colori_esclusi):
                continue
            if not _entita_chiusa(entity):
                continue
            pts = _entity_polyline(entity, scala=scala)
            if not pts or len(pts) < 3:
                continue
            try:
                poly = Polygon(pts)
                if not poly.is_valid:
                    poly = make_valid(poly)
                    if hasattr(poly, 'geoms'):
                        cand = [g for g in poly.geoms if g.geom_type == 'Polygon']
                        if not cand:
                            continue
                        poly = max(cand, key=lambda g: g.area)
                if poly.geom_type == 'Polygon' and poly.area >= MIN_AREA_MM2:
                    out.append(poly)
            except Exception:
                continue
        return _dedup_poligoni(out)[0]
    return _cache_doc(msp, ('chiusi', frozenset(colori_esclusi)), _calcola)


def _sembra_cornice(outer, msp, colori_esclusi=frozenset({1, 2})) -> bool:
    """True se l'outer riempie quasi tutto il foglio (larghezza E altezza ≥90%
    dell'estensione totale) → è la CORNICE del disegno, non il pezzo. Rete di
    sicurezza contro il click sul frame (l'errore 13,6× del vecchio detector).
    Nessun pezzo reale riempie il foglio in entrambe le dimensioni."""
    try:
        from ezdxf import bbox
        ext = bbox.extents(msp)
        s = _scala(msp)   # estensione in unità disegno → mm come l'outer
        ew = (ext.extmax.x - ext.extmin.x) * s
        eh = (ext.extmax.y - ext.extmin.y) * s
        if ew <= 0 or eh <= 0:
            return False
        minx, miny, maxx, maxy = outer.bounds
        if not ((maxx - minx) >= 0.90 * ew and (maxy - miny) >= 0.90 * eh):
            return False
        # Riempie il foglio: è cornice solo se il disegno ha annotazioni. Un DXF
        # con il SOLO pezzo (es. sviluppo esportato pulito) prima era rifiutato.
        return _ha_annotazioni(msp)
    except Exception:
        return False


def _ha_annotazioni(msp) -> bool:
    """True se il disegno ha testi/quote/blocchi (cioè può avere una cornice col
    cartiglio). Un DXF di SOLA geometria (sviluppo esportato pulito, DXF pulito o
    canonico) non ha cornice: il contorno che riempie il foglio è il pezzo."""
    def _calcola():
        # blocchi espansi: il contenuto dei blocchi di annotazione è marcato
        for e in entita_espanse(msp, solo_geometria=False):
            if e.dxftype() in ('TEXT', 'MTEXT', 'DIMENSION', 'ATTRIB',
                               'LEADER', 'MULTILEADER', 'HATCH'):
                return True
            if getattr(e, '_ft_annotazione', False):
                return True
        return False
    return _cache_doc(msp, ('annotazioni',), _calcola)


def _punti_medi_semicerchi(msp, colori_esclusi: set[int]) -> list:
    """Punto medio (mm) degli ARC ~semicirconferenza (estremità di asole)."""
    scala = _scala(msp)
    out = []
    for e in entita_espanse(msp, solo_geometria=True):
        if e.dxftype() != 'ARC':
            continue
        try:
            if _entity_color_excluded(e, colori_esclusi) or _layer_da_escludere(e.dxf.layer):
                continue
        except Exception:
            pass
        try:
            sweep = (e.dxf.end_angle - e.dxf.start_angle) % 360.0
            if not (150.0 <= sweep <= 210.0):  # solo semicerchi (non fillet di piega ~90°)
                continue
            # punto medio dal tracciato in WCS (gestisce archi di blocchi specchiati)
            pts = _entity_polyline(e, scala=scala)
            if pts:
                out.append(pts[len(pts) // 2])
        except Exception:
            continue
    return out


def _holes_inside(msp, outer, colori_esclusi: set[int]) -> list:
    """Fori = entità chiuse contenute strettamente nell'outer (esclude l'outer
    stesso e contorni ~coincidenti).

    SVASATURE: due (o più) cerchi ~concentrici sono una svasatura (foro passante
    + smusso conico). Il laser taglia SOLO il foro passante (il più piccolo); la
    svasatura è lavorazione successiva, non un taglio. Quindi tra fori annidati
    e ~concentrici si tiene solo l'INTERNO (evita di sotto-contare area/perim).
    """
    raw = []
    # Contenimento VERO del poligono (bordo coincidente tollerato 0.05mm), non del
    # solo representative_point: un contorno a cavallo del bordo non è un foro.
    pp_outer = prep(outer.buffer(0.05))
    for poly in _closed_entity_polygons(msp, colori_esclusi):
        if poly.area >= outer.area * 0.95:
            continue  # è l'outer stesso o quasi
        if pp_outer.contains(poly):
            raw.append(poly)

    # PROFILI INTERNI disegnati come LOOP di segmenti sciolti (asole, quadrati,
    # esagoni, sagome…) che _closed_entity_polygons non vede. Li recuperiamo dalle
    # facce polygonize STRETTAMENTE interne all'outer, ma solo se sono davvero un
    # foro e non una faccia generata da una linea di piega. Criterio:
    #   - ha un ARCO ~semicerchio sul bordo (estremità di un'asola/foro tondeggiante), OPPURE
    #   - è COMPATTA e NON attraversa il pezzo (un foro è piccolo rispetto al pezzo;
    #     una striscia di piega è lunga/sottile e attraversa il pezzo → esclusa).
    try:
        arc_mids = _cache_doc(msp, ('arc_mids', frozenset(colori_esclusi)),
                              lambda: _punti_medi_semicerchi(msp, colori_esclusi))
        ob = outer.bounds
        ow, oh = ob[2] - ob[0], ob[3] - ob[1]
        for fc in _faces_from_msp(msp, colori_esclusi):
            if fc.area >= outer.area * 0.5:
                continue
            if not outer.contains(fc):
                continue  # tocca il bordo esterno → non è un foro interno
            rp = fc.representative_point()
            if any(h.contains(rp) for h in raw):
                continue  # già coperto da un'entità chiusa
            fb = fc.bounds
            fw, fh = fb[2] - fb[0], fb[3] - fb[1]
            has_arc = any(fc.exterior.distance(Point(mx, my)) < 1.0 for (mx, my) in arc_mids)
            aspect = max(fw, fh) / max(min(fw, fh), 1e-6)
            compatto = (ow > 0 and oh > 0 and fw <= 0.6 * ow and fh <= 0.6 * oh and aspect <= 6.0)
            if has_arc or compatto:
                raw.append(fc)
    except Exception:
        pass

    # Scarta il foro esterno di ogni coppia ~concentrica (svasatura): se un foro
    # ne contiene un altro col centroide quasi coincidente → è lo smusso, si toglie.
    drop = set()
    for i, a in enumerate(raw):
        ca = a.centroid
        for j, b in enumerate(raw):
            if i == j or j in drop or i in drop:
                continue
            if a.area <= b.area:
                continue  # a deve essere il più grande per essere lo smusso
            cb = b.centroid
            dist = ca.distance(cb)
            # concentrici: centroidi entro il 15% del "raggio" del foro interno
            r_inner = (b.area / math.pi) ** 0.5
            if dist <= max(0.5, 0.15 * r_inner) and a.contains(b.representative_point()):
                drop.add(i)  # a è lo smusso esterno → scarta
    return [p for k, p in enumerate(raw) if k not in drop]


def _nearest_node(nodes, key, x, y):
    """Nodo del grafo più vicino al punto (x,y). Snap ai vertici dei segmenti."""
    best, bd = None, None
    for k, p in nodes.items():
        d = (p[0] - x) ** 2 + (p[1] - y) ** 2
        if bd is None or d < bd:
            bd, best = d, k
    return best


def _dijkstra_path(nodes, adj, src, dst):
    """Cammino minimo (somma lunghezze) da src a dst sul grafo. Ritorna lista
    di chiavi nodo o None se non connessi."""
    import heapq
    if src == dst:
        return [src]
    dist = {src: 0.0}
    prev = {}
    pq = [(0.0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if u == dst:
            break
        if d > dist.get(u, float('inf')):
            continue
        pu = nodes[u]
        for v in adj.get(u, ()):
            pv = nodes[v]
            w = math.hypot(pv[0] - pu[0], pv[1] - pu[1])
            nd = d + w
            if nd < dist.get(v, float('inf')):
                dist[v] = nd
                prev[v] = u
                heapq.heappush(pq, (nd, v))
    if dst not in prev and dst != src:
        return None
    # ricostruisci
    path = [dst]
    while path[-1] != src:
        p = prev.get(path[-1])
        if p is None:
            return None
        path.append(p)
    path.reverse()
    return path


def trace_contour_waypoints(path: str, points: list, config: dict | None = None) -> dict:
    """Tracciamento GUIDATO: l'operatore fornisce N punti (waypoint) lungo il
    contorno; il sistema segue la geometria DXF reale (cammino minimo sul grafo)
    tra waypoint consecutivi, poi chiude tornando al primo.

    Risolve i bivi ambigui dove il single-click 'più dritto' devia: l'operatore
    guida il percorso. Ogni tratto è geometria DXF reale (archi inclusi) → area
    e perimetro esatti.

    points: [[x,y], ...] in mm DXF (almeno 2; 3+ per contorni chiusi utili).
    Ritorna stesso formato di follow_contour_from_click, o {success:False,error}.
    """
    if not _HAS_SHAPELY:
        return {'success': False, 'error': 'Shapely non installato'}
    if not points or len(points) < 2:
        return {'success': False, 'error': 'Servono almeno 2 waypoint'}
    cfg = config or {}
    colori_esclusi = set(cfg.get('dxf_colori_piega', [2])) | set(cfg.get('dxf_colori_saldatura', [1]))
    try:
        doc = ezdxf.readfile(path)
    except Exception as e:
        return {'success': False, 'error': f'DXF non leggibile: {e}'}
    msp = doc.modelspace()
    segs = _segments_all(msp, colori_esclusi)
    if not segs:
        return {'success': False, 'error': 'Nessuna geometria nel DXF'}
    nodes, adj, key = _build_graph(segs)

    # Snap ogni waypoint al nodo più vicino
    snapped = [_nearest_node(nodes, key, float(p[0]), float(p[1])) for p in points]
    snapped = [s for s in snapped if s is not None]
    if len(snapped) < 2:
        return {'success': False, 'error': 'Waypoint non agganciati alla geometria'}

    # Concatena i cammini minimi waypoint→waypoint, poi chiudi (ultimo→primo)
    seq = [snapped[0]]
    chain = list(snapped) + [snapped[0]]  # chiude sul primo
    for a, b in zip(chain, chain[1:]):
        sub = _dijkstra_path(nodes, adj, a, b)
        if sub is None:
            return {'success': False,
                    'error': 'Tratto non connesso tra due waypoint — aggiungi un waypoint intermedio'}
        seq.extend(sub[1:])  # evita di duplicare il nodo di giunzione

    pts = [nodes[k] for k in seq]
    if len(pts) < 4:
        return {'success': False, 'error': 'Contorno troppo corto'}
    try:
        outer = Polygon(pts)
        if not outer.is_valid:
            outer = make_valid(outer)
            if hasattr(outer, 'geoms'):
                cand = [g for g in outer.geoms if g.geom_type == 'Polygon']
                if not cand:
                    return {'success': False, 'error': 'Contorno auto-intersecante — rivedi i waypoint'}
                outer = max(cand, key=lambda g: g.area)
        if outer.geom_type != 'Polygon' or outer.area < MIN_AREA_MM2:
            return {'success': False, 'error': 'Contorno degenere'}
    except Exception as e:
        return {'success': False, 'error': f'Contorno non valido: {e}'}

    holes = _holes_inside(msp, outer, colori_esclusi)
    area_netta = (outer.area - sum(h.area for h in holes)) / 10000.0
    perim_taglio = (outer.exterior.length + sum(h.exterior.length for h in holes)) / 1000.0
    minx, miny, maxx, maxy = outer.bounds
    return {
        'success': True,
        'area_dm2': area_netta,
        'area_lorda_dm2': outer.area / 10000.0,
        'perimetro_taglio_m': perim_taglio,
        **_campi_forature(holes),
        'bbox_width_mm': maxx - minx,
        'bbox_height_mm': maxy - miny,
        'outer_xy': list(outer.exterior.coords),
        'holes_xy': [list(h.exterior.coords) for h in holes],
        'source': 'manual-waypoints',
        'warnings': [],
    }


def _enumerate_loops(nodes, adj, start_edge, max_loops=8, max_branch=14,
                     max_steps=400000):
    """Enumera i contorni chiusi che passano per start_edge=(ka,kb).

    Cammina automatico sui nodi di grado 2 (nessuna scelta); ai BIVI (grado ≥3)
    esplora le continuazioni in ordine di 'dirittezza' (DFS con backtracking).
    Raccoglie fino a max_loops loop distinti. Bounded per non esplodere.
    Ritorna lista di liste-di-chiavi-nodo (i loop).
    """
    ka, kb = start_edge
    loops = []
    steps = [0]
    branches = [0]

    def straightness_order(prev, cur):
        pa, pc = nodes[prev], nodes[cur]
        din = math.atan2(pc[1] - pa[1], pc[0] - pa[0])
        cand = []
        for nb in adj.get(cur, ()):
            if nb == prev:
                continue
            pn = nodes[nb]
            dout = math.atan2(pn[1] - pc[1], pn[0] - pc[0])
            turn = abs((dout - din + math.pi) % (2 * math.pi) - math.pi)
            cand.append((turn, nb))
        cand.sort()
        return [nb for _, nb in cand]

    # stack di stati: (path, prev, cur, local_set)
    stack = [([ka, kb], ka, kb, {ka, kb})]
    while stack and len(loops) < max_loops and steps[0] < max_steps:
        path, prev, cur, local = stack.pop()
        steps[0] += 1
        if cur == ka and len(path) > 3:
            loops.append(path)
            continue
        opts = straightness_order(prev, cur)
        # chiusura sul primo se raggiungibile diretto
        if ka in opts and len(path) > 3:
            loops.append(path + [ka])
            opts = [o for o in opts if o != ka]
        if not opts:
            continue
        deg = len(opts)
        # nodo di passaggio (1 sola scelta) → segui senza contare come branch
        if deg == 1:
            nb = opts[0]
            if nb in local:
                continue
            stack.append((path + [nb], cur, nb, local | {nb}))
            continue
        # BIVIO: esplora le alternative (limita il numero di bivi esplorati)
        if branches[0] >= max_branch:
            # oltre il budget: prosegui solo la più dritta (greedy)
            opts = opts[:1]
        else:
            branches[0] += 1
            opts = opts[:3]  # top-3 continuazioni più dritte
        # push in ordine inverso così la più dritta viene esplorata per prima
        for nb in reversed(opts):
            if nb in local:
                continue
            stack.append((path + [nb], cur, nb, local | {nb}))
    return loops


def _shapely_candidate_for_click(faces: list, x: float, y: float, msp,
                                 colori_esclusi: set[int]) -> dict | None:
    """Candidato via polygonize Shapely: robusto su contorni a LINEE SPARSE che il
    graph-walk non riesce a chiudere (pezzi piccoli con svasature grandi, viste
    multiple sullo stesso foglio).

    Il PEZZO è la faccia (non-cornice) il cui bordo esterno racchiude il click,
    con area ESTERNA massima (racchiude i propri fori/dettagli): così cliccando
    ovunque nel corpo — anche sopra un foro — si seleziona il pezzo, non il foro.
    I fori veri li ricava _holes_inside (svasature-aware: tiene il passante).
    """
    if not faces:
        return None

    # Estensione geometrica = bbox unione di tutte le facce. La CORNICE è la faccia
    # che la riempie quasi tutta (in entrambe le dimensioni): un pezzo reale no.
    # (Filtro per DIMENSIONE, non per "quante facce racchiude" — così non scarta
    #  per errore un pezzo con molti fori.)
    gx0 = min(f.bounds[0] for f in faces); gy0 = min(f.bounds[1] for f in faces)
    gx1 = max(f.bounds[2] for f in faces); gy1 = max(f.bounds[3] for f in faces)
    gw, gh = gx1 - gx0, gy1 - gy0

    annotato = _ha_annotazioni(msp)

    def _is_frame(f):
        b = f.bounds
        grande = (gw > 0 and gh > 0 and (b[2] - b[0]) >= 0.9 * gw and (b[3] - b[1]) >= 0.9 * gh)
        # In un DXF di sola geometria (niente testi/quote) non c'è cornice: il
        # contorno grande (anche di formato A4/A3) è il pezzo.
        return annotato and (grande or _is_iso_format_bounds(b))

    cand = [f for f in faces if not _is_frame(f)]
    if not cand:
        return None  # solo cornici → lascia decidere al graph-walk

    click = Point(x, y)
    # Il PEZZO = la faccia non-cornice il cui BORDO ESTERNO racchiude il click, con
    # area esterna MASSIMA: racchiude i propri fori/dettagli, quindi cliccando
    # ovunque nel suo ingombro (anche sopra un foro) si prende il pezzo, non il foro.
    ext_in = [f for f in cand if Polygon(f.exterior).contains(click)]
    if ext_in:
        outer_face = max(ext_in, key=lambda f: Polygon(f.exterior).area)
    else:
        # Click sul bordo o appena fuori: faccia col contorno più vicino (con guardia)
        outer_face = min(cand, key=lambda f: f.exterior.distance(click))
        d = outer_face.exterior.distance(click)
        diag = math.hypot(outer_face.bounds[2] - outer_face.bounds[0],
                          outer_face.bounds[3] - outer_face.bounds[1])
        if diag > 0 and d > 0.5 * diag:
            return None  # click troppo lontano da qualsiasi pezzo
    outer = Polygon(outer_face.exterior)   # bordo esterno pieno; i fori li ricalcolo
    holes = _holes_inside(msp, outer, colori_esclusi)
    area_netta = (outer.area - sum(h.area for h in holes)) / 10000.0
    if area_netta <= 0:
        return None
    minx, miny, maxx, maxy = outer.bounds
    return {
        'area_dm2': area_netta,
        'area_lorda_dm2': outer.area / 10000.0,
        'perimetro_taglio_m': (outer.exterior.length
                               + sum(h.exterior.length for h in holes)) / 1000.0,
        **_campi_forature(holes),
        'bbox_width_mm': maxx - minx,
        'bbox_height_mm': maxy - miny,
        'outer_xy': list(outer.exterior.coords),
        'holes_xy': [list(h.exterior.coords) for h in holes],
        'source': 'polygonize-face',
    }


def pick_candidates(path: str, click_x_mm: float, click_y_mm: float,
                    config: dict | None = None) -> dict:
    """Multi-ipotesi: dal click enumera i contorni chiusi plausibili e li
    ritorna come CANDIDATI, così l'operatore sceglie quello giusto.

    Ritorna {success, candidates: [{area_dm2, perimetro_taglio_m, n_forature,
    bbox, outer_xy, holes_xy}], click}. I candidati sono dedotti per area e
    ordinati (il più 'pezzo-simile' per primo). Se un solo candidato → l'UI può
    auto-selezionarlo.
    """
    if not _HAS_SHAPELY:
        return {'success': False, 'error': 'Shapely non installato'}
    cfg = config or {}
    colori_esclusi = set(cfg.get('dxf_colori_piega', [2])) | set(cfg.get('dxf_colori_saldatura', [1]))
    try:
        doc = ezdxf.readfile(path)
    except Exception as e:
        return {'success': False, 'error': f'DXF non leggibile: {e}'}
    msp = doc.modelspace()
    segs = _segments_all(msp, colori_esclusi)
    if not segs:
        return {'success': False, 'error': 'Nessuna geometria nel DXF'}
    nodes, adj, key = _build_graph(segs)

    click = Point(click_x_mm, click_y_mm)
    best_seg, best_d = None, None
    for a, b in segs:
        d = LineString([a, b]).distance(click)
        if best_d is None or d < best_d:
            best_d, best_seg = d, (a, b)
    if best_seg is None:
        return {'success': False, 'error': 'Nessun segmento vicino al click'}
    ka, kb = key(best_seg[0]), key(best_seg[1])
    if ka == kb:
        return {'success': False, 'error': 'Segmento degenere'}

    raw_loops = _enumerate_loops(nodes, adj, (ka, kb)) + _enumerate_loops(nodes, adj, (kb, ka))
    seen_area = []
    candidates = []
    for loop in raw_loops:
        try:
            poly = Polygon([nodes[k] for k in loop])
            if not poly.is_valid:
                poly = make_valid(poly)
                if hasattr(poly, 'geoms'):
                    cand = [g for g in poly.geoms if g.geom_type == 'Polygon']
                    if not cand:
                        continue
                    poly = max(cand, key=lambda g: g.area)
            if poly.geom_type != 'Polygon' or poly.area < MIN_AREA_MM2:
                continue
        except Exception:
            continue
        if _sembra_cornice(poly, msp, colori_esclusi):
            continue
        holes = _holes_inside(msp, poly, colori_esclusi)
        area_netta = (poly.area - sum(h.area for h in holes)) / 10000.0
        # dedup per area (entro 1%)
        if any(abs(area_netta - a) / max(a, 1e-6) < 0.01 for a in seen_area):
            continue
        seen_area.append(area_netta)
        minx, miny, maxx, maxy = poly.bounds
        candidates.append({
            'area_dm2': area_netta,
            'area_lorda_dm2': poly.area / 10000.0,
            'perimetro_taglio_m': (poly.exterior.length + sum(h.exterior.length for h in holes)) / 1000.0,
            **_campi_forature(holes),
            'bbox_width_mm': maxx - minx,
            'bbox_height_mm': maxy - miny,
            'outer_xy': list(poly.exterior.coords),
            'holes_xy': [list(h.exterior.coords) for h in holes],
        })
    # Candidato robusto via polygonize (contorni a linee sparse che il graph-walk
    # non chiude: pezzi piccoli con svasature grandi, viste multiple sul foglio).
    try:
        shp = _shapely_candidate_for_click(
            _faces_from_msp(msp, colori_esclusi), click_x_mm, click_y_mm, msp, colori_esclusi)
    except Exception:
        shp = None
    if shp:
        dup = any(abs(shp['area_dm2'] - c['area_dm2']) / max(c['area_dm2'], 1e-6) < 0.01
                  for c in candidates)
        if not dup:
            candidates.append(shp)

    if not candidates:
        return {'success': False,
                'error': 'Nessun contorno chiuso da questo click',
                'warnings': ['clicca sul bordo del pezzo o usa il tracciamento guidato']}
    # ordina: preferisci più fori (pezzo reale) e area maggiore, ma non la cornice
    candidates.sort(key=lambda c: (c['n_forature'], c['area_dm2']), reverse=True)
    # Scarta i candidati che sono FORI/dettagli del migliore (contenuti nel suo
    # contorno esterno): non sono "pezzi alternativi". Così su un pezzo con fori
    # grandi resta un solo candidato → l'UI auto-seleziona senza chiedere.
    if len(candidates) > 1:
        try:
            # contenimento VERO (il representative_point del pezzo può cadere in
            # un suo foro centrale → il pezzo veniva scartato come "foro")
            top_pp = prep(Polygon(candidates[0]['outer_xy']).buffer(0.05))
            candidates = [candidates[0]] + [
                c for c in candidates[1:]
                if not top_pp.contains(Polygon(c['outer_xy']))
            ]
        except Exception:
            pass
    candidates = candidates[:5]
    return {'success': True, 'candidates': candidates,
            'click': [click_x_mm, click_y_mm]}


def polygonize_faces(path: str, config: dict | None = None) -> list[dict]:
    """Ritorna TUTTE le facce polygonize con i loro dati (per probe/debug e viewer).

    Ogni faccia: {area_dm2, perim_m, bbox, is_iso, centroid, exterior_xy}.
    Utile per: (a) misurare vs Lantek quale faccia è il pezzo, (b) disegnare
    gli overlay cliccabili nel CAD interno.
    """
    cfg = config or {}
    colori_esclusi = set(cfg.get('dxf_colori_piega', [2])) | set(cfg.get('dxf_colori_saldatura', [1]))
    faces = _build_faces(path, colori_esclusi)
    out = []
    for f in faces:
        minx, miny, maxx, maxy = f.bounds
        c = f.representative_point()
        out.append({
            'area_dm2': f.area / 10000.0,
            'perim_m': f.exterior.length / 1000.0,
            'bbox': [minx, miny, maxx, maxy],
            'bbox_w_mm': maxx - minx,
            'bbox_h_mm': maxy - miny,
            'is_iso': _is_iso_format_bounds(f.bounds),
            'centroid': [c.x, c.y],
        })
    # Ordina per area decrescente (il pezzo è di solito tra i più grandi non-ISO)
    out.sort(key=lambda d: d['area_dm2'], reverse=True)
    return out
