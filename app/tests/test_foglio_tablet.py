# -*- coding: utf-8 -*-
"""Sul tablet di consultazione il foglio d'ordine si guarda, e si richiude.

I due tablet in officina servono a sapere cosa c'e' da fare. Il foglio
d'ordine e' la cosa piu' utile che possono mostrare, ma va mostrato bene:

  - DENTRO la pagina, non in una scheda nuova. Su un tablet fisso a schermo
    pieno una scheda nuova porta in un visore di sistema, e da li' l'operaio
    non trova piu' la strada per tornare agli ordini;
  - con pulsanti che si premono in piedi, con le mani sporche: sotto i 44
    pixel di altezza un dito manca il bersaglio;
  - richiudendolo si svuota, perche' il tablet resta acceso tutto il giorno e
    un PDF lasciato caricato occupa memoria fino a quando qualcuno se ne
    accorge.

Serve il server in ascolto. Senza, il test si salta.

    python app/tests/test_foglio_tablet.py [url]
"""
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from playwright.sync_api import sync_playwright

B = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5056'
U = {'id': 'postazione-visione', 'name': 'Tablet di visione', 'role': 'Visione',
     'permissions': ['overview']}

esiti = []


def check(nome, cond, extra=''):
    esiti.append(bool(cond))
    print('  [%s] %s %s' % ('OK' if cond else 'KO', nome, extra if not cond else ''))


import json
import urllib.request
try:
    with urllib.request.urlopen(B + '/api/health', timeout=4) as _r:
        json.load(_r)
except Exception as _e:
    print('Server non raggiungibile su %s: %s' % (B, str(_e)[:60]))
    print("Avvia un'istanza di prova e riesegui. Test saltato.")
    sys.exit(0)

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={'width': 1024, 'height': 768})
    err = []
    pg.on('pageerror', lambda e: err.append(str(e)))

    pg.goto(B + '/login.html')
    pg.evaluate('u => localStorage.setItem("currentUser", JSON.stringify(u))', U)
    pg.goto(B + '/operaio-info.html')
    pg.wait_for_load_state('networkidle')
    pg.wait_for_timeout(2500)
    check('la pagina si apre senza errori', not err, err[:1])

    # Un ordine che il foglio ce l'ha davvero.
    righe = pg.locator('.ordine')
    print('  ordini in elenco: %d' % righe.count())
    trovato = False
    for i in range(min(righe.count(), 25)):
        righe.nth(i).click()
        pg.wait_for_timeout(500)
        if pg.locator('button.apri-pdf').count():
            trovato = True
            break
        righe.nth(i).click()      # richiude
        pg.wait_for_timeout(200)
    check('c\'e\' un ordine col foglio', trovato)

    if trovato:
        b_ = pg.locator('button.apri-pdf').first
        box = b_.bounding_box()
        check('il pulsante e\' grande abbastanza per un dito',
              box and box['height'] >= 44, box)
        check('dice cosa fa', 'foglio' in b_.inner_text().lower(), b_.inner_text())

        b_.click()
        pg.wait_for_timeout(2000)
        check('il foglio si apre nella pagina, non in una scheda nuova',
              pg.locator('#foglio').is_visible() and len(pg.context.pages) == 1)
        src = pg.locator('#foglio-pdf').get_attribute('src')
        check('carica il PDF di quell\'ordine', '/pdf' in (src or ''), src)
        check('adattato alla larghezza, senza barra del visore',
              'view=FitH' in (src or '') and 'toolbar=0' in (src or ''), src)

        chiudi = pg.locator('.foglio-pieno .chiudi')
        cb = chiudi.bounding_box()
        check('il pulsante Chiudi si preme col dito',
              cb and cb['height'] >= 44, cb)

        chiudi.click()
        pg.wait_for_timeout(800)
        check('si chiude', not pg.locator('#foglio').is_visible())
        check('e non resta caricato in memoria',
              not pg.locator('#foglio-pdf').get_attribute('src'))

    # Non devono esserci piu' collegamenti ai disegni: un tablet non li apre.
    check('niente collegamenti a file che il tablet non sa aprire',
          pg.locator('a.disegno').count() == 0)
    check('nessun errore in tutta la prova', not err, err[:1])
    b.close()

print('\nPASSATI: %d   FALLITI: %d' % (sum(esiti), len(esiti) - sum(esiti)))
