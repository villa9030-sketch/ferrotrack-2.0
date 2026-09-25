"""Lettura ESATTA (senza AI) degli ordini in PDF testuale dal formato noto.

Formato riconosciuto: "Ordine Fornitore" (gestionale di DECA S.r.l., file
OAFA*.pdf). Ogni riga della tabella e':

    <codice> <commessa> <descrizione> <q.ta'><u.m.> [prezzo importo] <consegna>

con la descrizione che puo' proseguire sulla riga sotto. Esempio reale
(OAFA202601184.pdf, 65 righe su 5 pagine):

    12C00042-00 C26-199-5 Piastra sup.colonna 350x164 sp.10 1.0037 1,00Nr 19,20000 19,20 02/10/26
    (S235JR) sostegno 401 TR - S235JR

Se il PDF non e' in questo formato si ritorna None e il chiamante usa
Gemini: questo lettore non indovina, o legge tutto o si fa da parte.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

# <codice> <commessa> <descrizione> <qta><um> [numeri...] <gg/mm/aa>
_RIGA = re.compile(
    r'^(?P<cod>[A-Z0-9][A-Z0-9._/-]*)\s+'
    r'(?P<comm>C\d{2}-\d+(?:-\d+)?)\s+'
    r'(?P<desc>.*?)\s*'
    r'(?P<qta>\d{1,6}(?:[.,]\d+)?)\s*(?P<um>Nr|NR|N°|nr|Pz|PZ|pz|Kg|KG|kg)\b'
    r'(?:\s+[\d.]+,\d+){0,3}'
    # la consegna a volte finisce fuori riga (righe col prezzo, 12C00023-00)
    r'(?:\s+(?P<cons>\d{2}/\d{2}/\d{2,4}))?\s*$')
_SOLO_DATA = re.compile(r'^\d{2}/\d{2}/\d{2,4}$')
_INTESTAZIONE = re.compile(r'Codice\s+Commessa\s+Descrizione\s+Q\.t', re.I)
_NUMERO = re.compile(r'Ordine\s+Fornitore\s+([A-Z]{0,3}\s*\d{2,8})\s+(\d{2}/\d{2}/\d{4})', re.I)
_COMMESSA = re.compile(r'^(C\d{2}-\d+)\b', re.M)
_FINE_TABELLA = ('Note', 'Vettore', 'Totale Articoli', 'Trasporto a mezzo', 'Pagamento')

_MATERIALI = (
    (re.compile(r'AISI\s*316|INOX\s*316', re.I), 'INOX_316'),
    (re.compile(r'AISI\s*304|ASIS\s*304|INOX|X5\s*CrNi', re.I), 'INOX_304'),
    (re.compile(r'ZINCAT|DX51|SENDZIMIR', re.I), 'ZINCATO'),
    (re.compile(r'ANTICORDAL|ALLUMINIO|\bALU\b|\b6082\b|\b5754\b|\b5083\b', re.I), 'ALU'),
    (re.compile(r'OTTONE', re.I), 'OTTONE'),
    (re.compile(r'S235|S275|S355|1\.0037|FE\s*360|\bFERRO\b', re.I), 'S235'),
)
_SPESSORE = re.compile(r'\bsp\.?\s*(\d{1,2}(?:[.,]\d+)?)', re.I)


def _num(s: str) -> float:
    return float(s.replace('.', '').replace(',', '.')) if ',' in s else float(s)


def _data_iso(s: str) -> str | None:
    for fmt in ('%d/%m/%y', '%d/%m/%Y'):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _materiale(desc: str) -> str | None:
    for rx, mat in _MATERIALI:
        if rx.search(desc):
            return mat
    return None


def _testo_pagine(pdf_bytes: bytes) -> list[str]:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return [(p.extract_text() or '') for p in pdf.pages]


def leggi_ordine_fornitore(pdf_bytes: bytes) -> dict | None:
    """Dati dell'ordine nello stesso schema di rfq_importer.parse_order_pdf
    ({cliente, numero_ordine_cliente, data_consegna, note, articoli[]}),
    oppure None se il PDF non e' un "Ordine Fornitore" leggibile."""
    try:
        pagine = _testo_pagine(pdf_bytes)
    except Exception as e:
        logger.info('ordine_testo: PDF non leggibile come testo: %s', e)
        return None
    tutto = '\n'.join(pagine)
    if 'Ordine Fornitore' not in tutto or not _INTESTAZIONE.search(tutto):
        return None

    articoli = []
    for testo in pagine:
        in_tabella = False
        ultimo = None
        for riga in (r.strip() for r in testo.splitlines()):
            if not riga:
                continue
            if _INTESTAZIONE.search(riga):
                in_tabella, ultimo = True, None
                continue
            if not in_tabella:
                continue
            # "fine" da solo e' la chiusura del documento, non descrizione
            if riga.startswith(_FINE_TABELLA) or riga.lower() == 'fine':
                in_tabella, ultimo = False, None
                continue
            m = _RIGA.match(riga)
            if m:
                qta = _num(m['qta'])
                ultimo = {
                    'codice': m['cod'],
                    'quantita': max(1, int(round(qta))),
                    'descrizione': m['desc'].strip(),
                    '_commessa': m['comm'],
                    '_consegna': _data_iso(m['cons']) if m['cons'] else None,
                    '_qta_letta': qta,
                }
                articoli.append(ultimo)
            elif _SOLO_DATA.match(riga):
                if ultimo is not None and not ultimo['_consegna']:
                    ultimo['_consegna'] = _data_iso(riga)
            elif ultimo is not None:
                # la descrizione prosegue sulla riga sotto
                ultimo['descrizione'] = (ultimo['descrizione'] + ' ' + riga).strip()

    if not articoli:
        return None
    for a in articoli:
        a['materiale'] = _materiale(a['descrizione'])
        sp = _SPESSORE.search(a['descrizione'])
        a['spessore_mm'] = _num(sp.group(1)) if sp else None

    cliente = next((r.strip() for r in pagine[0].splitlines()[:3]
                    if re.search(r'S\.?\s?r\.?\s?l|S\.?\s?p\.?\s?A|S\.?\s?n\.?\s?c', r, re.I)), '')
    mn = _NUMERO.search(tutto)
    commesse = sorted(set(_COMMESSA.findall(tutto)))
    consegne = sorted(a['_consegna'] for a in articoli if a['_consegna'])
    note = []
    if mn:
        note.append(f'Ordine Fornitore {" ".join(mn.group(1).split())} del {mn.group(2)}')
    if commesse:
        note.append('Commessa ' + ', '.join(commesse))
    decimali = [a['codice'] for a in articoli if a['_qta_letta'] != int(a['_qta_letta'])]
    if decimali:
        note.append('Quantita\' non intere arrotondate: ' + ', '.join(decimali))
    for a in articoli:
        a.pop('_qta_letta', None)
    logger.info('ordine_testo: Ordine Fornitore letto dal testo: %d righe, cliente=%s', len(articoli), cliente)
    return {
        'cliente': cliente or None,
        'numero_ordine_cliente': ' '.join(mn.group(1).split()) if mn else None,
        'data_consegna': consegne[0] if consegne else None,
        'note': ' · '.join(note),
        'articoli': articoli,
        '_fonte': 'testo PDF (Ordine Fornitore)',
    }
