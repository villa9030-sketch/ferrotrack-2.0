"""Taratura del modello dei tempi laser sui tempi CAM di Lantek.

    .venv\\Scripts\\python.exe tools\\calibra_tempi_laser.py [--valido-dal 2022-11-01] [--prova]

Legge da Lantek (SOLA LETTURA: solo SELECT, READ UNCOMMITTED,
APP=FerroTrack-lettura) i pezzi col tempo CAM e i nesting, e scrive
backend/preventivi/tempi_laser_modello.json:

- per ogni materiale+spessore:  secondi = a*taglio_m + b*inneschi + c*marcatura_m + d
  (taglio_m = DIS_CutPerim, inneschi = NC001, marcatura = DIS_MrkPerim; il
  tempo e' ND025 "Total time"). Regressione robusta sull'errore RELATIVO
  (minimi quadrati pesati 1/t, pesi di Huber), coefficienti non negativi.
  p10/p90 = rapporto modello/Lantek sui pezzi della taratura.
- solo i tempi CAM calcolati dal `valido_dal` in poi: prima (import di aprile
  2022 e tabelle fino a ottobre 2022) Lantek usava altre velocita' di taglio
  (es. FERRO 2 mm 5.300 mm/min contro 11.700 di adesso).
- fattore_foglio: tempo del foglio (ETime del nesting) / somma dei tempi
  CAM dei pezzi sul foglio, per materiale (movimenti tra i pezzi, avvio).
- lamiere: formato mediano e resa (area netta dei pezzi / area dei fogli,
  sommate) dei nesting per materiale+spessore, dal `valido_dal`.

--prova: calcola e stampa senza scrivere il JSON.
Si rilancia quando Lantek cresce: i pezzi nuovi entrano nella taratura.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

import numpy as np

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if APP not in sys.path:
    sys.path.insert(0, APP)
# importare il pacchetto backend carica l'app: niente inizializzazione del
# database di FerroTrack (qui si legge solo Lantek)
os.environ.setdefault('FERROTRACK_SKIP_DB_INIT', '1')

from backend.preventivi.tempi_laser import PERCORSO_MODELLO, materiale_lantek  # noqa: E402

VALIDO_DAL = '2022-11-01'
MIN_PEZZI = 12
MATERIALE_SIMILE = {'ZINCATO': 'FERRO', 'ALLUMINIO': 'INOX', 'INOX': 'FERRO', 'FERRO': 'INOX'}


# ---------------------------------------------------------------------------
# Lettura (sola) da Lantek
# ---------------------------------------------------------------------------
def leggi_parti_lantek() -> list:
    """[{codice, mat, sp, data, t, L, N, M}] : un record per pezzo, il CAM piu' recente."""
    from backend import lantek
    con, cur = lantek._connessione(lantek._config())
    try:
        cur.execute("SELECT UPPER(LTRIM(RTRIM(PartRef))), MatRef, Thickness, LastDate, MCode, DValue "
                    "FROM DIS_SHPR_PPTT_00000200 WHERE MCode IN ('ND025','NC001')")
        cam = cur.fetchall()
        cur.execute('SELECT UPPER(LTRIM(RTRIM(PrdRef))), DIS_CutPerim, DIS_MrkPerim, DIS_Area '
                    'FROM PPRR_PPRR_00000100 WHERE PType = 0')
        geo = {r[0]: r[1:] for r in cur.fetchall()}
    finally:
        con.close()
    tmp = {}
    for ref, mat, sp, ld, mc, val in cam:
        tmp.setdefault((ref, mat, sp, ld), {})[mc] = val
    ultimo = {}
    for (ref, mat, sp, ld), d in tmp.items():
        if ref not in ultimo or ld > ultimo[ref][2]:
            ultimo[ref] = (mat, sp, ld, d)
    out = []
    for ref, (mat, sp, ld, d) in ultimo.items():
        g = geo.get(ref)
        if not g:
            continue
        out.append({'codice': ref, 'mat': materiale_lantek(mat), 'sp': float(sp or 0),
                    'data': ld.isoformat()[:10], 't': float(d.get('ND025') or 0),
                    'L': float(g[0] or 0), 'N': float(d.get('NC001') or 0), 'M': float(g[1] or 0),
                    'area_m2': float(g[2] or 0)})
    return out


def leggi_lamiere_lantek(valido_dal: str = VALIDO_DAL) -> list:
    """[{mat, sp, area_m2 (foglio), usata_m2 (area NETTA dei pezzi sul foglio), fogli}]
    dei nesting dal `valido_dal`. La quantita' di ogni pezzo nel nesting
    (DIS_NEST_NEST_00000500) e' per foglio; Quantity del nesting = fogli uguali."""
    from backend import lantek
    con, cur = lantek._connessione(lantek._config())
    try:
        cur.execute('SELECT n.MatRef, n.SThickness, n.SArea, n.Quantity, SUM(p.DIS_Area * l.Quantity) '
                    'FROM DIS_NEST_NEST_00000100 n '
                    'JOIN DIS_NEST_NEST_00000500 l ON l.NstRef = n.NstRef '
                    'JOIN PPRR_PPRR_00000100 p ON p.PrdRef = l.PrdRefDst '
                    'WHERE n.SArea > 0 AND n.CDate >= ? '
                    'GROUP BY n.NstRef, n.MatRef, n.SThickness, n.SArea, n.Quantity', [valido_dal])
        righe = cur.fetchall()
    finally:
        con.close()
    return [{'mat': materiale_lantek(m), 'sp': float(s or 0), 'area_m2': float(a), 'usata_m2': float(u or 0),
             'fogli': int(q or 1)} for m, s, a, q, u in righe]


def leggi_fogli_lantek(valido_dal: str = VALIDO_DAL) -> list:
    """[{mat, etime (s per foglio), fogli, pezzi: [(codice, quantita' per foglio)]}]
    dei nesting dal `valido_dal`: serve al fattore foglio (tempo del foglio
    rispetto alla somma dei tempi dei pezzi che ci stanno sopra)."""
    from backend import lantek
    con, cur = lantek._connessione(lantek._config())
    try:
        cur.execute('SELECT n.NstRef, n.MatRef, n.ETime, n.Quantity, UPPER(LTRIM(RTRIM(l.PrdRefDst))), l.Quantity '
                    'FROM DIS_NEST_NEST_00000100 n JOIN DIS_NEST_NEST_00000500 l ON l.NstRef = n.NstRef '
                    'WHERE n.ETime > 0 AND n.CDate >= ?', [valido_dal])
        righe = cur.fetchall()
    finally:
        con.close()
    per = {}
    for ref, mat, et, q, cod, qp in righe:
        f = per.setdefault(ref, {'mat': materiale_lantek(mat), 'etime': float(et), 'fogli': int(q or 1), 'pezzi': []})
        f['pezzi'].append((cod, float(qp or 0)))
    return list(per.values())


def tara_fattore_foglio(fogli: list, parti: list, valido_dal: str = VALIDO_DAL) -> dict:
    """Tempo dei fogli / somma dei tempi CAM dei pezzi, per materiale (somme pesate).
    Il di piu' sono i movimenti a vuoto tra un pezzo e l'altro e l'avvio del foglio."""
    t = {p['codice']: p['t'] for p in parti if p['t'] > 0 and p['data'] >= valido_dal}
    num, den = {}, {}
    for f in fogli:
        if not f['mat'] or any(c not in t for c, _q in f['pezzi']):
            continue
        s = sum(t[c] * q for c, q in f['pezzi'])
        if s <= 0:
            continue
        for k in (f['mat'], '*'):
            num[k] = num.get(k, 0.0) + f['etime'] * f['fogli']
            den[k] = den.get(k, 0.0) + s * f['fogli']
    return {k: round(min(max(num[k] / den[k], 1.0), 1.3), 3) for k in sorted(num)}


# ---------------------------------------------------------------------------
# Taratura
# ---------------------------------------------------------------------------
def _valida(p: dict) -> bool:
    return p.get('mat') and p['sp'] > 0 and p['t'] > 0 and p['L'] > 0 and p['N'] >= 1 \
        and p['t'] < 4 * 3600


def regressione(X: np.ndarray, t: np.ndarray, iterazioni: int = 12) -> np.ndarray:
    """Minimi quadrati sull'errore relativo (pesi 1/t) con pesi di Huber e
    coefficienti >= 0 (si toglie il piu' negativo e si rifa')."""
    attive = [j for j in range(X.shape[1]) if np.any(X[:, j] != 0)]
    while True:
        beta = np.zeros(X.shape[1])
        w = 1.0 / t
        for _ in range(iterazioni):
            A = X[:, attive] * w[:, None]
            b = t * w
            sol, *_ = np.linalg.lstsq(A, b, rcond=None)
            beta[:] = 0
            beta[attive] = sol
            r = (X @ beta - t) / t
            s = 1.4826 * np.median(np.abs(r - np.median(r))) or 1e-3
            k = 1.345 * s
            hub = np.minimum(1.0, k / np.maximum(np.abs(r), 1e-12))
            w = np.sqrt(hub) / t
        neg = [j for j in attive if beta[j] < 0]
        if not neg:
            return beta
        attive.remove(min(neg, key=lambda j: beta[j]))
        if not attive:
            return beta


def calibra(parti: list, valido_dal: str = VALIDO_DAL, min_pezzi: int = MIN_PEZZI,
            lamiere: list | None = None, fogli: list | None = None) -> dict:
    """Il modello (dict pronto per il JSON) dai pezzi di Lantek."""
    usati = [p for p in parti if _valida(p) and p['data'] >= valido_dal]
    per = {}
    for p in usati:
        per.setdefault('%s|%g' % (p['mat'], round(p['sp'], 2)), []).append(p)
    gruppi = {}
    for k, ps in sorted(per.items()):
        if len(ps) < min_pezzi:
            continue
        X = np.array([[p['L'], p['N'], p['M'], 1.0] for p in ps])
        t = np.array([p['t'] for p in ps])
        a, b, c, d = regressione(X, t)
        q = (X @ np.array([a, b, c, d])) / t
        gruppi[k] = {'a': round(float(a), 4), 'b': round(float(b), 4), 'c': round(float(c), 4),
                     'd': round(float(d), 3), 'n': len(ps),
                     'p10': round(float(np.quantile(q, 0.1)), 3), 'p90': round(float(np.quantile(q, 0.9)), 3),
                     'errore_mediano': round(float(np.median(np.abs(q - 1))), 3)}
    out = {
        'versione': 1,
        'creato': datetime.now().isoformat(timespec='seconds'),
        'valido_dal': valido_dal,
        'formula': 'secondi = a*taglio_m + b*inneschi + c*marcatura_m + d (ND025 di Lantek)',
        'n_pezzi': len(usati),
        'materiale_simile': MATERIALE_SIMILE,
        'gruppi': gruppi,
    }
    if lamiere is not None:
        out['lamiere'] = tara_lamiere(lamiere)
    if fogli is not None:
        out['fattore_foglio'] = tara_fattore_foglio(fogli, parti, valido_dal)
    return out


def tara_lamiere(lamiere: list, min_nest: int = 10) -> dict:
    """Formato e resa mediani per materiale+spessore (e per materiale, e '*')."""
    def riassunto(ls):
        # resa complessiva (somma pezzi / somma fogli): anche l'ultimo foglio
        # mezzo vuoto e le lamiere piccole pesano per quello che consumano
        a = np.array([x['area_m2'] for x in ls])
        f = np.array([x.get('fogli', 1) for x in ls])
        u = np.array([x['usata_m2'] for x in ls])
        resa = float((u * f).sum() / (a * f).sum())
        return {'area_m2': round(float(np.median(np.repeat(a, f))), 3), 'resa': round(min(max(resa, 0.2), 0.95), 3),
                'n': len(ls)}
    out, per, perm = {}, {}, {}
    for x in lamiere:
        if not x['mat'] or x['usata_m2'] <= 0 or x['usata_m2'] > x['area_m2'] * 1.01:
            continue
        per.setdefault('%s|%g' % (x['mat'], round(x['sp'], 2)), []).append(x)
        perm.setdefault(x['mat'], []).append(x)
    for k, ls in sorted(per.items()):
        if len(ls) >= min_nest:
            out[k] = riassunto(ls)
    for k, ls in sorted(perm.items()):
        if len(ls) >= min_nest:
            out[k] = riassunto(ls)
    tutti = [x for ls in perm.values() for x in ls]
    if tutti:
        out['*'] = riassunto(tutti)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--valido-dal', default=VALIDO_DAL)
    ap.add_argument('--min-pezzi', type=int, default=MIN_PEZZI)
    ap.add_argument('--prova', action='store_true', help='non scrive il JSON')
    ap.add_argument('--uscita', default=PERCORSO_MODELLO)
    a = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    parti = leggi_parti_lantek()
    lam = leggi_lamiere_lantek(a.valido_dal)
    fogli = leggi_fogli_lantek(a.valido_dal)
    m = calibra(parti, a.valido_dal, a.min_pezzi, lam, fogli)
    print('pezzi letti %d, usati %d, gruppi %d, nesting %d, fattore foglio %s'
          % (len(parti), m['n_pezzi'], len(m['gruppi']), len(lam), m['fattore_foglio']))
    for k, g in m['gruppi'].items():
        print('  %-14s n=%5d  a=%7.2f s/m  b=%5.2f s/innesco  c=%5.2f  d=%5.2f  err.med %4.1f%%  P10-P90 %.2f-%.2f'
              % (k, g['n'], g['a'], g['b'], g['c'], g['d'], g['errore_mediano'] * 100, g['p10'], g['p90']))
    if not a.prova:
        with open(a.uscita, 'w', encoding='utf-8') as fh:
            json.dump(m, fh, ensure_ascii=False, indent=1)
        print('scritto', a.uscita)
    return 0


if __name__ == '__main__':
    sys.exit(main())
