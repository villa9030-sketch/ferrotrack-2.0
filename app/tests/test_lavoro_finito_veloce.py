"""Test del "Lavoro finito" veloce dell'ufficio (16/10/2026).

I ragazzi dell'officina non segnano niente sul tablet: quando finiscono lo
dicono all'ufficio, che lo registra in un passo (Ctrl+K, riga, pannello,
scelta multipla). Qui si prova il lato server:

 1. "Segna finito" con nota (chi l'ha detto) e cosa risulta ancora aperto
    (taglio non segnato, da procurare non arrivato, fasi aperte): avviso, non blocco
 2. gia' finito: niente cambia, la nota del primo resta, `gia_fatto`
 3. Annulla: torna in lavorazione e la nota se ne va
 4. "Segna finiti" su piu' ordini: ognuno atomico, gia' finiti e inesistenti non fermano gli altri
 5. la nota scritta dopo: solo su un ordine finito, lunghezza massima, nello storico
 6. le rotte nuove sono solo dell'ufficio

Gira su DATABASE TEMPORANEO. Esecuzione: python app/tests/test_lavoro_finito_veloce.py
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
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Order, ProcessingStep, DaProcurare, AuditLog  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_finito_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend.app import app  # noqa: E402
from backend import ordini_service as osv  # noqa: E402

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
        now = datetime.utcnow()
        base = dict(cliente='Officine Prova', data_ricezione=now - timedelta(days=3),
                    data_consegna=now + timedelta(days=7), status='RICEVUTO')
        # tagliato, niente in sospeso: nessun avviso
        s.add(Order(id='o-pulito', numero_ordine='1302', taglio_richiesto=True, taglio_completato=True,
                    data_taglio_completato=now - timedelta(hours=3), **base))
        # va al laser ma il taglio non e' segnato, 2 voci da procurare, una fase aperta
        s.add(Order(id='o-aperto', numero_ordine='1288', taglio_richiesto=True, taglio_completato=False, **base))
        s.add(DaProcurare(order_id='o-aperto', chiave='tub:1', tipo='tubo', quantita=4, stato='ordinato'))
        s.add(DaProcurare(order_id='o-aperto', chiave='tub:2', tipo='tubo', quantita=2, stato='da_ordinare'))
        s.add(DaProcurare(order_id='o-aperto', chiave='tub:3', tipo='tubo', quantita=2, stato='arrivato'))
        s.add(ProcessingStep(id='ps-1', order_id='o-aperto', fase='PIEGA', timestamp_inizio=now - timedelta(hours=1)))
        # senza laser
        s.add(Order(id='o-a', numero_ordine='1297', taglio_richiesto=False, **base))
        s.add(Order(id='o-b', numero_ordine='1305', taglio_richiesto=False, **base))
        s.add(Order(id='o-c', numero_ordine='1310', taglio_richiesto=False, **base))
        s.add(Order(id='o-gia', numero_ordine='1250', taglio_richiesto=False,
                    data_completamento_operativo=now - timedelta(days=1), completato_operativo_da='elena',
                    nota_completamento='Paolo al telefono', **dict(base, status='DA_FATTURARE')))
        s.commit()
    finally:
        s.close()


def riga(oid):
    return [x for x in osv.elenco()['ordini'] if x['id'] == oid][0]


def audit(oid, azione):
    s = models.SessionLocal()
    try:
        return [a.details if hasattr(a, 'details') else getattr(a, 'detail', None)
                for a in s.query(AuditLog).filter(AuditLog.entity_id == oid, AuditLog.action == azione).all()]
    finally:
        s.close()


def main():
    setup()
    from tests.accesso_aiuto import postazioni, persona, entra, modalita
    modalita('protetto')
    postazioni()
    persona('elena', 'Elena Colombo', 'Impiegata', pin='2580')
    c = entra(app, 'ufficio', pin='2580')
    reparto = entra(app, 'reparto')

    # =====================================================================
    print('\n1) Segna finito: nota e cosa risulta ancora aperto')
    r = c.post('/api/ordini/o-pulito/completamento', json={'nota': '  Marco,   finito tutto '})
    d = r.get_json()
    check('segnato (200)', r.status_code == 200 and d.get('success'), d)
    check('niente da controllare su un ordine tagliato senza sospesi', d.get('restano') == [], d.get('restano'))
    check('non e\' "gia fatto"', not d.get('gia_fatto'), d)
    x = riga('o-pulito')
    check('passa a DDT da fare', x['passo'] == 'attesa_ddt', x['passo'])
    check('nota salvata, spazi puliti', x['nota_completamento'] == 'Marco, finito tutto', x['nota_completamento'])
    check('la nota e\' nello storico', any('Marco, finito tutto' in (t or '') for t in audit('o-pulito', 'ORDINE_COMPLETAMENTO')),
          audit('o-pulito', 'ORDINE_COMPLETAMENTO'))

    r = c.post('/api/ordini/o-aperto/completamento', json={})
    d = r.get_json()
    codici = [v['codice'] for v in d.get('restano') or []]
    check('NON bloccato anche se resta qualcosa (200)', r.status_code == 200 and riga('o-aperto')['passo'] == 'attesa_ddt', d)
    check('avvisa: taglio non segnato', 'taglio' in codici, codici)
    check('avvisa: 2 voci da procurare (l\'arrivata no)', any(v['codice'] == 'procura' and v['testo'].startswith('2 voci')
                                                               for v in d['restano']), d['restano'])
    check('avvisa: fase d\'officina aperta', 'fasi' in codici, codici)

    # =====================================================================
    print('\n2) Gia\' finito')
    r = c.post('/api/ordini/o-gia/completamento', json={'nota': 'altra nota'})
    d = r.get_json()
    check('risponde 200 con gia_fatto', r.status_code == 200 and d.get('gia_fatto') is True, d)
    check('la nota del primo resta', riga('o-gia')['nota_completamento'] == 'Paolo al telefono')
    check('nessuna riga nuova nello storico', audit('o-gia', 'ORDINE_COMPLETAMENTO') == [])

    # =====================================================================
    print('\n3) Annulla')
    r = c.delete('/api/ordini/o-pulito/completamento')
    check('annullato (200)', r.status_code == 200, r.get_json())
    x = riga('o-pulito')
    check('torna in lavorazione', x['fase'] == 'aperto' and x['passo'] == 'in_lavorazione', x['fase'])
    check('la nota se ne va con il lavoro finito', x['nota_completamento'] == '', x['nota_completamento'])
    r = c.post('/api/ordini/o-pulito/completamento', json={})
    check('si puo segnare di nuovo', r.status_code == 200 and not r.get_json().get('gia_fatto'))

    # =====================================================================
    print('\n4) Segna finiti (piu\' ordini insieme)')
    r = c.post('/api/ordini/completamento-multiplo', json={'ids': ['o-a', 'o-b', 'o-gia', 'non-esiste', 'o-a']})
    d = r.get_json()
    check('200', r.status_code == 200, d)
    ris = {x['id']: x for x in d.get('risultati') or []}
    check('doppioni tolti', len(d.get('risultati') or []) == 4, len(d.get('risultati') or []))
    check('2 segnati, 1 gia finito, 1 errore', (d.get('n_fatti'), d.get('n_gia'), d.get('n_errori')) == (2, 1, 1),
          (d.get('n_fatti'), d.get('n_gia'), d.get('n_errori')))
    check('l\'inesistente dice perche', ris['non-esiste']['codice'] == 'non_trovato', ris['non-esiste'])
    check('i due passano a DDT da fare', riga('o-a')['passo'] == riga('o-b')['passo'] == 'attesa_ddt')
    check('il gia finito non cambia autore ne nota', riga('o-gia')['nota_completamento'] == 'Paolo al telefono')
    check('o-c non toccato', riga('o-c')['fase'] == 'aperto')
    check('una riga di storico per ordine segnato', len(audit('o-a', 'ORDINE_COMPLETAMENTO')) == 1
          and len(audit('o-b', 'ORDINE_COMPLETAMENTO')) == 1)
    r = c.post('/api/ordini/completamento-multiplo', json={'ids': []})
    check('elenco vuoto rifiutato (400)', r.status_code == 400, r.status_code)
    r = c.post('/api/ordini/completamento-multiplo', json={'ids': 'o-c'})
    check('ids non elenco rifiutato (400)', r.status_code == 400, r.status_code)
    for i in ('o-a', 'o-b'):
        c.delete(f'/api/ordini/{i}/completamento')
    check('Annulla di "Segna finiti": tornano in lavorazione', riga('o-a')['fase'] == riga('o-b')['fase'] == 'aperto')

    # =====================================================================
    print('\n5) La nota scritta dopo')
    r = c.post('/api/ordini/o-c/completamento/nota', json={'nota': 'Marco'})
    check('su un ordine in lavorazione no (409)', r.status_code == 409 and r.get_json()['codice'] == 'sequenza', r.get_json())
    r = c.post('/api/ordini/o-aperto/completamento/nota', json={'nota': 'Marco, mancano 2 pezzi'})
    check('sul finito si (200)', r.status_code == 200, r.get_json())
    check('salvata', riga('o-aperto')['nota_completamento'] == 'Marco, mancano 2 pezzi')
    r = c.post('/api/ordini/o-aperto/completamento/nota', json={'nota': 'x' * 301})
    check('troppo lunga rifiutata', r.status_code == 409 and r.get_json()['codice'] == 'nota_lunga', r.status_code)
    r = c.post('/api/ordini/o-aperto/completamento/nota', json={'nota': ''})
    check('vuota la toglie', r.status_code == 200 and riga('o-aperto')['nota_completamento'] == '')
    check('ogni versione nello storico', len(audit('o-aperto', 'ORDINE_COMPLETAMENTO_NOTA')) == 2,
          audit('o-aperto', 'ORDINE_COMPLETAMENTO_NOTA'))

    # =====================================================================
    print('\n6) Solo l\'ufficio')
    for url in ('/api/ordini/completamento-multiplo', '/api/ordini/o-aperto/completamento/nota'):
        r = reparto.post(url, json={'ids': ['o-c'], 'nota': 'x'})
        check(f'il tablet d\'officina respinto su {url}', r.status_code == 403, r.status_code)
        r = app.test_client().post(url, json={'ids': ['o-c'], 'nota': 'x'})
        check(f'senza accesso respinto su {url}', r.status_code == 401, r.status_code)
    check('o-c sempre in lavorazione', riga('o-c')['fase'] == 'aperto')

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
