"""scan_hub.py — bridge tra pistole barcode e FerroTrack.

DISMESSO (settembre 2026). Le pistole barcode non sono piu' in uso: gli operai
dichiarano le ore dal tablet della timbratrice. Il server risponde 410 a ogni
scansione, quindi questo servizio non ha piu' nulla da fare e va FERMATO sul PC
di Elena:

    nssm stop FerroTrackScanHub
    nssm remove FerroTrackScanHub confirm

Il file resta per poter tornare indietro: riattivando `pistole_attive` in
Admin -> Soglie sistema -> Rilevazione delle ore, e riavviando questo servizio,
tutto torna a funzionare come prima.

Gira sul PC di Elena (sempre acceso, in officina/soppalco con vista vetri).
I dongle USB 2.4GHz delle 5 pistole sono collegati al PC. Quando un operaio
scansiona, la pistola "digita" via HID nel sistema operativo. Questo script:

  1. intercetta gli eventi tastiera globali (libreria `keyboard`),
  2. accumula i caratteri SOLO se l'input parte con uno dei prefissi
     mappati a pistole conosciute (es. "M:", "E:") — non interferisce
     col lavoro di Elena su mail/excel/impiegata.html,
  3. al ricevimento del terminatore (Invio = scan completa), parsa
     "PREFISSO:CODICE" e fa POST a /api/scan con il pistola_id mappato.

Avvio manuale:
    pip install -r requirements.txt
    python scan_hub.py

Avvio come servizio Windows (consigliato in produzione):
    nssm install FerroTrackScanHub "C:\\path\\to\\python.exe" "C:\\path\\to\\scan_hub.py"
    nssm start FerroTrackScanHub
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Dict, Optional

import keyboard
import requests

BASE_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = BASE_DIR / 'scan_hub_config.json'
LOG_PATH = BASE_DIR / 'scan_hub.log'

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_PATH, encoding='utf-8'),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger('scan_hub')


DEFAULT_CONFIG = {
    'server_url': 'http://localhost:5000',
    'scan_endpoint': '/api/scan',
    'prefix_map': {
        # Esempio: "M:": "pistola-mirko",
    },
    'timeout_secs': 3,
    'max_codice_chars': 64,
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        logger.warning('Config non trovata (%s). Uso defaults — nessun prefisso mappato.', CONFIG_PATH)
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
        return {**DEFAULT_CONFIG, **cfg}
    except Exception as e:
        logger.error('Config non leggibile (%s): %s. Uso defaults.', CONFIG_PATH, e)
        return dict(DEFAULT_CONFIG)


class ScanHub:
    """Intercetta gli scan barcode e li inoltra al server FerroTrack."""

    # Caratteri "stampabili" che possono comparire dentro un barcode (Code128).
    # Modificatori, frecce, F-keys ecc vengono ignorati.
    _SPECIAL_TO_CHAR = {
        'space': ' ',
        'colon': ':',
        'minus': '-',
        'period': '.',
        'comma': ',',
        'slash': '/',
        'backslash': '\\',
        'semicolon': ';',
        'equal': '=',
    }

    def __init__(self, config: dict):
        self.server_url = config['server_url'].rstrip('/')
        self.endpoint = config['scan_endpoint']
        self.timeout = config['timeout_secs']
        self.max_chars = config['max_codice_chars']
        self.prefix_map: Dict[str, str] = dict(config.get('prefix_map') or {})
        # Prefissi ordinati per lunghezza decrescente: "AB:" matcha prima di "A:".
        self.sorted_prefixes = sorted(self.prefix_map.keys(), key=len, reverse=True)

        # Il server ha risposto che le pistole sono dismesse: si smette di
        # provare invece di riempire il log a ogni scansione.
        self.dismesso = False

        # Stato di parsing
        self._buffer = ''
        self._collecting = False
        self._current_prefix: Optional[str] = None

    # ---- Conversione evento keyboard → carattere ----------------------------

    def _key_to_char(self, event) -> str:
        name = event.name or ''
        if len(name) == 1:
            return name
        return self._SPECIAL_TO_CHAR.get(name, '')

    # ---- Match prefisso ------------------------------------------------------

    def _prefix_match_state(self, candidate: str):
        """Ritorna:
          - prefisso completo (str) se candidate == uno dei prefissi
          - 'partial' se candidate è inizio di qualche prefisso
          - None altrimenti
        """
        for p in self.sorted_prefixes:
            if candidate == p:
                return p
        for p in self.sorted_prefixes:
            if p.startswith(candidate):
                return 'partial'
        return None

    # ---- Callback principale -------------------------------------------------

    def on_key(self, event):
        # Reagisce solo al keydown
        if event.event_type != 'down':
            return

        name = event.name or ''

        # Enter chiude la scansione corrente (se collecting)
        if name == 'enter':
            if self._collecting and self._buffer:
                codice = self._buffer[: self.max_chars]
                self._submit(self._current_prefix, codice)
            self._reset()
            return

        if self._collecting:
            ch = self._key_to_char(event)
            if ch:
                self._buffer += ch
                if len(self._buffer) > self.max_chars:
                    logger.warning('Buffer scan troppo lungo, scartato')
                    self._reset()
            return

        # Non in modalità collecting: candidato a inizio prefisso
        ch = self._key_to_char(event)
        if not ch:
            return  # tasto speciale (modifier, freccia, ecc.) → ignora
        candidate = self._buffer + ch
        state = self._prefix_match_state(candidate)
        if state is None:
            self._reset()
        elif state == 'partial':
            self._buffer = candidate
        else:
            # prefisso completo trovato
            self._current_prefix = state
            self._collecting = True
            self._buffer = ''
            logger.debug('Inizio scan con prefisso "%s"', state)

    def _reset(self):
        self._buffer = ''
        self._collecting = False
        self._current_prefix = None

    # ---- Submit al server ----------------------------------------------------

    def _submit(self, prefix: Optional[str], codice: str):
        codice = (codice or '').strip()
        if not codice or not prefix:
            return
        pistola_id = self.prefix_map.get(prefix)
        if not pistola_id:
            logger.warning('Prefisso "%s" non mappato — scan ignorata', prefix)
            return
        if self.dismesso:
            return
        url = f'{self.server_url}{self.endpoint}'
        payload = {'pistola_id': pistola_id, 'codice': codice}
        try:
            r = requests.post(url, json=payload, timeout=self.timeout)
            if r.ok:
                logger.info('OK pistola=%s codice=%s', pistola_id, codice)
            elif r.status_code == 410:
                # Le pistole sono state dismesse lato server: continuare a
                # riprovare riempirebbe il log senza costrutto.
                logger.error(
                    'Rilevazione con pistola DISATTIVATA sul server: questo servizio '
                    'non serve più. Fermalo (nssm stop FerroTrackScanHub). '
                    'Le ore si registrano dal tablet della timbratrice.')
                self.dismesso = True
            else:
                logger.error('Server %s: HTTP %s — %s', pistola_id, r.status_code, r.text[:200])
        except requests.exceptions.RequestException as e:
            logger.error('Connessione fallita per pistola=%s codice=%s: %s', pistola_id, codice, e)

    # ---- Loop ---------------------------------------------------------------

    def run(self):
        if not self.prefix_map:
            logger.warning(
                'Nessun prefisso mappato in %s. Configura "prefix_map" prima di usare in produzione.',
                CONFIG_PATH,
            )
        logger.info(
            'ScanHub avviato — server=%s endpoint=%s prefissi=%s',
            self.server_url, self.endpoint, list(self.prefix_map.keys()),
        )
        keyboard.hook(self.on_key)
        try:
            keyboard.wait()
        except KeyboardInterrupt:
            logger.info('Arresto richiesto da utente')


if __name__ == '__main__':
    ScanHub(load_config()).run()
