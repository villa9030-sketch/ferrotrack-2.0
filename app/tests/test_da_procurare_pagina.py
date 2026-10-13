# -*- coding: utf-8 -*-
"""Pagina laser, nel browser, sul server di prova: "Da procurare".

Il giro intero, coi tasti:
 1. si apre un disegno di un ordine "da mettere in Lantek", N: la scelta di
    cosa procurare, con T gia' scelto e profilo/lunghezza proposti (Invio
    li confermerebbe); T salva la decisione col tipo, il profilo e la lunghezza
 2. l'ordine mostra il chip "Da procurare (N)" e la sezione coi suoi pezzi;
    il bottone in testata conta una voce in piu'
 3. D apre il pannello: il tubo sta nel gruppo del suo profilo, con le barre
    e il piano di taglio; si spunta il gruppo
 4. R: lo ZIP della richiesta d'offerta (il PDF e i disegni di quel codice,
    nient'altro)
 5. O: fornitore e data prevista, Invio; "Annulla" nell'avviso lo riporta da
    ordinare; di nuovo O; poi fra gli "Ordinati" A = arrivato
 6. Esc torna all'ordine com'era; il chip e' sparito, il conteggio e' tornato

La proposta del motore (riga dell'ordine "Tubo 40x40x2 Lg. 795") la mette il
test se il pezzo vero non ne ha una: la lettura delle descrizioni e' provata
in test_da_procurare.py. Le sole scritture lasciate passare sono la decisione
sul pezzo di prova e le rotte di /api/procura (COPIA del database); alla
fine il pezzo torna com'era. Ogni altra scrittura e' bloccata nel browser.
Serve il server di prova (default 127.0.0.1:5056).

    python app/tests/test_da_procurare_pagina.py [url]
"""
import io
import json
import os
import re
import sys
import tempfile
import urllib.request
import zipfile
from datetime import date, timedelta

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

PROFILO = 'Tubo quadro 40x40 sp.2mm'
stato = {'ordine': None, 'aid': None, 'decisioni': [], 'scritture': [], 'bloccate': [], 'proposta_finta': False}


def filtro(route):
    req = route.request
    url = req.url.split('?')[0]
    oid, aid = stato['ordine'], stato['aid']
    if req.method in ('GET', 'HEAD', 'OPTIONS'):
        if aid and url.endswith(f'/api/orders/{oid}/pezzi/{aid}/procura-proposta'):
            r = route.fetch()
            d = r.json()
            if not (d.get('proposta') or {}).get('tipo'):
                stato['proposta_finta'] = True
                d['proposta'] = {'tipo': 'tubo', 'profilo': PROFILO, 'lunghezza_mm': 795, 'fonte': "riga dell'ordine",
                                 'testo': 'Tubo 40x40x2 Lg. 795', 'sezione': False, 'alternativa': 'laser_tubo'}
            return route.fulfill(response=r, body=json.dumps(d), headers={**r.headers, 'content-type': 'application/json'})
        return route.continue_()
    if req.method == 'POST' and aid and url.endswith(f'/api/orders/{oid}/pezzi/{aid}/decisione'):
        stato['decisioni'].append(req.post_data_json)
        return route.continue_()
    if req.method == 'POST' and re.search(r'/api/procura/(stato|annulla|richiesta-offerta)$', url):
        stato['scritture'].append(url.rsplit('/', 1)[-1])
        return route.continue_()
    stato['bloccate'].append(req.method + ' ' + req.url)
    return route.abort()


def chiave(nome):
    b = re.sub(r'\.[A-Za-z0-9]{2,4}$', '', os.path.basename(nome)).upper()
    return re.sub(r'[-_]\d{2}$', '', b)


with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = contesto_stazione(b.new_context(viewport={'width': 1440, 'height': 900}, accept_downloads=True), B, 'laser')
    ctx.route('**/*', filtro)
    pg = ctx.new_page()
    err = []
    pg.on('pageerror', lambda e: err.append(str(e)))
    pg.goto(B + '/laser.html')
    pg.wait_for_selector('.lz-r[data-id]', timeout=30000)
    pg.wait_for_function('Object.keys(LZ.s.riep).length > 0 && LZ.procura.n', timeout=120000)
    coda = pg.evaluate("() => LZ.ordiniLaser().filter(o => ['coda', 'in_lantek'].includes(LZ.statoLaser(o))).map(o => o.id)")
    scelto = None
    for oid in coda:
        pg.evaluate(f"LZ.lista.apri('{oid}')")
        pg.wait_for_function(f"LZ.s.dett['{oid}'] && LZ.s.dett['{oid}'].dati", timeout=120000)
        pz = pg.evaluate(f"""LZ.s.dett['{oid}'].dati.pezzi.filter(p => p.articolo_id && p.disegno && /\\.dxf$/i.test(p.disegno)
              && !p.foglio && !p.decisione).map(p => ({{aid: p.articolo_id, codice: p.codice, dec: p.decisione}}))""")
        voci = pg.evaluate(f"(LZ.s.dett['{oid}'].dati.procura_voci || []).length")
        if pz and not voci:
            scelto = (oid, pz[0])
            break
    if not scelto:
        print('Nessun ordine con un disegno da provare nella copia: test saltato.')
        b.close()
        sys.exit(0)
    oid, pezzo = scelto
    aid, cod, prima = pezzo['aid'], pezzo['codice'], pezzo['dec']
    stato.update(ordine=oid, aid=aid)
    n0 = pg.evaluate('LZ.procura.n.aperti')
    print(f'Ordine {oid[:8]}, pezzo {cod} (decisione di prima: {prima}), da procurare prima: {n0}')
    vid = None
    try:
        print('1) N e poi T')
        pg.evaluate("document.activeElement && document.activeElement.blur()")
        pg.click(f'#lz-dett tr[data-vedi="{aid}"]')
        pg.wait_for_selector('#lz-ctl .lz-foglio svg, #lz-ctl .lz-foglio-msg b', timeout=60000)
        pg.keyboard.press('n')
        pg.wait_for_selector('#lz-ctl .lz-pr-tasti .lz-btn.pri', timeout=30000)
        pri = pg.inner_text('#lz-ctl .lz-pr-tasti .lz-btn.pri')
        check('T gia\' scelto (Invio lo conferma)', 'Tubo/profilo' in pri and pg.locator('#lz-ctl .lz-pr-invio').count() == 1, pri)
        check('profilo e lunghezza proposti, modificabili',
              pg.input_value('#lz-ctl [data-pr="profilo"]') == PROFILO and pg.input_value('#lz-ctl [data-pr="lunghezza"]') == '795')
        check('L suggerito come alternativa', pg.locator('#lz-ctl [data-c="pr-laser_tubo"].suggerito').count() == 1)
        check('tutti i tasti T L M C A sui bottoni', pg.locator('#lz-ctl .lz-pr-tasti .lz-tasto').all_inner_texts()[:5] == ['T', 'L', 'M', 'C', 'A'],
              pg.locator('#lz-ctl .lz-pr-tasti .lz-tasto').all_inner_texts())
        pg.screenshot(path=os.path.join(tempfile.gettempdir(), 'ft_procura_1_n.png'))
        # Esc torna al disegno, N di nuovo
        pg.keyboard.press('Escape')
        check('Esc: torna al disegno (il controllo resta aperto)', pg.locator('#lz-ctl .lz-pr-tasti').count() == 0
              and pg.locator('#lz-ctl [data-c="non_laser"]').count() == 1)
        pg.keyboard.press('n')
        pg.wait_for_selector('#lz-ctl .lz-pr-tasti .lz-btn.pri', timeout=30000)
        pg.keyboard.press('t')
        pg.wait_for_selector('#lz-ctl', state='detached', timeout=15000)
        pg.wait_for_function(f'LZ.procura.n && LZ.procura.n.aperti === {n0 + 1}', timeout=30000)
        dec = stato['decisioni'][-1] if stato['decisioni'] else {}
        check('salvato: non_laser + tubo, profilo e lunghezza',
              dec.get('scelta') == 'non_laser' and dec.get('procura') == {'tipo': 'tubo', 'profilo': PROFILO, 'lunghezza_mm': 795, 'nota': None},
              dec)

        print('2) L\'ordine dice "Da procurare"')
        pg.wait_for_selector('#lz-dett .lz-o-sotto .lz-pc-chip', timeout=30000)
        chip = pg.inner_text('#lz-dett .lz-o-sotto .lz-pc-chip')
        check('chip ambra "Da procurare (1)"', 'Da procurare (1)' in chip
              and 'att' in pg.get_attribute('#lz-dett .lz-o-sotto .lz-pc-chip', 'class'), chip)
        pg.wait_for_selector('#lz-dett .lz-o-procura', timeout=30000)
        sez = pg.inner_text('#lz-dett .lz-o-procura')
        check('sezione dell\'ordine col pezzo e "da ordinare"', cod in sez and 'da ordinare' in sez and 'non è pronto per il montaggio' in sez, sez)
        check('bottone in testata: una voce in piu\'', f'({n0 + 1})' in pg.inner_text('#lz-procura-btn'), pg.inner_text('#lz-procura-btn'))
        riga = pg.locator(f'#lz-dett tr[data-vedi="{aid}"]').inner_text()
        check('il pezzo resta fuori da Lantek', 'non è da laser' in riga, riga)
        pg.screenshot(path=os.path.join(tempfile.gettempdir(), 'ft_procura_2_ordine.png'))

        print('3) D: il pannello')
        pg.evaluate("document.activeElement && document.activeElement.blur()")
        pg.keyboard.press('d')
        pg.wait_for_selector('#lz-pc .lz-pc-g', timeout=30000)
        gr = pg.locator('#lz-pc tr.lz-pc-g', has_text=PROFILO)
        check('gruppo del profilo, 1 barra', gr.count() == 1 and gr.locator('.lz-pc-barre').inner_text() == '1', gr.all_inner_texts())
        gr.locator('[data-pc-apri]').click()
        pg.wait_for_selector('#lz-pc .lz-pc-barre-el li', timeout=10000)
        piano = pg.inner_text('#lz-pc .lz-pc-piano')
        check('piano di taglio e da dove vengono i pezzi', 'Barra 1' in piano and cod in piano, piano[:300])
        gr.locator('[data-pc-gruppo]').check()
        pg.wait_for_selector('#lz-pc .lz-pc-az', timeout=10000)
        check('azioni sulla selezione, coi tasti R O A', pg.locator('#lz-pc .lz-pc-az .lz-tasto').all_inner_texts() == ['R', 'O', 'A'],
              pg.locator('#lz-pc .lz-pc-az .lz-tasto').all_inner_texts())
        pg.screenshot(path=os.path.join(tempfile.gettempdir(), 'ft_procura_3_pannello.png'))
        vid = pg.evaluate("[...document.querySelectorAll('#lz-pc .lz-pc-dove [data-pc-sel]')].map(x => x.dataset.pcSel)")
        vid = vid[0] if len(vid) == 1 else None
        check('una voce nel gruppo', bool(vid))

        print('4) R: richiesta d\'offerta')
        pg.evaluate("document.activeElement && document.activeElement.blur()")
        with pg.expect_download(timeout=60000) as dl:
            pg.keyboard.press('r')
        d = dl.value
        dati = open(d.path(), 'rb').read()
        z = zipfile.ZipFile(io.BytesIO(dati))
        nomi = z.namelist()
        pdf = [n for n in nomi if not n.startswith('disegni/')]
        dis = [n for n in nomi if n.startswith('disegni/')]
        check('ZIP: il PDF della richiesta', d.suggested_filename.startswith('Richiesta offerta') and len(pdf) == 1
              and z.read(pdf[0])[:5] == b'%PDF-', (d.suggested_filename, nomi))
        check('ZIP: i disegni di quel codice e nient\'altro (c\'e\' il DXF)',
              dis and all(chiave(n) == chiave(cod) for n in dis) and any(n.lower().endswith('.dxf') for n in dis), nomi)

        print('5) O ordinato, annulla, di nuovo; A arrivato')
        pg.keyboard.press('o')
        pg.wait_for_selector('#lz-pc [data-pc-forn]', timeout=10000)
        check('il fornitore si scrive subito (fuoco nel campo)', pg.evaluate("document.activeElement && document.activeElement.matches('[data-pc-forn]')"))
        pg.keyboard.type('Fornitore di prova')
        prevista = (date.today() + timedelta(days=7)).isoformat()
        pg.fill('#lz-pc [data-pc-data]', prevista)
        pg.focus('#lz-pc [data-pc-forn]')
        pg.keyboard.press('Enter')
        toast = pg.locator('.ft-toast:has-text("ordinata (Fornitore di prova)")')
        toast.last.wait_for(timeout=15000)
        pg.wait_for_function("!document.querySelector('#lz-pc tr.lz-pc-g')", timeout=15000)
        check('ordinato: sparisce da "Da ordinare"', pg.locator('#lz-pc tr.lz-pc-g').count() == 0)
        toast.last.locator('.ft-toast-act').click()
        pg.wait_for_selector('#lz-pc tr.lz-pc-g', timeout=15000)
        check('Annulla: torna da ordinare', pg.locator('#lz-pc tr.lz-pc-g', has_text=PROFILO).count() == 1)
        pg.locator('#lz-pc tr.lz-pc-g', has_text=PROFILO).locator('[data-pc-gruppo]').check()
        pg.evaluate("document.activeElement && document.activeElement.blur()")
        pg.keyboard.press('o')
        pg.wait_for_selector('#lz-pc [data-pc-forn]', timeout=10000)
        pg.fill('#lz-pc [data-pc-forn]', 'Fornitore di prova')
        pg.fill('#lz-pc [data-pc-data]', prevista)
        pg.click('#lz-pc [data-pc="ordinato-ok"]')
        pg.wait_for_function("!document.querySelector('#lz-pc tr.lz-pc-g')", timeout=15000)
        pg.click('#lz-pc [data-pc-stato="ordinato"]')
        pg.wait_for_selector('#lz-pc tr.lz-pc-g', timeout=15000)
        ap = pg.locator('#lz-pc tr.lz-pc-g', has_text=PROFILO).locator('[data-pc-apri]')
        if ap.get_attribute('aria-expanded') != 'true':        # resta aperto da prima
            ap.click()
        stato_txt = pg.inner_text('#lz-pc .lz-pc-dove')
        check('fra gli ordinati: fornitore e data prevista', 'Fornitore di prova' in stato_txt
              and (date.today() + timedelta(days=7)).strftime('%d/%m') in stato_txt, stato_txt)
        pg.locator('#lz-pc tr.lz-pc-g', has_text=PROFILO).locator('[data-pc-gruppo]').check()
        pg.evaluate("document.activeElement && document.activeElement.blur()")
        pg.keyboard.press('a')
        pg.locator('.ft-toast:has-text("arrivata")').last.wait_for(timeout=15000)
        pg.click('#lz-pc [data-pc-stato="arrivato"]')
        pg.wait_for_selector('#lz-pc tr.lz-pc-g', timeout=15000)
        check('arrivato: fra gli "Arrivati"', pg.locator('#lz-pc tr.lz-pc-g', has_text=PROFILO).count() == 1)
        pg.screenshot(path=os.path.join(tempfile.gettempdir(), 'ft_procura_5_arrivati.png'))

        print('6) Esc: si torna all\'ordine')
        pg.evaluate("document.activeElement && document.activeElement.blur()")
        pg.keyboard.press('Escape')
        pg.wait_for_selector('#lz-pc', state='detached', timeout=10000)
        check('l\'ordine aperto e\' lo stesso', pg.evaluate('LZ.s.aperto') == oid)
        pg.wait_for_function(f"!document.querySelector('#lz-dett .lz-o-sotto .lz-pc-chip') && LZ.procura.n.aperti === {n0}", timeout=30000)
        bt = pg.inner_text('#lz-procura-btn')
        check('chip sparito, conteggio tornato', pg.locator('#lz-dett .lz-o-sotto .lz-pc-chip').count() == 0
              and ((f'({n0})' in bt) if n0 else '(' not in bt), bt)
        sez = pg.inner_text('#lz-dett .lz-o-procura') if pg.locator('#lz-dett .lz-o-procura').count() else ''
        check('la sezione dell\'ordine dice "tutto arrivato"', 'tutto arrivato' in sez, sez)
    finally:
        # il pezzo torna com'era (la voce si toglie solo se e' "da ordinare")
        fine = pg.evaluate(f"""async () => {{
            const v = await (await fetch('/api/procura?stato=tutti&ordine={oid}')).json();
            const ids = (v.voci || []).filter(x => x.chiave === 'art:{aid}').map(x => x.id);
            if (ids.length) await fetch('/api/procura/stato', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{ids, stato: 'da_ordinare'}})}});
            const r = await fetch('/api/orders/{oid}/pezzi/{aid}/decisione', {{method: 'POST', headers: {{'Content-Type': 'application/json'}},
                body: JSON.stringify({{scelta: {json.dumps(prima)} }})}});
            const dopo = await (await fetch('/api/procura?stato=tutti&ordine={oid}')).json();
            return {{dec: r.status, rimaste: (dopo.voci || []).filter(x => x.chiave === 'art:{aid}').length}};
        }}""")
        check('alla fine il pezzo torna com\'era, nessuna voce rimasta', fine == {'dec': 200, 'rimaste': 0}, fine)
    check('nessun errore nella pagina', not err, err)
    check('nessun\'altra scrittura tentata', not stato['bloccate'], stato['bloccate'])
    if stato['proposta_finta']:
        print('  (proposta del motore messa dal test: il pezzo vero non ha un profilo scritto)')
    b.close()

print('\n%d/%d OK' % (sum(esiti), len(esiti)))
sys.exit(0 if all(esiti) else 1)
