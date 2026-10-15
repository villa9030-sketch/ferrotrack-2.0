"""Confronto di piu' modelli del motore sugli stessi dump (banco_lantek --modello dump).

    python valuta_modelli.py --dump dump_dxf.jsonl [--dump-dwg dump_dwg.jsonl] [--dump-es dump_es.jsonl]
        --modello attuale=../../backend/preventivi/modello_motore.json --modello nuovo=<cartella o json>
        [--codici regressione_1500.txt] [--solo-es tenuti|tutti] [--punto prec_995]

Per ogni disegno e modello rifa' quello che fa l'import con motore_modello = on:
- RANKER sui candidati del dump: se cambia la scelta (margine e p minima del
  modello) la geometria e' quella del candidato nuovo (giusta se area esterna e
  ingombro coincidono con la risposta giusta: approssimato) e il disegno NON e'
  sicuro (regola fissa 'contorno cambiato');
- SICURO: feature del disegno con le r_* del ranker di QUEL modello, soglia del
  punto di lavoro, regole fisse del dump.
Se il modello e' una CARTELLA (uscita di addestra.py) ogni disegno usa il
modello del suo fold (fold_codici.json), cioe' uno che non l'ha visto.
Errore silenzioso = sicuro ma geometria sbagliata.
"""
import argparse
import collections
import json
import os

import comune
from addestra import etichetta_cand, mm, iF

R_FEAT = ('r_ok', 'r_p_sel', 'r_p_best', 'r_margine', 'r_sel_top1', 'r_n')


class Modello:
    def __init__(self, nome, percorso):
        self.nome = nome
        self.fold = None
        if os.path.isdir(percorso):
            self.fin = json.load(open(os.path.join(percorso, 'modello_motore.json'), encoding='utf-8'))
            self.fold = json.load(open(os.path.join(percorso, 'fold_codici.json'), encoding='utf-8'))
            self.per_fold = {k: json.load(open(os.path.join(percorso, f'fold{k}.json'), encoding='utf-8'))
                             for k in range(5)}
        else:
            self.fin = json.load(open(percorso, encoding='utf-8'))

    def per(self, fonte, codice):
        if self.fold is not None:
            k = self.fold.get(f'{fonte}:{codice}')
            if k is not None:
                return self.per_fold[k]
        return self.fin


def decidi(M, r, punto):
    """(cambiato, geo_cand_ok o None, sicuro, p)"""
    m = r.get('modello') or {}
    cand = m.get('cand') or {}
    rk = M.get('ranker') or {}
    sic = M.get('sicuro') or {}
    x = list(m.get('xd') or [])
    cambiato, k_new = False, None
    if cand.get('X') and rk:
        ps = [mm.probabilita(rk, xx) for xx in cand['X']]
        o = sorted(range(len(ps)), key=lambda k: -ps[k])
        base = m.get('idx_base')
        ks = cand['idx'].index(base) if base in cand['idx'] else None
        p_sel = ps[ks] if ks is not None else 0.0
        p1, p2 = ps[o[0]], (ps[o[1]] if len(o) > 1 else 0.0)
        if x:
            vals = {'r_ok': 1.0, 'r_p_sel': p_sel, 'r_p_best': p1, 'r_margine': p1 - p2,
                    'r_sel_top1': 1.0 if cand['idx'][o[0]] == base else 0.0, 'r_n': float(len(ps))}
            for n in R_FEAT:
                x[iF[n]] = round(vals[n], 5)
        if cand['idx'][o[0]] != base and p1 - p_sel >= float(rk.get('margine_cambio', 1.0)) \
                and p1 >= float(rk.get('p_min_cambio', 1.0)):
            cambiato, k_new = True, o[0]
    if cambiato:
        return True, cand['geo'][k_new], False, None
    if not x or not sic or not r.get('ok'):
        return False, None, False, None
    p = mm.probabilita(sic, x)
    soglia = float((sic.get('soglie') or {}).get(punto, sic.get('soglia', 2.0)))
    fisse = [f for f in (m.get('fisse') or []) if f != 'contorno cambiato dal modello']
    return False, None, (p >= soglia and not fisse), p


def misura(dumps, modelli, voluti=None, solo_es=None, punto=mm.PUNTO_DI_LAVORO):
    """dumps: {'dxf': path, 'dwg': path, 'es': path}; modelli: [Modello].
    Ritorna {'n', 'regole': {...}, nome: {'geo','camb','camb_meglio','camb_peggio','sic','sic_ok','silenzi'}}."""
    gt = comune.carica_gt()
    info = comune.info_codici()
    C = collections.Counter()
    silenzi = collections.defaultdict(set)
    for fonte in ('dxf', 'dwg', 'es'):
        p = dumps.get(fonte)
        if not p:
            continue
        for c, r in comune.leggi_jsonl(p).items():
            g = (gt.get(fonte) or {}).get(c)
            if g is None or (voluti is not None and c.lower() not in voluti):
                continue
            ii = info.get((fonte, c)) or {}
            if fonte == 'es' and solo_es == 'tenuti' and not ii.get('tenuto_fuori'):
                continue
            e = comune.rep.valuta(r, g)
            C['n'] += 1
            C['regole', 'geo'] += e['geo']
            C['regole', 'sic'] += e['sicuro']
            C['regole', 'sic_ok'] += e['sicuro'] and e['geo']
            if e['sicuro'] and not e['geo']:
                silenzi['regole'].add((fonte, c))
            for M in modelli:
                camb, gc, sicuro, _p = decidi(M.per(fonte, c), r, punto)
                geo = (etichetta_cand(gc, g) > 0) if camb else e['geo']
                C[M.nome, 'geo'] += geo
                C[M.nome, 'camb'] += camb
                C[M.nome, 'camb_meglio'] += camb and geo and not e['geo']
                C[M.nome, 'camb_peggio'] += camb and e['geo'] and not geo
                C[M.nome, 'sic'] += sicuro
                C[M.nome, 'sic_ok'] += sicuro and geo
                if sicuro and not geo:
                    silenzi[M.nome].add((fonte, c))
    out = {'n': C['n']}
    for nome in ['regole'] + [M.nome for M in modelli]:
        out[nome] = {k: C[nome, k] for k in ('geo', 'camb', 'camb_meglio', 'camb_peggio', 'sic', 'sic_ok')}
        out[nome]['silenzi'] = sorted(silenzi[nome])
    return out


def testo(ris, titolo=''):
    n = ris['n']
    righe = [f"{titolo} disegni {n}"]
    for nome, v in ris.items():
        if nome == 'n':
            continue
        s, ok = v['sic'], v['sic_ok']
        righe.append(f"  {nome:12s}: geometria {v['geo']} ({100 * v['geo'] / max(n, 1):.2f}%), "
                     f"ranker cambia {v['camb']} (+{v['camb_meglio']} -{v['camb_peggio']}), "
                     f"sicuri {s} (copertura {100 * s / max(n, 1):.1f}%), giusti {100 * ok / max(s, 1):.2f}%, "
                     f"silenziosi {s - ok}")
    return '\n'.join(righe)


def nuovi_silenzi(ris, prima, dopo):
    return sorted(set(map(tuple, ris[dopo]['silenzi'])) - set(map(tuple, ris[prima]['silenzi'])))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dump')
    ap.add_argument('--dump-dwg')
    ap.add_argument('--dump-es')
    ap.add_argument('--modello', action='append', required=True, help='nome=percorso')
    ap.add_argument('--codici')
    ap.add_argument('--solo-es', choices=('tenuti', 'tutti'))
    ap.add_argument('--punto', default=mm.PUNTO_DI_LAVORO)
    ap.add_argument('--elenca', action='store_true', help='elenca gli errori silenziosi')
    a = ap.parse_args()
    modelli = [Modello(*x.split('=', 1)) for x in a.modello]
    voluti = None
    if a.codici:
        voluti = {l.strip().lower() for l in open(a.codici, encoding='utf-8') if l.strip()}
    ris = misura({'dxf': a.dump, 'dwg': a.dump_dwg, 'es': a.dump_es}, modelli, voluti, a.solo_es, a.punto)
    print(testo(ris, f'(punto {a.punto})'))
    if len(modelli) >= 2:
        nv = nuovi_silenzi(ris, modelli[0].nome, modelli[-1].nome)
        print(f"  silenziosi NUOVI di {modelli[-1].nome} rispetto a {modelli[0].nome}: {len(nv)} {nv[:20]}")
    if a.elenca:
        for nome, v in ris.items():
            if nome != 'n':
                print(nome, v['silenzi'])


if __name__ == '__main__':
    main()
