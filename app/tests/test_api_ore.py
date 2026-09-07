"""Test delle API DICHIARAZIONI ORE e dei PERMESSI VERIFICATI DAL SERVER.

Verifica in particolare che i permessi NON dipendano da quello che dichiara il
client (requisito 5): un tablet con scope 'ore' non deve poter fare operazioni
d'ufficio nemmeno chiamando le API direttamente.

Copre: 4 (doppio invio/retry), 5 (nessun falso successo), 6 (input invalidi),
11 (scope insufficiente bloccato lato API), 20 (modifiche concorrenti).

Gira su DATABASE TEMPORANEO. Esecuzione:  python app/tests/test_api_ore.py
"""
import os
import sys
import tempfile
import uuid

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

# Impedisce l'inizializzazione del DB reale all'import di backend.app
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'

# Werkzeug recente non espone __version__, che Flask usa nel test client.
import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User  # noqa: E402
from backend import models_ore  # noqa: E402
from backend.models_ore import Cliente  # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), f'test_api_ore_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend.app import app  # noqa: E402
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


def setup():
    s = models.SessionLocal()
    try:
        s.add(User(id='op1', name='Mario Rossi', role='Operaio Officina', is_active=True))
        s.add(User(id='op2', name='Luca Bianchi', role='Operaio Laser', is_active=True))
        for n in ('Cliente Y', 'Cliente Z'):
            s.add(Cliente(id=str(uuid.uuid4()), nome=n, attivo=True))
        s.commit()
    finally:
        s.close()
    t_ore = crea_token('Tablet officina TEST', 'ore', 'test')['token']
    t_uff = crea_token('PC ufficio TEST', 'ufficio', 'test')['token']
    t_rep = crea_token('Tablet reparto TEST', 'reparto', 'test')['token']
    return t_ore, t_uff, t_rep


def main():
    t_ore, t_uff, t_rep = setup()
    c = app.test_client()
    H = lambda tok: {'X-Device-Token': tok}  # noqa: E731

    # --- 1. Autorizzazione ------------------------------------------------
    print('\n1) Permessi verificati dal server')
    r = c.get('/api/ore/operai')
    check('senza token -> 401', r.status_code == 401, r.status_code)

    r = c.get('/api/ore/operai', headers={'X-Device-Token': 'token-inventato'})
    check('token falso -> 401', r.status_code == 401, r.status_code)

    r = c.get('/api/ore/operai', headers=H(t_rep))
    check('tablet REPARTO non accede alle ore -> 403', r.status_code == 403, r.status_code)

    # Il client dichiara di essere l'impiegata: NON deve contare nulla.
    r = c.post('/api/ore/giornata', headers=H(t_ore), json={
        'operatore_id': 'op1', 'data': '2020-01-02', 'righe': [],
        'user_id': 'elena-impiegata', 'role': 'Impiegata', 'is_capo': True,
    })
    check('user_id/ruolo dichiarati dal client ignorati (giorno passato negato)',
          r.status_code == 403 and r.get_json().get('codice') == 'giorno_non_corrente',
          r.get_json())

    r = c.get('/api/ore/operai', headers=H(t_ore))
    check('tablet ORE accede alle ore -> 200', r.status_code == 200, r.status_code)
    check('elenco operai popolato', len(r.get_json().get('operai', [])) == 2)

    ctx = c.get('/api/ore/contesto', headers=H(t_ore)).get_json()
    oggi = ctx['oggi']
    check('contesto espone oggi e passo', bool(oggi) and ctx.get('passo_minuti') == 30, ctx)

    r = c.get('/api/ore/clienti', headers=H(t_ore))
    check('clienti disponibili', len(r.get_json().get('clienti', [])) == 2)

    # --- 2. Salvataggio ---------------------------------------------------
    print('\n2) Salvataggio giornata dal tablet')
    body = {'operatore_id': 'op1', 'data': oggi, 'revisione_attesa': 0,
            'richiesta_id': 'req-A',
            'righe': [{'cliente': 'Cliente Y', 'minuti': 300},
                      {'cliente': 'Cliente Z', 'minuti': 180}]}
    r = c.post('/api/ore/giornata', headers=H(t_ore), json=body)
    check('salvataggio -> 200', r.status_code == 200, r.status_code)
    g = r.get_json()['giornata']
    check('totale 480 minuti', g['totale_minuti'] == 480, g)
    check('revisione 1', g['revisione'] == 1)

    # --- 3. Idempotenza ---------------------------------------------------
    print('\n3) Doppio invio / retry dopo timeout (stessa richiesta)')
    r = c.post('/api/ore/giornata', headers=H(t_ore), json=body)
    g2 = r.get_json().get('giornata', {})
    check('replay -> 200', r.status_code == 200)
    check('nessun doppio conteggio (480)', g2.get('totale_minuti') == 480, g2)
    check('revisione invariata (1)', g2.get('revisione') == 1, g2)

    # --- 4. Conflitto -----------------------------------------------------
    print('\n4) Modifica concorrente')
    r = c.post('/api/ore/giornata', headers=H(t_ore), json={
        'operatore_id': 'op1', 'data': oggi, 'revisione_attesa': 0,
        'richiesta_id': 'req-B', 'righe': [{'cliente': 'Cliente Y', 'minuti': 60}]})
    check('revisione vecchia -> 409', r.status_code == 409, r.status_code)
    check('409 restituisce lo stato corrente',
          r.get_json().get('giornata', {}).get('totale_minuti') == 480)
    letto = c.get(f'/api/ore/giornata?operatore_id=op1', headers=H(t_ore)).get_json()
    check('dati non sovrascritti', letto['giornata']['totale_minuti'] == 480)

    # --- 5. Validazione ---------------------------------------------------
    print('\n5) Input invalidi respinti dal backend')
    for nome, righe in [
        ('negativi', [{'cliente': 'Cliente Y', 'minuti': -30}]),
        ('non numerici', [{'cliente': 'Cliente Y', 'minuti': 'otto'}]),
        ('cliente inesistente', [{'cliente': 'Ditta Fantasma', 'minuti': 60}]),
        ('oltre la giornata', [{'cliente': 'Cliente Y', 'minuti': 2000}]),
    ]:
        rr = c.post('/api/ore/giornata', headers=H(t_ore), json={
            'operatore_id': 'op1', 'data': oggi, 'revisione_attesa': 1,
            'richiesta_id': f'bad-{nome}', 'righe': righe})
        check(f'respinto: {nome}', rr.status_code == 400, rr.status_code)

    rr = c.post('/api/ore/giornata', headers=H(t_ore), json={
        'operatore_id': 'nessuno', 'data': oggi, 'righe': []})
    check('operatore inesistente respinto', rr.status_code == 403, rr.status_code)

    # --- 6. Regole per dispositivo ---------------------------------------
    print('\n6) Tablet vs Ufficio')
    r = c.get('/api/ore/giornata?operatore_id=op1&data=2020-01-02', headers=H(t_ore))
    check('tablet non consulta giorni passati -> 403', r.status_code == 403, r.status_code)

    r = c.get('/api/ore/giornata?operatore_id=op1&data=2020-01-02', headers=H(t_uff))
    check('ufficio consulta giorni passati -> 200', r.status_code == 200, r.status_code)
    check('giorno mai dichiarato risulta NON dichiarato',
          r.get_json()['giornata']['dichiarata'] is False)

    r = c.post('/api/ore/giornata', headers=H(t_uff), json={
        'operatore_id': 'op1', 'data': '2020-01-02', 'revisione_attesa': 0,
        'richiesta_id': 'uff-1', 'righe': [{'cliente': 'Cliente Y', 'minuti': 120}]})
    check('ufficio corregge un giorno passato -> 200', r.status_code == 200, r.status_code)
    check('origine registrata come ufficio',
          r.get_json()['giornata']['origine'] == 'ufficio')

    # --- 7. Dichiarata a zero != mancante --------------------------------
    print('\n7) Dichiarata a zero diversa da mancante')
    pre = c.get('/api/ore/giornata?operatore_id=op2', headers=H(t_ore)).get_json()
    check('op2 non dichiarata', pre['giornata']['dichiarata'] is False)
    c.post('/api/ore/giornata', headers=H(t_ore), json={
        'operatore_id': 'op2', 'data': oggi, 'revisione_attesa': 0,
        'richiesta_id': 'zero', 'righe': []})
    post = c.get('/api/ore/giornata?operatore_id=op2', headers=H(t_ore)).get_json()
    check('ora dichiarata con 0', post['giornata']['dichiarata'] is True
          and post['giornata']['totale_minuti'] == 0, post)

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
