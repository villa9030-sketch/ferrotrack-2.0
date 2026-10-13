"""Storico: ogni ordine creato ha la sua riga "chi l'ha creato".

Dalla prova notturna (12/10): gli ordini caricati dal PDF (POST /api/orders)
non lasciavano nessuna riga di creazione nel registro attivita' (67 casi).
Ora: POST /api/orders -> CREATE_ORDER sull'ordine, con la persona;
preventivo accettato -> ACCEPT_PREVENTIVO_CREATE_ORDER sull'ORDINE (prima
stava sul preventivo e lo storico dell'ordine non lo vedeva). Il pacchetto
scrive gia' CREATE_ORDER_FROM_PACKAGE (test_ordine_pacchetto).

Gira su DATABASE TEMPORANEO. Esecuzione: python app/tests/test_audit_creazione.py
"""
import os
import sys
import tempfile
import uuid
from datetime import date, timedelta

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
from backend.models import Base, AuditLog  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_audit_cr_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend.app import app  # noqa: E402
from tests.accesso_aiuto import ufficio_pronto, stazione_pronta  # noqa: E402

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


def righe(oid):
    s = models.SessionLocal()
    try:
        return [(a.action, a.entity_type, a.user_id, a.user_name) for a in
                s.query(AuditLog).filter(AuditLog.entity_id == oid).all()]
    finally:
        s.close()


def main():
    uff = ufficio_pronto(app, pid='elena', pin='7913')
    consegna = (date.today() + timedelta(days=20)).isoformat()

    print('\n1) Ordine dal PDF / a mano')
    r = uff.post('/api/orders', json={'numero_ordine': 'A-1', 'cliente': 'Cliente Alfa',
                                      'data_consegna': consegna})
    check('creato', r.status_code == 201, (r.status_code, r.get_json()))
    oid = (r.get_json() or {}).get('order_id')
    cr = [x for x in righe(oid) if 'CREATE' in x[0]]
    check('una riga CREATE_ORDER sull\'ordine, con chi l\'ha creato',
          len(cr) == 1 and cr[0][0] == 'CREATE_ORDER' and cr[0][1] == 'order' and cr[0][2] == 'elena', righe(oid))

    print('\n2) Ordine da preventivo accettato')
    com = stazione_pronta(app, 'commerciale')
    r = com.post('/api/preventivi', json={'cliente': 'Cliente Beta', 'quantita': 1})
    pid = r.get_json()['preventivo']['id']
    com.put(f'/api/preventivi/{pid}/articoli', json={'articoli': [
        {'codice': 'X-1', 'quantita': 5, 'costo_base_stimato': 10.0, 'spessore_mm': 3, 'materiale': 'S235'}]})
    com.post(f'/api/preventivi/{pid}/invia', json={})
    r = com.post(f'/api/preventivi/{pid}/accetta', json={'data_consegna': consegna})
    d = r.get_json() or {}
    check('accettato', r.status_code == 200 and d.get('order_id'), (r.status_code, d))
    oid2 = d.get('order_id')
    cr = [x for x in righe(oid2) if 'CREATE' in x[0]]
    check('una riga di creazione sull\'ordine, con chi',
          len(cr) == 1 and cr[0][1] == 'order' and cr[0][2], righe(oid2))

    print(f'\nRisultato: {OK} ok, {len(KO)} falliti')
    return 1 if KO else 0


if __name__ == '__main__':
    sys.exit(main())
