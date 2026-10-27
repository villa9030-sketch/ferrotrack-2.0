"""Assiemi negli ordini dal pacchetto del cliente (backend/assiemi_ordine.py).

Il caso vero: DECA A 001221 BIS, riga 26T30SA0233-00 "Struttura x MTR" x2
(C26-232) + x1 (C26-233) = 3 assiemi; la sottocartella dell'assieme coi
disegni dei componenti, che nell'ordine non hanno una riga loro. Prima:
assieme x1, ogni componente x1 "controllato", 3 lamiere mandate a Lantek
con 1 pezzo, "Da procurare" con 1.

Copre:
 1. la quantita' dell'assieme dalla sua riga dell'ordine (righe ripetute
    sommate), nell'analisi del pacchetto e nel record dell'assieme
 2. componenti senza riga: "pezzi per assieme da confermare" (non 1 in
    silenzio), anche dopo la conferma dell'ufficio che non li tocca; se
    l'ufficio scrive il numero, e' controllato; distinta dell'assieme letta
    se c'e'
 3. pagina laser: l'assieme coi suoi pezzi, il blocco "pezzi per assieme"
    col campo, Lantek bloccato (Manda e file XML: 409)
 4. pezzi per assieme e quantita' dell'assieme scritti a mano: totali
    ricalcolati, storico, voci "Da procurare" che seguono
 5. tasto N: profilo e lunghezza obbligatori per tubo / laser tubo, pezzi
    per assieme chiesti se l'ordine non li dice, materiale scelto; proposta
    dalle misure del disegno (581x50 = tubo lato 50, L 581)
 6. voce "Da procurare" corretta dopo (profilo, materiale, pezzi per
    assieme), "da completare" e fuori dalle barre senza profilo
 7. quantita' cambiata dopo l'invio a Lantek: avviso "In Lantek: X ha 1, ora
    servono 6", dai file mandati o da Lantek; "Corretto in Lantek" lo chiude

Gira su DATABASE TEMPORANEO e LANTEK SIMULATO (nessun programma di Lantek).
Esecuzione: python app/tests/test_assiemi_ordine.py
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
os.environ['FERROTRACK_ESEMPI_DIR'] = tempfile.mkdtemp(prefix='ft_ass_esempi_')

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, AuditLog, DaProcurare, Order  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_assiemi_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend import lantek as L  # noqa: E402
from backend import api_laser as AL  # noqa: E402
from backend import procura as PC  # noqa: E402
from backend import assiemi_ordine as AO  # noqa: E402
from backend import lantek_correzioni as LC  # noqa: E402
from backend.database import PreventivoManager  # noqa: E402
from backend.preventivi import dxf_cache, rfq_importer  # noqa: E402
from backend.preventivi import tempi_laser as TL  # noqa: E402

_CARTELLE = tempfile.mkdtemp(prefix='ft_ass_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
A.PDFS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'pdfs')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)
os.makedirs(A.PDFS_FOLDER, exist_ok=True)
dxf_cache._CACHE_DB_PATH = os.path.join(_CARTELLE, 'dxf_cache.db')
_config_vera = A.ConfigManager.load_config
A.ConfigManager.load_config = staticmethod(lambda: {**(_config_vera() or {}),
                                                    'disegni_export_root': os.path.join(_CARTELLE, 'rete')})

# Lantek simulato: spento (nessun programma, nessun database di Lantek)
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
L.expert_aperto = lambda: False   # Lantek Expert chiuso (sul PC vero puo' essere aperto)

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


def audit(azione, oid=None):
    s = models.SessionLocal()
    try:
        q = s.query(AuditLog).filter(AuditLog.action == azione)
        if oid:
            q = q.filter(AuditLog.entity_id == oid)
        return [a.detail for a in q.all()]
    finally:
        s.close()


# ─── Il pacchetto (come DECA A 001221 BIS) ───────────────────────────────
CONSEGNA = (datetime.now() + timedelta(days=30)).date()
ASS = '26T30SA0233-00'


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


def dxf_rett(w, h):
    import ezdxf
    doc = ezdxf.new()
    doc.modelspace().add_lwpolyline([(0, 0), (w, 0), (w, h), (0, h)], close=True)
    s = io.StringIO()
    doc.write(s)
    return s.getvalue().encode('utf-8')


def pacchetto():
    files = {'Ordine/OAFA202601221.pdf': pdf_ordine(),
             'Ordine/disegni/11PCOPA0001-01.dxf': dxf_rett(450, 470),
             f'Ordine/disegni/{ASS}/191700045-00.dxf': dxf_rett(50, 43.6),
             f'Ordine/disegni/{ASS}/191700114-00.dxf': dxf_rett(88, 50),
             f'Ordine/disegni/{ASS}/26CCSA0038-00.dxf': dxf_rett(581, 50)}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for n, b in files.items():
            z.writestr(n, b)
    return buf.getvalue()


def per_codice(xs, k='codice'):
    return {x[k]: x for x in xs}


def main():
    from tests.accesso_aiuto import postazioni, entra, modalita, persona, PIN_UFFICIO
    s = models.SessionLocal()
    s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
    s.commit(); s.close()
    modalita('protetto')
    postazioni()
    persona('elena', 'Elena Ufficio', pin=PIN_UFFICIO)
    laser = entra(A.app, 'laser')
    uff = entra(A.app, 'ufficio', pin=PIN_UFFICIO)

    # =====================================================================
    print("\n1) Quantita' dell'assieme dalla sua riga dell'ordine")
    res = rfq_importer.process_rfq_package(pacchetto(), tollerante=True)
    check('importer: assieme x3 (2 + 1, righe ripetute sommate)', res.qta_assiemi.get(ASS) == 3, res.qta_assiemi)
    check('importer: descrizione dalla riga', 'Struttura x MTR' in (res.desc_assiemi.get(ASS) or ''), res.desc_assiemi)
    comp = [a for a in res.articoli if a.codice_assieme == ASS and not a.e_master]
    check('importer: i 3 componenti ci sono, "per_assieme" (non 1 in silenzio)',
          len(comp) == 3 and all(a.qta_fonte == 'per_assieme' for a in comp),
          [(a.codice, a.qta_fonte) for a in res.articoli])

    r = uff.post('/api/orders/da-pacchetto/analizza', data={'zip': (io.BytesIO(pacchetto()), 'p.zip')},
                 content_type='multipart/form-data')
    d = r.get_json() or {}
    check('analisi 200', r.status_code == 200 and d.get('success'), d.get('error'))
    a0 = (d.get('assiemi') or [{}])[0]
    check("analisi: l'assieme ha quantita' 3 dalla riga dell'ordine e la descrizione",
          a0.get('codice') == ASS and a0.get('quantita') == 3 and a0.get('quantita_fonte') == 'pdf'
          and 'Struttura' in (a0.get('descrizione') or ''), a0)
    righe = per_codice(d.get('righe') or [])
    check("analisi: il master dell'assieme non e' un pezzo", ASS not in righe, list(righe))
    check("analisi: componente senza riga = quantita' da controllare con l'avviso",
          righe.get('191700045-00', {}).get('quantita_fonte') == 'mancante'
          and any('per assieme' in a for a in righe['191700045-00'].get('avvisi') or []), righe.get('191700045-00'))
    pid = d.get('pacchetto_id')
    prev = PreventivoManager.get_tecnico(pid) or {}
    check("record dell'assieme: qty 3", [(x['codice_assieme'], x['qty']) for x in prev.get('assiemi') or []] == [(ASS, 3)],
          prev.get('assiemi'))

    # l'ufficio conferma mandando tutte le righe com'erano (cosi' fa la pagina)
    corpo = {'numero_ordine': 'A 001221 BIS', 'cliente': 'DECA S.r.l.', 'data_consegna': CONSEGNA.isoformat(),
             'righe': [{'id': x['id'], 'quantita': x['quantita'], 'materiale': x.get('materiale') or '',
                        'spessore_mm': x.get('spessore_mm')} for x in d['righe'] if not x['fuori_ordine']]}
    r = uff.post(f'/api/orders/da-pacchetto/{pid}/conferma', json=corpo)
    dc = r.get_json() or {}
    check('ordine creato', r.status_code == 201 and dc.get('success'), dc)
    oid = dc.get('order_id')
    arts = per_codice(PreventivoManager.get(pid, include_children=True)['articoli'])
    check("2) i componenti NON toccati dall'ufficio restano 'da confermare'",
          all(arts[c]['ordine']['stato'] == 'qta_da_confermare' for c in ('191700045-00', '191700114-00', '26CCSA0038-00')),
          {c: arts[c].get('ordine') for c in arts})
    check("   il pezzo con la sua riga e' controllato come prima", arts['11PCOPA0001-01']['ordine']['stato'] in ('controllato', 'ok'),
          arts['11PCOPA0001-01'].get('ordine'))

    # l'ufficio che scrive il numero lo conferma
    od = {'stato': 'qta_da_confermare', 'pos': 1, 'qta': 1}
    nuovi, _e = A._leggi_correzioni_pacchetto({'articoli': [{'id': 'x', 'codice': 'K', 'quantita': 1, 'ordine': od},
                                                            {'id': 'y', 'codice': 'J', 'quantita': 1, 'ordine': od}]},
                                              [{'id': 'x', 'quantita': 2}, {'id': 'y', 'quantita': 1}])
    st = {a['id']: (a['quantita'], a['ordine']['stato']) for a in nuovi}
    check("   ufficio: numero cambiato = controllato, lasciato com'era = resta da confermare",
          st == {'x': (2, 'controllato'), 'y': (1, 'qta_da_confermare')}, st)

    # distinta del PDF dell'assieme (se c'e'): i pezzi per assieme si leggono li'
    import backend.preventivi.distinte_pdf as DP
    prima_dist = DP.distinte_per_codici
    DP.distinte_per_codici = lambda cart, cod, **kw: {'26T30SA0233': {'righe': [{'codice': '191700045-00', 'qta': 2},
                                                                          {'codice': '191700045', 'qta': 1}]}}
    try:
        res2 = rfq_importer.process_rfq_package(pacchetto(), tollerante=True)
        n = AO.quantita_da_distinte(_CARTELLE, res2)
        x = {a.codice: (a.quantita, a.qta_fonte) for a in res2.articoli if a.codice_assieme == ASS and not a.e_master}
        check("   distinta dell'assieme: 191700045 = 3 per assieme, gli altri restano da confermare",
              n == 1 and x['191700045-00'] == (3, 'distinta') and x['191700114-00'] == (1, 'per_assieme'), x)
    finally:
        DP.distinte_per_codici = prima_dist

    # =====================================================================
    print('\n3) Pagina laser: assieme, blocco "pezzi per assieme", Lantek fermo')
    A.OrderManager.smista(oid, True, user_id='laser')
    AL.invalida(oid)
    j = laser.get(f'/api/orders/{oid}/laser').get_json() or {}
    check('ordine laser 200', j.get('success'), j.get('error'))
    ass = j.get('assiemi') or []
    check("l'assieme c'e' con quantita' 3, descrizione e 3 componenti da confermare",
          len(ass) == 1 and ass[0]['quantita'] == 3 and 'Struttura' in (ass[0].get('descrizione') or '')
          and ass[0]['n_componenti'] == 3 and ass[0]['n_da_confermare'] == 3, ass)
    pz = per_codice(j.get('pezzi') or [])
    check('i componenti hanno assieme, pezzi per assieme e "da confermare"',
          pz['191700045-00']['assieme'] == ASS and pz['191700045-00']['qta_per'] == 1
          and pz['191700045-00']['qta_da_confermare'] and pz['191700045-00']['quantita'] == 3, pz.get('191700045-00'))
    bl = per_codice(j.get('bloccati') or [])
    b = bl.get('191700045-00') or {}
    check('bloccato col campo "pezzi per assieme"', b.get('campo') == 'pz_assieme' and b.get('articolo_id')
          and b.get('assieme') == ASS and b.get('qta_assieme') == 3 and 'per assieme' in b.get('motivo', ''), b)
    check('non pronto per Lantek', not j.get('pronto') and j.get('n_qta_da_confermare') == 3, j.get('n_qta_da_confermare'))
    r = laser.post(f'/api/orders/{oid}/lantek-invia', json={'codici': [], 'nuovi': []})
    check('Manda a Lantek: 409 qta_da_confermare coi codici', r.status_code == 409
          and r.get_json().get('codice') == 'qta_da_confermare' and '191700045-00' in r.get_json().get('codici'), r.get_json())
    r = laser.get(f'/api/orders/{oid}/lantek-ordini.xml')
    check('file XML per Lantek: 409 anche lui', r.status_code == 409
          and (r.get_json() or {}).get('codice') == 'qta_da_confermare', r.status_code)

    # =====================================================================
    print('\n4) Pezzi per assieme e assiemi scritti a mano')
    aid45 = pz['191700045-00']['articolo_id']
    aid114 = pz['191700114-00']['articolo_id']
    aid38 = pz['26CCSA0038-00']['articolo_id']
    r = laser.post(f'/api/orders/{oid}/pezzi/{aid45}/per-assieme', json={'pezzi': 'due'})
    check('pezzi non validi: 400', r.status_code == 400, r.get_json())
    r = laser.post(f'/api/orders/{oid}/pezzi/{aid45}/per-assieme', json={'pezzi': 2})
    check('2 per assieme x 3 assiemi = 6', r.status_code == 200 and r.get_json().get('quantita') == 6, r.get_json())
    r = laser.post(f'/api/orders/{oid}/pezzi/{aid114}/per-assieme', json={'pezzi': 1})
    check('1 per assieme (confermato anche se era gia\' 1)', r.status_code == 200 and r.get_json().get('quantita') == 3, r.get_json())
    det = audit('PEZZO_PER_ASSIEME', oid)
    check('storico: chi ha scritto i pezzi per assieme', any('191700045-00' in x and '1 -> 2' in x and 'confermati' in x for x in det), det)
    j = laser.get(f'/api/orders/{oid}/laser').get_json() or {}
    pz = per_codice(j.get('pezzi') or [])
    check('confermati: non piu\' bloccati, quantita\' 6 e 3',
          not pz['191700045-00']['qta_da_confermare'] and pz['191700045-00']['quantita'] == 6
          and pz['191700114-00']['quantita'] == 3 and '191700045-00' not in per_codice(j.get('bloccati') or []),
          (pz['191700045-00'], j.get('bloccati')))
    r = laser.post(f'/api/orders/{oid}/assiemi/{ASS}/quantita', json={'quantita': 0})
    check('assiemi 0: 400', r.status_code == 400, r.get_json())
    r = laser.post(f'/api/orders/{oid}/assiemi/NONCE/quantita', json={'quantita': 2})
    check('assieme che non c\'e\': 404', r.status_code == 404, r.get_json())
    r = laser.post(f'/api/orders/{oid}/assiemi/{ASS}/quantita', json={'quantita': 4})
    check('assieme 3 -> 4', r.status_code == 200 and r.get_json().get('prima') == 3, r.get_json())
    check('storico dell\'assieme', any('3 -> 4' in x for x in audit('ASSIEME_QUANTITA', oid)), audit('ASSIEME_QUANTITA', oid))
    j = laser.get(f'/api/orders/{oid}/laser').get_json() or {}
    pz = per_codice(j.get('pezzi') or [])
    check('tutti i componenti x4: 8 e 4', pz['191700045-00']['quantita'] == 8 and pz['191700114-00']['quantita'] == 4
          and (j.get('assiemi') or [{}])[0].get('quantita') == 4, (pz['191700045-00']['quantita'], j.get('assiemi')))

    # =====================================================================
    print('\n5) Tasto N: quale tubo, quanti per assieme')
    r = laser.get(f'/api/orders/{oid}/pezzi/{aid38}/procura-proposta')
    pr = r.get_json() or {}
    p = pr.get('proposta') or {}
    check('proposta dalle misure del disegno: tubo lato 50, L 581',
          p.get('tipo') == 'tubo' and p.get('lunghezza_mm') == 581 and p.get('lato_mm') == 50
          and p.get('fonte') == 'misure del disegno' and not p.get('profilo'), p)
    check('proposta: assieme x4, pezzi per assieme da confermare, materiale (dal cartiglio, qui vuoto) e profili del catalogo',
          pr.get('assieme') == ASS and pr.get('qta_assieme') == 4 and pr.get('qta_da_confermare')
          and 'materiale' in pr and len(pr.get('profili') or []) > 50, {k: pr.get(k) for k in ('assieme', 'qta_assieme', 'qta_da_confermare', 'materiale')})
    url = f'/api/orders/{oid}/pezzi/{aid38}/decisione'
    r = laser.post(url, json={'scelta': 'non_laser', 'procura': {'tipo': 'tubo', 'lunghezza_mm': 581, 'per_assieme': 1}})
    check('T senza profilo: 400 "Quale tubo?"', r.status_code == 400 and 'Quale tubo' in r.get_json().get('error', ''), r.get_json())
    r = laser.post(url, json={'scelta': 'non_laser', 'procura': {'tipo': 'laser_tubo', 'profilo': 'Tubo quadro 50x50 sp.3mm', 'per_assieme': 1}})
    check('L senza lunghezza: 400', r.status_code == 400 and 'lunghezza' in r.get_json().get('error', ''), r.get_json())
    r = laser.post(url, json={'scelta': 'non_laser', 'procura': {'tipo': 'tubo', 'profilo': 'Tubo quadro 50x50 sp.3mm', 'lunghezza_mm': 581}})
    check('pezzi per assieme non detti dall\'ordine: li chiede', r.status_code == 400
          and r.get_json().get('codice') == 'qta_da_confermare', r.get_json())
    r = laser.post(url, json={'scelta': 'non_laser', 'procura': {'tipo': 'tubo', 'profilo': 'Tubo quadro 50x50 sp.3mm',
                                                                  'lunghezza_mm': 581, 'per_assieme': 2, 'materiale': 'INOX 304'}})
    v = (r.get_json() or {}).get('procura') or {}
    check('T col profilo: voce con 2 x 4 = 8 pezzi, materiale scelto', r.status_code == 200 and v.get('quantita') == 8
          and v.get('profilo') == 'Tubo quadro 50x50 sp.3mm' and v.get('lunghezza_mm') == 581 and v.get('materiale') == 'INOX 304', v)
    j = laser.get(f'/api/orders/{oid}/laser').get_json() or {}
    vv = per_codice(j.get('procura_voci') or [])
    x = vv.get('26CCSA0038-00') or {}
    check("la voce sa dell'assieme: 2 per assieme x 4", x.get('assieme') == ASS and x.get('per_assieme') == 2
          and x.get('qta_assieme') == 4 and not x.get('qta_da_confermare') and not x.get('da_completare'), x)

    # =====================================================================
    print("\n6) Voce corretta dopo, cambio dell'assieme, da completare")
    laser.post(f'/api/orders/{oid}/assiemi/{ASS}/quantita', json={'quantita': 3})
    x = per_codice(PC.elenco('tutti', ordine=oid))['26CCSA0038-00']
    check("assieme di nuovo x3: la voce segue (2 x 3 = 6)", x['quantita'] == 6, x['quantita'])
    r = uff.post(f"/api/procura/{x['id']}", json={'quantita': 10})
    check("quantita' di un pezzo d'assieme: no, si scrivono i pezzi per assieme", r.status_code == 400
          and r.get_json().get('codice') == 'per_assieme', r.get_json())
    r = uff.post(f"/api/procura/{x['id']}", json={'per_assieme': 3, 'profilo': 'Tubo rett. 50x30 sp.2mm',
                                                    'materiale': 'INOX 316', 'lunghezza_mm': 575})
    y = (r.get_json() or {}).get('voce') or {}
    check('corretta: 3 x 3 = 9, profilo, lunghezza e materiale', r.status_code == 200 and y.get('quantita') == 9
          and y.get('profilo') == 'Tubo rett. 50x30 sp.2mm' and y.get('materiale') == 'INOX 316' and y.get('lunghezza_mm') == 575, y)
    check('storico della voce e dei pezzi per assieme', any('INOX 316' in z for z in audit('PROCURA_MODIFICA', oid))
          and any('26CCSA0038-00' in z and '2 -> 3' in z for z in audit('PEZZO_PER_ASSIEME', oid)))
    r = uff.post(f"/api/procura/{x['id']}", json={'profilo': ''})
    check('il profilo di un tubo non si toglie', r.status_code == 400, r.get_json())
    s = models.SessionLocal()
    s.add(DaProcurare(order_id=oid, chiave='prova:l', origine='classificato', codice='26CCSA0099-00', tipo='laser_tubo',
                      quantita=1, stato='da_ordinare'))
    s.add(DaProcurare(order_id=oid, chiave='prova:t', origine='classificato', codice='26CCSA0098-00', tipo='tubo',
                      quantita=2, lunghezza_mm=300, stato='da_ordinare'))
    s.commit(); s.close()
    tutte = per_codice(PC.elenco('tutti', ordine=oid))
    check('senza profilo: "da completare"', tutte['26CCSA0099-00']['da_completare'] == 'manca profilo e lunghezza'
          and tutte['26CCSA0098-00']['da_completare'] == 'manca profilo', tutte['26CCSA0099-00']['da_completare'])
    g = PC.gruppi_tubi(list(tutte.values()))
    check('fuori dal calcolo delle barre', all('26CCSA0098-00' not in [t['codice'] for b in gg['piano'] for t in b['tagli']]
                                               for gg in g) and any(gg['profilo'] == 'Tubo rett. 50x30 sp.2mm' for gg in g), g)

    # =====================================================================
    print("\n7) Quantita' cambiata dopo l'invio a Lantek")
    o = A._ordine_esistente(oid)
    commessa = L.commessa_lantek(A._numero_per_cartelle(o) or o.numero_ordine)
    log = os.path.join(A.UPLOAD_FOLDER, 'lantek_import', 'Importer log')
    os.makedirs(log, exist_ok=True)
    righe_xml = [{'codice': c, 'quantita': 1} for c in ('191700045-00', '191700114-00')] + \
                [{'codice': '11PCOPA0001-01', 'quantita': 3}]
    with open(os.path.join(log, 'ORDINI_LANTEK_-_prova_2026-10-12 142446.xml'), 'wb') as f:
        f.write(L.xml_ordini_produzione(righe_xml, commessa, 'DECA', CONSEGNA.isoformat(), invio='2610121424'))
    check('file mandati letti: 1, 1, 3', LC.quantita_inviate(commessa, os.path.join(A.UPLOAD_FOLDER, 'lantek_import'))
          == {'191700045-00': 1, '191700114-00': 1, '11PCOPA0001-01': 3})
    AL.invalida(oid)
    j = laser.get(f'/api/orders/{oid}/laser').get_json() or {}
    cc = per_codice([c for c in j.get('correzioni') or [] if c.get('tipo') == 'quantita'])
    c45 = cc.get('191700045-00') or {}
    check('avviso: "In Lantek: 191700045-00 ha 1, ora servono 6"', c45.get('testo', '').startswith(
        "In Lantek: 191700045-00 ha 1, ora servono 6") and c45.get('stato') == 'da_correggere' and c45.get('fonte') == 'invio', c45)
    check('anche 191700114-00 (1 -> 3); il pezzo giusto (3) no', cc.get('191700114-00', {}).get('quantita_voluta') == 3
          and '11PCOPA0001-01' not in cc, list(cc))
    check("l'ordine conta i codici da correggere", j.get('n_correggere') == 2 and not j.get('tutto_in_lantek'), j.get('n_correggere'))
    r = laser.post(f'/api/orders/{oid}/lantek-correzioni', json={'codice': '191700045-00', 'azione': 'quantita_corretta',
                                                                 'quantita': 6, 'quantita_lantek': 1})
    check('"Corretto in Lantek" salvato', r.status_code == 200, r.get_json())
    j = laser.get(f'/api/orders/{oid}/laser').get_json() or {}
    cc = per_codice([c for c in j.get('correzioni') or [] if c.get('tipo') == 'quantita'])
    check('191700045-00 non piu\' da correggere, 191700114-00 si\'', '191700045-00' not in cc and '191700114-00' in cc, list(cc))
    check('storico della correzione', any('1 -> 6' in z for z in audit('LANTEK_QUANTITA_CORRETTA', oid)))
    laser.post(f'/api/orders/{oid}/pezzi/{aid45}/per-assieme', json={'pezzi': 3})
    j = laser.get(f'/api/orders/{oid}/laser').get_json() or {}
    cc = per_codice([c for c in j.get('correzioni') or [] if c.get('tipo') == 'quantita'])
    check('cambiata ancora (9): di nuovo da correggere', cc.get('191700045-00', {}).get('quantita_voluta') == 9, list(cc))
    # Lantek acceso: e' lui a dire cosa c'e' (aperti + fatti)
    LANTEK.update(su=True, ordine={'191700045-00': 9, '191700114-00': 1, '11PCOPA0001-01': 3})
    L._svuota_cache()
    AL.invalida(oid)
    j = laser.get(f'/api/orders/{oid}/laser?fresco=1').get_json() or {}
    cc = per_codice([c for c in j.get('correzioni') or [] if c.get('tipo') == 'quantita'])
    check('Lantek acceso: 191700045-00 ha 9 = a posto; 191700114-00 ha 1, servono 3',
          '191700045-00' not in cc and cc.get('191700114-00', {}).get('fonte') == 'lantek', cc)
    LANTEK.update(su=False, ordine={})

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
