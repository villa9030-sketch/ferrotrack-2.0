"""Lettura dei disegni DXF con un tempo massimo, fuori dal server.

Dalla prova notturna (12/10): un DXF con 20.000 fori ha tenuto occupata
l'analisi del pacchetto per oltre 5 minuti, e intanto tutto il server
rallentava (il motore lavorava DENTRO il server, nei suoi thread, col GIL
di Python preso; un thread non si puo' fermare).

Ora ogni disegno si legge in un processo lavoratore (lavoratore_dxf.py):
  - TEMPO MASSIMO per disegno (90 s; il piu' lento dell'archivio vero ne
    chiede ~25): oltre, il lavoratore si ferma e il disegno torna
    "da guardare: troppo complesso o lento da leggere" invece di bloccare;
  - prima ancora, un controllo da pochi millisecondi su dimensione del file
    e numero di elementi: un file mostruoso non si prova nemmeno;
  - al massimo PROCESSI_MAX letture insieme per tutto il server, a priorita'
    bassa: il PC e' anche il server di produzione;
  - se un lavoratore cade (crash di una libreria), risponde l'errore di quel
    disegno e il server resta su.
Il risultato e' quello di sempre: stessa funzione (process_single_dxf),
stessa configurazione, stessa cache. Se il lavoratore non si puo' avviare
si legge come prima, nel server (meglio lento che niente).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lavoratore_dxf.py')


def _num_env(nome, predefinito, tipo=float):
    try:
        return tipo(os.environ.get(nome, '') or predefinito)
    except ValueError:
        return predefinito


# Secondi massimi per leggere UN disegno (il piu' lento dell'archivio: ~25 s).
TEMPO_MAX_S = _num_env('FERROTRACK_DXF_TEMPO_MAX', 90.0)
# Letture contemporanee in tutto il server (12 core: 3, a priorita' bassa).
PROCESSI_MAX = max(1, _num_env('FERROTRACK_DXF_PROCESSI', min(3, max(1, (os.cpu_count() or 4) // 4)), int))
# Oltre questi limiti il disegno non si prova nemmeno. Archivio vero (10.556
# DXF): file al massimo 6 MB, al massimo ~20.000 elementi.
MB_MAX = _num_env('FERROTRACK_DXF_MB_MAX', 60.0)
ELEMENTI_MAX = _num_env('FERROTRACK_DXF_ELEMENTI_MAX', 250000, int)
# Un lavoratore si rinnova dopo tanti disegni (memoria) e si chiude se resta
# fermo a lungo (non tiene memoria occupata quando nessuno carica pacchetti).
_COMPITI_PER_LAVORATORE = 150
_RIPOSO_MAX_S = 600

MSG_LENTO = ('Disegno troppo complesso o lento da leggere (oltre {s:.0f} s): '
             'da guardare a mano')
MSG_GRANDE = 'Disegno troppo grande da leggere ({motivo}): da guardare a mano'

_BELOW_NORMAL = 0x00004000
_NO_WINDOW = 0x08000000


def controllo_rapido(path: str) -> str | None:
    """Motivo per NON leggere il disegno (troppo grande), oppure None.
    Pochi millisecondi: dimensione del file e conteggio dei codici 0 del DXF."""
    try:
        mb = os.path.getsize(path) / (1024 * 1024)
    except OSError:
        return None
    if mb > MB_MAX:
        return f'{mb:.0f} MB'
    try:
        with open(path, 'rb') as fh:
            dati = fh.read()
    except OSError:
        return None
    n = dati.count(b'\n  0\n') + dati.count(b'\n  0\r\n')
    if n > ELEMENTI_MAX:
        return f'{n} elementi'
    return None


def _risultato_errore(filename, messaggio, codice):
    return {'success': False, 'filename': filename, 'error': messaggio,
            'codice': codice, 'da_guardare': True}


def _eseguibile():
    """python.exe accanto a pythonw.exe: con le pipe e' piu' affidabile, e
    CREATE_NO_WINDOW evita comunque qualsiasi finestra."""
    exe = sys.executable or 'python'
    base = os.path.basename(exe).lower()
    if base == 'pythonw.exe':
        alt = os.path.join(os.path.dirname(exe), 'python.exe')
        if os.path.isfile(alt):
            return alt
    return exe


class _Lavoratore:
    def __init__(self):
        env = dict(os.environ, PYTHONIOENCODING='utf-8', FERROTRACK_SKIP_DB_INIT='1')
        flags = (_BELOW_NORMAL | _NO_WINDOW) if os.name == 'nt' else 0
        self.proc = subprocess.Popen(
            [_eseguibile(), _SCRIPT, _APP_DIR], cwd=_APP_DIR, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=flags, text=True, encoding='utf-8', bufsize=1)
        self.righe = queue.Queue()
        self.compiti = 0
        self.libero_dal = time.time()
        self._seq = 0
        threading.Thread(target=self._leggi, daemon=True, name='lettore-dxf').start()

    def _leggi(self):
        try:
            for riga in self.proc.stdout:
                self.righe.put(riga)
        except Exception:
            pass
        self.righe.put(None)          # fine: il processo e' uscito

    def vivo(self):
        return self.proc.poll() is None

    def esegui(self, compito: dict, tempo_max: float):
        """('ok', payload) | ('errore', msg) | ('lento', None) | ('morto', None)"""
        self._seq += 1
        compito = dict(compito, id=self._seq)
        self.compiti += 1
        try:
            self.proc.stdin.write(json.dumps(compito) + '\n')
            self.proc.stdin.flush()
        except (OSError, ValueError):
            return 'morto', None
        scadenza = time.time() + tempo_max
        while True:
            resto = scadenza - time.time()
            if resto <= 0:
                return 'lento', None
            try:
                riga = self.righe.get(timeout=resto)
            except queue.Empty:
                return 'lento', None
            if riga is None:
                return 'morto', None
            try:
                r = json.loads(riga)
            except ValueError:
                continue
            if r.get('id') != compito['id']:
                continue                  # risposta vecchia: si ignora
            if r.get('ok'):
                return 'ok', r.get('payload')
            return 'errore', r.get('error') or 'errore nella lettura del disegno'

    def chiudi(self):
        # Il python.exe del .venv e' un lanciatore: il lavoro lo fa un suo
        # processo figlio. Si ferma anche quello (di solito lo fa gia' Windows).
        try:
            import psutil
            for figlio in psutil.Process(self.proc.pid).children(recursive=True):
                try:
                    figlio.kill()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self.proc.kill()
        except Exception:
            pass
        try:
            self.proc.wait(5)
        except Exception:
            pass
        for f in (self.proc.stdin, self.proc.stdout):
            try:
                f.close()
            except Exception:
                pass


_lock = threading.Lock()
_posti = threading.BoundedSemaphore(PROCESSI_MAX)
_liberi: list[_Lavoratore] = []
_custode = None
_avvio_fallito_il = 0.0


def _custode_riposo():
    """Chiude i lavoratori fermi da troppo: non tengono memoria per niente."""
    while True:
        time.sleep(60)
        with _lock:
            vecchi = [w for w in _liberi if time.time() - w.libero_dal > _RIPOSO_MAX_S]
            for w in vecchi:
                _liberi.remove(w)
        for w in vecchi:
            w.chiudi()


def _prendi() -> _Lavoratore | None:
    global _custode, _avvio_fallito_il
    with _lock:
        while _liberi:
            w = _liberi.pop()
            if w.vivo():
                return w
            w.chiudi()
        if _custode is None:
            _custode = threading.Thread(target=_custode_riposo, daemon=True, name='custode-dxf')
            _custode.start()
        if time.time() - _avvio_fallito_il < 60:
            return None
    try:
        return _Lavoratore()
    except Exception as e:  # noqa: BLE001
        logger.warning('lavoratore DXF non avviato (si legge nel server): %s', e)
        _avvio_fallito_il = time.time()
        return None


def _rendi(w: _Lavoratore):
    if not w.vivo() or w.compiti >= _COMPITI_PER_LAVORATORE:
        w.chiudi()
        return
    w.libero_dal = time.time()
    with _lock:
        _liberi.append(w)


def analizza(path: str, filename: str, dxf_cfg: dict, tempo_max: float | None = None) -> dict:
    """Come process_single_dxf(path, filename, dxf_cfg), ma con tempo massimo
    e fuori dal server. Ritorna sempre un dict (success False se non letto)."""
    motivo = controllo_rapido(path)
    if motivo:
        logger.warning('[%s] disegno non letto: troppo grande (%s)', filename, motivo)
        return _risultato_errore(filename, MSG_GRANDE.format(motivo=motivo), 'troppo_grande')
    tempo_max = TEMPO_MAX_S if tempo_max is None else tempo_max
    from . import dxf_cache
    compito = {'path': os.path.abspath(path), 'filename': filename, 'cfg': dxf_cfg or {},
               'cache': dxf_cache._CACHE_DB_PATH}
    with _posti:
        w = _prendi()
        if w is None:
            from .dxf_batch_worker import process_single_dxf
            return process_single_dxf(path, filename, dxf_cfg)
        t0 = time.time()
        stato, dato = w.esegui(compito, tempo_max)
        if stato == 'ok':
            _rendi(w)
            return dato if isinstance(dato, dict) else _risultato_errore(
                filename, 'Risposta non valida dalla lettura del disegno', 'errore_lettura')
        if stato == 'errore':
            _rendi(w)
            return {'success': False, 'filename': filename, 'error': dato}
        w.chiudi()                         # lento o caduto: si butta
        if stato == 'lento':
            logger.warning('[%s] lettura interrotta dopo %.0f s: troppo lenta', filename, time.time() - t0)
            return _risultato_errore(filename, MSG_LENTO.format(s=tempo_max), 'troppo_lento')
        logger.warning('[%s] il lavoratore DXF si e\' fermato durante la lettura', filename)
        return _risultato_errore(filename, 'La lettura del disegno si e\' interrotta (file anomalo): '
                                           'da guardare a mano', 'lettura_interrotta')


def analizza_tutti(compiti, dxf_cfg: dict, tempo_max: float | None = None) -> dict:
    """{filename: payload} per [(path, filename), ...]: in parallelo, ognuno
    col suo tempo massimo. I thread qui aspettano soltanto i lavoratori."""
    compiti = list(compiti)
    if not compiti:
        return {}
    out = {}
    with ThreadPoolExecutor(max_workers=min(PROCESSI_MAX, len(compiti))) as pool:
        fut = {pool.submit(analizza, p, f, dxf_cfg, tempo_max): f for p, f in compiti}
        for x, f in fut.items():
            try:
                out[f] = x.result()
            except Exception as e:  # noqa: BLE001
                logger.exception('analisi DXF fallita per %s', f)
                out[f] = {'success': False, 'filename': f, 'error': str(e)}
    return out


def chiudi_tutti():
    """Per i test e l'arresto: chiude i lavoratori fermi."""
    with _lock:
        ws = list(_liberi)
        _liberi.clear()
    for w in ws:
        w.chiudi()
