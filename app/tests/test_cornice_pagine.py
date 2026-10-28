"""La cornice uguale in ogni stazione (ft-cornice.js, 15/10/2026), nel browser.

Per ogni pagina di stazione (Laser, Ufficio, Ore dell'ufficio, Capo officina):
 1. si carica con la cornice: menu a sinistra con il nome della stazione,
    piede con Esci, barra in alto con la ricerca e "Segnala";
 2. nessun errore in console;
 3. una voce del menu fa il suo lavoro;
 4. Ctrl K porta alla casella di ricerca;
 5. a 900 px (tablet) il menu si nasconde e si apre col pulsante;
 6. Esci porta fuori (ingresso o cambio postazione);
 7. (16/10/2026) le regole "meno AI" (reports/Design FerroTrack meno AI.md):
    Inter con cifre tabellari, niente MAIUSCOLO spaziato, niente ombre sui
    pannelli, indaco profondo solo come accento; al laser la riga "Adesso" e
    i gruppi per lamiera, all'ufficio una riga al posto dei riquadri.

Serve un server di prova (tools/server_prova.py, porta 5056) su una COPIA
del database. Senza, si salta. Uso: python tests/test_cornice_pagine.py [base]
"""
import os
import sys

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from playwright.sync_api import sync_playwright  # noqa: E402
from tests.accesso_server import contesto_stazione, PIN_ADMIN  # noqa: E402

B = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5056'
B = B.rstrip('/')
ESITI = []


def check(nome, ok, dett=''):
    ESITI.append((nome, bool(ok)))
    print(('  ok  ' if ok else '  KO  ') + nome + ('' if ok else f'  -> {str(dett)[:200]}'))


# pagina, stazione del dispositivo, nome atteso nel menu, voce da provare, cosa deve succedere
PAGINE = [
    ('/laser.html', 'laser', 'Laser',
     # dal 16/10 la voce filtra la tabella; senza ordini in Lantek lo dice un avviso
     '#lz-menu [data-lz-vai="in_lantek"]', "document.querySelector('#lz-menu [data-lz-vai=\"in_lantek\"]').classList.contains('active') && LZ.s.filtroStato === 'in_lantek' || !!document.querySelector('.ft-toast')"),
    ('/impiegata.html', 'ufficio', 'Ufficio',
     '.ftc-side .ft-tab[data-tab="ordini"]', "document.getElementById('panel-ordini').classList.contains('active')"),
    ('/ufficio-ore.html', 'ufficio', 'Ufficio', None, None),
    ('/capo-officina.html', 'laser', 'Capo officina',
     '.ftc-side .ft-tab[data-tab="calendario"]', "document.getElementById('panel-calendario').classList.contains('active')"),
]

with sync_playwright() as p:
    br = p.chromium.launch(headless=True)
    for url, stazione, nome, voce, fatto in PAGINE:
        print(f'\n{url} ({stazione})')
        ctx = contesto_stazione(br.new_context(viewport={'width': 1440, 'height': 900}), B, stazione)
        if url == '/capo-officina.html':
            ctx.request.post(B + '/api/accesso/admin', data={'pin': PIN_ADMIN})
        pg = ctx.new_page()
        errori = []
        pg.on('pageerror', lambda e: errori.append('JS: ' + str(e)))
        pg.on('console', lambda m: errori.append(m.text) if m.type == 'error' and 'favicon' not in m.text else None)
        pg.goto(B + url)
        pg.wait_for_selector('.ftc-side .ftc-stazione', timeout=30000)
        pg.wait_for_timeout(2500)
        check('cornice: nome della stazione', pg.inner_text('.ftc-side .ftc-stazione b').strip() == nome, pg.inner_text('.ftc-side .ftc-stazione'))
        check('cornice: un solo menu e un solo Esci', pg.locator('.ftc-side').count() == 1 and pg.locator('.ftc-esci').count() == 1)
        check('cornice: chi sei, nel piede', pg.inner_text('#ft-user-name').strip() not in ('', '—'), pg.inner_text('#ft-user-name'))
        check('barra: la ricerca', pg.locator('.ft-topbar #ftc-cerca, .ft-topbar #cerca-globale').count() == 1)
        pg.wait_for_selector('.ft-topbar .fts-btn', timeout=15000)
        check('barra: "Segnala" nella barra', pg.locator('.ft-topbar .fts-btn').count() == 1)
        lato = pg.evaluate("getComputedStyle(document.querySelector('.ftc-side')).left")
        larg = pg.evaluate("document.querySelector('.ftc-side').getBoundingClientRect().width")
        check('menu a sinistra, 232 px', lato == '0px' and abs(larg - 232) < 1, (lato, larg))
        sx = pg.evaluate("document.querySelector('.ft-topbar').getBoundingClientRect().left")
        check('la barra comincia dopo il menu', abs(sx - 232) < 1, sx)
        check('un solo accento: indaco profondo (non il #6366F1/#4F46E5 di Tailwind)', pg.evaluate(
            "getComputedStyle(document.documentElement).getPropertyValue('--ft-accent').trim()").upper() == '#3D3FB5')
        # Regole "meno AI" (16/10/2026)
        r = pg.evaluate("""() => {
          const vis = el => { const b = el.getBoundingClientRect(); return b.width > 0 && b.height > 0; };
          const tutti = [...document.querySelectorAll('body *')].filter(vis);
          const maiusc = tutti.filter(el => { const c = getComputedStyle(el); return c.textTransform === 'uppercase' && el.textContent.trim(); })
            .map(el => el.className || el.tagName).slice(0, 3);
          const pannelli = [...document.querySelectorAll('.ft-card, .og-card, .card, .lz-tabella, .ft-kpis')].filter(vis)
            .filter(el => getComputedStyle(el).boxShadow !== 'none').map(el => el.className).slice(0, 3);
          const b = getComputedStyle(document.body);
          return { maiusc, pannelli, font: b.fontFamily, num: b.fontVariantNumeric };
        }""")
        check('Inter con cifre tabellari', r['font'].startswith('Inter') and 'tabular-nums' in r['num'], r)
        check('niente MAIUSCOLO', not r['maiusc'], r['maiusc'])
        check('pannelli senza ombra (separati per contrasto)', not r['pannelli'], r['pannelli'])
        if url == '/laser.html':
            pg.wait_for_selector('.lz-r[data-id]', timeout=30000)
            check('laser: la riga "Adesso" in cima', pg.locator('#lz-l-testa .lz-adesso').count() == 1)
            # dal 28/10: un ordine = una riga, gruppi per passo; le lamiere nella vista "Per lamiera"
            ids = pg.evaluate("[...document.querySelectorAll('.lz-r[data-id]')].map(r => r.dataset.id)")
            check('laser: ogni ordine una riga sola', len(ids) == len(set(ids)), ids)
            pg.click('#lz-menu [data-lz-vai="lamiere"]')
            pg.wait_for_timeout(400)
            lam = pg.evaluate("[...document.querySelectorAll('.lz-lam')].map(g => g.dataset.gruppo)")
            check('laser: "Per lamiera" raggruppa per lamiera', any('|' in (g or '') for g in lam), lam)
            pg.click('#lz-menu [data-lz-vai="tutti"]')
            pg.wait_for_timeout(400)
        if url == '/impiegata.html':
            check('ufficio: una riga al posto dei riquadri di numeri', pg.locator('#og-kpi.ft-adesso').count() == 1
                  and pg.locator('.og-k').count() == 0)
        if voce:
            pg.click(voce)
            pg.wait_for_timeout(600)
            check('il menu funziona', pg.evaluate(fatto))
        pg.keyboard.press('Control+k')
        check('Ctrl K: alla ricerca', pg.evaluate("['ftc-cerca','cerca-globale'].includes(document.activeElement && document.activeElement.id)"))
        pg.keyboard.press('Escape')
        # tablet: il menu si apre col pulsante
        pg.set_viewport_size({'width': 900, 'height': 1100})
        pg.wait_for_timeout(400)
        vis = pg.evaluate("getComputedStyle(document.querySelector('.ftc-side')).visibility")
        check('a 900 px il menu e\' nascosto', vis == 'hidden', vis)
        pg.click('.ftc-apri')
        pg.wait_for_timeout(400)
        check('a 900 px si apre col pulsante', pg.evaluate("getComputedStyle(document.querySelector('.ftc-side')).visibility") == 'visible')
        pg.keyboard.press('Escape')
        pg.wait_for_timeout(300)
        largo = pg.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
        check('a 900 px niente scorrimento in orizzontale', largo, pg.evaluate('document.documentElement.scrollWidth'))
        pg.set_viewport_size({'width': 1440, 'height': 900})
        check('nessun errore in console', not errori, errori[:3])
        # Esci (per ultimo: chiude la sessione di questo contesto)
        pg.click('.ftc-esci')
        try:
            pg.wait_for_url(lambda u: not u.rstrip('/').endswith(url.rstrip('/')), timeout=15000)
            uscito = True
        except Exception:
            uscito = False
        check('Esci porta fuori dalla pagina', uscito, pg.url)
        ctx.close()
    br.close()

ko = [n for n, ok in ESITI if not ok]
print(f'\n{len(ESITI) - len(ko)} ok, {len(ko)} KO')
sys.exit(1 if ko else 0)
