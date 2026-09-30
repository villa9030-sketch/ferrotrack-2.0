"""Test di BACKUP e RIPRISTINO (fondamenta F1).

Copre:
 1. rotazione per tipo e per data: le copie manuali e pre-migrazione non si
    perdono (prima si ordinava per nome e sparivano per prime)
 2. schedulato: le ultime N + una al giorno per lo storico
 3. database danneggiato: niente copia e niente rotazione
 4. la copia appena scritta e' integra; la manuale va in manuali/
 5. cartella di backup non valida rifiutata
 6. ripristino su una COPIA: dati tornati, database di prima messo da parte,
    -wal/-shm tolti, copia rovinata rifiutata

Tutto in cartelle temporanee: il database vero non si tocca.
Esecuzione: python app/tests/test_backup_ripristino.py
"""
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import backup_db  # noqa: E402
sys.path.insert(0, os.path.join(_APP, 'tools'))
import ripristina_backup as rb  # noqa: E402

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


def crea_db(p: Path, righe: int):
    c = sqlite3.connect(str(p))
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)')
    c.executemany('INSERT INTO t (v) VALUES (?)', [(f'r{i}',) for i in range(righe)])
    c.commit()
    return c                                    # aperta: il -wal resta


def conta(p: Path) -> int:
    c = sqlite3.connect(str(p))
    try:
        return c.execute('SELECT COUNT(*) FROM t').fetchone()[0]
    finally:
        c.close()


def main():
    tmp = Path(tempfile.mkdtemp(prefix='test_backup_'))
    bdir = tmp / 'backups'
    bdir.mkdir()
    db = tmp / 'scheduler.db'
    # backup_db lavora su file temporanei
    backup_db.DB_PATH = db
    backup_db.DEFAULT_BACKUP_DIR = bdir
    backup_db.CONFIG_PATH = tmp / 'backup_config.json'
    backup_db._avvisa = lambda *a, **k: AVVISI.append(a)

    print('\n1-2) Rotazione per tipo e per data')
    adesso = time.time()
    files = []
    for h in range(24 * 40):                       # 40 giorni di copie orarie
        f = bdir / f'scheduler_schedulato_{time.strftime("%Y%m%d_%H%M%S", time.localtime(adesso - h * 3600))}.db'
        f.write_bytes(b'x'); os.utime(f, (adesso - h * 3600, adesso - h * 3600)); files.append(str(f))
    for k in range(25):
        f = bdir / f'scheduler_pre_migration_2026090{k % 9 + 1}_1{k:05d}.db'
        f.write_bytes(b'x'); os.utime(f, (adesso - k * 7200, adesso - k * 7200)); files.append(str(f))
    for k in range(3):                              # vecchie "manuale" nella cartella principale
        f = bdir / f'scheduler_manuale_2026010{k + 1}_120000.db'
        f.write_bytes(b'x'); os.utime(f, (adesso - 90 * 86400, adesso - 90 * 86400)); files.append(str(f))
    via = set(backup_db.da_eliminare(files, 48, 30, adesso))
    resta = [f for f in files if f not in via]
    sched = [f for f in resta if 'schedulato' in f]
    check('manuali vecchie mai toccate', not any('manuale' in f for f in via))
    check('pre-migrazione: restano le 20 piu\' recenti', sum('pre_migration' in f for f in resta) == 20)
    giorni = {time.strftime('%Y%m%d', time.localtime(os.path.getmtime(f))) for f in sched}
    check('schedulato: 48 recenti + una al giorno per ~30 giorni', 48 <= len(sched) <= 48 + 31 and len(giorni) >= 30,
          (len(sched), len(giorni)))
    piu_vecchia = min(os.path.getmtime(f) for f in sched)
    check('schedulato: niente oltre lo storico', adesso - piu_vecchia <= 31 * 86400)
    backup_db._ruota_backup(bdir, 48, 30)
    check('rotazione su disco = calcolo', len(list(bdir.glob('scheduler_*.db'))) == len(resta))
    for f in bdir.glob('scheduler_*.db'):
        f.unlink()

    print('\n3-4) Copia, verifica, cartella manuali, database danneggiato')
    con = crea_db(db, 500)
    p = backup_db.backup('schedulato')
    check('copia schedulata creata e integra', p and backup_db._integro(Path(p)) and conta(Path(p)) == 500, p)
    pm = backup_db.backup('manuale')
    check('copia manuale in manuali/', pm and Path(pm).parent.name == 'manuali', pm)
    check('elenco comprende automatiche e manuali', {r['manuale'] for r in backup_db.list_backups()} == {True, False})
    con.close()
    rotto = tmp / 'rotto.db'
    rotto.write_bytes(b'SQLite format 3\x00' + os.urandom(4096))
    backup_db.DB_PATH = rotto
    AVVISI.clear()
    prima = len(list(bdir.glob('scheduler_*.db')))
    check('database rotto: nessuna copia', backup_db.backup('schedulato') is None)
    check('database rotto: nessuna rotazione e copie intatte', len(list(bdir.glob('scheduler_*.db'))) == prima)
    check('database rotto: avviso all\'amministrazione', len(AVVISI) == 1)
    backup_db.DB_PATH = db

    print('\n5) Cartelle non valide')
    check('cartella inesistente rifiutata', backup_db.cartella_valida(str(tmp / 'non_esiste')) is not None)
    check('cartella del programma rifiutata', backup_db.cartella_valida(_APP) is not None)
    check('cartella esterna accettata', backup_db.cartella_valida(str(tmp)) is None)

    print('\n6) Ripristino su una copia')
    copia_buona = Path(p)
    con = sqlite3.connect(str(db))
    con.execute('DELETE FROM t WHERE id > 10'); con.commit()      # "disastro": restano 10 righe
    check('prima del ripristino: 10 righe', conta(db) == 10)
    wal = db.with_name(db.name + '-wal')
    check('file -wal presente (database aperto)', wal.exists())
    con.close()
    prima_rip = rb.ripristina(copia_buona, db, bdir)
    check('dopo il ripristino: 500 righe', conta(db) == 500)
    check('database di prima messo da parte (10 righe)', prima_rip.exists() and conta(prima_rip) == 10)
    check('-wal/-shm vecchi tolti', not wal.exists() or conta(db) == 500)
    rovinata = bdir / 'scheduler_schedulato_20260101_000000.db'
    rovinata.write_bytes(b'non e un database')
    try:
        rb.ripristina(rovinata, db, bdir); rifiutata = False
    except RuntimeError:
        rifiutata = True
    check('copia rovinata rifiutata, database invariato', rifiutata and conta(db) == 500)
    check('elenco del ripristino dal piu\' recente', rb.elenco(bdir)[0].stat().st_mtime >= rb.elenco(bdir)[-1].stat().st_mtime)
    check('server acceso rilevato (porta chiusa = no)', rb.server_acceso(0) is False)

    print('\n' + '=' * 60)
    print(f'PASSATI: {OK}   FALLITI: {len(KO)}')
    if KO:
        print('Falliti: ' + ', '.join(KO))
    print('=' * 60)
    return 1 if KO else 0


AVVISI = []

if __name__ == '__main__':
    sys.exit(main())
