# -*- coding: utf-8 -*-
"""Pagina ore dell'ufficio: nella correzione di una giornata si vede lo storico.

Due salvataggi della stessa giornata -> due righe, la piu' recente "attuale",
con chi ha salvato e cosa e' cambiato (es. "Cliente 8 -> 6 h").

Serve un server di prova (tools/server_prova.py, porta 5056). Senza, si salta.
    python app/tests/test_ore_storico_pagina.py [url]
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
from tests.accesso_server import Sessione, contesto_stazione  # noqa: E402

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

# Dati nella COPIA del server di prova: una persona e due salvataggi di ieri
uff = Sessione(B, 'ufficio')
uff.post('/api/ore/operai', {'nome': 'Prova Storico'})
_c, d = uff.get('/api/ore/operai')
op = next(o for o in d['operai'] if o['nome'] == 'Prova Storico')
_c, cl = uff.get('/api/ore/clienti')
cliente = cl['clienti'][0]['nome'] if isinstance(cl['clienti'][0], dict) else cl['clienti'][0]
_c, ctxo = uff.get('/api/ore/contesto')
from datetime import date, timedelta  # noqa: E402
ieri = (date.fromisoformat(ctxo['oggi']) - timedelta(days=1)).isoformat()
_c, g = uff.get('/api/ore/giornata?operatore_id=%s&data=%s' % (op['id'], ieri))
rev = g['giornata'].get('revisione') or 0
c1, r1 = uff.post('/api/ore/giornata', {'operatore_id': op['id'], 'data': ieri, 'revisione_attesa': rev,
                                        'righe': [{'cliente': cliente, 'minuti': 480}], 'richiesta_id': 'st-%d-a' % rev})
c2, r2 = uff.post('/api/ore/giornata', {'operatore_id': op['id'], 'data': ieri,
                                        'revisione_attesa': r1['giornata']['revisione'],
                                        'righe': [{'cliente': cliente, 'minuti': 360},
                                                  {'attivita_interna': True, 'minuti': 120}],
                                        'richiesta_id': 'st-%d-b' % rev})
check('due salvataggi fatti', c1 == 200 and c2 == 200, (c1, r1, c2, r2))

with sync_playwright() as p:
    try:
        b = p.chromium.launch(channel='msedge', headless=True)
    except Exception:
        b = p.chromium.launch(headless=True)
    ctx = contesto_stazione(b.new_context(viewport={'width': 1600, 'height': 900}), B, 'ufficio')
    pg = ctx.new_page()
    errori = []
    pg.on('pageerror', lambda e: errori.append(str(e)))
    pg.goto(B + '/ufficio-ore.html')
    pg.wait_for_timeout(2500)
    pg.evaluate('([o, d, n]) => apriCorrezione(o, d, n)', [op['id'], ieri, op['nome']])
    pg.wait_for_selector('#corr-storico li', timeout=8000)
    voci = pg.locator('#corr-storico li')
    testo = pg.inner_text('#corr-storico')
    check('almeno due voci nello storico', voci.count() >= 2, voci.count())
    check('la prima e\' quella attuale', 'attuale' in voci.first.inner_text(), voci.first.inner_text())
    check('si vede il cambio 8 -> 6 h', '8 → 6 h' in testo, testo)
    check('si vede chi ha salvato (Ufficio)', 'Ufficio' in testo, testo)
    pg.locator('#corr-storico').screenshot(path=os.path.join(os.environ.get('TEMP', '.'), 'storico_ore.png'))
    check('nessun errore JavaScript', not errori, errori[:1])
    b.close()

print('\nPASSATI: %d   FALLITI: %d' % (sum(esiti), len(esiti) - sum(esiti)))
sys.exit(0 if all(esiti) else 1)
