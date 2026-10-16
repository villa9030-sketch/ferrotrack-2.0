# -*- coding: utf-8 -*-
"""Il giro ufficio <-> laser nelle PAGINE (verifica del 13/10/2026).

 1. Stessi numeri: "In ritardo", "Taglio in ritardo" e "Laser, prossimi 5
    giorni lavorativi" uguali in impiegata.html e laser.html.
 2. Forse finiti: il laser segna "Taglio fatto", l'ufficio vede l'ordine fra
    i "Forse finiti" e lo rimanda con "Non ancora" (poi il taglio si annulla:
    la copia torna com'era).
 3. Revisione del pacchetto: i pezzi d'assieme "per assieme x N = totale" e
    "Va al laser" proposto.
 4. Conferma di un preventivo: la finestra dice se manca il PDF del cliente
    e propone "Va al laser" (si chiude con Annulla: niente viene accettato).
 5. Le parole: nessuna pagina dice "Pronto per Lantek"/"Da importare".

Serve il server di prova (default 127.0.0.1:5056) su una COPIA del database.
    python app/tests/test_flusso_ufficio_laser_pagine.py [url]
"""
import json
import os
import re
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


def numero(testo, etichetta):
    m = re.search(re.escape(etichetta) + r'\D{0,4}(\d+)', testo)
    return int(m.group(1)) if m else 0


with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    cu = contesto_stazione(b.new_context(viewport={'width': 1500, 'height': 950}), B, 'ufficio')
    cl = contesto_stazione(b.new_context(viewport={'width': 1500, 'height': 950}), B, 'laser')
    laser_api = Sessione(B, 'laser')
    u = cu.new_page()
    l = cl.new_page()
    err = []
    u.on('pageerror', lambda e: err.append('ufficio: ' + str(e)))
    l.on('pageerror', lambda e: err.append('laser: ' + str(e)))

    print('1) Stessi numeri in ufficio e al laser')
    u.goto(B + '/impiegata.html')
    u.wait_for_function("typeof CaricoLaser !== 'undefined' && CaricoLaser.riepilogo() && document.querySelector('#og-laser .og-giorni')",
                        timeout=60000)
    l.goto(B + '/laser.html')
    l.wait_for_selector('.lz-sat .riga', timeout=60000)
    l.wait_for_function("CaricoLaser.riepilogo()", timeout=60000)
    l.wait_for_timeout(800)
    rp_u = u.evaluate('CaricoLaser.riepilogo()')
    rp_l = l.evaluate('CaricoLaser.riepilogo()')
    check('stesso riepilogo dal server', rp_u['in_ritardo']['n'] == rp_l['in_ritardo']['n']
          and rp_u['taglio_in_ritardo']['n'] == rp_l['taglio_in_ritardo']['n']
          and abs(rp_u['periodo']['min'] - rp_l['periodo']['min']) < 1, (rp_u, rp_l))
    kpi = u.inner_text('#og-kpi')
    check('ufficio: "In ritardo" = consegna passata del server', numero(kpi, 'IN RITARDO') == rp_u['in_ritardo']['n']
          or numero(kpi, 'In ritardo') == rp_u['in_ritardo']['n'], kpi[:80])
    og = u.inner_text('#og-laser')
    sat = l.inner_text('.lz-sat')
    check('stessa etichetta del periodo', 'Laser, prossimi 5 giorni lavorativi' in og and 'Laser, prossimi 5 giorni lavorativi' in sat)
    ore_u = re.search(r'Laser, prossimi 5 giorni lavorativi: ([\d,]+ (?:h|min))', og)
    check("l'ufficio dice le ore del periodo", bool(ore_u), og[:120])
    if rp_u['taglio_in_ritardo']['n']:
        check('"Taglio in ritardo" con le stesse parole e lo stesso numero',
              f"Taglio in ritardo: {rp_u['taglio_in_ritardo']['n']}" in og
              and f"Taglio in ritardo: {rp_l['taglio_in_ritardo']['n']}" in l.inner_text('#lz-l-testa'), (og[:200],))

    print('\n2) Forse finiti: dal "Taglio fatto" del laser all\'ufficio')
    c, d = laser_api.get('/api/orders')
    ordini = d.get('orders') if isinstance(d, dict) else d
    cand = [o for o in ordini or [] if o.get('taglio_richiesto') is True and not o.get('taglio_completato')
            and not o.get('is_deleted') and o.get('status') == 'RICEVUTO' and o.get('lavorazione_esterna') is not True]
    fatto = None
    for o in cand:
        c, r = laser_api.post('/api/orders/%s/mark-laser-done' % o['id'], {})
        if c == 200:
            u.evaluate('caricaOggi(true)')
            try:
                # la riga dei "Forse finiti" (con "Non ancora"): un ordine in
                # ritardo resta fra i ritardi e non serve a questa prova
                u.wait_for_selector(f'#og-dafare .og-r[onclick*="{o["id"]}"]:has(button:has-text("Non ancora"))', timeout=15000)
                fatto = o
                break
            except Exception:
                laser_api.post('/api/orders/%s/mark-laser-undone' % o['id'], {})   # da procurare o altro: il prossimo
    if not fatto:
        print('  Nessun ordine adatto nella copia: parte 2 saltata.')
    else:
        try:
            riga = u.locator(f'#og-dafare .og-r[onclick*="{fatto["id"]}"]:has(button:has-text("Non ancora"))')
            check('compare fra i "Forse finiti" con le due scelte', u.locator('#og-s-forse').count() == 1
                  and riga.count() == 1 and 'Finito: DDT da fare' in riga.inner_text(), u.inner_text('#og-dafare')[:300])
            riga.locator('button:has-text("Non ancora")').click()
            u.wait_for_function(f'!FORSE_FINITI.some(f => f.id === "{fatto["id"]}")', timeout=15000)
            u.wait_for_timeout(300)
            check('"Non ancora": esce dai forse finiti', riga.count() == 0)
        finally:
            laser_api.post('/api/orders/%s/mark-laser-undone' % fatto['id'], {})

    print('\n3) Revisione del pacchetto: pezzi per assieme e "Va al laser"')
    u.evaluate("""() => { switchTab('carica'); PKG = { id: 'finto', stato: 'revisione', filtro: 'tutti', forza: false, secondi: 1, pdfUrl: '',
        dati: { righe: [], avvisi: [], assiemi: [{ codice: 'SA-1', quantita: 3 }] },
        righe: [{ id: 1, codice: 'PZ-1', q: 2, quantita_fonte: 'pdf', assieme: 'SA-1', disegno: 'PZ-1.dxf', mat: 'S235', sp: '3', avvisi: [], avvisi_tecnici: [] },
                { id: 2, codice: 'PZ-2', q: 5, quantita_fonte: 'pdf', assieme: null, disegno: 'PZ-2.dxf', mat: 'S235', sp: '3', avvisi: [], avvisi_tecnici: [] }],
        testa: { numero: 'X', cliente: 'Y', data: '', note: '' }, alLaser: true }; pkgMostra(true); pkgRender(); }""")
    qc = u.inner_text('#pkg-card tr[data-i="0"] .imp-pk-qc')
    check('pezzo d\'assieme: "per assieme x 3 = 6"', 'per assieme × 3 = 6' in qc, qc)
    check('il totale conta gli assiemi (2x3 + 5 = 11)', u.inner_text('#pkg-sum').startswith('11 pezzi'), u.inner_text('#pkg-sum')[:40])
    check('"Va al laser" proposto e spiegato', u.is_checked('#pkg-al-laser') and 'Da mandare a Lantek' in u.inner_text('#pkg-al-laser-sub'))
    u.evaluate('pkgChiudi()')

    print('\n4) Conferma di un preventivo: il PDF del cliente')
    u.evaluate("switchTab('conferme')")
    u.wait_for_timeout(1500)
    btn = u.locator('#conferme-list button:has-text("Conferma ordine")')
    if not btn.count():
        print('  Nessun preventivo da confermare nella copia: parte 4 saltata.')
    else:
        btn.first.click()
        u.wait_for_selector('#modal-accetta.open, #modal-accetta[open], #modal-accetta.show', timeout=10000)
        u.wait_for_function("document.querySelector('#acc-pdf .ft-alert')", timeout=15000)
        testo = u.inner_text('#modal-accetta')
        check('la finestra dice del PDF del cliente (manca, o chi l\'ha allegato)',
              'Manca l’ordine del cliente (PDF).' in testo or 'Ordine del cliente allegato' in testo, testo[:300])
        check('propone "Va al laser"', u.locator('#acc-laser').count() == 1 and 'Va al laser' in testo)
        u.click('#modal-accetta button:has-text("Annulla")')
        u.wait_for_timeout(500)
        check('Annulla: niente accettato, la finestra si chiude', u.evaluate('ACC') is None)

    print('\n4b) Ordini eliminati: l\'ufficio li vede e li ripristina')
    u.evaluate("switchTab('ordini', 'aperto')")
    u.wait_for_timeout(800)
    u.click('button:has-text("Eliminati")')
    u.wait_for_selector('#modal-eliminati.open', timeout=10000)
    u.wait_for_function("!document.querySelector('#el-lista .ft-skel')", timeout=15000)
    check('si apre la lista degli eliminati', 'Ordini eliminati' in u.inner_text('#modal-eliminati'))
    u.keyboard.press('Escape')

    print('\n5) Le parole')
    vecchie = ['Pronto per Lantek', 'Da importare', 'Questa settimana']
    check('il laser non dice piu\' le parole vecchie', not [v for v in vecchie if v in l.inner_text('body')])
    check('nessun errore nelle pagine', not err, err[:3])
    b.close()

print('\n%d/%d OK' % (sum(esiti), len(esiti)))
sys.exit(0 if all(esiti) else 1)
