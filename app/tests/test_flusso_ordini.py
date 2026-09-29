"""Test del FLUSSO ORDINI unificato: dalla fine del lavoro all'archivio.

Prima c'erano due strade parallele per chiudere un ordine (vista ordini e
scheda "Ordini da fatturare"), il DDT stava in due colonne diverse, i disegni
di un ordine nato da un preventivo non si aprivano da nessuna pagina, il
"Lavoro finito" del capo non registrava ne' data ne' autore, e gli orari
arrivavano al browser senza fuso (due ore indietro).

Copre:
 1. "Lavoro finito" del capo: completamento, notifica all'ufficio, avviso
    se il laser non ha segnato il taglio, idempotenza
 2. una sola chiusura: servono consegna e fattura (anche dalla via vecchia)
 3. badge "da fatturare" == vista "consegnati"
 4. DDT unificato in lettura, scrittura e migrazione dei dati vecchi
 5. disegni dell'ordine: elenco, download, anteprima SVG, zip, nomi ostili
 6. distinta di un ordine nato da un preventivo (vera accettazione)
 7. contatori della dashboard live coerenti con le viste
 8. orari serializzati con il fuso ("Z"), date di calendario senza

Gira su DATABASE TEMPORANEO e cartelle temporanee. Esecuzione:
    python app/tests/test_flusso_ordini.py
"""
import io
import os
import shutil
import sys
import tempfile
import uuid
import zipfile
from datetime import date, datetime, timedelta

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine, text  # noqa: E402

from backend import models  # noqa: E402
from backend.models import (Base, User, Order, OrderFile, Notification,  # noqa: E402
                            Preventivo, OfficinaScan)
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_flusso_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')  # il MODULO, non l'oggetto Flask
from backend import ordini_service as osv  # noqa: E402
from backend import orario  # noqa: E402
from backend.database import PreventivoManager  # noqa: E402
from backend.migrations_ore import _unifica_ddt  # noqa: E402

# Cartelle dei file: temporanee, mai quelle dell'installazione.
_CARTELLE = tempfile.mkdtemp(prefix='ft_flusso_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)
_RETE = os.path.join(_CARTELLE, 'rete')
_config_vera = A.BarcodeManager.load_config


def _config_prova():
    cfg = dict(_config_vera() or {})
    cfg['disegni_export_root'] = _RETE
    return cfg


A.BarcodeManager.load_config = staticmethod(_config_prova)

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


def dxf_quadrato(percorso, lato=100):
    """Un DXF minimo e vero: un quadrato chiuso."""
    import ezdxf
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (lato, 0), (lato, lato), (0, lato)], close=True)
    doc.saveas(percorso)


def ordine(oid, **campi):
    now = datetime.utcnow()
    base = dict(id=oid, numero_ordine=oid.upper(), cliente='Cliente Alfa',
                status='RICEVUTO', data_ricezione=now - timedelta(days=2),
                data_consegna=datetime(2026, 10, 15))
    base.update(campi)
    return Order(**base)


def setup():
    s = models.SessionLocal()
    try:
        s.add(User(id='elena', name='Elena Colombo', role='Impiegata', is_active=True))
        s.add(User(id='postazione-amministrazione', name='Amministrazione',
                   role='Amministrazione', is_active=True))
        s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina',
                   is_active=True, is_capo=True))
        s.add(User(id='enzo', name='Enzo Bianchi', role='Operaio Officina', is_active=True))
        s.add(ordine('o-laser', taglio_richiesto=True, taglio_completato=False))
        s.add(ordine('o-tagliato', taglio_richiesto=True, taglio_completato=True,
                     data_taglio_completato=datetime(2026, 9, 28, 20, 19)))
        s.add(ordine('o-legacy', status='DA_FATTURARE'))
        # DDT scritto dalla vecchia scheda: solo nelle colonne vecchie
        s.add(ordine('o-vecchio-ddt', status='DA_FATTURARE', numero_ddt='DDT-OLD-7',
                     data_ddt=datetime(2026, 9, 1)))
        s.add(ordine('o-dis'))
        s.add(ordine('o-nuovo'))           # mai toccato: "ricevuto"
        s.commit()
    finally:
        s.close()


def notifiche(order_id):
    s = models.SessionLocal()
    try:
        return [(n.user_id, n.title, n.message) for n in
                s.query(Notification).filter(Notification.order_id == order_id).all()]
    finally:
        s.close()


def leggi(oid):
    s = models.SessionLocal()
    try:
        return s.query(Order).filter(Order.id == oid).first()
    finally:
        s.close()


def riga(oid):
    return [x for x in osv.elenco()['ordini'] if x['id'] == oid][0]


def main():
    setup()
    c = A.app.test_client()

    # =====================================================================
    print('\n1) "Lavoro finito" del capo = completamento del ciclo')
    r = c.post('/api/orders/o-laser/close', json={'user_id': 'enzo'})
    check('un operaio non puo (403)', r.status_code == 403, r.status_code)
    r = c.post('/api/orders/o-laser/close', json={'user_id': 'paolo'})
    d = r.get_json()
    check('il capo chiude (200)', r.status_code == 200, d)
    check('forma della risposta compatibile',
          d.get('success') is True and d.get('order_id') == 'o-laser'
          and 'scan_chiuse' in d and d.get('nuovo_status') == 'DA_FATTURARE', d)
    check('avviso: il laser non ha segnato il taglio',
          'laser' in (d.get('avviso') or '').lower(), d)
    o = leggi('o-laser')
    check('data completamento registrata', o.data_completamento_operativo is not None)
    check('autore registrato', o.completato_operativo_da == 'paolo', o.completato_operativo_da)
    check('vista: pronto per DDT', riga('o-laser')['fase'] == 'pronto_ddt')
    n = notifiche('o-laser')
    destinatari = sorted(x[0] for x in n)
    check('notifica a tutte le postazioni d ufficio',
          destinatari == ['elena', 'postazione-amministrazione'], n)
    check('la notifica dice del taglio mancante', all('laser' in x[2].lower() for x in n), n)
    r = c.post('/api/orders/o-laser/close', json={'user_id': 'paolo'})
    check('ripetere non e un errore', r.status_code == 200
          and r.get_json().get('gia_registrato') is True, r.get_json())
    check('e non manda notifiche doppie', len(notifiche('o-laser')) == 2)
    r = c.post('/api/orders/o-tagliato/close', json={'user_id': 'paolo'})
    check('ordine tagliato: nessun avviso', r.status_code == 200
          and 'avviso' not in r.get_json(), r.get_json())
    r = c.post('/api/orders/o-tagliato/close', json={'user_id': 'elena'})
    check('l impiegata puo farlo come riserva', r.status_code == 200, r.status_code)
    r = c.post('/api/orders/non-esiste/close', json={'user_id': 'paolo'})
    check('ordine inesistente -> 404', r.status_code == 404, r.status_code)

    # =====================================================================
    print('\n2) Una sola chiusura: consegna + fattura')
    r = c.post('/api/ordini/o-laser/chiudi',
               json={'user_id': 'elena', 'numero_fattura': 'FT 1'})
    check('senza consegna: 409', r.status_code == 409, r.get_json())
    check('e lo dice', 'consegna' in (r.get_json().get('error') or '').lower(), r.get_json())
    r = c.post('/api/ordini/o-laser/ddt', json={'user_id': 'elena', 'numero': 'DDT 42',
                                                 'data': '2026-09-29'})
    check('DDT registrato con la sua data', r.status_code == 200
          and r.get_json()['ordine']['ddt_data'] == '2026-09-29T00:00:00', r.get_json())
    rr = riga('o-laser')
    check('col solo DDT resta fra i pronti (passo "DDT registrato")',
          rr['fase'] == 'pronto_ddt' and rr['passo'] == 'ddt_registrato', rr)
    check('non ancora chiudibile, e si sa perche',
          rr['chiudibile'] is False and 'consegna' in rr['manca_per_chiudere'].lower(), rr)
    r = c.post('/api/ordini/o-laser/ddt', json={'user_id': 'elena', 'numero': 'X',
                                                 'data': '29/09/2026'})
    check('data DDT in formato sbagliato: 409 chiaro',
          r.status_code == 409 and r.get_json().get('codice') == 'data_non_valida', r.get_json())
    r = c.post('/api/ordini/o-laser/consegna', json={'user_id': 'elena', 'completa': True})
    check('consegna registrata', r.status_code == 200, r.get_json())
    rr = riga('o-laser')
    check('ora e fra i consegnati / da fatturare',
          rr['fase'] == 'consegnato' and rr['passo'] == 'da_fatturare' and rr['chiudibile'], rr)
    r = c.post('/api/ordini/o-laser/chiudi', json={'user_id': 'elena'})
    check('senza numero fattura: 409 fattura_mancante',
          r.status_code == 409 and r.get_json().get('codice') == 'fattura_mancante', r.get_json())
    r = c.post('/api/ordini/o-laser/chiudi',
               json={'user_id': 'elena', 'numero_fattura': 'FT 9', 'data_fattura': '2026-13-45'})
    check('data fattura non valida: 409', r.status_code == 409
          and r.get_json().get('codice') == 'data_non_valida', r.get_json())
    r = c.post('/api/ordini/o-laser/chiudi',
               json={'user_id': 'elena', 'numero_fattura': 'FT 2026/118', 'note': 'ok'})
    d = r.get_json()
    check('con consegna e fattura si chiude', r.status_code == 200, d)
    oggi = orario.oggi_locale().isoformat()
    check('fattura salvata, data di default = oggi (Italia)',
          d['ordine']['numero_fattura'] == 'FT 2026/118'
          and d['ordine']['data_fattura'] == oggi + 'T00:00:00', d['ordine'])
    check('in archivio, con chi e quando',
          d['ordine']['fase'] == 'archivio' and d['ordine']['chiuso_da_id'] == 'elena'
          and (d['ordine']['data_chiusura_amministrativa'] or '').endswith('Z'), d['ordine'])
    r = c.post('/api/ordini/o-laser/chiudi',
               json={'user_id': 'elena', 'numero_fattura': 'FT 3'})
    check('richiudere un archiviato: 409', r.status_code == 409, r.status_code)

    # Via vecchia: stesse regole
    r = c.post('/api/orders/o-legacy/chiudi-amministrativo',
               json={'user_id': 'elena', 'numero_ddt': 'DDT 77', 'data_ddt': '2026-09-20',
                     'numero_fattura': 'FT 77'})
    check('chiudi-amministrativo senza consegna: rifiutato',
          r.status_code == 400 and r.get_json().get('codice') == 'sequenza', r.get_json())
    check('...ma il DDT passato e stato registrato nel campo buono',
          leggi('o-legacy').ddt_numero == 'DDT 77', leggi('o-legacy').ddt_numero)
    c.post('/api/ordini/o-legacy/consegna', json={'user_id': 'elena', 'completa': True})
    r = c.post('/api/orders/o-legacy/chiudi-amministrativo',
               json={'user_id': 'elena', 'numero_fattura': ''})
    check('chiudi-amministrativo senza fattura: rifiutato',
          r.status_code == 400 and r.get_json().get('codice') == 'fattura_mancante', r.get_json())
    r = c.post('/api/orders/o-legacy/chiudi-amministrativo',
               json={'user_id': 'elena', 'numero_fattura': 'FT 77', 'data_fattura': '2026-09-21'})
    d = r.get_json()
    check('chiudi-amministrativo consegnato + fattura: forma vecchia',
          r.status_code == 200 and d.get('status') == 'CHIUSO' and d.get('order_id') == 'o-legacy'
          and 'data_chiusura' in d, d)
    r = c.post('/api/orders/o-legacy/riapri', json={'user_id': 'elena'})
    check('riapri (via vecchia) riporta fra i consegnati',
          r.status_code == 200 and riga('o-legacy')['fase'] == 'consegnato', r.get_json())
    check('e toglie la data di chiusura', leggi('o-legacy').data_chiusura_amministrativa is None)
    c.post('/api/ordini/o-legacy/chiudi', json={'user_id': 'elena', 'numero_fattura': 'FT 77'})

    # =====================================================================
    print('\n3) Badge "da fatturare" == vista "consegnati"')
    c.post('/api/ordini/o-tagliato/consegna', json={'user_id': 'elena', 'completa': True})
    c.post('/api/ordini/o-vecchio-ddt/consegna',
           json={'user_id': 'elena', 'completa': False, 'note': 'mancano 2 staffe'})
    viste = c.get('/api/ordini/viste').get_json()
    consegnati = sorted(o['id'] for o in viste['ordini'] if o['fase'] == 'consegnato')
    n_badge = c.get('/api/ordini-da-fatturare/count').get_json()['count']
    lista = c.get('/api/ordini-da-fatturare?limit=100').get_json()['data']
    check('contatore == conteggio della vista',
          n_badge == viste['conteggi']['consegnato'] == 2, (n_badge, viste['conteggi']))
    check('stessi ordini', sorted(o['id'] for o in lista['orders']) == consegnati,
          (lista['orders'], consegnati))
    check('un ordine solo completato NON e da fatturare',
          'o-dis' not in consegnati and 'o-legacy' not in consegnati)
    parz = [o for o in lista['orders'] if o['id'] == 'o-vecchio-ddt'][0]
    check('la consegna parziale e visibile ma non chiudibile',
          parz['passo'] == 'consegna_parziale' and parz['chiudibile'] is False, parz)
    r = c.get('/api/ordini/viste?fase=consegnato').get_json()
    campi = ('ddt_numero', 'ddt_data', 'numero_fattura', 'data_fattura', 'consegna',
             'completamento', 'completato_da', 'prezzo_quotato', 'cliente',
             'numero_ordine', 'data_consegna', 'taglio_stato', 'passo')
    check('le righe delle viste portano i campi che servono',
          all(all(k in o for k in campi) for o in r['ordini']), list(r['ordini'][0]))

    # =====================================================================
    print('\n4) DDT: un campo solo')
    rr = riga('o-vecchio-ddt')
    check('il DDT scritto nelle colonne vecchie si vede nella vista nuova',
          rr['ddt_numero'] == 'DDT-OLD-7' and rr['numero_ddt'] == 'DDT-OLD-7'
          and rr['ddt_data'] == '2026-09-01T00:00:00', rr)
    s = models.SessionLocal()
    try:
        s.add(ordine('o-utc-ddt', status='DA_FATTURARE', ddt_numero='DDT 5',
                     ddt_data=datetime(2026, 3, 10, 23, 30)))  # 00:30 dell'11 in Italia
        s.commit()
    finally:
        s.close()
    toccati = _unifica_ddt(_ENG, __import__('sqlalchemy').inspect(_ENG))
    o = leggi('o-vecchio-ddt')
    check('migrazione: numero copiato nel campo buono', o.ddt_numero == 'DDT-OLD-7', o.ddt_numero)
    check('migrazione: data copiata', o.ddt_data == datetime(2026, 9, 1), o.ddt_data)
    check('migrazione: data-istante UTC diventa il giorno italiano',
          leggi('o-utc-ddt').ddt_data == datetime(2026, 3, 11), leggi('o-utc-ddt').ddt_data)
    check('migrazione idempotente',
          toccati >= 2 and _unifica_ddt(_ENG, __import__('sqlalchemy').inspect(_ENG)) == 0,
          toccati)
    r = c.put('/api/orders/o-tagliato/salva-bozza-fattura',
              json={'user_id': 'elena', 'numero_ddt': 'DDT-BOZZA', 'data_ddt': '2026-09-25',
                    'numero_fattura': 'FT bozza'})
    check('bozza dalla scheda vecchia accettata', r.status_code == 200, r.get_json())
    o = leggi('o-tagliato')
    check('la bozza scrive nel campo buono (e allinea il vecchio)',
          o.ddt_numero == 'DDT-BOZZA' and o.numero_ddt == 'DDT-BOZZA'
          and o.ddt_data == datetime(2026, 9, 25), (o.ddt_numero, o.numero_ddt, o.ddt_data))
    check('la vista nuova la vede', riga('o-tagliato')['ddt_numero'] == 'DDT-BOZZA')
    c.post('/api/ordini/o-tagliato/chiudi', json={'user_id': 'elena', 'numero_fattura': 'FT 5'})
    arch = c.get('/api/archive/orders?limit=100').get_json()['data']['orders']
    a = {x['id']: x for x in arch}
    check('archivio: DDT unificato con entrambi i nomi',
          a.get('o-tagliato', {}).get('numero_ddt') == 'DDT-BOZZA'
          and a['o-tagliato'].get('ddt_numero') == 'DDT-BOZZA'
          and a['o-tagliato'].get('data_ddt') == '2026-09-25T00:00:00', a.get('o-tagliato'))
    check('archivio: il DDT della vista nuova si vede anche li',
          a.get('o-laser', {}).get('numero_ddt') == 'DDT 42', a.get('o-laser'))
    check('archivio: fattura della chiusura unica',
          a.get('o-laser', {}).get('numero_fattura') == 'FT 2026/118', a.get('o-laser'))
    det = c.get('/api/orders/o-laser').get_json()
    det = det.get('order', det)
    check('dettaglio ordine: DDT unificato', det.get('ddt_numero') == 'DDT 42'
          and det.get('numero_ddt') == 'DDT 42', {k: det.get(k) for k in ('ddt_numero', 'numero_ddt')})

    # =====================================================================
    print('\n5) Disegni dell ordine')
    cartella = os.path.join(A.DRAWINGS_FOLDER, 'o-dis')
    os.makedirs(cartella, exist_ok=True)
    dxf_quadrato(os.path.join(cartella, '12B100114-00.dxf'))
    dxf_quadrato(os.path.join(A.DRAWINGS_FOLDER, 'o-dis_vecchio.dxf'), 50)  # cartella piatta
    with open(os.path.join(cartella, 'note.txt'), 'w') as f:
        f.write('non e un disegno')
    s = models.SessionLocal()
    try:
        # riga di order_files col percorso di un'altra macchina: stesso nome,
        # non deve comparire due volte
        s.add(OrderFile(id=str(uuid.uuid4()), order_id='o-dis', filename='12B100114-00.dxf',
                        filepath=r'Z:\\altrove\\12B100114-00.dxf', file_type='DXF'))
        s.commit()
    finally:
        s.close()
    r = c.get('/api/orders/o-dis/disegni')
    d = r.get_json()
    nomi = sorted(x['nome'] for x in d.get('disegni', []))
    check('elenco (200) con cartella dell ordine + cartella piatta, senza doppioni',
          r.status_code == 200 and nomi == ['12B100114-00.dxf', 'vecchio.dxf'], d)
    x = [x for x in d['disegni'] if x['nome'] == '12B100114-00.dxf'][0]
    check('link e dimensione',
          x['url_dxf'] == '/api/orders/o-dis/dxf/12B100114-00.dxf'
          and x['url_svg'] == '/api/orders/o-dis/dxf/12B100114-00.dxf/svg'
          and x['dimensione'] > 0, x)
    check('zip e cartella condivisa nel contratto',
          d['url_zip'] == '/api/orders/o-dis/disegni.zip'
          and d['cartella_condivisa']['configurata'] is True
          and d['cartella_condivisa']['esportato'] is False
          and d['cartella_condivisa']['percorso'], d['cartella_condivisa'])
    r = c.get(x['url_dxf'])
    check('download come allegato col suo nome',
          r.status_code == 200 and 'attachment' in r.headers.get('Content-Disposition', '')
          and '12B100114-00.dxf' in r.headers.get('Content-Disposition', ''),
          (r.status_code, r.headers.get('Content-Disposition')))
    r = c.get('/api/orders/o-dis/dxf/vecchio.dxf')
    check('anche il disegno della cartella piatta', r.status_code == 200, r.status_code)
    r = c.get(x['url_svg'])
    check('anteprima SVG', r.status_code == 200 and r.mimetype == 'image/svg+xml'
          and b'<svg' in r.data[:500], (r.status_code, r.mimetype, r.data[:80]))
    for ostile in ('..%5C..%5Capp.py', '..%2F..%2Fbackend%2Fapp.py', 'note.txt',
                   '%2E%2E', 'non-esiste.dxf'):
        r = c.get('/api/orders/o-dis/dxf/' + ostile)
        check(f'nome ostile/assente "{ostile}" -> 404', r.status_code == 404, r.status_code)
    r = c.get('/api/orders/..%2F..%2Fbackend/disegni')
    check('ordine ostile -> 404', r.status_code == 404, r.status_code)
    r = c.get('/api/orders/o-dis/disegni.zip')
    ok_zip = r.status_code == 200
    nomi_zip = sorted(zipfile.ZipFile(io.BytesIO(r.data)).namelist()) if ok_zip else []
    check('zip di tutti i disegni', nomi_zip == ['12B100114-00.dxf', 'vecchio.dxf'],
          (r.status_code, nomi_zip))
    r = c.get('/api/orders/o-nuovo/disegni')
    check('ordine senza disegni: elenco vuoto', r.status_code == 200
          and r.get_json()['disegni'] == [], r.get_json())
    r = c.get('/api/orders/o-dis/pdf')
    check('senza PDF: 404 con codice pdf_assente',
          r.status_code == 404 and r.get_json() == {
              'success': False, 'codice': 'pdf_assente',
              'error': "Quest'ordine non ha un PDF allegato"}, r.get_json())

    # =====================================================================
    print('\n6) Distinta di un ordine nato da un preventivo')
    pr = PreventivoManager.create('Cliente Beta', 'paolo', quantita=2)
    pid = pr['id'] if isinstance(pr, dict) else pr
    tmp_prev = os.path.join(A.UPLOAD_FOLDER, 'preventivi_tmp', pid)
    os.makedirs(tmp_prev, exist_ok=True)
    dxf_quadrato(os.path.join(tmp_prev, 'a1.dxf'))
    dxf_quadrato(os.path.join(tmp_prev, 'b1.dxf'))
    r1 = PreventivoManager.replace_articoli(pid, [
        {'codice': 'A1', 'quantita': 3, 'materiale': 'S235', 'spessore_mm': 3,
         'dxf_filename': 'a1.dxf', 'pieghe': 2, 'costo_base_override': 5},
        {'codice': 'B1', 'quantita': 2, 'materiale': 'INOX_304', 'spessore_mm': 2,
         'dxf_filename': 'b1.dxf', 'codice_assieme': 'ASS1', 'costo_base_override': 4,
         'saldatura_ml': 0.5},
    ])
    r2 = PreventivoManager.replace_assiemi(pid, [{'codice_assieme': 'ASS1', 'qty': 4,
                                                  'ore_montaggio': 1, 'costo': 20}])
    r3 = PreventivoManager.replace_tubolari(pid, [{'codice_assieme': 'ASS1',
                                                   'profilo': 'Quadro 40x40 sp.2mm',
                                                   'lunghezza_m': 0.5, 'qty': 2,
                                                   'n_tagli_dritti': 2}])
    r4 = PreventivoManager.replace_piastre(pid, [{'spessore_mm': 10, 'area_dm2': 1.5,
                                                  'materiale': 'S235', 'costo': 3}])
    check('preventivo preparato', not any(isinstance(x, dict) and x.get('error')
                                          for x in (r1, r2, r3, r4)), (r1, r2, r3, r4))
    s = models.SessionLocal()
    try:
        s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'INVIATO'
        s.commit()
    finally:
        s.close()
    acc = PreventivoManager.accetta_e_crea_ordine(pid, 'paolo')
    oid = acc.get('order_id')
    check('accettazione: ordine creato', acc.get('success') and oid, acc)
    stats = A._copy_cleaned_dxf_to_drawings(pid, oid)
    check('i DXF passano in drawings/<order_id>/',
          stats.get('copied_original_fallback') == 2
          and os.path.isfile(os.path.join(A.DRAWINGS_FOLDER, oid, 'a1.dxf')), stats)
    d = c.get(f'/api/orders/{oid}/disegni').get_json()
    check('...e l ordine li vede', sorted(x['nome'] for x in d['disegni']) == ['a1.dxf', 'b1.dxf'], d)
    r = c.get(f'/api/orders/{oid}/dxf/a1.dxf/svg')
    check('...e li mostra in anteprima', r.status_code == 200, r.status_code)
    r = c.get(f'/api/orders/{oid}/distinta')
    d = r.get_json()
    check('distinta (200) dal preventivo', r.status_code == 200
          and d['origine'] == 'preventivo', d)
    per = {x['codice']: x for x in d['righe']}
    check('articolo sciolto: quantita x lotto (3 x 2 = 6), col disegno',
          per['A1']['quantita'] == 6 and per['A1']['disegno'] == 'a1.dxf'
          and per['A1']['assieme'] is None and per['A1']['spessore_mm'] == 3
          and per['A1']['materiale'] == 'S235', per.get('A1'))
    check('lavorazioni leggibili', 'Taglio laser' in per['A1']['lavorazioni']
          and any('Piegatura' in l for l in per['A1']['lavorazioni']), per['A1'])
    check('componente di assieme: quantita x assiemi (2 x 4 = 8)',
          per['B1']['quantita'] == 8 and per['B1']['assieme'] == 'ASS1'
          and per['B1']['disegno'] == 'b1.dxf', per.get('B1'))
    tub = [x for x in d['righe'] if x.get('tipo') == 'tubolare'][0]
    check('tubolare dell assieme: 2 x 4 = 8', tub['quantita'] == 8 and tub['assieme'] == 'ASS1', tub)
    pia = [x for x in d['righe'] if x.get('tipo') == 'piastra'][0]
    check('piastra sciolta: 1', pia['quantita'] == 1, pia)
    check('totale pezzi (assieme escluso) = 6 + 8 + 8 + 1',
          d['totale_pezzi'] == 23, d['totale_pezzi'])
    check('ogni riga ha la forma del contratto',
          all(set(('codice', 'descrizione', 'quantita', 'materiale', 'spessore_mm',
                   'lavorazioni', 'assieme', 'disegno')) <= set(x) for x in d['righe']))
    d = c.get('/api/orders/o-dis/distinta').get_json()
    check('ordine a mano senza articoli: origine "nessuna"',
          d['origine'] == 'nessuna' and d['righe'] == [] and d['totale_pezzi'] == 0, d)

    # =====================================================================
    print('\n7) Dashboard live coerente con le viste')
    s = models.SessionLocal()
    try:
        s.add(OfficinaScan(id=str(uuid.uuid4()), order_id='o-dis', operatore_id='enzo',
                           timestamp_inizio=datetime.utcnow() - timedelta(hours=30),
                           timestamp_fine=datetime.utcnow() - timedelta(hours=29)))
        s.commit()
    finally:
        s.close()
    live = c.get('/api/dashboard-live').get_json()
    cont = osv.elenco()['conteggi']
    check('in produzione == ordini aperti',
          live['snapshot']['ordini_in_produzione'] == cont['aperto'], (live, cont))
    check('pronti == pronti per DDT (non i tagliati in officina)',
          live['snapshot']['ordini_pronti'] == cont['pronto_ddt'] == live['kanban']['pronti'],
          (live, cont))
    check('ricevuti + in lavorazione == in produzione',
          live['kanban']['ricevuti'] + live['kanban']['lavorazione']
          == live['snapshot']['ordini_in_produzione'], live['kanban'])
    check('o-nuovo e ricevuto, o-dis (scansionato) e in lavorazione',
          live['kanban']['ricevuti'] >= 1 and live['kanban']['lavorazione'] >= 1, live['kanban'])
    check('istante della dashboard con la Z', live['timestamp'].endswith('Z'), live['timestamp'])

    # =====================================================================
    print('\n8) Orari con il fuso, date di calendario senza')
    lst = {o['id']: o for o in c.get('/api/orders').get_json()['orders']}
    t = lst['o-tagliato']
    check('taglio: istante con la Z (22:19 italiane = 20:19Z)',
          t['data_taglio_completato'] == '2026-09-28T20:19:00Z', t['data_taglio_completato'])
    check('ricezione con la Z', (t['data_ricezione'] or '').endswith('Z'), t['data_ricezione'])
    check('consegna prevista SENZA fuso (le pagine ne tagliano i primi 10 caratteri)',
          t['data_consegna'] == '2026-10-15T00:00:00', t['data_consegna'])
    check('elenco ordini: DDT unificato', t.get('ddt_numero') == 'DDT-BOZZA', t.get('ddt_numero'))
    rv = riga('o-dis')
    check('vista: completamento/consegna con la Z o null',
          all(v is None or v.endswith('Z') for v in (rv['completamento'], rv['consegna'])), rv)
    check('health con la Z', c.get('/api/health').get_json()['timestamp'].endswith('Z'))
    check('orario: le 22:30 UTC del 29/9 sono il 30/9 in Italia',
          orario.data_locale(datetime(2026, 9, 29, 22, 30)) == date(2026, 9, 30))
    ini, fine = orario.giorno_locale_in_utc(date(2026, 9, 30))
    check('orario: il 30/9 italiano va dalle 22:00Z del 29 alle 22:00Z del 30',
          ini == datetime(2026, 9, 29, 22) and fine == datetime(2026, 9, 30, 22), (ini, fine))

    print('\n' + '=' * 60)
    print(f'PASSATI: {OK}   FALLITI: {len(KO)}')
    if KO:
        print('Falliti: ' + ', '.join(KO))
    print('=' * 60)
    return 1 if KO else 0


if __name__ == '__main__':
    try:
        code = main()
    finally:
        A.BarcodeManager.load_config = _config_vera
    try:
        _ENG.dispose()
        os.remove(_TMP)
    except Exception:
        pass
    shutil.rmtree(_CARTELLE, ignore_errors=True)
    sys.exit(code)
