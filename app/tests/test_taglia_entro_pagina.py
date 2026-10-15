# -*- coding: utf-8 -*-
"""Taglia entro nella pagina laser (laser.html), nel browser, sul server di prova.

Si prova che:
 1. l'elenco e' in fila per "taglia entro" (poi consegna), come dice il server;
    ogni riga mostra il taglia entro grande e "cons. GG/MM" piccolo; in rosso
    "in ritardo N gg" solo se il taglia entro e' passato
 2. l'ordine aperto ha "Taglia entro" con il motivo; si cambia a mano con la
    data (salvata davvero, sulla COPIA del database) e "Torna al calcolo" lo
    rimette com'era; l'elenco si rimette in fila
 3. niente si apre o si sposta da solo dopo il cambio (aggiornamento automatico)
 4. la saturazione (carico-laser.js, la stessa dell'ufficio) mette l'ordine
    nel giorno del taglia entro
 5. la spunta "Lavorazione esterna" (con cosa): +5 giorni subito, il motivo
    lo dice; tolta la spunta (= no) niente +5; "Non so" torna alla deduzione

Le sole scritture lasciate passare sono /pianifica-taglio e /lavorazione-esterna
(copia del database),
rimesse com'erano alla fine; ogni altra scrittura e' bloccata nel browser.
Serve il server di prova (default 127.0.0.1:5056).

    python app/tests/test_taglia_entro_pagina.py [url]
"""
import json
import os
import sys
import urllib.request
from datetime import date, timedelta

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print('Playwright non installato: test saltato.')
    sys.exit(0)

B = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5056'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.accesso_server import contesto_stazione  # noqa: E402

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

scritture = {'pianifica': [], 'esterna': [], 'bloccate': []}


def filtro(route):
    """Solo /pianifica-taglio passa; le altre scritture si fermano qui."""
    req = route.request
    if req.method in ('GET', 'HEAD', 'OPTIONS'):
        return route.continue_()
    if req.url.split('?')[0].endswith('/pianifica-taglio'):
        scritture['pianifica'].append((req.url, req.post_data))
        return route.continue_()
    if req.url.split('?')[0].endswith('/lavorazione-esterna'):
        scritture['esterna'].append((req.url, req.post_data))
        return route.continue_()
    scritture['bloccate'].append(req.method + ' ' + req.url)
    return route.abort()


ORDINE_JS = """() => [...document.querySelectorAll('.lz-r[data-id]')].map(r => r.dataset.id)"""

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = contesto_stazione(b.new_context(viewport={'width': 1440, 'height': 900}), B, 'laser')
    ctx.route('**/*', filtro)
    pg = ctx.new_page()
    err = []
    pg.on('pageerror', lambda e: err.append(str(e)))
    pg.goto(B + '/laser.html')
    pg.wait_for_selector('.lz-r[data-id]', timeout=30000)
    pg.wait_for_function('Object.keys(LZ.s.riep).length > 0 && LZ.sat.pronto', timeout=120000)
    pg.wait_for_timeout(500)

    print('1) Elenco in fila per "taglia entro"')
    righe = pg.evaluate("""() => LZ.ordiniLaser().filter(o => LZ.statoLaser(o) !== 'tagliato').map(o => ({
        id: o.id, entro: LZ.entro(o), cons: LZ.giorno(o), man: o.data_taglio_pianificata || null,
        server: (LZ.te(o) || {}).data || null, fonte: (LZ.te(o) || {}).fonte || null}))""")
    check('ci sono ordini da tagliare', len(righe) > 0)
    chiavi = [(r['entro'] or '9999', r['cons'] or '9999') for r in righe]
    check('in fila per taglia entro, poi consegna', chiavi == sorted(chiavi), chiavi[:8])
    # dal 15/10 l'elenco e' in gruppi (Da smistare, Da mandare, In Lantek):
    # dentro ogni gruppo resta la fila per taglia entro
    dom = pg.evaluate(ORDINE_JS)
    visti = pg.evaluate("() => LZ.ordiniVisti().filter(o => LZ.statoLaser(o) !== 'tagliato').map(o => o.id)")
    check("l'elenco a schermo e' in quell'ordine (per gruppi)", dom[:len(visti)] == visti)
    for g in ('smistare', 'coda', 'in_lantek'):
        ch = pg.evaluate(f"""() => LZ.ordiniVisti().filter(o => LZ.statoLaser(o) === '{g}').map(o => [LZ.entro(o) || '9999', LZ.giorno(o) || '9999'])""")
        check(f'gruppo {g}: in fila per taglia entro', ch == sorted(ch), ch[:5])
    coerenti = [r for r in righe if r['server'] and not r['man']]
    check('il taglia entro e\' quello del server', all(r['entro'] == r['server'] for r in coerenti),
          [r for r in coerenti if r['entro'] != r['server']][:3])
    check('il taglia entro non e\' mai dopo la consegna (se calcolato)',
          all(not r['cons'] or r['entro'] <= r['cons'] for r in righe if not r['man']))
    primo = righe[0]
    t = pg.locator(f'.lz-r[data-id="{primo["id"]}"] .lz-q').inner_text()
    gm = lambda iso: '%s/%s' % (iso[8:10], iso[5:7])  # noqa: E731
    check('la riga: "entro" col taglia entro a destra', 'entro ' + gm(primo['entro']) in t, t)
    oggi = date.today().isoformat()
    rit = [r for r in righe if r['entro'] and r['entro'] < oggi]
    if rit:
        tr = pg.locator(f'.lz-r[data-id="{rit[0]["id"]}"] .lz-q.indietro small')
        check('taglia entro passato: "passato da N gg" in arancione', tr.count() == 1 and 'passato da' in tr.inner_text())
    nonrit = [r for r in righe if r['entro'] and r['entro'] >= oggi]
    if nonrit:
        check('non in ritardo: niente arancione', pg.locator(f'.lz-r[data-id="{nonrit[0]["id"]}"] .lz-q.indietro').count() == 0)
    testa = pg.locator('.lz-lista-testa').inner_text()
    # "Taglio in ritardo" (taglia entro passato) e' detto cosi', non "in ritardo":
    # "In ritardo" e' la consegna, come all'ufficio (backend/carico_laser.py)
    check('il conto "Taglio in ritardo" in testa segue il taglia entro',
          (f'Taglio in ritardo: {len(rit)}' in testa) if rit else 'Taglio in ritardo' not in testa, testa)

    print('\n2) Taglia entro nell\'ordine aperto, a mano e "Torna al calcolo"')
    sc = [r for r in righe if not r['man']]
    if not sc:
        print('Tutti gli ordini hanno gia\' una data a mano: parte 2 saltata.')
    else:
        oid = sc[len(sc) // 2]['id']
        pg.click(f'.lz-r[data-id="{oid}"]')
        pg.wait_for_selector('.lz-entro', timeout=20000)
        fatto = pg.locator('.lz-entro').inner_text()
        te = pg.evaluate(f'LZ.te(LZ.trova("{oid}"))') or {}
        check('"Taglia entro" con data e motivo', 'Taglia entro' in fatto and gm(sc[len(sc) // 2]['entro']) in fatto
              and (te.get('motivo') or '---') in fatto, (fatto, te))
        check('niente "Torna al calcolo" se e\' calcolato', pg.locator('[data-az="entro-calcolo"]').count() == 0)
        nuova = (date.today() + timedelta(days=45)).isoformat()
        y0 = pg.evaluate("document.querySelector('.lz-righe').scrollTop")
        pg.locator(f'input[data-entro="{oid}"]').fill(nuova)
        pg.wait_for_function(f'(LZ.te(LZ.trova("{oid}")) || {{}}).fonte === "a mano"', timeout=30000)
        pg.wait_for_timeout(400)
        check('salvata con /pianifica-taglio', any(nuova in (d or '') for _u, d in scritture['pianifica']))
        te = pg.evaluate(f'LZ.te(LZ.trova("{oid}"))')
        check('il server: a mano, con la data scelta', te['data'] == nuova and te['fonte'] == 'a mano', te)
        fatto = pg.locator('.lz-entro').inner_text()
        check('"(a mano)" e "Torna al calcolo"', '(a mano)' in fatto and pg.locator('[data-az="entro-calcolo"]').count() == 1, fatto)
        check('la riga dice la data nuova', gm(nuova) in pg.locator(f'.lz-r[data-id="{oid}"] .lz-q').inner_text())
        ids = pg.evaluate("""() => LZ.ordiniLaser().filter(o => LZ.statoLaser(o) !== 'tagliato').map(o => [LZ.entro(o) || '9999', LZ.giorno(o) || '9999'])""")
        check("l'elenco si rimette in fila", ids == sorted(ids))
        check("l'ordine resta aperto", pg.evaluate('LZ.s.aperto') == oid)
        # la saturazione: il giorno dell'ordine e' il taglia entro (ufficio e laser)
        g = pg.evaluate(f'CaricoLaser.giorno(LZ.trova("{oid}"))')
        check('saturazione: l\'ordine pesa sul giorno scelto', g == nuova, g)

        print('\n3) Niente si muove da solo')
        pg.wait_for_timeout(6500)                      # un giro dell'aggiornamento automatico
        check('dopo un giro: stesso ordine aperto', pg.evaluate('LZ.s.aperto') == oid)
        check('dopo un giro: nessun errore nella pagina', not err, err[:3])

        pg.click('[data-az="entro-calcolo"]')
        pg.wait_for_function(f'(LZ.te(LZ.trova("{oid}")) || {{}}).fonte === "calcolata" && !LZ.trova("{oid}").data_taglio_pianificata',
                             timeout=30000)
        pg.wait_for_timeout(400)
        te = pg.evaluate(f'LZ.te(LZ.trova("{oid}"))')
        check('"Torna al calcolo": di nuovo calcolata', te['fonte'] == 'calcolata' and te['data'] == te['calcolata'], te)
        check('e la data di prima', te['data'] == sc[len(sc) // 2]['entro'], (te, sc[len(sc) // 2]))
        check('"Torna al calcolo" sparito', pg.locator('[data-az="entro-calcolo"]').count() == 0)
        g = pg.evaluate(f'CaricoLaser.giorno(LZ.trova("{oid}"))')
        # da tagliare col taglia entro gia' passato: pesa su oggi
        check('saturazione: di nuovo sul giorno calcolato (oggi, se passato)', g == max(te['data'], date.today().isoformat()), g)

    print('\n4) Saturazione = taglia entro (stesso conto dell\'ufficio)')
    diversi = pg.evaluate("""() => LZ.ordiniLaser().filter(o => LZ.statoLaser(o) !== 'tagliato')
        .filter(o => CaricoLaser.giorno(o) !== (LZ.entro(o) && LZ.entro(o) < FT.oggiISO() ? FT.oggiISO() : LZ.entro(o)))
        .map(o => [o.id, CaricoLaser.giorno(o), LZ.entro(o)])""")
    check('ogni ordine da tagliare pesa sul suo taglia entro (oggi, se gia passato)', not diversi, diversi[:3])
    print('\n5) Spunta "Lavorazione esterna"')
    cand = pg.evaluate('''() => LZ.ordiniLaser().filter(o => ['coda', 'smistare'].includes(LZ.statoLaser(o))
        && o.lavorazione_esterna == null && !o.data_taglio_pianificata && o.data_consegna
        && LZ.te(o) && !(LZ.te(o).voci || []).includes('esterna')).map(o => o.id)''')
    if not cand:
        print('Nessun ordine adatto (da tagliare, senza esterna): parte 5 saltata.')
    else:
        oid = cand[0]
        pg.click(f'.lz-r[data-id="{oid}"]')
        pg.wait_for_selector(f'input[data-esterna="{oid}"]', timeout=20000)
        g0 = pg.evaluate(f'LZ.te(LZ.trova("{oid}")).giorni')
        check("la spunta c'e', vicino al taglia entro, non spuntata",
              pg.locator(f'.lz-entro input[data-esterna="{oid}"]').count() == 1
              and not pg.locator(f'input[data-esterna="{oid}"]').is_checked())
        pg.locator(f'input[data-esterna="{oid}"]').check()
        pg.wait_for_function(f'LZ.trova("{oid}").lavorazione_esterna === true && (LZ.te(LZ.trova("{oid}")) || {{}}).giorni === {g0} + 5',
                             timeout=30000)
        check('spuntata: +5 giorni sul taglia entro', True)
        nota = pg.locator(f'input[data-esterna-nota="{oid}"]')
        check('compare il campo "quale?"', nota.count() == 1)
        nota.fill('zincatura')
        nota.press('Enter')
        pg.wait_for_function(f'LZ.trova("{oid}").lavorazione_esterna_nota === "zincatura"', timeout=30000)
        pg.wait_for_timeout(300)
        mot = pg.locator('.lz-entro small.mot').inner_text()
        check('il motivo dice la lavorazione esterna (zincatura)', 'zincatura' in mot and 'dedotto' not in mot, mot)
        check("l'ordine resta aperto", pg.evaluate('LZ.s.aperto') == oid)
        g = pg.evaluate(f'CaricoLaser.giorno(LZ.trova("{oid}"))')
        e = pg.evaluate(f'LZ.entro(LZ.trova("{oid}"))')
        check('saturazione: sul taglia entro nuovo (oggi, se passato)', g == max(e, date.today().isoformat()), (g, e))
        pg.locator(f'input[data-esterna="{oid}"]').uncheck()
        pg.wait_for_function(f'LZ.trova("{oid}").lavorazione_esterna === false && (LZ.te(LZ.trova("{oid}")) || {{}}).giorni === {g0}',
                             timeout=30000)
        check('tolta la spunta (= no): niente +5', True)
        pg.click('[data-az="esterna-togli"]')
        pg.wait_for_function(f'LZ.trova("{oid}").lavorazione_esterna === null', timeout=30000)
        check('"Non so": torna non detta (come prima)', pg.evaluate(f'LZ.te(LZ.trova("{oid}")).giorni') == g0)
        check('salvate con /lavorazione-esterna', len(scritture['esterna']) == 4, scritture['esterna'])

    check('nessun errore nella pagina', not err, err[:3])
    print('  scritture bloccate:', len(scritture['bloccate']), scritture['bloccate'][:3])
    b.close()

print('\n%d ok, %d ko' % (sum(esiti), len(esiti) - sum(esiti)))
sys.exit(0 if all(esiti) else 1)
