"""Svasature e filettature lette dai TESTI del disegno (callout dei fori).

Il callout del foro e' la fonte piu' affidabile: il disegnatore scrive quanti
fori e quale lavorazione ("6 x <> PASSANTE ⌵ Ø11.47 X 90°" = 6 svasature,
"4 x M8 - 6H" = 4 filettature). La geometria (cerchi concentrici, arco di 3/4)
resta per i disegni senza callout.

Fonti, in ordine:
- DXF: quote (DIMENSION, testo sostituito del callout di SolidWorks/Inventor),
  MTEXT/TEXT/ATTRIB/MULTILEADER; i testi vicini (callout esploso: "4 x" e
  "8,50 PASSANTE" separati) si riuniscono prima di leggerli;
- PDF del pezzo (stesso nome, stessa cartella) quando il DXF non ne ha.

Regole:
- conteggio = "N x" / "n° N" / "N fori" / "(x N)", altrimenti 1;
- svasatura: simbolo ⌵ (GDT "w"), "svasat…", oppure "Ø D X 90°" (82/90/100/120)
  in un callout di foro; la lamatura (⌴, GDT "v") e' lavorazione di macchina
  come la svasatura e si conta con le svasature;
- filettatura: M + diametro unificato (M6, M8x1.25, "M10 - 6H"), "G 1/4";
  NON se e' una vite/dado/inserto/rivetto/grano da montare o saldare, un
  foro "per M6"/"passaggio" (foro libero) o la misura della vite di una
  svasatura ("2 svasature M6");
- le note di revisione ("Aggiunte 4 svasature") non contano;
- i callout delle quote si sommano; descrizione del pezzo (cartiglio:
  "Piatto 100x50 sp8 - 4 x M8 int.50") e note col numero ripetono di solito
  i fori quotati: per ogni misura (M8, Ø18,25) vale il maggiore, non la somma;
- note senza numero ("attenzione alle svasature", "fori filettati"): la
  lavorazione c'e', il numero lo da' la geometria.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# Simboli normalizzati
SIM_SVAS = '⌵'      # countersink (GDT 'w')
SIM_LAMA = '⌴'      # counterbore (GDT 'v')
SIM_PROF = '↧'      # depth (GDT 'x')

# Diametri metrici unificati (M1,6 … M36): "M1:4", "M45" o "M7" non sono filetti
_DIAM_M = {'1.6', '2', '2.5', '3', '4', '5', '6', '8', '10', '12', '14', '16',
           '18', '20', '22', '24', '27', '30', '33', '36'}
_ANGOLI_SVAS = r'(?:82|90|100|120)'

_GDT = {'w': f' {SIM_SVAS} ', 'v': f' {SIM_LAMA} ', 'x': f' {SIM_PROF} ', 'n': 'Ø',
        'y': f' {SIM_SVAS} '}

# Font GDT di AutoCAD/SolidWorks: {\fGDT;w}  \fAIGDT|b0|i0;{\H1;w}  {\fgdt|c0;x}
_RE_GDT = re.compile(r'\\f[^;|]*gdt[^;]*;\s*(?:\{?\\H[^;]*;)?\s*([a-z])', re.I)

RE_REVISIONE = re.compile(
    r'^\W*(?:\d{1,2}\s+)?(?:mod\.|sot\.|rev\.)|'
    r'\b(?:aggiunt|aggiorn|modific|spostat|sostitu|elimin|tolt[oiae]\b|cambiat|inserit[oiae]\b|'
    r'variat|ridott|allungat|accorciat|added|moved|changed|removed|revisione)',
    re.I)
# minuterie e lavorazioni che NON sono un foro filettato del pezzo
RE_NON_FILETTO = re.compile(
    r'dad[oi]\b|rosett|rondell|\bvit[ei]\b|bullon|insert[oi]|rivett|\bgran[oi]\b|perno|perni|'
    r'prigion|saldar|saldat|ecrou|écrou|\bnuts?\b|\bbolts?\b|screw|washer|rivet|'
    r'\bpem\b|clinch|a tirare|tolleranz|thread class|\btc?ei\b|\btsei\b|\bt\.?e\.?\b|\bts\b|'
    r'passaggio|\bper\s+(?:vite\s+)?m\s?\d|clearance|\bisos?\s?\d{3,4}\b|\buni\s?\d{3,4}\b|\bdin\s?\d{2,4}\b',
    re.I)
RE_PAROLA_SVAS = re.compile(r'svas|countersink|\bcsk\b|fraisur|\bsenk', re.I)
RE_PAROLA_LAMA = re.compile(r'lamatur|lamare|counterbore|\bcbore\b', re.I)
RE_PAROLA_FIL = re.compile(r'filett|filetto|filetti|maschia|maschiare|\btapped\b|\bthread', re.I)

_NUM = r'\d+(?:[.,]\d+)?'
RE_M = re.compile(r'(?<![A-WYZa-wyz0-9])M\s?(\d{1,2}(?:[.,]\d)?)(?:\s*[xX×]\s*(\d(?:[.,]\d{1,2})?))?(?![0-9A-Za-z])')
RE_GAS = re.compile(r'(?<![A-Za-z0-9])G\s?(1/8|1/4|3/8|1/2|3/4)(?:"|”|\b)')
RE_ANGOLO_SVAS = re.compile(rf'(?:Ø|<>|\d)\s*[\d.,]*\s*[xX×]\s*{_ANGOLI_SVAS}\s*°')
RE_MISURA_SVAS = re.compile(rf'({_NUM})\s*[xX×]\s*{_ANGOLI_SVAS}\s*°')
RE_RIGA_SOLO_SVAS = re.compile(rf'^\s*Ø?\s*({_NUM})\s*[xX×]\s*{_ANGOLI_SVAS}\s*°\s*$')
RE_CONTESTO_FORO = re.compile(r'Ø|<>|passant|attravers|\bthru\b|through|\bfor[oi]\b|\bholes?\b', re.I)
# "N x" in testa al callout (prima riga), davanti a <>, Ø, M o a un numero
RE_N_TESTA = re.compile(r'^\s*(\d{1,3})\s*[xX×]\s*(?=<>|Ø|M\s?\d|\d|$)')
RE_N_DAVANTI = re.compile(r'(\d{1,3})\s*[xX×]\s*$')
RE_N_PAROLA = re.compile(
    r'(?:\bn\s*[°º.]?|\bnr\.?|\bnum\.?)\s*(\d{1,3})\b|'
    r'\b(\d{1,3})\s*(?:fori|holes|svasature|svasati|filetti|filettature|filettati|lamature)|'
    r'\(\s*[xX×]\s*(\d{1,3})\s*\)|\(\s*(\d{1,3})\s*[xX×]\s*\)', re.I)
# parole che possono stare in un callout di foro senza farne una descrizione
_PAROLE_CALLOUT = {
    'passante', 'passanti', 'attraverso', 'thru', 'all', 'through', 'hole', 'holes',
    'foro', 'fori', 'filettato', 'filettati', 'filettata', 'filettate', 'filettatura',
    'filettature', 'filetto', 'filetti', 'filettare', 'maschiare', 'maschiato', 'maschiati',
    'pass', 'prof', 'equidistanti', 'lato', 'opposto', 'opposite', 'side',
    'svasatura', 'svasature', 'svasato', 'svasati', 'svasata', 'svasate', 'svasare',
    'lamatura', 'lamature', 'anche', 'entrambi', 'lati', 'su', 'per', 'tutti', 'the', 'and',
    'csk', 'countersink', 'tapped', 'thread', 'deep', 'fino', 'fondo', 'cieco', 'ciechi',
}


def normalizza(raw: str) -> str:
    """Testo DXF/MTEXT → testo semplice con i simboli dei fori.

    \\P = a capo, {\\fGDT;w} = ⌵, %%c = Ø, %%d = °, \\U+XXXX decodificati,
    codici di formattazione MTEXT tolti."""
    if not raw:
        return ''
    s = str(raw)
    s = re.sub(r'\\U\+([0-9A-Fa-f]{4})', lambda m: chr(int(m.group(1), 16)), s)
    s = _RE_GDT.sub(lambda m: _GDT.get(m.group(1).lower(), ' '), s)
    s = s.replace('\\P', '\n').replace('^J', '\n').replace('\\~', ' ')
    s = re.sub(r'%%[cC]', 'Ø', s)
    s = re.sub(r'%%[dD]', '°', s)
    s = re.sub(r'%%[pP]', '±', s)
    s = re.sub(r'%%[uUoOkK]', '', s)
    s = s.replace('⌀', 'Ø').replace('∅', 'Ø').replace('ø', 'Ø').replace('\u2335', SIM_SVAS)
    s = s.replace('\u2334', SIM_LAMA).replace('\u21a7', SIM_PROF)
    s = re.sub(r'\\S([^;^/#]*)[\^/#]([^;]*);', r'\1/\2', s)          # frazioni impilate
    s = re.sub(r'\\[LlOoKk]', '', s)                                 # sottolineato/barrato on/off
    s = re.sub(r'\\[ACcFfHhQTWw][^;\\{}]*;', '', s)                  # \A1; \H2.5; \fArial|b0;
    s = s.replace('{', '').replace('}', '')
    s = s.replace('\r', '')
    righe = [' '.join(r.split()) for r in s.split('\n')]
    return '\n'.join(r for r in righe if r)


def _conta_esplicito(testo: str, pos: int, fine: int) -> int | None:
    """"N x" subito davanti alla specifica in testo[pos:fine], oppure
    "n° N"/"N fori"/"(x N)" sulla stessa riga; None se non c'e'."""
    inizio_riga = testo.rfind('\n', 0, pos) + 1
    fine_riga = testo.find('\n', fine)
    fine_riga = len(testo) if fine_riga < 0 else fine_riga
    m = RE_N_DAVANTI.search(testo[inizio_riga:pos])
    if m:
        return int(m.group(1))
    m = RE_N_PAROLA.search(testo[inizio_riga:fine_riga])
    if m:
        return int(next(g for g in m.groups() if g))
    return None


def _e_descrizione(testo: str) -> bool:
    """Testo con altre parole oltre al callout (descrizione del pezzo nel
    cartiglio: "Piatto 100x50 sp8 - 4 x M8 int.50"). Un callout di quota
    (valore misurato <>, simboli ⌵ ⌴ ↧) non e' mai una descrizione, anche con
    una parola in piu' ("…X 90°, Vicino")."""
    if '<>' in testo or SIM_SVAS in testo or SIM_LAMA in testo or SIM_PROF in testo:
        return False
    pulito = RE_M.sub(' ', testo)
    parole = re.findall(r'[A-Za-zÀ-ÿ]{3,}', pulito)
    return any(p.lower() not in _PAROLE_CALLOUT for p in parole)


def analizza_callout(testo: str) -> dict:
    """Legge UN callout (o una nota/descrizione) gia' normalizzato.

    Returns {'svasature': n, 'filettature': n, 'svas_nota': bool,
             'fil_nota': bool, 'descrizione': bool,
             'specifiche': [(tipo 'svas'|'fil', misura, n, numero_scritto)]}."""
    out = {'svasature': 0, 'filettature': 0, 'svas_nota': False, 'fil_nota': False,
           'descrizione': False, 'specifiche': []}
    t = normalizza(testo) if ('\\' in testo or '%%' in testo or '{' in testo) else testo
    t = t.strip()
    if not t or RE_REVISIONE.search(t):
        return out
    m_testa = RE_N_TESTA.match(t)
    n_testa = int(m_testa.group(1)) if m_testa else None
    if not n_testa:
        m = RE_N_PAROLA.search(t.split('\n', 1)[0])
        n_testa = int(next(g for g in m.groups() if g)) if m else None
    out['descrizione'] = _e_descrizione(t)

    # "lamature" scritto a parole vale come "svasature": nota, o numero se c'e'
    parola_svas = bool(RE_PAROLA_SVAS.search(t)) or bool(RE_PAROLA_LAMA.search(t))
    simbolo_svas = SIM_SVAS in t or SIM_LAMA in t
    angolo = RE_ANGOLO_SVAS.search(t)
    angolo_ok = bool(angolo) and (bool(RE_CONTESTO_FORO.search(t))
                                  or any(RE_RIGA_SOLO_SVAS.match(r) and ('.' in r or ',' in r)
                                         for r in t.split('\n')))

    # --- filettature ---
    non_filetto = bool(RE_NON_FILETTO.search(t)) or parola_svas
    if not non_filetto:
        visti = set()
        for m in list(RE_M.finditer(t)) + list(RE_GAS.finditer(t)):
            d = m.group(1).replace(',', '.')
            if m.re is RE_M and d not in _DIAM_M:
                continue
            if m.re is RE_M and (simbolo_svas or angolo_ok) and not m.group(2) \
                    and not re.match(r'\s*-\s*\d[HhGg]', t[m.end():]) and not RE_PAROLA_FIL.search(t):
                continue   # "M4 PASSANTE ⌵ Ø8.4 X 90°": vite della svasatura, non filetto
            if m.start() in visti:
                continue
            visti.add(m.start())
            n = _conta_esplicito(t, m.start(), m.end())
            if n is None and out['descrizione'] and not RE_PAROLA_FIL.search(t):
                continue   # "Piano scorrimento M3": nome del pezzo, non un foro filettato
            scritto = n is not None or n_testa is not None
            n = n if n is not None else (n_testa or 1)
            out['filettature'] += n
            misura = ('M' + d) if m.re is RE_M else ('G' + d)
            out['specifiche'].append(('fil', misura, n, scritto))
        if not out['filettature'] and RE_PAROLA_FIL.search(t):
            out['fil_nota'] = True

    # --- svasature (e lamature) ---
    n_svas, scritto = 0, False
    if simbolo_svas or angolo_ok:
        if n_testa:
            n_svas, scritto = n_testa, True
        else:
            pos = (angolo.start() if angolo else 0)
            n = _conta_esplicito(t, pos, pos)
            n_svas, scritto = (n, True) if n is not None else (1, False)
    elif parola_svas:
        m = RE_N_PAROLA.search(t)
        if m and not RE_REVISIONE.search(t):
            n_svas, scritto = int(next(g for g in m.groups() if g)), True
        elif n_testa:
            n_svas, scritto = n_testa, True
        else:
            out['svas_nota'] = True
    if n_svas:
        out['svasature'] = n_svas
        md = RE_MISURA_SVAS.search(t)
        out['specifiche'].append(('svas', md.group(1).replace(',', '.') if md else None, n_svas, scritto))
    return out


def _riunisci_righe(righe: list[str]) -> list[str]:
    """Righe di testo in callout: una riga che e' solo il conteggio ("4 x",
    "6 x 5,50 PASSANTE") si unisce alle righe successive che continuano il
    callout (filetto "M10 - 6H", svasatura "11,47 X 90°") — callout esplosi in
    piu' testi o letti dal PDF."""
    out = []
    i = 0
    while i < len(righe):
        r = righe[i]
        blocco = [r]
        if RE_N_TESTA.match(r) and not RE_M.search(r) and SIM_SVAS not in r \
                and not RE_ANGOLO_SVAS.search(r):
            j = i + 1
            while j < len(righe) and len(blocco) < 3:
                s = righe[j]
                if RE_N_TESTA.match(s):
                    break
                if RE_M.search(s) or RE_RIGA_SOLO_SVAS.match(s) or SIM_SVAS in s or SIM_LAMA in s \
                        or SIM_PROF in s or re.match(r'^\s*Ø?\s*' + _NUM + r'\s*(passant|attravers|thru)', s, re.I):
                    blocco.append(s)
                    j += 1
                else:
                    break
            i = j
        else:
            i += 1
        out.append('\n'.join(blocco))
    return out


def somma_callout(callouts: list) -> dict:
    """Totale dei callout di un disegno.

    `callouts`: testi, oppure coppie (testo, 'quota'|'testo').

    - callout delle QUOTE (uno per gruppo di fori) e testi senza numero
      ("M5" sulla freccia di un foro): si sommano;
    - testi col numero scritto fuori dalle quote ("8xM8 - 2xØ9" nel
      cartiglio, descrizione "Piatto … 4 x M8 int.50", note): ripetono di
      solito i fori gia' quotati. Per ogni misura (M8, Ø18,25…) vale il
      maggiore fra le quote e questi testi, non la somma."""
    tot = {'svasature': 0, 'filettature': 0, 'svas_nota': False, 'fil_nota': False}
    quote, altri = {}, {}
    for c in callouts:
        testo, origine = (c if isinstance(c, tuple) else (c, 'testo'))
        a = analizza_callout(testo)
        tot['svas_nota'] |= a['svas_nota']
        tot['fil_nota'] |= a['fil_nota']
        for tipo, misura, n, scritto in a['specifiche']:
            chiave = (tipo, misura)
            if origine == 'quota' or (not scritto and not a['descrizione']):
                quote[chiave] = quote.get(chiave, 0) + n
            else:
                altri[chiave] = max(altri.get(chiave, 0), n)
    for tipo, campo in (('svas', 'svasature'), ('fil', 'filettature')):
        misure = {k for k in set(quote) | set(altri) if k[0] == tipo and k[1] is not None}
        n = sum(max(quote.get(k, 0), altri.get(k, 0)) for k in misure) + quote.get((tipo, None), 0)
        # testo senza misura ("2 svasature M6 int.45"): vale per tutto il tipo
        tot[campo] = max(n, altri.get((tipo, None), 0))
    return tot


# ---------------------------------------------------------------------------
# DXF
# ---------------------------------------------------------------------------

def _testo_raw(e) -> str | None:
    t = e.dxftype()
    try:
        if t == 'MTEXT':
            return e.text
        if t in ('TEXT', 'ATTRIB'):
            return e.dxf.text
        if t == 'DIMENSION':
            s = e.dxf.get('text', '') or ''
            return s if s.strip() not in ('', '<>', ' ') else None
        if t == 'MULTILEADER':
            return e.context.mtext.default_content
    except Exception:
        return None
    return None


def _posizione(e):
    try:
        if e.dxftype() == 'DIMENSION':
            p = e.dxf.text_midpoint
        elif e.dxftype() == 'MULTILEADER':
            p = e.context.mtext.insert
        else:
            p = e.dxf.insert
        return float(p.x), float(p.y)
    except Exception:
        return None


def _altezza(e) -> float:
    try:
        if e.dxftype() == 'MTEXT':
            return float(e.dxf.get('char_height', 2.5) or 2.5)
        if e.dxftype() in ('TEXT', 'ATTRIB'):
            return float(e.dxf.get('height', 2.5) or 2.5)
    except Exception:
        pass
    return 2.5


_RE_SIGLA = re.compile(r'^[A-Z]{1,2}\d{1,3}$')
_RE_TITOLO_MISURA = re.compile(r'^(dimensione|dimensioni|size|dimension|descrizione|description|misura)$', re.I)


def _tabelle_fori(doc) -> tuple[list[tuple[str, str]], set]:
    """Tabelle fori (SolidWorks "Scheda | Loc X | Loc Y | Dimensione"): una
    riga per foro con la sigla (B1…B16), la misura scritta una volta sola
    nella cella unita del gruppo ("Ø5 PASSANTE / M6 - 6H PASSANTE" per 16
    fori, 25CCPA0245-00). Ogni misura vale per il gruppo di sigle (stessa
    lettera) alla sua altezza.

    Returns ([("16 x Ø 5 PASSANTE\\nM6 - 6H PASSANTE", 'quota'), …],
             id() degli INSERT delle tabelle, da non rileggere come testi)."""
    out, inserti = [], set()
    try:
        candidati = list(doc.modelspace().query('INSERT'))
    except Exception:
        return out, inserti
    for ins in candidati:
        try:
            blocco = doc.blocks.get(ins.dxf.name)
        except Exception:
            blocco = None
        if blocco is None:
            continue
        testi = []
        for e in blocco:
            if e.dxftype() not in ('TEXT', 'MTEXT'):
                continue
            raw = _testo_raw(e)
            pos = _posizione(e)
            if raw and pos is not None:
                testi.append((normalizza(raw), pos, _altezza(e)))
        titolo = [t for t in testi if _RE_TITOLO_MISURA.match(t[0])]
        if not titolo:
            continue
        # sigle a sinistra della colonna misura ("M6" nella colonna misura non e' una sigla)
        x_col = titolo[0][1][0] - 2 * titolo[0][2]
        sigle = [t for t in testi if _RE_SIGLA.match(t[0]) and t[1][0] < x_col]
        if len(sigle) < 2:
            continue
        celle = [t for t in testi if t[1][0] >= x_col and t is not titolo[0]]
        if not celle:
            continue
        # righe della colonna misura: testi alla stessa altezza, da sinistra a destra
        righe = []
        for t in sorted(celle, key=lambda t: (-t[1][1], t[1][0])):
            if righe and abs(righe[-1][0] - t[1][1]) <= 0.5 * t[2]:
                righe[-1][1].append(t[0])
            else:
                righe.append([t[1][1], [t[0]]])
        # gruppi di sigle per lettera, con l'intervallo di altezze
        gruppi = {}
        for t in sigle:
            lettera = re.match(r'[A-Z]+', t[0]).group(0)
            g = gruppi.setdefault(lettera, [float('inf'), float('-inf'), 0, t[2]])
            g[0] = min(g[0], t[1][1])
            g[1] = max(g[1], t[1][1])
            g[2] += 1
        per_gruppo = {}
        for y, parti in righe:
            riga = ' '.join(parti)
            if not (RE_M.search(riga) or 'Ø' in riga or SIM_SVAS in riga or RE_ANGOLO_SVAS.search(riga)
                    or re.search(r'passant|attravers|thru', riga, re.I)):
                continue      # coordinate (Loc X / Loc Y), titoli
            # gruppo che contiene l'altezza della riga (la cella unita e'
            # centrata sul gruppo), altrimenti il piu' vicino
            def distanza(item):
                lo, hi, _n, h = item[1]
                return 0.0 if lo - h <= y <= hi + h else min(abs(y - lo), abs(y - hi))
            lettera = min(gruppi.items(), key=distanza)[0]
            g = per_gruppo.setdefault(lettera, [])
            if riga not in g:          # misura ripetuta su ogni riga del gruppo
                g.append(riga)
        if not per_gruppo:
            continue
        inserti.add(id(ins))
        for lettera, righe_g in per_gruppo.items():
            # 'testo' col numero scritto: se gli stessi fori hanno anche il
            # callout sulla vista, per ogni misura vale il maggiore, non la somma
            out.append((f"{gruppi[lettera][2]} x " + '\n'.join(righe_g), 'testo'))
    return out, inserti


def callout_dxf(doc) -> list[tuple[str, str]]:
    """Testi del DXF che possono essere callout di fori, gia' normalizzati,
    con l'origine: 'quota' (DIMENSION/MULTILEADER) o 'testo'.

    Le quote valgono da sole (il callout e' tutto nel testo della quota); i
    testi semplici vicini si riuniscono (callout esploso); le tabelle fori
    contano una riga per foro."""
    from .dxf_polygon_detector_v3 import entita_espanse
    tabelle, inserti_tabella = _tabelle_fori(doc)
    quote, sparsi = list(tabelle), []
    msp = [e for e in doc.modelspace() if id(e) not in inserti_tabella] if inserti_tabella \
        else doc.modelspace()
    for e in entita_espanse(msp, solo_geometria=False):
        if e.dxftype() not in ('MTEXT', 'TEXT', 'ATTRIB', 'DIMENSION', 'MULTILEADER'):
            continue
        raw = _testo_raw(e)
        if not raw or not raw.strip():
            continue
        t = normalizza(raw)
        if not t:
            continue
        if e.dxftype() in ('DIMENSION', 'MULTILEADER'):
            quote.append((t, 'quota'))
            continue
        pos = _posizione(e)
        sparsi.append((t, pos, _altezza(e)))
    # testi semplici: righe vicine (stessa colonna, poche altezze di distanza)
    # ordinate dall'alto in basso, da sinistra a destra
    con_pos = [s for s in sparsi if s[1] is not None]
    senza = [(s[0], 'testo') for s in sparsi if s[1] is None]
    gruppi = _gruppi_vicini(con_pos)
    testi = list(quote) + senza
    for g in gruppi:
        g.sort(key=lambda s: (-round(s[1][1] / max(s[2], 1e-6)), s[1][0]))
        righe = []
        for s in g:
            righe.extend(s[0].split('\n'))
        blocchi = _riunisci_righe(righe) if len(g) > 1 else ['\n'.join(righe)]
        testi.extend((b, 'testo') for b in blocchi)
    return testi


def _gruppi_vicini(testi: list) -> list[list]:
    """Unisce i testi a meno di ~3 altezze di carattere l'uno dall'altro
    (solo quelli che sembrano pezzi di callout: conteggio, foro, filetto,
    svasatura), gli altri restano da soli."""
    def pezzo_callout(t):
        return bool(RE_N_TESTA.match(t) or RE_M.search(t) or RE_RIGA_SOLO_SVAS.match(t)
                    or SIM_SVAS in t or SIM_LAMA in t or SIM_PROF in t
                    or re.match(r'^\s*Ø?\s*' + _NUM + r'\s*(passant|attravers|thru)', t, re.I))
    cand = [i for i, s in enumerate(testi) if pezzo_callout(s[0]) and len(s[0]) < 60]
    padre = {i: i for i in cand}

    def trova(i):
        while padre[i] != i:
            padre[i] = padre[padre[i]]
            i = padre[i]
        return i
    for a in range(len(cand)):
        i = cand[a]
        (xi, yi), hi = testi[i][1], testi[i][2]
        for b in range(a + 1, len(cand)):
            j = cand[b]
            (xj, yj), hj = testi[j][1], testi[j][2]
            h = max(hi, hj, 1e-6)
            if abs(yi - yj) <= 2.2 * h and abs(xi - xj) <= 12 * h:
                padre[trova(i)] = trova(j)
    per_rad = {}
    for i in cand:
        per_rad.setdefault(trova(i), []).append(testi[i])
    altri = [[s] for k, s in enumerate(testi) if k not in padre]
    return list(per_rad.values()) + altri


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def pdf_gemello(dxf_path: str) -> str | None:
    """PDF del pezzo: stesso nome del DXF nella stessa cartella (anche con
    " (1)" aggiunto al salvataggio)."""
    try:
        cartella = os.path.dirname(os.path.abspath(dxf_path))
        stem = os.path.splitext(os.path.basename(dxf_path))[0].strip().lower()
        stem = re.sub(r'\s*\(\d+\)$', '', stem)
        for f in os.listdir(cartella):
            b, ext = os.path.splitext(f)
            if ext.lower() != '.pdf':
                continue
            if re.sub(r'\s*\(\d+\)$', '', b.strip().lower()) == stem:
                return os.path.join(cartella, f)
    except Exception:
        return None
    return None


def righe_pdf(pdf_path: str, max_pagine: int = 3) -> list[str]:
    """Righe di testo del PDF (pypdfium2, veloce; pdfplumber se manca)."""
    testo = ''
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(pdf_path)
        try:
            for i in range(min(len(pdf), max_pagine)):
                pg = pdf[i]
                tp = pg.get_textpage()
                testo += tp.get_text_range() + '\n'
                tp.close()
                pg.close()
        finally:
            pdf.close()
    except Exception:
        try:
            import pdfplumber
            with pdfplumber.open(pdf_path) as p:
                for pg in p.pages[:max_pagine]:
                    testo += (pg.extract_text() or '') + '\n'
        except Exception as e:
            logger.debug('PDF %s non leggibile: %s', pdf_path, e)
            return []
    return [' '.join(r.split()) for r in testo.replace('\r', '').split('\n') if r.strip()]


def callout_pdf(pdf_path: str) -> list[str]:
    return _riunisci_righe(righe_pdf(pdf_path))


def lavorazioni_da_testi(doc, dxf_path: str | None = None) -> dict:
    """Svasature/filettature scritte nel disegno (DXF, poi PDF gemello).

    Returns {'svasature', 'filettature', 'svas_nota', 'fil_nota', 'fonte'}
    con fonte 'dxf' | 'pdf' | None."""
    vuoto = {'svasature': 0, 'filettature': 0, 'svas_nota': False, 'fil_nota': False, 'fonte': None}
    try:
        r = somma_callout(callout_dxf(doc))
    except Exception as e:
        logger.debug('callout DXF non letti: %s', e)
        r = dict(vuoto)
    r['fonte'] = 'dxf' if (r['svasature'] or r['filettature']) else None
    if not (r['svasature'] or r['filettature']) and dxf_path:
        pdf = pdf_gemello(dxf_path)
        if pdf:
            try:
                rp = somma_callout(callout_pdf(pdf))
            except Exception as e:
                logger.debug('callout PDF non letti: %s', e)
                rp = None
            if rp and (rp['svasature'] or rp['filettature'] or rp['svas_nota'] or rp['fil_nota']):
                rp['svas_nota'] |= r['svas_nota']
                rp['fil_nota'] |= r['fil_nota']
                rp['fonte'] = 'pdf' if (rp['svasature'] or rp['filettature']) else None
                r = rp
    return r
