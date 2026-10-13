"""Da procurare: i pezzi che non si tagliano al laser (Stefano, 12/10/2026).

Negli assiemi ci sono pezzi che il laser non fa: tubi e profili (comprati in
barre da 6 m e tagliati in casa), tubi tagliati al laser tubo da un
fornitore, meccanica (tornitura, fresatura), commerciali da comprare finiti.
Prima il tasto N ("non e' da laser") li toglieva da Lantek e basta: nessuno
si ricordava di procurarli. Ora:

  - chi dice N li classifica (T tubo, L laser tubo, M meccanica,
    C commerciale, A altro) e diventano una voce "da procurare";
  - le righe tubolari della distinta (preventivo) ci finiscono da sole:
    T, oppure L se hanno la "Lav. esterna" del preventivatore;
  - i tubi si sommano per profilo e materiale, in barre da 6 m (piano di
    taglio first-fit decreasing, con la lama);
  - si prepara la richiesta d'offerta (PDF + disegni dei pezzi) e si segna
    a mano ordinato / arrivato (con annulla e storico).

Qui: lettura dei profili dalle descrizioni, proposta per il tasto N, piano
delle barre, database (tabella da_procurare), file dei pezzi e la richiesta
d'offerta. Le rotte stanno in api_procura.py. Niente Lantek.
"""
from __future__ import annotations

import io
import json
import logging
import math
import os
import re
import threading
import time
import zipfile
from datetime import date, datetime

logger = logging.getLogger(__name__)

TIPI = {'tubo': 'Tubo/profilo da ordinare', 'laser_tubo': 'Taglio laser tubo (fornitore)',
        'meccanica': 'Meccanica (fornitore)', 'commerciale': 'Commerciale (da comprare)', 'altro': 'Altro'}
TIPI_BREVI = {'tubo': 'Tubo/profilo', 'laser_tubo': 'Laser tubo', 'meccanica': 'Meccanica',
              'commerciale': 'Commerciale', 'altro': 'Altro'}
# i tasti del controllo dei disegni (laser-controllo.js)
TASTI = {'t': 'tubo', 'l': 'laser_tubo', 'm': 'meccanica', 'c': 'commerciale', 'a': 'altro'}
STATI = ('da_ordinare', 'ordinato', 'arrivato')
# lavorazione fatta fuori: per il "taglia entro" contano come lavorazione esterna
TIPI_ESTERNI = {'laser_tubo': 'laser tubo dal fornitore', 'meccanica': 'meccanica dal fornitore'}

BARRA_MM_DEFAULT = 6000.0
LAMA_MM_DEFAULT = 3.0


# ---------------------------------------------------------------------------
# Impostazioni (app_config.json, "procura": {"barra_mm": 6000, "lama_mm": 3})
# ---------------------------------------------------------------------------
def config() -> dict:
    try:
        from .database import ConfigManager
        c = (ConfigManager.load_config() or {}).get('procura') or {}
    except Exception:            # noqa: BLE001
        c = {}
    out = {'barra_mm': BARRA_MM_DEFAULT, 'lama_mm': LAMA_MM_DEFAULT}
    try:
        b = float(c.get('barra_mm'))
        if 500 <= b <= 24000:
            out['barra_mm'] = b
    except (TypeError, ValueError):
        pass
    try:
        k = float(c.get('lama_mm'))
        if 0 <= k <= 20:
            out['lama_mm'] = k
    except (TypeError, ValueError):
        pass
    return out


# ---------------------------------------------------------------------------
# Profili scritti a parole: "Tubo 40x40x2 Lg. 795", "Angolare 50x5"...
# ---------------------------------------------------------------------------
_CATALOGO = None


def catalogo() -> dict:
    """profili_tubolari.json (lo stesso del preventivatore)."""
    global _CATALOGO
    if _CATALOGO is None:
        p = os.path.join(os.path.dirname(__file__), 'preventivi', 'profili_tubolari.json')
        try:
            with open(p, encoding='utf-8') as f:
                _CATALOGO = json.load(f) or {}
        except Exception:        # noqa: BLE001
            _CATALOGO = {}
    return _CATALOGO


def _dal_catalogo(fam, **dim):
    for p in catalogo().get(fam, []) or []:
        try:
            if all(abs(float(p.get(k, -1)) - v) < 0.05 for k, v in dim.items()):
                return p
        except (TypeError, ValueError):
            continue
    return None


def _g(v: float) -> str:
    """40.0 -> '40', 1.5 -> '1,5' (all'italiana)."""
    return ('%g' % round(float(v), 2)).replace('.', ',')


_NUM = r'(\d+(?:[.,]\d+)?)'
_X = r'\s*[xX×*]\s*'
_RX_LUNG = re.compile(r'(?:\blg|\blungh(?:ezza)?|\blung|\bL\s*(?=[=:.]))\s*[.=:]?\s*' + _NUM + r'\s*(?:mm)?', re.I)
# "sp.2", "sp 2", "s=2", "th 2" (non "S235": e' il materiale)
_RX_SP = re.compile(r'\b(?:sp\s*[.=:]?|s\s*[=:]|th\s*[.=:]?)\s*' + _NUM, re.I)
_RX_DIAM = re.compile(r'(?:[Øø⌀Ф]|\bdiam\.?|\bd\.(?=\s*\d)|\bD\s*(?=\d))\s*' + _NUM, re.I)
_RX_TRAVE = re.compile(r'\b(UPN|UPE|IPE|IPN|HEA|HEB|HEM)\s*' + _NUM, re.I)


def _f(s) -> float:
    return float(str(s).replace(',', '.'))


def _norm(t: str) -> str:
    t = ' '.join(str(t or '').replace(' ', ' ').split())
    # il catalogo e alcuni cartigli hanno la Ø rovinata dalla codifica
    return t.replace('�', 'Ø').replace('Ã˜', 'Ø')


# La parola del profilo deve APRIRE la descrizione (o un suo pezzo dopo
# " - ", ":" ...): "Nervatura per tondo Ø10" e "Lamiera ... angolare 52x48
# sp4" sono lamiere, non profili (righe vere degli ordini DECA).
_RX_INIZIO = re.compile(r'^(?:TUB[OI]\b|TUBOLAR|T\.\s*(?:QUADR|RETT|TOND)|SCATOLAT|PROFILAT|QUADR|RETT|TOND'
                        r'|ANGOLAR|PIATT|L\s*\d|UPN|UPE|IPE|IPN|HEA|HEB|HEM|SHS|RHS|CHS)', re.I)
_RX_LAMIERA = re.compile(r'\bLAMIER|\bPIASTR|\bNERVATUR|\bH\s*\d', re.I)


def leggi_profilo(testo) -> dict | None:
    """Il profilo da barra scritto in una descrizione, o None.

    {famiglia: SHS|RHS|CHS|L|FLAT|BAR|SQBAR|TRAVE, nome, chiave, lunghezza_mm,
     spessore, da_catalogo, kg_m, testo}

    Famiglie: SHS tubo quadro, RHS tubo rettangolare, CHS tubo tondo, L
    angolare, FLAT piatto, BAR tondo pieno, SQBAR quadro pieno, TRAVE
    UPN/IPE/HEA... Il nome e' quello con cui si raggruppano le barre: lo
    stesso profilo scritto in modi diversi da' lo stesso nome.

    Prudente: la parola del profilo deve aprire la descrizione (o un suo
    pezzo), e niente lamiere ("lamiera", "piastra", "H38"); angolari e piatti
    con "sp." sono lamiere piegate o tagliate al laser."""
    t = _norm(testo)
    if not t:
        return None
    pezzi = [x.strip() for x in re.split(r'\s[-–]\s|[;:()]|,\s', t) if x and x.strip()]
    for seg in pezzi:
        if not _RX_INIZIO.match(seg) or _RX_LAMIERA.search(seg):
            continue
        r = _leggi_segmento(seg)
        if r:
            if r.get('lunghezza_mm') is None and seg != t:
                r['lunghezza_mm'] = _lunghezza(t)
            r['testo'] = t[:120]
            return r
    return None


def _lunghezza(t: str):
    m = _RX_LUNG.search(t)
    if not m:
        return None
    try:
        g = m.group(1)
        v = _f(g.replace('.', '') if re.fullmatch(r'\d{1,2}\.\d{3}', g) else g)
    except ValueError:
        return None
    return v if 1 <= v <= 24000 else None


def _leggi_segmento(t: str) -> dict | None:
    u = t.upper()
    lung = None
    m = _RX_LUNG.search(t)
    if m:
        try:
            g = m.group(1)
            # "Lg. 1.200": il punto delle migliaia
            lung = _f(g.replace('.', '') if re.fullmatch(r'\d{1,2}\.\d{3}', g) else g)
        except ValueError:
            lung = None
        t_senza = t[:m.start()] + ' ' + t[m.end():]
    else:
        t_senza = t
    us = t_senza.upper()
    tre = re.search(_NUM + _X + _NUM + _X + _NUM + r'(?:' + _X + _NUM + r')?', us)
    due = re.search(_NUM + _X + _NUM, us)
    sp_m = _RX_SP.search(t_senza)
    sp = _f(sp_m.group(1)) if sp_m else None
    diam = _RX_DIAM.search(t_senza)
    fam = None
    out = None

    tubo = re.search(r'\bTUB[OI]\b|\bTUBOLAR|\bT\.\s*(?:QUADR|RETT|TOND)|\bSCATOLAT|\bPROFILAT|\bSHS\b|\bRHS\b|\bCHS\b', u)
    quadro = re.search(r'\bQUADR', u)
    rett = re.search(r'\bRETT', u)
    tondo = re.search(r'\bTOND', u)
    trave = _RX_TRAVE.search(t_senza)

    def quarto(mm):
        """Il quarto numero di "40x40x2x795": la lunghezza."""
        return _f(mm.group(4)) if mm and mm.lastindex and mm.lastindex >= 4 and mm.group(4) else None

    if trave:
        nome = f'{trave.group(1).upper()} {_g(_f(trave.group(2)))}'
        out = {'famiglia': 'TRAVE', 'nome': nome, 'spessore': None, 'da_catalogo': False, 'kg_m': None}
    elif tondo and tubo is None and sp is None and not due:
        # "Tondo Ø20", "Tondo 20", "Tondo pieno Ø20": barra piena
        d = _f(diam.group(1)) if diam else None
        if d is None:
            mm = re.search(r'\bTOND\w*\s*(?:PIEN\w*\s*)?' + _NUM, us)
            d = _f(mm.group(1)) if mm else None
        if d:
            out = {'famiglia': 'BAR', 'nome': f'Tondo pieno Ø{_g(d)}', 'spessore': None, 'da_catalogo': False,
                   'kg_m': round(math.pi * d * d / 4 * 7.85e-3, 3)}
    elif (tondo or (tubo and diam)) and not re.search(r'\bPIEN', u):
        # tubo tondo: "Tubo Ø40x2", "Tondo Ø16 sp.1.5mm", "T. tondo 40 x 2"
        d = t_ = None
        if diam:
            d = _f(diam.group(1))
            dopo = re.match(_X + _NUM, t_senza[diam.end():])
            if dopo:
                t_ = _f(dopo.group(1))
        if d is None and due:
            d, t_ = _f(due.group(1)), _f(due.group(2))
        if t_ is None:
            t_ = sp
        if d and t_ and t_ < d / 2:
            p = _dal_catalogo('CHS', d_ext=d, spessore=t_)
            out = {'famiglia': 'CHS', 'nome': f'Tubo tondo Ø{_g(d)} sp.{_g(t_)}mm', 'spessore': t_,
                   'da_catalogo': bool(p), 'kg_m': (p or {}).get('peso_kg_m') or round(math.pi * t_ * (d - t_) * 7.85e-3, 3)}
    elif tondo and re.search(r'\bPIEN', u):
        d = _f(diam.group(1)) if diam else None
        if d is None:
            mm = re.search(r'\bTOND\w*\s*(?:PIEN\w*\s*)?' + _NUM, us)
            d = _f(mm.group(1)) if mm else None
        if d:
            out = {'famiglia': 'BAR', 'nome': f'Tondo pieno Ø{_g(d)}', 'spessore': None, 'da_catalogo': False,
                   'kg_m': round(math.pi * d * d / 4 * 7.85e-3, 3)}
    elif re.search(r'^\s*ANGOLAR|\bANGOLAR[EI]\b|\bL\s*\d', u) and (tre or due) and sp_m is None:
        if tre:
            a, b, s = _f(tre.group(1)), _f(tre.group(2)), _f(tre.group(3))
            if lung is None:
                lung = quarto(tre)
        else:
            a, s = _f(due.group(1)), _f(due.group(2))
            b = a
        if s < min(a, b):
            out = {'famiglia': 'L', 'nome': f'Angolare {_g(max(a, b))}x{_g(min(a, b))}x{_g(s)}', 'spessore': s,
                   'da_catalogo': False, 'kg_m': round(s * (a + b - s) * 7.85e-3, 3)}
    elif re.search(r'\bPIATT', u) and due and sp_m is None:
        a, s = _f(due.group(1)), _f(due.group(2))
        if tre and lung is None:
            c3 = _f(tre.group(3))
            if c3 > 2 * max(a, s):
                lung = c3
        a, s = max(a, s), min(a, s)
        out = {'famiglia': 'FLAT', 'nome': f'Piatto {_g(a)}x{_g(s)}', 'spessore': s, 'da_catalogo': False,
               'kg_m': round(a * s * 7.85e-3, 3)}
    elif (tubo or quadro or rett) and (tre or (due and sp is not None)):
        if tre:
            a, b, s = _f(tre.group(1)), _f(tre.group(2)), _f(tre.group(3))
            if lung is None:
                lung = quarto(tre)
        else:
            a, b, s = _f(due.group(1)), _f(due.group(2)), sp
        A, B = max(a, b), min(a, b)
        if s and s < B / 2:
            if abs(A - B) < 0.01 and not (rett and not quadro):
                p = _dal_catalogo('SHS', lato_a=A, lato_b=B, spessore=s)
                fam, nome = 'SHS', f'Tubo quadro {_g(A)}x{_g(B)} sp.{_g(s)}mm'
            else:
                p = _dal_catalogo('RHS', lato_a=A, lato_b=B, spessore=s)
                fam, nome = 'RHS', f'Tubo rett. {_g(A)}x{_g(B)} sp.{_g(s)}mm'
            out = {'famiglia': fam, 'nome': nome, 'spessore': s, 'da_catalogo': bool(p),
                   'kg_m': (p or {}).get('peso_kg_m') or round(2 * s * (A + B - 2 * s) * 7.85e-3, 3)}
    elif quadro and re.search(r'\bPIEN', u):
        mm = re.search(_NUM, us[quadro.end():])
        if mm:
            a = _f(mm.group(1))
            out = {'famiglia': 'SQBAR', 'nome': f'Quadro pieno {_g(a)}', 'spessore': None, 'da_catalogo': False,
                   'kg_m': round(a * a * 7.85e-3, 3)}
    if not out:
        return None
    if lung is not None and not (1 <= lung <= 24000):
        lung = None
    out['lunghezza_mm'] = lung
    out['chiave'] = out['nome'].lower().replace(' ', '')
    out['testo'] = t[:120]
    return out


def chiave_profilo(profilo) -> str:
    """Per raggruppare: il nome letto (stesso profilo scritto in modi diversi
    = stessa chiave), altrimenti il testo cosi' com'e'."""
    p = leggi_profilo(profilo)
    if p:
        return p['chiave']
    return ' '.join(str(profilo or '').lower().split()) or '-'


def nome_profilo(profilo) -> str:
    p = leggi_profilo(profilo)
    return p['nome'] if p else (' '.join(str(profilo or '').split()) or 'Profilo non indicato')


# ---------------------------------------------------------------------------
# Sezione di tubo disegnata (cerchi concentrici, rettangoli uno dentro l'altro)
# ---------------------------------------------------------------------------
_SEZIONI: dict = {}
_SEZ_LOCK = threading.Lock()


def sezione_tubo(percorso: str | None) -> dict | None:
    """Una sezione di tubo disegnata nel DXF (una vista a parte, non un foro
    dentro un pezzo): due cerchi concentrici con parete sottile, o due
    rettangoli uno dentro l'altro a distanza costante.
    {famiglia, nome, d|lati, spessore} o None. Solo un suggerimento."""
    if not percorso or not str(percorso).lower().endswith('.dxf'):
        return None
    try:
        k = (percorso, os.path.getmtime(percorso))
    except OSError:
        return None
    with _SEZ_LOCK:
        if k in _SEZIONI:
            return _SEZIONI[k]
    r = None
    try:
        r = _cerca_sezione(percorso)
    except Exception:            # noqa: BLE001
        logger.debug('sezione di tubo non letta: %s', percorso, exc_info=True)
    with _SEZ_LOCK:
        if len(_SEZIONI) > 200:
            _SEZIONI.clear()
        _SEZIONI[k] = r
    return r


def _cerca_sezione(percorso: str) -> dict | None:
    import ezdxf
    doc = ezdxf.readfile(percorso)
    msp = doc.modelspace()
    cerchi, rett = [], []
    for e in msp.query('CIRCLE'):
        c = e.dxf.center
        cerchi.append((round(c.x, 2), round(c.y, 2), float(e.dxf.radius)))
    for e in msp.query('LWPOLYLINE'):
        if not e.closed:
            continue
        pts = [(float(p[0]), float(p[1])) for p in e.get_points()]
        if len(pts) < 4:
            continue
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        rett.append((min(xs), min(ys), max(xs), max(ys)))
    # tutte le forme chiuse: una sezione vera non sta dentro un altro pezzo
    forme = [(x - r, y - r, x + r, y + r) for x, y, r in cerchi] + rett

    def dentro_altro(bb):
        w, h = bb[2] - bb[0], bb[3] - bb[1]
        for o in forme:
            if o is bb:
                continue
            ow, oh = o[2] - o[0], o[3] - o[1]
            if ow * oh > w * h * 1.5 and o[0] < bb[0] - 0.1 and o[1] < bb[1] - 0.1 and o[2] > bb[2] + 0.1 and o[3] > bb[3] + 0.1:
                return True
        return False

    for i, (x, y, r1) in enumerate(cerchi):
        for (x2, y2, r2) in cerchi[i + 1:]:
            if abs(x - x2) > 0.05 or abs(y - y2) > 0.05 or abs(r1 - r2) < 1e-6:
                continue
            ro, ri = max(r1, r2), min(r1, r2)
            parete = ro - ri
            if 0.5 <= parete <= 12 and 4 <= ro <= 170 and parete < ro / 3:
                bb = (x - ro, y - ro, x + ro, y + ro)
                if dentro_altro(bb):
                    continue
                d, s = round(ro * 2, 1), round(parete, 1)
                return {'famiglia': 'CHS', 'nome': f'Tubo tondo Ø{_g(d)} sp.{_g(s)}mm', 'spessore': s}
    for i, a in enumerate(rett):
        for b in rett[i + 1:]:
            o, n = (a, b) if (a[2] - a[0]) > (b[2] - b[0]) else (b, a)
            off = [n[0] - o[0], n[1] - o[1], o[2] - n[2], o[3] - n[3]]
            if min(off) < 0.5 or max(off) > 12 or max(off) - min(off) > 0.2:
                continue
            W, H = o[2] - o[0], o[3] - o[1]
            if not (8 <= min(W, H) and max(W, H) <= 400) or dentro_altro(o):
                continue
            s = round(sum(off) / 4, 1)
            A, B = round(max(W, H), 1), round(min(W, H), 1)
            if abs(A - B) < 0.05:
                return {'famiglia': 'SHS', 'nome': f'Tubo quadro {_g(A)}x{_g(B)} sp.{_g(s)}mm', 'spessore': s}
            return {'famiglia': 'RHS', 'nome': f'Tubo rett. {_g(A)}x{_g(B)} sp.{_g(s)}mm', 'spessore': s}
    return None


# ---------------------------------------------------------------------------
# La proposta per il tasto N
# ---------------------------------------------------------------------------
def proposta(testi_riga: list, testo_cartiglio: str | None, percorso: str | None,
             misure=None) -> dict:
    """Cosa proporre quando il laser dice "non e' da laser". Decide sempre
    lui col tasto: questo pre-sceglie T e riempie profilo e lunghezza.

    testi_riga: le descrizioni della riga dell'ordine del cliente;
    testo_cartiglio: la descrizione del profilo nel cartiglio
    (verifica_coerenza.descrizione_profilo); percorso: il DXF (sezione di
    tubo disegnata); misure: [w, h] del pezzo letto.
    {tipo, profilo, lunghezza_mm, fonte, testo, sezione, alternativa}"""
    vuota = {'tipo': None, 'profilo': None, 'lunghezza_mm': None, 'fonte': None, 'testo': None,
             'sezione': False, 'alternativa': None}
    for fonte, testi in (("riga dell'ordine", testi_riga or []), ('cartiglio', [testo_cartiglio] if testo_cartiglio else [])):
        for t in testi:
            p = leggi_profilo(t)
            if p:
                lung = p.get('lunghezza_mm')
                if lung is None:
                    # la lunghezza in un'altra riga/descrizione dello stesso pezzo
                    for t2 in list(testi_riga or []) + ([testo_cartiglio] if testo_cartiglio else []):
                        q = leggi_profilo(t2)
                        if q and q['chiave'] == p['chiave'] and q.get('lunghezza_mm'):
                            lung = q['lunghezza_mm']
                            break
                return {**vuota, 'tipo': 'tubo', 'profilo': p['nome'], 'lunghezza_mm': lung,
                        'fonte': fonte, 'testo': p['testo'],
                        'alternativa': 'laser_tubo' if p['famiglia'] in ('SHS', 'RHS', 'CHS') else None}
    # la lunghezza scritta a parole nella riga ("... MTR - Lg 581 - AISI304",
    # anche la riga della distinta dell'assieme) vale piu' delle misure
    lung_txt = next((_lunghezza(_norm(t)) for t in testi_riga or [] if _lunghezza(_norm(t))), None)
    # La distinta scritta nel disegno stesso (DECA 26CCSA0038: "T.Quadro
    # Aisi304 50x 50x 3 L. 581" nella tabella dei componenti)
    for t in testi_profilo_dxf(percorso):
        p = leggi_profilo(t)
        if p:
            return {**vuota, 'tipo': 'tubo', 'profilo': p['nome'], 'lunghezza_mm': p.get('lunghezza_mm') or lung_txt,
                    'fonte': 'disegno', 'testo': p['testo'],
                    'alternativa': 'laser_tubo' if p['famiglia'] in ('SHS', 'RHS', 'CHS') else None}
    lung_dis = None
    try:
        if misure and misure[0] and misure[1]:
            lung_dis = round(max(float(misure[0]), float(misure[1])))
    except (TypeError, ValueError):
        lung_dis = None
    if lung_txt:
        lung_dis = lung_txt
    sez = sezione_tubo(percorso)
    if sez:
        return {**vuota, 'tipo': 'tubo', 'profilo': sez['nome'], 'lunghezza_mm': lung_dis,
                'fonte': 'sezione di tubo nel disegno', 'testo': None, 'sezione': True, 'alternativa': 'laser_tubo'}
    # Solo la parola, senza misure ("Angolare supporto guide", "Piatto per
    # deviatore": nell'archivio quasi sempre cosi'): e' un profilo da barra,
    # T si pre-sceglie ma il profilo lo scrive chi guarda il disegno.
    for fonte, testi in (("riga dell'ordine", testi_riga or []), ('cartiglio', [testo_cartiglio] if testo_cartiglio else [])):
        for t in testi:
            w = parola_profilo(t)
            if w:
                return {**vuota, 'tipo': 'tubo', 'profilo': None, 'lunghezza_mm': lung_dis, 'fonte': fonte,
                        'testo': _norm(t)[:120], 'solo_parola': w,
                        'alternativa': 'laser_tubo' if w.startswith('TUB') else None}
    # Un pezzo lungo e stretto disegnato in DXF (DECA 26CCSA0038: 581x50) e'
    # quasi sempre un tubo o un profilo visto di fianco: lato e lunghezza
    # dal disegno, il profilo lo sceglie chi guarda (lato 50 x ?).
    lato = misure_tubo(misure)
    if lato:
        return {**vuota, 'tipo': 'tubo', 'profilo': None, 'lunghezza_mm': lung_txt or lato['lunghezza_mm'],
                'fonte': 'misure del disegno', 'lato_mm': lato['lato_mm'],
                'testo': f"disegno {_g(lato['lunghezza_mm'])} x {_g(lato['lato_mm'])} mm: tubo o profilo "
                         f"lato {_g(lato['lato_mm'])} x ?, L = {_g(lato['lunghezza_mm'])} mm",
                'alternativa': 'laser_tubo'}
    return vuota


_TESTI: dict = {}


def testi_profilo_dxf(percorso: str | None) -> list:
    """Le scritte del DXF che descrivono un profilo da barra (la riga della
    distinta nel disegno: "T.Quadro Aisi304 50x 50x 3 L. 581"). In cache
    finche' il file non cambia. [] se non ce ne sono."""
    if not percorso or not str(percorso).lower().endswith('.dxf'):
        return []
    try:
        k = (percorso, os.path.getmtime(percorso))
    except OSError:
        return []
    with _SEZ_LOCK:
        if k in _TESTI:
            return _TESTI[k]
    out = []
    try:
        import ezdxf
        doc = ezdxf.readfile(percorso)
        fonti = [doc.modelspace()] + [b for b in doc.blocks if not b.name.startswith('*')]
        for f in fonti:
            for e in f.query('TEXT MTEXT'):
                try:
                    t = e.plain_text() if e.dxftype() == 'MTEXT' else e.dxf.text
                except Exception:        # noqa: BLE001
                    continue
                t = ' '.join(str(t or '').split())
                if 6 <= len(t) <= 160 and t not in out and leggi_profilo(t):
                    out.append(t)
    except Exception:            # noqa: BLE001
        logger.debug('scritte del disegno non lette: %s', percorso, exc_info=True)
    with _SEZ_LOCK:
        if len(_TESTI) > 200:
            _TESTI.clear()
        _TESTI[k] = out[:5]
    return out[:5]


def misure_tubo(misure) -> dict | None:
    """{lato_mm, lunghezza_mm} se le misure del disegno sono quelle di un
    tubo/profilo visto di fianco (lungo almeno 4 volte il lato, lato fino a
    200 mm), altrimenti None."""
    try:
        a, b = float(misure[0]), float(misure[1])
    except (TypeError, ValueError, IndexError):
        return None
    lung, lato = max(a, b), min(a, b)
    if lato <= 0 or lato > 200 or lung < 4 * lato or lung > 24000:
        return None
    return {'lato_mm': round(lato, 1), 'lunghezza_mm': round(lung)}


def nomi_profili() -> list:
    """I profili del catalogo (profili_tubolari.json) scritti come li legge
    leggi_profilo (cosi' si raggruppano bene nelle barre), per scegliere
    scrivendo: "Tubo quadro 40x40 sp.2mm"..."""
    out, visti = [], set()
    for fam in ('SHS', 'RHS', 'CHS'):
        for p in catalogo().get(fam, []) or []:
            n = p.get('nome')
            if not n:
                continue
            letto = leggi_profilo(('Tubo ' if not re.match(r'(?i)tub', n) else '') + _norm(n))
            nome = letto['nome'] if letto else _norm(n)
            if nome.lower() not in visti:
                visti.add(nome.lower())
                out.append(nome)
    return out


def da_completare(v: dict) -> str | None:
    """Cosa manca a una voce perche' si possa ordinare: il profilo e la
    lunghezza di un tubo / laser tubo. None se e' completa. Le voci da
    completare restano fuori dal calcolo delle barre."""
    if v.get('tipo') in ('tubo', 'laser_tubo'):
        manca = [x for x, k in (('profilo', 'profilo'), ('lunghezza', 'lunghezza_mm')) if not v.get(k)]
        if manca:
            return 'manca ' + ' e '.join(manca)
    return None


def parola_profilo(testo) -> str | None:
    """La parola del profilo che apre la descrizione (o un suo pezzo), senza
    le misure: "Angolare supporto guide" -> 'ANGOLARE'. None per le lamiere."""
    t = _norm(testo)
    for seg in [x.strip() for x in re.split(r'\s[-–]\s|[;:()]|,\s', t) if x and x.strip()]:
        m = _RX_INIZIO.match(seg)
        if m and not _RX_LAMIERA.search(seg) and not _RX_SP.search(seg):
            return seg.split()[0].upper().rstrip('.')
    return None


# ---------------------------------------------------------------------------
# Barre: piano di taglio (first-fit decreasing, con la lama)
# ---------------------------------------------------------------------------
def piano_barre(pezzi: list, barra_mm: float = BARRA_MM_DEFAULT, lama_mm: float = LAMA_MM_DEFAULT) -> dict:
    """pezzi: [{lunghezza_mm, ...riferimenti}] (uno per pezzo fisico).

    First-fit decreasing: dal pezzo piu' lungo, ognuno nella prima barra in
    cui sta (ogni taglio in piu' consuma la lama). I pezzi piu' lunghi di una
    barra non entrano nel piano: si segnalano a parte.
    {barre: [{tagli: [pezzo], usato_mm, sfrido_mm}], n_barre, troppo_lunghi,
     senza_lunghezza, metri, barra_mm, lama_mm}"""
    barra, lama = float(barra_mm), float(lama_mm)
    troppo, senza, buoni = [], [], []
    for p in pezzi:
        try:
            l = float(p.get('lunghezza_mm') or 0)
        except (TypeError, ValueError):
            l = 0
        if l <= 0:
            senza.append(p)
        elif l > barra + 1e-6:
            troppo.append(p)
        else:
            buoni.append((l, p))
    buoni.sort(key=lambda x: -x[0])
    barre = []
    for l, p in buoni:
        for b in barre:
            serve = l + (lama if b['tagli'] else 0)
            if b['usato_mm'] + serve <= barra + 1e-6:
                b['tagli'].append(p)
                b['usato_mm'] += serve
                break
        else:
            barre.append({'tagli': [p], 'usato_mm': l})
    for b in barre:
        b['usato_mm'] = round(b['usato_mm'], 1)
        b['sfrido_mm'] = round(barra - b['usato_mm'], 1)
    metri = sum(l for l, _ in buoni) / 1000 + sum(float(p.get('lunghezza_mm') or 0) for p in troppo) / 1000
    return {'barre': barre, 'n_barre': len(barre), 'troppo_lunghi': troppo, 'senza_lunghezza': senza,
            'metri': round(metri, 3), 'barra_mm': barra, 'lama_mm': lama}


def gruppi_tubi(voci: list, cfg: dict | None = None) -> list:
    """Le voci "tubo" raggruppate per profilo + materiale, ognuna col piano
    delle barre. voci: dict come _diz (con 'ordine')."""
    cfg = cfg or config()
    per = {}
    for v in voci:
        if v.get('tipo') != 'tubo' or not v.get('profilo'):
            continue            # senza profilo: "da completare", fuori dalle barre
        # "INOX_304" (cartiglio) e "INOX 304" (scritto a mano) sono lo stesso materiale
        k = (chiave_profilo(v.get('profilo') or v.get('descrizione')),
             ' '.join(str(v.get('materiale') or '').replace('_', ' ').upper().split()))
        g = per.setdefault(k, {'chiave': '|'.join(k), 'profilo': nome_profilo(v.get('profilo') or v.get('descrizione')),
                               'materiale': (v.get('materiale') or '').strip() or None, 'voci': [], 'pezzi': []})
        g['voci'].append(v['id'])
        for _ in range(max(0, int(v.get('quantita') or 0))):
            g['pezzi'].append({'lunghezza_mm': v.get('lunghezza_mm'), 'id': v['id'], 'codice': v.get('codice'),
                               'ordine': (v.get('ordine') or {}).get('etichetta')})
    out = []
    for g in per.values():
        pb = piano_barre(g['pezzi'], cfg['barra_mm'], cfg['lama_mm'])
        p = leggi_profilo(g['profilo'])
        kg = round(pb['metri'] * p['kg_m'], 1) if p and p.get('kg_m') else None
        # il piano: per ogni barra i tagli (lunghezza, codice, ordine)
        piano = [{'tagli': [{'lunghezza_mm': t['lunghezza_mm'], 'codice': t['codice'], 'ordine': t['ordine'], 'id': t['id']}
                            for t in b['tagli']], 'usato_mm': b['usato_mm'], 'sfrido_mm': b['sfrido_mm']} for b in pb['barre']]
        troppo = {}
        for t in pb['troppo_lunghi']:
            troppo.setdefault(t['id'], {'id': t['id'], 'codice': t['codice'], 'lunghezza_mm': t['lunghezza_mm'], 'n': 0})['n'] += 1
        senza = {}
        for t in pb['senza_lunghezza']:
            senza.setdefault(t['id'], {'id': t['id'], 'codice': t['codice'], 'n': 0})['n'] += 1
        out.append({'chiave': g['chiave'], 'profilo': g['profilo'], 'materiale': g['materiale'], 'voci': g['voci'],
                    'n_pezzi': len(g['pezzi']), 'metri': pb['metri'], 'n_barre': pb['n_barre'], 'kg': kg,
                    'barra_mm': pb['barra_mm'], 'lama_mm': pb['lama_mm'], 'piano': piano,
                    'troppo_lunghi': list(troppo.values()), 'senza_lunghezza': list(senza.values())})
    out.sort(key=lambda g: (g['profilo'].lower(), g['materiale'] or ''))
    return out


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def _iso(d):
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.isoformat() + 'Z'
    return d.isoformat()


def _diz(r) -> dict:
    return {'id': r.id, 'order_id': r.order_id, 'chiave': r.chiave, 'origine': r.origine,
            'articolo_id': r.articolo_id, 'codice': r.codice, 'tipo': r.tipo,
            'tipo_testo': TIPI_BREVI.get(r.tipo, r.tipo), 'descrizione': r.descrizione, 'profilo': r.profilo,
            'lunghezza_mm': r.lunghezza_mm, 'quantita': r.quantita, 'materiale': r.materiale,
            'stato': r.stato, 'fornitore': r.fornitore, 'data_prevista': _iso(r.data_prevista),
            'ordinato_il': _iso(r.ordinato_il), 'arrivato_il': _iso(r.arrivato_il), 'nota': r.nota,
            'creato_il': _iso(r.creato_il), 'creato_da': r.creato_da,
            'aggiornato_il': _iso(r.aggiornato_il), 'aggiornato_da': r.aggiornato_da}


def _foto(r) -> dict:
    """Quello che un cambio di stato tocca: per annullarlo."""
    return {'id': r.id, 'stato': r.stato, 'fornitore': r.fornitore, 'data_prevista': _iso(r.data_prevista),
            'ordinato_il': _iso(r.ordinato_il), 'arrivato_il': _iso(r.arrivato_il)}


def _data(v):
    if v in (None, ''):
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    try:
        return datetime.strptime(str(v)[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def _istante(v):
    if v in (None, ''):
        return None
    try:
        return datetime.fromisoformat(str(v).rstrip('Z'))
    except ValueError:
        return None


def classifica(order_id: str, articolo_id, *, tipo: str, codice=None, descrizione=None, profilo=None,
               lunghezza_mm=None, quantita=1, materiale=None, nota=None, chi: str = '') -> dict:
    """Il laser ha detto N e ha scelto cosa serve: la voce del pezzo si crea
    o si aggiorna (una sola per ordine + pezzo). Lo stato resta quello che
    era (un pezzo gia' ordinato resta ordinato)."""
    from .database import get_session
    from .models import DaProcurare
    if tipo not in TIPI:
        raise ValueError('tipo non valido')
    s = get_session()
    try:
        chiave = f'art:{articolo_id}'
        r = s.query(DaProcurare).filter(DaProcurare.order_id == order_id, DaProcurare.chiave == chiave).first()
        nuovo = r is None
        if nuovo:
            r = DaProcurare(order_id=order_id, chiave=chiave, origine='classificato', articolo_id=str(articolo_id),
                            stato='da_ordinare', creato_il=datetime.utcnow(), creato_da=chi or None)
            s.add(r)
        else:
            r.aggiornato_il, r.aggiornato_da = datetime.utcnow(), chi or None
        r.tipo = tipo
        r.codice = (str(codice).strip() or None) if codice else None
        r.descrizione = (str(descrizione).strip()[:200] or None) if descrizione else None
        r.profilo = (str(profilo).strip()[:80] or None) if profilo else None
        r.lunghezza_mm = float(lunghezza_mm) if lunghezza_mm not in (None, '') else None
        r.quantita = max(1, int(quantita or 1))
        r.materiale = (str(materiale).strip() or None) if materiale else None
        r.nota = (str(nota).strip()[:200] or None) if nota else None
        s.commit()
        out = {**_diz(r), 'nuova': nuovo}
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
    if nuovo:
        avvisa_ufficio(order_id)
    return out


def togli_classificato(order_id: str, articolo_id) -> dict | None:
    """La decisione N e' stata tolta o cambiata: la voce del pezzo si toglie
    se non e' ancora stata ordinata. Se e' gia' ordinata (o arrivata) resta:
    il materiale e' stato comprato davvero. Ritorna la voce tolta / rimasta."""
    from .database import get_session
    from .models import DaProcurare
    s = get_session()
    try:
        r = s.query(DaProcurare).filter(DaProcurare.order_id == order_id,
                                        DaProcurare.chiave == f'art:{articolo_id}').first()
        if not r:
            return None
        d = _diz(r)
        if r.stato == 'da_ordinare':
            s.delete(r)
            s.commit()
            return {**d, 'tolta': True}
        return {**d, 'tolta': False}
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def _qta(v) -> int:
    try:
        n = int(round(float(v)))
        return n if n > 0 else 1
    except (TypeError, ValueError):
        return 1


def _lung_riga(r: dict):
    m = re.search(r'L=(\d+(?:[.,]\d+)?)\s*mm', str(r.get('descrizione') or ''))
    return _f(m.group(1)) if m else None


def voci_tubolari(righe: list) -> dict:
    """Le righe tubolari della distinta come voci "da procurare", per chiave
    (righe uguali dello stesso assieme si sommano). Tubo (T), o laser tubo
    (L) se la riga ha la lavorazione esterna del preventivatore."""
    out = {}
    for r in righe or []:
        if not isinstance(r, dict) or r.get('tipo') != 'tubolare':
            continue
        desc = str(r.get('descrizione') or '')
        lung = _lung_riga(r)
        prof = re.sub(r'\s*L=\d+(?:[.,]\d+)?\s*mm\s*$', '', desc).strip() or str(r.get('codice') or '')
        tipo = 'laser_tubo' if r.get('lav_esterna') else 'tubo'
        k = 'tub:' + '|'.join([str(r.get('assieme') or ''), chiave_profilo(prof),
                               ('%g' % lung) if lung else '', str(r.get('materiale') or '').lower(), tipo])
        v = out.get(k)
        if v:
            v['quantita'] += _qta(r.get('quantita'))
            continue
        cod = r.get('codice') or ''
        out[k] = {'chiave': k, 'tipo': tipo, 'codice': cod if cod and cod != prof else (r.get('assieme') or None),
                  'descrizione': desc[:200], 'profilo': prof[:80], 'lunghezza_mm': lung,
                  'quantita': _qta(r.get('quantita')), 'materiale': r.get('materiale') or None,
                  'nota': (r.get('lav_esterna') if tipo == 'laser_tubo' else None),
                  'assieme': r.get('assieme')}
    return out


def sincronizza_righe(order_id: str, righe: list) -> int:
    """Le voci dalle righe tubolari dell'ordine: si creano quelle nuove, si
    aggiornano quantita'/profilo di quelle che ci sono, si tolgono quelle
    sparite dalla distinta se non ancora ordinate. Nessun passo a mano.
    Ritorna quante voci ha cambiato."""
    from .database import get_session
    from .models import DaProcurare
    nuove = voci_tubolari(righe)
    s = get_session()
    n = 0
    try:
        ci = {r.chiave: r for r in s.query(DaProcurare).filter(DaProcurare.order_id == order_id,
                                                               DaProcurare.origine == 'tubolare').all()}
        for k, v in nuove.items():
            r = ci.get(k)
            if r is None:
                s.add(DaProcurare(order_id=order_id, chiave=k, origine='tubolare', tipo=v['tipo'],
                                  codice=v['codice'], descrizione=v['descrizione'], profilo=v['profilo'],
                                  lunghezza_mm=v['lunghezza_mm'], quantita=v['quantita'], materiale=v['materiale'],
                                  nota=v['nota'], stato='da_ordinare', creato_il=datetime.utcnow(),
                                  creato_da='distinta'))
                n += 1
            elif (r.quantita, r.profilo, r.descrizione) != (v['quantita'], v['profilo'], v['descrizione']):
                r.quantita, r.profilo, r.descrizione = v['quantita'], v['profilo'], v['descrizione']
                r.aggiornato_il, r.aggiornato_da = datetime.utcnow(), 'distinta'
                n += 1
        for k, r in ci.items():
            if k not in nuove and r.stato == 'da_ordinare':
                s.delete(r)
                n += 1
        # Voci dei pezzi classificati col tasto N: la quantita' e' quella
        # della distinta (pezzi per assieme x assiemi, o x lotto), anche
        # quando cambiano dopo (assieme da 1 a 3, pezzi per assieme scritti).
        qta_pezzo = {}
        for x in righe or []:
            if isinstance(x, dict) and x.get('tipo') == 'lamiera' and x.get('articolo_id'):
                qta_pezzo[str(x['articolo_id'])] = qta_pezzo.get(str(x['articolo_id']), 0) + _qta(x.get('quantita'))
        if qta_pezzo:
            for r in s.query(DaProcurare).filter(DaProcurare.order_id == order_id,
                                                 DaProcurare.origine == 'classificato').all():
                q = qta_pezzo.get(str(r.articolo_id or ''))
                if q and r.quantita != q:
                    r.quantita = q
                    r.aggiornato_il, r.aggiornato_da = datetime.utcnow(), 'distinta'
                    n += 1
        nuove_voci = sum(1 for k in nuove if k not in ci)
        if n:
            s.commit()
    except Exception:
        s.rollback()
        logger.warning('voci tubolari non sincronizzate per %s', order_id, exc_info=True)
        return 0
    finally:
        s.close()
    if nuove_voci:
        avvisa_ufficio(order_id)
    return n


def avvisa_ufficio(order_id: str) -> None:
    """Un avviso alla campanella dell'Ufficio (amministrazione: e' li' che si
    ordina) quando nascono voci nuove: UNO per ordine, non uno per pezzo.
    Se c'e' gia' un avviso non letto per quell'ordine, si aggiorna il numero
    ("3 pezzi da procurare per DECA A 001302"); letto o cancellato, ne nasce
    uno nuovo. Non blocca mai il salvataggio."""
    try:
        import uuid as _uuid
        from .accesso import STAZIONI
        from .database import get_session
        from .models import DaProcurare, Notification
        dest = STAZIONI['ufficio']['utente']
        s = get_session()
        try:
            o = _ordini_vivi(s, {order_id}).get(order_id)
            if not o:
                return
            n = s.query(DaProcurare).filter(DaProcurare.order_id == order_id,
                                            DaProcurare.stato == 'da_ordinare').count()
            if not n:
                return
            msg = f"{n} {'pezzo' if n == 1 else 'pezzi'} da procurare per {o['etichetta']}"
            av = s.query(Notification).filter(Notification.user_id == dest, Notification.order_id == order_id,
                                              Notification.notification_type == 'procura',
                                              Notification.is_read == False,  # noqa: E712
                                              Notification.is_deleted == False).first()  # noqa: E712
            if av:
                av.message, av.timestamp = msg, datetime.utcnow()
            else:
                s.add(Notification(id=str(_uuid.uuid4()), user_id=dest, order_id=order_id, title='Da procurare',
                                   message=msg, notification_type='procura', notification_category='attiva',
                                   is_read=False, is_deleted=False, timestamp=datetime.utcnow()))
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()
    except Exception:            # noqa: BLE001
        logger.warning("avviso all'Ufficio non scritto per %s", order_id, exc_info=True)


_SYNC = {'t': 0.0}
_SYNC_LOCK = threading.Lock()
SYNC_OGNI_S = 20


def sincronizza_tutti(forza: bool = False) -> None:
    """Le righe tubolari di tutti gli ordini vivi nati da un preventivo (al
    massimo ogni SYNC_OGNI_S secondi: le legge chi apre "Da procurare")."""
    with _SYNC_LOCK:
        if not forza and time.time() - _SYNC['t'] < SYNC_OGNI_S:
            return
        _SYNC['t'] = time.time()
    import importlib
    from .database import get_session, PreventivoManager
    from .models import Order
    from .ordini_service import STATI_ARCHIVIO
    A = importlib.import_module(__package__ + '.app')
    s = get_session()
    try:
        ordini = [(o.id, o.preventivo_id_origine) for o in s.query(Order).filter(
            Order.is_deleted == False,  # noqa: E712
            Order.preventivo_id_origine.isnot(None),
            (Order.status.is_(None)) | (~Order.status.in_(STATI_ARCHIVIO))).all()]
    finally:
        s.close()
    for oid, pid in ordini:
        try:
            prev = PreventivoManager.get(pid, include_children=True)
            if prev:
                sincronizza_righe(oid, A._distinta_da_preventivo(prev, {}))
        except Exception:        # noqa: BLE001
            logger.warning('tubolari dell\'ordine %s non letti', oid, exc_info=True)


def _ordini_vivi(session, ids=None) -> dict:
    """{order_id: {cliente, numero, consegna, etichetta}} degli ordini non
    eliminati (le voci degli ordini eliminati non si vedono)."""
    from .models import Order, Preventivo
    q = session.query(Order.id, Order.cliente, Order.numero_ordine, Order.data_consegna, Order.preventivo_id_origine,
                      Order.status).filter(Order.is_deleted == False)  # noqa: E712
    if ids is not None:
        q = q.filter(Order.id.in_(list(ids)))
    righe = q.all()
    pids = [r[4] for r in righe if r[4]]
    num_cli = {}
    if pids:
        for pid, n in session.query(Preventivo.id, Preventivo.numero_ordine_cliente).filter(Preventivo.id.in_(pids)).all():
            num_cli[pid] = (n or '').strip()
    out = {}
    for oid, cli, num, cons, pid, status in righe:
        numero = num_cli.get(pid) or num or oid[:8]
        out[oid] = {'id': oid, 'cliente': cli or '', 'numero': numero, 'status': status,
                    'consegna': cons.date().isoformat() if isinstance(cons, datetime) else (_iso(cons) if cons else None),
                    'etichetta': f"{cli or ''} {numero}".strip()}
    return out


def elenco(stato: str | None = 'da_ordinare', tipo: str | None = None, ordine: str | None = None,
           ids=None) -> list:
    """Le voci (dict) degli ordini non eliminati, con 'ordine' (cliente,
    numero) e 'scaduta' (ordinata e la data prevista e' passata).
    stato: da_ordinare | ordinato | arrivato | aperti (non arrivate) | tutti."""
    from .database import get_session
    from .models import DaProcurare
    s = get_session()
    try:
        q = s.query(DaProcurare)
        if ids is not None:
            q = q.filter(DaProcurare.id.in_(list(ids)))
        if stato in STATI:
            q = q.filter(DaProcurare.stato == stato)
        elif stato == 'aperti':
            q = q.filter(DaProcurare.stato != 'arrivato')
        if tipo in TIPI:
            q = q.filter(DaProcurare.tipo == tipo)
        if ordine:
            q = q.filter(DaProcurare.order_id == ordine)
        righe = q.all()
        vivi = _ordini_vivi(s, {r.order_id for r in righe})
        oggi = date.today()
        ass = _assiemi_dei_pezzi(s, [r.articolo_id for r in righe if r.articolo_id and r.order_id in vivi])
        out = []
        for r in righe:
            o = vivi.get(r.order_id)
            if not o:
                continue                 # ordine eliminato: la voce non si vede
            d = _diz(r)
            d['ordine'] = o
            d['scaduta'] = bool(r.stato == 'ordinato' and r.data_prevista and r.data_prevista < oggi)
            a = ass.get(str(r.articolo_id or ''))
            # pezzo di un assieme: quanti per assieme x quanti assiemi
            d['assieme'] = a['assieme'] if a else None
            d['per_assieme'] = a['per_assieme'] if a else None
            d['qta_assieme'] = a['qta_assieme'] if a else None
            d['qta_da_confermare'] = bool(a and a['da_confermare'])
            d['da_completare'] = da_completare(d)
            out.append(d)
        out.sort(key=lambda d: ((d['ordine'] or {}).get('consegna') or '9999', d['ordine']['etichetta'],
                                d['tipo'], d.get('codice') or '', d.get('profilo') or ''))
        return out
    finally:
        s.close()


def _assiemi_dei_pezzi(session, articoli_ids) -> dict:
    """{articolo_id: {assieme, per_assieme, qta_assieme, da_confermare}} per
    i pezzi che stanno in un assieme (lettura diretta, leggera)."""
    ids = sorted({str(i) for i in articoli_ids or [] if i})
    if not ids:
        return {}
    from sqlalchemy import text as _text
    out = {}
    try:
        for i in range(0, len(ids), 400):
            parte = ids[i:i + 400]
            seg = ', '.join(f':a{k}' for k in range(len(parte)))
            par = {f'a{k}': v for k, v in enumerate(parte)}
            for aid, pid, cod_ass, q, extra in session.execute(_text(
                    'SELECT id, preventivo_id, codice_assieme, quantita, extra_campi FROM preventivo_articoli '
                    f'WHERE id IN ({seg}) AND codice_assieme IS NOT NULL AND codice_assieme != \'\''), par).fetchall():
                qa = session.execute(_text('SELECT qty FROM preventivo_assiemi WHERE preventivo_id = :p '
                                           'AND codice_assieme = :c'), {'p': pid, 'c': cod_ass}).fetchone()
                try:
                    od = (json.loads(extra or '{}') or {}).get('ordine') or {}
                except (TypeError, ValueError):
                    od = {}
                out[str(aid)] = {'assieme': cod_ass, 'per_assieme': _qta(q),
                                 'qta_assieme': _qta(qa[0]) if qa else 1,
                                 'da_confermare': isinstance(od, dict) and od.get('stato') == 'qta_da_confermare'}
    except Exception:            # noqa: BLE001
        logger.warning('assiemi dei pezzi da procurare non letti', exc_info=True)
    return out


def riassunto_per_ordini(ids=None) -> dict:
    """{order_id: {n, n_da_ordinare, n_ordinati, n_scaduti, esterne: [...]}}:
    voci non ancora arrivate. Per il chip "Da procurare (N)" e il "taglia
    entro" (laser tubo e meccanica = lavorazione esterna)."""
    out = {}
    ids = set(ids) if ids is not None else None
    for v in elenco('aperti'):
        if ids is not None and v['order_id'] not in ids:
            continue
        x = out.setdefault(v['order_id'], {'n': 0, 'n_da_ordinare': 0, 'n_ordinati': 0, 'n_scaduti': 0, 'esterne': []})
        x['n'] += 1
        x['n_da_ordinare' if v['stato'] == 'da_ordinare' else 'n_ordinati'] += 1
        if v['scaduta']:
            x['n_scaduti'] += 1
        e = TIPI_ESTERNI.get(v['tipo'])
        if e and e not in x['esterne']:
            x['esterne'].append(e)
    return out


def cambia_stato(ids: list, stato: str, *, fornitore=None, data_prevista=None, chi: str = '') -> list:
    """Segna ordinato (fornitore e data prevista scritti adesso), arrivato o
    di nuovo da ordinare. Ritorna [(prima, dopo)] per l'audit e l'annulla."""
    from .database import get_session
    from .models import DaProcurare
    if stato not in STATI:
        raise ValueError('stato non valido')
    s = get_session()
    try:
        righe = s.query(DaProcurare).filter(DaProcurare.id.in_(list(ids))).all()
        vivi = _ordini_vivi(s, {r.order_id for r in righe})
        cambi = []
        ora = datetime.utcnow()
        for r in righe:
            if r.order_id not in vivi:
                continue
            prima = _foto(r)
            if stato == 'ordinato':
                r.fornitore = (str(fornitore).strip()[:80] or None) if fornitore else r.fornitore
                r.data_prevista = _data(data_prevista) if data_prevista else r.data_prevista
                r.ordinato_il = r.ordinato_il if r.stato == 'ordinato' else ora
                r.arrivato_il = None
            elif stato == 'arrivato':
                r.arrivato_il = ora
                if not r.ordinato_il:
                    r.ordinato_il = ora
            else:
                r.ordinato_il = r.arrivato_il = None
            r.stato = stato
            r.aggiornato_il, r.aggiornato_da = ora, chi or None
            cambi.append((prima, {**_diz(r), 'ordine': vivi[r.order_id]}))
        s.commit()
        return cambi
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def ripristina(foto: list, chi: str = '') -> list:
    """Annulla un cambio di stato: rimette stato, fornitore e date com'erano."""
    from .database import get_session
    from .models import DaProcurare
    per = {str(f.get('id')): f for f in foto or [] if isinstance(f, dict) and f.get('stato') in STATI}
    if not per:
        return []
    s = get_session()
    try:
        out = []
        for r in s.query(DaProcurare).filter(DaProcurare.id.in_(list(per))).all():
            f = per[r.id]
            prima = _foto(r)
            r.stato = f['stato']
            r.fornitore = f.get('fornitore') or None
            r.data_prevista = _data(f.get('data_prevista'))
            r.ordinato_il = _istante(f.get('ordinato_il'))
            r.arrivato_il = _istante(f.get('arrivato_il'))
            r.aggiornato_il, r.aggiornato_da = datetime.utcnow(), chi or None
            out.append((prima, _diz(r)))
        s.commit()
        return out
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def modifica(voce_id: str, campi: dict, chi: str = '') -> dict | None:
    """Profilo, lunghezza, quantita', materiale, descrizione, tipo o nota di
    una voce (correzioni a mano). La quantita' di un pezzo di un assieme non
    si scrive qui: segue i pezzi per assieme (api_procura)."""
    from .database import get_session
    from .models import DaProcurare
    s = get_session()
    try:
        r = s.get(DaProcurare, voce_id)
        if not r or r.order_id not in _ordini_vivi(s, {r.order_id}):
            return None
        prima = _diz(r)
        if 'profilo' in campi:
            r.profilo = (str(campi['profilo'] or '').strip()[:80] or None)
        if 'lunghezza_mm' in campi:
            v = campi['lunghezza_mm']
            r.lunghezza_mm = float(str(v).replace(',', '.')) if v not in (None, '') else None
        if 'quantita' in campi:
            r.quantita = max(1, int(campi['quantita']))
        if 'nota' in campi:
            r.nota = (str(campi['nota'] or '').strip()[:200] or None)
        if 'materiale' in campi:
            r.materiale = (str(campi['materiale'] or '').strip()[:40] or None)
        if 'descrizione' in campi:
            r.descrizione = (str(campi['descrizione'] or '').strip()[:200] or None)
        if 'tipo' in campi and campi['tipo'] in TIPI:
            r.tipo = campi['tipo']
        r.aggiornato_il, r.aggiornato_da = datetime.utcnow(), chi or None
        s.commit()
        return {'prima': prima, 'dopo': _diz(r)}
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


# ---------------------------------------------------------------------------
# I file del pezzo (DXF, PDF, STEP): solo i suoi, mai l'ordine del cliente
# ---------------------------------------------------------------------------
_ESTENSIONI = ('.dxf', '.dwg', '.pdf', '.step', '.stp')


def _chiave_codice(nome: str) -> str:
    """'46SA00578-00.pdf' / '46SA00578' -> '46SA00578' (senza estensione,
    revisione e copia)."""
    b = os.path.basename(str(nome or ''))
    b = re.sub(r'\.[A-Za-z0-9]{2,4}$', '', b).upper().strip()
    b = re.sub(r'\s*\(\d+\)$', '', b)
    b = re.sub(r'_(CLEANED|CANONICO)$', '', b)
    return re.sub(r'[-_ ]REV\.?\s*\d+$|[-_]\d{2}$', '', b)


def file_del_pezzo(order, codice: str) -> list:
    """[(nome, percorso)] dei disegni del pezzo: DXF/DWG dell'ordine, PDF e
    STEP salvati col preventivo d'origine, con lo stesso codice (senza
    revisione). Mai il PDF dell'ordine del cliente, mai i DXF ripuliti."""
    import importlib
    A = importlib.import_module(__package__ + '.app')
    k = _chiave_codice(codice)
    if not k:
        return []
    out, visti = [], set()

    def prendi(nome, percorso):
        n = os.path.basename(nome)
        low = n.lower()
        if not low.endswith(_ESTENSIONI) or '_cleaned' in low or '_canonico' in low:
            return
        if _chiave_codice(n) != k or low in visti or not os.path.isfile(percorso):
            return
        visti.add(low)
        out.append((n, percorso))

    try:
        for d in A._disegni_ordine(order):
            prendi(d['nome'], d['percorso'])
    except Exception:            # noqa: BLE001
        logger.warning('disegni dell\'ordine non letti', exc_info=True)
    escludi = set()
    try:
        from .models import OrderFile
        from .database import get_session
        s = get_session()
        try:
            for f in s.query(OrderFile).filter(OrderFile.order_id == order.id, OrderFile.file_type == 'PDF').all():
                if f.filepath:
                    escludi.add(os.path.normcase(os.path.abspath(f.filepath)))
                    escludi.add(os.path.basename(f.filepath).lower())
        finally:
            s.close()
    except Exception:            # noqa: BLE001
        pass
    pid = getattr(order, 'preventivo_id_origine', None)
    if pid:
        try:
            cart = A._cartella_preventivo(pid)
            segnato = A._ordine_cliente_segnato(cart)
            if segnato:
                escludi.add(segnato.lower())
            if os.path.isdir(cart):
                for nome in sorted(os.listdir(cart)):
                    p = os.path.join(cart, nome)
                    if nome.lower() in escludi or os.path.normcase(os.path.abspath(p)) in escludi:
                        continue
                    prendi(nome, p)
        except (ValueError, OSError):
            pass
    out.sort(key=lambda x: x[0].lower())
    return out


def disegni_disponibili(voci: list) -> dict:
    """{id voce: n file} per le voci con un codice (cache per ordine)."""
    from .database import get_session
    from .models import Order
    per_ordine = {}
    for v in voci:
        if v.get('codice'):
            per_ordine.setdefault(v['order_id'], []).append(v)
    out = {}
    if not per_ordine:
        return out
    s = get_session()
    try:
        ordini = {o.id: o for o in s.query(Order).filter(Order.id.in_(list(per_ordine))).all()}
    finally:
        s.close()
    for oid, vv in per_ordine.items():
        o = ordini.get(oid)
        for v in vv:
            out[v['id']] = len(file_del_pezzo(o, v['codice'])) if o else 0
    return out


# ---------------------------------------------------------------------------
# Richiesta d'offerta: PDF + disegni dei pezzi, in uno ZIP
# ---------------------------------------------------------------------------
def _mm(v) -> str:
    try:
        return _g(float(v)) + ' mm'
    except (TypeError, ValueError):
        return ''


def _mat(m) -> str:
    return str(m or '—').replace('_', ' ')


def pdf_richiesta(voci: list, gruppi: list, azienda: dict | None = None, quando: datetime | None = None) -> bytes:
    """La lista della richiesta d'offerta (PDF, A4): titolo, data, i tubi in
    barre e la tabella dei pezzi. Senza nome del fornitore."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape as x

    quando = quando or datetime.now()
    az = azienda or {}
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=16 * mm,
                            bottomMargin=16 * mm, title="Richiesta d'offerta", author=az.get('nome') or '')
    st = {
        'tit': ParagraphStyle('tit', fontName='Helvetica-Bold', fontSize=17, leading=21, spaceAfter=2),
        'sotto': ParagraphStyle('sotto', fontName='Helvetica', fontSize=9.5, leading=13, textColor=colors.HexColor('#474F5C')),
        'h': ParagraphStyle('h', fontName='Helvetica-Bold', fontSize=11.5, leading=15, spaceBefore=10, spaceAfter=5),
        'c': ParagraphStyle('c', fontName='Helvetica', fontSize=8.6, leading=10.6, alignment=TA_LEFT),
        'cb': ParagraphStyle('cb', fontName='Helvetica-Bold', fontSize=8.6, leading=10.6),
        'n': ParagraphStyle('n', fontName='Helvetica', fontSize=8.6, leading=11, textColor=colors.HexColor('#474F5C')),
    }
    P = lambda t, s='c': Paragraph(x(str(t if t is not None else '')), st[s])  # noqa: E731
    stile_tab = TableStyle([
        ('FONT', (0, 0), (-1, 0), 'Helvetica-Bold', 8.6),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#F3F4F6')),
        ('LINEBELOW', (0, 0), (-1, 0), 0.6, colors.HexColor('#A2A8B2')),
        ('LINEBELOW', (0, 1), (-1, -1), 0.3, colors.HexColor('#E4E7EB')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('TOPPADDING', (0, 0), (-1, -1), 3.5), ('BOTTOMPADDING', (0, 0), (-1, -1), 3.5),
        ('LEFTPADDING', (0, 0), (-1, -1), 4), ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ])
    el = [P("Richiesta d'offerta", 'tit')]
    testa = [quando.strftime('Data: %d/%m/%Y')]
    if az.get('nome'):
        testa.append(' · '.join(v for v in (az.get('nome'), az.get('indirizzo'), az.get('telefono'), az.get('email')) if v))
    el.append(P('  —  '.join(testa), 'sotto'))
    el.append(Spacer(1, 4 * mm))
    el.append(P("Vi chiediamo la vostra migliore offerta per il materiale e le lavorazioni elencati qui sotto, "
                "con prezzi e tempi di consegna. I disegni dei pezzi sono allegati (stesso codice nel nome del file).", 'n'))

    tubi = [g for g in gruppi if g.get('n_pezzi')]
    if tubi:
        el.append(P('Tubi e profili in barre', 'h'))
        righe = [['Profilo', 'Materiale', 'Pezzi', 'Metri', 'Barre', 'Note']]
        for g in tubi:
            note = []
            if g.get('troppo_lunghi'):
                note.append('pezzi più lunghi di una barra: ' + ', '.join(f"{t.get('codice') or ''} {_mm(t['lunghezza_mm'])}".strip()
                                                                         for t in g['troppo_lunghi']))
            if g.get('senza_lunghezza'):
                note.append(f"{sum(t['n'] for t in g['senza_lunghezza'])} pezzi senza lunghezza")
            righe.append([P(g['profilo'], 'cb'), P(_mat(g.get('materiale'))), P(g['n_pezzi']),
                          P(_g(g['metri']) + ' m'), P(f"{g['n_barre']} da {_g(g['barra_mm'] / 1000)} m", 'cb'),
                          P('; '.join(note) or '—')])
        t = Table(righe, colWidths=[52 * mm, 24 * mm, 14 * mm, 18 * mm, 24 * mm, None], repeatRows=1)
        t.setStyle(stile_tab)
        el.append(t)
        el.append(Spacer(1, 1.5 * mm))
        el.append(P(f"Barre calcolate con taglio da {_g(tubi[0]['lama_mm'])} mm fra un pezzo e l'altro.", 'n'))

    el.append(P('Pezzi', 'h'))
    righe = [['Codice', 'Descrizione', 'Profilo / lunghezza', 'Q.tà', 'Materiale', 'Lavorazione', 'Note']]
    for v in voci:
        pl = ' · '.join(z for z in (v.get('profilo') or '', _mm(v.get('lunghezza_mm')) if v.get('lunghezza_mm') else '') if z)
        righe.append([P(v.get('codice') or '—', 'cb'), P(v.get('descrizione') or '—'), P(pl or '—'),
                      P(v.get('quantita')), P(_mat(v.get('materiale'))), P(TIPI_BREVI.get(v.get('tipo'), v.get('tipo'))),
                      P(v.get('nota') or '')])
    t = Table(righe, colWidths=[26 * mm, 36 * mm, 30 * mm, 10 * mm, 18 * mm, 25 * mm, None], repeatRows=1)
    t.setStyle(stile_tab)
    el.append(t)

    def piede(canvas, d):
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(colors.HexColor('#737B88'))
        canvas.drawRightString(A4[0] - 16 * mm, 10 * mm, f"Richiesta d'offerta del {quando.strftime('%d/%m/%Y')} · pagina {d.page}")
        canvas.restoreState()
    doc.build(el, onFirstPage=piede, onLaterPages=piede)
    return buf.getvalue()


def _nome_zip(n: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', '_', n).strip() or 'file'


def zip_richiesta(voci: list, gruppi: list, ordini: dict, azienda: dict | None = None,
                  quando: datetime | None = None) -> tuple:
    """(bytes dello ZIP, [nomi dentro]): il PDF della richiesta e i disegni
    dei pezzi scelti (cartella disegni/). ordini: {order_id: Order}."""
    quando = quando or datetime.now()
    buf = io.BytesIO()
    nomi = []
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        pdf = pdf_richiesta(voci, gruppi, azienda, quando)
        nome_pdf = quando.strftime('Richiesta offerta %Y-%m-%d.pdf')
        z.writestr(nome_pdf, pdf)
        nomi.append(nome_pdf)
        visti = set()
        for v in voci:
            o = ordini.get(v['order_id'])
            if not o or not v.get('codice'):
                continue
            for nome, percorso in file_del_pezzo(o, v['codice']):
                arc = 'disegni/' + _nome_zip(nome)
                if arc.lower() in visti:
                    continue
                visti.add(arc.lower())
                z.write(percorso, arc)
                nomi.append(arc)
    return buf.getvalue(), nomi
