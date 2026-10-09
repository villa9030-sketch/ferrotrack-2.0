import csv, collections, sys, comune
oof = list(csv.DictReader(open(sys.argv[1], encoding='utf-8'), delimiter=';'))
d = {'dxf': comune.leggi_jsonl('dump_I.jsonl'), 'dwg': comune.leggi_jsonl('dump_dwg_I.jsonl')}
gt = comune.carica_gt()
oof.sort(key=lambda r: -float(r['p']))
n = int(sys.argv[2])
top = oof[:n]
err = [r for r in top if r['geo_ok'] == '0']
print(len(top), 'errati', len(err))
c = collections.Counter()
for r in err:
    f = r['fonte']; m = d[f][r['codice']]; g = gt[f][r['codice']]; e = comune.rep.valuta(m, g)
    c[e['tipo']] += 1
    print(f, r['codice'], r['p'], e['tipo'], 'sb', r['sicuro_base'], m.get('conf'), 'a', m.get('area_dm2'), round(g['area'],4), 'pe', m.get('perim_m'), round(g['perim'],3), 'pz', m.get('n_pierce'), g['inneschi'], 'bb', m.get('bbox'), [round(x,1) for x in g['bbox']], r['cartella'][-30:] if 'cartella' in r else '')
print(c)
