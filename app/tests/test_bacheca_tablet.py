"""Test della BACHECA della timbratrice e del tablet di REPARTO.

Il tablet fisso alla timbratrice mostra, per ogni operaio, se ha registrato,
quante ore e per quali clienti. Gli altri due tablet servono solo a guardare
gli ordini: non devono poter leggere ne' scrivere le ore.

Copre:
 1. la bacheca elenca tutti gli operai, anche chi non ha ancora registrato
 2. "registrato zero" resta distinto da "non ha registrato"
 3. le righe mostrano cliente e minuti, con le attivita' interne separate
 4. il tablet della timbratrice vede solo la giornata di oggi
 5. il tablet di reparto NON accede alle ore, e il suo endpoint di identita'
    non rivela nulla di piu' del necessario
 6. l'ufficio puo' rileggere una giornata passata

Gira su DATABASE TEMPORANEO. Esecuzione: python app/tests/test_bacheca_tablet.py
"""
import os
import sys
import tempfile
import uuid
from datetime import timedelta

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
from backend.models import Base, User  # noqa: E402
from backend import models_ore  # noqa: E402
from backend.models_ore import Cliente, OreAttese  # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), f'test_bacheca_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend.app import app  # noqa: E402
from backend.auth_device import crea_token  # noqa: E402
from backend import ore_service as svc  # noqa: E402

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


OGGI = svc.oggi_locale()
IERI = OGGI - timedelta(days=1)


def setup():
    s = models.SessionLocal()
    try:
        for uid, nome in (('enzo', 'Enzo Bianchi'), ('mirko', 'Mirko Verdi'),
                          ('gino', 'Gino Rossi')):
            s.add(User(id=uid, name=nome, role='Operaio Officina', is_active=True))
            s.add(OreAttese(id=str(uuid.uuid4()), operatore_id=uid,
                            tenuto_alla_compilazione=True, minuti_attesi=480,
                            giorni_settimana=[1, 2, 3, 4, 5, 6, 7]))
        for n in ('Cliente Alfa', 'Cliente Beta'):
            s.add(Cliente(id=str(uuid.uuid4()), nome=n, attivo=True))
        s.commit()
    finally:
        s.close()
    return (crea_token('Tablet timbratrice', 'ore', 'test')['token'],
            crea_token('Tablet reparto', 'reparto', 'test')['token'],
            crea_token('PC ufficio', 'ufficio', 'test')['token'])


def voce(dati, nome):
    for v in dati:
        if v['nome'] == nome:
            return v
    return None


def main():
    t_ore, t_rep, t_uff = setup()
    c = app.test_client()
    H = lambda tok: {'X-Device-Token': tok}  # noqa: E731

    # Enzo: giornata piena. Gino: registrata a ZERO. Mirko: non registra nulla.
    c.post('/api/ore/giornata', headers=H(t_ore), json={
        'operatore_id': 'enzo', 'data': OGGI.isoformat(),
        'righe': [{'cliente': 'Cliente Alfa', 'minuti': 300},
                  {'cliente': 'Cliente Beta', 'minuti': 120},
                  {'attivita_interna': True, 'minuti': 60}],
        'revisione_attesa': 0, 'richiesta_id': 'b1'})
    c.post('/api/ore/giornata', headers=H(t_ore), json={
        'operatore_id': 'gino', 'data': OGGI.isoformat(), 'righe': [],
        'revisione_attesa': 0, 'richiesta_id': 'b2', 'scostamento_confermato': True})

    # =====================================================================
    print('\n1) La bacheca elenca tutti, anche chi non ha registrato')
    r = c.get('/api/ore/bacheca', headers=H(t_ore))
    check('risposta ok', r.status_code == 200, r.status_code)
    dati = r.get_json()['operai']
    check('ci sono tutti e tre', len(dati) == 3, [v['nome'] for v in dati])
    check('ordinati per nome',
          [v['nome'] for v in dati] == sorted(v['nome'] for v in dati), dati)

    e = voce(dati, 'Enzo Bianchi')
    check('Enzo: registrato', e['dichiarata'] is True)
    check('Enzo: totale 8 h', e['totale_minuti'] == 480, e['totale_minuti'])
    check('Enzo: obiettivo mostrato', e['minuti_attesi'] == 480, e)

    m = voce(dati, 'Mirko Verdi')
    check('Mirko: NON registrato', m['dichiarata'] is False)
    check('Mirko: nessuna riga', m['righe'] == [], m['righe'])
    check('Mirko: totale zero', m['totale_minuti'] == 0)

    # =====================================================================
    print('\n2) Registrato a zero non e la stessa cosa di non registrato')
    g = voce(dati, 'Gino Rossi')
    check('Gino: risulta REGISTRATO', g['dichiarata'] is True, g)
    check('Gino: totale zero', g['totale_minuti'] == 0, g)
    check('i due casi sono distinguibili',
          g['dichiarata'] != m['dichiarata'] and g['totale_minuti'] == m['totale_minuti'])
    check('Gino: lo zero risulta confermato', g['scostamento_confermato'] is True, g)

    # =====================================================================
    print('\n3) Righe: cliente, minuti, interne separate')
    righe = e['righe']
    check('tre righe', len(righe) == 3, righe)
    clienti = [x for x in righe if not x['attivita_interna']]
    interne = [x for x in righe if x['attivita_interna']]
    check('due clienti + una interna', len(clienti) == 2 and len(interne) == 1, righe)
    check('i clienti hanno un nome', all(x['cliente'] for x in clienti), clienti)
    check('la riga interna non ha cliente', interne[0]['cliente'] is None, interne)
    check('i clienti vengono prima delle interne',
          righe[-1]['attivita_interna'] is True, righe)
    check('somma coerente col totale',
          sum(x['minuti'] for x in righe) == e['totale_minuti'])

    # =====================================================================
    print('\n4) Il tablet della timbratrice vede solo oggi')
    r = c.get(f'/api/ore/bacheca?data={IERI.isoformat()}', headers=H(t_ore))
    check('ieri rifiutato al tablet (403)', r.status_code == 403, r.status_code)
    r = c.get(f'/api/ore/bacheca?data={OGGI.isoformat()}', headers=H(t_ore))
    check('oggi consentito', r.status_code == 200)

    # =====================================================================
    print('\n5) Il tablet di reparto non tocca le ore')
    for percorso in ('/api/ore/bacheca', '/api/ore/operai', '/api/ore/clienti',
                     '/api/ore/giornata?operatore_id=enzo'):
        r = c.get(percorso, headers=H(t_rep))
        check(f'reparto respinto su {percorso}', r.status_code == 403, r.status_code)
    r = c.post('/api/ore/giornata', headers=H(t_rep), json={
        'operatore_id': 'enzo', 'data': OGGI.isoformat(),
        'righe': [{'cliente': 'Cliente Alfa', 'minuti': 60}],
        'revisione_attesa': 1, 'richiesta_id': 'x'})
    check('reparto non puo scrivere ore', r.status_code == 403, r.status_code)

    r = c.get('/api/ore/dispositivo', headers=H(t_rep))
    check('ma sa dire chi e (200)', r.status_code == 200, r.status_code)
    d = r.get_json()
    check('espone scope ed etichetta',
          d.get('scope') == 'reparto' and d.get('dispositivo') == 'Tablet reparto', d)
    check('non espone il token', 'token' not in d, d)
    r = c.get('/api/ore/dispositivo')
    check('senza token respinto (401)', r.status_code == 401, r.status_code)

    # verifica che le ore non siano state toccate
    r = c.get('/api/ore/bacheca', headers=H(t_ore))
    check('le ore di Enzo sono intatte',
          voce(r.get_json()['operai'], 'Enzo Bianchi')['totale_minuti'] == 480)

    # =====================================================================
    print('\n6) L ufficio puo rileggere un giorno passato')
    r = c.get(f'/api/ore/bacheca?data={IERI.isoformat()}', headers=H(t_uff))
    check('ufficio legge ieri (200)', r.status_code == 200, r.status_code)
    ieri = r.get_json()['operai']
    check('ieri nessuno aveva registrato',
          all(v['dichiarata'] is False for v in ieri), ieri)
    check('la data risposta e quella chiesta',
          r.get_json()['data'] == IERI.isoformat())

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
