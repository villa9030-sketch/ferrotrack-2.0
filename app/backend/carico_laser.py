"""Carico del laser e ritardi: UN solo conto per l'ufficio e per il laser.

Prima il conto stava nel browser (carico-laser.js) e ogni pagina lo faceva a
modo suo: l'ufficio guardava i prossimi 10 giorni e diceva "In ritardo: 0,
laser 0 ore su 60", il laser guardava la settimana da lunedi' e diceva
"3 in ritardo, 5 h di laser" sullo stesso database. Due motivi:

  - "in ritardo" voleva dire due cose: all'ufficio la CONSEGNA passata, al
    laser il "taglia entro" passato;
  - il lavoro da tagliare col "taglia entro" gia' passato pesava su un giorno
    passato: nei prossimi giorni dell'ufficio spariva (0 ore), mentre era
    proprio il lavoro piu' urgente.

Qui, una volta per tutte (lo manda /api/laser/banco, lo leggono entrambe le
pagine tramite carico-laser.js):

  In ritardo         = consegna al cliente passata e merce non consegnata.
  Taglio in ritardo  = "taglia entro" passato e taglio non ancora fatto.
  Ore di laser       = taglio dei pezzi (tempi tarati su Lantek) + carico
                       lamiere + scarico pezzi; il lavoro non tagliato col
                       giorno gia' passato pesa su OGGI.
  Periodo            = i prossimi 5 giorni lavorativi del laser, oggi compreso.

Solo funzioni pure (niente database, niente Flask): si provano da sole.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .taglia_entro import lavorativo

GIORNI_PERIODO = 5
ETICHETTA_PERIODO = 'Laser, prossimi 5 giorni lavorativi'

DEF_CAL = {'ore_turno': 8, 'ore_riserva': 2, 'giorni': [1, 2, 3, 4, 5],
           'carico_min_lamiera': 5, 'scarico_s_pezzo': 3, 'fattore_tempo': 1}


def _g(v) -> date | None:
    if v is None or v == '':
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


# ── lamiere: disposizione per ingombro (stessa regola che stava in carico-laser.js)
def _disponi(pezzi: list, W: float, H: float, marg: float, gap: float) -> dict:
    UW, UH = W - 2 * marg + gap, H - 2 * marg + gap
    items = []
    for p in pezzi:
        w, h = p.get('w_mm') or 0, p.get('h_mm') or 0
        if not (w > 0 and h > 0):
            continue
        for _ in range(min(int(p.get('quantita') or 1), 2000)):
            items.append((float(w), float(h)))
    items.sort(key=lambda it: (-max(it[0], it[1]), -it[0] * it[1]))
    fogli, fuori = [], 0

    def trova(f, w, h):
        best = None
        for (rx, ry, rw, rh) in f['free']:
            for ww, hh in ((w, h), (h, w)):
                if ww > rw or hh > rh:
                    continue
                destra, sotto = rx + ww, ry + hh
                if best is None or destra < best[4] or (destra == best[4] and sotto < best[5]):
                    best = (rx, ry, ww, hh, destra, sotto)
        return best

    def piazza(f, n):
        nx, ny, nw, nh = n[0], n[1], n[2], n[3]
        out = []
        for r in f['free']:
            rx, ry, rw, rh = r
            if nx >= rx + rw or nx + nw <= rx or ny >= ry + rh or ny + nh <= ry:
                out.append(r)
                continue
            if nx > rx:
                out.append((rx, ry, nx - rx, rh))
            if nx + nw < rx + rw:
                out.append((nx + nw, ry, rx + rw - nx - nw, rh))
            if ny > ry:
                out.append((rx, ry, rw, ny - ry))
            if ny + nh < ry + rh:
                out.append((rx, ny + nh, rw, ry + rh - ny - nh))
        tenuti = []
        for i, a in enumerate(out):
            dentro = False
            for j, b in enumerate(out):
                if j == i:
                    continue
                if (a[0] >= b[0] and a[1] >= b[1] and a[0] + a[2] <= b[0] + b[2] and a[1] + a[3] <= b[1] + b[3]
                        and (j < i or a != b)):
                    dentro = True
                    break
            if not dentro:
                tenuti.append(a)
        f['free'] = tenuti

    for (iw, ih) in items:
        w, h = iw + gap, ih + gap
        if not ((w <= UW and h <= UH) or (h <= UW and w <= UH)):
            fuori += 1
            continue
        posto = foglio = None
        for f in fogli:
            posto = trova(f, w, h)
            if posto:
                foglio = f
                break
        if not posto:
            foglio = {'free': [(0.0, 0.0, UW, UH)], 'rects': []}
            fogli.append(foglio)
            posto = trova(foglio, w, h)
        piazza(foglio, posto)
        foglio['rects'].append((marg + posto[0], marg + posto[1], posto[2] - gap, posto[3] - gap))
    consumo = 0.0
    for f in fogli:
        x_max = max([0.0] + [r[0] + r[2] for r in f['rects']])
        avanzo = max(0.0, W - marg - x_max - gap)
        usato = min(W, x_max + gap) / W if avanzo > 50 else 1.0
        consumo += usato * W * H
    n = len(fogli)
    punteggio = (1e15 * fuori + consumo) if fuori else consumo * (1 + 0.04 * max(0, n - 1))
    return {'n': n, 'punteggio': punteggio}


def lamiere(pezzi: list, formati: list, parametri: dict) -> int:
    """Quante lamiere servono a questi pezzi (stesso materiale e spessore),
    col formato che ne consuma meno."""
    if not formati or not any((p.get('w_mm') or 0) > 0 and (p.get('h_mm') or 0) > 0 for p in pezzi):
        return 1 if pezzi else 0
    m, d = float(parametri.get('margine_mm', 10)), float(parametri.get('distanza_mm', 5))
    piani = sorted((_disponi(pezzi, float(f['w_mm']), float(f['h_mm']), m, d) for f in formati),
                   key=lambda x: x['punteggio'])
    return max(1, piani[0]['n'])


def durata_ordine(gruppi_pezzi: list, manuale_min, cal: dict, formati: list, parametri: dict) -> dict:
    """Minuti di laser di un ordine: {'min': float|None, 'fonte': 'stima'|'mano'|'ignota'}.
    gruppi_pezzi = una lista di pezzi per ogni lamiera (materiale+spessore)."""
    c = {**DEF_CAL, **(cal or {})}
    gruppi_pezzi = [g for g in gruppi_pezzi if g]
    if gruppi_pezzi:
        tot = 0.0
        for pezzi in gruppi_pezzi:
            t = sum((p.get('tempo_min') or 0) * (p.get('quantita') or 1) for p in pezzi) * (c.get('fattore_tempo') or 1)
            n = sum(p.get('quantita') or 1 for p in pezzi)
            tot += t + lamiere(pezzi, formati, parametri) * c['carico_min_lamiera'] + n * c['scarico_s_pezzo'] / 60.0
        return {'min': round(tot, 2), 'fonte': 'stima'}
    try:
        man = float(manuale_min or 0)
    except (TypeError, ValueError):
        man = 0
    if man > 0:
        return {'min': man, 'fonte': 'mano'}
    return {'min': None, 'fonte': 'ignota'}


def capacita(g: date, cal: dict) -> float:
    """Minuti utili del laser quel giorno (turno meno riserva; giorni speciali)."""
    c = {**DEF_CAL, **(cal or {})}
    e = (c.get('eccezioni') or {}).get(g.isoformat())
    if isinstance(e, dict):
        try:
            return max(0.0, float(e.get('ore') or 0)) * 60
        except (TypeError, ValueError):
            pass
    if g.isoweekday() in (c.get('giorni') or [1, 2, 3, 4, 5]):
        return max(0.0, float(c['ore_turno']) - float(c['ore_riserva'])) * 60
    return 0.0


def giorni_lavorativi(dal: date, n: int, cal: dict) -> list:
    out, g = [], dal
    for _ in range(400):
        if len(out) >= n:
            break
        if capacita(g, cal) > 0:
            out.append(g)
        g += timedelta(days=1)
    return out


def giorno_carico(tagliato: bool, giorno_taglio, entro, consegna, oggi: date) -> tuple:
    """(giorno su cui l'ordine pesa, arretrato?). Tagliato: il giorno del taglio.
    Da tagliare: il "taglia entro" (o la consegna); se e' gia' passato, OGGI."""
    if tagliato:
        return _g(giorno_taglio), False
    g = _g(entro) or _g(consegna)
    if g is not None and g < oggi:
        return oggi, True
    return g, False


def in_ritardo(fase: str, consegna, oggi: date) -> bool:
    """In ritardo: consegna al cliente passata e merce non consegnata
    (fase dell'ufficio 'aperto' o 'pronto_ddt', vedi ordini_service.fase)."""
    c = _g(consegna)
    return fase in ('aperto', 'pronto_ddt') and c is not None and c < oggi


def riepilogo(ordini: list, cal: dict, oggi: date, ritardo_ids: list) -> dict:
    """ordini: [{'id', 'tagliato', 'escluso', 'giorno': date|None, 'arretrato', 'min'}]
    (quelli che pesano sul laser); ritardo_ids: gli ordini aperti in ritardo
    sulla consegna (vedi in_ritardo).

    {'periodo': {...}, 'in_ritardo': {'n', 'ids'}, 'taglio_in_ritardo': {'n', 'ids', 'min'}}"""
    giorni = giorni_lavorativi(oggi, GIORNI_PERIODO, cal)
    insieme = set(giorni)
    mins, cap, ignoti, n = 0.0, sum(capacita(g, cal) for g in giorni), 0, 0
    tr_ids, tr_min = [], 0.0
    for o in ordini:
        if o.get('escluso'):
            continue
        if o.get('giorno') in insieme:
            n += 1
            if o.get('min') is None:
                ignoti += 1
            else:
                mins += o['min']
        if not o.get('tagliato') and o.get('arretrato'):
            tr_ids.append(o['id'])
            tr_min += o.get('min') or 0
    rit = list(ritardo_ids or [])
    return {
        'periodo': {'etichetta': ETICHETTA_PERIODO, 'dal': giorni[0].isoformat() if giorni else None,
                    'al': giorni[-1].isoformat() if giorni else None, 'giorni': [g.isoformat() for g in giorni],
                    'min': round(mins, 1), 'cap': round(cap, 1), 'ignoti': ignoti, 'n': n},
        'in_ritardo': {'n': len(rit), 'ids': rit,
                       'definizione': 'consegna al cliente passata e merce non ancora consegnata'},
        'taglio_in_ritardo': {'n': len(tr_ids), 'ids': tr_ids, 'min': round(tr_min, 1),
                              'definizione': '"taglia entro" passato e taglio non ancora fatto'},
    }
