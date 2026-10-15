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
                              commessa=commessa or None, invio=invio or None, origine='ft',
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


# ---------------------------------------------------------------------------
# Gli invii di PRIMA del registro (file dell'XML Importer)
# ---------------------------------------------------------------------------
def _quando_file(nome: str):
    """'..._20261012_142444_...' -> istante UTC (il nome e' in ora locale)."""
    import re
    m = re.search(r'_(\d{8})_(\d{6})', nome)
    if not m:
        return None
    from .orario import locale_a_utc
    return locale_a_utc(datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S'))


def importa_storico(cartella: str) -> int:
    """Riempie il registro con gli invii fatti prima che esistesse: i file
    ORDINI_LANTEK_*.xml in <cartella>/Importer log, solo se il loro rapporto
    (*_log.html, letto come fa "Manda a Lantek": lantek.leggi_rapporto) dice
    che l'import e' riuscito; i codici col loro errore nel rapporto restano
    fuori. SaleOrder -> ordine FerroTrack con la stessa regola dell'invio
    (commessa_lantek del numero d'ordine del cliente). Una riga gia' presente
    (stesso ordine, codice, riferimento) non si riscrive: si puo' rifare
    quante volte si vuole. Ritorna le righe aggiunte."""
    import glob
    import os
    import xml.etree.ElementTree as ET
    from .database import get_session
    from .models import LantekInvio, Order
    from . import lantek as _lt
    base = os.path.join(cartella or '', 'Importer log')
    files = sorted(glob.glob(os.path.join(glob.escape(base), 'ORDINI_LANTEK_*.xml')))
    if not files:
        return 0
    s = get_session()
    try:
        # commessa (come la scrive l'invio) -> ordini FerroTrack
        # (come app._numero_per_cartelle: il numero del cliente del preventivo, se c'e')
        from .models import Preventivo
        num_cli = {pid: (n or '').strip() for pid, n in s.query(Preventivo.id, Preventivo.numero_ordine_cliente).all()}
        per_commessa = {}
        for o in s.query(Order).all():
            num = num_cli.get(o.preventivo_id_origine or '') or o.numero_ordine or ''
            per_commessa.setdefault(_lt.commessa_lantek(num).strip().upper(), []).append(o.id)
        gia = {(r.order_id, str(r.codice).strip().upper(), r.riferimento or '')
               for r in s.query(LantekInvio).all()}
        aggiunte = 0
        for f in files:
            log = f[:-4] + '_log.html'
            if not os.path.isfile(log):
                continue
            try:
                rap = _lt.leggi_rapporto(log)
            except Exception:
                continue
            if not rap.get('totale') or not rap.get('ok'):
                continue
            con_errore = {str(e.get('pezzo') or '').strip().upper() for e in rap.get('errori') or []}
            quando = _quando_file(os.path.basename(f))
            try:
                cmds = [{x.get('FldRef'): x.get('FldValue') for x in c.iter('FIELD')}
                        for c in ET.parse(f).getroot().iter('COMMAND')]
            except Exception:
                continue
            for c in cmds:
                cod = str(c.get('Product') or '').strip()
                so = str(c.get('SaleOrder') or '').strip().upper()
                if not cod or cod.upper() in con_errore:
                    continue
                try:
                    q = float(str(c.get('Quantity') or '0').replace(',', '.'))
                except ValueError:
                    q = 0
                rif = str(c.get('Reference') or '')
                inv = rif.rsplit('-', 1)[-1] if rif.rsplit('-', 1)[-1].isdigit() else None
                for oid in per_commessa.get(so, []):
                    k = (oid, cod.upper(), rif)
                    if k in gia:
                        continue
                    gia.add(k)
                    s.add(LantekInvio(order_id=oid, codice=cod, quantita=q, commessa=c.get('SaleOrder'),
                                      riferimento=rif or None, invio=inv, origine='storico',
                                      creato_il=quando or datetime.utcnow(), creato_da='FerroTrack (storico)'))
                    aggiunte += 1
        s.commit()
        return aggiunte
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
