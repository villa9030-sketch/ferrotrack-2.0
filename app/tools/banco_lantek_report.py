"""Confronto dei risultati di banco_lantek.py con l'archivio di Lantek.

    python tools/banco_lantek_report.py --gt <lantek_gt.csv> --ris <risultati.jsonl>
           [--prima <altro.jsonl>] [--csv dettaglio.csv]

Un pezzo e' "geometria giusta" se area netta, ingombro e perimetro di taglio
coincidono con Lantek entro le tolleranze qui sotto; "tutto giusto" se in piu'
tornano gli inneschi (contorno + fori) e lo spessore.

La domanda che conta: quando il motore si dice SICURO (pulito 'auto', cioe'
nessuna revisione chiesta), quante volte sbaglia? Quello e' l'errore che passa
in silenzio.

Con --prima confronta con una misura precedente: pezzi migliorati e PEGGIORATI.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import math

TOL_AREA_REL, TOL_AREA_ABS_DM2 = 0.01, 0.005      # 1% o 50 mm2
TOL_PERIM_REL, TOL_PERIM_ABS_M = 0.02, 0.005      # 2% o 5 mm
TOL_BBOX_REL, TOL_BBOX_ABS_MM = 0.005, 1.0        # 0,5% o 1 mm
TOL_SP_MM = 0.05


def famiglia(m: str | None) -> str | None:
    if not m:
        return None
    m = m.upper()
    if 'INOX' in m or 'AISI' in m or '304' in m or '316' in m or 'X5CR' in m:
        return 'INOX'
    if 'ALL' in m or 'AL' == m[:2] or 'ALU' in m or '5754' in m or '6082' in m or '1050' in m:
        return 'ALLUMINIO'
    if 'ZINC' in m or 'DX51' in m or 'SENDZ' in m or 'ZN' in m:
        return 'ZINCATO'
    return 'FERRO'


def vicino(a, b, rel, ass):
    return a is not None and b is not None and abs(a - b) <= max(rel * abs(b), ass)


def carica_gt(path):
    gt = {}
    for r in csv.DictReader(open(path, encoding='utf-8-sig'), delimiter=';'):
        f = lambda k: float(r[k]) if r.get(k) not in (None, '') else None
        gt[r['code']] = {
            'area': f('area_m2') * 100, 'area_est': f('ext_area_m2') * 100,
            'perim': f('cut_perim_m'), 'bbox': sorted([f('bbox_len_mm'), f('bbox_wid_mm')]),
            'inneschi': int(float(r['cam_cut_starts'])) if r.get('cam_cut_starts') not in (None, '', '0') else None,
            'sp': f('thickness_mm'), 'mat': r['material'].replace('GEN_', ''),
        }
    return gt


def classifica_errore(m, g):
    """Una parola per il tipo di errore della geometria."""
    a, ga = m.get('area_dm2') or 0, g['area']
    if not m.get('ok'):
        return 'errore_lettura'
    if a <= 0:
        return 'nessuna_area'
    rap = a / ga if ga else 0
    for k in (2, 2.5, 4, 5, 10, 20, 25.4, 50, 100, 1000):
        for s in (k * k, 1 / (k * k)):
            if abs(rap / s - 1) < 0.03:
                return f'scala_x{k:g}' if s > 1 else f'scala_1:{k:g}'
    bb = sorted(x for x in (m.get('bbox') or []) if x)
    if len(bb) == 2 and vicino(bb[0], g['bbox'][0], TOL_BBOX_REL, TOL_BBOX_ABS_MM) \
            and vicino(bb[1], g['bbox'][1], TOL_BBOX_REL, TOL_BBOX_ABS_MM):
        if m.get('area_lorda_dm2') and vicino(m['area_lorda_dm2'], g['area_est'], TOL_AREA_REL, TOL_AREA_ABS_DM2):
            return 'contorno_ok_fori_diversi'
        return 'ingombro_ok_forma_diversa'
    if rap > 1.5:
        return 'preso_troppo_grande'
    if rap < 0.67:
        return 'preso_troppo_piccolo'
    return 'forma_simile_non_uguale'


def con_fori_trapano(m):
    """La lettura come se i fori da trapano fossero tagliati al laser (per il
    confronto con Lantek: la regola di Stefano dei 2/3 dello spessore vale da
    ottobre 2026, lo storico di Lantek spesso li tagliava)."""
    ft = m.get('fori_trapano') or []
    if not ft or not m.get('ok'):
        return None
    tondi = list(m.get('fori_tondi') or [])
    a = p = 0.0
    for d in ft:
        if not tondi:
            return None
        k = min(range(len(tondi)), key=lambda i: abs((tondi[i][0] or 0) - (d or 0)))
        _d, ar, pe = tondi.pop(k)
        a += ar or 0
        p += pe or 0
    return {**m, 'area_dm2': (m.get('area_dm2') or 0) - a / 1e4, 'perim_m': (m.get('perim_m') or 0) + p / 1e3,
            'n_pierce': (m.get('n_pierce') or 0) + len(ft), 'fori_trapano': []}


def valuta(m, g):
    e = _valuta(m, g)
    if not e['geo']:
        m2 = con_fori_trapano(m)
        if m2 is not None:
            e2 = _valuta(m2, g)
            if e2['geo']:
                e2['sicuro'] = e['sicuro']
                e2['trapano_regola'] = True
                return e2
    return e


def _valuta(m, g):
    bb = sorted(x for x in (m.get('bbox') or []) if x)
    e = {
        'area': vicino(m.get('area_dm2'), g['area'], TOL_AREA_REL, TOL_AREA_ABS_DM2),
        'perim': vicino(m.get('perim_m'), g['perim'], TOL_PERIM_REL, TOL_PERIM_ABS_M),
        'bbox': len(bb) == 2 and vicino(bb[0], g['bbox'][0], TOL_BBOX_REL, TOL_BBOX_ABS_MM)
        and vicino(bb[1], g['bbox'][1], TOL_BBOX_REL, TOL_BBOX_ABS_MM),
        'inneschi': None if g['inneschi'] is None else m.get('n_pierce') == g['inneschi'],
        'sp': vicino(m.get('sp_mm'), g['sp'], 0, TOL_SP_MM),
        'mat': famiglia(m.get('mat')) == famiglia(g['mat']) if m.get('mat') else None,
    }
    # pezzo salvato RUOTATO in Lantek: area netta, area esterna e perimetro
    # uguali ma ingombro diverso -> la lettura e' giusta (non dipende dalla rotazione)
    e['ruotato'] = (not e['bbox'] and e['area'] and e['perim']
                    and vicino(m.get('area_lorda_dm2'), g['area_est'], TOL_AREA_REL, TOL_AREA_ABS_DM2))
    e['geo'] = bool(m.get('ok')) and e['area'] and e['perim'] and (e['bbox'] or e['ruotato'])
    e['tutto'] = e['geo'] and e['inneschi'] is not False and e['sp']
    e['sicuro'] = m.get('pulito') == 'auto'
    e['tipo'] = None if e['geo'] else classifica_errore(m, g)
    return e


def pct(n, d):
    return f'{n}/{d} = {100 * n / d:.1f}%' if d else f'{n}/0'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--gt', required=True)
    ap.add_argument('--ris', required=True)
    ap.add_argument('--prima')
    ap.add_argument('--csv')
    a = ap.parse_args()
    gt = carica_gt(a.gt)

    def leggi(p):
        out = {}
        for l in open(p, encoding='utf-8'):
            r = json.loads(l)
            if r['codice'] in gt:
                out[r['codice']] = r
        return out

    ris = leggi(a.ris)
    val = {c: valuta(m, gt[c]) for c, m in ris.items()}
    N = len(val)
    cnt = collections.Counter()
    for c, e in val.items():
        for k in ('geo', 'tutto', 'area', 'perim', 'bbox', 'inneschi', 'sp', 'sicuro'):
            cnt[k] += bool(e[k])
        cnt['mat_letto'] += e['mat'] is not None
        cnt['mat_ok'] += bool(e['mat'])
        if e['sicuro']:
            cnt['sicuro_geo_ok'] += e['geo']
            cnt['sicuro_tutto_ok'] += e['tutto']
    print(f'PEZZI MISURATI: {N}')
    print(f'  errori di lettura/timeout: {sum(1 for m in ris.values() if not m.get("ok"))}')
    print(f'GEOMETRIA GIUSTA (area+perimetro+ingombro): {pct(cnt["geo"], N)}')
    print(f'TUTTO GIUSTO (+ inneschi + spessore):       {pct(cnt["tutto"], N)}')
    print(f'  area {pct(cnt["area"], N)} | perimetro {pct(cnt["perim"], N)} | ingombro {pct(cnt["bbox"], N)}'
          f' | inneschi {pct(cnt["inneschi"], N)} | spessore {pct(cnt["sp"], N)}')
    print(f'  materiale letto {pct(cnt["mat_letto"], N)}, giusto quando letto {pct(cnt["mat_ok"], cnt["mat_letto"])}')
    s = cnt['sicuro']
    print(f'MOTORE SICURO (pulito auto, nessuna revisione): {pct(s, N)}')
    print(f'  di questi geometria giusta {pct(cnt["sicuro_geo_ok"], s)}  -> SBAGLIATI IN SILENZIO: {s - cnt["sicuro_geo_ok"]}')
    print(f'  di questi tutto giusto     {pct(cnt["sicuro_tutto_ok"], s)}')
    print('TIPI DI ERRORE DELLA GEOMETRIA:')
    for t, n in collections.Counter(e['tipo'] for e in val.values() if e['tipo']).most_common():
        ns = sum(1 for e in val.values() if e['tipo'] == t and e['sicuro'])
        print(f'  {t:28s} {n:6d}   (di cui dati per sicuri: {ns})')
    tempi = sorted(m.get('t') or 0 for m in ris.values())
    if tempi:
        print(f'TEMPO per disegno: mediana {tempi[len(tempi) // 2]:.1f}s, 95% {tempi[int(len(tempi) * .95)]:.1f}s, max {tempi[-1]:.0f}s')

    if a.prima:
        prima = leggi(a.prima)
        comuni = set(prima) & set(ris)
        vp = {c: valuta(prima[c], gt[c]) for c in comuni}
        meglio = sorted(c for c in comuni if val[c]['geo'] and not vp[c]['geo'])
        peggio = sorted(c for c in comuni if vp[c]['geo'] and not val[c]['geo'])
        silenzio = sorted(c for c in comuni if val[c]['sicuro'] and not val[c]['geo']
                          and not (vp[c]['sicuro'] and not vp[c]['geo']))
        print(f'CONFRONTO con {a.prima} su {len(comuni)} pezzi: migliorati {len(meglio)}, PEGGIORATI {len(peggio)},'
              f' nuovi sbagliati-in-silenzio {len(silenzio)}')
        for c in peggio[:40]:
            print('  PEGGIORATO', c, val[c]['tipo'])
        for c in silenzio[:40]:
            print('  NUOVO SILENZIOSO', c, val[c]['tipo'])

    if a.csv:
        with open(a.csv, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.writer(f, delimiter=';')
            w.writerow(['codice', 'file', 'geo', 'tutto', 'sicuro', 'tipo', 'area', 'area_lantek', 'perim', 'perim_lantek',
                        'bbox', 'bbox_lantek', 'inneschi', 'inneschi_lantek', 'sp', 'sp_lantek', 'mat', 'mat_lantek',
                        'conf', 'pulito', 'fonte_geo', 'avvisi', 't'])
            for c, m in sorted(ris.items()):
                e, g = val[c], gt[c]
                w.writerow([c, m.get('file'), int(e['geo']), int(e['tutto']), int(e['sicuro']), e['tipo'] or '',
                            m.get('area_dm2'), round(g['area'], 4), m.get('perim_m'), g['perim'],
                            m.get('bbox'), [round(x, 1) for x in g['bbox']], m.get('n_pierce'), g['inneschi'],
                            m.get('sp_mm'), g['sp'], m.get('mat'), g['mat'], m.get('conf'), m.get('pulito'),
                            m.get('fonte_geo'), ' | '.join(m.get('avvisi') or [])[:300], m.get('t')])


if __name__ == '__main__':
    main()
