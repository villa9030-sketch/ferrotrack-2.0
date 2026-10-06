#!/usr/bin/env python
"""
Avvio di FerroTrack: l'UNICO modo di far partire il server.

- server di produzione waitress su 0.0.0.0:5000 (niente debugger esposto in
  rete); con FLASK_DEBUG=true il server di sviluppo di Flask, ma solo su
  127.0.0.1
- log su file a rotazione in logs/ferrotrack.log (+ console quando c'e')
- thread: backup, export JSON, vigilanza
- se il server e' gia' acceso si ferma subito (una sola istanza)

Si avvia con START_FERROTRACK.bat (senza finestra) o
START_FERROTRACK_CONSOLE.bat (con la finestra, per vedere i messaggi).
"""

import sys
import os
import socket
import threading
import time
import logging
from logging.handlers import RotatingFileHandler

PORTA = int(os.environ.get('FERROTRACK_PORTA', 5000))
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(_LOG_DIR, exist_ok=True)
_fmt = logging.Formatter('[%(asctime)s] %(levelname)s %(name)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
_handlers = [RotatingFileHandler(os.path.join(_LOG_DIR, 'ferrotrack.log'), maxBytes=10 * 1024 * 1024,
                                 backupCount=5, encoding='utf-8')]
if sys.stderr is not None:                      # pythonw: niente console
    _handlers.append(logging.StreamHandler())
for _h in _handlers:
    _h.setFormatter(_fmt)
logging.basicConfig(level=logging.INFO, handlers=_handlers, force=True)
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

# backend.app si importa in main(), DOPO il controllo della porta: l'import
# inizializza il database (e fa la copia pre-migrazione) anche per un secondo
# avvio che poi verrebbe rifiutato.

# Intervallo export JSON in secondi (default: 24 ore)
EXPORT_INTERVALLO = int(os.environ.get('EXPORT_INTERVALLO_SECONDI', 86400))


def _loop_export_json():
    """Thread daemon: esegue export JSON giornaliero di tutti gli ordini."""
    time.sleep(120)  # Attende 2 minuti dopo l'avvio
    while True:
        try:
            _esegui_export_json()
        except Exception as e:
            logger.error(f'Errore nel thread export JSON: {e}')
        time.sleep(EXPORT_INTERVALLO)


def _loop_vigilanza():
    """Thread daemon di VIGILANZA: ogni 30 min converte in notifiche PUSH i rischi che
    prima erano solo 'pull' (visibili solo aprendo la dashboard), così un collo di
    bottiglia non passa inosservato se nessuno guarda. Controlla:
      - consegna imminente/scaduta con ordine non pronto → capi
      - ordini 'sospetti finiti' (aperti da troppo, mai chiusi) → capi + Impiegata
      - dichiarazioni ORE mancanti o incomplete -> Impiegata
    Ogni alert è dedup (una notifica per ordine) e solo in orario lavorativo."""
    from backend.database import OrderManager, ConfigManager
    time.sleep(180)  # attende 3 minuti dopo l'avvio
    while True:
        try:
            cfg = ConfigManager.load_config()
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


def _porta_occupata(porta: int) -> bool:
    try:
        with socket.create_connection(('127.0.0.1', porta), timeout=1):
            return True
    except OSError:
        return False


def main():
    if _porta_occupata(PORTA):
        logger.error(f'La porta {PORTA} risulta gia in uso: FerroTrack e gia acceso? Non parto una seconda volta.')
        return 1

    from backend.app import app
    from backend.models import initialize_database
    # Inizializza database
    initialize_database()

    # Backup: un solo scheduler (backup_db), intervallo da backup_config.json.
    # Primo backup un minuto dopo l'avvio; database non integro = niente copia.
    from backup_db import start_scheduler as _start_backup, load_config as _bcfg
    _start_backup(primo_dopo_s=60)
    logger.info(f"Backup schedulato ogni {_bcfg().get('interval_hours')} ore")

    # Avvia thread export JSON giornaliero
    t_export = threading.Thread(target=_loop_export_json, daemon=True, name='export-scheduler')
    t_export.start()
    logger.info(f'Thread export JSON schedulato ogni {EXPORT_INTERVALLO//3600} ore')

    # Avvia thread di vigilanza (consegne a rischio + sospetti finiti + ore mancanti)
    t_alert = threading.Thread(target=_loop_vigilanza, daemon=True, name='vigilanza')
    t_alert.start()
    logger.info('Thread vigilanza (consegne / sospetti finiti / ore mancanti) attivo')

    debug_mode = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    if debug_mode:
        # Solo sviluppo: debugger di Flask raggiungibile SOLO da questo PC
        logger.warning(f'DEBUG attivo: server di sviluppo su http://127.0.0.1:{PORTA} (non in rete)')
        app.run(host='127.0.0.1', port=PORTA, debug=True, use_reloader=False, threaded=True)
        return 0
    from waitress import serve
    logger.info(f'Avvio FerroTrack (waitress) su porta {PORTA}: http://localhost:{PORTA}')
    # 16 thread: anteprime SVG, autosave, stime e tablet in parallelo
    serve(app, host='0.0.0.0', port=PORTA, threads=16, channel_timeout=300,
          max_request_body_size=60 * 1024 * 1024, ident='FerroTrack')
    return 0


if __name__ == '__main__':
    sys.exit(main())
