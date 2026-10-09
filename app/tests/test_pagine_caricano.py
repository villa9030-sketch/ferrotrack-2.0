"""Ogni pagina si apre senza errori JavaScript.

Nasce da un guaio vero: scrivendo in italiano, un apostrofo dentro una stringa
JavaScript ('il residuo e' sovrastimato') la chiude a meta' e ROMPE l'intera
pagina. Era successo a due pagine — quella del laser e quella dell'ufficio — e
nessun test lo aveva visto, perche' i test provavano la logica e non il
caricamento.

Un errore cosi' non si nota leggendo il codice: si nota solo aprendo la pagina.
Per questo il controllo sta qui.

Serve un server in ascolto (default 127.0.0.1:5056, cioe' un'istanza di prova
su una COPIA del database). Se non lo trova, il test si salta.

    python app/tests/test_pagine_caricano.py [url]
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print('Playwright non installato: test saltato.')
    sys.exit(0)

BASE = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5056'

# Ogni pagina si apre da un dispositivo registrato come la sua stazione
# (cookie), con la persona dentro col PIN negli uffici: lo prepara
# tests/accesso_server.py sul server di prova.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.accesso_server import contesto_stazione, PIN_ADMIN  # noqa: E402

# Da quale stazione si apre ciascuna pagina ('admin' = Ufficio + PIN admin).
STAZIONE = {
    'laser.html': 'laser', 'laser-vecchio.html': 'laser', 'capo-officina.html': 'laser',
    'impiegata.html': 'ufficio', 'archivio.html': 'ufficio', 'ufficio-ore.html': 'ufficio',
    'admin.html': 'admin',
    'preventivi.html': 'commerciale', 'dxf-editor.html': 'commerciale',
    'operaio-info.html': 'reparto', 'ore.html': 'ore',
}

# Le pagine si leggono dalla cartella, non da un elenco scritto a mano: un
# elenco invecchia in silenzio, e le pagine tolte continuano a risultare sane.
_FRONTEND = os.path.join(_APP, 'frontend')

# Pagine che di per se' non stanno in piedi da sole: sono pezzi aperti da
# un'altra pagina, e da sole mostrerebbero errori che non sono guasti.
NON_AUTONOME = {'preview-dxf.html', 'preview-step.html', 'fold3d.html'}

PAGINE = sorted(f for f in os.listdir(_FRONTEND)
                if f.endswith('.html') and f not in NON_AUTONOME)


OK = 0
KO = []


def check(nome, cond, extra=''):
    global OK
    if cond:
        OK += 1
        print(f'  [OK] {nome}')
    else:
        KO.append(nome)
        print(f'  [KO] {nome} {extra}')


def main():
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(BASE + '/api/health', timeout=4) as r:
            json.load(r)
    except Exception as e:
        print(f'Server non raggiungibile su {BASE}: {e}')
        print('Avvia un\'istanza di prova e riesegui. Test saltato.')
        return 0

    print(f'Controllo {len(PAGINE)} pagine su {BASE}\n')
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        for pagina in PAGINE:
            errori = []
            ctx = b.new_context(viewport={'width': 1440, 'height': 900})
            st = STAZIONE.get(pagina)
            if st:
                contesto_stazione(ctx, BASE, 'ufficio' if st == 'admin' else st)
                if st == 'admin':
                    ctx.request.post(BASE + '/api/accesso/admin', data={'pin': PIN_ADMIN})
            pg = ctx.new_page()
            pg.on('pageerror', lambda e: errori.append(str(e)))
            try:
                risposta = pg.goto(BASE + '/' + pagina)
                # Il codice HTTP va guardato: una pagina che risponde 500
                # restituisce un JSON di errore, dentro cui non c'e' nessun
                # JavaScript da sbagliare, e passerebbe per sana.
                stato = risposta.status if risposta else 0
                if stato != 200:
                    check(f'{pagina} viene servita dal server', False,
                          f'HTTP {stato}')
                    continue
                pg.wait_for_load_state('networkidle', timeout=20000)
                pg.wait_for_timeout(1800)
                e_html = pg.evaluate('() => !!document.querySelector("body *")')
                check(f'{pagina} viene servita dal server', e_html,
                      'il corpo della pagina e\' vuoto')
                check(f'{pagina} si apre senza errori JavaScript',
                      not errori, errori[:2])
            except Exception as e:
                check(f'{pagina} si apre senza errori JavaScript', False, str(e)[:90])
            finally:
                ctx.close()
        b.close()

    print('\n' + '=' * 60)
    print(f'PASSATI: {OK}   FALLITI: {len(KO)}')
    if KO:
        print('Falliti: ' + ', '.join(KO))
        print('\nUn errore JavaScript rende la pagina INUTILIZZABILE, anche se il')
        print('resto del programma funziona. Guarda il messaggio: quasi sempre e\'')
        print('un apostrofo italiano dentro una stringa fra apici singoli.')
    print('=' * 60)
    return 1 if KO else 0


if __name__ == '__main__':
    sys.exit(main())
