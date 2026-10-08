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
import re
from typing import Any

import ezdxf
from ezdxf.path import make_path

try:
    from shapely.geometry import Polygon, Point, MultiPolygon
    from shapely.geometry.polygon import orient
    from shapely.prepared import prep
    from shapely.validation import make_valid
    import numpy as _np
    import shapely as _shp
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
    'marcatur',   # layer MARCATURA del DXF pulito per Lantek (incisioni, loghi)
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
    # gruppi di quote/annotazioni esportati come blocchi con nome (Solid Edge:
    # GDIMGGROUP = quote, SEANNOT_GROUP = assi/annotazioni). Espansi come
    # geometria, le linee di richiamo delle quote chiudevano finte "falde"
    # attaccate al pezzo (1890400210: +3 falde, 12,4 dm2 invece di 9,6).
    # Non 'dim' da solo: 'DIMA' e' un nome di pezzo.
    'gdim', 'dimens', 'annot', 'quot',
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
_POLLICI_MAX_MM = 1600.0

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
MIN_LATO_PEZZO_MM = 3.0              # pezzo laser piu' stretto = vista di fianco (spessore)
MAX_FALDA_REL = 25.0                 # falda/regione unita al massimo 25 volte il contorno scelto

# Scritte incise disegnate come geometria (vedi _separa_scritte)
SCRITTA_MAX_MM = 15.0                # lettera: lato maggiore al massimo
SCRITTA_MIN_LETTERE = 2              # una fila di almeno 2 contorni piccoli
SCRITTA_MIN_CONCAVI = 2              # di cui almeno 2 a forma di lettera (concave o ovali), 1 concava
SCRITTA_SPAZIO = 2.0                 # lettere vicine: distanza <= 2 volte l'altezza
SCRITTA_LATO_DRITTO = 0.25           # ovale "da lettera": nessun lato dritto >= 25% del lato maggiore
SEGNO_MAX_MM = 10.0                  # segno isolato (lettera/cifra sola): lato maggiore massimo
SEGNO_CONCAVO = 0.8                  # ...e area/inviluppo convesso sotto questa soglia
SCRITTA_CONVESSO = 0.97              # area/area dell'inviluppo convesso: >= convesso
SCRITTA_MARCATA_MM = 8.0             # scritte piu' alte: forse tagliate passanti -> revisione
SCRITTA_REL_PEZZO = 0.30             # ...o piu' alte del 30% del lato corto del pezzo (targhette)
SCRITTA_QUOTA_AREA = 0.025           # ...o alte >20% del lato corto e lettere oltre il 2,5% del pezzo
SCRITTA_QUOTA_MAX = 0.10             # ...o lettere che coprono oltre il 10% del pezzo

# Confidence thresholds
CONF_ALTA = 0.85
CONF_MEDIA = 0.6
CONF_BASSA = 0.3
# Pezzo giusto ma fori dubbi (scritta forse tagliata...): sotto lo 0.7 del
# "pulito auto" -> revisione, ma senza selezione manuale del contorno
CONF_FORI_DUBBI = 0.65


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
        elif u == 1:
            # Pollici: un foglio in pollici (cornice compresa) sta entro ~63"
            # (1,6 m). I disegni dell'archivio che dichiarano pollici con
            # 126-446 unita' sono tutti in mm (Lantek: 7 su 7): 446" = 11 m.
            plausibile = est <= 0 or _ESTENSIONE_MM_PLAUSIBILE[0] <= est * f <= _POLLICI_MAX_MM
        else:
            plausibile = (est <= 0 or
                          _ESTENSIONE_MM_PLAUSIBILE[0] <= est * f <= _ESTENSIONE_MM_PLAUSIBILE[1])
        if not plausibile:
            res = (1.0, f'Il DXF dichiara unità "{nome}" ma le misure ({est:g}) sono '
                        f'plausibili solo in millimetri: assunti millimetri')
        else:
            res = (f, f'Disegno in {nome}: misure convertite in mm (×{f:g})')
    # Scala del DISEGNO (foglio esportato in scala, es. 1:8): le misure nel
    # file sono ridotte, le quote le riportano al vero con DIMLFAC.
    k, avviso_scala = _scala_disegno(doc, res[0])
    if k != 1.0:
        f0, w0 = res
        res = (f0 * k, '; '.join(w for w in (w0, avviso_scala) if w))
    elif avviso_scala:
        res = (res[0], '; '.join(w for w in (res[1], avviso_scala) if w))
    try:
        doc._ft_scala_mm = res
    except Exception:
        pass
    return res


_RE_SCALA_TESTO = re.compile(r'SCALA\s*:?\s*(\d+(?:[.,]\d+)?)\s*:\s*(\d+(?:[.,]\d+)?)', re.I)


def _punti_quota(d) -> list:
    """Punti con cui la quota tocca il disegno (unita' disegno): origini delle
    linee di richiamo per le lineari, centro e punto sul cerchio per le radiali."""
    t = d.dimtype & 7
    nomi = ('defpoint2', 'defpoint3') if t in (0, 1) else ('defpoint', 'defpoint4')
    out = []
    for n in nomi:
        try:
            if d.dxf.hasattr(n):
                p = d.dxf.get(n)
                out.append((float(p[0]), float(p[1])))
        except Exception:
            pass
    return out


_RE_DETTAGLIO = re.compile(r'\b(DETTAGLIO|DETAIL|PARTICOLARE\s+[A-Z]\b)', re.IGNORECASE)


def _etichetta_dettaglio_vicina(doc, bounds, f_unita: float = 1.0) -> bool:
    """C'e' una scritta "DETTAGLIO ..." (SolidWorks: "DETTAGLIO A", sotto la
    vista) dentro o appena attorno al riquadro? Allora e' una vista di
    dettaglio, disegnata in un'altra scala, non il pezzo."""
    x1, y1, x2, y2 = bounds
    m = 0.3 * max(x2 - x1, y2 - y1)
    for t in doc.modelspace().query('TEXT MTEXT'):
        try:
            testo = t.plain_text() if t.dxftype() == 'MTEXT' else t.dxf.text
            if not _RE_DETTAGLIO.search(testo or ''):
                continue
            p = t.dxf.insert
            px, py = float(p[0]) * f_unita, float(p[1]) * f_unita
        except Exception:
            continue
        if x1 - m <= px <= x2 + m and y1 - m <= py <= y2 + m:
            return True
    return False


_SCALA_VISTA_CACHE: dict = {}


def _scala_vista_pezzo(doc, quote: list, f_unita: float) -> tuple[float, str | None]:
    """Come _calcola_scala_vista_pezzo, ma una volta sola per file: rifa' la
    scelta del contorno (secondi sui fogli grandi, es. 26T30SA0328-00 6 s) e
    lo stesso file viene riletto da CAD, anteprima, lavorazioni..."""
    chiave = None
    try:
        fn = getattr(doc, 'filename', None)
        if fn and os.path.exists(fn):
            st = os.stat(fn)
            chiave = (os.path.abspath(fn), st.st_mtime_ns, st.st_size, round(f_unita, 9))
    except Exception:
        chiave = None
    if chiave is not None and chiave in _SCALA_VISTA_CACHE:
        return _SCALA_VISTA_CACHE[chiave]
    res = _calcola_scala_vista_pezzo(doc, quote, f_unita)
    if chiave is not None:
        if len(_SCALA_VISTA_CACHE) > 500:
            _SCALA_VISTA_CACHE.clear()
        _SCALA_VISTA_CACHE[chiave] = res
    return res


def _calcola_scala_vista_pezzo(doc, quote: list, f_unita: float) -> tuple[float, str | None]:
    """Foglio con viste in scale diverse: il fattore delle quote che stanno sul
    contorno del pezzo.

    Esempio 191700612-00 (DECA, SolidWorks): lo sviluppo 30x92 e' disegnato
    2:1 (60x184, le sue quote hanno DIMLFAC=0,5) mentre le viste piegate sono
    1:1. Prima si rinunciava a scalare: area 4 volte, perimetro 2 volte.

    Il contorno si cerca con la stessa pipeline del detector a scala 1 (la
    scelta del pezzo non dipende dalla scala). Si corregge solo se le quote
    che cadono sul pezzo (almeno una: in 191700615-00 le altre misurano la
    vista di fianco, appena fuori) hanno tutte lo stesso fattore; altrimenti
    le misure restano quelle disegnate, con l'avviso."""
    dubbio = (1.0, 'Quote con fattori di scala diversi nello stesso foglio: '
                   'misure lette cosi\' come sono, verificare')
    if not _HAS_SHAPELY:
        return dubbio
    try:
        doc._ft_scala_mm = (f_unita, None)      # provvisoria: solo unita'
        base = _poligoni_documento(doc, {})
        candidati, _n = _separa_cartiglio(base['polys'], base.get('testi'))
        if not candidati:
            return dubbio
        idx, _conf, _sc = _pick_outer_with_confidence(candidati, candidati, base['centri_cerchi'], base.get('quote'), base.get('testi'))
        if idx < 0:
            return dubbio
        x1, y1, x2, y2 = candidati[idx].bounds
        m = max(1.0, 0.02 * max(x2 - x1, y2 - y1))
        sul_pezzo = []
        for v, d in quote:
            pts = _punti_quota(d)
            if pts and all(x1 - m <= px * f_unita <= x2 + m and y1 - m <= py * f_unita <= y2 + m
                           for px, py in pts):
                sul_pezzo.append(v)
        al_vero = bool(sul_pezzo) and all(abs(v - 1.0) < 1e-6 for v in sul_pezzo)
        if not al_vero and _etichetta_dettaglio_vicina(doc, (x1, y1, x2, y2), f_unita):
            # Il contorno scelto e' un DETTAGLIO ingrandito (33PP00086-00: foglio
            # 1:10, "DETTAGLIO A" 10:1 con l'unica quota a DIMLFAC 0,1): la sua
            # scala non e' quella del pezzo. Prima tutto il disegno veniva
            # ridotto di 10 volte e il pezzo non si ritrovava piu'. Vale la
            # scala delle viste principali: quella della maggior parte delle
            # quote (25NDSPA0105-02 esportato 1:6 -> 6, come le altre copie).
            conta = {}
            for v, _d in quote:
                conta[v] = conta.get(v, 0) + 1
            k, n = max(conta.items(), key=lambda kv: kv[1])
            if n < 0.6 * len(quote) or not (0.05 <= k <= 200):
                return dubbio
            if abs(k - 1.0) < 1e-6:
                return 1.0, None
            rapporto = f'1:{k:g}' if k >= 1 else f'{1 / k:g}:1'
            return k, f'Disegno in scala {rapporto}: misure riportate al vero (x{k:g})'
        if not sul_pezzo or any(abs(v - sul_pezzo[0]) > 1e-4 * sul_pezzo[0] for v in sul_pezzo):
            return dubbio
        k = sul_pezzo[0]
        if abs(k - 1.0) < 1e-6:
            return 1.0, None                    # pezzo 1:1, ingranditi solo i dettagli
        if not (0.05 <= k <= 200):
            return dubbio
        rapporto = f'1:{k:g}' if k >= 1 else f'{1 / k:g}:1'
        return k, (f'Vista del pezzo in scala {rapporto} (le altre viste no): '
                   f'misure riportate al vero (x{k:g})')
    except Exception as e:
        logger.debug('scala della vista del pezzo non letta: %s', e)
        return dubbio
    finally:
        try:
            del doc._ft_scala_mm
        except Exception:
            pass


def _segna_scala_dubbia(doc):
    """La scala del disegno e' incerta: il detector non si dira' sicuro."""
    try:
        doc._ft_scala_dubbia = True
    except Exception:
        pass


def _scala_disegno(doc, f_unita: float = 1.0) -> tuple[float, str | None]:
    """Fattore che riporta al vero un disegno esportato in scala.

    Esempio 25NSIPA0015-00: foglio A3 "SCALA:1:8", il pezzo 142x1675 e'
    disegnato 17,76x209,4 e tutte le quote hanno DIMLFAC=8 (moltiplicano la
    misura disegnata per scrivere 1675). Senza questo l'area era 64 volte piu'
    piccola e i fori (Ø6,5 → 0,8 mm) sparivano sotto la soglia minima.

    Si scala SOLO se tutte le quote del foglio hanno lo stesso fattore (viste
    in scale diverse → nessuna correzione) e, se c'e' la scritta "SCALA a:b"
    del cartiglio, questa concorda."""
    try:
        quote = []
        for d in doc.modelspace().query('DIMENSION'):
            try:
                v = float(d.override().get('dimlfac', 1.0) or 1.0)
            except Exception:
                continue
            if v > 0:
                quote.append((round(v, 4), d))
        valori = [v for v, _d in quote]
        if not valori:
            return 1.0, None
        k = valori[0]
        if any(abs(v - k) > 1e-4 * k for v in valori):
            # Viste in scale diverse: conta la scala della vista del pezzo
            return _scala_vista_pezzo(doc, quote, f_unita)
        if abs(k - 1.0) < 1e-6:
            return 1.0, None
        if not (0.05 <= k <= 200):
            return 1.0, None
        # la scritta del cartiglio, se c'e', deve dire la stessa cosa
        confermata = False
        for t in doc.modelspace().query('TEXT MTEXT'):
            try:
                testo = t.plain_text() if t.dxftype() == 'MTEXT' else t.dxf.text
            except Exception:
                continue
            m = _RE_SCALA_TESTO.search(testo or '')
            if not m:
                continue
            a = float(m.group(1).replace(',', '.'))
            b = float(m.group(2).replace(',', '.'))
            if a > 0 and b > 0 and abs(b / a - k) > 0.02 * k:
                # Quote e cartiglio non concordano. Valgono le QUOTE: sono
                # legate alla vista, la scritta "SCALA" e' quella del foglio
                # (archivio: 13 disegni su 14 tornano con Lantek usando il
                # fattore delle quote, 0 leggendo le misure cosi' come sono).
                # Resta da verificare.
                _segna_scala_dubbia(doc)
                rapporto = f'1:{k:g}' if k >= 1 else f'{1 / k:g}:1'
                return k, (f'Quote in scala {rapporto} ma il cartiglio dice "{m.group(0)}": '
                           f'misure riportate al vero con le quote (x{k:g}), verificare')
            confermata = True
            break
        if k < 0.2 and not confermata:
            # Quote che DIVIDONO per 5 o piu' senza "SCALA 10:1" nel cartiglio:
            # di solito e' il disegno in mm quotato in centimetri (DIMLFAC 0,1),
            # non un ingrandimento: un pezzo laser disegnato 10 volte piu'
            # grande e' raro. Si tengono le misure disegnate, da verificare.
            _segna_scala_dubbia(doc)
            return 1.0, (f'Quote con fattore x{k:g} (centimetri?) senza scala nel cartiglio: '
                         'misure lette cosi\' come sono, verificare')
        rapporto = f'1:{k:g}' if k >= 1 else f'{1 / k:g}:1'
        try:
            doc._ft_scala_da_quote = k      # il pezzo scelto dovra' avere quote addosso
        except Exception:
            pass
        return k, f'Disegno in scala {rapporto}: misure riportate al vero (x{k:g})'
    except Exception:
        return 1.0, None


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
        lt_ins = linetype_effettivo(e)
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
                    if str(ve.dxf.get('linetype', 'BYLAYER')).upper() == 'BYBLOCK':
                        ve._ft_lt_blocco = lt_ins
                    if annot:
                        ve._ft_annotazione = True
                except Exception:
                    pass
            yield from entita_espanse(virt, solo_geometria, _depth + 1)


_LINETYPE_CONTINUI = {'', 'CONTINUOUS', 'BYLAYER', 'BYBLOCK'}
_LINETYPE_NOMI_TRATTEGGIATI = ('HIDDEN', 'DASH', 'CENTER', 'PHANTOM', 'DOT', 'CHAIN',
                               'TRATT', 'ASSE', 'BORDER', 'DIVIDE', 'NASCOST')


def linetype_effettivo(entity) -> str:
    """Nome del tipo di linea reale (BYLAYER risolto col layer; BYBLOCK = quello
    dell'INSERT, propagato da entita_espanse)."""
    try:
        lt = str(entity.dxf.get('linetype', 'BYLAYER') or 'BYLAYER')
    except Exception:
        return 'CONTINUOUS'
    u = lt.upper()
    if u == 'BYBLOCK':
        return str(getattr(entity, '_ft_lt_blocco', 'CONTINUOUS') or 'CONTINUOUS')
    if u == 'BYLAYER':
        try:
            lay = entity.doc.layers.get(entity.dxf.get('layer', '0'))
            return str(lay.dxf.get('linetype', 'CONTINUOUS') or 'CONTINUOUS')
        except Exception:
            return 'CONTINUOUS'
    return lt


def linea_tratteggiata(entity) -> bool:
    """True se l'entita' e' disegnata con un tipo di linea a tratti (nascosta,
    asse, fantasma, tratteggio): nel disegno tecnico queste linee sono spigoli
    nascosti, assi, linee di piega o ingombri, mai il profilo da tagliare, che
    e' sempre a linea continua."""
    nome = linetype_effettivo(entity)
    u = nome.strip().upper()
    if u in _LINETYPE_CONTINUI:
        return False
    doc = getattr(entity, 'doc', None)
    cache = getattr(doc, '_ft_lt_tratti', None) if doc is not None else None
    if cache is None:
        cache = {}
        try:
            doc._ft_lt_tratti = cache
        except Exception:
            pass
    if u in cache:
        return cache[u]
    esito = None
    try:
        lt = doc.linetypes.get(nome)
        tags = [t for t in lt.pattern_tags.tags if t.code == 49]
        # un elemento negativo = un vuoto nel tratto
        esito = any(float(t.value) < 0 for t in tags)
    except Exception:
        esito = None
    if esito is None:
        esito = any(k in u for k in _LINETYPE_NOMI_TRATTEGGIATI)
    cache[u] = esito
    return esito


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

def _extract_polygons(msp, colori_esclusi: set[int], scala: float = 1.0,
                      escludi_tratteggi: bool = True) -> dict:
    """Raccoglie i contorni dal layout (INSERT espansi), già scalati in mm.

    Returns dict:
        chiusi, aperti          — geometria "normale" (list[verts] / list[(s, e, verts)])
        chiusi_col, aperti_col  — entità con colore piega/saldatura (effettivo)
        centri_cerchi           — centri (mm) dei CIRCLE normali (per lo scoring)
    """
    out = {'chiusi': [], 'aperti': [], 'chiusi_col': [], 'aperti_col': [], 'centri_cerchi': [],
           'n_tratteggiati': 0}
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
        # Linee a tratti (nascoste, assi, pieghe, ingombri): non sono profili di
        # taglio (25NDSPA0223-00: il rettangolo d'ingombro a tratto-punto attorno
        # allo sviluppo; 08PA03722-00: spigoli nascosti della vista piegata)
        if escludi_tratteggi and et != 'POINT' and linea_tratteggiata(entity):
            out['n_tratteggiati'] += 1
            continue
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


class _Indice:
    """Area e ingombro di un elenco di contorni, letti una volta sola.

    Sui fogli grandi (26T30SA0328-00: ~700 contorni) i confronti a coppie
    rileggevano area e bounds da GEOS centinaia di migliaia di volte: 4-8 s
    per aprire il disegno nel CAD. Qui il filtro su area e ingombro e' fatto
    su vettori; il contenimento vero resta quello di _contiene."""

    def __init__(self, polys: list):
        self.polys = list(polys)
        arr = _np.array(self.polys, dtype=object) if self.polys else _np.empty(0, dtype=object)
        self.aree = _shp.area(arr) if len(arr) else _np.empty(0)
        self.bb = _shp.bounds(arr) if len(arr) else _np.empty((0, 4))

    def contenuti(self, outer, pp=None) -> list:
        """I contorni dell'indice contenuti in `outer` (come _contiene)."""
        if not self.polys:
            return []
        t = TOL_CONTENIMENTO_MM
        ob = outer.bounds
        b = self.bb
        m = ((self.aree < outer.area * 0.999)
             & (b[:, 0] >= ob[0] - t) & (b[:, 1] >= ob[1] - t)
             & (b[:, 2] <= ob[2] + t) & (b[:, 3] <= ob[3] + t))
        idx = _np.nonzero(m)[0]
        if not len(idx):
            return []
        pp = pp if pp is not None else _prep_buf(outer)
        out = []
        for i in idx:
            o = self.polys[i]
            if o is outer:
                continue
            try:
                if pp.contains(o):
                    out.append(o)
            except Exception:
                pass
        return out

    def contenitori(self, inner, preps: list) -> bool:
        """True se `inner` e' contenuto in almeno un contorno dell'indice
        (preps[i] = _prep_buf dell'i-esimo)."""
        if not self.polys:
            return False
        t = TOL_CONTENIMENTO_MM
        ib = inner.bounds
        b = self.bb
        m = ((inner.area < self.aree * 0.999)
             & (ib[0] >= b[:, 0] - t) & (ib[1] >= b[:, 1] - t)
             & (ib[2] <= b[:, 2] + t) & (ib[3] <= b[:, 3] + t))
        for i in _np.nonzero(m)[0]:
            if self.polys[i] is inner:
                continue
            try:
                if preps[i].contains(inner):
                    return True
            except Exception:
                pass
        return False


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


def _intaglio_d_angolo(poly, o) -> bool:
    """True se `o` e' un piccolo contorno che copre uno spigolo del rettangolo
    `poly`: gli scantonati d'angolo di uno sviluppo (lamiera con bordi piegati)
    sono disegnati cosi', come quadratini sovrapposti agli angoli."""
    if not _foro_tipico(o, poly):
        return False
    t = CORNICE_TOCCO_MM
    minx, miny, maxx, maxy = poly.bounds
    ox0, oy0, ox1, oy1 = o.bounds
    for cx, cy in ((minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy)):
        if ox0 - t <= cx <= ox1 + t and oy0 - t <= cy <= oy1 + t:
            return True
    return False


def applica_intagli(outer, inners: list):
    """Separa dai fori gli INTAGLI: contorni interni che toccano il bordo
    esterno (scantonati d'angolo, tacche disegnate sovrapposte al contorno).
    Non sono fori da forare: si tolgono dal contorno esterno, che cambia forma.
    Ritorna (outer_effettivo, fori, n_intagli). Se la sottrazione non da' un
    poligono unico valido, lascia tutto com'era."""
    if not inners:
        return outer, inners, 0
    bordo = outer.exterior
    intagli = [p for p in inners if _foro_tipico(p, outer) and bordo.distance(p) <= CORNICE_TOCCO_MM]
    if not intagli:
        return outer, inners, 0
    try:
        from shapely.ops import unary_union
        eff = outer.difference(unary_union(intagli))
        if eff.geom_type == 'MultiPolygon':
            eff = max(eff.geoms, key=lambda g: g.area)
            if eff.area < 0.9 * outer.area:
                return outer, inners, 0
        if eff.geom_type != 'Polygon' or not eff.is_valid or eff.is_empty:
            return outer, inners, 0
        eff = Polygon(eff.exterior)   # i fori restano separati in `fori`
    except Exception:
        return outer, inners, 0
    ids = {id(p) for p in intagli}
    return eff, [p for p in inners if id(p) not in ids], len(intagli)


def _tondo(p) -> bool:
    """Contorno circolare (rapporto isoperimetrico >= 0,85)."""
    return p.length > 0 and 4 * math.pi * p.area / (p.length ** 2) >= 0.85


def _foro_svasato(o, contenuti: list) -> bool:
    """`o` e' un cerchio che racchiude solo cerchi concentrici (smusso di una
    svasatura, filetto)?"""
    if not _tondo(o):
        return False
    po = _prep_buf(o)
    dentro = [x for x in contenuti if x is not o and _contiene(o, x, po)]
    if not dentro:
        return False
    r = (o.area / math.pi) ** 0.5
    c = o.centroid
    return all(_tondo(x) and x.centroid.distance(c) <= max(0.5, 0.15 * r) for x in dentro)


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
    attaccati = [o for o in contenuti
                 if o.area < 0.9 * poly.area and bordo.distance(o) <= CORNICE_TOCCO_MM]
    # Piccoli contorni su due o piu' spigoli = scantonati di uno sviluppo, non
    # celle di cartiglio (07_pannello_basso: pannello 1040x690 scartato come
    # cornice, si quotava un foro 5,5x5,5). Un cartiglio sta su un solo angolo
    # e ha celle attaccate ai lati.
    if attaccati and len(attaccati) >= 2 and all(_intaglio_d_angolo(poly, o) for o in attaccati):
        attaccati = []
    # Un contorno TONDO attaccato al bordo e' un foro tangente o un simbolo,
    # mai una cella di cartiglio (1119-0109-5071.00: piatto 25x570 con 4
    # cerchi Ø11,5 a filo del bordo scartato come cornice).
    attaccati = [o for o in attaccati if not _tondo(o)]
    if attaccati:
        return True  # b) cella attaccata alla cornice
    # Un cerchio con dentro solo cerchi CONCENTRICI e' un foro svasato o
    # filettato disegnato col suo smusso, non un pezzo dentro una cornice
    # (13PA00013-00: piastrina 30x140 con due svasature, scartata).
    grandi = [o for o in contenuti if not _foro_tipico(o, poly) and not _foro_svasato(o, contenuti)]
    for o in grandi:
        po = _prep_buf(o)
        if any(_contiene(o, x, po) for x in contenuti if x is not o):
            return True  # a) c'è un pezzo con fori dentro
    if grandi and _is_iso_format(poly):
        return True  # c) foglio ISO con dentro un disegno
    return False


def _separa_cartiglio(all_polys: list, testi: list | None = None) -> tuple[list, int]:
    """Divide i poligoni in (candidati pezzo, n_scartati come cornice/cartiglio).

    Oltre alle cornici (_is_cornice_cartiglio) scarta i contorni che TOCCANO il
    bordo di una cornice: sono le celle del cartiglio / le strisce d'intestazione
    (un pezzo non è mai disegnato attaccato alla cornice del foglio).
    Poi, con i testi del disegno, scarta i contorni che ne racchiudono piu'
    della meta' (_troppi_testi).
    """
    cornici = [p for p in all_polys if _is_cornice_cartiglio(p, all_polys)]
    if not cornici:
        return _via_celle_di_testo(list(all_polys), 0, testi)
    ids_cornici = {id(c) for c in cornici}
    # Un riquadro preso per cornice perche' ha dei FORI TONDI attaccati al bordo,
    # e che sta dentro un altro contorno staccato dal suo bordo, non e' una cella
    # del cartiglio: e' un segno disegnato sul pezzo (13PA00680: rettangolini
    # 100x15 attorno alle coppie di fori svasati, a 0,1 mm dal bordo). Il segno
    # non si taglia, ma non deve far scartare il pezzo che lo contiene (prima lo
    # sviluppo intero finiva scartato come "contorno che contiene una cornice")
    # ne' i fori che racchiude. Le celle del cartiglio e le viste racchiudono
    # rettangoli e pezzi, non solo cerchi: per loro non cambia niente.
    def _solo_fori_tondi(c):
        pc = _prep_buf(c)
        dentro = [o for o in all_polys if o is not c and _contiene(c, o, pc)]
        return bool(dentro) and all(
            o.length > 0 and 4 * math.pi * o.area / (o.length ** 2) >= 0.85 for o in dentro)

    def _segno_sul_pezzo(c):
        return _solo_fori_tondi(c) and any(
            id(q) not in ids_cornici and q.area > c.area and _contiene(q, c)
            and q.exterior.distance(c.exterior) > CORNICE_TOCCO_MM
            for q in all_polys)
    vere = [c for c in cornici if not _segno_sul_pezzo(c)]
    candidati = []
    n = len(cornici)
    for p in all_polys:
        if id(p) in ids_cornici:
            continue
        if any(c.exterior.distance(p) <= CORNICE_TOCCO_MM for c in vere):
            n += 1
            continue
        candidati.append(p)
    return _via_celle_di_testo(candidati, n, testi)


def _via_celle_di_testo(candidati: list, n: int, testi: list | None) -> tuple[list, int]:
    """Un contorno che racchiude almeno META' delle scritte del foglio e' una
    cella del cartiglio o la cornice, non un pezzo. Su 420 disegni reali i
    pezzi veri ne contengono meno del 10% (anche con note e quote sopra),
    cartigli e cornici oltre il 60%: nessun caso in mezzo. Serve quando la
    cornice non e' riconosciuta per forma (foglio in scala diversa dal pezzo,
    es. 19122001-00: prima veniva prezzata la cella 93,6x22 del cartiglio).
    Se si scarterebbe tutto, non si scarta nulla."""
    if not testi or len(testi) < 5 or not candidati:
        return candidati, n
    soglia = 0.5 * len(testi)
    tenuti = [p for p in candidati if _n_testi_dentro(p, testi) < soglia]
    if not tenuti:
        return candidati, n
    return tenuti, n + (len(candidati) - len(tenuti))


def _forma_regolare(p) -> bool:
    """Foro "da officina": tondo, oppure convesso (asola, rettangolo, poligono).
    Le lettere di una scritta esplosa in linee/spline sono quasi tutte concave
    (A, B, E, F, K, M, N, P, R, S, T, 1, 2, 3, 4, 5, 7...)."""
    if p.length <= 0:
        return True
    minx, miny, maxx, maxy = p.bounds
    w, h = maxx - minx, maxy - miny
    m = max(w, h)
    if m <= 0:
        return True
    if 4 * math.pi * p.area / (p.length ** 2) >= 0.9 and abs(w - h) <= 0.1 * m:
        return True     # cerchio
    try:
        return p.area >= SCRITTA_CONVESSO * p.convex_hull.area
    except Exception:
        return True


def _senza_tratti_dritti(p) -> bool:
    """Contorno curvo ovunque (ovale da spline: lo 0 o la O di una scritta):
    nessun lato dritto lungo. Un'asola o un rettangolo hanno lati dritti
    lunghi almeno un quarto del contorno."""
    try:
        xy = list(p.exterior.coords)
        minx, miny, maxx, maxy = p.bounds
        m = max(maxx - minx, maxy - miny)
        lato = max(math.hypot(xy[k + 1][0] - xy[k][0], xy[k + 1][1] - xy[k][1]) for k in range(len(xy) - 1))
        return lato < SCRITTA_LATO_DRITTO * m
    except Exception:
        return False


def _forma_lettera(p) -> str:
    """'concava' (lettera tipica), 'ovale' (O, 0 da spline), '' (foro da officina)."""
    if not _forma_regolare(p):
        return 'concava'
    minx, miny, maxx, maxy = p.bounds
    w, h = maxx - minx, maxy - miny
    if abs(w - h) > 0.1 * max(w, h) and _senza_tratti_dritti(p):
        return 'ovale'
    return ''


def _segni_isolati(fori: list) -> list:
    """Contorni piccoli e molto concavi rimasti soli (una cifra o una lettera
    isolata: 07PA01730-00, il '4' inciso). Un foro da officina non ha quella
    forma, ma da solo non basta per toglierlo: si chiede la revisione."""
    out = []
    for p in fori:
        minx, miny, maxx, maxy = p.bounds
        if max(maxx - minx, maxy - miny) > SEGNO_MAX_MM:
            continue
        try:
            if p.area < SEGNO_CONCAVO * p.convex_hull.area:
                out.append(p)
        except Exception:
            pass
    return out


def _separa_scritte(fori: list, outer, dubbi_out: list | None = None) -> tuple[list, list]:
    """Scritte incise disegnate come geometria (testo esploso in linee/spline:
    codici, numeri, sigle) dentro il pezzo. Ogni lettera chiusa diventava un
    "foro": 07PA01525-00 16 fori contro gli 11 tagliati da Lantek, che le
    lettere le MARCA (mark_perim > 0).

    Una scritta e' una FILA di contorni piccoli (<= SCRITTA_MAX_MM), vicini tra
    loro (distanza <= SCRITTA_SPAZIO volte la loro altezza) e di altezza
    simile, con almeno una lettera concava e, se sono solo 2, entrambe a forma
    di lettera: concava, oppure ovale senza lati dritti (O, 0 da spline). Un foro vero
    e' tondo o convesso con lati dritti (asola, quadro): i fori allineati di
    una foratura non fanno mai scattare la regola da soli; un contorno
    convesso (D, 1, I) entra solo se sta nella stessa fila delle lettere. Va chiamata PRIMA di
    _riduci_fori_annidati: gli occhielli delle lettere (interno di O, A, 8)
    contano nella fila e se ne vanno con la lettera.

    Si tolgono solo le scritte piccole, sia in assoluto (altezza mediana <=
    SCRITTA_MARCATA_MM) sia rispetto al pezzo (<= SCRITTA_REL_PEZZO del lato
    corto, lettere <= SCRITTA_QUOTA_AREA dell'area): una scritta grande, o che
    occupa il pezzo, puo' essere TAGLIATA
    passante (insegne, targhette: 2743-05021-0001.03 lettere da 9-15 mm,
    11TRCF005 targhetta 30x10 con lettere da 6,8 mm, tagliate in Lantek).
    Quelle restano fori e finiscono in `dubbi_out`: il chiamante chiede la
    revisione.
    Returns (fori, scritte)."""
    ob = outer.bounds
    lato_corto = min(ob[2] - ob[0], ob[3] - ob[1])
    piccoli = []
    for i, p in enumerate(fori):
        minx, miny, maxx, maxy = p.bounds
        m = max(maxx - minx, maxy - miny)
        if m <= SCRITTA_MAX_MM and not (4 * math.pi * p.area / max(p.length ** 2, 1e-9) >= 0.9
                                        and abs((maxx - minx) - (maxy - miny)) <= 0.1 * m):
            piccoli.append(i)
    if len(piccoli) < SCRITTA_MIN_LETTERE:
        return list(fori), []
    # gruppi per vicinanza (union-find)
    padre = {i: i for i in piccoli}

    def radice(i):
        while padre[i] != i:
            padre[i] = padre[padre[i]]
            i = padre[i]
        return i
    alt = {i: max(fori[i].bounds[2] - fori[i].bounds[0], fori[i].bounds[3] - fori[i].bounds[1]) for i in piccoli}
    geo_p = [fori[i] for i in piccoli]
    albero = _shp.STRtree(geo_p)
    for a_i, i in enumerate(piccoli):
        b = fori[i].bounds
        d = SCRITTA_SPAZIO * min(SCRITTA_MAX_MM, 2.5 * alt[i])
        vicini = albero.query(_shp.box(b[0] - d, b[1] - d, b[2] + d, b[3] + d))
        for b_i in vicini:
            b_i = int(b_i)
            if b_i <= a_i:
                continue
            j = piccoli[b_i]
            d_max = SCRITTA_SPAZIO * max(alt[i], alt[j])
            if max(alt[i], alt[j]) > 2.5 * min(alt[i], alt[j]):
                continue    # altezze troppo diverse: non e' la stessa scritta
            if fori[i].distance(fori[j]) <= d_max:
                padre[radice(i)] = radice(j)
    gruppi: dict = {}
    for i in piccoli:
        gruppi.setdefault(radice(i), []).append(i)
    via, dubbi = set(), []
    for g in gruppi.values():
        if len(g) < SCRITTA_MIN_LETTERE:
            continue
        forme = [_forma_lettera(fori[i]) for i in g]
        if forme.count('concava') < 1 or (len(forme) - forme.count('') < SCRITTA_MIN_CONCAVI and len(g) < 3):
            continue
        altezze = sorted(alt[i] for i in g)
        h_med = altezze[len(altezze) // 2]
        quota = sum(fori[i].area for i in g) / max(outer.area, 1e-9)
        h_rel = h_med / max(lato_corto, 1e-9)
        # scritta che E' il pezzo (targhetta, insegna): alta rispetto al pezzo,
        # o abbastanza alta e con lettere che ne coprono una parte visibile
        protagonista = (h_rel > SCRITTA_REL_PEZZO or quota > SCRITTA_QUOTA_MAX
                        or (h_rel > 0.2 and quota > SCRITTA_QUOTA_AREA))
        if h_med <= SCRITTA_MARCATA_MM and not protagonista:
            via.update(g)
        else:
            dubbi.extend(fori[i] for i in g)
    if dubbi_out is not None:
        dubbi_out.extend(dubbi)
    if not via:
        return list(fori), []
    # gli occhielli (interno di O, A, 8...) stanno dentro una lettera tolta
    tolte = [fori[k] for k in via]
    for k, p in enumerate(fori):
        if k not in via and p.area < SCRITTA_MAX_MM ** 2:
            for q in tolte:
                if q.area > p.area and _contiene(q, p):
                    via.add(k)
                    break
    return [p for k, p in enumerate(fori) if k not in via], [fori[k] for k in sorted(via)]


def _riduci_fori_annidati(inners: list, outer=None) -> tuple[list, int]:
    """Contorni interni annidati in altri contorni interni.

    - Concentrici (svasatura: passante + smusso) → il laser taglia SOLO il
      passante (il più piccolo); lo smusso è lavorazione successiva, contata come
      svasatura dallo scanner. Coerente con pick_part._holes_inside.
    - Non concentrici (isola dentro un foro) → si tiene il foro esterno: l'isola
      cade con lo sfrido, non è un taglio del pezzo.
    - con `outer` (il contorno del pezzo) toglie anche le lettere delle scritte
      incise disegnate come geometria (_separa_scritte): si marcano, non si
      tagliano.
    Returns (fori_tenuti, n_svasature_scartate).
    """
    if outer is not None:
        return _riduci_fori_annidati(_separa_scritte(inners, outer)[0])
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

def _punti_testo_mm(msp, scala: float) -> list:
    """Punti d'inserimento dei testi del disegno (TEXT, MTEXT, attributi dei
    blocchi), in mm, blocchi espansi. Servono a riconoscere le celle di
    cartiglio e tabelle: un pezzo da tagliare non ha scritte dentro."""
    punti = []

    def _visita(entita, profondita):
        for e in entita:
            t = e.dxftype()
            try:
                if t in ('TEXT', 'MTEXT', 'ATTRIB'):
                    p = e.dxf.insert
                    if t == 'TEXT' and e.dxf.hasattr('align_point') and (e.dxf.get('halign', 0) or e.dxf.get('valign', 0)):
                        p = e.dxf.align_point
                    punti.append((float(p.x) * scala, float(p.y) * scala))
                elif t == 'INSERT' and profondita < 4:
                    for a in getattr(e, 'attribs', []) or []:
                        p = a.dxf.insert
                        punti.append((float(p.x) * scala, float(p.y) * scala))
                    _visita(e.virtual_entities(), profondita + 1)
            except Exception:
                continue

    try:
        _visita(msp, 0)
    except Exception:
        pass
    return punti


def _n_testi_dentro(poly, punti_testo: list) -> int:
    if not punti_testo:
        return 0
    # prima l'ingombro (aritmetica), poi il contenimento vero solo per i
    # testi che ci cadono: sui fogli grandi erano migliaia di Point per contorno
    x1, y1, x2, y2 = poly.bounds
    vicini = [(x, y) for x, y in punti_testo if x1 <= x <= x2 and y1 <= y <= y2]
    if not vicini:
        return 0
    pp = prep(poly)
    return sum(1 for x, y in vicini if pp.contains(Point(x, y)))


def _score_candidate(poly, all_polys: list, circles_centri: list, indice: '_Indice | None' = None) -> dict:
    """Calcola score/features di un candidato pezzo.

    Score composto da:
    - n_circles: n° CIRCLE contenuti (fori del pezzo) — segnale forte
    - n_inner: n° altri poligoni contenuti (fori/dettagli) — contenimento vero
    - area_rel: area relativa (rispetto al max)
    - is_rectangle: penalità se rettangolo puro (potrebbe essere cornice)
    """
    pp = _prep_buf(poly)
    n_circles = 0
    if len(circles_centri):
        # centri dei cerchi: prima l'ingombro (vettori), poi il contenimento
        # vero solo per quelli dentro (fogli con migliaia di cerchi: prima un
        # Point per ogni cerchio e ogni contorno, 40 s su 13PA00284-00)
        arr = circles_centri if isinstance(circles_centri, _np.ndarray) else _np.asarray(circles_centri, dtype=float)
        x1, y1, x2, y2 = poly.bounds
        m = (arr[:, 0] >= x1) & (arr[:, 0] <= x2) & (arr[:, 1] >= y1) & (arr[:, 1] <= y2)
        if m.any():
            n_circles = int(_shp.contains_xy(poly, arr[m, 0], arr[m, 1]).sum())
    if indice is not None:
        n_inner = len(indice.contenuti(poly, pp))
    else:
        n_inner = sum(1 for other in all_polys if _contiene(poly, other, pp))
    return {
        'n_circles': n_circles,
        'n_inner': n_inner,
        'area': poly.area,
        'is_rectangle': _is_rectangle_like(poly),
        'bbox': poly.bounds,
        'perimeter': poly.length,
    }


def _copia_identica(a, b, all_polys: list, circles_centri: list) -> bool:
    """Due contorni sono lo stesso pezzo disegnato due volte? Area entro lo
    0,5%, ingombro entro 1 mm (anche ruotato di 90°) e stessi fori/cerchi."""
    try:
        if abs(a.area - b.area) > 0.005 * max(a.area, b.area):
            return False
        ax0, ay0, ax1, ay1 = a.bounds
        bx0, by0, bx1, by1 = b.bounds
        da = sorted((ax1 - ax0, ay1 - ay0))
        db = sorted((bx1 - bx0, by1 - by0))
        if abs(da[0] - db[0]) > 1.0 or abs(da[1] - db[1]) > 1.0:
            return False
        fa = _score_candidate(a, all_polys, circles_centri)
        fb = _score_candidate(b, all_polys, circles_centri)
        return fa['n_circles'] == fb['n_circles'] and fa['n_inner'] == fb['n_inner']
    except Exception:
        return False


def _pick_outer_with_confidence(candidates: list, all_polys: list, circles_centri: list,
                                quote: list | None = None, testi: list | None = None) -> tuple[int, float, list]:
    """Scelta del contorno (vedi _pick_base) corretta con le QUOTE del foglio.

    - Una casella vuota (rettangolo senza fori) che nessuna quota descrive,
      mentre un altro contorno ha entrambi i lati quotati e almeno 2 quote
      addosso: il pezzo e' quello quotato (19 cartigli "15 x 187,2" con le
      scritte presi al posto del pezzo). Scelta corretta ma non sicura (0,6).
    - Striscia larga <= 6 mm: e' quasi sempre la vista di fianco di una
      lamiera (larghezza = spessore), non il pezzo: non sicura.
    - Contorno senza nessuna quota, in un foglio con almeno 3 quote e altre
      viste di dimensione confrontabile (>= 10% dell'area): le quote stanno
      su un'altra vista, la scelta e' un'ipotesi: non sicura."""
    idx, conf, scored = _pick_base(candidates, all_polys, circles_centri)
    if idx < 0 or not quote:
        if idx >= 0 and conf >= 0.7 and _striscia(candidates[idx]):
            conf = 0.6
        return idx, conf, scored
    best = candidates[idx]
    feat = next(s['features'] for s in scored if s['idx'] == idx)
    nd_best = _quote_sul_contorno(best, quote)
    lati_best = _lati_quotati(best, quote)
    n_quote = len(quote)
    if _striscia(best):
        return idx, min(conf, 0.6), scored
    if nd_best == 0 and lati_best < 2 and n_quote >= 2 \
            and feat['n_inner'] == 0 and _is_rectangle_like(best):
        ind_cand = _Indice(candidates)
        preps = [_prep_buf(c) for c in candidates]
        alt = []
        for s in scored:
            c = s['poly']
            if s['idx'] == idx or _striscia(c) or c.area < MIN_AREA_MM2 * 10:
                continue
            if _lati_quotati(c, quote) < 2:
                continue
            nd = _quote_sul_contorno(c, quote)
            if nd < 2 or ind_cand.contenitori(c, preps):
                continue
            alt.append((nd, c.area, s['idx']))
        if alt:
            return max(alt)[2], 0.6, scored
    if nd_best == 0 and feat['n_inner'] == 0 and _is_rectangle_like(best) \
            and testi and _n_testi_dentro(best, testi) > 0:
        # Casella con scritte dentro, senza fori e senza quote: e' una cella
        # del cartiglio o un'etichetta. Se un'altra vista ha quote addosso, il
        # pezzo e' quella (lati quotati, poi n. di quote, poi area). Non sicuro.
        ind_cand = _Indice(candidates)
        preps = [_prep_buf(c) for c in candidates]
        alt = []
        for s in scored:
            c = s['poly']
            if s['idx'] == idx or _striscia(c) or ind_cand.contenitori(c, preps):
                continue
            nd = _quote_sul_contorno(c, quote)
            if nd < 1:
                continue
            if s['features']['n_inner'] == 0 and _is_rectangle_like(c) and _n_testi_dentro(c, testi) > 0:
                continue
            alt.append((_lati_quotati(c, quote), nd, c.area, s['idx']))
        if alt:
            return max(alt)[3], 0.6, scored
    if conf >= 0.7 and conf < CONF_ALTA and nd_best == 0 and n_quote >= 3:
        conf = 0.6
    return idx, conf, scored


def _striscia(poly) -> bool:
    x1, y1, x2, y2 = poly.bounds
    return min(x2 - x1, y2 - y1) <= STRISCIA_MAX_MM


def _pick_base(candidates: list, all_polys: list, circles_centri: list) -> tuple[int, float, list]:
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
    if len(circles_centri):
        circles_centri = _np.asarray(circles_centri, dtype=float).reshape(-1, 2)
    scored = []
    indice = _Indice(all_polys)
    for i, poly in enumerate(candidates):
        f = _score_candidate(poly, all_polys, circles_centri, indice)
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
    ind_cand = _Indice(candidates)
    preps = [_prep_buf(c) for c in candidates]
    top_level = [c for c in candidates if not ind_cand.contenitori(c, preps)]
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
            # Copie identiche restano da verificare a mano: puo' essere lo
            # stesso pezzo disegnato due volte (18B4A3004-00) o due pezzi da
            # tagliare (quantita' 2). Lo decide l'operatore, non il programma.

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
    if raw['n_tratteggiati'] and not (raw['chiusi'] or raw['aperti'] or raw['chiusi_col'] or raw['aperti_col']):
        # disegno tutto a linee tratteggiate: e' comunque il pezzo
        raw = _extract_polygons(msp, colori_esclusi, scala, escludi_tratteggi=False)
        warnings.append('Disegno solo a linee tratteggiate: usate come contorno')
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
        'testi': _punti_testo_mm(msp, scala),
        'quote': _quote_mm(msp, scala),
        'warnings': warnings, 'n_raw': n_raw, 'n_dup': n_dup,
    }


# ============================================================================
# Quote: quale contorno descrivono
# ============================================================================

def _quote_mm(msp, scala: float) -> list:
    """Quote del foglio: punti con cui toccano il disegno (mm, come i
    contorni) e misure che possono scrivere (mm veri, in piu' letture:
    misura x DIMLFAC, misura x scala del disegno). Servono a capire QUALE
    contorno il disegnatore ha descritto: il pezzo da tagliare e' la vista
    quotata, non la casella del cartiglio o la vista di fianco."""
    out = []
    try:
        dims = list(msp.query('DIMENSION'))
    except Exception:
        return out
    for d in dims:
        try:
            pts = [(px * scala, py * scala) for px, py in _punti_quota(d)]
        except Exception:
            pts = []
        vals = set()
        try:
            m = d.get_measurement()
            m = m if isinstance(m, (int, float)) else m.magnitude
            lf = float(d.override().get('dimlfac', 1.0) or 1.0)
            for v in (m * lf, m * scala, m * lf * scala):
                if v > 0:
                    vals.add(round(v, 1))
        except Exception:
            pass
        out.append({'pts': pts, 'vals': vals})
    return out


def _quote_sul_contorno(poly, quote: list) -> int:
    """Numero di quote i cui punti cadono tutti sul contorno (ingombro + 2%)."""
    if not quote:
        return 0
    x1, y1, x2, y2 = poly.bounds
    m = max(1.0, 0.02 * max(x2 - x1, y2 - y1))
    n = 0
    for q in quote:
        pts = q['pts']
        if pts and all(x1 - m <= px <= x2 + m and y1 - m <= py <= y2 + m for px, py in pts):
            n += 1
    return n


def _lati_quotati(poly, quote: list) -> int:
    """Quanti lati dell'ingombro (0-2) compaiono come misura di una quota."""
    if not quote:
        return 0
    x1, y1, x2, y2 = poly.bounds
    n = 0
    for lato in (x2 - x1, y2 - y1):
        if any(abs(v - lato) <= 0.15 for q in quote for v in q['vals']):
            n += 1
    return n


# Lato corto massimo di una VISTA DI FIANCO di lamiera (larghezza = spessore):
# in Lantek i pezzi con lato corto <= 6 mm sono 32 su 23.505 (0,14%)
STRISCIA_MAX_MM = 6.0


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
        candidati, _n = _separa_cartiglio(base['polys'], base.get('testi'))
        if candidati:
            idx, conf, _sc = _pick_outer_with_confidence(candidati, candidati, base['centri_cerchi'], base.get('quote'), base.get('testi'))
            outer = candidati[idx] if idx >= 0 and conf >= 0.5 else None
            if outer is None and idx >= 0:
                # Scelta incerta solo perche' il pezzo e' disegnato piu' volte
                # identico: per contare le lavorazioni una copia vale l'altra
                # (18B4A3004-00: 64 filetti contati invece di 32).
                best = candidati[idx]
                grandi = [c for c in candidati if c is not best
                          and c.area >= PEZZI_CONFRONTABILI_REL * best.area
                          and not best.buffer(0.05).contains(c)]
                if grandi and all(_copia_identica(best, c, candidati, base['centri_cerchi']) for c in grandi):
                    outer = best
    except Exception as e:
        logger.debug('contorno_pezzo_mm fallito: %s', e)
        outer = None
    try:
        doc._ft_contorno_pezzo = outer
    except Exception:
        pass
    return outer


# Spessori di lamiera a magazzino: la larghezza di una vista laterale vale
# come spessore solo se è uno di questi (una striscia di 11,76 mm è un'altra cosa).
SPESSORI_STOCK_MM = (0.5, 0.6, 0.8, 1.0, 1.2, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0,
                     8.0, 10.0, 12.0, 15.0, 20.0, 25.0, 30.0)


def spessore_vista_laterale(polys: list, outer) -> dict | None:
    """Spessore dalla VISTA LATERALE del pezzo piano: un rettangolo sottile,
    disegnato fuori dal pezzo, lungo quanto un lato del pezzo (entro 1 mm /
    1%) e largo uno spessore a magazzino. Regola di disegno: la vista di fianco
    di una lamiera piana è una striscia L x spessore.

    Le linee nascoste dei fori spezzano la striscia in più rettangoli della
    stessa larghezza: si riuniscono prima del confronto. Se ci sono strisce di
    larghezze diverse che tornano tutte → ambiguo, None."""
    if not _HAS_SHAPELY or outer is None or not polys:
        return None
    try:
        from shapely.ops import unary_union
        cc = list(outer.minimum_rotated_rectangle.exterior.coords)
        b = outer.bounds
        lati = [math.dist(cc[0], cc[1]), math.dist(cc[1], cc[2]), b[2] - b[0], b[3] - b[1]]
        sottili: dict = {}
        for p in polys:
            if p is outer or p.area <= 0:
                continue
            r = p.minimum_rotated_rectangle
            if r.area <= 0 or p.area < 0.97 * r.area:
                continue                       # non è un rettangolo
            c = list(r.exterior.coords)
            a, lung = sorted([math.dist(c[0], c[1]), math.dist(c[1], c[2])])
            if not (0.4 <= a <= 30.5) or lung < 3 * a:
                continue
            if p.intersects(outer):
                continue                       # dentro / sul pezzo: asola, nervatura…
            sottili.setdefault(round(a, 1), []).append(p)
        trovati = set()
        for _a, ps in sottili.items():
            u = unary_union([p.buffer(0.02) for p in ps])
            for g in getattr(u, 'geoms', [u]):
                c = list(g.minimum_rotated_rectangle.exterior.coords)
                a, lung = sorted([math.dist(c[0], c[1]), math.dist(c[1], c[2])])
                a -= 0.04
                lung -= 0.04
                if lung < 8 * a:
                    continue
                if not any(abs(lung - L) <= max(1.0, 0.01 * L) for L in lati):
                    continue
                stock = min(SPESSORI_STOCK_MM, key=lambda s: abs(s - a))
                if abs(stock - a) <= 0.03:
                    trovati.add(stock)
        if len(trovati) == 1:
            return {'spessore_mm': next(iter(trovati)), 'source': 'vista_laterale'}
        if len(trovati) > 1:
            return {'spessore_mm': None, 'source': 'vista_laterale', 'ambigui': sorted(trovati)}
    except Exception as e:  # mai rompere il detector per una stima di spessore
        logger.debug('vista laterale: %s', e)
    return None


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


def _percorso_vuoto_mm(outer, inners) -> float:
    """Spostamenti a vuoto della testa tra uno sfondamento e il successivo:
    prima i fori (dal centro, nell'ordine del piu' vicino), poi il contorno
    esterno nel suo punto piu' vicino all'ultimo foro. Si prova ogni foro come
    partenza e si tiene il giro piu' corto, come fa il CAM. Su 18B3F10101-00
    da' 301,6 mm contro i 301,9 di Lantek."""
    if not inners:
        return 0.0
    centri = [(p.centroid.x, p.centroid.y) for p in inners]

    def giro(i0):
        resto = centri[:i0] + centri[i0 + 1:]
        cur, lung = centri[i0], 0.0
        while resto:
            j = min(range(len(resto)), key=lambda k: math.hypot(cur[0] - resto[k][0], cur[1] - resto[k][1]))
            lung += math.hypot(cur[0] - resto[j][0], cur[1] - resto[j][1])
            cur = resto.pop(j)
        # ultimo tratto: dall'ultimo foro al contorno esterno
        return lung + outer.exterior.distance(Point(cur))

    try:
        # con tanti fori basta partire dal piu' vicino all'angolo del pezzo
        # (provare ogni partenza costerebbe n^3)
        if len(centri) > 60:
            minx, miny, _, _ = outer.bounds
            i0 = min(range(len(centri)), key=lambda k: math.hypot(centri[k][0] - minx, centri[k][1] - miny))
            return giro(i0)
        return min(giro(i) for i in range(len(centri)))
    except Exception as e:
        logger.debug('percorso a vuoto non calcolato: %s', e)
        return 0.0


def _linee_che_attraversano(msp, colori, base):
    """Unione (buffer 0,1 mm) dei segmenti che hanno piu' di 1 mm dentro e
    piu' di 1 mm fuori dal contorno `base`, o None."""
    from .pick_part import _collect_segments
    from shapely.geometry import LineString
    from shapely.strtree import STRtree
    from shapely.ops import unary_union
    segs = _collect_segments(msp, colori)
    if not segs or len(segs) > 60000:
        return None
    lines = [LineString([(a, b), (c, d)]) for (a, b, c, d) in segs]
    albero = STRtree(lines)
    fuori = base.buffer(0.2)
    dentro = base.buffer(-0.2)
    if dentro.is_empty:
        return None
    presi = []
    for j in albero.query(base.exterior, predicate='intersects'):
        ln = lines[j]
        try:
            if ln.difference(fuori).length > 1.0 and ln.intersection(dentro).length > 1.0:
                presi.append(ln)
        except Exception:
            continue
    if not presi:
        return None
    return unary_union(presi).buffer(0.1)


def _cerchi_che_attraversano(msp, base):
    """Unione (buffer 0,2 mm) dei CIRCLE che attraversano il bordo di `base`
    (in parte dentro, in parte fuori), o None."""
    try:
        f = scala_unita_mm(msp.doc)[0]
    except Exception:
        f = 1.0
    anelli = []
    bordo = base.exterior
    for e in entita_espanse(msp, solo_geometria=True):
        if e.dxftype() != 'CIRCLE':
            continue
        try:
            c = e.ocs().to_wcs(e.dxf.center)
            r = float(e.dxf.radius) * f
            p = Point(c.x * f, c.y * f)
        except Exception:
            continue
        d = bordo.distance(p)
        if r <= 0 or d >= r:
            continue        # il cerchio non tocca il bordo
        cerchio = p.buffer(r, 32)
        if base.contains(cerchio) or cerchio.contains(base):
            continue
        anelli.append(cerchio.exterior.buffer(0.2))
    if not anelli:
        return None
    from shapely.ops import unary_union
    return unary_union(anelli)


def _estendi_oltre_pieghe(outer, msp, cfg: dict):
    """Sviluppo spezzato dalle linee di piega disegnate sul layer di taglio.

    Il chain walking chiude anche i giri che passano per una linea di piega:
    tra i contorni vince una sola falda e il resto dello sviluppo sparisce
    (13PA00677: quotata la parte alta, 0,74 dm2 invece di 1,57; 13PA00188:
    13,0 invece di 19,0). Un pezzo vero non ha nessun contorno ATTACCATO al
    proprio bordo lungo un tratto: se ce ne sono (le altre falde), si uniscono
    finche' la sagoma si chiude, come fa il click nel CAD.

    Ritorna (outer_esteso, regione_piena, n_falde_aggiunte). regione_piena
    tiene i fori veri come buchi e serve a non scambiare le falde per fori."""
    try:
        from .pick_part import _faces_from_msp
        from shapely.ops import unary_union
        colori = set(cfg.get('dxf_colori_piega', [2])) | set(cfg.get('dxf_colori_saldatura', [1]))
        facce = _faces_from_msp(msp, colori)
    except Exception as e:
        logger.debug('estensione sviluppo non disponibile: %s', e)
        return outer, None, 0
    if not facce:
        return outer, None, 0
    base = Polygon(outer.exterior)
    base_buf = prep(base.buffer(0.1))
    esterni = [Polygon(f.exterior) for f in facce]
    # Cerchi che attraversano il contorno: richiami di dettaglio (vista
    # ingrandita), non falde. Le facce chiuse da un loro arco non si uniscono
    # (33PP00852-00: il cerchio del dettaglio sull'angolo diventava una falda).
    anelli_dettaglio = _cerchi_che_attraversano(msp, base)
    try:
        from .pick_part import linee_con_coda
        coda = linee_con_coda(msp, colori)
    except Exception:
        coda = None
    # Linee che ATTRAVERSANO il bordo del contorno (in parte dentro e in parte
    # fuori): richiami di note, linee di quota. Il bordo di una falda arriva sul
    # contorno e si ferma li', non lo scavalca (25NDSPA1945-00: il richiamo di
    # una nota chiudeva un triangolo preso per falda).
    try:
        trav = _linee_che_attraversano(msp, colori, base)
        if trav is not None:
            coda = trav if coda is None else coda.union(trav)
    except Exception:
        pass
    if anelli_dettaglio is not None:
        esterni = [None if fe.exterior.intersection(anelli_dettaglio).length >= 0.2 * fe.exterior.length
                   else fe for fe in esterni]
    # facce che compongono il contorno scelto (esclusa la zona del foglio che lo racchiude)
    dentro = [i for i, f in enumerate(facce)
              if esterni[i] is not None and esterni[i].area <= base.area * 1.001 and base_buf.contains(f.representative_point())]
    regione = base
    aggiunte = 0
    # Nessuna faccia (a parte i fori) riempie il contorno: il chain walking ha
    # preso una scorciatoia a un incrocio a T e il contorno scelto e' solo parte
    # di una faccia (24TPCPA0064: aletta superiore tagliata a meta'; 24T30PA0074:
    # falda con le asole). La faccia che lo racchiude e ne ricalca gran parte
    # del bordo e' il contorno vero.
    piu_grande = max((esterni[i].area for i in dentro), default=0.0)
    cont = []
    if piu_grande < 0.5 * base.area:
        bordo_buf = base.exterior.buffer(0.1)
        cont = [i for i, fe in enumerate(esterni)
                if fe is not None and base.area * 1.001 < fe.area <= 2.0 * base.area
                and fe.intersects(base)
                and base.difference(fe.buffer(0.1)).area <= 0.005 * base.area
                and fe.exterior.intersection(bordo_buf).length >= 0.4 * base.exterior.length]
        if not cont and not dentro:
            return outer, None, 0
    if cont:
        i0 = min(cont, key=lambda i: esterni[i].area)
        dentro = dentro + [i0]
        regione = esterni[i0]
        aggiunte = 1
    else:
        # Facce "dentro" che sporgono dal contorno: il chain walking ha tagliato
        # una faccia a un incrocio a T (07PA00692-00: falda inferiore con i fori
        # rimasta fuori). La faccia intera e' il contorno vero.
        reg_buf0 = base.buffer(0.1)
        sporgenti = []
        for i in dentro:
            try:
                if esterni[i].difference(reg_buf0).area >= 1.0:
                    sporgenti.append(i)
            except Exception:
                continue
        if sporgenti:
            try:
                u = unary_union([base] + [esterni[i] for i in sporgenti])
                if u.geom_type == 'Polygon':
                    regione = u
                    aggiunte = len(sporgenti)
            except Exception:
                pass
    usate = set(dentro)
    for _giro in range(40):
        nuove = []
        reg_buf = regione.buffer(0.1)
        for i, fe in enumerate(esterni):
            if i in usate or fe is None:
                continue
            if fe.area > MAX_FALDA_REL * base.area:
                continue  # zona del foglio / cornice
            if not fe.bounds[0] < regione.bounds[2] + 1 or not regione.bounds[0] < fe.bounds[2] + 1                     or not fe.bounds[1] < regione.bounds[3] + 1 or not regione.bounds[1] < fe.bounds[3] + 1:
                continue  # lontana
            try:
                if fe.difference(reg_buf).area < 1.0:
                    continue  # e' dentro: foro o dettaglio
            except Exception:
                continue
            # una faccia che la RACCHIUDE vale solo se ne ricalca il bordo (lo
            # sviluppo intero); la zona del foglio la ha come buco, non sul bordo
            try:
                comune = fe.exterior.intersection(reg_buf).length
            except Exception:
                continue
            if comune < 2.0:
                continue
            if coda is not None:
                # chiusa da linee che finiscono nel vuoto (richiami di note,
                # frecce, linee di quota): non e' una falda (25NDSPA1945-00)
                try:
                    libero = fe.exterior.difference(reg_buf)
                    if libero.length > 0 and libero.intersection(coda).length >= 0.6 * libero.length:
                        continue
                except Exception:
                    pass
            nuove.append(i)
        if not nuove:
            break
        usate.update(nuove)
        aggiunte += len(nuove)
        regione = unary_union([regione] + [esterni[i] for i in nuove])
        if regione.geom_type == 'MultiPolygon':
            # falde che toccano il contorno lungo un lato ma staccate di qualche
            # centesimo (contorno dal chain walking, falda dal polygonize): sono
            # attaccate, si chiude la fessura (27SLPA0017: aletta superiore persa)
            try:
                regione = unary_union([regione.buffer(0.05, join_style=2)] + [
                    esterni[i].buffer(0.05, join_style=2) for i in nuove]).buffer(-0.05, join_style=2)
            except Exception:
                pass
        if regione.geom_type != 'Polygon' or regione.area > MAX_FALDA_REL * base.area:
            return outer, None, 0
    if not aggiunte:
        return outer, None, 0
    try:
        # materiale = facce usate tranne quelle che stanno in un buco di un'altra
        # (i fori veri: restano buchi, cosi' non vengono scartati come falde)
        buchi = [Polygon(r) for i in usate for r in facce[i].interiors]
        materiale = [facce[i] for i in usate
                     if not any(b.contains(facce[i].representative_point()) for b in buchi)]
        piena = unary_union(materiale)
        esteso = Polygon(regione.exterior)
    except Exception:
        return outer, None, 0
    if not esteso.is_valid or esteso.area <= base.area * 1.001:
        return outer, None, 0
    return esteso, piena, aggiunte


def _linetype_tratteggiato(entity) -> bool:
    """Tipo linea EFFETTIVO (risolve BYLAYER) a tratti: tratteggio, asse,
    tratto-punto. Le linee di piega si disegnano cosi'."""
    try:
        nome = str(entity.dxf.get('linetype', 'BYLAYER') or 'BYLAYER')
        doc = entity.doc
        if nome.upper() == 'BYLAYER':
            lay = doc.layers.get(entity.dxf.get('layer', '0'))
            nome = str(lay.dxf.get('linetype', 'CONTINUOUS') or 'CONTINUOUS')
        if nome.upper() in ('BYLAYER', 'BYBLOCK', 'CONTINUOUS', 'SOLID', ''):
            return False
        try:
            lt = doc.linetypes.get(nome)
            if lt is not None and float(lt.dxf.get('length', 0) or 0) <= 0:
                return False    # pattern vuoto = linea continua
        except Exception:
            pass
        return True
    except Exception:
        return False


def _falde_confermate(base, esteso, msp, scala: float, quote: list | None) -> bool:
    """Le facce attaccate al contorno scelto sono davvero falde dello stesso
    sviluppo (e non viste del pezzo disegnate a contatto)?

    Su 809 disegni dell'archivio con contorni attaccati: quando il confine
    comune e' una linea A TRATTI (convenzione delle linee di piega) lo
    sviluppo intero era giusto 115 volte su 115; quando le quote misurano
    l'ingombro dello sviluppo intero e non quello del solo contorno, 181 su
    181. Con confine a linea continua e senza quote dello sviluppo il
    contorno chiuso era quello giusto 359 volte contro 36."""
    if quote and _lati_quotati(esteso, quote) == 2 and _lati_quotati(base, quote) < 2:
        return True
    try:
        from shapely.geometry import LineString
        interno = base.exterior.intersection(esteso.buffer(-0.2))
        if interno.is_empty or interno.length < 1.0:
            return False
        zona = prep(interno.buffer(0.15))
        zx1, zy1, zx2, zy2 = interno.bounds
    except Exception:
        return False
    f = float(scala or 1.0)
    tratti = continui = 0.0
    for e in entita_espanse(msp, solo_geometria=True):
        if e.dxftype() not in ('LINE', 'LWPOLYLINE', 'POLYLINE', 'ARC', 'SPLINE'):
            continue
        try:
            if _layer_da_escludere(e.dxf.layer):
                continue
        except AttributeError:
            pass
        vs = _flatten_entity(e, 0.5 / f)
        if not vs:
            continue
        pts = [(x * f, y * f) for x, y in vs]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        if max(xs) < zx1 - 0.2 or min(xs) > zx2 + 0.2 or max(ys) < zy1 - 0.2 or min(ys) > zy2 + 0.2:
            continue
        try:
            ls = LineString(pts)
            if ls.length < 1.0 or not zona.intersects(ls):
                continue
            dentro = interno.buffer(0.15).intersection(ls).length / ls.length
        except Exception:
            continue
        if dentro < 0.8:
            continue
        if _linetype_tratteggiato(e):
            tratti += ls.length
        else:
            continui += ls.length
    return tratti > 0 and tratti >= continui

def _facce(msp, cfg: dict) -> list:
    """Facce del polygonize (flatten -> noding -> polygonize), con cache."""
    try:
        from .pick_part import _faces_from_msp
        colori = set(cfg.get('dxf_colori_piega', [2])) | set(cfg.get('dxf_colori_saldatura', [1]))
        return _faces_from_msp(msp, colori)
    except Exception as e:
        logger.debug('facce non disponibili: %s', e)
        return []


def _materiale_dentro(S, facce: list):
    """Unione delle facce dentro S che sono lamiera (non stanno nel buco di
    un'altra faccia: quelle sono i fori), o None."""
    try:
        from shapely.ops import unary_union
        from shapely.strtree import STRtree
        ps = prep(S.buffer(0.2))
        albero = STRtree(facce)
        dentro = [facce[j] for j in albero.query(S) if ps.contains(facce[j])]
        buchi = [Polygon(r) for f in dentro for r in f.interiors]
        if buchi:
            alb_b = STRtree(buchi)
            mat = [f for f in dentro
                   if not any(buchi[k].contains(f.representative_point())
                              for k in alb_b.query(f.representative_point()))]
        else:
            mat = dentro
        return unary_union(mat) if mat else None
    except Exception:
        return None


def _silhouette_viste(msp, cfg: dict) -> list:
    """Contorno esterno di ogni gruppo di linee collegate del disegno (una
    vista): il buco che il gruppo lascia nella faccia che lo racchiude, o il
    bordo dell'unione delle sue facce se non e' racchiuso. Tiene solo quelle
    fatte di PIU' facce: le altre sono gia' contorni chiusi del chain walking.
    Lo sviluppo con le linee di piega continue e' cosi': una sagoma chiusa
    divisa in strisce, che il chain walking spezza nelle singole falde."""
    def calcola():
        facce = _facce(msp, cfg)
        if not facce or len(facce) > 5000:
            return []
        from shapely.ops import unary_union
        anelli = []
        for f in facce:
            for r in f.interiors:
                anelli.append(Polygon(r))
        try:
            u = unary_union(facce)
            for g in getattr(u, 'geoms', [u]):
                anelli.append(Polygon(g.exterior))
        except Exception:
            pass
        from shapely.strtree import STRtree
        albero = STRtree(facce)
        out = []
        for S in anelli:
            if not S.is_valid or S.area < MIN_AREA_MM2 * 100:
                continue
            ps = prep(S.buffer(0.2))
            n = 0
            for j in albero.query(S):
                f = facce[j]
                if f.area < S.area * 0.999 and ps.contains(f):
                    n += 1
                    if n >= 2:
                        break
            if n < 2:
                continue
            if any(_stesso_ingombro(S, q, 0.05) and abs(S.area - q.area) <= 1e-3 * S.area for q in out):
                continue
            out.append(S)
        return out
    try:
        from .pick_part import _cache_doc
        return _cache_doc(msp, ('silhouette',), calcola)
    except Exception:
        return calcola()


def _domina(S, C, margine: float = 0.5) -> bool:
    """L'ingombro di S contiene quello di C in un verso o nell'altro (lato
    corto >= lato corto, lato lungo >= lato lungo) ed e' piu' grande."""
    sb, cb = S.bounds, C.bounds
    s = sorted((sb[2] - sb[0], sb[3] - sb[1]))
    c = sorted((cb[2] - cb[0], cb[3] - cb[1]))
    return s[0] >= c[0] - margine and s[1] >= c[1] - margine and S.area >= 1.15 * C.area


def _sviluppo_altrove(outer, candidati: list, msp, cfg: dict, quote: list | None):
    """Sviluppo disegnato con le linee di piega CONTINUE, mentre il contorno
    scelto e' un'altra vista (di solito il pezzo piegato, che e' un contorno
    chiuso unico). Lo sviluppo e' la sagoma (silhouette) di un gruppo di
    linee, che il chain walking divide in falde: nessuna falda da sola vince.

    Prove:
    - le quote del foglio misurano entrambi i lati della sagoma e almeno una
      ci sta sopra (quante quelle sul contorno scelto, se e' quotato anche lui);
    - la sagoma DOMINA il contorno scelto: stendere le falde allunga il pezzo,
      quindi lo sviluppo e' almeno grande quanto la vista piegata in entrambe
      le direzioni (24T15PA0282-00: vista 145 x 15, sviluppo 145 x 30). Una
      sagoma piu' piccola e' un particolare o una vista di fianco.
    Una sola sagoma deve passare, altrimenti non si sceglie. Ritorna la
    silhouette o None."""
    if not quote:
        return None
    try:
        sil = _silhouette_viste(msp, cfg)
    except Exception:
        return None
    lq_outer = _lati_quotati(outer, quote)
    qs_outer = _quote_sul_contorno(outer, quote)
    po = outer.buffer(0.5)
    alt = []
    for S in list(sil) + [c for c in candidati if c is not outer]:
        if _is_iso_format(S) or _striscia(S):
            continue        # foglio / vista di fianco (larga uno spessore)
        if _lati_quotati(S, quote) < 2:
            continue
        nq = _quote_sul_contorno(S, quote)
        if nq < 1:
            continue
        if not _domina(S, outer):
            continue        # stendere le falde allunga: lo sviluppo non e' piu' piccolo
        if nq < qs_outer and lq_outer >= 2:
            continue        # la vista scelta e' quotata almeno quanto la sagoma
        if S.buffer(0.5).contains(outer) or po.contains(S):
            continue
        # la stessa sagoma gia' trovata (silhouette = contorno chiuso)
        if any(_stesso_ingombro(S, a[2], 0.5) and abs(S.area - a[2].area) <= 0.002 * S.area for a in alt):
            continue
        alt.append((nq, S.area, S))
    if len(alt) != 1:
        return None         # nessuna o piu' sagome quotate: non si sceglie
    S = alt[0][2]
    # contorno chiuso del chain walking (una faccia sola) o sagoma di piu' facce
    chiuso = next((c for c in candidati if _stesso_ingombro(S, c, 0.5)
                   and abs(S.area - c.area) <= 0.002 * S.area), None)
    return chiuso if chiuso is not None else S


def _stesso_ingombro(a, b, tol: float = 1.0) -> bool:
    """Stesso rettangolo d'ingombro (tutti e 4 i lati entro `tol` mm)."""
    try:
        return all(abs(x - y) <= tol for x, y in zip(a.bounds, b.bounds))
    except Exception:
        return False


def _profilo_piegato_incompatibile(outer, polys: list):
    """Cerca una vista di fianco piegata: contorno sottile (larghezza media
    2A/P <= 6 mm, lo spessore) e non diritto (area < meta' del rettangolo
    minimo che lo contiene). Il suo ingombro maggiore e' la misura del pezzo
    piegato, meta' del suo perimetro meno lo spessore e' circa lo sviluppo.
    Se un lato del contorno scelto coincide con l'ingombro del profilo e
    nessun lato coincide con lo sviluppo, il contorno scelto e' una vista del
    pezzo piegato. Ritorna (ingombro, sviluppo) del profilo o None."""
    try:
        x0, y0, x1, y1 = outer.bounds
        lati = (x1 - x0, y1 - y0)
        for p in polys:
            if p is outer or p.length <= 0:
                continue
            w = 2.0 * p.area / p.length
            if w > 6.0 or p.length < 40.0:
                continue
            mrr = p.minimum_rotated_rectangle
            if mrr.area <= 0 or p.area >= 0.5 * mrr.area:
                continue                    # profilo diritto (lamiera vista di taglio)
            px0, py0, px1, py1 = p.bounds
            span = max(px1 - px0, py1 - py0)
            svil = p.length / 2.0 - w
            if svil < span + 3.0 * w + 2.0:
                continue
            if any(abs(l - span) <= max(1.0, 0.005 * span) for l in lati) and                     not any(abs(l - svil) <= 0.03 * svil for l in lati):
                return span, svil
    except Exception:
        return None
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
        'lunghezza_vuoto_mm': round(_percorso_vuoto_mm(outer, inners), 1),
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
    candidati, cartiglio_count = _separa_cartiglio(all_polys, base.get('testi'))

    if not candidati:
        return _empty_result(warnings + [f'Solo {cartiglio_count} cornici/cartigli rilevati, nessun pezzo'])

    # ---- 6. Pick best outer + confidence
    best_idx, confidence, all_scored = _pick_outer_with_confidence(candidati, candidati, circles_centri, base.get('quote'), base.get('testi'))
    outer = candidati[best_idx]
    if getattr(doc, '_ft_scala_dubbia', False):
        confidence = min(confidence, 0.6)
    elif getattr(doc, '_ft_scala_da_quote', 1.0) != 1.0 and not _quote_sul_contorno(outer, base.get('quote')):
        # Misure moltiplicate per il fattore delle quote, ma sul contorno
        # scelto non c'e' nessuna quota: la scala del pezzo non e' confermata
        # (di solito e' anche la vista sbagliata)
        confidence = min(confidence, 0.6)
        warnings.append('Scala presa dalle quote del foglio ma il contorno scelto non ha quote: verificare')

    # ---- 6b. Sviluppo spezzato dalle linee di piega: riunisci le falde
    scelto = outer
    outer, regione_piena, n_falde = _estendi_oltre_pieghe(outer, msp, cfg)
    falde_ok = bool(n_falde) and _falde_confermate(scelto, outer, msp, scala, base.get('quote'))
    if n_falde and not falde_ok and _stesso_ingombro(scelto, outer):
        # Le facce attaccate stanno tutte DENTRO l'ingombro del contorno
        # chiuso: non sono viste disegnate a fianco (allargherebbero
        # l'ingombro) ma pezzi di lamiera tra il contorno e il bordo vero, che
        # il chain walking ha saltato prendendo una scorciatoia a un incrocio a
        # T. Il bordo esterno disegnato e' il taglio (archivio: 36 volte giusto
        # il contorno intero, 0 volte quello chiuso). Da verificare comunque.
        warnings.append(f'{n_falde} facce dentro l\'ingombro del contorno unite al pezzo '
                        f'(bordo esterno disegnato): verificare')
        confidence = min(confidence, 0.6)
    elif n_falde and not falde_ok:
        # Contorni attaccati al pezzo ma nessuna prova che siano falde: niente
        # linee di piega a tratti sul confine, quote che non misurano lo
        # sviluppo intero. Di solito sono viste disegnate a contatto: si tiene
        # il contorno chiuso, ma la scelta va verificata.
        warnings.append(f'{n_falde} contorni attaccati al pezzo senza linee di piega a tratti '
                        f'ne\' quote dello sviluppo intero: preso il contorno chiuso, verificare')
        outer, regione_piena, n_falde = scelto, None, 0
        confidence = min(confidence, 0.6)
    if n_falde:
        warnings.append(f'Sviluppo diviso da linee di piega: unite {n_falde} falde al contorno')
    # ---- 6c. Sviluppo con pieghe continue disegnato a parte dalla vista scelta
    sviluppo = _sviluppo_altrove(outer, candidati, msp, cfg, base.get('quote'))
    if sviluppo is not None:
        if any(sviluppo is c for c in candidati):
            piena = sviluppo        # contorno chiuso: fori = contorni dentro
        else:
            piena = _materiale_dentro(sviluppo, _facce(msp, cfg))
        if piena is not None:
            sb_, ob_ = sviluppo.bounds, outer.bounds
            warnings.append(
                f'Preso lo sviluppo quotato {sb_[2] - sb_[0]:.0f} x {sb_[3] - sb_[1]:.0f} '
                f'(piu\' grande della vista scelta in entrambe le direzioni) invece della vista '
                f'{ob_[2] - ob_[0]:.0f} x {ob_[3] - ob_[1]:.0f}: verificare')
            outer, scelto = sviluppo, sviluppo
            regione_piena = None if piena is sviluppo else piena
            idx_c = next((i for i, c in enumerate(candidati) if c is sviluppo), None)
            if idx_c is not None:
                best_idx = idx_c        # il pulito per Lantek lo ritrova cosi'
            confidence = min(confidence, 0.6)

    # ---- 7. Inner holes (contenuti VERAMENTE nell'outer — BUG FIX D2) + svasature (D4)
    pp_outer = _prep_buf(outer)
    inners = [p for i, p in enumerate(candidati)
              if i != best_idx and p is not outer and _contiene(outer, p, pp_outer)]
    if regione_piena is not None:
        # le falde unite sono materiale, non fori: foro = contorno non coperto dalle facce
        inners = [p for p in inners if regione_piena.intersection(p).area < 0.5 * p.area]
    scritte_dubbie: list = []
    inners, scritte = _separa_scritte(inners, outer, scritte_dubbie)
    inners, n_svas = _riduci_fori_annidati(inners)
    if scritte:
        warnings.append(f'{len(scritte)} contorni piccoli in fila come una scritta incisa: '
                        f'non contati come fori (vanno marcati, non tagliati)')
    if scritte_dubbie:
        warnings.append(f'{len(scritte_dubbie)} contorni in fila come una scritta alta: contati come fori '
                        f'tagliati, verificare se vanno tagliati o marcati')
    outer_mis, inners, n_intagli = applica_intagli(outer, inners)
    if n_intagli:
        warnings.append(f'{n_intagli} intaglio/i sul bordo (scantonati): tolti dal contorno, non contati come fori')
    segni = _segni_isolati(inners)
    if segni:
        warnings.append(f'{len(segni)} contorno/i piccolo/i a forma di lettera o cifra: contato/i come '
                        f'foro, verificare se va tagliato o marcato')

    # ---- 8. Calcoli finali
    mis = _misure(outer_mis, inners, scala)

    # Più pezzi nello stesso disegno: segnala invece di sceglierne uno in silenzio
    ind_c = _Indice(candidati)
    preps_c = [_prep_buf(o) for o in candidati]
    top_level = [c for c in candidati if not ind_c.contenitori(c, preps_c)]
    altri_grandi = [c for c in top_level
                    if c is not outer and c is not scelto and c.area >= PEZZI_CONFRONTABILI_REL * outer.area
                    and not _contiene(outer, c, pp_outer)]
    n_pezzi = 1 + len(altri_grandi)
    if altri_grandi and all(_copia_identica(outer, c, candidati, circles_centri) for c in altri_grandi):
        warnings.append(f'{n_pezzi} contorni esterni identici (dimensioni confrontabili): misure e '
                        f'lavorazioni contate su una sola copia. Verificare se e\' lo stesso pezzo '
                        f'disegnato {n_pezzi} volte o se vanno tagliati {n_pezzi} pezzi')
    elif n_pezzi > 1:
        warnings.append(f'{n_pezzi} contorni esterni di dimensioni confrontabili nel disegno: '
                        f'verificare quale pezzo quotare')
    if n_svas:
        warnings.append(f'{n_svas} svasatura/e: tagliato solo il foro passante')

    # Vista di fianco PIEGATA (profilo sottile a L/U) che dice che il pezzo
    # scelto e' quello gia' piegato: un lato del contorno e' lungo quanto il
    # profilo da estremo a estremo, ma lo sviluppo (la lunghezza del profilo
    # disteso) non compare (me01_000447: vista a U di 54 mm, sviluppo 79 mm;
    # nel disegno manca lo sviluppo e Lantek lo ha calcolato).
    prof = _profilo_piegato_incompatibile(outer_mis, all_polys)
    if prof and confidence >= 0.5:
        confidence = 0.45
        warnings.append(f'Il disegno mostra il pezzo piegato (profilo lungo {prof[0]:.0f} mm, '
                        f'disteso circa {prof[1]:.0f} mm) e il contorno trovato ha la misura del '
                        f'piegato: manca lo sviluppo, verificare')

    # Contorno largo meno di 3 mm: e' la vista di fianco della lamiera (lo
    # spessore), non uno sviluppo da tagliare (07PA01517-00: preso il bordo
    # 1,4 x 82 della vista isometrica). In Lantek 6 pezzi su 23.505 sono cosi'.
    sb = scelto.bounds
    lato_min = min(mis['bbox_width_mm'], mis['bbox_height_mm'], sb[2] - sb[0], sb[3] - sb[1])
    if lato_min < MIN_LATO_PEZZO_MM and confidence >= 0.5:
        confidence = 0.45
        warnings.append(f'Contorno largo meno di {MIN_LATO_PEZZO_MM:g} mm: probabile vista di '
                        f'fianco (spessore), non lo sviluppo — scegliere il pezzo')

    # ---- 8b. Fori dubbi: il contorno e' giusto ma cosa si taglia dentro no.
    # Confidenza sotto la soglia del "sicuro" (pulizia 'auto_review'), senza
    # chiedere la scelta manuale del pezzo.
    if scritte_dubbie or segni:
        confidence = min(confidence, CONF_FORI_DUBBI)

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
        # fori dubbi (scritte/lettere contate come fori): la conferma delle
        # misure del cartiglio riguarda il contorno, non toglie questo dubbio
        'dubbi_fori': bool(scritte_dubbie or segni),
        # spessore dalla vista laterale (striscia lunga quanto un lato del pezzo)
        'spessore_vista': spessore_vista_laterale(all_polys, scelto),
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
    inners, _n_svas = _riduci_fori_annidati(inners, outer)

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
    non_cartiglio = _separa_cartiglio(all_polys, base.get('testi'))[0]

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

    # Inner: i poligoni contenuti nell'outer (fori, dettagli, sub-contorni), con
    # lo stesso filtro dell'import: i segni scambiati per celle del cartiglio
    # (13PA00680, rettangolini attorno ai fori) non si contano come tagli.
    pp = _prep_buf(outer)
    inners = [p for p in non_cartiglio if p is not outer and _contiene(outer, p, pp)]
    inners, _n_svas = _riduci_fori_annidati(inners, outer)

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
    all_polys = _separa_cartiglio(base['polys'], base.get('testi'))[0]
    for p in all_polys:
        if abs(p.area - outer_poly.area) <= 0.01 * p.area and p.hausdorff_distance(outer_poly) <= 1.0:
            outer_poly = p
            break

    pp = _prep_buf(outer_poly)
    # Trova inners: poligoni contenuti in outer che NON siano l'outer stesso
    inners = [p for p in all_polys if p is not outer_poly and _contiene(outer_poly, p, pp)]
    inners, _n_svas = _riduci_fori_annidati(inners, outer_poly)
    mis = _misure(outer_poly, inners, scala)

    r_out = dict(r)
    r_out.update(mis)
    r_out['selected_candidate_idx'] = candidate_idx
    r_out['confidence'] = 1.0
    r_out['confidence_label'] = 'manuale'
    r_out['needs_manual_select'] = False
    r_out['warnings'] = r_out.get('warnings', []) + [f'Selezione manuale utente: candidato #{candidate_idx}']
    return r_out
