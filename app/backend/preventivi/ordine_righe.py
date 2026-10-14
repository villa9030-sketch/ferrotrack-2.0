"""Le righe dell'ordine del cliente e la SEQUENZA in cui elencare i pezzi.

Stefano (14/10/2026): "ti avevo detto di rispettare i PDF, ancora
nell'ultimo preventivo gli assiemi erano tutti alla fine". Il PDF del
preventivo, la pagina e la distinta dell'ordine seguono le righe dell'ordine
del cliente: gli assiemi stanno alla loro riga, i loro componenti (senza riga
propria) subito sotto, i disegni che l'ordine non cita in fondo, segnati.

- `righe_ordine_pdf`: tutte le righe del PDF "Ordine Fornitore" (DECA) nel
  loro ordine, con la posizione (1, 2, 3...). Servono anche le righe SENZA
  disegno (guide in plastica, commerciali): nel preventivo devono esserci.
- `chiave` / `chiave_variante`: lo stesso codice a meno di estensione,
  revisione (-00), copia " (2)", "_cleaned" e variante "-G-" (grezzo:
  191700609-G-00 e' il pezzo del disegno 191700609-00).
- `sequenza`: in che ordine elencare pezzi sciolti e assiemi.

Nessuna dipendenza da Flask o dal database.
"""
from __future__ import annotations

import os
import re


def chiave(s) -> str:
    """'46SA00578-00.dxf' / '46SA00578' / '46SA00578-00 (2)' → '46SA00578'."""
    b = os.path.splitext(os.path.basename(str(s or '')))[0] if re.search(r'\.[A-Za-z]{3,4}$', str(s or '')) \
        else os.path.basename(str(s or ''))
    b = b.upper().strip()
    b = re.sub(r'\s*\(\d+\)$', '', b)
    b = re.sub(r'_CLEANED$', '', b)
    return re.sub(r'[-_]\d{2}$', '', b)


def chiave_variante(s) -> str:
    """Come `chiave`, senza la lettera di variante DECA: '191700609-G-00' →
    '191700609' (la G e' il pezzo grezzo, stesso disegno)."""
    return re.sub(r'-[A-Z]$', '', chiave(s))


def righe_ordine_pdf(pdf_bytes: bytes) -> list | None:
    """[{pos, codice, descrizione, quantita, commessa}] di un "Ordine Fornitore"
    leggibile dal testo, nell'ordine del PDF; None se il formato non e' quello."""
    from .ordine_testo import leggi_ordine_fornitore
    try:
        d = leggi_ordine_fornitore(pdf_bytes)
    except Exception:
        return None
    if not d or not d.get('articoli'):
        return None
    return [{'pos': i + 1, 'codice': a.get('codice') or '',
             'descrizione': (a.get('descrizione') or '')[:200],
             'quantita': a.get('quantita') or 1,
             'commessa': a.get('_commessa')}
            for i, a in enumerate(d['articoli'])]


def indice_righe(righe: list) -> tuple[dict, dict]:
    """({chiave: [righe]}, {chiave_variante: [righe]}) per cercare un codice."""
    per_k, per_v = {}, {}
    for r in righe or []:
        per_k.setdefault(chiave(r.get('codice')), []).append(r)
        per_v.setdefault(chiave_variante(r.get('codice')), []).append(r)
    return per_k, per_v


def righe_del_codice(codice, idx: tuple[dict, dict]) -> list:
    """Le righe dell'ordine di un codice: prima uguale, poi variante ("-G-")."""
    per_k, per_v = idx
    return per_k.get(chiave(codice)) or per_v.get(chiave_variante(codice)) or []


def _num(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def pos_articolo(a: dict):
    """Posizione nell'ordine di un pezzo: la sua prima riga (None se il pezzo
    non e' nell'ordine o l'ordine non e' stato letto)."""
    o = a.get('ordine') if isinstance(a.get('ordine'), dict) else None
    if not o or o.get('stato') == 'non_trovato':
        return None
    pp = [_num(r.get('pos')) for r in (o.get('righe') or []) if isinstance(r, dict)]
    pp = [p for p in pp + [_num(o.get('pos'))] if p is not None]
    return min(pp) if pp else None


def sequenza(articoli: list, assiemi: list, righe: list | None = None) -> list[dict]:
    """In che ordine elencare le righe di un preventivo.

    articoli: pezzi nell'ordine della lista (quella salvata); i componenti
    (codice_assieme) non hanno una riga loro: stanno sotto il loro assieme.
    assiemi: codici degli assiemi, nell'ordine in cui sono salvati.
    righe: righe dell'ordine del cliente (righe_ordine_pdf), se note.

    Ritorna [{'tipo': 'articolo', 'indice': i} | {'tipo': 'assieme',
    'codice': c}, con 'pos' e 'fuori_ordine'}]. Senza alcuna posizione (ordine
    mai letto) resta com'era: pezzi sciolti poi assiemi, fuori_ordine False.
    """
    idx = indice_righe(righe) if righe else None
    voci = []
    ultimo_pos = None
    pos_ass_lista = {}
    for i, a in enumerate(articoli or []):
        ca = a.get('codice_assieme')
        if ca:
            # assieme senza riga nota: dove sono i suoi pezzi nella lista
            # (la pagina li mette subito dopo la riga dell'assieme)
            if ca not in pos_ass_lista:
                pos_ass_lista[ca] = (ultimo_pos if ultimo_pos is not None else 0) + 0.5
            continue
        o = a.get('ordine') if isinstance(a.get('ordine'), dict) else {}
        if idx:
            # righe salvate: la posizione e' QUELLA riga (1, 2, 3...). Le pos
            # scritte sui pezzi dei preventivi di prima sono righe di testo del
            # PDF, un'altra scala: mescolarle sposterebbe gli assiemi.
            rr = [] if (o.get('stato') == 'non_trovato'
                        or re.search(r'_cleaned$', str(a.get('codice') or ''), re.I)) else \
                (righe_del_codice(o.get('codice'), idx) if o.get('codice') else []) or righe_del_codice(a.get('codice'), idx)
            p = min(r['pos'] for r in rr) if rr else None
        else:
            p = pos_articolo(a)
        if p is not None:
            ultimo_pos = p
        voci.append({'tipo': 'articolo', 'indice': i, 'pos': p, '_ord': i})
    for j, c in enumerate(assiemi or []):
        p = None
        if idx:
            rr = righe_del_codice(c, idx)
            p = min(r['pos'] for r in rr) if rr else None
        voci.append({'tipo': 'assieme', 'codice': c, 'pos': p, '_ord': 10 ** 6 + j,
                     '_pos_lista': pos_ass_lista.get(c)})
    noto = any(v['pos'] is not None for v in voci)
    if not noto:
        for v in voci:
            v['fuori_ordine'] = False
            v.pop('_pos_lista', None)
        return sorted(voci, key=lambda v: v['_ord'])
    for v in voci:
        if v['pos'] is None and v.get('_pos_lista') is not None and not idx:
            # ordine letto ma righe non salvate (preventivi di prima): l'assieme
            # sta dove la lista mette i suoi componenti
            v['pos'] = v['_pos_lista']
        v.pop('_pos_lista', None)
        v['fuori_ordine'] = v['pos'] is None
    dentro = sorted((v for v in voci if v['pos'] is not None), key=lambda v: (v['pos'], v['_ord']))
    fuori = sorted((v for v in voci if v['pos'] is None), key=lambda v: v['_ord'])
    return dentro + fuori


def righe_senza_pezzo(righe: list, articoli: list, assiemi: list) -> list:
    """Righe dell'ordine che nel preventivo non ci sono (ne' pezzo ne' assieme)."""
    presenti_k, presenti_v = set(), set()
    for a in articoli or []:
        o = a.get('ordine') if isinstance(a.get('ordine'), dict) else {}
        for c in (a.get('codice'), o.get('codice')):
            if c:
                presenti_k.add(chiave(c))
                presenti_v.add(chiave_variante(c))
    for c in assiemi or []:
        presenti_k.add(chiave(c))
        presenti_v.add(chiave_variante(c))
    out, visti = [], set()
    for r in righe or []:
        k = chiave(r.get('codice'))
        if k in presenti_k or chiave_variante(r.get('codice')) in presenti_v or k in visti:
            continue
        visti.add(k)
        out.append(r)
    return out
