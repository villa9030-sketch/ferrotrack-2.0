"""Codici "da correggere in Lantek" e "Elimina ordine" dalla pagina laser.

Copre:
 A. "Manda a Lantek" con un codice tenuto fuori scegliendo il valore
    dell'ORDINE (in Lantek 2 mm, nell'ordine 3 mm):
    1. resta scritto "da correggere" (cosa, chi, quando) con l'audit
    2. l'ordine non e' pronto ne' "tutto in Lantek": n_correggere, rosso
    3. Lantek letto di nuovo ancora a 2 mm: resta da correggere
    4. Lantek corretto a 3 mm: "corretto", pronto da mandare SOLO quel
       codice, un avviso alla postazione laser (una volta sola)
    5. mandato: la correzione si chiude
    6. tutti i codici tenuti fuori: niente parte, ma 200 e "da correggere"
    7. cambio idea ("va bene Lantek"): si manda subito, la scelta resta per
       la conferma; permessi e codici sbagliati
    8. messo in Lantek a mano (ha l'ordine di produzione): si chiude da solo
    9. tenuto fuori un codice senza differenza: niente da correggere
 B. Elimina ordine: il laser e l'ufficio si', il tablet no; soft delete con
    audit; esce dall'elenco del laser; ripristino con audit.

Gira su DATABASE TEMPORANEO e LANTEK SIMULATO (nessun programma di Lantek,
nessuna lettura del database di Lantek).
Esecuzione: python app/tests/test_lantek_correzioni.py
"""
import os
import shutil
import sys
import tempfile
import uuid

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
_ESEMPI = tempfile.mkdtemp(prefix='ft_lc_esempi_')
os.environ['FERROTRACK_ESEMPI_DIR'] = _ESEMPI

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Preventivo, Order, AuditLog, Notification, LantekCorrezione  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_lc_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend import lantek as L  # noqa: E402
from backend import api_laser as AL  # noqa: E402
from backend.database import PreventivoManager  # noqa: E402
from backend.preventivi import tempi_laser as TL  # noqa: E402

_CARTELLE = tempfile.mkdtemp(prefix='ft_lc_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)

# ── Lantek simulato ─────────────────────────────────────────────────────
TL._leggi_lantek = lambda codici: None
PEZZI_LANTEK = {}
for _c, _sp in (('P-OK-00', 3.0), ('P-SPESS-00', 2.0), ('Q-SPESS-00', 2.0), ('R-SPESS-00', 2.0), ('S-OK-00', 3.0)):
    PEZZI_LANTEK[_c] = {'esiste': True, 'codice_lantek': _c, 'materiale': 'FERRO', 'spessore': _sp, 'revisioni': []}
LANTEK_SU = [True]
LETTURE = []
CHIAMATE = []
ORDINI_LT = {}


def pezzi_in_lantek(codici):
    LETTURE.append(list(codici))
    if not LANTEK_SU[0]:
        return {'disponibile': False, 'errore': 'Lantek non risponde: prova', 'pezzi': {}}
    vuoto = {'esiste': False, 'codice_lantek': None, 'materiale': None, 'spessore': None, 'revisioni': []}
    return {'disponibile': True, 'errore': None, 'pezzi': {c: dict(PEZZI_LANTEK.get(c) or vuoto) for c in codici}}


def xml_finto(contenuto, nome, cartella, attesa_s=300):
    CHIAMATE.append(('xml', contenuto))
    for c in PEZZI_LANTEK:
        if ('"%s"' % c).encode() in contenuto:
            ORDINI_LT[c] = 1.0
    return {'eseguito': True, 'errore': None, 'file_rapporto': 'x',
            'rapporto': {'totale': 1, 'ok': 1, 'avvisi': 0, 'con_errore': 0, 'errori': []}}


def pezzi_finti(*a, **k):
    CHIAMATE.append(('pezzi', None))
    return {'eseguito': False, 'errore': 'non deve servire'}


L.pezzi_in_lantek = pezzi_in_lantek
L.clienti_lantek = lambda: ['DECA']
L.ordini_in_lantek = lambda commessa: dict(ORDINI_LT)
L.ordini_fatti_in_lantek = lambda commessa: {}
L.pezzi_dell_ordine = lambda commessa: {}
L.procesos_disponibile = lambda: True
L.materiali_lantek_noti = lambda: {'FERRO', 'INOX'}
L.xmlimporter_disponibile = lambda: True
L.importa_pezzi_dxf = pezzi_finti
L.importa_xml = xml_finto

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


def audit(azione, oid=None):
    s = models.SessionLocal()
    try:
        q = s.query(AuditLog).filter(AuditLog.action == azione)
        if oid:
            q = q.filter(AuditLog.entity_id == oid)
        return [(a.user_name, a.detail) for a in q.all()]
    finally:
        s.close()


def correzioni(oid):
    s = models.SessionLocal()
    try:
        return [{'codice': r.codice, 'sp': r.spessore_voluto, 'sp_lt': r.spessore_lantek, 'mat': r.materiale_voluto,
                 'da': r.creato_da, 'il': r.creato_il, 'corretto': r.corretto_il, 'chiuso': r.chiuso_il, 'esito': r.esito}
                for r in s.query(LantekCorrezione).filter(LantekCorrezione.order_id == oid).all()]
    finally:
        s.close()


def avvisi_laser():
    s = models.SessionLocal()
    try:
        return [n.message for n in s.query(Notification).filter(Notification.user_id == 'postazione-laser').all()
                if 'corretto in Lantek' in (n.message or '')]
    finally:
        s.close()


def nuovo_ordine(numero, codici):
    base = {'quantita': 2, 'materiale': 'S235', 'spessore_mm': 3, 'costo_base_override': 5,
            'perimetro_taglio_m': 0.8, 'n_forature': 0, 'area_dm2': 3.0, 'bbox_w_mm': 200, 'bbox_h_mm': 150}
    pr = PreventivoManager.create('DECA S.r.l.', 'paolo', quantita=1, numero_ordine_cliente=numero)
    pid = pr['id'] if isinstance(pr, dict) else pr
    PreventivoManager.replace_articoli(pid, [dict(base, codice=c) for c in codici])
    s = models.SessionLocal()
    s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'INVIATO'
    s.commit(); s.close()
    oid = PreventivoManager.accetta_e_crea_ordine(pid, 'paolo').get('order_id')
    A.OrderManager.smista(oid, True, user_id='laser')
    return oid


def riep(c, oid, fresco=True):
    AL.invalida(oid)
    return ((c.get('/api/laser/ordini' + ('?fresco=1' if fresco else '')).get_json() or {}).get('ordini') or {}).get(oid) or {}


def main():
    from tests.accesso_aiuto import postazioni, entra, modalita, persona, PIN_UFFICIO
    s = models.SessionLocal()
    s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
    s.commit(); s.close()
    modalita('protetto')
    postazioni()
    persona('elena', 'Elena Ufficio', pin=PIN_UFFICIO)
    laser = entra(A.app, 'laser')
    rep = entra(A.app, 'reparto')
    uff = entra(A.app, 'ufficio', pin=PIN_UFFICIO)

    print('A1) Manda a Lantek tenendo fuori il codice diverso (scelto l\'ordine)')
    oid = nuovo_ordine('5001', ['P-OK-00', 'P-SPESS-00'])
    o = laser.get(f'/api/orders/{oid}/laser').get_json() or {}
    check('prima: pronto, 2 da mandare, differenza 3 mm / 2 mm su P-SPESS',
          o.get('pronto') and o.get('n_invio') == 2
          and [(x['codice'], x['ordine'], x['lantek']) for x in o.get('differenze') or []] == [('P-SPESS-00', '3 mm', '2 mm')],
          (o.get('pronto'), o.get('n_invio'), o.get('differenze'), o.get('bloccati')))
    u = f'/api/orders/{oid}/lantek-invia'
    r = laser.post(u, json={'codici': ['P-OK-00'], 'nuovi': [], 'esclusi': ['P-SPESS-00']})
    j = r.get_json() or {}
    xml = next((c[1] for c in CHIAMATE if c[0] == 'xml'), b'')
    check('mandato solo P-OK', r.status_code == 200 and b'"P-OK-00"' in xml and b'P-SPESS-00' not in xml, (r.status_code, j))
    check('la risposta dice cosa resta da correggere', j.get('da_correggere') == ['P-SPESS-00'], j)
    cc = correzioni(oid)
    check('scritto: P-SPESS da portare a 3 mm (in Lantek 2 mm), chi e quando',
          len(cc) == 1 and cc[0]['codice'] == 'P-SPESS-00' and cc[0]['sp'] == 3.0 and cc[0]['sp_lt'] == 2.0
          and cc[0]['mat'] is None and cc[0]['da'] == 'Laser' and cc[0]['il'] and not cc[0]['chiuso'], cc)
    check('audit LANTEK_DA_CORREGGERE', any('P-SPESS-00' in (d or '') and '3 mm' in (d or '')
                                            for _, d in audit('LANTEK_DA_CORREGGERE', oid)), audit('LANTEK_DA_CORREGGERE', oid))

    print('\nA2) L\'ordine resta "da correggere": non e\' fatto')
    d = riep(laser, oid)
    check('riassunto: 1 da correggere, 0 corretti', d.get('n_correggere') == 1 and d.get('n_corretti') == 0, d)
    check('non pronto e non "tutto in Lantek" (P-OK e\' in produzione, P-SPESS fermo)',
          d.get('pronto') is False and d.get('tutto_in_lantek') is False and d.get('n_invio') == 0, d)
    check('la correzione dice cosa scrivere in Lantek', (d.get('correzioni') or [{}])[0].get('cosa') == 'spessore 3 mm (in Lantek 2 mm)'
          and d['correzioni'][0]['stato'] == 'da_correggere', d.get('correzioni'))
    o = laser.get(f'/api/orders/{oid}/laser').get_json() or {}
    check('ordine aperto: lo stesso, e il codice e\' ancora fra quelli da mandare (per la conferma)',
          o.get('n_correggere') == 1 and o['invio']['codici'] == ['P-SPESS-00'] and o.get('usa_lantek') == [], o.get('invio'))

    print('\nA3) Lantek riletto: ancora 2 mm')
    n0 = len(LETTURE)
    d = riep(laser, oid)
    check('Lantek riletto (sola lettura)', len(LETTURE) > n0 and any('P-SPESS-00' in x for x in LETTURE[n0:]))
    check('ancora da correggere, nessun avviso', d.get('n_correggere') == 1 and not avvisi_laser(), (d.get('n_correggere'), avvisi_laser()))
    LANTEK_SU[0] = False
    d = riep(laser, oid)
    check('Lantek non risponde: resta da correggere (non si indovina)', d.get('n_correggere') == 1, d.get('correzioni'))
    LANTEK_SU[0] = True

    print('\nA4) Corretto in Lantek a 3 mm')
    PEZZI_LANTEK['P-SPESS-00']['spessore'] = 3.0
    d = riep(laser, oid)
    check('corretto: pronto da mandare', d.get('n_correggere') == 0 and d.get('n_corretti') == 1
          and d['correzioni'][0]['stato'] == 'corretto' and d.get('pronto') is True and d.get('n_invio') == 1, d)
    av = avvisi_laser()
    check('avviso alla postazione laser, col numero dell\'ordine del cliente',
          av == ['P-SPESS-00 corretto in Lantek: pronto da mandare (DECA S.r.l. 5001)'], av)
    check('audit LANTEK_CORRETTO', len(audit('LANTEK_CORRETTO', oid)) == 1, audit('LANTEK_CORRETTO', oid))
    riep(laser, oid)
    check('l\'avviso non si ripete', len(avvisi_laser()) == 1 and len(audit('LANTEK_CORRETTO', oid)) == 1, avvisi_laser())
    o = laser.get(f'/api/orders/{oid}/laser?fresco=1').get_json() or {}
    check('nella conferma: solo P-SPESS, senza piu\' differenze', o['invio']['codici'] == ['P-SPESS-00'] and not o.get('differenze'), o.get('invio'))

    print('\nA5) Mandato: risolto')
    CHIAMATE.clear()
    r = laser.post(u, json={'codici': ['P-SPESS-00'], 'nuovi': [], 'esclusi': []})
    xml = next((c[1] for c in CHIAMATE if c[0] == 'xml'), b'')
    check('mandato P-SPESS', r.status_code == 200 and b'"P-SPESS-00"' in xml, (r.status_code, r.get_json()))
    cc = correzioni(oid)
    check('correzione chiusa: mandato', cc and cc[0]['chiuso'] and cc[0]['esito'] == 'mandato', cc)
    d = riep(laser, oid)
    check('niente piu\' da correggere: tutto in Lantek', not d.get('correzioni') and d.get('n_correggere') == 0
          and d.get('tutto_in_lantek') is True, d)

    print('\nA6) Tenuto fuori l\'unico codice: niente parte, ma resta scritto')
    oid2 = nuovo_ordine('5002', ['Q-SPESS-00'])
    CHIAMATE.clear()
    r = laser.post(f'/api/orders/{oid2}/lantek-invia', json={'codici': [], 'nuovi': [], 'esclusi': ['Q-SPESS-00']})
    j = r.get_json() or {}
    check('200, niente mandato, Lantek non lanciato', r.status_code == 200 and j.get('mandati') == 0
          and j.get('da_correggere') == ['Q-SPESS-00'] and not CHIAMATE, (r.status_code, j, CHIAMATE))
    d = riep(laser, oid2)
    check('ordine: da correggere, non pronto', d.get('n_correggere') == 1 and d.get('pronto') is False and not d.get('tutto_in_lantek'), d)

    print('\nA7) Cambio idea: va bene il valore di Lantek')
    url = f'/api/orders/{oid2}/lantek-correzioni'
    check('tablet officina: no', rep.post(url, json={'codice': 'Q-SPESS-00', 'azione': 'usa_lantek'}).status_code in (401, 403))
    check('azione sbagliata: 400', laser.post(url, json={'codice': 'Q-SPESS-00', 'azione': 'boh'}).status_code == 400)
    check('codice che non e\' da correggere: 404', laser.post(url, json={'codice': 'P-OK-00', 'azione': 'usa_lantek'}).status_code == 404)
    r = laser.post(url, json={'codice': 'Q-SPESS-00', 'azione': 'usa_lantek'})
    check('salvato', r.status_code == 200 and r.get_json().get('codice') == 'Q-SPESS-00', r.get_json())
    cc = correzioni(oid2)
    check('correzione chiusa: usa_lantek', cc and cc[0]['esito'] == 'usa_lantek' and cc[0]['chiuso'], cc)
    check('audit LANTEK_USA_LANTEK', len(audit('LANTEK_USA_LANTEK', oid2)) == 1)
    d = riep(laser, oid2)
    check('subito pronto da mandare', d.get('pronto') is True and d.get('n_invio') == 1 and d.get('n_correggere') == 0, d)
    o = laser.get(f'/api/orders/{oid2}/laser').get_json() or {}
    check('la conferma sa gia\' la scelta (Lantek)', o.get('usa_lantek') == ['Q-SPESS-00'] and o['invio']['codici'] == ['Q-SPESS-00'], o.get('usa_lantek'))
    r = laser.post(f'/api/orders/{oid2}/lantek-invia', json={'codici': ['Q-SPESS-00'], 'nuovi': [], 'esclusi': []})
    check('si manda com\'e\' in Lantek', r.status_code == 200 and (r.get_json() or {}).get('mandati') == 1, r.get_json())

    print('\nA8) Messo in Lantek a mano: si chiude da solo')
    oid3 = nuovo_ordine('5003', ['R-SPESS-00'])
    laser.post(f'/api/orders/{oid3}/lantek-invia', json={'codici': [], 'nuovi': [], 'esclusi': ['R-SPESS-00']})
    check('da correggere', riep(laser, oid3).get('n_correggere') == 1)
    ORDINI_LT['R-SPESS-00'] = 1.0           # ordine di produzione fatto dal MES
    d = riep(laser, oid3)
    cc = correzioni(oid3)
    check('ha l\'ordine di produzione: correzione chiusa (mandato)', d.get('n_correggere') == 0 and not d.get('correzioni')
          and cc and cc[0]['esito'] == 'mandato', (d.get('correzioni'), cc))

    print('\nA9) Tenuto fuori un codice uguale a Lantek: niente da correggere')
    oid4 = nuovo_ordine('5004', ['S-OK-00'])
    r = laser.post(f'/api/orders/{oid4}/lantek-invia', json={'codici': [], 'nuovi': [], 'esclusi': ['S-OK-00']})
    check('come prima: 409 niente da mandare, nessuna correzione', r.status_code == 409 and not correzioni(oid4), (r.status_code, r.get_json()))

    print('\nB) Elimina ordine dalla pagina laser')
    oid5 = nuovo_ordine('5005', ['S-OK-00'])
    check('tablet officina: non elimina', rep.delete(f'/api/orders/{oid5}').status_code in (401, 403))
    check('tablet officina: non ripristina', rep.post(f'/api/orders/{oid5}/ripristina').status_code in (401, 403))
    check('ordine inesistente: 404', laser.delete('/api/orders/non-esiste').status_code == 404)
    check('ripristino di un ordine non eliminato: 409', laser.post(f'/api/orders/{oid5}/ripristina').status_code == 409)
    r = laser.delete(f'/api/orders/{oid5}')
    s = models.SessionLocal()
    o5 = s.get(Order, oid5)
    s.close()
    check('il laser elimina: soft delete (l\'ordine c\'e\' ancora)', r.status_code == 200 and o5 is not None and o5.is_deleted, (r.status_code, r.get_json()))
    check('audit ORDINE_ELIMINATO (recuperabile), col nome della postazione',
          any('recuperabile' in (d or '') and n == 'Laser' for n, d in audit('ORDINE_ELIMINATO', oid5)), audit('ORDINE_ELIMINATO', oid5))
    ordini = (laser.get('/api/laser/ordini').get_json() or {}).get('ordini') or {}
    aperti = laser.get('/api/orders?aperti=1').get_json()
    aperti = aperti if isinstance(aperti, list) else (aperti or {}).get('orders') or []
    check('esce dall\'elenco del laser e dagli ordini aperti', oid5 not in ordini and oid5 not in [x.get('id') for x in aperti if not x.get('is_deleted')])
    r = laser.post(f'/api/orders/{oid5}/ripristina')
    s = models.SessionLocal()
    o5 = s.get(Order, oid5)
    s.close()
    check('ripristinato', r.status_code == 200 and not o5.is_deleted, (r.status_code, r.get_json()))
    check('audit ORDINE_RIPRISTINATO', len(audit('ORDINE_RIPRISTINATO', oid5)) == 1)
    check('torna nell\'elenco del laser', oid5 in ((laser.get('/api/laser/ordini').get_json() or {}).get('ordini') or {}))
    r = uff.delete(f'/api/orders/{oid5}')
    check('l\'ufficio elimina ancora', r.status_code == 200, (r.status_code, r.get_json()))
    r = uff.post(f'/api/orders/{oid5}/ripristina')
    check('e ripristina', r.status_code == 200 and len(audit('ORDINE_RIPRISTINATO', oid5)) == 2, (r.status_code, r.get_json()))

    print(f'\nPASSATI: {OK}   FALLITI: {len(KO)}')
    if KO:
        print('Falliti: ' + ', '.join(KO))
    return 1 if KO else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_CARTELLE, ignore_errors=True)
        shutil.rmtree(_ESEMPI, ignore_errors=True)
