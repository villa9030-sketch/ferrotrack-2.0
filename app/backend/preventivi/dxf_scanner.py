"""DXF scanning services: bend detection, welding measurement, threading/countersinking detection.

Extracted from preventivatore 2.0.py — pure functions, no UI references.

Tutte le misure sono in MILLIMETRI: il DXF è scalato secondo $INSUNITS
(helper `scala_unita_mm`) e i blocchi (INSERT) vengono espansi
(`entita_espanse`), come nel detector v3 e in pick_part.
"""

import logging
import math
import os
import re

import ezdxf

from .dxf_polygon_detector import detect_pezzo_geometry as _detect_v2
from .dxf_polygon_detector_v3 import (
    colore_effettivo as _colore_effettivo,
    contorno_pezzo_mm as _contorno_pezzo_mm,
    entita_espanse as _entita_espanse,
    scala_unita_mm as _scala_unita_mm,
    _layer_piega,
)

logger = logging.getLogger(__name__)

# Testo piega nel disegno sviluppato: "SU 90° R 2", "GIU' 82° R 2",
# "SU 34.5° R 161.61", "GIU' 6.57° ZERO". Cattura verso + gradi + raggio.
RE_PIEGA_3D = re.compile(r"^(SU|GIU'?|GIÙ)\s+([\d.]+)\s*°\s*(?:R\s*([\d.]+)|(ZERO))", re.I)

# Testo piega per il CONTEGGIO: verso + angolo in gradi obbligatori (il raggio
# può mancare). Prima bastava che il testo iniziasse con "SU"/"GIU" → anche
# "SUPPORTO", "SUPERFICIE SGRASSATA" erano contati come pieghe.
RE_PIEGA_TESTO = re.compile(r"^(SU|GIU'?|GIÙ|UP|DOWN)\s*([\d]+(?:[.,]\d+)?)\s*°", re.I)


def _testo_entita(e) -> str:
    """Testo pulito di TEXT/MTEXT/ATTRIB: MTEXT senza codici di formattazione
    (plain_text di ezdxf), %%d/%%c/%%p convertiti, \\U+XXXX decodificati."""
    try:
        if e.dxftype() == 'MTEXT':
            s = e.plain_text()
        else:
            s = e.dxf.text or ''
    except Exception:
        s = getattr(e.dxf, 'text', '') or ''
    s = re.sub(r'\\U\+([0-9A-Fa-f]{4})', lambda m: chr(int(m.group(1), 16)), s)
    s = s.replace('%%d', '°').replace('%%D', '°').replace('%%c', 'Ø').replace('%%C', 'Ø')
    s = s.replace('%%p', '±').replace('%%P', '±')
    s = re.sub(r'\\[A-Za-z][^;]*;', '', s)   # residui di formattazione (TEXT con codici)
    s = re.sub(r'[{}]', '', s)
    return ' '.join(s.split())


def _punto_testo(e):
    """Punto di inserimento del testo (WCS)."""
    try:
        p = e.dxf.insert
        return float(p.x), float(p.y)
    except Exception:
        return None


def _dist_punto_segmento_3d(px, py, x1, y1, x2, y2):
    """Distanza punto→segmento (per accoppiare testo piega alla linea cerniera)."""
    dx, dy = x2 - x1, y2 - y1
    L = dx * dx + dy * dy
    if L == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / L))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def estrai_pieghe_3d(path: str, config: dict | None = None) -> list[dict]:
    """Estrae le pieghe dal disegno sviluppato per la visualizzazione 3D.

    A differenza di `scansiona_dxf_dettagli` (che le CONTA soltanto), qui
    conserviamo per ogni piega tutto ciò che serve a piegarla in 3D:
      - verso: 'su' | 'giu'          (dal testo SU/GIU')
      - gradi: float                 (angolo di piega, es. 90.0)
      - raggio: float                (R del testo, 0.0 se 'ZERO')
      - hinge: (x1,y1,x2,y2)         (linea cerniera in mm: la LINE più vicina al testo)
      - testo: str                   (annotazione originale, per debug)

    Deterministico al 100%: legge esattamente ciò che il disegnatore ha scritto.
    Ritorna solo le pieghe accoppiate a una linea cerniera (le altre non sono
    piegabili senza ambiguità).
    """
    cfg = config or {}
    lung_min = float(cfg.get("dxf_lunghezza_minima", 15.0))
    dist_max = float(cfg.get("dxf_piega_dist_max", 150.0))
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    f = _scala_unita_mm(doc)[0]

    linee = []
    for e in _entita_espanse(msp, solo_geometria=True):
        if e.dxftype() != 'LINE':
            continue
        try:
            s, en = e.dxf.start, e.dxf.end
            x1, y1, x2, y2 = float(s.x) * f, float(s.y) * f, float(en.x) * f, float(en.y) * f
            linee.append((x1, y1, x2, y2, math.hypot(x2 - x1, y2 - y1)))
        except AttributeError:
            continue

    pieghe = []
    for e in _entita_espanse(msp, solo_geometria=False):
        if e.dxftype() not in ('TEXT', 'MTEXT'):
            continue
        raw = _testo_entita(e)
        m = RE_PIEGA_3D.match(raw.upper())
        if not m:
            continue
        verso = 'su' if m.group(1).upper().startswith('SU') else 'giu'
        gradi = float(m.group(2))
        raggio = 0.0 if m.group(4) else float(m.group(3) or 0.0)
        pt = _punto_testo(e)
        if pt is None:
            continue
        tx, ty = pt[0] * f, pt[1] * f
        best, best_d = None, dist_max
        for (x1, y1, x2, y2, L) in linee:
            if L < lung_min:
                continue
            d = _dist_punto_segmento_3d(tx, ty, x1, y1, x2, y2)
            if d < best_d:
                best_d, best = d, (x1, y1, x2, y2)
        if best is None:
            continue
        pieghe.append({
            'verso': verso, 'gradi': gradi, 'raggio': raggio,
            'hinge': best, 'testo': raw,
        })
    return pieghe


def _lunghezza_tracciato(entity, f: float) -> float:
    """Lunghezza (mm) di LINE/ARC/polilinee (bulge inclusi)/SPLINE via path ezdxf."""
    try:
        from ezdxf.path import make_path
        p = make_path(entity)
        pts = list(p.flattening(0.1 / f))
        return sum(math.hypot(b.x - a.x, b.y - a.y) for a, b in zip(pts, pts[1:])) * f
    except Exception:
        return 0.0


def _poligono_entita(entity, f: float):
    """Polygon Shapely (mm) di un'entità chiusa, o None."""
    try:
        from ezdxf.path import make_path
        from shapely.geometry import Polygon
        pts = [(v.x * f, v.y * f) for v in make_path(entity).flattening(0.2 / f)]
        if len(pts) < 3:
            return None
        poly = Polygon(pts)
        return poly if poly.is_valid and poly.area > 0 else poly.buffer(0)
    except Exception:
        return None


def _entita_chiusa_scanner(entity) -> bool:
    et = entity.dxftype()
    try:
        if et == 'CIRCLE':
            return True
        if et == 'LWPOLYLINE':
            return bool(entity.closed)
        if et == 'POLYLINE':
            return bool(getattr(entity, 'is_closed', False))
        if et == 'SPLINE':
            return bool(getattr(entity, 'closed', False))
        if et == 'ARC':
            return (entity.dxf.end_angle - entity.dxf.start_angle) % 360.0 >= 359.9
    except Exception:
        return False
    return False


def scansiona_dxf_dettagli(path: str, config: dict) -> tuple[int, float, int, int]:
    """Scansiona il DXF e restituisce (pieghe, saldatura_ml, filettatura, svasatura).

    Versione AGGIORNATA (porting dal desktop main_window.py:1105):
    - pieghe: metodo IBRIDO — priorità ai testi 'SU 90°'/'GIU 90°' nel disegno
      (verso + angolo obbligatori), fallback alle linee con colore EFFETTIVO in
      dxf_colori_piega (anche BYLAYER) o su layer PIEGA/BEND/FOLD
    - saldatura: somma lunghezze entità con colore effettivo in dxf_colori_saldatura
      (escluso il contorno chiuso del pezzo, se disegnato in quel colore)
    - filettatura: CIRCLE + ARC concentrici (archi spezzati sommati per raggio)
    - svasatura: CIRCLE concentrici DENTRO il pezzo (escluso se c'è arco
      concentrico = sarebbe filettatura, non svasatura; escluso se il cerchio
      esterno è il contorno del pezzo, es. rondella)
    Blocchi (INSERT) espansi, misure in mm ($INSUNITS).
    """
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    f = _scala_unita_mm(doc)[0]

    colori_piega = config.get("dxf_colori_piega", [2])
    colori_sald = config.get("dxf_colori_saldatura", [1])
    lunghezza_minima = float(config.get("dxf_lunghezza_minima", 15))
    tolleranza_centro = float(config.get("dxf_tolleranza_centro", 1.0))
    ratio_min = float(config.get("dxf_svasatura_ratio_min", 1.3))
    ratio_max = float(config.get("dxf_svasatura_ratio_max", 3.0))
    angolo_min = float(config.get("dxf_semicerchio_angolo_min", 150))
    angolo_max = float(config.get("dxf_semicerchio_angolo_max", 210))
    filtra_zona = config.get("dxf_filtra_zona_sviluppata", False)

    totale_saldatura = 0.0
    circles = []
    arcs = []
    linee_piega = []
    min_x_global = float('inf')
    max_x_global = float('-inf')

    # BUG FIX #4: escludi CIRCLE/ARC del cartiglio/logo/quote per non gonfiare
    # il conteggio filettature/svasature con cerchi decorativi (loghi aziendali
    # composti da cerchi concentrici erano contati come svasature spurie).
    # Filtri applicati:
    #  a) Layer name contiene keyword "cartig", "cartouche", "quot", "dim",
    #     "text", "note", "logo", "frame" → skip
    #  b) Colore (effettivo) in colori_piega o colori_sald → skip (queste sono
    #     lavorazioni, non contorni di taglio)
    #  c) Blocchi di annotazione (note, tabelle, simboli) non espansi
    _CARTIGLIO_LAYER_KEYWORDS = ('cartig', 'cartouche', 'quot', 'dim', 'text',
                                 'note', 'logo', 'frame', 'tratteggio', 'hatch')
    colori_lavorazione = set(colori_piega) | set(colori_sald)

    def _layer_cartiglio(entity):
        try:
            layer = str(getattr(entity.dxf, 'layer', '') or '').lower()
            return any(kw in layer for kw in _CARTIGLIO_LAYER_KEYWORDS)
        except Exception:
            return False

    def _entity_da_escludere(entity):
        if _layer_cartiglio(entity):
            return True
        try:
            return _colore_effettivo(entity) in colori_lavorazione
        except Exception:
            return False

    geometria = list(_entita_espanse(msp, solo_geometria=True))

    # === PASSO 1: Raccolta cerchi e archi (mm, WCS) ===
    for entity in geometria:
        et = entity.dxftype()
        if et not in ('CIRCLE', 'ARC'):
            continue
        if _entity_da_escludere(entity):
            continue
        try:
            c = entity.ocs().to_wcs(entity.dxf.center)
            cx, cy, r = float(c.x) * f, float(c.y) * f, float(entity.dxf.radius) * f
        except Exception:
            continue
        if et == 'CIRCLE':
            circles.append((cx, cy, r))
        else:
            arcs.append((cx, cy, r, float(entity.dxf.start_angle), float(entity.dxf.end_angle)))

    # Cerchi duplicati (stesso centro e raggio): un solo cerchio
    _uniq = []
    for c in circles:
        if not any(abs(c[0] - u[0]) <= 0.01 and abs(c[1] - u[1]) <= 0.01 and abs(c[2] - u[2]) <= 0.01
                   for u in _uniq):
            _uniq.append(c)
    circles = _uniq

    # === PASSO 2A: Detection pieghe via testi "SU 90°"/"GIU 90°" (priorità 1) ===
    testi_piega = []
    for entity in _entita_espanse(msp, solo_geometria=False):
        if entity.dxftype() not in ('TEXT', 'MTEXT'):
            continue
        testo = _testo_entita(entity).upper()
        if RE_PIEGA_TESTO.match(testo):
            pt = _punto_testo(entity)
            if pt is not None:
                testi_piega.append((pt[0] * f, pt[1] * f))

    # === PASSO 2B: Raccogli tutte le linee ===
    tutte_linee = []
    for entity in geometria:
        if entity.dxftype() != 'LINE':
            continue
        try:
            start = entity.dxf.start
            end = entity.dxf.end
            x1, y1 = float(start.x) * f, float(start.y) * f
            x2, y2 = float(end.x) * f, float(end.y) * f
            colore = _colore_effettivo(entity)
            x_centro = (x1 + x2) / 2
            y_centro = (y1 + y2) / 2
            lunghezza = math.hypot(x2 - x1, y2 - y1)
            min_x_global = min(min_x_global, x1, x2)
            max_x_global = max(max_x_global, x1, x2)
            tutte_linee.append({'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2,
                                'x_centro': x_centro, 'y_centro': y_centro,
                                'lunghezza': lunghezza, 'colore': colore})
            su_layer_piega = _layer_piega(getattr(entity.dxf, 'layer', ''))
            if (colore in colori_piega or su_layer_piega) and lunghezza > lunghezza_minima:
                linee_piega.append((x1, y1, x2, y2))
        except AttributeError:
            continue

    # === Saldatura: tutte le entità (LINE, polilinee, archi, spline) col colore
    # effettivo saldatura. BUG FIX #3: la saldatura è spesso disegnata come
    # polilinea/spline/arco (cordoni curvi, multi-tratto).
    # BUG FIX D7: un contorno CHIUSO in colore saldatura che è il contorno del
    # pezzo (o lo racchiude) non è una saldatura: prima il perimetro esterno di
    # un pezzo disegnato in rosso finiva interamente in saldatura_ml.
    outer_pezzo = None
    outer_pezzo_letto = False
    for entity in geometria:
        et = entity.dxftype()
        if et not in ('LINE', 'LWPOLYLINE', 'POLYLINE', 'ARC', 'SPLINE', 'CIRCLE', 'ELLIPSE'):
            continue
        try:
            if _colore_effettivo(entity) not in colori_sald:
                continue
        except Exception:
            continue
        if _layer_cartiglio(entity):
            continue
        if _entita_chiusa_scanner(entity) or et == 'ELLIPSE':
            if not outer_pezzo_letto:
                outer_pezzo = _contorno_pezzo_mm(doc, config)
                outer_pezzo_letto = True
            poly = _poligono_entita(entity, f)
            if outer_pezzo is not None and poly is not None:
                try:
                    dentro = (poly.area < 0.95 * outer_pezzo.area and
                              outer_pezzo.buffer(0.05).contains(poly))
                except Exception:
                    dentro = True
                if not dentro:
                    continue  # è il contorno del pezzo (o lo racchiude): taglio, non saldatura
        totale_saldatura += _lunghezza_tracciato(entity, f)

    # === PASSO 2C: Detection pieghe ibrido ===
    x_medio = (min_x_global + max_x_global) / 2 if max_x_global > min_x_global else 0

    pieghe_da_testo = []
    if testi_piega:
        distanza_max = 150.0
        def _dist_punto_segmento(px, py, x1, y1, x2, y2):
            dx, dy = x2 - x1, y2 - y1
            len_sq = dx * dx + dy * dy
            if len_sq == 0:
                return math.sqrt((px - x1) ** 2 + (py - y1) ** 2)
            t = max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / len_sq))
            proj_x = x1 + t * dx
            proj_y = y1 + t * dy
            return math.sqrt((px - proj_x) ** 2 + (py - proj_y) ** 2)

        for (tx, ty) in testi_piega:
            min_dist = float('inf')
            linea_vicina = None
            for linea in tutte_linee:
                if linea['lunghezza'] < lunghezza_minima:
                    continue
                d = _dist_punto_segmento(tx, ty, linea['x1'], linea['y1'], linea['x2'], linea['y2'])
                if d < min_dist and d < distanza_max:
                    min_dist = d
                    linea_vicina = linea
            if linea_vicina:
                if filtra_zona:
                    if linea_vicina['x_centro'] < x_medio:
                        pieghe_da_testo.append(linea_vicina)
                else:
                    pieghe_da_testo.append(linea_vicina)

    if testi_piega:
        conteggio_pieghe = max(len(testi_piega), len(pieghe_da_testo))
    elif pieghe_da_testo:
        conteggio_pieghe = len(pieghe_da_testo)
    else:
        if filtra_zona and linee_piega and max_x_global > min_x_global:
            linee_filtrate = [l for l in linee_piega if ((l[0] + l[2]) / 2) < x_medio]
            conteggio_pieghe = len(linee_filtrate)
        else:
            conteggio_pieghe = len(linee_piega)

    # === PASSO 3: Bounding box "zona sviluppata" (filtro per filettatura/svasatura) ===
    zona_min_x, zona_max_x = float('inf'), float('-inf')
    zona_min_y, zona_max_y = float('inf'), float('-inf')
    if linee_piega:
        for (x1, y1, x2, y2) in linee_piega:
            zona_min_x = min(zona_min_x, x1, x2)
            zona_max_x = max(zona_max_x, x1, x2)
            zona_min_y = min(zona_min_y, y1, y2)
            zona_max_y = max(zona_max_y, y1, y2)
        ha_zona = True
    elif testi_piega:
        for (tx, ty) in testi_piega:
            zona_min_x = min(zona_min_x, tx)
            zona_max_x = max(zona_max_x, tx)
            zona_min_y = min(zona_min_y, ty)
            zona_max_y = max(zona_max_y, ty)
        ha_zona = True
    else:
        ha_zona = False

    if ha_zona:
        margine = 100.0
        zona_min_x -= margine; zona_max_x += margine
        zona_min_y -= margine; zona_max_y += margine

    def in_zona(x, y):
        if not ha_zona:
            return True
        return zona_min_x <= x <= zona_max_x and zona_min_y <= y <= zona_max_y

    # === PASSO 4: Filettatura (CIRCLE + ARC concentrici, in zona sviluppata) ===
    # Gli archi del simbolo filetto (3/4 di cerchio) a volte sono spezzati in più
    # ARC: si sommano le ampiezze degli archi concentrici dello stesso raggio.
    # Un cerchio = al massimo una filettatura.
    conteggio_filettatura = 0
    for (cxc, cyc, rc) in circles:
        if not in_zona(cxc, cyc) or rc == 0:
            continue
        gruppi = []   # [raggio, ampiezza_totale]
        for (cxa, cya, ra, sa, ea) in arcs:
            if math.hypot(cxc - cxa, cyc - cya) > tolleranza_centro:
                continue
            if not (0.9 <= ra / rc <= 1.5):
                continue
            arc_angle = (ea - sa) % 360.0
            if arc_angle == 0:
                arc_angle = 360.0
            for g in gruppi:
                if abs(g[0] - ra) <= 0.02 * ra:
                    g[1] += arc_angle
                    break
            else:
                gruppi.append([ra, arc_angle])
        for _r, ampiezza in gruppi:
            ampiezza = min(ampiezza, 360.0)
            if (angolo_min <= ampiezza <= angolo_max) or (240 <= ampiezza <= 360):
                conteggio_filettatura += 1
                break

    # === PASSO 5: Svasatura (CIRCLE concentrici, escluso se c'è arco concentrico = filettatura) ===
    # Un gruppo di cerchi concentrici = al massimo una svasatura. Conta solo se il
    # cerchio esterno è DENTRO il pezzo e non è il contorno del pezzo stesso: una
    # rondella Ø60/Ø25 (contorno + foro) non è una svasatura.
    conteggio_svasatura = 0
    gruppi_conc = []
    for c in circles:
        for g in gruppi_conc:
            if math.hypot(c[0] - g[0][0], c[1] - g[0][1]) <= tolleranza_centro:
                g.append(c)
                break
        else:
            gruppi_conc.append([c])
    for g in gruppi_conc:
        if len(g) < 2:
            continue
        cx1, cy1 = g[0][0], g[0][1]
        if not in_zona(cx1, cy1):
            continue
        r_outer = max(c[2] for c in g)
        r_inner = min(c[2] for c in g)
        if r_inner == 0:
            continue
        ratio = r_outer / r_inner
        if not (ratio_min <= ratio <= ratio_max):
            continue
        if any(math.hypot(cx1 - cxa, cy1 - cya) <= tolleranza_centro for (cxa, cya, _ra, _, _) in arcs):
            continue
        if not outer_pezzo_letto:
            outer_pezzo = _contorno_pezzo_mm(doc, config)
            outer_pezzo_letto = True
        if outer_pezzo is not None:
            try:
                from shapely.geometry import Point
                cerchio = Point(cx1, cy1).buffer(r_outer, 64)
                if not (cerchio.area < 0.95 * outer_pezzo.area and
                        outer_pezzo.buffer(0.05).contains(cerchio)):
                    continue  # cerchio esterno = contorno del pezzo o fuori dal pezzo
            except Exception:
                pass
        conteggio_svasatura += 1

    totale_saldatura_ml = round(totale_saldatura / 1000.0, 2)
    logger.info("DXF %s: pieghe=%d, saldatura=%.2f ml, filettatura=%d, svasatura=%d",
                os.path.basename(path), conteggio_pieghe, totale_saldatura_ml,
                conteggio_filettatura, conteggio_svasatura)
    return conteggio_pieghe, totale_saldatura_ml, conteggio_filettatura, conteggio_svasatura


# Leghe di alluminio riconosciute come numero "nudo" (prima bastava qualsiasi
# \b5\d{3}\b → "Dis. 5401" diventava ALU)
_LEGHE_ALU = r'(?:1050|1200|3003|3105|5005|5052|5083|5086|5154|5182|5251|5454|5754|6005|6060|6061|6063|6082|7020|7075)'


def _normalize_materiale_strict(s: str) -> str:
    """Riconoscimento SENZA etichetta 'Materiale': solo designazioni forti come
    token interi (S235JR, AISI 304, INOX, 1.4301, 5754, DC01, DX51D…). Esclusi
    i nomi generici (FERRO, ACCIAIO, STEEL: compaiono in ragioni sociali tipo
    'FERROTRACK srl') e i numeri dentro codici ('A316-02', 'Dis. 5401')."""
    import re as _re
    if _re.search(r'\bDX5[1-6]D?\s*\+\s*Z|\bS[23]\d{2}GD\b|\+\s*Z\d{2,4}\b|\bZINCAT', s):
        return 'ZINCATO'
    if _re.search(r'\bOTTONE\b|\bBRASS\b|\bCUZN\d|\bCW\d{3}[A-Z]\b', s):
        return 'OTTONE'
    if _re.search(r'\b(?:AISI|INOX|SS)\s*-?\s*3(?:04|16)L?\b|\b1\.4(?:301|307|401|404)\b|\bX\d+CRNI|\bINOX\b|\bSTAINLESS\b', s):
        return 'INOX_304'
    if _re.search(r'\b(?:EN\s*-?\s*)?AW\s*-?\s*' + _LEGHE_ALU + r'\b|\bALLUMINIO\b|\bALUMINIUM\b|\bALUMINUM\b|\bALU\b|\bAL\s*MG\s*\d', s):
        return 'ALU'
    if _re.search(r'(?:^|\bAL\s*-?\s*|\bALU\s*-?\s*)' + _LEGHE_ALU + r'\b', s):
        return 'ALU'
    if _re.search(r'\bS(?:235|275|355)(?:J[R0-2]|JRC|J2\+N|MC|N|NL)?\b|\b1\.0(?:037|038|044|577)\b|\bST\s*37\b|\bFE\s*3[67]0?\b|\bDC0[1-6]\b|\bDD1[1-4]\b', s):
        return 'S235'
    return ''


def _normalize_materiale_cartiglio(raw: str, strict: bool = False) -> str:
    """Mappa il valore raw del cartiglio al codice del laser_estimator.

    Copre i 5 materiali del cliente (S235, ZINCATO, INOX_304, ALU, OTTONE)
    con pattern noti dei principali standard: DIN/EN, ASTM/AISI, UNI, nomi
    commerciali. Se non riconosciuto → stringa vuota (caller può fallback a LLM).

    strict=True: testo trovato SENZA etichetta 'Materiale' vicina → accetta solo
    designazioni forti (vedi _normalize_materiale_strict).

    Pattern reali trovati nei DXF cliente: 'AISI 304', '1.0037 (S235JR)',
    'S235JR', 'X5CrNi18-10', 'INOX 316', 'C75 S', 'S235JR+Z275'.
    """
    import re as _re
    s = (raw or '').strip().upper()
    if not s:
        return ''
    if strict:
        return _normalize_materiale_strict(s)

    # ---- ZINCATO (prima di S235 perché ha marker aggiuntivo Z/GD) ----
    # Lamiere pre-zincate (DIN EN 10346: DX51D+Z, DX52D+Z, DX53D+Z, DX54D+Z)
    if _re.search(r'\bDX5[1-6]D?\s*\+?\s*Z', s):
        return 'ZINCATO'
    # Acciai galvanizzati per profilazione (S220GD, S250GD, S320GD, S350GD)
    if _re.search(r'\bS[23]\d{2}GD\b', s):
        return 'ZINCATO'
    # S235JR + Z275 / +Z100 (rivestimento zinco su acciaio strutturale)
    if _re.search(r'\+\s*Z\d{2,4}\b', s):
        return 'ZINCATO'
    if 'ZINCAT' in s or 'GALVAN' in s or 'SENDZIMIR' in s or 'ZINCK' in s:
        return 'ZINCATO'

    # ---- OTTONE ----
    if 'OTTONE' in s or 'BRASS' in s or 'MESSING' in s or _re.search(r'\bCUZN\d', s):
        return 'OTTONE'
    # Codici commerciali ottone (MS58, MS63, MS72, CW508L, CW614N)
    if _re.match(r'^MS\d{2}', s) or _re.match(r'^CW\d{3}[A-Z]?', s):
        return 'OTTONE'

    # ---- INOX 316 (numerazione DIN 1.4401/1.4404) ----
    # (token interi: prima bastava '316' ovunque → "Codice A316-02" = INOX)
    if _re.search(r'\b316L?\b', s) or '1.4401' in s or '1.4404' in s or 'X2CRNIMO' in s:
        return 'INOX_304'  # mappato a 304 (no 316 in laser_config attuale)

    # ---- INOX 304 (numerazione DIN 1.4301 / nome X5CrNi) ----
    if _re.search(r'\b304L?\b', s) or '1.4301' in s or 'X5CRNI' in s or 'INOX' in s or 'AISI' in s or 'STAINLESS' in s:
        return 'INOX_304'
    if _re.search(r'\bX\d+CRNI\b', s):
        return 'INOX_304'

    # ---- Alluminio (tutte le leghe → ALU nel laser_config) ----
    if 'ALLUM' in s or s.startswith('ALU') or 'ALUMIN' in s or s == 'AL':
        return 'ALU'
    if _re.search(r'\b' + _LEGHE_ALU + r'\b', s):  # 5052, 5083, 5754, ecc. (leghe reali)
        return 'ALU'
    if _re.search(r'\b(6060|6061|6082|7075|3003|1050)\b', s):
        return 'ALU'
    if _re.search(r'\bENAW\b', s) or _re.search(r'\bAW-?\d{4}\b', s):
        return 'ALU'

    # ---- S235 aggregato (tutti gli acciai al carbonio strutturali) ----
    if 'S235' in s or '1.0037' in s or 'ST37' in s or 'FE 37' in s or 'FE37' in s:
        return 'S235'
    if 'S275' in s or '1.0044' in s:
        return 'S235'
    if 'S355' in s or '1.0577' in s or 'ST52' in s:
        return 'S235'
    if 'S460' in s or 'S500' in s or 'S550' in s:
        return 'S235'  # HSLA aggregati
    # Acciai al carbonio per molle/lamine (C45, C50, C60, C75, C45E, C60E)
    # (senza spazio: "C 20 revisione" non è un acciaio C20)
    if _re.search(r'\bC\d{2,3}[A-Z]?\b', s):
        return 'S235'
    # Lamiere a freddo per imbutitura (DIN EN 10130: DC01..DC06)
    if _re.match(r'^DC0?\d', s) or _re.match(r'^DD1\d', s):
        return 'S235'
    # Acciai E335/E355/E360 (DIN EN 10025)
    if _re.search(r'\bE3[3-9]\d\b', s):
        return 'S235'
    # Nomi generici acciaio — parole intere ("FERROTRACK srl" non è ferro)
    if _re.search(r'\b(?:ACCIAIO|ACCIAI|STEEL|FERRO|STAHL)\b', s):
        return 'S235'

    # le designazioni forti valgono anche vicino all'etichetta (AlMg3, Fe 360…)
    return _normalize_materiale_strict(s)


# Etichetta materiale: "MAT", "MAT.", "MATERIALE", "MATERIAL", "Materiale:",
# anche col valore nello stesso testo ("Materiale: S235", "MAT. AISI 304").
# Il lookahead evita "MATRICOLA", "MATITA"…
_RE_ETICHETTA_MATERIALE = re.compile(
    r'^\s*MAT(?:ERIALE?)?(?![A-Z0-9])\s*\.?\s*[:=]?\s*(?P<val>.*)$', re.IGNORECASE)

# Oltre questa distanza (mm) dall'etichetta, un testo è accettato come materiale
# solo se è una designazione forte (strict): evita di prendere la ragione sociale.
_DIST_MATERIALE_LENIENT_MM = 60.0


def estrai_materiale_da_cartiglio(path: str) -> dict:
    """Estrae il materiale dal cartiglio del disegno DXF.

    Strategia:
    1. Etichetta con valore nello stesso testo ("Materiale: S235", "MAT. AISI 304").
    2. Etichetta da sola ("MATERIALE", "MAT.", "Material:") → il testo più vicino
       che sia un materiale (oltre 60mm solo designazioni forti).
    3. Nessuna etichetta → solo designazioni forti a token intero (S235JR,
       AISI 304, INOX, 5754, DC01…) con confidence BASSA (0.4). Prima qualsiasi
       testo passava dalla tabella: "FERROTRACK srl" → S235, "Dis. 5401" → ALU.
    Testi letti anche dentro i blocchi (INSERT/ATTRIB), MTEXT senza formattazione.

    Returns:
        {materiale: 'S235'|'INOX_304'|..., materiale_raw: stringa originale,
         confidence: float 0..1}.
        Se non trovato: materiale='', confidence=0. Se il materiale resta vuoto
        perché il fallback LLM non era disponibile (o ha avuto un errore
        transitorio) c'è anche `_llm_non_disponibile: True` (il chiamante non
        deve mettere in cache il risultato).
    """
    testi = _raccogli_testi_dxf(path)
    vuoto = {'materiale': '', 'materiale_raw': '', 'confidence': 0.0}
    if not testi:
        return vuoto

    # ---- 1-2. Etichette
    label_pos = None
    label_idx = set()
    inline_unmapped = None
    for i, (x, y, t) in enumerate(testi):
        m = _RE_ETICHETTA_MATERIALE.match(t)
        if not m:
            continue
        label_idx.add(i)
        val = m.group('val').strip(' /:-=')
        if val and not _RE_ETICHETTA_MATERIALE.match(val):
            mat = _normalize_materiale_cartiglio(val)
            if mat:
                return {'materiale': mat, 'materiale_raw': val, 'confidence': 0.95}
            if len(val) >= 3 and any(c.isalpha() for c in val) and inline_unmapped is None:
                inline_unmapped = val
        if label_pos is None:
            label_pos = (x, y)

    if not label_pos:
        # ---- 3. Nessuna etichetta: solo designazioni forti, confidence bassa
        for x, y, t in testi:
            mat = _normalize_materiale_cartiglio(t, strict=True)
            if mat:
                return {'materiale': mat, 'materiale_raw': t, 'confidence': 0.4}
        # LAST RESORT: se abbiamo trovato una stringa candidata ma la tabella
        # non la riconosce, chiediamo a Gemini (se configurato)
        return _try_llm_fallback(testi, label_pos=None)

    # Trova il testo più vicino spazialmente che sia un materiale valido
    best = None
    best_dist = float('inf')
    best_unmapped_raw = inline_unmapped  # candidato raw ma non normalizzato dalla tabella
    best_unmapped_dist = 0.0 if inline_unmapped else float('inf')
    lx, ly = label_pos
    for i, (x, y, t) in enumerate(testi):
        if i in label_idx:
            continue
        d = math.hypot(x - lx, y - ly)
        mat = _normalize_materiale_cartiglio(t, strict=d > _DIST_MATERIALE_LENIENT_MM)
        if mat:
            if d < best_dist:
                best_dist = d
                best = (mat, t, d)
        elif (d <= _DIST_MATERIALE_LENIENT_MM and len(t) >= 3 and any(c.isalpha() for c in t)
              and d < best_unmapped_dist):
            # Candidato non mappato dalla tabella: potrebbe essere un codice
            # sconosciuto (es. cartiglio custom). Salviamo per fallback LLM.
            best_unmapped_dist = d
            best_unmapped_raw = t
    if best:
        # confidence proporzionale alla distanza (più vicino = più alta)
        # entro 50 unità DXF: 1.0, oltre 500: 0.3
        conf = max(0.3, min(1.0, 1.0 - (best[2] / 500.0)))
        return {'materiale': best[0], 'materiale_raw': best[1], 'confidence': round(conf, 2)}

    # Fallback LLM: la tabella non ha riconosciuto ma abbiamo un candidato raw
    if best_unmapped_raw:
        llm_result, transitorio = _try_llm_normalize(best_unmapped_raw)
        if llm_result:
            return llm_result
        if transitorio:
            return {**vuoto, 'materiale_raw': best_unmapped_raw, '_llm_non_disponibile': True}

    return {'materiale': '', 'materiale_raw': best_unmapped_raw or '', 'confidence': 0.0}


def _somma_perim_fori_da_circle(path: str, min_r_mm: float = 1.0,
                                  max_r_mm: float = 25.0,
                                  dedup_tol_mm: float = 0.5) -> tuple[float, int]:
    """Somma il perimetro dei CIRCLE nel DXF che sono verosimili "fori di taglio".

    Politica:
    - Include solo CIRCLE con raggio ∈ [min_r_mm, max_r_mm] (esclude marker
      minuscoli e cerchi enormi tipo bordi decorativi).
    - Cerchi concentrici (stesso center entro dedup_tol) sono trattati come
      un solo foro con svasatura: si prende SOLO il raggio minimo (foro
      passante = quello che il laser deve tagliare). La svasatura è
      lavorazione post-taglio, non contribuisce al perimetro di taglio.
    - Blocchi espansi (esclusi quelli di annotazione), misure in mm.

    Returns:
        (perimetro_totale_mm, n_fori)
    """
    try:
        doc = ezdxf.readfile(path)
    except Exception:
        return (0.0, 0)
    f = _scala_unita_mm(doc)[0]
    circles_by_center: dict = {}
    for e in _entita_espanse(doc.modelspace(), solo_geometria=True):
        if e.dxftype() != 'CIRCLE':
            continue
        try:
            c = e.ocs().to_wcs(e.dxf.center)
            cx = float(c.x) * f
            cy = float(c.y) * f
            r = float(e.dxf.radius) * f
        except Exception:
            continue
        if r < min_r_mm or r > max_r_mm:
            continue
        # Chiave di clustering: centro arrotondato a dedup_tol
        key = (round(cx / dedup_tol_mm), round(cy / dedup_tol_mm))
        # Tieni solo il raggio più piccolo per cluster (passante)
        if key not in circles_by_center or r < circles_by_center[key]:
            circles_by_center[key] = r
    perim_tot = sum(2 * math.pi * r for r in circles_by_center.values())
    return (perim_tot, len(circles_by_center))


# ---- Pattern dimensioni/spessore nelle descrizioni del cartiglio ----
# Numero con decimali (anche virgola: "45,5")
_RX_NUM = r'(\d{1,4}(?:[.,]\d+)?)'
# Etichetta spessore ESPLICITA: sp / sp. / spess. / spessore / s= / thk / thickness
_RX_ETICHETTA_SP = r'(?<![A-Za-z])(?:sp(?:ess(?:ore)?)?\.?|s\s*=|thk\.?|thickness)\s*(?:/\s*[⌀Øø]\s*)?[:=]?\s*'
# "45x12 sp.3", "100x50 sp 4mm", "45,5x12 - SP=2": lo spessore è accettato SOLO
# con etichetta (BUG FIX D9: prima "sp" era opzionale → "FORMATO 420x297 1:1"
# dava pezzo 12 dm² sp.1 e "100x50 2 PZ" dava spessore 2).
RX_RECT_SP = re.compile(
    r'(?<![\d.,])' + _RX_NUM + r'\s*[xX×]\s*' + _RX_NUM + r'\s*(?:mm)?\s*[-,;]?\s*'
    + _RX_ETICHETTA_SP + r'(\d+(?:[.,]\d+)?)\s*(?:mm)?(?![\d.,]*\s*[xX×])',
    re.IGNORECASE,
)
RX_RECT_ONLY = re.compile(
    r'(?<![\d.,])' + _RX_NUM + r'\s*[xX×]\s*' + _RX_NUM + r'(?![\d.,]*\s*[xX×])\s*(?:mm)?'
)
# Spessore esplicito in un testo: "SP. 3", "SP=3", "S=2", "SPESSORE 1,5", "sp3", "thk 2"
RX_INTERNAL_SP = re.compile(
    _RX_ETICHETTA_SP + r'(\d+(?:[.,]\d+)?)\s*(?:mm)?(?![\d.,]*\s*[xX×])', re.IGNORECASE
)
# Testi che contengono misure del FOGLIO / scala, non del pezzo
_RX_TESTO_FORMATO = re.compile(r'\b(?:FORMAT[OI]?|SCALA|SCALE|FOGLIO|SHEET|A[0-4])\b|\b\d+\s*:\s*\d+\b',
                               re.IGNORECASE)


def _num_it(s: str) -> float:
    return float(s.replace(',', '.'))


def _is_formato_foglio(dx: float, dy: float) -> bool:
    """True se dx×dy coincide con un formato foglio ISO (A4…A0)."""
    a, b = max(dx, dy), min(dx, dy)
    return any(abs(a - w) <= 1 and abs(b - h) <= 1
               for w, h in ((297, 210), (420, 297), (594, 420), (841, 594), (1189, 841)))


def estrai_dimensioni_da_descrizione_cartiglio(path: str) -> dict:
    """Fallback per DXF dove il detector non riesce a chiudere il contorno:
    cerca nel cartiglio una descrizione tipo "Lama di contenimento 45x12 sp.3"
    o "Piastra 100x50 sp.4" e ne ricava area/perimetro/spessore rettangolari.

    Perimetro di taglio: outer_rettangolo + perimetro dei fori interni
    (rilevati come CIRCLE con raggio commerciale plausibile).

    Usato per pezzi rettangolari semplici dove il DXF ha contorno rotto ma il
    testo del cartiglio è esplicito. Formati foglio / scale ("FORMATO 420x297
    1:1") sono ignorati; lo spessore va SEMPRE etichettato (sp/spessore/s=/thk).

    Returns:
        {area_dm2, perimetro_taglio_m, spessore_mm, dim_x_mm, dim_y_mm,
         raw_text, confidence, n_forature, source='cartiglio_descrizione'}
        Oppure {area_dm2: None} se non trovato.
    """
    testi = _raccogli_testi_dxf(path)
    if not testi:
        return {'area_dm2': None, 'source': 'none'}

    for x, y, t in testi:
        # Skip cartigli standard che potrebbero avere numeri incoerenti
        if len(t) > 200 or _RX_TESTO_FORMATO.search(t):
            continue
        m = RX_RECT_SP.search(t)
        if m:
            try:
                dx = _num_it(m.group(1))
                dy = _num_it(m.group(2))
                sp = _num_it(m.group(3))
                # Sanity: pezzo lamiera plausibile
                if 5 <= dx <= 3000 and 5 <= dy <= 3000 and 0.5 <= sp <= 30:
                    area_dm2 = (dx * dy) / 10000.0
                    perim_outer_mm = 2 * (dx + dy)
                    # Aggiungi perimetro dei fori interni (CIRCLE clusterizzati)
                    perim_fori_mm, n_fori = _somma_perim_fori_da_circle(path)
                    perim_totale_m = (perim_outer_mm + perim_fori_mm) / 1000.0
                    return {
                        'area_dm2': round(area_dm2, 4),
                        'perimetro_taglio_m': round(perim_totale_m, 4),
                        'spessore_mm': sp,
                        'dim_x_mm': dx, 'dim_y_mm': dy,
                        'raw_text': t,
                        'confidence': 0.85,
                        'n_forature': n_fori + 1,  # +1 per contorno esterno (1 pierce)
                        'source': 'cartiglio_descrizione',
                    }
            except (ValueError, AttributeError):
                continue

    # Secondo tentativo: dimensioni rettangolari separate dallo spessore
    # (es. "Piastra 100x50" in un TEXT + "Sp.: 3" in un altro)
    best_rect = None
    best_sp = None
    for x, y, t in testi:
        if len(t) > 200 or _RX_TESTO_FORMATO.search(t):
            continue
        m = RX_RECT_ONLY.search(t)
        if m and not best_rect:
            try:
                dx = _num_it(m.group(1))
                dy = _num_it(m.group(2))
                if 5 <= dx <= 3000 and 5 <= dy <= 3000 and not _is_formato_foglio(dx, dy):
                    best_rect = (dx, dy, t)
            except ValueError:
                pass
        m2 = RX_INTERNAL_SP.search(t)
        if m2 and not best_sp:
            try:
                sp = _num_it(m2.group(1))
                if 0.5 <= sp <= 30:
                    best_sp = sp
            except ValueError:
                continue
    if best_rect and best_sp:
        dx, dy, raw_t = best_rect
        area_dm2 = (dx * dy) / 10000.0
        perim_outer_mm = 2 * (dx + dy)
        perim_fori_mm, n_fori = _somma_perim_fori_da_circle(path)
        perim_totale_m = (perim_outer_mm + perim_fori_mm) / 1000.0
        return {
            'area_dm2': round(area_dm2, 4),
            'perimetro_taglio_m': round(perim_totale_m, 4),
            'spessore_mm': best_sp,
            'dim_x_mm': dx, 'dim_y_mm': dy,
            'raw_text': raw_t,
            'confidence': 0.65,
            'n_forature': n_fori + 1,
            'source': 'cartiglio_descrizione',
        }

    # Terzo tentativo: cartiglio "tabellare" con label separate spazialmente
    # (es. label "Lunghezza:" @ pos_A, valore numerico "280" @ pos_B).
    # Pattern usato dai cartigli Lantek/UNI standardizzati.
    result = _estrai_da_cartiglio_tabellare(testi, path)
    if result:
        return result

    return {'area_dm2': None, 'source': 'none'}


def _estrai_da_cartiglio_tabellare(testi: list, path: str) -> dict | None:
    """Cartiglio standardizzato: label 'Lunghezza:' 'Larghezza:' 'Sp./⌀:' con
    valore numerico nel TEXT più vicino. Il valore va cercato in un raggio
    di alcuni cm dalla label (non tutto il cartiglio).

    Guardrail:
    - Escludo revisioni (numeri a 2 cifre "00", "01" ecc. sono spesso rev.)
    - Range Lunghezza/Larghezza: 5-3000mm
    - Range Spessore: 0.5-30mm
    - Sanity check finale: se peso disponibile → verifica peso ≈ V × densità
    """
    import re
    import math

    def _num(s: str) -> float | None:
        m = re.match(r'^\s*(\d+[.,]?\d*)\s*(?:mm)?\s*$', s.strip())
        if not m:
            return None
        try:
            return float(m.group(1).replace(',', '.'))
        except ValueError:
            return None

    def _find_value(label_key: str, min_v: float, max_v: float,
                     max_dist_mm: float = 100.0) -> tuple[float, float] | None:
        """Trova il valore della label nella CELLA del cartiglio: stessa riga a
        destra della label (|dy| ≤ 5mm), altrimenti la cella sotto.

        Prima si prendeva il numero più vicino in qualsiasi direzione: su
        20R201N0401 accanto a "Sp./Ø:" c'è il numero di revisione "01" (4mm, a
        sinistra/sotto) → spessore 1.0 invece del 3 scritto nella cella a destra.
        Numeri con zeri iniziali ("01", "00") sono codici/revisioni, mai misure.
        """
        label_pos = None
        for x, y, t in testi:
            if t.strip().lower().startswith(label_key):
                label_pos = (x, y)
                break
        if not label_pos:
            return None
        lx, ly = label_pos
        riga, sotto = [], []
        for x, y, t in testi:
            if (x, y) == label_pos:
                continue
            ts = t.strip()
            if re.match(r'^0\d', ts):
                continue  # "01", "007": revisione / codice
            v = _num(ts)
            if v is None or v < min_v or v > max_v:
                continue
            dx, dy = x - lx, y - ly
            d = math.hypot(dx, dy)
            if d > max_dist_mm:
                continue
            if abs(dy) <= 5.0 and dx > 0:
                riga.append((dx, v, d))
            elif abs(dx) <= 30.0 and -15.0 <= dy < 0:
                sotto.append((-dy, v, d))
        for gruppo in (riga, sotto):
            if gruppo:
                _k, v, d = min(gruppo)
                return (v, d)
        return None

    # Cerco valori con range di plausibilità stringenti (escludono revisioni,
    # anno, protocol number, ecc.)
    lunghezza = _find_value('lunghezza', min_v=5, max_v=3000)
    larghezza = _find_value('larghezza', min_v=5, max_v=3000)
    spessore = _find_value('sp.', min_v=0.5, max_v=30)  # 'sp./⌀:', 'sp.', 'sp:'
    if not spessore:
        spessore = _find_value('spessore', min_v=0.5, max_v=30)

    if not (lunghezza and larghezza and spessore):
        return None

    dx = lunghezza[0]
    dy = larghezza[0]
    sp = spessore[0]
    # Sanity semantico: per una lamiera, il rapporto lato-min/spessore
    # deve essere >= 3 (altrimenti sarebbe una barra/tondino, non lamiera).
    # Blocca match spuri tipo L=8mm W=8mm sp=2mm (ratio 4) che sembrano
    # cifre da campi vuoti (00, 8, 7...) invece che vere dimensioni.
    lato_min = min(dx, dy)
    if lato_min / sp < 3:
        return None
    area_dm2 = (dx * dy) / 10000.0
    perim_outer_mm = 2 * (dx + dy)
    perim_fori_mm, n_fori = _somma_perim_fori_da_circle(path)
    perim_totale_m = (perim_outer_mm + perim_fori_mm) / 1000.0

    # Confidence: dipende da distanza label→valore. Se distanza > 50mm,
    # confidence media (potrebbe essere fluke). Altrimenti alta.
    max_dist = max(lunghezza[1], larghezza[1], spessore[1])
    if max_dist < 30:
        conf = 0.80
    elif max_dist < 80:
        conf = 0.65
    else:
        conf = 0.50

    return {
        'area_dm2': round(area_dm2, 4),
        'perimetro_taglio_m': round(perim_totale_m, 4),
        'spessore_mm': sp,
        'dim_x_mm': dx, 'dim_y_mm': dy,
        'raw_text': f'Cartiglio tabellare: L={dx} W={dy} sp={sp}',
        'confidence': conf,
        'n_forature': n_fori + 1,
        'source': 'cartiglio_tabellare',
    }


def _try_llm_normalize(raw: str) -> tuple[dict | None, bool]:
    """Tenta normalizzazione via LLM.

    Returns (risultato, transitorio): risultato = dict compat se successo, None
    altrimenti; transitorio=True se l'esito negativo dipende da LLM non
    disponibile / errore di rete (il risultato NON va messo in cache: ripetendo
    l'import più tardi il materiale potrebbe essere riconosciuto)."""
    try:
        from . import llm_material_normalizer
        if not llm_material_normalizer.is_available():
            return None, True
        mat, esito = llm_material_normalizer.normalizza_con_esito(raw)
        if mat:
            return {
                'materiale': mat,
                'materiale_raw': raw,
                'confidence': 0.75,  # confidence media: LLM ha risposto ma non è deterministico
                '_source': 'llm',
            }, False
        return None, esito in ('errore', 'non_disponibile')
    except Exception as e:
        logger.warning('LLM material fallback fallito: %s', e)
    return None, True


def _try_llm_fallback(testi: list, label_pos=None) -> dict:
    """Fallback quando non troviamo etichetta 'Materiale': chiediamo a LLM
    di analizzare tutti i TEXT ragionevoli. Usato solo se rules-based fallisce."""
    # Per ora restituiamo empty result — implementazione full richiede prompt
    # multi-text che è overhead senza copertura reale nei DXF cliente attuali
    return {'materiale': '', 'materiale_raw': '', 'confidence': 0.0}


# Cache dei testi per file: nello stesso import materiale/peso/dimensioni/
# spessore rileggono gli stessi testi (chiave = path + mtime + size).
_CACHE_TESTI: dict = {}
_CACHE_TESTI_MAX = 32


def _raccogli_testi_dxf(path: str) -> list[tuple[float, float, str]]:
    """Raccoglie TEXT/MTEXT/ATTRIB dal DXF con pulizia codici e coordinate (mm).

    Espande i blocchi (INSERT, anche di annotazione: il cartiglio è spesso un
    blocco con ATTRIB), MTEXT via plain_text() (niente codici di formattazione),
    coordinate scalate in mm secondo $INSUNITS (le distanze label→valore sono
    soglie in mm).

    Returns lista di (x, y, testo_pulito).
    """
    try:
        st = os.stat(path)
        chiave = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
    except OSError:
        chiave = None
    if chiave is not None and chiave in _CACHE_TESTI:
        return list(_CACHE_TESTI[chiave])
    try:
        doc = ezdxf.readfile(path)
    except Exception:
        return []
    f = _scala_unita_mm(doc)[0]
    testi = []
    for e in _entita_espanse(doc.modelspace(), solo_geometria=False):
        if e.dxftype() not in ('MTEXT', 'TEXT', 'ATTRIB'):
            continue
        try:
            t_clean = _testo_entita(e)
            if not t_clean:
                continue
            pt = _punto_testo(e)
            if pt is None:
                continue
            testi.append((pt[0] * f, pt[1] * f, t_clean))
        except Exception:
            continue
    if chiave is not None:
        if len(_CACHE_TESTI) >= _CACHE_TESTI_MAX:
            _CACHE_TESTI.pop(next(iter(_CACHE_TESTI)), None)
        _CACHE_TESTI[chiave] = list(testi)
    return testi


def estrai_peso_da_cartiglio(path: str) -> dict:
    """Estrae il peso in kg dal cartiglio del DXF.

    Pattern univoco "Peso kg", "Peso (kg)", "Weight kg", "Peso:" seguito
    da un numero (o numero vicino spazialmente se in TEXT separato).

    Returns:
        {peso_kg: float|None, peso_raw: str, confidence: 0..1,
         source: 'inline'|'label'|'none'}
    """
    import re
    import math as _math

    testi = _raccogli_testi_dxf(path)
    if not testi:
        return {'peso_kg': None, 'peso_raw': '', 'confidence': 0.0, 'source': 'none'}

    def _parse_num(s: str) -> float | None:
        s = s.strip().replace(',', '.')
        try:
            v = float(s)
            # Peso pezzo plausibile: 0.001 kg (1g) - 2000 kg
            if 0.001 <= v <= 2000.0:
                return v
        except ValueError:
            pass
        return None

    # --- 1. INLINE: "Peso Kg 0.34", "Peso: 0.34 kg", "Weight 12.5" ---
    RX_INLINE = re.compile(
        r'\b(?:peso|weight)\b\s*(?:\(?\s*kg\s*\)?)?\s*[:=]?\s*(\d+[.,]?\d*)\s*(?:kg)?\b',
        re.IGNORECASE,
    )
    for x, y, t in testi:
        m = RX_INLINE.search(t)
        if m:
            v = _parse_num(m.group(1))
            if v is not None:
                return {'peso_kg': v, 'peso_raw': t, 'confidence': 0.95, 'source': 'inline'}

    # --- 2. LABEL + numero vicino ---
    # Cerca TEXT che sia solo la label "Peso kg" / "Peso" / "Weight"
    LABEL_RX = re.compile(r'^\s*(?:peso|weight)\b\s*(?:\(?\s*kg\s*\)?)?[:=]?\s*$',
                          re.IGNORECASE)
    label_pos = None
    for x, y, t in testi:
        if LABEL_RX.match(t):
            label_pos = (x, y)
            break
    if label_pos:
        lx, ly = label_pos
        best = None
        best_dist = float('inf')
        NUM_ONLY_RX = re.compile(r'^\s*(\d+[.,]?\d*)\s*(?:kg)?\s*$', re.IGNORECASE)
        for x, y, t in testi:
            if (x, y) == label_pos:
                continue
            m = NUM_ONLY_RX.match(t)
            if not m:
                continue
            v = _parse_num(m.group(1))
            if v is None:
                continue
            d = _math.hypot(x - lx, y - ly)
            if d < best_dist:
                best_dist = d
                best = (v, t, d)
        if best:
            # Peso in cartiglio raramente lontano dalla label. Se dist > 50 unità
            # DXF, la confidence cala.
            conf = max(0.5, min(0.95, 1.0 - (best[2] / 200.0)))
            return {'peso_kg': best[0], 'peso_raw': best[1],
                    'confidence': round(conf, 2), 'source': 'label'}

    return {'peso_kg': None, 'peso_raw': '', 'confidence': 0.0, 'source': 'none'}


# Densità standard (kg/dm3) usate per stima spessore da peso+area.
# Aligned con laser_cost_estimator.DEFAULT_LASER_CONFIG['materiali'].
_DENSITA_STD = {
    'S235': 7.85, 'ZINCATO': 7.85, 'INOX_304': 8.00, 'INOX_316': 8.00,
    'ALU': 2.70, 'ALU_5754': 2.70, 'ALU_5083': 2.66, 'OTTONE': 8.50,
}


STD_SPESSORI_MM = [
    0.5, 0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0,
    6.0, 8.0, 10.0, 12.0, 15.0, 20.0, 25.0, 30.0
]


def stima_spessore_da_peso(peso_kg: float, area_dm2: float,
                            materiale: str) -> dict:
    """Calcola spessore da peso × area × densità del materiale.

    Formula: spessore_mm = peso_kg / (area_dm2 * densita_kg_dm3) * 100
    (area in dm², spessore in dm = mm/100)

    Include physics sanity check + warnings strutturati:
    - Verifica materiale coerente con densità (cross-material check)
    - Arrotondamento a valore commerciale standard più vicino
    - Warnings esposti in `warnings` per UI traffic-light

    Returns:
        {spessore_mm, confidence, source, peso_kg, area_dm2, densita,
         errore_std_pct, warnings, possibili_materiali}
        oppure risultato "none" se input non plausibili.
    """
    if not peso_kg or peso_kg <= 0 or not area_dm2 or area_dm2 <= 0:
        return {'spessore_mm': None, 'confidence': 0.0, 'source': 'none',
                'warnings': ['peso o area non validi']}
    mat_key = (materiale or '').strip().upper()
    densita = _DENSITA_STD.get(mat_key)
    if not densita:
        return {'spessore_mm': None, 'confidence': 0.0, 'source': 'none',
                'warnings': [f'materiale {materiale!r} non in tabella densità']}
    # spessore in dm = peso / (area * densita); convert to mm
    sp = (peso_kg / (area_dm2 * densita)) * 100.0
    warnings = []
    if sp <= 0 or sp > 60.0:
        # Physics sanity: spessore fuori range plausibile → materiale probabilmente
        # sbagliato. Provo con altri materiali per suggerire il corretto.
        suggested = _cross_material_suggest(peso_kg, area_dm2)
        return {
            'spessore_mm': None, 'confidence': 0.0, 'source': 'none',
            'warnings': [
                f'spessore calcolato {sp:.1f}mm fuori range plausibile (0.5-30mm)',
                *([f'materiale probabilmente {suggested}, non {mat_key}'] if suggested else []),
            ],
            'possibili_materiali': suggested,
        }
    # Arrotonda ai valori commerciali standard più vicini
    nearest = min(STD_SPESSORI_MM, key=lambda s: abs(s - sp))
    err_pct = abs(nearest - sp) / nearest * 100
    # Confidence in base a errore vs valore commerciale
    if err_pct < 5:
        conf = 0.95
    elif err_pct < 15:
        conf = 0.80
        warnings.append(f'spessore calcolato {sp:.2f}mm → arrotondato a {nearest}mm (err {err_pct:.0f}%)')
    else:
        conf = 0.55
        warnings.append(
            f'spessore calcolato {sp:.2f}mm dista {err_pct:.0f}% dal valore commerciale {nearest}mm — verificare materiale o peso'
        )
        # Cross-material check: c'è un altro materiale che spiega meglio il peso?
        suggested = _cross_material_suggest(peso_kg, area_dm2, exclude=mat_key)
        if suggested:
            warnings.append(f'materiale potrebbe essere {suggested} invece di {mat_key}')

    return {
        'spessore_mm': round(nearest, 2),
        'spessore_calc_raw': round(sp, 3),
        'confidence': conf,
        'source': 'peso_area',
        'peso_kg': peso_kg,
        'area_dm2': area_dm2,
        'densita': densita,
        'errore_std_pct': round(err_pct, 1),
        'warnings': warnings,
    }


def _cross_material_suggest(peso_kg: float, area_dm2: float,
                             exclude: str = '') -> str | None:
    """Physics sanity: quale materiale (tra i 5 noti) spiega meglio il peso
    dato, assumendo che lo spessore sia un valore commerciale standard?

    Uso: se il calcolo con materiale scelto dà spessore assurdo, controllo
    se un altro materiale porta a uno spessore commerciale plausibile.
    Restituisce il codice del miglior candidato oppure None.
    """
    if not peso_kg or peso_kg <= 0 or not area_dm2 or area_dm2 <= 0:
        return None
    exclude_upper = (exclude or '').upper()
    best_score = float('inf')
    best_mat = None
    for mat, densita in _DENSITA_STD.items():
        if mat == exclude_upper:
            continue
        sp = (peso_kg / (area_dm2 * densita)) * 100.0
        if sp <= 0 or sp > 30.0:
            continue
        nearest = min(STD_SPESSORI_MM, key=lambda s: abs(s - sp))
        err = abs(nearest - sp) / nearest * 100.0
        # Score = err% (più basso = migliore match con valore commerciale)
        if err < best_score and err < 10.0:  # solo se plausibile
            best_score = err
            best_mat = mat
    return best_mat


def estrai_spessore_da_cartiglio(path: str, area_dm2: float | None = None,
                                  materiale: str | None = None,
                                  area_incerta: bool = False) -> dict:
    """Estrae/calcola lo spessore lamiera con la migliore strategia disponibile.

    Priorità:
    1. PESO + AREA + MATERIALE → calcolo fisico (più affidabile).
       Il peso si estrae dal cartiglio con pattern univoco "Peso kg".
    2. Testo con spessore ESPLICITAMENTE etichettato: "SP. 3", "SP=3", "S=2",
       "SPESSORE 1,5", "sp3", "thk 2" (mai numeri nudi o "3 mm" senza etichetta:
       la vecchia ricerca generica matchava smussi, tolleranze, quote). Se il
       cartiglio riporta spessori diversi tra loro → ambiguo, ignorato.
    3. Fallback: nome file (`_sp3`, `_10mm`).
    Le fonti disponibili sono confrontate da `scegli_spessore`: se discordano
    vince la più affidabile (testo etichettato > peso/area > nome file) e il
    risultato porta un warning con tutti i valori.

    Returns:
        {spessore_mm: float|None, confidence: 0..1,
         source: 'peso_area'|'testo'|'filename'|'none',
         details: dict con peso_kg, densita, errore_std_pct, ecc. per debug UI}
    """
    candidati = []
    avvisi = []
    peso_info = estrai_peso_da_cartiglio(path)
    peso = peso_info.get('peso_kg')

    if peso and area_dm2 and materiale:
        sp_info = stima_spessore_da_peso(peso, area_dm2, materiale)
        if sp_info.get('spessore_mm'):
            # Confidence finale = min(peso, calc) — se peso incerto abbassa
            conf = min(peso_info['confidence'], sp_info['confidence'])
            candidati.append({
                'spessore_mm': sp_info['spessore_mm'],
                'confidence': round(conf, 2),
                'source': 'peso_area',
                'warnings': sp_info.get('warnings', []),  # esposto per UI traffic-light
                'details': {
                    'peso_kg': peso,
                    'peso_source': peso_info['source'],
                    'peso_raw': peso_info['peso_raw'],
                    'area_dm2': area_dm2,
                    'materiale': materiale,
                    'densita_kg_dm3': sp_info['densita'],
                    'spessore_calc_raw': sp_info['spessore_calc_raw'],
                    'errore_std_pct': sp_info['errore_std_pct'],
                },
            })
        elif sp_info.get('warnings'):
            # Stima non riuscita: propago i warning (es. cross-material suggest)
            avvisi.extend(sp_info['warnings'])

    # Spessore scritto esplicitamente nel cartiglio/descrizione
    candidati.append(_spessore_da_testi(path))
    # Nome file
    candidati.append(_spessore_from_filename(path))

    scelto = scegli_spessore(candidati, area_incerta=area_incerta)
    if avvisi:
        scelto = {**scelto, 'warnings': list(scelto.get('warnings') or []) + avvisi}
        if not scelto.get('spessore_mm'):
            scelto['details'] = {'peso_kg': peso, 'area_dm2': area_dm2, 'materiale': materiale}
    return scelto


# Affidabilità delle fonti di spessore (0 = più affidabile). Regola usata quando
# due fonti DISCORDANO: testo con etichetta esplicita ("sp.3", descrizione
# "45x12 sp.3") > verifica fisica peso/area > nome file > cartiglio tabellare
# (valore letto in una cella vicina alla label, non nello stesso testo).
_RANGO_SPESSORE = {
    'testo': 0, 'cartiglio_descrizione': 0,
    'peso_area': 1,
    'filename': 2,
    'cartiglio_tabellare': 3,
}
_TOL_CONCORDANZA_SP = 0.10   # entro il 10% le fonti concordano


def scegli_spessore(candidati: list, area_incerta: bool = False) -> dict:
    """Sceglie lo spessore tra più fonti ({spessore_mm, confidence, source, ...}).

    - Fonti concordi (entro 10%): vince la più affidabile, con la confidence
      più alta tra le concordi.
    - Fonti discordi: vince la più affidabile secondo _RANGO_SPESSORE e si
      aggiunge un warning con TUTTI i valori (niente più sostituzioni silenziose).
    - area_incerta=True (pezzo da selezionare a mano): la stima peso/area dipende
      da un'area dubbia → scende in fondo alla classifica.
    """
    validi = [c for c in candidati if c and c.get('spessore_mm')]
    if not validi:
        return {'spessore_mm': None, 'confidence': 0.0, 'source': 'none', 'details': {}}

    def _rango(c):
        src = str(c.get('source') or '').split('+')[0]
        r = _RANGO_SPESSORE.get(src, 4)
        if area_incerta and src == 'peso_area':
            r = 5
        return r

    validi.sort(key=lambda c: (_rango(c), -(c.get('confidence') or 0)))
    best = dict(validi[0])
    v0 = float(best['spessore_mm'])
    concordi = [c for c in validi
                if abs(float(c['spessore_mm']) - v0) <= _TOL_CONCORDANZA_SP * max(v0, 1e-6)]
    discordi = [c for c in validi if not any(c is k for k in concordi)]
    best['confidence'] = max(c.get('confidence') or 0 for c in concordi)
    warnings = list(best.get('warnings') or [])
    if discordi:
        altri = ', '.join(f"{c['spessore_mm']} mm ({c.get('source')})" for c in discordi)
        warnings.append(f"Spessore discordante: {v0:g} mm ({best.get('source')}) vs {altri} — usato {v0:g} mm")
        best['alternative'] = [{'spessore_mm': c['spessore_mm'], 'source': c.get('source'),
                                'confidence': c.get('confidence')} for c in discordi]
    best['warnings'] = warnings
    return best


def _spessore_da_testi(path: str) -> dict | None:
    """Spessore da testi con etichetta esplicita (RX_INTERNAL_SP). None se
    assente o se i testi riportano spessori diversi (ambiguo)."""
    valori = {}
    for _x, _y, t in _raccogli_testi_dxf(path):
        if len(t) > 200:
            continue
        for m in RX_INTERNAL_SP.finditer(t):
            try:
                v = _num_it(m.group(1))
            except ValueError:
                continue
            if 0.3 <= v <= 30:
                valori.setdefault(round(v, 2), t)
    if len(valori) != 1:
        return None
    v, raw = next(iter(valori.items()))
    return {'spessore_mm': v, 'confidence': 0.8, 'source': 'testo',
            'details': {'raw': raw}}


def _spessore_from_filename(path: str) -> dict:
    """Cerca spessore nel nome file: '_sp3', '_10mm', 'sp.3', 'sp3.0'."""
    import re
    import os
    name = os.path.basename(path)
    patterns = [
        re.compile(r'[_\-\s]sp\.?[_\-\s]?(\d+[.,]?\d*)\s*(?:mm)?', re.IGNORECASE),
        re.compile(r'[_\-\s](\d+[.,]?\d*)\s*mm(?=[_\-\.]|$)', re.IGNORECASE),
    ]
    for rx in patterns:
        m = rx.search(name)
        if m:
            try:
                v = float(m.group(1).replace(',', '.'))
                if 0.3 <= v <= 30.0:
                    return {'spessore_mm': v, 'confidence': 0.75,
                            'source': 'filename', 'details': {'raw': m.group(0)}}
            except ValueError:
                pass
    return {'spessore_mm': None, 'confidence': 0.0,
            'source': 'none', 'details': {}}


def dxf_to_svg_string(path: str) -> str:
    """Converte un DXF in stringa SVG ad alta fedeltà via ezdxf SVGBackend.

    Rendering completo: colori, spessori, archi, spline, polyline complesse —
    qualunque entità DXF supportata dal `Frontend` di ezdxf viene riprodotta
    fedelmente. Usato dalla preview interattiva in `preview-dxf.html`.

    Il viewBox del SVG risultante è impostato a partire dal bbox reale del DXF,
    così che il frontend possa fare screen→DXF con una semplice trasformazione
    affine (viewBox coord = mm DXF, a meno del flip Y-up→Y-down).
    """
    from ezdxf.addons.drawing import Frontend, RenderContext
    from ezdxf.addons.drawing.svg import SVGBackend
    from ezdxf.addons.drawing import layout
    from ezdxf.addons.drawing.config import Configuration, BackgroundPolicy, ColorPolicy
    from ezdxf.bbox import extents

    doc = ezdxf.readfile(path)
    msp = doc.modelspace()

    # Tema chiaro: sfondo BIANCO + colori scuri (leggibili nel thumbnail).
    # ColorPolicy.MONOCHROME_LIGHT_BG converte tutti i colori in scuri.
    # Prima usavamo BackgroundPolicy.DEFAULT che dava sfondo nero + linee bianche
    # → poco leggibile nel thumbnail 150x100 (i pezzi sembravano macchie nere).
    cfg = Configuration(
        background_policy=BackgroundPolicy.WHITE,
        color_policy=ColorPolicy.MONOCHROME_LIGHT_BG,
    )
    backend = SVGBackend()
    ctx = RenderContext(doc)
    frontend = Frontend(ctx, backend, config=cfg)
    frontend.draw_layout(msp)

    # Passiamo dimensioni pagina esatte in mm (senza margini) così il viewBox
    # SVG mappa 1:1 alla bbox DXF. Se il bbox non è disponibile (DXF vuoto),
    # fallback a layout auto (Page(0,0)).
    try:
        bb = extents(msp)
        if bb.has_data:
            w_mm = float(bb.extmax.x - bb.extmin.x)
            h_mm = float(bb.extmax.y - bb.extmin.y)
            if w_mm > 0 and h_mm > 0:
                page = layout.Page(
                    w_mm, h_mm,
                    units=layout.Units.mm,
                    margins=layout.Margins.all(0),
                )
                return backend.get_string(page)
    except Exception:
        pass
    return backend.get_string(layout.Page(0, 0))


def estrai_geometria_taglio(path: str, config: dict | None = None) -> dict:
    """Estrae area, perimetro_taglio e n_forature da DXF per stima costo laser.

    Dispatcher v1/v2 via config flag `dxf_scanner_version` (default 'v2').

    - **v2** (default): polygon detection vero (algoritmo CAM standard).
      Identifica outer/inner contours, filtra cartiglio per formati ISO + cornici
      rettangolari grandi, sceglie outer come "poligono con più CIRCLE contenuti".
      Errore tipico <10% su DXF Lantek puliti.

    - **v1** (legacy): euristica bbox + cluster densità. Errore medio 38%.
      Fallback se v2 non rileva geometria (es. DXF molto scadenti).

    Entrambi restituiscono ora una `confidence` ESPLICITA (prima mancava → il
    chiamante la leggeva come 0 e trattava sempre il risultato come debole).

    Returns:
        Dict con campi compatibili tra v1 e v2:
        {area_dm2, perimetro_taglio_m, n_forature (pierce = 1 + fori),
         bbox_width_mm, bbox_height_mm, tipo_disegno, confidence,
         confidence_label, needs_manual_select, ...}.
    """
    cfg = config or {}
    version = cfg.get('dxf_scanner_version', 'v2')

    if version == 'v2':
        try:
            r = _detect_v2(path, cfg)
            # Se v2 ha trovato geometria valida, usa il suo risultato
            if r and r.get('area_dm2', 0) > 0:
                # Compatibilità con vecchio schema (alias n_pierce → n_forature)
                if 'n_pierce' in r and 'n_forature' not in r:
                    r['n_forature'] = r['n_pierce']
                # Garantisci tutti i campi attesi da chi consuma estrai_geometria_taglio
                r.setdefault('n_polyline_chiuse', r.get('poligoni_grezzi', 0))
                r.setdefault('area_mm2_raw', round(r['area_dm2'] * 10000.0, 2))
                r.setdefault('zona_pezzo_filtered', True)
                r.setdefault('confidence', 0.6)
                r.setdefault('confidence_label', 'media' if r['confidence'] >= 0.6 else 'bassa')
                r.setdefault('needs_manual_select', r['confidence'] < 0.6)
                return r
            logger.warning("dxf_scanner v2 ha restituito area=0 per %s — fallback v1", os.path.basename(path))
        except Exception as e:
            logger.warning("dxf_scanner v2 errore su %s: %s — fallback v1", os.path.basename(path), e)
        # Fallthrough a v1

    return _estrai_geometria_v1(path, cfg)


def _estrai_geometria_v1(path: str, cfg: dict) -> dict:
    """v1 (legacy euristica): zona pezzo per cluster di densità / zona pieghe.

    Correzioni rispetto all'originale:
    - entità duplicate contate una volta sola (prima il perimetro raddoppiava)
    - area NETTA quando esiste un contorno chiuso che copre la zona pezzo
      (contorno − fori); altrimenti area del bbox MENO i fori, segnalata come stima
    - confidence sempre BASSA (0.35 contorno / 0.2 bbox) + needs_manual_select
    - blocchi espansi, colori effettivi, misure in mm ($INSUNITS)
    - n_forature = pierce (1 contorno esterno + fori), come v2/v3
    """
    colori_esclusi = set(cfg.get('dxf_colori_piega', [2])) | set(cfg.get('dxf_colori_saldatura', [1]))
    colori_piega = set(cfg.get('dxf_colori_piega', [2]))
    vuoto = {'area_dm2': 0.0, 'perimetro_taglio_m': 0.0, 'n_forature': 0,
             'n_polyline_chiuse': 0, 'area_mm2_raw': 0.0,
             'bbox_width_mm': 0.0, 'bbox_height_mm': 0.0,
             'confidence': 0.0, 'confidence_label': 'nessuna', 'needs_manual_select': True}

    try:
        doc = ezdxf.readfile(path)
    except Exception as e:
        logger.warning('estrai_geometria_taglio: impossibile aprire %s: %s', path, e)
        return dict(vuoto)
    msp = doc.modelspace()
    f, w_unita = _scala_unita_mm(doc)
    from .dxf_polygon_detector_v3 import _layer_da_escludere as _layer_escluso

    # Ignora annotazioni DIMENSION/MTEXT/TEXT (sono testo, non geometria di taglio);
    # gli INSERT sono espansi da _entita_espanse
    TIPI_ANNOTAZIONE = {'DIMENSION', 'MTEXT', 'TEXT', 'ATTRIB', 'ATTDEF', 'LEADER', 'MULTILEADER', 'HATCH'}

    def _len_line(x1, y1, x2, y2):
        return math.hypot(x2 - x1, y2 - y1)

    def _shoelace_area(verts):
        n = len(verts)
        if n < 3:
            return 0.0
        a = 0.0
        for i in range(n):
            x1, y1 = verts[i][0], verts[i][1]
            x2, y2 = verts[(i + 1) % n][0], verts[(i + 1) % n][1]
            a += x1 * y2 - x2 * y1
        return abs(a) / 2.0

    def _perim_polyline(verts, closed=False):
        if len(verts) < 2:
            return 0.0
        p = 0.0
        for i in range(len(verts) - 1):
            p += _len_line(verts[i][0], verts[i][1], verts[i + 1][0], verts[i + 1][1])
        if closed:
            p += _len_line(verts[-1][0], verts[-1][1], verts[0][0], verts[0][1])
        return p

    def _verts_path(entity):
        """Vertici (mm) dal path ezdxf: gestisce bulge, spline, OCS."""
        try:
            from ezdxf.path import make_path
            return [(v.x * f, v.y * f) for v in make_path(entity).flattening(0.2 / f)]
        except Exception:
            return []

    # === Raccolta entità di taglio (dedup) ===
    entita = []   # (kind, cx, cy, dati)
    firme = set()
    pieghe_pts = []
    for entity in _entita_espanse(msp, solo_geometria=True):
        et = entity.dxftype()
        if et in TIPI_ANNOTAZIONE:
            continue
        try:
            layer = entity.dxf.layer
        except AttributeError:
            layer = ''
        colore = _colore_effettivo(entity)
        if et == 'LINE' and (colore in colori_piega or _layer_piega(layer)):
            try:
                s, e = entity.dxf.start, entity.dxf.end
                pieghe_pts.append(((s.x + e.x) / 2 * f, (s.y + e.y) / 2 * f))
            except Exception:
                pass
        if colore in colori_esclusi or _layer_escluso(layer):
            continue
        try:
            if et == 'LINE':
                s, e = entity.dxf.start, entity.dxf.end
                a, b = (s.x * f, s.y * f), (e.x * f, e.y * f)
                firma = ('L',) + tuple(round(v, 2) for p in sorted([a, b]) for v in p)
                dati = (a[0], a[1], b[0], b[1])
                cx, cy = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
                kind = 'LINE'
            elif et == 'CIRCLE':
                c = entity.ocs().to_wcs(entity.dxf.center)
                r = float(entity.dxf.radius) * f
                cx, cy = c.x * f, c.y * f
                firma = ('C', round(cx, 2), round(cy, 2), round(r, 2))
                dati = (cx, cy, r)
                kind = 'CIRCLE'
            elif et in ('ARC', 'LWPOLYLINE', 'POLYLINE', 'SPLINE', 'ELLIPSE'):
                verts = _verts_path(entity)
                if len(verts) < 2:
                    continue
                closed = _entita_chiusa_scanner(entity) or (
                    len(verts) >= 3 and _len_line(verts[0][0], verts[0][1], verts[-1][0], verts[-1][1]) < 0.1)
                firma = ('P', len(verts)) + tuple(round(v, 2) for p in sorted([verts[0], verts[-1], verts[len(verts) // 2]]) for v in p)
                dati = (verts, closed)
                cx = sum(v[0] for v in verts) / len(verts)
                cy = sum(v[1] for v in verts) / len(verts)
                kind = 'POLY'
            else:
                continue
        except Exception:
            continue
        if firma in firme:
            continue  # entità duplicata (stesso tratto disegnato due volte)
        firme.add(firma)
        entita.append((kind, cx, cy, dati))

    # Testi piega (verso + angolo) → pezzo SVILUPPATO
    for entity in _entita_espanse(msp, solo_geometria=False):
        if entity.dxftype() in ('TEXT', 'MTEXT') and RE_PIEGA_TESTO.match(_testo_entita(entity).upper()):
            pt = _punto_testo(entity)
            if pt is not None:
                pieghe_pts.append((pt[0] * f, pt[1] * f))

    centroidi_xs = [e[1] for e in entita]
    centroidi_ys = [e[2] for e in entita]

    # === IDENTIFICAZIONE ZONA PEZZO ===
    # Due tipi di DXF cliente:
    # 1. SVILUPPATO: pezzo in lamiera con linee di piega marcate (testi SU/GIU
    #    o linee in COLOR 2). Il pezzo è dove ci sono le pieghe → bbox piega
    # 2. A VISTE: disegno tecnico con front/top/side. Niente pieghe → fallback
    #    cluster densità (prende la vista più ricca di entità = principale)
    is_sviluppato = len(pieghe_pts) > 0
    zona_pezzo_bbox = None

    if is_sviluppato:
        pxs = [p[0] for p in pieghe_pts]
        pys = [p[1] for p in pieghe_pts]
        zx_min, zx_max = min(pxs), max(pxs)
        zy_min, zy_max = min(pys), max(pys)
        w_p = zx_max - zx_min
        h_p = zy_max - zy_min
        # Margine misto: 100mm assoluti OPPURE 50% dimensione bbox piega (il max)
        margine_x = max(100.0, w_p * 0.5)
        margine_y = max(100.0, h_p * 0.5)
        zona_pezzo_bbox = (zx_min - margine_x, zx_max + margine_x,
                           zy_min - margine_y, zy_max + margine_y)

    # Cluster densità SOLO per pezzi a viste (no pieghe)
    if zona_pezzo_bbox is None and len(centroidi_xs) >= 10:
        x_min_tot, x_max_tot = min(centroidi_xs), max(centroidi_xs)
        y_min_tot, y_max_tot = min(centroidi_ys), max(centroidi_ys)
        N_BINS = 20
        cell_w = max(1e-6, (x_max_tot - x_min_tot) / N_BINS)
        cell_h = max(1e-6, (y_max_tot - y_min_tot) / N_BINS)
        grid = {}
        for cx, cy in zip(centroidi_xs, centroidi_ys):
            ix = min(N_BINS - 1, max(0, int((cx - x_min_tot) / cell_w)))
            iy = min(N_BINS - 1, max(0, int((cy - y_min_tot) / cell_h)))
            grid[(ix, iy)] = grid.get((ix, iy), 0) + 1
        if grid:
            max_cell = max(grid, key=grid.get)
            max_count = grid[max_cell]
            threshold = max(1, max_count * 0.20)  # 20% — isola vista più densa
            # BFS espansione greedy
            visited = {max_cell}
            queue = [max_cell]
            while queue:
                ix, iy = queue.pop(0)
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        if dx == 0 and dy == 0:
                            continue
                        nx, ny = ix + dx, iy + dy
                        if 0 <= nx < N_BINS and 0 <= ny < N_BINS and (nx, ny) not in visited:
                            if grid.get((nx, ny), 0) >= threshold:
                                visited.add((nx, ny))
                                queue.append((nx, ny))
            cxs = [c[0] for c in visited]
            cys = [c[1] for c in visited]
            bx_min = x_min_tot + min(cxs) * cell_w
            bx_max = x_min_tot + (max(cxs) + 1) * cell_w
            by_min = y_min_tot + min(cys) * cell_h
            by_max = y_min_tot + (max(cys) + 1) * cell_h
            # Margine cella per non tagliare entità di bordo
            zona_pezzo_bbox = (bx_min - cell_w * 0.5, bx_max + cell_w * 0.5,
                               by_min - cell_h * 0.5, by_max + cell_h * 0.5)

    if zona_pezzo_bbox:
        x_min_z, x_max_z, y_min_z, y_max_z = zona_pezzo_bbox

        def _in_pezzo(x, y):
            return x_min_z <= x <= x_max_z and y_min_z <= y <= y_max_z
    else:
        def _in_pezzo(x, y):
            return True

    # Itera le entità collezionate, conta solo quelle nella zona pezzo
    perimetro_mm = 0.0
    cerchi = []
    chiusi = []   # (area, verts) polilinee chiuse
    xs_real, ys_real = [], []  # bbox effettivo delle entità nel pezzo
    for kind, cx, cy, dati in entita:
        if not _in_pezzo(cx, cy):
            continue
        if kind == 'LINE':
            x1, y1, x2, y2 = dati
            perimetro_mm += _len_line(x1, y1, x2, y2)
            xs_real += [x1, x2]; ys_real += [y1, y2]
        elif kind == 'CIRCLE':
            cxx, cyy, r = dati
            perimetro_mm += 2 * math.pi * r
            cerchi.append(dati)
            xs_real += [cxx - r, cxx + r]; ys_real += [cyy - r, cyy + r]
        else:
            verts, closed = dati
            perimetro_mm += _perim_polyline(verts, closed)
            if closed and len(verts) >= 3:
                chiusi.append((_shoelace_area(verts), verts))
            for vx, vy in verts:
                xs_real.append(vx); ys_real.append(vy)

    if not xs_real:
        return {**vuoto, 'warnings': ([w_unita] if w_unita else []) + ['Nessuna geometria di taglio rilevata']}

    bbox_w_mm = max(xs_real) - min(xs_real)
    bbox_h_mm = max(ys_real) - min(ys_real)
    area_bbox = bbox_w_mm * bbox_h_mm
    area_fori = sum(math.pi * r * r for _cx, _cy, r in cerchi)
    warnings = [w_unita] if w_unita else []
    # Contorno chiuso che copre (quasi) tutta la zona → area netta vera
    contorno = max(chiusi, key=lambda c: c[0], default=None)
    if contorno and area_bbox > 0 and contorno[0] >= 0.5 * area_bbox:
        interni = sum(a for a, _v in chiusi if a < contorno[0])
        area_mm2 = max(0.0, contorno[0] - interni - area_fori)
        area_source = 'contorno'
        confidence = 0.35
    else:
        # Stima: ingombro del pezzo meno i fori rilevati
        area_mm2 = max(0.0, area_bbox - area_fori)
        area_source = 'bbox'
        confidence = 0.2
        warnings.append('Area stimata dal rettangolo di ingombro (contorno non chiuso): verificare')

    return {
        'area_dm2': round(area_mm2 / 10000.0, 4),       # mm² → dm² (1 dm² = 10000 mm²)
        'perimetro_taglio_m': round(perimetro_mm / 1000.0, 4),  # mm → m
        'n_forature': 1 + len(cerchi),                   # pierce: contorno esterno + fori
        'n_fori': len(cerchi),
        'n_polyline_chiuse': len(chiusi),
        'area_mm2_raw': round(area_mm2, 2),
        'area_source': area_source,
        'bbox_width_mm': round(bbox_w_mm, 2),
        'bbox_height_mm': round(bbox_h_mm, 2),
        'zona_pezzo_filtered': zona_pezzo_bbox is not None,
        'tipo_disegno': 'sviluppato' if is_sviluppato else 'a_viste',
        'confidence': confidence,
        'confidence_label': 'bassa',
        'needs_manual_select': True,
        'warnings': warnings,
    }


def dxf_to_segments(path: str) -> tuple[list, list, list]:
    """Legge un file DXF e restituisce (segments, xs, ys).

    segments: lista di (x1, y1, x2, y2) in mm per LINE, POLYLINE, CIRCLE, ARC, SPLINE.
    xs, ys: liste di coordinate (mm) per calcolo bounding box.
    Blocchi di geometria (INSERT) espansi, unità da $INSUNITS, come nel resto
    dell'estrazione.
    Solleva eccezione se il file non è leggibile.
    """
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    f = _scala_unita_mm(doc)[0]
    segments = []
    xs = []
    ys = []

    for entity in _entita_espanse(msp, solo_geometria=True):
        et = entity.dxftype()
        if et == 'LINE':
            try:
                x1, y1 = entity.dxf.start.x, entity.dxf.start.y
                x2, y2 = entity.dxf.end.x, entity.dxf.end.y
                segments.append((x1, y1, x2, y2))
                xs += [x1, x2]
                ys += [y1, y2]
            except Exception:
                continue

        elif et in ('LWPOLYLINE', 'POLYLINE'):
            pts = []
            try:
                pts = list(entity.get_points())
            except Exception:
                try:
                    pts = list(entity.points())
                except Exception:
                    try:
                        pts = [v.dxf.location for v in entity.vertices()]
                    except Exception:
                        pts = []

            cleaned = []
            for p in pts:
                if p is None:
                    continue
                if hasattr(p, 'x') and hasattr(p, 'y'):
                    cleaned.append((float(p.x), float(p.y)))
                elif isinstance(p, (tuple, list)) and len(p) >= 2:
                    cleaned.append((float(p[0]), float(p[1])))
            # polilinea chiusa: anche il lato di chiusura (prima mancava)
            try:
                chiusa = bool(entity.closed) if et == 'LWPOLYLINE' else bool(getattr(entity, 'is_closed', False))
            except Exception:
                chiusa = False
            if chiusa and len(cleaned) >= 3 and cleaned[0] != cleaned[-1]:
                cleaned.append(cleaned[0])

            for a, b in zip(cleaned, cleaned[1:]):
                x1, y1 = a
                x2, y2 = b
                segments.append((x1, y1, x2, y2))
                xs += [x1, x2]
                ys += [y1, y2]

        elif et == 'CIRCLE':
            try:
                c = entity.dxf.center
                r = float(entity.dxf.radius)
                for i in range(24):
                    a1 = 2 * math.pi * i / 24
                    a2 = 2 * math.pi * (i + 1) / 24
                    x1 = c.x + r * math.cos(a1)
                    y1 = c.y + r * math.sin(a1)
                    x2 = c.x + r * math.cos(a2)
                    y2 = c.y + r * math.sin(a2)
                    segments.append((x1, y1, x2, y2))
                    xs += [x1, x2]
                    ys += [y1, y2]
            except Exception:
                continue

        elif et == 'ARC':
            try:
                c = entity.dxf.center
                r = float(entity.dxf.radius)
                start_angle = math.radians(float(entity.dxf.start_angle))
                end_angle = math.radians(float(entity.dxf.end_angle))
                num_segments = 24
                angle_range = end_angle - start_angle
                if angle_range < 0:
                    angle_range += 2 * math.pi
                for i in range(num_segments):
                    a1 = start_angle + angle_range * i / num_segments
                    a2 = start_angle + angle_range * (i + 1) / num_segments
                    x1 = c.x + r * math.cos(a1)
                    y1 = c.y + r * math.sin(a1)
                    x2 = c.x + r * math.cos(a2)
                    y2 = c.y + r * math.sin(a2)
                    segments.append((x1, y1, x2, y2))
                    xs += [x1, x2]
                    ys += [y1, y2]
            except Exception:
                continue

        elif et == 'SPLINE':
            try:
                points = list(entity.flattening(0.1))
                for i in range(len(points) - 1):
                    p1 = points[i]
                    p2 = points[i + 1]
                    x1, y1 = float(p1[0]), float(p1[1])
                    x2, y2 = float(p2[0]), float(p2[1])
                    segments.append((x1, y1, x2, y2))
                    xs += [x1, x2]
                    ys += [y1, y2]
            except Exception:
                continue

    if f != 1.0:
        segments = [(a * f, b * f, c * f, d * f) for a, b, c, d in segments]
        xs = [v * f for v in xs]
        ys = [v * f for v in ys]
    return segments, xs, ys


def crea_mappatura_dxf(dxf_paths: list) -> dict:
    """Crea una mappa {basename_lower: [paths]} dai file DXF selezionati."""
    m = {}
    for p in dxf_paths:
        base = os.path.splitext(os.path.basename(p))[0].lower()
        # conserviamo liste per gestire eventuali omonimi
        m.setdefault(base, []).append(p)
    return m


def trova_dxf_per_codice(codice: str, dxf_map: dict) -> str | None:
    """Tenta di trovare il miglior percorso DXF per `codice` nella mappa.

    Strategie (in ordine):
    - match esatto base==codice
    - match dove codice è substring del basename
    - match dove basename è substring del codice
    Restituisce path string o None.
    """
    key = codice.lower()

    # 1) match esatto
    if key in dxf_map:
        paths = dxf_map[key]
        return paths[0] if paths else None

    # 2) codice è substring del basename
    for base, paths in dxf_map.items():
        if key in base:
            return paths[0]

    # 3) basename è substring del codice
    for base, paths in dxf_map.items():
        if base in key:
            return paths[0]

    return None
