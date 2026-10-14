"""Cosa FerroTrack ha GIA' mandato a Lantek, ordine per ordine.

Prova notturna del 14/10/2026, ore 01:25: SQL Server di Lantek era in
aggiornamento ("single user mode", "Communication link failure"). "Manda a
Lantek" ha risposto 200, e lo stesso invio ripetuto subito dopo ha mandato di
nuovo gli stessi 66 codici: il controllo dei doppioni ("gia' in produzione")
leggeva Lantek, e senza Lantek lasciava passare tutto.

Ora:
  - FerroTrack tiene il suo registro (tabella lantek_invii: codice,
    quantita', commessa, riferimento, quando, chi) e lo guarda PRIMA di
    Lantek: un codice gia' mandato per quell'ordine non riparte;
  - un ordine si manda uno alla volta (prenota): due "Manda" premuti insieme
    non partono entrambi;
  - se Lantek non si legge, quello che non e' nel registro non parte a occhi
    chiusi: si dice "Lantek non risponde" e decide chi sta mandando.
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime

MSG_LANTEK_GIU = ("Lantek non risponde in questo momento: riprova tra qualche minuto. "
                  "Niente è stato mandato.")

_LOCKS: dict = {}
_GUARDIA = threading.Lock()


@contextmanager
def prenota(order_id: str):
    """Un invio alla volta per ordine: True se tocca a noi, False se un altro
    invio dello stesso ordine e' in corso adesso."""
    with _GUARDIA:
        lk = _LOCKS.setdefault(order_id, threading.Lock())
    preso = lk.acquire(blocking=False)
    try:
        yield preso
    finally:
        if preso:
            lk.release()


def mandati(order_id: str) -> dict:
    """{CODICE MAIUSCOLO: {'quantita', 'il'}} gia' mandati (invii attivi)."""
    from .database import get_session
    from .models import LantekInvio
    s = get_session()
    try:
        out = {}
        for r in s.query(LantekInvio).filter(LantekInvio.order_id == order_id,
                                             LantekInvio.annullato_il.is_(None)).all():
            k = str(r.codice or '').strip().upper()
            x = out.setdefault(k, {'quantita': 0.0, 'il': None})
            x['quantita'] += float(r.quantita or 0)
            if r.creato_il and (x['il'] is None or r.creato_il > x['il']):
                x['il'] = r.creato_il
        return out
    finally:
        s.close()


def togli_mandati(righe: list, gia: dict) -> list:
    """Le righe senza i codici gia' mandati da FerroTrack per quest'ordine."""
    return [r for r in righe if str(r.get('codice') or '').strip().upper() not in gia]


def registra(order_id: str, righe: list, commessa: str, invio: str, riferimento=None, chi: str = '') -> int:
    from .database import get_session
    from .models import LantekInvio
    s = get_session()
    try:
        for r in righe:
            s.add(LantekInvio(order_id=order_id, codice=str(r['codice']).strip(), quantita=float(r.get('quantita') or 0),
                              commessa=commessa or None, invio=invio or None,
                              riferimento=riferimento(r['codice']) if callable(riferimento) else None,
                              creato_da=chi or None))
        s.commit()
        return len(righe)
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def annulla(order_id: str, chi: str = '') -> int:
    """"Non e' ancora in Lantek": gli invii registrati non contano piu' (si
    possono rimandare apposta). Ritorna quanti."""
    from .database import get_session
    from .models import LantekInvio
    s = get_session()
    try:
        righe = s.query(LantekInvio).filter(LantekInvio.order_id == order_id,
                                            LantekInvio.annullato_il.is_(None)).all()
        ora = datetime.utcnow()
        for r in righe:
            r.annullato_il, r.annullato_da = ora, chi or None
        s.commit()
        return len(righe)
    finally:
        s.close()
