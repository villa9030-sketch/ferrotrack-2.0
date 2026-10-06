"""Copia di sicurezza ESTERNA (cartella su un altro PC della rete).

Copre:
 1. oltre al database si copiano le impostazioni (app_config.json) e i disegni
    (uploads): col solo database un ordine ripristinato non ha piu' disegni
 2. i disegni si copiano solo se nuovi o cambiati; i temporanei no
 3. un file tolto dal programma resta nella copia esterna
 4. le copie del database all'esterno si ruotano (non riempiono il disco)
 5. cartella esterna non raggiungibile: nessun errore, un solo avviso

Tutto in cartelle temporanee: il database e i disegni veri non si toccano.
Esecuzione: python app/tests/test_backup_esterno.py
"""
import json
import os
import shutil
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


def main():
    tmp = Path(tempfile.mkdtemp(prefix='test_backup_est_'))
    prog = tmp / 'app'
    (prog / 'database' / 'backups').mkdir(parents=True)
    (prog / 'uploads' / 'drawings' / 'o1').mkdir(parents=True)
    (prog / 'uploads' / 'pdfs').mkdir(parents=True)
    (prog / 'uploads' / 'preventivi_tmp').mkdir(parents=True)
    esterno = tmp / 'PC_UFFICIO' / 'FerroTrack'
    esterno.mkdir(parents=True)

    db = prog / 'database' / 'scheduler.db'
    c = sqlite3.connect(str(db))
    c.execute('CREATE TABLE t (id INTEGER PRIMARY KEY)'); c.commit(); c.close()
    (prog / 'app_config.json').write_text('{"tariffa": 1}', encoding='utf-8')
    (prog / 'uploads' / 'drawings' / 'o1' / 'pezzo.dxf').write_text('DXF', encoding='utf-8')
    (prog / 'uploads' / 'pdfs' / 'ordine.pdf').write_bytes(b'%PDF')
    (prog / 'uploads' / 'tmp_x.dxf').write_text('temp', encoding='utf-8')

    backup_db._HERE = prog
    backup_db.DB_PATH = db
    backup_db.DEFAULT_BACKUP_DIR = prog / 'database' / 'backups'
    backup_db.CONFIG_PATH = prog / 'backup_config.json'
    backup_db.UPLOADS_DIR = prog / 'uploads'
    avvisi = []
    backup_db._avvisa = lambda t, m: avvisi.append((t, m))
    backup_db.CONFIG_PATH.write_text(json.dumps({'remote_path': str(esterno), 'max_backups': 3,
                                                 'giorni_storico': 0}), encoding='utf-8')

    print('1) Database, impostazioni e disegni')
    r = backup_db.backup()
    check('backup fatto', r is not None)
    check('database nella copia esterna', len(list((esterno / 'database').glob('scheduler_*.db'))) == 1)
    check('impostazioni nella copia esterna', (esterno / 'impostazioni' / 'app_config.json').exists())
    check('disegni nella copia esterna', (esterno / 'disegni' / 'drawings' / 'o1' / 'pezzo.dxf').exists()
          and (esterno / 'disegni' / 'pdfs' / 'ordine.pdf').exists())
    check('i temporanei no', not (esterno / 'disegni' / 'tmp_x.dxf').exists()
          and not (esterno / 'disegni' / 'preventivi_tmp').exists())

    print('\n2) Solo i file nuovi o cambiati')
    n = backup_db._copia_disegni(backup_db.UPLOADS_DIR, esterno / 'disegni')
    check('nulla di nuovo: niente copiato', n == 0, n)
    time.sleep(1.1)
    (prog / 'uploads' / 'drawings' / 'o1' / 'pezzo.dxf').write_text('DXF v2', encoding='utf-8')
    (prog / 'uploads' / 'drawings' / 'o1' / 'nuovo.dxf').write_text('N', encoding='utf-8')
    n = backup_db._copia_disegni(backup_db.UPLOADS_DIR, esterno / 'disegni')
    check('copiati il cambiato e il nuovo', n == 2, n)
    check('la versione nuova e\' arrivata',
          (esterno / 'disegni' / 'drawings' / 'o1' / 'pezzo.dxf').read_text(encoding='utf-8') == 'DXF v2')

    print('\n3) Un file tolto dal programma resta nella copia')
    os.remove(prog / 'uploads' / 'pdfs' / 'ordine.pdf')
    backup_db.backup()
    check('il PDF tolto c\'e\' ancora fuori', (esterno / 'disegni' / 'pdfs' / 'ordine.pdf').exists())

    print('\n4) Le copie esterne del database si ruotano')
    for _ in range(5):
        time.sleep(1.05)
        backup_db.backup()
    n_db = len(list((esterno / 'database').glob('scheduler_schedulato_*.db')))
    check('all\'esterno non piu\' di quelle tenute (3)', n_db <= 3, n_db)

    print('\n5) Cartella esterna non raggiungibile')
    shutil.rmtree(esterno.parent)
    avvisi.clear()
    backup_db._ultimo_avviso_remoto = 0.0
    r1 = backup_db.backup()
    time.sleep(1.05)
    r2 = backup_db.backup()
    check('il backup locale si fa lo stesso', r1 is not None and r2 is not None)
    check('un solo avviso all\'amministrazione, non uno all\'ora', len(avvisi) == 1, avvisi)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f'\nPASSATI: {OK}   FALLITI: {len(KO)}')
    return 0 if not KO else 1


if __name__ == '__main__':
    sys.exit(main())
