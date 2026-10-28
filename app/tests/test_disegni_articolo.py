"""Pagina "Disegni Lantek per articolo" (28/10/2026): le rotte del server.

 1. GET /api/lantek/abbinamenti: tutti gli articoli abbinati, raggruppati,
    coi disegni di Lantek e quanti per articolo;
 2. POST: articolo nuovo (senza ordine), cambio, "non va al laser", togli;
    descrizione e cliente restano se chi cambia non li riscrive;
 3. errori: codice vuoto, nessun disegno, quantita' fuori, disegno che in
    Lantek non c'e', Lantek che non risponde; il codice esatto di Lantek;
 4. senza accesso: niente.

Lantek e' FINTO (pezzi_in_lantek sostituita). Database TEMPORANEO:
    python app/tests/test_disegni_articolo.py
"""
import os
import sys
import tempfile
import uuid

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

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

_TMP = os.path.join(tempfile.gettempdir(), f'test_art_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend import abbinamenti_backup as AB  # noqa: E402
from backend import lantek as L  # noqa: E402
from backend.app import app  # noqa: E402

AB.CARTELLA = os.path.join(tempfile.mkdtemp(prefix='test_art_dati_'), 'dati_clienti')
OK = KO = 0

# Lantek finto: questi disegni ci sono (codice esatto com'e' in Lantek)
IN_LANTEK = {'POLIFORM FONDO 563.5X554', 'POLIFORM SQUAD. FISS. SAGOMA', 'POLIFORM ARR. ZOCC. 435 DX'}
LANTEK = {'su': True}


def _pezzi_in_lantek(codici):
    if not LANTEK['su']:
        return {'disponibile': False, 'errore': 'Lantek non risponde', 'pezzi': {}}
    out = {}
    for c in codici:
        vero = next((x for x in IN_LANTEK if x.upper() == str(c).strip().upper()), None)
        out[c] = {'esiste': bool(vero), 'codice_lantek': vero}
    return {'disponibile': True, 'pezzi': out}


L.pezzi_in_lantek = _pezzi_in_lantek


def check(nome, cond, extra=None):
    global OK, KO
    if cond:
        OK += 1
        print(f'  OK  {nome}')
    else:
        KO += 1
        print(f'  KO  {nome} :: {extra!r}'[:500])


def main():
    from tests.accesso_aiuto import stazione_pronta, modalita
    modalita('protetto')
    c = stazione_pronta(app, 'laser')
    art = lambda: (c.get('/api/lantek/abbinamenti').get_json() or {}).get('articoli')  # noqa: E731

    print('1) Elenco vuoto, articolo nuovo senza ordine')
    check('elenco vuoto', art() == [], art())
    r = c.post('/api/lantek/abbinamenti', json={'codice': '05hfom0700', 'cliente': 'Poliform', 'descrizione': 'FONDO METALLO',
                                                 'pezzi': [{'codice': 'poliform fondo 563.5x554', 'quantita': 2}]})
    check('salvato', r.status_code == 200, r.get_json())
    a = art()
    check('in elenco: codice in maiuscolo, disegno col codice esatto di Lantek, x2',
          a == [{'codice': '05HFOM0700', 'descrizione': 'FONDO METALLO', 'cliente': 'Poliform', 'non_laser': False,
                 'pezzi': [{'codice': 'POLIFORM FONDO 563.5X554', 'quantita': 2}], 'da': a[0]['da'], 'il': a[0]['il']}], a)
    check('copia fuori dal database aggiornata', os.path.exists(AB.percorso_json()))

    print('\n2) Cambio, piu\' disegni, descrizione che resta')
    r = c.post('/api/lantek/abbinamenti', json={'codice': '05HFOM0700', 'pezzi': [
        {'codice': 'POLIFORM FONDO 563.5X554', 'quantita': 1}, {'codice': 'POLIFORM ARR. ZOCC. 435 DX', 'quantita': 3},
        {'codice': 'POLIFORM ARR. ZOCC. 435 DX', 'quantita': 5}]})
    a = art()
    check('due disegni (il doppione non conta), quantita\' per articolo',
          r.status_code == 200 and a[0]['pezzi'] == [{'codice': 'POLIFORM ARR. ZOCC. 435 DX', 'quantita': 3},
                                                     {'codice': 'POLIFORM FONDO 563.5X554', 'quantita': 1}], a)
    check('descrizione e cliente restano', a[0]['descrizione'] == 'FONDO METALLO' and a[0]['cliente'] == 'Poliform', a)
    r = c.post('/api/lantek/abbinamenti', json={'codice': '05HVAR1700', 'cliente': 'Poliform', 'non_laser': True})
    a = {x['codice']: x for x in art()}
    check('"non va al laser"', r.status_code == 200 and a['05HVAR1700']['non_laser'] and a['05HVAR1700']['pezzi'] == [], a)

    print('\n3) Errori')
    casi = [
        ({'codice': '', 'pezzi': [{'codice': 'POLIFORM FONDO 563.5X554', 'quantita': 1}]}, 400, 'codice vuoto'),
        ({'codice': 'X1', 'pezzi': []}, 400, 'nessun disegno'),
        ({'codice': 'X1', 'pezzi': [{'codice': 'POLIFORM FONDO 563.5X554', 'quantita': 0}]}, 400, 'quantita\' 0'),
        ({'codice': 'X1', 'pezzi': [{'codice': 'POLIFORM FONDO 563.5X554', 'quantita': 10000}]}, 400, 'quantita\' troppo grande'),
        ({'codice': 'X1', 'pezzi': [{'codice': 'NON ESISTE', 'quantita': 1}]}, 400, 'disegno che in Lantek non c\'e\''),
        ({'codice': 'POLIFORM FONDO 563.5X554', 'pezzi': [{'codice': 'POLIFORM FONDO 563.5X554', 'quantita': 1}]}, 400, 'se stesso'),
    ]
    for corpo, atteso, nome in casi:
        r = c.post('/api/lantek/abbinamenti', json=corpo)
        check(f'{nome}: {atteso}', r.status_code == atteso, (r.status_code, r.get_json()))
    LANTEK['su'] = False
    r = c.post('/api/lantek/abbinamenti', json={'codice': 'X1', 'pezzi': [{'codice': 'POLIFORM FONDO 563.5X554', 'quantita': 1}]})
    check('Lantek non risponde: 503, niente salvato', r.status_code == 503 and 'X1' not in {x['codice'] for x in art()},
          (r.status_code, r.get_json()))
    LANTEK['su'] = True
    check('gli errori non hanno toccato niente', len(art()) == 2, art())

    print('\n4) Togli, e senza accesso')
    r = c.post('/api/lantek/abbinamenti', json={'codice': '05hfom0700', 'dimentica': True})
    check('tolto', r.status_code == 200 and (r.get_json() or {}).get('tolti') == 2
          and [x['codice'] for x in art()] == ['05HVAR1700'], (r.get_json(), art()))
    anon = app.test_client()
    check('senza accesso: elenco no', anon.get('/api/lantek/abbinamenti').status_code in (401, 403))
    check('senza accesso: salva no', anon.post('/api/lantek/abbinamenti', json={'codice': 'X', 'non_laser': True}).status_code in (401, 403))


if __name__ == '__main__':
    try:
        main()
    finally:
        try:
            _ENG.dispose()
            os.remove(_TMP)
        except OSError:
            pass
    print(f'\nPASSATI: {OK}   FALLITI: {KO}')
    sys.exit(1 if KO else 0)
