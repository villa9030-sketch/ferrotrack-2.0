"""Registro degli esempi per il motore di riconoscimento.

Ogni volta che una persona conferma o corregge in FerroTrack quello che il
motore ha letto da un disegno, qui si scrive una riga: il disegno (percorso e
impronta del file), cosa aveva letto il motore, cosa ha deciso la persona.
Sono gli esempi con cui il motore si rimisura e si riaddestra (banco_motore).

Resta tutto su questo PC: dati_motore/esempi.jsonl accanto all'app, una riga
JSON per evento, solo aggiunte. Non deve MAI rompere il salvataggio vero:
ogni errore qui e' ignorato (con un avviso nel log).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

_APP = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CARTELLA = os.environ.get('FERROTRACK_ESEMPI_DIR') or os.path.join(_APP, 'dati_motore')
FILE = 'esempi.jsonl'
_LOCK = threading.Lock()


def _impronta(percorso: str | None) -> str | None:
    try:
        h = hashlib.sha256()
        with open(percorso, 'rb') as f:
            for blocco in iter(lambda: f.read(1 << 20), b''):
                h.update(blocco)
        return h.hexdigest()
    except Exception:
        return None


def registra(evento: str, percorso: str | None, *, codice=None, motore: dict | None = None,
             decisione: dict | None = None, chi=None, contesto: dict | None = None) -> bool:
    """Aggiunge un esempio. evento: es. 'contorno_scelto_a_mano', 'contorno_confermato'.
    motore/decisione: dict piccoli con misure (area_dm2, perimetro_taglio_m,
    n_forature, bbox, contorno...). Ritorna True se scritto."""
    try:
        riga = {
            'quando': datetime.now().isoformat(timespec='seconds'),
            'evento': evento,
            'codice': codice,
            'file': percorso,
            'sha256': _impronta(percorso) if percorso else None,
            'motore': motore or {},
            'decisione': decisione or {},
            'chi': chi,
            'contesto': contesto or {},
        }
        os.makedirs(CARTELLA, exist_ok=True)
        with _LOCK, open(os.path.join(CARTELLA, FILE), 'a', encoding='utf-8') as f:
            f.write(json.dumps(riga, ensure_ascii=False, default=str) + '\n')
        return True
    except Exception as e:      # noqa: BLE001 - il registro non deve mai bloccare il lavoro
        logger.warning('registro esempi non scritto (%s): %s', evento, e)
        return False
