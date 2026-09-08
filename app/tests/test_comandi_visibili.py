# -*- coding: utf-8 -*-
"""Nessun comando deve sparire proprio mentre lo si sta per premere.

E' successo tre volte in un giorno solo:

  - la riga dell'ordine selezionata al laser: pulsante bianco su verde;
  - il pulsante "Lavorazione finita" dell'amministrazione: una regola generica
    scritta DOPO quella del pulsante verde ne schiariva lo sfondo al passaggio
    del mouse, lasciando il testo bianco. Bianco su bianco;
  - il badge del confronto ore, illeggibile perche' ereditava dal numero
    grande una spaziatura negativa.

Sono difetti che a leggere il CSS non si vedono: le regole si sovrascrivono per
ordine di scrittura, non per come sembrano. Si trovano solo misurando il
contrasto vero come lo calcola il browser, a riposo e col mouse sopra.

Serve un server in ascolto (default 127.0.0.1:5056, cioe' un'istanza di prova
su una COPIA del database). Senza, il test si salta.

    python app/tests/test_comandi_visibili.py [url]
"""
import io
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

CAPO = {'id': 'postazione-laser', 'name': 'Laser', 'role': 'Laser', 'is_capo': True,
        'permissions': ['overview', 'supervisione', 'lavorazione', 'archive']}
UFFICIO = {'id': 'postazione-amministrazione', 'name': 'Amministrazione',
           'role': 'Amministrazione', 'permissions': ['overview', 'supervisione']}

PAGINE = [
    ('impiegata.html', UFFICIO),
    ('capo-officina.html', CAPO),
    ('laser.html', CAPO),
    ('preventivi.html', CAPO),
    ('admin.html', CAPO),
    ('archivio.html', CAPO),
]

# Sotto 3 una scritta si fatica a leggerla; sotto 2 e' praticamente sparita.
# Si segnala da 3 in giu', perche' un comando che non si legge e' un comando
# che non si preme.
SOGLIA = 3.0

# Colore del testo e etichetta: quelli si leggono dal CSS senza ambiguita'.
COLORE_TESTO = """(el) => {
  const st = getComputedStyle(el);
  return { testo: st.color,
           spento: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
           etichetta: (el.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 38) };
}"""


def _luminanza(c):
    """Quanto e' chiaro un colore, come lo percepisce l'occhio."""
    v = []
    for x in c[:3]:
        x = x / 255.0
        v.append(x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4)
    return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]


def _contrasto(a, b):
    la, lb = _luminanza(a), _luminanza(b)
    return round((max(la, lb) + 0.05) / (min(la, lb) + 0.05), 2)


def _rgb(css):
    """Da 'rgb(26, 122, 72)' a (26, 122, 72)."""
    import re
    n = [float(x) for x in re.findall(r'[0-9.]+', css or '')]
    return tuple(int(x) for x in (n + [0, 0, 0])[:3])


def _sfondo_dai_pixel(png):
    """Il colore di sfondo, guardando la fotografia del comando.

    Si campiona lungo il bordo alto, un paio di pixel dentro: li' c'e' lo
    sfondo e non il testo. Dedurlo dal CSS non basta — una sfumatura o
    un'immagine non hanno un `background-color`, e si finirebbe per misurare
    il bianco della pagina dietro.
    """
    from PIL import Image
    im = Image.open(io.BytesIO(png)).convert('RGB')
    w, h = im.size
    if w < 8 or h < 6:
        return None
    y = 2 if h > 6 else 1
    punti = [im.getpixel((x, y)) for x in range(3, w - 3, max(1, (w - 6) // 12))]
    if not punti:
        return None
    punti.sort(key=lambda c: c[0] + c[1] + c[2])
    return punti[len(punti) // 2]      # il mediano: ignora i casi strani


OK = 0
KO = []


def check(nome, cond, extra=''):
    global OK
    if cond:
        OK += 1
        print('  [OK] %s' % nome)
    else:
        KO.append(nome)
        print('  [KO] %s %s' % (nome, extra))


def guarda_comandi(pg, dove):
    """Passa sopra ogni comando visibile e torna quelli che spariscono."""
    guasti = []
    comandi = pg.locator('button:visible, a.tab-item:visible, .vista-btn:visible')
    for i in range(min(comandi.count(), 40)):
        el = comandi.nth(i)
        try:
            if not el.is_visible():
                continue
            el.hover(timeout=1200)
            # Le animazioni durano: misurare subito coglie i colori a meta'
            # strada e fa gridare a difetti che non ci sono.
            pg.wait_for_timeout(450)
            info = el.evaluate(COLORE_TESTO)
            if not info['etichetta']:
                continue
            # Un comando spento e' sbiadito apposta: e' cosi' che si dice
            # "non ancora". Segnalarlo vuol dire lamentarsi di un segnale
            # che funziona.
            if info['spento']:
                continue
            sfondo = _sfondo_dai_pixel(el.screenshot(timeout=3000))
        except Exception:
            continue
        if sfondo is None:
            continue
        testo = _rgb(info['testo'])
        c = _contrasto(testo, sfondo)
        if c < SOGLIA:
            guasti.append('%s "%s" (%.2f, testo rgb%s su sfondo rgb%s)' % (
                dove, info['etichetta'], c, testo, sfondo))
    return guasti


def main():
    global OK
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(BASE + '/api/health', timeout=4) as r:
            json.load(r)
    except Exception as e:
        print('Server non raggiungibile su %s: %s' % (BASE, str(e)[:60]))
        print('Avvia un\'istanza di prova e riesegui. Test saltato.')
        return 0

    print('Comandi che spariscono al passaggio del mouse\n')
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        for pagina, utente in PAGINE:
            pg = b.new_page(viewport={'width': 1500, 'height': 950})
            guasti = []
            try:
                pg.goto(BASE + '/login.html')
                pg.evaluate('u => localStorage.setItem("currentUser", JSON.stringify(u))',
                            utente)
                pg.goto(BASE + '/' + pagina)
                pg.wait_for_load_state('networkidle', timeout=20000)
                pg.wait_for_timeout(1500)

                guasti += guarda_comandi(pg, pagina)

                # Anche dentro le schede: molti comandi stanno li' e non si
                # vedrebbero mai fermandosi alla prima videata.
                schede = pg.locator('.tab-item:visible, .tab:visible')
                for k in range(min(schede.count(), 8)):
                    try:
                        schede.nth(k).click(timeout=1500)
                        pg.wait_for_timeout(1200)
                        guasti += guarda_comandi(pg, '%s/%s' % (
                            pagina, schede.nth(k).inner_text().strip()[:16]))
                    except Exception:
                        continue
            except Exception as e:
                check('%s si apre' % pagina, False, str(e)[:70])
                pg.close()
                continue
            finally:
                try:
                    pg.close()
                except Exception:
                    pass

            check('%-22s nessun comando sparisce' % pagina, not guasti,
                  '\n        ' + '\n        '.join(guasti[:4]))
        b.close()

    print('\n' + '=' * 60)
    print('PASSATI: %d   FALLITI: %d' % (OK, len(KO)))
    if KO:
        print('\nUn comando che non si legge e\' un comando che non si preme.')
        print('Quasi sempre e\' una regola :hover generica scritta DOPO quella')
        print('della variante colorata: a parita\' di priorita\' vince l\'ultima,')
        print('e schiarisce lo sfondo lasciando il testo bianco.')
    print('=' * 60)
    return 1 if KO else 0


if __name__ == '__main__':
    sys.exit(main())
