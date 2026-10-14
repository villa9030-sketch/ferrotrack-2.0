"""Distinta base letta dal PDF di un disegno d'insieme (assieme o sotto-assieme).

Tabella tipo DECA ("Num. art. | Codice | REV | Descrizione | ... | Materiale |
Lunghezza | ANGOLO1 | ANGOLO2 | Qta"): per ogni riga posizione, codice,
descrizione, lunghezza di taglio, angoli e quantita'. Le righe con un profilo
riconoscibile (tubo quadro/rettangolare/tondo, angolare, piatto) e una
lunghezza diventano TUBOLARI con peso al metro dal catalogo; le altre sono
COMPONENTI (lamiere, sotto-assiemi) di cui si usa la quantita'.

Esempio (DECA 46SA00578 "Telaio 1040x790"): 9 righe di tubo quadro
80x80x3 / 60x60x3 con lunghezze e angoli 0/45; 46SA00579: telaio x1,
piastra piede 1901K101 x4, piastra superiore x1.

Solo lettura: non tocca il riconoscimento dei contorni.
"""
from __future__ import annotations

import json
import math
import os
import re

_POS = re.compile(r'^\d{1,3}(?:\.\d{1,3})*$')
_NUM = re.compile(r'^\d+(?:[.,]\d+)?$')
_ANG = re.compile(r'^(\d{1,3}(?:[.,]\d+)?)\s*(?:°|º|�|deg)?$')
_CATALOGO = None


def _catalogo():
    global _CATALOGO
    if _CATALOGO is None:
        p = os.path.join(os.path.dirname(__file__), 'profili_tubolari.json')
        try:
            with open(p, encoding='utf-8') as f:
                _CATALOGO = json.load(f)
        except Exception:
            _CATALOGO = {}
    return _CATALOGO


def _f(s) -> float:
    return float(str(s).replace(',', '.'))


def profilo_da_descrizione(desc: str) -> dict | None:
    """'T. quadrato saldato 80 X 80 X 3' → {tipo:'SHS', nome:'Quadro 80x80 sp.3mm',
    kg_m: 7.07, ...}; None se la descrizione non e' un profilo da barra."""
    d = ' '.join(str(desc or '').split())
    u = d.upper()
    num = r'(\d+(?:[.,]\d+)?)'
    x = r'\s*[X×*]\s*'
    tre = re.search(num + x + num + x + num, u)
    due = re.search(num + x + num, u)
    cat = _catalogo()

    def dal_catalogo(fam, **dim):
        for p in cat.get(fam, []):
            if all(abs(float(p.get(k, -1)) - v) < 0.05 for k, v in dim.items()):
                return p
        return None

    if re.search(r'\bT\.?\s*QUADR|\bTUBO\s+QUADR|\bQUADRO\b|\bSHS\b', u) and tre:
        a, b, t = _f(tre.group(1)), _f(tre.group(2)), _f(tre.group(3))
        p = dal_catalogo('SHS' if abs(a - b) < 0.01 else 'RHS', lato_a=max(a, b), lato_b=min(a, b), spessore=t)
        kg = p['peso_kg_m'] if p else (2 * t * (a + b - 2 * t)) * 7.85e-3
        return {'tipo': 'SHS' if abs(a - b) < 0.01 else 'RHS', 'nome': (p or {}).get('nome') or f'Tubo {a:g}x{b:g} sp.{t:g}',
                'kg_m': round(kg, 3), 'da_catalogo': bool(p)}
    # "T.RettSaldato Aisi304 150x 50x 3" (DECA 26CCSA0040): anche T.Rett...
    if re.search(r'\bT\.?\s*RETT|\bTUBO\s+RETT|\bRETT\.|\bRHS\b', u) and tre:
        a, b, t = _f(tre.group(1)), _f(tre.group(2)), _f(tre.group(3))
        p = dal_catalogo('RHS', lato_a=max(a, b), lato_b=min(a, b), spessore=t)
        kg = p['peso_kg_m'] if p else (2 * t * (a + b - 2 * t)) * 7.85e-3
        return {'tipo': 'RHS', 'nome': (p or {}).get('nome') or f'Tubo {max(a,b):g}x{min(a,b):g} sp.{t:g}',
                'kg_m': round(kg, 3), 'da_catalogo': bool(p)}
    if re.search(r'\bT\.?\s*TOND|\bTUBO\s+TOND|\bCHS\b', u) and due:
        dd, t = _f(due.group(1)), _f(due.group(2))
        p = dal_catalogo('CHS', d_ext=dd, spessore=t)
        kg = p['peso_kg_m'] if p else math.pi * t * (dd - t) * 7.85e-3
        return {'tipo': 'CHS', 'nome': (p or {}).get('nome') or f'Tondo Ø{dd:g} sp.{t:g}',
                'kg_m': round(kg, 3), 'da_catalogo': bool(p)}
    if re.search(r'^\s*ANGOLAR|\bL\s*\d', u) and tre:
        a, b, t = _f(tre.group(1)), _f(tre.group(2)), _f(tre.group(3))
        return {'tipo': 'L', 'nome': f'Angolare {a:g}x{b:g}x{t:g}', 'kg_m': round(t * (a + b - t) * 7.85e-3, 3),
                'da_catalogo': False}
    if re.search(r'^\s*PIATT', u) and due and not re.search(r'\bSP\s*\.?\s*\d', u):
        a, t = _f(due.group(1)), _f(due.group(2))
        return {'tipo': 'FLAT', 'nome': f'Piatto {a:g}x{t:g}', 'kg_m': round(a * t * 7.85e-3, 3), 'da_catalogo': False}
    return None


# Il materiale per intero ("AISI 304", "Aisi304", "S235JR"): il prezzo dei
# tubi dipende dal materiale (inox ≠ ferro)
_RX_MAT = re.compile(r'S235\w*|S275\w*|S355\w*|1\.[04]\d{3}|INOX(?:\s*\d{3}L?)?|AISI\s*\d{3}L?|AISI|ALLUMIN\w*|ALU\w*', re.I)


def _riga_da_celle(celle: list) -> dict | None:
    """Una riga della tabella dei componenti (celle da sinistra a destra) o
    None se non e' una riga di componente. Tabella DECA: "Num. art. | Codice |
    REV | Descrizione | Descrizione Aggiuntiva | Materiale | Trattamento |
    Qta" (anche con Lunghezza e angoli per i tubi); il codice e' senza la
    revisione (191700045 con REV 00 = il disegno 191700045-00)."""
    celle = [' '.join(str(c).split()) if c is not None else '' for c in (celle or [])]
    pos_i = next((i for i, c in enumerate(celle[:4]) if _POS.match(c)), None)
    if pos_i is None:
        return None
    resto = celle[pos_i + 1:]
    codice = next((c for c in resto if c), '')
    # codice vero: lettere e cifre (46SA00578, 80105L0EB0067) o solo
    # cifre ma lungo (250200046). Scarta le tabelle fori ("827,50")
    # e la riga di revisione ("00 EMISSIONE").
    if not codice or ' ' in codice or ',' in codice or len(codice) < 5:
        return None
    if not ((re.search(r'\d', codice) and re.search(r'[A-Za-z]', codice))
            or re.fullmatch(r'\d{7,}', codice)):
        return None
    dopo = resto[resto.index(codice) + 1:]
    # la colonna REV subito dopo il codice ("00", "01"): non e' una lunghezza
    piene = [c for c in dopo if c]
    rev = None
    if piene and re.fullmatch(r'\d{2}', piene[0]):
        rev = piene[0]
        dopo = dopo[dopo.index(rev) + 1:]
    testi = [c for c in dopo if c and re.search(r'[A-Za-z]', c)]
    # materiale: la cella corta col nome del materiale ("Aisi304", "AISI 304");
    # "inferiore MTR - Lg 581 - AISI304" e' la descrizione aggiuntiva
    corti = [c for c in testi if _RX_MAT.search(c) and len(c) <= 14]
    mat = corti[-1] if corti else next((c for c in testi if _RX_MAT.search(c)), '')
    altri = [c for c in testi if c != mat]
    descr = altri[0] if altri else ''
    agg = altri[1] if len(altri) > 1 else ''
    numeri = [c for c in dopo if _NUM.match(c)]
    angoli = [c for c in dopo if c and _ANG.match(c) and not _NUM.match(c)]
    qta = int(_f(numeri[-1])) if numeri and float(_f(numeri[-1])).is_integer() else 1
    lung = _f(numeri[0]) if len(numeri) >= 2 else None
    if not lung:
        lung = None                         # "00" della revisione non e' una lunghezza
    return {
        'pos': celle[pos_i], 'codice': codice, 'rev': rev, 'descrizione': descr, 'descrizione_agg': agg,
        'materiale': mat, 'lunghezza_mm': lung, 'qta': qta,
        'angolo_1': _f(_ANG.match(angoli[0]).group(1)) if len(angoli) >= 1 else None,
        'angolo_2': _f(_ANG.match(angoli[1]).group(1)) if len(angoli) >= 2 else None,
    }


# Riga della distinta scritta come TESTO (nessuna tabella: DECA 26CCSA0038,
# "1 80105L2CB0055 00 T.Quadro Aisi304 50x 50x 3 L. 581 AISI 304 1"):
# posizione (a volte ripetuta: "1 1 80105..."), codice, revisione,
# descrizione, quantita'.
_RIGA_TESTO = re.compile(r'^(?:\d{1,3}\s+)?(\d{1,3})\s+([A-Z0-9][A-Z0-9.\-]{4,})\s+(\d{2})\s+(.+?)\s+(\d{1,4})$')
# Lunghezza di taglio scritta nella descrizione: "L. 581", "Lg 895", "Lg.0500"
_LUNG_TESTO = re.compile(r'\bL(?:G|UNG)?\s*\.?\s*(\d{2,5})\b(?!\s*[xX×*]\s*\d)', re.I)


def lunghezza_da_testo(*testi) -> float | None:
    """'Longherone telaio inferiore MTR Lg 895' → 895; None se non c'e'."""
    for t in testi:
        m = _LUNG_TESTO.search(str(t or ''))
        if m and float(m.group(1)) > 0:
            return float(m.group(1))
    return None


def _righe_testo(testo: str) -> list:
    """Righe della distinta dal testo, dopo l'intestazione "Num. art.":
    quando pdfplumber mette tutta la tabella in una cella sola."""
    out, dentro = [], False
    for riga in (testo or '').splitlines():
        r = ' '.join(riga.split())
        if re.search(r'Num\.?\s*art', r, re.I):
            dentro = True
            continue
        if not dentro:
            continue
        m = _RIGA_TESTO.match(r)
        if not m:
            if out:
                break
            continue
        codice = m.group(2)
        if not (re.search(r'\d', codice) and re.search(r'[A-Za-z]', codice)) and not re.fullmatch(r'\d{7,}', codice):
            continue
        descr = m.group(4)
        mat = _RX_MAT.findall(descr)
        out.append({'pos': m.group(1), 'codice': codice, 'rev': m.group(3), 'descrizione': descr,
                    'descrizione_agg': '', 'materiale': mat[-1] if mat else '',
                    'lunghezza_mm': lunghezza_da_testo(descr), 'qta': int(m.group(5)),
                    'angolo_1': None, 'angolo_2': None, 'da_testo': True})
    return out


def _una_per_posizione(righe: list) -> list:
    # la stessa tabella puo' uscire due volte: una riga per posizione
    visti, out = set(), []
    for r in righe:
        if r['pos'] in visti:
            continue
        visti.add(r['pos'])
        out.append(r)
    return out


def _righe_tabella(pdf) -> list:
    righe = []
    for pg in pdf.pages:
        for tb in pg.extract_tables() or []:
            for r in tb:
                x = _riga_da_celle(r)
                if x:
                    righe.append(x)
    return _una_per_posizione(righe)


def leggi_distinta_dxf(sorgente) -> dict | None:
    """La tabella dei componenti scritta nel DXF dell'assieme (percorso o
    bytes): le scritte (anche dentro i blocchi, es. SW_TABLEANNOTATION di
    SolidWorks) si mettono in righe per altezza e in celle da sinistra a
    destra, poi come la tabella del PDF. None se non c'e'."""
    try:
        import ezdxf
        if isinstance(sorgente, (bytes, bytearray)):
            # dal pacchetto, in memoria: ezdxf legge la codifica giusta solo da file
            import tempfile
            fd, tmp = tempfile.mkstemp(suffix='.dxf')
            try:
                with os.fdopen(fd, 'wb') as fh:
                    fh.write(bytes(sorgente))
                doc = ezdxf.readfile(tmp)
            finally:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        else:
            doc = ezdxf.readfile(sorgente)
    except Exception:
        return None

    def scritte(entita):
        out = []
        for e in entita:
            t = e.dxftype()
            if t not in ('TEXT', 'MTEXT', 'ATTRIB'):
                continue
            try:
                testo = e.plain_text() if t == 'MTEXT' else e.dxf.text
                h = float(e.dxf.char_height if t == 'MTEXT' else e.dxf.height) or 1.0
                out.append((float(e.dxf.insert.y), float(e.dxf.insert.x), h, ' '.join(str(testo).split())))
            except Exception:
                continue
        return out

    gruppi = []
    msp = doc.modelspace()
    gruppi.append(scritte(msp))
    for ins in msp.query('INSERT'):
        try:
            gruppi.append(scritte(ins.virtual_entities()) + scritte(ins.attribs))
        except Exception:
            continue
    righe = []
    for sc in gruppi:
        sc = [x for x in sc if x[3]]
        sc.sort(key=lambda x: -x[0])
        riga, y0 = [], None
        for x in sc + [None]:
            if x is not None and (y0 is None or abs(x[0] - y0) <= 0.5 * x[2]):
                riga.append(x)
                y0 = x[0] if y0 is None else y0
                continue
            if riga:
                r = _riga_da_celle([c[3] for c in sorted(riga, key=lambda c: c[1])])
                if r:
                    righe.append(r)
            riga, y0 = ([x], x[0]) if x is not None else ([], None)
    righe = _una_per_posizione(righe)
    if not righe:
        return None
    for r in righe:
        prof = profilo_da_descrizione(r['descrizione'])
        if prof and r.get('lunghezza_mm'):
            r['profilo'] = prof
    nome = os.path.basename(sorgente) if isinstance(sorgente, str) else None
    return {'file': nome, 'peso_kg': None, 'righe': righe, 'da': 'dxf'}


def _materiale_cartiglio(testo: str) -> str | None:
    """Il materiale scritto nel cartiglio: riga "Materiale Peso Kg ..." e sotto
    "Aisi304 2.496" (DECA) → "Aisi304"; None se non c'e'."""
    righe = testo.splitlines()
    for i, riga in enumerate(righe):
        if re.match(r'\s*Materiale\b.*Peso', riga, re.I) and i + 1 < len(righe):
            m = re.match(r'\s*(.*?[A-Za-z].*?)\s+\d+(?:[.,]\d+)?\b', righe[i + 1] + ' 0')
            if m and len(m.group(1)) <= 24:
                return m.group(1).strip()
    return None


def _peso_cartiglio(testo: str) -> float | None:
    righe = testo.splitlines()
    for i, riga in enumerate(righe):
        if re.search(r'Peso\s*Kg', riga, re.I) and i + 1 < len(righe):
            m = re.findall(r'(\d+[.,]\d+|\d+)', righe[i + 1])
            if m:
                try:
                    return _f(m[-1])
                except ValueError:
                    return None
    return None


_CACHE: dict = {}


def chiave_codice(s: str) -> str:
    """'46SA00578-00.pdf' / '46SA00578' → '46SA00578' (senza estensione e revisione)."""
    b = os.path.splitext(os.path.basename(str(s or '')))[0].upper().strip()
    b = re.sub(r'\s*\(\d+\)$', '', b)                  # copia: "x (2).pdf"
    return re.sub(r'[-_]\d{2}$', '', b)


def distinte_per_codici(cartella: str, codici: list, profondita: int = 3) -> dict:
    """Distinte dei codici richiesti (assiemi) e, a cascata, dei sotto-assiemi
    citati nelle loro righe che hanno un PDF con distinta nella cartella."""
    if not os.path.isdir(cartella):
        return {}
    pdf = {}
    for f in os.listdir(cartella):
        if f.lower().endswith('.pdf'):
            pdf.setdefault(chiave_codice(f), os.path.join(cartella, f))
    out, coda = {}, [(chiave_codice(c), 0) for c in codici if c]
    while coda:
        k, liv = coda.pop(0)
        if k in out or k not in pdf:
            continue
        p = pdf[k]
        try:
            st = os.stat(p)
            firma = (p, st.st_mtime, st.st_size)
        except OSError:
            continue
        if firma not in _CACHE:
            if len(_CACHE) > 300:      # tetto: non cresce per sempre
                _CACHE.clear()
            _CACHE[firma] = leggi_distinta(p)
        d = _CACHE[firma]
        if not d:
            continue
        out[k] = d
        if liv < profondita:
            for r in d['righe']:
                kr = chiave_codice(r['codice'])
                if kr != k:
                    coda.append((kr, liv + 1))
    return out


def leggi_distinta(pdf_path: str) -> dict | None:
    """{peso_kg, righe:[{pos, codice, descrizione, qta, lunghezza_mm, angoli,
    profilo?}]} o None se il PDF non ha una distinta."""
    try:
        import pdfplumber
        with pdfplumber.open(pdf_path) as pdf:
            righe = _righe_tabella(pdf)
            testo = '\n'.join((p.extract_text() or '') for p in pdf.pages[:2])
    except Exception:
        return None
    if not righe:
        # la tabella non esce come tabella (tutta in una cella): dal testo
        righe = _una_per_posizione(_righe_testo(testo))
    if not righe:
        return None
    for r in righe:
        if not r.get('lunghezza_mm'):
            # "Lg 581" scritto nella descrizione (DECA 26T30SA0233: le righe
            # dei sotto-assiemi di tubo non hanno la colonna Lunghezza)
            r['lunghezza_mm'] = lunghezza_da_testo(r.get('descrizione'), r.get('descrizione_agg'))
        prof = profilo_da_descrizione(r['descrizione'])
        if prof and r.get('lunghezza_mm'):
            r['profilo'] = prof
    return {'file': os.path.basename(pdf_path), 'peso_kg': _peso_cartiglio(testo),
            'materiale': _materiale_cartiglio(testo), 'righe': righe}
