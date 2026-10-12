# -*- coding: utf-8 -*-
"""Prepara la COPIA per la prova: uploads, database, impostazioni, persone.

La produzione (ferrotrack-2.0) si LEGGE soltanto.
"""
import json
import os
import shutil
import sqlite3
import subprocess

from comune import (APP, PROD_CONFIG, PROD_DB, PROD_DXF_CACHE, PROD_UPLOADS, PRODUZIONE_APP,
                    PYTHON, STRESS_UPLOADS, PIN_ADMIN, DISEGNI_LANTEK)

CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL = 0x00004000


def impronta_produzione():
    """mtime e dimensioni di cio' che non si deve toccare (per verificarlo alla fine)."""
    out = {}
    for p in (PROD_DB, PROD_DB + '-wal', PROD_CONFIG, PROD_DXF_CACHE):
        if os.path.exists(p):
            st = os.stat(p)
            out[p] = [st.st_mtime, st.st_size]
    for radice in (PROD_UPLOADS, DISEGNI_LANTEK):
        n, ultimo, tot = 0, 0.0, 0
        if os.path.isdir(radice):
            for d, _, files in os.walk(radice):
                for f in files:
                    try:
                        st = os.stat(os.path.join(d, f))
                    except OSError:
                        continue
                    n += 1
                    tot += st.st_size
                    ultimo = max(ultimo, st.st_mtime)
        out[radice] = [n, tot, ultimo]
    return out


def copia_uploads(reg):
    """robocopy /MIR della cartella uploads di produzione nella copia (solo lettura alla sorgente)."""
    assert os.path.normcase(STRESS_UPLOADS).find('ferrotrack-stress') >= 0
    os.makedirs(STRESS_UPLOADS, exist_ok=True)
    cmd = ['robocopy', PROD_UPLOADS, STRESS_UPLOADS, '/MIR', '/R:1', '/W:1', '/NFL', '/NDL', '/NP', '/MT:4']
    r = subprocess.run(cmd, capture_output=True, text=True, errors='replace',
                       creationflags=CREATE_NO_WINDOW | BELOW_NORMAL)
    if r.returncode >= 8:
        raise RuntimeError('robocopy uploads fallito (%s): %s' % (r.returncode, r.stdout[-800:]))
    reg.log('uploads copiati (robocopy %s)' % r.returncode)


def copia_database(dati_dir, reg):
    os.makedirs(dati_dir, exist_ok=True)
    dst = os.path.join(dati_dir, 'scheduler.db')
    for coda in ('', '-wal', '-shm'):
        if os.path.exists(dst + coda):
            os.remove(dst + coda)
    src = sqlite3.connect('file:%s?mode=ro' % PROD_DB.replace('\\', '/'), uri=True)
    out = sqlite3.connect(dst)
    src.backup(out)
    src.close()
    # I percorsi dei file nel DB sono assoluti e puntano alla produzione:
    # nella copia si fanno puntare agli uploads della copia.
    vecchio = os.path.join(PRODUZIONE_APP, '')
    nuovo = os.path.join(APP, '')
    n = out.execute('update order_files set filepath = replace(filepath, ?, ?) where instr(filepath, ?) > 0',
                    (vecchio, nuovo, vecchio)).rowcount
    out.commit()
    rimasti = out.execute("select count(*) from order_files where instr(filepath, 'ferrotrack-2.0') > 0").fetchone()[0]
    out.close()
    if rimasti:
        raise RuntimeError('%d percorsi di file puntano ancora alla produzione' % rimasti)
    reg.log('database copiato (%d percorsi riscritti)' % n)
    # cache dei DXF: solo per velocita', nella copia del codice
    try:
        cache_dst = os.path.join(APP, 'database', 'dxf_cache.db')
        os.makedirs(os.path.dirname(cache_dst), exist_ok=True)
        if os.path.isfile(PROD_DXF_CACHE) and not os.path.isfile(cache_dst):
            s = sqlite3.connect('file:%s?mode=ro' % PROD_DXF_CACHE.replace('\\', '/'), uri=True)
            o = sqlite3.connect(cache_dst)
            s.backup(o)
            s.close()
            o.close()
    except Exception as e:
        reg.log('cache DXF non copiata:', e)
    return dst


def copia_configurazione(dati_dir, reg):
    with open(PROD_CONFIG, encoding='utf-8') as f:
        cfg = json.load(f)
    cfg['disegni_export_root'] = ''
    cfg['cartelle_disegni_consentite'] = []
    # ogni altra voce che sembra un percorso esterno si svuota
    svuotati = []

    def pulisci(d, prefisso=''):
        for k, v in list(d.items()):
            if isinstance(v, dict):
                pulisci(v, prefisso + k + '.')
            elif isinstance(v, str) and (v.startswith('\\\\') or (len(v) > 2 and v[1] == ':' and v[2] in '\\/')):
                if k not in ('logo_path',):
                    d[k] = ''
                    svuotati.append(prefisso + k)
    pulisci(cfg)
    for k in ('email', 'smtp', 'notifiche_email'):
        if isinstance(cfg.get(k), dict):
            cfg[k]['abilitato'] = False
    # Lantek: niente programmi (gli stub del server li sostituiscono comunque)
    lt = cfg.setdefault('lantek_db', {}) if isinstance(cfg.get('lantek_db', {}), dict) else {}
    lt['xmlimporter'] = os.path.join(dati_dir, 'NON_ESISTE_XmlImporter.exe')
    lt['procesos'] = os.path.join(dati_dir, 'NON_ESISTE_Procesos.exe')
    dst = os.path.join(dati_dir, 'app_config.json')
    with open(dst, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    reg.log('configurazione copiata, svuotati:', ['disegni_export_root'] + svuotati)
    return dst


def crea_admin(db, cfg, reg):
    """L'amministratore di prova, sulla COPIA, con tools/persone.py."""
    env = dict(os.environ, FERROTRACK_DB=db, FERROTRACK_CONFIG=cfg, PYTHONIOENCODING='utf-8')
    tmp = os.path.join(os.path.dirname(os.path.dirname(db)), 'tmp')
    os.makedirs(tmp, exist_ok=True)
    env['TEMP'] = env['TMP'] = tmp
    r = subprocess.run([PYTHON, os.path.join(APP, 'tools', 'persone.py'), 'crea', 'Amministratore Stress',
                        '--ruolo', 'Amministrazione', '--admin', '--pin', PIN_ADMIN],
                       capture_output=True, text=True, errors='replace', env=env, cwd=APP,
                       creationflags=CREATE_NO_WINDOW | BELOW_NORMAL, timeout=180)
    reg.log('persone.py crea admin:', r.returncode, (r.stdout + r.stderr)[-300:].replace('\n', ' '))
    if r.returncode != 0 and 'gia' not in (r.stdout + r.stderr).lower():
        raise RuntimeError('admin di prova non creato: %s' % (r.stdout + r.stderr)[-500:])


def prepara(run_dir, reg, copia_upl=True):
    dati = os.path.join(run_dir, 'dati')
    if copia_upl:
        copia_uploads(reg)
    db = copia_database(dati, reg)
    cfg = copia_configurazione(dati, reg)
    crea_admin(db, cfg, reg)
    with open(os.path.join(run_dir, 'lantek_modo.json'), 'w', encoding='utf-8') as f:
        json.dump({'modo': 'normale'}, f)
    return db, cfg
