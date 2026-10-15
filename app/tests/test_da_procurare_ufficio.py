# -*- coding: utf-8 -*-
"""Pagina dell'Ufficio (impiegata.html), nel browser, sul server di prova:
"Da procurare" arriva all'amministrazione, che ordina.

Si prova che:
 1. il laser classifica un pezzo (N poi T, qui via API come farebbe la
    pagina del laser): all'Ufficio arriva UN avviso per l'ordine nella
    campanella ("1 pezzo da procurare per ...") e il bottone in testata
    conta "Da procurare (N)"
 2. il bottone apre lo STESSO pannello del laser (laser-procura.js): il tubo
    nel gruppo del suo profilo con le barre; si spunta; R scarica lo ZIP
 3. O: fornitore e data prevista, Invio; poi fra gli ordinati A = arrivato
 4. Esc chiude e la pagina dell'Ufficio e' com'era; nessun errore

Le scritture del browser lasciate passare sono solo quelle di /api/procura
(e lettura/segno degli avvisi); la voce di prova la crea il test e alla fine
la toglie (COPIA del database). Serve il server di prova (127.0.0.1:5056).

    python app/tests/test_da_procurare_ufficio.py [url]
"""
import io
import json
import os
import re
import sys
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
from tests.accesso_server import Sessione, contesto_stazione  # noqa: E402

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

PROFILO = 'Tubo quadro 35x35 sp.2mm'  # non nella copia: i laser tubo DECA sono 50x50 (barre anche loro)
bloccate, scritture = [], []


def filtro(route):
    req = route.request
    url = req.url.split('?')[0]
    if req.method in ('GET', 'HEAD', 'OPTIONS'):
        return route.continue_()
    if req.method == 'POST' and re.search(r'/api/procura/(stato|annulla|richiesta-offerta)$', url):
        scritture.append(url.rsplit('/', 1)[-1])
        return route.continue_()
    if re.search(r'/api/notifications/[^/]+/read$', url):
        return route.continue_()
    bloccate.append(req.method + ' ' + req.url)
    return route.abort()


# ── la voce di prova: il laser dice N + T su un pezzo senza decisione ──
laser = Sessione(B, 'laser')
c, d = laser.get('/api/laser/ordini')
scelto = None
for oid, r in (d.get('ordini') or {}).items():
    if r.get('stato') not in ('coda', 'in_lantek'):
        continue
    c, o = laser.get(f'/api/orders/{oid}/laser')
    if (o.get('procura_voci') or []):
        continue
    p = next((x for x in o.get('pezzi') or [] if x.get('articolo_id') and not x.get('decisione') and not x.get('foglio')), None)
    if p:
        scelto = (oid, p)
        break
if not scelto:
    print('Nessun pezzo da provare nella copia: test saltato.')
    sys.exit(0)
oid, pezzo = scelto
aid, cod = pezzo['articolo_id'], pezzo['codice']
print(f'Ordine {oid[:8]}, pezzo {cod}')
try:
    c, d = laser.post(f'/api/orders/{oid}/pezzi/{aid}/decisione',
                      {'scelta': 'non_laser', 'procura': {'tipo': 'tubo', 'profilo': PROFILO, 'lunghezza_mm': 1200}})
    check('il laser classifica (N + T)', c == 200 and (d.get('procura') or {}).get('tipo') == 'tubo', (c, d))

    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        ctx = contesto_stazione(b.new_context(viewport={'width': 1440, 'height': 900}, accept_downloads=True), B, 'ufficio')
        ctx.route('**/*', filtro)
        pg = ctx.new_page()
        err = []
        pg.on('pageerror', lambda e: err.append(str(e)))
        pg.goto(B + '/impiegata.html')
        pg.wait_for_selector('#lz-procura-btn', timeout=30000)
        pg.wait_for_function("LZ.procura && LZ.procura.n && LZ.procura.n.aperti > 0", timeout=60000)
        n0 = pg.evaluate('LZ.procura.n.aperti')
        bt = pg.inner_text('#lz-procura-btn')
        check('1) bottone in testata "Da procurare (N)", ambra', f'({n0})' in bt
              and 'att' in pg.get_attribute('#lz-procura-btn', 'class'), bt)
        pg.click('#notif-btn')
        pg.wait_for_timeout(1200)
        campana = pg.inner_text('#notif-panel-body')
        check("avviso nella campanella dell'Ufficio, uno per l'ordine", re.search(r'\d+ pezz[oi] da procurare per ', campana)
              and campana.count('da procurare per') >= 1, campana[:400])
        pg.click('#notif-btn')
        y0 = pg.evaluate('window.scrollY')

        pg.click('#lz-procura-btn')
        pg.wait_for_selector('#lz-pc .lz-pc-g', timeout=30000)
        gr = pg.locator('#lz-pc tr.lz-pc-g', has_text=PROFILO)
        check('2) lo stesso pannello: il gruppo del profilo, 1 barra', gr.count() == 1
              and gr.locator('.lz-pc-barre').inner_text() == '1', gr.all_inner_texts())
        pg.wait_for_timeout(600)
        pg.screenshot(path=os.path.join(os.environ.get('TEMP', '.'), 'ft_procura_ufficio.png'))
        check('il pannello copre la pagina (opaco)', pg.evaluate("getComputedStyle(document.getElementById('lz-pc')).backgroundColor") == 'rgb(243, 244, 246)'
              and pg.evaluate("getComputedStyle(document.getElementById('lz-pc')).opacity") == '1')
        gr.locator('[data-pc-gruppo]').check()
        pg.wait_for_selector('#lz-pc .lz-pc-az', timeout=10000)
        pg.evaluate("document.activeElement && document.activeElement.blur()")
        with pg.expect_download(timeout=60000) as dl:
            pg.keyboard.press('r')
        z = zipfile.ZipFile(io.BytesIO(open(dl.value.path(), 'rb').read()))
        check('R: ZIP con il PDF della richiesta', any(n.startswith('Richiesta offerta') and n.endswith('.pdf') for n in z.namelist()),
              z.namelist())

        pg.keyboard.press('o')
        pg.wait_for_selector('#lz-pc [data-pc-forn]', timeout=10000)
        pg.keyboard.type('Tubi Rossi')
        pg.fill('#lz-pc [data-pc-data]', (date.today() + timedelta(days=4)).isoformat())
        pg.focus('#lz-pc [data-pc-forn]')
        pg.keyboard.press('Enter')
        pg.locator('.ft-toast:has-text("Tubi Rossi")').last.wait_for(timeout=15000)
        pg.click('#lz-pc [data-pc-stato="ordinato"]')
        pg.locator('#lz-pc tr.lz-pc-g', has_text=PROFILO).first.wait_for(timeout=15000)  # il gruppo di prova, non uno qualunque
        g2 = pg.locator('#lz-pc tr.lz-pc-g', has_text=PROFILO)
        check('3) O: fra gli ordinati', g2.count() == 1)
        g2.locator('[data-pc-gruppo]').check()
        pg.evaluate("document.activeElement && document.activeElement.blur()")
        pg.keyboard.press('a')
        pg.locator('.ft-toast:has-text("arrivat")').last.wait_for(timeout=15000)
        pg.click('#lz-pc [data-pc-stato="arrivato"]')
        pg.locator('#lz-pc tr.lz-pc-g', has_text=PROFILO).first.wait_for(timeout=15000)  # il gruppo di prova, non uno qualunque
        check('A: fra gli arrivati', pg.locator('#lz-pc tr.lz-pc-g', has_text=PROFILO).count() == 1)

        pg.evaluate("document.activeElement && document.activeElement.blur()")
        pg.keyboard.press('Escape')
        pg.wait_for_selector('#lz-pc', state='detached', timeout=10000)
        check("4) Esc: la pagina dell'Ufficio com'era", pg.evaluate('window.scrollY') == y0 and pg.locator('.ft-tabs, nav').count() > 0)
        pg.wait_for_function(f'LZ.procura.n.aperti === {n0 - 1}', timeout=15000)
        check('il bottone conta una voce in meno', f'({n0 - 1})' in pg.inner_text('#lz-procura-btn') if n0 > 1
              else '(' not in pg.inner_text('#lz-procura-btn'), pg.inner_text('#lz-procura-btn'))
        c, st = laser.get(f'/api/procura?stato=tutti&ordine={oid}')
        v = next((x for x in st.get('voci') or [] if x['chiave'] == f'art:{aid}'), {})
        check('fatto dalla persona dell\'Ufficio (aggiornato da)', v.get('aggiornato_da') == 'Prova Ufficio', v.get('aggiornato_da'))
        check('nessun errore nella pagina', not err, err)
        check("nessun'altra scrittura tentata", not bloccate, bloccate)
        b.close()
finally:
    c, st = laser.get(f'/api/procura?stato=tutti&ordine={oid}')
    ids = [x['id'] for x in st.get('voci') or [] if x['chiave'] == f'art:{aid}']
    if ids:
        laser.post('/api/procura/stato', {'ids': ids, 'stato': 'da_ordinare'})
    c, _ = laser.post(f'/api/orders/{oid}/pezzi/{aid}/decisione', {'scelta': None})
    c2, st = laser.get(f'/api/procura?stato=tutti&ordine={oid}')
    check("alla fine il pezzo torna com'era", c == 200 and not [x for x in st.get('voci') or [] if x['chiave'] == f'art:{aid}'])

print('\n%d/%d OK' % (sum(esiti), len(esiti)))
sys.exit(0 if all(esiti) else 1)
