"""KPI della dashboard: GET /api/kpi/dashboard rispondeva SEMPRE 500.

La rotta risponde 500 se il risultato non ha 'success': il calcolo era giusto
ma non lo diceva (prova notturna 12/10). Ora risponde 200 coi numeri, e
/api/admin/kpi legge anche "completati oggi" e "puntualita'" dal riepilogo.

Gira su DATABASE TEMPORANEO. Esecuzione: python app/tests/test_kpi_dashboard.py
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

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, Order  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_kpi_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend.app import app  # noqa: E402
from tests.accesso_aiuto import ufficio_pronto, eleva, persona, PIN_ADMIN  # noqa: E402

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


def main():
    adesso = datetime.utcnow()
    s = models.SessionLocal()
    # aperto in ritardo, finito oggi puntuale, finito in ritardo
    s.add(Order(id=str(uuid.uuid4()), cliente='A', numero_ordine='K-1', status='IN_LAVORAZIONE',
                data_consegna=adesso - timedelta(days=3)))
    s.add(Order(id=str(uuid.uuid4()), cliente='B', numero_ordine='K-2', status='COMPLETATO',
                data_consegna=adesso + timedelta(days=3), data_completamento_operativo=adesso))
    s.add(Order(id=str(uuid.uuid4()), cliente='C', numero_ordine='K-3', status='COMPLETATO',
                data_consegna=adesso - timedelta(days=10),
                data_completamento_operativo=adesso - timedelta(days=5)))
    s.commit()
    s.close()

    c = ufficio_pronto(app)
    persona('admin-k', 'Admin Kpi', pin=PIN_ADMIN, admin=True)
    eleva(c, PIN_ADMIN)

    r = c.get('/api/kpi/dashboard')
    d = r.get_json() or {}
    check('/api/kpi/dashboard -> 200 con success', r.status_code == 200 and d.get('success') is True,
          (r.status_code, d))
    check('numeri: 3 ordini, 1 aperto, 1 in ritardo', (d.get('totale_ordini'), d.get('ordini_aperti'),
                                                        d.get('in_ritardo')) == (3, 1, 1), d)
    rie = d.get('riepilogo') or {}
    check('riepilogo: 1 finito oggi, puntualita\' 50%',
          rie.get('completati_oggi') == 1 and rie.get('efficienza_puntualita') == 50.0, rie)

    r = c.get('/api/admin/kpi')
    k = ((r.get_json() or {}).get('kpi_globali') or {})
    check('/api/admin/kpi legge il riepilogo', r.status_code == 200 and k.get('completati_oggi') == 1
          and k.get('efficienza') == 50.0, (r.status_code, k))

    print(f'\nRisultato: {OK} ok, {len(KO)} falliti')
    return 1 if KO else 0


if __name__ == '__main__':
    sys.exit(main())
