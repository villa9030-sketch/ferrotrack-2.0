"""Ordini arrivati SOLO in PDF (senza disegni): le righe per Lantek.

Stefano (07/10/2026): "quando viene caricato un ordine solo in pdf, io da
Lantek ci posso associare i disegni che ho fatto cosi' alla successiva
fornitura il programma puo' prepararmi il pacchetto da inviare al Lantek".

- Le righe (codice, quantita') si leggono dal PDF dell'ordine con i lettori a
  regole che ci sono gia' (DECA "Ordine Fornitore", Poliform, B&B): niente AI.
  La lettura automatica dei formati sconosciuti NON si usa: il suo "codice" e'
  la prima parola della riga, e qui finirebbe in un ordine di produzione.
- In Lantek i pezzi hanno lo stesso codice del PDF (Stefano): quelle righe
  vanno da sole (pezzi_in_lantek trova anche la revisione, 08PA04452 ->
  08PA04452-00).
- Le righe che in Lantek non ci sono sono assiemi: Stefano le abbina UNA volta
  ai loro pezzi Lantek (tabella assiemi_lantek), oppure le segna "non va al
  laser". Dalla fornitura dopo FerroTrack le scompone da solo.

Lantek si legge e basta (backend/lantek.py); qui si scrive solo nel database
di FerroTrack, e solo quando Stefano conferma un abbinamento.
"""
from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger(__name__)

_CACHE_PDF = {}
_LOCK = threading.Lock()


def _chiave(codice) -> str:
    return str(codice or '').strip().upper()


def righe_dal_pdf_bytes(pdf_bytes: bytes) -> list:
    """[{'pos', 'codice', 'descrizione', 'quantita'}] dal PDF dell'ordine, []
    se il formato non e' fra quelli che si leggono con certezza. Niente prezzi:
    al laser non servono (e non si mostrano)."""
    from .preventivi.ordine_testo import leggi_ordine_fornitore
    from .preventivi.ordine_prezzi import leggi_ordine_prezzato
    righe = []
    d = leggi_ordine_fornitore(pdf_bytes)
    if d and d.get('articoli'):
        righe = [(a.get('codice'), a.get('descrizione'), a.get('quantita')) for a in d['articoli']]
    else:
        p = leggi_ordine_prezzato(pdf_bytes)
        if p and not p.get('generico'):
            # con le misure delle righe sotto (Poliform): senza, 11 telai
            # diversi si leggono tutti "TELAIO IN METALLO VERNICIATO RAL 7115"
            righe = [(r.get('codice'), ' · '.join(x for x in (r.get('descrizione'), r.get('dettaglio')) if x),
                      r.get('quantita')) for r in p.get('righe') or []]
    out = []
    for cod, desc, q in righe:
        cod = str(cod or '').strip()
        try:
            q = int(round(float(q or 0)))
        except (TypeError, ValueError):
            q = 0
        if not cod or q <= 0:
            continue
        out.append({'pos': len(out) + 1, 'codice': cod, 'descrizione': str(desc or '').strip(), 'quantita': q})
    return out


def righe_dal_pdf(percorso: str) -> list:
    """Come righe_dal_pdf_bytes, dal file (ricordato finche' il file non cambia)."""
    try:
        st = os.stat(percorso)
    except OSError:
        return []
    chiave = (os.path.normcase(os.path.abspath(percorso)), st.st_mtime, st.st_size)
    with _LOCK:
        if chiave in _CACHE_PDF:
            return [dict(r) for r in _CACHE_PDF[chiave]]
    try:
        with open(percorso, 'rb') as fh:
            righe = righe_dal_pdf_bytes(fh.read())
    except Exception:
        logger.warning('righe del PDF non lette: %s', percorso, exc_info=True)
        righe = []
    with _LOCK:
        if len(_CACHE_PDF) > 100:
            _CACHE_PDF.clear()
        _CACHE_PDF[chiave] = righe
    return [dict(r) for r in righe]


# ---------------------------------------------------------------------------
# Abbinamenti salvati
# ---------------------------------------------------------------------------
def abbinamenti(codici: list) -> dict:
    """{codice in maiuscolo: {'non_laser': bool, 'pezzi': [{'codice', 'quantita'}],
    'da', 'il'}} per i codici gia' abbinati."""
    from .models import get_session, AssiemeLantek
    chiavi = sorted({_chiave(c) for c in codici if _chiave(c)})
    if not chiavi:
        return {}
    s = get_session()
    try:
        out = {}
        for a in s.query(AssiemeLantek).filter(AssiemeLantek.codice.in_(chiavi)) \
                .order_by(AssiemeLantek.pezzo_lantek).all():
            x = out.setdefault(a.codice, {'non_laser': False, 'pezzi': [], 'da': a.creato_da,
                                          'il': a.creato_il.isoformat() if a.creato_il else None})
            if a.non_laser:
                x['non_laser'] = True
            elif a.pezzo_lantek:
                x['pezzi'].append({'codice': a.pezzo_lantek, 'quantita': int(a.quantita or 1)})
        return out
    finally:
        s.close()


def salva_abbinamento(codice: str, pezzi: list, non_laser: bool, descrizione: str = '',
                      cliente: str = '', chi: str = '') -> None:
    """Sostituisce l'abbinamento di quel codice (pezzi: [{'codice', 'quantita'}])."""
    from .models import get_session, AssiemeLantek
    chiave = _chiave(codice)
    s = get_session()
    try:
        s.query(AssiemeLantek).filter(AssiemeLantek.codice == chiave).delete()
        voci = [None] if non_laser else pezzi
        for p in voci:
            s.add(AssiemeLantek(codice=chiave, pezzo_lantek=None if p is None else p['codice'],
                                quantita=1 if p is None else int(p['quantita']), non_laser=bool(non_laser),
                                descrizione=(descrizione or '')[:200] or None,
                                cliente=(cliente or '')[:120] or None, creato_da=chi or None))
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def dimentica_abbinamento(codice: str) -> int:
    from .models import get_session, AssiemeLantek
    s = get_session()
    try:
        n = s.query(AssiemeLantek).filter(AssiemeLantek.codice == _chiave(codice)).delete()
        s.commit()
        return n
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Distinta per Lantek
# ---------------------------------------------------------------------------
def distinta(righe_pdf: list, abb: dict, info: dict | None = None) -> list:
    """Le righe della distinta (come app._distinta_ordine, tipo 'lamiera') per
    il flusso di Lantek: la riga col suo codice, oppure i pezzi dell'assieme
    (pezzi per assieme x quantita' della riga); le righe "non laser" no.
    Materiale e spessore non si mettono: i pezzi sono gia' in Lantek e vale
    il suo (quelli del PDF darebbero solo differenze finte).

    info (lantek.pezzi_in_lantek dei codici del PDF): la riga prende il codice
    esatto di Lantek (08PA04452 -> 08PA04452-00), cosi' se lo stesso pezzo
    arriva anche da un assieme le quantita' si sommano in UN ordine di
    produzione invece di farne due."""
    pezzi = (info or {}).get('pezzi') or {}
    out = []
    for r in righe_pdf:
        a = abb.get(_chiave(r['codice']))
        if a and a['non_laser']:
            continue
        if a and a['pezzi']:
            for p in a['pezzi']:
                out.append({'codice': p['codice'], 'descrizione': r['descrizione'], 'tipo': 'lamiera',
                            'quantita': p['quantita'] * r['quantita'], 'materiale': None,
                            'spessore_mm': None, 'assieme': r['codice'], 'disegno': None})
            continue
        p = pezzi.get(r['codice']) or {}
        cod = (p.get('codice_lantek') if p.get('esiste') else None) or r['codice']
        out.append({'codice': cod, 'descrizione': r['descrizione'], 'tipo': 'lamiera',
                    'quantita': r['quantita'], 'materiale': None, 'spessore_mm': None,
                    'assieme': None, 'disegno': None})
    return out


def stato_righe(righe_pdf: list, abb: dict, info: dict, pezzi_ordine: dict | None) -> dict:
    """Come stanno le righe del PDF rispetto a Lantek, per la guida:

    {'n_righe', 'da_abbinare': [{codice, descrizione, quantita, proposta}],
     'assiemi': [{codice, descrizione, quantita, pezzi}], 'non_laser': [...],
     'liberi': [{codice, quantita}]}

    'liberi' = pezzi che Lantek ha con questo numero d'ordine e che nessuna
    riga spiega: sono i pezzi degli assiemi da abbinare. Se c'e' UN solo
    assieme da abbinare gli si propongono tutti (pezzi per assieme = quantita'
    in Lantek / quantita' della riga); con piu' assiemi sceglie Stefano."""
    pezzi = (info or {}).get('pezzi') or {}
    spiegati = set()
    da_abbinare, assiemi, non_laser = [], [], []
    for r in righe_pdf:
        a = abb.get(_chiave(r['codice']))
        voce = {'codice': r['codice'], 'descrizione': r['descrizione'], 'quantita': r['quantita']}
        if a and a['non_laser']:
            non_laser.append(voce)
        elif a and a['pezzi']:
            assiemi.append({**voce, 'pezzi': a['pezzi'], 'da': a.get('da'), 'il': a.get('il')})
            spiegati.update(_chiave(p['codice']) for p in a['pezzi'])
        else:
            p = pezzi.get(r['codice'])
            if p and p.get('esiste'):
                spiegati.add(_chiave(p.get('codice_lantek') or r['codice']))
            elif (info or {}).get('disponibile'):
                da_abbinare.append(voce)
    liberi = [{'codice': k, 'quantita': int(q) if float(q).is_integer() else q}
              for k, q in sorted((pezzi_ordine or {}).items()) if k not in spiegati]
    for v in da_abbinare:
        v['proposta'] = []
    if len(da_abbinare) == 1 and liberi:
        v = da_abbinare[0]
        v['proposta'] = [{'codice': x['codice'], 'quantita': max(1, int(round(float(x['quantita']) / v['quantita'])))}
                         for x in liberi]
    return {'n_righe': len(righe_pdf), 'da_abbinare': da_abbinare, 'assiemi': assiemi,
            'non_laser': non_laser, 'liberi': liberi}
