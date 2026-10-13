"""Ore: due postazioni salvano la STESSA giornata nello stesso istante.

Dalla prova notturna (12/10): due timbratrici con la stessa revisione
passavano tutte e due (200 + 200: le ore della prima sparivano senza che
nessuno lo sapesse) e a volte usciva un 500. Ora il salvataggio fa un
confronta-e-scrivi atomico sulla revisione: una vince (200), l'altra riceve
409 con i dati aggiornati, mai un 500 o "database is locked".

Gira su DATABASE TEMPORANEO (file vero, per avere la concorrenza di SQLite).
Esecuzione:  python app/tests/test_ore_concorrenza.py
"""
import os
import sys
import tempfile
import threading
import uuid

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
from backend.models import Base, User  # noqa: E402
from backend.models_ore import Cliente, GiornataOre, VersioneGiornataOre  # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), f'test_ore_conc_{uuid.uuid4().hex[:8]}.db')
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
from backend import ore_service as svc  # noqa: E402
from backend.auth_device import crea_token  # noqa: E402

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
        except Exception as e:  # un'eccezione qui e' gia' un errore
            out[i] = ('eccezione', repr(e))
    th = [threading.Thread(target=corri, args=(i, f)) for i, f in enumerate(funzioni)]
    for t in th:
        t.start()
    for t in th:
        t.join(60)
    return out


def setup():
    s = models.SessionLocal()
    try:
        for i in range(12):
            s.add(User(id=f'op{i}', name=f'Operaio {i}', role='Operaio Officina', is_active=True))
        s.add(Cliente(id=str(uuid.uuid4()), nome='Cliente Y', attivo=True))
        s.commit()
    finally:
        s.close()


def rev_di(op, giorno):
    s = models.SessionLocal()
    try:
        g = s.query(GiornataOre).filter(GiornataOre.operatore_id == op,
                                        GiornataOre.data == giorno).first()
        n_ver = s.query(VersioneGiornataOre).filter(
            VersioneGiornataOre.giornata_id == g.id).count() if g else 0
        return (int(g.revisione) if g else 0), n_ver
    finally:
        s.close()


def main():
    setup()
    oggi = svc.oggi_locale()

    print('\n1) Servizio: stessa revisione da due postazioni, 20 giri')
    esiti_ko = []
    for giro in range(20):
        op = f'op{giro % 12}'
        rev, _ = rev_di(op, oggi)

        def salva(minuti):
            return svc.salva_giornata(op, oggi.isoformat(),
                                      [{'cliente': 'Cliente Y', 'minuti': minuti}],
                                      origine='tablet', revisione_attesa=rev,
                                      richiesta_id=uuid.uuid4().hex)
        r = insieme([lambda: salva(240), lambda: salva(480)])
        vinti = [x for x in r if isinstance(x, dict) and x.get('success')]
        conflitti = [x for x in r if isinstance(x, dict) and x.get('codice') == 'conflitto']
        if not (len(vinti) == 1 and len(conflitti) == 1):
            esiti_ko.append((giro, r))
            continue
        nuova, _ = rev_di(op, oggi)
        if nuova != rev + 1 or conflitti[0]['giornata'].get('revisione') != rev + 1:
            esiti_ko.append((giro, 'revisione', rev, nuova))
    check('ogni giro: una vince e l\'altra ha il conflitto, revisione +1',
          not esiti_ko, esiti_ko[:3])

    print('\n2) Servizio: stesso doppio invio (stessa richiesta_id) insieme')
    op = 'op11'
    rev, nver = rev_di(op, oggi)
    rid = uuid.uuid4().hex

    def stessa():
        return svc.salva_giornata(op, oggi.isoformat(), [{'cliente': 'Cliente Y', 'minuti': 120}],
                                  origine='tablet', revisione_attesa=rev, richiesta_id=rid)
    r = insieme([stessa, stessa])
    check('tutte e due 200 (la seconda e\' idempotente, non un conflitto)',
          all(isinstance(x, dict) and x.get('success') for x in r), r)
    nuova, nver2 = rev_di(op, oggi)
    check('applicata una volta sola', nuova == rev + 1 and nver2 == nver + 1, (rev, nuova, nver, nver2))

    print('\n3) API: due timbratrici insieme -> una 200 e una 409, mai 500')
    t_ore = [crea_token(f'Tablet ore {i}', 'ore', 'test')['token'] for i in range(2)]
    codici = []
    for giro in range(10):
        op = f'op{giro}'
        c0 = app.test_client()
        g = c0.get(f'/api/ore/giornata?operatore_id={op}&data={oggi.isoformat()}',
                   headers={'X-Device-Token': t_ore[0]}).get_json()
        rev = (g.get('giornata') or {}).get('revisione', 0)

        def post(i, minuti):
            def f():
                c = app.test_client()
                rr = c.post('/api/ore/giornata', headers={'X-Device-Token': t_ore[i]}, json={
                    'operatore_id': op, 'data': oggi.isoformat(), 'revisione_attesa': rev,
                    'richiesta_id': uuid.uuid4().hex,
                    'righe': [{'cliente': 'Cliente Y', 'minuti': minuti}],
                    'scostamento_confermato': True})
                return rr.status_code, rr.get_json()
            return f
        r = insieme([post(0, 240), post(1, 480)])
        codici.append(sorted(x[0] if isinstance(x, tuple) else x for x in r))
    check('ogni coppia: [200, 409]', all(c == [200, 409] for c in codici), codici)

    print(f'\nRisultato: {OK} ok, {len(KO)} falliti')
    try:
        _ENG.dispose()
        os.remove(_TMP)
    except OSError:
        pass
    return 1 if KO else 0


if __name__ == '__main__':
    sys.exit(main())
