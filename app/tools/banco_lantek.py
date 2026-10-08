"""Banco di prova del motore di riconoscimento contro l'archivio di Lantek.

Per ogni pezzo di Lantek che ha il suo DXF nell'archivio (stesso nome del
codice) fa girare il motore VERO dell'import (dxf_batch_worker.process_single_dxf:
detector v3, cartiglio, spessore, pulizia) e salva quello che ha letto. Il
confronto con Lantek (area, perimetro, inneschi, ingombro, spessore, materiale)
lo fa banco_lantek_report.py.

    python tools/banco_lantek.py --gt <lantek_gt.csv> --out <cartella> [--workers 4]
           [--limite N] [--campione N --seme 1] [--codici file.txt] [--timeout 120]

- non tocca l'archivio: ogni disegno viene copiato in una cartella temporanea
  (la pulizia scrive <nome>_cleaned.dxf accanto al file);
- niente cache dei DXF (misura sempre il codice di adesso);
- processi a priorita' bassa: il PC e' anche il server di FerroTrack e Lantek;
- riprende da dove si era fermato (salta i codici gia' nel file dei risultati).
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import multiprocessing as mp
import os
import queue
import random
import shutil
import sys
import tempfile
import threading
import time
import types

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DXF_CFG = {
    'dxf_colori_piega': [2], 'dxf_colori_saldatura': [1],
    'dxf_lunghezza_minima': 15.0, 'dxf_tolleranza_centro': 1.0,
    'dxf_svasatura_ratio_min': 1.8, 'dxf_svasatura_ratio_max': 3.0,
    'dxf_semicerchio_angolo_min': 150.0, 'dxf_semicerchio_angolo_max': 320.0,
    'dxf_filtra_zona_sviluppata': True,
}


def _priorita_bassa():
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetPriorityClass(k.GetCurrentProcess(), 0x00004000)   # BELOW_NORMAL
    except Exception:
        pass


def _carica_moduli():
    """backend.preventivi.* senza backend/__init__ (che avvia l'app e il DB)."""
    if APP not in sys.path:
        sys.path.insert(0, APP)
    if 'backend' not in sys.modules:
        pkg = types.ModuleType('backend')
        pkg.__path__ = [os.path.join(APP, 'backend')]
        sys.modules['backend'] = pkg
    logging.disable(logging.CRITICAL)
    from backend.preventivi import dxf_cache
    dxf_cache.get = lambda *a, **k: None
    dxf_cache.put = lambda *a, **k: None


def _num(v):
    try:
        return None if v is None else float(v)
    except Exception:
        return None


def misura(path: str, cartella_tmp: str) -> dict:
    from backend.preventivi.dxf_batch_worker import process_single_dxf
    nome = os.path.basename(path)
    copia = os.path.join(cartella_tmp, nome)
    shutil.copy2(path, copia)
    t0 = time.perf_counter()
    try:
        p = process_single_dxf(copia, nome, dict(DXF_CFG))
    finally:
        for f in os.listdir(cartella_tmp):
            try:
                os.remove(os.path.join(cartella_tmp, f))
            except OSError:
                pass
    dt = time.perf_counter() - t0
    if not p.get('success'):
        return {'ok': False, 'errore': str(p.get('error'))[:300], 't': round(dt, 2)}
    g = p.get('geometria') or {}
    sp = p.get('spessore') or {}
    ca = p.get('cartiglio') or {}
    cl = p.get('cleanup') or {}
    return {
        'ok': True, 't': round(dt, 2),
        'area_dm2': _num(g.get('area_dm2')),
        'area_lorda_dm2': _num(g.get('area_lorda_dm2')),
        'perim_m': _num(g.get('perimetro_taglio_m')),
        'n_pierce': g.get('n_pierce', g.get('n_forature')),
        'n_fori': g.get('n_fori', g.get('n_inner')),
        'bbox': [_num(g.get('bbox_width_mm')), _num(g.get('bbox_height_mm'))],
        'conf': _num(g.get('confidence')),
        'manuale': bool(g.get('needs_manual_select')),
        'fonte_geo': g.get('_source') or 'detector',
        'n_pezzi': g.get('n_pezzi_rilevati'),
        'scala': _num(g.get('scala_unita_mm')),
        'avvisi': [str(w)[:160] for w in (g.get('warnings') or [])][:8],
        'sp_mm': _num(sp.get('spessore_mm')), 'sp_conf': _num(sp.get('confidence')),
        'sp_fonte': sp.get('source'),
        'sp_fonti': sp.get('fonti'), 'sp_avvisi': [str(w)[:160] for w in (sp.get('warnings') or [])][:4],
        'mat': ca.get('materiale'), 'mat_conf': _num(ca.get('confidence')),
        'tratti': g.get('tratti_aperti'),
        'pulito': cl.get('cleaned_status'), 'pulito_motivo': (cl.get('cleanup_reason') or '')[:160],
        'indizi': cl.get('indizi'),
        # fori tondi (diametro, area mm2, perimetro mm) e fori da trapano (regola dei 2/3)
        'fori_tondi': [[f.get('d_mm'), f.get('area_mm2'), f.get('perim_mm')] for f in (g.get('fori_tondi') or [])],
        'fori_trapano': [f.get('d_mm') for f in (g.get('fori_trapano') or [])],
        'fori_trapano_dubbio': bool(g.get('fori_trapano_dubbio')),
        'pul_trapano': {k: (cl.get('cleanup_stats') or {}).get(k) for k in ('n_fori_trapano', 'n_trapano_tolti')},
    }


def _lavoratore(id_w: int, compiti, risultati, timeout: float):
    _priorita_bassa()
    _carica_moduli()
    tmp = tempfile.mkdtemp(prefix=f'banco_w{id_w}_')
    while True:
        try:
            item = compiti.get(timeout=5)
        except queue.Empty:
            continue
        if item is None:
            break
        codice, path = item
        esito = {}

        def corri():
            try:
                esito['r'] = misura(path, tmp)
            except Exception as e:      # noqa: BLE001
                esito['r'] = {'ok': False, 'errore': f'{type(e).__name__}: {e}'[:300]}

        th = threading.Thread(target=corri, daemon=True)
        th.start()
        th.join(timeout)
        if th.is_alive():
            risultati.put({'codice': codice, 'file': path, 'ok': False, 'errore': 'TIMEOUT', 't': timeout})
            os._exit(3)     # il thread non si puo' fermare: si chiude il processo, il capo ne avvia un altro
        r = esito.get('r') or {'ok': False, 'errore': 'nessun risultato'}
        r.update({'codice': codice, 'file': path})
        risultati.put(r)
    shutil.rmtree(tmp, ignore_errors=True)


def scegli(gt_csv: str, args) -> list[tuple[str, str]]:
    righe = list(csv.DictReader(open(gt_csv, encoding='utf-8-sig'), delimiter=';'))
    sel = [r for r in righe
           if r['dxf_match_kind'] == 'exact' and not r['flags']
           and r['dxf_match'].lower().endswith('.dxf') and os.path.isfile(r['dxf_match'])]
    if args.codici:
        voluti = {l.strip().lower() for l in open(args.codici, encoding='utf-8') if l.strip()}
        sel = [r for r in sel if r['code'].lower() in voluti]
    sel.sort(key=lambda r: r['code'])
    if args.campione:
        random.Random(args.seme).shuffle(sel)
        sel = sel[:args.campione]
    if args.limite:
        sel = sel[:args.limite]
    return [(r['code'], r['dxf_match']) for r in sel]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--gt', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--workers', type=int, default=4)
    ap.add_argument('--limite', type=int, default=0)
    ap.add_argument('--campione', type=int, default=0)
    ap.add_argument('--seme', type=int, default=1)
    ap.add_argument('--codici')
    ap.add_argument('--timeout', type=float, default=120)
    ap.add_argument('--nome', default='risultati')
    args = ap.parse_args()
    _priorita_bassa()

    os.makedirs(args.out, exist_ok=True)
    uscita = os.path.join(args.out, args.nome + '.jsonl')
    fatti = set()
    if os.path.isfile(uscita):
        for l in open(uscita, encoding='utf-8'):
            try:
                fatti.add(json.loads(l)['codice'])
            except Exception:
                pass
    lavoro = [x for x in scegli(args.gt, args) if x[0] not in fatti]
    print(f'da misurare {len(lavoro)} (gia\' fatti {len(fatti)}) -> {uscita}', flush=True)
    if not lavoro:
        return

    ctx = mp.get_context('spawn')
    compiti, risultati = ctx.Queue(), ctx.Queue()
    for x in lavoro:
        compiti.put(x)
    for _ in range(args.workers):
        compiti.put(None)

    def avvia(i):
        p = ctx.Process(target=_lavoratore, args=(i, compiti, risultati, args.timeout), daemon=True)
        p.start()
        return p

    proc = [avvia(i) for i in range(args.workers)]
    n, t0 = 0, time.time()
    with open(uscita, 'a', encoding='utf-8') as f:
        while n < len(lavoro):
            try:
                r = risultati.get(timeout=10)
            except queue.Empty:
                r = None
            if r is not None:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')
                f.flush()
                n += 1
                if n % 100 == 0 or n == len(lavoro):
                    v = n / max(time.time() - t0, 1e-6)
                    print(f'{n}/{len(lavoro)}  {v * 60:.0f}/min  resto ~{(len(lavoro) - n) / max(v, 1e-6) / 60:.0f} min',
                          flush=True)
            # processi chiusi per timeout: se ne avvia un altro (la coda ha ancora lavoro)
            for i, p in enumerate(proc):
                if not p.is_alive() and p.exitcode == 3:
                    proc[i] = avvia(i)
            if all(not p.is_alive() for p in proc) and risultati.empty():
                break
    print(f'finito: {n} pezzi in {(time.time() - t0) / 60:.1f} min', flush=True)


if __name__ == '__main__':
    main()
