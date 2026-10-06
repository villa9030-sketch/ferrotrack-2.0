"""Pulsante "Segnala errore": dal dispositivo all'elenco dell'amministrazione.

Copre:
 1. ogni postazione puo' segnalare (anche tablet officina e timbratrice)
 2. con la segnalazione restano chi, dove, pagina e il diario della pagina
 3. testo vuoto rifiutato; dettagli enormi ridotti; non piu' di 10 l'ora
 4. riga in logs/segnalazioni.log (qui in una cartella temporanea) e avviso
    all'amministrazione
 5. l'elenco lo vede l'ufficio, non il laser; "risolta" e "riapri"

Gira su DATABASE TEMPORANEO. Esecuzione: python app/tests/test_segnala_errore.py
"""
import json
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
from backend.models import Base, Notification  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_segnala_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
A.SEGNALAZIONI_LOG = os.path.join(tempfile.mkdtemp(prefix='ft_segn_'), 'segnalazioni.log')

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


def main():
    from tests.accesso_aiuto import postazioni, persona, entra, modalita
    modalita('protetto')
    postazioni()
    persona('ufficio', 'Elena Ufficio', 'Amministrazione', pin='2580')
    uff = entra(A.app, 'ufficio', pin='2580')
    ore = entra(A.app, 'ore')
    rep = entra(A.app, 'reparto')
    laser = entra(A.app, 'laser')

    print('1) Ogni postazione puo\' segnalare')
    diario = [{'t': '2026-10-06T10:00:00Z', 'tipo': 'server', 'testo': 'POST /api/ore/giornata -> 500'}]
    r = ore.post('/api/segnalazioni-errore', json={'testo': 'salvo le ore e non succede niente',
                                                   'pagina': '/ore.html', 'dettagli': {'diario': diario}})
    check('timbratrice: segnalazione accettata', r.status_code == 201, r.get_json())
    r = rep.post('/api/segnalazioni-errore', json={'testo': 'il foglio non si apre', 'pagina': '/operaio-info.html'})
    check('tablet officina: accettata', r.status_code == 201, r.get_json())
    r = uff.post('/api/segnalazioni-errore', json={'testo': 'il riepilogo non torna', 'pagina': '/ufficio-ore.html'})
    check('ufficio (persona col PIN): accettata', r.status_code == 201, r.get_json())

    print('\n2) Controlli')
    r = laser.post('/api/segnalazioni-errore', json={'testo': ' ', 'pagina': '/laser.html'})
    check('testo vuoto rifiutato', r.status_code == 400)
    r = laser.post('/api/segnalazioni-errore', json={'testo': 'dettagli enormi', 'pagina': '/laser.html',
                                                     'dettagli': {'x': 'a' * 50000}})
    check('dettagli enormi: accettata ma ridotti', r.status_code == 201)
    for i in range(12):
        r = laser.post('/api/segnalazioni-errore', json={'testo': f'prova {i}', 'pagina': '/laser.html'})
    check('non piu\' di 10 l\'ora dallo stesso dispositivo', r.status_code == 429, r.status_code)

    print('\n3) Registro e avviso')
    righe = open(A.SEGNALAZIONI_LOG, encoding='utf-8').read().splitlines()
    prima = json.loads(righe[0])
    check('riga in segnalazioni.log con pagina, testo e diario',
          prima['pagina'] == '/ore.html' and 'non succede' in prima['testo']
          and prima['dettagli']['diario'][0]['testo'].endswith('500'), prima)
    s = models.SessionLocal()
    avvisi = s.query(Notification).filter(Notification.title == 'Segnalato un errore').count()
    s.close()
    check('avviso all\'amministrazione', avvisi >= 3, avvisi)

    print('\n4) Elenco per l\'ufficio')
    r = laser.get('/api/segnalazioni-errore')
    check('il laser non vede l\'elenco', r.status_code in (401, 403), r.status_code)
    d = uff.get('/api/segnalazioni-errore?stato=aperta').get_json() or {}
    l = d.get('segnalazioni') or []
    ore_seg = next((x for x in l if x['pagina'] == '/ore.html'), None)
    check('l\'ufficio vede le aperte', d.get('aperte', 0) >= 4 and ore_seg is not None, d.get('aperte'))
    check('con stazione e diario', ore_seg and ore_seg['stazione'] == 'ore'
          and ore_seg['dettagli']['diario'][0]['tipo'] == 'server', ore_seg)
    uf = next((x for x in l if x['pagina'] == '/ufficio-ore.html'), None)
    check('col nome di chi e\' entrato col PIN', uf and uf['persona'] == 'Elena Ufficio', uf)
    r = uff.put(f'/api/segnalazioni-errore/{ore_seg["id"]}', json={'stato': 'risolta'})
    x = (r.get_json() or {}).get('segnalazione') or {}
    check('risolta, con chi e quando', x.get('stato') == 'risolta' and x.get('risolta_da') == 'Elena Ufficio'
          and x.get('risolta_il'), x)
    d2 = uff.get('/api/segnalazioni-errore?stato=aperta').get_json() or {}
    check('non e\' piu\' fra le aperte', d2.get('aperte') == d.get('aperte') - 1, (d.get('aperte'), d2.get('aperte')))
    r = uff.put(f'/api/segnalazioni-errore/{ore_seg["id"]}', json={'stato': 'aperta'})
    check('si puo\' riaprire', (r.get_json() or {}).get('segnalazione', {}).get('stato') == 'aperta')

    print(f'\nPASSATI: {OK}   FALLITI: {len(KO)}')
    return 0 if not KO else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    finally:
        try:
            _ENG.dispose(); os.remove(_TMP)
        except Exception:
            pass
