"""Test della RIMOZIONE di cartellini e pistole barcode, e di "Stampa ordine".

Prima c'erano le pistole barcode (gia' spente da un interruttore) e i
cartellini A6 col barcode. Ora sono tolti del tutto: in officina l'ordine si
riconosce dal PDF d'ordine stampato. Questo test sostituisce
test_pistole_dismesse.py (che provava l'interruttore, che non c'e' piu').

Copre:
 1. gli indirizzi delle pistole e del cartellino non esistono piu' (404)
 2. lo storico delle scansioni resta nel database, intatto
 3. le ore dei KPI arrivano dalle DICHIARAZIONI del tablet
 4. gli ordini "probabilmente finiti" si riconoscono senza scansioni
 5. "Stampa ordine": il PDF del cliente se c'e', altrimenti il foglio
    d'ordine con i pezzi raggruppati per lamiera
 6. la configurazione si legge e si salva (ConfigManager), le chiavi delle
    pistole non si accettano piu' e quelle vecchie nel file restano
 7. niente piu' il vecchio gestore delle pistole, cartellini, dashboard dalle scansioni

Gira su DATABASE, CONFIG e CARTELLE TEMPORANEI. Esecuzione:
    python app/tests/test_senza_cartellini.py
"""
import importlib
import json
import os
import shutil
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
from backend.models import Base, User, Order, OrderFile, OfficinaScan, Preventivo  # noqa: E402
from backend import models_ore  # noqa: E402,F401
from backend.models_ore import Cliente  # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), f'test_senza_cart_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

A = importlib.import_module('backend.app')  # il MODULO, non l'oggetto Flask
from backend import database as D  # noqa: E402
from backend.database import ConfigManager, KPIManager, OrderManager, PreventivoManager  # noqa: E402
from backend import ore_service as svc  # noqa: E402

# Config e cartelle isolate: i test NON toccano app_config.json ne' uploads veri
_CFG = os.path.join(tempfile.gettempdir(), f'test_cfg_{uuid.uuid4().hex[:8]}.json')
ConfigManager._CONFIG_PATH = _CFG
_CARTELLE = tempfile.mkdtemp(prefix='ft_stampa_')
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
        print(f'  [KO] {nome} {extra}')


def scrivi_config(**kv):
    with open(_CFG, 'w', encoding='utf-8') as f:
        json.dump(kv, f)


PDF_CLIENTE = os.path.join(_CARTELLE, 'ordine_cliente.pdf')


def setup():
    with open(PDF_CLIENTE, 'wb') as f:
        f.write(b'%PDF-1.4\n% ordine del cliente\n%%EOF\n')
    s = models.SessionLocal()
    try:
        s.add(User(id='enzo', name='Enzo Bianchi', role='Operaio Officina', is_active=True))
        s.add(User(id='mirko', name='Mirko Verdi', role='Operaio Laser', is_active=True))
        s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina',
                   is_active=True, is_capo=True))
        s.add(Cliente(id=str(uuid.uuid4()), nome='Cliente Alfa', attivo=True))
        now = datetime.utcnow()
        s.add(Order(id='ord-vecchio', numero_ordine='0100-26', cliente='Cliente Alfa',
                    data_ricezione=now - timedelta(days=20),
                    data_consegna=now + timedelta(days=5),
                    status='RICEVUTO', taglio_completato=False))
        s.add(Order(id='ord-nuovo', numero_ordine='0101-26', cliente='Cliente Alfa',
                    data_ricezione=now - timedelta(hours=6),
                    data_consegna=now + timedelta(days=20),
                    status='RICEVUTO', taglio_completato=False))
        s.add(Order(id='ord-chiuso-op', numero_ordine='0102-26', cliente='Cliente Alfa',
                    data_ricezione=now - timedelta(days=30),
                    data_consegna=now + timedelta(days=2),
                    status='RICEVUTO', taglio_completato=True,
                    data_completamento_operativo=now - timedelta(days=1),
                    completato_operativo_da='paolo'))
        # Ordine col PDF del cliente
        s.add(Order(id='ord-pdf', numero_ordine='4521', cliente='Cliente PDF',
                    data_ricezione=now, data_consegna=datetime(2026, 10, 21),
                    status='RICEVUTO', origine='PDF'))
        s.add(OrderFile(id=str(uuid.uuid4()), order_id='ord-pdf', filename='ordine_cliente.pdf',
                        filepath=PDF_CLIENTE, file_type='PDF'))
        # Scansione STORICA: deve restare dov'e'
        s.add(OfficinaScan(id='scan-storica', order_id='ord-vecchio',
                           operatore_id='enzo', pistola_id='PISTOLA-A1',
                           timestamp_inizio=now - timedelta(days=18, hours=3),
                           timestamp_fine=now - timedelta(days=18)))
        s.commit()
    finally:
        s.close()


def ordine_da_preventivo():
    """Un preventivo vero, accettato con la strada vera: torna l'id dell'ordine."""
    pr = PreventivoManager.create('DECA S.r.l.', 'paolo', quantita=2)
    pid = pr['id'] if isinstance(pr, dict) else pr
    PreventivoManager.replace_articoli(pid, [
        {'codice': 'A1-SOTTILE', 'quantita': 3, 'materiale': 'S235', 'spessore_mm': 2,
         'area_dm2': 1.8, 'pieghe': 2, 'costo_base_override': 5},
        {'codice': 'B1-SPESSO', 'quantita': 1, 'materiale': 'S235', 'spessore_mm': 5,
         'area_dm2': 1.0, 'filettatura_pz': 4, 'costo_base_override': 4},
        {'codice': 'C1-INOX', 'quantita': 2, 'materiale': 'INOX_304', 'spessore_mm': 2,
         'area_dm2': 1.0, 'costo_base_override': 4},
    ])
    PreventivoManager.replace_tubolari(pid, [{'profilo': 'Quadro 40x40 sp.2mm',
                                              'lunghezza_m': 0.5, 'qty': 2}])
    s = models.SessionLocal()
    try:
        s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'INVIATO'
        s.commit()
    finally:
        s.close()
    acc = PreventivoManager.accetta_e_crea_ordine(pid, 'paolo')
    return acc


def main():
    setup()
    # l'Ufficio: legge KPI, ordini sospetti e il foglio d'ordine
    from tests.accesso_aiuto import stazione_pronta, modalita
    modalita('protetto')
    c = stazione_pronta(A.app, 'ufficio')

    # =====================================================================
    print('\n1) Pistole e cartellini: gli indirizzi non esistono piu')
    r = c.post('/api/scan', json={'pistola_id': 'PISTOLA-A1', 'codice': '0101-26'})
    check('POST /api/scan -> 404', r.status_code == 404, r.status_code)
    r = c.get('/api/orders/ord-vecchio/cartellino')
    check('GET /api/orders/<id>/cartellino -> 404', r.status_code == 404, r.status_code)
    for url in ('/api/admin/pistole', '/api/orders/ord-vecchio/tempo-officina',
                '/api/officina/live-status'):
        r = c.get(url)
        check(f'GET {url} -> 404', r.status_code == 404, r.status_code)
    r = c.post('/api/admin/close-residual', json={'user_id': 'paolo'})
    check('POST /api/admin/close-residual -> 404', r.status_code == 404, r.status_code)
    s = models.SessionLocal()
    try:
        nuove = s.query(OfficinaScan).filter(OfficinaScan.order_id == 'ord-nuovo').count()
    finally:
        s.close()
    check('nessuna scansione registrata', nuove == 0, nuove)

    # =====================================================================
    print('\n2) Lo storico delle scansioni resta nel database')
    s = models.SessionLocal()
    try:
        storica = s.query(OfficinaScan).filter(OfficinaScan.id == 'scan-storica').first()
    finally:
        s.close()
    check('la scan storica esiste ancora, coi suoi tempi',
          storica is not None and storica.timestamp_fine is not None)

    # =====================================================================
    print('\n3) Le ore dei KPI arrivano dalle DICHIARAZIONI')
    oggi = date.today()
    svc.salva_giornata('enzo', oggi,
                       [{'cliente': 'Cliente Alfa', 'minuti': 300},
                        {'attivita_interna': True, 'minuti': 60}],
                       origine='tablet', richiesta_id='k1')
    kpi = {k['operatore_id']: k for k in KPIManager.get_kpi_operai()}
    check('Enzo compare nei KPI', 'enzo' in kpi, list(kpi))
    check('ore di oggi = 6h (5h cliente + 1h interna)',
          kpi.get('enzo', {}).get('ore_oggi') == 6.0, kpi.get('enzo'))
    check('la fonte e dichiarata', kpi.get('enzo', {}).get('fonte') == 'dichiarazioni')
    check('la scan storica NON viene sommata', kpi.get('enzo', {}).get('ore_mese') == 6.0,
          kpi.get('enzo'))
    check('anche chi non ha dichiarato compare a zero',
          kpi.get('mirko', {}).get('ore_oggi') == 0.0, kpi.get('mirko'))
    check('il capo non e trattato come operaio', 'paolo' not in kpi)
    r = c.get('/api/capo/kpi-operai')
    check('/api/capo/kpi-operai risponde coi KPI', r.status_code == 200
          and any(k['operatore_id'] == 'enzo' for k in r.get_json()), r.status_code)

    # =====================================================================
    print('\n4) Ordini "probabilmente finiti" senza scansioni')
    scrivi_config(sospetto_giorni_apertura=10)
    fermi = {o['id']: o for o in OrderManager.get_ordini_sospetti_finiti()}
    check('ordine aperto da 20 giorni segnalato', 'ord-vecchio' in fermi, list(fermi))
    check('ordine arrivato oggi NON segnalato', 'ord-nuovo' not in fermi)
    check('ordine gia completato dall ufficio NON segnalato', 'ord-chiuso-op' not in fermi)
    v = fermi.get('ord-vecchio', {})
    check('spiega il motivo in chiaro', 'giorni' in (v.get('motivo') or ''), v)
    check('nessun campo delle scansioni', not ({'ultima_scan', 'numero_scan'} & set(v)), v)
    check('dice quando e arrivato', bool(v.get('data_ricezione')), v)
    scrivi_config(sospetto_giorni_apertura=60)
    check('soglia rispettata (60 giorni: nessuno)',
          len(OrderManager.get_ordini_sospetti_finiti()) == 0)
    scrivi_config(sospetto_giorni_apertura=10)
    r = c.get('/api/orders/sospetti-finiti')
    check('/api/orders/sospetti-finiti risponde', r.status_code == 200
          and r.get_json().get('count', 0) >= 1, r.status_code)
    check('nessun alert "taglio fermo" (viveva solo con le pistole)',
          not hasattr(OrderManager, 'alert_ordini_taglio_fermo'))

    # =====================================================================
    print('\n5) Stampa ordine')
    r = c.get('/api/orders/ord-pdf/stampa')
    check('col PDF del cliente: il PDF', r.status_code == 200
          and r.mimetype == 'application/pdf' and r.data.startswith(b'%PDF'),
          (r.status_code, r.mimetype))
    check('e proprio quello del cliente', b'ordine del cliente' in r.data)
    r.close()

    acc = ordine_da_preventivo()
    oid = acc.get('order_id')
    check('ordine dal preventivo creato', bool(oid), acc)
    check('accettazione senza cartellino', 'cartellino_url' not in acc, acc)
    r = c.get(f'/api/orders/{oid}/stampa')
    html = r.get_data(as_text=True)
    check('senza PDF: il foglio d\'ordine in HTML', r.status_code == 200
          and r.mimetype == 'text/html', (r.status_code, r.mimetype))
    check('con cliente e consegna', 'DECA S.r.l.' in html and 'Consegna' in html)
    check('con i pezzi', all(k in html for k in ('A1-SOTTILE', 'B1-SPESSO', 'C1-INOX')))
    i2, i5 = html.find('sp. 2 mm'), html.find('sp. 5 mm')
    check('raggruppati per lamiera, dalla piu sottile', 0 <= i2 < i5, (i2, i5))
    check('inox separato dal ferro', 'Lamiera INOX 304 sp. 2 mm' in html
          and 'Lamiera S235 sp. 2 mm' in html)
    check('con le lavorazioni d\'officina', 'Piegatura (2 pieghe)' in html
          and 'Filettatura (4)' in html)
    check('il solo taglio non e una lavorazione', 'solo taglio' in html)
    check('i tubolari a parte', 'Tubolari' in html)
    check('pronto per la stampa A4', '@page' in html and 'A4' in html
          and 'window.print()' in html)
    r = c.get('/api/orders/non-esiste/stampa')
    check('ordine inesistente: 404', r.status_code == 404, r.status_code)

    # =====================================================================
    print('\n6) Configurazione: si legge e si salva')
    scrivi_config(sospetto_giorni_apertura=10, pistole_attive=False,
                  orario_lavoro=[['07:30', '12:00']], laser_config={'x': 1})
    cfg = ConfigManager.load_config()
    check('load_config legge il file', cfg.get('sospetto_giorni_apertura') == 10
          and cfg.get('laser_config') == {'x': 1}, cfg)
    out = ConfigManager.save_config({'sospetto_giorni_apertura': 12,
                                     'pistole_attive': True, 'fine_turno_hhmm': '17:30'})
    check('save_config salva le chiavi buone', out.get('sospetto_giorni_apertura') == 12, out)
    ign = ' '.join(out.get('_ignorati') or [])
    check('le chiavi delle pistole non si accettano piu',
          'pistole_attive' in ign and 'fine_turno_hhmm' in ign, out.get('_ignorati'))
    with open(_CFG, encoding='utf-8') as f:
        su_file = json.load(f)
    check('le chiavi vecchie gia nel file restano (dati dell\'utente)',
          su_file.get('pistole_attive') is False and su_file.get('orario_lavoro'), su_file)
    check('il resto del file non si tocca', su_file.get('laser_config') == {'x': 1}, su_file)

    # =====================================================================
    print('\n7) Niente piu codice delle pistole')
    check('il vecchio gestore delle pistole non esiste piu', not hasattr(D, 'Barcode' + 'Manager'))
    try:
        importlib.import_module('backend.pdf_cartellino')
        sparito = False
    except ImportError:
        sparito = True
    check('il generatore di cartellini non esiste piu', sparito)
    live = c.get('/api/dashboard-live').get_json()
    check('dashboard live senza contatori dalle scansioni',
          'operatori_attivi' not in live.get('snapshot', {}) and 'curva_ore' not in live, live)

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
        shutil.rmtree(_CARTELLE, ignore_errors=True)
    except Exception:
        pass
    sys.exit(code)
