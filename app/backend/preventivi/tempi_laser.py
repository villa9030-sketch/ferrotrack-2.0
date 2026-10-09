"""Tempi di taglio laser per la saturazione (calendario del laser).

Tre fonti, in quest'ordine:

- 'lantek': il codice e' nell'archivio di Lantek e ha il tempo CAM (ND025,
  "Total time") calcolato con le tabelle di taglio attuali: si usa quello.
  E' il tempo TEORICO di Lantek, non un tempo misurato in macchina.
- 'modello': modello tarato sui tempi CAM di Lantek, per materiale e spessore:
      secondi = a * taglio_m + b * inneschi + c * marcatura_m + d
  (taglio_m = perimetro di taglio del disegno, inneschi = contorni tagliati).
  Si usa per i codici nuovi, e per quelli che in Lantek hanno il tempo
  calcolato con le tabelle vecchie (prima di `valido_dal`, vedi il JSON) o
  con un altro materiale/spessore: in quel caso la geometria e' quella di Lantek.
- 'stima': materiale o spessore che il modello non conosce bene (lontani dai
  dati): si prende il gruppo piu' vicino, con incertezza larga.

Lantek si legge in SOLA LETTURA (lantek._connessione: READ UNCOMMITTED,
APP=FerroTrack-lettura) con una cache in memoria; se Lantek non risponde si
passa al modello senza errori e per un minuto non si riprova.

I coefficienti stanno in tempi_laser_modello.json, accanto a questo file,
prodotto da tools/calibra_tempi_laser.py (si rilancia quando Lantek cresce).

`incertezza` e' relativa al tempo di Lantek: 0.15 = l'80% dei pezzi simili
sta entro circa +-15% dal tempo CAM.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time

logger = logging.getLogger(__name__)

PERCORSO_MODELLO = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tempi_laser_modello.json')

# Carico/scarico: costanti del titolare (laser_calendario in app_config)
CARICO_MIN_LAMIERA = 5.0
SCARICO_S_PEZZO = 3.0

_MOD = {'mtime': None, 'dati': None}
_MOD_LOCK = threading.Lock()

# cache Lantek: codice in maiuscolo -> (istante, dati | None)
_CACHE: dict = {}
_CACHE_LOCK = threading.Lock()
DURATA_CACHE_S = 600
_PAUSA_DOPO_ERRORE_S = 60
_ULTIMO_ERRORE = {'t': 0.0}


# ---------------------------------------------------------------------------
# Modello
# ---------------------------------------------------------------------------
def carica_modello(percorso: str | None = None) -> dict | None:
    """Il modello dal JSON (riletto se il file cambia). None se manca."""
    p = percorso or PERCORSO_MODELLO
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return None
    with _MOD_LOCK:
        if percorso is None and _MOD['mtime'] == mt and _MOD['dati'] is not None:
            return _MOD['dati']
        try:
            with open(p, encoding='utf-8') as fh:
                dati = json.load(fh)
        except (OSError, ValueError) as e:
            logger.warning('modello tempi laser illeggibile: %s', e)
            return None
        if percorso is None:
            _MOD.update(mtime=mt, dati=dati)
        return dati


# Materiali di FerroTrack -> famiglia di Lantek (FERRO / INOX / ALLUMINIO / ZINCATO)
_FAMIGLIE = (
    ('GEN_', None),
    ('ZINC', 'ZINCATO'), ('DX51', 'ZINCATO'), ('GALV', 'ZINCATO'),
    ('INOX', 'INOX'), ('AISI', 'INOX'), ('304', 'INOX'), ('316', 'INOX'), ('430', 'INOX'),
    ('ALLUM', 'ALLUMINIO'), ('ALU', 'ALLUMINIO'), ('AL5', 'ALLUMINIO'), ('AL6', 'ALLUMINIO'),
    ('FERRO', 'FERRO'), ('S235', 'FERRO'), ('S275', 'FERRO'), ('S355', 'FERRO'),
    ('DC0', 'FERRO'), ('FE', 'FERRO'), ('ACCIAIO', 'FERRO'), ('C75', 'FERRO'), ('DD11', 'FERRO'),
)


def materiale_lantek(materiale) -> str | None:
    """'S235' -> 'FERRO', 'INOX_304' -> 'INOX', 'GEN_INOX' -> 'INOX', 'ALU' -> 'ALLUMINIO'."""
    m = str(materiale or '').strip().upper()
    if m.startswith('GEN_'):
        m = m[4:]
    if not m:
        return None
    for pref, fam in _FAMIGLIE[1:]:
        if m.startswith(pref):
            return fam
    return None


def _num(v) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _gruppo(modello: dict, mat: str | None, sp: float) -> tuple[dict | None, str]:
    """Coefficienti per materiale e spessore: (coeff, fonte).

    Gruppo tarato -> 'modello'. Spessore tra due tarati dello stesso materiale
    -> interpolazione lineare ('modello'). Fuori dai tarati, o materiale senza
    dati -> il piu' vicino ('stima', incertezza allargata)."""
    gruppi = modello.get('gruppi') or {}
    mat = mat or 'FERRO'
    chiave = '%s|%g' % (mat, sp)
    if chiave in gruppi:
        return gruppi[chiave], 'modello'
    stesso = sorted((float(k.split('|')[1]), g) for k, g in gruppi.items() if k.split('|')[0] == mat)
    fonte = 'modello'
    if not stesso:
        alt = (modello.get('materiale_simile') or {}).get(mat, 'FERRO')
        stesso = sorted((float(k.split('|')[1]), g) for k, g in gruppi.items() if k.split('|')[0] == alt)
        fonte = 'stima'
        if not stesso:
            return None, 'stima'
    uguali = [g for s, g in stesso if abs(s - sp) < 1e-6]
    if uguali:
        return uguali[0], fonte
    sotto =[x for x in stesso if x[0] < sp]
    sopra = [x for x in stesso if x[0] > sp]
    if sotto and sopra:
        (s0, g0), (s1, g1) = sotto[-1], sopra[0]
        f = (sp - s0) / (s1 - s0)
        g = {k: g0.get(k, 0.0) * (1 - f) + g1.get(k, 0.0) * f for k in ('a', 'b', 'c', 'd', 'p10', 'p90')}
        # interpolare tra spessori lontani e' meno sicuro
        g['larghezza_extra'] = 0.0 if s1 - s0 <= 2 else 0.5
        return g, fonte
    s0, g0 = (sotto[-1] if sotto else sopra[0])
    g = dict(g0)
    # fuori dai dati: la velocita' cambia molto con lo spessore -> scala grezza
    # sul taglio (tempo ~ spessore) e incertezza larga
    if s0 > 0 and sp > 0:
        g['a'] = g0['a'] * (sp / s0)
        g['b'] = g0['b'] * max(1.0, sp / s0)
    g['larghezza_extra'] = 1.0
    return g, 'stima'


def predici(modello: dict, materiale, spessore, taglio_m, inneschi, marcatura_m=0.0) -> dict | None:
    """Secondi del modello per un pezzo, senza Lantek.
    {'secondi', 'fonte': 'modello'|'stima', 'incertezza'} oppure None se mancano i dati."""
    sp = _num(spessore)
    L = _num(taglio_m)
    if sp <= 0 or L <= 0 or not modello:
        return None
    mat = materiale_lantek(materiale) or (str(materiale).strip().upper() if materiale else None)
    g, fonte = _gruppo(modello, mat, round(sp, 2))
    if g is None:
        return None
    N = max(1.0, _num(inneschi))
    M = max(0.0, _num(marcatura_m))
    sec = g['a'] * L + g['b'] * N + g.get('c', 0.0) * M + g.get('d', 0.0)
    if materiale_lantek(materiale) is None:
        fonte = 'stima'
    # banda P10-P90 del rapporto modello/Lantek -> mezza larghezza relativa
    inc = max(0.03, (g.get('p90', 1.15) - g.get('p10', 0.85)) / 2.0)
    inc *= 1.0 + g.get('larghezza_extra', 0.0)
    if fonte == 'stima':
        inc = max(inc, 0.35)
    return {'secondi': round(max(sec, 0.5), 1), 'fonte': fonte, 'incertezza': round(inc, 3)}


# ---------------------------------------------------------------------------
# Lantek (sola lettura, con cache)
# ---------------------------------------------------------------------------
def _leggi_lantek(codici: list) -> dict | None:
    """{CODICE: {'cam': [{mat, sp, data, t, L_cam, N}], 'geo': {mat, sp, L, M}}}
    per i codici chiesti. None se Lantek non e' leggibile."""
    from .. import lantek
    cfg = lantek._config()
    if not cfg.get('abilitato'):
        return None
    out = {c: {'cam': [], 'geo': None} for c in codici}
    con, cur = lantek._connessione(cfg)
    try:
        for i in range(0, len(codici), 500):
            blocco = codici[i:i + 500]
            segni = ','.join('?' * len(blocco))
            cur.execute('SELECT UPPER(LTRIM(RTRIM(PartRef))), MatRef, Thickness, LastDate, MCode, DValue '
                        'FROM DIS_SHPR_PPTT_00000200 '
                        "WHERE MCode IN ('ND025','NB002','NC001') AND UPPER(LTRIM(RTRIM(PartRef))) IN (" + segni + ')',
                        blocco)
            tmp = {}
            for ref, mat, sp, ld, mc, val in cur.fetchall():
                tmp.setdefault((ref, mat, sp, ld), {})[mc] = val
            for (ref, mat, sp, ld), d in tmp.items():
                if ref in out and d.get('ND025'):
                    out[ref]['cam'].append({
                        'mat': materiale_lantek(mat), 'sp': _num(sp),
                        'data': ld.isoformat()[:10] if hasattr(ld, 'isoformat') else str(ld or '')[:10],
                        't': _num(d.get('ND025')), 'N': _num(d.get('NC001'))})
            cur.execute('SELECT UPPER(LTRIM(RTRIM(PrdRef))), DIS_MatRef, DIS_Thickness, DIS_CutPerim, DIS_MrkPerim, '
                        'DIS_Area FROM PPRR_PPRR_00000100 WHERE UPPER(LTRIM(RTRIM(PrdRef))) IN (' + segni + ')', blocco)
            for ref, mat, sp, per, mrk, area in cur.fetchall():
                if ref in out:
                    out[ref]['geo'] = {'mat': materiale_lantek(mat), 'sp': _num(sp),
                                       'L': _num(per), 'M': _num(mrk), 'area_dm2': _num(area) * 100}
    finally:
        con.close()
    return out


def dati_lantek(codici, lettore=None) -> dict:
    """Dati CAM e geometria di Lantek per i codici (dalla cache se recenti).
    Codici sconosciuti o Lantek spento -> assenti dal risultato. Non solleva."""
    lettore = lettore or _leggi_lantek
    codici = sorted({str(c).strip().upper() for c in codici if c and str(c).strip()})
    ora = time.time()
    out, mancano = {}, []
    with _CACHE_LOCK:
        for c in codici:
            v = _CACHE.get(c)
            if v and ora - v[0] < DURATA_CACHE_S:
                if v[1] is not None:
                    out[c] = v[1]
            else:
                mancano.append(c)
    if not mancano or ora - _ULTIMO_ERRORE['t'] < _PAUSA_DOPO_ERRORE_S:
        return out
    try:
        letti = lettore(mancano)
    except Exception as e:  # Lantek spento, driver mancante, rete...
        logger.info('tempi laser: Lantek non leggibile (%s), si usa il modello', e)
        letti = None
    if letti is None:
        _ULTIMO_ERRORE['t'] = time.time()
        return out
    with _CACHE_LOCK:
        if len(_CACHE) > 20000:
            _CACHE.clear()
        for c in mancano:
            d = letti.get(c)
            d = d if d and (d.get('cam') or d.get('geo')) else None
            _CACHE[c] = (ora, d)
            if d is not None:
                out[c] = d
    return out


def svuota_cache() -> None:
    """Rilegge Lantek alla prossima richiesta (es. dopo un import di pezzi)."""
    with _CACHE_LOCK:
        _CACHE.clear()
    _ULTIMO_ERRORE['t'] = 0.0


# ---------------------------------------------------------------------------
# Tempo di un pezzo / di un ordine
# ---------------------------------------------------------------------------
def _articolo_val(articolo: dict, *chiavi):
    for k in chiavi:
        if articolo.get(k) not in (None, ''):
            return articolo.get(k)
    return None


def _tempo_con_dati(codice, articolo, materiale, spessore, lk, modello) -> dict:
    articolo = articolo or {}
    materiale = materiale or articolo.get('materiale')
    spessore = _num(spessore or articolo.get('spessore_mm'))
    mat = materiale_lantek(materiale)
    valido_dal = (modello or {}).get('valido_dal') or '0000'
    nota = ''
    if lk:
        # tempo CAM calcolato per lo stesso materiale e spessore, con le tabelle attuali
        buoni = [r for r in lk.get('cam') or []
                 if r['t'] > 0 and (mat is None or r['mat'] == mat)
                 and (spessore <= 0 or abs(r['sp'] - spessore) < 0.01)]
        if buoni:
            r = max(buoni, key=lambda x: x['data'])
            if r['data'] >= valido_dal:
                return {'secondi': round(r['t'], 1), 'fonte': 'lantek', 'incertezza': 0.0,
                        'nota': 'tempo CAM di Lantek del %s' % r['data'],
                        'materiale': r['mat'], 'spessore_mm': r['sp']}
            nota = 'tempo CAM di Lantek calcolato con le tabelle vecchie (%s): ricalcolato' % r['data']
        elif lk.get('cam'):
            nota = 'in Lantek il tempo e\' per un altro materiale/spessore: ricalcolato'
    # geometria: quella del disegno di FerroTrack, altrimenti quella di Lantek
    L = _num(_articolo_val(articolo, 'perimetro_taglio_m'))
    N = _num(_articolo_val(articolo, 'n_forature', 'n_pierce'))
    M = _num(_articolo_val(articolo, 'marcatura_m'))
    geo = (lk or {}).get('geo') or {}
    if L <= 0 and geo.get('L'):
        L, M = geo['L'], geo.get('M') or M
        cams = [r for r in (lk or {}).get('cam') or [] if r.get('N')]
        if cams and N <= 0:
            N = max(cams, key=lambda x: x['data'])['N']
        nota = (nota + '; ' if nota else '') + 'geometria di Lantek'
    if spessore <= 0 and geo.get('sp'):
        spessore = geo['sp']
    if not mat and geo.get('mat'):
        mat = materiale = geo['mat']
    p = predici(modello, materiale, spessore, L, N, M) if modello else None
    if p is None:
        mancante = 'modello mancante' if not modello else (
            'perimetro mancante' if L <= 0 else 'spessore mancante' if spessore <= 0 else 'dati mancanti')
        return {'secondi': None, 'fonte': 'mancante', 'incertezza': None, 'nota': mancante,
                'materiale': mat, 'spessore_mm': spessore or None}
    p.update(nota=nota, materiale=mat or materiale_lantek(materiale), spessore_mm=spessore)
    return p


def tempo_pezzo(codice=None, articolo: dict | None = None, materiale=None, spessore=None,
                usa_lantek: bool = True, modello: dict | None = None, lettore=None) -> dict:
    """Secondi di laser di UN pezzo.

    articolo: dict di FerroTrack (perimetro_taglio_m, n_forature, materiale,
    spessore_mm, eventuale marcatura_m). materiale/spessore, se dati, vincono
    su quelli dell'articolo.
    Ritorna {'secondi': float|None, 'fonte': 'lantek'|'modello'|'stima'|'mancante',
             'incertezza': float|None, 'nota': str, 'materiale': famiglia Lantek
             usata, 'spessore_mm': spessore usato}."""
    modello = modello or carica_modello()
    lk = None
    if usa_lantek and codice:
        lk = dati_lantek([codice], lettore).get(str(codice).strip().upper())
    return _tempo_con_dati(codice, articolo, materiale, spessore, lk, modello)


def lamiere_stimate(area_m2: float, materiale, spessore, modello: dict | None = None) -> float:
    """Fogli per un'area di pezzi (stessa lamiera): area / (area foglio x resa).
    Frazionario: la lamiera si divide con gli altri ordini dello stesso spessore."""
    modello = modello or carica_modello() or {}
    lam = modello.get('lamiere') or {}
    mat = materiale_lantek(materiale) or 'FERRO'
    g = lam.get('%s|%g' % (mat, round(_num(spessore), 2))) or lam.get(mat) or lam.get('*') \
        or {'area_m2': 4.5, 'resa': 0.65}
    a = _num(area_m2)
    return a / (g['area_m2'] * g['resa']) if a > 0 else 0.0


def tempo_ordine(righe: list, usa_lantek: bool = True, carico_min_lamiera: float = CARICO_MIN_LAMIERA,
                 scarico_s_pezzo: float = SCARICO_S_PEZZO, modello: dict | None = None,
                 lettore=None, lamiere_intere: bool = True) -> dict:
    """Tempo di laser di un ordine: taglio + carico lamiere + scarico pezzi.

    righe: [{codice, quantita, materiale, spessore_mm, perimetro_taglio_m,
             n_forature, area_dm2, ...}] (solo lamiere).
    Taglio: somma dei tempi dei pezzi x fattore foglio del materiale (sul
    foglio Lantek mette anche i movimenti tra un pezzo e l'altro: +3% circa).
    Lamiere: per materiale+spessore, area dei pezzi / (foglio x resa), formato
    e resa tarati sui nesting di Lantek. lamiere_intere=True (predefinito)
    arrotonda per eccesso ogni gruppo: sui nesting passati e' la regola che
    indovina meglio i fogli caricati per un ordine; False da' la frazione
    (utile per sommare piu' ordini sulla stessa lamiera).
    Ritorna {secondi, secondi_taglio, secondi_carico, secondi_scarico, lamiere,
             pezzi, incertezza, fonti: {lantek: n, ...}, mancanti, per: [...], righe: [...]}"""
    modello = modello or carica_modello()
    codici = [r.get('codice') for r in righe if r.get('codice')]
    lk = dati_lantek(codici, lettore) if usa_lantek and codici else {}
    taglio_pezzi = var = 0.0
    fonti, gruppi, dett = {}, {}, []
    mancanti = pezzi = 0
    for r in righe:
        q = max(0, int(_num(r.get('quantita')) or 1))
        cod = str(r.get('codice') or '').strip().upper()
        t = _tempo_con_dati(cod, r, r.get('materiale'), r.get('spessore_mm'), lk.get(cod), modello)
        fonti[t['fonte']] = fonti.get(t['fonte'], 0) + q
        dett.append({'codice': r.get('codice'), 'quantita': q, **t})
        pezzi += q
        if t['secondi'] is None:
            mancanti += q
            continue
        taglio_pezzi += t['secondi'] * q
        # errori dei pezzi dello stesso codice: tutti nello stesso verso
        var += (t['secondi'] * q * (t['incertezza'] or 0)) ** 2
        # materiale e spessore usati (della riga, o di Lantek se la riga non li ha)
        mat = t.get('materiale') or 'FERRO'
        sp = round(_num(t.get('spessore_mm')), 2)
        area_dm2 = _num(r.get('area_dm2')) or _num(((lk.get(cod) or {}).get('geo') or {}).get('area_dm2'))
        g = gruppi.setdefault('%s|%g' % (mat, sp), {'chiave': '%s|%g' % (mat, sp), 'mat': mat, 'sp': sp,
                                                     'area_m2': 0.0, 'secondi_taglio': 0.0, 'pezzi': 0})
        g['area_m2'] += area_dm2 / 100.0 * q
        g['secondi_taglio'] += t['secondi'] * q
        g['pezzi'] += q
    ff = (modello or {}).get('fattore_foglio') or {}
    taglio = 0.0
    for g in gruppi.values():
        f = _num(ff.get(g['mat']) or ff.get('*')) or 1.0
        g['secondi_taglio'] = round(g['secondi_taglio'] * f, 1)
        taglio += g['secondi_taglio']
    lamiere = 0.0
    for g in gruppi.values():
        n = lamiere_stimate(g['area_m2'], g['mat'], g['sp'], modello)
        if lamiere_intere:
            n = float(math.ceil(n - 1e-9)) if n > 0 else 1.0
        g['lamiere'] = round(n, 2)
        lamiere += n
    carico = lamiere * carico_min_lamiera * 60.0
    scarico = (pezzi - mancanti) * scarico_s_pezzo
    return {
        'secondi': round(taglio + carico + scarico, 1),
        'secondi_taglio': round(taglio, 1), 'secondi_carico': round(carico, 1),
        'secondi_scarico': round(scarico, 1), 'lamiere': round(lamiere, 2), 'pezzi': pezzi,
        'incertezza': round(math.sqrt(var) / taglio_pezzi, 3) if taglio_pezzi > 0 else None,
        'fonti': fonti, 'mancanti': mancanti,
        'per': sorted(gruppi.values(), key=lambda g: g['chiave']), 'righe': dett,
    }
