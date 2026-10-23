"""Scritture insieme sullo stesso ordine: nessuna cancella quella dell'altra.

Prova notturna 22-23/10 (GRAVE, 1 volta su 789 giri): "pianificata
2026-11-03, nell'ordine non c'e' (DDT e smistamento salvati insieme)". Nel
registro della copia (audit_log) l'ordine mostra, 5 ms dopo la pianificazione
del banco (-> 2026-11-03) e il DDT, una SECONDA pianificazione
(2026-11-03 -> 2026-10-24, senza durata): quella di una postazione laser del
banco stesso che pianifica a caso gli stessi ordini. Non un aggiornamento
perso: l'ultima scrittura vince, come deve.

Questo test tiene ferma la regola che lo garantisce: ogni salvataggio
sull'ordine scrive SOLO le sue colonne (UPDATE mirato), mai la riga intera
letta prima; cosi' pianifica-taglio, DDT, smistamento, taglio fatto e lavoro
finito insieme non si cancellano a vicenda.

 1. UPDATE di ciascuna azione: solo le colonne sue
 2. 40 giri con pianifica-taglio + DDT + smistamento nello stesso istante
    (thread, sulle rotte vere): data, durata, DDT e smistamento restano tutti
 3. con anche taglio fatto e nota del lavoro finito nello stesso istante

Gira su DATABASE TEMPORANEO (file vero, per la concorrenza di SQLite).
Esecuzione: python app/tests/test_aggiornamento_perso.py
"""
import os
import re
import sys
import tempfile
import threading
import uuid
from datetime import date, datetime, timedelta

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine, event  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Order  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_agg_perso_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False, 'timeout': 20})


@event.listens_for(_ENG, 'connect')
def _wal(dbapi_connection, _rec):
    cur = dbapi_connection.cursor()
    cur.execute('PRAGMA journal_mode=WAL')
    cur.close()


_UPDATE = []
_REGISTRA = threading.local()


@event.listens_for(_ENG, 'before_cursor_execute')
def _cattura(conn, cursor, statement, parameters, context, executemany):
    if getattr(_REGISTRA, 'attivo', False) and statement.lstrip().upper().startswith('UPDATE ORDERS'):
        _UPDATE.append(statement)


models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend.database import OrderManager  # noqa: E402
from backend import ordini_service as osv  # noqa: E402
from tests.accesso_aiuto import postazioni, entra, modalita, ufficio_pronto  # noqa: E402

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


def nuovo_ordine(finito=True):
    oid = str(uuid.uuid4())
    s = models.SessionLocal()
    s.add(Order(id=oid, numero_ordine='AP-' + oid[:4], cliente='Cliente Alfa', status='RICEVUTO',
                data_consegna=datetime.utcnow() + timedelta(days=20),
                data_completamento_operativo=datetime.utcnow() if finito else None,
                completato_operativo_da='paolo' if finito else None))
    s.commit()
    s.close()
    return oid


def leggi(oid):
    s = models.SessionLocal()
    try:
        return s.query(Order).filter(Order.id == oid).first()
    finally:
        s.close()


def colonne_set(stmt):
    m = re.search(r'SET (.*?) WHERE', stmt, re.S | re.I)
    return {c.split('=')[0].strip() for c in (m.group(1).split(',') if m else [])}


def main_prove():
    s = models.SessionLocal()
    s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
    s.commit()
    s.close()

    print('\n1) Ogni azione scrive solo le sue colonne')
    oid = nuovo_ordine()
    giorno = date.today() + timedelta(days=5)
    azioni = {
        'pianifica-taglio': (lambda: OrderManager.pianifica_taglio(oid, 'laser', giorno=giorno, durata_min=30.0),
                             {'data_taglio_pianificata', 'durata_laser_manuale_min'}),
        'smistamento': (lambda: OrderManager.smista(oid, True, 'laser'),
                        {'taglio_richiesto', 'smistato_il', 'smistato_da'}),
        'DDT': (lambda: osv.registra_ddt(oid, 'ufficio', 'DDT-1'), None),
        'taglio fatto': (lambda: OrderManager.mark_laser_done(oid, user_id='laser'), None),
    }
    vietate = {'data_taglio_pianificata', 'durata_laser_manuale_min', 'taglio_richiesto', 'smistato_il',
               'smistato_da', 'numero_ddt', 'data_ddt', 'ddt_numero', 'ddt_data', 'note', 'cliente',
               'numero_ordine', 'data_consegna', 'status'}
    proprie = {'pianifica-taglio': {'data_taglio_pianificata', 'durata_laser_manuale_min'},
               'smistamento': {'taglio_richiesto', 'smistato_il', 'smistato_da'},
               'DDT': {'numero_ddt', 'data_ddt', 'ddt_numero', 'ddt_data', 'status'},
               'taglio fatto': {'taglio_completato', 'data_taglio_completato', 'taglio_completato_da',
                                'fase_corrente', 'status'}}
    for nome, (fn, _x) in azioni.items():
        _UPDATE.clear()
        _REGISTRA.attivo = True
        try:
            r = fn()
        finally:
            _REGISTRA.attivo = False
        col = set().union(*[colonne_set(x) for x in _UPDATE]) if _UPDATE else set()
        fuori = (col & vietate) - proprie[nome]
        check(f'{nome}: riuscito', (r or {}).get('success') is not False and not (r or {}).get('error'), r)
        check(f'{nome}: UPDATE mirato ({", ".join(sorted(col)) or "-"})', col and not fuori, fuori)

    print('\n2) pianifica-taglio + DDT + smistamento nello stesso istante, sulle rotte vere')
    modalita('protetto')
    uff = ufficio_pronto(A.app)
    las1 = entra(A.app, 'laser')
    las2 = entra(A.app, 'laser')
    persi = []
    for i in range(40):
        oid = nuovo_ordine()
        g = (date.today() + timedelta(days=1 + i % 15)).isoformat()
        ddt = f'DDT{i:03d}'
        r = insieme([
            lambda: las1.post(f'/api/orders/{oid}/pianifica-taglio', json={'data': g, 'durata_min': 30}),
            lambda: uff.post(f'/api/ordini/{oid}/ddt', json={'numero': ddt}),
            lambda: las2.post(f'/api/orders/{oid}/smistamento', json={'va_tagliato': True}),
        ])
        cod = [x.status_code if hasattr(x, 'status_code') else x for x in r]
        o = leggi(oid)
        stato = (str(o.data_taglio_pianificata), o.durata_laser_manuale_min, osv.ddt_di(o) if hasattr(osv, 'ddt_di') else None,
                 o.taglio_richiesto)
        if cod != [200, 200, 200] or str(o.data_taglio_pianificata) != g or o.durata_laser_manuale_min != 30.0 \
                or o.taglio_richiesto is not True or ddt not in str(stato[2]):
            persi.append((i, cod, stato))
    check('40 giri: niente perso', not persi, persi[:3])

    print('\n3) Con anche taglio fatto e nota del lavoro finito')
    persi = []
    for i in range(20):
        oid = nuovo_ordine()
        g = (date.today() + timedelta(days=2 + i % 10)).isoformat()
        r = insieme([
            lambda: las1.post(f'/api/orders/{oid}/pianifica-taglio', json={'data': g}),
            lambda: uff.post(f'/api/ordini/{oid}/ddt', json={'numero': f'X{i}'}),
            lambda: las2.post(f'/api/orders/{oid}/smistamento', json={'va_tagliato': True}),
            lambda: las1.post(f'/api/orders/{oid}/mark-laser-done', json={}),
            lambda: uff.post(f'/api/ordini/{oid}/completamento/nota', json={'nota': f'detto da Mario {i}'}),
        ])
        o = leggi(oid)
        if not (str(o.data_taglio_pianificata) == g and o.taglio_richiesto is True and o.taglio_completato
                and f'X{i}' in str(osv.ddt_di(o)) and o.nota_completamento == f'detto da Mario {i}'):
            persi.append((i, [getattr(x, 'status_code', x) for x in r],
                          (o.data_taglio_pianificata, o.taglio_richiesto, o.taglio_completato, osv.ddt_di(o),
                           o.nota_completamento)))
    check('20 giri: niente perso', not persi, persi[:3])


def main():
    try:
        main_prove()
    except Exception:
        import traceback
        traceback.print_exc()
        KO.append('eccezione')
    print(f'\nPASSATI: {OK}   FALLITI: {len(KO)}')
    for k in KO:
        print('  -', k)
    try:
        _ENG.dispose()
        os.remove(_TMP)
    except OSError:
        pass
    return 1 if KO else 0


if __name__ == '__main__':
    sys.exit(main())
