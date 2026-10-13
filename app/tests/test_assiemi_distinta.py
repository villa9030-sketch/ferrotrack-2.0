"""Pezzi per assieme letti nella DISTINTA del disegno d'insieme (13/10/2026).

Il pacchetto DECA A 001221 BIS: la riga 26T30SA0233-00 "Struttura x MTR"
x2 + x1, e il disegno d'insieme 26T30SA0233-00 (PDF e DXF) con la tabella
"Num. art. | Codice | REV | Descrizione | Descrizione Aggiuntiva | Materiale
| Trattamento | Qta": codici SENZA revisione (191700045 = 191700045-00.dxf),
REV in una colonna sua. Nel DXF la tabella e' un blocco di testi
(SolidWorks, SW_TABLEANNOTATION).

Copre:
 A. distinte_pdf: la tabella dal PDF (REV a parte, materiale e descrizione
    aggiuntiva al loro posto) e dal DXF (testi dentro un blocco)
 B. pacchetto coi componenti nella cartella dell'assieme, PDF + DXF
    d'insieme: assieme x3, pezzi per assieme 1,2,1,1,1,2,1,2 -> 3,6,3,3,3,
    6,3,6, nessuno "da confermare", il disegno d'insieme non e' un pezzo
 C. pacchetto come quello vero: componenti FUORI dalla cartella
    dell'assieme, solo il DXF d'insieme: stesso risultato
 D. "Da procurare": tasto N sul tubo -> 1 per assieme x 3, lunghezza dalla
    riga della distinta ("Lg 581") e non dalle misure del disegno

Database e cartelle temporanei, Lantek simulato. python app/tests/test_assiemi_distinta.py
"""
import io
import json
import os
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
os.environ['FERROTRACK_ESEMPI_DIR'] = tempfile.mkdtemp(prefix='ft_dist_esempi_')

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_distinta_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend import lantek as L  # noqa: E402
from backend import api_laser as AL  # noqa: E402
from backend.database import PreventivoManager  # noqa: E402
from backend.preventivi import dxf_cache, distinte_pdf as DP  # noqa: E402
from backend.preventivi import tempi_laser as TL  # noqa: E402

_CARTELLE = tempfile.mkdtemp(prefix='ft_dist_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
A.PDFS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'pdfs')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)
os.makedirs(A.PDFS_FOLDER, exist_ok=True)
dxf_cache._CACHE_DB_PATH = os.path.join(_CARTELLE, 'dxf_cache.db')
_config_vera = A.ConfigManager.load_config
A.ConfigManager.load_config = staticmethod(lambda: {**(_config_vera() or {}),
                                                    'disegni_export_root': os.path.join(_CARTELLE, 'rete')})

LANTEK = {'su': False, 'ordine': {}}
TL._leggi_lantek = lambda codici: None
L.pezzi_in_lantek = lambda codici: ({'disponibile': True, 'errore': None, 'pezzi': {
    c: {'esiste': True, 'codice_lantek': c, 'materiale': 'INOX', 'spessore': 5.0, 'revisioni': []} for c in codici}}
    if LANTEK['su'] else {'disponibile': False, 'errore': 'simulato', 'pezzi': {}})
L.clienti_lantek = lambda: ['DECA']
L.ordini_in_lantek = lambda commessa: dict(LANTEK['ordine']) if LANTEK['su'] else None
L.ordini_fatti_in_lantek = lambda commessa: {}
L.pezzi_dell_ordine = lambda commessa: dict(LANTEK['ordine']) if LANTEK['su'] else None
L.procesos_disponibile = lambda: False
L.materiali_lantek_noti = lambda: {'FERRO', 'INOX'}
L.xmlimporter_disponibile = lambda: True
L.importa_pezzi_dxf = lambda *a, **k: {'eseguito': False, 'errore': 'bloccato nel test'}
L.importa_xml = lambda *a, **k: {'eseguito': False, 'errore': 'bloccato nel test'}

OK = 0
KO = []


def check(nome, cond, extra=''):
    global OK
    if cond:
        OK += 1
        print(f'  [OK] {nome}')
    else:
        KO.append(nome)
        print(f'  [KO] {nome}  {extra}')


ASS = '26T30SA0233-00'
CONSEGNA = (datetime.now() + timedelta(days=30)).date()
# la distinta del disegno d'insieme, come quella vera (codici senza revisione)
DISTINTA = [
    ('1', '26CCSA0038', '00', 'Sostegno per traversa alta telaio', 'inferiore MTR - Lg 581 - AISI304', 'Aisi304', '1'),
    ('2', '26CCSA0039', '00', 'Longherone telaio inferiore MTR Lg 895', 'AISI304', 'Aisi304', '2'),
    ('3', '26CCSA0040', '00', 'Traversa attacco alto telaio inferiore', 'MTR - Lg 595 - AISI304', 'Aisi304', '1'),
    ('4', '26CCSA0041', '00', 'Traversa alta telaio inferiore MTR', 'Lg 304 - AISI304', 'Aisi304', '1'),
    ('5', '26CCSA0042', '00', 'Traversa attacco basso telaio inferiore', 'MTR - Lg 595 - AISI304', 'Aisi304', '1'),
    ('6', '191700045', '00', 'Chiusura x tubolare 50x50 (50x43,6 sp.5)', '1 x M8 - AISI304', 'AISI 304', '2'),
    ('7', '191700112', '00', 'Rinforzo gamba x telaio MTR 80x81,5 sp.5', 'AISI304', 'AISI 304', '1'),
    ('8', '191700114', '00', 'Gambe basse telaio inf. MTR 88x50 sp.10', '1 x Ø9 - AISI304', 'AISI 304', '2'),
]
ATTESI = {'26CCSA0038-00': (1, 3), '26CCSA0039-00': (2, 6), '26CCSA0040-00': (1, 3), '26CCSA0041-00': (1, 3),
          '26CCSA0042-00': (1, 3), '191700045-00': (2, 6), '191700112-00': (1, 3), '191700114-00': (2, 6)}
# misure dei componenti (il tubo 26CCSA0038 disegnato un po' piu' lungo: la
# lunghezza giusta e' quella scritta nella distinta, Lg 581)
MISURE = {'26CCSA0038-00': (600, 50), '26CCSA0039-00': (895, 50), '26CCSA0040-00': (595, 150),
          '26CCSA0041-00': (304, 50), '26CCSA0042-00': (595, 150), '191700045-00': (50, 43.6),
          '191700112-00': (80, 81.5), '191700114-00': (88, 50)}


def pdf_ordine():
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    cons = CONSEGNA.strftime('%d/%m/%y')
    righe = [f'11PCOPA0001-01 C26-232 Lamiera fondo sp.2 AISI 304 2,00Nr {cons}',
             f'{ASS} C26-232 Struttura x MTR - La.300 - H700 Aisi304 Saldata 2,00Nr {cons}',
             f'11PCOPA0001-01 C26-233 Lamiera fondo sp.2 AISI 304 1,00Nr {cons}',
             f'{ASS} C26-233 Struttura x MTR - La.300 - H700 Aisi304 Saldata 1,00Nr {cons}']
    testo = ['DECA S.r.l.', 'Via Roma 1 - 20100 Milano', 'Ordine Fornitore A 001221 BIS 19/09/2026',
             'Codice Commessa Descrizione Q.ta U.M. Prezzo Consegna'] + righe + ['Note', 'fine']
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for r in testo:
        c.drawString(40, y, r)
        y -= 18
    c.showPage()
    c.save()
    return buf.getvalue()


def pdf_insieme():
    """Il PDF del disegno d'insieme con la tabella dei componenti (griglia)."""
    from reportlab.lib.pagesizes import A3, landscape
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib import colors
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A3))
    st = getSampleStyleSheet()
    righe = [['Num. art.', 'Codice', 'REV', 'Descrizione', 'Descrizione Aggiuntiva', 'Materiale', 'Trattamento', 'Qtà']]
    for r in DISTINTA:
        # "Descrizione Aggiuntiva" su due righe nella cella, come nel disegno
        agg = r[4].replace(' - Lg', '\n- Lg') if len(r[4]) > 20 else r[4]
        righe.append([r[0], r[1], r[2], r[3], agg, r[5], '', r[6]])
    t = Table(righe)
    t.setStyle(TableStyle([('GRID', (0, 0), (-1, -1), 0.5, colors.black), ('FONTSIZE', (0, 0), (-1, -1), 8)]))
    doc.build([Paragraph('Struttura x MTR - La.300 - H700 Saldata - AISI304', st['Normal']), Spacer(1, 12), t])
    return buf.getvalue()


def dxf_insieme():
    """Il DXF del disegno d'insieme: la tabella e' un blocco di testi."""
    import ezdxf
    doc = ezdxf.new()
    blk = doc.blocks.new('SW_TABLEANNOTATION_0')
    xs = [82, 181, 338, 400, 860, 1195, 1330, 1472]
    testa = ['Num. art.', 'Codice', 'REV', 'Descrizione', 'Descrizione Aggiuntiva', 'Materiale', 'Trattamento', 'Qtà']
    y = 323.3
    for x, t in zip(xs, testa):
        blk.add_mtext(t, dxfattribs={'insert': (x, y), 'char_height': 16.9})
    for r in DISTINTA:
        y -= 27.4
        for x, t in zip(xs, [r[0], r[1], r[2], r[3], r[4], r[5], '', r[6]]):
            if t:
                blk.add_mtext(t, dxfattribs={'insert': (x, y), 'char_height': 16.9})
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (2000, 0), (2000, 1400), (0, 1400)], close=True)
    msp.add_blockref('SW_TABLEANNOTATION_0', (2500, 0))
    s = io.StringIO()
    doc.write(s)
    return s.getvalue().encode('utf-8')


def dxf_rett(w, h):
    import ezdxf
    doc = ezdxf.new()
    doc.modelspace().add_lwpolyline([(0, 0), (w, 0), (w, h), (0, h)], close=True)
    s = io.StringIO()
    doc.write(s)
    return s.getvalue().encode('utf-8')


def pacchetto(componenti_fuori=False, con_pdf=True):
    dentro = '' if componenti_fuori else f'{ASS}/'
    f = {'Ordine/OAFA202601221.pdf': pdf_ordine(),
         'Ordine/disegni/11PCOPA0001-01.dxf': dxf_rett(450, 470),
         f'Ordine/disegni/{ASS}/{ASS}.dxf': dxf_insieme()}
    if con_pdf:
        f[f'Ordine/disegni/{ASS}/{ASS}.pdf'] = pdf_insieme()
    for c, (w, h) in MISURE.items():
        f[f'Ordine/disegni/{dentro}{c}.dxf'] = dxf_rett(w, h)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for n, b in f.items():
            z.writestr(n, b)
    return buf.getvalue()


def per_codice(xs, k='codice'):
    return {x[k]: x for x in xs}


def carica(uff, zip_bytes, numero):
    r = uff.post('/api/orders/da-pacchetto/analizza', data={'zip': (io.BytesIO(zip_bytes), 'p.zip')},
                 content_type='multipart/form-data')
    d = r.get_json() or {}
    corpo = {'numero_ordine': numero, 'cliente': 'DECA S.r.l.', 'data_consegna': CONSEGNA.isoformat(), 'forza': True,
             'righe': [{'id': x['id'], 'quantita': x['quantita'], 'materiale': x.get('materiale') or '',
                        'spessore_mm': x.get('spessore_mm')} for x in d.get('righe') or [] if not x['fuori_ordine']]}
    rc = uff.post(f"/api/orders/da-pacchetto/{d.get('pacchetto_id')}/conferma", json=corpo)
    return d, (rc.get_json() or {}).get('order_id')


def controlla(nome, laser, d, oid):
    righe = per_codice(d.get('righe') or [])
    a0 = (d.get('assiemi') or [{}])[0]
    check(f'{nome}: analisi, assieme x3', a0.get('codice') == ASS and a0.get('quantita') == 3, d.get('assiemi'))
    check(f"{nome}: il disegno d'insieme non e' un pezzo ne' un disegno fuori ordine",
          ASS not in righe and f'{ASS}.dxf' not in (d.get('dxf_senza_riga') or []), (list(righe), d.get('dxf_senza_riga')))
    q = {c: (righe.get(c) or {}).get('quantita') for c in ATTESI}
    check(f'{nome}: analisi, pezzi per assieme dalla distinta 1,2,1,1,1,2,1,2',
          q == {c: v[0] for c, v in ATTESI.items()} and all(righe[c]['assieme'] == ASS and righe[c]['quantita_fonte'] == 'pdf'
                                                         for c in ATTESI), q)
    A.OrderManager.smista(oid, True, user_id='laser')
    AL.invalida(oid)
    j = laser.get(f'/api/orders/{oid}/laser').get_json() or {}
    pz = per_codice(j.get('pezzi') or [])
    tot = {c: ((pz.get(c) or {}).get('qta_per'), (pz.get(c) or {}).get('quantita')) for c in ATTESI}
    check(f'{nome}: ordine, totali 3,6,3,3,3,6,3,6', tot == ATTESI, tot)
    check(f'{nome}: nessuno "da confermare", niente bloccato per le quantita\'',
          not any((pz.get(c) or {}).get('qta_da_confermare') for c in ATTESI) and not j.get('n_qta_da_confermare'),
          j.get('bloccati'))
    return j, pz


def main():
    global DISTINTA
    from tests.accesso_aiuto import postazioni, entra, modalita, persona, PIN_UFFICIO
    s = models.SessionLocal()
    s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
    s.commit(); s.close()
    modalita('protetto')
    postazioni()
    persona('elena', 'Elena Ufficio', pin=PIN_UFFICIO)
    laser = entra(A.app, 'laser')
    uff = entra(A.app, 'ufficio', pin=PIN_UFFICIO)

    print('A) La tabella dei componenti dal PDF e dal DXF del disegno d\'insieme')
    d = os.path.join(_CARTELLE, 'tab')
    os.makedirs(d)
    with open(os.path.join(d, ASS + '.pdf'), 'wb') as f:
        f.write(pdf_insieme())
    for nome, dist in (('PDF', DP.leggi_distinta(os.path.join(d, ASS + '.pdf'))), ('DXF', DP.leggi_distinta_dxf(dxf_insieme()))):
        rr = [(r['codice'], r['rev'], r['qta']) for r in (dist or {}).get('righe') or []]
        check(f'{nome}: 8 righe, codici senza revisione, REV a parte, Qta', rr == [(r[1], r[2], int(r[6])) for r in DISTINTA], rr)
        r1 = ((dist or {}).get('righe') or [{}])[0]
        check(f'{nome}: materiale e descrizione aggiuntiva al loro posto', r1.get('materiale') == 'Aisi304'
              and 'Lg 581' in (r1.get('descrizione_agg') or ''), r1)

    # PDF e DXF che non concordano su un codice: quel codice e' "dubbio"
    from types import SimpleNamespace
    from backend import assiemi_ordine as AO
    vera = DISTINTA
    DISTINTA = [r if r[1] != '191700045' else r[:6] + ('3',) for r in vera]
    altro = dxf_insieme()
    DISTINTA = vera
    dd = AO.distinta_assieme(d, SimpleNamespace(dxf_map={ASS + '.dxf': altro}, master_dxf={ASS + '.dxf': ASS}), ASS)
    check('PDF dice 2, DXF dice 3: 191700045 dubbio (lo conferma il laser), gli altri no',
          dd['191700045']['dubbio'] and not dd['191700114']['dubbio'] and dd['26CCSA0039']['qta'] == 2
          and 'Lg 581' in dd['26CCSA0038']['testo'], dd.get('191700045'))

    print('\nB) Componenti nella cartella dell\'assieme, PDF + DXF d\'insieme')
    an, oid = carica(uff, pacchetto(), 'A 001221 BIS')
    check('ordine creato', bool(oid), an.get('error'))
    jb, pzb = controlla('B', laser, an, oid)

    print('\nC) Come il pacchetto vero: componenti fuori dalla cartella, solo il DXF d\'insieme')
    an2, oid2 = carica(uff, pacchetto(componenti_fuori=True, con_pdf=False), 'A 001221 TER')
    check('ordine creato', bool(oid2), an2.get('error'))
    controlla('C', laser, an2, oid2)

    print('\nD) Da procurare: il tubo coi pezzi per assieme e la lunghezza della distinta')
    aid = pzb['26CCSA0038-00']['articolo_id']
    pr = laser.get(f'/api/orders/{oid}/pezzi/{aid}/procura-proposta').get_json() or {}
    p = pr.get('proposta') or {}
    check('proposta: lunghezza 581 dalla riga della distinta ("Lg 581"), non 600 del disegno',
          p.get('lunghezza_mm') == 581 and p.get('tipo') == 'tubo', p)
    check('proposta: 1 per assieme x 3, non da confermare', pr.get('per_assieme') == 1 and pr.get('qta_assieme') == 3
          and not pr.get('qta_da_confermare'), pr)
    r = laser.post(f'/api/orders/{oid}/pezzi/{aid}/decisione', json={'scelta': 'non_laser', 'procura': {
        'tipo': 'tubo', 'profilo': 'Tubo quadro 50x50 sp.3mm', 'lunghezza_mm': p.get('lunghezza_mm')}})
    v = (r.get_json() or {}).get('procura') or {}
    check('voce Da procurare: 3 pezzi da 581 mm', r.status_code == 200 and v.get('quantita') == 3
          and v.get('lunghezza_mm') == 581, r.get_json())

    print('\n' + '=' * 60)
    print(f'{OK} OK, {len(KO)} KO')
    for k in KO:
        print('  KO:', k)
    return 1 if KO else 0


if __name__ == '__main__':
    try:
        code = main()
    finally:
        try:
            _ENG.dispose()
            os.unlink(_TMP)
        except OSError:
            pass
    sys.exit(code)
