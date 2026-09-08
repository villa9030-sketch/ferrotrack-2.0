"""Test del CICLO AMMINISTRATIVO DEGLI ORDINI (sezione 7).

L'operaio comunica a voce: solo l'ufficio registra i passaggi. E i passaggi
sono quattro fatti DIVERSI, che non devono essere confusi in uno solo:
lavorazione finita, DDT preparato, merce consegnata, pratica chiusa.

Copre:
 1. le quattro viste, compreso lo storico precedente all'intervento
 2. le transizioni sono riservate all'ufficio anche via chiamata API diretta
 3. i quattro fatti restano distinti e in sequenza
 4. consegne parziali: un ordine con residuo non si archivia
 5. correzioni (annulla completamento, riapri) senza stati incoerenti
 6. i tablet di reparto smettono di mostrare cio' che e' stato completato

Gira su DATABASE TEMPORANEO. Esecuzione: python app/tests/test_ordini_ufficio.py
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
from backend.models import Base, User, Order  # noqa: E402
from backend import models_ore  # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), f'test_ord_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend.app import app  # noqa: E402
from backend import ordini_service as osv  # noqa: E402
from backend.database import OrderManager  # noqa: E402

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
        s.add(User(id='elena', name='Elena Colombo', role='Impiegata', is_active=True))
        s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina',
                   is_active=True, is_capo=True))
        s.add(User(id='enzo', name='Enzo Bianchi', role='Operaio Officina',
                   is_active=True))
        now = datetime.utcnow()
        base = dict(cliente='Cliente Alfa', data_ricezione=now - timedelta(days=3),
                    data_consegna=now + timedelta(days=7))
        s.add(Order(id='o-aperto', numero_ordine='A-1', status='RICEVUTO', **base))
        s.add(Order(id='o-aperto2', numero_ordine='A-2', status='IN_LAVORAZIONE', **base))
        # Storico: chiuso dall'officina PRIMA di questo intervento, senza le nuove date
        s.add(Order(id='o-storico', numero_ordine='S-1', status='DA_FATTURARE', **base))
        s.add(Order(id='o-chiuso', numero_ordine='C-1', status='CHIUSO', **base))
        s.commit()
    finally:
        s.close()


def fasi():
    return {o['id']: o['fase'] for o in osv.elenco()['ordini']}


def main():
    setup()
    c = app.test_client()

    # =====================================================================
    print('\n1) Le quattro viste, storico compreso')
    d = osv.elenco()
    f = fasi()
    check('ordine nuovo -> aperto', f['o-aperto'] == 'aperto', f)
    check('in lavorazione -> aperto', f['o-aperto2'] == 'aperto', f)
    check('storico DA_FATTURARE -> pronto per DDT (non fra gli aperti)',
          f['o-storico'] == 'pronto_ddt', f)
    check('CHIUSO -> archivio', f['o-chiuso'] == 'archivio', f)
    check('conteggi di tutte e quattro le viste',
          d['conteggi'] == {'aperto': 2, 'pronto_ddt': 1, 'consegnato': 0, 'archivio': 1},
          d['conteggi'])
    check('etichette in italiano', d['etichette']['pronto_ddt'] == 'Pronti per DDT')
    check('nessuna fase produttiva nella riga',
          all(k not in d['ordini'][0] for k in ('fase_corrente', 'processing_steps')),
          list(d['ordini'][0]))

    # =====================================================================
    print('\n2) Le transizioni sono riservate all ufficio')
    for chi, atteso in (('enzo', 403), ('', 403), ('non-esiste', 403)):
        r = c.post('/api/ordini/o-aperto/completamento', json={'user_id': chi})
        check(f'"{chi or "senza utente"}" respinto', r.status_code == atteso, r.status_code)
    check('lo stato non e cambiato', fasi()['o-aperto'] == 'aperto')

    r = c.post('/api/ordini/o-aperto/completamento', json={'user_id': 'elena'})
    check('l impiegata puo registrare (200)', r.status_code == 200, r.get_json())
    r = c.post('/api/ordini/o-aperto2/completamento', json={'user_id': 'paolo'})
    check('anche il capo puo registrare', r.status_code == 200, r.status_code)

    # =====================================================================
    print('\n3) I quattro fatti restano distinti e in sequenza')
    f = fasi()
    check('dopo il completamento -> pronto per DDT', f['o-aperto'] == 'pronto_ddt', f)
    riga = [x for x in osv.elenco()['ordini'] if x['id'] == 'o-aperto'][0]
    check('registra QUANDO e CHI',
          bool(riga['completamento']) and riga['completato_da_id'] == 'elena'
          and riga['completato_da'] == 'Elena Colombo', riga)
    check('ma non inventa un DDT', riga['ddt_numero'] is None)
    check('ne una consegna', riga['consegna'] is None)

    r = c.post('/api/ordini/o-aperto2/chiudi', json={'user_id': 'elena'})
    check('non si chiude senza consegna (409)', r.status_code == 409, r.status_code)

    s = models.SessionLocal()
    try:
        s.add(Order(id='o-salto', numero_ordine='X-1', cliente='Cliente Beta',
                    status='RICEVUTO', data_ricezione=datetime.utcnow(),
                    data_consegna=datetime.utcnow()))
        s.commit()
    finally:
        s.close()
    r = c.post('/api/ordini/o-salto/ddt', json={'user_id': 'elena', 'numero': 'DDT 9'})
    check('non si registra un DDT prima del completamento', r.status_code == 409, r.status_code)
    r = c.post('/api/ordini/o-salto/consegna', json={'user_id': 'elena'})
    check('ne una consegna', r.status_code == 409, r.status_code)

    r = c.post('/api/ordini/o-aperto/ddt', json={'user_id': 'elena', 'numero': ''})
    check('numero DDT obbligatorio', r.status_code == 409, r.status_code)
    r = c.post('/api/ordini/o-aperto/ddt', json={'user_id': 'elena', 'numero': 'DDT 128'})
    check('DDT registrato (200)', r.status_code == 200, r.get_json())
    check('DDT sposta la vista a consegnato', fasi()['o-aperto'] == 'consegnato')

    r = c.post('/api/ordini/o-aperto/consegna', json={'user_id': 'elena', 'completa': True})
    check('consegna registrata', r.status_code == 200, r.status_code)
    r = c.post('/api/ordini/o-aperto/chiudi', json={'user_id': 'elena'})
    check('ora la pratica si chiude', r.status_code == 200, r.get_json())
    check('e finisce in archivio', fasi()['o-aperto'] == 'archivio')

    # =====================================================================
    print('\n4) Consegne parziali: un residuo non e una pratica chiusa')
    r = c.post('/api/ordini/o-aperto2/consegna',
               json={'user_id': 'elena', 'completa': False, 'note': ''})
    check('parziale senza spiegazione rifiutata', r.status_code == 409, r.status_code)
    r = c.post('/api/ordini/o-aperto2/consegna',
               json={'user_id': 'elena', 'completa': False, 'note': 'mancano 12 staffe'})
    check('parziale con nota accettata', r.status_code == 200, r.get_json())
    riga = [x for x in osv.elenco()['ordini'] if x['id'] == 'o-aperto2'][0]
    check('segnata come parziale', riga['consegna_parziale'] is True, riga)
    check('con la nota del residuo', 'staffe' in (riga['note_consegna'] or ''), riga)
    r = c.post('/api/ordini/o-aperto2/chiudi', json={'user_id': 'elena'})
    check('NON archiviabile con residuo (409)', r.status_code == 409, r.status_code)
    check('lo dice in chiaro', 'residuo' in (r.get_json().get('error') or ''), r.get_json())

    r = c.post('/api/ordini/o-aperto2/consegna', json={'user_id': 'elena', 'completa': True})
    check('completata la consegna', r.status_code == 200)
    r = c.post('/api/ordini/o-aperto2/chiudi', json={'user_id': 'elena'})
    check('ora si archivia', r.status_code == 200, r.get_json())

    # =====================================================================
    print('\n5) Correzioni senza stati incoerenti')
    r = c.post('/api/ordini/o-salto/completamento', json={'user_id': 'elena'})
    check('completamento registrato', r.status_code == 200)
    r = c.post('/api/ordini/o-salto/completamento', json={'user_id': 'elena'})
    check('ripeterlo non rompe nulla', r.status_code == 200
          and r.get_json().get('gia_registrato') is True, r.get_json())
    r = c.delete('/api/ordini/o-salto/completamento?user_id=elena')
    check('si puo annullare', r.status_code == 200, r.get_json())
    check('torna fra gli aperti', fasi()['o-salto'] == 'aperto')
    r = c.delete('/api/ordini/o-salto/completamento?user_id=enzo')
    check('ma non da un operaio', r.status_code == 403, r.status_code)

    c.post('/api/ordini/o-salto/completamento', json={'user_id': 'elena'})
    c.post('/api/ordini/o-salto/ddt', json={'user_id': 'elena', 'numero': 'DDT 200'})
    r = c.delete('/api/ordini/o-salto/completamento?user_id=elena')
    check('non si annulla il completamento con un DDT gia registrato',
          r.status_code == 409, r.status_code)

    r = c.post('/api/ordini/o-chiuso/riapri', json={'user_id': 'elena'})
    check('un archiviato si puo riaprire', r.status_code == 200, r.get_json())
    check('e torna disponibile', fasi()['o-chiuso'] != 'archivio', fasi())
    r = c.post('/api/ordini/o-salto/riapri', json={'user_id': 'elena'})
    check('riaprire cio che non e archiviato viene rifiutato', r.status_code == 409)

    r = c.post('/api/ordini/non-esiste/completamento', json={'user_id': 'elena'})
    check('ordine inesistente -> 404', r.status_code == 404, r.status_code)

    # =====================================================================
    print('\n6) I tablet di reparto non mostrano cio che e stato completato')
    ordini = {o['id']: o for o in OrderManager.get_all_orders_dict()}
    check('la fase viaggia con l ordine', 'fase' in ordini['o-aperto2'], list(ordini['o-aperto2']))
    aperti = [o for o in ordini.values() if o.get('fase') == 'aperto']
    check('un ordine completato non e piu "aperto"',
          all(o['id'] != 'o-aperto' for o in aperti), [o['id'] for o in aperti])
    check('un ordine ancora da fare resta visibile',
          any(o['id'] == 'o-salto' for o in ordini.values()))

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
