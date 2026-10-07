# -*- coding: utf-8 -*-
"""Laser: "Metti in Lantek" passo per passo (laser-guida.js).

Stefano (07/10/2026): "se qua sbaglio o faccio confusione mando in crisi il
taglio". Si prova che:
 1. con un codice nuovo che FerroTrack non puo' creare, "Avanti" e' BLOCCATO
 2. con una differenza fra ordine e Lantek si sceglie uno per uno: finche'
    non si sceglie (o se si sceglie "lo correggo io") resta bloccato
 3. la conferma dice quanti pezzi e ordini di produzione verranno creati
 4. l'invio manda esattamente i codici visti; se riesce l'ordine passa fra
    quelli "In Lantek" e si propone il prossimo
 5. se Lantek non crea tutto: errore, e l'ordine NON viene segnato
 6. ordine senza codici (solo PDF): si fa a mano, poi "E' in Lantek"

Lantek e' SIMULATO (risposte finte del server per lantek-quantita,
lantek-invia e importato): niente arriva a Lantek ne' al database.
Serve il server di prova (default 127.0.0.1:5056).

    python app/tests/test_guida_lantek.py [url]
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


def riga(codice, stato='in_lantek', avvisi=None, quantita=2, in_produzione=0, gia_fatti=0):
    return {'codice': codice, 'codice_ft': codice, 'quantita': quantita, 'materiale': 'FERRO', 'spessore': 3.0,
            'stato': stato, 'avvisi': avvisi or [], 'in_produzione': in_produzione, 'gia_fatti': gia_fatti,
            'gia_fatti_il': '2026-08-06' if gia_fatti else None}


def dati(righe, nuovi_si=(), nuovi_no=()):
    da_mandare = [{'codice': r['codice'], 'quantita': r['quantita'], 'gia_fatti': r['gia_fatti'],
                   'gia_fatti_il': r['gia_fatti_il']}
                  for r in righe if r['stato'] == 'in_lantek' and not r['in_produzione']]
    return {'success': True, 'righe': righe, 'lantek': {'disponibile': True, 'errore': None},
            'commessa': '9999', 'consegna': '2030-11-06', 'cliente_lantek': 'PROVA', 'nome_file': 'x.xlsx',
            'nome_file_xml': 'x.xml', 'n_codici': len(righe), 'n_pezzi': sum(r['quantita'] for r in righe),
            'n_nuovi': sum(1 for r in righe if r['stato'] == 'nuovo'),
            'n_in_produzione': sum(1 for r in righe if r['in_produzione']),
            'n_gia_fatti': sum(1 for r in righe if r['gia_fatti']), 'n_xml': len(da_mandare),
            'da_mandare': da_mandare,
            'nuovi_da_creare': [{'codice': c, 'materiale': 'FERRO', 'spessore': 3.0, 'quantita': 2} for c in nuovi_si],
            'nuovi_esclusi': [{'codice': c, 'motivo': 'manca il disegno'} for c in nuovi_no],
            'invio_automatico': True, 'n_avvisi': sum(1 for r in righe if r['avvisi'])}


SCENARI = {
    'bloccato': dati([riga('T-A1'), riga('T-N1', 'nuovo', ['non ancora in Lantek: importa prima il disegno'])],
                     nuovi_no=['T-N1']),
    'decidere': dati([riga('T-A1'), riga('T-B1', avvisi=["spessore: l'ordine dice 2 mm, in Lantek e' 3 mm"])]),
    'pronto': dati([riga('T-A1'), riga('T-B1', gia_fatti=4), riga('T-P1', in_produzione=2),
                    riga('T-N2', 'nuovo', ['non ancora in Lantek: importa prima il disegno'])], nuovi_si=['T-N2']),
    'vuoto': {**dati([]), 'n_codici': 0},
}
stato = {'scenario': 'bloccato', 'invii': [], 'importati': 0, 'esito_invio': 'ok'}


def finta_quantita(route):
    route.fulfill(status=200, content_type='application/json', body=json.dumps(SCENARI[stato['scenario']]))


def finto_invio(route):
    stato['invii'].append(json.loads(route.request.post_data or '{}'))
    if stato['esito_invio'] == 'ok':
        corpo = {'success': True, 'pezzi_nuovi': 1, 'pezzi_creati': 1, 'pezzi_non_creati': [], 'mandati': 3,
                 'verificati': 3, 'mancanti': [], 'rapporto': {'totale': 3, 'ok': 3, 'avvisi': 0, 'con_errore': 0, 'errori': []}}
    else:
        corpo = {'success': True, 'pezzi_nuovi': 1, 'pezzi_creati': 1, 'pezzi_non_creati': [], 'mandati': 3,
                 'verificati': 2, 'mancanti': ['T-B1'],
                 'rapporto': {'totale': 3, 'ok': 2, 'avvisi': 0, 'con_errore': 1,
                              'errori': [{'comando': '2.', 'pezzo': 'T-B1', 'messaggio': 'insert failed'}]}}
    route.fulfill(status=200, content_type='application/json', body=json.dumps(corpo))


def finto_importato(route):
    stato['importati'] += 1
    route.fulfill(status=200, content_type='application/json', body=json.dumps({'success': True}))


def avanti_attivo(pg):
    return pg.evaluate("(() => { const b = [...document.querySelectorAll('.lg-piede button')]"
                       ".find(x => x.textContent.includes('Avanti')); return b ? !b.disabled : null; })()")


with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    ctx = contesto_stazione(b.new_context(viewport={'width': 1500, 'height': 950}), B, 'laser')
    ctx.route('**/lantek-quantita*', finta_quantita)
    ctx.route('**/lantek-invia', finto_invio)
    ctx.route('**/importato', finto_importato)
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

    print('1) Codice nuovo che non posso creare: bloccato')
    apri('bloccato')
    testo = pg.locator('#lg').inner_text()
    check('si apre la procedura (passo 1, Controllo)', 'Controllo' in testo and 'Da sistemare prima' in testo, testo[:200])
    check('il codice bloccato col motivo e cosa fare', 'T-N1' in testo and 'manca il disegno' in testo and 'MES' in testo)
    check('Avanti bloccato', avanti_attivo(pg) is False)

    print('\n2) Differenza ordine/Lantek: decido io')
    apri('decidere')
    check('la differenza e\' mostrata', 'Da decidere' in pg.locator('#lg').inner_text())
    check('Avanti bloccato finche\' non scelgo', avanti_attivo(pg) is False)
    pg.locator('.lg-dec button:has-text("Lo correggo io")').first.click()
    pg.wait_for_timeout(200)
    check('"lo correggo io": resta bloccato e dice cosa fare',
          avanti_attivo(pg) is False and 'Ricontrolla' in pg.locator('.lg-dec').inner_text())
    pg.locator('.lg-dec button:has-text("Va bene così")').first.click()
    pg.wait_for_timeout(200)
    check('"va bene cosi\'": Avanti si sblocca', avanti_attivo(pg) is True)

    print('\n3) Conferma')
    apri('pronto')
    testo = pg.locator('#lg').inner_text()
    check('passo 1: dice chi crea, chi manda, chi e\' gia\' in produzione',
          'li creo io' in testo and 'non li rimando' in testo and 'già fatti in passato' in testo, testo[:400])
    check('Avanti attivo', avanti_attivo(pg) is True)
    pg.evaluate(f"guidaVai('{oid}', 2)")
    pg.wait_for_timeout(300)
    testo = pg.locator('#lg').inner_text()
    check('riepilogo: 1 pezzo nuovo e 3 ordini di produzione, ordine e cliente',
          'Sto per creare in Lantek' in testo and '9999' in testo and 'PROVA' in testo
          and pg.locator('.lg-num b').all_inner_texts() == ['1', '3'], pg.locator('.lg-num b').all_inner_texts())
    check('avviso dei gia\' fatti in passato', 'già stati fatti in passato' in testo)
    check('nessun invio prima di premere "Manda a Lantek"', not stato['invii'])

    print('\n4) Invio riuscito')
    pg.locator('#lg-manda').click()
    pg.wait_for_timeout(2500)
    testo = pg.locator('#lg').inner_text()
    inv = stato['invii'][-1] if stato['invii'] else {}
    check('mandati esattamente i codici visti (T-P1 in produzione escluso)',
          sorted(inv.get('codici') or []) == ['T-A1', 'T-B1'] and inv.get('nuovi') == ['T-N2'], inv)
    check('passo 3 "Fatto" e ordine segnato in Lantek', 'Fatto' in testo and 'è in Lantek' in testo
          and stato['importati'] == 1, (stato['importati'], testo[:300]))
    check('dice di lanciarli dal MES', 'MES' in testo)

    print('\n5) Invio non riuscito del tutto')
    stato['esito_invio'] = 'parziale'
    apri('pronto')
    pg.evaluate(f"guidaVai('{oid}', 2)")
    pg.wait_for_timeout(300)
    pg.locator('#lg-manda').click()
    pg.wait_for_timeout(2500)
    testo = pg.locator('#lg').inner_text()
    check('errore col codice, l\'ordine NON segnato', 'non ha creato tutto' in testo and 'T-B1' in testo
          and stato['importati'] == 1, (stato['importati'], testo[:300]))
    check('propone Ricontrolla', 'Ricontrolla' in pg.locator('.lg-piede').inner_text())

    print('\n6) Ordine senza codici (solo PDF)')
    apri('vuoto')
    testo = pg.locator('#lg').inner_text()
    check('si fa a mano dal MES, poi "E\' in Lantek"', 'a mano dal MES' in testo and 'È in Lantek' in testo, testo[:200])

    check('nessun errore nella pagina', not err, err[:2])
    b.close()

ko = esiti.count(False)
print(f'\nPASSATI: {len(esiti) - ko}   FALLITI: {ko}')
sys.exit(1 if ko else 0)
