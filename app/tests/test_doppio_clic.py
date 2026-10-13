"""Doppio clic / due postazioni insieme su "Taglio fatto" e "Lavoro finito".

Dalla prova notturna (12/10): premuti nello stesso istante da due postazioni
passavano tutte e due (333 volte su "Taglio fatto"): storico doppio e, per
"Lavoro finito", avvisi doppi all'ufficio. Ora la scrittura e' condizionata e
atomica: il primo vince, il secondo riceve "gia' fatto" (gia_fatto: true) e
non cambia nulla. Regola:
  - Taglio fatto ripetuto  -> 409, codice 'gia_fatto' (prima era 400)
  - Lavoro finito ripetuto -> 200, gia_registrato / gia_fatto true (come prima)

Gira su DATABASE TEMPORANEO (file vero, per la concorrenza di SQLite).
Esecuzione: python app/tests/test_doppio_clic.py
"""
import os
import sys
import tempfile
import threading
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

from sqlalchemy import create_engine, event  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Order, AuditLog, Notification  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_doppio_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False, 'timeout': 20})


@event.listens_for(_ENG, 'connect')
def _wal(dbapi_connection, _rec):
    cur = dbapi_connection.cursor()
    cur.execute('PRAGMA journal_mode=WAL')
    cur.close()


models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend.app import app  # noqa: E402
from backend.database import OrderManager  # noqa: E402
from tests.accesso_aiuto import stazione_pronta, entra  # noqa: E402

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


def insieme(funzioni):
    barriera = threading.Barrier(len(funzioni))
    out = [None] * len(funzioni)

    def corri(i, f):
        barriera.wait(10)
        try:
            out[i] = f()
        except Exception as e:
            out[i] = ('eccezione', repr(e))
    th = [threading.Thread(target=corri, args=(i, f)) for i, f in enumerate(funzioni)]
    for t in th:
        t.start()
    for t in th:
        t.join(60)
    return out


def nuovo_ordine():
    oid = str(uuid.uuid4())
    s = models.SessionLocal()
    s.add(Order(id=oid, numero_ordine='DC-' + oid[:4], cliente='Cliente Alfa', status='RICEVUTO',
                data_consegna=datetime.utcnow() + timedelta(days=10), taglio_richiesto=True))
    s.commit()
    s.close()
    return oid


def conta(modello, **filtri):
    s = models.SessionLocal()
    try:
        q = s.query(modello)
        for k, v in filtri.items():
            q = q.filter(getattr(modello, k) == v)
        return q.count()
    finally:
        s.close()


def main():
    s = models.SessionLocal()
    s.add(User(id='elena', name='Elena Colombo', role='Impiegata', is_active=True))
    s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
    s.commit()
    s.close()

    print('\n1) Taglio fatto: due postazioni insieme, 15 giri')
    male = []
    for _ in range(15):
        oid = nuovo_ordine()
        r = insieme([lambda: OrderManager.mark_laser_done(oid, 'postazione-laser'),
                     lambda: OrderManager.mark_laser_done(oid, 'postazione-laser')])
        vinti = [x for x in r if isinstance(x, dict) and x.get('success')]
        gia = [x for x in r if isinstance(x, dict) and x.get('gia_fatto')]
        n_audit = conta(AuditLog, entity_id=oid, action='MARK_LASER_DONE')
        if not (len(vinti) == 1 and len(gia) == 1 and n_audit == 1):
            male.append((r, n_audit))
    check('uno vince, l\'altro "gia\' fatto", un solo storico', not male, male[:2])

    print('\n2) Lavoro finito: due postazioni insieme, 15 giri')
    male = []
    for _ in range(15):
        oid = nuovo_ordine()
        r = insieme([lambda: OrderManager.close_order(oid, 'paolo'),
                     lambda: OrderManager.close_order(oid, 'paolo')])
        gia = sorted(bool(x.get('gia_registrato')) for x in r if isinstance(x, dict) and x.get('success'))
        n_audit = conta(AuditLog, entity_id=oid, action='CLOSE_ORDER')
        n_compl = conta(AuditLog, entity_id=oid, action='ORDINE_COMPLETAMENTO')
        n_notif = conta(Notification, order_id=oid)
        if not (gia == [False, True] and n_audit == 1 and n_compl == 1 and n_notif == 1):
            male.append((r, n_audit, n_compl, n_notif))
    check('una registra, l\'altra "gia\' registrato"; storico e avviso una volta', not male,
          [(m[1:], [x.get('gia_registrato') if isinstance(x, dict) else x for x in m[0]]) for m in male[:2]])

    print('\n3) API: risposte delle pagine')
    laser = stazione_pronta(app, 'laser')
    oid = nuovo_ordine()
    r1 = laser.post(f'/api/orders/{oid}/mark-laser-done', json={})
    r2 = laser.post(f'/api/orders/{oid}/mark-laser-done', json={})
    d2 = r2.get_json() or {}
    check('primo Taglio fatto -> 200', r1.status_code == 200, r1.get_json())
    check('secondo -> 409 gia_fatto con messaggio chiaro', r2.status_code == 409 and d2.get('gia_fatto') is True
          and 'gia\' segnato' in (d2.get('error') or ''), (r2.status_code, d2))
    c1 = laser.post(f'/api/orders/{oid}/close', json={})
    c2 = laser.post(f'/api/orders/{oid}/close', json={})
    check('Lavoro finito ripetuto -> 200 gia_registrato e gia_fatto',
          c1.status_code == 200 and c1.get_json().get('gia_fatto') is False and c2.status_code == 200
          and c2.get_json().get('gia_registrato') is True and c2.get_json().get('gia_fatto') is True,
          (c1.get_json(), c2.get_json()))
    check('un solo CLOSE_ORDER nello storico', conta(AuditLog, entity_id=oid, action='CLOSE_ORDER') == 1)

    print(f'\nRisultato: {OK} ok, {len(KO)} falliti')
    try:
        _ENG.dispose()
        os.remove(_TMP)
    except OSError:
        pass
    return 1 if KO else 0


if __name__ == '__main__':
    sys.exit(main())
