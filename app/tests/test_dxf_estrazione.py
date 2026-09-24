"""Test dell'ESTRAZIONE DATI DAI DXF (preventivatore: area, perimetro di taglio,
inneschi, pieghe, saldatura, filettature/svasature, spessore, materiale).

Ogni caso genera un piccolo DXF sintetico con ezdxf in una cartella temporanea
e verifica i numeri che finiscono nel preventivo. Copre i bug D1-D16 della
revisione dell'estrazione DXF:

 D1  piastra con fori scartata come "cornice"; cornice vera riconosciuta
 D2  contenimento col representative_point (foro centrale "conteneva" il pezzo)
 D3  contorni / linee duplicati (area 0, perimetro doppio)
 D4  foro svasato (2 cerchi concentrici): si taglia solo il passante
 D5  pezzo dentro un blocco (INSERT, anche scalato/ruotato)
 D6  unità $INSUNITS (pollici) e unitless
 D7  contorno esterno col colore "saldatura"; linee di piega BYLAYER
 D8  pieghe da testi "SUPPORTO"/"SUPERFICIE"
 D9  spessore dalla descrizione solo se etichettato; formati foglio ignorati
 D10 confidence realistica; v1/v2 con confidence esplicita
 D11 materiale da testi non pertinenti (ragione sociale, codici)
 D12 chiave cache (nome file, config) e DXF pulito rigenerato sul cache hit
 D13 cache LLM: gli errori transitori non vengono ricordati
 D14 n_forature = inneschi (1 contorno + fori) anche nel percorso manuale
 D15 fallback rettangolo dopo cleanup: solo contorni interni, bulge inclusi
 D16 v1: duplicati, area netta, confidence bassa
 + rondella non è svasatura, archi filetto spezzati, ELLIPSE aperte, giunzione
   estremi a tolleranza, spessore da testo etichettato, stock 0.8/2.5,
   più pezzi nello stesso disegno.

Si importano solo i moduli DXF (senza avviare l'app Flask).
Esecuzione: python app/tests/test_dxf_estrazione.py   (exit 0 = tutto ok)
"""
import json
import math
import os
import shutil
import sys
import tempfile
import types

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

# Pacchetto 'backend' senza eseguire backend/__init__ (che importa l'app Flask)
if 'backend' not in sys.modules:
    _pkg = types.ModuleType('backend')
    _pkg.__path__ = [os.path.join(_APP, 'backend')]
    sys.modules['backend'] = _pkg

os.environ.pop('GEMINI_API_KEY', None)
os.environ.pop('GOOGLE_API_KEY', None)

import logging  # noqa: E402
logging.disable(logging.CRITICAL)

import ezdxf  # noqa: E402

from backend.preventivi import dxf_scanner as S  # noqa: E402
from backend.preventivi import dxf_cache  # noqa: E402
from backend.preventivi import dxf_cleanup  # noqa: E402
from backend.preventivi import llm_material_normalizer as LLM  # noqa: E402
from backend.preventivi import pick_part as PP  # noqa: E402
from backend.preventivi.dxf_polygon_detector_v3 import detect_pezzo_geometry_v3  # noqa: E402

TMP = tempfile.mkdtemp(prefix='test_dxf_')
# cache del parsing su DB temporaneo (mai quella vera dell'app)
dxf_cache._CACHE_DB_PATH = os.path.join(TMP, 'cache_test.db')
dxf_cache._INITIALIZED = False
CFG = {
    'dxf_colori_piega': [2], 'dxf_colori_saldatura': [1],
    'dxf_lunghezza_minima': 15.0, 'dxf_tolleranza_centro': 1.0,
    'dxf_svasatura_ratio_min': 1.8, 'dxf_svasatura_ratio_max': 3.0,
    'dxf_semicerchio_angolo_min': 150.0, 'dxf_semicerchio_angolo_max': 320.0,
    'dxf_filtra_zona_sviluppata': True,
}

OK = 0
KO = []


def check(nome, cond, extra=''):
    global OK
    if cond:
        OK += 1
        print(f'  [OK] {nome}')
    else:
        KO.append(nome)
        print(f'  [KO] {nome} {extra}')


def vicino(a, b, tol=0.002):
    return a is not None and abs(float(a) - float(b)) <= tol


def dxf(nome, build, units=4):
    """Crea un DXF (unità mm di default) e ne restituisce il percorso."""
    doc = ezdxf.new('R2010', units=units)
    build(doc, doc.modelspace())
    p = os.path.join(TMP, nome)
    doc.saveas(p)
    return p


def rett(m, x0, y0, w, h, **attr):
    m.add_lwpolyline([(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)],
                     close=True, dxfattribs=attr)


def rett_linee(m, x0, y0, w, h, **attr):
    pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
    for a, b in zip(pts, pts[1:] + pts[:1]):
        m.add_line(a, b, dxfattribs=attr)


def geo(p, cfg=None):
    return detect_pezzo_geometry_v3(p, cfg or CFG)


A_FORO10 = math.pi * 25.0          # mm² di un foro Ø10
P_FORO10 = math.pi * 10.0          # mm di un foro Ø10


def main():
    # =====================================================================
    print('\nD1) Piastra 300x200 con 4 fori NON è una cornice; cornice vera sì')
    def _d1(doc, m):
        rett(m, 0, 0, 300, 200)
        for x, y in [(20, 20), (280, 20), (280, 180), (20, 180)]:
            m.add_circle((x, y), 5)
    r = geo(dxf('d1_piastra.dxf', _d1))
    atteso = (60000 - 4 * A_FORO10) / 10000
    check('area = piastra meno 4 fori', vicino(r['area_dm2'], atteso), r['area_dm2'])
    check('perimetro = 1000 + 4 fori', vicino(r['perimetro_taglio_m'], (1000 + 4 * P_FORO10) / 1000), r['perimetro_taglio_m'])
    check('n_forature = 5 inneschi', r['n_forature'] == 5, r['n_forature'])
    check('nessuna cornice rimossa', r['poligoni_cartiglio_rimossi'] == 0, r['poligoni_cartiglio_rimossi'])

    def _d1b(doc, m):
        rett(m, 0, 0, 420, 297)                     # cornice foglio A3
        rett(m, 250, 0, 170, 20)                    # cella cartiglio attaccata alla cornice
        rett(m, 250, 20, 170, 20)
        rett(m, 50, 100, 100, 50)                   # pezzo
        m.add_circle((100, 125), 5)
    r = geo(dxf('d1_cornice.dxf', _d1b))
    check('cornice A3: si quota il pezzo 100x50', vicino(r['area_dm2'], (5000 - A_FORO10) / 10000), r['area_dm2'])
    check('cornice A3: bbox pezzo 100x50', r['bbox_width_mm'] == 100 and r['bbox_height_mm'] == 50,
          (r['bbox_width_mm'], r['bbox_height_mm']))

    # =====================================================================
    print('\nD2) Foro rettangolare centrale: contenimento vero')
    def _d2(doc, m):
        rett(m, 0, 0, 200, 100)
        rett(m, 80, 40, 40, 20)
    r = geo(dxf('d2_foro_centrale.dxf', _d2))
    check('area = 200x100 - 40x20', vicino(r['area_dm2'], 1.92), r['area_dm2'])
    check('bbox = pezzo, non il foro', r['bbox_width_mm'] == 200, r['bbox_width_mm'])
    check('2 inneschi', r['n_forature'] == 2, r['n_forature'])

    # =====================================================================
    print('\nD3) Entità duplicate')
    def _d3(doc, m):
        rett(m, 0, 0, 300, 100)
        rett(m, 0, 0, 300, 100)
        m.add_circle((50, 50), 5)
        m.add_circle((50, 50), 5)
    r = geo(dxf('d3_duplicati.dxf', _d3))
    check('contorno doppio: area corretta', vicino(r['area_dm2'], (30000 - A_FORO10) / 10000), r['area_dm2'])
    check('contorno doppio: perimetro non raddoppiato', vicino(r['perimetro_taglio_m'], (800 + P_FORO10) / 1000), r['perimetro_taglio_m'])
    check('contorno doppio: 2 inneschi (non 3)', r['n_forature'] == 2, r['n_forature'])

    def _d3b(doc, m):
        rett_linee(m, 0, 0, 300, 100)
        rett_linee(m, 0, 0, 300, 100)
    r = geo(dxf('d3_linee_doppie.dxf', _d3b))
    check('linee doppie: area 3 dm²', vicino(r['area_dm2'], 3.0), r['area_dm2'])
    check('linee doppie: perimetro 0.8 m', vicino(r['perimetro_taglio_m'], 0.8), r['perimetro_taglio_m'])

    # =====================================================================
    print('\nD4) Foro svasato: si taglia solo il passante')
    def _d4(doc, m):
        rett(m, 0, 0, 100, 100)
        m.add_circle((50, 50), 3.25)
        m.add_circle((50, 50), 6.5)
    p4 = dxf('d4_svasatura.dxf', _d4)
    r = geo(p4)
    check('area: sottratto solo il passante', vicino(r['area_dm2'], (10000 - math.pi * 3.25 ** 2) / 10000), r['area_dm2'])
    check('perimetro: solo il passante', vicino(r['perimetro_taglio_m'], (400 + 2 * math.pi * 3.25) / 1000), r['perimetro_taglio_m'])
    check('2 inneschi', r['n_forature'] == 2, r['n_forature'])
    check('scanner: 1 svasatura', S.scansiona_dxf_dettagli(p4, CFG)[3] == 1, S.scansiona_dxf_dettagli(p4, CFG))

    # =====================================================================
    print('\nD5) Pezzo definito in un blocco (INSERT)')
    def _d5(doc, m):
        b = doc.blocks.new('PEZZO')
        rett(b, 0, 0, 100, 50)
        b.add_circle((25, 25), 5)
        m.add_blockref('PEZZO', (10, 10))
    p5 = dxf('d5_blocco.dxf', _d5)
    r = geo(p5)
    check('blocco: area 100x50 - foro', vicino(r['area_dm2'], (5000 - A_FORO10) / 10000), r['area_dm2'])
    check('blocco: 2 inneschi', r['n_forature'] == 2, r['n_forature'])

    def _d5b(doc, m):
        b = doc.blocks.new('PEZZO')
        rett(b, 0, 0, 100, 50)
        b.add_circle((25, 25), 5)
        m.add_blockref('PEZZO', (500, 0), dxfattribs={'xscale': 2, 'yscale': 2, 'rotation': 90})
    r = geo(dxf('d5_blocco_scalato.dxf', _d5b))
    check('blocco scalato x2 e ruotato: area 200x100 - foro Ø20',
          vicino(r['area_dm2'], (20000 - math.pi * 100) / 10000), r['area_dm2'])
    check('blocco ruotato: bbox 100x200', r['bbox_width_mm'] == 100 and r['bbox_height_mm'] == 200,
          (r['bbox_width_mm'], r['bbox_height_mm']))
    rc = PP.follow_contour_from_click(p5, 60, 10, CFG)
    check('pick_part su blocco: area corretta', rc.get('success') and vicino(rc['area_dm2'], (5000 - A_FORO10) / 10000),
          rc.get('area_dm2'))

    def _d5c(doc, m):
        rett(m, 0, 0, 100, 50)
        b = doc.blocks.new('CARTIGLIO_DATI')
        b.add_attdef('MAT', (0, 0))
        ins = m.add_blockref('CARTIGLIO_DATI', (300, 0))
        m.add_text('MATERIALE', dxfattribs={'insert': (280, 0)})
        ins.add_attrib('MAT', 'S235JR', (300, 0))
    mc = S.estrai_materiale_da_cartiglio(dxf('d5_cartiglio_blocco.dxf', _d5c))
    check('materiale letto da ATTRIB di un blocco', mc['materiale'] == 'S235', mc)

    # =====================================================================
    print('\nD6) Unità del disegno')
    def _d6(doc, m):
        rett(m, 0, 0, 4, 2)
        m.add_circle((1, 1), 0.25)
    r = geo(dxf('d6_pollici.dxf', _d6, units=1))
    atteso = (101.6 * 50.8 - math.pi * 6.35 ** 2) / 10000
    check('pollici: area convertita in mm', vicino(r['area_dm2'], atteso), (r['area_dm2'], atteso))
    check('pollici: bbox 101.6 mm', vicino(r['bbox_width_mm'], 101.6, 0.01), r['bbox_width_mm'])
    check('pollici: warning conversione', any('pollici' in w for w in r['warnings']), r['warnings'])

    def _d6b(doc, m):
        rett(m, 0, 0, 100, 50)
    r = geo(dxf('d6_unitless.dxf', _d6b, units=0))
    check('unitless: assunti mm', vicino(r['area_dm2'], 0.5), r['area_dm2'])
    check('unitless: warning', any('INSUNITS=0' in w for w in r['warnings']), r['warnings'])

    def _d6c(doc, m):
        rett(m, 0, 0, 300, 200)    # geometria in mm ma header "metri" (default ezdxf)
    r = geo(dxf('d6_metri_falsi.dxf', _d6c, units=6))
    check('header metri implausibile: assunti mm', vicino(r['area_dm2'], 6.0), r['area_dm2'])

    def _d6d(doc, m):
        rett(m, 0, 0, 0.3, 0.2)    # pezzo 300x200 disegnato davvero in metri
        m.add_circle((0.15, 0.1), 0.005)
    r = geo(dxf('d6_metri_veri.dxf', _d6d, units=6))
    check('metri veri (0.3x0.2 m): convertiti in mm',
          vicino(r['area_dm2'], (60000 - A_FORO10) / 10000) and r['bbox_width_mm'] == 300,
          (r['area_dm2'], r['bbox_width_mm']))

    def _d6e(doc, m):
        m.add_circle((0, 0), 4)     # rondella Ø8 in mm, header "metri"
        m.add_circle((0, 0), 2)
    r = geo(dxf('d6_rondella_metri_falsi.dxf', _d6e, units=6))
    check('pezzo piccolo 8 mm con header metri: resta in mm (non 8 m)',
          vicino(r['area_dm2'], math.pi * 12 / 10000, 0.0005) and any('millimetri' in w for w in r['warnings']),
          (r['area_dm2'], r['warnings']))

    def _d6f(doc, m):
        b = doc.blocks.new('P')
        rett(b, 0, 0, 4, 2)
        m.add_blockref('P', (0, 0))
    segs, xs, ys = S.dxf_to_segments(dxf('d6_segmenti.dxf', _d6f, units=1))
    check('dxf_to_segments: blocco espanso e in mm', len(segs) == 4 and vicino(max(xs), 101.6, 0.01),
          (len(segs), max(xs) if xs else None))
    p6g = dxf('d6_fallback_pollici.dxf', lambda doc, m: (rett(m, 0, 0, 4, 2), m.add_circle((1, 1), 0.25)), units=1)
    fb = dxf_cleanup.perimetro_interni_fallback(p6g, [0, 0, 4, 2])
    check('fallback bbox su DXF in pollici: scala e foro in mm',
          dxf_cleanup.scala_mm(p6g) == 25.4 and vicino(fb['perim_fori_mm'], 2 * math.pi * 6.35, 0.5),
          (dxf_cleanup.scala_mm(p6g), fb))

    # =====================================================================
    print('\nD7) Colori: contorno rosso e piega BYLAYER')
    def _d7(doc, m):
        rett(m, 0, 0, 100, 50, color=1)
        m.add_circle((25, 25), 5)
    p7 = dxf('d7_contorno_rosso.dxf', _d7)
    r = geo(p7)
    check('contorno rosso resta il pezzo', vicino(r['area_dm2'], (5000 - A_FORO10) / 10000), r['area_dm2'])
    check('contorno rosso non è saldatura', S.scansiona_dxf_dettagli(p7, CFG)[1] == 0.0, S.scansiona_dxf_dettagli(p7, CFG))

    def _d7b(doc, m):
        rett(m, 0, 0, 100, 50)
        m.add_line((20, 25), (80, 25), dxfattribs={'color': 1})   # cordone di saldatura 60mm
    check('linea rossa aperta = saldatura 0.06 ml',
          S.scansiona_dxf_dettagli(dxf('d7_saldatura.dxf', _d7b), CFG)[1] == 0.06)

    def _d7c(doc, m):
        doc.layers.add('PIEGA', color=2)
        rett_linee(m, 0, 0, 100, 50)
        # colore BYLAYER; a sinistra del centro (dxf_filtra_zona_sviluppata)
        m.add_line((40, 0), (40, 50), dxfattribs={'layer': 'PIEGA'})
    p7c = dxf('d7_piega_bylayer.dxf', _d7c)
    check('piega BYLAYER riconosciuta', S.scansiona_dxf_dettagli(p7c, CFG)[0] == 1, S.scansiona_dxf_dettagli(p7c, CFG))
    r = geo(p7c)
    check('piega BYLAYER non spezza il pezzo', vicino(r['area_dm2'], 0.5) and r['n_forature'] == 1,
          (r['area_dm2'], r['n_forature']))

    # =====================================================================
    print('\nD8) Pieghe solo da testi "SU/GIU <angolo>°"')
    def _d8(doc, m):
        rett(m, 0, 0, 100, 50)
        m.add_text('SUPPORTO LATERALE', dxfattribs={'insert': (500, 10)})
        m.add_text('SUPERFICIE SGRASSATA', dxfattribs={'insert': (500, 20)})
        m.add_text('GIUNTO SALDATO', dxfattribs={'insert': (500, 30)})
    check('SUPPORTO/SUPERFICIE/GIUNTO non sono pieghe',
          S.scansiona_dxf_dettagli(dxf('d8_testi.dxf', _d8), CFG)[0] == 0)

    def _d8b(doc, m):
        rett_linee(m, 0, 0, 100, 50)
        m.add_line((60, 0), (60, 50))
        m.add_text('SU 90%%d R 2', dxfattribs={'insert': (62, 25)})
    check('"SU 90° R 2" = 1 piega', S.scansiona_dxf_dettagli(dxf('d8_piega.dxf', _d8b), CFG)[0] == 1)

    # =====================================================================
    print('\nD9) Spessore dalla descrizione solo se etichettato')
    def _d9(doc, m):
        rett(m, 0, 0, 100, 50)
        m.add_text('FORMATO 420x297 1:1', dxfattribs={'insert': (500, 30)})
        m.add_text('100x50 2 PZ', dxfattribs={'insert': (500, 50)})
    p9 = dxf('d9_formato.dxf', _d9)
    d = S.estrai_dimensioni_da_descrizione_cartiglio(p9)
    check('formato foglio / "2 PZ" non danno pezzo né spessore', d.get('area_dm2') is None, d)
    check('nessuno spessore dal testo', S.estrai_spessore_da_cartiglio(p9).get('spessore_mm') is None,
          S.estrai_spessore_da_cartiglio(p9))

    def _d9b(doc, m):
        rett(m, 0, 0, 45.5, 12)
        m.add_text('Piastrina 45,5x12 sp.3', dxfattribs={'insert': (500, 30)})
    d = S.estrai_dimensioni_da_descrizione_cartiglio(dxf('d9_virgola.dxf', _d9b))
    check('"45,5x12 sp.3": 45.5 x 12, sp 3',
          d.get('dim_x_mm') == 45.5 and d.get('dim_y_mm') == 12 and d.get('spessore_mm') == 3.0, d)

    for testo, atteso in [('SP. 3', 3.0), ('SP=3', 3.0), ('S=2', 2.0), ('SPESSORE 1,5', 1.5),
                          ('sp3', 3.0), ('THK 2 mm', 2.0)]:
        def _d9c(doc, m, testo=testo):
            rett(m, 0, 0, 100, 50)
            m.add_text(testo, dxfattribs={'insert': (500, 30)})
        sp = S.estrai_spessore_da_cartiglio(dxf('d9_sp.dxf', _d9c))
        check(f'spessore da testo "{testo}" = {atteso}', sp.get('spessore_mm') == atteso and sp.get('source') == 'testo', sp)

    def _d9d(doc, m):
        rett(m, 0, 0, 100, 50)
        m.add_mtext('{\\fArial|b1;SPESSORE} \\P2 mm', dxfattribs={'insert': (500, 30)})
        m.add_text('3 mm', dxfattribs={'insert': (500, 60)})   # non etichettato: ignorato
    sp = S.estrai_spessore_da_cartiglio(dxf('d9_mtext.dxf', _d9d))
    check('MTEXT formattato "SPESSORE 2 mm" = 2', sp.get('spessore_mm') == 2.0, sp)

    print('\nD9b) Cartiglio tabellare (layout 20R201N0401) e fonti di spessore discordi')

    def _tabella(m, sp_valore):
        # stessa disposizione del cartiglio reale: label a x=750, valori nella
        # cella a destra (x≈820, +3mm in y), revisione "01" appena sotto "Sp./Ø:"
        m.add_text('Lunghezza:', dxfattribs={'insert': (750.2, 80.6)})
        m.add_text('400', dxfattribs={'insert': (819.7, 83.8)})
        m.add_text('Larghezza:', dxfattribs={'insert': (750.2, 67.1)})
        m.add_text('22', dxfattribs={'insert': (820.5, 70.3)})
        m.add_text('Sp./Ø:', dxfattribs={'insert': (750.2, 53.3)})
        m.add_text('01', dxfattribs={'insert': (746.1, 51.8)})
        m.add_text(sp_valore, dxfattribs={'insert': (819.9, 56.4)})
        m.add_text('SCALA:1:3', dxfattribs={'insert': (837.4, 56.2)})
        m.add_text('01', dxfattribs={'insert': (821.3, 31.5)})

    def _d9t(doc, m):
        rett(m, 263.4, 344.8, 360, 22)
        _tabella(m, '3')
    d = S.estrai_dimensioni_da_descrizione_cartiglio(dxf('d9_tabellare.dxf', _d9t))
    check('tabellare: spessore dalla cella a destra (3), non la revisione "01"',
          d.get('spessore_mm') == 3.0 and d.get('source') == 'cartiglio_tabellare', d)
    check('tabellare: L=400 W=22', d.get('dim_x_mm') == 400 and d.get('dim_y_mm') == 22, d)

    s = S.scegli_spessore([
        {'spessore_mm': 3.0, 'confidence': 0.57, 'source': 'peso_area'},
        {'spessore_mm': 1.0, 'confidence': 0.65, 'source': 'cartiglio_tabellare'}])
    check('discordanti: peso/area batte tabellare, warning con entrambi',
          s['spessore_mm'] == 3.0 and any('1.0 mm' in w and '3 mm' in w for w in s['warnings']), s)
    s = S.scegli_spessore([
        {'spessore_mm': 1.2, 'confidence': 0.9, 'source': 'peso_area'},
        {'spessore_mm': 3.0, 'confidence': 0.8, 'source': 'testo'}])
    check('discordanti: testo etichettato batte peso/area', s['spessore_mm'] == 3.0 and s['warnings'], s)
    s = S.scegli_spessore([
        {'spessore_mm': 10.0, 'confidence': 0.9, 'source': 'peso_area'},
        {'spessore_mm': 3.0, 'confidence': 0.75, 'source': 'filename'}], area_incerta=True)
    check('area incerta: peso/area in coda, vince il nome file', s['spessore_mm'] == 3.0, s)
    s = S.scegli_spessore([
        {'spessore_mm': 3.0, 'confidence': 0.57, 'source': 'peso_area'},
        {'spessore_mm': 3.0, 'confidence': 0.65, 'source': 'cartiglio_tabellare'}])
    check('concordanti: nessun warning, confidence massima', s['spessore_mm'] == 3.0 and not s['warnings']
          and s['confidence'] == 0.65, s)

    from backend.preventivi import dxf_batch_worker as _w

    def _d9w(doc, m):
        rett(m, 0, 0, 360, 22)
        _tabella(m, '1')          # la tabella dice 1, il nome file dice sp3
    p9w = dxf('pezzo_sp3.dxf', _d9w)
    rw = _w.process_single_dxf(p9w, 'pezzo_sp3.dxf', CFG)
    sp = rw.get('spessore') or {}
    check('pipeline: tabellare 1.0 non sovrascrive il nome file sp3 (warning)',
          sp.get('spessore_mm') == 3.0 and any('discordante' in w for w in sp.get('warnings') or []), sp)

    # =====================================================================
    print('\nD10) Confidence')
    def _d10(doc, m):
        rett(m, 0, 0, 120, 80)
        m.add_circle((60, 40), 5)
    p10 = dxf('d10_un_foro.dxf', _d10)
    r = geo(p10)
    check('piastra con 1 foro: confidence alta', r['confidence'] >= 0.85 and not r['needs_manual_select'],
          (r['confidence'], r['needs_manual_select']))
    r2 = S.estrai_geometria_taglio(p10, {**CFG, 'dxf_scanner_version': 'v2'})
    check('v2: confidence esplicita', 'confidence' in r2 and r2['confidence'] >= 0.5, r2.get('confidence'))
    check('v2: area corretta', vicino(r2['area_dm2'], (9600 - A_FORO10) / 10000), r2.get('area_dm2'))
    r1 = S.estrai_geometria_taglio(p10, {**CFG, 'dxf_scanner_version': 'v1'})
    check('v1: confidence esplicita bassa', 0 < r1.get('confidence', 0) < 0.5 and r1.get('needs_manual_select'), r1)

    # =====================================================================
    print('\nD11) Materiale da testi non pertinenti')
    def _d11(doc, m):
        rett(m, 0, 0, 100, 50)
        m.add_text('FERROTRACK srl', dxfattribs={'insert': (500, 10)})
        m.add_text('Dis. 5401', dxfattribs={'insert': (500, 20)})
        m.add_text('Codice A316-02', dxfattribs={'insert': (500, 30)})
    mc = S.estrai_materiale_da_cartiglio(dxf('d11_rumore.dxf', _d11))
    check('ragione sociale / codici non sono materiali', mc['materiale'] == '', mc)

    def _d11b(doc, m):
        m.add_text('Materiale: S235JR', dxfattribs={'insert': (500, 10)})
    mc = S.estrai_materiale_da_cartiglio(dxf('d11_inline.dxf', _d11b))
    check('"Materiale: S235JR" = S235', mc['materiale'] == 'S235' and mc['confidence'] >= 0.9, mc)

    def _d11c(doc, m):
        m.add_text('MAT.', dxfattribs={'insert': (500, 10)})
        m.add_text('AISI 304', dxfattribs={'insert': (520, 10)})
        m.add_text('FERROTRACK srl', dxfattribs={'insert': (900, 900)})
    mc = S.estrai_materiale_da_cartiglio(dxf('d11_label.dxf', _d11c))
    check('etichetta "MAT." + "AISI 304" = INOX_304', mc['materiale'] == 'INOX_304', mc)

    def _d11d(doc, m):
        m.add_text('Lamiera AISI 304 finitura 2B', dxfattribs={'insert': (500, 10)})
    mc = S.estrai_materiale_da_cartiglio(dxf('d11_forte.dxf', _d11d))
    check('senza etichetta: designazione forte, confidence bassa',
          mc['materiale'] == 'INOX_304' and mc['confidence'] < 0.5, mc)

    # =====================================================================
    print('\nD12) Cache: chiave e DXF pulito')
    k1 = dxf_cache.chiave_cache('abc', 'pezzo_sp3.dxf', CFG)
    check('chiave diversa con nome file diverso', k1 != dxf_cache.chiave_cache('abc', 'pezzo_sp5.dxf', CFG))
    check('chiave diversa con config diversa',
          k1 != dxf_cache.chiave_cache('abc', 'pezzo_sp3.dxf', {**CFG, 'dxf_colori_saldatura': [1, 6]}))
    check('chiave stabile', k1 == dxf_cache.chiave_cache('abc', 'pezzo_sp3.dxf', dict(CFG)))

    from backend.preventivi import dxf_batch_worker
    prev_a, prev_b = os.path.join(TMP, 'prev_a'), os.path.join(TMP, 'prev_b')
    os.makedirs(prev_a)
    os.makedirs(prev_b)

    def _d12a(doc, m):
        rett(m, 0, 0, 120, 80)
        m.add_circle((60, 40), 5)
        m.add_text('CARTIGLIO', dxfattribs={'insert': (400, 300)})   # foglio più grande del pezzo
    p12a = dxf('d12_cache.dxf', _d12a)
    shutil.copy(p12a, os.path.join(prev_a, 'piastra.dxf'))
    shutil.copy(p12a, os.path.join(prev_b, 'piastra.dxf'))
    ra = dxf_batch_worker.process_single_dxf(os.path.join(prev_a, 'piastra.dxf'), 'piastra.dxf', CFG)
    rb = dxf_batch_worker.process_single_dxf(os.path.join(prev_b, 'piastra.dxf'), 'piastra.dxf', CFG)
    check('secondo import = cache hit', rb.get('_cache_hit') is True, rb.get('_cache_hit'))
    nome_pulito = (rb.get('cleanup') or {}).get('cleaned_dxf_filename')
    check('cache hit: DXF pulito creato nella cartella del preventivo corrente',
          nome_pulito and os.path.exists(os.path.join(prev_b, nome_pulito)), rb.get('cleanup'))
    check('cache hit: stessi numeri', vicino(ra['geometria']['area_dm2'], rb['geometria']['area_dm2']))

    def _d12(doc, m):
        rett(m, 0, 0, 100, 50)
        m.add_text('MATERIALE', dxfattribs={'insert': (500, 10)})
        m.add_text('HARDOX 450', dxfattribs={'insert': (530, 10)})
    p12 = dxf('d12_llm.dxf', _d12)
    r12 = dxf_batch_worker.process_single_dxf(p12, 'd12_llm.dxf', CFG)
    check('materiale non riconosciuto senza Gemini: segnalato', r12['cartiglio'].get('_llm_non_disponibile') is True,
          r12['cartiglio'])
    r12b = dxf_batch_worker.process_single_dxf(p12, 'd12_llm.dxf', CFG)
    check('... e NON messo in cache', not r12b.get('_cache_hit'), r12b.get('_cache_hit'))

    # =====================================================================
    print('\nD13) Cache LLM: errori transitori non ricordati')
    chiamate = {'n': 0}

    class _Resp:
        text = json.dumps({'materiale': 'S235', 'confidence': 0.9})

    class _Model:
        def __init__(self, *a, **k):
            pass

        def generate_content(self, prompt):
            chiamate['n'] += 1
            if chiamate['n'] == 1:
                raise RuntimeError('timeout di rete')
            return _Resp()

    fake_genai = types.ModuleType('google.generativeai')
    fake_genai.configure = lambda **k: None
    fake_genai.GenerativeModel = _Model
    fake_google = types.ModuleType('google')
    fake_google.generativeai = fake_genai
    salvati = {k: sys.modules.get(k) for k in ('google', 'google.generativeai')}
    sys.modules['google'] = fake_google
    sys.modules['google.generativeai'] = fake_genai
    orig_key = LLM._get_api_key
    LLM._get_api_key = lambda: 'chiave-finta'
    try:
        LLM.normalize_via_llm.cache_clear()
        m1 = LLM.normalizza_con_esito('FE360 speciale')
        m2 = LLM.normalizza_con_esito('FE360 speciale')
        m3 = LLM.normalizza_con_esito('FE360 speciale')
        check('1a chiamata: errore transitorio', m1 == (None, 'errore'), m1)
        check('2a chiamata: riprova e riconosce', m2 == ('S235', 'ok'), m2)
        check('3a chiamata: dalla cache (nessuna nuova chiamata)', m3 == ('S235', 'ok') and chiamate['n'] == 2,
              (m3, chiamate['n']))
    finally:
        LLM._get_api_key = orig_key
        LLM.normalize_via_llm.cache_clear()
        for k, v in salvati.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    # =====================================================================
    print('\nD14) n_forature = inneschi anche nel percorso manuale (pick_part)')
    def _d14(doc, m):
        rett_linee(m, 0, 0, 200, 100)
        m.add_circle((50, 50), 5)
        m.add_circle((150, 50), 5)
    p14 = dxf('d14_pick.dxf', _d14)
    rc = PP.follow_contour_from_click(p14, 100, 0, CFG)
    check('follow: n_forature = 3 (1 contorno + 2 fori)', rc.get('n_forature') == 3, rc.get('n_forature'))
    check('follow: n_fori = 2, n_pierce = 3', rc.get('n_fori') == 2 and rc.get('n_pierce') == 3, rc)
    cands = PP.pick_candidates(p14, 100, 0, CFG).get('candidates') or [{}]
    check('pick_candidates: n_forature = 3', cands[0].get('n_forature') == 3, cands[0].get('n_forature'))
    check('v3 automatico: n_forature = 3', geo(p14)['n_forature'] == 3)

    # =====================================================================
    print('\nD15) Fallback rettangolo dopo cleanup: solo contorni interni, bulge')
    def _d15(doc, m):
        # contorno esterno con angoli raccordati (bulge) + asola (bulge) + foro
        m.add_lwpolyline([(10, 0, 0), (90, 0, 0.4142), (100, 10, 0), (100, 40, 0.4142), (90, 50, 0),
                          (10, 50, 0.4142), (0, 40, 0), (0, 10, 0.4142)], format='xyb', close=True)
        m.add_lwpolyline([(30, 20, 0), (50, 20, 1), (50, 30, 0), (30, 30, 1)], format='xyb', close=True)
        m.add_circle((75, 25), 5)
    fb = dxf_cleanup.perimetro_interni_fallback(dxf('d15_bulge.dxf', _d15), [0, 0, 100, 50])
    asola = 2 * 20 + math.pi * 10
    check('perimetro interni = asola (con archi) + foro', vicino(fb['perim_fori_mm'], asola + P_FORO10, 0.5),
          (fb['perim_fori_mm'], asola + P_FORO10))
    check('inneschi = 1 + 2', fb['n_pierce'] == 3, fb)

    # =====================================================================
    print('\nD16) v1: duplicati, area netta, confidence bassa')
    def _d16(doc, m):
        rett(m, 0, 0, 200, 100)
        rett(m, 0, 0, 200, 100)
        m.add_circle((50, 50), 5)
        m.add_circle((150, 50), 5)
    r1 = S.estrai_geometria_taglio(dxf('d16_v1.dxf', _d16), {**CFG, 'dxf_scanner_version': 'v1'})
    perim_atteso = (600 + 2 * P_FORO10) / 1000
    check('v1: perimetro senza duplicati', vicino(r1['perimetro_taglio_m'], perim_atteso), (r1['perimetro_taglio_m'], perim_atteso))
    area_attesa = (20000 - 2 * A_FORO10) / 10000
    check('v1: area netta (contorno - fori), non bbox', vicino(r1['area_dm2'], area_attesa), (r1['area_dm2'], area_attesa))
    check('v1: 3 inneschi', r1['n_forature'] == 3, r1['n_forature'])

    # =====================================================================
    print('\nExtra) Svasature/filetti, ELLIPSE, giunzioni, stock, più pezzi')
    def _rondella(doc, m):
        m.add_circle((0, 0), 30)
        m.add_circle((0, 0), 12.5)
    pr = dxf('x_rondella.dxf', _rondella)
    check('rondella Ø60/Ø25: nessuna svasatura', S.scansiona_dxf_dettagli(pr, CFG)[3] == 0, S.scansiona_dxf_dettagli(pr, CFG))
    r = geo(pr)
    check('rondella: area anello', vicino(r['area_dm2'], math.pi * (900 - 156.25) / 10000), r['area_dm2'])

    def _filetto(doc, m):
        rett(m, 0, 0, 100, 100)
        m.add_circle((50, 50), 3.4)
        m.add_arc((50, 50), 4.0, 0, 135)
        m.add_arc((50, 50), 4.0, 135, 270)
    check('filetto con arco spezzato in 2 = 1 filettatura',
          S.scansiona_dxf_dettagli(dxf('x_filetto.dxf', _filetto), CFG)[2] == 1)

    def _ellisse(doc, m):
        m.add_ellipse((50, 0), major_axis=(50, 0), ratio=0.5, start_param=0, end_param=math.pi)
        m.add_line((0, 0), (100, 0))
    r = geo(dxf('x_ellisse.dxf', _ellisse))
    check('mezza ELLIPSE aperta + linea = contorno chiuso',
          vicino(r['area_dm2'], math.pi * 50 * 25 / 2 / 10000, 0.003), r['area_dm2'])

    def _gap(doc, m):
        m.add_line((0, 0), (50.24, 0))
        m.add_line((50.26, 0), (100, 0))
        m.add_line((100, 0), (100, 50))
        m.add_line((100, 50), (0, 50))
        m.add_line((0, 50), (0, 0))
    r = geo(dxf('x_gap.dxf', _gap))
    check('estremi a 0.02mm a cavallo della griglia: contorno chiuso', vicino(r['area_dm2'], 0.5), r['area_dm2'])

    check('stock: 0.8 e 2.5 riconosciuti', PP._snap_stock(0.8) == 0.8 and PP._snap_stock(2.45) == 2.5,
          (PP._snap_stock(0.8), PP._snap_stock(2.45)))

    def _due(doc, m):
        rett(m, 0, 0, 100, 50)
        rett(m, 200, 0, 100, 50)
    r = geo(dxf('x_due_pezzi.dxf', _due))
    check('due pezzi uguali: segnalati', r['n_pezzi_rilevati'] == 2 and any('confrontabili' in w for w in r['warnings']),
          (r.get('n_pezzi_rilevati'), r['warnings']))
    check('due pezzi uguali: confidence bassa', r['needs_manual_select'], r['confidence'])

    print('\n' + '=' * 60)
    print(f'PASSATI: {OK}   FALLITI: {len(KO)}')
    if KO:
        print('Falliti: ' + ', '.join(KO))
    print('=' * 60)
    return 1 if KO else 0


if __name__ == '__main__':
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
