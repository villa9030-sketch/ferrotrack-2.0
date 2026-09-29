"""Banco di prova del preventivatore sull'archivio dei disegni.

Misura, sulle coppie DXF + STEP dell'archivio (lo STEP e' la risposta giusta):
  1. riconoscimento all'import: area letta dal DXF entro il 5% dello STEP;
  2. controllo del peso del cartiglio: quanti pezzi letti male segnala
     (e quanti falsi allarmi da' su quelli letti bene);
  3. correzione automatica: proposte fatte e proposte giuste (area entro 3%).

Salva il risultato pezzo per pezzo e lo confronta con la misura precedente:
elenca i pezzi migliorati e quelli PEGGIORATI. Da lanciare prima di mettere in
uso qualunque modifica a riconoscimento, verifica o correzione.

Uso (dalla cartella app):
    .venv\\Scripts\\python.exe tools\\banco_prova_archivio.py
    .venv\\Scripts\\python.exe tools\\banco_prova_archivio.py --rapido      (senza la correzione, ~2 min)
    .venv\\Scripts\\python.exe tools\\banco_prova_archivio.py --archivio "D:\\altra\\cartella"

Non avvia l'app e non tocca il database: carica solo i moduli di calcolo.
Le soglie dei controlli sono le stesse della pagina (preventivi.html,
_wbControlliCoerenza / _wbDaCorreggereAuto): se cambiano la', vanno cambiate qui.
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import os
import sys
import time
import types
from concurrent.futures import ProcessPoolExecutor, as_completed

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCHIVIO = r'C:\Users\Stefano\Desktop\AUTOCAD\TAGLIO'
USCITA = os.path.join(APP, 'tools', 'banco_prova')
DENSITA = 7.85            # il materiale non si sa dallo STEP: acciaio (inox ~1% in piu')


def _carica_moduli():
    """backend.preventivi.* senza backend/__init__ (che avvia l'app e il DB)."""
    if APP not in sys.path:
        sys.path.insert(0, APP)
    if 'backend' not in sys.modules:
        pkg = types.ModuleType('backend')
        pkg.__path__ = [os.path.join(APP, 'backend')]
        sys.modules['backend'] = pkg
    logging.disable(logging.WARNING)


# Soglie: le stesse della pagina
def peso_ok(app_kg, cart_kg):
    return abs(app_kg / cart_kg - 1) <= 0.15 or abs(app_kg - cart_kg) <= 0.015


def peso_grave(app_kg, cart_kg):
    r = app_kg / cart_kg
    return (r < 0.5 or r > 2) and abs(app_kg - cart_kg) > 0.05


def coppie(radice):
    step = {}
    for dp, _dn, fn in os.walk(radice):
        for f in fn:
            b, e = os.path.splitext(f)
            if e.lower() in ('.step', '.stp'):
                step[os.path.join(dp, b).lower()] = os.path.join(dp, f)
    out = []
    for dp, _dn, fn in os.walk(radice):
        for f in fn:
            b, e = os.path.splitext(f)
            if e.lower() == '.dxf' and os.path.join(dp, b).lower() in step:
                out.append((os.path.join(dp, f), step[os.path.join(dp, b).lower()]))
    return sorted(out)


def misura_pezzo(args):
    dxf, stp, con_correzione = args
    _carica_moduli()
    from backend.preventivi import dxf_scanner
    from backend.preventivi.verifica_coerenza import dati_step_lamiera, peso_cartiglio, proponi_contorno
    r = {'dxf': dxf}
    s = dati_step_lamiera(stp, DENSITA)
    if not s or not s.get('ok'):
        r['escluso'] = (s or {}).get('motivo') or 'non lamiera'
        return r
    r['step'] = {k: s[k] for k in ('spessore_mm', 'area_dm2', 'n_pieghe', 'perimetro_taglio_m', 'inneschi')}
    try:
        g = dxf_scanner.estrai_geometria_taglio(dxf, {})
        r['letto'] = {'area_dm2': g.get('area_dm2') or 0, 'perimetro_taglio_m': g.get('perimetro_taglio_m') or 0,
                      'n_forature': g.get('n_forature'), 'bbox': [g.get('bbox_width_mm'), g.get('bbox_height_mm')]}
    except Exception as e:
        r['letto'] = {'area_dm2': 0, 'errore': str(e)[:120]}
    a_s, sp = s['area_dm2'], s['spessore_mm']
    a_l = r['letto']['area_dm2']
    r['letto_bene'] = abs(a_l - a_s) <= 0.05 * a_s
    pc = peso_cartiglio(dxf)
    # Controllo con le quote (quando manca il peso): un lato dell'ingombro deve essere quotato
    from backend.preventivi.verifica_coerenza import quote_disegno
    q = quote_disegno(dxf)
    bb = [x for x in (r['letto'].get('bbox') or []) if x]
    if q and len(bb) == 2:
        quotato = lambda v: any(abs(v - x) <= max(0.6, 0.01 * x) for x in q)
        r['quote'] = {'segnala': not (quotato(bb[0]) or quotato(bb[1]))}
    if pc.get('peso_kg') and (pc.get('confidence') or 0) >= 0.5:
        cart = pc['peso_kg']
        app = a_l * sp / 100 * DENSITA
        r['peso'] = {'cartiglio': cart, 'app': round(app, 4), 'segnala': app <= 0 or not peso_ok(app, cart),
                     'grave': app <= 0 or peso_grave(app, cart),
                     'cartiglio_ok': abs(cart / (a_s * sp / 100 * DENSITA) - 1) <= 0.1}
        if con_correzione and r['peso']['grave']:
            try:
                p = proponi_contorno(dxf, sp, DENSITA, cart, {})
            except Exception as e:
                p = {'trovato': False, 'motivo': str(e)[:120]}
            if p.get('trovato'):
                r['correzione'] = {'area_dm2': p['area_dm2'], 'giusta': abs(p['area_dm2'] / a_s - 1) <= 0.03,
                                   'secondi': p.get('secondi')}
            else:
                r['correzione'] = {'trovata': False, 'secondi': p.get('secondi')}
    return r


def stato(r):
    """Etichetta sintetica di un pezzo, per il confronto tra due misure."""
    if 'escluso' in r:
        return 'escluso'
    if r.get('letto_bene'):
        return 'letto bene'
    c = r.get('correzione')
    if c and c.get('giusta'):
        return 'corretto giusto'
    if c and 'giusta' in c:
        return 'corretto SBAGLIATO'
    if r.get('peso', {}).get('segnala') or r.get('quote', {}).get('segnala'):
        return 'letto male, segnalato'
    return 'letto male, NON segnalato'


def riepilogo(ris):
    lam = [r for r in ris if 'escluso' not in r]
    bene = [r for r in lam if r['letto_bene']]
    male = [r for r in lam if not r['letto_bene']]
    cp = lambda lst, f: sum(1 for r in lst if f(r))
    con_peso_m = [r for r in male if 'peso' in r]
    con_peso_b = [r for r in bene if 'peso' in r]
    corr = [r['correzione'] for r in male if 'correzione' in r]
    q_m = [r for r in male if 'peso' not in r and 'quote' in r]
    q_b = [r for r in bene if 'peso' not in r and 'quote' in r]
    return {
        'senza_peso': cp(lam, lambda r: 'peso' not in r),
        'quote_male_segnalati': cp(q_m, lambda r: r['quote']['segnala']), 'quote_male': len(q_m),
        'quote_falsi_allarmi': cp(q_b, lambda r: r['quote']['segnala']), 'quote_bene': len(q_b),
        'coppie': len(ris), 'lamiere': len(lam),
        'letto_bene': len(bene), 'letto_male': len(male),
        'peso_nel_cartiglio': cp(lam, lambda r: 'peso' in r),
        'male_segnalati': cp(con_peso_m, lambda r: r['peso']['segnala']),
        'male_con_peso': len(con_peso_m),
        'falsi_allarmi': cp(con_peso_b, lambda r: r['peso']['segnala']),
        'bene_con_peso': len(con_peso_b),
        'correzioni_provate': len(corr),
        'correzioni_giuste': sum(1 for c in corr if c.get('giusta')),
        'correzioni_sbagliate': sum(1 for c in corr if c.get('giusta') is False),
        'correzioni_non_trovate': sum(1 for c in corr if c.get('trovata') is False),
    }


def stampa(s, titolo):
    pct = lambda a, b: f'{100 * a / b:.0f}%' if b else '-'
    print(f'\n== {titolo} ==')
    print(f"lamiere a corpo unico con STEP: {s['lamiere']} (su {s['coppie']} coppie DXF+STEP)")
    print(f"1. riconoscimento all'import: area giusta {s['letto_bene']} ({pct(s['letto_bene'], s['lamiere'])}), sbagliata {s['letto_male']}")
    print(f"2. controllo peso (peso nel cartiglio su {s['peso_nel_cartiglio']}): "
          f"letti male segnalati {s['male_segnalati']}/{s['male_con_peso']} ({pct(s['male_segnalati'], s['male_con_peso'])}), "
          f"falsi allarmi {s['falsi_allarmi']}/{s['bene_con_peso']}")
    if 'quote_male' in s:
        print(f"   senza peso nel cartiglio ({s['senza_peso']}), controllo con le quote: "
              f"letti male segnalati {s['quote_male_segnalati']}/{s['quote_male']}, "
              f"falsi allarmi {s['quote_falsi_allarmi']}/{s['quote_bene']}")
    if s['correzioni_provate']:
        print(f"3. correzione automatica: provata su {s['correzioni_provate']}, giuste {s['correzioni_giuste']}, "
              f"sbagliate {s['correzioni_sbagliate']}, non trovate {s['correzioni_non_trovate']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--archivio', default=ARCHIVIO)
    ap.add_argument('--uscita', default=USCITA)
    ap.add_argument('--rapido', action='store_true', help='senza la correzione automatica')
    ap.add_argument('--processi', type=int, default=6)
    a = ap.parse_args()
    os.makedirs(a.uscita, exist_ok=True)
    cs = coppie(a.archivio)
    print(f'{len(cs)} coppie DXF+STEP in {a.archivio}', flush=True)
    t0 = time.time()
    ris = []
    with ProcessPoolExecutor(a.processi) as ex:
        fut = [ex.submit(misura_pezzo, (d, s, not a.rapido)) for d, s in cs]
        for i, f in enumerate(as_completed(fut), 1):
            try:
                ris.append(f.result())
            except Exception as e:
                ris.append({'dxf': '?', 'escluso': f'errore: {e}'})
            if i % 100 == 0:
                print(f'  {i}/{len(cs)} ({time.time() - t0:.0f}s)', flush=True)
    ris.sort(key=lambda r: r['dxf'])
    s = riepilogo(ris)
    ora = datetime.datetime.now().strftime('%Y%m%d_%H%M')
    tipo = 'rapido' if a.rapido else 'completo'
    precedente = sorted(f for f in os.listdir(a.uscita) if f.startswith('banco_') and f.endswith(f'_{tipo}.json'))
    uscita = os.path.join(a.uscita, f'banco_{ora}_{tipo}.json')
    with open(uscita, 'w', encoding='utf-8') as fo:
        json.dump({'quando': ora, 'secondi': round(time.time() - t0), 'riepilogo': s, 'pezzi': ris}, fo, ensure_ascii=False)
    stampa(s, f'misura del {ora} ({time.time() - t0:.0f}s)')
    if precedente:
        with open(os.path.join(a.uscita, precedente[-1]), encoding='utf-8') as fi:
            prima = json.load(fi)
        stampa(prima['riepilogo'], f"misura precedente ({prima['quando']})")
        vecchi = {r['dxf']: stato(r) for r in prima['pezzi']}
        ordine = ['letto male, NON segnalato', 'corretto SBAGLIATO', 'letto male, segnalato', 'corretto giusto', 'letto bene']
        rango = {k: i for i, k in enumerate(ordine)}
        meglio, peggio = [], []
        for r in ris:
            v, n = vecchi.get(r['dxf']), stato(r)
            if v is None or v == n or 'escluso' in (v, n):
                continue
            (meglio if rango.get(n, 0) > rango.get(v, 0) else peggio).append(f'  {os.path.relpath(r["dxf"], a.archivio)}: {v} -> {n}')
        print(f'\nMIGLIORATI: {len(meglio)}')
        print('\n'.join(meglio[:30]))
        print(f'PEGGIORATI: {len(peggio)}')
        print('\n'.join(peggio[:60]) if peggio else '  nessuno')
    print(f'\nrisultati: {uscita}')


if __name__ == '__main__':
    main()
