"""Versione dei dati e cache breve per le letture che tante postazioni
chiedono insieme (tablet dell'officina, schermo in ufficio).

Dalla prova notturna del 22-23/10: con 10 postazioni collegate e un disegno
pesante in lavorazione, GET /api/officina/tablet arrivava a 30-65 s (p95
29,7 s) e /api/dashboard-live a 4 s. Ogni tablet, ogni minuto, rifaceva da
capo distinta, disegni, "taglia entro" e segnalazioni di tutti gli ordini
aperti: decine di letture del database per richiesta, e con il server sotto
sforzo ogni lettura aspetta il suo turno (il GIL di Python).

Ora il risultato si calcola UNA volta e lo riusano tutte le postazioni,
finche' i dati non cambiano:

  - `generazione()`: un numero che cresce a ogni salvataggio nel database
    fatto da questo server (dopo il commit). Non conta le scritture di
    servizio dell'accesso (ultimo uso del dispositivo, sessioni, tentativi
    del PIN), che arrivano a ogni giro di ogni postazione e non cambiano
    niente di quello che si mostra.
  - `CacheBreve`: tiene l'ultimo risultato; vale finche' la generazione e'
    la stessa e non e' piu' vecchio di `durata_s` (per i cambi che non
    passano dal database, es. un file copiato a mano). Un calcolo alla volta:
    chi arriva mentre un'altra postazione sta gia' ricalcolando riceve il
    risultato precedente se e' recente (`vecchia_max_s`), altrimenti aspetta
    quel calcolo (al massimo `attesa_s`), invece di rifarlo in parallelo.
"""
from __future__ import annotations

import threading
import time

from sqlalchemy import event
from sqlalchemy.orm import Session

# Scritture che non cambiano i dati mostrati: le fa l'accesso a ogni richiesta.
TABELLE_DI_SERVIZIO = frozenset({'device_tokens', 'sessioni_accesso', 'tentativi_pin'})

_lock = threading.Lock()
_generazione = 0
_CHIAVE = 'ft_tabelle_scritte'


def generazione() -> int:
    return _generazione


def avanza() -> int:
    """Segna che i dati sono cambiati (lo fa da solo il commit; serve ai test
    e a chi cambia dati fuori dal database)."""
    global _generazione
    with _lock:
        _generazione += 1
        return _generazione


def _segna(session, tabelle) -> None:
    session.info.setdefault(_CHIAVE, set()).update(tabelle)


@event.listens_for(Session, 'after_flush')
def _dopo_flush(session, _ctx):
    tab = set()
    for obj in list(session.new) + list(session.dirty) + list(session.deleted):
        t = getattr(obj, '__table__', None)
        tab.add(getattr(t, 'name', '?'))
    if tab:
        _segna(session, tab)


@event.listens_for(Session, 'do_orm_execute')
def _esecuzione(stato):
    if stato.is_select:
        return
    tab = getattr(getattr(stato.statement, 'table', None), 'name', None)
    _segna(stato.session, {tab or '?'})


@event.listens_for(Session, 'after_commit')
def _dopo_commit(session):
    tab = session.info.pop(_CHIAVE, None)
    if tab and (tab - TABELLE_DI_SERVIZIO):
        avanza()


@event.listens_for(Session, 'after_rollback')
def _dopo_rollback(session):
    session.info.pop(_CHIAVE, None)


class CacheBreve:
    """L'ultimo risultato di un calcolo pesante, per tutte le richieste.

    prendi(chiave, calcola): `chiave` sono le cose che cambiano il risultato
    e non stanno nel database (es. la data di oggi, le impostazioni)."""

    def __init__(self, durata_s: float = 30.0, vecchia_max_s: float = 60.0, attesa_s: float = 20.0):
        self.durata_s = durata_s
        self.vecchia_max_s = vecchia_max_s
        self.attesa_s = attesa_s
        self._lock = threading.Lock()
        self._voce = None          # (chiave, generazione, inizio, valore)
        self._in_corso = None      # threading.Event del calcolo in corso
        self.calcoli = 0           # quante volte si e' calcolato (per i test)

    def svuota(self) -> None:
        with self._lock:
            self._voce = None

    def _calcola(self, chiave, calcola, evento):
        gen = generazione()        # PRIMA di leggere: un salvataggio durante il calcolo lo invalida
        inizio = time.monotonic()
        try:
            valore = calcola()
        except BaseException:
            with self._lock:
                if self._in_corso is evento:
                    self._in_corso = None
            evento.set()
            raise
        with self._lock:
            self.calcoli += 1
            self._voce = (chiave, gen, inizio, valore)
            if self._in_corso is evento:
                self._in_corso = None
        evento.set()
        return valore

    def prendi(self, chiave, calcola):
        with self._lock:
            ora = time.monotonic()
            v = self._voce
            stessa = v is not None and v[0] == chiave
            if stessa and v[1] == generazione() and ora - v[2] < self.durata_s:
                return v[3]
            evento = self._in_corso
            if evento is None:
                evento = self._in_corso = threading.Event()
                mio = True
            else:
                mio = False
                # un'altra richiesta sta gia' ricalcolando: intanto vale l'ultimo
                if stessa and ora - v[2] < self.vecchia_max_s:
                    return v[3]
        if mio:
            return self._calcola(chiave, calcola, evento)
        evento.wait(self.attesa_s)
        with self._lock:
            v = self._voce
            if v is not None and v[0] == chiave and time.monotonic() - v[2] < self.vecchia_max_s + self.attesa_s:
                return v[3]
        # niente di utile e il calcolo dell'altra non arriva: si calcola qui
        return calcola()
