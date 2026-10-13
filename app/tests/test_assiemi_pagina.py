"""Pagina laser: assiemi, pezzi per assieme, "Da procurare" e avviso Lantek
sul caso vero DECA A 001221 BIS (Playwright, server di prova).

Il giro di Stefano sulla COPIA del database:
  1. apre l'ordine: l'assieme 26T30SA0233-00 "Struttura x MTR" e' una riga
     di testata coi suoi pezzi sotto (anche quelli non da laser);
  2. scrive x3 nella testata (era x1): i pezzi passano a 3;
  3. scrive 2 pezzi per assieme per 191700045-00: 6;
  4. avviso per le 3 lamiere gia' mandate a Lantek con 1: "In Lantek:
     191700045-00 ha 1, ora servono 6";
  5. tasto N su 26CCSA0038-00 (disegnato 581x50): T senza profilo non si
     salva; col profilo scritto si', 1 per assieme x 3 = 3;
  6. "Da procurare": la voce col profilo e 3 pezzi; un'altra voce si
     corregge con la matita (profilo, lunghezza, materiale).

SOLO su un'istanza di prova (rifiuta la produzione); i programmi di Lantek
nel server di prova sono bloccati (avvia_assiemi.py). Porta di default 5057.
    python app/tests/test_assiemi_pagina.py [http://127.0.0.1:5057]
"""
import json
import os
import re
import sys
import urllib.request

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print('Playwright non installato: test saltato.')
    sys.exit(0)

B = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5057'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.accesso_server import contesto_stazione  # noqa: E402

OID = '2b88c2e8-55b7-4c95-8de6-c3286644763b'
ASS = '26T30SA0233-00'
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

FOTO = os.environ.get('FT_FOTO_DIR')


def foto(pg, nome):
    if FOTO:
        pg.screenshot(path=os.path.join(FOTO, nome + '.png'), full_page=False)


with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = contesto_stazione(b.new_context(viewport={'width': 1440, 'height': 900}), B, 'laser')
    pg = ctx.new_page()
    err = []
    pg.on('pageerror', lambda e: err.append(str(e)))
    pg.goto(B + '/laser.html')
    pg.wait_for_selector('.lz-r[data-id]', timeout=60000)
    if not pg.evaluate(f"!!LZ.trova('{OID}')"):
        print('Ordine A 001221 BIS non nella copia: test saltato.')
        b.close()
        sys.exit(0)
    # si riparte come all'arrivo dell'ordine (assieme x1, 1 per assieme):
    # la prova si puo' rifare sulla stessa copia
    pg.request.post(f'{B}/api/orders/{OID}/assiemi/{ASS}/quantita', data={'quantita': 1})
    dati0 = pg.request.get(f'{B}/api/orders/{OID}/laser').json()
    for x in dati0.get('pezzi') or []:
        if x.get('assieme') == ASS and x.get('articolo_id') and (x.get('qta_per') != 1 or x.get('qta_da_confermare')):
            pg.request.post(f"{B}/api/orders/{OID}/pezzi/{x['articolo_id']}/per-assieme", data={'pezzi': 1})
    pg.evaluate(f"LZ.lista.apri('{OID}')")
    pg.wait_for_function(f"LZ.s.dett['{OID}'] && LZ.s.dett['{OID}'].dati", timeout=120000)
    pg.wait_for_selector('#lz-dett tr.lz-ass', timeout=30000)

    print("1) L'assieme coi suoi pezzi")
    testa = pg.inner_text('#lz-dett tr.lz-ass')
    check('testata: Assieme 26T30SA0233-00 · Struttura x MTR', ASS in testa and 'Struttura x MTR' in testa, testa)
    comp = pg.eval_on_selector_all('#lz-dett tr.comp td.cod', 'els => els.map(e => e.textContent.trim())')
    check('sotto: le 3 lamiere e i 5 tubi', all(any(c.startswith(x) for c in comp) for x in
                                              ('191700045-00', '191700114-00', '26CCSA0038-00', '26CCSA0042-00')), comp)
    riga38 = pg.inner_text('#lz-dett tr.comp:has(td.cod:text-is("26CCSA0038-00"))') if pg.locator(
        '#lz-dett tr.comp:has(td.cod:text-is("26CCSA0038-00"))').count() else ''
    check('il tubo non mostra lo spessore della lamiera ("INOX · 3 mm"): è "da procurare"',
          'da procurare' in riga38 and 'INOX ·' not in riga38, riga38)
    pg.locator('#lz-dett tr.lz-ass').scroll_into_view_if_needed()
    foto(pg, 'assiemi_1_testata')
    pg.click('#lz-dett [data-ass-apri]')
    check('si chiude', pg.locator('#lz-dett tr.comp').count() == 0 or not pg.locator(
        '#lz-dett tr.comp td.cod:text-is("191700045-00")').count())
    pg.click('#lz-dett [data-ass-apri]')
    pg.wait_for_selector('#lz-dett tr.comp td.cod:text-is("191700045-00")')

    print('2) Assieme x3')
    q = pg.locator(f'#lz-dett input[data-assq="{ASS}"]')
    q.fill('3')
    q.press('Enter')
    pg.wait_for_function(f"(LZ.s.dett['{OID}'].dati.assiemi || [{{}}])[0].quantita === 3 && !LZ.staScrivendo(document.getElementById('lz-dett'))", timeout=60000)
    pg.wait_for_function(f"document.querySelector('#lz-dett input[data-assq]').value === '3'", timeout=30000)
    pz = pg.evaluate(f"Object.fromEntries(LZ.s.dett['{OID}'].dati.pezzi.filter(p => p.assieme).map(p => [p.codice, p.quantita]))")
    check('i pezzi dell\'assieme passano a 3', pz.get('191700112-00') == 3 and pz.get('26CCSA0038-00') == 3, pz)

    print('3) 2 pezzi per assieme per 191700045-00')
    aid45 = pg.evaluate(f"LZ.s.dett['{OID}'].dati.pezzi.find(p => p.codice === '191700045-00').articolo_id")
    i = pg.locator(f'#lz-dett input[data-pza="{aid45}"]')
    i.fill('2')
    i.press('Enter')
    pg.wait_for_function(f"LZ.s.dett['{OID}'].dati.pezzi.find(p => p.codice === '191700045-00').quantita === 6", timeout=60000)
    pg.wait_for_function("!LZ.staScrivendo(document.getElementById('lz-dett'))")
    cella = pg.inner_text(f'#lz-dett tr.comp:has(input[data-pza="{aid45}"]) td.q')
    check('191700045-00: 2 per assieme x 3 = 6', cella.strip().startswith('6')
          and pg.input_value(f'#lz-dett input[data-pza="{aid45}"]') == '2', cella)

    print("4) Avviso: in Lantek c'e' il numero vecchio")
    pg.wait_for_selector('#lz-dett .lz-correggi', timeout=60000)
    av = pg.inner_text('#lz-dett .lz-correggi')
    check('"In Lantek: 191700045-00 ha 1, ora servono 6"', 'In Lantek: 191700045-00 ha 1, ora servono 6' in av, av)
    check('anche 191700112-00 e 191700114-00 (1 -> 3)', '191700112-00 ha 1, ora servono 3' in av
          and '191700114-00 ha 1, ora servono 3' in av, av)
    check('bottone "Corretto in Lantek"', pg.locator('#lz-dett [data-az="qta-corretta"]').count() == 3)
    pg.locator('#lz-dett .lz-correggi').scroll_into_view_if_needed()
    foto(pg, 'assiemi_2_avviso')
    pg.locator('#lz-dett tr.lz-ass').scroll_into_view_if_needed()
    foto(pg, 'assiemi_2b_assieme_x3')

    print('5) Tasto N sul tubo: quale tubo')
    pg.evaluate("document.activeElement && document.activeElement.blur()")
    aid38 = pg.evaluate(f"LZ.s.dett['{OID}'].dati.pezzi.find(p => p.codice === '26CCSA0038-00').articolo_id")
    pg.evaluate(f"LZ.controllo.vedi('{OID}', '{aid38}', 'procura')")
    pg.wait_for_selector('#lz-ctl .lz-pr-campi', timeout=60000)
    pg.wait_for_function("!document.querySelector('#lz-ctl .lz-pr-tasti .lz-btn[disabled]')", timeout=60000)
    lato = pg.inner_text('#lz-ctl-lato')
    check('pezzi per assieme e assiemi nel modulo', 'Pezzi per assieme' in lato and '3 assiemi' in lato, lato[:400])
    pg.evaluate("document.activeElement && document.activeElement.blur()")
    pg.fill('#lz-ctl [data-pr="profilo"]', '')
    pg.evaluate("document.activeElement && document.activeElement.blur()")
    n0 = pg.evaluate(f"(LZ.s.dett['{OID}'].dati.procura_voci || []).find(v => v.codice === '26CCSA0038-00').profilo")
    pg.keyboard.press('t')
    pg.wait_for_timeout(400)
    check('T senza profilo non si salva: resta nel modulo, sul profilo',
          pg.locator('#lz-ctl .lz-pr-campi').count() == 1
          and pg.evaluate("document.activeElement && document.activeElement.dataset.pr") == 'profilo', n0)
    foto(pg, 'assiemi_3_tasto_n')
    pg.fill('#lz-ctl [data-pr="profilo"]', 'Tubo quadro 50x50 sp.3mm')
    pg.fill('#lz-ctl [data-pr="lunghezza"]', '581')
    pg.fill('#lz-ctl [data-pr="per"]', '1')
    pg.fill('#lz-ctl [data-pr="materiale"]', 'INOX 304')
    pg.locator('#lz-ctl [data-pr="profilo"]').press('Enter')
    pg.wait_for_selector('#lz-ctl', state='detached', timeout=60000)
    pg.wait_for_function(f"""(() => {{ const v = (LZ.s.dett['{OID}'].dati.procura_voci || []).find(v => v.codice === '26CCSA0038-00');
        return v && v.profilo === 'Tubo quadro 50x50 sp.3mm'; }})()""", timeout=60000)
    v = pg.evaluate(f"(LZ.s.dett['{OID}'].dati.procura_voci || []).find(v => v.codice === '26CCSA0038-00')")
    check('voce: tubo, 50x50, L 581, 1 x 3 = 3, INOX 304', v['tipo'] == 'tubo' and v['lunghezza_mm'] == 581
          and v['quantita'] == 3 and v['per_assieme'] == 1 and v['qta_assieme'] == 3 and v['materiale'] == 'INOX 304', v)

    print('6) Da procurare: quantita\' giuste, voce corretta con la matita')
    pg.evaluate(f"LZ.procura.apri({{ ordine: '{OID}', stato: 'tutti' }})")
    pg.wait_for_selector('#lz-pc .lz-pc-tab', timeout=60000)
    testo = pg.inner_text('#lz-pc-corpo')
    check('il tubo nelle barre: Tubo quadro 50x50 sp.3mm, 3 pezzi', 'Tubo quadro 50x50 sp.3mm' in testo, testo[:600])
    check('laser tubo senza profilo: "da completare"', 'da completare' in testo, testo[:600])
    v39 = pg.evaluate("""fetch('/api/procura?stato=tutti&ordine=%s').then(r => r.json()).then(d => d.voci.find(v => v.codice === '26CCSA0039-00'))""" % OID)
    pg.click(f'#lz-pc [data-pc-mod="{v39["id"]}"]')
    pg.wait_for_selector('#lz-pc .lz-pc-mod')
    foto(pg, 'assiemi_4_correggi')
    pg.fill('#lz-pc [data-pcm="profilo"]', 'Tubo rett. 50x30 sp.2mm')
    pg.fill('#lz-pc [data-pcm="lunghezza_mm"]', '895')
    pg.fill('#lz-pc [data-pcm="materiale"]', 'INOX 316')
    pg.locator('#lz-pc [data-pcm="lunghezza_mm"]').press('Enter')
    pg.wait_for_selector('#lz-pc .lz-pc-mod', state='detached', timeout=60000)
    v39 = pg.evaluate("""fetch('/api/procura?stato=tutti&ordine=%s').then(r => r.json()).then(d => d.voci.find(v => v.codice === '26CCSA0039-00'))""" % OID)
    check('26CCSA0039-00 corretta: profilo, 895 mm, INOX 316, 3 pezzi (1 x 3)', v39['profilo'] == 'Tubo rett. 50x30 sp.2mm'
          and v39['lunghezza_mm'] == 895 and v39['materiale'] == 'INOX 316' and v39['quantita'] == 3 and not v39['da_completare'], v39)
    righe = pg.inner_text('#lz-pc-corpo')
    check('quantita\' per assieme mostrata ("1 × 3 ass.")', re.search(r'1\s*×\s*3 ass\.', righe) is not None, righe[:500])
    foto(pg, 'assiemi_5_da_procurare')
    pg.keyboard.press('Escape')
    check('Esc chiude il pannello', pg.locator('#lz-pc').count() == 0)
    check('nessun errore nella pagina', not err, err)
    b.close()

print('\n%d OK, %d KO' % (sum(esiti), len(esiti) - sum(esiti)))
sys.exit(0 if all(esiti) else 1)
