"""Prova della correzione dello spessore in Lantek (28/10/2026, punto 5).

 1. il prossimo passo: niente -> "crea"; FT-PROVA-30 a 2 mm -> "reimporta";
    altrimenti nessuno (prova fatta, o Lantek non letto);
 2. l'esito: creato / aggiornato / doppione / rifiutato;
 3. le rotte: passo sbagliato rifiutato e Procesos mai lanciato; i due passi
    in ordine con Lantek FINTO (Procesos sostituito); esito salvato; senza
    accesso niente.

Database TEMPORANEO, Lantek FINTO:
    python app/tests/test_prova_spessore.py
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

_TMP = os.path.join(tempfile.gettempdir(), f'test_ps_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend import lantek as L  # noqa: E402
from backend import lantek_prova_spessore as PS  # noqa: E402
import backend.app  # noqa: E402,F401
A = sys.modules['backend.app']

A.UPLOAD_FOLDER = tempfile.mkdtemp(prefix='test_ps_up_')
OK = KO = 0


def check(nome, cond, extra=None):
    global OK, KO
    if cond:
        OK += 1
        print(f'  OK  {nome}')
    else:
        KO += 1
        print(f'  KO  {nome} :: {extra!r}'[:600])


def foto(*pezzi, ok=True):
    return {'ok': ok, 'errore': None if ok else 'giu', 'pezzi': [
        {'codice': c, 'tipo': 0, 'materiale': 'FERRO', 'spessore': s, 'creato': '2026-10-28 16:00:00'} for c, s in pezzi]}


def main():
    print('1) Il prossimo passo')
    check('niente in Lantek: crea', PS.prossimo_passo(foto()) == 'crea')
    check('FT-PROVA-30 a 2 mm: reimporta', PS.prossimo_passo(foto(('FT-PROVA-30', 2.0))) == 'reimporta')
    check('a 3 mm: prova fatta', PS.prossimo_passo(foto(('FT-PROVA-30', 3.0))) is None)
    check('doppione: niente', PS.prossimo_passo(foto(('FT-PROVA-30', 2.0), ('FT-PROVA-30-1', 3.0))) is None)
    check('Lantek non letto: niente', PS.prossimo_passo(foto(ok=False)) is None)

    print('\n2) L\'esito')
    e = PS.esito
    check('crea: creato', e('crea', foto(), foto(('FT-PROVA-30', 2.0)))['parola'] == 'creato')
    check('crea: non creato', e('crea', foto(), foto())['parola'] == 'non_creato')
    check('reimporta: aggiornato', e('reimporta', foto(('FT-PROVA-30', 2.0)), foto(('FT-PROVA-30', 3.0)))['parola'] == 'aggiornato')
    check('reimporta: doppione', e('reimporta', foto(('FT-PROVA-30', 2.0)),
                                   foto(('FT-PROVA-30', 2.0), ('FT-PROVA-30-1', 3.0)))['parola'] == 'doppione')
    check('reimporta: rifiutato', e('reimporta', foto(('FT-PROVA-30', 2.0)), foto(('FT-PROVA-30', 2.0)))['parola'] == 'rifiutato')

    print('\n3) Le rotte')
    from tests.accesso_aiuto import stazione_pronta, modalita
    modalita('protetto')
    c = stazione_pronta(A.app, 'laser')
    lantek = {'pezzi': [], 'lanci': []}
    PS.fotografia = lambda: foto(*lantek['pezzi'])
    L.procesos_disponibile = lambda: True
    L.expert_aperto = lambda: False

    def procesos(pezzi, cartella, ud=None, attesa_s=900):
        lantek['lanci'].append(pezzi)
        p = pezzi[0]
        if not lantek['pezzi']:
            lantek['pezzi'] = [(p['codice'], p['spessore'])]
        else:
            lantek['pezzi'] = [(p['codice'], p['spessore'])]      # Lantek "aggiorna"
        return {'eseguito': True, 'errore': None}
    L.importa_pezzi_dxf = procesos

    g = c.get('/api/lantek/prova-spessore').get_json() or {}
    check('GET: prossimo passo "crea"', g.get('prossimo') == 'crea' and g.get('codice') == 'FT-PROVA-30', g)
    r = c.post('/api/lantek/prova-spessore', json={'passo': 'reimporta'})
    check('passo sbagliato: 409, Procesos mai lanciato', r.status_code == 409 and not lantek['lanci'], r.get_json())
    r = c.post('/api/lantek/prova-spessore', json={'passo': 'crea'})
    d = r.get_json() or {}
    check('passo 1: creato a 2 mm, FERRO, dal DXF di prova',
          r.status_code == 200 and d['esito']['parola'] == 'creato' and lantek['lanci'][0][0]['spessore'] == 2.0
          and lantek['lanci'][0][0]['materiale'] == 'FERRO' and os.path.isfile(lantek['lanci'][0][0]['dxf']), d)
    r = c.post('/api/lantek/prova-spessore', json={'passo': 'reimporta'})
    d = r.get_json() or {}
    check('passo 2: reimportato a 3 mm, "aggiornato"',
          r.status_code == 200 and d['esito']['parola'] == 'aggiornato' and lantek['lanci'][1][0]['spessore'] == 3.0, d)
    g = c.get('/api/lantek/prova-spessore').get_json() or {}
    check('esiti salvati, niente altri passi', [x['esito']['parola'] for x in g.get('storico') or []] == ['creato', 'aggiornato']
          and g.get('prossimo') is None, g)
    anon = A.app.test_client()
    check('senza accesso: no', anon.get('/api/lantek/prova-spessore').status_code in (401, 403)
          and anon.post('/api/lantek/prova-spessore', json={'passo': 'crea'}).status_code in (401, 403))


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
