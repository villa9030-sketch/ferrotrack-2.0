"""Completa gli esempi confermati con l'area ESTERNA del pezzo (serve per
etichettare i candidati del ranker) e li divide in addestramento / tenuti fuori.

    python prepara_esempi.py --gt esempi_gt.csv --dump dump_es.jsonl --out esempi_gt_completo.csv
                             [--quota-fuori 3]

- area esterna: dal contorno scelto a mano (se c'e'); altrimenti dal candidato del
  motore con lo stesso ingombro (0,5%/1 mm) e area >= area netta confermata (il
  piu' piccolo); se la lettura del motore coincide con la conferma, la sua area lorda;
- esempio senza area esterna ricavabile -> scartato (contato);
- tenuti fuori: i gruppi (preventivo/ordine) con hash % quota == 0 -> colonna
  'tenuto_fuori' = 1; non entrano MAI nell'addestramento (misura onesta).
"""
import argparse
import csv
import hashlib
import json

TOL_BB_REL, TOL_BB_ABS = 0.005, 1.0


def vicino(a, b, rel, ass):
    return a is not None and b is not None and abs(a - b) <= max(rel * abs(b), ass)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--gt', required=True)
    ap.add_argument('--dump', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--quota-fuori', type=int, default=3)
    a = ap.parse_args()
    dump = {}
    for l in open(a.dump, encoding='utf-8'):
        try:
            r = json.loads(l)
            dump[r['codice']] = r
        except Exception:
            pass
    righe = list(csv.DictReader(open(a.gt, encoding='utf-8-sig'), delimiter=';'))
    campi = list(righe[0].keys()) + ['tenuto_fuori', 'ext_da'] if righe else []
    tenuti, scarti, fonti = 0, [], {}
    out = []
    for r in righe:
        net = float(r['area_m2']) * 100           # dm2
        bb = sorted([float(r['bbox_len_mm']), float(r['bbox_wid_mm'])])
        ext, da = (float(r['ext_area_m2']) * 100, 'contorno') if r.get('ext_area_m2') else (None, None)
        m = dump.get(r['code'])
        if ext is None and m and m.get('ok'):
            mb = sorted(x for x in (m.get('bbox') or []) if x)
            if (len(mb) == 2 and vicino(m.get('area_dm2'), net, 0.01, 0.005)
                    and vicino(mb[0], bb[0], TOL_BB_REL, TOL_BB_ABS) and vicino(mb[1], bb[1], TOL_BB_REL, TOL_BB_ABS)
                    and m.get('area_lorda_dm2')):
                ext, da = m['area_lorda_dm2'], 'lettura_motore'
            else:
                cand = ((m.get('modello') or {}).get('cand') or {}).get('geo') or []
                ok = [g[0] / 1e4 for g in cand
                      if g[0] / 1e4 >= net * 0.99
                      and vicino(min(g[1], g[2]), bb[0], TOL_BB_REL, TOL_BB_ABS)
                      and vicino(max(g[1], g[2]), bb[1], TOL_BB_REL, TOL_BB_ABS)]
                if ok:
                    ext, da = min(ok), 'candidato'
        if ext is None:
            scarti.append(r['code'])
            continue
        fonti[da] = fonti.get(da, 0) + 1
        fuori = int(hashlib.md5(r['gruppo'].encode('utf-8')).hexdigest(), 16) % a.quota_fuori == 0
        tenuti += fuori
        out.append({**r, 'ext_area_m2': ext / 100, 'tenuto_fuori': int(fuori), 'ext_da': da})
    with open(a.out, 'w', encoding='utf-8-sig', newline='') as fh:
        w = csv.DictWriter(fh, campi, delimiter=';')
        w.writeheader()
        w.writerows(out)
    print(f'esempi {len(righe)}: pronti {len(out)} (area esterna da {fonti}), tenuti fuori {tenuti}, '
          f'scartati senza area esterna {len(scarti)} {scarti}')


if __name__ == '__main__':
    main()
