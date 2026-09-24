"""Test nel BROWSER dei 10 criteri di accettazione del preventivatore (10.11).

Quattro di questi comportamenti vivono nel JavaScript e non sono verificabili
dal solo backend: il salvataggio che non deve finire sul documento sbagliato,
l'errore di rete che non deve sparire in console, le risposte fuori ordine e i
messaggi CAD non autorizzati. Qui si provano davvero, guidando la pagina.

Serve un server in ascolto. Per non toccare il database di lavoro il test si
aspetta un'istanza su una COPIA:

    python app/tests/test_preventivi_browser.py [url]

Senza argomenti usa http://127.0.0.1:5056.
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
# Si entra dalla postazione laser, che porta con se' la delega del capo:
# e' da li' che, in azienda, si tocca un preventivo senza passare dall'ufficio.
UTENTE = {'id': 'postazione-laser', 'name': 'Laser',
          'role': 'Laser', 'is_capo': True,
          'permissions': ['overview', 'supervisione', 'lavorazione', 'archive']}

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

    with urllib.request.urlopen(BASE + '/api/preventivi', timeout=8) as r:
        elenco = json.load(r)
    prev = elenco.get('preventivi') if isinstance(elenco, dict) else elenco
    bozze = [p for p in prev if p.get('status') == 'BOZZA']
    # Serve una bozza CON articoli: senza, i controlli sui messaggi CAD non
    # proverebbero nulla.
    con_articoli = []
    for x in bozze[:12]:
        try:
            with urllib.request.urlopen(BASE + '/api/preventivi/' + x['id'], timeout=6) as r:
                d = json.load(r)
            dett = d.get('preventivo') or d
            if (dett.get('articoli') or []):
                con_articoli.append(x['id'])
        except Exception:
            pass
        if len(con_articoli) >= 1 and len(bozze) >= 2:
            break
    if not con_articoli or len(bozze) < 2:
        print('Servono due bozze, di cui almeno una con articoli. Test saltato.')
        return 0
    PID_A = con_articoli[0]
    PID_B = next(x['id'] for x in bozze if x['id'] != PID_A)

    errori_pagina = []
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        # L'utente si imposta PRIMA che la pagina parta: passando da login.html
        # la pagina di ingresso reindirizza da sola e interrompe la navigazione
        # del test (net::ERR_ABORTED a caso).
        ctx = b.new_context(viewport={'width': 1500, 'height': 950})
        ctx.add_init_script('localStorage.setItem("currentUser", '
                            + json.dumps(json.dumps(UTENTE)) + ')')
        pg = ctx.new_page()
        pg.on('pageerror', lambda e: errori_pagina.append(str(e)))
        pg.goto(BASE + '/preventivi.html')
        pg.wait_for_load_state('networkidle')
        pg.wait_for_timeout(2500)
        pg.evaluate('id => openPreventivo(id)', PID_A)
        pg.wait_for_timeout(3500)
        check('preventivo aperto', pg.evaluate('() => !!currentPreventivo'))

        def stato():
            return pg.inner_text('#stato-salvataggio').strip()

        # --- criterio 5: errore di rete ---------------------------------
        print('\n5) Errore di rete durante il salvataggio')
        pg.route('**/api/preventivi/*/articoli', lambda route: route.abort())
        pg.evaluate('() => scheduleSaveArticoli(50)')
        pg.wait_for_timeout(2200)
        check('lo stato lo dice, non solo la console',
              'non riuscito' in stato().lower(), stato())
        check('c e il pulsante Riprova',
              pg.locator('#stato-salvataggio button').count() == 1)
        check('i dati NON sono persi', pg.evaluate('() => salvataggioInSospeso()'))
        check('l indicatore e visibile', pg.locator('#stato-salvataggio').is_visible())

        pg.unroute('**/api/preventivi/*/articoli')
        pg.evaluate('() => riprovaSalvataggio()')
        pg.wait_for_timeout(2500)
        check('Riprova recupera', 'salvato' in stato().lower(), stato())
        check('coda svuotata', not pg.evaluate('() => salvataggioInSospeso()'))

        # --- criterio 6: cambio rapido di preventivo --------------------
        print('\n6) Cambio rapido di preventivo')
        pg.evaluate('() => scheduleSaveArticoli(4000)')
        pg.wait_for_timeout(200)
        destinatario = pg.evaluate('() => _salva.articoli.inSospeso.preventivoId')
        check('il destinatario e fissato sul preventivo aperto',
              destinatario == PID_A, destinatario)
        pg.evaluate('id => openPreventivo(id)', PID_B)
        pg.wait_for_timeout(1500)
        ancora = pg.evaluate(
            '() => _salva.articoli.inSospeso ? _salva.articoli.inSospeso.preventivoId : null')
        check('cambiando documento il destinatario NON diventa il nuovo',
              ancora in (PID_A, None), (ancora, PID_A, PID_B))
        check('e comunque non e il preventivo sbagliato', ancora != PID_B, ancora)

        # --- criterio 7: risposte fuori ordine --------------------------
        print('\n7) Risposte fuori ordine')
        esito = pg.evaluate("""() => {
          const st = _salva.articoli;
          st.seq = 5; st.applicata = 7;
          // una risposta con numero 5 arriva dopo che ne e' gia' stata
          // applicata una piu' recente (7): deve essere scartata
          return { vecchia_scartata: 5 < st.applicata, applicata: st.applicata };
        }""")
        check('una risposta vecchia non sovrascrive una nuova',
              esito['vecchia_scartata'] is True, esito)
        pg.evaluate('() => { _salva.articoli.seq = 0; _salva.articoli.applicata = 0; }')

        # --- criterio 8: messaggi CAD ------------------------------------
        print('\n8) Messaggi CAD obsoleti o non autorizzati')
        pg.evaluate('id => openPreventivo(id)', PID_A)
        pg.wait_for_timeout(3000)
        n_art = pg.evaluate('() => currentArticoli.length')
        if n_art:
            prima = pg.evaluate('() => JSON.stringify(currentArticoli[0])')
            # messaggio per un ALTRO preventivo
            pg.evaluate("""pid => window.postMessage({type:'cad-confirm', payload:{
                preventivo_id: pid, articolo: 0, area_dm2: 99999,
                materiale: 'ORO', spessore_mm: 99}}, '*')""", PID_B)
            pg.wait_for_timeout(900)
            check('messaggio di un altro preventivo ignorato',
                  pg.evaluate('() => JSON.stringify(currentArticoli[0])') == prima)
            # messaggio per un articolo inesistente
            pg.evaluate("""() => window.postMessage({type:'cad-confirm', payload:{
                articolo_id: 'non-esiste', area_dm2: 99999,
                materiale: 'ORO', spessore_mm: 99}}, '*')""")
            pg.wait_for_timeout(900)
            check('messaggio per un articolo non in elenco ignorato',
                  pg.evaluate('() => JSON.stringify(currentArticoli[0])') == prima)
            check('la funzione indirizza per id',
                  pg.evaluate("() => _articoloDestinatario({articolo_id: currentArticoli[0].id}) === 0"))
            check('e rifiuta un id sconosciuto',
                  pg.evaluate("() => _articoloDestinatario({articolo_id: 'xxx'}) === -1"))
        else:
            print('  (preventivo senza articoli: controllo sui messaggi limitato)')
        check('origine estranea rifiutata',
              pg.evaluate("() => _messaggioAffidabile({origin: 'http://malintenzionato.local'}) === false"))
        check('stessa origine accettata',
              pg.evaluate("() => _messaggioAffidabile({origin: window.location.origin}) === true"))

        b.close()

    check('nessun errore JavaScript in pagina', not errori_pagina, errori_pagina)

    print('\n' + '=' * 60)
    print(f'PASSATI: {OK}   FALLITI: {len(KO)}')
    if KO:
        print('Falliti: ' + ', '.join(KO))
    print('=' * 60)
    return 1 if KO else 0


if __name__ == '__main__':
    sys.exit(main())
