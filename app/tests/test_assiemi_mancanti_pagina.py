# -*- coding: utf-8 -*-
"""Assiemi senza componenti nella PAGINA dell'ufficio (DECA A 001252).

Il caso vero: si scelgono i soli file della radice di LS 1252 (non la
cartella). La revisione deve:
  - dire in rosso "Mancano i disegni di 7 assiemi" coi 7 codici;
  - dire "Hai scelto dei file: ... Trascina la cartella intera.";
  - tenere spento "Crea ordine" finche' non si scrive CREA in
    "Crea lo stesso (gli assiemi restano bloccati)".
Poi l'analisi si scarta (niente ordine creato). La pagina laser si apre
senza errori.

Serve il server di prova su una COPIA del database.
    python app/tests/test_assiemi_mancanti_pagina.py [url]
"""
import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from playwright.sync_api import sync_playwright

B = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5056'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.accesso_server import contesto_stazione  # noqa: E402

LS1252 = r'C:\Users\Stefano\Desktop\AUTOCAD\TAGLIO\DECA\ORDINI\LS 1252\LS 1252'

try:
    with urllib.request.urlopen(B + '/api/health', timeout=4) as _r:
        _salute = json.load(_r)
except Exception as _e:
    print('Server non raggiungibile su %s: %s. Test saltato.' % (B, str(_e)[:60]))
    sys.exit(0)
if not _salute.get('istanza_di_prova'):
    print('RIFIUTO: %s sta lavorando sul DATABASE VERO. Questa prova scrive.' % B)
    sys.exit(1)
if not os.path.isdir(LS1252):
    print('Cartella LS 1252 non trovata: test saltato.')
    sys.exit(0)

esiti = []


def check(nome, cond, extra=''):
    esiti.append(bool(cond))
    print('  [%s] %s %s' % ('OK' if cond else 'KO', nome, extra if not cond else ''))


ATTESI = ['25NDSSA0622-00', '25NDSSA0625-00', '25NDSSA1021', '25NDSSA1022',
          '25NDSSA1024', '25NDSSA1028', '25NDSSA1055']
radice = [os.path.join(LS1252, f) for f in sorted(os.listdir(LS1252))
          if f.lower().endswith(('.pdf', '.dxf')) and os.path.isfile(os.path.join(LS1252, f))]

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    cu = contesto_stazione(b.new_context(viewport={'width': 1500, 'height': 950}), B, 'ufficio')
    u = cu.new_page()
    err = []
    u.on('pageerror', lambda e: err.append('ufficio: ' + str(e)))

    print('1) La cartella e\' l\'azione principale')
    u.goto(B + '/impiegata.html')
    u.wait_for_function("typeof switchTab === 'function'", timeout=60000)
    u.evaluate("switchTab('carica')")
    check('"Scegli la cartella" in evidenza', u.locator('#pdf-upload-btn button.primary:has-text("Scegli la cartella")').count() == 1)

    print('\n2) LS 1252 coi soli file della radice')
    u.set_input_files('#pdf-input', radice)
    u.wait_for_selector('#pkg-card .imp-pk-blocco, #pkg-card .imp-pk-foot', timeout=300000)
    blocco = u.locator('#pkg-card .imp-pk-blocco')
    testo = blocco.inner_text() if blocco.count() else ''
    check('blocco rosso: mancano i disegni di 7 assiemi', 'Mancano i disegni di 7 assiemi' in testo, testo[:200])
    check('i 7 codici elencati', all(c in testo for c in ATTESI), testo[:400])
    check('"Hai scelto dei file ... Trascina la cartella intera."',
          'Hai scelto dei file: le sottocartelle degli assiemi non ci sono. Trascina la cartella intera.' in testo)
    # una data valida: resta solo il blocco degli assiemi
    from datetime import date, timedelta
    u.fill('#pkg-data', (date.today() + timedelta(days=60)).isoformat())
    check('"Crea ordine" spento, e il perche\'', u.is_disabled('#pkg-crea')
          and 'i disegni degli assiemi' in u.inner_text('#pkg-hint'), u.inner_text('#pkg-hint'))
    check('le righe degli assiemi sono rosse', u.locator('#pkg-card tr.manca').count() == 7,
          u.locator('#pkg-card tr.manca').count())
    u.click('#pkg-card button:has-text("Crea lo stesso (gli assiemi restano bloccati)")')
    u.fill('#pkg-stesso-input', 'si')
    check('una parola qualsiasi non basta', u.is_disabled('#pkg-crea'))
    u.fill('#pkg-stesso-input', 'CREA')
    check('scritto CREA: "Crea ordine" si accende', not u.is_disabled('#pkg-crea'))
    if os.environ.get('FERROTRACK_SCREEN_DIR'):
        u.screenshot(path=os.path.join(os.environ['FERROTRACK_SCREEN_DIR'], 'assiemi_mancanti_ufficio.png'))
    # niente ordine: si scarta l'analisi
    pid = u.evaluate('PKG && PKG.id')
    u.evaluate("(id) => inviaJson('/api/orders/da-pacchetto/' + encodeURIComponent(id), {}, 'DELETE')", pid)
    u.evaluate('pkgChiudi()')

    print('\n3) Laser: la pagina si apre')
    cl = contesto_stazione(b.new_context(viewport={'width': 1500, 'height': 950}), B, 'laser')
    l = cl.new_page()
    l.on('pageerror', lambda e: err.append('laser: ' + str(e)))
    l.goto(B + '/laser.html')
    l.wait_for_selector('.lz-sat .riga', timeout=60000)
    l.wait_for_timeout(1500)
    check('nessun errore di pagina', not err, err[:3])
    b.close()

print('\n%d ok, %d ko' % (sum(esiti), len(esiti) - sum(esiti)))
sys.exit(0 if all(esiti) else 1)
