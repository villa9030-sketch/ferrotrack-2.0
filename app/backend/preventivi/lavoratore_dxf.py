"""Lavoratore DXF: legge i disegni in un PROCESSO separato dal server.

Lo avvia analisi_isolata.py (mai a mano). Riceve una riga JSON per disegno
su stdin, risponde una riga JSON su stdout, finche' stdin non si chiude.

Perche' un processo a parte: un disegno enorme o patologico (20.000 fori)
teneva il motore occupato per minuti dentro il server, col GIL di Python
preso, e rallentava tutte le altre richieste; un thread non si puo'
fermare. Un processo si', e se si blocca o cade (shapely puo' andare in
crash su geometrie strane) il server resta su.

Il calcolo e' lo STESSO di prima: dxf_batch_worker.process_single_dxf, con
la stessa configurazione e la stessa cache dei disegni.
"""
import json
import os
import sys
import types


def _prepara(app_dir: str):
    """Importa solo il motore dei disegni, non tutto il server.

    backend/__init__.py importa l'applicazione Flask (database, rotte...):
    qui non serve e rallenterebbe l'avvio. Il pacchetto 'backend' si registra
    vuoto, puntato alla sua cartella: i sottomoduli si importano come sempre.
    """
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    pkg = types.ModuleType('backend')
    pkg.__path__ = [os.path.join(app_dir, 'backend')]
    sys.modules['backend'] = pkg


def main() -> int:
    app_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    # Il canale delle risposte e' una COPIA di stdout; lo stdout "normale" va
    # su stderr, cosi' un print di una libreria non rompe le righe JSON.
    uscita = os.fdopen(os.dup(1), 'w', encoding='utf-8', newline='\n')
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    entrata = os.fdopen(os.dup(0), 'r', encoding='utf-8')

    _prepara(app_dir)
    from backend.preventivi import dxf_cache
    from backend.preventivi.dxf_batch_worker import process_single_dxf

    for riga in entrata:
        riga = riga.strip()
        if not riga:
            continue
        try:
            compito = json.loads(riga)
        except ValueError:
            continue
        risposta = {'id': compito.get('id')}
        try:
            cache = compito.get('cache')
            if cache and cache != dxf_cache._CACHE_DB_PATH:
                dxf_cache._CACHE_DB_PATH = cache
                dxf_cache._INITIALIZED = False
            risposta['payload'] = process_single_dxf(
                compito['path'], compito['filename'], compito.get('cfg') or {})
            risposta['ok'] = True
        except Exception as e:  # noqa: BLE001 - un disegno non fa cadere il lavoratore
            risposta['ok'] = False
            risposta['error'] = f'{type(e).__name__}: {e}'
        uscita.write(json.dumps(risposta, default=str) + '\n')
        uscita.flush()
    return 0


if __name__ == '__main__':
    sys.exit(main())
