"""Test dell'ORDINE DAL PACCHETTO DEL CLIENTE (PDF d'ordine + disegni).

L'ufficio caricava l'ordine col solo PDF: niente distinta, niente disegni,
quindi niente banco lamiere per il laser. Ora trascina la cartella che manda
il cliente: il pacchetto si legge con la pipeline del preventivatore, i pezzi
finiscono su un PREVENTIVO TECNICO nascosto e l'ordine ci si aggancia come
un ordine nato da un preventivo accettato.

Copre:
 1. import del preventivatore (import-rfq-package) ancora uguale dopo il
    riordino in una funzione condivisa
 2. /api/orders/da-pacchetto/analizza: forma e valori della risposta
    (testata dal PDF, righe con quantita' e fonte, materiale/spessore,
    ingombro, disegni fuori ordine, righe senza disegno, anteprima SVG)
 3. il preventivo tecnico non compare MAI nel preventivatore (elenco,
    doppioni, storico prezzi) e non si apre/modifica/invia/accetta
 4. /conferma: errori di validazione chiari, correzioni dell'ufficio
    (quantita', rimozione con 0, materiale/spessore, disegno fuori ordine
    aggiunto), ordine PACCHETTO con disegni e PDF, distinta corretta,
    file temporanei puliti, seconda conferma = 409
 5. banco lamiere del laser con i pezzi del nuovo ordine
 6. ordine gia' caricato: avviso all'analisi, 409 alla conferma, forza
 7. DELETE scarta l'analisi; analisi abbandonate pulite dopo 24 ore
 8. pacchetto col solo PDF; PDF non riconosciuto (quantita' da scrivere)
 9. migrazione della colonna solo_tecnico: aggiunta e idempotente

Gira su DATABASE TEMPORANEO e cartelle temporanee. Esecuzione:
    python app/tests/test_ordine_pacchetto.py
"""
import io
import os
import shutil
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

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine, inspect, text  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Order, OrderFile, Notification, Preventivo  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_pacchetto_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')  # il MODULO, non l'oggetto Flask
from backend.database import PreventivoManager  # noqa: E402
from backend.migrations_ore import _migra_colonne_sottosistema  # noqa: E402
from backend.preventivi import dxf_cache  # noqa: E402

# Cartelle dei file e cache dei disegni: temporanee, mai quelle vere.
_CARTELLE = tempfile.mkdtemp(prefix='ft_pacchetto_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
A.PDFS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'pdfs')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)
os.makedirs(A.PDFS_FOLDER, exist_ok=True)
dxf_cache._CACHE_DB_PATH = os.path.join(_CARTELLE, 'dxf_cache.db')
_RETE = os.path.join(_CARTELLE, 'rete')
_config_vera = A.ConfigManager.load_config


def _config_prova():
    cfg = dict(_config_vera() or {})
    cfg['disegni_export_root'] = _RETE
    return cfg


A.ConfigManager.load_config = staticmethod(_config_prova)

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


# ─── Pacchetto finto ma vero: PDF d'ordine (reportlab) + DXF (ezdxf) ──────

CONSEGNA = (datetime.now() + timedelta(days=60)).date()
CONSEGNA_ISO = CONSEGNA.isoformat()


def pdf_ordine(righe=None, numero='4521', cliente='Officine Rossi S.r.l.'):
    """"Ordine Fornitore" nel formato letto da ordine_testo (DECA)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    cons = CONSEGNA.strftime('%d/%m/%y')
    if righe is None:
        righe = [f'PZ-A100-00 C26-101 Piastra base sp.3 S235JR 4,00Nr {cons}',
                 f'PZ-B200-00 C26-101 Staffa sp.2 AISI 304 6,00Nr {cons}',
                 f'PZ-C300-00 C26-101 Rondella piana 10,00Nr {cons}']
    testo = [cliente, 'Via Roma 1 - 20100 Milano',
             f'Ordine Fornitore {numero} 01/09/2026',
             'Codice Commessa Descrizione Q.ta U.M. Prezzo Consegna'] + righe + ['Note', 'fine']
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for riga in testo:
        c.drawString(40, y, riga)
        y -= 18
    c.showPage()
    c.save()
    return buf.getvalue()


def pdf_libero():
    """Un PDF che non e' un ordine riconoscibile e non cita i codici."""
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(40, 800, 'Buongiorno, vi mandiamo i disegni allegati. Saluti.')
    c.showPage()
    c.save()
    return buf.getvalue()


def dxf_rettangolo(w, h):
    import ezdxf
    doc = ezdxf.new()
    doc.modelspace().add_lwpolyline([(0, 0), (w, 0), (w, h), (0, h)], close=True)
    s = io.StringIO()
    doc.write(s)
    return s.getvalue().encode('utf-8')


DXF = {'PZ-A100-00.dxf': dxf_rettangolo(200, 100),
       'PZ-B200-00.dxf': dxf_rettangolo(150, 80),
       'EXTRA-900.dxf': dxf_rettangolo(60, 60)}


def cartella(pdf=None, dxf=None, nome_pdf='ORDINE 4521.pdf'):
    """File sciolti come da una cartella trascinata: (files, paths)."""
    pdf = pdf_ordine() if pdf is None else pdf
    dxf = DXF if dxf is None else dxf
    files = [(io.BytesIO(pdf), nome_pdf)] + [(io.BytesIO(b), n) for n, b in dxf.items()]
    paths = ['Pacchetto/' + nome_pdf] + ['Pacchetto/disegni/' + n for n in dxf]
    return files, paths


def zip_di(contenuti):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for n, b in contenuti.items():
            zf.writestr(n, b)
    return buf.getvalue()


# Chi agisce lo dice il dispositivo (+ PIN negli uffici): un client per
# ognuno. Si riempie in main().
CL = {}


def analizza(c, user='ufficio', files=None, paths=None, zip_bytes=None):
    c = CL.get(user, c)
    data = {}
    if zip_bytes is not None:
        data['zip'] = (io.BytesIO(zip_bytes), 'pacchetto.zip')
    else:
        if files is None:
            files, paths = cartella()
        data['files'] = files
        data['paths'] = paths
    return c.post('/api/orders/da-pacchetto/analizza', data=data,
                  content_type='multipart/form-data')


def conferma(c, pid, **corpo):
    c = CL.get(corpo.pop('user_id', None), c)
    base = {'numero_ordine': '4521', 'cliente': 'Officine Rossi S.r.l.',
            'data_consegna': CONSEGNA_ISO}
    base.update(corpo)
    return c.post(f'/api/orders/da-pacchetto/{pid}/conferma', json=base)


def per_codice(righe):
    return {r['codice']: r for r in righe}


def main():
    s = models.SessionLocal()
    try:
        s.add(User(id='ufficio', name='Amministrazione', role='Amministrazione', is_active=True))
        s.add(User(id='comm', name='Commerciale', role='Commerciale', is_active=True))
        s.add(User(id='capo', name='Capo', role='Capo Officina', is_active=True, is_capo=True))
        s.add(User(id='operaio', name='Operaio', role='Operaio', is_active=True))
        s.commit()
    finally:
        s.close()
    from tests.accesso_aiuto import postazioni, persona, entra, modalita
    modalita('protetto')
    postazioni()
    persona('ufficio', 'Amministrazione', 'Amministrazione', pin='2580')
    persona('comm', 'Commerciale', 'Commerciale', pin='3691')
    CL.update({'ufficio': entra(A.app, 'ufficio', pin='2580'),
               'comm': entra(A.app, 'commerciale', pin='3691'),
               'operaio': entra(A.app, 'reparto')})
    c = CL['ufficio']

    # =====================================================================
    print('\n1) Import del preventivatore dopo il riordino')
    files, paths = cartella(nome_pdf='RICHIESTA.pdf')
    r = CL['comm'].post('/api/preventivi/import-rfq-package',
               data={'files': files, 'paths': paths},
               content_type='multipart/form-data')
    d = r.get_json() or {}
    check('import-rfq-package risponde come prima', r.status_code == 200 and d.get('success')
          and d.get('n_articoli') == 3 and d.get('n_dxf_matched') == 2
          and d.get('dxf_no_match') == ['EXTRA-900.dxf'], d)
    prev_vero = d.get('preventivo_id')
    arts = (PreventivoManager.get(prev_vero) or {}).get('articoli') or []
    check('i pezzi del preventivo vero non hanno la riga-ordine del pacchetto',
          arts and not any(a.get('ordine') for a in arts), arts[:1])
    PreventivoManager.soft_delete(prev_vero)   # fuori dai piedi per i doppioni

    # =====================================================================
    print('\n2) Analisi del pacchetto')
    r = analizza(c, user='operaio')
    check('un operaio non puo\'', r.status_code == 403
          and r.get_json().get('codice') == 'stazione_non_ammessa', r.get_json())
    r = c.post('/api/orders/da-pacchetto/analizza', data={},
               content_type='multipart/form-data')
    check('senza file: 400 nessun_file', r.status_code == 400
          and r.get_json().get('codice') == 'nessun_file', r.get_json())

    r = analizza(c)
    d = r.get_json() or {}
    check('risposta 200', r.status_code == 200 and d.get('success'), d)
    pid = d.get('pacchetto_id')
    chiavi = {'pacchetto_id', 'cliente', 'numero_ordine', 'data_consegna', 'note', 'pdf',
              'righe', 'assiemi', 'tubolari', 'dxf_senza_riga', 'righe_senza_disegno',
              'avvisi', 'ordine_esistente'}
    check('forma della risposta', chiavi <= set(d), chiavi - set(d))
    check('testata dal PDF', d.get('cliente') == 'Officine Rossi S.r.l.'
          and d.get('numero_ordine') == '4521' and d.get('data_consegna') == CONSEGNA_ISO,
          (d.get('cliente'), d.get('numero_ordine'), d.get('data_consegna')))
    check('PDF riconosciuto', (d.get('pdf') or {}).get('nome') == 'ORDINE 4521.pdf', d.get('pdf'))
    rp = c.get((d.get('pdf') or {}).get('url') or '/x')
    check('PDF d\'ordine visibile per la revisione', rp.status_code == 200
          and rp.data[:5] == b'%PDF-', rp.status_code)
    rp.close()
    righe = per_codice(d.get('righe') or [])
    check('4 righe: 3 dell\'ordine + 1 disegno fuori ordine',
          set(righe) == {'PZ-A100-00', 'PZ-B200-00', 'PZ-C300-00', 'EXTRA-900'}, list(righe))
    ra, rb, rc, rx = (righe.get(k, {}) for k in ('PZ-A100-00', 'PZ-B200-00', 'PZ-C300-00', 'EXTRA-900'))
    campi = {'id', 'codice', 'descrizione', 'quantita', 'quantita_fonte', 'materiale', 'spessore_mm',
             'bbox_w_mm', 'bbox_h_mm', 'area_dm2', 'confidenza', 'disegno', 'url_svg', 'avvisi'}
    check('forma della riga', campi <= set(ra), campi - set(ra))
    check('A: quantita\' 4 dal PDF, S235 sp.3', ra.get('quantita') == 4
          and ra.get('quantita_fonte') == 'pdf' and 'S235' in str(ra.get('materiale')).upper()
          and ra.get('spessore_mm') == 3, ra)
    check('A: ingombro e area dal disegno', abs((ra.get('bbox_w_mm') or 0) - 200) < 1
          and abs((ra.get('bbox_h_mm') or 0) - 100) < 1 and abs((ra.get('area_dm2') or 0) - 2.0) < 0.05,
          (ra.get('bbox_w_mm'), ra.get('bbox_h_mm'), ra.get('area_dm2')))
    check('A: disegno e anteprima', ra.get('disegno') == 'PZ-A100-00.dxf'
          and ra.get('url_svg') == f'/api/preventivi/{pid}/dxf/PZ-A100-00.dxf/svg'
          and ra.get('confidenza') is not None, ra)
    check('B: INOX 304 sp.2, 6 pezzi', ra and rb.get('quantita') == 6
          and 'INOX' in str(rb.get('materiale')).upper() and rb.get('spessore_mm') == 2, rb)
    check('C: riga senza disegno', rc.get('disegno') is None and rc.get('url_svg') is None
          and rc.get('quantita') == 10 and any('disegno' in a.lower() for a in rc.get('avvisi') or []), rc)
    check('EXTRA: fuori ordine, quantita\' 0, da decidere', rx.get('quantita') == 0
          and rx.get('fuori_ordine') is True and rx.get('quantita_fonte') == 'mancante'
          and rx.get('disegno') == 'EXTRA-900.dxf', rx)
    check('disegni senza riga / righe senza disegno', d.get('dxf_senza_riga') == ['EXTRA-900.dxf']
          and d.get('righe_senza_disegno') == ['PZ-C300-00'],
          (d.get('dxf_senza_riga'), d.get('righe_senza_disegno')))
    check('nessun ordine gia\' caricato', d.get('ordine_esistente') is None)
    check('assiemi e tubolari: liste', d.get('assiemi') == [] and d.get('tubolari') == [])
    svg = c.get(ra.get('url_svg') or '/x')
    check('anteprima SVG del disegno servita', svg.status_code == 200
          and b'<svg' in svg.data[:500], svg.status_code)
    check('file nella cartella temporanea',
          os.path.isfile(os.path.join(A.UPLOAD_FOLDER, 'preventivi_tmp', pid, 'PZ-A100-00.dxf'))
          and os.path.isfile(os.path.join(A.UPLOAD_FOLDER, 'preventivi_tmp', pid, 'ORDINE 4521.pdf')))

    # =====================================================================
    print('\n3) Il preventivo tecnico non esiste per il preventivatore')
    lista = c.get('/api/preventivi?limit=500').get_json()
    check('fuori dall\'elenco', pid not in {p['id'] for p in lista['preventivi']}
          and lista['count'] == len(lista['preventivi']), lista['count'])
    for st in ('BOZZA', 'ACCETTATO'):
        l2 = c.get(f'/api/preventivi?status={st}').get_json()
        check(f'fuori dall\'elenco {st}', pid not in {p['id'] for p in l2['preventivi']})
    check('fuori dall\'elenco per cliente', pid not in {p['id'] for p in
          c.get('/api/preventivi?cliente=Rossi').get_json()['preventivi']})
    check('non conta come doppione di una richiesta', PreventivoManager.find_by_numero_ordine('4521') is None)
    st = c.get('/api/preventivi/storico-prezzo?codice=PZ-A100-00').get_json() or {}
    check('fuori dallo storico prezzi', not any(o.get('preventivo_id') == pid
          for o in (st.get('occorrenze') or [])), st)
    sb = PreventivoManager.storico_prezzo_batch([{'codice': 'PZ-A100-00'}])
    check('fuori dallo storico prezzi (lista)', sb == [None], sb)
    r = c.get(f'/api/preventivi/{pid}')
    check('non si apre: 404 chiaro', r.status_code == 404
          and r.get_json().get('codice') == 'pacchetto_ordine', r.get_json())
    for metodo, url, corpo in (
            ('put', f'/api/preventivi/{pid}', {'note': 'x', 'user_id': 'comm'}),
            ('put', f'/api/preventivi/{pid}/articoli', {'articoli': [], 'user_id': 'comm'}),
            ('post', f'/api/preventivi/{pid}/invia', {'user_id': 'comm'}),
            ('post', f'/api/preventivi/{pid}/accetta', {'user_id': 'comm'}),
            ('post', f'/api/preventivi/{pid}/duplica', {'user_id': 'comm', 'created_by': 'comm'}),
            ('delete', f'/api/preventivi/{pid}', {'user_id': 'comm'})):
        r = getattr(CL['comm'], metodo)(url, json=corpo)
        check(f'{metodo.upper()} {url.split(pid)[1] or "/"} rifiutato (409)', r.status_code == 409
              and 'pacchetto' in (r.get_json() or {}).get('error', ''), (r.status_code, r.get_json()))
    check('update / transizioni rifiutati anche dal gestore',
          'error' in (PreventivoManager.update(pid, {'note': 'x'}) or {})
          and 'error' in (PreventivoManager.transition_status(pid, 'INVIATO') or {})
          and 'error' in (PreventivoManager.accetta_e_crea_ordine(pid, 'comm') or {}))
    check('duplicato impossibile', PreventivoManager.duplicate(pid, 'X', 'comm') is None)
    check('il pacchetto e\' ancora integro', PreventivoManager.get_tecnico(pid) is not None)

    # =====================================================================
    print('\n4) Conferma: validazione')
    casi = (({'numero_ordine': ''}, 'numero_mancante'),
            ({'cliente': '  '}, 'cliente_mancante'),
            ({'data_consegna': ''}, 'data_mancante'),
            ({'data_consegna': '15/12/2026'}, 'data_non_valida'),
            ({'data_consegna': '2020-01-01'}, 'data_passata'),
            ({'righe': [{'id': ra['id'], 'quantita': -1}]}, 'quantita_non_valida'),
            ({'righe': [{'id': ra['id'], 'quantita': 'tre'}]}, 'quantita_non_valida'),
            ({'righe': [{'id': ra['id'], 'spessore_mm': 0}]}, 'spessore_non_valido'),
            ({'righe': [{'id': 'nessuno', 'quantita': 1}]}, 'riga_sconosciuta'))
    for corpo, codice in casi:
        r = conferma(c, pid, **corpo)
        j = r.get_json() or {}
        check(f'{codice}', r.status_code == 400 and j.get('codice') == codice
              and j.get('error'), (r.status_code, j))
    r = conferma(c, pid, user_id='operaio')
    check('operaio: 403', r.status_code == 403)
    r = conferma(c, 'non-esiste-proprio')
    check('id non valido: 400', r.status_code == 400, r.status_code)
    r = conferma(c, str(uuid.uuid4()))
    check('pacchetto inesistente: 404', r.status_code == 404
          and r.get_json().get('codice') == 'pacchetto_non_trovato')
    check('nessun ordine creato dagli errori', not c.get('/api/orders').get_json()['orders'])

    # =====================================================================
    print('\n5) Conferma con le correzioni dell\'ufficio')
    r = conferma(c, pid, note='Imballo in cassa', righe=[
        {'id': ra['id'], 'quantita': 5},                                   # 4 -> 5
        {'id': rb['id'], 'materiale': 'S275', 'spessore_mm': '2,5'},       # materiale e spessore
        {'id': rc['id'], 'quantita': 0},                                   # tolta
        {'id': rx['id'], 'quantita': 2},                                   # fuori ordine: aggiunta
    ])
    d = r.get_json() or {}
    check('ordine creato (201)', r.status_code == 201 and d.get('success') and d.get('order_id'), d)
    oid = d.get('order_id')
    check('risposta completa', d.get('numero_ordine') == '4521'
          and 'cartellino_url' not in d
          and (d.get('ordine') or {}).get('id') == oid, d)
    s = models.SessionLocal()
    try:
        o = s.query(Order).filter(Order.id == oid).first()
        check('origine PACCHETTO, agganciato al pacchetto, senza prezzo',
              o.origine == 'PACCHETTO' and o.preventivo_id_origine == pid
              and o.prezzo_quotato is None and o.cliente == 'Officine Rossi S.r.l.'
              and o.note == 'Imballo in cassa'
              and o.data_consegna.date() == CONSEGNA, (o.origine, o.preventivo_id_origine))
        files_o = s.query(OrderFile).filter(OrderFile.order_id == oid).all()
        pdfs = [f for f in files_o if f.file_type == 'PDF']
        check('PDF d\'ordine allegato', len(pdfs) == 1 and os.path.isfile(pdfs[0].filepath)
              and pdfs[0].filepath.startswith(A.PDFS_FOLDER), [(f.filename, f.file_type) for f in files_o])
        notif = s.query(Notification).filter(Notification.user_id == 'capo',
                                             Notification.order_id == oid).count()
        check('capo avvisato del nuovo ordine', notif == 1, notif)
        p = s.query(Preventivo).filter(Preventivo.id == pid).first()
        check('pacchetto segnato come usato, testata allineata', p.status == 'ACCETTATO'
              and p.numero_ordine_cliente == '4521' and p.solo_tecnico is True)
    finally:
        s.close()
    cart = os.path.join(A.DRAWINGS_FOLDER, oid)
    check('disegni nella cartella dell\'ordine',
          sorted(n for n in os.listdir(cart) if os.path.isfile(os.path.join(cart, n)))
          == ['EXTRA-900.dxf', 'PZ-A100-00.dxf', 'PZ-B200-00.dxf'],
          os.listdir(cart) if os.path.isdir(cart) else None)
    lan = os.path.join(cart, 'LANTEK')
    tutti = sorted(os.path.relpath(os.path.join(b, f), lan).replace(os.sep, '/')
                   for b, _d, fs in os.walk(lan) for f in fs) if os.path.isdir(lan) else []
    check('cartella LANTEK: ogni pezzo una volta, dentro una cartella per lamiera',
          len(tutti) == 3 and all('/' in t for t in tutti), tutti)
    # il pulito dell'analisi va registrato sul pezzo: prima tutti "da preparare"
    check('pezzi del pacchetto pronti per Lantek (non da preparare)',
          tutti and not any(t.startswith('_DA PREPARARE/') for t in tutti), tutti)
    # cartella master: <cliente>\<numero>\ divisa per lamiera + originali
    ex = d.get('export_disegni') or {}
    albero = [os.path.relpath(os.path.join(b, f), ex.get('percorso') or _RETE).replace(os.sep, '/')
              for b, _d, fs in os.walk(ex.get('percorso') or _RETE) for f in fs]
    check('copia nella cartella di rete, divisa per lamiera',
          (ex.get('percorso') or '').endswith('4521') and len([x for x in albero if x.startswith('_DISEGNI ORIGINALI/')]) == 3
          and len([x for x in albero if not x.startswith('_DISEGNI ORIGINALI/')]) == 3, (ex, albero))
    check('file temporanei puliti',
          not os.path.exists(os.path.join(A.UPLOAD_FOLDER, 'preventivi_tmp', pid)) and not d.get('avviso'))
    dis = c.get(f'/api/orders/{oid}/distinta').get_json()
    rig = per_codice(dis.get('righe') or [])
    check('distinta dal pacchetto', dis.get('success') and dis.get('origine') == 'pacchetto'
          and set(rig) == {'PZ-A100-00', 'PZ-B200-00', 'EXTRA-900'}, (dis.get('origine'), list(rig)))
    check('distinta: correzioni applicate', rig.get('PZ-A100-00', {}).get('quantita') == 5
          and rig.get('PZ-B200-00', {}).get('materiale') == 'S275'
          and rig.get('PZ-B200-00', {}).get('spessore_mm') == 2.5
          and rig.get('EXTRA-900', {}).get('quantita') == 2
          and dis.get('totale_pezzi') == 5 + 6 + 2, rig)
    check('distinta: disegni collegati e ingombro', rig['PZ-A100-00'].get('disegno') == 'PZ-A100-00.dxf'
          and rig['PZ-A100-00'].get('ingombro') == 'disegno', rig['PZ-A100-00'])
    dg = c.get(f'/api/orders/{oid}/disegni').get_json()
    check('disegni dell\'ordine elencati', len(dg.get('disegni') or []) == 3, dg)
    r = conferma(c, pid)
    j = r.get_json() or {}
    check('seconda conferma: 409 con l\'ordine esistente', r.status_code == 409
          and j.get('codice') == 'gia_confermato' and j.get('order_id') == oid, j)
    lista = c.get('/api/preventivi?limit=500').get_json()
    check('anche usato resta fuori dal preventivatore', pid not in {p['id'] for p in lista['preventivi']})
    r = c.delete(f'/api/orders/da-pacchetto/{pid}', json={})
    check('un pacchetto diventato ordine non si annulla', r.status_code == 409)

    # =====================================================================
    print('\n6) Banco lamiere del laser')
    banco = c.get('/api/laser/banco').get_json()
    check('non ancora smistato: non e\' nel banco',
          not any(p['ordine_id'] == oid for g in banco['gruppi'] for p in g['pezzi']))
    banco = c.get('/api/laser/banco?da_smistare=1').get_json()
    check('fra i da smistare si', any(p['ordine_id'] == oid for g in banco['gruppi'] for p in g['pezzi']))
    s = models.SessionLocal()
    try:
        s.query(Order).filter(Order.id == oid).first().taglio_richiesto = True
        s.commit()
    finally:
        s.close()
    banco = c.get('/api/laser/banco').get_json()
    pezzi = {p['codice']: (g, p) for g in banco['gruppi'] for p in g['pezzi'] if p['ordine_id'] == oid}
    check('i pezzi dell\'ordine sono sul banco', set(pezzi) == {'PZ-A100-00', 'PZ-B200-00', 'EXTRA-900'},
          list(pezzi))
    ga, pa = pezzi.get('PZ-A100-00', ({}, {}))
    check('gruppo per materiale e spessore, ingombro dal disegno', ga.get('spessore_mm') == 3
          and pa.get('quantita') == 5 and abs((pa.get('w_mm') or 0) - 200) < 1
          and pa.get('ingombro') == 'disegno' and (pa.get('url_svg') or '').startswith(f'/api/orders/{oid}/dxf/'),
          (ga.get('chiave'), pa))
    gb, pb = pezzi.get('PZ-B200-00', ({}, {}))
    check('materiale corretto dall\'ufficio sul banco', gb.get('materiale') == 'S275'
          and gb.get('spessore_mm') == 2.5, gb.get('chiave'))
    check('non e\' fra gli ordini senza distinta', oid not in {x['ordine_id'] for x in banco['senza_distinta']})

    # =====================================================================
    print('\n7) Ordine gia\' caricato, annullamento, analisi abbandonate')
    r = analizza(c)
    d2 = r.get_json() or {}
    pid2 = d2.get('pacchetto_id')
    check('analisi: avviso ordine gia\' caricato', (d2.get('ordine_esistente') or {}).get('id') == oid,
          d2.get('ordine_esistente'))
    r = conferma(c, pid2)
    j = r.get_json() or {}
    check('conferma senza forza: 409 ordine_duplicato', r.status_code == 409
          and j.get('codice') == 'ordine_duplicato' and j.get('ordine_esistente', {}).get('id') == oid, j)
    check('il pacchetto resta confermabile', PreventivoManager.get_tecnico(pid2) is not None)
    r = CL['operaio'].delete(f'/api/orders/da-pacchetto/{pid2}', json={})
    check('annulla: operaio 403', r.status_code == 403)
    r = c.delete(f'/api/orders/da-pacchetto/{pid2}', json={})
    check('annulla: 200', r.status_code == 200 and r.get_json().get('success'), r.get_json())
    s = models.SessionLocal()
    try:
        check('annulla: preventivo tecnico cancellato davvero',
              s.query(Preventivo).filter(Preventivo.id == pid2).first() is None)
    finally:
        s.close()
    check('annulla: file temporanei cancellati',
          not os.path.exists(os.path.join(A.UPLOAD_FOLDER, 'preventivi_tmp', pid2)))
    r = c.delete(f'/api/orders/da-pacchetto/{pid2}', json={})
    check('annulla di nuovo: 404', r.status_code == 404)
    r = c.delete(f'/api/orders/da-pacchetto/{prev_vero}', json={})
    check('un preventivo vero non si cancella da qui', r.status_code == 404)

    # forza: un secondo ordine con lo stesso numero, se l'ufficio lo vuole
    d3 = analizza(c).get_json()
    r = conferma(c, d3['pacchetto_id'], forza=True)
    check('con forza si crea lo stesso', r.status_code == 201, r.get_json())

    # abbandonata da due giorni: sparisce alla prossima analisi
    d4 = analizza(c).get_json()
    vecchio = d4['pacchetto_id']
    s = models.SessionLocal()
    try:
        s.query(Preventivo).filter(Preventivo.id == vecchio).first().data_creazione = \
            datetime.utcnow() - timedelta(hours=30)
        s.commit()
    finally:
        s.close()
    d5 = analizza(c).get_json()
    s = models.SessionLocal()
    try:
        check('analisi abbandonata (30 ore) cancellata',
              s.query(Preventivo).filter(Preventivo.id == vecchio).first() is None
              and not os.path.exists(os.path.join(A.UPLOAD_FOLDER, 'preventivi_tmp', vecchio)))
        check('quella nuova resta', s.query(Preventivo).filter(
            Preventivo.id == d5['pacchetto_id']).first() is not None)
        check('i pacchetti usati non si toccano', s.query(Preventivo).filter(Preventivo.id == pid).first() is not None)
    finally:
        s.close()
    c.delete(f"/api/orders/da-pacchetto/{d5['pacchetto_id']}", json={})

    # =====================================================================
    print('\n8) Pacchetti incompleti')
    z = zip_di({'Ordine 7788.pdf': pdf_ordine(numero='7788')})
    r = analizza(c, zip_bytes=z)
    d = r.get_json() or {}
    check('solo PDF (ZIP): analisi riuscita', r.status_code == 200 and d.get('success'), d)
    rr = per_codice(d.get('righe') or [])
    check('solo PDF: righe dal PDF senza disegno', set(rr) == {'PZ-A100-00', 'PZ-B200-00', 'PZ-C300-00'}
          and all(x['disegno'] is None and x['url_svg'] is None for x in rr.values())
          and rr['PZ-A100-00']['quantita'] == 4, rr)
    r = conferma(c, d['pacchetto_id'], numero_ordine='7788')
    j = r.get_json() or {}
    check('solo PDF: ordine creato', r.status_code == 201, j)
    oid_pdf = j.get('order_id')
    dis = c.get(f'/api/orders/{oid_pdf}/distinta').get_json()
    check('solo PDF: distinta con 3 righe, nessun disegno', len(dis.get('righe') or []) == 3
          and not any(x.get('disegno') for x in dis['righe']), dis)
    s = models.SessionLocal()
    try:
        check('solo PDF: PDF allegato', s.query(OrderFile).filter(
            OrderFile.order_id == oid_pdf, OrderFile.file_type == 'PDF').count() == 1)
    finally:
        s.close()
    check('solo PDF: temporanei puliti',
          not os.path.exists(os.path.join(A.UPLOAD_FOLDER, 'preventivi_tmp', d['pacchetto_id'])))

    files, paths = cartella(pdf=pdf_libero(), dxf={'PZ-A100-00.dxf': DXF['PZ-A100-00.dxf'],
                                                   'PZ-B200-00.dxf': DXF['PZ-B200-00.dxf']},
                            nome_pdf='lettera.pdf')
    r = analizza(c, files=files, paths=paths)
    d = r.get_json() or {}
    rr = per_codice(d.get('righe') or [])
    check('PDF non riconosciuto: analisi riuscita lo stesso', r.status_code == 200 and d.get('success'), d)
    check('PDF non riconosciuto: un pezzo per disegno, quantita\' da controllare',
          set(rr) == {'PZ-A100-00', 'PZ-B200-00'}
          and all(x['quantita'] == 1 and x['quantita_fonte'] == 'mancante' and x['disegno'] for x in rr.values()),
          rr)
    check('PDF non riconosciuto: testata vuota da compilare, con avviso',
          d.get('cliente') == '' and d.get('numero_ordine') == '' and d.get('data_consegna') is None
          and any('quantita' in a.lower() for a in d.get('avvisi') or []), (d.get('cliente'), d.get('avvisi')))
    c.delete(f"/api/orders/da-pacchetto/{d['pacchetto_id']}", json={})

    z = zip_di({'disegno.dxf': DXF['EXTRA-900.dxf']})
    r = analizza(c, zip_bytes=z)
    check('senza PDF: errore chiaro', r.status_code == 400
          and r.get_json().get('codice') == 'pacchetto_illeggibile', r.get_json())
    # Caricare gli ordini e' dell'Ufficio (stazioni): prima lo poteva fare
    # anche il commerciale, che pero' non ha la pagina per farlo.
    r = analizza(c, user='comm')
    check("il commerciale non carica pacchetti d'ordine (e' dell'Ufficio)", r.status_code == 403, r.status_code)

    # =====================================================================
    print('\n9) Migrazione della colonna solo_tecnico')
    vecchio_db = os.path.join(_CARTELLE, 'vecchio.db')
    eng = create_engine('sqlite:///' + vecchio_db.replace(chr(92), '/'))
    try:
        with eng.connect() as conn:
            conn.execute(text('CREATE TABLE preventivi (id VARCHAR PRIMARY KEY, cliente VARCHAR)'))
            conn.execute(text("INSERT INTO preventivi (id, cliente) VALUES ('p1', 'Vecchio')"))
            conn.commit()
        n1 = _migra_colonne_sottosistema(eng, inspect(eng))
        n2 = _migra_colonne_sottosistema(eng, inspect(eng))
        cols = {x['name'] for x in inspect(eng).get_columns('preventivi')}
        with eng.connect() as conn:
            v = conn.execute(text("SELECT solo_tecnico FROM preventivi WHERE id='p1'")).scalar()
        check('colonna aggiunta, righe vecchie = 0', 'solo_tecnico' in cols and v == 0 and n1 >= 1, (cols, v))
        check('idempotente', n2 == 0, n2)
    finally:
        eng.dispose()

    print('\n' + '=' * 60)
    print(f'PASSATI: {OK}   FALLITI: {len(KO)}')
    if KO:
        print('Falliti: ' + ', '.join(KO))
    print('=' * 60)
    return 1 if KO else 0


if __name__ == '__main__':
    code = 1
    try:
        code = main()
    finally:
        A.ConfigManager.load_config = _config_vera
        try:
            _ENG.dispose()
            os.remove(_TMP)
        except Exception:
            pass
        shutil.rmtree(_CARTELLE, ignore_errors=True)
    sys.exit(code)
