"""Le letture pesanti di un disegno per la pagina laser, fuori dalle richieste.

Prova notturna 22-23/10, 19:14-19:20: sullo stesso pezzo (un DWG convertito
in un DXF da 16 MB) /lettura, /giusto e /pezzi-foglio non hanno risposto
per 120 s, 7 volte. Ogni richiesta rileggeva il disegno DENTRO il server
(geometria 122 s + motore 192 s, misurati) e la geometria, oltre 16 MB, non
entrava nemmeno nella cache: ogni richiesta ricominciava da capo, e intanto
tutto il server rallentava (il tablet dell'officina fino a 65 s).

Ora:
  - la lettura si fa UNA volta per disegno (stessa chiave: percorso, data
    del file, impostazioni), in un processo lavoratore
    (preventivi/analisi_isolata.esegui_per_pagina) con tempo massimo e posti
    riservati alle pagine; un thread qui aspetta soltanto il lavoratore;
  - chi la chiede aspetta al massimo `attesa_s`: se non e' pronta riceve
    InPreparazione (la pagina riprova da sola dopo qualche secondo), e il
    calcolo continua per conto suo;
  - un disegno troppo lento o troppo grande si ferma e la risposta resta in
    memoria: le richieste dopo rispondono subito "da guardare a mano"
    (DisegnoNonLeggibile), senza rileggerlo mai nel server;
  - al massimo MAX_IN_CORSO letture in coda: oltre, InPreparazione subito.
Se il lavoratore non si puo' avviare, la lettura si fa nel server come prima
(meglio lento che niente).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# Attesa massima di una richiesta della pagina (la pagina poi riprova).
ATTESA_PAGINA_S = float(os.environ.get('FERROTRACK_ATTESA_DISEGNO_S', '') or 20.0)
MAX_IN_CORSO = 24
_RISULTATI_MAX = 12            # letture riuscite tenute (possono essere grandi)
_ERRORI_MAX = 256              # "troppo lento" & co.: piccoli, si tengono di piu'

MSG_LENTO = ('Disegno troppo complesso o lento da leggere (oltre {s:.0f} s): '
             'da guardare a mano')
MSG_GRANDE = 'Disegno troppo grande da leggere ({motivo}): da guardare a mano'
MSG_INTERROTTO = "La lettura del disegno si e' interrotta (file anomalo): da guardare a mano"
MSG_IN_PREPARAZIONE = 'Sto preparando il disegno: un attimo, riprovo da solo'


class InPreparazione(Exception):
    """La lettura non e' ancora pronta: riprovare tra poco."""

    def __init__(self, msg: str = MSG_IN_PREPARAZIONE):
        super().__init__(msg)


class DisegnoNonLeggibile(Exception):
    """Il disegno non si legge (troppo lento, troppo grande, errore)."""

    def __init__(self, msg: str, codice: str = 'errore'):
        super().__init__(msg)
        self.codice = codice


_lock = threading.Lock()
_voci: 'OrderedDict' = OrderedDict()   # chiave -> {'evento', 'esito'}
_pool = None
_in_corso = 0


def _chiave(tipo: str, percorso: str, cfg: dict):
    try:
        mt = os.path.getmtime(percorso)
    except OSError:
        mt = 0
    return (tipo, os.path.normcase(os.path.abspath(percorso)), mt,
            json.dumps(cfg or {}, sort_keys=True, default=str))


def _nel_server(tipo: str, percorso: str, cfg: dict):
    """Lo stesso calcolo del lavoratore, qui: solo se il lavoratore non parte."""
    from .preventivi import analisi_isolata
    if tipo == 'foglio':
        return analisi_isolata.dati_foglio(percorso, cfg)
    if tipo == 'pezzi_foglio':
        from .preventivi.tipo_disegno import pezzi_del_foglio_da_file
        return pezzi_del_foglio_da_file(percorso, cfg)
    raise ValueError('lettura sconosciuta: %s' % tipo)


def _leggi(tipo: str, percorso: str, cfg: dict) -> tuple:
    """('ok', dato) | ('errore', msg, codice) | ('occupato',)."""
    from .preventivi import analisi_isolata as ai
    motivo = ai.controllo_rapido(percorso)
    if motivo:
        return ('errore', MSG_GRANDE.format(motivo=motivo), 'troppo_grande')
    compito = {'tipo': tipo, 'path': os.path.abspath(percorso), 'cfg': cfg or {}}
    try:
        stato, dato = ai.esegui_per_pagina(compito)
    except ai.Occupato:
        return ('occupato',)
    if stato == 'ok':
        return ('ok', dato)
    if stato == 'senza_lavoratore':
        try:
            return ('ok', _nel_server(tipo, percorso, cfg))
        except Exception as e:  # noqa: BLE001
            return ('errore', f'{type(e).__name__}: {e}', 'errore')
    if stato == 'lento':
        logger.warning('[%s] %s: lettura interrotta dopo %.0f s, troppo lenta', os.path.basename(percorso),
                       tipo, ai.TEMPO_MAX_S)
        return ('errore', MSG_LENTO.format(s=ai.TEMPO_MAX_S), 'troppo_lento')
    if stato == 'morto':
        logger.warning('[%s] %s: il lavoratore si e\' fermato', os.path.basename(percorso), tipo)
        return ('errore', MSG_INTERROTTO, 'lettura_interrotta')
    return ('errore', str(dato or 'errore nella lettura del disegno'), 'errore')


def _esegui(chiave, voce, tipo, percorso, cfg):
    global _in_corso
    t0 = time.time()
    try:
        esito = _leggi(tipo, percorso, cfg)
    except Exception as e:  # noqa: BLE001
        logger.exception('lettura %s del disegno fallita: %s', tipo, percorso)
        esito = ('errore', f'{type(e).__name__}: {e}', 'errore')
    with _lock:
        _in_corso -= 1
        voce['esito'] = esito
        voce['fatto_il'] = time.time()
        if esito[0] == 'occupato':
            # niente da ricordare: la prossima richiesta riprova
            if _voci.get(chiave) is voce:
                _voci.pop(chiave, None)
        _pota()
    voce['evento'].set()
    if time.time() - t0 > 10:
        logger.info('lettura %s di %s: %.1f s (%s)', tipo, os.path.basename(percorso), time.time() - t0, esito[0])


def _pota():
    """Sotto _lock: tiene poche letture riuscite e piu' esiti di errore."""
    fatte = [(k, v) for k, v in _voci.items() if v.get('esito')]
    ok = [k for k, v in fatte if v['esito'][0] == 'ok']
    ko = [k for k, v in fatte if v['esito'][0] != 'ok']
    for k in ok[:max(0, len(ok) - _RISULTATI_MAX)]:
        _voci.pop(k, None)
    for k in ko[:max(0, len(ko) - _ERRORI_MAX)]:
        _voci.pop(k, None)


def prepara(tipo: str, percorso: str, cfg: dict, attesa_s: float | None = None):
    """Il risultato della lettura `tipo` del disegno ('foglio': {'geometria',
    'motore'}; 'pezzi_foglio': la lista dei pezzi). Aspetta al massimo
    attesa_s (None = ATTESA_PAGINA_S). InPreparazione se non e' pronta,
    DisegnoNonLeggibile se il disegno non si legge."""
    global _pool, _in_corso
    attesa = ATTESA_PAGINA_S if attesa_s is None else float(attesa_s)
    chiave = _chiave(tipo, percorso, cfg)
    with _lock:
        voce = _voci.get(chiave)
        if voce is None:
            if _in_corso >= MAX_IN_CORSO:
                raise InPreparazione('Troppi disegni in preparazione: riprovo tra poco')
            voce = {'evento': threading.Event(), 'esito': None}
            _voci[chiave] = voce
            _in_corso += 1
            if _pool is None:
                _pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='prepara-disegno')
            _pool.submit(_esegui, chiave, voce, tipo, percorso, cfg)
        else:
            _voci.move_to_end(chiave)
    if not voce['evento'].wait(max(0.0, attesa)):
        raise InPreparazione()
    esito = voce['esito']
    if esito[0] == 'ok':
        return esito[1]
    if esito[0] == 'occupato':
        raise InPreparazione()
    raise DisegnoNonLeggibile(esito[1], esito[2])


def stato(tipo: str, percorso: str, cfg: dict):
    """'pronto' | 'in_corso' | 'errore' | None (mai chiesto): senza aspettare."""
    with _lock:
        voce = _voci.get(_chiave(tipo, percorso, cfg))
    if voce is None:
        return None
    if not voce['evento'].is_set():
        return 'in_corso'
    return 'pronto' if voce['esito'][0] == 'ok' else 'errore'


def svuota() -> None:
    """Per i test."""
    with _lock:
        _voci.clear()
