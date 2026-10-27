"""Il giro ufficio <-> laser: quello che va avanti e quello che torna indietro.

Verifica del 13/10/2026 (giro completo con i dati veri, su una copia):

 1. "Forse finiti": quando il laser preme "Taglio fatto" l'ufficio non
    sapeva niente. Ora l'ordine tagliato e senza altro in sospeso compare come
    PROPOSTA nell'ufficio; "Finito" lo porta a "DDT da fare" (la stessa
    /completamento), "Non ancora" lo rimanda di due giorni (nello storico).
    Non compare se: taglio non fatto, da procurare non arrivato, lavorazione
    esterna, gia' completato, eliminato.

Gira su DATABASE TEMPORANEO, impostazioni temporanee, LANTEK SIMULATO.
Esecuzione: python app/tests/test_flusso_ufficio_laser.py
"""
import json
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
os.environ['FERROTRACK_ESEMPI_DIR'] = tempfile.mkdtemp(prefix='ft_fl_esempi_')
_CFG = os.path.join(tempfile.mkdtemp(prefix='ft_fl_cfg_'), 'app_config.json')
with open(_CFG, 'w', encoding='utf-8') as f:
    json.dump({'laser_calendario': {'giorni': [1, 2, 3, 4, 5], 'eccezioni': {}}}, f)
os.environ['FERROTRACK_CONFIG'] = _CFG

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, Order  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_fl_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend import lantek as L  # noqa: E402
from backend import ordini_service as OS  # noqa: E402
from backend.database import ConfigManager  # noqa: E402
from backend.preventivi import tempi_laser as TL  # noqa: E402

ConfigManager.load_config()
assert os.path.abspath(ConfigManager._CONFIG_PATH) == os.path.abspath(_CFG), 'impostazioni NON temporanee: fermo'
_CARTELLE = tempfile.mkdtemp(prefix='ft_fl_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
A.PDFS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'pdfs')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)
os.makedirs(A.PDFS_FOLDER, exist_ok=True)

TL._leggi_lantek = lambda codici: None
L.pezzi_in_lantek = lambda codici: {'disponibile': False, 'errore': 'prova', 'pezzi': {}}
L.importa_pezzi_dxf = lambda *a, **k: (_ for _ in ()).throw(AssertionError('Lantek non si lancia nei test'))
L.importa_xml = lambda *a, **k: (_ for _ in ()).throw(AssertionError('Lantek non si lancia nei test'))
L.expert_aperto = lambda: False   # Lantek Expert chiuso (sul PC vero puo' essere aperto)

from tests.accesso_aiuto import modalita, entra, ufficio_pronto  # noqa: E402

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


modalita('protetto')
UFF = ufficio_pronto(A.app)
LAS = entra(A.app, 'laser')


def nuovo_ordine(**k):
    oid = str(uuid.uuid4())
    s = models.SessionLocal()
    s.add(Order(id=oid, cliente=k.pop('cliente', 'DECA S.r.l.'), numero_ordine=k.pop('numero', 'FL-' + oid[:4]),
                data_consegna=datetime.utcnow() + timedelta(days=5), status='RICEVUTO', **k))
    s.commit(); s.close()
    return oid


def forse_finiti():
    print('1) Forse finiti: il ritorno dal laser all\'ufficio')
    tagliato = nuovo_ordine(taglio_richiesto=True)
    nontagliato = nuovo_ordine(taglio_richiesto=True)
    esterno = nuovo_ordine(taglio_richiesto=True, lavorazione_esterna=True)
    procura = nuovo_ordine(taglio_richiesto=True)
    for oid in (tagliato, esterno, procura):
        r = LAS.post(f'/api/orders/{oid}/mark-laser-done', json={})
        check(f'il laser segna "Taglio fatto" ({oid[:4]})', r.status_code == 200, r.get_json())
    from backend.models import DaProcurare
    s = models.SessionLocal()
    s.add(DaProcurare(order_id=procura, chiave='art:x', tipo='tubo', quantita=2, stato='ordinato'))
    s.commit(); s.close()

    r = UFF.get('/api/ordini/forse-finiti')
    ids = [x['id'] for x in (r.get_json() or {}).get('ordini', [])]
    check("l'ordine tagliato compare fra i forse finiti", tagliato in ids, ids)
    check('non tagliato: no', nontagliato not in ids)
    check('lavorazione esterna dichiarata: no', esterno not in ids)
    check('da procurare non ancora arrivato: no', procura not in ids)
    riga = next((x for x in r.get_json()['ordini'] if x['id'] == tagliato), {})
    check('dice quando e\' stato tagliato', bool(riga.get('tagliato_il')), riga)
    check('il laser non vede questa lista (e\' dell\'ufficio)', LAS.get('/api/ordini/forse-finiti').status_code in (401, 403))

    r = UFF.post(f'/api/ordini/{tagliato}/forse-finito/non-ancora', json={})
    check('"Non ancora": risposta 200', r.status_code == 200, r.get_json())
    ids = [x['id'] for x in UFF.get('/api/ordini/forse-finiti').get_json()['ordini']]
    check('"Non ancora": sparisce per ora', tagliato not in ids, ids)
    from backend.models import AuditLog
    s = models.SessionLocal()
    a = s.query(AuditLog).filter(AuditLog.action == 'FORSE_FINITO_NON_ANCORA', AuditLog.entity_id == tagliato).first()
    if a:
        a.timestamp = datetime.utcnow() - timedelta(days=OS.GIORNI_NON_ANCORA + 1)
        s.commit()
    s.close()
    check('"Non ancora" e\' nello storico', a is not None)
    ids = [x['id'] for x in UFF.get('/api/ordini/forse-finiti').get_json()['ordini']]
    check('passati i giorni, torna a chiederlo', tagliato in ids, ids)

    r = UFF.post(f'/api/ordini/{tagliato}/completamento', json={})
    check('"Finito": la stessa /completamento dell\'ufficio', r.status_code == 200, r.get_json())
    ids = [x['id'] for x in UFF.get('/api/ordini/forse-finiti').get_json()['ordini']]
    check('confermato: non e\' piu\' fra i forse finiti', tagliato not in ids)
    v = {o['id']: o for o in UFF.get('/api/ordini/viste').get_json()['ordini']}
    check('e nell\'ufficio e\' "DDT da fare"', v[tagliato]['passo'] == 'attesa_ddt', v[tagliato].get('passo'))

    print('   (regola pura)')

    class _O:
        is_deleted = False
        status = 'RICEVUTO'
        taglio_completato = True
        data_taglio_completato = datetime(2026, 10, 10, 10)
        lavorazione_esterna = None
        data_consegna_effettiva = None
        data_completamento_operativo = None
        ddt_numero = numero_ddt = None
    o = _O()
    check('regola: tagliato e niente in sospeso', OS.motivo_non_forse_finito(o) is None)
    check('regola: fase d\'officina aperta', OS.motivo_non_forse_finito(o, n_fasi_aperte=1) == "fase d'officina aperta")
    check('regola: "Non ancora" PRIMA del taglio non conta (ritagliato dopo)',
          OS.motivo_non_forse_finito(o, rimandato_il=datetime(2026, 10, 9), adesso=datetime(2026, 10, 10, 12)) is None)
    check('regola: "Non ancora" dopo il taglio, entro 2 giorni',
          OS.motivo_non_forse_finito(o, rimandato_il=datetime(2026, 10, 10, 11), adesso=datetime(2026, 10, 11)) == 'rimandato')


def preventivo_accettato():
    print('\n2) Preventivo accettato: PDF del cliente al laser e "va al laser"')
    from backend.database import PreventivoManager
    from backend.models import Preventivo

    def nuovo_prev(rif):
        pr = PreventivoManager.create('DECA S.r.l.', 'paolo', quantita=1, numero_ordine_cliente=rif)
        pid = pr['id'] if isinstance(pr, dict) else pr
        PreventivoManager.replace_articoli(pid, [{'codice': 'P-1-00', 'quantita': 2, 'materiale': 'S235', 'spessore_mm': 3,
                                                  'costo_base_override': 5, 'perimetro_taglio_m': 0.8, 'area_dm2': 2.9}])
        s = models.SessionLocal()
        s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'INVIATO'
        s.commit(); s.close()
        return pid

    pid = nuovo_prev('ACC-1')
    r = UFF.get(f'/api/preventivi/{pid}/ordine-cliente')
    check('la finestra sa se manca l\'ordine del cliente', r.status_code == 200 and r.get_json().get('file') is None, r.get_json())
    r = UFF.post(f'/api/preventivi/{pid}/accetta', json={'al_laser': True})
    d = r.get_json() or {}
    oid = d.get('order_id')
    check('accettato con "Va al laser": risposta al_laser', r.status_code == 200 and d.get('al_laser') is True, d)
    s = models.SessionLocal()
    o = s.query(Order).filter(Order.id == oid).first()
    check('...e l\'ordine nasce fra quelli da tagliare', o is not None and o.taglio_richiesto is True)
    s.close()
    pdf = b'%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n'
    import io
    r = UFF.post(f'/api/orders/{oid}/replace-pdf', data={'file': (io.BytesIO(pdf), 'ORDINE DECA 1300.pdf')},
                 content_type='multipart/form-data')
    check('PDF del cliente allegato subito dopo (stessa finestra)', r.status_code == 200, r.get_json())
    r = LAS.get(f'/api/orders/{oid}/pdf')
    check('il laser vede quel PDF', r.status_code == 200 and r.data.startswith(b'%PDF'), r.status_code)
    lst = {x['id']: x for x in (LAS.get('/api/orders').get_json() or {}).get('orders', [])}
    check('e nella sua lista l\'ordine ha il PDF', bool((lst.get(oid) or {}).get('pdf_file')), lst.get(oid))

    pid2 = nuovo_prev('ACC-2')
    r = UFF.post(f'/api/preventivi/{pid2}/accetta', json={})
    oid2 = (r.get_json() or {}).get('order_id')
    s = models.SessionLocal()
    o2 = s.query(Order).filter(Order.id == oid2).first()
    check('senza spunta: resta da smistare (come prima)', o2 is not None and o2.taglio_richiesto is None)
    s.close()
    t = open(os.path.join(_APP, 'frontend', 'impiegata.html'), encoding='utf-8').read()
    check('la finestra di conferma avvisa se manca il PDF e lo fa allegare',
          'Manca l’ordine del cliente (PDF).' in t and "replace-pdf" in t and 'id="acc-laser"' in t)


def notifiche():
    print('\n3) Notifiche: solo quelle da fare, una per ordine')
    from backend.models import Notification

    def avviso(uid, oid, tipo, titolo, cat='attiva', letto=False):
        s = models.SessionLocal()
        s.add(Notification(id=str(uuid.uuid4()), user_id=uid, order_id=oid, title=titolo, message=titolo,
                           notification_type=tipo, notification_category=cat, is_read=letto, is_deleted=False))
        s.commit(); s.close()

    s = models.SessionLocal()
    for n in s.query(Notification).all():
        n.is_deleted = True
    s.commit(); s.close()
    smistato = nuovo_ordine(taglio_richiesto=True)
    da_smistare = nuovo_ordine()
    eliminato = nuovo_ordine(taglio_richiesto=True, is_deleted=True)
    tagliato = nuovo_ordine(taglio_richiesto=True, taglio_completato=True, data_taglio_completato=datetime.utcnow())
    LZ = 'postazione-laser'
    avviso(LZ, smistato, 'order', 'Nuovo ordine', 'informativa')
    avviso(LZ, da_smistare, 'order', 'Nuovo ordine', 'informativa')
    avviso(LZ, eliminato, 'consegna_rischio', 'Consegna in scadenza')
    avviso(LZ, tagliato, 'consegna_rischio', 'Consegna in scadenza')
    avviso(LZ, smistato, 'consegna_rischio', 'Consegna in scadenza')
    avviso(LZ, smistato, 'consegna_rischio', 'Consegna SCADUTA')
    avviso(LZ, None, 'lantek', 'Senza ordine')
    d = (LAS.get('/api/notifications?limit=50').get_json() or {}).get('data') or {}
    lista = d.get('notifications') or []
    per = {(x['order_id'], x['notification_type']): x for x in lista}
    check('"Nuovo ordine" gia\' smistato dal laser: tolto', (smistato, 'order') not in per, list(per))
    check('"Nuovo ordine" da smistare: resta', (da_smistare, 'order') in per)
    check('ordine eliminato: tolto', (eliminato, 'consegna_rischio') not in per)
    check('consegna a rischio di un ordine gia\' tagliato: tolta', (tagliato, 'consegna_rischio') not in per)
    g = per.get((smistato, 'consegna_rischio')) or {}
    check('due avvisi uguali sullo stesso ordine: uno solo, col numero', g.get('ripetute') == 2
          and '(+1 avviso precedente)' in (g.get('message') or ''), g)
    check('gli avvisi da fare sono segnati importanti', g.get('importante') is True
          and per[(da_smistare, 'order')].get('importante') is False)
    check('quelli senza ordine restano', any(x['title'] == 'Senza ordine' for x in lista))
    check('il numero sul campanello conta i gruppi', d.get('unread_count') == len(lista) == 3, (d.get('unread_count'), len(lista)))
    r = LAS.put(f"/api/notifications/{g['id']}/read", json={})
    d = (LAS.get('/api/notifications?limit=1').get_json() or {}).get('data') or {}
    check('letta una del gruppo: lette tutte (il campanello scende di 1)', r.status_code == 200 and d.get('unread_count') == 2,
          d.get('unread_count'))
    from backend.models import AuditLog
    s = models.SessionLocal()
    check('le tolte sono scritte nello storico', s.query(AuditLog).filter(AuditLog.action == 'NOTIFICHE_SUPERATE').count() >= 1)
    s.close()
    # ufficio: "Nuovo ordine da protocollare" superato quando l'ordine e' avanti
    UFF.post(f'/api/ordini/{da_smistare}/completamento', json={})
    avviso('postazione-amministrazione', da_smistare, 'order', 'Nuovo ordine da protocollare')
    avviso('postazione-amministrazione', smistato, 'order', 'Nuovo ordine da protocollare')
    lista = ((UFF.get('/api/notifications').get_json() or {}).get('data') or {}).get('notifications') or []
    ids = {x['order_id'] for x in lista}
    check('ufficio: ordine gia\' avanti nel ciclo, avviso tolto; quello in lavorazione resta',
          da_smistare not in ids and smistato in ids, ids)


def doppioni_bis():
    print('\n4) Ordine ricaricato: "A 001221" dopo "A 001221 BIS"')
    nuovo_ordine(numero='A 009221 BIS', cliente='DECA S.r.l.')
    e = A._ordine_gia_caricato('A 009221', 'DECA S.r.l.')
    check('il BIS gia\' caricato viene segnalato, come variante', e and e['numero_ordine'] == 'A 009221 BIS'
          and e['variante'] is True, e)
    e = A._ordine_gia_caricato('a 009221 bis', 'DECA S.r.l.')
    check('lo stesso numero esatto: doppione, non variante', e and e['variante'] is False, e)
    check('un altro numero no', A._ordine_gia_caricato('A 0092210', 'DECA S.r.l.') is None)
    check('un altro cliente no', A._ordine_gia_caricato('A 009221', 'Poliform') is None)


def postazione_sbagliata():
    print('\n5) "Questa postazione non puo\' farlo": e dove si fa')
    r = LAS.get('/api/ordini/viste')
    d = r.get_json() or {}
    check('dice quale postazione e dove si fa', r.status_code == 403 and d.get('codice') == 'stazione_non_ammessa'
          and '(Laser)' in d.get('error', '') and 'postazione Ufficio' in d.get('error', ''), d)
    t = open(os.path.join(_APP, 'frontend', 'ft-accesso.js'), encoding='utf-8').read()
    check('la pagina mostra la frase del server', "avviso((d && d.error) || 'Questa postazione non può farlo.')" in t)


def eliminati():
    print('\n6) Eliminato dal laser, ripristinato dall\'ufficio')
    oid = nuovo_ordine(taglio_richiesto=True)
    r = LAS.delete(f'/api/orders/{oid}')
    check('il laser elimina', r.status_code == 200, r.get_json())
    r = UFF.get('/api/ordini/eliminati')
    riga = next((x for x in (r.get_json() or {}).get('ordini', []) if x['id'] == oid), None)
    check("l'ufficio lo vede fra gli eliminati, con quando", r.status_code == 200 and riga and riga['eliminato_il'], riga)
    r = UFF.post(f'/api/orders/{oid}/ripristina', json={})
    check("l'ufficio lo ripristina", r.status_code == 200, r.get_json())
    ids = [x['id'] for x in UFF.get('/api/ordini/eliminati').get_json()['ordini']]
    v = {o['id'] for o in UFF.get('/api/ordini/viste').get_json()['ordini']}
    check('torna fra gli ordini e non e\' piu\' fra gli eliminati', oid not in ids and oid in v)
    t = open(os.path.join(_APP, 'frontend', 'impiegata.html'), encoding='utf-8').read()
    check('la pagina dell\'ufficio ha "Eliminati" e "Annulla" dopo Elimina',
          'onclick="apriEliminati()"' in t and "fn: () => ripristinaOrdine(orderId)" in t)


def main():
    forse_finiti()
    preventivo_accettato()
    notifiche()
    doppioni_bis()
    postazione_sbagliata()
    eliminati()
    print(f'\n{OK} ok, {len(KO)} ko')
    if KO:
        print('FALLITI:', *KO, sep='\n  ')
    return 1 if KO else 0


if __name__ == '__main__':
    code = main()
    try:
        _ENG.dispose()
        os.remove(_TMP)
    except OSError:
        pass
    sys.exit(code)
