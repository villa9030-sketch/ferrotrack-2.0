"""Lettura ESATTA (senza AI) degli ordini GIA' PREZZATI dei clienti a gestionale.

Per gli ordini che non passano dal preventivatore (i pezzi sono gia' in
Lantek, i prezzi sono quelli del listino del cliente): dal PDF si leggono
numero, data di consegna, righe con i prezzi e il totale netto, cosi' l'ordine
resta prezzato dentro l'app.

Formati riconosciuti (testo del PDF, nessuna AI):
  - Poliform  "Ordine Fornitore / Order to Supplier" (300013106, 300014938)
      3 05HVAR1960---- NR 60 TELAIO IN METALLO ... 47,3300 2.839,80 06/11/26
      ...
      Importo Netto / Net amount Imponibile ...
      5.770,30 5.770,30 22,00 1.269,47 7.039,77
  - B&B Italia "Ordine Acquisto" (PO_20260004215-16, -18)
      1 M3000100 TF CORONADO BRACCIOLO SINISTRO NR 5,00 29,3900 26/10/2026
      ...
      Totale EUR 1.107,75

Formato non riconosciuto -> LETTURA GENERICA (_generico): in ogni riga si
cerca quantita' x prezzo = importo (anche con uno sconto in mezzo), oppure,
se l'importo non e' stampato, quantita' e prezzo unitario (3-5 decimali). La
somma delle righe deve essere un numero stampato FUORI dalle righe (il
totale): solo allora si propone il valore. Cosi' un cliente nuovo funziona
senza scrivere regole. Su 400 PDF dell'archivio (disegni, ordini DECA...)
nessun valore inventato; l'unica proposta era un ordine vero (FOR-ORDINE
2041, 769,00). Numero, cliente e consegna in questo caso restano a mano.

Regola di sicurezza: il valore si propone SOLO se la somma delle righe torna
col totale stampato sull'ordine. Se non torna (riga letta male, sconti, spese)
le righe restano ma il totale no: l'ufficio lo scrive a mano. Un formato che
non si riconosce torna None: questo lettore non indovina.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime

logger = logging.getLogger(__name__)

_UM = r'(?:NR|Nr|nr|PZ|Pz|pz|N\.?)'
_IMP = r'\d{1,3}(?:\.\d{3})*,\d{2}'          # 1.186,50
_PREZZO = r'\d{1,3}(?:\.\d{3})*,\d{2,5}'     # 47,3300

# Poliform: pos codice NR qta descrizione prezzo [sconto1 sconto2] importo consegna
_POLI_RIGA = re.compile(
    rf'^(?P<pos>\d{{1,4}})\s+(?P<cod>[A-Z0-9][A-Z0-9._/]*?)-*\s+{_UM}\s+(?P<qta>\d+(?:,\d+)?)\s+'
    rf'(?P<desc>.*?)\s+(?P<prezzo>{_PREZZO})(?P<sconti>(?:\s+\d{{1,2}},\d{{2}})*)\s+(?P<imp>{_IMP})\s+'
    r'(?P<cons>\d{2}/\d{2}/\d{2,4})\s*$')
_POLI_NUMERO = re.compile(r'^N\.\s*([\d ]{6,20})', re.M)
_POLI_DATA = re.compile(r'Data\s*/\s*Date\s+(\d{2}/\d{2}/\d{4})')
_POLI_CONSEGNA = re.compile(r'Consegna richiesta.*\n\s*\d{2}/\d{2}/\d{4}\s+(\d{2}/\d{2}/\d{4})')
_POLI_NETTO = re.compile(rf'Importo Netto\s*/\s*Net amount.*\n\s*({_IMP})')

# B&B Italia: pos codice descrizione UM qta prezzo consegna (importo = qta x prezzo)
_BB_RIGA = re.compile(
    rf'^(?P<pos>\d{{1,4}})\s+(?P<cod>[A-Z0-9][A-Z0-9._/-]*)\s+(?P<desc>.*?)\s+{_UM}\s+'
    rf'(?P<qta>\d+(?:,\d+)?)\s+(?P<prezzo>{_PREZZO})\s+(?P<cons>\d{{2}}/\d{{2}}/\d{{4}})\s*$')
_BB_NUMERO = re.compile(r'Nr\.\s*([0-9][0-9-]{5,})')
_BB_DATA = re.compile(r'\bData\s+(\d{2}/\d{2}/\d{4})')
_BB_TOTALE = re.compile(rf'Totale\s+EUR\s+({_IMP})')


def _num(s: str) -> float:
    return float(s.replace('.', '').replace(',', '.'))


def _data_iso(s: str | None) -> str | None:
    if not s:
        return None
    for fmt in ('%d/%m/%Y', '%d/%m/%y'):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _testo_pagine(pdf_bytes: bytes) -> list[str]:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return [(p.extract_text() or '') for p in pdf.pages]


def _quadra(somma: float, totale: float | None) -> bool:
    return totale is not None and abs(somma - totale) <= max(0.02, 0.0001 * totale)


def _poliform(pagine: list[str]) -> dict | None:
    tutto = '\n'.join(pagine)
    if 'Order to Supplier' not in tutto or 'Net amount' not in tutto:
        return None
    righe = []
    for testo in pagine:
        for r in (x.strip() for x in testo.splitlines()):
            m = _POLI_RIGA.match(r)
            if m:
                righe.append({
                    'pos': int(m['pos']), 'codice': m['cod'], 'descrizione': m['desc'].strip(),
                    'quantita': _num(m['qta']), 'prezzo_unitario': _num(m['prezzo']),
                    'importo': _num(m['imp']), 'consegna': _data_iso(m['cons']),
                    'sconti': [_num(s) for s in m['sconti'].split()] or None,
                })
    if not righe:
        return None
    mn = _POLI_NUMERO.search(tutto)
    md = _POLI_DATA.search(tutto)
    mc = _POLI_CONSEGNA.search(tutto)
    mt = _POLI_NETTO.search(tutto)
    return {
        'formato': 'Poliform - Ordine Fornitore',
        'cliente': 'Poliform',
        'numero_ordine': ''.join(mn.group(1).split()) if mn else None,
        'data_ordine': _data_iso(md.group(1)) if md else None,
        'data_consegna': _data_iso(mc.group(1)) if mc else min((r['consegna'] for r in righe if r['consegna']), default=None),
        'righe': righe,
        'totale_stampato': _num(mt.group(1)) if mt else None,
    }


def _bebitalia(pagine: list[str]) -> dict | None:
    tutto = '\n'.join(pagine)
    if 'Ordine Acquisto' not in tutto or 'B&B ITALIA' not in tutto.upper():
        return None
    righe = []
    for testo in pagine:
        for r in (x.strip() for x in testo.splitlines()):
            m = _BB_RIGA.match(r)
            if m:
                qta, prezzo = _num(m['qta']), _num(m['prezzo'])
                righe.append({
                    'pos': int(m['pos']), 'codice': m['cod'], 'descrizione': m['desc'].strip(),
                    'quantita': qta, 'prezzo_unitario': prezzo, 'importo': round(qta * prezzo, 2),
                    'consegna': _data_iso(m['cons']), 'sconti': None,
                })
    if not righe:
        return None
    mn = _BB_NUMERO.search(tutto)
    md = _BB_DATA.search(tutto)
    mt = _BB_TOTALE.search(tutto)
    return {
        'formato': 'B&B Italia - Ordine Acquisto',
        'cliente': 'B&B Italia',
        'numero_ordine': mn.group(1) if mn else None,
        'data_ordine': _data_iso(md.group(1)) if md else None,
        'data_consegna': min((r['consegna'] for r in righe if r['consegna']), default=None),
        'righe': righe,
        'totale_stampato': _num(mt.group(1)) if mt else None,
    }


# Numeri all'italiana in una riga: 1.234,50  47,3300  300
_NUM = re.compile(r'(?<![\d/.,])(\d{1,3}(?:\.\d{3})+,\d{2,5}|\d+,\d{2,5}|\d+)(?![\d/,]|\.\d)')


def _numeri(riga: str) -> list:
    return [(_num(m.group(1)) if ',' in m.group(1) else float(m.group(1)), m.group(1))
            for m in _NUM.finditer(riga)]


def _riga_con_importo(ns: list, riga: str = ''):
    """(qta, prezzo, importo) se nella riga c'e' q x p (- sconto %) = importo."""
    trovata = None
    for i in range(len(ns)):
        q = ns[i][0]
        if q <= 0:
            continue
        for j in range(i + 1, len(ns)):
            p, ps = ns[j]
            if p <= 0 or ',' not in ps:
                continue
            for k in range(j + 1, len(ns)):
                imp, ks = ns[k]
                if imp <= 0 or not re.search(r',\d{2}$', ks):
                    continue
                if abs(q * p - imp) <= 0.011:
                    trovata = (q, p, imp)
                    continue
                # uno sconto % stampato fra prezzo e importo
                for d, _ds in ns[j + 1:k]:
                    if 0 < d < 100 and abs(q * p * (1 - d / 100) - imp) <= 0.011:
                        trovata = (q, p, imp)
    return trovata


_UM_QTA = re.compile(r'\b(?:NR|Nr|nr|PZ|Pz|pz|N\.|KG|Kg|kg|MT|ML|CAD)\s+\d')
# Il totale sta accanto a una di queste parole (sulla sua riga o nelle 3 sopra:
# "Totale EUR 1.107,75", "Importo Netto / Net amount" e sotto i numeri)
_PAROLA_TOTALE = re.compile(r'total|imponibile|netto|importo|amount', re.I)


def _riga_senza_importo(ns: list, riga: str = ''):
    """(qta, prezzo, importo calcolato): prezzo con 3-5 decimali, la quantita'
    e' il numero subito prima (B&B: "NR 5,00 29,3900 26/10/2026"). Serve
    l'unita' di misura davanti alla quantita': senza, il peso "6,601" di un
    disegno (47PA00170) sembrava un prezzo."""
    if not _UM_QTA.search(riga):
        return None
    for j in range(1, len(ns)):
        if re.search(r',\d{3,5}$', ns[j][1]) and ns[j - 1][0] > 0 and ns[j][0] > 0:
            q, p = ns[j - 1][0], ns[j][0]
            return (q, p, round(q * p, 2))
    return None


def _generico(pagine: list[str]) -> dict | None:
    linee = [r.strip() for t in pagine for r in t.splitlines() if r.strip()]
    for leggi_riga in (_riga_con_importo, _riga_senza_importo):
        righe, usate = [], set()
        for n, r in enumerate(linee):
            x = leggi_riga(_numeri(r), r)
            if x:
                q, p, imp = x
                tok = r.split()
                righe.append({'pos': len(righe) + 1, 'codice': tok[0] if tok else '', 'descrizione': r[:80],
                              'quantita': q, 'prezzo_unitario': p, 'importo': imp,
                              'consegna': None, 'sconti': None})
                usate.add(n)
        if not righe:
            continue
        somma = round(sum(r['importo'] for r in righe), 2)
        fuori = [v for n, r in enumerate(linee) if n not in usate
                 and any(_PAROLA_TOTALE.search(x) for x in linee[max(0, n - 3):n + 1])
                 for v, vs in _numeri(r) if ',' in vs]
        totale = next((v for v in fuori if _quadra(somma, v)), None)
        if somma > 0 and totale is not None:
            return {'formato': 'Lettura automatica (formato nuovo)', 'generico': True,
                    'cliente': None, 'numero_ordine': None, 'data_ordine': None, 'data_consegna': None,
                    'righe': righe, 'totale_stampato': totale}
    return None


def leggi_ordine_prezzato(pdf_bytes: bytes) -> dict | None:
    """Ordine gia' prezzato del cliente, oppure None se il formato non e' noto.

    {formato, cliente, numero_ordine, data_ordine, data_consegna,
     righe: [{pos, codice, descrizione, quantita, prezzo_unitario, importo,
              consegna, sconti}],
     totale_stampato, somma_righe, quadra, valore_ordine}
    `valore_ordine` (netto, IVA esclusa) c'e' solo se `quadra`."""
    try:
        pagine = _testo_pagine(pdf_bytes)
    except Exception as e:
        logger.info('ordine_prezzi: PDF non leggibile come testo: %s', e)
        return None
    for lettore in (_poliform, _bebitalia, _generico):
        try:
            d = lettore(pagine)
        except Exception:
            logger.exception('ordine_prezzi: lettura %s fallita', lettore.__name__)
            d = None
        if d:
            somma = round(sum(r['importo'] for r in d['righe']), 2)
            d['somma_righe'] = somma
            d['quadra'] = _quadra(somma, d['totale_stampato'])
            d['valore_ordine'] = d['totale_stampato'] if d['quadra'] else None
            logger.info('ordine_prezzi: %s n.%s, %d righe, somma %.2f, totale %s, quadra=%s',
                        d['formato'], d['numero_ordine'], len(d['righe']), somma,
                        d['totale_stampato'], d['quadra'])
            return d
    return None
