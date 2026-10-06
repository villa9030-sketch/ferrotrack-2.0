"""Test del TABLET OFFICINA: cosa c'e' da fare, per reparto, e "Segnala un problema".

Il tablet appeso in officina deve dire a colpo d'occhio quali ordini servono
alla piega, alla saldatura, alla filettatura e al montaggio, col numero
dell'ordine del cliente (come il laser), le note dell'ufficio e quanto e'
tagliato. E da li' un ragazzo deve poter avvisare l'ufficio di un problema
senza alzarsi a cercare qualcuno.

Copre:
 1. traduzione delle lavorazioni in reparti (tablet_officina): testi del
    preventivatore, maiuscole, dizionari, solo taglio, assiemi, pieghe
 2. stato del taglio pezzo per pezzo: ricavato dall'ordine (tutto/niente/in
    Lantek) e, col dato per pezzo, "parziale"; i tubi non passano dal laser
 3. GET /api/officina/tablet: chi puo' (reparto, ufficio, persona attiva) e
    chi no (nessuno, token sbagliato, tablet delle ore, utente sconosciuto)
 4. forma e valori: numero del cliente, note, reparti contati, pezzi coi
    disegni, piegato 3D solo per chi piega, STEP dell'assieme, ordini
    archiviati e finiti esclusi
 5. POST /api/orders/<id>/segnalazioni: notifica all'ufficio (solo utenze
    d'ufficio attive), audit, doppioni entro 10 minuti, tipi e note non
    validi, pezzo estraneo, ordine inesistente, permessi
 6. gli endpoint di prima rispondono come prima (distinta, disegni, lista
    ordini, piegato 3D del preventivo)

Gira su DATABASE TEMPORANEO e cartelle temporanee. Esecuzione:
    python app/tests/test_tablet_officina.py
"""
import os
import shutil
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Order, Preventivo, AuditLog, Notification  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_tablet_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')  # il MODULO, non l'oggetto Flask
from backend.database import PreventivoManager  # noqa: E402
from backend.auth_device import crea_token  # noqa: E402
from tests.accesso_aiuto import persona, per_token, modalita, entra, PIN_UFFICIO  # noqa: E402
from backend import tablet_officina as tab  # noqa: E402

_CARTELLE = tempfile.mkdtemp(prefix='ft_tablet_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)

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


def imposta(oid, **campi):
    s = models.SessionLocal()
    try:
        o = s.query(Order).filter(Order.id == oid).first()
        for k, v in campi.items():
            setattr(o, k, v)
        s.commit()
    finally:
        s.close()


def ordine_da_preventivo():
    """Un ordine vero nato da un preventivo: pieghe, saldatura, filettatura,
    svasatura, un assieme da montare e saldare, un tubo. Torna (order_id, prev_id)."""
    pr = PreventivoManager.create('DECA', 'paolo', quantita=2, numero_ordine_cliente='1252')
    pid = pr['id'] if isinstance(pr, dict) else pr
    PreventivoManager.replace_articoli(pid, [
        # sciolto: 3 x lotto 2 = 6 pezzi; piega 3 volte, saldato, 4 filetti
        {'codice': 'A1', 'quantita': 3, 'materiale': 'S235', 'spessore_mm': 3,
         'pieghe': 3, 'saldatura_ml': 0.4, 'filettatura_pz': 4, 'dxf_filename': 'A1.dxf',
         'area_dm2': 1.8, 'costo_base_override': 5},
        # nell'assieme ASS1 (x4): 2 x 4 = 8 pezzi, svasati, senza pieghe
        {'codice': 'B1', 'quantita': 2, 'materiale': 'INOX_304', 'spessore_mm': 2,
         'svasatura_pz': 2, 'codice_assieme': 'ASS1', 'dxf_filename': 'B1.dxf',
         'area_dm2': 1.0, 'costo_base_override': 4},
        # solo taglio: 1 x lotto 2 = 2 pezzi
        {'codice': 'C1', 'quantita': 1, 'materiale': 'S235', 'spessore_mm': 3,
         'area_dm2': 0.5, 'costo_base_override': 2},
    ])
    PreventivoManager.replace_assiemi(pid, [{'codice_assieme': 'ASS1', 'qty': 4, 'costo': 10,
                                             'ore_montaggio': 1, 'saldatura_mt': 2}])
    PreventivoManager.replace_tubolari(pid, [{'profilo': 'Quadro 40x40 sp.2mm',
                                              'lunghezza_m': 0.5, 'qty': 2, 'n_tagli_dritti': 2}])
    s = models.SessionLocal()
    try:
        s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'INVIATO'
        s.commit()
    finally:
        s.close()
    acc = PreventivoManager.accetta_e_crea_ordine(pid, 'paolo')
    return acc.get('order_id'), pid


def prova_traduzione():
    print('\n1. Lavorazioni -> reparti')
    check('piegatura del preventivatore', tab.reparti_di(['Taglio laser', 'Piegatura (3 pieghe)']) == ['piega'])
    check('maiuscole e parole brevi', tab.reparti_di(['PIEGA', 'saldatura']) == ['piega', 'saldatura'])
    check('filettatura e svasatura nello stesso reparto',
          tab.reparti_di(['Filettatura (4)', 'Svasatura (2)']) == ['filettatura'])
    check('maschiatura come dizionario (ordini a mano)',
          tab.reparti_di([{'nome': 'Maschiatura', 'quantita': 3}]) == ['filettatura'])
    check('puntatura: saldatura e montaggio', tab.reparti_di(['Puntatura']) == ['saldatura', 'montaggio'])
    check('solo taglio: nessun reparto',
          tab.reparti_di(['Taglio laser', 'Taglio dritto (2)', 'Taglio']) == [])
    check('un assieme si monta sempre', tab.reparti_di([], 'assieme') == ['montaggio'])
    check('niente lavorazioni: niente reparti', tab.reparti_di(None) == [])
    check('pieghe contate', tab.pieghe_di(['Taglio laser', 'Piegatura (3 pieghe)']) == 3)
    check('piega senza numero vale 1', tab.pieghe_di(['Piega']) == 1)
    check('senza piega: zero', tab.pieghe_di(['Saldatura']) == 0)
    check('il taglio non e\' una lavorazione d\'officina',
          tab.lavorazioni_officina(['Taglio laser', 'Saldatura']) == ['Saldatura'])


def prova_taglio_pezzi():
    print('\n2. Taglio pezzo per pezzo')

    class O:
        importato_lantek_il = None
    righe = [{'codice': 'A', 'tipo': 'lamiera', 'quantita': 6},
             {'codice': 'T', 'tipo': 'tubolare', 'quantita': 2},
             {'codice': 'S', 'tipo': 'assieme', 'quantita': 1}]
    st, rie = tab.stato_taglio_pezzi(O(), righe, 'tagliato')
    check('ordine tagliato: lamiere tagliate', st[0]['stato'] == 'tagliato' and st[0]['tagliati'] == 6)
    check('tubi e assiemi non passano dal laser', st[1]['stato'] == 'non_laser' and st[2]['stato'] == 'non_laser')
    check('riepilogo: 6 su 6, ricavato dall\'ordine', rie == {'tagliati': 6, 'totale': 6, 'fonte': 'ordine'}, rie)
    st, rie = tab.stato_taglio_pezzi(O(), righe, 'da_tagliare')
    check('da tagliare: nessun pezzo inventato come tagliato', st[0]['stato'] == 'da_tagliare' and rie['tagliati'] == 0)
    o = O()
    o.importato_lantek_il = datetime.utcnow()
    st, _ = tab.stato_taglio_pezzi(o, righe, 'da_tagliare')
    check('importato: "in Lantek"', st[0]['stato'] == 'in_lantek')
    st, _ = tab.stato_taglio_pezzi(O(), righe, 'non_serve')
    check('ordine che non passa dal laser', st[0]['stato'] == 'non_laser')
    st, rie = tab.stato_taglio_pezzi(O(), righe, 'da_tagliare', {'A': 4})
    check('col dato di Lantek: parziale 4 su 6',
          st[0]['stato'] == 'parziale' and st[0]['tagliati'] == 4 and rie['fonte'] == 'lantek', (st[0], rie))
    check('aggancio Lantek oggi vuoto (nessun dato inventato)', tab.tagliati_da_lantek(O(), righe) is None)


def main():
    prova_traduzione()
    prova_taglio_pezzi()

    s = models.SessionLocal()
    try:
        s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
        # Gli avvisi vanno alle POSTAZIONI d'ufficio (la campanella della
        # stazione), non alle persone che ci entrano col PIN.
        s.add(User(id='uff1', name='Elena', role='Amministrazione', is_active=True, e_postazione=True))
        s.add(User(id='uff2', name='Marta', role='Impiegata', is_active=True, e_postazione=True))
        s.add(User(id='uff-persona', name='Gina', role='Amministrazione', is_active=True))
        s.add(User(id='uff-via', name='Ex', role='Amministrazione', is_active=False))
        s.add(User(id='op1', name='Luca', role='Operaio Officina', is_active=True))
        oggi = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        s.add(Order(id='o-mano', numero_ordine='4711', cliente='Meccanica Brianza', status='RICEVUTO',
                    data_ricezione=datetime.utcnow(), data_consegna=oggi - timedelta(days=3),
                    note='Vernice a parte'))
        s.add(Order(id='o-chiuso', numero_ordine='9', cliente='Vecchio', status='CHIUSO',
                    data_ricezione=datetime.utcnow(), data_consegna=oggi))
        s.add(Order(id='o-finito', numero_ordine='10', cliente='Finito', status='RICEVUTO',
                    data_ricezione=datetime.utcnow(), data_consegna=oggi,
                    data_completamento_operativo=datetime.utcnow()))
        s.commit()
    finally:
        s.close()

    oid, pid = ordine_da_preventivo()
    check('ordine creato dal preventivo', bool(oid), oid)
    imposta(oid, note='Piegare prima A1, il cliente passa alle 14', data_consegna=datetime(2026, 10, 20))
    # disegni dell'ordine (come li copia l'accettazione) e STEP dell'assieme
    cart = os.path.join(A.DRAWINGS_FOLDER, oid)
    os.makedirs(cart, exist_ok=True)
    for n in ('A1.dxf', 'B1.dxf'):
        with open(os.path.join(cart, n), 'w') as f:
            f.write('0\nEOF\n')
    cart_prev = os.path.join(A.UPLOAD_FOLDER, 'preventivi_tmp', pid)
    os.makedirs(cart_prev, exist_ok=True)
    with open(os.path.join(cart_prev, 'ASS1-00.step'), 'w') as f:
        f.write('ISO-10303-21;')

    tok_rep = crea_token('Tablet officina 1', 'reparto')['token']
    tok_uff = crea_token('PC ufficio', 'ufficio')['token']
    tok_ore = crea_token('Tablet ore', 'ore')['token']
    REP = {'X-Device-Token': tok_rep}
    # Ogni token e' un dispositivo (un browser a se'); l'Ufficio e' una
    # persona entrata col PIN.
    modalita('protetto')
    persona('uff-pin', 'Persona Ufficio', 'Amministrazione', pin=PIN_UFFICIO)
    C = per_token(A.app, pin_ufficio=PIN_UFFICIO)
    anon = A.app.test_client()

    print('\n3. Chi puo\' leggere il tablet')
    check('senza niente: 401', anon.get('/api/officina/tablet').status_code == 401)
    check('token inventato: 401', anon.get('/api/officina/tablet', headers={'X-Device-Token': 'boh'}).status_code == 401)
    r = C(tok_ore).get('/api/officina/tablet', headers={'X-Device-Token': tok_ore})
    check('tablet delle ore: 403', r.status_code == 403 and r.get_json().get('codice') == 'stazione_non_ammessa', r.status_code)
    check('tablet di reparto: 200', C(tok_rep).get('/api/officina/tablet', headers=REP).status_code == 200)
    check('PC ufficio: 200', C(tok_uff).get('/api/officina/tablet', headers={'X-Device-Token': tok_uff}).status_code == 200)
    check('Bearer come gli altri endpoint',
          A.app.test_client().get('/api/officina/tablet', headers={'Authorization': 'Bearer ' + tok_rep}).status_code == 200)
    check('una persona che si dichiara col suo nome (X-User-Id) non basta piu\': 401',
          anon.get('/api/officina/tablet', headers={'X-User-Id': 'op1'}).status_code == 401)
    check('utente sconosciuto: 401', anon.get('/api/officina/tablet', headers={'X-User-Id': 'chi-sei'}).status_code == 401)
    check('utente disattivato: 401', anon.get('/api/officina/tablet', headers={'X-User-Id': 'uff-via'}).status_code == 401)
    c = C(tok_rep)          # da qui in poi: il tablet di reparto

    print('\n4. Cosa mostra')
    t0 = time.perf_counter()
    d = c.get('/api/officina/tablet', headers=REP).get_json()
    ms = (time.perf_counter() - t0) * 1000
    ordini = {o['id']: o for o in d['ordini']}
    check('aperti si, archiviati e finiti no',
          set(ordini) == {'o-mano', oid}, sorted(ordini))
    check('dalla consegna piu\' vicina', [o['id'] for o in d['ordini']] == ['o-mano', oid])
    o = ordini[oid]
    check('numero del CLIENTE, non PREV-...', o['numero'] == '1252' and o['numero_ordine_cliente'] == '1252'
          and o['numero_ordine'].startswith('PREV-'), (o['numero'], o['numero_ordine']))
    check('note dell\'ufficio', o['note'].startswith('Piegare prima A1'))
    check('consegna come data di calendario', o['data_consegna'] == '2026-10-20', o['data_consegna'])
    rep = {r['reparto']: r for r in o['reparti']}
    check('reparti nell\'ordine giusto', [r['reparto'] for r in o['reparti']] == ['piega', 'saldatura', 'filettatura', 'montaggio'],
          [r['reparto'] for r in o['reparti']])
    check('piega: 6 pezzi (A1 3 x lotto 2)', rep['piega']['pezzi'] == 6 and rep['piega']['codici'] == 1, rep['piega'])
    check('filettatura e svasatura: A1 + B1 = 14 pezzi', rep['filettatura']['pezzi'] == 14 and rep['filettatura']['codici'] == 2,
          rep['filettatura'])
    check('saldatura: A1 e l\'assieme', rep['saldatura']['codici'] == 2, rep['saldatura'])
    check('montaggio: l\'assieme', rep['montaggio']['codici'] == 1 and rep['montaggio']['pezzi'] == 4, rep['montaggio'])
    pz = {p['codice']: p for p in o['pezzi']}
    a1 = pz.get('A1', {})
    check('pezzo: materiale, spessore, quantita\', pieghe',
          a1.get('materiale') == 'S235' and a1.get('spessore_mm') == 3 and a1.get('quantita') == 6 and a1.get('pieghe') == 3, a1)
    check('pezzo: lavorazioni senza il taglio', 'Taglio laser' not in a1.get('lavorazioni', []) and 'Saldatura' in a1.get('lavorazioni', []))
    check('pezzo: reparti', a1.get('reparti') == ['piega', 'saldatura', 'filettatura'], a1.get('reparti'))
    check('pezzo col disegno e l\'anteprima', a1.get('disegno') == 'A1.dxf' and (a1.get('url_svg') or '').endswith('/A1.dxf/svg'))
    check('piegato 3D per chi piega, con lo spessore',
          (a1.get('url_piega_3d') or '').endswith('/A1.dxf/fold-model?spessore=3'), a1.get('url_piega_3d'))
    check('niente piegato 3D per chi non piega', pz['B1'].get('url_piega_3d') is None and pz['C1'].get('url_piega_3d') is None)
    check('C1 solo taglio: nessun reparto', pz['C1']['reparti'] == [])
    ass = pz.get('ASS1', {})
    check('assieme: STEP trovato anche con la revisione (-00)',
          ass.get('url_step') == f'/api/preventivi/{pid}/step/ASS1-00.step', ass.get('url_step'))
    check('assieme da montare e saldare', ass.get('reparti') == ['saldatura', 'montaggio'], ass.get('reparti'))
    tubo = next((p for p in o['pezzi'] if p['tipo'] == 'tubolare'), {})
    check('il tubo non passa dal laser', tubo.get('taglio', {}).get('stato') == 'non_laser')
    check('disegni dell\'ordine', o['n_disegni'] == 2 and len(o['disegni']) == 2)
    check('pezzi totali senza gli assiemi', o['pezzi_totali'] == 6 + 8 + 2 + 2, o['pezzi_totali'])
    check('mai smistato: da tagliare, 0 su 16', o['stato_taglio'] == 'da_smistare'
          and o['taglio'] == {'tagliati': 0, 'totale': 16, 'fonte': 'ordine'}, o['taglio'])
    check('senza PDF', o['has_pdf'] is False)
    m = ordini['o-mano']
    check('ordine a mano: numero suo, nota, nessuna distinta',
          m['numero'] == '4711' and m['note'] == 'Vernice a parte' and m['pezzi'] == [] and m['reparti'] == [])

    imposta(oid, taglio_richiesto=True, importato_lantek_il=datetime.utcnow())
    o = {x['id']: x for x in c.get('/api/officina/tablet', headers=REP).get_json()['ordini']}[oid]
    check('importato: pezzi "in Lantek"', o['fase_laser'] == 'in_lantek'
          and {p['codice']: p for p in o['pezzi']}['A1']['taglio']['stato'] == 'in_lantek')
    imposta(oid, taglio_completato=True, data_taglio_completato=datetime.utcnow())
    o = {x['id']: x for x in c.get('/api/officina/tablet', headers=REP).get_json()['ordini']}[oid]
    check('tagliato: 16 pezzi su 16', o['taglio']['tagliati'] == 16 and o['taglio']['totale'] == 16, o['taglio'])

    print('\n5. Segnala un problema')
    url = f'/api/orders/{oid}/segnalazioni'

    def notifiche():
        s = models.SessionLocal()
        try:
            # solo le nostre: accettando il preventivo l'ufficio riceve gia' "Nuovo ordine"
            return s.query(Notification).filter(Notification.order_id == oid,
                                                Notification.title.like('Officina:%')).all()
        finally:
            s.close()
    check('senza niente: 401', anon.post(url, json={'tipo': 'manca_materiale'}).status_code == 401)
    check('tablet delle ore: 403', C(tok_ore).post(url, json={'tipo': 'manca_materiale'},
                                                   headers={'X-Device-Token': tok_ore}).status_code == 403)
    r = c.post(url, json={'tipo': 'boh'}, headers=REP)
    check('tipo non valido: 400', r.status_code == 400 and r.get_json().get('codice') == 'tipo_non_valido')
    check('nessun corpo: 400', c.post(url, data='x', headers=REP).status_code == 400)
    check('"Altro" senza nota: 400', c.post(url, json={'tipo': 'altro'}, headers=REP).status_code == 400)
    check('nota troppo lunga: 400', c.post(url, json={'tipo': 'disegno', 'nota': 'x' * 301}, headers=REP).status_code == 400)
    check('pezzo di un altro ordine: 400',
          c.post(url, json={'tipo': 'da_rifare', 'codice_pezzo': 'ZZZ'}, headers=REP).status_code == 400)
    r = c.post('/api/orders/non-esiste/segnalazioni', json={'tipo': 'manca_materiale'}, headers=REP)
    check('ordine inesistente: 404', r.status_code == 404)
    check('nessuna notifica per le richieste sbagliate', notifiche() == [])

    r = c.post(url, json={'tipo': 'manca_materiale', 'codice_pezzo': 'A1', 'nota': 'finito il | 3 mm'}, headers=REP)
    j = r.get_json()
    check('segnalazione valida: 201', r.status_code == 201 and j.get('success') and j.get('gia_segnalato') is False, (r.status_code, j))
    nn = notifiche()
    check('una notifica per ogni utenza d\'ufficio attiva', sorted(n.user_id for n in nn) == ['uff1', 'uff2'],
          [n.user_id for n in nn])
    n = nn[0] if nn else None
    check('titolo chiaro', n is not None and n.title == 'Officina: manca materiale', n and n.title)
    check('messaggio con numero, cliente, problema, pezzo, nota e chi',
          n is not None and all(x in n.message for x in ('#1252', 'DECA', 'manca materiale', 'pezzo A1', 'finito il / 3 mm', 'Tablet officina 1')),
          n and n.message)
    check('da fare (categoria attiva)', n is not None and n.notification_category == 'attiva')
    s = models.SessionLocal()
    try:
        au = s.query(AuditLog).filter(AuditLog.action == 'SEGNALAZIONE_OFFICINA', AuditLog.entity_id == oid).all()
    finally:
        s.close()
    check('riga d\'audit', len(au) == 1 and au[0].user_name == 'Tablet officina 1' and 'pezzo: A1' in (au[0].detail or ''),
          [(a.user_name, a.detail) for a in au])

    r = c.post(url, json={'tipo': 'manca_materiale', 'codice_pezzo': 'A1'}, headers=REP)
    check('stesso problema entro 10 minuti: gia\' segnalato', r.status_code == 200 and r.get_json().get('gia_segnalato') is True,
          r.get_json())
    check('e nessuna notifica in piu\'', len(notifiche()) == 2)
    r = c.post(url, json={'tipo': 'manca_materiale', 'codice_pezzo': 'B1'}, headers=REP)
    check('stesso problema su un altro pezzo: nuova segnalazione', r.status_code == 201 and len(notifiche()) == 4)
    r = c.post(url, json={'tipo': 'disegno', 'da': 'Luca'}, headers=REP)
    check('col nome di chi segnala, senza pezzo', r.status_code == 201, r.get_json())
    check('col nome di chi segnala', any('Segnalato da Luca' in x.message for x in notifiche()))
    # dopo 10 minuti lo stesso problema si puo' segnalare di nuovo
    s = models.SessionLocal()
    try:
        for a in s.query(AuditLog).filter(AuditLog.action == 'SEGNALAZIONE_OFFICINA').all():
            a.timestamp = a.timestamp - timedelta(minutes=11)
        s.commit()
    finally:
        s.close()
    r = c.post(url, json={'tipo': 'manca_materiale', 'codice_pezzo': 'A1'}, headers=REP)
    check('dopo 10 minuti si segnala di nuovo', r.status_code == 201 and r.get_json().get('gia_segnalato') is False)

    o = {x['id']: x for x in c.get('/api/officina/tablet', headers=REP).get_json()['ordini']}[oid]
    so = o.get('segnalazioni_oggi') or []
    check('le segnalazioni di oggi sul tablet', len(so) == 4 and so[0]['tipo'] == 'manca_materiale'
          and so[0]['codice_pezzo'] == 'A1', so[:1])
    check('con la nota leggibile', any(x['nota'] == 'finito il / 3 mm' for x in so), [x['nota'] for x in so])
    senza = next((x for x in so if x['tipo'] == 'disegno'), {})
    check('senza nota: vuota, e con chi l\'ha segnalata', senza.get('nota') == '' and senza.get('da') == 'Luca', senza)

    print('\n6. Gli endpoint di prima non cambiano')
    dist = c.get(f'/api/orders/{oid}/distinta').get_json()
    check('distinta: stesse chiavi', set(dist) == {'success', 'origine', 'righe', 'totale_pezzi', 'preventivo_id'}, sorted(dist))
    check('distinta: stessi valori', dist['origine'] == 'preventivo' and dist['totale_pezzi'] == 18
          and {r['codice'] for r in dist['righe']} >= {'A1', 'B1', 'C1', 'ASS1'}, dist['totale_pezzi'])
    a1d = next(r for r in dist['righe'] if r['codice'] == 'A1')
    check('distinta: lavorazioni come prima', a1d['lavorazioni'] == ['Taglio laser', 'Piegatura (3 pieghe)', 'Saldatura', 'Filettatura (4)'],
          a1d['lavorazioni'])
    dis = c.get(f'/api/orders/{oid}/disegni').get_json()
    check('disegni: come prima', dis['success'] and len(dis['disegni']) == 2 and 'url_zip' in dis)
    # La lista ordini e il preventivatore non sono cose del tablet: ora li
    # chiede l'ufficio (prima erano aperti a chiunque).
    check('lista ordini: il tablet non la legge', c.get('/api/orders?aperti=1').status_code == 403)
    lst = C(tok_uff).get('/api/orders?aperti=1').get_json()['orders']
    check("lista ordini: l'ufficio si", any(x['id'] == oid and x['numero_ordine_cliente'] == '1252' for x in lst))
    comm = entra(A.app, 'commerciale', pin=PIN_UFFICIO)
    r = comm.post(f'/api/preventivi/{pid}/dxf/nessuno.dxf/fold-model', json={})
    check('piegato 3D del preventivo: risponde come prima', r.status_code == 404 and r.get_json().get('success') is False)
    r = c.get(f'/api/orders/{oid}/dxf/nessuno.dxf/fold-model')
    check('piegato 3D dell\'ordine: disegno estraneo 404', r.status_code == 404)
    r = c.get('/api/orders/non-esiste/dxf/A1.dxf/fold-model')
    check('piegato 3D dell\'ordine: ordine inesistente 404', r.status_code == 404)
    print(f'\n  (tablet: {ms:.0f} ms per 2 ordini)')


try:
    main()
except Exception as e:  # un crash e' un fallimento, non un test saltato
    import traceback
    traceback.print_exc()
    KO.append(f'eccezione: {e}')
finally:
    try:
        _ENG.dispose()
        os.remove(_TMP)
    except OSError:
        pass
    shutil.rmtree(_CARTELLE, ignore_errors=True)

print(f'\nPASSATI: {OK}   FALLITI: {len(KO)}')
for k in KO:
    print('  -', k)
sys.exit(0 if not KO else 1)
