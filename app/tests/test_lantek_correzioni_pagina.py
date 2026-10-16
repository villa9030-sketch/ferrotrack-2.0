# -*- coding: utf-8 -*-
"""Pagina laser, nel browser, sul server di prova: codici "da correggere in
Lantek" e "Elimina ordine".

Si prova che:
 1. con un codice da correggere: in testa all'elenco il promemoria rosso
    "1 codice da correggere in Lantek", l'etichetta dell'ordine rossa; il clic
    sul promemoria apre l'ordine; il prossimo passo e' rosso e dice cosa
    scrivere in Lantek, con "Va bene Lantek (2 mm)"
 2. Lantek corretto (Ricontrolla): avviso "… corretto in Lantek: pronto da
    mandare", il passo diventa "Manda a Lantek: 1 codice", promemoria blu
 3. "Va bene Lantek": conferma, poi il codice e' pronto da mandare
 4. Elimina ordine dal menu ⋯: la finestra dice cliente e numero (e avvisa se
    e' gia' in Lantek); Esc non elimina; "Elimina ordine" lo toglie
    dall'elenco, a destra torna "Scegli un ordine"; "Annulla" nell'avviso lo
    rimette

Lo stato "da correggere" lo inventa il test (risposte di /api/laser/ordini e
/api/orders/<id>/laser ritoccate nel browser): niente Lantek, niente scritture.
Le sole scritture lasciate passare sono DELETE /api/orders/<id> e
/ripristina sull'ordine di prova (COPIA del database), rimesso com'era alla
fine; ogni altra scrittura e' bloccata nel browser.
Serve il server di prova (default 127.0.0.1:5056).

    python app/tests/test_lantek_correzioni_pagina.py [url]
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

COD = '11PCOPA0025-01'
stato = {'ordine': None, 'fase': 'da', 'elimina': None, 'scritture': [], 'bloccate': [], 'usa': []}


def correzioni():
    f = stato['fase']
    if f == 'via':
        return {'correzioni': [], 'n_correggere': 0, 'n_corretti': 0, 'pronto': True, 'n_invio': 1, 'tutto_in_lantek': False}
    c = {'codice': COD, 'stato': 'da_correggere' if f == 'da' else 'corretto',
         'cosa': 'spessore 3 mm (in Lantek 2 mm)' if f == 'da' else 'spessore 3 mm (in Lantek 3 mm)',
         'spessore_voluto': 3.0, 'materiale_voluto': None, 'spessore_lantek': 2.0 if f == 'da' else 3.0,
         'materiale_lantek': 'FERRO', 'creato_il': '2026-10-12T08:00:00Z', 'creato_da': 'Laser', 'corretto_il': None}
    return {'correzioni': [c], 'n_correggere': 1 if f == 'da' else 0, 'n_corretti': 0 if f == 'da' else 1,
            'pronto': f != 'da', 'n_invio': 0 if f == 'da' else 1, 'tutto_in_lantek': False}


def ritocca(d):
    """Il riassunto dell'ordine di prova come se avesse un codice da correggere."""
    d.update({'errore': None, 'n_abbinare': 0, 'n_controllare': 0, 'bloccati': [],
              'lantek': {'letto': True, 'disponibile': True, 'errore': None, 'crea_pezzi': True, 'invio_automatico': True}})
    d.pop('errore', None)
    d.update(correzioni())
    return d


def filtro(route):
    req = route.request
    url = req.url.split('?')[0]
    oid = stato['ordine']
    if req.method in ('GET', 'HEAD', 'OPTIONS'):
        if oid and url.endswith('/api/laser/ordini'):
            r = route.fetch()
            d = r.json()
            if oid in (d.get('ordini') or {}):
                ritocca(d['ordini'][oid])
            return route.fulfill(response=r, body=json.dumps(d), headers={**r.headers, 'content-type': 'application/json'})
        if oid and url.endswith(f'/api/orders/{oid}/laser'):
            r = route.fetch()
            d = r.json()
            ritocca(d)
            d.update({'differenze': [], 'usa_lantek': []})
            return route.fulfill(response=r, body=json.dumps(d), headers={**r.headers, 'content-type': 'application/json'})
        return route.continue_()
    if oid and req.method == 'POST' and url.endswith(f'/api/orders/{oid}/lantek-correzioni'):
        stato['usa'].append(req.post_data)
        stato['fase'] = 'via'
        return route.fulfill(status=200, body=json.dumps({'success': True, 'codice': COD}), content_type='application/json')
    el = stato['elimina']
    if el and ((req.method == 'DELETE' and url.endswith(f'/api/orders/{el}'))
               or (req.method == 'POST' and url.endswith(f'/api/orders/{el}/ripristina'))):
        stato['scritture'].append(req.method + ' ' + url)
        return route.continue_()
    stato['bloccate'].append(req.method + ' ' + req.url)
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
    ordini = pg.evaluate("""() => LZ.ordiniLaser().filter(o => LZ.statoLaser(o) !== 'tagliato')
        .map(o => ({id: o.id, st: LZ.statoLaser(o), cliente: o.cliente || '', numero: LZ.numero(o),
                    lantek: !!o.importato_lantek_il}))""")
    coda = [o for o in ordini if o['st'] == 'coda']
    eliminato = None
    try:
        if not coda:
            print("Nessun ordine \"da mettere in Lantek\" nella copia: parti 1-3 saltate.")
        else:
            x = coda[0]
            stato['ordine'] = x['id']
            print('1) Un codice da correggere in Lantek')
            pg.evaluate('LZ.avvio.caricaRiep()')
            pg.wait_for_selector('.lz-ricorda:not(.pronti)', timeout=30000)
            t = pg.locator('.lz-ricorda:not(.pronti)').inner_text()
            check('promemoria in testa: "1 codice da correggere in Lantek"', '1 codice da correggere in Lantek' in t, t)
            # dal 15/10 l'etichetta sta sotto il cliente (.lz-sot)
            chip = pg.locator(f'.lz-r[data-id="{x["id"]}"] .lz-sot')
            check("l'etichetta dell'ordine e' rossa", 'err' in (chip.get_attribute('class') or '')
                  and 'Da correggere (1)' in chip.inner_text(), chip.inner_text())
            pg.click('.lz-ricorda:not(.pronti)')
            pg.wait_for_function(f'LZ.s.aperto === "{x["id"]}"', timeout=10000)
            pg.wait_for_selector('.lz-correggi', timeout=30000)
            passo = pg.locator('.lz-passo').inner_text()
            check('clic sul promemoria: si apre l\'ordine, passo rosso "da correggere"',
                  'err' in (pg.locator('.lz-passo').get_attribute('class') or '') and '1 codice da correggere in Lantek' in passo, passo)
            lista = pg.locator('.lz-correggi').inner_text()
            check('dice cosa scrivere in Lantek e chi l\'ha deciso', COD in lista and 'spessore 3 mm (in Lantek 2 mm)' in lista
                  and 'Deciso da Laser' in lista, lista)
            check('"Va bene Lantek (2 mm)"', pg.locator('[data-az="usa-lantek"]').inner_text().strip() == 'Va bene Lantek (2 mm)',
                  pg.locator('[data-az="usa-lantek"]').inner_text())

            print('\n2) Corretto in Lantek: Ricontrolla')
            stato['fase'] = 'ok'
            pg.evaluate('LZ.ordine.ricontrolla(LZ.s.aperto)')      # come "Ricontrolla" (il giro automatico puo' arrivare prima)
            pg.wait_for_selector(f'.ft-toast:has-text("{COD} corretto in Lantek: pronto da mandare")', timeout=30000)
            check('avviso "corretto in Lantek: pronto da mandare"', True)
            pg.wait_for_function("document.querySelector('.lz-passo') && document.querySelector('.lz-passo').innerText.includes('Manda a Lantek: 1 codice')", timeout=30000)
            check('il passo diventa "Manda a Lantek: 1 codice" (Invio)',
                  pg.locator('.lz-passo [data-az="lantek"]').count() == 1, pg.locator('.lz-passo').inner_text())
            t = pg.locator('.lz-lista-testa').inner_text()
            check('promemoria: "1 codice corretto: da mandare", non piu\' rosso',
                  '1 codice corretto: da mandare' in t and pg.locator('.lz-ricorda:not(.pronti)').count() == 0, t)
            riga = pg.locator(f'.lz-r[data-id="{x["id"]}"]')
            check("l'etichetta non e' piu' rossa", riga.locator('.lz-sot.err').count() == 0, riga.inner_text())

            print('\n3) Cambio idea: "Va bene Lantek"')
            stato['fase'] = 'da'
            pg.evaluate('LZ.ordine.ricontrolla(LZ.s.aperto)')      # come "Ricontrolla" (il giro automatico puo' arrivare prima)
            pg.wait_for_selector('[data-az="usa-lantek"]', timeout=30000)
            pg.click('[data-az="usa-lantek"]')
            pg.wait_for_selector('.ft-modal-bg.open', timeout=5000)
            check('chiede conferma', "va bene com'è in Lantek" in pg.locator('.ft-modal-bg.open').inner_text())
            pg.click('.ft-modal-bg.open button[data-v="1"]')
            pg.wait_for_function("document.querySelector('.lz-passo') && document.querySelector('.lz-passo').innerText.includes('Da mandare a Lantek')", timeout=30000)
            check('salvato e pronto da mandare', len(stato['usa']) == 1 and json.loads(stato['usa'][0]) == {'codice': COD, 'azione': 'usa_lantek'},
                  stato['usa'])
            check('niente piu\' promemoria', pg.locator('.lz-ricorda').count() == 0)
            stato['ordine'] = None
            pg.evaluate('LZ.avvio.caricaRiep()')

        print('\n4) Elimina ordine dal menu ⋯')
        cand = [o for o in ordini if o['st'] in ('coda', 'smistare')] or ordini
        if not cand:
            print('Nessun ordine al laser nella copia: parte 4 saltata.')
        else:
            y = cand[-1]
            gia = [o for o in ordini if o['lantek']]
            if gia:
                pg.click(f'.lz-r[data-id="{gia[0]["id"]}"]')
                pg.wait_for_selector('[data-az="menu"]', timeout=20000)
                pg.click('[data-az="menu"]')
                pg.click('.lz-menu button.rosso')
                pg.wait_for_selector('#lz-elimina', timeout=5000)
                check('ordine gia\' in Lantek: avvisa che gli ordini di produzione restano (MES)',
                      'Gli ordini di produzione in Lantek restano' in pg.locator('#lz-elimina').inner_text())
                pg.keyboard.press('Escape')
                pg.wait_for_selector('#lz-elimina', state='detached', timeout=5000)
            pg.click(f'.lz-r[data-id="{y["id"]}"]')
            pg.wait_for_function(f'LZ.s.aperto === "{y["id"]}"', timeout=10000)
            pg.wait_for_selector('[data-az="menu"]', timeout=20000)
            pg.click('[data-az="menu"]')
            voce = pg.locator('.lz-menu button.rosso')
            check('nel menu ⋯ "Elimina ordine…" (in rosso)', voce.count() == 1 and 'Elimina ordine' in voce.inner_text())
            voce.click()
            pg.wait_for_selector('#lz-elimina', timeout=5000)
            dlg = pg.locator('#lz-elimina').inner_text()
            check('la finestra dice cliente e numero', (y['cliente'] or 'Cliente non indicato') in dlg and y['numero'] in dlg, dlg)
            check("il fuoco e' su Annulla (Invio non elimina)",
                  pg.evaluate("document.activeElement && document.activeElement.dataset.el") == 'no')
            pg.keyboard.press('Escape')
            pg.wait_for_selector('#lz-elimina', state='detached', timeout=5000)
            check('Esc: non eliminato', not stato['scritture'] and pg.locator(f'.lz-r[data-id="{y["id"]}"]').count() == 1)
            stato['elimina'] = y['id']
            pg.click('[data-az="menu"]')
            pg.click('.lz-menu button.rosso')
            pg.wait_for_selector('#lz-elimina', timeout=5000)
            pg.click('#lz-elimina [data-el="si"]')
            eliminato = y['id']
            pg.wait_for_selector(f'.lz-r[data-id="{y["id"]}"]', state='detached', timeout=30000)
            check("eliminato: non e' piu' nell'elenco", stato['scritture'] == [f'DELETE {B}/api/orders/{y["id"]}'], stato['scritture'])
            # dal 16/10 il pannello si chiude e la tabella torna a tutta larghezza
            check('il pannello dell’ordine si chiude', pg.evaluate('LZ.s.aperto') is None
                  and not pg.evaluate("document.querySelector('.lz-main').classList.contains('con-ordine')"))
            toast = pg.locator('.ft-toast:has-text("eliminato")')
            check('avviso con "Annulla"', toast.count() >= 1 and 'Annulla' in toast.last.inner_text())
            toast.last.locator('.ft-toast-act').click()
            pg.wait_for_selector(f'.lz-r[data-id="{y["id"]}"]', timeout=30000)
            eliminato = None
            check('"Annulla": l\'ordine torna, aperto', pg.evaluate('LZ.s.aperto') == y['id']
                  and stato['scritture'][-1] == f'POST {B}/api/orders/{y["id"]}/ripristina', stato['scritture'])
    finally:
        if eliminato:      # rimesso com'era anche se il test si ferma a meta'
            r = ctx.request.post(f'{B}/api/orders/{eliminato}/ripristina')
            print('  ripristino di sicurezza:', r.status)
    check('nessun errore nella pagina', not err, err[:3])
    print('  scritture bloccate:', len(stato['bloccate']), stato['bloccate'][:3])
    b.close()

print('\n%d ok, %d ko' % (sum(esiti), len(esiti) - sum(esiti)))
sys.exit(0 if all(esiti) else 1)
