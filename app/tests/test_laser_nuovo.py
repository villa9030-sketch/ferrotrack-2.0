# -*- coding: utf-8 -*-
"""Pagina laser nuova (laser.html) nel browser, sul server di prova.

Si prova che:
 1. si apre senza errori e NIENTE si apre da solo (nessun ordine scelto)
 2. elenco -> ordine -> controllo disegni coi tasti (frecce, V, Invio), la
    decisione e' salvata davvero (sulla COPIA del database) e poi tolta
 3. S = scegli sul disegno: modo "scegli", Esc torna al contorno del motore
 4. Esc chiude il controllo e torna ESATTAMENTE dov'eri (ordine, scheda,
    scorrimento, fuoco)
 5. "Manda a Lantek" (Lantek SIMULATO: /lantek-invia e /importato finti):
    conferma, invio coi codici visti, esito per ordine, stato che cambia
 6. piu' ordini: una coda sola col separatore fra gli ordini; Manda a Lantek
    con un riassunto per ordine
 7. l'aggiornamento automatico non sposta niente e non ridisegna cio' che si
    sta leggendo

Nessun programma di Lantek parte: /lantek-invia, /importato, /contorno e
/crea-cartella sono intercettati nel browser. Le sole scritture vere sono
decisioni sui pezzi nella copia del database, e vengono tolte alla fine.
Serve il server di prova (default 127.0.0.1:5056).

    python app/tests/test_laser_nuovo.py [url]
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

finti = {'invia': [], 'importato': [], 'contorno': [], 'giusto': []}


def finto_invio(route):
    corpo = json.loads(route.request.post_data or '{}')
    finti['invia'].append((route.request.url, corpo))
    n = len(corpo.get('codici') or []) + len(corpo.get('nuovi') or [])
    route.fulfill(status=200, content_type='application/json', body=json.dumps({
        'success': True, 'pezzi_nuovi': len(corpo.get('nuovi') or []), 'pezzi_creati': len(corpo.get('nuovi') or []),
        'pezzi_non_creati': [], 'mandati': n, 'verificati': n, 'mancanti': [],
        'rapporto': {'totale': n, 'ok': n, 'avvisi': 0, 'con_errore': 0, 'errori': []}}))


def finto(nome):
    def f(route):
        if route.request.method != 'POST':
            return route.continue_()
        finti[nome].append((route.request.url, json.loads(route.request.post_data or '{}')))
        corpo = {'success': True, 'modo': 'conferma'} if nome == 'giusto' else {'success': True}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(corpo))
    return f


def vietato(route):
    route.abort()


with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = contesto_stazione(b.new_context(viewport={'width': 1440, 'height': 900}), B, 'laser')
    ctx.route('**/lantek-invia', finto_invio)
    ctx.route('**/importato', finto('importato'))
    ctx.route('**/pezzi/*/contorno', finto('contorno'))
    ctx.route('**/pezzi/*/giusto', finto('giusto'))
    ctx.route('**/crea-cartella', vietato)
    ctx.route('**/smistamento', vietato)
    ctx.route('**/mark-laser-done', vietato)
    pg = ctx.new_page()
    err = []
    pg.on('pageerror', lambda e: err.append(str(e)))
    pg.goto(B + '/laser.html')
    pg.wait_for_selector('.lz-r[data-id]', timeout=20000)
    pg.wait_for_function('Object.keys(LZ.s.riep).length > 0', timeout=60000)

    print('1) Apertura')
    check('elenco con gli ordini', pg.locator('.lz-r[data-id]').count() > 0)
    check('nessun ordine aperto da solo', pg.evaluate('LZ.s.aperto') is None and pg.locator('.lz-r.on').count() == 0)
    check('a destra: "Scegli un ordine"', 'Scegli un ordine' in pg.locator('#lz-dett').inner_text())
    check('la riga della settimana', 'Questa settimana' in pg.locator('.lz-sat').inner_text())
    check('niente calendario ne\' lamiere', pg.locator('text=Calendario').count() == 0 and pg.locator('text=Banco lamiere').count() == 0)

    riep = pg.evaluate('LZ.s.riep')
    ctl = [k for k, v in riep.items() if v.get('stato') == 'coda' and v.get('n_controllare')]
    pronti = [k for k, v in riep.items() if v.get('stato') == 'coda' and v.get('pronto') and (v.get('lantek') or {}).get('invio_automatico')]

    if not ctl:
        print('Nessun ordine con disegni da controllare nella copia: parti 2-4 saltate.')
    else:
        oid = ctl[0]
        print('\n2) Controllo dei disegni coi tasti')
        pg.click(f'.lz-r[data-id="{oid}"]')
        pg.wait_for_function(f'LZ.s.dett["{oid}"] && LZ.s.dett["{oid}"].dati', timeout=60000)
        check('ordine aperto col prossimo passo "Controlla"', 'Controlla' in pg.locator('.lz-passo').inner_text())
        pg.click('[data-scheda="disegni"]')
        pg.click('[data-scheda="pezzi"]')
        pg.evaluate("document.getElementById('lz-dett').scrollTop = 120")
        pg.locator('.lz-r[data-id="%s"]' % oid).focus()
        prima = pg.evaluate("({a: LZ.s.aperto, s: LZ.s.scheda, y: document.getElementById('lz-dett').scrollTop, f: document.activeElement.dataset.fuoco})")
        n = riep[oid]['n_controllare']
        pg.keyboard.press('Enter')
        pg.wait_for_selector('#lz-ctl', timeout=10000)
        pg.wait_for_selector('#lz-ctl-foglio svg.lz-foglio-svg', state='attached', timeout=60000)
        check('a tutto schermo, il foglio intero', pg.locator('#lz-ctl-foglio svg[data-y-invertita="1"]').count() == 1)
        check('il contorno del motore in verde', pg.locator('#lz-ctl-foglio .fg-pezzo').count() <= 1)
        check(f'1 di {n}', f'1 di {n}' in pg.locator('#lz-ctl-barra').inner_text(), pg.locator('#lz-ctl-barra').inner_text())
        check('"Perche\' te lo chiedo" o motore sicuro', pg.locator('.lz-perche, .lz-sicuro').count() >= 1)
        if n > 1:
            pg.keyboard.press('ArrowRight')
            pg.wait_for_timeout(300)
            check('→ salta al secondo', f'2 di {n}' in pg.locator('#lz-ctl-barra').inner_text())
            pg.keyboard.press('ArrowLeft')
            pg.wait_for_timeout(300)
            check('← torna al primo', f'1 di {n}' in pg.locator('#lz-ctl-barra').inner_text())
        aid = pg.evaluate("LZ.controllo && document.querySelector('#lz-ctl') ? null : null")
        primo = pg.evaluate(f"""(LZ.s.dett["{oid}"].dati.pezzi.find(p => p.da_controllare) || {{}}).articolo_id""")
        print('\n3) S = lo scelgo io sul disegno')
        pg.keyboard.press('s')
        pg.wait_for_timeout(200)
        check('modo "scegli": cursore a croce e guida', pg.locator('#lz-ctl-foglio.scegli').count() == 1
              and 'Clicca il bordo' in pg.locator('#lz-ctl-lato').inner_text())
        box = pg.locator('#lz-ctl-foglio').bounding_box()
        pg.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
        pg.wait_for_timeout(2500)
        check('dopo il clic: niente salvato senza conferma', not finti['contorno'])
        pg.keyboard.press('Escape')
        pg.wait_for_timeout(200)
        check('Esc: torna al contorno del motore, il controllo resta aperto',
              pg.locator('#lz-ctl').count() == 1 and pg.locator('#lz-ctl-foglio.scegli').count() == 0)
        print('\n   V = va sviluppato (salvato davvero sulla copia)')
        pg.keyboard.press('v')
        pg.wait_for_timeout(1500)
        r = ctx.request.get(f'{B}/api/orders/{oid}/laser')
        pz = next((x for x in r.json().get('pezzi') or [] if x['articolo_id'] == primo), {})
        check('decisione salvata: va sviluppato, fuori da Lantek', pz.get('decisione') == 'sviluppo'
              and pz.get('escluso') == 'va sviluppato', pz.get('decisione'))
        if n > 1:
            check('passa da solo al prossimo della coda', f'2 di {n}' in pg.locator('#lz-ctl-barra').inner_text())
            pg.keyboard.press('Enter')
            pg.wait_for_timeout(800)
            check('Invio = "giusto" (chiamata intercettata, col pezzo nuovo da pulire)',
                  finti['giusto'] and 'serve_pulito' in finti['giusto'][-1][1], finti['giusto'][-1:])
        print('\n4) Esc torna dov\'eri')
        if pg.locator('#lz-ctl').count():
            pg.keyboard.press('Escape')
        pg.wait_for_timeout(500)
        dopo = pg.evaluate("({a: LZ.s.aperto, s: LZ.s.scheda, y: document.getElementById('lz-dett').scrollTop, f: document.activeElement.dataset.fuoco})")
        check('controllo chiuso', pg.locator('#lz-ctl').count() == 0)
        check('stesso ordine, stessa scheda, stesso punto, stesso fuoco',
              dopo['a'] == prima['a'] and dopo['s'] == prima['s'] and abs(dopo['y'] - prima['y']) < 2 and dopo['f'] == prima['f'],
              (prima, dopo))
        # si toglie la decisione di prova
        r = ctx.request.post(f'{B}/api/orders/{oid}/pezzi/{primo}/decisione', data={'scelta': None})
        check('decisione di prova tolta', r.status == 200)
        pg.evaluate('LZ.avvio.caricaRiep()')
        pg.wait_for_timeout(2500)
        riep = pg.evaluate('LZ.s.riep')

    print('\n5) Manda a Lantek (simulato)')
    if not pronti:
        print('   Nessun ordine pronto per Lantek nella copia: parte saltata.')
    else:
        oid = pronti[0]
        pg.click(f'.lz-r[data-id="{oid}"]')
        pg.wait_for_function(f'LZ.s.dett["{oid}"] && LZ.s.dett["{oid}"].dati', timeout=60000)
        check('prossimo passo: Manda a Lantek', 'Manda a Lantek' in pg.locator('.lz-passo').inner_text())
        pg.keyboard.press('Enter')
        pg.wait_for_selector('#lz-lantek [data-k="manda"]', timeout=60000)
        pg.wait_for_function("!document.querySelector('#lz-lantek .lz-skel')", timeout=60000)
        testo = pg.locator('#lz-lantek').inner_text()
        check('riassunto: gia\' in Lantek, nuovi', 'già in Lantek' in testo, testo[:300])
        check('nessun invio prima della conferma', not finti['invia'])
        for r in pg.locator('#lz-lantek input[value="lantek"]').all():
            r.check()
        pg.keyboard.press('Enter')
        pg.wait_for_function("document.querySelector('#lz-lantek') && document.querySelector('#lz-lantek').innerText.includes('Mandato a Lantek')", timeout=60000)
        dett = pg.evaluate(f'LZ.s.dett["{oid}"].dati.invio')
        corpo = finti['invia'][-1][1] if finti['invia'] else {}
        check('mandati esattamente i codici visti', sorted(corpo.get('codici') or []) == sorted(dett['codici'])
              and sorted(corpo.get('nuovi') or []) == sorted(dett['nuovi']), (corpo, dett))
        check('esito dell\'ordine: fatto, segnato in Lantek', 'Fatto' in pg.locator('#lz-lantek').inner_text()
              and any(oid in u for u, _ in finti['importato']))
        pg.keyboard.press('Enter')
        pg.wait_for_timeout(500)
        check('finestra chiusa', pg.locator('#lz-lantek').count() == 0)

    print('\n6) Piu\' ordini insieme')
    coda = [k for k, v in riep.items() if v.get('stato') == 'coda']
    con_ctl = [k for k in coda if riep[k].get('n_controllare')]
    if len(con_ctl) >= 2 or (con_ctl and len(coda) >= 2):
        pg.click('[data-az="sel-ctl"]')
        pg.wait_for_timeout(300)
        n_sel = pg.evaluate('LZ.s.spuntati.size')
        tot = sum(riep[k]['n_controllare'] for k in con_ctl)
        check('"Seleziona quelli da controllare" spunta gli ordini giusti', n_sel == len(con_ctl), n_sel)
        check(f'"Controlla {tot} disegni"', f'Controlla {tot} disegni' in pg.locator('.lz-azione').inner_text(),
              pg.locator('.lz-azione').inner_text())
        pg.keyboard.press('c')
        pg.wait_for_selector('#lz-ctl', timeout=60000)
        seps = pg.locator('#lz-ctl-barra .tacche i.sep').count()
        check('una coda sola, un separatore fra un ordine e l\'altro', seps == len(con_ctl) - 1, seps)
        check('il nome dell\'ordine sempre visibile', pg.locator('#lz-ctl-barra .dove').inner_text().strip() != '')
        pg.keyboard.press('Escape')
        pg.wait_for_timeout(300)
        if pronti:
            pg.click(f'.lz-r[data-id="{pronti[0]}"] input[type=checkbox]')
            pg.wait_for_timeout(300)
            pg.click('[data-az="lantek-multi"]')
            pg.wait_for_selector('#lz-lantek .lz-blocco', timeout=60000)
            pg.wait_for_function("!document.querySelector('#lz-lantek .lz-skel')", timeout=60000)
            nb = pg.locator('#lz-lantek .lz-blocco').count()
            check('un riassunto per ogni ordine selezionato (anche quelli che non partono)',
                  nb == pg.evaluate('LZ.s.spuntati.size'), nb)
            check('chi non e\' pronto dice perche\'', pg.locator('#lz-lantek .lz-blocco.fuori').count() == 0
                  or 'Non parte' in pg.locator('#lz-lantek').inner_text())
            pg.keyboard.press('Escape')
        pg.click('[data-az="sel-via"]')
    else:
        print('   Servono due ordini da mettere in Lantek, uno da controllare: parte saltata.')

    print('\n7) L\'aggiornamento automatico non sposta niente')
    primo = pg.locator('.lz-r[data-id]').first.get_attribute('data-id')
    pg.click(f'.lz-r[data-id="{primo}"]')
    pg.wait_for_timeout(2500)
    pg.evaluate("document.querySelector('#lz-o-scheda')._prova = 1; document.querySelector('.lz-righe')._prova = 1")
    pg.evaluate("document.getElementById('lz-dett').scrollTop = 80")
    y = pg.evaluate("document.getElementById('lz-dett').scrollTop")
    pg.evaluate("LZ.avvio.caricaRiep()")
    pg.wait_for_timeout(6500)
    check('stesso ordine aperto', pg.evaluate('LZ.s.aperto') == primo)
    check('stesso scorrimento', abs(pg.evaluate("document.getElementById('lz-dett').scrollTop") - y) < 2)
    check('la scheda non e\' stata rifatta', pg.evaluate("document.querySelector('#lz-o-scheda')._prova") == 1)

    check('nessun errore JavaScript', not err, err[:3])
    b.close()

ko = esiti.count(False)
print(f'\nPASSATI: {len(esiti) - ko}   FALLITI: {ko}')
sys.exit(1 if ko else 0)
