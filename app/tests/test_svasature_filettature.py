"""Test di SVASATURE e FILETTATURE (preventivatore, import del pacchetto).

- lettura dei callout dei fori (lavorazioni_testi): SolidWorks/Inventor con
  codici di formattazione, descrizioni del cartiglio, note, minuterie;
- callout esploso in piu' testi e righe del PDF;
- geometria: svasature "strette" (Ø7,47/Ø11,47) con lo stesso pezzo in due
  viste → contate una volta sola;
- il caso reale 25CCPA0998-00 (6 svasature, 0 filettature) se il disegno e'
  presente su questo PC (non e' nel repository: e' del cliente).

Esecuzione: python app/tests/test_svasature_filettature.py   (exit 0 = tutto ok)
"""
import os
import shutil
import sys
import tempfile
import types

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)
if 'backend' not in sys.modules:
    _pkg = types.ModuleType('backend')
    _pkg.__path__ = [os.path.join(_APP, 'backend')]
    sys.modules['backend'] = _pkg

import logging  # noqa: E402
logging.disable(logging.CRITICAL)

import ezdxf  # noqa: E402

from backend.preventivi import dxf_scanner as S  # noqa: E402
from backend.preventivi import lavorazioni_testi as LT  # noqa: E402

TMP = tempfile.mkdtemp(prefix='test_svas_')
CFG = {
    'dxf_colori_piega': [2], 'dxf_colori_saldatura': [1],
    'dxf_lunghezza_minima': 15.0, 'dxf_tolleranza_centro': 1.0,
    'dxf_svasatura_ratio_min': 1.8, 'dxf_svasatura_ratio_max': 3.0,
    'dxf_semicerchio_angolo_min': 150.0, 'dxf_semicerchio_angolo_max': 320.0,
    'dxf_filtra_zona_sviluppata': True,
}
CASO_REALE = (r'C:\Users\Stefano\Desktop\AUTOCAD\TAGLIO\DECA\ORDINI\ls 1153-26'
              r'\ls 1153-26\25CCPA0998-00.DXF')

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


def sv_fil(testo):
    r = LT.analizza_callout(testo)
    return r['svasature'], r['filettature']


def dxf(nome, build):
    doc = ezdxf.new('R2010', units=4)
    build(doc, doc.modelspace())
    p = os.path.join(TMP, nome)
    doc.saveas(p)
    return p


def main():
    print('\n1) Callout dei fori (testo)')
    casi = [
        # (testo, svasature, filettature)
        (r'6 x <> PASSANTE\P{\fGDT;w}%%c 11.47 X 90°\P', 6, 0),        # 25CCPA0998-00
        (r'<> PASSANTE\P{\fGDT;w}%%c 18.25 X 90°\P', 1, 0),
        (r'4 x <> PASSANTE\P{\fGDT;w}%%c 18.25 X 90°\Plato opposto', 4, 0),
        (r'3 x <> PASSANTE\P{\fGDT;v}%%c 11 {\fGDT;x} 6\P', 3, 0),        # lamatura
        (r'4 x <> PASSANTE\PM10 - 6H PASSANTE\P', 0, 4),
        (r'4 x <> {\fGDT;x} 22.25\PM8 - 6H {\fGDT;x} 16\P', 0, 4),
        (r'<> {\fGDT;x} 17\PM6 - 6H {\fGDT;x} 12\P', 0, 1),
        ('M6 - 6H PASSANTE', 0, 1),
        ('M6', 0, 1),
        ('M6 x 1', 0, 1),
        ('M8x1.25', 0, 1),
        ('6 fori M5', 0, 6),
        ('6 x M5', 0, 6),
        (r'\A1;M6 {} n° 4 equidistanti', 0, 4),
        (r'M8 (x4)\PTHROUGH HOLE', 0, 4),
        (r'N\U+00B04 fori M6x1\fAIGDT|b0|i0;{\H1;x}\fArial|b0|i0;{\H1;10}', 0, 4),
        ('4xM8 int.125x153 - 4xM10 int.100x210 - Z', 0, 8),
        ('Flangia 200x200 sp.10 - 4 x M8', 0, 4),
        ('2 svasature M6 int.45 - AISI304', 2, 0),                      # M6 = vite della svasatura
        ('3 x M6 svasate int.40', 3, 0),
        ('maschiare 2 fori M6', 0, 2),
        # niente lavorazione
        ('Aggiunto 4 svasature', 0, 0),                                  # nota di revisione
        ('Modificate dimensioni, aggiunto foro svasato x M8', 0, 0),
        ('Dado esagonale M Iso 4032 CL 8 GR  M 6', 0, 0),
        ('4 x M6 - a saldare', 0, 0),
        ('N° 2 INSERTI FILETTATI M6', 0, 0),
        ('Per vite TS M6 H8', 0, 0),
        ('foro per M8', 0, 0),
        ('Piano scorrimento lineare M3', 0, 0),                          # nome del pezzo
        ('M45', 0, 0),
        ('SCALA 1:4', 0, 0),
        ('0.5mm x45°', 0, 0),                                            # smusso
        ("GIU'  5°  R 2", 0, 0),
        ('tutte le pieghe sono a 90°', 0, 0),
        ('2 X 90°', 0, 0),
    ]
    for testo, sv, fi in casi:
        got = sv_fil(testo)
        check(f'{testo[:50]!r} -> svas {sv}, fil {fi}', got == (sv, fi), got)

    r = LT.analizza_callout('attenzione alle svasature')
    check('nota "attenzione alle svasature": c\'e\' ma senza numero',
          r['svas_nota'] and r['svasature'] == 0, r)
    r = LT.analizza_callout('fori filettati')
    check('nota "fori filettati": c\'e\' ma senza numero', r['fil_nota'] and r['filettature'] == 0, r)
    check('normalizza: GDT w = svasatura, %%c = Ø, \\P = a capo',
          LT.normalizza(r'6 x <> PASSANTE\P{\fGDT;w}%%c 11.47 X 90°') == '6 x <> PASSANTE\n⌵ Ø 11.47 X 90°',
          repr(LT.normalizza(r'6 x <> PASSANTE\P{\fGDT;w}%%c 11.47 X 90°')))

    print('\n2) Descrizione e viste non si sommano; righe del PDF')
    t = LT.somma_callout([r'4 x <> PASSANTE\PM10 - 6H PASSANTE', r'4 x <> {\fGDT;x} 22\PM8 - 6H',
                          '4xM8 int.125x153 - 4xM10 int.100x210 - Z'])
    check('callout 4+4 e descrizione 4+4: 8 filettature, non 16', t['filettature'] == 8, t)
    righe = ['PARTICOLARE PRIVO DI BAVE E DIFETTI SUPERFICIALI', '1117,16', '6 x 5,50 PASSANTE',
             '11,47 X 90°', '202', "GIU' 5° R 2", 'attenzione alle svasature']
    t = LT.somma_callout(LT._riunisci_righe(righe))
    check('PDF: "6 x 5,50 PASSANTE" + "11,47 X 90°" = 6 svasature', t['svasature'] == 6 and t['filettature'] == 0, t)
    t = LT.somma_callout(LT._riunisci_righe(['4 x 8,50 PASSANTE', 'M10 - 6H PASSANTE', '6 x 6,60 PASSANTE']))
    check('PDF: "4 x 8,50 PASSANTE" + "M10 - 6H" = 4 filettature (i fori lisci no)', t['filettature'] == 4 and t['svasature'] == 0, t)

    print('\n3) DXF: quota col callout, callout esploso, PDF gemello')

    def _quota(doc, m):
        m.add_lwpolyline([(0, 0), (200, 0), (200, 100), (0, 100)], close=True)
        for x in (50, 150):
            m.add_circle((x, 50), 3.0)
        d = m.add_linear_dim(base=(0, 120), p1=(0, 100), p2=(200, 100),
                             text=r'2 x <> {\fGDT;x} 12\PM8 - 6H {\fGDT;x} 10\P')
        d.render()
    p = dxf('quota_m8.dxf', _quota)
    check('quota "2 x <> … M8 - 6H": 2 filettature', S.scansiona_dxf_dettagli(p, CFG)[2:] == (2, 0),
          S.scansiona_dxf_dettagli(p, CFG))

    def _esploso(doc, m):
        m.add_lwpolyline([(0, 0), (200, 0), (200, 100), (0, 100)], close=True)
        for x in (40, 80, 120, 160):
            m.add_circle((x, 50), 4.25)
        m.add_text('4 x', dxfattribs={'height': 3.5, 'insert': (40, 60)})
        m.add_text('8,50 PASSANTE', dxfattribs={'height': 3.5, 'insert': (52, 60)})
        m.add_text('M10 - 6H PASSANTE', dxfattribs={'height': 3.5, 'insert': (42, 55)})
    p = dxf('esploso.dxf', _esploso)
    check('callout esploso in 3 testi: 4 filettature', S.scansiona_dxf_dettagli(p, CFG)[2:] == (4, 0),
          S.scansiona_dxf_dettagli(p, CFG))

    def _tabella(doc, m):
        # tabella fori di SolidWorks: sigle B1…B4, misura scritta una volta
        # sola nella cella unita ("Ø5 PASSANTE / M6 - 6H PASSANTE")
        m.add_lwpolyline([(0, 0), (300, 0), (300, 100), (0, 100)], close=True)
        b = doc.blocks.new('SW_TABLEANNOTATION_0')
        b.add_mtext('Scheda', dxfattribs={'insert': (5, -5), 'char_height': 10})
        b.add_mtext('Dimensione', dxfattribs={'insert': (190, -5), 'char_height': 10})
        for i in range(4):
            b.add_mtext(f'B{i + 1}', dxfattribs={'insert': (60, -40 - 20 * i), 'char_height': 10})
        b.add_mtext('%%c', dxfattribs={'insert': (220, -60), 'char_height': 10})
        b.add_mtext('5', dxfattribs={'insert': (245, -60), 'char_height': 10})
        b.add_mtext('PASSANTE', dxfattribs={'insert': (262, -60), 'char_height': 10})
        b.add_mtext('M6', dxfattribs={'insert': (202, -75), 'char_height': 10})
        b.add_mtext('- 6H PASSANTE', dxfattribs={'insert': (236, -75), 'char_height': 10})
        m.add_blockref('SW_TABLEANNOTATION_0', (400, 300))
    p = dxf('tabella.dxf', _tabella)
    check('tabella fori B1…B4 con M6: 4 filettature (non 1)', S.scansiona_dxf_dettagli(p, CFG)[2:] == (4, 0),
          S.scansiona_dxf_dettagli(p, CFG))

    def _nudo(doc, m):
        m.add_lwpolyline([(0, 0), (200, 0), (200, 100), (0, 100)], close=True)
        m.add_circle((50, 50), 3.0)
    p = dxf('nudo.dxf', _nudo)
    check('DXF senza testi e senza PDF: niente', S.scansiona_dxf_dettagli(p, CFG)[2:] == (0, 0))
    open(os.path.join(TMP, 'nudo.PDF'), 'wb').write(b'%PDF-1.4\n')
    check('PDF gemello trovato (stesso nome, maiuscole diverse)',
          LT.pdf_gemello(p) == os.path.join(TMP, 'nudo.PDF'), LT.pdf_gemello(p))
    check('PDF illeggibile: nessun errore, niente lavorazioni', S.scansiona_dxf_dettagli(p, CFG)[2:] == (0, 0))

    print('\n4) Geometria: stesso pezzo in due viste, svasature strette')

    def _due_viste(doc, m):
        # sviluppo 280x50 e vista piegata 270x50: pezzi confrontabili, scelta
        # incerta; 3 svasature Ø7,47/Ø11,47 (rapporto 1,54) in ciascuna vista
        m.add_lwpolyline([(0, 0), (280, 0), (280, 50), (0, 50)], close=True)
        m.add_lwpolyline([(0, 100), (270, 100), (270, 150), (0, 150)], close=True)
        for y in (25, 125):
            for x in (40, 135, 230):
                m.add_circle((x, y), 3.735)
                m.add_circle((x, y), 5.735)
    p = dxf('due_viste.dxf', _due_viste)
    r = S.scansiona_dxf_dettagli(p, CFG)
    check('due viste con 3 svasature Ø7,47/Ø11,47: 3, non 6 e non 0', r[3] == 3, r)

    def _filetto_cerchi(doc, m):
        # filetto disegnato con due cerchi interi (M6: Ø5/Ø6, rapporto 1,2): non e' svasatura
        m.add_lwpolyline([(0, 0), (200, 0), (200, 100), (0, 100)], close=True)
        m.add_circle((50, 50), 2.5)
        m.add_circle((50, 50), 3.0)
    p = dxf('filetto_cerchi.dxf', _filetto_cerchi)
    check('due cerchi con rapporto 1,2 (simbolo filetto): nessuna svasatura',
          S.scansiona_dxf_dettagli(p, CFG)[3] == 0, S.scansiona_dxf_dettagli(p, CFG))

    def _callout_parziale(doc, m):
        # il callout quota un gruppo (2) ma nel pezzo ci sono 5 svasature uguali
        m.add_lwpolyline([(0, 0), (300, 0), (300, 100), (0, 100)], close=True)
        for x in (40, 90, 140, 190, 240):
            m.add_circle((x, 50), 2.25)
            m.add_circle((x, 50), 4.59)
        m.add_mtext(r'2 x <> PASSANTE\P{\fGDT;w}%%c 9.18 X 90°', dxfattribs={'insert': (40, 80), 'char_height': 3})
    p = dxf('callout_parziale.dxf', _callout_parziale)
    r = S.scansiona_dxf_dettagli(p, CFG)
    check('callout "2 x" ma 5 svasature uguali nel pezzo: 5', r[3] == 5, r)

    print('\n5) Caso reale 25CCPA0998-00')
    if os.path.exists(CASO_REALE):
        copia = os.path.join(TMP, 'reale')
        os.makedirs(copia)
        shutil.copy2(CASO_REALE, copia)
        p = os.path.join(copia, os.path.basename(CASO_REALE))
        r = S.scansiona_dxf_dettagli(p, CFG)
        check('25CCPA0998-00: 6 svasature, 0 filettature', r[2:] == (0, 6), r)
        r = S.scansiona_dxf_dettagli(p, {**CFG, 'dxf_lavorazioni_da_testi': False})
        check('25CCPA0998-00 dalla sola geometria: 6 svasature (una vista)', r[2:] == (0, 6), r)
        pdf = os.path.splitext(CASO_REALE)[0] + '.PDF'
        if os.path.exists(pdf):
            t = LT.somma_callout(LT.callout_pdf(pdf))
            check('25CCPA0998-00 dal PDF: 6 svasature', t['svasature'] == 6 and t['filettature'] == 0, t)
    else:
        print('  (disegno non presente su questo PC: saltato)')

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
