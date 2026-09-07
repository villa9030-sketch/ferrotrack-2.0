#!/usr/bin/env python
"""
Launcher per SCHEDULATORE LASER backend
Avvia il server Flask sulla porta 5000
"""

import sys
import os
import threading
import time
import logging

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('schedulatore')

# Verifica versione Python — richiesto 3.10+ per sintassi Union types (dict | None)
if sys.version_info < (3, 10):
    print(f"[ERRORE] Python {sys.version_info.major}.{sys.version_info.minor} non supportato.")
    print("[ERRORE] Richiesto Python 3.10 o superiore.")
    print("[INFO]   Su macOS usa: /opt/homebrew/bin/python3.12 run.py")
    print("[INFO]   Su Windows installa Python 3.10+ da python.org")
    sys.exit(1)

# Aggiungi la cartella app al path
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).parent / ".env")  # Carica app/.env (contiene GEMINI_API_KEY)

# Importa app dal backend
from backend.app import app
from backend.models import initialize_database

# Intervallo backup in secondi (default: 1 ora, configurabile via env)
BACKUP_INTERVALLO = int(os.environ.get('BACKUP_INTERVALLO_SECONDI', 3600))
# Intervallo export JSON in secondi (default: 24 ore)
EXPORT_INTERVALLO = int(os.environ.get('EXPORT_INTERVALLO_SECONDI', 86400))
# Orario fine turno (HH:MM) — chiusura automatica scan officina rimaste aperte
# Default 17:30 = orario di fine turno aziendale. Configurabile via env var.
FINE_TURNO_HHMM = os.environ.get('FINE_TURNO_HHMM', '17:30')


def _loop_backup():
    """Thread daemon: esegue backup orario del database."""
    from backup_db import backup, integrity_check
    time.sleep(60)  # Attende 1 minuto dopo l'avvio prima del primo backup
    while True:
        try:
            integrity_check()
            backup(motivo='schedulato')
        except Exception as e:
            logger.error(f'Errore nel thread backup schedulato: {e}')
        time.sleep(BACKUP_INTERVALLO)


def _loop_export_json():
    """Thread daemon: esegue export JSON giornaliero di tutti gli ordini."""
    time.sleep(120)  # Attende 2 minuti dopo l'avvio
    while True:
        try:
            _esegui_export_json()
        except Exception as e:
            logger.error(f'Errore nel thread export JSON: {e}')
        time.sleep(EXPORT_INTERVALLO)


def _loop_fine_turno():
    """Thread daemon: ogni minuto controlla se è l'ora di fine turno.
    Orario letto dinamicamente da app_config.json (chiave `fine_turno_hhmm`, default 17:30),
    con fallback su env var FINE_TURNO_HHMM. La modifica via admin diventa effettiva al
    prossimo controllo (entro 30s) senza riavviare il server.
    """
    import datetime as _dt
    from backend.database import BarcodeManager

    def _get_target_hhmm():
        try:
            cfg = BarcodeManager.load_config()
            v = (cfg.get('fine_turno_hhmm') or FINE_TURNO_HHMM).strip()
            hh, mm = (int(x) for x in v.split(':'))
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                return hh, mm
        except Exception:
            pass
        return 17, 30

    logger.info('Cron fine turno attivo (orario letto da app_config.json)')
    last_run_date = None
    while True:
        try:
            hh, mm = _get_target_hhmm()
            now = _dt.datetime.now()
            if now.hour == hh and now.minute == mm and last_run_date != now.date():
                n = BarcodeManager.close_residual_scans(motivo='fine_turno')
                logger.info(f'Cron fine turno {hh:02d}:{mm:02d}: chiuse {n} scan rimaste aperte')
                last_run_date = now.date()
        except Exception as e:
            logger.error(f'Errore nel thread fine turno: {e}')
        time.sleep(30)


def _loop_vigilanza():
    """Thread daemon di VIGILANZA: ogni 30 min converte in notifiche PUSH i rischi che
    prima erano solo 'pull' (visibili solo aprendo la dashboard), così un collo di
    bottiglia non passa inosservato se nessuno guarda. Controlla:
      - taglio non confermato oltre soglia (alert_taglio_ore, default 4h) → capi
      - consegna imminente/scaduta con ordine non pronto → capi
      - ordini 'sospetti finiti' (lavorati ma mai chiusi) → capi + Impiegata
      - dichiarazioni ORE mancanti o incomplete -> Impiegata
    Ogni alert è dedup (una notifica per ordine) e solo in orario lavorativo."""
    from backend.database import OrderManager, BarcodeManager
    time.sleep(180)  # attende 3 minuti dopo l'avvio
    while True:
        try:
            cfg = BarcodeManager.load_config()
            try:
                soglia = float(cfg.get('alert_taglio_ore', 4) or 4)
            except (TypeError, ValueError):
                soglia = 4.0
            OrderManager.alert_ordini_taglio_fermo(soglia_ore=soglia)
            OrderManager.alert_consegne_a_rischio(giorni=int(cfg.get('alert_consegna_giorni', 1) or 1))
            OrderManager.alert_sospetti_finiti_push()
            # Controllo mancanze ORE: rileva le anomalie (recuperando gli
            # arretrati dopo uno spegnimento) e notifica UNA SOLA VOLTA ciascuna.
            try:
                from backend.anomalie_service import controlla_e_notifica
                controlla_e_notifica()
            except Exception as e:
                logger.error(f'Errore nel controllo mancanze ore: {e}')
        except Exception as e:
            logger.error(f'Errore nel thread vigilanza: {e}')
        time.sleep(1800)  # 30 minuti


def _esegui_export_json():
    """Esporta tutti gli ordini in un file JSON nella cartella database/exports."""
    import json
    from backend.database import OrderManager
    from pathlib import Path

    export_dir = Path(__file__).parent / 'database' / 'exports'
    export_dir.mkdir(parents=True, exist_ok=True)

    ts = time.strftime("%Y%m%d_%H%M%S")
    export_path = export_dir / f'ordini_{ts}.json'

    ordini = OrderManager.get_all_orders_dict()
    with open(export_path, 'w', encoding='utf-8') as f:
        json.dump(ordini, f, ensure_ascii=False, indent=2, default=str)

    size_kb = export_path.stat().st_size // 1024
    logger.info(f'JSON salvato: {export_path.name} ({size_kb} KB, {len(ordini)} ordini)')

    # Mantieni solo gli ultimi 30 export
    import glob
    files = sorted(glob.glob(str(export_dir / 'ordini_*.json')))
    for old in files[:-30]:
        os.remove(old)


if __name__ == '__main__':
    # Inizializza database
    initialize_database()

    # Avvia thread backup orario (daemon: si chiude con il processo principale)
    t_backup = threading.Thread(target=_loop_backup, daemon=True, name='backup-scheduler')
    t_backup.start()
    logger.info(f'Thread backup schedulato ogni {BACKUP_INTERVALLO//60} minuti')

    # Avvia thread export JSON giornaliero
    t_export = threading.Thread(target=_loop_export_json, daemon=True, name='export-scheduler')
    t_export.start()
    logger.info(f'Thread export JSON schedulato ogni {EXPORT_INTERVALLO//3600} ore')

    # Avvia thread chiusura scan a fine turno
    t_eot = threading.Thread(target=_loop_fine_turno, daemon=True, name='eot-scheduler')
    t_eot.start()

    # Avvia thread di vigilanza (taglio fermo + consegne a rischio + sospetti finiti)
    t_alert = threading.Thread(target=_loop_vigilanza, daemon=True, name='vigilanza')
    t_alert.start()
    logger.info('Thread vigilanza (taglio / consegne / sospetti finiti / ore mancanti) attivo')

    # Beta: debug=False per stabilità
    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'

    # Controlla se esistono certificati SSL per HTTPS (necessario per PWA su tablet)
    cert_file = os.path.join(os.path.dirname(__file__), 'certs', 'cert.pem')
    key_file = os.path.join(os.path.dirname(__file__), 'certs', 'key.pem')
    has_ssl = os.path.exists(cert_file) and os.path.exists(key_file)

    # Avvia Flask HTTP
    logger.info("Avvio SCHEDULATORE LASER su porta 5000")
    logger.info("Accedi via browser: http://localhost:5000")
    logger.info(f"Debug mode: {'ON' if debug_mode else 'OFF'}")
    # threaded=True: consente al server dev di gestire piu' richieste concorrenti
    # (es. thumbnail SVG multipli, autosave in background, stima costo mentre
    # l'utente naviga). Senza questo, ogni richiesta accoda quelle successive.
    app.run(host='0.0.0.0', port=5000, debug=debug_mode, threaded=True)
