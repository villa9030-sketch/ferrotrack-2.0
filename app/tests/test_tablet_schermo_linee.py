# -*- coding: utf-8 -*-
"""Tablet dell'officina: schermo intero vero e disegni con linee leggibili.

16/10/2026, Stefano: "mi rimane la barra dei comandi sotto, non e'
letteralmente a tutto schermo" e "i disegni hanno le righe finissime, si
leggono poco su tablet".

  - ogni pagina da tablet (Officina, Ore, Cartellone) ha il SUO manifest, con
    "display": "fullscreen" e start_url sulla pagina stessa: l'icona
    installata apre direttamente quella pagina, senza barre di Android;
  - il manifest del PC non ha piu' il verde vecchio;
  - aperta in Chrome (non installata) la pagina offre "Schermo intero";
  - il disegno sul tablet ha linee di almeno 1,5 px dello schermo a qualunque
    ingrandimento (vector-effect: non-scaling-stroke); quello del PC no.

Serve il server di prova in ascolto (porta 5056 o FERROTRACK_PROVA_URL).

    python app/tests/test_tablet_schermo_linee.py [url]
"""
import json
import os
import re
import sys
import urllib.request

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from playwright.sync_api import sync_playwright  # noqa: E402

B = (sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5056').rstrip('/')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.accesso_server import contesto_stazione  # noqa: E402

esiti = []


def check(nome, cond, extra=''):
    esiti.append(bool(cond))
    print('  [%s] %s %s' % ('OK' if cond else 'KO', nome, extra if not cond else ''))


try:
    with urllib.request.urlopen(B + '/api/health', timeout=4) as _r:
        json.load(_r)
except Exception as _e:
    print('Server non raggiungibile su %s: %s' % (B, str(_e)[:60]))
    print("Avvia un'istanza di prova e riesegui. Test saltato.")
    sys.exit(0)


def leggi(percorso):
    with urllib.request.urlopen(B + percorso, timeout=10) as r:
        return r.read().decode('utf-8')


PAGINE = [('/operaio-info.html', '/manifest-officina.json', 'reparto'),
          ('/ore.html', '/manifest-ore.json', 'ore'),
          ('/dashboard-live.html', '/manifest-cartellone.json', None)]

# ── i manifest ──
ids = set()
for pagina, man, _ in PAGINE:
    m = json.loads(leggi(man))
    ids.add(m.get('id'))
    check('%s: display fullscreen' % man, m.get('display') == 'fullscreen', m.get('display'))
    check('%s: prima fullscreen, poi standalone' % man,
          (m.get('display_override') or [])[:2] == ['fullscreen', 'standalone'], m.get('display_override'))
    check('%s: si apre sulla pagina del tablet' % man, m.get('start_url') == pagina, m.get('start_url'))
    scopi = {i.get('purpose') for i in m.get('icons', [])}
    check('%s: icone "any" e "maskable" separate' % man, {'any', 'maskable'} <= scopi, scopi)
    check('%s: colori di adesso (non il verde vecchio)' % man,
          m.get('theme_color', '').lower() != '#1a7a48', m.get('theme_color'))
check('un\'app diversa per pagina (id distinti)', len(ids) == 3, ids)
pc = json.loads(leggi('/manifest.json'))
check('manifest del PC: niente verde vecchio', pc.get('theme_color', '').lower() != '#1a7a48', pc.get('theme_color'))
check('manifest del PC: resta "standalone"', pc.get('display') == 'standalone', pc.get('display'))

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    for pagina, man, stazione in PAGINE:
        ctx = b.new_context(viewport={'width': 1280, 'height': 800}, device_scale_factor=2,
                            is_mobile=True, has_touch=True)
        if stazione:
            ctx = contesto_stazione(ctx, B, stazione)
        pg = ctx.new_page()
        err = []
        pg.on('pageerror', lambda e: err.append(str(e)))
        pg.goto(B + pagina)
        pg.wait_for_load_state('networkidle')
        pg.wait_for_timeout(1200)
        href = pg.evaluate("(document.querySelector('link[rel=manifest]') || {}).getAttribute ? "
                           "document.querySelector('link[rel=manifest]').getAttribute('href') : null")
        check('%s: collegata a %s' % (pagina, man), href == man, href)
        btn = pg.locator('#schermo-intero')
        check('%s: c\'e\' "Schermo intero" (pagina non installata)' % pagina,
              btn.count() == 1 and btn.is_visible())
        sw = pg.evaluate("navigator.serviceWorker.getRegistration().then(r => !!r)")
        check('%s: sw.js registrato (serve per "Installa app")' % pagina, sw)
        check('%s: nessun errore' % pagina, not err, err[:1])

        if pagina == '/operaio-info.html':
            # Il disegno a schermo pieno: SVG con linee in pixel dello schermo
            url = pg.evaluate("""() => { for (const o of ORDINI) { const d = disegniDi(o).find(x => x.svg);
                                   if (d) return [o.id, d.svg, d.mini]; } return null; }""")
            if not url:
                print('  -  nessun ordine con disegni DXF sul server: controllo delle linee saltato')
            else:
                oid, svg_url, mini_url = url
                check('il visore chiede le linee spesse', '?linee=tablet' in svg_url, svg_url)
                check('le miniature anche', '?linee=miniatura' in mini_url, mini_url)
                pg.evaluate("id => { APERTO = id; apriDisegno(0); }", oid)
                pg.wait_for_function("document.getElementById('vis-img').complete && "
                                     "document.getElementById('vis-img').naturalWidth > 0", timeout=20000)
                testo = pg.evaluate("fetch(document.getElementById('vis-img').src).then(r => r.text())")
                larg = [float(x) for x in re.findall(r'stroke-width:([0-9.]+)px', testo)]
                check('linee a spessore fisso sullo schermo (non-scaling-stroke)',
                      'non-scaling-stroke' in testo)
                check('nessuna linea sotto 1,5 px', larg and min(larg) >= 1.5, larg)
                # ingrandito 4 volte la linea non si assottiglia: lo spessore e' in px
                pg.evaluate("zoomIn(4)")
                pg.wait_for_timeout(300)
                check('ingrandito resta lo stesso SVG (stesse linee in px)',
                      pg.evaluate("document.getElementById('vis-img').src") == svg_url)
                pc_svg = pg.evaluate("u => fetch(u).then(r => r.text())", svg_url.split('?')[0])
                check('il disegno del PC resta com\'era', 'non-scaling-stroke' not in pc_svg)
                pg.evaluate("chiudiVisore()")
        ctx.close()
    b.close()

print('\nPASSATI: %d   FALLITI: %d' % (sum(esiti), len(esiti) - sum(esiti)))
sys.exit(0 if all(esiti) else 1)
