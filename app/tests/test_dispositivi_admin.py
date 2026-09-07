"""Test dell'ABILITAZIONE DEI TABLET dall'interfaccia di amministrazione.

Il tablet appeso vicino alla timbratrice non ha login: si abilita una volta
sola con un token di dispositivo. Prima si poteva fare solo da riga di comando,
quindi in pratica la pagina delle ore era irraggiungibile.

Verifica soprattutto che aprire questa comodita' non apra anche un buco:
 - solo capi/amministratori possono abilitare o revocare
 - dall'interfaccia NON si possono creare token d'ufficio (darebbero i poteri
   dell'impiegata a chiunque sappia chiamare l'endpoint)
 - il token creato funziona davvero sulla pagina ore
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
from backend.models import Base, User  # noqa: E402
from backend import models_ore  # noqa: E402
from backend.models_ore import Cliente  # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), f'test_disp_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend.app import app  # noqa: E402

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
    s = models.SessionLocal()
    try:
        s.add(User(id='capo', name='Paolo Capo', role='Capo Officina',
                   is_active=True, is_capo=True))
        s.add(User(id='enzo', name='Enzo Bianchi', role='Operaio Officina',
                   is_active=True))
        s.add(Cliente(id=str(uuid.uuid4()), nome='Cliente Alfa', attivo=True))
        s.commit()
    finally:
        s.close()


def main():
    setup()
    c = app.test_client()

    # =====================================================================
    print('\n1) Solo capi e amministratori possono abilitare un tablet')
    r = c.post('/api/admin/dispositivi',
               json={'label': 'Abusivo', 'scope': 'ore', 'admin_id': 'enzo'})
    check('operaio respinto (403)', r.status_code == 403, r.status_code)
    r = c.post('/api/admin/dispositivi',
               json={'label': 'Abusivo', 'scope': 'ore', 'admin_id': ''})
    check('senza identita respinto (403)', r.status_code == 403, r.status_code)

    # =====================================================================
    print('\n2) Dall interfaccia non si creano token d ufficio')
    r = c.post('/api/admin/dispositivi',
               json={'label': 'Scorciatoia', 'scope': 'ufficio', 'admin_id': 'capo'})
    check('scope ufficio rifiutato (400)', r.status_code == 400, r.status_code)
    check('spiega il perche in italiano',
          'officina' in (r.get_json().get('error') or '').lower(), r.get_json())
    r = c.post('/api/admin/dispositivi',
               json={'label': 'Fantasia', 'scope': 'root', 'admin_id': 'capo'})
    check('scope inventato rifiutato', r.status_code == 400, r.status_code)

    # =====================================================================
    print('\n3) Il tablet abilitato funziona davvero')
    r = c.post('/api/admin/dispositivi',
               json={'label': '', 'scope': 'ore', 'admin_id': 'capo'})
    check('nome obbligatorio', r.status_code == 400, r.status_code)

    r = c.post('/api/admin/dispositivi',
               json={'label': 'Tablet timbratrice', 'scope': 'ore', 'admin_id': 'capo'})
    check('creato (201)', r.status_code == 201, r.status_code)
    disp = r.get_json()['dispositivo']
    token, tid = disp['token'], disp['id']
    check('indirizzo pronto per la pagina ore',
          '/ore.html?token=' in disp.get('url', ''), disp.get('url'))
    check('indirizzo non punta a localhost',
          'localhost' not in disp['url'] and '127.0.0.1' not in disp['url'], disp['url'])

    r = c.get('/api/ore/contesto', headers={'X-Device-Token': token})
    check('il token apre la pagina ore', r.status_code == 200, r.status_code)
    check('con lo scope giusto', r.get_json().get('scope') == 'ore', r.get_json())

    r = c.get('/api/ore/anomalie', headers={'X-Device-Token': token})
    check('ma NON le funzioni d ufficio (403)', r.status_code == 403, r.status_code)

    # =====================================================================
    print('\n4) L elenco non espone mai il segreto')
    r = c.get('/api/admin/dispositivi')
    righe = r.get_json()['dispositivi']
    mio = [x for x in righe if x['id'] == tid]
    check('il tablet compare in elenco', len(mio) == 1, righe)
    check('nessun campo token nell elenco',
          all('token' not in x for x in righe), righe)
    check('nessun hash esposto',
          all('token_hash' not in x and 'hash' not in x for x in righe), righe)
    check('indirizzo base fornito per il QR', bool(r.get_json().get('url_base')))

    # =====================================================================
    print('\n5) Revoca: il tablet smette subito di funzionare')
    r = c.delete(f'/api/admin/dispositivi/{tid}?admin_id=enzo')
    check('operaio non puo revocare (403)', r.status_code == 403, r.status_code)

    r = c.get('/api/ore/contesto', headers={'X-Device-Token': token})
    check('prima della revoca funziona ancora', r.status_code == 200)

    r = c.delete(f'/api/admin/dispositivi/{tid}?admin_id=capo')
    check('capo revoca (200)', r.status_code == 200, r.status_code)

    r = c.get('/api/ore/contesto', headers={'X-Device-Token': token})
    check('dopo la revoca e respinto (401)', r.status_code == 401, r.status_code)

    r = c.get('/api/admin/dispositivi')
    mio = [x for x in r.get_json()['dispositivi'] if x['id'] == tid]
    check('resta in elenco come revocato',
          len(mio) == 1 and mio[0]['is_active'] is False, mio)

    r = c.delete('/api/admin/dispositivi/non-esiste?admin_id=capo')
    check('id inesistente -> 404', r.status_code == 404, r.status_code)

    # =====================================================================
    print('\n6) QR di abilitazione')
    r = c.get('/api/admin/dispositivi/qr?testo=http://192.168.1.9:5000/ore.html?token=x')
    check('QR generato', r.status_code == 200, r.status_code)
    check('e una immagine', 'svg' in r.headers.get('Content-Type', ''),
          r.headers.get('Content-Type'))
    check('non vuoto', len(r.data) > 500, len(r.data))
    r = c.get('/api/admin/dispositivi/qr?testo=')
    check('senza testo rifiutato', r.status_code == 400, r.status_code)
    r = c.get('/api/admin/dispositivi/qr?testo=' + 'x' * 600)
    check('testo troppo lungo rifiutato', r.status_code == 400, r.status_code)

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
