"""DXF Polygon Detector — Algoritmo CAM standard per estrazione geometria pezzo.

Sostituisce l'euristica cluster-density del vecchio scanner con polygon detection
vero: identifica contorni chiusi (outer/inner), filtra cartiglio per formati standard,
dedupa viste multiple, calcola area netta e perimetro esatti.

Funzione pubblica: `detect_pezzo_geometry(path, config) -> dict`.

Algoritmo:
1. Estrai tutte le entità di taglio (esclusi annotazioni/hatch/layer quotatura)
2. Converti in Path ezdxf + flatten in poligoni
3. Chain walking per ricostruire poligoni da LINE/ARC sciolte
4. Filtra cartiglio (bbox = formato ISO A0-A4 ±5%)
5. Dedup viste multiple (cluster di poligoni con bbox simile → tieni più grande)
6. Outer contour = area massima; Inner = contenuti in outer (raycasting)
7. Area netta = area(outer) - Σ area(inner)
8. Perimetro = perim(outer) + Σ perim(inner)
9. N_pierce = 1 + count(inner)
"""

from __future__ import annotations

import logging
import math
import os
from typing import Iterable

import ezdxf
from ezdxf.path import make_path

logger = logging.getLogger(__name__)


FORMATI_FOGLIO_ISO_MM: list[tuple[float, float]] = [
    (1189.0, 841.0),   # A0
    (841.0, 594.0),    # A1
    (594.0, 420.0),    # A2
    (420.0, 297.0),    # A3
    (297.0, 210.0),    # A4
]

LAYER_DA_ESCLUDERE_PATTERNS = (
    'dim', 'quote', 'quota', 'text', 'testo', 'annot',
    'hatch', 'tratteggio', 'cartiglio', 'frame', 'border',
    'cornice', 'logo', 'symb', 'mark', 'note', 'tit',
    'format',   # Lantek: layer cartiglio si chiama 'FORMAT'
    'pieg', 'bend', 'fold', 'bieg',   # linee di piega: marcature, non contorni
)

TIPI_ANNOTAZIONE = {
    'DIMENSION', 'MTEXT', 'TEXT', 'INSERT', 'ATTRIB', 'ATTDEF',
    'LEADER', 'MULTILEADER', 'HATCH', 'IMAGE', 'VIEWPORT',
}

# Tolleranze
TOL_ENDPOINT_MM = 0.5       # endpoint coincidenti (chain walking) — CAD non sempre preciso
TOL_CARTIGLIO_PCT = 0.05    # 5% tolleranza riconoscimento formato foglio
TOL_DEDUP_PCT = 0.05        # 5% tolleranza dedup viste multiple
FLATTEN_DISTANCE_MM = 0.2   # discretizzazione curve/spline (mm di deflessione max)
RATIO_RETTANGOLO_PURO = 0.95   # se area/bbox_area ≥ questo → forma rettangolare = cornice
MAX_VERTICI_RETTANGOLO = 12    # rettangoli puri hanno ≤ N vertici (con qualche tolleranza)
MIN_BBOX_CORNICE_MM = 200.0    # cornice = rettangolo puro con ENTRAMBE le dim ≥ N mm
                               # (cornici cartiglio sono almeno ~A4=210x297, pezzi più piccoli passano)
MIN_CONTENUTI_CORNICE = 8      # cornice "strana" = rettangolo puro che contiene ≥ N altri poligoni
                               # (catch cornici lunghe-strette tipo Lantek 562x133)


# ============================================================================
# Geometria base
# ============================================================================

def _shoelace_area(verts: list[tuple[float, float]]) -> float:
    """Area di poligono via formula di Gauss (shoelace). Restituisce |area|."""
    n = len(verts)
    if n < 3:
        return 0.0
    a = 0.0
    for i in range(n):
        x1, y1 = verts[i]
        x2, y2 = verts[(i + 1) % n]
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def _polygon_perim(verts: list[tuple[float, float]], closed: bool = True) -> float:
    """Somma lunghezze segmenti consecutivi. Se closed, chiude poligono."""
    n = len(verts)
    if n < 2:
        return 0.0
    p = 0.0
    for i in range(n - 1):
        p += math.hypot(verts[i + 1][0] - verts[i][0], verts[i + 1][1] - verts[i][1])
    if closed and n >= 3:
        p += math.hypot(verts[0][0] - verts[-1][0], verts[0][1] - verts[-1][1])
    return p


def _polygon_bbox(verts: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    """Bounding box (xmin, ymin, xmax, ymax) del poligono."""
    xs = [v[0] for v in verts]
    ys = [v[1] for v in verts]
    return (min(xs), min(ys), max(xs), max(ys))


def _bbox_size(bb: tuple[float, float, float, float]) -> tuple[float, float]:
    """Restituisce (width, height) ordinati: width = max, height = min."""
    w = bb[2] - bb[0]
    h = bb[3] - bb[1]
    return (max(w, h), min(w, h))


def _point_in_polygon(point: tuple[float, float], verts: list[tuple[float, float]]) -> bool:
    """Ray casting algorithm — restituisce True se point è dentro al poligono.

    Convenzione: punto su bordo = dentro. Tolleranza 1e-9.
    """
    x, y = point
    n = len(verts)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = verts[i]
        xj, yj = verts[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


# ============================================================================
# Cartiglio detection — formati standard ISO
# ============================================================================

def _is_cartiglio_format(bb: tuple[float, float, float, float], tol_pct: float = TOL_CARTIGLIO_PCT) -> bool:
    """True se la bbox corrisponde a un formato foglio standard ISO A0-A4 entro tolleranza."""
    w_max, h_min = _bbox_size(bb)
    for w_std, h_std in FORMATI_FOGLIO_ISO_MM:
        if (abs(w_max - w_std) / w_std <= tol_pct and
                abs(h_min - h_std) / h_std <= tol_pct):
            return True
    return False


def _is_rettangolo(verts: list[tuple[float, float]],
                   ratio_min: float = RATIO_RETTANGOLO_PURO,
                   max_vertici: int = MAX_VERTICI_RETTANGOLO) -> bool:
    """True se la forma è approssimativamente un rettangolo (pochi vertici + area≈bbox).

    Criterio puramente geometrico — NON dice se è cornice o pezzo, solo forma.
    """
    n = len(verts)
    if n > max_vertici:
        return False
    bb = _polygon_bbox(verts)
    bw = bb[2] - bb[0]
    bh = bb[3] - bb[1]
    bbox_area = bw * bh
    if bbox_area <= 0:
        return False
    poly_area = _shoelace_area(verts)
    return (poly_area / bbox_area) >= ratio_min


def _poligono_contenuto(inner: list[tuple[float, float]], outer: list[tuple[float, float]]) -> bool:
    """True se TUTTI i vertici (campionati) di inner stanno dentro outer.

    BUG FIX D2: prima si testava il solo centroide dei vertici; per un pezzo
    rettangolare con foro centrale il centroide cade nel foro → risultava che
    il FORO conteneva il PEZZO.
    """
    if not inner or inner is outer:
        return False
    passo = max(1, len(inner) // 16)
    return all(_point_in_polygon(v, outer) for v in inner[::passo])


def _conta_poligoni_contenuti(target_verts: list[tuple[float, float]],
                              all_polygons: list[list[tuple[float, float]]],
                              area_min_rel: float = 0.0) -> int:
    """Conta quanti altri poligoni sono contenuti in target_verts
    (opzionale: solo quelli con area ≥ area_min_rel × area target)."""
    n = 0
    a_t = _shoelace_area(target_verts)
    for other in all_polygons:
        if other is target_verts or not other:
            continue
        if area_min_rel and _shoelace_area(other) < area_min_rel * a_t:
            continue
        if _poligono_contenuto(other, target_verts):
            n += 1
    return n


def _is_cornice_cartiglio(verts: list[tuple[float, float]],
                          all_polygons: list[list[tuple[float, float]]],
                          min_bbox_mm: float = MIN_BBOX_CORNICE_MM,
                          min_contenuti: int = MIN_CONTENUTI_CORNICE) -> bool:
    """True se il poligono è una cornice cartiglio.

    Criterio combinato (OR), sempre su rettangolo puro che contiene un disegno
    (almeno un contorno NON-foro, > 5% della sua area):
    a) entrambe le dim ≥ min_bbox_mm (cornice grande = formato foglio)
    b) contiene ≥ min_contenuti altri poligoni
       (catch cornici lunghe-strette tipo cartiglio Lantek 562x133)

    BUG FIX D1: prima bastava la dimensione (a) o il numero di contenuti (b):
    una piastra 300×200 con fori, o con 8 fori, era scartata come cornice.
    I fori (piccoli) non fanno di un rettangolo una cornice.
    """
    if not _is_rettangolo(verts):
        return False
    if _conta_poligoni_contenuti(verts, all_polygons, area_min_rel=0.05) == 0:
        return False
    bb = _polygon_bbox(verts)
    bw = bb[2] - bb[0]
    bh = bb[3] - bb[1]
    # Criterio a: cornice grande in entrambe le dim
    if bw >= min_bbox_mm and bh >= min_bbox_mm:
        return True
    # Criterio b: rettangolo che contiene molte cose (cartiglio strano)
    if _conta_poligoni_contenuti(verts, all_polygons) >= min_contenuti:
        return True
    return False


# ============================================================================
# Layer filtering
# ============================================================================

def _layer_da_escludere(layer_name: str) -> bool:
    """True se il layer dovrebbe essere ignorato per estrazione geometria taglio."""
    n = (layer_name or '').strip().lower()
    if not n:
        return False
    return any(p in n for p in LAYER_DA_ESCLUDERE_PATTERNS)


# ============================================================================
# Estrazione poligoni dal DXF
# ============================================================================

def _entity_color_excluded(entity, colori_esclusi: set[int]) -> bool:
    """True se l'entità ha colore (EFFETTIVO: anche BYLAYER) in colori esclusi
    (linee piega/saldatura)."""
    try:
        from .dxf_polygon_detector_v3 import colore_effettivo
        return colore_effettivo(entity) in colori_esclusi
    except Exception:
        return False


def _flatten_entity_to_polygon(entity, distance: float = FLATTEN_DISTANCE_MM,
                               scala: float = 1.0) -> list[tuple[float, float]] | None:
    """Converte un'entità DXF in lista di vertici 2D (mm) via ezdxf.path.make_path + flattening.

    Funziona per LWPOLYLINE/POLYLINE chiusi, CIRCLE, ARC, ELLIPSE, SPLINE.
    Restituisce None se la conversione fallisce o entità non ha geometria.
    """
    try:
        p = make_path(entity)
        if len(p) == 0:
            return None
        verts = [(v.x * scala, v.y * scala) for v in p.flattening(distance / scala)]
        return verts if len(verts) >= 2 else None
    except Exception as e:
        logger.debug("make_path fallito per %s: %s", entity.dxftype(), e)
        return None


def _is_closed_polygon(verts: list[tuple[float, float]], tol: float = TOL_ENDPOINT_MM) -> bool:
    """True se primo e ultimo vertice sono entro tolleranza (poligono chiuso)."""
    if len(verts) < 3:
        return False
    return math.hypot(verts[-1][0] - verts[0][0], verts[-1][1] - verts[0][1]) <= tol


def _extract_polygons(msp, colori_esclusi: set[int], scala: float = 1.0) -> tuple[list[list[tuple[float, float]]], list]:
    """Estrae poligoni chiusi e entità aperte dal modelspace (INSERT espansi, mm).

    Restituisce (poligoni_chiusi, entita_aperte_da_chain).

    Poligoni chiusi: LWPOLYLINE/POLYLINE con flag closed o endpoints coincidenti,
                     CIRCLE (sempre chiusi), ARC con sweep=360°, SPLINE chiuse.
    Entità aperte: LINE, ARC<360°, polyline non chiuse → candidate al chain walking.
    Entità duplicate (stesso tratto disegnato due volte) contate una volta.
    """
    from .dxf_polygon_detector_v3 import entita_espanse
    closed_polygons: list[list[tuple[float, float]]] = []
    open_segments: list[tuple[tuple[float, float], tuple[float, float], object]] = []
    firme: set = set()

    def _nuova(verts) -> bool:
        """False se un'entità identica (entro 0.01mm) è già stata raccolta."""
        pts = sorted({(round(x, 2), round(y, 2)) for x, y in verts})
        firma = (len(verts), tuple(pts[:4]), tuple(pts[-4:]))
        if firma in firme:
            return False
        firme.add(firma)
        return True

    for entity in entita_espanse(msp, solo_geometria=True):
        et = entity.dxftype()
        if et in TIPI_ANNOTAZIONE:
            continue
        # Filtra layer di annotazione
        try:
            layer = entity.dxf.layer
            if _layer_da_escludere(layer):
                continue
        except AttributeError:
            pass
        # Filtra colore (pieghe/saldature non sono geometria taglio)
        if _entity_color_excluded(entity, colori_esclusi):
            continue

        if et == 'LINE':
            try:
                s = (entity.dxf.start.x * scala, entity.dxf.start.y * scala)
                e = (entity.dxf.end.x * scala, entity.dxf.end.y * scala)
                if s != e and _nuova([s, e]):
                    open_segments.append((s, e, [s, e]))
            except AttributeError:
                pass
            continue
        if et not in ('CIRCLE', 'LWPOLYLINE', 'POLYLINE', 'SPLINE', 'ELLIPSE', 'ARC'):
            continue
        verts = _flatten_entity_to_polygon(entity, scala=scala)
        if verts is None or not _nuova(verts):
            continue

        if et == 'CIRCLE':
            if len(verts) >= 3:
                closed_polygons.append(verts)
            continue

        if et in ('LWPOLYLINE', 'POLYLINE'):
            # Determina chiusura: flag esplicito O endpoint coincidenti
            try:
                is_closed_flag = bool(entity.closed) if et == 'LWPOLYLINE' else bool(getattr(entity, 'is_closed', False))
            except Exception:
                is_closed_flag = False
            if is_closed_flag or _is_closed_polygon(verts):
                closed_polygons.append(verts)
            else:
                # Polyline aperta → segmento per chain walking (start, end)
                open_segments.append((verts[0], verts[-1], verts))
            continue

        if et == 'ARC':
            # ARC con sweep ≥ 359.9° = cerchio chiuso
            try:
                sweep = (entity.dxf.end_angle - entity.dxf.start_angle) % 360.0
            except Exception:
                sweep = 0.0
            if sweep >= 359.9 or _is_closed_polygon(verts):
                closed_polygons.append(verts)
            else:
                open_segments.append((verts[0], verts[-1], verts))
            continue

        # SPLINE / ELLIPSE: chiuse se gli estremi coincidono, altrimenti tratti da concatenare
        if _is_closed_polygon(verts):
            closed_polygons.append(verts)
        else:
            open_segments.append((verts[0], verts[-1], verts))

    return closed_polygons, open_segments


# ============================================================================
# Chain walking — ricostruisce poligoni da entità aperte (LINE/ARC sciolte)
# ============================================================================

MIN_VERTICI_CHAIN = 4   # Scarta chain con meno di N vertici (artefatti a-b-a)
MIN_SEGMENTI_CHAIN = 3  # Scarta chain con meno di N segmenti aggregati


def _chain_polygons(open_segments: list, tol: float = TOL_ENDPOINT_MM) -> list[list[tuple[float, float]]]:
    """Assembla open_segments in poligoni chiusi tramite walk degli endpoint.

    Input: lista di (start_pt, end_pt, verts) — endpoints e geometria appiattita di
    ogni entità aperta.

    Algoritmo greedy:
    - Indice spaziale (hash su coordinate arrotondate) per lookup endpoint vicini
    - Per ogni segmento non visitato, walk seguendo endpoint coincidenti finché
      torno al punto di partenza (= ciclo chiuso) o non trovo continuazione (= scarto)

    Filtri anti-artefatto:
    - Chain con < MIN_SEGMENTI_CHAIN entità aggregate scartata (di solito linee a-b-a)
    - Chain con < MIN_VERTICI_CHAIN vertici scartata

    Output: lista di poligoni chiusi (vertici concatenati).
    """
    if not open_segments:
        return []

    # Index: chiave = endpoint arrotondato → lista di (idx_segmento, endpoint_type 'start'/'end')
    def _key(pt: tuple[float, float]) -> tuple[int, int]:
        # Risoluzione = tol (es. 0.05mm → griglia 0.05mm)
        return (int(round(pt[0] / tol)), int(round(pt[1] / tol)))

    endpoint_index: dict[tuple[int, int], list[tuple[int, str]]] = {}
    for i, (s, e, _verts) in enumerate(open_segments):
        endpoint_index.setdefault(_key(s), []).append((i, 'start'))
        endpoint_index.setdefault(_key(e), []).append((i, 'end'))

    visited = set()
    polygons: list[list[tuple[float, float]]] = []

    for seed_idx in range(len(open_segments)):
        if seed_idx in visited:
            continue

        # Walk dal seed_idx
        chain_verts: list[tuple[float, float]] = []
        current_idx = seed_idx
        current_orientation = 'forward'  # forward = usa verts; backward = usa verts reversed
        start_pt = open_segments[seed_idx][0]
        last_pt = start_pt
        local_visited: set[int] = set()
        max_steps = len(open_segments) + 10  # backstop loop

        for _step in range(max_steps):
            if current_idx in local_visited:
                # Loop interno → poligono chiuso trovato
                break
            local_visited.add(current_idx)

            s, e, verts = open_segments[current_idx]
            # Se siamo entrati dall'endpoint 'e', dobbiamo girare i vertici
            if current_orientation == 'forward':
                chain_verts.extend(verts if not chain_verts else verts[1:])
                next_pt = e
            else:
                rev = list(reversed(verts))
                chain_verts.extend(rev if not chain_verts else rev[1:])
                next_pt = s

            last_pt = next_pt

            # Verifica chiusura ciclo
            if len(chain_verts) >= MIN_VERTICI_CHAIN and math.hypot(last_pt[0] - start_pt[0], last_pt[1] - start_pt[1]) <= tol:
                # Chain chiusa — accetta solo se aggregava ≥ MIN_SEGMENTI_CHAIN entità
                if len(local_visited) >= MIN_SEGMENTI_CHAIN:
                    visited.update(local_visited)
                    polygons.append(chain_verts)
                break

            # Cerca prossimo segmento via endpoint
            candidates = endpoint_index.get(_key(last_pt), [])
            next_idx = None
            next_orient = 'forward'
            for cand_idx, cand_type in candidates:
                if cand_idx in local_visited or cand_idx in visited:
                    continue
                # Se candidato attacca con 'start' → forward; con 'end' → backward
                next_idx = cand_idx
                next_orient = 'forward' if cand_type == 'start' else 'backward'
                break

            if next_idx is None:
                # Dead end → chain non chiusa, scarta
                break
            current_idx = next_idx
            current_orientation = next_orient

    return polygons


# ============================================================================
# Dedup viste multiple
# ============================================================================

def _dedup_viste_multiple(polygons: list[list[tuple[float, float]]],
                          tol_pct: float = TOL_DEDUP_PCT) -> tuple[list[list[tuple[float, float]]], int]:
    """Rimuove duplicati di viste multiple: poligoni con bbox simile entro tol_pct.

    Logica: due poligoni sono "stessa vista in posizione diversa" se le loro bbox
    hanno dimensioni che differiscono meno di tol_pct. Tieni quello con area maggiore.
    Tipico DXF officina: pezzo sviluppato + 2 sezioni → 3 poligoni dimensione simile.

    Returns: (poligoni_filtrati, n_duplicati_rimossi)
    """
    if len(polygons) <= 1:
        return polygons, 0

    # Calcola bbox per ogni poligono
    polys_with_meta = []
    for verts in polygons:
        bb = _polygon_bbox(verts)
        w, h = _bbox_size(bb)
        area = _shoelace_area(verts)
        polys_with_meta.append({'verts': verts, 'bb': bb, 'w': w, 'h': h, 'area': area})

    # Cluster: due poligoni nello stesso cluster se entrambi w e h differiscono <tol_pct
    clusters: list[list[dict]] = []
    for p in polys_with_meta:
        assigned = False
        for cl in clusters:
            ref = cl[0]
            if ref['w'] == 0 or ref['h'] == 0:
                continue
            dw = abs(p['w'] - ref['w']) / ref['w']
            dh = abs(p['h'] - ref['h']) / ref['h']
            if dw <= tol_pct and dh <= tol_pct:
                cl.append(p)
                assigned = True
                break
        if not assigned:
            clusters.append([p])

    # Per ogni cluster con >1 elementi, tieni solo quello con area massima
    filtered = []
    n_dup = 0
    for cl in clusters:
        if len(cl) == 1:
            filtered.append(cl[0]['verts'])
        else:
            best = max(cl, key=lambda x: x['area'])
            filtered.append(best['verts'])
            n_dup += len(cl) - 1

    return filtered, n_dup


# ============================================================================
# Outer/Inner separation
# ============================================================================

def _classify_outer_inner(polygons: list[list[tuple[float, float]]],
                          circles_centri: list[tuple[float, float]] | None = None
                          ) -> tuple[list[tuple[float, float]] | None, list[list[tuple[float, float]]]]:
    """Identifica outer contour e inner contours.

    Score outer = (n_cerchi_contenuti, area_poligono). Lessicografico:
    - Primo criterio: numero di CIRCLE contenuti nel poligono (= fori del pezzo).
      Il pezzo ha fori, la cornice cartiglio no.
    - Secondo criterio: area (per tie-break tra poligoni senza fori).

    Inner = altri poligoni con centroide dentro l'outer.

    Restituisce (outer_verts_or_None, list_of_inner_verts).
    """
    if not polygons:
        return None, []

    circles_centri = circles_centri or []

    def _score(verts):
        n_cerchi = sum(1 for c in circles_centri if _point_in_polygon(c, verts))
        area = _shoelace_area(verts)
        return (n_cerchi, area)

    scored = [(verts, _score(verts)) for verts in polygons]
    scored.sort(key=lambda x: x[1], reverse=True)

    outer = scored[0][0]
    inners = []
    for verts, _s in scored[1:]:
        if not verts:
            continue
        if _poligono_contenuto(verts, outer):
            inners.append(verts)
    # Contorni interni annidati: svasatura (concentrici) → resta il passante
    # (il più piccolo); isola dentro un foro → resta il foro esterno.
    drop = set()
    for i, a in enumerate(inners):
        for j, b in enumerate(inners):
            if i == j or i in drop or j in drop:
                continue
            if _shoelace_area(b) >= _shoelace_area(a) or not _poligono_contenuto(b, a):
                continue
            ca = (sum(v[0] for v in a) / len(a), sum(v[1] for v in a) / len(a))
            cb = (sum(v[0] for v in b) / len(b), sum(v[1] for v in b) / len(b))
            r_b = (_shoelace_area(b) / math.pi) ** 0.5
            if math.hypot(ca[0] - cb[0], ca[1] - cb[1]) <= max(0.5, 0.15 * r_b):
                drop.add(i)
            else:
                drop.add(j)
    inners = [p for k, p in enumerate(inners) if k not in drop]
    return outer, inners


# ============================================================================
# API PUBBLICA
# ============================================================================

def detect_pezzo_geometry(path: str, config: dict | None = None) -> dict:
    """Estrae geometria pezzo da DXF con polygon detection vero (algoritmo CAM).

    Args:
        path: percorso file DXF.
        config: dict opzionale con `dxf_colori_piega` e `dxf_colori_saldatura`
                (colori da escludere come non-taglio).

    Returns:
        {
            area_dm2: float,                   # area NETTA (outer - inner)
            area_lorda_dm2: float,             # area solo outer (per peso lamiera)
            perimetro_taglio_m: float,         # outer + inner perimeters
            n_pierce: int,                     # 1 (outer) + count(inner)
            n_inner: int,                      # numero fori interni
            bbox_width_mm: float,
            bbox_height_mm: float,
            tipo_disegno: str,                 # 'normale' | 'cartiglio_filtrato' | 'viste_multiple'
            poligoni_grezzi: int,              # totale poligoni rilevati pre-filter
            poligoni_cartiglio_rimossi: int,
            poligoni_viste_duplicate_rimosse: int,
            warnings: list[str],
        }
    """
    cfg = config or {}
    colori_esclusi = set(cfg.get('dxf_colori_piega', [2])) | set(cfg.get('dxf_colori_saldatura', [1]))
    warnings: list[str] = []

    try:
        doc = ezdxf.readfile(path)
    except Exception as e:
        logger.warning("detect_pezzo_geometry: lettura DXF fallita %s: %s", path, e)
        return _empty_result([f"DXF non leggibile: {e}"])

    msp = doc.modelspace()
    from .dxf_polygon_detector_v3 import scala_unita_mm
    scala, w_unita = scala_unita_mm(doc)
    if w_unita:
        warnings.append(w_unita)

    # ---- 1. Estrai poligoni chiusi + segmenti aperti (mm, INSERT espansi)
    closed_polys, open_segs = _extract_polygons(msp, colori_esclusi, scala)
    logger.debug("DXF %s: %d poligoni chiusi, %d entità aperte", os.path.basename(path), len(closed_polys), len(open_segs))

    # ---- 2. Chain walking SEMPRE attivo
    # I DXF cliente (Lantek inclusi) hanno il contorno pezzo come LINE/ARC sciolti
    # sul layer 0 — vanno SEMPRE ricostruiti via chain walking. I poligoni nativi
    # sono solo CIRCLE (fori) e cornici cartiglio (layer FORMAT, già filtrato).
    chained = _chain_polygons(open_segs)
    n_chained = len(chained)
    logger.debug("DXF %s: chain walking → %d poligoni assemblati", os.path.basename(path), n_chained)
    all_polygons = closed_polys + chained
    n_grezzi = len(all_polygons)

    if not all_polygons:
        warnings.append("Nessun poligono chiuso rilevato (DXF con sole linee aperte/non assemblabili)")
        return _empty_result(warnings, n_grezzi=0)

    # ---- 3. Filtra poligoni cartiglio
    # Due criteri (DXF Lantek hanno cartiglio in più forme):
    # a) Bbox = formato foglio ISO A0-A4 (cartigli standard di altri vendor)
    # b) Cornice cartiglio (rettangolo puro che CONTIENE molti altri poligoni)
    #    — distingue cornici da pezzi rettangolari (un pezzo ha pochi fori dentro)
    cartiglio_count = 0
    filtered_polygons = []
    for verts in all_polygons:
        bb = _polygon_bbox(verts)
        if _is_cartiglio_format(bb):
            cartiglio_count += 1
            continue
        if _is_cornice_cartiglio(verts, all_polygons):
            cartiglio_count += 1
            continue
        filtered_polygons.append(verts)

    if cartiglio_count > 0:
        logger.debug("DXF %s: %d poligoni cartiglio/cornice filtrati", os.path.basename(path), cartiglio_count)

    if not filtered_polygons:
        warnings.append(f"Solo {cartiglio_count} poligoni cartiglio rilevati, nessun pezzo")
        return _empty_result(warnings, n_grezzi=n_grezzi)

    # ---- 4. Filtra micro-artefatti (poligoni < 1% dell'area max)
    # Tipici artefatti: linee allungate chiuse erroneamente (perim/area ratio molto alto)
    n_dup = 0
    if filtered_polygons:
        areas = [_shoelace_area(p) for p in filtered_polygons]
        area_max = max(areas) if areas else 0.0
        soglia_min = max(1.0, area_max * 0.001)  # almeno 1 mm² o 0.1% area max
        filtered_polygons = [p for p, a in zip(filtered_polygons, areas) if a >= soglia_min]
    tipo_disegno = 'normale'
    if cartiglio_count > 0:
        tipo_disegno = 'cartiglio_filtrato'

    # ---- 5. Outer/Inner classification
    # Outer = poligono che contiene PIÙ CIRCLE (= fori del pezzo); tie-break su area
    # Inner = poligoni con centroide DENTRO l'outer = fori complessi del pezzo
    # Strategia: il pezzo Lantek ha sempre fori (forature, asole), la cornice cartiglio no
    from .dxf_polygon_detector_v3 import entita_espanse
    circles_centri = []
    for e in entita_espanse(msp, solo_geometria=True):
        if e.dxftype() != 'CIRCLE':
            continue
        try:
            layer = e.dxf.layer
            if _layer_da_escludere(layer):
                continue
        except AttributeError:
            pass
        if _entity_color_excluded(e, colori_esclusi):
            continue
        try:
            c = e.ocs().to_wcs(e.dxf.center)
            circles_centri.append((float(c.x) * scala, float(c.y) * scala))
        except AttributeError:
            continue

    outer, inners = _classify_outer_inner(filtered_polygons, circles_centri)
    if outer is None:
        warnings.append("Outer contour non determinabile")
        return _empty_result(warnings, n_grezzi=n_grezzi)

    # ---- 6. Calcoli finali
    area_outer_mm2 = _shoelace_area(outer)
    area_inner_mm2 = sum(_shoelace_area(inn) for inn in inners)
    area_netta_mm2 = max(0.0, area_outer_mm2 - area_inner_mm2)

    perim_outer_mm = _polygon_perim(outer, closed=True)
    perim_inner_mm = sum(_polygon_perim(inn, closed=True) for inn in inners)
    perim_totale_mm = perim_outer_mm + perim_inner_mm

    bb_outer = _polygon_bbox(outer)
    bbox_w_mm = bb_outer[2] - bb_outer[0]
    bbox_h_mm = bb_outer[3] - bb_outer[1]

    n_pierce = 1 + len(inners)  # 1 per contorno esterno + 1 per ogni foro

    # ---- 7. Sanity check: warning se peso/area sembrano sospetti
    if area_netta_mm2 < 100:  # < 1 cm² è strano
        warnings.append(f"Area pezzo molto piccola: {area_netta_mm2:.1f} mm² — verificare DXF")
    if perim_totale_mm < 10:
        warnings.append(f"Perimetro molto corto: {perim_totale_mm:.1f} mm")
    if bbox_w_mm > 3000 or bbox_h_mm > 3000:
        warnings.append(f"Pezzo molto grande: {bbox_w_mm:.0f}×{bbox_h_mm:.0f} mm — verificare unità DXF")

    # ---- 8. Confidence ESPLICITA (BUG FIX D10: prima mancava → il chiamante la
    # leggeva come 0 e sostituiva sempre area/perimetro col fallback cartiglio).
    # v2 = fallback del v3: al massimo "media". Bassa se c'è un altro contorno
    # esterno di dimensione confrontabile (pezzo scelto in modo ambiguo).
    ids_inner = {id(p) for p in inners}
    altri = [p for p in filtered_polygons
             if p is not outer and id(p) not in ids_inner and not _poligono_contenuto(p, outer)]
    ambiguo = any(_shoelace_area(p) >= 0.3 * area_outer_mm2 for p in altri)
    confidence = 0.45 if ambiguo else 0.6
    if ambiguo:
        warnings.append('Più contorni esterni di dimensioni confrontabili: verificare il pezzo scelto')

    return {
        'area_dm2': round(area_netta_mm2 / 10000.0, 4),
        'area_lorda_dm2': round(area_outer_mm2 / 10000.0, 4),
        'perimetro_taglio_m': round(perim_totale_mm / 1000.0, 4),
        'n_pierce': n_pierce,
        'n_forature': n_pierce,
        'n_fori': len(inners),
        'confidence': confidence,
        'confidence_label': 'media' if confidence >= 0.6 else 'bassa',
        'needs_manual_select': confidence < 0.6,
        'n_inner': len(inners),
        'bbox_width_mm': round(bbox_w_mm, 2),
        'bbox_height_mm': round(bbox_h_mm, 2),
        'tipo_disegno': tipo_disegno,
        'poligoni_grezzi': n_grezzi,
        'poligoni_cartiglio_rimossi': cartiglio_count,
        'poligoni_viste_duplicate_rimosse': n_dup,
        'warnings': warnings,
    }


def _empty_result(warnings: list[str], n_grezzi: int = 0) -> dict:
    return {
        'area_dm2': 0.0,
        'area_lorda_dm2': 0.0,
        'perimetro_taglio_m': 0.0,
        'n_pierce': 0,
        'n_inner': 0,
        'bbox_width_mm': 0.0,
        'bbox_height_mm': 0.0,
        'confidence': 0.0,
        'tipo_disegno': 'vuoto',
        'poligoni_grezzi': n_grezzi,
        'poligoni_cartiglio_rimossi': 0,
        'poligoni_viste_duplicate_rimosse': 0,
        'warnings': warnings,
    }
