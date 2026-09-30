"""
backup_db.py — Backup del database SQLite

Funzionalita':
  - Backup a caldo con SQLite Online Backup API (sicuro anche con server attivo
    e col database in WAL)
  - Database danneggiato: niente backup e niente rotazione (una copia rotta non
    deve scalzare le copie buone), avviso nel log e all'amministrazione
  - Ogni copia appena scritta viene verificata; se non e' integra si cancella
  - Rotazione PER TIPO e PER DATA: prima si ordinava per nome
    (scheduler_{motivo}_{ts}) e le copie "manuale" e "pre_migration"
    finivano cancellate per prime anche se erano le piu' recenti
      * schedulato: le ultime `max_backups` + una al giorno per `giorni_storico`
      * pre_migration: le ultime 20
      * manuale e prima_*: in backups/manuali, mai cancellate in automatico
  - Backup remoto opzionale (NAS/secondo PC), solo su una cartella esistente
  - Un solo scheduler (lo avvia run.py)
  - Eseguibile manualmente: python backup_db.py
"""

import sqlite3
import shutil
import os
import glob
import json
import re
import time
import threading
import logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

# Percorsi default
_HERE = Path(__file__).parent
DB_PATH = _HERE / 'database' / 'scheduler.db'
DEFAULT_BACKUP_DIR = _HERE / 'database' / 'backups'
CONFIG_PATH = _HERE / 'backup_config.json'

# Config defaults
DEFAULT_CONFIG = {
    'backup_enabled': True,
    'interval_hours': 1,
    'max_backups': 48,          # copie "schedulato" recenti tenute tutte
    'giorni_storico': 30,       # poi una al giorno per questi giorni
    'backup_path': '',
    'remote_path': ''
}
MAX_PRE_MIGRATION = 20
# Copie fatte a mano (dalla pagina o prima di un aggiornamento/ripristino):
# stanno in manuali/ e la rotazione non le tocca.
_MOTIVI_MANUALI = ('manuale',)
_RX_NOME = re.compile(r'^scheduler_(?P<motivo>.+)_(?P<ts>\d{8}_\d{6})\.db$')


def load_config() -> dict:
    """Carica configurazione da backup_config.json."""
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, 'r') as f:
                config = json.load(f)
            merged = {**DEFAULT_CONFIG, **config}
            return merged
    except Exception as e:
        logger.warning(f'[BACKUP] Errore lettura config: {e}')
    return dict(DEFAULT_CONFIG)


def save_config(config: dict):
    """Salva configurazione su backup_config.json."""
    try:
        with open(CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        logger.info('[BACKUP] Config salvata')
    except Exception as e:
        logger.error(f'[BACKUP] Errore salvataggio config: {e}')


def cartella_valida(percorso: str) -> str | None:
    """Motivo per cui `percorso` NON va bene come cartella di backup (None = ok).
    Deve esistere ed essere fuori dalla cartella del programma: prima si
    accettava qualunque stringa, e la rotazione avrebbe cancellato file
    scheduler_*.db dove capitava."""
    p = str(percorso or '').strip()
    if not p:
        return None
    try:
        reale = os.path.realpath(p)
    except (OSError, ValueError):
        return 'percorso non valido'
    if not os.path.isdir(reale):
        return 'la cartella non esiste'
    prog = os.path.realpath(str(_HERE))
    dflt = os.path.realpath(str(DEFAULT_BACKUP_DIR))
    if reale != dflt and (reale == prog or reale.startswith(prog + os.sep)):
        return 'non puo\' stare dentro la cartella del programma'
    return None


def _get_backup_dir() -> Path:
    """Ritorna la cartella backup (da config o default)."""
    config = load_config()
    custom_path = (config.get('backup_path') or '').strip()
    if custom_path and cartella_valida(custom_path) is None:
        return Path(custom_path)
    if custom_path:
        logger.warning(f'[BACKUP] backup_path non valido ({cartella_valida(custom_path)}): uso {DEFAULT_BACKUP_DIR}')
    return DEFAULT_BACKUP_DIR


def cartella_manuali() -> Path:
    return _get_backup_dir() / 'manuali'


def _integro(percorso: Path) -> bool:
    try:
        conn = sqlite3.connect(f'file:{percorso}?mode=ro', uri=True)
        try:
            r = conn.execute('PRAGMA integrity_check').fetchone()
        finally:
            conn.close()
        return bool(r) and r[0] == 'ok'
    except Exception as e:
        logger.error(f'[BACKUP] integrity_check {percorso.name}: {e}')
        return False


def _avvisa(titolo: str, messaggio: str):
    """Notifica all'amministrazione (best effort: il log c'e' comunque)."""
    try:
        from backend.database import NotificationManager
        NotificationManager.create_notification(
            'postazione-amministrazione', None, titolo, messaggio,
            notification_type='alert', notification_category='urgente')
    except Exception as e:
        logger.warning(f'[BACKUP] notifica non inviata: {e}')


def backup(motivo: str = 'schedulato', controlla: bool = True) -> str | None:
    """
    Backup a caldo del database con l'SQLite Online Backup API (sicuro anche
    col server attivo e in WAL).

    controlla=True: se il database non passa l'integrity_check niente copia e
    niente rotazione. controlla=False per le copie "prima di" (migrazione,
    ripristino): meglio una copia di un database rovinato che nessuna.

    Returns:
        Path del file di backup creato, oppure None in caso di errore.
    """
    if not DB_PATH.exists():
        logger.error(f'[BACKUP] Database non trovato: {DB_PATH}')
        return None
    if controlla and not integrity_check():
        logger.error('[BACKUP] Database NON integro: backup e rotazione saltati per non '
                     'sostituire le copie buone')
        _avvisa('Database da controllare',
                'Il controllo di integrita\' del database e\' fallito: il backup automatico e\' sospeso '
                'per non sovrascrivere le copie buone. Vedi RIPRISTINO.md.')
        return None

    manuale = motivo in _MOTIVI_MANUALI or motivo.startswith('prima_')
    backup_dir = cartella_manuali() if manuale else _get_backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    dst = backup_dir / f'scheduler_{motivo}_{ts}.db'

    try:
        src_conn = sqlite3.connect(str(DB_PATH))
        dst_conn = sqlite3.connect(str(dst))
        src_conn.backup(dst_conn, pages=100)
        dst_conn.close()
        src_conn.close()
    except Exception as e:
        logger.error(f'[BACKUP] ERRORE: {e}')
        if dst.exists():
            dst.unlink()
        return None

    # la copia appena scritta deve essere leggibile e integra
    if not _integro(dst):
        logger.error(f'[BACKUP] Copia {dst.name} non integra: cancellata')
        try:
            dst.unlink()
        except OSError:
            pass
        _avvisa('Backup non riuscito', f'La copia {dst.name} non era integra ed e\' stata scartata.')
        return None

    size_kb = dst.stat().st_size // 1024
    logger.info(f'[BACKUP] OK: {dst.name} ({size_kb} KB)')

    config = load_config()
    remote_path = (config.get('remote_path') or '').strip()
    if remote_path:
        _copia_remota(dst, remote_path)

    if not manuale:
        _ruota_backup(backup_dir, int(config.get('max_backups') or DEFAULT_CONFIG['max_backups']),
                      int(config.get('giorni_storico') or DEFAULT_CONFIG['giorni_storico']))
    return str(dst)


def _copia_remota(src: Path, remote_path: str):
    """Copia il backup su percorso remoto (NAS, UNC share, cartella di rete)."""
    motivo = cartella_valida(remote_path)
    if motivo:
        logger.warning(f'[BACKUP] copia remota saltata: {remote_path} {motivo}')
        return
    try:
        dst_remote = Path(remote_path) / src.name
        shutil.copy2(src, dst_remote)
        logger.info(f'[BACKUP] Remoto OK: {dst_remote}')
    except Exception as e:
        logger.warning(f'[BACKUP] WARN copia remota fallita: {e}')


def _info(percorso: str):
    m = _RX_NOME.match(os.path.basename(percorso))
    if not m:
        return None
    try:
        mtime = os.path.getmtime(percorso)
    except OSError:
        return None
    return m.group('motivo'), mtime


def da_eliminare(files: list, max_backups: int, giorni_storico: int, adesso: float | None = None) -> list:
    """Quali copie togliere (pura, per i test).
    schedulato: le `max_backups` piu' recenti + la piu' recente di ogni giorno
    negli ultimi `giorni_storico` giorni. pre_migration: le ultime 20. Altri
    tipi sconosciuti: non si toccano."""
    adesso = adesso or time.time()
    per_tipo = {}
    for f in files:
        inf = _info(f)
        if inf:
            per_tipo.setdefault(inf[0], []).append((inf[1], f))
    via = []
    sched = sorted(per_tipo.get('schedulato', []), reverse=True)
    giorni_visti = set()
    limite = adesso - giorni_storico * 86400
    for k, (mt, f) in enumerate(sched):
        if k < max_backups:
            giorni_visti.add(time.strftime('%Y%m%d', time.localtime(mt)))
            continue
        giorno = time.strftime('%Y%m%d', time.localtime(mt))
        if mt >= limite and giorno not in giorni_visti:
            giorni_visti.add(giorno)
            continue
        via.append(f)
    pre = sorted(per_tipo.get('pre_migration', []), reverse=True)
    via += [f for _, f in pre[MAX_PRE_MIGRATION:]]
    return via


def _ruota_backup(backup_dir: Path, max_backups: int, giorni_storico: int = 30):
    """Rotazione per tipo e per data (vedi da_eliminare)."""
    files = glob.glob(str(backup_dir / 'scheduler_*.db'))
    for old in da_eliminare(files, max_backups, giorni_storico):
        try:
            os.remove(old)
            logger.info(f'[BACKUP] Rimosso vecchio: {Path(old).name}')
        except Exception as e:
            logger.warning(f'[BACKUP] WARN rimozione fallita {old}: {e}')


def integrity_check() -> bool:
    """Verifica l'integrita' del database. Restituisce True se tutto OK."""
    if not DB_PATH.exists():
        return False
    try:
        conn = sqlite3.connect(str(DB_PATH))
        result = conn.execute('PRAGMA integrity_check').fetchone()
        conn.close()
        ok = result and result[0] == 'ok'
        if ok:
            logger.info('[BACKUP] Integrity check: OK')
        else:
            logger.warning(f'[BACKUP] ATTENZIONE integrity check: {result}')
        return ok
    except Exception as e:
        logger.error(f'[BACKUP] ERRORE integrity check: {e}')
        return False


def list_backups() -> list:
    """Backup esistenti (automatici e manuali), dal piu' recente."""
    backup_dir = _get_backup_dir()
    files = glob.glob(str(backup_dir / 'scheduler_*.db')) + glob.glob(str(backup_dir / 'manuali' / 'scheduler_*.db'))
    result = []
    for f in files:
        p = Path(f)
        try:
            stat = p.stat()
        except OSError:
            continue
        m = _RX_NOME.match(p.name)
        result.append({
            'filename': p.name,
            'tipo': m.group('motivo') if m else '',
            'manuale': p.parent.name == 'manuali',
            'size_kb': stat.st_size // 1024,
            'created': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
            'mtime': stat.st_mtime,
            'path': str(p)
        })
    result.sort(key=lambda r: r['mtime'], reverse=True)
    return result


# === SCHEDULER BACKGROUND ===
# Uno solo, avviato da run.py. Prima ce n'erano due (questo all'import di
# backend.app ogni 12 h e uno orario in run.py) che si contendevano le 30 copie.

_scheduler_thread = None
_scheduler_stop = threading.Event()


def start_scheduler(primo_dopo_s: int = 60):
    """Avvia il backup scheduler in background."""
    global _scheduler_thread
    if _scheduler_thread and _scheduler_thread.is_alive():
        return

    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(target=_scheduler_loop, args=(primo_dopo_s,), daemon=True,
                                         name='backup-scheduler')
    _scheduler_thread.start()
    logger.info('[BACKUP] Scheduler avviato')


def stop_scheduler():
    """Ferma il backup scheduler."""
    _scheduler_stop.set()
    logger.info('[BACKUP] Scheduler fermato')


def _scheduler_loop(primo_dopo_s: int = 60):
    """Primo backup poco dopo l'avvio, poi ogni `interval_hours`."""
    attesa = primo_dopo_s
    while not _scheduler_stop.is_set():
        if _scheduler_stop.wait(attesa):
            break
        config = load_config()
        attesa = max(1, int(config.get('interval_hours') or 1)) * 3600
        if not config.get('backup_enabled', True):
            attesa = 60
            continue
        logger.info('[BACKUP] Esecuzione backup schedulato...')
        backup(motivo='schedulato')
        logger.info(f'[BACKUP] Prossimo backup tra {attesa // 3600}h')

        # Pulizia notifiche lette > 30 giorni
        try:
            from backend.database import NotificationManager
            deleted = NotificationManager.cleanup_old_notifications(30)
            if deleted:
                logger.info(f'[BACKUP] Pulizia: {deleted} notifiche vecchie eliminate')
        except Exception as e:
            logger.debug(f'[BACKUP] Pulizia notifiche skip: {e}')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    print('=== BACKUP MANUALE ===')
    path = backup(motivo='manuale')
    if path:
        print(f'Backup salvato in: {path}')
    else:
        print('Backup fallito (vedi messaggi sopra).')
