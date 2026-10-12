# -*- coding: utf-8 -*-
"""Il server di FerroTrack per la prova notturna, su COPIE, con Lantek finto.

    python server_stress.py --porta 5060 --run <cartella della prova>

- database e impostazioni: le copie nella cartella della prova
  (FERROTRACK_DB / FERROTRACK_CONFIG impostati PRIMA di importare l'app);
- TEMP/TMP dentro la cartella della prova;
- priorita' BELOW_NORMAL;
- GUARDIA SCRITTURE: aprire in scrittura, cancellare, rinominare o creare
  cartelle fuori da ferrotrack-stress/ e dalla cartella della prova solleva
  un errore (e lo annota in guardia_violazioni.log);
- Lantek: XmlImporter e Procesos.exe NON si lanciano mai. Al loro posto
  stub che salvano i file che avrebbero mandato nella cartella della prova e
  li validano contro i formati di backend/lantek.py. Modalita' di guasto
  (normale / lento / errore / irraggiungibile) lette a ogni chiamata da
  <run>/lantek_modo.json, che il banco cambia durante la prova;
- email: mai spedite (stub).
"""
import argparse
import builtins
import io
import json
import os
import re
import shutil
import sys
import threading
import time
from datetime import datetime

QUI = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(os.path.dirname(QUI))
WORKTREE = os.path.dirname(APP)

ap = argparse.ArgumentParser()
ap.add_argument('--porta', type=int, required=True)
ap.add_argument('--run', required=True)
ap.add_argument('--threads', type=int, default=16)
args = ap.parse_args()

if not (5060 <= args.porta <= 5069):
    raise SystemExit('Porta %d non ammessa: solo 5060-5069' % args.porta)
RUN = os.path.abspath(args.run)
DB = os.path.join(RUN, 'dati', 'scheduler.db')
CFG = os.path.join(RUN, 'dati', 'app_config.json')
for p in (DB, CFG):
    if not os.path.isfile(p):
        raise SystemExit('Manca la copia %s: prepara prima la prova' % p)

TMP = os.path.join(RUN, 'tmp')
os.makedirs(TMP, exist_ok=True)
os.environ['FERROTRACK_DB'] = DB
os.environ['FERROTRACK_CONFIG'] = CFG
for k in ('TEMP', 'TMP', 'TMPDIR'):
    os.environ[k] = TMP
for k in [k for k in os.environ if k.startswith('SMTP_')]:
    os.environ.pop(k, None)
import tempfile  # noqa: E402
tempfile.tempdir = TMP

if APP not in sys.path:
    sys.path.insert(0, APP)

# ---------------------------------------------------------------------------
# Priorita' bassa
# ---------------------------------------------------------------------------
try:
    import psutil
    psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == 'nt' else 10)
except Exception:
    pass

# ---------------------------------------------------------------------------
# Guardia sulle scritture
# ---------------------------------------------------------------------------
_CONSENTITE = [os.path.normcase(os.path.abspath(x)) + os.sep for x in (WORKTREE, RUN)]
_LOG_GUARDIA = os.path.join(RUN, 'guardia_violazioni.log')
_orig_open = builtins.open


def _consentito(percorso) -> bool:
    try:
        if isinstance(percorso, int):
            return True
        p = os.path.normcase(os.path.abspath(os.fspath(percorso)))
    except Exception:
        return True
    if p in ('nul', os.devnull.lower()):
        return True
    return any(p.startswith(c) or p + os.sep == c for c in _CONSENTITE)


def _viola(operazione, percorso):
    try:
        with _orig_open(_LOG_GUARDIA, 'a', encoding='utf-8') as f:
            f.write('%s\t%s\t%s\n' % (datetime.now().isoformat(timespec='seconds'), operazione, percorso))
    except Exception:
        pass
    raise PermissionError('PROVA NOTTURNA: scrittura fuori dalla copia bloccata (%s %s)' % (operazione, percorso))


def _open_guardato(file, mode='r', *a, **kw):
    if isinstance(mode, str) and any(c in mode for c in 'wax+') and not _consentito(file):
        _viola('open[%s]' % mode, file)
    return _orig_open(file, mode, *a, **kw)


builtins.open = _open_guardato
io.open = _open_guardato


def _avvolgi(modulo, nome, quanti=1):
    orig = getattr(modulo, nome)

    def f(*a, **kw):
        for x in a[:quanti]:
            if not _consentito(x):
                _viola(nome, x)
        for k in ('dst', 'dest', 'path', 'name'):
            if k in kw and not _consentito(kw[k]):
                _viola(nome, kw[k])
        return orig(*a, **kw)
    f.__wrapped__ = orig
    setattr(modulo, nome, f)


for _n in ('remove', 'unlink', 'rmdir', 'mkdir', 'makedirs', 'removedirs'):
    _avvolgi(os, _n, 1)
for _n in ('rename', 'replace'):
    _avvolgi(os, _n, 2)
_avvolgi(shutil, 'rmtree', 1)
for _n in ('copy', 'copy2', 'copyfile', 'move', 'copytree'):
    # solo la destinazione conta
    _orig = getattr(shutil, _n)

    def _fattore(orig, nome):
        def f(src, dst, *a, **kw):
            if not _consentito(dst):
                _viola(nome, dst)
            return orig(src, dst, *a, **kw)
        return f
    setattr(shutil, _n, _fattore(_orig, _n))

# ---------------------------------------------------------------------------
# L'app (solo ora: i percorsi si leggono all'import)
# ---------------------------------------------------------------------------
from backend.app import app  # noqa: E402
modulo_app = sys.modules["backend.app"]
from backend.models import DATABASE_PATH, e_istanza_di_prova  # noqa: E402
from backend import lantek as lt  # noqa: E402

if os.path.normcase(os.path.abspath(DATABASE_PATH)) != os.path.normcase(os.path.abspath(DB)) or not e_istanza_di_prova():
    raise SystemExit('ATTENZIONE: il database non e\' la copia (%s). Interrompo.' % DATABASE_PATH)
if 'ferrotrack-2.0' in os.path.abspath(modulo_app.UPLOAD_FOLDER):
    raise SystemExit('ATTENZIONE: uploads di produzione. Interrompo.')

# ---------------------------------------------------------------------------
# Lantek finto
# ---------------------------------------------------------------------------
STUB_DIR = os.path.join(RUN, 'lantek_stub')
os.makedirs(STUB_DIR, exist_ok=True)
_REGISTRO_INVII = os.path.join(STUB_DIR, 'invii.jsonl')
_lock_reg = threading.Lock()
_MODO_FILE = os.path.join(RUN, 'lantek_modo.json')
_conn_orig = lt._connessione


def _modo() -> dict:
    try:
        with _orig_open(_MODO_FILE, encoding='utf-8') as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _annota(voce):
    voce['quando'] = datetime.now().isoformat(timespec='milliseconds')
    voce['porta'] = args.porta
    with _lock_reg:
        with _orig_open(_REGISTRO_INVII, 'a', encoding='utf-8') as f:
            f.write(json.dumps(voce, ensure_ascii=False, default=str) + '\n')


def valida_xml(contenuto: bytes) -> list:
    """Errori del file per l'XML Importer rispetto a lantek.xml_ordini_produzione."""
    import xml.etree.ElementTree as ET
    err = []
    try:
        radice = ET.fromstring(contenuto)
    except Exception as e:
        return ['XML non leggibile: %s' % e]
    if not contenuto.startswith(b'<?xml version="1.0" encoding="utf-8"?>'):
        err.append('intestazione XML diversa da quella attesa')
    if radice.tag != 'DATAEX':
        err.append('radice %s invece di DATAEX' % radice.tag)
    comandi = list(radice)
    if not comandi:
        err.append('nessun COMMAND')
    obblig = {'Reference': '20', 'Product': '20', 'WorkCenter': '20', 'Operation': '20', 'Quantity': '100'}
    riferimenti = set()
    for i, c in enumerate(comandi, 1):
        if c.tag != 'COMMAND' or c.get('Name') != 'Import' or c.get('TblRef') != 'MANUFACTURING':
            err.append('comando %d: %s Name=%s TblRef=%s' % (i, c.tag, c.get('Name'), c.get('TblRef')))
        campi = {}
        for f in c:
            if f.tag != 'FIELD':
                err.append('comando %d: elemento %s' % (i, f.tag))
                continue
            campi[f.get('FldRef')] = (f.get('FldValue'), f.get('FldType'))
        for k, t in obblig.items():
            if k not in campi:
                err.append('comando %d: manca %s' % (i, k))
            elif campi[k][1] != t:
                err.append('comando %d: %s di tipo %s (atteso %s)' % (i, k, campi[k][1], t))
        if 'Reference' in campi:
            r = campi['Reference'][0] or ''
            if len(r) > 40:
                err.append('comando %d: Reference piu\' lunga di 40' % i)
            if r in riferimenti:
                err.append('comando %d: Reference ripetuta nello stesso file' % i)
            riferimenti.add(r)
        if 'Quantity' in campi:
            try:
                q = int(campi['Quantity'][0])
                if q <= 0:
                    err.append('comando %d: quantita\' %s' % (i, q))
            except Exception:
                err.append('comando %d: quantita\' non intera %r' % (i, campi['Quantity'][0]))
        if not (campi.get('Product', ('',))[0] or '').strip():
            err.append('comando %d: Product vuoto' % i)
        if 'DeliveryDate' in campi:
            v, t = campi['DeliveryDate']
            if t != '120' or not re.fullmatch(r'\d{8}', v or ''):
                err.append('comando %d: DeliveryDate %r tipo %s' % (i, v, t))
        for k in ('Customer', 'SaleOrder'):
            if k in campi and len(campi[k][0] or '') > 40:
                err.append('comando %d: %s oltre 40 caratteri' % (i, k))
    return err


_RX_RIGA_LST = re.compile(r'^"[^"]+" 0 "" "" "" "[^"]*" "[^"]*" \d+(\.\d+)? 1 "[^"]+"( "[^"]*"){8} $')


def valida_pezzi(pezzi: list) -> tuple:
    """Righe di DxfLista.Lst e processo .Prc rifatti coi formati di lantek.py, e i loro errori."""
    err, righe = [], []
    noti = lt.materiali_lantek_noti()
    codici = set()
    for i, p in enumerate(pezzi, 1):
        cod = str(p.get('codice') or '')
        if not cod.strip():
            err.append('pezzo %d: codice vuoto' % i)
        if cod in codici:
            err.append('pezzo %d: codice %s ripetuto' % (i, cod))
        codici.add(cod)
        dxf = p.get('dxf') or ''
        if not os.path.isfile(dxf):
            err.append('pezzo %s: DXF inesistente' % cod)
        else:
            try:
                with _orig_open(dxf, 'rb') as f:
                    testa = f.read(4096)
                if b'SECTION' not in testa and not testa.startswith(b'AutoCAD Binary DXF'):
                    err.append('pezzo %s: il file non sembra un DXF' % cod)
            except Exception as e:
                err.append('pezzo %s: DXF non leggibile (%s)' % (cod, e))
        mat = str(p.get('materiale') or '')
        if mat.upper() not in noti:
            err.append('pezzo %s: materiale %r non noto a Lantek' % (cod, mat))
        try:
            sp = float(p.get('spessore'))
            if not (0 < sp <= 50):
                err.append('pezzo %s: spessore %s fuori misura' % (cod, sp))
        except Exception:
            err.append('pezzo %s: spessore %r non numerico' % (cod, p.get('spessore')))
            sp = 0
        try:
            riga = lt.riga_lista_dxf(cod, os.path.abspath(dxf), mat, sp)
            if not _RX_RIGA_LST.match(riga):
                err.append('pezzo %s: riga dell\'elenco fuori formato' % cod)
            righe.append(riga)
        except Exception as e:
            err.append('pezzo %s: riga non generabile (%s)' % (cod, e))
    prc = lt.processo_import_dxf('DxfLista.Lst', 'FTIMPSTUB')
    linee = prc.splitlines()
    if linee[0] != '0 FILEPROLT 8.02' or not any(x.startswith('107 1 ') for x in linee):
        err.append('processo .Prc fuori formato')
    return err, righe, prc


def _guasto(tipo):
    m = _modo()
    modo = m.get('modo', 'normale')
    if modo == 'lento':
        time.sleep(float(m.get('secondi', 20)))
    if modo == 'errore':
        return 'Lantek finto: errore simulato durante %s' % tipo
    if modo == 'irraggiungibile':
        return 'Lantek finto: Lantek irraggiungibile (simulato)'
    if modo == 'timeout':
        return 'XML Importer non ha finito in 300 s: controlla in Lantek prima di riprovare'
    return None


def stub_importa_xml(contenuto: bytes, nome: str, cartella: str, attesa_s: int = 300) -> dict:
    if not lt._LOCK_IMPORT.acquire(blocking=False):
        _annota({'tipo': 'xml', 'esito': 'occupato', 'nome': nome})
        return {'eseguito': False, 'errore': 'Un altro invio a Lantek è in corso: riprova tra poco'}
    try:
        base = re.sub(r'[^\w\-]+', '_', nome).strip('_')[:60] or 'ordini'
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        percorso = os.path.join(STUB_DIR, '%s_%s.xml' % (base, stamp))
        with _orig_open(percorso, 'wb') as f:
            f.write(contenuto)
        errori = valida_xml(contenuto)
        import xml.etree.ElementTree as ET
        prodotti = []
        try:
            for c in ET.fromstring(contenuto):
                campi = {f.get('FldRef'): f.get('FldValue') for f in c}
                prodotti.append({'Product': campi.get('Product'), 'SaleOrder': campi.get('SaleOrder'),
                                 'Quantity': campi.get('Quantity'), 'Reference': campi.get('Reference')})
        except Exception:
            pass
        g = _guasto('l\'import degli ordini di produzione')
        _annota({'tipo': 'xml', 'nome': nome, 'file': percorso, 'errori_formato': errori,
                 'prodotti': prodotti, 'esito': 'fallito' if g else 'ok', 'guasto': g,
                 'cartella_app': cartella})
        if g:
            return {'eseguito': False, 'errore': g}
        n = len(prodotti)
        return {'eseguito': True, 'errore': None, 'file_rapporto': percorso,
                'rapporto': {'totale': n, 'ok': n, 'avvisi': 0, 'con_errore': 0, 'errori': []}}
    finally:
        lt._LOCK_IMPORT.release()
        with lt._LOCK:
            for k in [k for k in lt._CACHE if k and k[0] == 'ordini']:
                lt._CACHE.pop(k, None)


def stub_importa_pezzi_dxf(pezzi: list, cartella: str, user_data=None, attesa_s: int = 900) -> dict:
    if not pezzi:
        return {'eseguito': True, 'errore': None, 'cartella': None}
    if not lt._LOCK_IMPORT.acquire(blocking=False):
        _annota({'tipo': 'pezzi', 'esito': 'occupato'})
        return {'eseguito': False, 'errore': 'Un altro invio a Lantek è in corso: riprova tra poco'}
    try:
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        d = os.path.join(STUB_DIR, 'pezzi_' + stamp)
        os.makedirs(d, exist_ok=True)
        errori, righe, prc = valida_pezzi(pezzi)
        with _orig_open(os.path.join(d, 'DxfLista.Lst'), 'w', encoding='cp1252', errors='replace',
                        newline='\r\n') as f:
            f.write('\n'.join(righe) + '\n')
        with _orig_open(os.path.join(d, 'ImportDxf.Prc'), 'w', encoding='cp1252', errors='replace',
                        newline='\r\n') as f:
            f.write(prc)
        g = _guasto('la creazione dei pezzi nuovi')
        _annota({'tipo': 'pezzi', 'cartella': d, 'codici': [p.get('codice') for p in pezzi],
                 'errori_formato': errori, 'user_data': user_data, 'esito': 'fallito' if g else 'ok',
                 'guasto': g})
        if g:
            return {'eseguito': False, 'errore': g}
        return {'eseguito': True, 'errore': None, 'cartella': d}
    finally:
        lt._LOCK_IMPORT.release()
        lt._svuota_cache()


def stub_connessione(cfg):
    if _modo().get('modo') == 'irraggiungibile' or _modo().get('db_irraggiungibile'):
        raise ConnectionError('Lantek finto: database di Lantek irraggiungibile (simulato)')
    return _conn_orig(cfg)


# Lantek finto "coerente": cio' che lo stub ha importato con successo si
# vede poi nelle letture (ordini di produzione aperti, pezzi creati), come
# succederebbe col Lantek vero. Senza, ogni ordine risulterebbe da rimandare.
_STUB_ORDINI = {}   # commessa -> {CODICE: qta}
_STUB_PEZZI = {}    # codice -> {'materiale', 'spessore'}
_ordini_orig = lt._ordini_lantek
_pezzi_orig = lt.pezzi_in_lantek


def _ordini_con_stub(commessa):
    out = _ordini_orig(commessa)
    extra = _STUB_ORDINI.get(str(commessa or '').strip())
    if out is None or not extra:
        return out
    out = {k: dict(v) for k, v in out.items()}
    for cod, q in extra.items():
        v = out.setdefault(cod, {'aperti': 0.0, 'fatti': 0.0, 'ultimo_fatto': None})
        v['aperti'] += q
    return out


def _pezzi_con_stub(codici):
    out = _pezzi_orig(codici)
    if not out.get('disponibile'):
        return out
    pezzi = dict(out.get('pezzi') or {})
    for c in codici:
        c = str(c).strip()
        if c in _STUB_PEZZI and not (pezzi.get(c) or {}).get('esiste'):
            pezzi[c] = dict(pezzi.get(c) or {}, esiste=True, materiale=_STUB_PEZZI[c]['materiale'],
                            spessore=_STUB_PEZZI[c]['spessore'], revisioni=[])
    return dict(out, pezzi=pezzi)


_xml_vero_stub = stub_importa_xml
_pezzi_vero_stub = stub_importa_pezzi_dxf


def _xml_e_ricorda(contenuto, nome, cartella, attesa_s=300):
    esito = _xml_vero_stub(contenuto, nome, cartella, attesa_s)
    if esito.get('eseguito'):
        import xml.etree.ElementTree as ET
        for c in ET.fromstring(contenuto):
            campi = {f.get('FldRef'): f.get('FldValue') for f in c}
            d = _STUB_ORDINI.setdefault(str(campi.get('SaleOrder') or '').strip(), {})
            k = str(campi.get('Product') or '').strip().upper()
            d[k] = d.get(k, 0) + float(campi.get('Quantity') or 0)
    return esito


def _pezzi_e_ricorda(pezzi, cartella, user_data=None, attesa_s=900):
    esito = _pezzi_vero_stub(pezzi, cartella, user_data, attesa_s)
    if esito.get('eseguito'):
        for p in pezzi:
            _STUB_PEZZI[str(p['codice']).strip()] = {'materiale': p.get('materiale'), 'spessore': p.get('spessore')}
    return esito


lt._ordini_lantek = _ordini_con_stub
lt.pezzi_in_lantek = _pezzi_con_stub
stub_importa_xml = _xml_e_ricorda
stub_importa_pezzi_dxf = _pezzi_e_ricorda

lt.importa_xml = stub_importa_xml
lt.importa_pezzi_dxf = stub_importa_pezzi_dxf
lt.xmlimporter_disponibile = lambda: True
lt.procesos_disponibile = lambda: True
lt._gia_aperto = lambda nome_exe: False
lt._connessione = stub_connessione
lt.XMLIMPORTER = os.path.join(RUN, 'NON_ESISTE_XmlImporter.exe')
lt.PROCESOS = os.path.join(RUN, 'NON_ESISTE_Procesos.exe')

# niente email
try:
    from backend import email_sender as _em

    def _no_email(*a, **kw):
        _annota({'tipo': 'email', 'a': str(a[:1])})
        return False, 'PROVA NOTTURNA: email disattivate'
    _em.send_email = _no_email
    _em.is_configured = lambda: False
except Exception:
    pass

# Backup: sulla copia, nella cartella della prova (backup_db ignora FERROTRACK_DB)
try:
    import pathlib
    import backup_db as _bk
    _bk.DB_PATH = pathlib.Path(DB)
    _bk.DEFAULT_BACKUP_DIR = pathlib.Path(RUN) / 'backup_app'
    _bk.CONFIG_PATH = pathlib.Path(RUN) / 'dati' / 'backup_config.json'
    if not _bk.CONFIG_PATH.exists():
        with _orig_open(_bk.CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump({'backup_enabled': True, 'interval_hours': 1, 'max_backups': 5,
                       'backup_path': '', 'remote_path': '', 'giorni_storico': 30}, f)
    if hasattr(_bk, '_avvisa'):
        _bk._avvisa = lambda *a, **kw: None
except Exception as _e:
    print('backup_db non reindirizzato:', _e, flush=True)

# Nessun programma di Lantek, mai: anche se qualcosa sfuggisse agli stub.
import subprocess  # noqa: E402
_run_orig = subprocess.run
_popen_orig = subprocess.Popen


def _vietato(cmd) -> bool:
    s = str(cmd).lower()
    return 'lantek' in s or 'xmlimporter' in s or 'procesos' in s or 'oddwgdxf' in s


class _PopenGuardato(_popen_orig):
    def __init__(self, cmd, *a, **kw):
        if _vietato(cmd):
            _viola('Popen', cmd)
        if os.name == 'nt':
            kw['creationflags'] = kw.get('creationflags', 0) | 0x08000000  # CREATE_NO_WINDOW
        super().__init__(cmd, *a, **kw)


subprocess.Popen = _PopenGuardato

if __name__ == '__main__':
    import logging
    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
                        stream=sys.stdout)
    print('SERVER PROVA NOTTURNA porta %d db %s' % (args.porta, DB), flush=True)
    try:
        rotte = [{'regola': r.rule, 'metodi': sorted(m for m in r.methods if m not in ('HEAD', 'OPTIONS')),
                  'endpoint': r.endpoint, 'argomenti': sorted(r.arguments)} for r in app.url_map.iter_rules()]
        with _orig_open(os.path.join(RUN, 'rotte.json'), 'w', encoding='utf-8') as f:
            json.dump(rotte, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print('rotte non scritte', e, flush=True)
    from waitress import serve
    serve(app, host='127.0.0.1', port=args.porta, threads=args.threads, channel_timeout=300,
          connection_limit=200)
