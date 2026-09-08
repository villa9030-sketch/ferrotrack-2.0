"""Test della DISMISSIONE delle pistole barcode.

Verifica che togliere il gesto agli operai non lasci buchi:
 1. la scansione viene rifiutata e NON registra nulla
 2. lo storico delle scansioni gia' fatte resta intatto e leggibile
 3. le ore dei KPI arrivano dalle DICHIARAZIONI (non piu' dalle scan)
 4. l'alert "taglio fermo" non spamma (nessuno auto-conferma piu' il taglio)
 5. gli ordini fermi si riconoscono senza scansioni (anzianita' + niente
    completamento registrato dall'ufficio)
 6. riattivando il flag si torna esattamente al comportamento precedente

Gira su DATABASE e CONFIG TEMPORANEI. Esecuzione:
    python app/tests/test_pistole_dismesse.py
"""
import json
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
from backend.models import Base, User, Order, OfficinaScan, Pistola  # noqa: E402
from backend import models_ore  # noqa: E402
from backend.models_ore import Cliente  # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), f'test_pist_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend.database import BarcodeManager, OrderManager  # noqa: E402
from backend import ore_service as svc  # noqa: E402

# Config isolata: i test NON devono toccare app_config.json reale
_CFG = os.path.join(tempfile.gettempdir(), f'test_cfg_{uuid.uuid4().hex[:8]}.json')
BarcodeManager._CONFIG_PATH = _CFG

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


def setup():
    s = models.SessionLocal()
    try:
        s.add(User(id='enzo', name='Enzo Bianchi', role='Operaio Officina',
                   is_active=True))
        s.add(User(id='mirko', name='Mirko Verdi', role='Operaio Laser',
                   is_active=True))
        s.add(User(id='capo', name='Capo Reparto', role='Capo Officina',
                   is_active=True, is_capo=True))
        s.add(Pistola(id=str(uuid.uuid4()), pistola_id='PISTOLA-A1',
                      operatore_id='enzo', attiva=True))
        s.add(Cliente(id=str(uuid.uuid4()), nome='Cliente Alfa', attivo=True))

        now = datetime.utcnow()
        # Ordine vecchio, mai completato dall'ufficio -> deve risultare fermo
        s.add(Order(id='ord-vecchio', numero_ordine='0100-26', cliente='Cliente Alfa',
                    data_ricezione=now - timedelta(days=20),
                    data_consegna=now + timedelta(days=5),
                    status='RICEVUTO', taglio_completato=False))
        # Ordine appena arrivato -> non deve risultare fermo
        s.add(Order(id='ord-nuovo', numero_ordine='0101-26', cliente='Cliente Alfa',
                    data_ricezione=now - timedelta(hours=6),
                    data_consegna=now + timedelta(days=20),
                    status='RICEVUTO', taglio_completato=False))
        # Ordine vecchio ma gia' chiuso operativamente dall'ufficio -> escluso
        s.add(Order(id='ord-chiuso-op', numero_ordine='0102-26', cliente='Cliente Alfa',
                    data_ricezione=now - timedelta(days=30),
                    data_consegna=now + timedelta(days=2),
                    status='RICEVUTO', taglio_completato=True,
                    data_taglio_completato=now - timedelta(days=25),
                    data_completamento_operativo=now - timedelta(days=1),
                    completato_operativo_da='elena'))
        # Scansione STORICA gia' registrata: non deve sparire
        s.add(OfficinaScan(id='scan-storica', order_id='ord-vecchio',
                           operatore_id='enzo', pistola_id='PISTOLA-A1',
                           timestamp_inizio=now - timedelta(days=18, hours=3),
                           timestamp_fine=now - timedelta(days=18)))
        s.commit()
    finally:
        s.close()


def main():
    setup()

    # =====================================================================
    print('\n1) Con le pistole DISMESSE la scansione viene rifiutata')
    scrivi_config(pistole_attive=False, sospetto_giorni_apertura=10)
    check('il flag legge "spente"', BarcodeManager.pistole_attive() is False)

    r = BarcodeManager.process_scan('PISTOLA-A1', '0101-26')
    check('scan rifiutata con 410', r.get('status_code') == 410, r)
    check('messaggio indirizza al tablet',
          'tablet' in (r.get('error') or '').lower(), r)

    s = models.SessionLocal()
    try:
        nuove = s.query(OfficinaScan).filter(
            OfficinaScan.order_id == 'ord-nuovo').count()
        ordine = s.query(Order).filter(Order.id == 'ord-nuovo').first()
        taglio = bool(ordine.taglio_completato)
    finally:
        s.close()
    check('nessuna scansione registrata', nuove == 0, nuove)
    check('nessuna auto-conferma del taglio', taglio is False)

    # =====================================================================
    print('\n2) Lo storico delle scansioni resta intatto')
    s = models.SessionLocal()
    try:
        storica = s.query(OfficinaScan).filter(
            OfficinaScan.id == 'scan-storica').first()
    finally:
        s.close()
    check('la scan storica esiste ancora', storica is not None)
    check('con i suoi tempi originali',
          storica is not None and storica.timestamp_fine is not None)
    dett = BarcodeManager.get_tempo_officina('ord-vecchio')
    check('consultabile dal dettaglio ordine',
          len(dett.get('sessioni') or dett.get('sessions') or []) == 1, dett)

    # =====================================================================
    print('\n3) Le ore dei KPI arrivano dalle DICHIARAZIONI')
    oggi = date.today()
    svc.salva_giornata('enzo', oggi,
                       [{'cliente': 'Cliente Alfa', 'minuti': 300},
                        {'attivita_interna': True, 'minuti': 60}],
                       origine='tablet', richiesta_id='k1')
    kpi = {k['operatore_id']: k for k in BarcodeManager.get_kpi_operai()}
    check('Enzo compare nei KPI', 'enzo' in kpi, list(kpi))
    check('ore di oggi = 6h (5h cliente + 1h interna)',
          kpi.get('enzo', {}).get('ore_oggi') == 6.0, kpi.get('enzo'))
    check('la fonte e dichiarata esplicitamente',
          kpi.get('enzo', {}).get('fonte') == 'dichiarazioni')
    check('la scan storica NON viene sommata (niente doppio conteggio)',
          kpi.get('enzo', {}).get('ore_mese') == 6.0, kpi.get('enzo'))
    check('anche chi non ha dichiarato compare a zero',
          kpi.get('mirko', {}).get('ore_oggi') == 0.0, kpi.get('mirko'))
    check('il capo non e trattato come operaio', 'capo' not in kpi)

    # =====================================================================
    print('\n4) Nessun alert "taglio fermo" (non esiste piu chi lo conferma)')
    creati = OrderManager.alert_ordini_taglio_fermo(soglia_ore=1,
                                                    work_start=0, work_end=24)
    check('alert taglio disattivato', creati == 0, creati)

    # =====================================================================
    print('\n5) Ordini fermi riconosciuti senza scansioni')
    fermi = {o['id']: o for o in BarcodeManager.get_ordini_sospetti_finiti()}
    check('ordine aperto da 20 giorni segnalato', 'ord-vecchio' in fermi, list(fermi))
    check('ordine arrivato oggi NON segnalato', 'ord-nuovo' not in fermi)
    check('ordine gia completato dall ufficio NON segnalato',
          'ord-chiuso-op' not in fermi)
    check('spiega il motivo in chiaro',
          'giorni' in (fermi.get('ord-vecchio', {}).get('motivo') or ''),
          fermi.get('ord-vecchio'))
    check('non promette tempi che non abbiamo',
          fermi.get('ord-vecchio', {}).get('ultima_scan') is None)
    check('non richiede il taglio confermato',
          fermi.get('ord-vecchio', {}).get('data_taglio_completato') is None)

    # soglia configurabile
    scrivi_config(pistole_attive=False, sospetto_giorni_apertura=60)
    check('soglia rispettata (60 giorni: nessuno)',
          len(BarcodeManager.get_ordini_sospetti_finiti()) == 0)
    scrivi_config(pistole_attive=False, sospetto_giorni_apertura=10)

    # =====================================================================
    print('\n6) Riattivando il flag si torna al comportamento precedente')
    scrivi_config(pistole_attive=True, sospetto_giorni_dal_taglio=5,
                  sospetto_giorni_da_ultima_scan=3)
    check('il flag legge "accese"', BarcodeManager.pistole_attive() is True)

    r = BarcodeManager.process_scan('PISTOLA-A1', '0101-26')
    check('la scansione torna ad essere accettata', r.get('ok') is True, r)
    s = models.SessionLocal()
    try:
        ordine = s.query(Order).filter(Order.id == 'ord-nuovo').first()
        riattivato = bool(ordine.taglio_completato)
    finally:
        s.close()
    check('auto-conferma taglio di nuovo attiva', riattivato is True)

    kpi2 = {k['operatore_id']: k for k in BarcodeManager.get_kpi_operai()}
    check('i KPI tornano a leggere le scansioni',
          kpi2.get('enzo', {}).get('fonte') is None, kpi2.get('enzo'))

    fermi2 = {o['id']: o for o in BarcodeManager.get_ordini_sospetti_finiti()}
    check('torna il criterio basato sulle scansioni',
          'ord-vecchio' not in fermi2 or fermi2['ord-vecchio']['numero_scan'] > 0,
          fermi2.get('ord-vecchio'))

    # --- il cartellino non deve chiedere di scansionare l'inscansionabile ---
    from backend.pdf_cartellino import genera_cartellino_pdf
    import datetime as _dt

    def _cartellino():
        return genera_cartellino_pdf('PREV-2026-0007', 'DECA S.r.l.',
                                     _dt.datetime(2026, 8, 20), 'URGENTE')

    BarcodeManager.save_config({'pistole_attive': False})
    senza = _cartellino()
    check('senza pistole il cartellino non stampa il barcode',
          b'/Subtype /Image' not in senza and b'/Subtype/Image' not in senza,
          '%d byte' % len(senza))
    check('e non dice di scansionarlo', b'scansiona' not in senza.lower())

    BarcodeManager.save_config({'pistole_attive': True})
    con = _cartellino()
    check('riattivando le pistole il barcode torna',
          b'/Subtype /Image' in con or b'/Subtype/Image' in con,
          '%d byte' % len(con))
    BarcodeManager.save_config({'pistole_attive': False})


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
