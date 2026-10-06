"""Test dei DISPOSITIVI dalla pagina di amministrazione.

Di solito un dispositivo si registra dalla sua pagina iniziale ("Cosa e'
questo dispositivo?") col PIN di un amministratore. Dalla pagina
Amministrazione si puo' anche preparare un codice da portare a mano (QR o
indirizzo), vedere l'elenco, rinominare e revocare.

Verifica che la comodita' non apra un buco:
 - solo un amministratore col PIN digitato adesso crea, elenca o revoca
   (X-User-Id, admin_id e simili non contano)
 - il codice creato funziona davvero: aperto una volta, diventa il cookie
 - revocato, smette di funzionare subito
 - l'elenco non espone mai il segreto

Gira su DATABASE TEMPORANEO. Esecuzione: python app/tests/test_dispositivi_admin.py
"""
import os
import sys
import tempfile
import uuid

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base  # noqa: E402
from backend import models_ore  # noqa: E402,F401
from backend.models_ore import Cliente  # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), f'test_disp_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend.app import app  # noqa: E402
from tests.accesso_aiuto import (postazioni, persona, entra, eleva, modalita,  # noqa: E402
                                 PIN_ADMIN, PIN_UFFICIO)

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


def setup():
    postazioni()
    persona('capo', 'Paolo Capo', 'Laser', pin=PIN_ADMIN, admin=True)
    persona('enzo', 'Enzo Bianchi', 'Operaio', pin=PIN_UFFICIO, admin=False)
    s = models.SessionLocal()
    try:
        s.add(Cliente(id=str(uuid.uuid4()), nome='Cliente Alfa', attivo=True))
        s.commit()
    finally:
        s.close()


def main():
    setup()
    modalita('protetto')
    anon = app.test_client()
    enzo = entra(app, 'ufficio', pin=PIN_UFFICIO)          # entrato, ma non amministratore
    adm = eleva(entra(app, 'laser'), PIN_ADMIN)              # al laser, col PIN di Paolo

    # =====================================================================
    print('\n1) Solo un amministratore col PIN')
    r = enzo.post('/api/admin/dispositivi', json={'label': 'Abusivo', 'scope': 'ore', 'admin_id': 'capo'},
                  headers={'X-User-Id': 'capo'})
    check('persona non amministratore (anche dichiarandosi capo): 403', r.status_code == 403, r.status_code)
    r = anon.post('/api/admin/dispositivi', json={'label': 'Abusivo', 'scope': 'ore', 'admin_id': 'capo'})
    check('browser qualunque: 403', r.status_code == 403, r.status_code)

    # =====================================================================
    print('\n2) Stazioni')
    r = adm.post('/api/admin/dispositivi', json={'label': 'Fantasia', 'scope': 'root'})
    check('stazione inventata rifiutata', r.status_code == 400, r.status_code)
    r = adm.post('/api/admin/dispositivi', json={'label': '', 'scope': 'ore'})
    check('nome obbligatorio', r.status_code == 400, r.status_code)

    # =====================================================================
    print('\n3) Il codice preparato funziona davvero')
    r = adm.post('/api/admin/dispositivi', json={'label': 'Tablet timbratrice', 'scope': 'ore'})
    check('creato (201)', r.status_code == 201, r.status_code)
    disp = r.get_json()['dispositivo']
    token, tid = disp['token'], disp['id']
    check('indirizzo della pagina iniziale col codice', '/?codice=' in disp.get('url', ''), disp.get('url'))
    check('indirizzo non punta a localhost',
          'localhost' not in disp['url'] and '127.0.0.1' not in disp['url'], disp['url'])
    tablet = app.test_client()
    r = tablet.post('/api/accesso/usa-codice', json={'codice': token})
    check('aperto sul tablet: diventa il cookie',
          r.status_code == 200 and r.get_json()['pagina'] == '/ore.html', r.get_json())
    r = tablet.get('/api/ore/contesto')
    check('il tablet apre le ore senza altro', r.status_code == 200, r.status_code)
    check('con la stazione giusta', r.get_json().get('scope') == 'ore', r.get_json())
    r = tablet.get('/api/ore/anomalie')
    check("ma NON le funzioni d'ufficio (403)", r.status_code == 403, r.status_code)
    r = app.test_client().post('/api/accesso/usa-codice', json={'codice': 'inventato'})
    check('codice inventato: 403', r.status_code == 403, r.status_code)

    # =====================================================================
    print("\n4) L'elenco non espone mai il segreto")
    r = enzo.get('/api/admin/dispositivi?admin_id=capo')
    check('elenco senza PIN admin: 403', r.status_code == 403, r.status_code)
    r = adm.get('/api/admin/dispositivi')
    righe = r.get_json()['dispositivi']
    mio = [x for x in righe if x['id'] == tid]
    check('il tablet compare in elenco', len(mio) == 1, righe)
    check('nessun campo token nell elenco', all('token' not in x for x in righe), righe)
    check('nessun hash esposto', all('token_hash' not in x and 'hash' not in x for x in righe), righe)
    check('indirizzo base fornito per il QR', bool(r.get_json().get('url_base')))
    check('si sa da quale dispositivo si guarda', sum(1 for x in righe if x.get('questo')) == 1)
    r = adm.put(f'/api/admin/dispositivi/{tid}', json={'nome': 'Timbratrice ingresso'})
    check('rinomina', r.status_code == 200 and any(
        x['nome'] == 'Timbratrice ingresso' for x in adm.get('/api/admin/dispositivi').get_json()['dispositivi']),
        r.status_code)

    # =====================================================================
    print('\n5) Revoca: il dispositivo smette subito di funzionare')
    r = enzo.delete(f'/api/admin/dispositivi/{tid}?admin_id=capo')
    check("non amministratore non puo' revocare (403)", r.status_code == 403, r.status_code)
    check('prima della revoca funziona ancora', tablet.get('/api/ore/contesto').status_code == 200)
    r = adm.delete(f'/api/admin/dispositivi/{tid}')
    check("l'amministratore revoca (200)", r.status_code == 200, r.status_code)
    r = tablet.get('/api/ore/contesto')
    check("dopo la revoca e' respinto (401)", r.status_code == 401, r.status_code)
    r = app.test_client().get('/api/ore/contesto', headers={'X-Device-Token': token})
    check("anche col vecchio token nell'intestazione", r.status_code == 401, r.status_code)
    mio = [x for x in adm.get('/api/admin/dispositivi').get_json()['dispositivi'] if x['id'] == tid]
    check('resta in elenco come revocato', len(mio) == 1 and mio[0]['is_active'] is False, mio)
    r = adm.delete('/api/admin/dispositivi/non-esiste')
    check('id inesistente -> 404', r.status_code == 404, r.status_code)

    # =====================================================================
    print('\n6) QR del codice')
    r = adm.get('/api/admin/dispositivi/qr?testo=http://192.168.1.9:5000/?codice=x')
    check('QR generato', r.status_code == 200, r.status_code)
    check("e' una immagine", 'svg' in r.headers.get('Content-Type', ''), r.headers.get('Content-Type'))
    check('non vuoto', len(r.data) > 500, len(r.data))
    r = adm.get('/api/admin/dispositivi/qr?testo=')
    check('senza testo rifiutato', r.status_code == 400, r.status_code)
    r = adm.get('/api/admin/dispositivi/qr?testo=' + 'x' * 600)
    check('testo troppo lungo rifiutato', r.status_code == 400, r.status_code)
    check('QR: solo amministratore', enzo.get('/api/admin/dispositivi/qr?testo=x').status_code == 403)

    print('\n' + '=' * 60)
    print(f'PASSATI: {OK}   FALLITI: {len(KO)}')
    if KO:
        print('Falliti: ' + ', '.join(KO))
    print('=' * 60)
    return 1 if KO else 0


if __name__ == '__main__':
    code = main()
    try:
        _ENG.dispose()
        os.remove(_TMP)
    except Exception:
        pass
    sys.exit(code)
