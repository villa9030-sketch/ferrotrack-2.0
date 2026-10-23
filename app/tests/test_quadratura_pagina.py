# -*- coding: utf-8 -*-
"""Quadratura nella pagina laser, nel browser, sul server di prova.

Si prova che:
 1. nell'elenco, la parola rossa "non quadra" solo sull'ordine che non quadra
 2. aperto l'ordine: in cima la riga rossa "Non quadra: 2 problemi"; il clic
    apre la tabella codice / ordinato / FerroTrack / Lantek / cosa fare
 3. "Taglio fatto" con l'ordine che non quadra: prima la finestra "Questi
    pezzi non arriverebbero al laser", il pulsante resta spento finche' non
    spunti "Ho controllato"; Annulla non segna niente; con la spunta il
    "controllato" va al server (storico) e solo dopo arriva la solita
    conferma "Taglio fatto?"
 4. "Manda a Lantek" con l'ordine che non quadra: la stessa finestra prima
    del riassunto per Lantek; Annulla non apre niente
 5. un ordine che quadra: riga grigia "Quadratura: tutto torna", "Taglio
    fatto" va dritto alla solita conferma
 6. l'API vera: /api/orders/<id>/quadratura?attendi=1 risponde con esito,
    testo e righe (Lantek letto SOLO in lettura)

La quadratura dei punti 1-5 la inventa il test (risposte ritoccate nel
browser); ogni scrittura e' bloccata nel browser (anche "Taglio fatto").
Serve il server di prova (default 127.0.0.1:5056).

    python app/tests/test_quadratura_pagina.py [url]
"""
import json
import os
import sys
import urllib.request

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print('Playwright non installato: test saltato.')
    sys.exit(0)

B = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5056'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.accesso_server import contesto_stazione, Sessione  # noqa: E402

esiti = []


def check(nome, cond, extra=''):
    esiti.append(bool(cond))
    print('  [%s] %s %s' % ('OK' if cond else 'KO', nome, '' if cond else extra))


try:
    with urllib.request.urlopen(B + '/api/health', timeout=4) as _r:
        _salute = json.load(_r)
except Exception as _e:
    print('Server non raggiungibile su %s: %s' % (B, str(_e)[:60]))
    print("Avvia un'istanza di prova e riesegui. Test saltato.")
    sys.exit(0)
if not _salute.get('istanza_di_prova'):
    print("RIFIUTO: %s non e' un'istanza di prova." % B)
    sys.exit(1)

stato = {'rosso': None, 'verde': None, 'controllati': [], 'bloccate': []}


def finta(oid, esito):
    righe = [{'codice': 'PROVA-P2-00', 'descrizione': 'piastra', 'livello': 1, 'assieme': 'PROVA-ASS-00', 'tipo': 'parte',
              'ordinato': 2, 'ferrotrack': None, 'lantek': None, 'lantek_fatti': None, 'lantek_stati': '', 'stato': 'non in FerroTrack',
              'problemi': [{'tipo': 'manca_ft', 'parola': 'Manca in FerroTrack', 'testo': "Ordinato x2: in FerroTrack non c'è.",
                            'cosa': "Aggiungilo all'ordine con «Aggiungi disegni»."}]},
             {'codice': 'PROVA-P1-00', 'descrizione': 'gamba', 'livello': 1, 'assieme': 'PROVA-ASS-00', 'tipo': 'parte',
              'ordinato': 6, 'ferrotrack': 6, 'lantek': 3, 'lantek_fatti': 0, 'lantek_stati': 'creato 3', 'stato': 'in Lantek',
              'problemi': [{'tipo': 'qta', 'parola': 'Quantità diversa', 'testo': 'In FerroTrack 6, in Lantek 3.',
                            'cosa': "Correggi l'ordine di produzione in Lantek a 6."}]}]
    if esito == 'QUADRA':
        return {'order_id': oid, 'numero': 'X', 'cliente': 'Y', 'esito': 'QUADRA', 'testo': 'Quadratura: tutto torna',
                'n_problemi': 0, 'non_verificabile': [], 'problemi_ordine': [], 'righe': [], 'problemi': [], 'note': [],
                'lantek_ok': True, 'calcolato_il': '2026-10-23T10:56:00'}
    return {'order_id': oid, 'numero': 'A 009999', 'cliente': 'Prova', 'esito': 'NON_QUADRA', 'testo': 'Non quadra: 2 problemi',
            'n_problemi': 2, 'non_verificabile': [], 'problemi_ordine': [], 'righe': righe, 'problemi': righe, 'note': [],
            'lantek_ok': True, 'calcolato_il': '2026-10-23T10:56:00'}


def filtro(route):
    req = route.request
    url = req.url.split('?')[0]
    rosso, verde = stato['rosso'], stato['verde']
    if req.method in ('GET', 'HEAD', 'OPTIONS'):
        if url.endswith('/api/quadratura') and rosso:
            d = {'success': True, 'in_corso': [], 'ordini': {
                rosso: {'numero': 'A', 'cliente': 'B', 'pronto': True, 'quadratura': finta(rosso, 'NON_QUADRA')},
                verde: {'numero': 'C', 'cliente': 'D', 'pronto': True, 'quadratura': finta(verde, 'QUADRA')}}}
            return route.fulfill(status=200, body=json.dumps(d), content_type='application/json')
        for oid, es in ((rosso, 'NON_QUADRA'), (verde, 'QUADRA')):
            if oid and url.endswith(f'/api/orders/{oid}/quadratura'):
                return route.fulfill(status=200, content_type='application/json', body=json.dumps(
                    {'success': True, 'pronto': True, 'in_corso': False, 'vecchio': False, 'quadratura': finta(oid, es)}))
        return route.continue_()
    if req.method == 'POST' and url.endswith('/quadratura/controllato'):
        stato['controllati'].append((url, req.post_data))
        return route.fulfill(status=200, body='{"success": true}', content_type='application/json')
    stato['bloccate'].append(req.method + ' ' + req.url)
    return route.abort()


with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = contesto_stazione(b.new_context(viewport={'width': 1440, 'height': 900}), B, 'laser')
    ctx.route('**/*', filtro)
    pg = ctx.new_page()
    err = []
    pg.on('pageerror', lambda e: err.append(str(e)))
    pg.goto(B + '/laser.html')
    pg.wait_for_selector('.lz-r[data-id]', timeout=30000)
    pg.wait_for_function('Object.keys(LZ.s.riep).length > 0', timeout=120000)
    ordini = pg.evaluate("""() => LZ.ordiniLaser().filter(o => LZ.statoLaser(o) === 'in_lantek')
        .map(o => ({id: o.id, numero: LZ.numero(o)}))""")
    if len(ordini) < 2:
        print('Servono due ordini "in Lantek" nella copia: test saltato.')
        sys.exit(0)
    rosso, verde = ordini[0]['id'], ordini[1]['id']
    stato.update(rosso=rosso, verde=verde)
    pg.evaluate('LZ.quad.aggiorna()')

    print('1) Elenco')
    pg.wait_for_selector(f'.lz-r[data-id="{rosso}"] .lz-quad-mini', timeout=15000)
    t = pg.locator(f'.lz-r[data-id="{rosso}"] .lz-quad-mini').inner_text()
    check('"non quadra" sull\'ordine che non quadra', t.strip() == 'non quadra', t)
    check('niente sull\'ordine che quadra', pg.locator(f'.lz-r[data-id="{verde}"] .lz-quad-mini').count() == 0)
    col = pg.evaluate(f"getComputedStyle(document.querySelector('.lz-r[data-id=\"{rosso}\"] .lz-quad-mini')).color")
    check('la parola e\' rossa', col.startswith('rgb(') and int(col[4:].split(',')[0]) > 150, col)

    print('\n2) Ordine aperto')
    pg.click(f'.lz-r[data-id="{rosso}"]')
    pg.wait_for_selector('#lz-o-passo .ftq.err', timeout=20000)
    t = pg.locator('#lz-o-passo .ftq.err .ftq-t').inner_text()
    check('in cima: "Non quadra: 2 problemi"', t.strip() == 'Non quadra: 2 problemi', t)
    pg.click('#lz-o-passo .ftq.err .ftq-t')
    pg.wait_for_selector('#lz-o-passo .ftq-tab', timeout=5000)
    pg.screenshot(path=os.path.join(os.environ.get('FT_FOTO_DIR') or os.environ.get('TEMP', '.'), 'ft_quadratura_riga.png'))
    tab = pg.locator('#lz-o-passo .ftq-tab').inner_text()
    check('la tabella: codici, ordinato, FerroTrack, Lantek, cosa fare',
          all(x in tab for x in ('PROVA-P2-00', 'PROVA-P1-00', 'Ordinato', 'FerroTrack', 'Lantek', 'Cosa fare',
                                 'Manca in FerroTrack', 'Correggi l\'ordine di produzione in Lantek a 6')), tab)
    pg.click('#lz-o-passo .ftq.err .ftq-t')
    pg.wait_for_function("!document.querySelector('#lz-o-passo .ftq-tab')", timeout=5000)
    check('secondo clic: si richiude', True)

    print('\n3) Taglio fatto con l\'ordine che non quadra')
    pg.wait_for_selector('#lz-o-passo [data-az="taglio"]', timeout=20000)
    pg.click('#lz-o-passo [data-az="taglio"]')
    pg.wait_for_selector('.ftq-gate .ftq-ord', timeout=20000)
    g = pg.locator('.ftq-gate')
    check('la finestra: "Questi pezzi non arriverebbero al laser"', 'Questi pezzi non arriverebbero al laser' in g.inner_text()
          and 'PROVA-P2-00' in g.inner_text(), g.inner_text()[:300])
    check('"Segna tagliato lo stesso" spento senza la spunta', g.locator('button[data-v="1"]').is_disabled())
    pg.keyboard.press('Enter')
    check('Invio non passa', pg.locator('.ftq-gate').count() == 1 and not stato['controllati'])
    g.locator('button[data-v="0"]').click()
    pg.wait_for_function("!document.querySelector('.ftq-gate')", timeout=5000)
    check('Annulla: niente segnato, niente nello storico', not stato['controllati']
          and not [x for x in stato['bloccate'] if 'mark-laser-done' in x] and pg.locator('.ft-modal-bg.open').count() == 0,
          (stato['controllati'], stato['bloccate']))
    pg.click('#lz-o-passo [data-az="taglio"]')
    pg.wait_for_selector('.ftq-gate .ftq-ord', timeout=20000)
    pg.screenshot(path=os.path.join(os.environ.get('FT_FOTO_DIR') or os.environ.get('TEMP', '.'), 'ft_quadratura_spunta.png'))
    pg.check('.ftq-gate input[data-ftq-ok]')
    check('con la spunta il pulsante si accende', not pg.locator('.ftq-gate button[data-v="1"]').is_disabled())
    pg.click('.ftq-gate button[data-v="1"]')
    pg.wait_for_function("!document.querySelector('.ftq-gate')", timeout=5000)
    pg.wait_for_selector('.ft-modal-bg.open:not(.ftq-gate)', timeout=5000)
    check('"Ho controllato" nello storico, coi codici', len(stato['controllati']) == 1
          and json.loads(stato['controllati'][0][1]) == {'azione': 'taglio', 'codici': ['PROVA-P2-00', 'PROVA-P1-00']},
          stato['controllati'])
    check('poi la solita conferma "Taglio fatto?"', 'Taglio fatto?' in pg.locator('.ft-modal-bg.open').inner_text())
    pg.click('.ft-modal-bg.open button[data-v="0"]')
    check('(niente segnato: conferma annullata)', not [x for x in stato['bloccate'] if 'mark-laser-done' in x], stato['bloccate'])

    print('\n4) Manda a Lantek con l\'ordine che non quadra')
    pg.evaluate(f'() => {{ LZ.lantek.apri(["{rosso}"]); }}')      # senza aspettare: la finestra aspetta noi
    pg.wait_for_selector('.ftq-gate .ftq-ord', timeout=20000)
    check('prima la stessa finestra, "Manda a Lantek lo stesso"',
          'Manda a Lantek lo stesso' in pg.locator('.ftq-gate button[data-v="1"]').inner_text())
    pg.keyboard.press('Escape')
    pg.wait_for_function("!document.querySelector('.ftq-gate')", timeout=5000)
    check('Esc: il riassunto per Lantek non si apre', pg.locator('#lz-lantek').count() == 0)

    print('\n5) Un ordine che quadra')
    pg.click(f'.lz-r[data-id="{verde}"]')
    pg.wait_for_function(f'LZ.s.aperto === "{verde}"', timeout=10000)
    pg.wait_for_selector('#lz-o-passo .ftq:not(.err)', timeout=20000)
    t = pg.locator('#lz-o-passo .ftq .ftq-t').inner_text()
    check('riga grigia "Quadratura: tutto torna"', t.strip() == 'Quadratura: tutto torna'
          and pg.locator('#lz-o-passo .ftq.err').count() == 0, t)
    pg.wait_for_selector('#lz-o-passo [data-az="taglio"]', timeout=20000)
    pg.click('#lz-o-passo [data-az="taglio"]')
    pg.wait_for_selector('.ft-modal-bg.open:not(.ftq-gate)', timeout=20000)
    check('"Taglio fatto" va dritto alla conferma', pg.locator('.ftq-gate').count() == 0
          and 'Taglio fatto?' in pg.locator('.ft-modal-bg.open').inner_text())
    pg.click('.ft-modal-bg.open button[data-v="0"]')
    pg.screenshot(path=os.path.join(os.environ.get('FT_FOTO_DIR') or os.environ.get('TEMP', '.'), 'ft_quadratura_pagina.png'))
    check('nessun errore nella pagina', not err, err)
    b.close()

print('\n6) L\'API vera')
s = Sessione(B, 'laser')
c, d = s.get('/api/quadratura')
check('/api/quadratura risponde', c == 200 and d.get('success') and isinstance(d.get('ordini'), dict), (c, str(d)[:200]))
if d.get('ordini'):
    oid = next(iter(d['ordini']))
    c, d = s.get(f'/api/orders/{oid}/quadratura?attendi=1')
    qd = (d or {}).get('quadratura') or {}
    check('/api/orders/<id>/quadratura?attendi=1: esito, testo, righe', c == 200 and qd.get('esito') in ('QUADRA', 'NON_QUADRA', 'NON_VERIFICABILE')
          and qd.get('testo') and isinstance(qd.get('righe'), list), (c, str(d)[:300]))
    print('     ', qd.get('numero'), '->', qd.get('testo'))

print('\n%d/%d ok' % (sum(esiti), len(esiti)))
sys.exit(0 if all(esiti) else 1)
