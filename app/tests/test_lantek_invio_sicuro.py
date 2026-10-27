"""Manda a Lantek: mai due volte gli stessi pezzi, anche se Lantek non si legge.

Prova notturna del 14/10/2026, 01:25: SQL Server di Lantek in aggiornamento
("single user mode", "Communication link failure"). "Manda a Lantek" -> 200,
lo stesso invio ripetuto subito -> 200 con mandati=66: i 66 codici sono
partiti due volte, perche' il controllo dei doppioni leggeva Lantek e senza
Lantek lasciava passare tutto.

Copre:
 1. Lantek leggibile ma in ritardo (non mostra ancora l'ordine appena
    mandato): il secondo invio non manda niente (registro di FerroTrack)
 2. Lantek giu' DOPO il primo invio: il secondo non manda niente, nemmeno
    confermando "manda lo stesso"
 3. Lantek giu' PRIMA di ogni invio: rifiutato con il messaggio, niente
    scritto (ne' registro, ne' correzioni, ne' XML); la lettura riprova una
    volta sugli errori passeggeri
 4. "Manda lo stesso" confermato a Lantek giu': parte, ed e' registrato
 5. due "Manda" premuti insieme: parte uno solo
 6. "Non e' ancora in Lantek": il registro si annulla, si puo' rimandare
 7. la pagina laser e /lantek-quantita non propongono i codici gia' mandati

Gira su DATABASE TEMPORANEO, LANTEK SIMULATO (nessun programma lanciato).
Esecuzione: python app/tests/test_lantek_invio_sicuro.py
"""
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime
import uuid

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
os.environ['FERROTRACK_ESEMPI_DIR'] = tempfile.mkdtemp(prefix='ft_li_esempi_')
_CFG = os.path.join(tempfile.mkdtemp(prefix='ft_li_cfg_'), 'app_config.json')
with open(_CFG, 'w', encoding='utf-8') as f:
    json.dump({'laser_calendario': {'giorni': [1, 2, 3, 4, 5], 'eccezioni': {}}}, f)
os.environ['FERROTRACK_CONFIG'] = _CFG

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, Preventivo, LantekInvio, LantekCorrezione  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_li_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend import lantek as L  # noqa: E402
from backend.database import ConfigManager, PreventivoManager  # noqa: E402
from backend.preventivi import tempi_laser as TL  # noqa: E402

ConfigManager.load_config()
assert os.path.abspath(ConfigManager._CONFIG_PATH) == os.path.abspath(_CFG), 'impostazioni NON temporanee: fermo'
_CARTELLE = tempfile.mkdtemp(prefix='ft_li_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)

# ── Lantek simulato: il database si legge con _connetti (finto) ──────────
STATO = {'giu': False, 'tentativi': 0}
INVII = []          # XML "importati"
PAUSA = {'s': 0}


class _Cur:
    def __init__(self):
        self.ultimo = []

    def execute(self, sql, par=None):
        s = sql.upper()
        if 'FROM PPRR_PPRR_00000100 WHERE PRDREF LIKE' in s:
            self.ultimo = []
        elif 'FROM PPRR_PPRR_00000100' in s:
            # ogni codice chiesto esiste, FERRO 3 mm
            self.ultimo = [(c, 'FERRO', 3.0, 0.02, 0.02, 0.6, 0) for c in (par or [])]
        elif 'FROM MMNN_MMOO_00000100' in s:
            self.ultimo = []        # Lantek "in ritardo": non mostra ancora gli ordini mandati
        else:
            self.ultimo = []

    def fetchall(self):
        return self.ultimo


class _Con:
    def close(self):
        pass


def _connetti_finto(cfg):
    STATO['tentativi'] += 1
    if STATO['giu']:
        raise Exception("('08S01', '[08S01] [Microsoft][ODBC Driver 17 for SQL Server]Communication link failure')")
    c = _Cur()
    return _Con(), c


L._connetti = _connetti_finto
L.PAUSA_RIPROVA_S = 0
L.procesos_disponibile = lambda: False
L.xmlimporter_disponibile = lambda: True
L.importa_pezzi_dxf = lambda *a, **k: (_ for _ in ()).throw(AssertionError('Procesos non si lancia nei test'))


def _importa_finto(contenuto, nome, cartella, attesa_s=300):
    time.sleep(PAUSA['s'])
    INVII.append(contenuto)
    return {'eseguito': True, 'errore': None, 'rapporto': {'totale': 1, 'ok': 1, 'avvisi': 0, 'con_errore': 0, 'errori': []}}


L.importa_xml = _importa_finto
L.expert_aperto = lambda: False   # Lantek Expert chiuso (sul PC vero puo' essere aperto)
TL._leggi_lantek = lambda codici: None

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


def svuota_cache():
    with L._LOCK:
        L._CACHE.clear()


from tests.accesso_aiuto import postazioni, entra, modalita  # noqa: E402
modalita('protetto')
postazioni()
LAS = entra(A.app, 'laser')


def nuovo_ordine(rif):
    base = {'quantita': 4, 'materiale': 'S235', 'spessore_mm': 3, 'costo_base_override': 5,
            'perimetro_taglio_m': 0.8, 'area_dm2': 2.9, 'bbox_w_mm': 200, 'bbox_h_mm': 150}
    pr = PreventivoManager.create('DECA S.r.l.', 'paolo', quantita=1, numero_ordine_cliente=rif)
    pid = pr['id'] if isinstance(pr, dict) else pr
    PreventivoManager.replace_articoli(pid, [dict(base, codice=f'{rif}-A-00'), dict(base, codice=f'{rif}-B-00', quantita=2)])
    s = models.SessionLocal()
    s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'INVIATO'
    s.commit(); s.close()
    oid = PreventivoManager.accetta_e_crea_ordine(pid, 'paolo').get('order_id')
    A.OrderManager.smista(oid, True, user_id='laser')
    return oid


def da_mandare(oid):
    svuota_cache()
    d = LAS.get(f'/api/orders/{oid}/lantek-quantita').get_json() or {}
    return [x['codice'] for x in d.get('da_mandare') or []], d


def manda(oid, codici, **extra):
    svuota_cache()
    r = LAS.post(f'/api/orders/{oid}/lantek-invia', json={'codici': codici, 'nuovi': [], **extra})
    return r.status_code, (r.get_json() or {})


def registro(oid):
    s = models.SessionLocal()
    try:
        return [(r.codice, r.quantita) for r in s.query(LantekInvio).filter(LantekInvio.order_id == oid,
                                                                            LantekInvio.annullato_il.is_(None)).all()]
    finally:
        s.close()


def _xml(righe):
    cmd = ''.join(f'<COMMAND Name="Import" TblRef="MANUFACTURING"><FIELD FldRef="Reference" FldValue="{rif}" FldType="20" />'
                  f'<FIELD FldRef="Product" FldValue="{cod}" FldType="20" /><FIELD FldRef="SaleOrder" FldValue="{so}" FldType="20" />'
                  f'<FIELD FldRef="Quantity" FldValue="{q}" FldType="100" /></COMMAND>' for rif, cod, so, q in righe)
    return f'<?xml version="1.0" encoding="utf-8"?><DATAEX>{cmd}</DATAEX>'


def _log(righe, errati=()):
    blocchi = ''.join(f'<div>{i}. IMPORT FOR TABLE MANUFACTURING <div>Production Order: {rif} Destination Product: {cod}</div></div>'
                      f'<ul><li><div>{"Product not found" if cod in errati else "Insert into table MANUFACTURING was successful."}</div></li></ul>'
                      for i, (rif, cod, so, q) in enumerate(righe, 1))
    n = len(righe)
    return (f'<html><body><table><tr><td>Total commands:</td><td>{n}</td></tr><tr><td>Commands Ok:</td><td>{n - len(errati)}</td></tr>'
            f'<tr><td>Commands with warning:</td><td>0</td></tr><tr><td>Commands with error:</td><td>{len(errati)}</td></tr></table>'
            f'{blocchi}</body></html>')


def storico():
    print('\n8) Gli invii di PRIMA del registro (file dell\'XML Importer)')
    from backend import lantek_invii as LI
    from backend.models import Order
    import shutil
    cart = tempfile.mkdtemp(prefix='ft_li_storico_')
    log = os.path.join(cart, 'Importer log')
    os.makedirs(log)
    s = models.SessionLocal()
    ids = {}
    for num in ('A 009500', 'A 009501', 'A 009502'):
        ids[num] = str(uuid.uuid4())
        s.add(Order(id=ids[num], cliente='DECA S.r.l.', numero_ordine=num, status='RICEVUTO', data_consegna=datetime(2026, 11, 2)))
    s.commit(); s.close()
    ok = [('FT9500-X1-00-2610121424', 'X1-00', '9500', '3'), ('FT9500-X2-00-2610121424', 'X2-00', '9500', '6')]
    errato = [('FT9501-Y1-00-2610121500', 'Y1-00', '9501', '2'), ('FT9501-Y2-00-2610121500', 'Y2-00', '9501', '1')]
    senza_log = [('FT9502-Z1-00-2610121600', 'Z1-00', '9502', '4')]
    for nome, righe, errati, con_log in (('ORDINI_LANTEK_-_9500_20261012_142444_2026-10-12 142446', ok, (), True),
                                         ('ORDINI_LANTEK_-_9501_20261012_150000_2026-10-12 150002', errato, ('Y2-00',), True),
                                         ('ORDINI_LANTEK_-_9502_20261012_160000_2026-10-12 160002', senza_log, (), False)):
        open(os.path.join(log, nome + '.xml'), 'w', encoding='utf-8').write(_xml(righe))
        if con_log:
            open(os.path.join(log, nome + '_log.html'), 'w', encoding='utf-8').write(_log(righe, errati))
    n = LI.importa_storico(cart)
    check('importati i codici riusciti (2 + 1)', n == 3, n)
    g = LI.mandati(ids['A 009500'])
    check('A 009500: X1-00 x3 e X2-00 x6 (SaleOrder 9500 = ordine A 009500)',
          {k: v['quantita'] for k, v in g.items()} == {'X1-00': 3.0, 'X2-00': 6.0}, g)
    check('il codice con l\'errore nel rapporto resta fuori', set(LI.mandati(ids['A 009501'])) == {'Y1-00'})
    check('senza il rapporto: niente', LI.mandati(ids['A 009502']) == {})
    s = models.SessionLocal()
    r = s.query(LantekInvio).filter(LantekInvio.order_id == ids['A 009500']).first()
    check('origine "storico", riferimento e ora dal file', r.origine == 'storico' and r.riferimento.startswith('FT9500-')
          and r.invio == '2610121424' and r.creato_il is not None)
    s.close()
    check('rifatto: nessuna riga in piu\'', LI.importa_storico(cart) == 0)

    # i file veri di produzione (copie, sola lettura), se ci sono su questo PC
    vera = os.path.join(_APP, '..', '..', 'ferrotrack-2.0', 'app', 'uploads', 'lantek_import', 'Importer log')
    if not os.path.isdir(vera):
        print('  (file veri di produzione non presenti: parte saltata)')
        return
    copia = tempfile.mkdtemp(prefix='ft_li_veri_')
    os.makedirs(os.path.join(copia, 'Importer log'))
    for f in os.listdir(vera):
        if f.startswith('ORDINI_LANTEK_') and any(k in f for k in ('A_001221_BIS', '1302', '1030')):
            shutil.copy2(os.path.join(vera, f), os.path.join(copia, 'Importer log', f))
    s = models.SessionLocal()
    veri = {}
    for num in ('A 001221 BIS', 'A 001302', 'A 001030'):
        veri[num] = str(uuid.uuid4())
        s.add(Order(id=veri[num], cliente='DECA S.r.l.', numero_ordine=num, status='RICEVUTO', data_consegna=datetime(2026, 11, 2)))
    s.commit(); s.close()
    n = LI.importa_storico(copia)
    bis, o1302, o1030 = (LI.mandati(veri[k]) for k in ('A 001221 BIS', 'A 001302', 'A 001030'))
    check('file veri: A 001221 BIS 42 codici, 191700045-00 x1 come mandato allora', len(bis) == 42
          and bis.get('191700045-00', {}).get('quantita') == 1.0, (len(bis), bis.get('191700045-00')))
    check('file veri: 1302 due codici', set(o1302) == {'050201310-00', '050201313-00'}, list(o1302))
    check('file veri: 1030 dai due invii', len(o1030) >= 29, len(o1030))
    check('file veri rifatti: nessuna riga in piu\'', LI.importa_storico(copia) == 0)
    # e ora "Manda a Lantek" non li rimanda nemmeno con Lantek giu'
    check('registrati: il secondo invio li salta', not LI.togli_mandati([{'codice': '191700045-00'}], bis))


def main():
    print('1) Lantek leggibile ma "in ritardo": il secondo invio non manda niente')
    o1 = nuovo_ordine('LI1')
    cod, _ = da_mandare(o1)
    check('due codici da mandare', sorted(cod) == ['LI1-A-00', 'LI1-B-00'], cod)
    c, d = manda(o1, cod)
    check('primo invio: 200, 2 mandati', c == 200 and d.get('mandati') == 2, (c, d))
    check('registrati da FerroTrack con quantita\'', sorted(registro(o1)) == [('LI1-A-00', 4.0), ('LI1-B-00', 2.0)], registro(o1))
    n = len(INVII)
    c, d = manda(o1, cod)
    check('stesso invio subito dopo: niente mandato (409)', c == 409 and d.get('codice') == 'niente_da_mandare'
          and not d.get('mandati') and len(INVII) == n, (c, d))

    print('\n2) Lantek giu\' DOPO il primo invio')
    STATO['giu'] = True
    c, d = manda(o1, cod)
    check('il secondo non manda niente', c == 409 and len(INVII) == n, (c, d))
    c, d = manda(o1, cod, senza_controllo_lantek=True)
    check('nemmeno con "manda lo stesso"', c == 409 and len(INVII) == n, (c, d))

    print('\n3) Lantek giu\' PRIMA di ogni invio')
    STATO['giu'] = False
    o2 = nuovo_ordine('LI2')
    cod2, _ = da_mandare(o2)
    STATO['giu'] = True
    svuota_cache()
    STATO['tentativi'] = 0
    c, d = manda(o2, cod2)
    check('rifiutato col messaggio chiaro', c == 503 and d.get('codice') == 'lantek_non_risponde'
          and d.get('error') == "Lantek non risponde in questo momento: riprova tra qualche minuto. Niente è stato mandato.", (c, d))
    check('niente XML, niente registro', len(INVII) == n and registro(o2) == [])
    s = models.SessionLocal()
    check('niente correzioni scritte', s.query(LantekCorrezione).filter(LantekCorrezione.order_id == o2).count() == 0)
    s.close()
    check('la lettura di Lantek ha riprovato (errore passeggero)', STATO['tentativi'] >= 2 and STATO['tentativi'] % 2 == 0,
          STATO['tentativi'])
    check('08S01 / single user mode sono passeggeri, un errore qualsiasi no',
          L.passeggero(Exception('[08S01] Communication link failure')) and L.passeggero(Exception('Server is in single user mode'))
          and not L.passeggero(Exception('Invalid object name PPRR')))

    print('\n4) "Manda lo stesso" confermato da chi manda')
    c, d = manda(o2, cod2, senza_controllo_lantek=True)
    check('parte, ed e\' registrato', c == 200 and d.get('mandati') == 2 and len(registro(o2)) == 2, (c, d))
    n = len(INVII)
    c, d = manda(o2, cod2, senza_controllo_lantek=True)
    check('ripremuto: non riparte', c == 409 and len(INVII) == n, (c, d))
    from backend.models import AuditLog
    s = models.SessionLocal()
    a = s.query(AuditLog).filter(AuditLog.action == 'LANTEK_INVIA', AuditLog.entity_id == o2).first()
    check('nello storico: mandato senza il controllo, confermato', a is not None and 'LANTEK NON LEGGIBILE' in (a.detail or ''))
    s.close()

    print('\n5) Due "Manda" premuti insieme')
    STATO['giu'] = False
    o3 = nuovo_ordine('LI3')
    cod3, _ = da_mandare(o3)
    PAUSA['s'] = 1.0
    n = len(INVII)
    esiti = []
    seconda = entra(A.app, 'laser')

    def premi(cl):
        r = cl.post(f'/api/orders/{o3}/lantek-invia', json={'codici': cod3, 'nuovi': []})
        esiti.append((r.status_code, (r.get_json() or {}).get('codice')))
    t1 = threading.Thread(target=premi, args=(LAS,))
    t2 = threading.Thread(target=premi, args=(seconda,))
    t1.start(); time.sleep(0.15); t2.start(); t1.join(); t2.join()
    PAUSA['s'] = 0
    check('ne parte uno solo', len(INVII) == n + 1 and sorted(c for c, _ in esiti) == [200, 409], esiti)
    check("l'altro dice che e' in corso (o gia' mandato)", any(k in ('invio_in_corso', 'niente_da_mandare') for _, k in esiti), esiti)

    print('\n6) "Non e\' ancora in Lantek": si puo\' rimandare apposta')
    r = LAS.post(f'/api/orders/{o1}/importato', json={'annulla': True})
    check('annullato', r.status_code == 200 and registro(o1) == [], (r.status_code, registro(o1)))
    cod, _ = da_mandare(o1)
    c, d = manda(o1, cod)
    check('ora riparte', c == 200 and d.get('mandati') == 2, (c, d))

    print('\n7) Le pagine non propongono i codici gia\' mandati')
    cod, d = da_mandare(o1)
    check('/lantek-quantita: niente da mandare, e dice quali ha gia\' mandato FerroTrack',
          cod == [] and d.get('n_xml') == 0 and sorted(d.get('gia_mandati_ft') or []) == ['LI1-A-00', 'LI1-B-00'], d.get('gia_mandati_ft'))
    svuota_cache()
    lz = LAS.get(f'/api/orders/{o1}/laser?fresco=1').get_json() or {}
    check('/laser: invio vuoto', not ((lz.get('invio') or {}).get('codici')), lz.get('invio'))

    storico()
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
