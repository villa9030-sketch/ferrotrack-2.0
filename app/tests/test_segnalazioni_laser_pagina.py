# -*- coding: utf-8 -*-
"""Nel browser: "Segnala un problema" dal tablet arriva al LASER (16/10/2026).

 1. Dal tablet dell'officina: un ordine, "Pezzo da rifare", il primo pezzo,
    una nota, Invia.
 2. Al laser: la riga dell'ordine dice "Segnalato: pezzo da rifare" in rosso,
    c'e' il promemoria "1 problema segnalato dall'officina".
 3. Aperto l'ordine: la sezione "Dall'officina" con chi, quando e nota; la
    riga del pezzo con la stessa parola; con l'ordine aperto la parola corta
    si legge intera.
 4. "Visto": la parola rossa sparisce dall'elenco, nell'ordine resta "visto".
 5. "Risolto" con "quanti da rifare": il messaggio con Annulla, la sezione
    sparisce; l'ufficio riceve "Laser: risolto".

Serve il server di prova (default 127.0.0.1:5056) su una COPIA del database.
    python app/tests/test_segnalazioni_laser_pagina.py [url]
"""
import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from playwright.sync_api import sync_playwright

B = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5056'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.accesso_server import Sessione, contesto_stazione  # noqa: E402

try:
    with urllib.request.urlopen(B + '/api/health', timeout=4) as _r:
        _salute = json.load(_r)
except Exception as _e:
    print('Server non raggiungibile su %s: %s. Test saltato.' % (B, str(_e)[:60]))
    sys.exit(0)
if not _salute.get('istanza_di_prova'):
    print('RIFIUTO: %s sta lavorando sul DATABASE VERO. Questa prova scrive.' % B)
    sys.exit(1)

esiti = []


def check(nome, cond, extra=''):
    esiti.append(bool(cond))
    print('  [%s] %s %s' % ('OK' if cond else 'KO', nome, extra if not cond else ''))


# Un ordine del tablet che passa dal laser, con dei pezzi, senza problemi aperti
rep = Sessione(B, 'reparto')
las = Sessione(B, 'laser')
c, d = rep.get('/api/officina/tablet')
assert c == 200, (c, d)
c, aperte = las.get('/api/laser/segnalazioni')
gia = set((aperte or {}).get('ordini') or {})
ordine = next((o for o in d['ordini'] if o.get('taglio_richiesto') is not False and o['id'] not in gia
               and any(p.get('codice') and p.get('tipo') != 'assieme' for p in o.get('pezzi') or [])), None)
if not ordine:
    print('Nessun ordine adatto sul server di prova: test saltato.')
    sys.exit(0)
OID = ordine['id']
PEZZO = next(p['codice'] for p in ordine['pezzi'] if p.get('codice') and p.get('tipo') != 'assieme')
NOTA = 'prova automatica: bava sul foro'
print('ordine', ordine['numero'], ordine['cliente'], 'pezzo', PEZZO)

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    # 1. il tablet
    tab = contesto_stazione(b.new_context(viewport={'width': 1280, 'height': 800}, is_mobile=True, has_touch=True), B, 'reparto')
    pt = tab.new_page()
    err = []
    pt.on('pageerror', lambda e: err.append(str(e)))
    pt.goto(B + '/operaio-info.html')
    pt.wait_for_selector(f'.ordine[data-id="{OID}"]', timeout=20000)
    pt.locator(f'.ordine[data-id="{OID}"]').click()
    pt.wait_for_selector('.segnala-btn', timeout=10000)
    pt.locator('.segnala-btn').click()
    pt.locator('#dlg-segnala button', has_text='rifare').first.click()
    pz = pt.locator('#dlg-segnala .pezzi-sc button', has_text=PEZZO)
    if pz.count():
        pz.first.click()
    pt.fill('#sg-nota', NOTA)
    pt.locator('#sg-invia').click()
    pt.wait_for_timeout(1500)
    check('tablet: segnalato', 'segnalato' in pt.locator('#dlg-segnala').inner_text().lower())
    check('tablet: nessun errore', not err, err)
    tab.close()

    c, aperte = las.get('/api/laser/segnalazioni')
    mia = next((x for x in ((aperte or {}).get('ordini') or {}).get(OID, []) if x.get('nota') == NOTA), None)
    check('il server la da\' al laser', mia is not None, aperte)
    col_pezzo = bool(mia and mia.get('codice_pezzo') == PEZZO)

    # 2. il laser
    ctx = contesto_stazione(b.new_context(viewport={'width': 1500, 'height': 950}), B, 'laser')
    pg = ctx.new_page()
    err = []
    pg.on('pageerror', lambda e: err.append(str(e)))
    pg.goto(B + '/laser.html')
    pg.wait_for_load_state('networkidle')
    pg.wait_for_selector(f'.lz-r[data-id="{OID}"]', timeout=20000)
    riga = pg.locator(f'.lz-r[data-id="{OID}"]')
    pg.wait_for_timeout(1000)
    check('elenco: "Segnalato: pezzo da rifare" in rosso',
          riga.locator('.lz-segn-l.err .lz-largo', has_text='Segnalato: pezzo da rifare').count() == 1, riga.inner_text())
    check('promemoria in alto', pg.locator('[data-az="vai-segnalati"]').count() == 1)

    # 3. l'ordine aperto
    riga.click()
    pg.wait_for_selector('.lz-segn', timeout=20000)
    sez = pg.locator('.lz-segn li', has_text=NOTA)
    check('ordine: sezione "Dall’officina" con la nota', sez.count() == 1)
    check('ordine: chi e quando', sez.count() == 1 and ', ' in sez.locator('small').first.inner_text())
    if col_pezzo:
        pg.wait_for_timeout(1500)
        rp = pg.locator('.lz-tab tr', has_text=PEZZO).locator('.lz-segn-pz', has_text='Pezzo da rifare')
        check('riga del pezzo: la parola con la nota', rp.count() >= 1 and NOTA in rp.first.inner_text())
    check('elenco stretto: la parola corta intera', riga.locator('.lz-segn-l .lz-stretto').is_visible()
          and riga.locator('.lz-segn-l').evaluate('e => e.scrollWidth <= e.clientWidth + 1'))
    if ordine.get('taglio_completato'):
        check('già tagliato: "da rimettere in Lantek"', 'da rimettere in Lantek' in sez.inner_text())

    # 4. Visto
    sez.locator('[data-az="segn-visto"]').click()
    pg.wait_for_timeout(1500)
    check('visto: niente parola rossa nell’elenco', riga.locator('.lz-segn-l.err').count() == 0)
    check('visto: nell’ordine resta "visto"', 'visto' in pg.locator('.lz-segn li', has_text=NOTA).inner_text())

    # 5. Risolto con quanti
    sez = pg.locator('.lz-segn li', has_text=NOTA)
    sez.locator('input[data-segn-quanti]').fill('2')
    sez.locator('[data-az="segn-risolto"]').click()
    pg.wait_for_timeout(1500)
    check('risolto: messaggio con Annulla', pg.locator('.ft-toast', has_text='Risolto').locator('.ft-toast-act', has_text='Annulla').count() >= 1)
    check('risolto: sparisce dall’ordine', pg.locator('.lz-segn li', has_text=NOTA).count() == 0)
    check('laser: nessun errore', not err, err)
    ctx.close()
    b.close()

uff = Sessione(B, 'ufficio')
c, d = uff.get('/api/notifications?limit=100')
msg = [(n['title'], n['message']) for n in (d.get('data') or {}).get('notifications', []) if n.get('order_id') == OID]
check('ufficio: "Laser: risolto" con quanti da rifare',
      any(t.startswith('Laser: risolto') and '2 da rifare' in m for t, m in msg), msg)

print('\nPASSATI: %d   FALLITI: %d' % (sum(esiti), len(esiti) - sum(esiti)))
sys.exit(0 if all(esiti) else 1)
