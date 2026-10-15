"""Raccoglie gli esempi CONFERMATI da Stefano (laser e preventivatore) per il
riaddestramento del modello del motore.

    python raccogli_esempi.py --esempi <app/dati_motore/esempi.jsonl> --db <COPIA di scheduler.db>
                              --gt <lantek_gt_corretto.csv> --out <cartella>

Fonti (sola lettura; il DB va passato come COPIA fatta con sqlite backup()):
- preventivo_articoli con geometria_manuale_confermata = 1 (CAD del preventivatore,
  "pezzo giusto"/"contorno scelto" del laser), o decisione_laser = giusto/scelto;
- esempi.jsonl (registro_esempi): contorni confermati/scelti, decisioni del laser
  (non_laser, sviluppo), spessori scelti dal laser.

Esce:
- esempi_gt.csv: stesso formato di lantek_gt_corretto.csv (code = 'ES_<codice>_<sha8>'),
  usabile con banco_lantek.py --gt; 'ext_area_m2' vuota se non nota (la ricava
  prepara_esempi.py dai candidati del motore);
- esempi_inventario.json: conteggi, scarti con motivo, contraddizioni con Lantek.

Regole: cliente TEST escluso; righe senza disegno trovato escluse; per lo stesso
disegno (sha256) vale l'ULTIMA decisione; 'non_laser' e 'sviluppo' non sono
esempi di geometria (contati a parte); file dentro ferrotrack-stress rifiutati.
"""
import argparse
import collections
import csv
import glob
import hashlib
import json
import os
import sqlite3

CAMPI_GT = ['code', 'material', 'thickness_mm', 'creation_method', 'area_m2', 'ext_area_m2', 'holes_area_m2',
            'rect_area_m2', 'bbox_len_mm', 'bbox_wid_mm', 'cut_perim_m', 'mark_perim_m', 'weight_kg',
            'cam_cut_starts', 'cam_cut_len_mm', 'cam_total_time_s', 'cam_mark_starts', 'created', 'last_modified',
            'lantek_recid', 'dxf_match', 'dxf_match_kind', 'dxf_n_candidates', 'flags', 'corretto_da',
            # in piu' per gli esempi
            'gruppo', 'quando', 'fonte_esempio', 'codice', 'cliente']


def sha(p):
    h = hashlib.sha256()
    with open(p, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def area_poligono(xy):
    a = 0.0
    for (x1, y1), (x2, y2) in zip(xy, xy[1:] + xy[:1]):
        a += x1 * y2 - x2 * y1
    return abs(a) / 2.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--esempi', required=True)
    ap.add_argument('--db', required=True)
    ap.add_argument('--gt', required=True)
    ap.add_argument('--uploads', help='cartella uploads della produzione (default: accanto a esempi.jsonl)')
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(a.esempi)))
    uploads = a.uploads or os.path.join(app_dir, 'uploads')
    inv = {'scarti': collections.Counter(), 'eventi': collections.Counter(), 'contraddizioni_gt': [],
           'accordi_gt': 0, 'stress_nel_registro': 0, 'spessori_confermati': 0, 'non_laser': [], 'sviluppo': []}

    # --- indice dei disegni della produzione (per nome, minuscolo)
    per_nome = collections.defaultdict(list)
    for p in glob.glob(os.path.join(uploads, '**', '*'), recursive=True):
        if p.lower().endswith(('.dxf', '.dwg')) and os.path.isfile(p):
            per_nome[os.path.basename(p).lower()].append(p)
    gt = {r['code']: r for r in csv.DictReader(open(a.gt, encoding='utf-8-sig'), delimiter=';')}
    gt_low = {k.lower(): v for k, v in gt.items()}

    # --- registro esempi
    reg = []
    for l in open(a.esempi, encoding='utf-8'):
        try:
            r = json.loads(l)
        except Exception:
            inv['scarti']['riga_registro_illeggibile'] += 1
            continue
        if 'ferrotrack-stress' in (r.get('file') or '').lower():
            inv['stress_nel_registro'] += 1
            continue
        inv['eventi'][r.get('evento')] += 1
        reg.append(r)

    # --- DB (copia)
    db = sqlite3.connect(a.db)
    righe = db.execute(
        'select p.id, p.preventivo_id, p.codice, p.dxf_filename, p.area_dm2, p.perimetro_taglio_m, p.n_forature, '
        'p.spessore_mm, p.materiale, p.geometria_manuale_confermata, p.geometry_source, p.extra_campi, '
        'p.canonical_dxf_sha256, v.cliente, v.is_deleted, v.data_creazione '
        'from preventivo_articoli p join preventivi v on v.id = p.preventivo_id').fetchall()
    # ordini nati da un preventivo: la cartella dei disegni e' quella dell'ordine
    ordine_di = collections.defaultdict(list)
    for oid, pid in db.execute('select id, preventivo_id_origine from orders where preventivo_id_origine is not null'):
        ordine_di[pid].append(oid)

    cand = {}   # sha -> esempio (vince l'ultimo)
    for (aid, pid, cod, dxf, area, per, nf, sp, mat, conf, src, ex, csha, cli, dele, dc) in righe:
        e = json.loads(ex or '{}')
        dl = (e.get('decisione_laser') or {})
        scelta = dl.get('scelta')
        if scelta in ('non_laser', 'sviluppo'):
            inv[scelta].append(cod)
        umano = bool(conf) or scelta in ('giusto', 'scelto')
        if not umano:
            continue
        if (cli or '').strip().upper() == 'TEST':
            inv['scarti']['cliente_TEST'] += 1
            continue
        if scelta in ('non_laser', 'sviluppo') or src == 'sviluppo':
            inv['scarti']['non_geometria(non_laser/sviluppo)'] += 1
            continue
        if not dxf or not dxf.lower().endswith('.dxf'):
            inv['scarti']['senza_dxf'] += 1
            continue
        nome = os.path.basename(dxf).lower()
        cart_ord = [os.path.join(uploads, 'drawings', o).lower() for o in ordine_di.get(pid, [])]
        tutti = per_nome.get(nome, [])
        pref = [p for p in tutti if os.path.dirname(p).lower() in cart_ord] or \
               [p for p in tutti if pid in p] or tutti
        if csha:
            pref = [p for p in pref if sha(p) == csha] or pref
        if not pref:
            inv['scarti']['disegno_non_trovato'] += 1
            continue
        path = pref[0]
        h = sha(path)
        bw, bh = e.get('bbox_w_mm'), e.get('bbox_h_mm')
        cs = e.get('contorno_scelto') or {}
        ext = None
        if cs.get('outer') and len(cs['outer']) >= 3:
            ext = area_poligono([tuple(p) for p in cs['outer']]) / 1e6
        if not (area and per and bw and bh):
            inv['scarti']['misure_incomplete'] += 1
            continue
        sp_man = (e.get('origine_campi') or {}).get('spessore_mm') == 'manuale'
        quando = (dl.get('quando') or (e.get('esito_verifica') or {}).get('quando') or dc or '')[:19]
        prec = cand.get(h)
        if prec and prec['quando'] > quando:
            inv['scarti']['duplicato_stesso_disegno'] += 1
            continue
        if prec:
            inv['scarti']['duplicato_stesso_disegno'] += 1
        cand[h] = {'codice': cod, 'path': path, 'sha': h, 'area': area, 'per': per, 'nf': nf, 'bw': bw, 'bh': bh,
                   'ext': ext, 'sp': sp if sp_man else None, 'mat': mat, 'gruppo': pid, 'quando': quando,
                   'fonte': f'prev:{src or scelta}', 'deleted': bool(dele), 'cliente': cli}

    # registro: contorni scelti/confermati con le misure (anche se il DB e' cambiato dopo)
    for r in sorted(reg, key=lambda r: r.get('quando') or ''):
        ev, d = r.get('evento'), r.get('decisione') or {}
        if ev == 'dati_scelti_dal_laser':
            inv['spessori_confermati'] += 1
        if ev not in ('contorno_confermato', 'contorno_scelto_a_mano') or not d.get('area_dm2'):
            continue
        h = r.get('sha256')
        p = r.get('file')
        if not h or not p or not os.path.isfile(p) or sha(p) != h:
            inv['scarti']['registro_file_mancante_o_cambiato'] += 1
            continue
        bb = d.get('bbox') or [None, None]
        ext = area_poligono([tuple(x) for x in d['outer_xy']]) / 1e6 if d.get('outer_xy') else None
        prec = cand.get(h)
        if prec and prec['quando'] > r['quando'] and prec['fonte'].startswith('prev:'):
            # il DB e' piu' recente: tiene il DB, ma l'area esterna dal registro se manca
            prec['ext'] = prec['ext'] or (ext if abs(prec['area'] - d['area_dm2']) < 0.01 * d['area_dm2'] else None)
            continue
        cand[h] = {'codice': r.get('codice'), 'path': p, 'sha': h, 'area': d['area_dm2'],
                   'per': d.get('perimetro_taglio_m'), 'nf': d.get('n_forature'), 'bw': bb[0], 'bh': bb[1],
                   'ext': ext, 'sp': None, 'mat': (r.get('motore') or {}).get('materiale'),
                   'gruppo': (r.get('contesto') or {}).get('ordine') or '?', 'quando': r['quando'],
                   'fonte': f'reg:{ev}', 'deleted': False, 'cliente': None}

    # --- confronto con Lantek (stesso codice nel GT)
    out = []
    for h, c in sorted(cand.items(), key=lambda x: (x[1]['quando'], x[1]['codice'] or '')):
        g = gt_low.get((c['codice'] or '').lower())
        if g:
            ga = float(g['area_m2']) * 100
            gb = sorted([float(g['bbox_len_mm']), float(g['bbox_wid_mm'])])
            cb = sorted([c['bw'], c['bh']])
            stesso_file = g.get('dxf_match') and os.path.isfile(g['dxf_match']) and sha(g['dxf_match']) == h
            ok = abs(ga - c['area']) <= max(0.01 * ga, 0.005) and all(abs(x - y) <= max(0.005 * y, 1.0)
                                                                       for x, y in zip(cb, gb))
            if ok:
                inv['accordi_gt'] += 1
            else:
                inv['contraddizioni_gt'].append({'codice': c['codice'], 'stesso_file': bool(stesso_file),
                                                 'conf': [round(c['area'], 4), [round(x, 1) for x in cb]],
                                                 'lantek': [round(ga, 4), [round(x, 1) for x in gb]],
                                                 'fonte': c['fonte']})
            c['in_gt'] = True
            c['stesso_file_gt'] = bool(stesso_file)
            c['contro_gt'] = not ok
        out.append(c)

    # --- uso di ogni esempio nell'addestramento
    for c in out:
        if '_cleaned' in os.path.basename(c['path']).lower():
            c['uso'] = 'escluso_file_pulito'          # copia ripulita dall'app, non il disegno del cliente
        elif c['fonte'] in ('prev:peso-cartiglio', 'prev:auto-accettato'):
            c['uso'] = 'escluso_proposta_automatica'  # contorno proposto dall'app (peso), solo accettato
        elif c.get('contro_gt'):
            c['uso'] = 'escluso_contro_lantek'
        elif c.get('stesso_file_gt'):
            c['uso'] = 'gia_nel_banco'                # stesso disegno gia' nella risposta giusta di Lantek
        else:
            c['uso'] = 'nuovo'
    inv['uso'] = collections.Counter(c['uso'] for c in out)
    with open(os.path.join(a.out, 'esempi_tutti.csv'), 'w', encoding='utf-8-sig', newline='') as fh:
        w = csv.writer(fh, delimiter=';')
        w.writerow(['codice', 'uso', 'fonte', 'quando', 'gruppo', 'cliente', 'area_dm2', 'bbox', 'perim_m', 'file'])
        for c in out:
            w.writerow([c['codice'], c['uso'], c['fonte'], c['quando'], c['gruppo'], c['cliente'] or '',
                        round(c['area'], 4), [round(c['bw'], 1), round(c['bh'], 1)], c['per'], c['path']])
    with open(os.path.join(a.out, 'esempi_gt.csv'), 'w', encoding='utf-8-sig', newline='') as fh:
        w = csv.DictWriter(fh, CAMPI_GT, delimiter=';')
        w.writeheader()
        for c in out:
            if c['uso'] != 'nuovo':
                continue
            bb = sorted([c['bw'], c['bh']], reverse=True)
            w.writerow({'code': f"ES_{c['codice']}_{c['sha'][:8]}", 'material': c['mat'] or '',
                        'thickness_mm': c['sp'] if c['sp'] else '', 'creation_method': 'CONFERMA',
                        'area_m2': c['area'] / 100, 'ext_area_m2': (c['ext'] or '') if c['ext'] else '',
                        'bbox_len_mm': bb[0], 'bbox_wid_mm': bb[1], 'cut_perim_m': c['per'],
                        'cam_cut_starts': c['nf'] or '', 'dxf_match': c['path'], 'dxf_match_kind': 'exact',
                        'dxf_n_candidates': 1, 'flags': '', 'corretto_da': '',
                        'gruppo': c['gruppo'], 'quando': c['quando'], 'fonte_esempio': c['fonte'],
                        'codice': c['codice'], 'cliente': c['cliente'] or ''})
    inv['esempi_geometria'] = len(out)
    inv['nuovi'] = sum(1 for c in out if c['uso'] == 'nuovo')
    inv['per_fonte'] = collections.Counter(c['fonte'] for c in out)
    inv['per_gruppo'] = collections.Counter(c['gruppo'] for c in out)
    inv['in_gt_stesso_file'] = sum(1 for c in out if c.get('stesso_file_gt'))
    inv['in_gt'] = sum(1 for c in out if c.get('in_gt'))
    inv['con_area_esterna'] = sum(1 for c in out if c['ext'])
    inv['non_laser'] = sorted(set(inv['non_laser']))
    inv['sviluppo'] = sorted(set(inv['sviluppo']))
    json.dump(inv, open(os.path.join(a.out, 'esempi_inventario.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1, default=str)
    print(json.dumps({k: v for k, v in inv.items() if k != 'contraddizioni_gt'}, ensure_ascii=False, default=str))
    print('contraddizioni con Lantek:', len(inv['contraddizioni_gt']))
    for x in inv['contraddizioni_gt']:
        print('  ', x)


if __name__ == '__main__':
    main()
