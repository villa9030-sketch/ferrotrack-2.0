"""Pulizia DXF: rimuove cartiglio/quote/note/viste secondarie mantenendo
solo la geometria del pezzo (contorno + fori interni).

Uso tipico: dopo che il detector v3 ha identificato il pezzo (bbox + polygon),
`save_cleaned_dxf` crea un nuovo file `<name>_cleaned.dxf` che contiene SOLO
le entità geometricamente dentro il bounding box del pezzo. Il commerciale
vede il thumbnail pulito, Mirko riceve un file DXF già pronto per il nesting
Lantek senza cartiglio.

NB: la pulizia AUTOMATICA dell'import usa `save_cleaned_dxf_pezzo` (contorno
esatto del detector + verifica misure, vedi sotto); `save_cleaned_dxf` resta
per il drag rettangolare manuale.

Regole di filtro (per bounding box, NON per layer perché i layer sono
variabili tra fornitori):

- LINE:             entrambi gli endpoint dentro bbox (con tolleranza)
- CIRCLE:           centro dentro bbox (raggio tipicamente piccolo → forature)
- ARC:              centro dentro bbox
- LWPOLYLINE:       tutti i vertici dentro bbox
- POLYLINE:         tutti i vertici dentro bbox
- SPLINE:           tutti i control points dentro bbox
- ELLIPSE:          centro dentro bbox
- TEXT/MTEXT:       SKIP (sono quote o note, non geometria)
- DIMENSION/LEADER: SKIP (linee di quota, sono metadati)
- INSERT:           blocchi di geometria ESPANSI (entità copiate esplose);
                    blocchi di annotazione (cartiglio, note, logo) SKIP
- HATCH:            SKIP (tratteggi, non tagliano)

Tolleranza: max(1 mm, 2% del lato bbox più corto). Serve perché alcuni
fornitori mettono forature al bordo del pezzo che escono per micron.

Sanity check post-cleanup:
- Se il file pulito è vuoto (0 entità) → errore, si mantiene originale
- Se il file pulito ha <5% delle entità originali → warning, sospetto
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from typing import Iterable, Sequence

import ezdxf

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Export automatico DXF puliti in <root>/<cliente>/<numero_ordine>/
# Chiamato dopo ogni save-cleaned-by-click. Best-effort: se il path
# non è configurato o la scrittura fallisce, log warning e continua
# (il file DXF pulito resta comunque in uploads/preventivi_tmp/).
# ═══════════════════════════════════════════════════════════════════

_PATH_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1F]')


def _sanitize_path_part(name: str, fallback: str = 'unknown') -> str:
    """Rende un nome sicuro per uso come parte di path filesystem.
    Rimuove caratteri invalidi Windows (<>:"/\\|?*) + spazi iniziali/finali."""
    if not name:
        return fallback
    clean = _PATH_INVALID_CHARS.sub('_', str(name)).strip().strip('.')
    if not clean:
        return fallback
    # Limita a 100 char per evitare path troppo lunghi
    return clean[:100]


def export_cleaned_dxf_to_client_folder(
    cleaned_source_path: str,
    cliente: str,
    numero_ordine: str,
    original_filename: str,
    export_root: str,
) -> dict:
    """Copia il DXF pulito nella cartella <export_root>/<cliente>/<numero_ordine>/.

    Args:
        cleaned_source_path: path assoluto del file cleaned in preventivi_tmp
        cliente: nome cliente (sanitizzato per path)
        numero_ordine: numero ordine cliente OPPURE fallback PREV-YYYY-NNNN
        original_filename: nome originale del DXF (senza _cleaned)
        export_root: root dell'export configurato dall'admin

    Returns:
        {'success': bool, 'exported_path': str|None, 'error': str|None}
    """
    result = {'success': False, 'exported_path': None, 'error': None}

    if not export_root or not export_root.strip():
        result['error'] = 'export_root non configurato (Impostazioni → Cartella disegni per Mirko)'
        return result

    if not os.path.exists(cleaned_source_path):
        result['error'] = f'source non trovato: {cleaned_source_path}'
        return result

    try:
        cliente_dir = _sanitize_path_part(cliente, 'cliente_sconosciuto')
        ordine_dir = _sanitize_path_part(numero_ordine, 'ordine_sconosciuto')
        # Nome file finale: usa l'originale (es: 20PA00693-00.dxf), non il "_cleaned"
        # perché a Mirko interessa il codice pezzo, non il flag di pulizia.
        target_name = _sanitize_path_part(os.path.basename(original_filename), 'pezzo.dxf')
        if not target_name.lower().endswith(('.dxf', '.dwg')):
            target_name += '.dxf'

        target_dir = os.path.join(export_root.strip(), cliente_dir, ordine_dir)
        os.makedirs(target_dir, exist_ok=True)
        target_path = os.path.join(target_dir, target_name)

        # Copia (overwrite se esiste — l'utente può aver rifatto il cleanup)
        shutil.copy2(cleaned_source_path, target_path)
        result['success'] = True
        result['exported_path'] = target_path
        logger.info('DXF esportato: %s → %s', os.path.basename(cleaned_source_path), target_path)
    except PermissionError as e:
        result['error'] = f'permessi negati sulla cartella {export_root}: {e}'
        logger.warning('export DXF fallito (permessi): %s', e)
    except OSError as e:
        result['error'] = f'errore filesystem: {e}'
        logger.warning('export DXF fallito (OS): %s', e)
    except Exception as e:
        result['error'] = f'errore: {e}'
        logger.warning('export DXF fallito: %s', e)
    return result

# Tipi entità sempre esclusi dal cleanup (sono metadati, non geometria del pezzo)
_SKIP_TYPES = frozenset({
    'TEXT', 'MTEXT', 'ATTDEF', 'ATTRIB',
    'DIMENSION', 'LEADER', 'MULTILEADER', 'ARC_DIMENSION',
    'INSERT',            # block reference (cartiglio spesso è un block)
    'HATCH',             # tratteggi
    'IMAGE', 'WIPEOUT',  # oggetti immagine/mascheratura
})


def _tolerance_for_bbox(bbox: Sequence[float]) -> float:
    """Tolleranza (mm) per contenimento in bbox.

    Il drag rettangolare umano è impreciso: l'utente traccia una selezione
    "grosso modo" attorno al pezzo, a volte 1-3mm troppo stretta su un lato.
    Se la tolerance è troppo piccola (es. 1mm), un pezzo 410×30mm dove
    l'utente ha selezionato fino a Y=281 invece di Y=283 perde le entità
    del bordo superiore (linea che chiude + smussi ai raccordi) e il bbox
    risultante è 410×28 invece di 410×30.

    Formula: `max(5mm, 8% del lato corto del bbox)`.
    Il connected-components filter downstream elimina comunque i cluster
    di cartiglio isolati, quindi una tolerance generosa non re-introduce
    falsi positivi.
    """
    minx, miny, maxx, maxy = bbox
    side = min(maxx - minx, maxy - miny)
    return max(5.0, side * 0.08)


def _point_in_bbox(x: float, y: float, bbox: Sequence[float], tol: float) -> bool:
    minx, miny, maxx, maxy = bbox
    return (minx - tol) <= x <= (maxx + tol) and (miny - tol) <= y <= (maxy + tol)


def _entity_bbox(entity) -> tuple[float, float, float, float] | None:
    """Bbox (minx, miny, maxx, maxy) di una singola entità, in mm.
    None se non calcolabile."""
    et = entity.dxftype()
    try:
        if et == 'LINE':
            s, ee = entity.dxf.start, entity.dxf.end
            return (min(s[0], ee[0]), min(s[1], ee[1]),
                    max(s[0], ee[0]), max(s[1], ee[1]))
        if et in ('CIRCLE', 'ARC'):
            c = entity.dxf.center
            r = float(getattr(entity.dxf, 'radius', 0) or 0)
            return (c[0] - r, c[1] - r, c[0] + r, c[1] + r)
        if et == 'ELLIPSE':
            c = entity.dxf.center
            return (c[0] - 1, c[1] - 1, c[0] + 1, c[1] + 1)  # approx
        if et == 'LWPOLYLINE':
            pts = list(entity.get_points('xy'))
            if not pts: return None
            xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
            return (min(xs), min(ys), max(xs), max(ys))
        if et == 'POLYLINE':
            pts = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
            if not pts: return None
            xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
            return (min(xs), min(ys), max(xs), max(ys))
        if et == 'SPLINE':
            ctrl = list(entity.control_points or []) or list(entity.fit_points or [])
            if not ctrl: return None
            xs = [p[0] for p in ctrl]; ys = [p[1] for p in ctrl]
            return (min(xs), min(ys), max(xs), max(ys))
        if et in ('POINT', 'SOLID'):
            loc = getattr(entity.dxf, 'location', None) or getattr(entity.dxf, 'vtx0', None)
            if loc is None: return None
            return (loc[0], loc[1], loc[0], loc[1])
    except Exception:
        return None
    return None


def _bboxes_overlap(a, b, gap: float) -> bool:
    """True se i due bbox si sovrappongono o distano <= gap (in mm)."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    if ax2 + gap < bx1 or bx2 + gap < ax1:
        return False
    if ay2 + gap < by1 or by2 + gap < ay1:
        return False
    return True


def _all_clusters(entities_with_bbox: list, gap: float) -> list[list[int]]:
    """Union-Find sulle entità in base a prossimità bbox (<=gap mm).
    Ritorna TUTTI i cluster come lista di liste di indici."""
    n = len(entities_with_bbox)
    if n == 0:
        return []
    if n == 1:
        return [[0]]
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry: parent[rx] = ry

    for i in range(n):
        bi = entities_with_bbox[i][1]
        for j in range(i + 1, n):
            bj = entities_with_bbox[j][1]
            if _bboxes_overlap(bi, bj, gap):
                union(i, j)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        r = find(i)
        groups.setdefault(r, []).append(i)
    return list(groups.values())


def _pick_best_cluster(entities_with_bbox: list, gap: float) -> list:
    """Union-Find sulle entità in base a prossimità bbox (<=gap mm), poi sceglie
    il cluster che ha maggiori probabilità di essere IL PEZZO.

    Metrica "pezzo-like":
      score = n_circles * 10           # i fori sono un forte segnale di pezzo laser
            + n_polylines * 5           # contorni chiusi sono tipici del pezzo
            + n_arcs * 2                # smussi/raccordi = pezzo
            + n_total * 0.1             # tiebreak: più entità = più probabile pezzo

    I cartigli tabellari sono fatti di LINE corte in griglia — score basso perché
    n_circles=0, n_polylines=0, n_arcs=0. I pezzi laser hanno tipicamente ≥1 foro
    o ≥1 polyline chiusa (contorno esterno).

    Ritorna la lista di indici del cluster vincente.
    """
    n = len(entities_with_bbox)
    if n <= 1:
        return list(range(n))
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry: parent[rx] = ry

    for i in range(n):
        bi = entities_with_bbox[i][1]
        for j in range(i + 1, n):
            bj = entities_with_bbox[j][1]
            if _bboxes_overlap(bi, bj, gap):
                union(i, j)
    # Raggruppa per root
    groups: dict[int, list[int]] = {}
    for i in range(n):
        r = find(i)
        groups.setdefault(r, []).append(i)

    def cluster_score(idxs: list[int]) -> float:
        n_c = n_p = n_a = 0
        for i in idxs:
            et = entities_with_bbox[i][0].dxftype()
            if et == 'CIRCLE': n_c += 1
            elif et in ('LWPOLYLINE', 'POLYLINE'): n_p += 1
            elif et == 'ARC': n_a += 1
        return n_c * 10 + n_p * 5 + n_a * 2 + len(idxs) * 0.1

    return max(groups.values(), key=cluster_score)


def _entity_in_bbox(entity, bbox: Sequence[float], tol: float) -> bool:
    """Ritorna True se l'entità è (in tutto o in parte) dentro il bbox.

    Regola permissiva: se ALMENO UN punto dell'entità è dentro il bbox
    con la tolleranza, la copio. Preferisco "troppo generoso" a "troppo
    stretto": Mirko poi vede il DXF pulito e capisce, mentre "0 entità"
    è UX rotta. La tolleranza aggiuntiva serve per bordi al mm.
    """
    et = entity.dxftype()
    try:
        if et == 'LINE':
            s, ee = entity.dxf.start, entity.dxf.end
            return (_point_in_bbox(s[0], s[1], bbox, tol) or
                    _point_in_bbox(ee[0], ee[1], bbox, tol))
        if et == 'CIRCLE':
            c = entity.dxf.center
            return _point_in_bbox(c[0], c[1], bbox, tol)
        if et == 'ARC':
            c = entity.dxf.center
            return _point_in_bbox(c[0], c[1], bbox, tol)
        if et == 'ELLIPSE':
            c = entity.dxf.center
            return _point_in_bbox(c[0], c[1], bbox, tol)
        if et == 'LWPOLYLINE':
            pts = list(entity.get_points('xy'))
            return any(_point_in_bbox(p[0], p[1], bbox, tol) for p in pts)
        if et == 'POLYLINE':
            pts = [(v.dxf.location.x, v.dxf.location.y) for v in entity.vertices]
            return any(_point_in_bbox(p[0], p[1], bbox, tol) for p in pts)
        if et == 'SPLINE':
            ctrl = list(entity.control_points or [])
            if ctrl:
                return any(_point_in_bbox(p[0], p[1], bbox, tol) for p in ctrl)
            fit = list(entity.fit_points or [])
            if fit:
                return any(_point_in_bbox(p[0], p[1], bbox, tol) for p in fit)
            return False
        if et in ('POINT', 'SOLID'):
            loc = getattr(entity.dxf, 'location', None) or getattr(entity.dxf, 'vtx0', None)
            if loc is None:
                return False
            return _point_in_bbox(loc[0], loc[1], bbox, tol)
    except Exception as e:
        logger.debug('entity_in_bbox %s failed: %s', et, e)
        return False
    return False


def save_cleaned_dxf(
    source_path: str,
    cleaned_path: str,
    bbox: Sequence[float],
    min_entities: int = 1,
) -> dict:
    """Scrive `cleaned_path` con solo le entità dentro bbox del pezzo.

    Args:
        source_path: DXF originale (input)
        cleaned_path: dove scrivere il DXF pulito (output)
        bbox: [minx, miny, maxx, maxy] del pezzo scelto (dal detector v3)
        min_entities: soglia minima entità da copiare (sotto = fail)

    Returns:
        {'success': bool, 'entities_copied': int, 'entities_source': int,
         'entities_skipped_meta': int, 'entities_out_of_bbox': int,
         'tolerance_mm': float, 'error': str|None, 'warnings': [str]}

    Non solleva su errori di parsing entità singole (li logga a DEBUG),
    solleva solo se il DXF non è leggibile o il pulito è vuoto.
    """
    result = {
        'success': False,
        'entities_copied': 0,
        'entities_source': 0,
        'entities_skipped_meta': 0,
        'entities_out_of_bbox': 0,
        'tolerance_mm': 0.0,
        'error': None,
        'warnings': [],
    }

    try:
        src = ezdxf.readfile(source_path)
    except Exception as e:
        result['error'] = f'DXF non leggibile: {e}'
        return result

    tol = _tolerance_for_bbox(bbox)
    result['tolerance_mm'] = tol

    # Nuovo documento vuoto con stessa DXF version dell'originale
    try:
        dst = ezdxf.new(dxfversion=src.dxfversion, setup=False)
    except Exception:
        # Fallback: version di default
        dst = ezdxf.new(setup=False)

    # Copia i layer usati (mantiene i colori originali). Solo quelli.
    src_layers = {l.dxf.name for l in src.layers}
    for lname in src_layers:
        if lname in dst.layers:
            continue
        try:
            src_layer = src.layers.get(lname)
            new_layer = dst.layers.add(lname)
            try:
                new_layer.dxf.color = src_layer.dxf.color
            except Exception:
                pass
        except Exception as e:
            logger.debug('layer %s copy failed: %s', lname, e)

    src_ms = src.modelspace()
    dst_ms = dst.modelspace()
    _copia_unita(src, dst)

    # ═══════════════════════════════════════════════════════════════════
    # STRATEGIA "a prova di stupido":
    #
    # Il bbox utente indica UN PUNTO NELLO SPAZIO ("il pezzo è qui"),
    # NON un ritaglio esatto. La geometria del pezzo viene poi ricostruita
    # per intero seguendo la CONNETTIVITÀ, senza mai tagliare al bbox.
    #
    # Passi:
    # 1. Raccogli TUTTE le entità geometriche del DXF (skip solo metadata:
    #    testo, quote, cartiglio-INSERT, hatch).
    # 2. Costruisci cluster di entità spazialmente connesse (bbox overlap
    #    con gap ~2mm). Ogni cluster è un "oggetto disegnato".
    # 3. Per ogni cluster, calcola quanto INTERSECA il bbox utente.
    # 4. Scarta cluster che non toccano affatto il bbox utente (cartiglio,
    #    viste secondarie, note tecniche).
    # 5. Fra i cluster rimasti, scegli quello più "pezzo-like" (score che
    #    premia CIRCLE/POLYLINE/ARC — indicatori di geometria di taglio).
    # 6. Copia TUTTE le entità del cluster vincente, ANCHE se qualcuna
    #    sta 2-3mm oltre il bbox utente (il bordo superiore del pezzo,
    #    uno smusso, ecc.) — la connettività garantisce che appartengano
    #    allo stesso oggetto.
    #
    # Vantaggi vs il vecchio approccio "filtra per bbox":
    # - Utente traccia rettangolo un pelo stretto → NON PERDE bordi
    # - Utente traccia rettangolo un pelo largo → NON PRENDE cartiglio
    # - Utente clicca dentro il pezzo con rettangolino minuscolo → OK
    # ═══════════════════════════════════════════════════════════════════

    # FASE 1: raccogli tutte le entità geometriche (con bbox individuale)
    all_geom: list[tuple[object, tuple[float, float, float, float]]] = []
    for e in _entita_sorgente(src_ms):
        result['entities_source'] += 1
        et = e.dxftype()
        if et in _SKIP_TYPES:
            result['entities_skipped_meta'] += 1
            continue
        eb = _entity_bbox(e)
        if eb is None:
            # Entità senza bbox: skip (non possiamo clusterizzarla)
            result['entities_skipped_meta'] += 1
            continue
        all_geom.append((e, eb))

    if not all_geom:
        result['error'] = 'DXF senza entità geometriche riconoscibili.'
        return result

    # FASE 2: cluster spaziale su TUTTE le entità (gap 2mm)
    merge_gap = 2.0
    clusters = _all_clusters(all_geom, merge_gap)

    # FASE 3+4: seleziona cluster che toccano bbox utente
    # Un cluster "tocca" il bbox utente se il suo bbox unione interseca
    # il bbox utente con una tolleranza generosa (10mm — il pezzo può
    # sporgere di 5-10mm dal drag utente in ogni direzione).
    touch_tol = 10.0
    touching_clusters: list[list[int]] = []
    for cluster_idxs in clusters:
        cx1 = min(all_geom[i][1][0] for i in cluster_idxs)
        cy1 = min(all_geom[i][1][1] for i in cluster_idxs)
        cx2 = max(all_geom[i][1][2] for i in cluster_idxs)
        cy2 = max(all_geom[i][1][3] for i in cluster_idxs)
        if _bboxes_overlap((cx1, cy1, cx2, cy2), tuple(bbox), touch_tol):
            touching_clusters.append(cluster_idxs)

    if not touching_clusters:
        # Fallback: nessun cluster tocca — usa il vecchio filtro bbox strict
        logger.warning('cleanup: nessun cluster tocca il bbox utente %s, uso fallback filtro strict', bbox)
        touching_clusters = [[i for i, (_e, eb) in enumerate(all_geom)
                              if _bboxes_overlap(eb, tuple(bbox), tol)]]

    # FASE 5: fra i cluster candidati, scegli il più "pezzo-like"
    def cluster_score(idxs: list[int]) -> float:
        n_c = n_p = n_a = 0
        for i in idxs:
            et = all_geom[i][0].dxftype()
            if et == 'CIRCLE': n_c += 1
            elif et in ('LWPOLYLINE', 'POLYLINE'): n_p += 1
            elif et == 'ARC': n_a += 1
        return n_c * 10 + n_p * 5 + n_a * 2 + len(idxs) * 0.1

    # BUG FIX: col solo score vinceva la CORNICE del foglio (contiene il bbox,
    # quindi lo "tocca") o una svasatura (2 cerchi = 20 punti contro un contorno
    # fatto di LINE). Il rettangolo utente approssima il PEZZO: si scartano i
    # cluster molto più grandi del rettangolo e vince quello che gli somiglia di
    # più (IoU dei bbox); lo score resta solo come spareggio.
    ux1, uy1, ux2, uy2 = [float(v) for v in bbox]
    area_utente = max(1e-9, (ux2 - ux1) * (uy2 - uy1))

    def _bbox_cluster(idxs):
        return (min(all_geom[i][1][0] for i in idxs), min(all_geom[i][1][1] for i in idxs),
                max(all_geom[i][1][2] for i in idxs), max(all_geom[i][1][3] for i in idxs))

    def _iou(idxs) -> float:
        cx1, cy1, cx2, cy2 = _bbox_cluster(idxs)
        ix = max(0.0, min(cx2, ux2) - max(cx1, ux1))
        iy = max(0.0, min(cy2, uy2) - max(cy1, uy1))
        inter = ix * iy
        area_c = max(0.0, cx2 - cx1) * max(0.0, cy2 - cy1)
        return inter / max(1e-9, area_c + area_utente - inter)

    def _area_cluster(idxs) -> float:
        cx1, cy1, cx2, cy2 = _bbox_cluster(idxs)
        return max(0.0, cx2 - cx1) * max(0.0, cy2 - cy1)

    non_giganti = [c for c in touching_clusters if _area_cluster(c) <= 4.0 * area_utente]
    winner = max(non_giganti or touching_clusters,
                 key=lambda c: (round(_iou(c), 2), cluster_score(c)))
    winner_set = set(winner)

    # FASE 5b: ASSORBIMENTO fori interni.
    # I fori del pezzo sono spesso entità isolate (CIRCLE piccoli, ARC di svasature,
    # slot di forature ovali) che NON toccano il contorno esterno — quindi
    # il clustering le mette in cluster separati. Ma sono geometricamente
    # DENTRO il bbox del contorno → appartengono al pezzo.
    # Regola: assorbi qualsiasi entità il cui bbox è interamente contenuto
    # nel bbox unione del cluster vincente (con tolleranza 1mm ai bordi).
    wx1 = min(all_geom[i][1][0] for i in winner)
    wy1 = min(all_geom[i][1][1] for i in winner)
    wx2 = max(all_geom[i][1][2] for i in winner)
    wy2 = max(all_geom[i][1][3] for i in winner)
    absorb_tol = 1.0
    absorbed = 0
    for idx, (_e, eb) in enumerate(all_geom):
        if idx in winner_set:
            continue
        ex1, ey1, ex2, ey2 = eb
        # Bbox entità interamente dentro bbox cluster (con tolleranza)
        if (ex1 >= wx1 - absorb_tol and ex2 <= wx2 + absorb_tol and
            ey1 >= wy1 - absorb_tol and ey2 <= wy2 + absorb_tol):
            winner_set.add(idx)
            absorbed += 1
    if absorbed > 0:
        logger.info('cleanup: assorbite %d entità interne al bbox del pezzo (fori isolati)',
                    absorbed)

    dropped_by_cluster = len(all_geom) - len(winner_set)
    if dropped_by_cluster > 0:
        result['entities_dropped_component'] = dropped_by_cluster
        logger.info('cleanup: cluster vincente ha %d entità (di cui %d assorbite), scartate %d (cartiglio/note/viste)',
                    len(winner_set), absorbed, dropped_by_cluster)

    # FASE 6: copia TUTTE le entità del cluster vincente (anche fuori bbox utente)
    entities_out_of_bbox_count = 0
    for idx, (e, eb) in enumerate(all_geom):
        if idx not in winner_set:
            # Statistica: era fuori bbox o solo scartato dal cluster?
            if not _bboxes_overlap(eb, tuple(bbox), tol):
                entities_out_of_bbox_count += 1
            continue
        try:
            new_e = e.copy()
            dst_ms.add_entity(new_e)
            result['entities_copied'] += 1
        except Exception as ex:
            logger.debug('entity copy failed %s: %s', e.dxftype(), ex)
    result['entities_out_of_bbox'] = entities_out_of_bbox_count

    if result['entities_copied'] < min_entities:
        result['error'] = (
            f'Nessuna geometria trovata nel rettangolo selezionato. '
            f'Prova a ridisegnare il rettangolo un po\' più grande, '
            f'assicurandoti di includere tutto il contorno del pezzo (esterno + fori).'
        )
        return result

    # Ratio warning: se abbiamo copiato meno del 5% dell'originale, sospetto
    src_geom = result['entities_source'] - result['entities_skipped_meta']
    if src_geom > 0:
        ratio = result['entities_copied'] / src_geom
        if ratio < 0.05:
            result['warnings'].append(
                f'Solo {result["entities_copied"]}/{src_geom} entità geom. copiate '
                f'({ratio*100:.1f}%): verifica bbox pezzo.'
            )

    _marca_pulito(dst, TIPO_PULIZIA_MANUALE)
    try:
        os.makedirs(os.path.dirname(cleaned_path), exist_ok=True)
        dst.saveas(cleaned_path)
    except Exception as e:
        result['error'] = f'Scrittura fallita: {e}'
        return result

    # Calcola bbox reale delle entità copiate (utile per dimensioni pezzo).
    # Iteriamo su dst_ms per prendere gli endpoint delle entità geometriche.
    try:
        xs, ys = [], []
        for e in dst_ms:
            et = e.dxftype()
            if et == 'LINE':
                s, ee = e.dxf.start, e.dxf.end
                xs.extend([s[0], ee[0]]); ys.extend([s[1], ee[1]])
            elif et in ('CIRCLE', 'ARC'):
                c = e.dxf.center
                r = float(getattr(e.dxf, 'radius', 0) or 0)
                xs.extend([c[0] - r, c[0] + r]); ys.extend([c[1] - r, c[1] + r])
            elif et == 'ELLIPSE':
                c = e.dxf.center
                xs.append(c[0]); ys.append(c[1])
            elif et == 'LWPOLYLINE':
                pts = list(e.get_points('xy'))
                for p in pts: xs.append(p[0]); ys.append(p[1])
            elif et == 'POLYLINE':
                for v in e.vertices:
                    xs.append(v.dxf.location.x); ys.append(v.dxf.location.y)
        if xs and ys:
            result['bbox_mm'] = [min(xs), min(ys), max(xs), max(ys)]
    except Exception as be:
        logger.debug('bbox calc failed: %s', be)
    _bbox_esatto(dst_ms, result)

    result['success'] = True
    return result


def _bbox_esatto(layout, result: dict) -> None:
    """bbox_mm (unità disegno) con ezdxf.bbox: include SPLINE/ELLIPSE e archi
    parziali reali (il loop sugli endpoint li ignorava o li sovrastimava)."""
    try:
        from ezdxf import bbox as _bbox
        bb = _bbox.extents(layout)
        if bb.has_data:
            result['bbox_mm'] = [bb.extmin.x, bb.extmin.y, bb.extmax.x, bb.extmax.y]
    except Exception as e:
        logger.debug('bbox esatto fallito: %s', e)


def _copia_unita(src, dst) -> None:
    """Copia $INSUNITS/$MEASUREMENT dal sorgente: ezdxf.new() dichiara METRI per
    default, e il DXF pulito (in mm) verrebbe riletto 1000× più grande."""
    for var, default in (('$INSUNITS', 4), ('$MEASUREMENT', 1)):
        try:
            dst.header[var] = src.header.get(var, default)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════
# Pulizia AUTOMATICA guidata dalla geometria del detector v3
#
# BUG FIX (pulizia errata all'import): l'auto-cleanup passava il bbox del
# pezzo a `save_cleaned_dxf`, pensata per il drag manuale: cluster per
# prossimità + vincitore scelto con score "cerchi×10". Risultato su DXF reali
# con cornice/cartiglio: vinceva la cornice (che "tocca" il bbox perché lo
# contiene → il DXF pulito era l'intero foglio 288×196, 576×392…) oppure un
# singolo foro/svasatura (2 cerchi concentrici = 20 punti battono un contorno
# fatto di LINE). Qui si usa invece il contorno ESATTO scelto dal detector:
# si copiano solo le entità che giacciono sul contorno esterno o sui fori
# tagliati del pezzo, poi si verifica che l'estensione del file pulito
# coincida con le misure del detector (altrimenti il file NON viene scritto).
# ═══════════════════════════════════════════════════════════════════

# Marca nell'header del DXF pulito ($USERI1..2 / $USERR1..2, presenti in
# tutte le versioni DXF): distingue i file generati dalla pulizia corretta
# da quelli "legacy" (possibilmente errati) rimasti sul disco.
MARCA_PULIZIA_VERSIONE = 2
# $USERI3: formato del pulito per Lantek (tre layer, mm veri). I puliti v2 senza
# questo segno NON si rigenerano all'apertura del preventivo (decine di secondi
# sui preventivi grandi): si rifanno quando vanno al laser.
FORMATO_LANTEK = 1
TIPO_PULIZIA_AUTO = 1
TIPO_PULIZIA_MANUALE = 2
# Scarto massimo ammesso fra estensione del pulito e misure del detector
TOL_VERIFICA_MM = 2.0
TOL_VERIFICA_REL = 0.005


def _marca_pulito(dst, tipo: int, w_mm: float = 0.0, h_mm: float = 0.0) -> None:
    try:
        dst.header['$USERI1'] = MARCA_PULIZIA_VERSIONE
        dst.header['$USERI2'] = int(tipo)
        dst.header['$USERR1'] = float(w_mm or 0.0)
        dst.header['$USERR2'] = float(h_mm or 0.0)
    except Exception as e:
        logger.debug('marca DXF pulito fallita: %s', e)


def _nuovo_documento(src):
    """Documento vuoto con stessa versione, layer (colori) e unità del sorgente."""
    try:
        dst = ezdxf.new(dxfversion=src.dxfversion, setup=False)
    except Exception:
        dst = ezdxf.new(setup=False)
    for lname in {l.dxf.name for l in src.layers}:
        if lname in dst.layers:
            continue
        try:
            new_layer = dst.layers.add(lname)
            try:
                new_layer.dxf.color = src.layers.get(lname).dxf.color
            except Exception:
                pass
        except Exception:
            pass
    _copia_unita(src, dst)
    return dst


def _estensione_mm(doc, scala: float | None = None) -> tuple[float, float] | None:
    """(larghezza, altezza) in mm del modelspace (blocchi inclusi, via ezdxf.bbox)."""
    try:
        from ezdxf import bbox as _bbox
        if scala is None:
            from .dxf_polygon_detector_v3 import scala_unita_mm
            scala = float(scala_unita_mm(doc)[0])
        bb = _bbox.extents(doc.modelspace())
        if not bb.has_data:
            return None
        return ((bb.extmax.x - bb.extmin.x) * scala, (bb.extmax.y - bb.extmin.y) * scala)
    except Exception as e:
        logger.debug('estensione DXF fallita: %s', e)
        return None


def _misure_coincidono(a: Sequence[float], b: Sequence[float],
                       tol_mm: float = TOL_VERIFICA_MM, tol_rel: float = TOL_VERIFICA_REL,
                       ruotato: bool = False) -> bool:
    """True se (w, h) di `a` e `b` coincidono entro max(tol_mm, tol_rel·lato).
    ruotato=True ammette anche W/H scambiati (misure confermate a mano)."""
    def _ok(x, y):
        return all(abs(p - q) <= max(tol_mm, tol_rel * max(p, q)) for p, q in zip(x, y))
    a2, b2 = (float(a[0]), float(a[1])), (float(b[0]), float(b[1]))
    return _ok(a2, b2) or (ruotato and _ok(a2, (b2[1], b2[0])))


def _contorni_pezzo(doc, geo: dict, cfg: dict):
    """(outer, fori_tagliati, scala) in mm del pezzo scelto dal detector, o None.

    Rifà la stessa pipeline di detect_pezzo_geometry_v3 (stessa config) e
    prende il candidato `selected_candidate_idx`; per sicurezza lo riconosce
    anche dal bbox (i candidati del risultato sono in unità disegno)."""
    from .dxf_polygon_detector_v3 import (
        _poligoni_documento, _separa_cartiglio, _riduci_fori_annidati,
        _contiene, _prep_buf, _estendi_oltre_pieghe, applica_intagli,
    )
    base = _poligoni_documento(doc, cfg)
    scala = float(base['scala'] or 1.0)
    candidati, _n = _separa_cartiglio(base['polys'], base.get('testi'))
    if not candidati:
        return None
    w_att = float(geo.get('bbox_width_mm') or 0)
    h_att = float(geo.get('bbox_height_mm') or 0)

    def _dim(p):
        x1, y1, x2, y2 = p.bounds
        return (x2 - x1, y2 - y1)

    outer = None
    sel = geo.get('selected_candidate_idx')
    if isinstance(sel, int) and 0 <= sel < len(candidati):
        if _misure_coincidono(_dim(candidati[sel]), (w_att, h_att), 0.05, 0.0):
            outer = candidati[sel]
        else:
            # Il detector puo' aver unito le falde di uno sviluppo o tolto gli
            # scantonati d'angolo: stessi passi, poi stesso controllo misure.
            scelto = candidati[sel]
            est, piena, _nf = _estendi_oltre_pieghe(scelto, doc.modelspace(), cfg)
            pp = _prep_buf(est)
            inners = [p for p in candidati if p is not scelto and _contiene(est, p, pp)]
            if piena is not None:
                inners = [p for p in inners if piena.intersection(p).area < 0.5 * p.area]
            inners, _n_svas = _riduci_fori_annidati(inners, est)
            est, inners, _ni = applica_intagli(est, inners)
            if _misure_coincidono(_dim(est), (w_att, h_att), 0.05, 0.0):
                return est, inners, scala
    if outer is None:
        # Riconoscimento dal bbox del candidato selezionato (unità disegno → mm)
        bb = get_pezzo_bbox(geo)
        if bb:
            bb_mm = [float(v) * scala for v in bb]
            tol = max(0.05, 0.001 * max(w_att, h_att))
            for p in candidati:
                if all(abs(a - b) <= tol for a, b in zip(p.bounds, bb_mm)):
                    outer = p
                    break
    if outer is None:
        return None
    pp = _prep_buf(outer)
    inners = [p for p in candidati if p is not outer and _contiene(outer, p, pp)]
    inners, _n_svas = _riduci_fori_annidati(inners, outer)
    return outer, inners, scala


# ═══════════════════════════════════════════════════════════════════
# DXF pulito per Lantek: tre layer, sempre in millimetri veri
#
#   TAGLIO     contorno esterno e fori del pezzo, e nient'altro
#   PIEGA      linee di piega (per tracciarle quando serve: pieghe a gradi)
#   MARCATURA  il resto della geometria DENTRO il pezzo (codici incisi,
#              loghi): Stefano decide in Lantek se marcarla o no
#
# Fuori dal file: tutto cio' che sta fuori dal pezzo (altre viste, cartiglio,
# quote), assi dei fori e simboli di filettatura/svasatura (l'arco di 3/4
# attorno al foro: prima finiva nel pulito perche' a 0,4 mm dal foro, e
# Lantek l'avrebbe tagliato).
# ═══════════════════════════════════════════════════════════════════
LAYER_TAGLIO = 'TAGLIO'
LAYER_PIEGA = 'PIEGA'
LAYER_MARCATURA = 'MARCATURA'
_LAYER_LANTEK = ((LAYER_TAGLIO, 7), (LAYER_PIEGA, 2), (LAYER_MARCATURA, 3))
_LINEE_ASSI = ('center', 'centr', 'dashdot', 'axis', 'asse', 'phantom', 'divide')


def _tipo_linea(entity, doc) -> str:
    """Tipo di linea effettivo (anche BYLAYER), minuscolo."""
    try:
        lt = entity.dxf.get('linetype', 'BYLAYER') or 'BYLAYER'
        if lt.upper() == 'BYLAYER':
            lt = doc.layers.get(entity.dxf.layer).dxf.get('linetype', '') or ''
        return lt.lower()
    except Exception:
        return ''


def scrivi_pulito_lantek(src, outer, fori: list, scala: float, cleaned_path: str,
                         tipo: int, w_att: float, h_att: float, cfg: dict | None = None) -> dict:
    """Scrive il DXF pulito del pezzo `outer` (con i fori `fori`, poligoni in mm)
    copiando le entita' VERE del disegno `src` (archi e cerchi restano archi e
    cerchi) sui tre layer, scalate in mm. Verifica prima di scrivere: ingombro
    del TAGLIO = pezzo e contorno coperto per almeno il 90%.

    Returns: {'success', 'error', 'entities_copied', 'entities_source',
              'n_taglio', 'n_piega', 'n_marcatura', 'n_simboli_tolti',
              'copertura', 'w_mm', 'h_mm', 'bbox_mm' (unita' disegno),
              'tolerance_mm', 'warnings'}"""
    from shapely.geometry import LineString, Point, Polygon
    from shapely.ops import unary_union
    from shapely.prepared import prep
    from ezdxf.math import Matrix44
    from .dxf_polygon_detector_v3 import (
        TIPI_ANNOTAZIONE, _layer_da_escludere, _layer_piega, _flatten_entity,
        FLATTEN_DISTANCE_MM, colore_effettivo, linea_tratteggiata,
    )
    cfg = cfg or {}
    result = {'success': False, 'error': None, 'entities_copied': 0, 'entities_source': 0,
              'entities_skipped_meta': 0, 'n_taglio': 0, 'n_piega': 0, 'n_marcatura': 0,
              'n_simboli_tolti': 0, 'copertura': 0.0, 'w_mm': None, 'h_mm': None,
              'bbox_mm': None, 'tolerance_mm': 0.0, 'warnings': []}
    colori_piega = set(cfg.get('dxf_colori_piega', [2]))
    anelli = [outer.exterior] + list(outer.interiors) + [f.exterior for f in fori]
    bordi = unary_union(anelli)
    lato = max(w_att, h_att, 1.0)
    # sul contorno: stretta (l'entita' e' quella da cui nasce il contorno);
    # "tocca il bordo" per le pieghe: piu' larga
    tol_taglio = max(0.1, min(0.25, 0.0005 * lato))
    tol_bordo = max(0.5, min(2.0, 0.003 * lato))
    result['tolerance_mm'] = round(tol_taglio, 3)
    sul_contorno = prep(bordi.buffer(tol_taglio))
    vicino_bordo = prep(bordi.buffer(tol_bordo))
    dentro = prep(Polygon(outer.exterior).buffer(tol_bordo))
    centri_fori = []
    for f in fori:
        c = f.centroid
        fw, fh = f.bounds[2] - f.bounds[0], f.bounds[3] - f.bounds[1]
        # raggio solo per i fori tondi (le asole non hanno un centro unico)
        r_eq = (f.area / 3.141592653589793) ** 0.5 if fh > 0 and abs(fw / fh - 1) < 0.02 else None
        centri_fori.append((c.x, c.y, max(0.05, 0.02 * fw), r_eq))
    dist = FLATTEN_DISTANCE_MM / (scala or 1.0)

    dst = _nuovo_documento(src)
    for nome, col in _LAYER_LANTEK:
        try:
            if nome in dst.layers:
                dst.layers.get(nome).dxf.color = col
            else:
                dst.layers.add(nome, color=col)
        except Exception:
            pass
    try:
        dst.header['$INSUNITS'] = 4          # sempre millimetri veri
        dst.header['$MEASUREMENT'] = 1
    except Exception:
        pass
    dst_ms = dst.modelspace()
    scala_m = Matrix44.scale(scala, scala, scala) if abs(scala - 1.0) > 1e-9 else None
    firme: set = set()
    lung_taglio = 0.0

    def _copia(e, layer):
        c = e.copy()
        if scala_m is not None:
            c.transform(scala_m)
        c.dxf.layer = layer
        # colore e tratto dal layer: il colore "fisso" (true color) esportato da
        # SolidWorks scavalcava quello del layer e confondeva TAGLIO e MARCATURA
        for att in ('true_color', 'color_name', 'transparency'):
            try:
                c.dxf.discard(att)
            except Exception:
                pass
        try:
            # colore SCRITTO sull'entita' (non "da layer"): Lantek non leggeva
            # i layer, e molti CAM vedono BYLAYER come colore 0
            c.dxf.color = dict(_LAYER_LANTEK).get(layer, 7)
            c.dxf.linetype = 'Continuous'
            c.dxf.lineweight = -1
        except Exception:
            pass
        dst_ms.add_entity(c)

    def _classifica(e, parte=False):
        nonlocal lung_taglio
        et = e.dxftype()
        if et in _SKIP_TYPES or et in TIPI_ANNOTAZIONE or et in ('SOLID', 'TRACE', 'POINT'):
            result['entities_skipped_meta'] += 1     # SOLID/TRACE: frecce delle quote
            return
        verts = _flatten_entity(e, dist)
        if not verts or len(verts) < 2:
            return
        pts = [(x * scala, y * scala) for x, y in verts]
        try:
            ls = LineString(pts)
            if ls.length <= 0 or not dentro.contains(ls):
                return                        # fuori dal pezzo: altre viste, cartiglio
        except Exception:
            return
        layer = getattr(e.dxf, 'layer', '') or ''
        try:
            col = colore_effettivo(e)
        except Exception:
            col = None
        piega_dichiarata = _layer_piega(layer) or col in colori_piega
        escluso = _layer_da_escludere(layer)
        if et in ('ARC', 'CIRCLE'):
            try:
                ex, ey, er = e.dxf.center.x * scala, e.dxf.center.y * scala, e.dxf.radius * scala
                for cx, cy, t, r_eq in centri_fori:
                    if (r_eq and abs(ex - cx) <= t and abs(ey - cy) <= t
                            and abs(er - r_eq) > max(0.05, 0.02 * r_eq)):
                        result['n_simboli_tolti'] += 1   # concentrico ma non e' il foro: filetto, svasatura
                        return
            except Exception:
                pass
        # una linea a tratti sul contorno (spigolo nascosto, ingombro) non e' il
        # taglio: il detector costruisce il contorno solo dalle linee continue
        su_taglio = (not piega_dichiarata and not escluso and sul_contorno.contains(ls)
                     and not linea_tratteggiata(e))
        if not su_taglio and not parte and et in ('LWPOLYLINE', 'POLYLINE'):
            # polilinea che segue in parte il contorno e in parte una piega
            # (sviluppi a falde unite, 191700606-00): si divide nei suoi tratti
            try:
                parti = list(e.virtual_entities())
            except Exception:
                parti = []
            if len(parti) > 1:
                for v in parti:
                    _classifica(v, True)
                return
        firma = (et, tuple(sorted((round(x, 2), round(y, 2)) for x, y in (pts[0], pts[-1], pts[len(pts) // 2]))),
                 round(ls.length, 2))
        if firma in firme:
            return                            # stesso tratto due volte: Lantek lo taglierebbe due volte
        try:
            if su_taglio:
                _copia(e, LAYER_TAGLIO)
                result['n_taglio'] += 1
                lung_taglio += ls.length
            elif any(a in _tipo_linea(e, src) for a in _LINEE_ASSI):
                return                        # assi dei fori
            elif et in ('ARC', 'CIRCLE') and any(
                    abs(e.dxf.center.x * scala - cx) <= t and abs(e.dxf.center.y * scala - cy) <= t
                    for cx, cy, t, _r in centri_fori):
                result['n_simboli_tolti'] += 1    # filettatura / svasatura attorno a un foro
                return
            elif piega_dichiarata or (et == 'LINE' and ls.length > 2 * tol_bordo
                                      and vicino_bordo.contains(Point(pts[0]))
                                      and vicino_bordo.contains(Point(pts[-1]))):
                _copia(e, LAYER_PIEGA)
                result['n_piega'] += 1
            elif escluso:
                result['entities_skipped_meta'] += 1
                return
            else:
                _copia(e, LAYER_MARCATURA)
                result['n_marcatura'] += 1
        except Exception as ex:
            logger.debug('copia entita %s fallita: %s', et, ex)
            return
        firme.add(firma)
        result['entities_copied'] += 1

    for e in _entita_sorgente(src.modelspace()):
        result['entities_source'] += 1
        _classifica(e)

    if result['n_taglio'] == 0:
        result['error'] = 'nessuna entità del DXF giace sul contorno del pezzo'
        return result
    try:
        from ezdxf import bbox as _bbox
        bb = _bbox.extents(dst_ms.query(f'*[layer=="{LAYER_TAGLIO}"]'))
        est = (bb.extmax.x - bb.extmin.x, bb.extmax.y - bb.extmin.y)
        s = scala or 1.0
        result['bbox_mm'] = [bb.extmin.x / s, bb.extmin.y / s, bb.extmax.x / s, bb.extmax.y / s]
    except Exception:
        result['error'] = 'estensione del DXF pulito non calcolabile'
        return result
    result['w_mm'], result['h_mm'] = round(est[0], 2), round(est[1], 2)
    if not _misure_coincidono(est, (w_att, h_att)):
        result['error'] = (f'verifica fallita: pulito {est[0]:.1f}×{est[1]:.1f} mm '
                           f'≠ pezzo {w_att:.1f}×{h_att:.1f} mm')
        return result
    perim_atteso = sum(a.length for a in anelli)
    result['copertura'] = round(lung_taglio / perim_atteso, 4) if perim_atteso > 0 else 0.0
    if perim_atteso > 0 and lung_taglio < 0.9 * perim_atteso:
        result['error'] = (f'verifica fallita: contorno copiato {lung_taglio:.0f} mm '
                           f'su {perim_atteso:.0f} mm attesi')
        return result
    if lung_taglio > 1.02 * perim_atteso:
        result['warnings'].append(f'sul taglio {lung_taglio:.0f} mm di linee su {perim_atteso:.0f} mm di contorno')

    _marca_pulito(dst, tipo, w_att, h_att)
    try:
        dst.header['$USERI3'] = FORMATO_LANTEK
    except Exception:
        pass
    tmp_path = cleaned_path + '.tmp'
    try:
        os.makedirs(os.path.dirname(cleaned_path) or '.', exist_ok=True)
        dst.saveas(tmp_path)
        os.replace(tmp_path, cleaned_path)
    except Exception as e:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        result['error'] = f'Scrittura fallita: {e}'
        return result
    _CACHE_VERIFICA.pop(cleaned_path, None)
    result['success'] = True
    return result


def pulito_da_contorno(source_path: str, cleaned_path: str, outer_xy: list,
                       holes_xy: list, config: dict | None = None) -> dict:
    """Pulito per Lantek dal contorno confermato a mano nel CAD (punti in mm,
    come li da' pick_part). Prima la conferma lasciava solo un "_canonico" a
    spezzate (fori a poligono, nome per posizione nell'elenco): qui si copiano
    le entita' vere del disegno, come nel pulito automatico."""
    try:
        from shapely.geometry import Polygon
        from .dxf_polygon_detector_v3 import scala_unita_mm
        src = ezdxf.readfile(source_path)
        outer = Polygon([(float(p[0]), float(p[1])) for p in outer_xy]).buffer(0)
        fori = []
        for h in holes_xy or []:
            if h and len(h) >= 3:
                fp = Polygon([(float(p[0]), float(p[1])) for p in h]).buffer(0)
                if not fp.is_empty and fp.geom_type == 'Polygon':
                    fori.append(fp)
        if outer.is_empty or outer.geom_type != 'Polygon':
            return {'success': False, 'error': 'contorno non valido'}
        scala = float(scala_unita_mm(src)[0] or 1.0)
    except Exception as e:
        return {'success': False, 'error': f'DXF o contorno non leggibile: {e}'}
    x1, y1, x2, y2 = outer.bounds
    return scrivi_pulito_lantek(src, outer, fori, scala, cleaned_path, TIPO_PULIZIA_MANUALE,
                                x2 - x1, y2 - y1, config or {})


def converti_pulito_in_lantek(source_path: str, cleaned_path: str,
                              config: dict | None = None) -> dict:
    """Riscrive nel formato Lantek un pulito fatto col riquadro o col clic
    (save_cleaned_dxf / save_cleaned_dxf_by_click): contorno = il piu' grande
    contorno chiuso del pulito, fori = quelli dentro; le entita' si ricopiano
    dall'ORIGINALE con la sua scala (quote DIMLFAC comprese: prima il pulito
    di un disegno in scala veniva riletto con le misure del foglio).
    Se non riesce il pulito di prima resta com'e' (success False).

    Returns come scrivi_pulito_lantek, con 'bbox_mm_mm' = ingombro in mm."""
    try:
        from shapely import affinity
        from .dxf_polygon_detector_v3 import (
            _poligoni_documento, _riduci_fori_annidati, _contiene, _prep_buf, scala_unita_mm,
        )
        if leggi_dxf_pulito(cleaned_path).get('lantek'):
            return {'success': False, 'error': 'gia\' nel formato Lantek'}
        src = ezdxf.readfile(source_path)
        cl = ezdxf.readfile(cleaned_path)
        base = _poligoni_documento(cl, config or {})
        polys = sorted(base['polys'], key=lambda p: -p.area)
        if not polys:
            return {'success': False, 'error': 'nessun contorno chiuso nel pulito'}
        outer = polys[0]
        pp = _prep_buf(outer)
        fori, _n = _riduci_fori_annidati([p for p in polys[1:] if _contiene(outer, p, pp)], outer)
        s_src = float(scala_unita_mm(src)[0] or 1.0)
        s_cl = float(base['scala'] or 1.0)
        f = s_src / s_cl
        if abs(f - 1.0) > 1e-9:
            outer = affinity.scale(outer, f, f, origin=(0, 0))
            fori = [affinity.scale(p, f, f, origin=(0, 0)) for p in fori]
    except Exception as e:
        return {'success': False, 'error': f'pulito non convertibile: {e}'}
    x1, y1, x2, y2 = outer.bounds
    tmp = cleaned_path + '.lantek.dxf'
    r = scrivi_pulito_lantek(src, outer, fori, s_src, tmp, TIPO_PULIZIA_MANUALE,
                             x2 - x1, y2 - y1, config or {})
    if not r.get('success'):
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return r
    os.replace(tmp, cleaned_path)
    _CACHE_VERIFICA.pop(cleaned_path, None)
    if r.get('bbox_mm'):
        r['bbox_mm_mm'] = [v * s_src for v in r['bbox_mm']]
    return r


def verifica_lantek(cleaned_path: str, articolo: dict | None = None) -> dict:
    """Il pulito puo' andare a Lantek cosi' com'e'?

    'pronto' se: formato Lantek; sul TAGLIO un solo contorno esterno con i fori
    dentro (niente altre viste); ingombro e area uguali a quelli del pezzo
    preventivato (area saltata se e' uno sviluppo stimato a mano).
    Altrimenti 'da_guardare' coi motivi: lo prepara Stefano in Lantek.

    Returns: {'stato': 'pronto'|'da_guardare', 'motivi': [str]}"""
    a = articolo or {}
    motivi = []
    info = leggi_dxf_pulito(cleaned_path)
    if info.get('errore'):
        return {'stato': 'da_guardare', 'motivi': [info['errore']]}
    if not info.get('lantek'):
        return {'stato': 'da_guardare', 'motivi': ['pulito nel vecchio formato']}
    try:
        from .dxf_polygon_detector_v3 import _poligoni_documento, _contiene, _prep_buf
        doc = ezdxf.readfile(cleaned_path)
        polys = sorted(_poligoni_documento(doc, {})['polys'], key=lambda p: -p.area)
    except Exception as e:
        return {'stato': 'da_guardare', 'motivi': [f'pulito non leggibile: {e}']}
    if not polys:
        return {'stato': 'da_guardare', 'motivi': ['nessun contorno chiuso sul taglio']}
    outer = polys[0]
    pp = _prep_buf(outer)
    fuori = [p for p in polys[1:] if not _contiene(outer, p, pp)]
    if fuori:
        motivi.append(f'{len(fuori)} contorni fuori dal pezzo')
    x1, y1, x2, y2 = outer.bounds
    try:
        rw, rh = float(a.get('bbox_w_mm') or 0), float(a.get('bbox_h_mm') or 0)
    except (TypeError, ValueError):
        rw = rh = 0.0
    if rw > 0 and rh > 0 and not _misure_coincidono((x2 - x1, y2 - y1), (rw, rh), 1.0, 0.01, ruotato=True):
        motivi.append(f'ingombro {x2 - x1:.0f}×{y2 - y1:.0f} mm diverso dal pezzo {rw:.0f}×{rh:.0f} mm')
    try:
        area = float(a.get('area_dm2') or 0)
    except (TypeError, ValueError):
        area = 0.0
    if area > 0 and not a.get('area_stimata_piega'):
        netta = (outer.area - sum(p.area for p in polys[1:] if p not in fuori)) / 1e4
        if abs(netta / area - 1) > 0.03:
            motivi.append(f'area {netta:.3f} dm² diversa dal preventivo {area:.3f} dm²')
    return {'stato': 'da_guardare' if motivi else 'pronto', 'motivi': motivi}


def prepara_pulito_lantek(original_path: str, cleaned_path: str | None, articolo: dict,
                          config: dict | None = None) -> dict:
    """All'accettazione: porta il pulito del pezzo nel formato Lantek se e'
    ancora nel vecchio (automatico: rigenerato dall'originale; a mano:
    convertito) e lo verifica. Non tocca l'originale.

    Returns: {'stato', 'motivi', 'path'}"""
    if (not cleaned_path or not os.path.exists(cleaned_path)) and original_path and os.path.exists(original_path):
        # confermato a mano nel CAD prima del formato Lantek: il contorno e'
        # nel "_canonico" (spezzate in mm), le entita' vere nell'originale
        can = articolo.get('canonical_dxf_filename')
        can_path = os.path.join(os.path.dirname(original_path), os.path.basename(can)) if can else None
        if can_path and os.path.exists(can_path):
            try:
                cd = ezdxf.readfile(can_path)
                outer = [[(p[0], p[1]) for p in e.get_points('xy')] for e in cd.modelspace()
                         if e.dxftype() == 'LWPOLYLINE' and e.dxf.layer == 'PEZZO']
                holes = [[(p[0], p[1]) for p in e.get_points('xy')] for e in cd.modelspace()
                         if e.dxftype() == 'LWPOLYLINE' and e.dxf.layer == 'FORI']
                if outer:
                    base_p, ext_p = os.path.splitext(original_path)
                    nuovo = base_p + '_cleaned' + ext_p
                    if pulito_da_contorno(original_path, nuovo, outer[0], holes, config).get('success'):
                        cleaned_path = nuovo
            except Exception as e:
                logger.info('pulito dal canonico non creato: %s', e)
    if (not cleaned_path or not os.path.exists(cleaned_path)) and original_path and os.path.exists(original_path):
        # disegno che contiene SOLO il pezzo (niente cornice ne' cartiglio): la
        # regola "pezzo > 90% del foglio = cartiglio incluso" lo scartava. Senza
        # scritte ne' quote non c'e' cartiglio da confondere col pezzo.
        try:
            from .dxf_polygon_detector_v3 import detect_pezzo_geometry_v3
            src = ezdxf.readfile(original_path)
            annotazioni = any(True for _ in src.modelspace().query('TEXT MTEXT DIMENSION INSERT'))
            if not annotazioni:
                geo = detect_pezzo_geometry_v3(original_path, config or {})
                ok, motivo = should_cleanup(geo)
                if ok or motivo.startswith('area_ratio'):
                    base_p, ext_p = os.path.splitext(original_path)
                    nuovo = base_p + '_cleaned' + ext_p
                    if save_cleaned_dxf_pezzo(original_path, nuovo, geo, config).get('success'):
                        cleaned_path = nuovo
        except Exception as e:
            logger.info('pulito del disegno senza cartiglio non creato: %s', e)
    if not cleaned_path or not os.path.exists(cleaned_path):
        return {'stato': 'da_guardare', 'motivi': ['nessun pulito: pezzo da preparare in Lantek'], 'path': None}
    if not leggi_dxf_pulito(cleaned_path).get('lantek') and original_path and os.path.exists(original_path):
        stato = (articolo.get('cleaned_status') or '').lower()
        fatto = False
        if stato != 'manual':
            tmp = cleaned_path + '.nuovo.dxf'
            r = rigenera_pulito_auto(original_path, tmp, config)
            if r.get('success'):
                os.replace(tmp, cleaned_path)
                _CACHE_VERIFICA.pop(cleaned_path, None)
                fatto = True
            elif os.path.exists(tmp):
                os.remove(tmp)
        if not fatto:
            # a mano, o automatico che oggi il riconoscimento non rifa' da solo:
            # la selezione e' quella del pulito vecchio
            converti_pulito_in_lantek(original_path, cleaned_path, config)
    v = verifica_lantek(cleaned_path, articolo)
    v['path'] = cleaned_path
    return v


def save_cleaned_dxf_pezzo(
    source_path: str,
    cleaned_path: str,
    detector_result: dict,
    config: dict | None = None,
) -> dict:
    """DXF pulito = SOLO contorno esterno + fori tagliati del pezzo del detector.

    Un'entità (anche dentro un blocco) viene copiata se TUTTI i suoi punti
    stanno a ≤ tol dal contorno esterno o dal bordo di un foro tagliato del
    pezzo. Restano fuori cornice, cartiglio, quote, assi, viste secondarie e
    lo smusso delle svasature (il laser taglia solo il passante, come nel
    calcolo del detector).

    Verifica prima di scrivere: estensione del pulito = bbox del detector
    (entro max(2 mm, 0.5%)) e perimetro coperto ≥ 90%. Se la verifica fallisce
    il file NON viene scritto (success=False, error con il motivo).

    Returns: {'success', 'entities_copied', 'entities_source', 'bbox_mm'
              (unità disegno), 'w_mm', 'h_mm', 'tolerance_mm', 'error', 'warnings'}
    """
    result = {
        'success': False, 'entities_copied': 0, 'entities_source': 0,
        'entities_skipped_meta': 0, 'tolerance_mm': 0.0, 'bbox_mm': None,
        'w_mm': None, 'h_mm': None, 'error': None, 'warnings': [],
    }
    geo = detector_result or {}
    cfg = config or {}
    try:
        from shapely.geometry import LineString
        from shapely.ops import unary_union
        from shapely.prepared import prep
        from .dxf_polygon_detector_v3 import (
            TIPI_ANNOTAZIONE, _layer_da_escludere, _flatten_entity, FLATTEN_DISTANCE_MM,
        )
    except Exception as e:
        result['error'] = f'shapely/detector non disponibili: {e}'
        return result
    try:
        src = ezdxf.readfile(source_path)
    except Exception as e:
        result['error'] = f'DXF non leggibile: {e}'
        return result

    try:
        contorni = _contorni_pezzo(src, geo, cfg)
    except Exception as e:
        logger.warning('pulizia pezzo: contorni non ricostruiti: %s', e)
        contorni = None
    if not contorni:
        result['error'] = 'contorno del pezzo non ritrovato nel DXF (detector incoerente)'
        return result
    outer, fori, scala = contorni
    w_att = float(geo.get('bbox_width_mm') or 0) or (outer.bounds[2] - outer.bounds[0])
    h_att = float(geo.get('bbox_height_mm') or 0) or (outer.bounds[3] - outer.bounds[1])
    r = scrivi_pulito_lantek(src, outer, fori, scala, cleaned_path, TIPO_PULIZIA_AUTO, w_att, h_att, cfg)
    result.update({k: v for k, v in r.items() if k in result or k.startswith('n_') or k == 'copertura'})
    return result


# Cache letture DXF puliti: path → (mtime, info). Evita di rileggere il file a
# ogni apertura del preventivo.
_CACHE_VERIFICA: dict = {}
# Rigenerazioni già tentate (path, mtime): una sola volta per file legacy
_RIGENERAZIONI_TENTATE: set = set()


def leggi_dxf_pulito(cleaned_path: str) -> dict:
    """Info sul DXF pulito: {w_mm, h_mm, marcato, tipo, w_att, h_att, errore}.
    `marcato` = generato dalla pulizia corretta (versione ≥ MARCA_PULIZIA_VERSIONE)."""
    try:
        mtime = os.path.getmtime(cleaned_path)
    except OSError:
        return {'errore': 'file mancante'}
    hit = _CACHE_VERIFICA.get(cleaned_path)
    if hit and hit[0] == mtime:
        return hit[1]
    info = {'w_mm': None, 'h_mm': None, 'marcato': False, 'tipo': None,
            'w_att': None, 'h_att': None, 'errore': None, 'lantek': False}
    try:
        doc = ezdxf.readfile(cleaned_path)
        est = _estensione_mm(doc)
        if est:
            info['w_mm'], info['h_mm'] = round(est[0], 2), round(est[1], 2)
        else:
            info['errore'] = 'DXF pulito vuoto'
        h = doc.header
        info['lantek'] = int(h.get('$USERI3', 0) or 0) >= FORMATO_LANTEK
        if int(h.get('$USERI1', 0) or 0) >= MARCA_PULIZIA_VERSIONE:
            info['marcato'] = True
            info['tipo'] = int(h.get('$USERI2', 0) or 0)
            info['w_att'] = float(h.get('$USERR1', 0) or 0) or None
            info['h_att'] = float(h.get('$USERR2', 0) or 0) or None
    except Exception as e:
        info['errore'] = f'DXF pulito non leggibile: {e}'
    if len(_CACHE_VERIFICA) > 2000:
        _CACHE_VERIFICA.clear()
    _CACHE_VERIFICA[cleaned_path] = (mtime, info)
    return info


def _plausibile_con_area(w_mm: float, h_mm: float, area_dm2) -> bool:
    """Estensione compatibile con l'area netta del pezzo: il bbox deve contenere
    l'area (niente foro 18×18 per una piastra 170×100) e non essere enorme
    rispetto ad essa (niente cornice del foglio per un pezzetto 30×30)."""
    try:
        area = float(area_dm2 or 0)
    except (TypeError, ValueError):
        area = 0.0
    if area <= 0:
        return False
    bbox_dm2 = (w_mm * h_mm) / 10000.0
    return bbox_dm2 >= area * 0.98 and area >= 0.10 * bbox_dm2


def rigenera_pulito_auto(original_path: str, cleaned_path: str,
                         config: dict | None = None) -> dict:
    """Rigenera il DXF pulito automatico dall'originale (detector + pulizia
    per contorno). Non tocca l'originale. Ritorna l'esito di
    save_cleaned_dxf_pezzo, oppure {'success': False, 'error': motivo}."""
    try:
        from .dxf_polygon_detector_v3 import detect_pezzo_geometry_v3
        geo = detect_pezzo_geometry_v3(original_path, config or {})
    except Exception as e:
        return {'success': False, 'error': f'detector fallito: {e}'}
    ok, motivo = should_cleanup(geo)
    if not ok:
        return {'success': False, 'error': f'pulizia saltata: {motivo}'}
    return save_cleaned_dxf_pezzo(original_path, cleaned_path, geo, config)


def verifica_pulito_articolo(cleaned_path: str, articolo: dict,
                             original_path: str | None = None,
                             config: dict | None = None,
                             rigenera: bool = True) -> dict:
    """Decide se il DXF pulito di un articolo è affidabile (per mostrarlo e per
    ricavarne le dimensioni). Se è un file LEGACY automatico (senza marca,
    generato dalla vecchia pulizia che poteva essere errata) prova UNA volta a
    rigenerarlo dall'originale.

    Returns: {'affidabile': bool, 'w_mm', 'h_mm', 'rigenerato': bool, 'motivo'}
    """
    out = {'affidabile': False, 'w_mm': None, 'h_mm': None,
           'rigenerato': False, 'motivo': None}
    info = leggi_dxf_pulito(cleaned_path)
    stato = (articolo.get('cleaned_status') or '').lower()
    if (not info.get('marcato') and stato != 'manual' and rigenera and original_path
            and os.path.exists(original_path)):
        chiave = (cleaned_path, os.path.getmtime(cleaned_path) if os.path.exists(cleaned_path) else 0)
        if chiave not in _RIGENERAZIONI_TENTATE:
            _RIGENERAZIONI_TENTATE.add(chiave)
            r = rigenera_pulito_auto(original_path, cleaned_path, config)
            if r.get('success'):
                out['rigenerato'] = True
                logger.info('DXF pulito legacy rigenerato: %s (%s×%s mm)',
                            os.path.basename(cleaned_path), r.get('w_mm'), r.get('h_mm'))
                info = leggi_dxf_pulito(cleaned_path)
            else:
                logger.info('DXF pulito legacy non rigenerato (%s): %s',
                            os.path.basename(cleaned_path), r.get('error'))
    if info.get('errore') or not info.get('w_mm') or not info.get('h_mm'):
        out['motivo'] = info.get('errore') or 'estensione non calcolabile'
        return out
    w, h = info['w_mm'], info['h_mm']
    out['w_mm'], out['h_mm'] = w, h

    # Misure già confermate sull'articolo (CAD/import): il pulito deve coincidere
    try:
        rw = float(articolo.get('bbox_w_mm') or 0)
        rh = float(articolo.get('bbox_h_mm') or 0)
    except (TypeError, ValueError):
        rw = rh = 0.0
    if rw > 0 and rh > 0:
        if _misure_coincidono((w, h), (rw, rh), TOL_VERIFICA_MM, 0.05, ruotato=True):
            out['affidabile'] = True
        else:
            out['motivo'] = (f'pulito {w:.0f}×{h:.0f} mm diverso dalle misure '
                             f'dell\'articolo {rw:.0f}×{rh:.0f} mm')
        return out
    if info.get('marcato'):
        if info.get('w_att') and info.get('h_att') and not _misure_coincidono(
                (w, h), (info['w_att'], info['h_att'])):
            out['motivo'] = 'pulito alterato dopo la verifica'
            return out
        out['affidabile'] = True
        return out
    # Legacy non rigenerabile: fidarsi solo se compatibile con l'area del pezzo
    if _plausibile_con_area(w, h, articolo.get('area_dm2')):
        out['affidabile'] = True
    else:
        out['motivo'] = (f'pulito {w:.0f}×{h:.0f} mm incompatibile con l\'area '
                         f'del pezzo ({articolo.get("area_dm2")} dm²)')
    return out


def _entita_sorgente(src_ms):
    """Entità del modelspace sorgente con i blocchi di GEOMETRIA espansi (un
    pezzo definito dentro un INSERT prima veniva scartato → DXF pulito vuoto).
    I blocchi di annotazione (note, tabelle, cartiglio) restano esclusi."""
    try:
        from .dxf_polygon_detector_v3 import entita_espanse
        return entita_espanse(src_ms, solo_geometria=True)
    except Exception:
        return iter(src_ms)


def scala_mm(path: str) -> float:
    """Fattore unità disegno → mm del DXF ($INSUNITS, vedi scala_unita_mm)."""
    try:
        from .dxf_polygon_detector_v3 import scala_unita_mm
        return float(scala_unita_mm(ezdxf.readfile(path))[0])
    except Exception:
        return 1.0


def perimetro_interni_fallback(cleaned_path: str, bbox: Sequence[float]) -> dict:
    """Perimetro dei CONTORNI INTERNI (fori/asole) del DXF pulito, per il
    fallback "rettangolo di ingombro" dopo il cleanup.

    BUG FIX D15: prima si sommavano TUTTI gli archi e le LWPOLYLINE chiuse →
    il contorno esterno veniva contato due volte (rettangolo + contorno vero) e
    i bulge (archi nelle polilinee) ignorati. Qui si usa la geometria del
    detector (bulge, spline, blocchi, duplicati gestiti) e si tengono solo i
    contorni chiusi che stanno DENTRO il bbox senza toccarne il bordo.

    bbox: [minx, miny, maxx, maxy] in unità del disegno.
    Returns: {perim_fori_mm, n_fori, n_pierce (1 + fori), scala_mm}
    """
    out = {'perim_fori_mm': 0.0, 'n_fori': 0, 'n_pierce': 1, 'scala_mm': 1.0}
    try:
        from .dxf_polygon_detector_v3 import _poligoni_documento, _riduci_fori_annidati
        doc = ezdxf.readfile(cleaned_path)
        base = _poligoni_documento(doc, {})
    except Exception as e:
        logger.warning('perimetro_interni_fallback: %s', e)
        return out
    s = base['scala']
    out['scala_mm'] = s
    bx1, by1, bx2, by2 = [float(v) * s for v in bbox]
    lato = max(1e-6, min(bx2 - bx1, by2 - by1))
    tol = max(0.5, 0.005 * lato)
    interni = []
    for p in base['polys']:
        x1, y1, x2, y2 = p.bounds
        if (x1 > bx1 + tol and y1 > by1 + tol and x2 < bx2 - tol and y2 < by2 - tol):
            interni.append(p)
    # un contorno contenuto in un altro contorno interno: svasatura o isola
    interni, _n_svas = _riduci_fori_annidati(interni)
    out['perim_fori_mm'] = sum(p.length for p in interni)
    out['n_fori'] = len(interni)
    out['n_pierce'] = 1 + len(interni)
    return out


def _distance_point_to_bbox(x: float, y: float, bbox: Sequence[float]) -> float:
    """Distanza euclidea approssimata da (x,y) al bbox (minx,miny,maxx,maxy)."""
    minx, miny, maxx, maxy = bbox
    dx = max(minx - x, 0, x - maxx)
    dy = max(miny - y, 0, y - maxy)
    return (dx * dx + dy * dy) ** 0.5


def save_cleaned_dxf_by_click(
    source_path: str,
    cleaned_path: str,
    click_x: float,
    click_y: float,
    max_pick_distance_mm: float = 15.0,  # deprecato: ora fallback su cluster-contains-point
) -> dict:
    """Pulizia DXF a partire da un CLICK su un'entità del pezzo.

    Approccio "a prova di stupido": l'utente non deve tracciare un rettangolo
    (fragile), ma cliccare direttamente su UNA linea/arco/cerchio del pezzo.
    Il sistema:
    1. Trova l'entità geometrica più vicina al punto cliccato.
    2. Identifica il cluster spazialmente connesso di quell'entità.
    3. Assorbe eventuali entità isolate contenute nel bbox del cluster (fori).
    4. Salva il DXF pulito con quel cluster + fori.

    Args:
        source_path: DXF originale
        cleaned_path: destinazione
        click_x, click_y: coordinate del click in mm (DXF)
        max_pick_distance_mm: distanza massima per considerare un'entità "cliccata"

    Returns:
        {'success', 'entities_copied', 'entities_source', 'entities_skipped_meta',
         'bbox_mm', 'clicked_entity_type', 'error', 'warnings'}
    """
    result = {
        'success': False,
        'entities_copied': 0,
        'entities_source': 0,
        'entities_skipped_meta': 0,
        'clicked_entity_type': None,
        'clicked_entity_distance_mm': None,
        'bbox_mm': None,
        'error': None,
        'warnings': [],
    }

    try:
        src = ezdxf.readfile(source_path)
    except Exception as e:
        result['error'] = f'DXF non leggibile: {e}'
        return result

    # Crea documento destinazione
    try:
        dst = ezdxf.new(dxfversion=src.dxfversion, setup=False)
    except Exception:
        dst = ezdxf.new(setup=False)
    for lname in {l.dxf.name for l in src.layers}:
        if lname in dst.layers:
            continue
        try:
            src_layer = src.layers.get(lname)
            new_layer = dst.layers.add(lname)
            try:
                new_layer.dxf.color = src_layer.dxf.color
            except Exception:
                pass
        except Exception:
            pass

    src_ms = src.modelspace()
    dst_ms = dst.modelspace()
    _copia_unita(src, dst)

    # FASE 1: raccogli tutte le entità geometriche con bbox
    all_geom: list[tuple[object, tuple[float, float, float, float]]] = []
    for e in _entita_sorgente(src_ms):
        result['entities_source'] += 1
        et = e.dxftype()
        if et in _SKIP_TYPES:
            result['entities_skipped_meta'] += 1
            continue
        eb = _entity_bbox(e)
        if eb is None:
            result['entities_skipped_meta'] += 1
            continue
        all_geom.append((e, eb))

    if not all_geom:
        result['error'] = 'DXF senza entità geometriche riconoscibili.'
        return result

    # FASE 2: trova l'entità più vicina al click (per riferimento — non più bloccante)
    best_idx = -1
    best_dist = float('inf')
    for i, (_e, eb) in enumerate(all_geom):
        d = _distance_point_to_bbox(click_x, click_y, eb)
        if d < best_dist:
            best_dist = d
            best_idx = i

    if best_idx < 0:
        result['error'] = 'Nessuna entità geometrica nel DXF.'
        return result

    clicked_ent = all_geom[best_idx][0]
    result['clicked_entity_type'] = clicked_ent.dxftype()
    result['clicked_entity_distance_mm'] = round(best_dist, 2)

    # FASE 3: cluster spaziale su TUTTE le entità
    clusters = _all_clusters(all_geom, gap=2.0)

    # FASE 4: seleziona il cluster.
    # STRATEGIA A PROVA DI STUPIDO:
    #
    # 1. Se il click è DIRETTAMENTE su un'entità (dist < 1mm):
    #    → usa il cluster che contiene quella entità. PUNTO.
    #    Ignora cartigli-frame che avvolgono tutto il foglio (loro bbox
    #    contiene il punto ma NON contiene la clicked entity — vince
    #    quello giusto).
    #
    # 2. Se il click è in area vuota (dist >= 1mm):
    #    → cerca cluster il cui bbox contiene il punto.
    #    → preferisci il PIÙ PICCOLO (per bbox area), non il più grande.
    #    Un cartiglio-frame ha bbox enorme, un pezzo piccolo ha bbox
    #    proporzionato → il pezzo vince.
    #    → tra cluster di area simile, usa lo score "pezzo-like".
    def cluster_score(idxs: list[int]) -> float:
        n_c = n_p = n_a = 0
        for i in idxs:
            et = all_geom[i][0].dxftype()
            if et == 'CIRCLE': n_c += 1
            elif et in ('LWPOLYLINE', 'POLYLINE'): n_p += 1
            elif et == 'ARC': n_a += 1
        return n_c * 10 + n_p * 5 + n_a * 2 + len(idxs) * 0.1

    def cluster_bbox_area(idxs: list[int]) -> float:
        cx1 = min(all_geom[i][1][0] for i in idxs)
        cy1 = min(all_geom[i][1][1] for i in idxs)
        cx2 = max(all_geom[i][1][2] for i in idxs)
        cy2 = max(all_geom[i][1][3] for i in idxs)
        return max(0.0, cx2 - cx1) * max(0.0, cy2 - cy1)

    if best_dist < 1.0:
        # Caso 1: click SU un'entità. Il cluster della clicked entity è LA verità.
        winner = None
        for cluster_idxs in clusters:
            if best_idx in cluster_idxs:
                winner = cluster_idxs
                break
        if winner is None:
            winner = [best_idx]
    else:
        # Caso 2: click in area vuota (dist >= 1mm).
        # Cerca cluster il cui bbox contiene il punto — preferisci il più
        # PICCOLO per area (evita cartigli-frame che avvolgono tutto).
        containing_clusters: list[list[int]] = []
        for cluster_idxs in clusters:
            cx1 = min(all_geom[i][1][0] for i in cluster_idxs)
            cy1 = min(all_geom[i][1][1] for i in cluster_idxs)
            cx2 = max(all_geom[i][1][2] for i in cluster_idxs)
            cy2 = max(all_geom[i][1][3] for i in cluster_idxs)
            if cx1 <= click_x <= cx2 and cy1 <= click_y <= cy2:
                containing_clusters.append(cluster_idxs)
        if containing_clusters:
            winner = min(containing_clusters, key=lambda c: (cluster_bbox_area(c), -cluster_score(c)))
        elif best_dist <= max_pick_distance_mm:
            # Nessun cluster contiene il punto → usa il cluster della clicked entity
            # (solo se abbastanza vicino, entro 15mm)
            winner = None
            for cluster_idxs in clusters:
                if best_idx in cluster_idxs:
                    winner = cluster_idxs
                    break
            if winner is None:
                winner = [best_idx]
        else:
            # Click completamente in area vuota, nessun bbox lo contiene
            # e nessuna entità vicina → errore chiaro
            result['error'] = (
                f'Click a {best_dist:.0f}mm dall\'entità più vicina e fuori da qualsiasi pezzo. '
                f'Click DENTRO il rettangolo (bbox) di uno dei pezzi disegnati oppure sul suo contorno.'
            )
            return result

    winner_set = set(winner)

    # FASE 5: assorbi entità isolate contenute nel bbox del cluster (fori)
    wx1 = min(all_geom[i][1][0] for i in winner)
    wy1 = min(all_geom[i][1][1] for i in winner)
    wx2 = max(all_geom[i][1][2] for i in winner)
    wy2 = max(all_geom[i][1][3] for i in winner)
    absorb_tol = 1.0
    for idx, (_e, eb) in enumerate(all_geom):
        if idx in winner_set:
            continue
        ex1, ey1, ex2, ey2 = eb
        if (ex1 >= wx1 - absorb_tol and ex2 <= wx2 + absorb_tol and
            ey1 >= wy1 - absorb_tol and ey2 <= wy2 + absorb_tol):
            winner_set.add(idx)

    # FASE 6: copia entità
    for idx, (e, _eb) in enumerate(all_geom):
        if idx not in winner_set:
            continue
        try:
            new_e = e.copy()
            dst_ms.add_entity(new_e)
            result['entities_copied'] += 1
        except Exception:
            pass

    if result['entities_copied'] < 1:
        result['error'] = 'Cluster vuoto — click non ha selezionato geometria valida.'
        return result

    # Scrivi
    _marca_pulito(dst, TIPO_PULIZIA_MANUALE)
    try:
        os.makedirs(os.path.dirname(cleaned_path), exist_ok=True)
        dst.saveas(cleaned_path)
    except Exception as e:
        result['error'] = f'Scrittura fallita: {e}'
        return result

    # BBox finale
    try:
        xs, ys = [], []
        for e in dst_ms:
            et = e.dxftype()
            if et == 'LINE':
                s, ee = e.dxf.start, e.dxf.end
                xs += [s[0], ee[0]]; ys += [s[1], ee[1]]
            elif et in ('CIRCLE', 'ARC'):
                c = e.dxf.center
                r = float(getattr(e.dxf, 'radius', 0) or 0)
                xs += [c[0] - r, c[0] + r]; ys += [c[1] - r, c[1] + r]
            elif et == 'LWPOLYLINE':
                for pt in e.get_points('xy'):
                    xs.append(pt[0]); ys.append(pt[1])
            elif et == 'POLYLINE':
                for v in e.vertices:
                    xs.append(v.dxf.location.x); ys.append(v.dxf.location.y)
        if xs and ys:
            result['bbox_mm'] = [min(xs), min(ys), max(xs), max(ys)]
    except Exception:
        pass
    _bbox_esatto(dst_ms, result)

    result['success'] = True
    return result


def should_cleanup(detector_result: dict) -> tuple[bool, str]:
    """Decide se il file va auto-pulito in base al risultato del detector v3.

    Returns:
        (yes_no, reason): yes_no=True se procedere con cleanup automatico.

    Regole:
    - confidence >= 0.7          → SI (alta fiducia)
    - confidence 0.5-0.7         → SI (media, ma marker "needs_review")
    - confidence < 0.5           → NO (utente deve intervenire manualmente)
    - area_ratio > 0.9           → NO (sospetto = cartiglio incluso)
    - needs_manual_select=True   → NO
    """
    if not detector_result:
        return (False, 'no detector result')
    if detector_result.get('needs_manual_select'):
        return (False, 'needs_manual_select')
    conf = float(detector_result.get('confidence') or 0)
    if conf < 0.5:
        return (False, f'confidence bassa ({conf:.2f})')
    # Sanity: pezzo che occupa quasi tutto il foglio è sospetto (probabile
    # cartiglio incluso). Confronto area pezzo vs bbox foglio DXF.
    area_pezzo = float(detector_result.get('area_dm2') or 0)
    dxf_bbox = detector_result.get('dxf_bbox_mm') or []
    if len(dxf_bbox) == 4 and area_pezzo > 0:
        # dxf_bbox_mm è in unità DISEGNO, area_dm2 in mm: si converte il foglio
        s = float(detector_result.get('scala_unita_mm') or 1.0)
        foglio_dm2 = (dxf_bbox[2] - dxf_bbox[0]) * (dxf_bbox[3] - dxf_bbox[1]) * s * s / 10000.0
        if foglio_dm2 > 0:
            ratio = area_pezzo / foglio_dm2
            if ratio > 0.9:
                return (False, f'area_ratio {ratio:.2f} > 0.9 (probabile cartiglio incluso)')
    return (True, f'confidence {conf:.2f}')


def get_pezzo_bbox(detector_result: dict) -> list | None:
    """Estrae il bbox del pezzo scelto dal detector v3.

    Il detector espone `candidates[selected_candidate_idx].bbox` con
    [minx, miny, maxx, maxy] in mm. Ritorna None se non disponibile.
    """
    if not detector_result:
        return None
    cands = detector_result.get('candidates') or []
    sel = detector_result.get('selected_candidate_idx')
    if not cands:
        return None
    # selected_candidate_idx può essere l'idx globale (non del vettore cands)
    for c in cands:
        if c.get('is_selected') or c.get('idx') == sel:
            b = c.get('bbox')
            if b and len(b) == 4:
                return list(b)
    # fallback: primo candidato (score più alto)
    b = cands[0].get('bbox')
    return list(b) if b and len(b) == 4 else None
