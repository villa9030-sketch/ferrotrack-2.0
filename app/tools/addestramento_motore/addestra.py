"""Addestramento ranker dei candidati + modello del "sicuro" (agente I).

    .venv_studio python addestra.py --dump dump_I.jsonl --dump-dwg dump_dwg_I.jsonl --out modelli/

- fold = hash della CARTELLA D'ORDINE (5 fold): mai righe a caso;
- tutti i numeri riportati sono su fold tenuti fuori;
- esporta alberi in JSON: un modello per fold (per misurare l'integrazione sul
  banco senza che un disegno sia giudicato da un modello che l'ha visto) e il
  modello finale (tutti i dati) per la produzione.
"""
import argparse
import collections
import importlib.util
import json
import math
import os

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression

import comune

spec = importlib.util.spec_from_file_location(
    'modello_motore', os.path.join(comune.WT, 'backend', 'preventivi', 'modello_motore.py'))
mm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mm)
FC, FD = mm.FEAT_CAND, mm.FEAT_DIS
iF = {n: i for i, n in enumerate(FD)}
iC = {n: i for i, n in enumerate(FC)}

PRECS = (0.99, 0.995, 0.998)
PAR_RANK = dict(max_iter=300, learning_rate=0.06, max_leaf_nodes=15, min_samples_leaf=30,
                l2_regularization=1.0, early_stopping=False, random_state=0)
PAR_SIC = dict(max_iter=250, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=25,
               l2_regularization=1.0, early_stopping=False, random_state=0)


def fori_dubbi(r):
    """Come geometria['fori_dubbi'] del detector: lettere/scritte alte contate
    come fori, o linee aperte dentro il pezzo che chiudono zone (archi spezzati)
    o curve da bordo a bordo."""
    dg = (r.get('indizi') or {}).get('diag') or {}
    tr = r.get('tratti') or {}
    return bool((dg.get('n_segni') or 0) > 0 or (dg.get('n_scritte_dubbie') or 0) > 0
                or 'facce_c' in tr or (tr.get('bordo2~') or [0, 0.0])[1] > 10.0)


def vicino(a, b, rel, ass):
    return a is not None and b is not None and abs(a - b) <= max(rel * abs(b), ass)


def etichetta_cand(geo, g):
    """1 se il candidato ha l'area esterna di Lantek (1%) e l'ingombro (0,5%/1 mm);
    0,5 se solo l'area (pezzo forse ruotato in Lantek)."""
    a, w, h = geo
    if not vicino(a / 1e4, g['area_est'], 0.01, 0.005):
        return 0
    bb = sorted((w, h))
    if vicino(bb[0], g['bbox'][0], 0.005, 1.0) and vicino(bb[1], g['bbox'][1], 0.005, 1.0):
        return 1
    return 0.5


def carica(args):
    info = comune.info_codici()
    gt = comune.carica_gt()
    rec = []
    for fonte, p in (('dxf', args.dump), ('dwg', args.dump_dwg)):
        if not p:
            continue
        for c, r in comune.leggi_jsonl(p).items():
            g = gt[fonte].get(c)
            if g is None:
                continue
            e = comune.rep.valuta(r, g)
            m = r.get('modello') or {}
            ii = info.get((fonte, c)) or {'cartella': '?', 'cliente': '?', 'fold': 0}
            rec.append({'fonte': fonte, 'codice': c, 'r': r, 'g': g, 'geo_ok': bool(e['geo']),
                        'sicuro_base': bool(e['sicuro']), 'manuale': bool(r.get('manuale')),
                        'conf': r.get('conf'), 'm': m, **ii})
    return rec


# ───────────── export alberi ─────────────
def esporta(clf, feat):
    alberi = []
    for pr in clf._predictors:
        nd = pr[0].nodes
        alberi.append({
            'f': [int(x) for x in nd['feature_idx']],
            's': [float(x) for x in nd['num_threshold']],
            'l': [int(x) if not lf else -1 for x, lf in zip(nd['left'], nd['is_leaf'])],
            'r': [int(x) for x in nd['right']],
            'v': [float(x) for x in nd['value']],
        })
    return {'feat': feat, 'init': float(np.ravel(clf._baseline_prediction)[0]), 'lr': 1.0, 'alberi': alberi}


def controlla_export(m, clf, X):
    z1 = clf.decision_function(X[:200])
    z2 = np.array([mm.grezzo(m, list(x)) for x in X[:200]])
    assert np.max(np.abs(z1 - z2)) < 1e-6, np.max(np.abs(z1 - z2))


# ───────────── ranker ─────────────
def dati_ranker(rec):
    X, y, gid, base = [], [], [], []
    per = []     # per disegno: (k_rec, righe)
    for k, d in enumerate(rec):
        cand = d['m'].get('cand') or {}
        if not cand.get('X'):
            continue
        lab = [etichetta_cand(geo, d['g']) for geo in cand['geo']]
        if 1 in lab:
            lab = [1 if v == 1 else 0 for v in lab]
        else:
            lab = [1 if v == 0.5 else 0 for v in lab]
        i0 = len(X)
        for x, v in zip(cand['X'], lab):
            X.append(x)
            y.append(v)
            gid.append(k)
        per.append((k, list(range(i0, len(X)))))
    return np.array(X, dtype=float), np.array(y), np.array(gid), per


def addestra_ranker(X, y, w=None):
    return HistGradientBoostingClassifier(**PAR_RANK).fit(X, y, sample_weight=w)


def pesi_ranker(y, gid):
    # ogni disegno pesa uguale (molti candidati = non piu' importante)
    cnt = collections.Counter(gid)
    return np.array([1.0 / cnt[g] for g in gid]) * 10


# ───────────── soglia ─────────────
def soglia_per(p, y, prec):
    """Soglia piu' bassa con precisione cumulata >= prec (ordinando per p decrescente)."""
    o = np.argsort(-p, kind='stable')
    ps, ys = p[o], y[o]
    cum = np.cumsum(ys) / np.arange(1, len(ys) + 1)
    best = None
    for i in range(len(ps)):
        if cum[i] >= prec and (i + 1 == len(ps) or ps[i + 1] < ps[i]):
            best = ps[i]
    return best if best is not None else 1.01


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dump', required=True)
    ap.add_argument('--dump-dwg')
    ap.add_argument('--out', required=True)
    ap.add_argument('--prec', type=float, default=0.995)
    ap.add_argument('--politica', default='sostituisce', choices=('sostituisce', 'promuove'))
    ap.add_argument('--par-sic', help='json: parametri del modello del sicuro')
    ap.add_argument('--escludi-fori-dubbi', action='store_true',
                    help='regola fissa: fori dubbi (lettere, archi spezzati) mai sicuri')
    args = ap.parse_args()
    if args.par_sic:
        PAR_SIC.update(json.loads(args.par_sic))
    os.makedirs(args.out, exist_ok=True)
    rec = carica(args)
    N = len(rec)
    fold = np.array([d['fold'] for d in rec])
    print(f'disegni {N} (dxf {sum(d["fonte"] == "dxf" for d in rec)}, dwg {sum(d["fonte"] == "dwg" for d in rec)}); '
          f'per fold {collections.Counter(fold.tolist())}; cartelle {len({d["cartella"] for d in rec})}')
    print(f'geometria giusta {sum(d["geo_ok"] for d in rec)}; sicuri a regole {sum(d["sicuro_base"] for d in rec)} '
          f'di cui giusti {sum(d["sicuro_base"] and d["geo_ok"] for d in rec)}')

    # ===== RANKER =====
    X, y, gid, per = dati_ranker(rec)
    fold_c = fold[gid]
    w = pesi_ranker(y, gid)
    p_oof = np.zeros(len(y))
    rank_fold = {}
    for k in range(5):
        tr, te = fold_c != k, fold_c == k
        clf = addestra_ranker(X[tr], y[tr], w[tr])
        p_oof[te] = clf.predict_proba(X[te])[:, 1]
        rank_fold[k] = esporta(clf, FC)
        controlla_export(rank_fold[k], clf, X[te])
    clf_r = addestra_ranker(X, y, w)
    rank_fin = esporta(clf_r, FC)
    jb = iC['scelto_base']
    # valutazione top-1 (solo disegni con un candidato giusto)
    stat = collections.Counter()
    rk_oof = {}
    cambi = []
    for k_rec, righe in per:
        d = rec[k_rec]
        ps = p_oof[righe]
        ys = y[righe]
        ib = [i for i, r in enumerate(righe) if X[r, jb] == 1]
        ib = ib[0] if ib else 0
        o = np.argsort(-ps, kind='stable')
        top = o[0]
        rk_oof[k_rec] = {'p_sel': float(ps[ib]), 'p_best': float(ps[top]),
                         'margine': float(ps[top] - (ps[o[1]] if len(o) > 1 else 0.0)),
                         'sel_top1': 1.0 if top == ib else 0.0, 'n': len(ps)}
        if ys.max() < 1:
            continue
        grp = 'manuale' if d['manuale'] else 'altri'
        stat[grp, 'n'] += 1
        stat[grp, 'base'] += int(ys[ib] == 1)
        stat[grp, 'modello'] += int(ys[top] == 1)
        if top != ib:
            cambi.append((float(ps[top] - ps[ib]), float(ps[top]), int(ys[top] == 1), int(ys[ib] == 1), d['geo_ok']))
    print('\nRANKER (top-1 su fold tenuti fuori, disegni con un candidato uguale a Lantek)')
    for grp in ('manuale', 'altri'):
        n = stat[grp, 'n']
        print(f'  {grp:8s} n={n}: regole {stat[grp, "base"]} ({100 * stat[grp, "base"] / max(n, 1):.1f}%)'
              f'  modello {stat[grp, "modello"]} ({100 * stat[grp, "modello"] / max(n, 1):.1f}%)')
    # politica del cambio: margine minimo
    print('  cambi di scelta (margine p_modello - p_regole): soglia -> cambi, migliora, peggiora')
    scelta_m = (1.0, 1.0)
    for mg in (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8):
        for pmin in (0.5, 0.7, 0.9):
            cc = [c for c in cambi if c[0] >= mg and c[1] >= pmin]
            mig = sum(1 for c in cc if c[2] and not c[3])
            peg = sum(1 for c in cc if c[3] and not c[2])
            # peggioramenti contati anche dove la geometria finale era giusta
            peg_geo = sum(1 for c in cc if not c[2] and c[4])
            if pmin == 0.7 or mg in (0.3, 0.5):
                print(f'    m>={mg:.1f} p>={pmin:.1f}: {len(cc)} cambi, +{mig} -{peg} (geo giusta persa {peg_geo})')
            if mig >= 4 * max(peg_geo, 1) and mig - peg_geo > 0 and (scelta_m == (1.0, 1.0) or mig - peg_geo > scelta_m[2]):
                scelta_m = (mg, pmin, mig - peg_geo)
    print(f'  politica scelta: {scelta_m}')
    for m_ in [rank_fin] + list(rank_fold.values()):
        m_['margine_cambio'] = scelta_m[0]
        m_['p_min_cambio'] = scelta_m[1]

    # ===== SICURO =====
    elig = []
    for k, d in enumerate(rec):
        m = d['m']
        if not m.get('xd') or m.get('fisse') or not d['r'].get('ok'):
            continue
        if args.escludi_fori_dubbi and fori_dubbi(d['r']):
            continue
        x = list(m['xd'])
        rk = rk_oof.get(k)
        if rk:
            x[iF['r_ok']] = 1.0
            for n in ('p_sel', 'p_best', 'margine', 'sel_top1', 'n'):
                x[iF['r_' + n]] = round(rk[n], 5)
        elig.append((k, x))
    ks = np.array([k for k, _ in elig])
    XS = np.array([x for _, x in elig], dtype=float)
    yS = np.array([rec[k]['geo_ok'] for k in ks]).astype(int)
    fS = fold[ks]
    bS = np.array([rec[k]['sicuro_base'] for k in ks], dtype=bool)
    print(f'\nSICURO: disegni ammessi (nessuna regola fissa contraria) {len(ks)} su {N}, giusti {yS.sum()}')

    def fit_s(Xa, ya):
        return HistGradientBoostingClassifier(**PAR_SIC).fit(Xa, ya)

    pS = np.zeros(len(yS))
    sic_fold = {}
    soglie_nested = {}
    for k in range(5):
        tr, te = fS != k, fS == k
        clf = fit_s(XS[tr], yS[tr])
        pS[te] = clf.predict_proba(XS[te])[:, 1]
        # soglia annidata: CV interna sui 4 fold d'addestramento
        pin = np.zeros(tr.sum())
        Xi, yi, fi, bi = XS[tr], yS[tr], fS[tr], bS[tr]
        for j in set(fi.tolist()):
            a, b = fi != j, fi == j
            pin[b] = fit_s(Xi[a], yi[a]).predict_proba(Xi[b])[:, 1]
        soglie_nested[k] = {}
        for pr in PRECS:
            soglie_nested[k]['sostituisce', pr] = soglia_per(pin, yi, pr)
            soglie_nested[k]['promuove', pr] = soglia_per(pin[~bi], yi[~bi], pr)
        sic_fold[k] = esporta(clf, FD)
        controlla_export(sic_fold[k], clf, XS[te])
        sic_fold[k]['soglia'] = soglie_nested[k][args.politica, args.prec]
        sic_fold[k]['politica'] = args.politica
    clf_s = fit_s(XS, yS)
    sic_fin = esporta(clf_s, FD)
    sic_fin['soglia'] = (soglia_per(pS, yS, args.prec) if args.politica == 'sostituisce'
                         else soglia_per(pS[~bS], yS[~bS], args.prec))
    sic_fin['politica'] = args.politica
    iso = IsotonicRegression(out_of_bounds='clip', y_min=0, y_max=1).fit(pS, yS)
    cal = {'x': [float(v) for v in iso.X_thresholds_], 'y': [float(v) for v in iso.y_thresholds_]}
    for m_ in [sic_fin] + list(sic_fold.values()):
        m_['calib'] = cal

    # risultati sui fold tenuti fuori
    tot_ok = sum(d['geo_ok'] for d in rec)
    base_s = sum(d['sicuro_base'] for d in rec)
    base_ok = sum(d['sicuro_base'] and d['geo_ok'] for d in rec)
    print(f'\nPRECISIONE / COPERTURA su {N} disegni (giusti {tot_ok}); fold tenuti fuori')
    print(f'  regole attuali: sicuri {base_s}, giusti {base_ok} = {100 * base_ok / base_s:.2f}%, '
          f'sbagliati {base_s - base_ok}, copertura dei giusti {100 * base_ok / tot_ok:.1f}%')
    print('  curva (soglia sulle probabilita\' OOF messe insieme):')
    o = np.argsort(-pS)
    cum_ok = np.cumsum(yS[o])
    for cov in (2000, 3000, 4000, 5000, 6000, 6500, 7000, 7500, 8000, 8500, 9000):
        if cov <= len(o):
            print(f'    sicuri {cov:5d}: giusti {cum_ok[cov - 1]} = {100 * cum_ok[cov - 1] / cov:.2f}%  '
                  f'(p >= {pS[o[cov - 1]]:.4f})')
    righe_tab = []

    def selezione(pol, pr):
        sel = np.zeros(len(yS), bool)
        for k in range(5):
            te = fS == k
            sel[te] = pS[te] >= soglie_nested[k][pol, pr]
        return sel | bS if pol == 'promuove' else sel

    for pol in ('sostituisce', 'promuove'):
        print(f'  politica {pol} (soglia annidata per fold, onesta):')
        for pr in PRECS:
            sel = selezione(pol, pr)
            n, ok = int(sel.sum()), int(yS[sel].sum())
            righe_tab.append([pol, float(pr), n, ok])
            print(f'    obiettivo {100 * pr:.1f}%: sicuri {n}, giusti {ok} = {100 * ok / max(n, 1):.2f}%, '
                  f'sbagliati {n - ok}, copertura dei giusti {100 * ok / tot_ok:.1f}%')
    # sicuri a regole: quanti sbagliati hanno p bassa
    for q in (0.5, 0.9, 0.97, 0.99):
        m_ = bS & (pS < q)
        print(f'  sicuri a regole con p < {q}: {m_.sum()} (sbagliati {int((m_ & (yS == 0)).sum())} su {int((bS & (yS == 0)).sum())})')
    # per gruppo e per tipo di dubbio, alla soglia annidata scelta
    sel = selezione(args.politica, args.prec)
    grp = collections.Counter()
    for j, k in enumerate(ks):
        d = rec[k]
        g = ('DECA' if comune.deca(d['cliente']) else 'altri') + '/' + d['fonte']
        grp[g, 'n'] += 1
        grp[g, 'ok'] += d['geo_ok']
        grp[g, 'sm'] += sel[j]
        grp[g, 'smok'] += sel[j] and d['geo_ok']
    for d in rec:
        g = ('DECA' if comune.deca(d['cliente']) else 'altri') + '/' + d['fonte']
        grp[g, 'tot'] += 1
        grp[g, 'sb'] += d['sicuro_base']
        grp[g, 'sbok'] += d['sicuro_base'] and d['geo_ok']
    print('  per gruppo (regole | modello, fold tenuti fuori):')
    for g in sorted({k[0] for k in grp}):
        print(f'    {g:10s} disegni {grp[g, "tot"]}: regole {grp[g, "sb"]} sicuri, {grp[g, "sb"] - grp[g, "sbok"]} sbagliati'
              f' | modello {grp[g, "sm"]} sicuri, {grp[g, "sm"] - grp[g, "smok"]} sbagliati')
    # da dove vengono i nuovi sicuri
    orig = collections.Counter()
    for j, k in enumerate(ks):
        d = rec[k]
        if sel[j] and not d['sicuro_base']:
            if d['manuale']:
                t = 'detector manuale'
            elif (d['conf'] or 0) < 0.7:
                t = f"conf {d['conf']}"
            else:
                t = 'fermato dal controllo a regole'
            orig[t, d['geo_ok']] += 1
    print('  nuovi sicuri per provenienza (giusti / sbagliati):')
    for t in sorted({k[0] for k in orig}):
        print(f'    {t:32s} {orig[t, True]:5d} / {orig[t, False]}')
    persi = sum(1 for j, k in enumerate(ks) if rec[k]['sicuro_base'] and not sel[j])
    persi_ok = sum(1 for j, k in enumerate(ks) if rec[k]['sicuro_base'] and not sel[j] and rec[k]['geo_ok'])
    print(f'  sicuri a regole tolti dal modello: {persi} (di cui giusti {persi_ok})')
    # importanza grezza: feature piu' usate
    uso = collections.Counter()
    for a in sic_fin['alberi']:
        for f, l in zip(a['f'], a['l']):
            if l >= 0:
                uso[FD[f]] += 1
    print('  feature piu\' usate (sicuro):', ', '.join(f'{n}:{c}' for n, c in uso.most_common(15)))

    # salva modelli
    json.dump({'versione': 1, 'ranker': rank_fin, 'sicuro': sic_fin},
              open(os.path.join(args.out, 'modello_motore.json'), 'w'), separators=(',', ':'))
    for k in range(5):
        json.dump({'versione': 1, 'ranker': rank_fold[k], 'sicuro': sic_fold[k]},
                  open(os.path.join(args.out, f'fold{k}.json'), 'w'), separators=(',', ':'))
    mappa = {f"{d['fonte']}:{d['codice']}": d['fold'] for d in rec}
    json.dump(mappa, open(os.path.join(args.out, 'fold_codici.json'), 'w'))
    json.dump({'tab': righe_tab}, open(os.path.join(args.out, 'tabella.json'), 'w'))
    # OOF per analisi
    with open(os.path.join(args.out, 'oof_sicuro.csv'), 'w', encoding='utf-8') as fh:
        fh.write('fonte;codice;fold;cliente;p;geo_ok;sicuro_base;manuale;conf\n')
        for j, k in enumerate(ks):
            d = rec[k]
            fh.write(f"{d['fonte']};{d['codice']};{d['fold']};{d['cliente']};{pS[j]:.5f};{int(d['geo_ok'])};"
                     f"{int(d['sicuro_base'])};{int(d['manuale'])};{d['conf']}\n")


if __name__ == '__main__':
    main()
