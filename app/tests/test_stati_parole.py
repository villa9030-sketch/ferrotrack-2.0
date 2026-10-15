"""Le parole degli stati: le stesse in ogni pagina (backend/stati.py).

Il 13/10/2026 lo stesso ordine era "Da importare" nell'ufficio, "Pronto per
Lantek" al laser e "Da tagliare" dal capo officina; il calendario dell'ufficio
diceva "Finito" a un ordine con la lavorazione finita ma non consegnato, e il
capo officina prometteva "Da fatturare" (nell'ufficio e' "DDT da fare").

Copre:
 1. la tabella: una voce per ogni fase laser e ogni passo dell'ufficio
    (le stesse chiavi di ordini_service), niente "Finito"
 2. /ft-stati.js la serve al browser, generata dalla stessa tabella
 3. le pagine la includono e non hanno piu' le parole vecchie scritte a mano

Esecuzione: python app/tests/test_stati_parole.py
"""
import json
import os
import re
import sys
import tempfile

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
os.environ.setdefault('FERROTRACK_DB', os.path.join(tempfile.mkdtemp(prefix='ft_st_'), 'x.db'))

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from backend import stati as ST  # noqa: E402
from backend import ordini_service as OS  # noqa: E402

OK, KO = 0, []


def check(nome, cond, extra=''):
    global OK
    if cond:
        OK += 1
        print(f'  [OK] {nome}')
    else:
        KO.append(nome)
        print(f'  [KO] {nome}  {extra}')


class _O:
    def __init__(self, **k):
        self.__dict__.update({'taglio_completato': False, 'taglio_richiesto': None, 'importato_lantek_il': None,
                              'status': 'RICEVUTO', 'data_consegna_effettiva': None,
                              'data_completamento_operativo': None, 'ddt_numero': None, 'numero_ddt': None,
                              'consegna_parziale': False})
        self.__dict__.update(k)


print('1) La tabella')
fasi = {OS.fase_laser(_O()), OS.fase_laser(_O(taglio_richiesto=True)),
        OS.fase_laser(_O(taglio_richiesto=True, importato_lantek_il=1)),
        OS.fase_laser(_O(taglio_completato=True)), OS.fase_laser(_O(taglio_richiesto=False))}
check('una parola per ogni fase del laser', fasi == set(ST.LASER), (fasi, set(ST.LASER)))
check('le stesse fasi hanno la spiegazione', set(ST.LASER_SPIEGA) == set(ST.LASER))
passi = {OS.passo(_O()), OS.passo(_O(data_completamento_operativo=1)), OS.passo(_O(data_completamento_operativo=1, ddt_numero='5')),
         OS.passo(_O(data_consegna_effettiva=1, consegna_parziale=True)), OS.passo(_O(data_consegna_effettiva=1)),
         OS.passo(_O(status='CHIUSO'))}
check('una parola per ogni passo dell\'ufficio', passi == set(ST.PASSO), (passi, set(ST.PASSO)))
check('le frasi del passo dell\'ufficio vengono dalla stessa tabella', OS.ETICHETTE_PASSO is ST.PASSO_SPIEGA)
check('"Da mandare a Lantek" e "DDT da fare"', ST.LASER['da_importare'] == 'Da mandare a Lantek'
      and ST.PASSO['attesa_ddt'] == 'DDT da fare')
tutte = list(ST.LASER.values()) + list(ST.PASSO.values())
check('niente "Finito" da solo', not any(v.strip().lower() == 'finito' for v in tutte))
check('DA_FATTURARE (storico) = lavorazione finita, non "da fatturare"', ST.DA_STATUS['DA_FATTURARE'] == 'attesa_ddt')

print('\n2) /ft-stati.js')
from backend.app import app  # noqa: E402
r = app.test_client().get('/ft-stati.js')
testo = r.get_data(as_text=True)
check('servito come JavaScript', r.status_code == 200 and 'javascript' in r.mimetype, r.status_code)
m = re.search(r'window\.FTStati = (\{.*?\});\n', testo)
dati = json.loads(m.group(1)) if m else {}
check('stessa tabella del server', dati.get('laser') == ST.LASER and dati.get('passo') == ST.PASSO)
check('con la regola della fase laser', 'FTStati.faseLaser = function' in testo)

print('\n3) Le pagine')
fe = os.path.join(_APP, 'frontend')


def leggi(n):
    return open(os.path.join(fe, n), encoding='utf-8').read()


for pag in ('impiegata.html', 'laser.html', 'capo-officina.html'):
    check(f'{pag} include /ft-stati.js', '<script src="/ft-stati.js"></script>' in leggi(pag))
vecchie = {
    'impiegata.html': ["'Finito'", 'Da importare', 'In coda laser', "'Laser: da importare in Lantek'"],
    'laser-base.js': ["'Pronto per Lantek'", "t: 'Da smistare'", "t: 'Tagliato'"],
    'laser-ordine.js': ["t: 'Pronto per Lantek'"],
    'capo-officina.html': ['Da tagliare', 'Senza taglio', 'passerà a "Da fatturare"', '${escapeHtml(ord.status||\'\')}'],
}
for pag, parole in vecchie.items():
    t = leggi(pag)
    rimaste = [p for p in parole if p in t]
    check(f'{pag}: niente parole vecchie scritte a mano', not rimaste, rimaste)

print(f'\n{OK} ok, {len(KO)} ko')
if KO:
    print('FALLITI:', *KO, sep='\n  ')
sys.exit(1 if KO else 0)
