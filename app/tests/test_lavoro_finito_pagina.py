# -*- coding: utf-8 -*-
"""Il "Lavoro finito" veloce nella pagina dell'ufficio (16/10/2026).

 1. Ctrl+K, il numero, Tab, F: segnato in un passo, senza finestra di
    conferma; il messaggio dice "Lavoro finito: N → DDT da fare" e Annulla
    lo riporta in lavorazione.
 2. La nota "chi l'ha detto": Ctrl+Invio dalla ricerca, si scrive, Invio.
 3. Cosa resta (taglio non segnato): avviso nel messaggio, non blocco.
 4. Scelta multipla: "Segna finiti" su due ordini, poi Annulla.
 5. Un ordine gia' finito: la ricerca dice "Già finito" e non cambia niente.
 6. Il pulsante del pannello dell'ordine: subito, senza conferma.

Serve il server di prova (default 127.0.0.1:5056) su una COPIA del database.
Alla fine ogni ordine toccato torna com'era.
    python app/tests/test_lavoro_finito_pagina.py [url]
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


api = Sessione(B, 'ufficio')
TOCCATI = set()


PAG = {}   # col browser dentro si chiede da li': la stessa persona ha una sessione sola


def chiama(metodo, percorso, corpo=None):
    if PAG.get('u'):
        return tuple(PAG['u'].evaluate("""async ([m, url, corpo]) => {
            const r = await fetch(url, { method: m, headers: { 'Content-Type': 'application/json' },
                                         body: corpo == null ? undefined : JSON.stringify(corpo) });
            return [r.status, await r.json().catch(() => null)]; }""", [metodo, percorso, corpo]))
    return api.chiama(metodo, percorso, corpo)


def viste():
    c, d = chiama('GET', '/api/ordini/viste')
    assert c == 200, (c, d)
    return {o['id']: o for o in d['ordini']}


def num(o):
    return o.get('numero_ordine_cliente') or o.get('numero_ordine') or ''


def unici(ordini, fase, quanti):
    """Ordini della fase con un numero che non compare in altri ordini."""
    tutti = list(ordini.values())
    out = []
    for o in tutti:
        n = num(o).lower()
        if o['fase'] != fase or not n:
            continue
        if sum(1 for x in tutti if any(n in (str(x.get(k) or '')).lower() for k in
                                       ('cliente', 'numero_ordine', 'numero_ordine_cliente', 'ddt_numero', 'numero_fattura'))) == 1:
            out.append(o)
    return out[:quanti]


def cerca(pg, o):
    pg.keyboard.press('Control+k')
    pg.keyboard.type(num(o))
    pg.wait_for_selector(f'#cerca-res .cr-r[data-id="{o["id"]}"]', timeout=10000)


ordini = viste()
aperti = unici(ordini, 'aperto', 6)
gia = unici(ordini, 'pronto_ddt', 1)
if len(aperti) < 4:
    print('Servono almeno 4 ordini in lavorazione con numero unico nella copia: test saltato.')
    sys.exit(0)

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = contesto_stazione(b.new_context(viewport={'width': 1440, 'height': 900}, locale='it-IT'), B, 'ufficio')
    u = ctx.new_page()
    err = []
    u.on('pageerror', lambda e: err.append(str(e)))
    PAG['u'] = u
    try:
        u.goto(B + '/impiegata.html#ordini/aperto')
        u.wait_for_selector('#vista-tbody tr[data-id]', timeout=60000)
        u.wait_for_function('VISTE_CARICATE === true', timeout=30000)

        print('1) Ctrl+K, numero, Tab, F: segnato in un passo')
        o1 = aperti[0]
        cerca(u, o1)
        r = u.locator(f'#cerca-res .cr-r[data-id="{o1["id"]}"]')
        check('la riga dice lo stato e ha "Segna finito"', 'In lavorazione' in r.inner_text() and r.locator('.cr-az').count() == 1,
              r.inner_text())
        # la riga giusta scelta con le frecce
        idx = int(r.get_attribute('data-i'))
        for _ in range(idx):
            u.keyboard.press('ArrowDown')
        u.keyboard.press('Tab')
        check('Tab porta su "Segna finito"', u.evaluate('document.activeElement.classList.contains("cr-az")'))
        u.keyboard.press('f')
        TOCCATI.add(o1['id'])
        u.wait_for_selector('#fin-box', timeout=10000)
        testo = u.inner_text('#fin-box')
        check('messaggio "Lavoro finito: N → DDT da fare"', f'Lavoro finito: {num(o1)} → DDT da fare' in testo, testo)
        check('nessuna finestra di conferma', u.locator('.ft-modal-bg.open').count() == 0)
        check('sul server: DDT da fare', viste()[o1['id']]['passo'] == 'attesa_ddt')
        u.click('#fin-box .fin-annulla')
        u.wait_for_function(f'(VISTE_DATI.ordini.find(x => x.id === "{o1["id"]}") || {{}}).fase === "aperto"', timeout=15000)
        check('Annulla: torna in lavorazione', viste()[o1['id']]['fase'] == 'aperto')
        check('e lo dice', 'torna in lavorazione' in u.inner_text('.ft-toasts'), u.inner_text('.ft-toasts'))

        print('\n2) La nota "chi l\'ha detto"')
        o2 = aperti[1]
        cerca(u, o2)
        idx = int(u.get_attribute(f'#cerca-res .cr-r[data-id="{o2["id"]}"]', 'data-i'))
        for _ in range(idx):
            u.keyboard.press('ArrowDown')
        u.keyboard.press('Control+Enter')
        TOCCATI.add(o2['id'])
        u.wait_for_selector('#fin-nota', timeout=10000)
        u.wait_for_function('document.activeElement && document.activeElement.id === "fin-nota"', timeout=5000)
        check('dalla tastiera il cursore va sulla nota', True)
        u.keyboard.type('Marco, mancano 2 pezzi')
        u.keyboard.press('Enter')
        u.wait_for_function('!document.getElementById("fin-box")', timeout=10000)
        check('nota salvata sull\'ordine', viste()[o2['id']]['nota_completamento'] == 'Marco, mancano 2 pezzi',
              viste()[o2['id']].get('nota_completamento'))

        print('\n3) Cosa resta: avviso, non blocco')
        con_taglio = [o for o in aperti[2:] if o.get('taglio_richiesto') is True and not o.get('taglio_fatto')]
        if not con_taglio:
            print('  Nessun ordine con taglio da fare: parte 3 saltata.')
        else:
            o3 = con_taglio[0]
            cerca(u, o3)
            idx = int(u.get_attribute(f'#cerca-res .cr-r[data-id="{o3["id"]}"]', 'data-i'))
            for _ in range(idx):
                u.keyboard.press('ArrowDown')
            u.keyboard.press('Control+Enter')
            TOCCATI.add(o3['id'])
            u.wait_for_selector('#fin-box .fin-avviso', timeout=10000)
            check('avviso "il laser non ha segnato il taglio"', 'il laser non ha segnato il taglio' in u.inner_text('#fin-box .fin-avviso'))
            check('ma segnato lo stesso', viste()[o3['id']]['passo'] == 'attesa_ddt')
            u.keyboard.press('Escape')
            chiama('DELETE', f'/api/ordini/{o3["id"]}/completamento')

        print('\n4) Scelta multipla: "Segna finiti"')
        u.evaluate("switchTab('ordini', 'aperto')")
        u.wait_for_timeout(300)
        due = [o for o in aperti if o['id'] not in (o2['id'],)][:2]
        for o in due:
            u.check(f'#vista-tbody input[data-sel="{o["id"]}"]')
        check('la barra dice "2 scelti"', u.is_visible('#vista-sel') and '2 scelti' in u.inner_text('#vista-sel'), u.inner_text('#vista-sel'))
        u.click('#btn-segna-finiti')
        TOCCATI.update(o['id'] for o in due)
        u.wait_for_selector('#fin-box', timeout=10000)
        check('"2 lavori finiti → DDT da fare"', '2 lavori finiti → DDT da fare' in u.inner_text('#fin-box'), u.inner_text('#fin-box'))
        v = viste()
        check('tutti e due a DDT da fare', all(v[o['id']]['passo'] == 'attesa_ddt' for o in due))
        check('la scelta si svuota', u.locator('#vista-tbody input[data-sel]:checked').count() == 0)
        u.click('#fin-box .fin-annulla')
        u.wait_for_function('!document.getElementById("fin-box")', timeout=10000)
        u.wait_for_timeout(1500)
        v = viste()
        check('Annulla: tutti e due tornano in lavorazione', all(v[o['id']]['fase'] == 'aperto' for o in due))

        print('\n5) Un ordine gia\' finito')
        g = gia[0] if gia else dict(viste()[o2['id']])   # o2 e' finito dal passo 2
        cerca(u, g)
        rg = u.locator(f'#cerca-res .cr-r[data-id="{g["id"]}"]')
        check('la riga dice "Già finito", senza pulsante', 'Già finito' in rg.inner_text() and rg.locator('.cr-az').count() == 0, rg.inner_text())
        prima = viste()[g['id']]
        idx = int(rg.get_attribute('data-i'))
        for _ in range(idx):
            u.keyboard.press('ArrowDown')
        u.keyboard.press('Control+Enter')
        u.wait_for_timeout(600)
        check('il messaggio dice "già finito"', 'già finito' in u.inner_text('.ft-toasts'), u.inner_text('.ft-toasts'))
        dopo = viste()[g['id']]
        check('niente cambia', (dopo['passo'], dopo['completamento']) == (prima['passo'], prima['completamento']))
        c, d = chiama('POST', f'/api/ordini/{g["id"]}/completamento', {})
        check('anche il server risponde "gia fatto"', c == 200 and d.get('gia_fatto') is True, d)

        print('\n6) Il pannello dell\'ordine: subito, senza conferma')
        o6 = [o for o in aperti if o['id'] not in TOCCATI][0] if [o for o in aperti if o['id'] not in TOCCATI] else aperti[0]
        u.evaluate(f'apriOrdine("{o6["id"]}")')
        u.wait_for_selector('#ord-drawer .imp-next button:has-text("Segna finito")', timeout=15000)
        u.click('#ord-drawer .imp-next button:has-text("Segna finito")')
        TOCCATI.add(o6['id'])
        u.wait_for_selector('#fin-box', timeout=10000)
        check('niente finestra di conferma', u.locator('.ft-modal-bg.open').count() == 0)
        u.wait_for_selector('#ord-drawer .imp-next button:has-text("Aggiungi nota")', timeout=15000)
        check('il pannello passa a "Pronto: prepara il DDT"', 'Pronto: prepara il DDT' in u.inner_text('#ord-drawer .imp-next'))
        u.click('#ord-drawer .imp-next button:has-text("Aggiungi nota")')
        u.fill('#dr-nota', 'Detto da Luca')
        u.keyboard.press('Enter')
        u.wait_for_selector('#ord-drawer .imp-next .imp-nota-fin', timeout=10000)
        check('la nota si vede nel pannello', 'Detto da Luca' in u.inner_text('#ord-drawer .imp-next'))

        print('\n7) Forse finiti (dopo il taglio laser): la stessa azione')
        chiudi = u.locator('#ord-drawer .ft-icon-btn[aria-label="Chiudi"]')
        if chiudi.count():
            chiudi.first.click()
        u.evaluate("switchTab('oggi')")
        u.evaluate('caricaOggi(true)')
        u.wait_for_timeout(1500)
        ff = u.evaluate('FORSE_FINITI.map(f => f.id)')
        riga = u.locator('#og-s-forse ~ .og-r:has(button:has-text("Non ancora"))').first
        if not ff or not riga.count():
            print('  Nessun "forse finito" nella copia: parte 7 saltata.')
        else:
            rid = u.evaluate('el => (el.getAttribute("onclick").match(/apriOrdine\\(\'([^\']+)\'/) || [])[1]', riga.element_handle())
            riga.locator('button:has-text("Segna finito")').click()
            TOCCATI.add(rid)
            u.wait_for_selector('#fin-box', timeout=10000)
            check('dai forse finiti: lo stesso messaggio con Annulla', 'Lavoro finito:' in u.inner_text('#fin-box')
                  and u.locator('#fin-box .fin-annulla').count() == 1 and u.locator('.ft-modal-bg.open').count() == 0)
            check('e sul server DDT da fare', viste()[rid]['passo'] == 'attesa_ddt')
            u.click('#fin-box .fin-annulla')
            u.wait_for_timeout(1500)
            check('Annulla: torna fra i forse finiti', viste()[rid]['fase'] == 'aperto')
        check('nessun errore nella pagina', not err, err)
    finally:
        for i in TOCCATI:
            chiama('DELETE', f'/api/ordini/{i}/completamento')
        v = viste()
        check("alla fine tutto com'era", all(v[i]['fase'] == 'aperto' for i in TOCCATI if i in v),
              [(i, v[i]['fase']) for i in TOCCATI if i in v and v[i]['fase'] != 'aperto'])
        b.close()

print('\n%d/%d controlli passati' % (sum(esiti), len(esiti)))
sys.exit(0 if all(esiti) else 1)
