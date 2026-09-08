# -*- coding: utf-8 -*-
"""Un'istanza di prova dell'applicazione, su una COPIA del database.

Serve per provare davvero le cose — cliccare i pulsanti, accettare preventivi,
dichiarare ore — senza il rischio di sporcare i dati della produzione. Il
database vero viene copiato in una cartella temporanea e l'applicazione lavora
sulla copia: qualunque cosa succeda, i dati veri non si toccano.

    python app/tools/server_prova.py [porta]      # default 5056

Alla chiusura la copia resta dov'e' (il percorso viene stampato all'avvio),
cosi' si puo' guardare cosa e' successo. Cancellala pure quando hai finito.
"""
import os
import shutil
import socket
import sys
import tempfile
from datetime import datetime

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

PORTA = int(sys.argv[1]) if len(sys.argv) > 1 else 5056
VERO = os.path.join(_APP, 'database', 'scheduler.db')


def _copia_database() -> str:
    """Copia il database vero in una cartella temporanea e ne torna il percorso."""
    if not os.path.isfile(VERO):
        raise SystemExit('Database non trovato: %s' % VERO)
    cartella = os.path.join(tempfile.gettempdir(), 'ferrotrack_prova')
    os.makedirs(cartella, exist_ok=True)
    copia = os.path.join(
        cartella, 'prova_%s.db' % datetime.now().strftime('%Y%m%d_%H%M%S'))
    shutil.copy2(VERO, copia)
    # SQLite puo' avere il giornale a fianco: senza, la copia risulta indietro.
    for coda in ('-wal', '-shm'):
        if os.path.isfile(VERO + coda):
            shutil.copy2(VERO + coda, copia + coda)
    return copia


def _copia_configurazione() -> str:
    """Copia le impostazioni, cosi' provare a salvarle non tocca la produzione.

    Senza questo, aprire Admin sull'istanza di prova e premere Salva cambiava
    le impostazioni vere: il database era copiato, la configurazione no.
    """
    vero = os.path.join(_APP, 'app_config.json')
    cartella = os.path.join(tempfile.gettempdir(), 'ferrotrack_prova')
    os.makedirs(cartella, exist_ok=True)
    copia = os.path.join(
        cartella, 'config_%s.json' % datetime.now().strftime('%Y%m%d_%H%M%S'))
    if os.path.isfile(vero):
        shutil.copy2(vero, copia)
    else:
        with open(copia, 'w', encoding='utf-8') as f:
            f.write('{}')
    return copia


def _porta_libera(porta: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(('127.0.0.1', porta)) != 0


def main():
    if not _porta_libera(PORTA):
        raise SystemExit(
            'La porta %d e\' gia\' occupata: o il server di prova gira gia\', '
            'oppure scegli un\'altra porta.' % PORTA)

    copia = _copia_database()
    copia_cfg = _copia_configurazione()
    # Vanno impostate PRIMA di importare l'applicazione: i percorsi vengono
    # letti al momento dell'import.
    os.environ['FERROTRACK_DB'] = copia
    os.environ['FERROTRACK_CONFIG'] = copia_cfg

    from backend.app import app
    from backend.models import DATABASE_PATH

    if os.path.abspath(DATABASE_PATH) != os.path.abspath(copia):
        raise SystemExit(
            'ATTENZIONE: l\'applicazione sta usando %s invece della copia. '
            'Interrompo per non toccare i dati veri.' % DATABASE_PATH)

    print('=' * 62)
    print('  ISTANZA DI PROVA  --  i dati veri non vengono toccati')
    print('=' * 62)
    print('  copia del database : %s' % copia)
    print('  copia impostazioni : %s' % copia_cfg)
    print('  indirizzo          : http://127.0.0.1:%d' % PORTA)
    print('=' * 62)
    app.run(host='127.0.0.1', port=PORTA, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()
