"""Test delle PROTEZIONI MINIME (fondamenta F3).

Copre:
 1. percorsi dei file di un preventivo: solo UUID, sempre dentro preventivi_tmp
    (prima un id come ".." usciva dalla cartella)
 2. endpoint di amministrazione (backup, impostazioni, elenco, audit,
    export, dispositivi): solo amministrazione/capi
 3. modifica/cancellazione/data/PDF di un ordine: solo amministrazione/capi,
    con una riga di audit
 4. un capo DISATTIVATO non comanda piu'
 5. accettazione: data di consegna sbagliata o nel passato rifiutata PRIMA di
    toccare il preventivo
 6. numeri d'ordine PREV- unici (indice parziale); i numeri dei clienti possono
    ripetersi

Gira su DATABASE TEMPORANEO. Esecuzione: python app/tests/test_protezioni.py
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Order, AuditLog, Preventivo  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_prot_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import backend.app  # noqa: E402,F401
A = sys.modules["backend.app"]
from backend.app import app  # noqa: E402
from backend.database import PreventivoManager  # noqa: E402

# il backup vero non si tocca: finte funzioni di backup
A._do_backup = lambda motivo='manuale': os.path.join(tempfile.gettempdir(), 'finto.db')
A._integrity_check = lambda: True
A._backup_list = lambda: []
A._backup_load_config = lambda: {'backup_enabled': True, 'interval_hours': 1, 'max_backups': 48,
                                 'backup_path': '', 'remote_path': ''}
A._backup_save_config = lambda cfg: None

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
        s.add(User(id='amm', name='Ufficio', role='Amministrazione', is_active=True))
        s.add(User(id='capo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
        s.add(User(id='capo-via', name='Ex capo', role='Capo Officina', is_active=False, is_capo=True))
        s.add(User(id='op', name='Enzo', role='Operaio Officina', is_active=True))
        s.add(User(id='comm', name='Commerciale', role='Commerciale', is_active=True))
        now = datetime.utcnow()
        base = dict(cliente='Cliente Alfa', data_ricezione=now, data_consegna=now + timedelta(days=7))
        s.add(Order(id='o1', numero_ordine='A-1', status='RICEVUTO', **base))
        s.add(Order(id='o2', numero_ordine='A-2', status='RICEVUTO', **base))
        s.commit()
    finally:
        s.close()


def audit(azione):
    s = models.SessionLocal()
    try:
        return s.query(AuditLog).filter(AuditLog.action == azione).all()
    finally:
        s.close()


def main():
    setup()
    c = app.test_client()
    H = {'X-User-Id': 'amm'}

    print('\n1) Percorsi dei file di un preventivo')
    u = str(uuid.uuid4())
    check('UUID valido: cartella dentro preventivi_tmp',
          os.path.dirname(A._cartella_preventivo(u)) == os.path.realpath(os.path.join(A.UPLOAD_FOLDER, 'preventivi_tmp')))
    for cattivo in ('..', '../x', '..\\x', 'abc', '', None, u + '/..'):
        try:
            A._cartella_preventivo(cattivo); passato = True
        except ValueError:
            passato = False
        check(f'id {cattivo!r} rifiutato', not passato)
    for rotta in ('/api/preventivi/non-un-uuid/file-disegni', '/api/preventivi/..%5C..%5Cx/file-disegni',
                  '/api/preventivi/..%2F/file-disegni'):
        r = c.get(rotta)
        check(f'GET {rotta} -> 400 o 404 (mai file)', r.status_code in (400, 404), r.status_code)
    r = c.get('/api/preventivi/non-un-uuid/file-disegni')
    check('id non UUID: 400 esplicito', r.status_code == 400, r.status_code)

    print('\n2) Endpoint di amministrazione')
    for metodo, rotta in (('post', '/api/admin/backup'), ('get', '/api/admin/backup/settings'),
                          ('put', '/api/admin/backup/settings'), ('get', '/api/admin/backup/list'),
                          ('get', '/api/admin/audit'), ('get', '/api/admin/export-json'),
                          ('get', '/api/admin/dispositivi')):
        kw = {'json': {}} if metodo == 'put' else {}
        r0 = getattr(c, metodo)(rotta, **kw)
        r1 = getattr(c, metodo)(rotta, headers={'X-User-Id': 'op'}, **kw)
        r2 = getattr(c, metodo)(rotta, headers=H, **kw)
        check(f'{metodo.upper()} {rotta}: anonimo 403, operaio 403, ufficio ok',
              r0.status_code == 403 and r1.status_code == 403 and r2.status_code == 200,
              (r0.status_code, r1.status_code, r2.status_code))
    r = c.get('/api/admin/audit?user_id=amm')
    check('audit: user_id nella query e\' un filtro, non un\'identita\'', r.status_code == 403, r.status_code)
    r = c.put('/api/admin/backup/settings', json={'backup_path': 'C:\\cartella\\che\\non\\esiste'}, headers=H)
    check('impostazioni: cartella inesistente rifiutata', r.status_code == 400, r.status_code)

    print('\n3) Ordini: modifica, data, cancellazione, PDF')
    r = c.put('/api/orders/o1', json={'note': 'x'})
    check('PUT ordine anonimo -> 403', r.status_code == 403, r.status_code)
    r = c.put('/api/orders/o1', json={'note': 'nota nuova'}, headers=H)
    check('PUT ordine ufficio -> ok', r.status_code == 200, (r.status_code, r.get_json()))
    check('PUT ordine: riga di audit con prima/dopo', any('nota nuova' in (a.detail or '') for a in audit('ORDINE_MODIFICA')))
    r = c.put('/api/orders/o1/delivery-date', json={'data_consegna': '2026-12-01', 'user_id': 'op'})
    check('data consegna da operaio -> 403', r.status_code == 403, r.status_code)
    r = c.put('/api/orders/o1/delivery-date', json={'data_consegna': '2026-12-01', 'user_id': 'capo'})
    check('data consegna da capo (user_id nel body) -> ok + audit',
          r.status_code == 200 and len(audit('ORDINE_DATA_CONSEGNA')) == 1, r.status_code)
    r = c.delete('/api/orders/o2')
    check('DELETE ordine anonimo -> 403', r.status_code == 403, r.status_code)
    r = c.delete('/api/orders/o2', headers=H)
    check('DELETE ordine ufficio -> ok + audit', r.status_code == 200 and len(audit('ORDINE_ELIMINATO')) == 1, r.status_code)
    import io as _io
    r = c.post('/api/orders/o1/replace-pdf', data={'file': (_io.BytesIO(b'%PDF-1.4'), 'nuovo.pdf')},
               content_type='multipart/form-data')
    check('sostituzione PDF anonima -> 403', r.status_code == 403, r.status_code)

    print('\n4) Capo disattivato')
    check('capo attivo comanda', A._require_capo('capo') is True)
    check('capo disattivato NON comanda', A._require_capo('capo-via') is False)

    print('\n5) Accettazione: data di consegna')
    p = PreventivoManager.create('Cliente Beta', 'comm', quantita=1)
    pid = p['id']
    s = models.SessionLocal()
    try:
        s.query(Preventivo).filter(Preventivo.id == pid).update({'status': 'INVIATO'})
        s.commit()
    finally:
        s.close()
    for data, nome in (('31/12/2026', 'formato sbagliato'), ('2020-01-01', 'nel passato')):
        r = c.post(f'/api/preventivi/{pid}/accetta', json={'user_id': 'comm', 'data_consegna': data,
                                                              'articoli': [{'codice': 'X'}]})
        stato = PreventivoManager.get(pid, include_children=False)['status']
        check(f'data {nome}: rifiutata (400) e preventivo ancora INVIATO',
              r.status_code == 400 and stato == 'INVIATO', (r.status_code, (r.get_json() or {}).get('error'), stato))

    print('\n6) Numeri d\'ordine PREV- unici')
    models._indice_numero_prev()
    s = models.SessionLocal()
    now = datetime.utcnow()
    base = dict(cliente='C', data_ricezione=now, data_consegna=now, status='RICEVUTO')
    try:
        s.add(Order(id='p1', numero_ordine='PREV-2026-0007', **base)); s.commit()
        s.add(Order(id='p2', numero_ordine='PREV-2026-0007', **base))
        try:
            s.commit(); doppio = True
        except IntegrityError:
            s.rollback(); doppio = False
        check('secondo PREV-2026-0007 rifiutato dal database', not doppio)
        s.add(Order(id='c1', numero_ordine='1240', **base)); s.add(Order(id='c2', numero_ordine='1240', **base))
        try:
            s.commit(); cliente_ok = True
        except IntegrityError:
            s.rollback(); cliente_ok = False
        check('numero del cliente ripetuto ammesso', cliente_ok)
    finally:
        s.close()
    from backend.database import OrderManager
    check('prossimo numero dopo PREV-2026-0007', OrderManager.next_numero_preventivo().endswith('-0008')
          or not OrderManager.next_numero_preventivo().startswith('PREV-2026'))

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
