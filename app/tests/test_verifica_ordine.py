"""Verifica automatica dei pezzi di un ordine (backend/preventivi/verifica_ordine.py).

Con DXF COSTRUITI in una cartella temporanea (i disegni dei clienti non vanno
nel repository) prova le regole portate dalla pagina del preventivatore:
peso del cartiglio, correzione del contorno, confronto con Lantek, materiale
mancante, rinuncia quando il controllo e' troppo lungo.

Poi una MISURA su 25 DXF veri dell'archivio (solo lettura, se l'archivio
c'e'): quanti verificati / corretti / da guardare e tempo medio per pezzo.
La misura si stampa e basta: non fa fallire il test.

Non tocca il database ne' i file del repository.

Esecuzione: app\\.venv\\Scripts\\python.exe app\\tests\\test_verifica_ordine.py
"""
import logging
import os
import random
import sys
import tempfile
import time
import types

os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
sys.path.insert(0, _APP)
# backend.preventivi senza backend/__init__ (che carica l'app intera)
if 'backend' not in sys.modules:
    _pkg = types.ModuleType('backend')
    _pkg.__path__ = [os.path.join(_APP, 'backend')]
    sys.modules['backend'] = _pkg
logging.disable(logging.WARNING)

import ezdxf  # noqa: E402

from backend.preventivi import verifica_ordine as vo  # noqa: E402

ARCHIVIO = r'C:\Users\Stefano\Desktop\AUTOCAD\TAGLIO'

OK = 0
KO = []


def check(nome, cond, extra=''):
    global OK
    if cond:
        OK += 1
        print(f'  [OK] {nome}')
    else:
        KO.append(nome)
        print(f'  [X] {nome}  {extra}')


# ── Disegni di prova ──
# Piastra 200 x 100 con 4 fori da 10: area 1,9686 dm2; in S235 da 2 mm pesa 0,309 kg
AREA_PIASTRA = (200 * 100 - 4 * 3.141592653589793 * 25) / 1e4
PERIM_PIASTRA = (600 + 4 * 3.141592653589793 * 10) / 1000
PESO_PIASTRA = AREA_PIASTRA * 2 / 100 * 7.85


def piastra(path, peso_testo=None, cornice=False):
    doc = ezdxf.new()
    doc.header['$INSUNITS'] = 4
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (200, 0), (200, 100), (0, 100)], close=True)
    for cx, cy in ((20, 20), (180, 20), (180, 80), (20, 80)):
        msp.add_circle((cx, cy), 5)
    if cornice:
        # cornice esterna 400 x 300 attorno al pezzo (il "contorno sbagliato")
        msp.add_lwpolyline([(-100, -100), (300, -100), (300, 200), (-100, 200)], close=True)
    if peso_testo:
        msp.add_text(peso_testo, dxfattribs={'height': 5}).set_placement((0, -60))
    doc.saveas(path)


def item_base(dxf, **kw):
    it = {'codice': os.path.splitext(dxf)[0], 'materiale': 'S235', 'spessore_mm': 2.0,
          'area_dm2': round(AREA_PIASTRA, 4), 'perimetro_taglio_m': round(PERIM_PIASTRA, 4),
          'n_forature': 5, 'dxf_filename': dxf, 'pieghe': 0, 'bbox_w_mm': 200.0, 'bbox_h_mm': 100.0,
          'dxf_confidence': 0.9, 'dxf_needs_verify': False}
    it.update(kw)
    return it


def prove_costruite(tmp):
    print('\n== DXF costruiti ==')
    piastra(os.path.join(tmp, 'P1.dxf'), peso_testo=f'PESO kg {PESO_PIASTRA:.2f}'.replace('.', ','))
    piastra(os.path.join(tmp, 'P2.dxf'), peso_testo=f'PESO kg {PESO_PIASTRA:.2f}'.replace('.', ','), cornice=True)
    piastra(os.path.join(tmp, 'P3.dxf'))                                   # senza peso
    piastra(os.path.join(tmp, 'P4.dxf'), peso_testo=f'PESO kg {PESO_PIASTRA:.2f}'.replace('.', ','), cornice=True)

    # 1. peso coerente
    r = vo.verifica_automatica(tmp, item_base('P1.dxf'))
    check('peso coerente -> verificato', r['esito']['stato'] == 'verificato'
          and 'peso' in r['esito']['fonti'] and r['correzione'] is None, r['esito'])
    check('verifica salvabile nel pezzo (peso letto)',
          (r['verifica'] or {}).get('peso_cartiglio', {}).get('peso_kg') == round(PESO_PIASTRA, 2), r['verifica'])

    # 2. contorno sbagliato: e' stata presa la cornice 400 x 300 (area 12 dm2 meno il pezzo)
    sbagliato = item_base('P2.dxf', area_dm2=12.0 - 2.0, perimetro_taglio_m=1.4, n_forature=1,
                          bbox_w_mm=400.0, bbox_h_mm=300.0, dxf_confidence=0.8)
    r = vo.verifica_automatica(tmp, sbagliato)
    st = r['esito']['stato']
    check('peso molto diverso -> corretto_da_confermare o da_guardare',
          st in ('corretto_da_confermare', 'da_guardare'), r['esito'])
    c = r['correzione']
    check('correzione proposta coerente col peso', c is not None
          and abs(c['area_dm2'] / AREA_PIASTRA - 1) <= 0.05 and st == 'corretto_da_confermare', (r['esito'], c))
    if c:
        ca = c['contorno_auto']
        check('correzione da confermare, annullabile', ca['stato'] == 'da_confermare'
              and ca['prima']['area_dm2'] == 10.0 and ca['prima']['confermato'] is False
              and ca['chiave'] == 'P2.dxf|2|S235' and c['geometry_source'] == 'peso-cartiglio', ca)
        check('n_forature e ingombro dal contorno nuovo', c['n_forature'] == 5
              and abs((c['bbox_w_mm'] or 0) - 200) < 1 and abs((c['bbox_h_mm'] or 0) - 100) < 1, c)
    # una sola volta: se il contorno_auto con la stessa chiave c'e' gia', non si riprova
    r2 = vo.verifica_automatica(tmp, dict(sbagliato, contorno_auto={'stato': 'non_trovato', 'chiave': 'P2.dxf|2|S235'}))
    check('una sola volta: gia\' provato -> da_guardare senza correzione',
          r2['esito']['stato'] == 'da_guardare' and r2['correzione'] is None, r2['esito'])
    # niente correzione su un pezzo scritto a mano o con correggi=False
    r3 = vo.verifica_automatica(tmp, dict(sbagliato, pezzo_manuale=True))
    check('pezzo manuale: nessuna correzione', r3['correzione'] is None and r3['esito']['stato'] == 'da_guardare')
    r4 = vo.verifica_automatica(tmp, sbagliato, correggi=False)
    check('correggi=False: nessuna correzione', r4['correzione'] is None and r4['esito']['stato'] == 'da_guardare')
    # spessore incerto: il peso non dice nulla sul contorno
    r5 = vo.verifica_automatica(tmp, dict(sbagliato, _spessore_conf=0.5))
    check('spessore incerto: nessuna correzione', r5['correzione'] is None)

    # 3. Lantek concorde senza peso nel cartiglio
    lk = {'codice': 'P3', 'area_dm2': round(AREA_PIASTRA * 1.02, 4), 'perimetro_m': round(PERIM_PIASTRA, 3),
          'spessore': 2.0, 'materiale': 'FERRO'}
    r = vo.verifica_automatica(tmp, item_base('P3.dxf'), lantek=lk)
    check('Lantek concorde senza peso -> verificato', r['esito']['stato'] == 'verificato'
          and r['esito']['fonti'] == ['lantek'], r['esito'])
    # Lantek concorde anche con contorno incerto
    r = vo.verifica_automatica(tmp, item_base('P3.dxf', dxf_needs_verify=True, dxf_confidence=0.4), lantek=lk)
    check('Lantek concorde conferma un contorno incerto', r['esito']['stato'] == 'verificato', r['esito'])

    # 4. Lantek con area +40%
    r = vo.verifica_automatica(tmp, item_base('P3.dxf'), lantek=dict(lk, area_dm2=round(AREA_PIASTRA * 1.4, 4)))
    check('Lantek area +40% -> da_guardare', r['esito']['stato'] == 'da_guardare'
          and any('Lantek' in m for m in r['esito']['motivi']), r['esito'])
    # Lantek con materiale di altra famiglia: avviso
    r = vo.verifica_automatica(tmp, item_base('P3.dxf'), lantek=dict(lk, materiale='INOX'))
    check('Lantek materiale diverso -> da_guardare', r['esito']['stato'] == 'da_guardare'
          and any('materiale' in m for m in r['esito']['motivi']), r['esito'])
    r = vo.verifica_automatica(tmp, item_base('P3.dxf', materiale='S355JR'), lantek=lk)
    check('FERRO in Lantek vale per S355JR', r['esito']['stato'] == 'verificato', r['esito'])

    # 5. materiale mancante
    r = vo.verifica_automatica(tmp, item_base('P1.dxf', materiale=None))
    check('materiale mancante -> da_guardare', r['esito']['stato'] == 'da_guardare'
          and any('materiale' in m for m in r['esito']['motivi']), r['esito'])
    r = vo.verifica_automatica(tmp, item_base('P1.dxf', spessore_mm=None))
    check('spessore mancante -> da_guardare', r['esito']['stato'] == 'da_guardare', r['esito'])

    # 6. nessun riferimento
    r = vo.verifica_automatica(tmp, item_base('P3.dxf'))
    check('nessun riferimento, contorno sicuro -> non_verificabile',
          r['esito']['stato'] == 'non_verificabile', r['esito'])
    r = vo.verifica_automatica(tmp, item_base('P3.dxf', dxf_needs_verify=True))
    check('nessun riferimento, contorno incerto -> da_guardare', r['esito']['stato'] == 'da_guardare', r['esito'])

    # 7. materiale aggiunto nelle Impostazioni (densita' sua)
    mats = {'C75': {'taglio_come': 'S235', 'densita_kg_dm3': 7.85, 'prezzo_kg': 6.5}}
    check('densita\' materiale aggiunto', vo.densita_materiale('c75', mats) == 7.85
          and vo.densita_materiale('C75', {}) == 0.0 and vo.densita_materiale('INOX_316L') == 8.0)
    r = vo.verifica_automatica(tmp, item_base('P1.dxf', materiale='C75'), materiali=mats)
    check('C75 col peso coerente -> verificato', r['esito']['stato'] == 'verificato', r['esito'])

    # 8. errore interno: mai eccezioni, da_guardare
    r = vo.verifica_automatica(os.path.join(tmp, 'non_esiste'), item_base('P1.dxf', area_dm2='abc'))
    check('dati strani o cartella mancante: nessuna eccezione', r['esito']['stato'] in ('da_guardare', 'non_verificabile'), r['esito'])

    # 9. ricerca del contorno troppo lunga: si rinuncia entro il tempo
    vero = vo._vc.proponi_contorno

    def lento(*a, **k):
        time.sleep(4)
        return vero(*a, **k)
    vo._vc.proponi_contorno = lento
    try:
        t0 = time.monotonic()
        r = vo.verifica_automatica(tmp, dict(sbagliato, dxf_filename='P4.dxf'), tempo_max_s=1.0)
        dt = time.monotonic() - t0
    finally:
        vo._vc.proponi_contorno = vero
    check('controllo lento -> da_guardare entro il tempo', r['esito']['stato'] == 'da_guardare'
          and any('troppo lungo' in m for m in r['esito']['motivi']) and dt < 2.0, (r['esito'], round(dt, 2)))

    # 10. tutto l'ordine insieme: stesso ordine dei pezzi
    items = [item_base('P1.dxf'), item_base('P3.dxf'), item_base('P1.dxf', materiale='')]
    ris = vo.verifica_ordine(tmp, items, lantek_per_codice={'P3': lk})
    check('verifica_ordine: un risultato per pezzo, nello stesso ordine',
          [x['esito']['stato'] for x in ris] == ['verificato', 'verificato', 'da_guardare'],
          [x['esito']['stato'] for x in ris])
    check('verifica_ordine con lista vuota', vo.verifica_ordine(tmp, []) == [])


def item_da_archivio(path):
    """Il pezzo come lo costruisce l'import (solo funzioni di lettura)."""
    from backend.preventivi import dxf_scanner as ds
    from backend.preventivi.pick_part import _snap_stock
    g = ds.estrai_geometria_taglio(path, {})
    mat = ds.estrai_materiale_da_cartiglio(path) or {}
    sp = ds.estrai_spessore_da_cartiglio(path, g.get('area_dm2'), mat.get('materiale') or None) or {}
    spm = sp.get('spessore_mm')
    return {'codice': os.path.splitext(os.path.basename(path))[0],
            'materiale': mat.get('materiale') if (mat.get('confidence') or 0) >= 0.5 else None,
            'spessore_mm': _snap_stock(float(spm)) if spm and (sp.get('confidence') or 0) >= 0.5 else None,
            'area_dm2': g.get('area_dm2', 0), 'perimetro_taglio_m': g.get('perimetro_taglio_m', 0),
            'n_forature': g.get('n_pierce', g.get('n_forature', 0)), 'dxf_filename': os.path.basename(path),
            'pieghe': 0, 'dxf_confidence': g.get('confidence'),
            'dxf_needs_verify': bool(g.get('needs_manual_select')),
            'bbox_w_mm': g.get('bbox_width_mm'), 'bbox_h_mm': g.get('bbox_height_mm')}


def misura_archivio(n=25):
    print(f'\n== Misura su {n} DXF dell\'archivio (solo lettura) ==')
    if not os.path.isdir(ARCHIVIO):
        print('  archivio non trovato: misura saltata')
        return
    tutti = []
    for dp, _dn, fn in os.walk(ARCHIVIO):
        tutti += [os.path.join(dp, f) for f in fn if f.lower().endswith('.dxf')]
    tutti.sort()
    campione = random.Random(20261007).sample(tutti, min(n, len(tutti)))
    conti, tempi, letti = {}, [], 0
    for p in campione:
        try:
            it = item_da_archivio(p)
        except Exception as e:
            print(f'  {os.path.basename(p)[:40]:40s} import non riuscito: {str(e)[:60]}')
            continue
        letti += 1
        t0 = time.monotonic()
        r = vo.verifica_automatica(os.path.dirname(p), it, tempo_max_s=15.0)
        dt = time.monotonic() - t0
        tempi.append(dt)
        st = r['esito']['stato']
        conti[st] = conti.get(st, 0) + 1
        print(f'  {os.path.basename(p)[:40]:40s} {st:22s} {dt:5.1f}s  {"; ".join(r["esito"]["motivi"])[:90]}')
    if tempi:
        print(f'  pezzi: {letti}  ' + '  '.join(f'{k}: {v}' for k, v in sorted(conti.items())))
        print(f'  tempo medio per pezzo: {sum(tempi) / len(tempi):.2f}s  (massimo {max(tempi):.1f}s)')


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        prove_costruite(tmp)
    try:
        misura_archivio()
    except Exception as e:      # la misura non deve far fallire il test
        print(f'  misura non riuscita: {e}')
    print(f'\n{OK} PASSATI, {len(KO)} FALLITI')
    if KO:
        for k in KO:
            print('  -', k)
    sys.exit(1 if KO else 0)
