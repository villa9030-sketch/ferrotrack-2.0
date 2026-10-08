"""Test della PULIZIA DXF (<nome>_cleaned.dxf = solo contorno + fori del pezzo).

Bug riprodotto sui DXF reali (22/23 puliti errati): l'auto-cleanup dell'import
passava il bbox del detector a `save_cleaned_dxf` (cluster per prossimità +
score "cerchi×10") → il pulito era la CORNICE del foglio col cartiglio
(288×196, 576×392…) oppure una sola svasatura (2 cerchi concentrici). E la GET
del preventivo sovrascriveva le misure dell'articolo leggendo quel pulito.

Casi (DXF sintetici generati con ezdxf):
 P1 piastra 170×100 con fori + svasature dentro cornice A4 con cartiglio,
    quote e testi → pulito 170×100, fori passanti sì, smussi/cornice no
 P2 pezzo definito dentro un BLOCCO (INSERT traslato) → pulito 200×80
 P3 DXF in POLLICI ($INSUNITS=1) → pulito convertito in millimetri ($INSUNITS=4)
 P4 detector non sicuro (due pezzi confrontabili) → pulizia saltata col motivo
 P5 pulito "legacy" errato: rigenerato una volta dall'originale; misure
    salvate sull'articolo mai sovrascritte; pulito diverso dalle misure → non
    affidabile; legacy manuale incompatibile con l'area → non affidabile
 P6 drag manuale (save_cleaned_dxf): rettangolo attorno al pezzo → non vince
    né la cornice né la svasatura
 P7 should_cleanup: rapporto area pezzo/foglio con DXF in pollici

Si importano solo i moduli DXF (senza avviare l'app Flask).
Esecuzione: python app/tests/test_dxf_pulizia.py   (exit 0 = tutto ok)
"""
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

import logging  # noqa: E402
logging.disable(logging.CRITICAL)

import ezdxf  # noqa: E402
from ezdxf import bbox as ezbbox  # noqa: E402

from backend.preventivi import dxf_cleanup as C  # noqa: E402
from backend.preventivi import dxf_batch_worker as W  # noqa: E402
from backend.preventivi.dxf_polygon_detector_v3 import detect_pezzo_geometry_v3, scala_unita_mm  # noqa: E402

TMP = tempfile.mkdtemp(prefix='test_pulizia_')
CFG = {'dxf_colori_piega': [2], 'dxf_colori_saldatura': [1]}

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


def dxf(nome, build, units=4):
    doc = ezdxf.new('R2010', units=units)
    build(doc, doc.modelspace())
    p = os.path.join(TMP, nome)
    doc.saveas(p)
    return p


def rett_linee(m, x0, y0, w, h, **attr):
    pts = [(x0, y0), (x0 + w, y0), (x0 + w, y0 + h), (x0, y0 + h)]
    for a, b in zip(pts, pts[1:] + pts[:1]):
        m.add_line(a, b, dxfattribs=attr)


def cornice_a4(m, s=1.0):
    """Cornice A4 (297×210) con bordo interno e cartiglio a celle, quote, testi.
    s = fattore per disegni in altre unità (1/25.4 per i pollici)."""
    rett_linee(m, 0, 0, 297 * s, 210 * s)
    rett_linee(m, 5 * s, 5 * s, 287 * s, 200 * s)
    # cartiglio: celle attaccate al bordo interno (in basso a destra)
    x0, y0 = 172 * s, 5 * s
    rett_linee(m, x0, y0, 120 * s, 40 * s)
    for k in (1, 2, 3):
        m.add_line((x0, y0 + 10 * k * s), (x0 + 120 * s, y0 + 10 * k * s))
    m.add_line((x0 + 60 * s, y0), (x0 + 60 * s, y0 + 40 * s))
    m.add_text('DISEGNO 12B100114', dxfattribs={'height': 3 * s, 'insert': (x0 + 2 * s, y0 + 32 * s)})
    m.add_text('MATERIALE S235 sp.3', dxfattribs={'height': 3 * s, 'insert': (x0 + 2 * s, y0 + 22 * s)})
    # simbolo di proiezione (cerchi concentrici nel cartiglio)
    m.add_circle((x0 + 90 * s, y0 + 20 * s), 3 * s)
    m.add_circle((x0 + 90 * s, y0 + 20 * s), 6 * s)


def estensione_mm(path):
    d = ezdxf.readfile(path)
    s = scala_unita_mm(d)[0]
    e = ezbbox.extents(d.modelspace())
    return (e.extmax.x - e.extmin.x) * s, (e.extmax.y - e.extmin.y) * s


def vicino2(a, b, tol=2.0):
    return a is not None and all(abs(x - y) <= tol for x, y in zip(a, b))


def raggi_cerchi(path):
    d = ezdxf.readfile(path)
    return sorted(round(e.dxf.radius, 3) for e in d.modelspace() if e.dxftype() == 'CIRCLE')


def pulisci(path, nome_pulito=None):
    """Pipeline dell'import: detector → _esegui_cleanup (come process_single_dxf)."""
    g = detect_pezzo_geometry_v3(path, CFG)
    info = W._esegui_cleanup(path, g, os.path.basename(path), CFG)
    cp = None
    if info.get('cleaned_dxf_filename'):
        cp = os.path.join(os.path.dirname(path), info['cleaned_dxf_filename'])
    return g, info, cp


# ── Piastra 170×100: contorno a LINE con raccordi ad ARC, 4 fori Ø9 svasati
#    (passante r=4.5 + smusso r=9.125) e 2 fori Ø6 semplici, dentro cornice
def _piastra(m, x0=60.0, y0=60.0, s=1.0):
    r = 5.0 * s
    W_, H_ = 170.0 * s, 100.0 * s
    x0, y0 = x0 * s, y0 * s
    m.add_line((x0 + r, y0), (x0 + W_ - r, y0))
    m.add_line((x0 + W_, y0 + r), (x0 + W_, y0 + H_ - r))
    m.add_line((x0 + W_ - r, y0 + H_), (x0 + r, y0 + H_))
    m.add_line((x0, y0 + H_ - r), (x0, y0 + r))
    m.add_arc((x0 + r, y0 + r), r, 180, 270)
    m.add_arc((x0 + W_ - r, y0 + r), r, 270, 360)
    m.add_arc((x0 + W_ - r, y0 + H_ - r), r, 0, 90)
    m.add_arc((x0 + r, y0 + H_ - r), r, 90, 180)
    # fori staccati dal contorno (> 2 mm): col vecchio clustering ogni svasatura
    # era un cluster a sé che "batteva" il contorno (caso reale 12B100114)
    for fx, fy in ((25, 25), (145, 25), (145, 75), (25, 75)):
        c = (x0 + fx * s, y0 + fy * s)
        m.add_circle(c, 4.5 * s)
        m.add_circle(c, 9.125 * s)
    m.add_circle((x0 + 85 * s, y0 + 30 * s), 3 * s)
    m.add_circle((x0 + 85 * s, y0 + 70 * s), 3 * s)


def main():
    # =====================================================================
    print('\nP1) Piastra con fori + svasature dentro cornice A4 con cartiglio')

    def _p1(doc, m):
        cornice_a4(m)
        _piastra(m)
        # assi dei fori (attraversano il foro, finiscono dentro il pezzo)
        m.add_line((60 + 85 - 6, 60 + 30), (60 + 85 + 6, 60 + 30), dxfattribs={'layer': 'ASSI'})
        # quota lineare (DIMENSION) e linee di richiamo
        d = m.add_linear_dim(base=(60, 50), p1=(60, 60), p2=(230, 60), dimstyle='Standard')
        d.render()
        m.add_text('170', dxfattribs={'height': 3, 'insert': (140, 45)})
    p1 = dxf('p1_piastra.dxf', _p1)
    g1, info1, cp1 = pulisci(p1)
    check('detector: piastra 170×100', vicino2((g1['bbox_width_mm'], g1['bbox_height_mm']), (170, 100), 0.5),
          (g1.get('bbox_width_mm'), g1.get('bbox_height_mm'), g1.get('confidence')))
    check('pulito creato (auto)', cp1 is not None and os.path.exists(cp1), info1)
    if cp1:
        est = estensione_mm(cp1)
        check('pulito = 170×100 (niente cornice/cartiglio)', vicino2(est, (170, 100)), est)
        rr = raggi_cerchi(cp1)
        check('pulito: 4 passanti r4.5 + 2 fori r3, senza smussi r9.125 né simbolo cartiglio',
              rr == [3.0, 3.0, 4.5, 4.5, 4.5, 4.5], rr)
        d = ezdxf.readfile(cp1)
        tipi = {e.dxftype() for e in d.modelspace()}
        check('pulito: niente testi/quote', not (tipi & {'TEXT', 'MTEXT', 'DIMENSION', 'INSERT'}), tipi)
        check('pulito: niente assi', not any(e.dxf.layer == 'ASSI' for e in d.modelspace()))
        check('pulito marcato come pulizia verificata', C.leggi_dxf_pulito(cp1).get('marcato'))
        st = info1.get('cleanup_stats') or {}
        check('cleanup_stats con misure del pulito',
              vicino2((st.get('bbox_w_mm') or 0, st.get('bbox_h_mm') or 0), (170, 100)), st)

    # =====================================================================
    print('\nP1b) Pezzetto 30×30 senza fori dentro cornice (prima: pulito = foglio)')

    def _p1b(doc, m):
        cornice_a4(m)
        rett_linee(m, 100, 100, 30, 30)
    p1b = dxf('p1b_pezzetto.dxf', _p1b)
    g1b, info1b, cp1b = pulisci(p1b)
    check('pulito creato', cp1b is not None, info1b)
    if cp1b:
        est = estensione_mm(cp1b)
        check('pulito = 30×30 (non la cornice 287×200)', vicino2(est, (30, 30)), est)

    # =====================================================================
    print('\nP2) Pezzo definito dentro un BLOCCO (INSERT traslato)')

    def _p2(doc, m):
        cornice_a4(m)
        b = doc.blocks.new('PEZZO_200x80')
        rett_linee(b, 0, 0, 200, 80)
        b.add_circle((20, 40), 5)
        b.add_circle((180, 40), 5)
        b.add_lwpolyline([(90, 30), (110, 30), (110, 50), (90, 50)], close=True)
        m.add_blockref('PEZZO_200x80', (40, 80))
    p2 = dxf('p2_blocco.dxf', _p2)
    g2, info2, cp2 = pulisci(p2)
    check('detector: pezzo nel blocco 200×80',
          vicino2((g2['bbox_width_mm'], g2['bbox_height_mm']), (200, 80), 0.5),
          (g2.get('bbox_width_mm'), g2.get('bbox_height_mm')))
    check('pulito creato', cp2 is not None, info2)
    if cp2:
        est = estensione_mm(cp2)
        check('pulito = 200×80 (entità del blocco esplose)', vicino2(est, (200, 80)), est)
        n = len(ezdxf.readfile(cp2).modelspace())
        check('pulito: 4 lati + 2 fori + asola = 7 entità', n == 7, n)

    # =====================================================================
    print('\nP3) DXF in POLLICI ($INSUNITS=1) dentro cornice')
    s = 1 / 25.4

    def _p3(doc, m):
        cornice_a4(m, s)
        _piastra(m, s=s)
    p3 = dxf('p3_pollici.dxf', _p3, units=1)
    g3, info3, cp3 = pulisci(p3)
    check('detector: 170×100 mm da pollici',
          vicino2((g3['bbox_width_mm'], g3['bbox_height_mm']), (170, 100), 0.5),
          (g3.get('bbox_width_mm'), g3.get('bbox_height_mm')))
    check('pulito creato', cp3 is not None, info3)
    if cp3:
        d3 = ezdxf.readfile(cp3)
        # Lantek legge millimetri: il pulito e' sempre convertito
        check('pulito in millimetri ($INSUNITS=4)', d3.header.get('$INSUNITS') == 4, d3.header.get('$INSUNITS'))
        est = estensione_mm(cp3)
        check('pulito = 170×100 mm', vicino2(est, (170, 100)), est)
        rr = raggi_cerchi(cp3)
        check('pulito pollici: 6 fori passanti in mm', rr == [3.0, 3.0, 4.5, 4.5, 4.5, 4.5], rr)

    # =====================================================================
    print('\nP4) Detector non sicuro → pulizia saltata con motivo')

    def _p4(doc, m):
        cornice_a4(m)
        rett_linee(m, 20, 60, 100, 60)
        rett_linee(m, 140, 60, 100, 60)
    p4 = dxf('p4_due_pezzi.dxf', _p4)
    g4, info4, cp4 = pulisci(p4)
    check('nessun pulito', cp4 is None and not os.path.exists(p4[:-4] + '_cleaned.dxf'), info4)
    check('motivo esplicito', bool(info4.get('cleanup_reason')), info4)

    # =====================================================================
    print('\nP5) Pulito LEGACY errato: rigenerazione, misure salvate intoccabili')
    # copia dell'originale P1 con un pulito "vecchio stile": solo la svasatura
    d5 = os.path.join(TMP, 'p5')
    os.makedirs(d5)
    orig5 = os.path.join(d5, '12B100114-00.dxf')
    shutil.copy2(p1, orig5)
    legacy = os.path.join(d5, '12B100114-00_cleaned.dxf')

    def scrivi_legacy():
        doc = ezdxf.new('R2010', units=4)
        doc.modelspace().add_circle((80, 80), 4.5)
        doc.modelspace().add_circle((80, 80), 9.125)
        doc.saveas(legacy)
    scrivi_legacy()
    art = {'cleaned_dxf_filename': os.path.basename(legacy), 'cleaned_status': 'auto',
           'dxf_filename': os.path.basename(orig5), 'area_dm2': 1.62}
    info = C.leggi_dxf_pulito(legacy)
    check('legacy: 18×18 non marcato', not info['marcato'] and vicino2((info['w_mm'], info['h_mm']), (18.25, 18.25), 0.1), info)
    es = C.verifica_pulito_articolo(legacy, art, original_path=orig5, config=CFG)
    check('legacy auto rigenerato → affidabile 170×100',
          es['affidabile'] and es['rigenerato'] and vicino2((es['w_mm'], es['h_mm']), (170, 100)), es)
    check('originale intatto', os.path.exists(orig5) and os.path.getsize(orig5) == os.path.getsize(p1))

    # misure già confermate (CAD) diverse dal pulito → pulito NON affidabile
    art_cad = dict(art, bbox_w_mm=120.0, bbox_h_mm=20.0)
    es = C.verifica_pulito_articolo(legacy, art_cad, original_path=orig5, config=CFG)
    check('pulito ≠ misure articolo → non affidabile', not es['affidabile'] and es['motivo'], es)
    # misure confermate uguali (anche ruotate) → affidabile
    es = C.verifica_pulito_articolo(legacy, dict(art, bbox_w_mm=100.0, bbox_h_mm=170.0),
                                    original_path=orig5, config=CFG)
    check('misure articolo ruotate 100×170 → affidabile', es['affidabile'], es)

    # legacy MANUALE (non si rigenera): fiducia solo se compatibile con l'area
    scrivi_legacy()
    art_man = dict(art, cleaned_status='manual')
    es = C.verifica_pulito_articolo(legacy, art_man, original_path=orig5, config=CFG)
    check('legacy manuale 18×18 con area 1.62 dm² → non affidabile',
          not es['affidabile'] and not es['rigenerato'], es)
    # legacy auto il cui originale non consente la pulizia: resta non affidabile
    d6 = os.path.join(TMP, 'p5b')
    os.makedirs(d6)
    orig6 = os.path.join(d6, 'due.dxf')
    shutil.copy2(p4, orig6)
    leg6 = os.path.join(d6, 'due_cleaned.dxf')
    shutil.copy2(p4, leg6)  # "pulito" = foglio intero
    es = C.verifica_pulito_articolo(leg6, {'cleaned_status': 'auto', 'area_dm2': 0.6},
                                    original_path=orig6, config=CFG)
    check('legacy = foglio intero non rigenerabile → non affidabile', not es['affidabile'], es)

    # =====================================================================
    print('\nP6) Drag manuale (save_cleaned_dxf) attorno alla piastra')
    out6 = os.path.join(TMP, 'p6_cleaned.dxf')
    r6 = C.save_cleaned_dxf(p1, out6, (58, 58, 232, 162))
    check('drag: successo', r6.get('success'), r6.get('error'))
    if r6.get('success'):
        est = estensione_mm(out6)
        check('drag: pulito = piastra (non cornice, non svasatura)', vicino2(est, (170, 100), 2.5), est)
        check('drag: marcato manuale', C.leggi_dxf_pulito(out6).get('tipo') == C.TIPO_PULIZIA_MANUALE)

    # =====================================================================
    print('\nP8) Pulito per Lantek: TAGLIO / PIEGA / MARCATURA, in mm veri')

    def _p8(doc, m, s=1.0):
        cornice_a4(m)
        x0, y0 = 60.0, 60.0
        rett_linee(m, x0, y0, 120, 80)
        m.add_circle((x0 + 30, y0 + 40), 2.1)                     # foro M5
        m.add_arc((x0 + 30, y0 + 40), 2.5, 0, 270)                # simbolo filettatura 3/4
        m.add_line((x0 + 80, y0), (x0 + 80, y0 + 80))             # piega da bordo a bordo
        m.add_line((x0 + 90, y0 + 30), (x0 + 100, y0 + 30))       # incisione dentro il pezzo
        m.add_line((x0 + 90, y0 + 30), (x0 + 95, y0 + 40))
        m.add_line((x0 + 20, y0 + 120), (x0 + 60, y0 + 120))      # fuori dal pezzo
    p8 = dxf('p8_lantek.dxf', _p8)
    g8, info8, cp8 = pulisci(p8)
    check('pulito creato', cp8 is not None, info8)
    check('segnato come formato Lantek', cp8 is not None and C.leggi_dxf_pulito(cp8).get('lantek'))
    if cp8:
        d8 = ezdxf.readfile(cp8)
        per = {}
        for e in d8.modelspace():
            per.setdefault(e.dxf.layer, []).append(e.dxftype())
        check('TAGLIO: 4 lati + foro', sorted(per.get('TAGLIO', [])) == ['CIRCLE', 'LINE', 'LINE', 'LINE', 'LINE'], per)
        check('PIEGA: la linea da bordo a bordo', per.get('PIEGA') == ['LINE'], per)
        check('MARCATURA: i due tratti incisi', per.get('MARCATURA') == ['LINE', 'LINE'], per)
        check('simbolo di filettatura tolto', 'ARC' not in sum(per.values(), []), per)
        check('niente fuori dal pezzo', sum(len(v) for v in per.values()) == 8, per)
        g8b = detect_pezzo_geometry_v3(cp8, CFG)
        check('il pulito riletto ha le stesse misure (piega e marcatura ignorate)',
              vicino2((g8b['bbox_width_mm'], g8b['bbox_height_mm']), (120, 80), 0.1)
              and abs(g8b['area_dm2'] - g8['area_dm2']) < 1e-3, (g8b.get('area_dm2'), g8.get('area_dm2')))

    # disegno in scala 2:1 con viste 1:1 (191700612-00): pulito al vero
    def _p8b(doc, m):
        x0, y0 = 60.0, 60.0
        rett_linee(m, x0, y0, 60, 184)                            # sviluppo disegnato 2:1
        m.add_circle((x0 + 30, y0 + 20), 4.2)
        for p1, p2 in (((x0, y0 - 10), (x0 + 60, y0 - 10)), ((x0 - 10, y0), (x0 - 10, y0 + 184))):
            dm = m.add_linear_dim(base=p1, p1=(x0, y0) if p1[1] < y0 else (x0, y0), p2=(x0 + 60, y0) if p1[1] < y0 else (x0, y0 + 184),
                                  angle=0 if p1[1] < y0 else 90, dimstyle='Standard', override={'dimlfac': 0.5})
            dm.render()
        rett_linee(m, 200, 60, 30, 48)                            # vista piegata 1:1
        dm = m.add_linear_dim(base=(200, 50), p1=(200, 60), p2=(230, 60), dimstyle='Standard')
        dm.render()
    p8b = dxf('p8b_scala.dxf', _p8b)
    g8s, info8s, cp8s = pulisci(p8b)
    check('pulito in scala creato', cp8s is not None, info8s)
    if cp8s:
        est = estensione_mm(cp8s)
        check('pulito al vero: 30×92 mm', vicino2(est, (30, 92), 0.1), est)
        check('foro al vero: r 2,1', raggi_cerchi(cp8s) == [2.1], raggi_cerchi(cp8s))

    print('\nP9) Riquadro a mano → pulito convertito nel formato Lantek')
    out9 = os.path.join(TMP, 'p9_riquadro_cleaned.dxf')
    r9 = C.save_cleaned_dxf(p8, out9, (55, 55, 185, 145))
    check('riquadro: pulito scritto', r9.get('success'), r9)
    c9 = C.converti_pulito_in_lantek(p8, out9, CFG)
    check('convertito', c9.get('success'), c9)
    if c9.get('success'):
        d9 = ezdxf.readfile(out9)
        lay = sorted({e.dxf.layer for e in d9.modelspace()})
        check('layer TAGLIO/PIEGA/MARCATURA', lay == ['MARCATURA', 'PIEGA', 'TAGLIO'], lay)
        check('filetto tolto anche qui', not any(e.dxftype() == 'ARC' for e in d9.modelspace()))
        check('marcato manuale e Lantek', C.leggi_dxf_pulito(out9).get('tipo') == C.TIPO_PULIZIA_MANUALE
              and C.leggi_dxf_pulito(out9).get('lantek'))
    out9b = os.path.join(TMP, 'p9b_scala_cleaned.dxf')
    r9b = C.save_cleaned_dxf(p8b, out9b, (55, 55, 125, 250))
    c9b = C.converti_pulito_in_lantek(p8b, out9b, CFG)
    check('disegno 2:1 col riquadro: convertito', r9b.get('success') and c9b.get('success'), (r9b.get('error'), c9b.get('error')))
    if c9b.get('success'):
        check('riquadro su disegno 2:1 → 30×92 mm', vicino2(estensione_mm(out9b), (30, 92), 0.1), estensione_mm(out9b))
        check('bbox in mm per l\'articolo', vicino2((c9b['bbox_mm_mm'][2] - c9b['bbox_mm_mm'][0],
                                                    c9b['bbox_mm_mm'][3] - c9b['bbox_mm_mm'][1]), (30, 92), 0.1), c9b.get('bbox_mm_mm'))
    check('gia\' Lantek: non si riconverte', not C.converti_pulito_in_lantek(p8, out9, CFG).get('success'))

    print('\nP10) Verifica finale per Lantek')
    if cp8:
        area8 = g8['area_dm2']
        v = C.verifica_lantek(cp8, {'bbox_w_mm': 120, 'bbox_h_mm': 80, 'area_dm2': area8})
        check('pezzo giusto → pronto', v['stato'] == 'pronto', v)
        v = C.verifica_lantek(cp8, {'bbox_w_mm': 80, 'bbox_h_mm': 120, 'area_dm2': area8})
        check('ruotato di 90° → pronto', v['stato'] == 'pronto', v)
        v = C.verifica_lantek(cp8, {'bbox_w_mm': 150, 'bbox_h_mm': 80, 'area_dm2': area8})
        check('ingombro diverso → da guardare', v['stato'] == 'da_guardare' and 'ingombro' in v['motivi'][0], v)
        v = C.verifica_lantek(cp8, {'bbox_w_mm': 120, 'bbox_h_mm': 80, 'area_dm2': area8 * 1.2})
        check('area diversa → da guardare', v['stato'] == 'da_guardare', v)
        v = C.verifica_lantek(cp8, {'bbox_w_mm': 120, 'bbox_h_mm': 80, 'area_dm2': area8 * 1.2, 'area_stimata_piega': True})
        check('sviluppo stimato a mano: area non confrontata', v['stato'] == 'pronto', v)
    v = C.prepara_pulito_lantek(p8, None, {})
    check('senza pulito → da preparare in Lantek', v['stato'] == 'da_guardare', v)
    vecchio = os.path.join(TMP, 'p10_vecchio_cleaned.dxf')
    C.save_cleaned_dxf(p8, vecchio, (55, 55, 185, 145))
    v = C.prepara_pulito_lantek(p8, vecchio, {'cleaned_status': 'manual', 'bbox_w_mm': 120, 'bbox_h_mm': 80})
    check('pulito vecchio a mano → convertito e pronto', v['stato'] == 'pronto' and C.leggi_dxf_pulito(vecchio).get('lantek'), v)

    print('\nP7) should_cleanup: rapporto area pezzo/foglio in pollici')
    ok, motivo = C.should_cleanup({'confidence': 0.9, 'area_dm2': 1.0,
                                   'dxf_bbox_mm': [0, 0, 11.7, 8.3], 'scala_unita_mm': 25.4})
    check('foglio A4 in pollici, pezzo 1 dm² → pulizia ammessa', ok, motivo)
    ok, motivo = C.should_cleanup({'confidence': 0.9, 'area_dm2': 1.0,
                                   'dxf_bbox_mm': [0, 0, 4.0, 4.0], 'scala_unita_mm': 25.4})
    check('pezzo che riempie il foglio → saltata', not ok and 'area_ratio' in motivo, motivo)

    print('\nP8) sicuro solo con un riscontro indipendente e senza indizi di taglio dubbio')
    from backend.preventivi import sicurezza_import as SI
    base = {'diag': {'lati_quotati': 2, 'foro_min_mm': 8.0}, 'area_dm2': 1.0, 'sp_mm': 3.0,
            'sp_indipendente': 3.0, 'densita': 7.85, 'peso_cart': 0.2355, 'peso_cart_conf': 0.95,
            'scala': 1.0, 'pul_n_piega': 0, 'pul_n_lung_marcatura_mm': 0.0, 'pul_n_simboli_tolti': 0}
    ok, motivi = SI.decidi_sicuro(base)
    check('quotato, peso giusto → sicuro', ok, motivi)
    ok, motivi = SI.decidi_sicuro({**base, 'diag': {'lati_quotati': 0}, 'peso_cart': None})
    check('nessun riscontro → da verificare', not ok and 'riscontro' in motivi[0], motivi)
    ok, motivi = SI.decidi_sicuro({**base, 'diag': {'lati_quotati': 0}})
    check('senza quote ma peso giusto → sicuro', ok, motivi)
    ok, motivi = SI.decidi_sicuro({**base, 'peso_cart': 0.35})
    check('pesa meno del cartiglio → da verificare', not ok and 'peso' in motivi[0], motivi)
    ok, motivi = SI.decidi_sicuro({**base, 'diag': {'lati_quotati': 2, 'foro_min_mm': 2.0}})
    check('foro piccolo con spessore sicuro → resta sicuro (va al trapano da solo)', ok, motivi)
    ok, motivi = SI.decidi_sicuro({**base, 'fori_trapano_dubbio': True})
    check('fori piccoli con spessore incerto → da verificare',
          not ok and 'laser o trapano' in motivi[0], motivi)

    print('\nP11) fori da trapano: sotto 2/3 dello spessore (per difetto al mm) non si tagliano')
    for t, d in ((10, 6), (12, 8), (8, 5), (5, 3), (3, 2), (1.5, 1)):
        check(f'spessore {t:g} → laser da Ø{d}', W.diametro_min_laser(t) == d, W.diametro_min_laser(t))

    def _p11(doc, m):
        cornice_a4(m)
        rett_linee(m, 20, 60, 200, 100)
        for x in (50, 190):
            m.add_circle((x, 90), 2.5)      # Ø5: al trapano su 10 mm
            m.add_circle((x, 130), 4.0)     # Ø8: al laser
        m.add_arc((50, 90), 3.0, 0, 270)    # simbolo di filetto M6 attorno al Ø5
        rett_linee(m, 115, 105, 4, 4)       # quadretto 4x4: non tondo, resta al laser
    p11 = dxf('p11_trapano.dxf', _p11)
    g11 = detect_pezzo_geometry_v3(p11, CFG)
    n0, a0, per0 = g11.get('n_pierce'), g11.get('area_dm2'), g11.get('perimetro_taglio_m')
    check('detector: 5 fori, 4 tondi', g11.get('n_fori') == 5 and len(g11.get('fori_tondi') or []) == 4,
          (g11.get('n_fori'), g11.get('fori_tondi')))
    sicuro = {'spessore_mm': 10.0, 'confidence': 0.9, 'fonti': [{'spessore_mm': 10.0, 'source': 'testo'}]}
    g11t = W.applica_fori_trapano(g11, sicuro)
    ft = g11t.get('fori_trapano') or []
    check('spessore 10 sicuro: i due Ø5 al trapano, gli Ø8 al laser',
          len(ft) == 2 and all(abs(f['d_mm'] - 5) < 0.05 for f in ft), ft)
    check('inneschi e fori -2', g11t['n_pierce'] == n0 - 2 and g11t['n_fori'] == 3,
          (g11t['n_pierce'], g11t['n_fori']))
    check('area + 2 fori Ø5, perimetro - 2 circonferenze',
          abs(g11t['area_dm2'] - a0 - 2 * math.pi * 2.5 ** 2 / 1e4) < 3e-4
          and abs(per0 - g11t['perimetro_taglio_m'] - 2 * math.pi * 5 / 1000) < 5e-4,
          (a0, g11t['area_dm2'], per0, g11t['perimetro_taglio_m']))
    check('avviso trapano', any('al trapano' in w for w in g11t.get('warnings') or []), g11t.get('warnings'))
    check('spessore 3 sicuro: nessun foro al trapano',
          not W.applica_fori_trapano(g11, {'spessore_mm': 3.0, 'confidence': 0.9}).get('fori_trapano'))
    incerto = {'spessore_mm': 3.0, 'confidence': 0.55,
               'fonti': [{'spessore_mm': 3.0, 'source': 'cella'}, {'spessore_mm': 10.0, 'source': 'peso_area'}]}
    g11i = W.applica_fori_trapano(g11, incerto)
    check('spessore incerto (3 o 10): geometria invariata, dubbio segnato',
          g11i.get('fori_trapano_dubbio') and not g11i.get('fori_trapano') and g11i['n_pierce'] == n0
          and g11i['area_dm2'] == a0, g11i.get('fori_trapano_dubbio'))
    ind = SI.raccogli_indizi(p11, g11i, {}, incerto, None, {}, g11i.get('confidence'))
    ok, motivi = SI.decidi_sicuro(ind)
    check('spessore incerto → non sicuro col motivo laser/trapano',
          not ok and any('laser o trapano' in m for m in motivi), motivi)
    g11n = W.applica_fori_trapano(g11, {'spessore_mm': 3.0, 'confidence': 0.55,
                                        'fonti': [{'spessore_mm': 3.0, 'source': 'cella'}]})
    check('spessore incerto ma nessuna lettura manda fori al trapano → nessun dubbio',
          not g11n.get('fori_trapano_dubbio'), g11n.get('fori_trapano_dubbio'))
    info11 = W._esegui_cleanup(p11, g11t, 'p11_trapano.dxf', CFG)
    cp11 = os.path.join(TMP, info11.get('cleaned_dxf_filename') or 'manca')
    check('pulito creato', os.path.exists(cp11), info11)
    if os.path.exists(cp11):
        d11 = ezdxf.readfile(cp11).modelspace()
        tag = [e for e in d11 if e.dxf.layer == C.LAYER_TAGLIO]
        cer = sorted(round(e.dxf.radius, 2) for e in tag if e.dxftype() == 'CIRCLE')
        check('TAGLIO: solo i due Ø8 (niente Ø5)', cer == [4.0, 4.0], cer)
        croci = [e for e in d11 if e.dxf.layer == C.LAYER_MARCATURA and e.dxftype() == 'LINE'
                 and abs(math.dist(e.dxf.start, e.dxf.end) - 4.0) < 1e-6]
        centri = sorted({(round((e.dxf.start.x + e.dxf.end.x) / 2, 2),
                          round((e.dxf.start.y + e.dxf.end.y) / 2, 2)) for e in croci})
        check('MARCATURA: croce (bracci 2 mm) sul centro di ogni Ø5',
              len(croci) == 4 and centri == [(50.0, 90.0), (190.0, 90.0)], centri)
        check('filetto attorno al Ø5 non copiato, niente marcatura del disegno',
              not any(e.dxftype() == 'ARC' for e in d11)
              and not info11['cleanup_stats'].get('n_lung_marcatura_mm'), info11['cleanup_stats'])
        v = C.verifica_lantek(cp11, {'bbox_w_mm': 200, 'bbox_h_mm': 100, 'area_dm2': g11t['area_dm2']})
        check('pulito pronto per Lantek con l\'area senza i fori da trapano', v['stato'] == 'pronto', v)
    ok, motivi = SI.decidi_sicuro({**base, 'pul_n_lung_marcatura_mm': 150.0})
    check('linee aperte dentro il pezzo → da verificare', not ok, motivi)
    ok, motivi = SI.decidi_sicuro({**base, 'diag': {'lati_quotati': 2, 'n_segni': 1}})
    check('foro a forma di lettera → da verificare', not ok, motivi)

    print(f'\nRisultato: {OK} ok, {len(KO)} ko')
    for k in KO:
        print('  KO:', k)
    shutil.rmtree(TMP, ignore_errors=True)
    return 0 if not KO else 1


if __name__ == '__main__':
    sys.exit(main())
