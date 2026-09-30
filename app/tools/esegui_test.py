"""Tutti i test di FerroTrack con un solo comando.

    .venv\\Scripts\\python.exe tools\\esegui_test.py            tutti
    .venv\\Scripts\\python.exe tools\\esegui_test.py ore backup  solo quelli col nome che contiene "ore" o "backup"

Ogni tests/test_*.py gira in un processo separato con:
  - FERROTRACK_SKIP_DB_INIT=1 (nessuna inizializzazione del database vero)
  - FERROTRACK_DB = un file temporaneo vuoto: anche un test che non si crea il
    suo database non puo' scrivere su quello di lavoro
I test che vogliono il server di prova (porta 5056) o Playwright si saltano,
dicendolo, se non sono disponibili. Uscita 0 solo se nessuno fallisce.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
TESTS = APP / 'tests'
PY = sys.executable


def _porta(p: int) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', p), timeout=1):
            return True
    except OSError:
        return False


def _playwright() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def perche_saltare(testo: str) -> str | None:
    if '5056' in testo and not _porta(5056):
        return 'serve il server di prova sulla porta 5056 (tools/server_prova.py)'
    if 'playwright' in testo and not _playwright():
        return 'serve Playwright'
    return None


def main(filtri: list[str]) -> int:
    files = sorted(TESTS.glob('test_*.py'))
    if filtri:
        files = [f for f in files if any(x.lower() in f.name.lower() for x in filtri)]
    esiti = []
    t0 = time.time()
    for f in files:
        testo = f.read_text(encoding='utf-8', errors='replace')
        motivo = perche_saltare(testo)
        if motivo:
            esiti.append((f.name, 'SALTATO', motivo, 0.0))
            print(f'  -  {f.name:45} saltato: {motivo}')
            continue
        tmpdb = Path(tempfile.gettempdir()) / f'ft_test_{os.getpid()}_{f.stem}.db'
        env = dict(os.environ, FERROTRACK_SKIP_DB_INIT='1', FERROTRACK_DB=str(tmpdb),
                   PYTHONIOENCODING='utf-8', PYTHONWARNINGS='ignore')
        s = time.time()
        try:
            r = subprocess.run([PY, str(f)], cwd=str(APP), env=env, capture_output=True,
                               text=True, encoding='utf-8', errors='replace', timeout=900)
            ok = r.returncode == 0
            coda = (r.stdout + r.stderr).strip().splitlines()[-12:]
        except subprocess.TimeoutExpired:
            ok, coda = False, ['TIMEOUT dopo 15 minuti']
        finally:
            for suff in ('', '-wal', '-shm'):
                try:
                    Path(str(tmpdb) + suff).unlink()
                except OSError:
                    pass
        dt = time.time() - s
        esiti.append((f.name, 'OK' if ok else 'FALLITO', '' if ok else '\n'.join(coda), dt))
        print(f"  {'ok' if ok else 'KO'} {f.name:45} {dt:6.1f} s")
    falliti = [e for e in esiti if e[1] == 'FALLITO']
    saltati = [e for e in esiti if e[1] == 'SALTATO']
    print('\n' + '=' * 70)
    print(f'{len(esiti) - len(falliti) - len(saltati)} passati, {len(falliti)} falliti, '
          f'{len(saltati)} saltati — {time.time() - t0:.0f} s')
    for nome, _, coda, _ in falliti:
        print(f'\n--- {nome} (ultime righe) ---\n{coda}')
    print('=' * 70)
    return 1 if falliti else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
