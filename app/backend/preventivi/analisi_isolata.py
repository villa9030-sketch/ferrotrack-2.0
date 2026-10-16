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


def converti_dwg(coppie, tempo_max: float | None = None) -> dict:
    """Converte i DWG in DXF [(dwg_path, dxf_path), ...] nel lavoratore
    separato, tutti in una chiamata a ODA (dwg_conversione.converti_percorsi).
    Ritorna {dwg_path: {'ok', 'errore', 'da_cache'}}.

    ODA e' gia' un processo a parte con un tempo massimo; passare dal
    lavoratore aggiunge un secondo limite: se la conversione si blocca in
    modo imprevisto, si ferma il lavoratore con ODA e il server resta su.
    Se il lavoratore non parte si converte dal server (stessa funzione)."""
    from . import dwg_conversione as dc
    coppie = [(os.path.abspath(a), os.path.abspath(b)) for a, b in coppie]
    if not coppie:
        return {}
    tmax = dc.TEMPO_MAX_S if tempo_max is None else float(tempo_max)
    # il tempo del lotto, piu' i ritentativi uno per uno nel caso peggiore
    limite = (min(dc._TEMPO_LOTTO_MAX_S, max(tmax, dc._TEMPO_BASE_S + dc._TEMPO_PER_DISEGNO_S * len(coppie)))
              + (tmax * len(coppie) if len(coppie) > 1 else 0) + 30)
    compito = {'tipo': 'dwg', 'coppie': coppie, 'tempo_max': tmax, 'cache_dwg': dc.CACHE_DIR}
    with _posti:
        w = _prendi()
        if w is None:
            return dc.converti_percorsi(coppie, tmax)
        stato, dato = w.esegui(compito, limite)
        if stato == 'ok' and isinstance(dato, dict):
            _rendi(w)
            # le chiavi tornano dal JSON: stesse stringhe dei percorsi passati
            return dato
        if stato == 'errore':
            _rendi(w)
            msg = dc.MSG_ROTTO.format(motivo=str(dato)[:160])
        else:
            w.chiudi()
            msg = (dc.MSG_LENTO.format(s=tmax) if stato == 'lento'
                   else dc.MSG_ROTTO.format(motivo='la conversione si e\' interrotta'))
        logger.warning('conversione DWG nel lavoratore: %s (%s)', stato, msg)
        return {a: {'ok': os.path.isfile(b) and dc._e_dxf(b), 'errore': None if (os.path.isfile(b) and dc._e_dxf(b)) else msg,
                    'da_cache': False} for a, b in coppie}


def _verifica_isolata(cartella, item, dxf_cfg, materiali, lantek, correggi, tempo_max_s) -> dict:
    """verifica_ordine.verifica_automatica di UN pezzo nel lavoratore.

    Il tempo massimo per pezzo resta quello della verifica (lo misura lei, nel
    lavoratore); qui un secondo limite largo, se il lavoratore si blocca. Il
    lavoratore che ha lasciato a meta' un controllo troppo lungo si butta (il
    thread abbandonato continuerebbe a consumare)."""
    from . import verifica_ordine as vo
    tmax = float(tempo_max_s or 0)
    compito = {'tipo': 'verifica', 'cartella': os.path.abspath(cartella), 'item': item,
               'dxf_cfg': dxf_cfg or {}, 'materiali': materiali, 'lantek': lantek,
               'correggi': correggi, 'tempo_max_s': tmax}
    try:
        json.dumps(compito)
        trasportabile = True
    except (TypeError, ValueError):
        trasportabile = False          # dati non trasportabili: nel server, come prima
    with _posti:
        w = _prendi() if trasportabile else None
        if w is None:
            return vo.verifica_automatica(cartella, item, dxf_cfg=dxf_cfg, materiali=materiali, lantek=lantek,
                                          correggi=correggi, tempo_max_s=tmax)
        stato, dato = w.esegui(compito, tmax * 2 + 60)
        if stato == 'ok' and isinstance(dato, dict):
            motivi = (dato.get('esito') or {}).get('motivi') or []
            if any('controllo troppo lungo' in str(m) for m in motivi):
                w.chiudi()
            else:
                _rendi(w)
            return dato
        if stato == 'errore':
            _rendi(w)
            return vo._risultato('da_guardare', [f'controllo non riuscito ({str(dato)[:80]})'], [])
        w.chiudi()
        logger.warning('[%s] verifica nel lavoratore: %s', (item or {}).get('codice'), stato)
        return vo._risultato('da_guardare', ['controllo troppo lungo: guardalo a mano'], [])


def verifica_pezzi(cartella, items: list, *, dxf_cfg=None, materiali=None,
                   lantek_per_codice: dict | None = None, correggi=True, tempo_max_s=15.0) -> list:
    """Come verifica_ordine.verifica_ordine (un risultato per pezzo, nello
    stesso ordine), ma ogni pezzo nei lavoratori fuori dal server.

    Prima la verifica girava in 4 thread DENTRO il server: e' geometria
    (shapely, pick_part) col GIL di Python preso, e con un pacchetto grande
    (C23-226, 15 pezzi: 120 letture di contorno, ~8 s) rallentava tutte le
    altre richieste, e sotto carico i pezzi finivano "controllo troppo lungo".
    Stessa funzione (verifica_automatica), stessi dati: stesso risultato."""
    items = list(items or [])
    if not items:
        return []
    lpc = lantek_per_codice or {}

    def uno(it):
        it = it or {}
        lk = lpc.get(it.get('codice')) if it.get('codice') else None
        return _verifica_isolata(cartella, it, dxf_cfg, materiali, lk, correggi, tempo_max_s)
    out = [None] * len(items)
    with ThreadPoolExecutor(max_workers=max(1, min(PROCESSI_MAX, len(items)))) as pool:
        fut = {pool.submit(uno, it): i for i, it in enumerate(items)}
        for f, i in fut.items():
            try:
                out[i] = f.result()
            except Exception as e:  # noqa: BLE001
                from . import verifica_ordine as vo
                out[i] = vo._risultato('da_guardare', [f'controllo non riuscito ({str(e)[:80]})'], [])
    return out


def contorno_preciso(percorso: str, punti, area: float, k: float, cfg: dict):
    """Il contorno del motore coi fori, preciso: si "clicca" sul bordo del
    contorno scelto (punti, in unita' del disegno; k = mm per unita') con lo
    strumento del CAD e si prende il contorno con la stessa area (2%).
    (outer_xy, holes_xy) oppure None. Lo usa la pagina laser ("giusto")."""
    from .pick_part import pick_candidates
    punti = list(punti or [])
    if not punti:
        return None
    for i in (0, len(punti) // 3, (2 * len(punti)) // 3):
        x, y = float(punti[i][0]) * k, float(punti[i][1]) * k
        r = pick_candidates(percorso, x, y, cfg)
        for c in (r or {}).get('candidates') or []:
            lorda = float(c.get('area_lorda_dm2') or c.get('area_dm2') or 0)
            if area > 0 and abs(lorda - area) / area < 0.02:
                return c.get('outer_xy'), c.get('holes_xy') or []
    return None


def contorno_preciso_isolato(percorso: str, punti, area: float, k: float, cfg: dict):
    """contorno_preciso nel lavoratore (geometria fuori dal server, senza il
    GIL); se il lavoratore non c'e' o non risponde, nel server come prima."""
    compito = {'tipo': 'contorno_preciso', 'path': os.path.abspath(percorso), 'punti': punti,
               'area': area, 'k': k, 'cfg': cfg or {}}
    try:
        json.dumps(compito)
    except (TypeError, ValueError):
        return contorno_preciso(percorso, punti, area, k, cfg)
    with _posti:
        w = _prendi()
        if w is not None:
            stato, dato = w.esegui(compito, TEMPO_MAX_S)
            if stato == 'ok':
                _rendi(w)
                return (dato[0], dato[1]) if dato else None
            if stato == 'errore':
                _rendi(w)
            else:
                w.chiudi()
            logger.warning('contorno preciso nel lavoratore: %s, si rifa\' nel server', stato)
    return contorno_preciso(percorso, punti, area, k, cfg)


def chiudi_tutti():
    """Per i test e l'arresto: chiude i lavoratori fermi."""
    with _lock:
        ws = list(_liberi)
        _liberi.clear()
    for w in ws:
        w.chiudi()
