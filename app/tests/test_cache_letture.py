"""Tablet dell'officina e schermo dell'ufficio sotto carico: un calcolo solo.

Prova notturna 22-23/10: GET /api/officina/tablet p95 29,7 s (fino a 65 s),
GET /api/dashboard-live p95 4,2 s. Ogni tablet, ogni minuto, rifaceva da capo
distinta, disegni, "taglia entro" e segnalazioni di tutti gli ordini aperti
(decine di letture del database per richiesta). Ora (backend/versione_dati.py)
il risultato si calcola una volta e vale finche' non cambia niente nel
database:

 1. stessa risposta del calcolo di sempre
 2. 10 tablet insieme a cache vuota: UN calcolo
 3. senza cambi: nessun ricalcolo; le scritture dell'accesso (ultimo uso del
    dispositivo, sessioni) non contano
 4. un ordine cambiato, una segnalazione, un ordine nuovo: si vedono SUBITO
 5. mentre un tablet ricalcola, gli altri ricevono l'ultimo elenco subito
    invece di aspettare (o ricalcolare anche loro)
 6. dashboard-live: stessi conteggi, ricalcolati solo quando serve

Gira su DATABASE TEMPORANEO (file vero, per i thread).
Esecuzione: python app/tests/test_cache_letture.py
"""
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta

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
from backend.models import Base, User, Order, Preventivo  # noqa: E402
from backend import models_ore  # noqa: E402,F401
from backend.models_ore import DeviceToken  # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), f'test_cache_letture_{uuid.uuid4().hex[:8]}.db')
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

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend import versione_dati as vd  # noqa: E402
from backend.database import PreventivoManager  # noqa: E402
from tests.accesso_aiuto import postazioni, entra, modalita  # noqa: E402

_CARTELLE = tempfile.mkdtemp(prefix='ft_cache_letture_')
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
        print(f'  [KO] {nome}  {extra}')


def nuovo_ordine(numero, giorni=10, note=''):
    pr = PreventivoManager.create('Cliente Alfa', 'paolo', quantita=1, numero_ordine_cliente=numero)
    pid = pr['id'] if isinstance(pr, dict) else pr
    PreventivoManager.replace_articoli(pid, [
        {'codice': f'{numero}-A', 'quantita': 3, 'materiale': 'S235', 'spessore_mm': 3, 'costo_base_override': 5,
         'lavorazioni': ['piega'], 'pieghe': 2},
        {'codice': f'{numero}-B', 'quantita': 2, 'materiale': 'S235', 'spessore_mm': 2, 'costo_base_override': 5}])
    s = models.SessionLocal()
    s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'INVIATO'
    s.commit()
    s.close()
    oid = PreventivoManager.accetta_e_crea_ordine(
        pid, 'paolo', data_consegna_override=(datetime.utcnow() + timedelta(days=giorni)).date().isoformat()).get('order_id')
    if note:
        s = models.SessionLocal()
        s.query(Order).filter(Order.id == oid).first().note = note
        s.commit()
        s.close()
    return oid


def tablet(c):
    r = c.get('/api/officina/tablet')
    assert r.status_code == 200, (r.status_code, r.get_json())
    return r.get_json()


def main_prove():
    s = models.SessionLocal()
    s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
    s.commit()
    s.close()
    modalita('protetto')
    postazioni()
    ids = [nuovo_ordine(f'C{i:03d}', giorni=5 + i, note=f'nota {i}') for i in range(6)]
    rep = entra(A.app, 'reparto')
    las = entra(A.app, 'laser')

    print('\n1) Stessa risposta del calcolo di sempre')
    A._CACHE_TABLET.svuota()
    d = tablet(rep)
    check('6 ordini', len(d['ordini']) == 6, len(d['ordini']))
    check('uguale al calcolo senza cache', d['ordini'] == __import__('json').loads(
        A.app.response_class(__import__('json').dumps(A._ordini_tablet())).get_data()))

    print('\n2) 10 tablet insieme a cache vuota: un calcolo solo')
    A._CACHE_TABLET.svuota()
    prima = A._CACHE_TABLET.calcoli
    barriera = threading.Barrier(10)
    risposte = []

    def uno():
        c = A.app.test_client()
        c.set_cookie(A.accesso.COOKIE_DISP, rep.segreto)
        barriera.wait(10)
        r = c.get('/api/officina/tablet')
        risposte.append((r.status_code, len((r.get_json() or {}).get('ordini') or [])))
    th = [threading.Thread(target=uno) for _ in range(10)]
    for t in th:
        t.start()
    for t in th:
        t.join(60)
    check('tutti 200 con 6 ordini', risposte == [(200, 6)] * 10, risposte)
    check('un calcolo', A._CACHE_TABLET.calcoli - prima == 1, A._CACHE_TABLET.calcoli - prima)

    print('\n3) Senza cambi: nessun ricalcolo (nemmeno per le scritture dell\'accesso)')
    prima = A._CACHE_TABLET.calcoli
    t = time.perf_counter()
    for _ in range(5):
        tablet(rep)
    ms = (time.perf_counter() - t) * 1000 / 5
    check('nessun ricalcolo', A._CACHE_TABLET.calcoli == prima)
    print(f'     risposta dalla cache: {ms:.1f} ms')
    g = vd.generazione()
    s = models.SessionLocal()
    for dt in s.query(DeviceToken).all():
        dt.last_used_at = datetime.utcnow() - timedelta(minutes=5)
    s.commit()
    s.close()
    entra(A.app, 'reparto')               # un tablet nuovo si registra: solo tabelle dell'accesso
    tablet(rep)
    check('scritture dell\'accesso: la versione dei dati non cambia', vd.generazione() == g, (g, vd.generazione()))
    check('e non si ricalcola', A._CACHE_TABLET.calcoli == prima)

    print('\n4) I cambi si vedono subito')
    s = models.SessionLocal()
    s.query(Order).filter(Order.id == ids[2]).first().note = 'nota cambiata adesso'
    s.commit()
    s.close()
    d = tablet(rep)
    check('nota cambiata visibile subito', any(o['id'] == ids[2] and o['note'] == 'nota cambiata adesso'
                                                for o in d['ordini']))
    r = rep.post(f'/api/orders/{ids[0]}/segnalazioni', json={'tipo': 'disegno', 'nota': 'quota sbagliata'})
    check('segnalazione inviata', r.status_code in (200, 201), r.get_json())
    d = tablet(rep)
    o0 = next(o for o in d['ordini'] if o['id'] == ids[0])
    check('segnalazione visibile subito', any(x.get('nota') == 'quota sbagliata' for x in o0['segnalazioni_oggi']),
          o0['segnalazioni_oggi'])
    r = las.post(f'/api/orders/{ids[1]}/pianifica-taglio', json={'data': (datetime.utcnow() + timedelta(days=2)).date().isoformat()})
    check('pianificato', r.status_code == 200, r.get_json())
    d = tablet(rep)
    check('uguale al calcolo senza cache, dopo i cambi', d['ordini'] == __import__('json').loads(
        A.app.response_class(__import__('json').dumps(A._ordini_tablet())).get_data()))
    nuovo = nuovo_ordine('C999', giorni=1)
    d = tablet(rep)
    check('ordine nuovo visibile subito', any(o['id'] == nuovo for o in d['ordini']))

    print('\n5) Mentre uno ricalcola, gli altri hanno subito l\'ultimo elenco')
    vero = A._ordini_tablet
    partito = threading.Event()

    def lento():
        partito.set()
        time.sleep(1.5)
        return vero()
    A._ordini_tablet = lento
    try:
        vd.avanza()                         # "e' cambiato qualcosa"
        ris = {}

        def primo():
            c = A.app.test_client()
            c.set_cookie(A.accesso.COOKIE_DISP, rep.segreto)
            t0 = time.perf_counter()
            r = c.get('/api/officina/tablet')
            ris['primo'] = (r.status_code, time.perf_counter() - t0)
        t1 = threading.Thread(target=primo)
        t1.start()
        partito.wait(10)
        t0 = time.perf_counter()
        d = tablet(rep)
        altro = time.perf_counter() - t0
        t1.join(30)
        check('l\'altro tablet risponde subito (< 0,5 s)', altro < 0.5, altro)
        check('con l\'ultimo elenco', len(d['ordini']) == 7, len(d['ordini']))
        check('chi ricalcola ha la risposta nuova', ris.get('primo', (0,))[0] == 200, ris)
    finally:
        A._ordini_tablet = vero

    print('\n6) dashboard-live')
    A._CACHE_DASHBOARD.svuota()
    pub = A.app.test_client()
    r = pub.get('/api/dashboard-live')
    d = r.get_json() or {}
    check('200', r.status_code == 200, r.status_code)
    attesi = A._conteggi_dashboard()
    check('stessi conteggi del calcolo', (d['snapshot']['ordini_in_produzione'], d['snapshot']['ordini_pronti'],
                                          d['kanban']['ricevuti']) == attesi, (d, attesi))
    prima = A._CACHE_DASHBOARD.calcoli
    for _ in range(5):
        pub.get('/api/dashboard-live')
    check('senza cambi: nessun ricalcolo', A._CACHE_DASHBOARD.calcoli == prima)
    nuovo_ordine('D001')
    d2 = pub.get('/api/dashboard-live').get_json()
    check('ordine nuovo contato subito', d2['snapshot']['ordini_in_produzione'] == d['snapshot']['ordini_in_produzione'] + 1,
          (d['snapshot'], d2['snapshot']))
    check('ha ricalcolato una volta', A._CACHE_DASHBOARD.calcoli == prima + 1)


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
    shutil.rmtree(_CARTELLE, ignore_errors=True)
    try:
        _ENG.dispose()
        os.remove(_TMP)
    except OSError:
        pass
    return 1 if KO else 0


if __name__ == '__main__':
    sys.exit(main())
