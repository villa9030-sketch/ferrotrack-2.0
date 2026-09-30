"""Ripristino del database da un backup.

    python tools\\ripristina_backup.py            elenca le copie (dalla piu' recente)
    python tools\\ripristina_backup.py 3          ripristina la copia n. 3 dell'elenco
    python tools\\ripristina_backup.py NOME.db    ripristina la copia con quel nome

Passi (vedi RIPRISTINO.md):
  1. rifiuta se il server risponde sulla porta 5000 (va spento prima);
  2. verifica l'integrita' della copia scelta;
  3. mette da parte il database attuale in backups/manuali/
     (scheduler_prima_ripristino_<data>.db), anche se rovinato;
  4. copia la copia scelta al posto del database e toglie i file -wal/-shm
     del database vecchio (altrimenti SQLite li riapplicherebbe).

Per le prove: --db <file> e --backup-dir <cartella> lavorano su una copia,
--porta 0 salta il controllo del server. Senza --si chiede conferma.
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import sqlite3
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP))
import backup_db  # noqa: E402


def server_acceso(porta: int) -> bool:
    if not porta:
        return False
    try:
        with socket.create_connection(('127.0.0.1', porta), timeout=1):
            return True
    except OSError:
        return False


def integro(percorso: Path) -> bool:
    try:
        c = sqlite3.connect(f'file:{percorso}?mode=ro', uri=True)
        try:
            r = c.execute('PRAGMA integrity_check').fetchone()
        finally:
            c.close()
        return bool(r) and r[0] == 'ok'
    except Exception:
        return False


def elenco(backup_dir: Path) -> list[Path]:
    files = list(backup_dir.glob('scheduler_*.db')) + list((backup_dir / 'manuali').glob('scheduler_*.db'))
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def ripristina(copia: Path, db: Path, backup_dir: Path) -> Path:
    """Ripristina `copia` su `db`. Ritorna dove ha messo il database di prima."""
    if not integro(copia):
        raise RuntimeError(f'la copia {copia.name} non passa il controllo di integrita\'')
    manuali = backup_dir / 'manuali'
    manuali.mkdir(parents=True, exist_ok=True)
    prima = manuali / f'scheduler_prima_ripristino_{time.strftime("%Y%m%d_%H%M%S")}.db'
    if db.exists():
        try:
            s = sqlite3.connect(str(db)); d = sqlite3.connect(str(prima))
            try:
                s.backup(d)             # include il contenuto del -wal
            finally:
                d.close(); s.close()
        except Exception:
            shutil.copy2(db, prima)     # database rovinato: copia com'e'
    tmp = db.with_name(db.name + '.ripristino')
    shutil.copy2(copia, tmp)
    for suff in ('-wal', '-shm'):
        f = db.with_name(db.name + suff)
        if f.exists():
            f.unlink()
    os.replace(tmp, db)
    if not integro(db):
        raise RuntimeError('dopo la copia il database non e\' integro: rimetti a mano ' + str(prima))
    return prima


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Ripristino del database FerroTrack da un backup')
    ap.add_argument('scelta', nargs='?', help='numero nell\'elenco o nome del file')
    ap.add_argument('--db', default=str(backup_db.DB_PATH))
    ap.add_argument('--backup-dir', default=str(backup_db._get_backup_dir()))
    ap.add_argument('--porta', type=int, default=5000)
    ap.add_argument('--si', action='store_true', help='non chiedere conferma')
    a = ap.parse_args(argv)
    db, bdir = Path(a.db), Path(a.backup_dir)
    copie = elenco(bdir)
    if not a.scelta:
        print(f'Copie in {bdir} (dalla piu\' recente):')
        for k, p in enumerate(copie[:40], 1):
            print(f'  {k:3}  {time.strftime("%d/%m/%Y %H:%M", time.localtime(p.stat().st_mtime))}  '
                  f'{p.stat().st_size // 1024:7} KB  {p.relative_to(bdir)}')
        print('\nPer ripristinare: python tools\\ripristina_backup.py <numero>')
        return 0
    if a.scelta.isdigit():
        k = int(a.scelta)
        if not 1 <= k <= len(copie):
            print('Numero fuori elenco.'); return 2
        copia = copie[k - 1]
    else:
        trovate = [p for p in copie if p.name == os.path.basename(a.scelta)]
        if not trovate:
            print('Copia non trovata.'); return 2
        copia = trovate[0]
    if server_acceso(a.porta):
        print(f'Il server risponde sulla porta {a.porta}: spegnilo prima di ripristinare.')
        return 3
    print(f'Ripristino di {copia.name} ({time.strftime("%d/%m/%Y %H:%M", time.localtime(copia.stat().st_mtime))}) su {db}')
    if not a.si and input('Confermi? Scrivi SI: ').strip().upper() != 'SI':
        print('Annullato.'); return 1
    try:
        prima = ripristina(copia, db, bdir)
    except RuntimeError as e:
        print('ERRORE:', e); return 4
    print(f'Fatto. Il database di prima e\' in {prima}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
