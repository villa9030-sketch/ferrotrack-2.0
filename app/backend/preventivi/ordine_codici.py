"""Lettura dell'ordine del cliente PARTENDO DAI CODICI DEI DISEGNI (senza AI).

I codici li abbiamo gia': sono i nomi dei DXF importati. Qui si cercano nel
testo del PDF d'ordine e si prende la quantita' sulla stessa riga ("4,00Nr",
"2 pz"). Non serve conoscere il formato del cliente.

- testo letto nell'ordine in cui e' scritto (use_text_flow): negli ordini DECA
  il percorso del file (\\\\server2025\\...\\13PA00434-00.pdf) sta sopra la
  descrizione e, letto per posizione, mescola le lettere di entrambi;
- lo stesso codice su piu' righe (piu' commesse) si SOMMA; una riga conta una
  volta anche se il codice ricompare nei percorsi PDF/STEP/DXF;
- codice citato senza quantita' (rif., note) → quantita' None: da controllare;
- due codici diversi con la stessa quantita' sulla stessa riga → 'dubbio'.

Prova sui 43 ordini DECA del 2025-26 (studio_sviluppo_piegati/o3_generico.py):
175 quantita' su 175 uguali al lettore esatto DECA (ordine_testo), letti anche
gli ordini che quello non legge (codice solo nel percorso del file).
"""
from __future__ import annotations

import io
import os
import re

_QTA = re.compile(r'(?<![\d,.])(\d{1,5})(?:[,.](\d{1,3}))?\s*(?:Nr|NR|N°|nr|Pz|PZ|pz|pezzi|PEZZI)\b')
_NUM_ORDINE = re.compile(r'Ordine\s+(?:Fornitore\s+|n\.?\s*|nr\.?\s*|N°\s*)?([A-Z]{0,3}\s*\d{2,8})\s+(?:del\s+)?(\d{2}/\d{2}/\d{2,4})', re.I)


def chiave(s: str) -> str:
    """'46SA00578-00.dxf' / '46SA00578' → '46SA00578' (senza estensione e revisione)."""
    b = os.path.splitext(os.path.basename(str(s or '')))[0].upper().strip()
    b = re.sub(r'\s*\(\d+\)$', '', b)
    b = re.sub(r'_CLEANED$', '', b)
    return re.sub(r'[-_]\d{2}$', '', b)


def testo_righe(pdf_bytes: bytes) -> list[str]:
    import pdfplumber
    righe = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for p in pdf.pages:
            righe += (p.extract_text(use_text_flow=True) or '').splitlines()
    return righe


_COMMESSA = re.compile(r'(?<![A-Z0-9])(C\d{2}-\d{1,5}(?:-\d{1,5})?)(?![\d])')


def leggi_per_codici(pdf_bytes: bytes, codici: list) -> dict:
    """{chiave: {qta: float|None, pos: int, righe: int, dubbio: bool,
    dettaglio: [{pos, commessa, qta}]}} per i codici trovati nel PDF; quelli
    non citati non compaiono. `dettaglio` = le righe dell'ordine una per una
    (DECA ripete lo stesso codice su piu' commesse: il preventivo per DECA le
    riporta uguali)."""
    chiavi = sorted({chiave(c) for c in codici if chiave(c)}, key=len, reverse=True)
    rx = {k: re.compile(r'(?<![A-Z0-9])' + re.escape(k) + r'(?:[-_]\d{2})?(?![A-Z0-9])') for k in chiavi}
    trovati = {}
    for i, riga in enumerate(testo_righe(pdf_bytes)):
        up = riga.upper()
        qui = [k for k in chiavi if rx[k].search(up)]
        if not qui:
            continue
        q = _QTA.findall(riga)
        qta = None
        if len(q) == 1:
            intero, dec = q[0]
            qta = float(intero + ('.' + dec if dec else ''))
        mc = _COMMESSA.search(up)
        for k in qui:
            t = trovati.setdefault(k, {'qta': 0.0, 'pos': i, 'righe': 0, 'dubbio': False, 'dettaglio': []})
            if qta is None:
                continue
            t['qta'] += qta
            t['righe'] += 1
            t['dettaglio'].append({'pos': i, 'commessa': mc.group(1) if mc else None, 'qta': qta})
            if len(qui) > 1:
                t['dubbio'] = True
    for t in trovati.values():
        if not t['righe']:
            t['qta'] = None
    return trovati


def intestazione(pdf_bytes: bytes) -> dict:
    """Numero e data dell'ordine se scritti in modo riconoscibile."""
    try:
        testo = '\n'.join(testo_righe(pdf_bytes)[:80])
    except Exception:
        return {}
    m = _NUM_ORDINE.search(testo)
    return {'numero_ordine': ' '.join(m.group(1).split()), 'data_ordine': m.group(2)} if m else {}
