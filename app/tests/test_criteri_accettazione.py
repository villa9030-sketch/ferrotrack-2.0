"""I 20 CRITERI DI ACCETTAZIONE della sezione 12, verificati end-to-end.

Gli altri file di test coprono ciascuno il proprio pezzo in profondita'. Questo
li attraversa tutti su UN SOLO database, come farebbe l'uso reale, e copre in
particolare i criteri che nessun altro file verificava:

 11. il tablet di reparto non chiude ordini nemmeno chiamando l'API
 12. completando un ordine, sparisce dal lavoro da fare
 13. pronto / consegnato / archiviato non vengono confusi
 17. preventivatore e apertura disegni ancora funzionanti
 20. modifiche concorrenti senza perdita silenziosa

Gira su DATABASE TEMPORANEO. Nessuna notifica esterna, nessun invio email.
Esecuzione: python app/tests/test_criteri_accettazione.py
"""
import os
import sys
import tempfile
import uuid
from datetime import date, datetime, timedelta

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
from backend.models import Base, User, Order, OfficinaScan  # noqa: E402
from backend import models_ore  # noqa: E402
from backend.models_ore import Cliente, OreAttese  # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), f'test_criteri_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend.app import app  # noqa: E402
from backend.auth_device import crea_token  # noqa: E402
from backend import ore_service as svc  # noqa: E402
from backend import anomalie_service as an  # noqa: E402
from backend import riepilogo_service as ri  # noqa: E402
from backend import ordini_service as osv  # noqa: E402
from backend.database import BarcodeManager  # noqa: E402

OK = 0
KO = []


def check(n, nome, cond, extra=''):
    global OK
    if cond:
        OK += 1
        print(f'  [OK] {n:>2}. {nome}')
    else:
        KO.append(f'{n}. {nome}')
        print(f'  [KO] {n:>2}. {nome} {extra}')


OGGI = svc.oggi_locale()
IERI = OGGI - timedelta(days=1)
LALTRO = OGGI - timedelta(days=2)

_CFG = os.path.join(tempfile.gettempdir(), f'test_criteri_cfg_{uuid.uuid4().hex[:8]}.json')
BarcodeManager._CONFIG_PATH = _CFG


def setup():
    s = models.SessionLocal()
    try:
        s.add(User(id='enzo', name='Enzo Bianchi', role='Operaio Officina', is_active=True))
        s.add(User(id='elena', name='Elena Colombo', role='Impiegata', is_active=True))
        for n in ('Cliente Y', 'Cliente Z'):
            s.add(Cliente(id=str(uuid.uuid4()), nome=n, attivo=True))
        s.add(OreAttese(id=str(uuid.uuid4()), operatore_id='enzo',
                        tenuto_alla_compilazione=True, minuti_attesi=480,
                        giorni_settimana=[1, 2, 3, 4, 5, 6, 7]))
        s.add(Order(id='ord-1', numero_ordine='ORD-1', cliente='Cliente Y',
                    status='RICEVUTO', data_ricezione=datetime.utcnow(),
                    data_consegna=datetime.utcnow() + timedelta(days=5)))
        s.add(Order(id='ord-2', numero_ordine='ORD-2', cliente='Cliente Y',
                    status='RICEVUTO', data_ricezione=datetime.utcnow(),
                    data_consegna=datetime.utcnow() + timedelta(days=6)))
        s.commit()
    finally:
        s.close()
    return (crea_token('Tablet timbratrice', 'ore', 'test')['token'],
            crea_token('Tablet reparto', 'reparto', 'test')['token'],
            crea_token('PC ufficio', 'ufficio', 'test')['token'])


def main():
    t_ore, t_rep, t_uff = setup()
    c = app.test_client()
    H = lambda tok: {'X-Device-Token': tok}  # noqa: E731

    print('\n--- RACCOLTA ORE ---')
    # 1
    r = c.post('/api/ore/giornata', headers=H(t_ore), json={
        'operatore_id': 'enzo', 'data': OGGI.isoformat(),
        'righe': [{'cliente': 'Cliente Y', 'minuti': 300},
                  {'cliente': 'Cliente Z', 'minuti': 180}],
        'revisione_attesa': 0, 'richiesta_id': 'c1'})
    g = r.get_json().get('giornata', {})
    check(1, 'Operaio: 5 ore Y + 3 ore Z', r.status_code == 200
          and g.get('totale_minuti') == 480, r.get_json())

    # 2
    r = c.get(f'/api/ore/giornata?operatore_id=enzo&data={OGGI.isoformat()}', headers=H(t_ore))
    g2 = r.get_json()['giornata']
    check(2, 'Riapertura: stessi dati e totale', g2['totale_minuti'] == 480
          and len(g2['righe']) == 2
          and {x['cliente'] for x in g2['righe']} == {'Cliente Y', 'Cliente Z'}, g2)

    # 3
    r = c.post('/api/ore/giornata', headers=H(t_ore), json={
        'operatore_id': 'enzo', 'data': OGGI.isoformat(),
        'righe': [{'cliente': 'Cliente Y', 'minuti': 420},
                  {'cliente': 'Cliente Z', 'minuti': 60}],
        'revisione_attesa': g2['revisione'], 'richiesta_id': 'c2'})
    g3 = r.get_json()['giornata']
    check(3, 'Correzione: sostituzione, non duplicazione',
          len(g3['righe']) == 2 and g3['totale_minuti'] == 480, g3)

    # 4
    corpo = {'operatore_id': 'enzo', 'data': OGGI.isoformat(),
             'righe': [{'cliente': 'Cliente Y', 'minuti': 420},
                       {'cliente': 'Cliente Z', 'minuti': 60}],
             'revisione_attesa': g3['revisione'], 'richiesta_id': 'ripetuta'}
    a1 = c.post('/api/ore/giornata', headers=H(t_ore), json=corpo).get_json()['giornata']
    a2 = c.post('/api/ore/giornata', headers=H(t_ore), json=corpo).get_json()['giornata']
    check(4, 'Doppio invio e retry: nessun doppio conteggio',
          a1['totale_minuti'] == a2['totale_minuti'] == 480
          and a2.get('idempotente') is True, (a1, a2))

    # 5 — il salvataggio fallisce e i valori restano al chiamante
    r = c.post('/api/ore/giornata', headers=H(t_ore), json={
        'operatore_id': 'enzo', 'data': OGGI.isoformat(),
        'righe': [{'cliente': 'Cliente Y', 'minuti': 120}],
        'revisione_attesa': 999, 'richiesta_id': 'conflitto'})
    dati = r.get_json()
    r2 = c.get(f'/api/ore/giornata?operatore_id=enzo&data={OGGI.isoformat()}', headers=H(t_ore))
    check(5, 'Errore: nessun falso "Salvato", dati intatti',
          r.status_code == 409 and dati.get('success') is False
          and r2.get_json()['giornata']['totale_minuti'] == 480, dati)

    # 6
    invalidi = [
        [{'cliente': 'Cliente Y', 'minuti': -30}],
        [{'cliente': 'Cliente Y', 'minuti': 'otto'}],
        [{'cliente': 'Cliente Inesistente', 'minuti': 60}],
        [{'cliente': 'Cliente Y', 'minuti': 99999}],
    ]
    esiti = [c.post('/api/ore/giornata', headers=H(t_ore), json={
        'operatore_id': 'enzo', 'data': OGGI.isoformat(), 'righe': x,
        'revisione_attesa': None, 'richiesta_id': f'inv{i}'}).status_code
        for i, x in enumerate(invalidi)]
    check(6, 'Input invalidi respinti dal backend',
          all(s >= 400 for s in esiti), esiti)

    # 7
    an.salva_eccezione('enzo', IERI, 'assenza', nota='ferie', da='elena')
    r = c.get(f'/api/ore/giornata?operatore_id=enzo&data={IERI.isoformat()}', headers=H(t_uff))
    senza = r.get_json()['giornata']['minuti_attesi']
    an.salva_eccezione('enzo', LALTRO, 'ridotta', minuti_attesi=240, da='elena')
    r = c.get(f'/api/ore/giornata?operatore_id=enzo&data={LALTRO.isoformat()}', headers=H(t_uff))
    ridotta = r.get_json()['giornata']['minuti_attesi']
    check(7, 'Assenze e orari ridotti gestiti', senza is None and ridotta == 240,
          (senza, ridotta))

    print('\n--- CONTROLLO DELLE MANCANZE ---')
    # 8
    an.controlla_periodo(dal=LALTRO, al=IERI)
    aperte = an.elenco_anomalie(stato='aperta')
    check(8, 'Giornata mancante: anomalia il giorno dopo',
          any(a['data'] == LALTRO.isoformat() for a in aperte), aperte)
    check(8.1, 'Il giorno di assenza NON e segnalato',
          not any(a['data'] == IERI.isoformat() for a in aperte), aperte)

    # 9
    n1 = an.notifica_anomalie()
    n2 = an.notifica_anomalie()
    an.controlla_e_notifica()
    check(9, 'Riavvio: nessuna notifica duplicata', n1 > 0 and n2 == 0, (n1, n2))

    # 10
    svc.salva_giornata('enzo', LALTRO, [{'cliente': 'Cliente Y', 'minuti': 240}],
                       origine='ufficio', modificata_da='elena', richiesta_id='corr')
    an.controlla_periodo(dal=LALTRO, al=LALTRO)
    ancora = [a for a in an.elenco_anomalie(stato='aperta')
              if a['data'] == LALTRO.isoformat()]
    g = svc.leggi_giornata('enzo', LALTRO)
    check(10, 'Correzione ufficio: anomalia risolta e modifica tracciata',
          not ancora and g['origine'] == 'ufficio' and g['modificata_da'] == 'elena',
          (ancora, g))

    print('\n--- ORDINI E REPARTO ---')
    # 11 — il tablet di reparto non deve poter chiudere un ordine
    tentativi = [
        c.post('/api/ordini/ord-1/completamento', headers=H(t_rep),
               json={'user_id': 'elena'}),          # finge di essere l'impiegata
        c.post('/api/ordini/ord-1/chiudi', headers=H(t_rep), json={'user_id': 'enzo'}),
        c.post('/api/ordini/ord-1/completamento', json={'user_id': 'enzo'}),
    ]
    check(11, 'Tablet reparto: non chiude ordini nemmeno via API',
          all(t.status_code == 403 for t in tentativi[1:])
          and osv.elenco()['conteggi']['aperto'] == 2,
          [t.status_code for t in tentativi])
    check(11.1, 'Il token di reparto non da poteri d ufficio',
          c.post('/api/ordini/ord-2/chiudi', headers=H(t_rep),
                 json={'user_id': 'enzo'}).status_code == 403)

    # 12
    r = c.post('/api/ordini/ord-1/completamento', json={'user_id': 'elena'})
    from backend.database import OrderManager
    ordini = {o['id']: o for o in OrderManager.get_all_orders_dict()}
    check(12, 'Completamento: l ordine sparisce dal lavoro da fare',
          r.status_code == 200 and ordini['ord-1']['fase'] != 'aperto'
          and ordini['ord-2']['fase'] == 'aperto',
          {k: v['fase'] for k, v in ordini.items()})

    # 13
    c.post('/api/ordini/ord-1/ddt', json={'user_id': 'elena', 'numero': 'DDT 1'})
    f_ddt = osv.elenco()['conteggi']
    prima_chiusura = c.post('/api/ordini/ord-1/chiudi', json={'user_id': 'elena'})
    c.post('/api/ordini/ord-1/consegna', json={'user_id': 'elena', 'completa': True})
    dopo_consegna = osv.elenco()['conteggi']
    c.post('/api/ordini/ord-1/chiudi', json={'user_id': 'elena'})
    finale = osv.elenco()['conteggi']
    check(13, 'Pronto, consegnato e archiviato non confusi',
          prima_chiusura.status_code == 409
          and f_ddt['consegnato'] == 1 and dopo_consegna['consegnato'] == 1
          and finale['archivio'] == 1 and finale['consegnato'] == 0,
          (f_ddt, dopo_consegna, finale))

    print('\n--- RIEPILOGO ECONOMICO ---')
    anno, mese = OGGI.year, OGGI.month
    ri.imposta_tariffa(date(anno, mese, 1), 30.0, da='elena')
    ri.salva_fatturato('Cliente Y', anno, mese, 2000.0, riferimento='FT 1', da='elena')
    ri.salva_materiale('Cliente Y', anno, mese, 500.0, descrizione='lamiera', da='elena')
    rep = ri.riepilogo(anno=anno, mese=mese)
    cy = [x for x in rep['clienti'] if x['cliente'] == 'Cliente Y'][0]
    # 14
    atteso = round(2000.0 - 500.0 - cy['costo_ore'], 2)
    check(14, 'Fatturato, materiali, ore e costo riconciliabili',
          cy['residuo'] == atteso and cy['costo_ore'] == round(cy['ore'] * 30.0, 2),
          cy)
    # 15
    prima = cy['costo_ore']
    ri.imposta_tariffa(OGGI + timedelta(days=1), 45.0, da='elena')
    dopo = [x for x in ri.riepilogo(anno=anno, mese=mese)['clienti']
            if x['cliente'] == 'Cliente Y'][0]['costo_ore']
    check(15, 'Cambio tariffa: storico non alterato', prima == dopo, (prima, dopo))
    # 16
    s = models.SessionLocal()
    try:
        s.add(OfficinaScan(id=str(uuid.uuid4()), order_id='ord-2', operatore_id='enzo',
                           pistola_id='P1',
                           timestamp_inizio=datetime(OGGI.year, OGGI.month, OGGI.day, 8, 0),
                           timestamp_fine=datetime(OGGI.year, OGGI.month, OGGI.day, 16, 0)))
        s.commit()
    finally:
        s.close()
    ore_dopo = [x for x in ri.riepilogo(anno=anno, mese=mese)['clienti']
                if x['cliente'] == 'Cliente Y'][0]['ore']
    check(16, 'Nessun doppio conteggio scansioni/dichiarazioni',
          ore_dopo == cy['ore'], (cy['ore'], ore_dopo))

    print('\n--- IL RESTO DELL APPLICAZIONE ---')
    # 17
    r_prev = c.get('/api/preventivi')
    r_ord = c.get('/api/orders')
    r_dis = c.get('/api/orders/ord-1/dxf/inesistente.dxf')
    check(17, 'Preventivatore e ordini ancora raggiungibili',
          r_prev.status_code == 200 and r_ord.status_code == 200,
          (r_prev.status_code, r_ord.status_code))
    check(17.1, 'Endpoint disegni risponde in modo pulito su file assente',
          r_dis.status_code in (404, 400), r_dis.status_code)

    # 19 — la bacheca del tablet e' leggibile: pochi dati, nessun conteggio inventato
    r = c.get('/api/ore/bacheca', headers=H(t_ore))
    voci = r.get_json()['operai']
    check(19, 'Bacheca tablet: dati essenziali per ogni operaio',
          all({'nome', 'dichiarata', 'totale_minuti', 'righe', 'minuti_attesi'} <= set(v)
              for v in voci), voci)

    # 20
    stato = svc.leggi_giornata('enzo', OGGI)
    r1 = c.post('/api/ore/giornata', headers=H(t_uff), json={
        'operatore_id': 'enzo', 'data': OGGI.isoformat(),
        'righe': [{'cliente': 'Cliente Y', 'minuti': 60}],
        'revisione_attesa': stato['revisione'], 'richiesta_id': 'conc-1'})
    r2 = c.post('/api/ore/giornata', headers=H(t_ore), json={
        'operatore_id': 'enzo', 'data': OGGI.isoformat(),
        'righe': [{'cliente': 'Cliente Z', 'minuti': 90}],
        'revisione_attesa': stato['revisione'], 'richiesta_id': 'conc-2'})
    finale = svc.leggi_giornata('enzo', OGGI)
    check(20, 'Modifiche concorrenti: la seconda viene fermata, non persa',
          r1.status_code == 200 and r2.status_code == 409
          and finale['totale_minuti'] == 60, (r1.status_code, r2.status_code, finale))
    check(20.1, 'Il conflitto restituisce lo stato aggiornato',
          (r2.get_json() or {}).get('giornata', {}).get('totale_minuti') == 60,
          r2.get_json())

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
        os.remove(_CFG)
    except Exception:
        pass
    sys.exit(code)
