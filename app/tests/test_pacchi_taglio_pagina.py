# -*- coding: utf-8 -*-
"""Il pacco di taglio nella pagina laser (laser.html), nel browser, sul server di prova.

Il giro intero, come Stefano al laser:
 1. spunta gli ordini di una lamiera ("Seleziona i N da mandare"), "Crea pacco" (P)
 2. il pacco PROPOSTO si apre a destra: stati in fila, conti per lamiera,
    ordini, "stessi spessori che potresti aggiungere"; nella tabella la
    sezione "Pacchi" e "nel pacco N" accanto agli ordini
 3. Conferma (Invio), poi "Manda a Lantek": la finestra di sempre, col nome
    del pacco; l'invio a Lantek e' FINTO (risposta preparata qui nel browser:
    nessun programma di Lantek parte, niente arriva al server)
 4. chiusa la finestra il pacco e' In Lantek
 5. "Segna tagliata" di una lamiera, poi "Segna tutto tagliato" (Invio): il
    pacco e' Tagliato, gli ordini sono "taglio fatto" e l'ufficio li vede in
    "Forse finiti"
 6. Ctrl K trova il pacco col numero
Alla fine tutto si rimette com'era (Annulla del taglio, "In Lantek" tolto,
pacco sciolto) sulla COPIA del database. Le sole scritture lasciate passare
sono quelle dei pacchi, /importato, /lamiera-tagliata e del taglio; ogni altra
si ferma nel browser.

    python app/tests/test_pacchi_taglio_pagina.py [url]      (default 127.0.0.1:5056)
    FT_SCATTI=<cartella>: salva le schermate dei passi
"""
import json
import os
import re
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
from tests.accesso_server import Sessione, contesto_stazione  # noqa: E402

SCATTI = os.environ.get('FT_SCATTI')
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

scritture = {'passate': [], 'bloccate': [], 'lantek': []}
PASSANO = re.compile(r'/api/pacchi(/[^/]+(/(ordini|stato|tagliato))?)?$|/api/orders/[^/]+/(importato|lamiera-tagliata)$')
risposte_taglio = []


def filtro(route):
    req = route.request
    url = req.url.split('?')[0]
    if req.method in ('GET', 'HEAD', 'OPTIONS'):
        if re.search(r'/api/orders/[^/]+/laser$', url) and 'fresco=1' in req.url:
            # la finestra "Manda a Lantek" rilegge l'ordine: qui il PC del
            # server di prova non ha l'XML Importer, si fa finta che ci sia
            r = route.fetch()
            d = r.json()
            d.setdefault('lantek', {})['invio_automatico'] = True
            return route.fulfill(response=r, json=d)
        return route.continue_()
    if url.endswith('/lantek-invia'):
        # l'invio a Lantek FINTO: niente parte, niente arriva al server
        corpo = json.loads(req.post_data or '{}')
        scritture['lantek'].append((url, corpo))
        n = len(corpo.get('codici') or [])
        return route.fulfill(json={'success': True, 'pezzi_nuovi': 0, 'pezzi_creati': 0, 'pezzi_non_creati': [],
                                   'mandati': n, 'verificati': n, 'mancanti': [], 'rapporto': {}, 'da_correggere': []})
    if PASSANO.search(url):
        scritture['passate'].append(req.method + ' ' + url)
        return route.continue_()
    scritture['bloccate'].append(req.method + ' ' + req.url)
    return route.abort()


def scatto(pg, nome):
    if SCATTI:
        os.makedirs(SCATTI, exist_ok=True)
        pg.wait_for_timeout(400)
        pg.screenshot(path=os.path.join(SCATTI, nome + '.png'))


def sulla_risposta(r):
    if r.request.method == 'POST' and r.url.split('?')[0].endswith('/tagliato'):
        try:
            d = r.json()
            if d.get('fatto'):
                risposte_taglio.append(d)
        except Exception:
            pass



with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = contesto_stazione(b.new_context(viewport={'width': 1440, 'height': 900}), B, 'laser')
    ctx.route('**/*', filtro)
    pg = ctx.new_page()
    err = []
    pg.on('pageerror', lambda e: err.append(str(e)))
    pg.on('response', sulla_risposta)
    pg.goto(B + '/laser.html')
    pg.wait_for_selector('.lz-r[data-id]', timeout=30000)
    pg.wait_for_function('Object.keys(LZ.s.riep).length > 0 && LZ.sat.pronto', timeout=120000)
    pg.wait_for_timeout(800)

    print('1) Crea il pacco da una lamiera')
    # la lamiera con almeno due ordini da mandare, tutti pronti e liberi
    g = pg.evaluate("""() => {
      const S = LZ.s;
      for (const g of LZ.lista.gruppi()) {
        if (!g.lam) continue;
        const sp = g.righe.filter(r => LZ.lista.spuntabile(r.o));
        if (sp.length >= 2 && sp.every(r => (S.riep[r.o.id] || {}).pronto && !LZ.pacchi.diOrdine(r.o.id)))
          return {k: g.k, ids: sp.map(r => r.o.id)};
      }
      return null; }""")
    check('una lamiera con due ordini pronti da mandare', g, 'servono dati di prova (ft_server demo)')
    if not g:
        b.close()
        sys.exit(1)
    pg.click(f'.lz-gruppo[data-gruppo="{g["k"]}"] [data-az="sel-lam"]')
    pg.wait_for_selector('[data-az="crea-pacco"]:not([disabled])')
    t = pg.inner_text('[data-az="crea-pacco"]')
    check('"Crea pacco (2 ordini)" e\' il pulsante principale',
          'Crea pacco' in t and pg.locator('[data-az="crea-pacco"].pri').count() == 1, t)
    scatto(pg, '1-crea')
    pg.keyboard.press('p')
    pg.wait_for_selector('#lz-dett .lz-pacco', timeout=20000)
    pg.wait_for_function("() => LZ.s.pacco && LZ.s.paccoDett[LZ.s.pacco] && LZ.s.paccoDett[LZ.s.pacco].d", timeout=60000)
    pg.wait_for_timeout(500)
    pid = pg.evaluate('LZ.s.pacco')
    pacco = pg.evaluate('LZ.pacchi.trova(LZ.s.pacco)')
    check('nato PROPOSTO coi due ordini', pacco['stato'] == 'proposto' and sorted(pacco['ordini']) == sorted(g['ids']), pacco)
    check('le caselle si sono svuotate', pg.evaluate('LZ.s.spuntati.size') == 0)

    print('\n2) Il pacco proposto a destra')
    pan = pg.inner_text('#lz-dett .lz-pacco')
    check('titolo e stati in fila', pacco['nome'] in pan and all(x in pan for x in ('Proposto', 'Confermato', 'In Lantek', 'Tagliato')))
    check('lo stato di adesso e\' Proposto', pg.inner_text('.lz-pk-stati [aria-current]') == 'Proposto')
    check('il passo: conferma', 'Conferma il pacco' in pan)
    righe_lam = pg.locator('#lz-dett .lz-pk-tab').first.locator('tbody tr').count()
    check('i conti per lamiera (pezzi, kg, ore)', righe_lam >= 1 and 'kg' in pan and 'Ore' in pan, righe_lam)
    check('"Stessi spessori che potresti aggiungere"', 'Stessi spessori che potresti aggiungere' in pan)
    check('nella tabella la sezione Pacchi col pacco', pg.locator(f'.lz-r[data-pacco="{pid}"]').count() == 1)
    nota = pg.locator(f'.lz-r[data-id="{g["ids"][0]}"] .lz-in-pacco').inner_text()
    check('accanto all\'ordine: "nel pacco N"', nota == 'nel ' + pacco['nome'].lower(), nota)
    check('indaco solo sul pulsante principale del pannello',
          pg.locator('#lz-dett .lz-btn.pri').count() == 1)
    scatto(pg, '2-proposto')

    print('\n3) Conferma e manda a Lantek')
    pg.locator('#lz-dett [data-pk="conferma"]').focus()
    pg.keyboard.press('Enter')
    pg.wait_for_function("() => (LZ.pacchi.trova(LZ.s.pacco) || {}).stato === 'confermato'", timeout=20000)
    pg.wait_for_selector('#lz-dett [data-pk="manda"]:not([disabled])', timeout=30000)
    check('confermato: il passo e\' "Manda a Lantek"', 'Manda a Lantek' in pg.inner_text('#lz-dett .lz-passo'))
    pg.click('#lz-dett [data-pk="manda"]')
    pg.wait_for_selector('#lz-lantek [data-k="manda"]:not([disabled])', timeout=60000)
    tit = pg.inner_text('#lz-lt-tit')
    check('la finestra di sempre, col nome del pacco', pacco['nome'].lower() in tit.lower(), tit)
    check('dentro tutti e due gli ordini', pg.locator('#lz-lantek .lz-blocco').count() == 2)
    scatto(pg, '3-manda')
    pg.click('#lz-lantek [data-k="manda"]')
    pg.wait_for_function("() => { const b = document.querySelector('#lz-lantek [data-k=\"annulla\"]'); return b && !b.disabled; }",
                         timeout=60000)
    check('mandati uno per uno (invio finto)', len(scritture['lantek']) == 2, scritture['lantek'])
    scatto(pg, '4-mandato')
    pg.click('#lz-lantek [data-k="annulla"]')
    pg.wait_for_function("() => (LZ.pacchi.trova(LZ.s.pacco) || {}).stato === 'in_lantek'", timeout=30000)
    check('chiusa la finestra: il pacco e\' In Lantek', pg.inner_text('.lz-pk-stati [aria-current]') == 'In Lantek')
    check('gli ordini sono In Lantek', pg.evaluate(f"() => {json.dumps(g['ids'])}.every(id => LZ.statoLaser(LZ.trova(id)) === 'in_lantek')"))
    pg.wait_for_selector('#lz-dett [data-pk="lamiera"]', timeout=30000)
    scatto(pg, '5-in-lantek')

    print('\n4) Segna tagliato')
    n_lam = pg.locator('#lz-dett [data-pk="lamiera"]').count()
    pg.locator('#lz-dett [data-pk="lamiera"]').first.click()
    pg.wait_for_function('n => document.querySelectorAll("#lz-dett [data-pk=lamiera]").length < n', arg=n_lam, timeout=20000)
    check('una lamiera segnata tagliata', pg.locator('#lz-dett .lz-pk-fatto').count() >= 1)
    pg.locator('#lz-dett [data-pk="tutto"]').focus()
    pg.keyboard.press('Enter')
    pg.wait_for_function("() => (LZ.pacchi.trova(LZ.s.pacco) || {}).stato === 'tagliato'", timeout=30000)
    pg.wait_for_timeout(600)
    check('il pacco e\' Tagliato', pg.inner_text('.lz-pk-stati [aria-current]') == 'Tagliato')
    check('gli ordini sono "taglio fatto"', pg.evaluate(f"() => {json.dumps(g['ids'])}.every(id => LZ.statoLaser(LZ.trova(id)) === 'tagliato')"))
    check("il pannello dice che l'ufficio lo vede", 'Forse finiti' in pg.inner_text('#lz-dett .lz-passo'))
    scatto(pg, '6-tagliato')
    uff = Sessione(B, 'ufficio')
    c, ff = uff.get('/api/ordini/forse-finiti')
    ids_ff = {x.get('id') or x.get('order_id') for x in ((ff or {}).get('ordini') or [])}
    check("l'ufficio li vede in Forse finiti", set(g['ids']) <= ids_ff, (c, sorted(ids_ff)[:5]))

    print('\n5) Ctrl K trova il pacco')
    trovati = pg.evaluate(f"() => FTC.cerca('pacco {pacco['numero']}').map(x => x.t)")
    check('"pacco N" -> il pacco', trovati[:1] == [pacco['nome']], trovati)
    pg.keyboard.press('Escape')
    pg.wait_for_timeout(300)
    check('Esc chiude il pacco', pg.evaluate('LZ.s.pacco') is None)
    check('nessun errore nella pagina', not err, err)
    check('nessuna scrittura fuori da quelle previste', not scritture['bloccate'], scritture['bloccate'])
    b.close()

print('\n6) Rimetto com\'era (copia del database)')
las = Sessione(B, 'laser')
for d in reversed(risposte_taglio):
    c, _x = las.post(f'/api/pacchi/{pid}/tagliato', {'annulla': True, 'fatto': d['fatto']})
    check('taglio annullato', c == 200, _x)
for oid in g['ids']:
    las.post(f'/api/orders/{oid}/importato', {'annulla': True})
c1, _a = las.post(f'/api/pacchi/{pid}/stato', {'stato': 'confermato'})
c2, _b = las.post(f'/api/pacchi/{pid}/stato', {'stato': 'annullato'})
check('pacco sciolto', c1 == 200 and c2 == 200, (_a, _b))

ok = sum(esiti)
print(f'\n{ok} OK, {len(esiti) - ok} KO')
sys.exit(0 if all(esiti) else 1)
