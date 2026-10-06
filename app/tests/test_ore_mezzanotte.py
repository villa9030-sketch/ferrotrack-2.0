# -*- coding: utf-8 -*-
"""Il tablet delle ore resta acceso per giorni: dopo mezzanotte non si blocca.

Prima "oggi" si leggeva una volta all'apertura: dopo mezzanotte ogni
salvataggio veniva rifiutato dal server (giorno non corrente) e l'operaio
restava li' finche' qualcuno non ricaricava la pagina. Ora:
  - la data di oggi si rilegge da sola (in testata cambia);
  - se il server rifiuta perche' il giorno e' cambiato, le ore restano, il
    messaggio spiega cosa fare e c'e' Riprova (che salva su oggi).
E niente finestre del browser (prompt/confirm) sul tablet.

Serve un server di prova (tools/server_prova.py, porta 5056). Senza, si salta.
    python app/tests/test_ore_mezzanotte.py [url]
"""
import json
import os
import sys
import urllib.request
from datetime import date, timedelta

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
_QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_QUI))
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print('Playwright non installato: test saltato.')
    sys.exit(0)
from tests.accesso_server import contesto_stazione  # noqa: E402

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
    ctx = contesto_stazione(b.new_context(viewport={'width': 1280, 'height': 800}), B, 'ore')
    pg = ctx.new_page()
    errori, finestre = [], []
    pg.on('pageerror', lambda e: errori.append(str(e)))
    pg.on('dialog', lambda d: (finestre.append(d.type), d.dismiss()))
    pg.goto(B + '/ore.html')
    pg.wait_for_selector('#vista-nome:not([hidden])', timeout=15000)
    pg.wait_for_timeout(800)
    oggi = pg.evaluate('CTX.oggi')
    domani = (date.fromisoformat(oggi) + timedelta(days=1)).isoformat()
    check('la pagina si apre', not errori, errori[:1])

    # "Aggiungi persona": finestra dell'app, non prompt del browser
    pg.evaluate('chiediNome(), 0')
    pg.wait_for_timeout(300)
    check('aggiungi persona: finestra dell\'app', pg.locator('.ore-chiedi-testo input').count() == 1 and not finestre, finestre)
    pg.locator('.ore-chiedi-testo button[data-v="0"]').click()

    persone = pg.locator('#griglia-operai .persona')
    if not persone.count():
        # copia del database senza operai: se ne aggiunge uno dalla finestra
        # (scrive solo nella copia del server di prova)
        pg.evaluate('chiediNome(), 0')
        pg.fill('.ore-chiedi-testo input', 'Prova Mezzanotte')
        pg.locator('.ore-chiedi-testo button[data-v="1"]').click()
        pg.wait_for_selector('#griglia-operai .persona', timeout=8000)
    check('c\'e\' almeno una persona sulla bacheca', persone.count() > 0)
    if persone.count():
        persone.first.click()
        pg.wait_for_selector('#vista-giornata:not([hidden])', timeout=8000)
        prima = pg.evaluate('righe.length')
        pg.locator('#griglia-clienti .btn-grande').first.click()
        pg.wait_for_timeout(300)
        check('una riga aggiunta', pg.evaluate('righe.length') == prima + 1)

        # E' passata la mezzanotte: il server ora dice domani e rifiuta "ieri"
        def contesto(route):
            r = route.fetch()
            d = r.json(); d['oggi'] = domani
            route.fulfill(response=r, body=json.dumps(d), content_type='application/json')
        pg.route('**/api/ore/contesto', contesto)
        rifiuti = []

        def giornata(route):
            if route.request.method == 'POST' and not rifiuti:
                rifiuti.append(json.loads(route.request.post_data or '{}').get('data'))
                return route.fulfill(status=403, content_type='application/json', body=json.dumps(
                    {'success': False, 'codice': 'giorno_non_corrente', 'error': 'Giorno non corrente'}))
            if route.request.method == 'POST':
                rifiuti.append(json.loads(route.request.post_data or '{}').get('data'))
                return route.fulfill(status=200, content_type='application/json', body=json.dumps(
                    {'success': True, 'giornata': {'revisione': 1, 'totale_minuti': 30, 'righe': []}}))
            route.continue_()
        pg.route('**/api/ore/giornata**', giornata)
        pg.evaluate('scostamentoOk = true')       # niente domanda sulle 8 ore: non e' quello che si prova
        pg.click('#btn-salva')
        pg.wait_for_timeout(1500)
        msg = pg.inner_text('#msg-giornata') if pg.locator('#msg-giornata:not([hidden])').count() else ''
        check('il primo salvataggio era con la data di ieri', rifiuti[:1] == [oggi], rifiuti)
        check('messaggio chiaro: e\' cambiato il giorno', 'cambiato il giorno' in msg, msg)
        check('le ore inserite sono ancora li\'', pg.evaluate('righe.length') == prima + 1)
        check('in testata la data e\' quella nuova', pg.inner_text('#oggi').strip() ==
              '%s/%s/%s' % (domani[8:10], domani[5:7], domani[:4]), pg.inner_text('#oggi'))
        pg.locator('#msg-giornata button', has_text='Riprova').click()
        pg.wait_for_timeout(500)
        # giorno nuovo, ore dovute forse diverse: la conferma sulle ore si richiede
        if pg.locator('#chiedi.on').count():
            pg.click('#chiedi-ok')
        pg.wait_for_timeout(1200)
        check('Riprova salva sulla data nuova', rifiuti[1:2] == [domani], rifiuti)
        check('nessuna finestra del browser', not finestre, finestre)

        # L'ufficio ha cambiato la giornata mentre l'operaio la compilava (409):
        # si vedono i dati dell'ufficio, ma le ore scritte si possono rimettere
        pg.unroute('**/api/ore/giornata**')
        pg.wait_for_selector('#vista-nome:not([hidden])', timeout=8000)
        persone.first.click()
        pg.wait_for_selector('#vista-giornata:not([hidden])', timeout=8000)
        pg.locator('#griglia-clienti .btn-grande').first.click()
        pg.wait_for_timeout(300)
        mie = pg.evaluate('JSON.stringify(righe)')

        def conflitto(route):
            if route.request.method == 'POST':
                return route.fulfill(status=409, content_type='application/json', body=json.dumps(
                    {'success': False, 'codice': 'conflitto', 'error': 'modificata',
                     'giornata': {'revisione': 7, 'righe': [{'cliente': None, 'attivita_interna': True, 'minuti': 60}]}}))
            route.continue_()
        pg.route('**/api/ore/giornata**', conflitto)
        pg.evaluate('scostamentoOk = true')
        pg.click('#btn-salva')
        pg.wait_for_timeout(1000)
        check("409: si vedono i dati dell'ufficio", pg.evaluate('righe.length') == 1 and pg.evaluate('revisione') == 7)
        bt = pg.locator('#msg-giornata button', has_text='Rimetti')
        check('409: compare Rimetti le ore che avevo scritto', bt.count() == 1)
        if bt.count():
            bt.click(); pg.wait_for_timeout(300)
            check('409: le ore scritte tornano', pg.evaluate('JSON.stringify(righe)') == mie)
            check('409: la revisione resta quella nuova', pg.evaluate('revisione') == 7)
    check('nessun errore JavaScript', not errori, errori[:1])
    b.close()

print('\nPASSATI: %d   FALLITI: %d' % (sum(esiti), len(esiti) - sum(esiti)))
sys.exit(0 if all(esiti) else 1)
