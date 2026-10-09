"""Confronto motore integrato (modello on, modelli per fold) vs base, DXF + DWG, per gruppo cliente."""
import collections
import sys

import comune

coppie = [('dxf', sys.argv[1], sys.argv[2]), ('dwg', sys.argv[3], sys.argv[4])]
info = comune.info_codici()
gt = comune.carica_gt()
C = collections.Counter()
peggio, silenzio, meglio = [], [], []
tempi = []
for fonte, p_on, p_base in coppie:
    on, base = comune.leggi_jsonl(p_on), comune.leggi_jsonl(p_base)
    for c, r in on.items():
        if c not in base or c not in gt[fonte]:
            continue
        g = gt[fonte][c]
        e1, e0 = comune.rep.valuta(r, g), comune.rep.valuta(base[c], g)
        cl = (info.get((fonte, c)) or {}).get('cliente')
        for grp in ('TUTTI', ('DECA' if comune.deca(cl) else 'altri') + '/' + fonte):
            C[grp, 'n'] += 1
            C[grp, 'geo0'] += e0['geo']
            C[grp, 'geo1'] += e1['geo']
            C[grp, 's0'] += e0['sicuro']
            C[grp, 's1'] += e1['sicuro']
            C[grp, 's0ok'] += e0['sicuro'] and e0['geo']
            C[grp, 's1ok'] += e1['sicuro'] and e1['geo']
            C[grp, 'peggio'] += e0['geo'] and not e1['geo']
            C[grp, 'meglio'] += e1['geo'] and not e0['geo']
            C[grp, 'silenzio'] += e1['sicuro'] and not e1['geo'] and not (e0['sicuro'] and not e0['geo'])
            C[grp, 'cambiati'] += bool((r.get('modello') or {}).get('cambiato'))
        if e0['geo'] and not e1['geo']:
            peggio.append((fonte, c, e1['tipo']))
        if e1['geo'] and not e0['geo']:
            meglio.append((fonte, c))
        if e1['sicuro'] and not e1['geo'] and not (e0['sicuro'] and not e0['geo']):
            silenzio.append((fonte, c, e1['tipo'], e0['sicuro']))
        tempi.append(r.get('t') or 0)
print(f"{'gruppo':12s} {'disegni':>7s} {'geo base':>8s} {'geo mod':>8s} {'sicuri base':>11s} {'sbagl':>5s} {'prec':>6s} "
      f"{'sicuri mod':>10s} {'sbagl':>5s} {'prec':>6s} {'migl':>5s} {'pegg':>5s} {'nuovi silenz':>12s} {'cambi rank':>10s}")
for grp in sorted({k[0] for k in C}):
    n = C[grp, 'n']
    s0, s1 = C[grp, 's0'], C[grp, 's1']
    print(f"{grp:12s} {n:7d} {C[grp, 'geo0']:8d} {C[grp, 'geo1']:8d} {s0:11d} {s0 - C[grp, 's0ok']:5d} "
          f"{100 * C[grp, 's0ok'] / max(s0, 1):6.2f} {s1:10d} {s1 - C[grp, 's1ok']:5d} {100 * C[grp, 's1ok'] / max(s1, 1):6.2f} "
          f"{C[grp, 'meglio']:5d} {C[grp, 'peggio']:5d} {C[grp, 'silenzio']:12d} {C[grp, 'cambiati']:10d}")
tempi.sort()
print(f'tempo mediana {tempi[len(tempi) // 2]:.2f}s, 95% {tempi[int(.95 * len(tempi))]:.2f}s, max {tempi[-1]:.1f}s')
print('PEGGIORATI', peggio[:30])
print('NUOVI SILENZIOSI', silenzio[:60])

# ---- what-if: altre soglie (per fold, annidate) sulle probabilita' del run integrato
if len(sys.argv) > 5:
    import json
    tab = json.load(open(sys.argv[5]))['soglie_annidate']
    righe = []
    for fonte, p_on, p_base in coppie:
        on = comune.leggi_jsonl(p_on)
        for c, r in on.items():
            if c not in gt[fonte]:
                continue
            m = r.get('modello') or {}
            k = (info.get((fonte, c)) or {}).get('fold')
            e1 = comune.rep.valuta(r, gt[fonte][c])
            righe.append((k, m.get('p_sicuro'), not m.get('fisse'), e1['geo'], fonte))
    for pr in ('0.99', '0.995', '0.998'):
        n = ok = 0
        for k, p, amm, geo, fonte in righe:
            if p is not None and amm and p >= tab[str(k)][f'sostituisce|{pr}']:
                n += 1
                ok += geo
        print(f'what-if obiettivo {pr}: sicuri {n}, sbagliati {n - ok}, precisione {100 * ok / max(n, 1):.2f}%, '
              f'giusti e sicuri {ok}')
