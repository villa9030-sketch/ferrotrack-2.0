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
 P3 DXF in POLLICI ($INSUNITS=1) → misure convertite, unità conservate
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
        check('pulito conserva $INSUNITS=1', d3.header.get('$INSUNITS') == 1, d3.header.get('$INSUNITS'))
        est = estensione_mm(cp3)
        check('pulito = 170×100 mm', vicino2(est, (170, 100)), est)
        rr = raggi_cerchi(cp3)
        check('pulito pollici: 6 fori passanti', len(rr) == 6 and max(rr) < 0.2, rr)

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
    print('\nP7) should_cleanup: rapporto area pezzo/foglio in pollici')
    ok, motivo = C.should_cleanup({'confidence': 0.9, 'area_dm2': 1.0,
                                   'dxf_bbox_mm': [0, 0, 11.7, 8.3], 'scala_unita_mm': 25.4})
    check('foglio A4 in pollici, pezzo 1 dm² → pulizia ammessa', ok, motivo)
    ok, motivo = C.should_cleanup({'confidence': 0.9, 'area_dm2': 1.0,
                                   'dxf_bbox_mm': [0, 0, 4.0, 4.0], 'scala_unita_mm': 25.4})
    check('pezzo che riempie il foglio → saltata', not ok and 'area_ratio' in motivo, motivo)

    print(f'\nRisultato: {OK} ok, {len(KO)} ko')
    for k in KO:
        print('  KO:', k)
    shutil.rmtree(TMP, ignore_errors=True)
    return 0 if not KO else 1


if __name__ == '__main__':
    sys.exit(main())
