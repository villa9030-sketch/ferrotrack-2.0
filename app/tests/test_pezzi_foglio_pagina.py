# -*- coding: utf-8 -*-
"""Pezzi del foglio nella pagina laser (laser-controllo.js), nel browser.

Si prova il giro di Stefano: P sul disegno -> i pezzi del foglio numerati e
colorati (la copia uguale tratteggiata, "×2") -> ne toglie uno (era una vista)
-> cambia un codice -> Invio conferma -> le righe dell'ordine sono i pezzi
("pezzo N del foglio ...") -> la conferma di "Manda a Lantek" li elenca.

Il server di prova da' il disegno vero di un pezzo di un ordine; i pezzi del
foglio, la conferma e l'ordine dopo la conferma sono risposte FINTE (nessuna
scrittura arriva al server: tutte le altre sono bloccate nel browser).
I disegni non si fotografano.
Serve il server di prova (default 127.0.0.1:5056).

    python app/tests/test_pezzi_foglio_pagina.py [url]
"""
import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print('Playwright non installato: test saltato.')
    sys.exit(0)

B = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5056'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.accesso_server import contesto_stazione  # noqa: E402

esiti = []


def check(nome, cond, extra=''):
    esiti.append(bool(cond))
    print('  [%s] %s %s' % ('OK' if cond else 'KO', nome, '' if cond else extra))


try:
    with urllib.request.urlopen(B + '/api/health', timeout=4) as _r:
        _salute = json.load(_r)
except Exception as _e:
    print('Server non raggiungibile su %s: %s' % (B, str(_e)[:60]))
    print("Avvia un'istanza di prova e riesegui. Test saltato.")
    sys.exit(0)
if not _salute.get('istanza_di_prova'):
    print("RIFIUTO: %s non e' un'istanza di prova." % B)
    sys.exit(1)

S = {'oid': None, 'aid': None, 'vista': None, 'confermati': None, 'bloccate': [], 'post': []}


def rett(x0, y0, w, h):
    return [[x0, y0], [x0 + w, y0], [x0 + w, y0 + h], [x0, y0 + h], [x0, y0]]


def pezzi_finti():
    """Tre pezzi dentro il foglio vero: due in alto, uno in basso ripetuto."""
    x0, y0, x1, y1 = S['vista']
    w, h = x1 - x0, y1 - y0
    a = rett(x0 + 0.05 * w, y0 + 0.6 * h, 0.3 * w, 0.3 * h)
    b = rett(x0 + 0.5 * w, y0 + 0.6 * h, 0.2 * w, 0.2 * h)
    c = rett(x0 + 0.05 * w, y0 + 0.1 * h, 0.15 * w, 0.15 * h)
    c2 = rett(x0 + 0.3 * w, y0 + 0.1 * h, 0.15 * w, 0.15 * h)
    base = {'holes_xy': [], 'area_dm2': 1.0, 'perimetro_taglio_m': 0.5, 'n_forature': 1}
    return {'success': True, 'confermati': False, 'codice_foglio': 'FOGLIO-T', 'qta_foglio': 3, 'altri_codici': ['altro-00'],
            'pezzi': [dict(base, n=1, codice='FOGLIO-T-1', codice_da='proposto', outer_xy=a, copie=[], qta_per_foglio=1, qta=3,
                           misure=[300, 200]),
                      dict(base, n=2, codice='FOGLIO-T-2', codice_da='proposto', outer_xy=b, copie=[], qta_per_foglio=1, qta=3,
                           misure=[150, 100]),
                      dict(base, n=3, codice='FOGLIO-T-3', codice_da='proposto', outer_xy=c, copie=[c2], qta_per_foglio=2,
                           qta=6, misure=[120, 80])]}


def filtro(route):
    req = route.request
    u = req.url.split('?')[0]
    if S['oid'] and S['aid'] and u.endswith(f"/pezzi/{S['aid']}/pezzi-foglio"):
        if req.method == 'GET':
            return route.fulfill(status=200, content_type='application/json', body=json.dumps(pezzi_finti()))
        if req.method == 'POST':
            corpo = json.loads(req.post_data or '{}')
            S['post'].append(corpo)
            S['confermati'] = corpo['pezzi']
            return route.fulfill(status=200, content_type='application/json', body=json.dumps(
                {'success': True, 'n_pezzi': len(corpo['pezzi']),
                 'pezzi': [{'codice': p['codice'], 'qta': p['qta'], 'file': 'LANTEK/x/' + p['codice'] + '.dxf'} for p in corpo['pezzi']]}))
    if S['confermati'] and req.method == 'GET' and u.endswith(f"/api/orders/{S['oid']}/laser"):
        # l'ordine dopo la conferma: al posto del foglio, i suoi pezzi
        r = route.fetch()
        j = r.json()
        foglio = next((p for p in j.get('pezzi') or [] if str(p.get('articolo_id')) == str(S['aid'])), {})
        figli = [{**foglio, 'codice': p['codice'], 'articolo_id': None, 'quantita': p['qta'], 'disegno': None,
                  'lantek': 'nuovo', 'crea': True, 'motore': 'deciso', 'decisione': 'piu_pezzi_confermati',
                  'da_controllare': False, 'blocco': None, 'escluso': None, 'perche': [],
                  'foglio': {'articolo_id': S['aid'], 'codice': 'FOGLIO-T', 'n': i, 'qta_per_foglio': p['qta_per_foglio'],
                             'disegno': foglio.get('disegno')}}
                 for i, p in enumerate(S['confermati'], 1)]
        j['pezzi'] = [p for p in j.get('pezzi') or [] if str(p.get('articolo_id')) != str(S['aid'])] + figli
        j.update({'n_controllare': 0, 'bloccati': [], 'n_abbinare': 0, 'pronto': True, 'tutto_in_lantek': False,
                  'stato': 'coda'})
        j['lantek'] = {**(j.get('lantek') or {}), 'letto': True, 'disponibile': True}
        inv = j.setdefault('invio', {})
        inv['nuovi'] = [p['codice'] for p in S['confermati']]
        inv['nuovi_da_creare'] = [{'codice': p['codice'], 'materiale': 'FERRO', 'spessore': 3, 'quantita': p['qta']}
                                  for p in S['confermati']]
        j['n_invio'] = len(inv.get('codici') or []) + len(inv['nuovi'])
        return route.fulfill(status=200, content_type='application/json', body=json.dumps(j))
    if req.method in ('GET', 'HEAD', 'OPTIONS'):
        return route.continue_()
    S['bloccate'].append(req.method + ' ' + u)
    return route.abort()


with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = contesto_stazione(b.new_context(viewport={'width': 1440, 'height': 900}), B, 'laser')
    ctx.route('**/*', filtro)
    pg = ctx.new_page()
    err = []
    pg.on('pageerror', lambda e: err.append(str(e)))
    pg.goto(B + '/laser.html')
    pg.wait_for_selector('.lz-r[data-id]', timeout=30000)
    pg.wait_for_function('Object.keys(LZ.s.riep).length > 0', timeout=120000)
    # un ordine da mettere in Lantek con un pezzo col DXF
    trovato = pg.evaluate("""async () => {
        for (const o of LZ.ordiniLaser().filter(o => LZ.statoLaser(o) === 'coda')) {
            const d = (await LZ.ordine.carica(o.id)) || {};
            const p = ((d.dati || {}).pezzi || []).find(p => p.articolo_id && /[.]dxf$/i.test(p.disegno || ''));
            if (p) return [o.id, p.articolo_id];
        }
        return null; }""")
    if not trovato:
        print("Nessun ordine da mettere in Lantek con un disegno DXF: test saltato.")
        b.close()
        sys.exit(0)
    S['oid'], S['aid'] = trovato
    lett = pg.evaluate(f'LZ.get("/api/orders/{S["oid"]}/pezzi/{S["aid"]}/lettura").then(r => r.d)')
    S['vista'] = lett.get('vista') or [0, 0, 1000, 700]
    pg.click(f'.lz-r[data-id="{S["oid"]}"]')
    pg.wait_for_selector('#lz-o-scheda tr', timeout=60000)

    print('1) P apre i pezzi del foglio')
    pg.evaluate(f'LZ.controllo.vedi("{S["oid"]}", "{S["aid"]}")')
    pg.wait_for_selector('#lz-ctl-foglio svg', timeout=60000)
    check('il pulsante dice P', 'Ci sono più pezzi' in pg.locator('#lz-ctl-lato [data-c="pezzi"]').inner_text()
          or 'Rivedi' in pg.locator('#lz-ctl-lato [data-c="pezzi"]').inner_text())
    pg.keyboard.press('p')
    pg.wait_for_selector('#lz-ctl-lato .lz-pf li', timeout=30000)
    check('tre pezzi nell\'elenco', pg.locator('#lz-ctl-lato .lz-pf li').count() == 3)
    check('sul disegno: tre contorni colorati e la copia tratteggiata',
          pg.locator('#lz-ctl-foglio .fg-pfp').count() == 3 and pg.locator('#lz-ctl-foglio .fg-pfc').count() == 1)
    check('numerati, la copia uguale "×2"', pg.evaluate("[...document.querySelectorAll('.fg-pfn')].map(t => t.textContent)")
          == ['1', '2', '3 ×2'])
    check('codici e quantità proposti', pg.evaluate("[...document.querySelectorAll('[data-pf-cod]')].map(i => i.value)")
          == ['FOGLIO-T-1', 'FOGLIO-T-2', 'FOGLIO-T-3']
          and pg.evaluate("[...document.querySelectorAll('[data-pf-qta]')].map(i => i.value)") == ['3', '3', '6'])
    check('Invio sul pulsante di conferma', 'Invio' in pg.locator('#lz-ctl-lato [data-c="pf-conferma"]').inner_text())

    print('\n2) Togli una vista, cambia un codice, conferma')
    pg.click('#lz-ctl-lato [data-pf-togli="0"]')
    pg.wait_for_timeout(200)
    check('tolto: restano due pezzi', pg.locator('#lz-ctl-lato .lz-pf li').count() == 2
          and pg.locator('#lz-ctl-foglio .fg-pfp').count() == 2)
    pg.click('#lz-ctl-foglio .fg-pfp[data-pf="1"]', force=True)
    pg.wait_for_timeout(200)
    check('clic su un pezzo del disegno: scelto nell\'elenco', 'on' in (pg.locator('#lz-ctl-lato .lz-pf li').nth(1).get_attribute('class') or ''))
    inp = pg.locator('#lz-ctl-lato [data-pf-cod="1"]')
    inp.fill('ALTRO-00')
    inp.press('Enter')
    pg.keyboard.press('Enter')
    pg.wait_for_timeout(400)
    aperto = pg.evaluate('LZ.controllo.aperto()')
    check("codice gia' nell'ordine: non parte, resta aperto", not S['post'] and aperto)
    inp.fill('STAFFA-T')
    inp.press('Enter')
    pg.locator('#lz-ctl-lato [data-pf-qta="0"]').fill('4')
    pg.locator('#lz-ctl-lato .lz-pf-tot').click()
    pg.keyboard.press('Enter')
    pg.wait_for_function('!LZ.controllo.aperto()', timeout=30000)
    corpo = S['post'][-1] if S['post'] else {}
    check('mandati due pezzi col codice nuovo e le quantità',
          [(x['codice'], x['qta_per_foglio'], x['qta']) for x in corpo.get('pezzi') or []] == [('FOGLIO-T-2', 1, 4), ('STAFFA-T', 2, 6)],
          corpo.get('pezzi'))
    check('col contorno', all(len(x['outer_xy']) >= 4 for x in corpo.get('pezzi') or []))

    print("\n3) L'ordine e la conferma d'invio")
    pg.wait_for_function(f'(LZ.s.dett["{S["oid"]}"].dati.pezzi || []).some(p => p.codice === "STAFFA-T")', timeout=60000)
    pg.wait_for_timeout(300)
    tr = pg.locator('#lz-o-scheda tr', has_text='STAFFA-T')
    check('le righe sono i pezzi, "pezzo N del foglio"', tr.count() == 1 and 'pezzo 2 del foglio FOGLIO-T' in tr.inner_text(),
          tr.inner_text() if tr.count() else '')
    check('il foglio non c\'è più fra le righe',
          not pg.evaluate(f'LZ.s.dett["{S["oid"]}"].dati.pezzi.some(p => String(p.articolo_id) === "{S["aid"]}")'))
    pg.click('[data-az="lantek"]')
    pg.wait_for_timeout(2500)
    testo = pg.evaluate('document.body.innerText')
    check('"Manda a Lantek" elenca i pezzi nuovi', 'FOGLIO-T-2' in testo and 'STAFFA-T' in testo)
    pg.keyboard.press('Escape')
    check('nessun errore nella pagina', not err, err[:3])
    check('nessuna scrittura arrivata al server', not S['bloccate'] or all('/pezzi-foglio' not in x for x in S['bloccate']),
          S['bloccate'][:3])
    print('  scritture bloccate:', len(S['bloccate']), S['bloccate'][:3])
    b.close()

print('\n%d ok, %d ko' % (sum(esiti), len(esiti) - sum(esiti)))
sys.exit(0 if all(esiti) else 1)
