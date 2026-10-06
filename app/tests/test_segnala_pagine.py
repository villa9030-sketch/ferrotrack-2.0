# -*- coding: utf-8 -*-
"""Pulsante "Segnala errore" sulle pagine vere.

 - c'e' su ogni pagina in cui si e' entrati: nella barra in alto se c'e',
   altrimenti in basso a sinistra (preventivi); non sulla pagina d'ingresso;
 - la finestra manda testo, pagina e il diario (qui: una chiamata al server
   andata male, provocata apposta);
 - l'amministrazione ha la scheda "Segnalazioni" con "Risolta".
Invii e risposte simulati: niente segnalazioni vere, niente registri toccati.

Serve un server di prova (tools/server_prova.py, porta 5056). Senza, si salta.
"""
import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
_QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_QUI))
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print('Playwright non installato: test saltato.')
    sys.exit(0)
from tests.accesso_server import contesto_stazione, PIN_ADMIN  # noqa: E402

B = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5056'
esiti = []


def check(nome, cond, extra=''):
    esiti.append(bool(cond))
    print('  [%s] %s %s' % ('OK' if cond else 'KO', nome, '' if cond else extra))


try:
    with urllib.request.urlopen(B + '/api/health', timeout=4) as r:
        salute = json.load(r)
except Exception as e:
    print('Server non raggiungibile su %s: %s. Test saltato.' % (B, str(e)[:60]))
    sys.exit(0)
if not salute.get('istanza_di_prova'):
    print('RIFIUTO: %s lavora sul database vero. Usa tools/server_prova.py.' % B)
    sys.exit(1)

with sync_playwright() as p:
    try:
        b = p.chromium.launch(channel='msedge', headless=True)
    except Exception:
        b = p.chromium.launch(headless=True)

    def pagina(stazione, url):
        ctx = contesto_stazione(b.new_context(viewport={'width': 1440, 'height': 900}), B, stazione)
        pg = ctx.new_page()
        err = []
        pg.on('pageerror', lambda e: err.append(str(e)))
        pg.goto(B + url)
        pg.wait_for_selector('.fts-btn', timeout=15000)
        return ctx, pg, err

    print('1) Dove sta il pulsante')
    for st, url, in_barra in (('ufficio', '/impiegata.html', True), ('ore', '/ore.html', True),
                              ('laser', '/laser.html', True), ('reparto', '/operaio-info.html', True),
                              ('commerciale', '/preventivi.html', True)):
        try:
            ctx, pg, err = pagina(st, url)
            nella_barra = pg.locator('.ft-topbar-end .fts-btn, .topbar-actions .fts-btn').count() == 1
            flottante = pg.locator('.fts-btn.fts-flottante').count() == 1
            check(f'{url}: c\'e\' ' + ('nella barra in alto' if in_barra else 'in basso a sinistra'),
                  nella_barra if in_barra else flottante, (nella_barra, flottante))
            check(f'{url}: nessun errore JavaScript', not err, err[:1])
            ctx.close()
        except Exception as e:
            check(f'{url}: pulsante presente', False, str(e)[:120])

    print('\n2) La finestra e cosa parte')
    ctx, pg, err = pagina('ore', '/ore.html')
    inviati = []

    def segnala(route):
        if route.request.method == 'POST':
            inviati.append(json.loads(route.request.post_data or '{}'))
            return route.fulfill(status=201, content_type='application/json', body=json.dumps({'success': True, 'id': 'x'}))
        route.continue_()
    pg.route('**/api/segnalazioni-errore', segnala)
    pg.route('**/api/ore/clienti', lambda r: r.fulfill(status=500, content_type='application/json', body='{"error":"prova"}'))
    pg.evaluate("fetch('/api/ore/clienti').catch(()=>{})")
    pg.wait_for_timeout(500)
    pg.click('.fts-btn')
    pg.wait_for_selector('.fts-velo textarea', timeout=5000)
    pg.click('.fts-velo .fts-si')
    check('testo vuoto: chiede di scrivere', 'due parole' in pg.inner_text('.fts-err'), pg.inner_text('.fts-err'))
    pg.fill('.fts-velo textarea', 'salvo le ore e non succede niente')
    pg.click('.fts-velo .fts-si')
    pg.wait_for_timeout(800)
    d = inviati[-1] if inviati else {}
    check('inviata col testo e la pagina', d.get('testo') == 'salvo le ore e non succede niente' and d.get('pagina') == '/ore.html', d)
    diario = (d.get('dettagli') or {}).get('diario') or []
    check('nel diario la chiamata andata male', any('/api/ore/clienti -> 500' in x.get('testo', '') for x in diario), diario)
    check('con postazione e browser', (d.get('dettagli') or {}).get('stazione') == 'ore' and (d.get('dettagli') or {}).get('browser'))
    check('la finestra si chiude', pg.locator('.fts-velo').count() == 0)
    check('nessun errore JavaScript', not err, err[:1])
    ctx.close()

    print('\n3) Amministrazione: scheda Segnalazioni')
    ctx = contesto_stazione(b.new_context(viewport={'width': 1440, 'height': 900}), B, 'ufficio')
    ctx.request.post(B + '/api/accesso/admin', data={'pin': PIN_ADMIN})
    pg = ctx.new_page()
    err = []
    pg.on('pageerror', lambda e: err.append(str(e)))
    finte = [{'id': 's1', 'creata_il': '2026-10-06T10:00:00Z', 'stazione': 'ore', 'dispositivo': 'Tablet ore',
              'persona': None, 'pagina': '/ore.html', 'testo': 'salvo le ore e non succede niente',
              'dettagli': {'diario': [{'tipo': 'server', 'testo': 'POST /api/ore/giornata -> 500'}]},
              'stato': 'aperta', 'risolta_il': None, 'risolta_da': None, 'nota': None}]
    messi = []

    def elenco(route):
        if route.request.method == 'GET':
            return route.fulfill(status=200, content_type='application/json',
                                 body=json.dumps({'success': True, 'segnalazioni': finte, 'aperte': 1}))
        route.continue_()

    def aggiorna(route):
        messi.append(json.loads(route.request.post_data or '{}'))
        route.fulfill(status=200, content_type='application/json', body=json.dumps({'success': True, 'segnalazione': {}}))
    pg.route('**/api/segnalazioni-errore?*', elenco)
    pg.route('**/api/segnalazioni-errore/s1', aggiorna)
    pg.goto(B + '/admin.html')
    pg.wait_for_selector('#tabs:not([hidden])', timeout=15000)
    pg.wait_for_timeout(1000)
    check('contatore sulla scheda', pg.inner_text('#seg-n').strip() == '1', pg.inner_text('#seg-n'))
    pg.click('#tabs .ft-tab[data-t=segnalazioni]')
    pg.wait_for_timeout(800)
    testo = pg.inner_text('#seg-tb')
    check('si vede cosa non va e dove', 'non succede niente' in testo and 'Timbratrice' in testo, testo[:200])
    check('si vedono gli errori tecnici', '/api/ore/giornata -> 500' in testo)
    pg.click('#seg-tb button:has-text("Risolta")')
    pg.wait_for_timeout(600)
    check('"Risolta" la segna', messi and messi[-1].get('stato') == 'risolta', messi)
    check('nessun errore JavaScript', not err, err[:1])
    ctx.close()
    b.close()

print('\nPASSATI: %d   FALLITI: %d' % (sum(esiti), len(esiti) - sum(esiti)))
sys.exit(0 if all(esiti) else 1)
