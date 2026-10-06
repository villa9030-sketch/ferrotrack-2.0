# -*- coding: utf-8 -*-
"""Pagina ufficio, "Carica ordine": il valore dell'ordine gia' prezzato.

Caricando il PDF di un ordine gia' prezzato il campo "Valore ordine" si
compila con la spiegazione; se la somma non torna resta vuoto con l'avviso; il
valore (anche scritto a mano, "5.770,30") parte con la creazione dell'ordine.
Scritture simulate: nessun file, nessun ordine vero.

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

PDF_FINTO = b'%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF'
LETTURE = [
    {'pdf_filename': 'x_300099001.pdf', 'numero_ordine': '300099001', 'cliente': 'Poliform',
     'data_consegna': '2030-11-06', 'valore_ordine': 5770.3,
     'lettura_prezzi': {'formato': 'Poliform - Ordine Fornitore', 'n_righe': 7, 'somma_righe': 5770.3,
                        'totale_stampato': 5770.3, 'quadra': True}},
    {'pdf_filename': 'x_2.pdf', 'numero_ordine': '2', 'cliente': 'Poliform', 'data_consegna': '2030-11-06',
     'valore_ordine': None,
     'lettura_prezzi': {'formato': 'Poliform - Ordine Fornitore', 'n_righe': 7, 'somma_righe': 5000.0,
                        'totale_stampato': 5770.3, 'quadra': False}},
    {'pdf_filename': 'x_3.pdf', 'numero_ordine': None, 'cliente': None, 'data_consegna': None,
     'valore_ordine': 769.0,
     'lettura_prezzi': {'formato': 'Lettura automatica (formato nuovo)', 'generico': True, 'n_righe': 3,
                        'somma_righe': 769.0, 'totale_stampato': 769.0, 'quadra': True}},
]

with sync_playwright() as p:
    try:
        b = p.chromium.launch(channel='msedge', headless=True)
    except Exception:
        b = p.chromium.launch(headless=True)
    ctx = contesto_stazione(b.new_context(viewport={'width': 1600, 'height': 1000}), B, 'ufficio')
    pg = ctx.new_page()
    errori, inviati = [], []
    pg.on('pageerror', lambda e: errori.append(str(e)))
    giro = {'n': 0}

    def estrai(route):
        d = LETTURE[giro['n']]; giro['n'] += 1
        route.fulfill(status=200, content_type='application/json', body=json.dumps({'success': True, 'data': d}))

    def ordini(route):
        if route.request.method == 'POST':
            inviati.append(json.loads(route.request.post_data or '{}'))
            return route.fulfill(status=400, content_type='application/json',
                                 body=json.dumps({'success': False, 'error': 'prova: non creato'}))
        route.continue_()
    pg.route('**/api/extract-pdf-data', estrai)
    pg.route('**/api/orders', ordini)
    pg.goto(B + '/impiegata.html')
    pg.wait_for_timeout(2500)
    pg.evaluate("switchTab('carica')")
    pg.wait_for_timeout(500)
    pg.set_input_files('#panel-carica input[type=file]', files=[{'name': '300099001.pdf', 'mimeType': 'application/pdf', 'buffer': PDF_FINTO}])
    pg.wait_for_timeout(1200)
    check('valore compilato dal PDF', pg.input_value('#mod-valore') == '5770,30', pg.input_value('#mod-valore'))
    check('spiega da dove viene', 'somma torna' in pg.inner_text('#hint-valore'), pg.inner_text('#hint-valore'))
    check('numero e cliente compilati', pg.input_value('#mod-numero') == '300099001' and pg.input_value('#mod-cliente') == 'Poliform')
    pg.click('#co-submit')
    pg.wait_for_timeout(800)
    check('il valore parte con l\'ordine', inviati and inviati[-1].get('valore_ordine') == 5770.3, inviati[-1:])

    # secondo PDF: la somma non torna -> niente valore, avviso
    pg.evaluate('resetCaricaForm(); rimuoviPdf()')
    pg.set_input_files('#panel-carica input[type=file]', files=[{'name': '2.pdf', 'mimeType': 'application/pdf', 'buffer': PDF_FINTO}])
    pg.wait_for_timeout(1200)
    check('somma che non torna: campo vuoto', pg.input_value('#mod-valore') == '')
    check('e avviso di scriverlo a mano', 'non torna' in pg.inner_text('#hint-valore'), pg.inner_text('#hint-valore'))
    pg.fill('#mod-valore', '5.770,30')
    pg.click('#co-submit')
    pg.wait_for_timeout(800)
    check('scritto a mano "5.770,30" -> 5770.3', inviati and inviati[-1].get('valore_ordine') == 5770.3, inviati[-1:])
    pg.fill('#mod-valore', 'tanto')
    pg.click('#co-submit')
    pg.wait_for_timeout(500)
    check('importo non valido: errore sul campo', 'importo' in pg.inner_text('#err-valore'), pg.inner_text('#err-valore'))
    # terzo PDF: cliente con un formato nuovo, lettura automatica
    pg.evaluate('resetCaricaForm(); rimuoviPdf()')
    pg.set_input_files('#panel-carica input[type=file]', files=[{'name': '3.pdf', 'mimeType': 'application/pdf', 'buffer': PDF_FINTO}])
    pg.wait_for_timeout(1200)
    check('formato nuovo: valore proposto', pg.input_value('#mod-valore') == '769,00', pg.input_value('#mod-valore'))
    check('e avviso "lettura automatica, controlla bene"',
          'Lettura automatica' in pg.inner_text('#hint-valore') and 'Controlla bene' in pg.inner_text('#hint-valore'),
          pg.inner_text('#hint-valore'))
    check('numero e cliente lasciati a chi carica', pg.input_value('#mod-numero') == '' and pg.input_value('#mod-cliente') == '')
    check('nessun errore JavaScript', not errori, errori[:1])
    b.close()

print('\nPASSATI: %d   FALLITI: %d' % (sum(esiti), len(esiti) - sum(esiti)))
sys.exit(0 if all(esiti) else 1)
