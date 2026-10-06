"""Test dell'ACCESSO VERO: dispositivi registrati, PIN negli uffici, amministratori.

Il buco che chiude: prima l'identita' la dichiarava la pagina (X-User-Id,
user_id nel corpo) e chiunque sulla rete diventava l'amministrazione. Ora il
server crede solo al dispositivo registrato (cookie HttpOnly) e, negli
uffici, al PIN della persona.

Copre:
 1. PIN: impronta scrypt con sale, confronto, PIN troppo facili rifiutati
 2. blocco dopo 5 PIN sbagliati (per dispositivo) e limite su tutti i dispositivi
 3. la sessione d'ufficio dura fino alle 3 di notte (orologio simulato, anche
    col cambio dell'ora)
 4. registrare un dispositivo richiede il PIN di un amministratore
 5. il PIN di una persona non amministratore non vale per le operazioni da
    amministratore
 6. ogni stazione puo' / non puo' (tabella sulle rotte rappresentative)
 7. X-User-Id, user_id, admin_id, created_by non cambiano niente
 8. ogni rotta /api ha una regola (elenco pubblico dichiarato)
 9. avvisi: ognuno vede e tocca solo i suoi
10. transizione e protetto
11. Esci chiude la sessione
12. revoca di un dispositivo e persona disattivata: effetto immediato
13. /api/upload-drawing non c'e' piu'
14. CORS: "http://192.168.1.10.evil.example" non e' un'origine ammessa
15. vecchio token nell'intestazione: funziona e diventa cookie
16. prezzi nascosti fuori dagli uffici, cartella disegni solo dentro le
    cartelle consentite, ultimo amministratore protetto, primo amministratore

Gira su DATABASE TEMPORANEO e impostazioni temporanee.
Esecuzione: python app/tests/test_accesso.py
"""
import json
import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
_CARTELLE = tempfile.mkdtemp(prefix='ft_accesso_')
_CFG = os.path.join(_CARTELLE, 'app_config.json')
with open(_CFG, 'w', encoding='utf-8') as f:
    json.dump({'accesso': {'modalita': 'protetto'}}, f)
os.environ['FERROTRACK_CONFIG'] = _CFG          # mai le impostazioni vere

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Order, Notification  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(_CARTELLE, 'test_accesso.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
APPMOD = importlib.import_module('backend.app')
app = APPMOD.app
from backend import accesso as A  # noqa: E402
from backend.database import ConfigManager, AuditManager  # noqa: E402
from backend import orario  # noqa: E402
from tests.accesso_aiuto import (entra, eleva, persona, postazioni, dispositivo,  # noqa: E402
                                 PIN_ADMIN, PIN_UFFICIO)

ConfigManager._CONFIG_PATH = _CFG
APPMOD.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
APPMOD.DRAWINGS_FOLDER = os.path.join(APPMOD.UPLOAD_FOLDER, 'drawings')
os.makedirs(APPMOD.DRAWINGS_FOLDER, exist_ok=True)

# Una rotta "dimenticata" senza regola: deve restare chiusa. Si registra
# qui, prima della prima richiesta (dopo Flask non lo permette).
@app.route('/api/prova-senza-regola')
def _prova_senza_regola():
    return 'aperta'


OK = 0
KO = []
_ADESSO_VERO = A.adesso


def check(nome, cond, extra=''):
    global OK
    if cond:
        OK += 1
        print(f'  [OK] {nome}')
    else:
        KO.append(nome)
        print(f'  [KO] {nome} {extra}')


def orologio(dt=None):
    A.adesso = (lambda: dt) if dt else _ADESSO_VERO


def pulisci_tentativi():
    s = models.SessionLocal()
    try:
        s.query(models_ore.TentativoPin).delete()
        s.commit()
    finally:
        s.close()


def setup():
    postazioni()
    persona('elena', 'Elena Rossi', 'Amministrazione', pin=PIN_ADMIN)          # admin
    persona('mario', 'Mario Laser', 'Laser', pin='4826')                       # admin
    persona('luca', 'Luca Commerciale', 'Commerciale', pin=PIN_UFFICIO)        # NON admin
    persona('enzo', 'Enzo Operaio', 'Operaio')                                 # senza PIN
    s = models.SessionLocal()
    try:
        now = datetime.utcnow()
        s.add(Order(id='o1', numero_ordine='A-1', status='RICEVUTO', cliente='Cliente Alfa',
                    data_ricezione=now, data_consegna=now + timedelta(days=7), prezzo_quotato=1234.5))
        s.commit()
    finally:
        s.close()


# Rotte rappresentative: (metodo, indirizzo, corpo, stazioni che possono)
TABELLA = [
    ('GET', '/api/orders', None, {'commerciale', 'ufficio', 'laser'}),
    ('POST', '/api/orders', {'numero_ordine': '', 'cliente': ''}, {'ufficio'}),
    ('POST', '/api/orders/o1/close', {}, {'ufficio', 'laser'}),
    ('POST', '/api/orders/o1/mark-laser-done', {}, {'laser'}),
    ('GET', '/api/officina/tablet', None, {'reparto', 'ufficio', 'laser'}),
    ('GET', '/api/orders/o1/distinta', None, {'commerciale', 'ufficio', 'laser', 'reparto'}),
    ('GET', '/api/preventivi', None, {'commerciale', 'ufficio'}),
    ('POST', '/api/preventivi', {'cliente': ''}, {'commerciale'}),
    ('GET', '/api/preventivi/config', None, {'commerciale', 'ufficio'}),
    ('GET', '/api/archive/orders', None, {'commerciale', 'ufficio'}),
    ('GET', '/api/ordini/viste', None, {'ufficio'}),
    ('GET', '/api/ordini-da-fatturare', None, {'ufficio'}),
    ('GET', '/api/capo/kpi-operai', None, {'ufficio'}),
    ('GET', '/api/admin/kpi', None, {'ufficio'}),
    ('GET', '/api/users', None, {'ufficio'}),
    ('GET', '/api/laser/banco', None, {'laser', 'ufficio'}),
    ('GET', '/api/ore/operai', None, {'ore', 'ufficio'}),
    ('GET', '/api/ore/riepilogo', None, {'ufficio'}),
    # le persone le aggiunge solo l'ufficio (non il tablet)
    ('POST', '/api/ore/operai', {'nome': ''}, {'ufficio'}),
    ('GET', '/api/ore/giornata/storico', None, {'ufficio'}),
    ('GET', '/api/notifications', None, {'commerciale', 'ufficio', 'laser'}),
    ('PUT', '/api/admin/config', {}, set()),
    ('GET', '/api/admin/persone', None, set()),
    ('POST', '/api/users', {}, set()),
    ('GET', '/api/admin/dispositivi', None, set()),
    ('PUT', '/api/preventivi/config', {}, set()),
    ('POST', '/api/notifications', {'user_id': 'postazione-laser', 'title': 'x'}, set()),
]

# Rotte senza controllo, dichiarate e motivate
PUBBLICHE_ATTESE = {
    '/api/health',                 # salute del server, nessun dato
    '/api/dashboard-live',         # schermo TV: numeri anonimi, nessun nome ne' prezzo
    '/api/auth/login',             # vecchio ingresso, risponde solo in transizione
    '/api/auth/logout',            # come /api/accesso/esci
    '/api/accesso/io', '/api/accesso/registra', '/api/accesso/usa-codice',
    '/api/accesso/entra', '/api/accesso/esci', '/api/accesso/admin',
    '/api/accesso/primo-admin',    # solo dal PC del server e solo senza amministratori
}


def chiama(c, metodo, url, corpo=None, **kw):
    f = getattr(c, metodo.lower())
    return f(url, json=corpo, **kw) if corpo is not None else f(url, **kw)


def main():
    setup()

    print('\n1) PIN: impronta e regole')
    h1, h2 = A.impronta_pin('2580'), A.impronta_pin('2580')
    check('impronta scrypt con sale diverso ogni volta', h1.startswith('scrypt$') and h1 != h2)
    check('il PIN giusto corrisponde, quello sbagliato no',
          A.pin_corrisponde('2580', h1) and not A.pin_corrisponde('2581', h1))
    check('impronta rovinata: nessuna corrispondenza (niente eccezioni)', not A.pin_corrisponde('2580', 'xx$y'))
    for cattivo in ('123', '123456789', 'abcd', '1111', '1234', '9876', ''):
        check(f'PIN {cattivo!r} rifiutato', bool(A.errore_formato_pin(cattivo)))
    check('PIN 2580 accettato', A.errore_formato_pin('2580') == '')
    s = models.SessionLocal()
    try:
        check('nel database c\'e\' solo l\'impronta', s.get(User, 'elena').pin_hash.startswith('scrypt$'))
    finally:
        s.close()

    print('\n2) Blocco dopo 5 PIN sbagliati')
    pulisci_tentativi()
    c = entra(app, 'ufficio')
    codici = [c.post('/api/accesso/entra', json={'pin': '0000'}).status_code for _ in range(5)]
    check('5 PIN sbagliati: 403 ogni volta, poi blocco', codici[:4] == [403] * 4 and codici[4] == 429, codici)
    r = c.post('/api/accesso/entra', json={'pin': PIN_ADMIN})
    check('bloccato: anche il PIN giusto aspetta (429, messaggio in italiano)',
          r.status_code == 429 and 'Riprova fra' in r.get_json()['error'], (r.status_code, r.get_json()))
    altro = entra(app, 'ufficio')
    r = altro.post('/api/accesso/entra', json={'pin': PIN_ADMIN})
    check('un altro dispositivo non e\' bloccato', r.status_code == 200, r.status_code)
    orologio(datetime.utcnow() + timedelta(minutes=6))
    r = c.post('/api/accesso/entra', json={'pin': PIN_ADMIN})
    check('dopo 5 minuti si rientra', r.status_code == 200, (r.status_code, r.get_json()))
    orologio()
    pulisci_tentativi()
    for i in range(A.MAX_ERRORI_TOTALI):
        x = entra(app, 'commerciale')
        x.post('/api/accesso/entra', json={'pin': '0000'})
    r = entra(app, 'commerciale').post('/api/accesso/entra', json={'pin': PIN_UFFICIO})
    check('troppi errori da piu\' dispositivi: pausa per tutti', r.status_code == 429, r.status_code)
    pulisci_tentativi()
    r = c.post('/api/accesso/entra', json={'pin': '0000'})
    check('errore normale: dice quanti tentativi restano solo alla fine',
          r.status_code == 403 and 'tentativi_rimasti' in r.get_json(), r.get_json())
    pulisci_tentativi()

    print('\n3) La giornata finisce alle 3 di notte')
    def utc(locale):
        return orario.locale_a_utc(locale)
    check('alle 10 la sessione scade alle 3 di domani',
          A.fine_giornata(utc(datetime(2026, 10, 5, 10, 0))) == utc(datetime(2026, 10, 6, 3, 0)))
    check('alle 2 di notte scade alle 3 dello stesso giorno',
          A.fine_giornata(utc(datetime(2026, 10, 6, 2, 0))) == utc(datetime(2026, 10, 6, 3, 0)))
    check('col cambio dell\'ora (25/10) scade alle 3 locali, cioe\' alle 2 UTC',
          A.fine_giornata(utc(datetime(2026, 10, 24, 18, 0))) == datetime(2026, 10, 25, 2, 0))
    c = entra(app, 'ufficio', pin=PIN_ADMIN)
    scade = A.fine_giornata()
    r = c.get('/api/ordini/viste')
    check('appena entrati: l\'Ufficio lavora', r.status_code == 200, r.status_code)
    orologio(scade - timedelta(minutes=1))
    check('alle 2:59 il PIN non si richiede', c.get('/api/ordini/viste').status_code == 200)
    orologio(scade + timedelta(minutes=1))
    r = c.get('/api/ordini/viste')
    check('alle 3:01 serve di nuovo il PIN (401 serve_pin)',
          r.status_code == 401 and r.get_json().get('codice') == 'serve_pin', (r.status_code, r.get_json()))
    orologio()

    print('\n4) Registrare un dispositivo vuole il PIN di un amministratore')
    c = app.test_client()
    io = c.get('/api/accesso/io').get_json()
    check('dispositivo nuovo: non registrato, elenco delle 5 stazioni',
          io['registrato'] is False and len(io['stazioni']) == 5, io)
    r = c.post('/api/accesso/registra', json={'stazione': 'laser'})
    check('senza PIN: rifiutato', r.status_code == 403, r.status_code)
    r = c.post('/api/accesso/registra', json={'stazione': 'laser', 'pin': PIN_UFFICIO})
    check('col PIN di una persona NON amministratore: rifiutato', r.status_code == 403, r.status_code)
    r = c.post('/api/accesso/registra', json={'stazione': 'laser', 'pin': '4826'})
    ck = r.headers.getlist('Set-Cookie')
    check('col PIN dell\'amministratore del laser: registrato, cookie HttpOnly SameSite=Strict',
          r.status_code == 200 and any(x.startswith('ft_disp=') and 'HttpOnly' in x and 'SameSite=Strict' in x
                                       for x in ck), (r.status_code, ck))
    io = c.get('/api/accesso/io').get_json()
    check('poi apre sempre il laser', io['registrato'] and io['stazione'] == 'laser' and io['pagina'] == '/laser.html', io)
    check('il segreto non e\' leggibile dalla risposta', 'segreto' not in json.dumps(r.get_json()))
    r = c.post('/api/accesso/registra', json={'stazione': 'ufficio', 'pin': PIN_ADMIN})
    io = c.get('/api/accesso/io').get_json()
    check('"cambia" col PIN admin: stesso dispositivo, ora Ufficio, chiede il PIN',
          r.status_code == 200 and io['stazione'] == 'ufficio' and io['serve_pin'], io)
    s = models.SessionLocal()
    try:
        righe = s.query(AuditLog_()).filter_by(action='DISPOSITIVO_CAMBIATO').all()
        check('il registro attivita\' dice CHI (la persona vera)', righe and righe[-1].user_name == 'Elena Rossi',
              [(x.user_id, x.user_name) for x in righe])
    finally:
        s.close()
    pulisci_tentativi()

    print('\n5) Operazioni da amministratore')
    c = entra(app, 'ufficio', pin=PIN_UFFICIO)          # Luca, non admin, dentro
    r = c.get('/api/admin/persone')
    check('persona entrata ma non admin: 403 serve_pin_admin',
          r.status_code == 403 and r.get_json().get('codice') == 'serve_pin_admin', r.get_json())
    r = c.post('/api/accesso/admin', json={'pin': PIN_UFFICIO})
    check('il suo PIN non vale come PIN di amministratore', r.status_code == 403, r.status_code)
    eleva(c, PIN_ADMIN)
    r = c.get('/api/admin/persone')
    check('col PIN di Elena: elenco persone (senza impronte)',
          r.status_code == 200 and 'pin_hash' not in json.dumps(r.get_json()), r.status_code)
    lab = entra(app, 'laser')
    check('al laser senza PIN admin: niente impostazioni',
          lab.put('/api/admin/config', json={'sospetto_giorni_apertura': 5}).status_code == 403)
    eleva(lab, '4826')
    r = lab.put('/api/admin/config', json={'sospetto_giorni_apertura': 7})
    check('al laser col PIN dell\'amministratore: impostazioni salvate', r.status_code == 200, r.get_json())
    orologio(datetime.utcnow() + A.DURATA_ADMIN + timedelta(minutes=1))
    check('il PIN admin vale solo qualche minuto',
          lab.put('/api/admin/config', json={'sospetto_giorni_apertura': 8}).status_code == 403)
    orologio()
    tab = entra(app, 'reparto')
    tab.set_cookie(A.COOKIE_ADMIN, lab.get_cookie(A.COOKIE_ADMIN).value)
    check('il cookie admin copiato su un altro dispositivo non vale',
          tab.get('/api/admin/persone').status_code == 403)
    pulisci_tentativi()

    print('\n6) Cosa puo\' fare ogni stazione')
    clienti = {st: entra(app, st, pin=(PIN_ADMIN if A.STAZIONI[st]['con_pin'] else None))
               for st in A.STAZIONI}
    for metodo, url, corpo, ammesse in TABELLA:
        esiti = {}
        for st, cl in clienti.items():
            esiti[st] = chiama(cl, metodo, url, corpo).status_code
        giusti = all((esiti[st] not in (401, 403)) == (st in ammesse) for st in esiti)
        check(f'{metodo} {url}: solo {sorted(ammesse) or "amministratore"}', giusti, esiti)
    r = clienti['laser'].get('/api/orders')
    o = [x for x in r.get_json()['orders'] if x['id'] == 'o1'][0]
    check('il laser vede l\'ordine ma non il prezzo', 'prezzo_quotato' not in o, list(o)[:8])
    r = clienti['ufficio'].get('/api/orders')
    o = [x for x in r.get_json()['orders'] if x['id'] == 'o1'][0]
    check('l\'Ufficio vede il prezzo', o.get('prezzo_quotato') == 1234.5)
    check('il tablet non chiude ordini', clienti['reparto'].post('/api/orders/o1/close', json={}).status_code == 403)
    anon = app.test_client()
    r = anon.get('/api/users')
    check('elenco utenti: anonimo 401 (prima era aperto)', r.status_code == 401, r.status_code)
    r = anon.get('/api/orders')
    check('ordini da un browser non registrato: 401 non_registrato',
          r.status_code == 401 and r.get_json().get('codice') == 'non_registrato', r.status_code)

    print('\n7) X-User-Id / user_id / admin_id non contano piu\'')
    r = anon.get('/api/orders', headers={'X-User-Id': 'postazione-amministrazione'})
    check('protetto: X-User-Id di una postazione da browser non registrato -> 401', r.status_code == 401)
    r = clienti['reparto'].post('/api/orders/o1/close', json={'user_id': 'postazione-amministrazione'},
                                headers={'X-User-Id': 'postazione-amministrazione'})
    check('tablet che si dichiara amministrazione: 403', r.status_code == 403, r.status_code)
    r = clienti['laser'].put('/api/admin/config', json={'admin_id': 'elena', 'sospetto_giorni_apertura': 3},
                             headers={'X-User-Id': 'elena'})
    check('laser con admin_id/X-User-Id di Elena: niente impostazioni', r.status_code == 403, r.status_code)
    r = clienti['laser'].get('/api/notifications?user_id=postazione-amministrazione')
    check('user_id nell\'indirizzo degli avvisi: ignorato', r.status_code == 200)
    luca = entra(app, 'commerciale', pin=PIN_UFFICIO)
    r = luca.post('/api/preventivi', json={'cliente': 'Cliente Beta', 'created_by': 'elena'})
    pid = (r.get_json() or {}).get('preventivo', {}).get('id') or (r.get_json() or {}).get('id')
    s = models.SessionLocal()
    try:
        from backend.models import Preventivo
        p = s.get(Preventivo, pid) if pid else None
        check('created_by dal corpo ignorato: il preventivo e\' di chi ha il PIN (luca)',
              p is not None and p.created_by == 'luca', (r.status_code, r.get_json(), getattr(p, 'created_by', None)))
    finally:
        s.close()

    print('\n8) Ogni rotta /api ha una regola')
    senza, pubbliche = [], set()
    for regola in app.url_map.iter_rules():
        if not regola.rule.startswith('/api/'):
            continue
        view = app.view_functions[regola.endpoint]
        for m in sorted(regola.methods - {'HEAD', 'OPTIONS'}):
            reg = A.regola_per(view, m)
            if reg is None:
                if regola.rule != '/api/prova-senza-regola':
                    senza.append(f'{m} {regola.rule}')
            elif reg.pubblico:
                pubbliche.add(regola.rule)
    check('nessuna rotta /api senza regola', not senza, senza)
    check('le rotte pubbliche sono solo quelle dichiarate', pubbliche == PUBBLICHE_ATTESE,
          sorted(pubbliche ^ PUBBLICHE_ATTESE))
    # e una rotta aggiunta senza regola resta chiusa (registrata all'avvio)
    r = clienti['ufficio'].get('/api/prova-senza-regola')
    check('una rotta dimenticata senza regola e\' chiusa (403)', r.status_code == 403, r.status_code)

    print('\n9) Avvisi: ognuno i suoi')
    s = models.SessionLocal()
    try:
        for nid, uid in (('n-uff', 'postazione-amministrazione'), ('n-las', 'postazione-laser')):
            s.add(Notification(id=nid, user_id=uid, title='t', message='m', is_read=False, is_deleted=False))
        s.commit()
    finally:
        s.close()
    lista = clienti['laser'].get('/api/notifications').get_json()['data']['notifications']
    check('il laser vede solo i suoi', [n['id'] for n in lista] == ['n-las'], [n['id'] for n in lista])
    r = clienti['laser'].put('/api/notifications/n-uff/read')
    check('non segna letti quelli dell\'Ufficio', r.status_code == 404, r.status_code)
    r = clienti['laser'].delete('/api/notifications/n-uff')
    check('non cancella quelli dell\'Ufficio', r.status_code == 404, r.status_code)
    clienti['laser'].delete('/api/notifications/clear-all?user_id=postazione-amministrazione')
    lista = clienti['ufficio'].get('/api/notifications').get_json()['data']['notifications']
    check('"cancella tutti" del laser non tocca l\'Ufficio', 'n-uff' in [n['id'] for n in lista],
          [(n['id'], n.get('title')) for n in lista])
    r = clienti['ufficio'].post('/api/notifications', json={'user_id': 'postazione-laser', 'title': 'x', 'message': 'y'})
    check('creare avvisi a mano: solo amministratore', r.status_code == 403, r.status_code)

    print('\n10) Transizione e protetto')
    A.modalita = lambda: 'transizione'
    r = anon.get('/api/ordini/viste', headers={'X-User-Id': 'postazione-amministrazione'})
    check('transizione: dispositivo non registrato col vecchio modo lavora', r.status_code == 200, r.status_code)
    io = anon.get('/api/accesso/io', headers={'X-User-Id': 'postazione-amministrazione'}).get_json()
    check('transizione: la pagina sa che e\' il vecchio modo (fascia gialla)', io['vecchio'] and not io['registrato'], io)
    r = anon.get('/api/ordini/viste', headers={'X-User-Id': 'elena'})
    check('transizione: X-User-Id di una PERSONA non vale', r.status_code == 401, r.status_code)
    vecchio = app.test_client()
    vecchio.set_cookie(A.COOKIE_VECCHIO, 'postazione-laser')
    r = vecchio.get('/api/orders/o1/distinta')
    check('transizione: la vecchia postazione vale anche dal cookie (PDF e disegni in una scheda nuova)',
          r.status_code == 200, r.status_code)
    r = anon.get('/api/admin/persone', headers={'X-User-Id': 'postazione-laser'})
    check('transizione: le operazioni da amministratore vogliono gia\' il PIN', r.status_code == 403, r.status_code)
    r = anon.get('/api/ore/riepilogo', headers={'X-User-Id': 'postazione-amministrazione'})
    check('transizione: le ore vogliono comunque un dispositivo', r.status_code == 401, r.status_code)
    r = anon.post('/api/auth/login', json={'user_id': 'postazione-laser'})
    check('transizione: vecchio ingresso per postazione', r.status_code == 200, r.status_code)
    r = anon.post('/api/auth/login', json={'user_id': 'elena'})
    check('transizione: il vecchio ingresso non fa entrare una persona', r.status_code == 404, r.status_code)
    reg = entra(app, 'ufficio')
    r = reg.get('/api/ordini/viste', headers={'X-User-Id': 'postazione-amministrazione'})
    check('transizione: un dispositivo REGISTRATO vuole il PIN (niente vecchio modo)', r.status_code == 401, r.status_code)
    A.modalita = lambda: 'protetto'
    r = anon.post('/api/auth/login', json={'user_id': 'postazione-laser'})
    check('protetto: il vecchio ingresso e\' rifiutato', r.status_code == 403, r.status_code)
    r = anon.get('/api/ordini/viste', headers={'X-User-Id': 'postazione-amministrazione'})
    check('protetto: il vecchio modo e\' rifiutato', r.status_code == 401, r.status_code)
    vecchio = app.test_client()
    vecchio.set_cookie(A.COOKIE_VECCHIO, 'postazione-laser')
    check('protetto: anche dal cookie', vecchio.get('/api/orders/o1/distinta').status_code == 401)
    A.modalita = _modalita_vera     # torna la funzione vera (legge il file temporaneo)
    adm = entra(app, 'ufficio', pin=PIN_ADMIN)
    eleva(adm)
    r = adm.put('/api/admin/accesso', json={'modalita': 'transizione'})
    with open(_CFG, encoding='utf-8') as f:
        salvato = json.load(f)
    check('l\'amministratore passa la modalita\' (scritta in app_config.json)',
          r.status_code == 200 and salvato['accesso']['modalita'] == 'transizione', (r.status_code, salvato))
    r = adm.put('/api/admin/config', json={'accesso': {'modalita': 'transizione'}})
    check('dalle impostazioni generali la modalita\' non si tocca',
          '_ignorati' in (r.get_json().get('config') or {}), r.get_json())
    adm.put('/api/admin/accesso', json={'modalita': 'protetto'})
    pulisci_tentativi()

    print('\n11) Esci')
    c = entra(app, 'ufficio', pin=PIN_ADMIN)
    check('dentro', c.get('/api/ordini/viste').status_code == 200)
    r = c.post('/api/accesso/esci')
    check('Esci: 200', r.status_code == 200)
    r = c.get('/api/ordini/viste')
    check('dopo Esci serve il PIN', r.status_code == 401 and r.get_json()['codice'] == 'serve_pin', r.status_code)
    check('il dispositivo resta registrato', c.get('/api/accesso/io').get_json()['stazione'] == 'ufficio')

    print('\n12) Revoca e persona disattivata: subito')
    c = entra(app, 'ufficio', pin=PIN_ADMIN)
    adm = entra(app, 'ufficio', pin=PIN_ADMIN)
    eleva(adm)
    r = adm.delete(f'/api/admin/dispositivi/{c.dispositivo_id}')
    check('revoca: 200', r.status_code == 200, r.status_code)
    r = c.get('/api/ordini/viste')
    check('il dispositivo revocato non entra piu\' (401)', r.status_code == 401, r.status_code)
    persona('anna', 'Anna Prova', 'Amministrazione', pin='5049', admin=False)
    c = entra(app, 'ufficio', pin='5049')
    r = adm.put('/api/admin/persone/anna', json={'attivo': False})
    check('disattivare una persona: 200', r.status_code == 200, r.get_json())
    check('la sua sessione si chiude subito', c.get('/api/ordini/viste').status_code == 401)
    c2 = entra(app, 'commerciale', pin=PIN_UFFICIO)
    adm.put('/api/admin/persone/luca/pin', json={'pin': '6170'})
    check('PIN rimesso dall\'amministratore: le sessioni col vecchio PIN si chiudono',
          c2.get('/api/preventivi').status_code == 401)
    r = adm.put('/api/admin/persone/luca/pin', json={'pin': '4826'})
    check('un PIN gia\' usato da un altro non si puo\' dare', r.status_code == 409, r.status_code)
    r = c2.post('/api/accesso/entra', json={'pin': '6170'})
    check('col PIN nuovo si rientra', r.status_code == 200, r.status_code)

    print('\n13) /api/upload-drawing tolta')
    r = clienti['ufficio'].post('/api/upload-drawing', data={'order_id': '../../x'})
    check('upload-drawing: 404', r.status_code == 404, r.status_code)

    print('\n14) CORS')
    r = clienti['ufficio'].get('/api/health', headers={'Origin': 'http://192.168.1.10.evil.example'})
    check('origine "192.168.1.10.evil.example" non riflessa',
          r.headers.get('Access-Control-Allow-Origin') is None, r.headers.get('Access-Control-Allow-Origin'))
    r = clienti['ufficio'].get('/api/health', headers={'Origin': 'http://192.168.1.10:5000'})
    check('origine della rete locale ammessa', r.headers.get('Access-Control-Allow-Origin') == 'http://192.168.1.10:5000')
    check('nessun permesso di mandare credenziali da altre origini',
          r.headers.get('Access-Control-Allow-Credentials') is None)

    print('\n15) Vecchio token nell\'intestazione')
    seg, _did = dispositivo('ore', 'Timbratrice vecchia')
    vecchio = app.test_client()
    r = vecchio.get('/api/ore/operai', headers={'X-Device-Token': seg})
    check('il tablet configurato col vecchio token funziona', r.status_code == 200, r.status_code)
    check('e da li\' in poi il token sta nel cookie',
          any(x.startswith('ft_disp=') and 'HttpOnly' in x for x in r.headers.getlist('Set-Cookie')))
    check('anche senza intestazione', vecchio.get('/api/ore/operai').status_code == 200)
    r = vecchio.get('/api/ordini/viste', headers={'X-Device-Token': seg})
    check('la timbratrice resta timbratrice', r.status_code == 403, r.status_code)

    print('\n16) Cartella dei disegni, ultimo amministratore, primo amministratore')
    adm = entra(app, 'ufficio', pin=PIN_ADMIN)
    eleva(adm)
    radice = os.path.join(_CARTELLE, 'commesse')
    r = adm.put('/api/laser/cartella-disegni', json={'percorso': radice})
    check('nessuna cartella consentita: rifiutata', r.status_code == 400 and r.get_json()['codice'] == 'non_consentita',
          r.get_json())
    ConfigManager.save_riservata({'cartelle_disegni_consentite': [radice]})
    r = adm.put('/api/laser/cartella-disegni', json={'percorso': os.path.join(radice, 'lantek')})
    check('dentro la cartella consentita: salvata', r.status_code == 200, r.get_json())
    for fuori in ('\\\\altro-pc\\condivisa', 'C:\\Windows\\Temp\\ft', os.path.join(_CARTELLE, 'commesse2')):
        r = adm.put('/api/laser/cartella-disegni', json={'percorso': fuori})
        check(f'fuori ({fuori}): rifiutata', r.status_code == 400, r.status_code)
    r = adm.put('/api/admin/config', json={'disegni_export_root': 'C:\\Windows'})
    check('nemmeno dalle impostazioni generali', '_ignorati' in (r.get_json().get('config') or {}))
    r = adm.put('/api/admin/config', json={'cartelle_disegni_consentite': ['C:\\']})
    check('l\'elenco delle cartelle consentite non si cambia dalle pagine',
          ConfigManager.load_config()['cartelle_disegni_consentite'] == [radice])
    r = entra(app, 'laser').put('/api/laser/cartella-disegni', json={'percorso': radice})
    check('dal laser senza PIN admin: no', r.status_code == 403)
    # ultimo amministratore
    adm.put('/api/admin/persone/mario', json={'admin': False})
    r = adm.put('/api/admin/persone/elena', json={'admin': False})
    check('non si toglie l\'ultimo amministratore', r.status_code == 409, r.status_code)
    r = adm.delete('/api/users/elena')
    check('nemmeno dalla vecchia rotta', r.status_code == 409, r.status_code)
    r = adm.get('/api/admin/persone').get_json()
    check('la pagina sa quanti amministratori ci sono (avviso se uno solo)', r['amministratori'] == 1, r['amministratori'])
    adm.put('/api/admin/persone/mario', json={'admin': True})
    r = app.test_client().post('/api/accesso/primo-admin', json={'nome': 'Primo Admin', 'pin': '3816'})
    check('primo amministratore: rifiutato se ce n\'e\' gia\' uno', r.status_code in (403, 409), r.status_code)
    r = app.test_client().post('/api/accesso/primo-admin', json={'nome': 'Primo Admin', 'pin': '3816'},
                               environ_base={'REMOTE_ADDR': '192.168.1.50'})
    check('primo amministratore: mai da un altro PC', r.status_code == 403, r.status_code)

    print('\n' + '=' * 60)
    print(f'PASSATI: {OK}   FALLITI: {len(KO)}')
    if KO:
        print('Falliti: ' + ', '.join(KO))
    print('=' * 60)
    return 1 if KO else 0


def AuditLog_():
    from backend.models import AuditLog
    return AuditLog


_modalita_vera = A.modalita


if __name__ == '__main__':
    code = 1
    try:
        code = main()
    finally:
        try:
            _ENG.dispose()
        except Exception:
            pass
        shutil.rmtree(_CARTELLE, ignore_errors=True)
    sys.exit(code)
