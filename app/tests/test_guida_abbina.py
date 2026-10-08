# -*- coding: utf-8 -*-
"""Laser, ordine arrivato SOLO in PDF: righe da abbinare ai pezzi di Lantek.

Stefano (07/10/2026): le righe del PDF hanno lo stesso codice dei pezzi in
Lantek; quelle che non ci sono sono assiemi, da abbinare UNA volta ai loro
pezzi (o "non va al laser"). Si prova che:
 1. con righe da abbinare "Avanti" e' BLOCCATO e si vede perche'
 2. "E' un assieme" propone i pezzi dell'ordine in Lantek senza riga, con i
    pezzi per assieme (quantita' in Lantek / quantita' della riga)
 3. "Salva" manda esattamente pezzi e quantita' scelti
 4. "Non va al laser" manda non_laser
 5. abbinato tutto: Avanti si sblocca e gli abbinati si possono rifare

Lantek e' SIMULATO (risposte finte di lantek-quantita, lantek-abbina,
lantek/pezzi): niente arriva a Lantek ne' al database.

    python app/tests/test_guida_abbina.py [url]
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

esiti = []


def check(nome, cond, extra=''):
    esiti.append(bool(cond))
    print('  [%s] %s %s' % ('OK' if cond else 'KO', nome, extra if not cond else ''))


try:
    with urllib.request.urlopen(B + '/api/health', timeout=4) as _r:
        _salute = json.load(_r)
except Exception as _e:
    print('Server non raggiungibile su %s: %s' % (B, str(_e)[:60]))
    print("Avvia un'istanza di prova e riesegui. Test saltato.")
    sys.exit(0)
if not _salute.get('istanza_di_prova'):
    print('RIFIUTO: %s non e\' un\'istanza di prova.' % B)
    sys.exit(1)


def riga(codice, stato='in_lantek', quantita=1, in_produzione=0):
    return {'codice': codice, 'codice_ft': codice, 'quantita': quantita, 'materiale': 'INOX', 'spessore': 2.0,
            'stato': stato, 'avvisi': [] if stato == 'in_lantek' else ['non ancora in Lantek: importa prima il disegno'],
            'in_produzione': in_produzione, 'gia_fatti': 0, 'gia_fatti_il': None}


def dati(righe, pdf):
    da_mandare = [{'codice': r['codice'], 'quantita': r['quantita'], 'gia_fatti': 0, 'gia_fatti_il': None}
                  for r in righe if r['stato'] == 'in_lantek' and not r['in_produzione']]
    return {'success': True, 'righe': righe, 'lantek': {'disponibile': True, 'errore': None},
            'commessa': '9999', 'consegna': '2030-11-06', 'cliente_lantek': 'PROVA', 'nome_file': 'x.xlsx',
            'nome_file_xml': 'x.xml', 'n_codici': len(righe), 'n_pezzi': sum(r['quantita'] for r in righe),
            'n_nuovi': 0, 'n_in_produzione': 0, 'n_gia_fatti': 0, 'n_xml': len(da_mandare),
            'da_mandare': da_mandare, 'nuovi_da_creare': [], 'nuovi_esclusi': [],
            'invio_automatico': True, 'n_avvisi': 0, 'pdf': pdf}


SCENARI = {
    'da_abbinare': dati(
        [riga('T-A1-00'), riga('T-CVE', 'nuovo', 2), riga('T-ASTA', 'nuovo')],
        {'n_righe': 3, 'assiemi': [], 'non_laser': [],
         'da_abbinare': [{'codice': 'T-CVE', 'descrizione': 'CVE saldata', 'quantita': 2, 'proposta': []},
                         {'codice': 'T-ASTA', 'descrizione': 'Asta tonda', 'quantita': 1, 'proposta': []}],
         'liberi': [{'codice': 'T-P1-00', 'quantita': 2}, {'codice': 'T-P2-00', 'quantita': 4}]}),
    'abbinato': dati(
        [riga('T-A1-00'), riga('T-P1-00', quantita=2), riga('T-P2-00', quantita=4)],
        {'n_righe': 3, 'da_abbinare': [], 'liberi': [],
         'assiemi': [{'codice': 'T-CVE', 'descrizione': 'CVE saldata', 'quantita': 2,
                      'pezzi': [{'codice': 'T-P1-00', 'quantita': 1}, {'codice': 'T-P2-00', 'quantita': 2}]}],
         'non_laser': [{'codice': 'T-ASTA', 'descrizione': 'Asta tonda', 'quantita': 1}]}),
}
stato = {'scenario': 'da_abbinare', 'abbina': []}


def finta_quantita(route):
    route.fulfill(status=200, content_type='application/json', body=json.dumps(SCENARI[stato['scenario']]))


def finto_abbina(route):
    stato['abbina'].append(json.loads(route.request.post_data or '{}'))
    route.fulfill(status=200, content_type='application/json', body=json.dumps({'success': True}))


def finta_ricerca(route):
    route.fulfill(status=200, content_type='application/json', body=json.dumps(
        {'success': True, 'pezzi': [{'codice': 'T-P9-00', 'materiale': 'INOX', 'spessore': 3.0}]}))


def avanti_attivo(pg):
    return pg.evaluate("(() => { const b = [...document.querySelectorAll('.lg-piede button')]"
                       ".find(x => x.textContent.includes('Avanti')); return b ? !b.disabled : null; })()")


with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = contesto_stazione(b.new_context(viewport={'width': 1500, 'height': 950}), B, 'laser')
    ctx.route('**/lantek-quantita*', finta_quantita)
    ctx.route('**/lantek-abbina', finto_abbina)
    ctx.route('**/api/lantek/pezzi*', finta_ricerca)
    pg = ctx.new_page()
    err = []
    pg.on('pageerror', lambda e: err.append(str(e)))
    pg.goto(B + '/laser.html')
    pg.wait_for_load_state('networkidle')
    pg.wait_for_timeout(1500)
    ids = pg.evaluate("ordiniFase('importare').map(o => o.id)")
    if len(ids) < 1:
        print('Nessun ordine "da importare" nella copia del database. Test saltato.')
        b.close()
        sys.exit(0)
    oid = ids[0]

    def apri(scenario):
        stato['scenario'] = scenario
        pg.evaluate("Object.keys(_guida).forEach(k => delete _guida[k])")
        pg.evaluate("selectedOrderId = null")
        pg.evaluate(f"selectOrder('{oid}')")
        pg.wait_for_timeout(1200)

    print('1) Righe da abbinare: bloccato')
    apri('da_abbinare')
    testo = pg.inner_text('#lg')
    check('sezione "da abbinare" con le 2 righe', 'Righe dell’ordine da abbinare' in testo and 'T-CVE' in testo and 'T-ASTA' in testo, testo[:300])
    check('le righe da abbinare non finiscono fra "importalo dal MES"', 'importalo dal MES' not in testo)
    check('Avanti bloccato', avanti_attivo(pg) is False)
    check('si dice perche\'', 'prima abbina le 2 righe' in pg.inner_text('.lg-piede'))

    print('2) "È un assieme": pezzi dell\'ordine senza riga')
    pg.locator('.lg-abb > li').nth(0).locator('button', has_text='È un assieme').click()
    pg.wait_for_timeout(300)
    ed = pg.locator('.lg-abb-ed')
    check('mostra i pezzi liberi', ed.count() == 1 and 'T-P1-00' in ed.inner_text() and 'T-P2-00' in ed.inner_text())
    ed.locator('label', has_text='T-P1-00').locator('input[type=checkbox]').check()
    pg.wait_for_timeout(200)
    pg.locator('.lg-abb-ed label', has_text='T-P2-00').locator('input[type=checkbox]').check()
    pg.wait_for_timeout(200)
    qta = pg.evaluate("[...document.querySelectorAll('.lg-abb-ed input[type=number]')].map(x => x.value)")
    check('pezzi per assieme = in Lantek / quantita\' riga (2/2=1, 4/2=2)', qta == ['1', '2'], str(qta))

    print('   ricerca di un altro pezzo in Lantek')
    pg.fill('.lg-abb-cerca input', 'T-P9')
    pg.click('.lg-abb-cerca button')
    pg.wait_for_timeout(500)
    pg.locator('.lg-abb-ed .lg-abb-p', has_text='T-P9-00').locator('button', has_text='Aggiungi').click()
    pg.wait_for_timeout(300)
    pg.locator('.lg-abb-ed label', has_text='T-P9-00').locator('input[type=number]').fill('3')
    pg.locator('.lg-abb-ed label', has_text='T-P9-00').locator('input[type=number]').dispatch_event('change')

    print('3) Salva')
    pg.locator('.lg-abb-ed button', has_text='Salva').click()
    pg.wait_for_timeout(800)
    inv = stato['abbina'][-1] if stato['abbina'] else {}
    check('manda codice e pezzi scelti',
          inv.get('codice') == 'T-CVE' and sorted((x['codice'], x['quantita']) for x in inv.get('pezzi', []))
          == [('T-P1-00', 1), ('T-P2-00', 2), ('T-P9-00', 3)], json.dumps(inv))

    print('4) Non va al laser')
    pg.locator('.lg-abb > li', has_text='T-ASTA').locator('button', has_text='Non va al laser').click()
    pg.wait_for_timeout(800)
    inv = stato['abbina'][-1] if stato['abbina'] else {}
    check('manda non_laser', inv == {'codice': 'T-ASTA', 'non_laser': True}, json.dumps(inv))

    print('5) Abbinato tutto: si va avanti')
    apri('abbinato')
    check('Avanti attivo', avanti_attivo(pg) is True)
    pg.click('.lg-abb-fatti summary')
    pg.wait_for_timeout(200)
    t = pg.inner_text('.lg-abb-fatti')
    check('si vedono assieme e non laser, con Rifai', 'T-P2-00 ×2' in t and 'non va al laser' in t and 'Rifai' in t, t)
    pg.locator('.lg-abb-fatti li', has_text='T-ASTA').locator('button', has_text='Rifai').click()
    pg.wait_for_timeout(800)
    check('Rifai manda dimentica', stato['abbina'][-1] == {'codice': 'T-ASTA', 'dimentica': True}, json.dumps(stato['abbina'][-1]))

    check('nessun errore JavaScript', not err, '; '.join(err)[:300])
    b.close()

print('\n%d/%d ok' % (sum(esiti), len(esiti)))
sys.exit(0 if all(esiti) else 1)
