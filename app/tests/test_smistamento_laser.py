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

import os  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.accesso_server import Sessione, contesto_stazione  # noqa: E402

# Chi agisce lo dice il dispositivo registrato (cookie), non lo user_id:
# un "dispositivo" Laser per le chiamate dirette, creato quando serve.
_LASER = None


def laser():
    global _LASER
    if _LASER is None:
        _LASER = Sessione(B, 'laser')
    return _LASER

esiti = []


def check(nome, cond, extra=''):
    esiti.append(bool(cond))
    print('  [%s] %s %s' % ('OK' if cond else 'KO', nome, extra if not cond else ''))


def ordini():
    _c, d = laser().get('/api/orders')
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
        c, _d = laser().post('/api/orders/%s/smistamento' % o['id'], {'annulla': True})
        if c == 200:
            fatti += 1
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
    ctx = contesto_stazione(b.new_context(viewport={'width': 1500, 'height': 950}), B, 'laser')
    pg = ctx.new_page()
    err = []
    pg.on('pageerror', lambda e: err.append(str(e)))
    # Tre: due si smistano nella prova, e uno deve restare da guardare —
    # e' quello che in officina si deve leggere come "Da smistare".
    if not rimetti_da_smistare(3):
        # Nella copia del database ogni ordine e' gia' tagliato: niente da
        # smistare, e su un taglio fatto non si torna indietro.
        print('Nessun ordine non tagliato in questa copia del database. Test saltato.')
        b.close()
        sys.exit(0)

    pg.goto(B + '/laser.html')
    pg.wait_for_selector('.lz-r[data-id]', timeout=20000)
    pg.wait_for_timeout(2500)
    check('la pagina del laser si apre senza errori', not err, err[:1])
    check('nessun ordine si apre da solo', pg.evaluate('LZ.s.aperto') is None)

    def da_smistare():
        return pg.evaluate("LZ.ordiniLaser().filter(o => LZ.statoLaser(o) === 'smistare').map(o => o.id)")

    scartati_prima = len([o for o in ordini() if o.get('taglio_richiesto') is False])
    ids = da_smistare()
    prima = len(ids)
    print('  ordini da smistare: %d' % prima)
    check('c\'e\' qualcosa da smistare, con l\'etichetta "Da smistare"', prima > 0
          and 'Da smistare' in pg.locator(f'.lz-r[data-id="{ids[0]}"]').inner_text())
    # Le righe servono a scegliere: nessun pulsante (07/10/2026). Si apre
    # l'ordine e si decide nel prossimo passo.
    check('nelle righe della lista non ci sono pulsanti', pg.locator('.lz-r button').count() == 0)

    # --- "non passa dal laser": chiede conferma, poi esce dalla coda ---
    pg.click(f'.lz-r[data-id="{ids[0]}"]')
    pg.wait_for_timeout(1500)
    check('il prossimo passo chiede "Passa dal laser?"', 'Passa dal laser?' in pg.locator('.lz-passo').inner_text())
    pg.locator('[data-az="smista-no"]').click()
    pg.wait_for_timeout(400)
    check('"No" chiede conferma', pg.locator('.ft-modal-bg.open').count() == 1)
    pg.locator('.ft-modal-bg.open button[data-v="0"]').click()     # ci ripenso
    pg.wait_for_timeout(1500)
    check('annullando la conferma non cambia niente', len(da_smistare()) == prima)
    pg.locator('[data-az="smista-no"]').click()
    pg.wait_for_timeout(400)
    pg.locator('.ft-modal-bg.open button[data-v="1"]').click()
    pg.wait_for_timeout(3000)
    check('scartandolo esce dall\'elenco', len(da_smistare()) == prima - 1 and ids[0] not in
          pg.evaluate("LZ.ordiniLaser().map(o => o.id)"), '%d -> %d' % (prima, len(da_smistare())))
    check('e non si apre da solo un altro ordine', pg.evaluate('LZ.s.aperto') in (None, ids[0]))

    # Uno IN PIU', non "esattamente uno": la copia del database conserva le
    # esecuzioni precedenti, e pretendere di partire da zero renderebbe il test
    # verde solo la prima volta.
    scartati = [o for o in ordini() if o.get('taglio_richiesto') is False]
    check("ce n'e' uno in piu' che non passa dal laser",
          len(scartati) == scartati_prima + 1,
          '%d -> %d' % (scartati_prima, len(scartati)))

    # --- "va tagliato": entra nella coda (senza conferma, Invio) ---
    resto = da_smistare()
    pg.click(f'.lz-r[data-id="{resto[0]}"]')
    pg.wait_for_timeout(1500)
    pg.keyboard.press('Enter')
    pg.wait_for_timeout(3000)
    accettati = [o for o in ordini()
                 if o.get('taglio_richiesto') is True and not o.get('taglio_completato')]
    check('l\'altro entra nella coda di taglio', len(accettati) >= 1, len(accettati))
    check('e resta aperto, ora da mettere in Lantek', pg.evaluate('LZ.s.aperto') == resto[0]
          and 'Da smistare' not in pg.locator(f'.lz-r[data-id="{resto[0]}"]').inner_text())
    check('nessun errore JavaScript in tutta la prova', not err, err[:1])
    pg.close()

    # --- l'officina vede il cambiamento ---
    ctx2 = contesto_stazione(b.new_context(viewport={'width': 1024, 'height': 768}), B, 'reparto')
    pg2 = ctx2.new_page()
    err2 = []
    pg2.on('pageerror', lambda e: err2.append(str(e)))
    pg2.goto(B + '/operaio-info.html')
    pg2.wait_for_load_state('networkidle')
    pg2.wait_for_timeout(2500)
    testo = pg2.inner_text('body')
    print()
    check('in officina compare "Lavorabile"', 'Lavorabile' in testo)
    check('e "In taglio"', 'In taglio' in testo)
    # Solo se resta un ordine ancora in lavorazione da smistare: uno gia'
    # "da fatturare" (lavoro finito) giustamente sul tablet non c'e' piu'.
    restano = [o for o in ordini() if o.get('taglio_richiesto') is None
               and o.get('status') in ('RICEVUTO', 'IN_LAVORAZIONE')]
    if restano:
        check('e cio\' che il laser non ha guardato resta "Da smistare"',
              'Da smistare' in testo, [o.get('numero_ordine') for o in restano])
    else:
        print('  (nessun ordine in lavorazione rimasto da smistare: controllo "Da smistare" saltato)')
    check('lo scartato dice che non passa dal laser',
          'non passa dal laser' in testo)
    check('nessun errore sul tablet', not err2, err2[:1])
    pg2.close()
    b.close()

print('\nPASSATI: %d   FALLITI: %d' % (sum(esiti), len(esiti) - sum(esiti)))
sys.exit(0 if all(esiti) else 1)
