# -*- coding: utf-8 -*-
"""E' il laser a smistare: decide cosa passa da lui, e l'officina lo vede.

Non tutto passa dal laser — i tubolari e gli assiemi di tubo vanno dritti in
officina — ma prima ogni ordine era implicitamente da tagliare, e quelli
restavano fermi in una coda che non li riguardava senza che nessuno in officina
sapesse che erano gia' lavorabili.

Il campo ha TRE stati, e la differenza conta: "non ancora guardato" non e'
"guardato e scartato". Confonderli manda in officina roba che nessuno ha visto.

Qui si prova il giro intero: il laser apre, smista, e il tablet di officina
cambia di conseguenza.

Serve un server in ascolto (default 127.0.0.1:5056, cioe' un'istanza di prova
su una COPIA del database): il test SCRIVE, e non deve farlo sui dati veri.

    python app/tests/test_smistamento_laser.py [url]
"""
import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from playwright.sync_api import sync_playwright

B = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5056'
LASER = {'id': 'postazione-laser', 'name': 'Laser', 'role': 'Laser', 'is_capo': True,
         'permissions': ['overview', 'supervisione', 'lavorazione', 'archive']}
VISIONE = {'id': 'postazione-visione', 'name': 'Tablet di visione',
           'role': 'Visione', 'permissions': ['overview']}

esiti = []


def check(nome, cond, extra=''):
    esiti.append(bool(cond))
    print('  [%s] %s %s' % ('OK' if cond else 'KO', nome, extra if not cond else ''))


def ordini():
    d = json.loads(urllib.request.urlopen(B + '/api/orders', timeout=15).read())
    return d if isinstance(d, list) else (d.get('orders') or [])


def rimetti_da_smistare(quanti=2):
    """Riporta un po' di ordini a "da guardare", per avere su cosa lavorare.

    Il test consuma cio' che smista: senza questo, alla seconda esecuzione non
    troverebbe piu' niente e fallirebbe per esaurimento invece che per un
    difetto vero. Si usa la stessa strada di una persona che ci ripensa.
    """
    fatti = 0
    for o in ordini():
        if fatti >= quanti:
            break
        if o.get('taglio_completato'):
            continue          # su un ordine gia' tagliato non si torna indietro
        corpo = json.dumps({'user_id': 'postazione-laser', 'annulla': True}).encode()
        req = urllib.request.Request(
            B + '/api/orders/%s/smistamento' % o['id'], data=corpo, method='POST')
        req.add_header('Content-Type', 'application/json')
        try:
            urllib.request.urlopen(req, timeout=15).read()
            fatti += 1
        except Exception:
            continue
    return fatti


try:
    with urllib.request.urlopen(B + '/api/health', timeout=4) as _r:
        _salute = json.load(_r)
except Exception as _e:
    print('Server non raggiungibile su %s: %s' % (B, str(_e)[:60]))
    print("Avvia un'istanza di prova e riesegui. Test saltato.")
    sys.exit(0)

# Questa prova SCRIVE. Se il programma sta lavorando sui dati veri non si parte:
# e' gia' successo di smistare per sbaglio otto ordini in produzione perche'
# l'indirizzo puntava alla porta del server vero.
if not _salute.get('istanza_di_prova'):
    print('RIFIUTO: %s sta lavorando sul DATABASE VERO.' % B)
    print('Questa prova scrive. Avvia app/tools/server_prova.py e ripunta li\'.')
    sys.exit(1)

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    pg = b.new_page(viewport={'width': 1500, 'height': 950})
    err = []
    pg.on('pageerror', lambda e: err.append(str(e)))

    pg.goto(B + '/login.html')
    pg.evaluate('u => localStorage.setItem("currentUser", JSON.stringify(u))', LASER)
    # Tre: due si smistano nella prova, e uno deve restare da guardare —
    # e' quello che in officina si deve leggere come "Da smistare".
    rimetti_da_smistare(3)

    pg.goto(B + '/laser.html')
    pg.wait_for_load_state('networkidle')
    pg.wait_for_timeout(2500)
    check('la pagina del laser si apre senza errori', not err, err[:1])

    # --- si apre sulla vista "da smistare" ---
    check('si apre su "Da smistare"',
          pg.locator('#vt-smistare').get_attribute('class').find('active') >= 0)
    titolo = pg.locator('#calendar-view-title').inner_text()
    check('il titolo lo dice', 'smistare' in titolo.lower(), titolo)

    scartati_prima = len([o for o in ordini() if o.get('taglio_richiesto') is False])
    prima = len(pg.locator('.riga-smista').all())
    print('  ordini da smistare: %d' % prima)
    check('c\'e\' qualcosa da smistare', prima > 0)
    conta = pg.locator('#conta-smistare').inner_text()
    check('la scheda porta il conteggio', str(prima) in conta, conta)

    # --- "non passa da qui": deve sparire dalla coda e diventare lavorabile ---
    pg.locator('.riga-smista .btn-no').first.click()
    pg.wait_for_timeout(3000)
    dopo = len(pg.locator('.riga-smista').all())
    check('scartandolo esce dall\'elenco da smistare', dopo == prima - 1,
          '%d -> %d' % (prima, dopo))

    # Uno IN PIU', non "esattamente uno": la copia del database conserva le
    # esecuzioni precedenti, e pretendere di partire da zero renderebbe il test
    # verde solo la prima volta.
    scartati = [o for o in ordini() if o.get('taglio_richiesto') is False]
    check("ce n'e' uno in piu' che non passa dal laser",
          len(scartati) == scartati_prima + 1,
          '%d -> %d' % (scartati_prima, len(scartati)))

    # --- "va tagliato": entra nella coda ---
    pg.locator('.riga-smista .btn-si').first.click()
    pg.wait_for_timeout(3000)
    accettati = [o for o in ordini()
                 if o.get('taglio_richiesto') is True and not o.get('taglio_completato')]
    check('l\'altro entra nella coda di taglio', len(accettati) >= 1, len(accettati))

    pg.locator('#vt-scadenza').click()
    pg.wait_for_timeout(1500)
    check('e si vede nella coda "Da tagliare"',
          pg.locator('button.btn-taglio').count() > 0)
    check('nella coda non ci sono piu\' i due pulsanti di smistamento',
          pg.locator('.riga-smista').count() == 0)
    check('nessun errore JavaScript in tutta la prova', not err, err[:1])
    pg.close()

    # --- l'officina vede il cambiamento ---
    pg2 = b.new_page(viewport={'width': 1024, 'height': 768})
    err2 = []
    pg2.on('pageerror', lambda e: err2.append(str(e)))
    pg2.goto(B + '/login.html')
    pg2.evaluate('u => localStorage.setItem("currentUser", JSON.stringify(u))', VISIONE)
    pg2.goto(B + '/operaio-info.html')
    pg2.wait_for_load_state('networkidle')
    pg2.wait_for_timeout(2500)
    testo = pg2.inner_text('body')
    print()
    check('in officina compare "Lavorabile"', 'Lavorabile' in testo)
    check('e "In taglio"', 'In taglio' in testo)
    check('e cio\' che il laser non ha guardato resta "Da smistare"',
          'Da smistare' in testo)
    check('lo scartato dice che non passa dal laser',
          'non passa dal laser' in testo)
    check('nessun errore sul tablet', not err2, err2[:1])
    pg2.close()
    b.close()

print('\nPASSATI: %d   FALLITI: %d' % (sum(esiti), len(esiti) - sum(esiti)))
sys.exit(0 if all(esiti) else 1)
