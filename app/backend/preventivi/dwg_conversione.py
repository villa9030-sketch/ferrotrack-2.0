"""Disegni DWG -> DXF, per tutte le strade da cui entra un disegno.

Il motore di riconoscimento legge solo DXF. Prima il DWG si convertiva solo
nell'import di UN file; il caricamento di piu' file, la cartella e il
pacchetto ZIP (preventivatore e ordine dell'ufficio) lo scartavano o lo
passavano al motore cosi' com'era ("Batch supporta solo .dxf"). Ora c'e' una
sola conversione, usata da tutte.

Come si converte:
  - con ODA File Converter (gratuito, installato in C:\\Program Files\\ODA);
    MAI con il convertitore di Lantek (OdDwgDxf.exe): apre finestre di
    errore sul desktop;
  - senza finestre: CREATE_NO_WINDOW + finestra nascosta (SW_HIDE), a
    priorita' bassa (il PC e' anche il server di produzione);
  - TUTTI i DWG di un caricamento in UNA chiamata (ODA converte una cartella
    intera: ~0,1 s a disegno, contro ~0,4 s di avvio per ogni chiamata);
  - con un TEMPO MASSIMO: oltre, ODA si ferma (con i suoi figli) e i disegni
    che non ha ancora convertito si riprovano uno per uno, ognuno col suo
    tempo massimo, cosi' un DWG che blocca ODA non trascina gli altri;
  - con una CACHE per impronta del file (SHA-256): lo stesso DWG caricato di
    nuovo non si riconverte;
  - un DWG danneggiato non blocca niente: torna "DWG non leggibile: ..." col
    motivo, e il resto del caricamento prosegue.

La chiamata dal server passa da analisi_isolata.converti_dwg, che la fa
eseguire al lavoratore separato (lavoratore_dxf.py): anche un blocco
imprevisto resta fuori dal server.
"""
from __future__ import annotations

import glob
import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import time

logger = logging.getLogger(__name__)

_APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _num_env(nome, predefinito, tipo=float):
    try:
        return tipo(os.environ.get(nome, '') or predefinito)
    except ValueError:
        return predefinito


# Secondi massimi per convertire UN disegno (di solito meno di mezzo secondo).
TEMPO_MAX_S = _num_env('FERROTRACK_DWG_TEMPO_MAX', 60.0)
# Tempo per una conversione di N disegni insieme: fisso + per disegno.
_TEMPO_BASE_S = 20.0
_TEMPO_PER_DISEGNO_S = 3.0
_TEMPO_LOTTO_MAX_S = 600.0
# Cache dei DXF convertiti (accanto alla cache del motore, database/).
CACHE_DIR = os.environ.get('FERROTRACK_DWG_CACHE') or os.path.join(_APP_DIR, 'database', 'cache_dwg')
_CACHE_MAX_FILE = 3000

_NO_WINDOW = 0x08000000
_BELOW_NORMAL = 0x00004000

MSG_NON_INSTALLATO = ('DWG non leggibile: manca il convertitore ODA File Converter su questo PC '
                      '(gratuito: opendesign.com). In alternativa salva il disegno come DXF dal CAD.')
MSG_LENTO = 'DWG non leggibile: la conversione ha superato {s:.0f} secondi (file anomalo)'
MSG_ROTTO = 'DWG non leggibile: {motivo}'


def trova_oda() -> str | None:
    """Percorso di ODAFileConverter.exe (ODA_FC_PATH, poi Program Files), o None.
    Con piu' versioni installate vince la piu' recente."""
    custom = os.environ.get('ODA_FC_PATH')
    if custom and os.path.isfile(custom):
        return custom
    candidati = []
    for base in (r'C:\Program Files\ODA', r'C:\Program Files (x86)\ODA'):
        if os.path.isdir(base):
            candidati += glob.glob(os.path.join(base, 'ODAFileConverter*', 'ODAFileConverter.exe'))

    def _versione(p):
        nome = os.path.basename(os.path.dirname(p))
        num = []
        for parte in nome.replace('ODAFileConverter', '').strip().split('.'):
            try:
                num.append(int(parte))
            except ValueError:
                num.append(0)
        return num
    return max(candidati, key=_versione) if candidati else None


def impronta(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for blocco in iter(lambda: fh.read(1 << 16), b''):
            h.update(blocco)
    return h.hexdigest()


def _da_cache(digest: str) -> str | None:
    p = os.path.join(CACHE_DIR, digest + '.dxf')
    return p if os.path.isfile(p) and os.path.getsize(p) > 0 else None


def _in_cache(digest: str, dxf_path: str):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = os.path.join(CACHE_DIR, digest + '.tmp')
        shutil.copyfile(dxf_path, tmp)
        os.replace(tmp, os.path.join(CACHE_DIR, digest + '.dxf'))
    except OSError:
        logger.warning('cache DWG non scritta per %s', digest[:12], exc_info=True)


def _pota_cache():
    """Tiene la cache entro _CACHE_MAX_FILE file (via i piu' vecchi)."""
    try:
        voci = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR) if f.endswith('.dxf')]
    except OSError:
        return
    if len(voci) <= _CACHE_MAX_FILE:
        return
    voci.sort(key=lambda p: os.path.getmtime(p))
    for p in voci[:len(voci) - _CACHE_MAX_FILE]:
        try:
            os.remove(p)
        except OSError:
            pass


def _ferma(proc: subprocess.Popen):
    """Ferma ODA e i suoi eventuali figli."""
    try:
        import psutil
        for figlio in psutil.Process(proc.pid).children(recursive=True):
            try:
                figlio.kill()
            except Exception:
                pass
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(10)
    except Exception:
        pass


def _esegui_oda(oda: str, cart_in: str, cart_out: str, tempo_max: float) -> bool:
    """Converte tutti i .dwg di cart_in in cart_out. False se oltre il tempo."""
    cmd = [oda, cart_in, cart_out, 'ACAD2018', 'DXF', '0', '1', '*.DWG']
    kw = {}
    if os.name == 'nt':
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0                       # SW_HIDE
        kw = {'startupinfo': si, 'creationflags': _NO_WINDOW | _BELOW_NORMAL}
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, **kw)
    try:
        proc.wait(tempo_max)
        return True
    except subprocess.TimeoutExpired:
        _ferma(proc)
        return False


def _motivo_err(cart_out: str, stem: str) -> str:
    """Il motivo scritto da ODA nel file .err, in breve."""
    err = os.path.join(cart_out, stem + '.dxf.err')
    try:
        with open(err, 'r', encoding='utf-8', errors='replace') as fh:
            testo = fh.read()
    except OSError:
        return 'file danneggiato o non e\' un DWG'
    righe = [r.strip() for r in testo.splitlines() if r.strip() and 'thrown during' not in r]
    motivo = (righe[-1] if righe else '').split(':')[0].strip()
    noti = {'Unexpected end of file': 'file incompleto (troncato)',
            'Invalid group code': 'file danneggiato o non e\' un DWG',
            'File is not a drawing': 'non e\' un disegno DWG'}
    for chiave, testo_it in noti.items():
        if chiave.lower() in motivo.lower():
            return testo_it
    return ('file danneggiato (' + motivo[:80] + ')') if motivo else 'file danneggiato o non e\' un DWG'


def _e_dxf(path: str) -> bool:
    """DXF completo: comincia con una SECTION e finisce con EOF (un file
    lasciato a meta' da un ODA fermato per il tempo non passa)."""
    try:
        if os.path.getsize(path) < 50:
            return False
        with open(path, 'rb') as fh:
            testa = fh.read(64)
            fh.seek(-32, os.SEEK_END)
            coda = fh.read()
        return b'SECTION' in testa and b'EOF' in coda
    except OSError:
        return False


def _lotto(oda: str, voci: list, tempo_max: float) -> dict:
    """Una chiamata a ODA per tutte le voci [(indice, dwg_path)].
    Ritorna {indice: ('ok', dxf_tmp) | ('errore', msg) | ('lento', None)}.
    I nomi dentro la cartella sono numeri: ODA non ama gli accenti e i
    caratteri strani nei nomi ("Ø80"), e due "flangia.dwg" non si pestano."""
    cart = tempfile.mkdtemp(prefix='ft_dwg_')
    cart_in, cart_out = os.path.join(cart, 'in'), os.path.join(cart, 'out')
    os.makedirs(cart_in)
    os.makedirs(cart_out)
    esiti = {}
    try:
        for i, src in voci:
            shutil.copyfile(src, os.path.join(cart_in, f'{i:05d}.dwg'))
        finito = _esegui_oda(oda, cart_in, cart_out, tempo_max)
        for i, _src in voci:
            stem = f'{i:05d}'
            out = os.path.join(cart_out, stem + '.dxf')
            if os.path.isfile(out) and _e_dxf(out):
                tenuto = os.path.join(cart, stem + '.dxf')
                shutil.move(out, tenuto)
                esiti[i] = ('ok', tenuto)
            elif not finito and not os.path.isfile(os.path.join(cart_out, stem + '.dxf.err')):
                esiti[i] = ('lento', None)
            else:
                esiti[i] = ('errore', MSG_ROTTO.format(motivo=_motivo_err(cart_out, stem)))
        return {'cartella': cart, 'esiti': esiti}
    except Exception:
        shutil.rmtree(cart, ignore_errors=True)
        raise


def converti_percorsi(coppie, tempo_max: float | None = None) -> dict:
    """Converte [(dwg_path, dxf_path_destinazione), ...].

    Ritorna {dwg_path: {'ok': bool, 'errore': str|None, 'da_cache': bool,
    'secondi': float}}. Il DXF si scrive in dxf_path solo se 'ok'. Non
    solleva eccezioni per un singolo disegno."""
    coppie = [(str(a), str(b)) for a, b in coppie]
    tempo_max = TEMPO_MAX_S if tempo_max is None else float(tempo_max)
    t0 = time.time()
    esiti: dict = {}
    if not coppie:
        return esiti
    oda = trova_oda()
    da_fare = []                               # (indice, dwg, dxf, impronta)
    for i, (dwg, dxf) in enumerate(coppie):
        try:
            dig = impronta(dwg)
        except OSError as e:
            esiti[dwg] = {'ok': False, 'errore': MSG_ROTTO.format(motivo=f'file non leggibile ({e})'),
                          'da_cache': False}
            continue
        c = _da_cache(dig)
        if c:
            try:
                shutil.copyfile(c, dxf)
                os.utime(c, None)              # usato di recente: resta in cache
                esiti[dwg] = {'ok': True, 'errore': None, 'da_cache': True}
                continue
            except OSError:
                pass
        da_fare.append((i, dwg, dxf, dig))
    if da_fare and not oda:
        for _i, dwg, _dxf, _d in da_fare:
            esiti[dwg] = {'ok': False, 'errore': MSG_NON_INSTALLATO, 'da_cache': False}
        da_fare = []

    def _applica(lotto, voci_lotto):
        rimasti = []
        for i, dwg, dxf, dig in voci_lotto:
            stato, dato = lotto['esiti'].get(i, ('errore', MSG_ROTTO.format(motivo='nessun risultato')))
            if stato == 'ok':
                try:
                    os.makedirs(os.path.dirname(os.path.abspath(dxf)), exist_ok=True)
                    shutil.copyfile(dato, dxf)
                    _in_cache(dig, dato)
                    esiti[dwg] = {'ok': True, 'errore': None, 'da_cache': False}
                except OSError as e:
                    esiti[dwg] = {'ok': False, 'errore': MSG_ROTTO.format(motivo=f'DXF non scritto ({e})'),
                                  'da_cache': False}
            elif stato == 'lento':
                rimasti.append((i, dwg, dxf, dig))
            else:
                esiti[dwg] = {'ok': False, 'errore': dato, 'da_cache': False}
        return rimasti

    if da_fare:
        budget = (tempo_max if len(da_fare) == 1 else
                  min(_TEMPO_LOTTO_MAX_S, max(tempo_max, _TEMPO_BASE_S + _TEMPO_PER_DISEGNO_S * len(da_fare))))
        lotto = _lotto(oda, [(i, dwg) for i, dwg, _x, _d in da_fare], budget)
        try:
            rimasti = _applica(lotto, da_fare)
        finally:
            shutil.rmtree(lotto['cartella'], ignore_errors=True)
        if rimasti and len(da_fare) > 1:
            # Il lotto si e' fermato: chi non e' stato convertito si riprova
            # da solo, col suo tempo massimo (uno blocca, gli altri passano).
            logger.warning('conversione DWG: lotto oltre %.0f s, %d disegni si riprovano uno per uno',
                           budget, len(rimasti))
            nuovi = []
            for voce in rimasti:
                lotto = _lotto(oda, [(voce[0], voce[1])], tempo_max)
                try:
                    nuovi += _applica(lotto, [voce])
                finally:
                    shutil.rmtree(lotto['cartella'], ignore_errors=True)
            rimasti = nuovi
        for _i, dwg, _dxf, _d in rimasti:
            esiti[dwg] = {'ok': False, 'errore': MSG_LENTO.format(s=tempo_max), 'da_cache': False,
                          'codice': 'troppo_lento'}
        _pota_cache()
    durata = round(time.time() - t0, 2)
    for e in esiti.values():
        e.setdefault('secondi', durata)
    n_ok = sum(1 for e in esiti.values() if e['ok'])
    logger.info('conversione DWG: %d/%d convertiti (%d dalla cache) in %.1f s',
                n_ok, len(esiti), sum(1 for e in esiti.values() if e.get('da_cache')), durata)
    return esiti


def nome_dxf(nome_dwg: str) -> str:
    """"Pezzo.DWG" -> "Pezzo.dxf"."""
    return os.path.splitext(nome_dwg)[0] + '.dxf'


def e_dwg(nome: str) -> bool:
    return (nome or '').lower().endswith('.dwg')
