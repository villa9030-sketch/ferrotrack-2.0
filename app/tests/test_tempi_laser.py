"""Tempi di taglio laser per la saturazione (backend/preventivi/tempi_laser.py).

Copre:
 1. codice in Lantek col tempo CAM attuale -> fonte 'lantek', tempo di Lantek
 2. tempo CAM con le tabelle vecchie, o per un altro materiale/spessore -> 'modello'
 3. riga senza disegno: geometria, materiale e spessore presi da Lantek
 4. Lantek spento (errore) -> modello, nessuna eccezione, niente nuovi tentativi per un po'
 5. cache: lo stesso codice non si rilegge
 6. modello: interpolazione tra spessori, materiale sconosciuto -> 'stima' con incertezza larga
 7. tempo_ordine: fattore foglio, lamiere per eccesso, 5 min a lamiera, 3 s a pezzo
 8. taratura (tools/calibra_tempi_laser.py) su dati sintetici: ritrova i coefficienti
 9. JSON del modello presente e coerente; solo SELECT verso Lantek

Lantek e' SIMULATO (lettore finto): i test non toccano il database di Lantek
e non scrivono niente. Esecuzione: python app/tests/test_tempi_laser.py
"""
import json
import os
import re
import sys

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
for p in (_APP, os.path.join(_APP, 'tools')):
    if p not in sys.path:
        sys.path.insert(0, p)
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from backend.preventivi import tempi_laser as T  # noqa: E402

FALLITI = []


def check(cond, msg):
    print(('  ok   ' if cond else '  FAIL ') + msg)
    if not cond:
        FALLITI.append(msg)


MODELLO = {
    'valido_dal': '2022-11-01',
    'materiale_simile': {'ZINCATO': 'FERRO'},
    'gruppi': {
        'FERRO|2': {'a': 5.0, 'b': 2.4, 'c': 21.0, 'd': 0.0, 'p10': 0.97, 'p90': 1.02},
        'FERRO|4': {'a': 18.0, 'b': 4.3, 'c': 22.0, 'd': 0.0, 'p10': 0.99, 'p90': 1.01},
        'INOX|2': {'a': 3.0, 'b': 2.35, 'c': 33.0, 'd': 0.0, 'p10': 0.96, 'p90': 1.02},
    },
    'fattore_foglio': {'FERRO': 1.03, '*': 1.05},
    'lamiere': {'FERRO|2': {'area_m2': 4.5, 'resa': 0.5}, '*': {'area_m2': 2.0, 'resa': 0.6}},
}

LANTEK = {
    'NUOVO-01': {'cam': [{'mat': 'FERRO', 'sp': 2.0, 'data': '2025-03-01', 't': 61.5, 'N': 3}],
                 'geo': {'mat': 'FERRO', 'sp': 2.0, 'L': 4.0, 'M': 0.0, 'area_dm2': 90.0}},
    'VECCHIO-01': {'cam': [{'mat': 'FERRO', 'sp': 2.0, 'data': '2022-04-20', 't': 999.0, 'N': 5}],
                   'geo': {'mat': 'FERRO', 'sp': 2.0, 'L': 3.0, 'M': 0.0, 'area_dm2': 50.0}},
}


class Lettore:
    """Lantek finto: conta le letture."""
    def __init__(self, dati=None, errore=None):
        self.dati, self.errore, self.chiamate = dati or {}, errore, []

    def __call__(self, codici):
        self.chiamate.append(list(codici))
        if self.errore:
            raise self.errore
        return {c: self.dati[c] for c in codici if c in self.dati}


def t_fonti():
    print('1-3. fonti del tempo')
    T.svuota_cache()
    lt = Lettore(LANTEK)
    r = T.tempo_pezzo('nuovo-01', {'perimetro_taglio_m': 4, 'n_forature': 3, 'materiale': 'S235', 'spessore_mm': 2},
                      modello=MODELLO, lettore=lt)
    check(r['fonte'] == 'lantek' and r['secondi'] == 61.5 and r['incertezza'] == 0.0, f'CAM attuale -> lantek {r}')
    r = T.tempo_pezzo('VECCHIO-01', {'perimetro_taglio_m': 3, 'n_forature': 5, 'materiale': 'FERRO', 'spessore_mm': 2},
                      modello=MODELLO, lettore=lt)
    check(r['fonte'] == 'modello' and abs(r['secondi'] - (5 * 3 + 2.4 * 5)) < 0.2 and 'vecchie' in r['nota'],
          f'CAM con tabelle vecchie -> modello {r}')
    r = T.tempo_pezzo('NUOVO-01', {'perimetro_taglio_m': 4, 'n_forature': 3, 'materiale': 'FERRO', 'spessore_mm': 4},
                      modello=MODELLO, lettore=lt)
    check(r['fonte'] == 'modello' and abs(r['secondi'] - (18 * 4 + 4.3 * 3)) < 0.2, f'altro spessore -> modello {r}')
    # riga senza disegno ne' materiale: tutto da Lantek
    r = T.tempo_pezzo('VECCHIO-01', {}, modello=MODELLO, lettore=lt)
    check(r['fonte'] == 'modello' and abs(r['secondi'] - (5 * 3 + 2.4 * 5)) < 0.2 and r['materiale'] == 'FERRO',
          f'geometria di Lantek {r}')
    r = T.tempo_pezzo('SCONOSCIUTO', {}, modello=MODELLO, lettore=lt)
    check(r['fonte'] == 'mancante' and r['secondi'] is None, f'niente dati -> mancante {r}')


def t_lantek_spento_e_cache():
    print('4-5. Lantek spento, cache')
    T.svuota_cache()
    lt = Lettore(errore=RuntimeError('SQL Server non risponde'))
    art = {'perimetro_taglio_m': 2, 'n_forature': 1, 'materiale': 'INOX_304', 'spessore_mm': 2}
    r = T.tempo_pezzo('NUOVO-01', art, modello=MODELLO, lettore=lt)
    check(r['fonte'] == 'modello' and abs(r['secondi'] - (3 * 2 + 2.35)) < 0.2, f'Lantek spento -> modello {r}')
    T.tempo_pezzo('ALTRO', art, modello=MODELLO, lettore=lt)
    check(len(lt.chiamate) == 1, 'dopo un errore non si riprova subito (%d letture)' % len(lt.chiamate))
    T.svuota_cache()
    lt = Lettore(LANTEK)
    T.tempo_pezzo('NUOVO-01', art, modello=MODELLO, lettore=lt)
    T.tempo_pezzo('nuovo-01', art, modello=MODELLO, lettore=lt)
    T.tempo_pezzo('SCONOSCIUTO', art, modello=MODELLO, lettore=lt)
    T.tempo_pezzo('SCONOSCIUTO', art, modello=MODELLO, lettore=lt)
    check(len(lt.chiamate) == 2, 'cache: codici trovati e non trovati letti una volta (%d letture)' % len(lt.chiamate))
    r = T.tempo_pezzo('NUOVO-01', art, usa_lantek=False, modello=MODELLO, lettore=lt)
    check(r['fonte'] == 'modello' and len(lt.chiamate) == 2, 'usa_lantek=False non legge Lantek')
    T.svuota_cache()


def t_modello():
    print('6. modello')
    r = T.predici(MODELLO, 'S235', 3, 2.0, 2)
    atteso = 0.5 * (5 * 2 + 2.4 * 2) + 0.5 * (18 * 2 + 4.3 * 2)
    check(r['fonte'] == 'modello' and abs(r['secondi'] - atteso) < 0.2, f'interpolazione 2-4 mm {r} atteso {atteso}')
    r = T.predici(MODELLO, 'OTTONE', 2, 2.0, 2)
    check(r['fonte'] == 'stima' and r['incertezza'] >= 0.35, f'materiale sconosciuto -> stima {r}')
    r = T.predici(MODELLO, 'ZINCATO', 2, 2.0, 2)
    check(r['fonte'] == 'stima' and abs(r['secondi'] - (5 * 2 + 2.4 * 2)) < 0.2, f'zincato senza dati -> come ferro {r}')
    r = T.predici(MODELLO, 'FERRO', 8, 2.0, 2)
    check(r['fonte'] == 'stima' and r['secondi'] > 18 * 2, f'fuori dai dati -> stima scalata {r}')
    check(T.predici(MODELLO, 'FERRO', 2, 0, 2) is None and T.predici(MODELLO, 'FERRO', 0, 1, 2) is None,
          'senza perimetro o spessore -> None')
    check([T.materiale_lantek(m) for m in ('S235JR', 'INOX_316L', 'ALU_5754', 'GEN_ZINCATO', 'ALLUMINIO', 'boh')]
          == ['FERRO', 'INOX', 'ALLUMINIO', 'ZINCATO', 'ALLUMINIO', None], 'materiali -> famiglie di Lantek')


def t_ordine():
    print('7. tempo_ordine')
    T.svuota_cache()
    lt = Lettore(LANTEK)
    righe = [
        {'codice': 'NUOVO-01', 'quantita': 10, 'materiale': 'S235', 'spessore_mm': 2, 'area_dm2': 90},
        {'codice': 'NUOVO-02', 'quantita': 4, 'materiale': 'S235', 'spessore_mm': 2, 'area_dm2': 100,
         'perimetro_taglio_m': 2, 'n_forature': 1},
        {'codice': 'X-INOX', 'quantita': 1, 'materiale': 'INOX_304', 'spessore_mm': 2, 'area_dm2': 10,
         'perimetro_taglio_m': 1, 'n_forature': 1},
        {'codice': 'SENZA', 'quantita': 2},
    ]
    o = T.tempo_ordine(righe, modello=MODELLO, lettore=lt)
    t_ferro = (61.5 * 10 + (5 * 2 + 2.4) * 4) * 1.03
    t_inox = (3 * 1 + 2.35) * 1.05
    check(abs(o['secondi_taglio'] - (t_ferro + t_inox)) < 0.5, f"taglio x fattore foglio {o['secondi_taglio']} vs {t_ferro + t_inox:.1f}")
    # ferro: (9 + 4) m2 / (4.5 x 0.5) = 5.78 -> 6 fogli; inox: 0.1 / 1.2 -> 1 foglio
    check(o['lamiere'] == 7 and abs(o['secondi_carico'] - 7 * 5 * 60) < 0.1, f"lamiere per eccesso {o['lamiere']}")
    check(abs(o['secondi_scarico'] - 15 * 3) < 0.1 and o['mancanti'] == 2 and o['pezzi'] == 17,
          f"scarico solo pezzi col tempo {o['secondi_scarico']}, mancanti {o['mancanti']}")
    check(abs(o['secondi'] - (o['secondi_taglio'] + o['secondi_carico'] + o['secondi_scarico'])) < 0.2, 'totale = somma')
    check(o['fonti'] == {'lantek': 10, 'modello': 5, 'mancante': 2}, f"conteggio fonti {o['fonti']}")
    check(len(lt.chiamate) == 1, 'una sola lettura di Lantek per tutto l\'ordine')
    f = T.tempo_ordine(righe, modello=MODELLO, lettore=lt, lamiere_intere=False)
    check(abs(f['lamiere'] - (13 / 2.25 + 0.1 / 1.2)) < 0.02, f"lamiere frazionarie {f['lamiere']}")
    c = T.tempo_ordine(righe, modello=MODELLO, lettore=lt, carico_min_lamiera=0, scarico_s_pezzo=0)
    check(abs(c['secondi'] - c['secondi_taglio']) < 0.1, 'carico e scarico configurabili')
    v = T.tempo_ordine([], modello=MODELLO, lettore=lt)
    check(v['secondi'] == 0 and v['lamiere'] == 0, 'ordine vuoto -> 0')
    T.svuota_cache()


def t_taratura():
    print('8. taratura su dati sintetici')
    import random
    import calibra_tempi_laser as C
    rnd = random.Random(3)
    parti = []
    for i in range(300):
        L, N = rnd.uniform(0.3, 8), rnd.randint(1, 30)
        t = 6.0 * L + 3.3 * N + 0.5
        if i % 25 == 0:
            t *= 3  # pezzi anomali: la regressione robusta non li deve seguire
        parti.append({'codice': 'P%d' % i, 'mat': 'INOX', 'sp': 3.0, 'data': '2024-01-01', 't': t, 'L': L, 'N': N, 'M': 0.0})
    parti += [{'codice': 'V%d' % i, 'mat': 'INOX', 'sp': 3.0, 'data': '2022-04-20', 't': 1000.0, 'L': 1.0, 'N': 1, 'M': 0.0}
              for i in range(100)]
    parti += [{'codice': 'R%d' % i, 'mat': 'FERRO', 'sp': 9.0, 'data': '2024-01-01', 't': 10.0, 'L': 1.0, 'N': 1, 'M': 0.0}
              for i in range(5)]
    m = C.calibra(parti)
    g = m['gruppi'].get('INOX|3') or {}
    check(abs(g.get('a', 0) - 6.0) < 0.1 and abs(g.get('b', 0) - 3.3) < 0.1, f'coefficienti ritrovati {g}')
    check(m['n_pezzi'] == 305 and g.get('n') == 300, 'esclusi i tempi con le tabelle vecchie')
    check('FERRO|9' not in m['gruppi'], 'gruppi con pochi pezzi non tarati')
    fogli = [{'mat': 'INOX', 'etime': 1.1 * (6.0 * 2 + 3.3 * 2 + 0.5) * 2, 'fogli': 3, 'pezzi': [('P1', 2.0)]}]
    parti[1].update(L=2.0, N=2, t=6.0 * 2 + 3.3 * 2 + 0.5)
    ff = C.tara_fattore_foglio(fogli, parti)
    check(abs(ff.get('INOX', 0) - 1.1) < 0.01, f'fattore foglio {ff}')
    lam = C.tara_lamiere([{'mat': 'FERRO', 'sp': 2.0, 'area_m2': 4.5, 'usata_m2': 3.0, 'fogli': 1}] * 12)
    check(abs(lam['FERRO|2']['resa'] - 0.667) < 0.01 and lam['FERRO|2']['area_m2'] == 4.5, f'lamiere {lam.get("FERRO|2")}')


def t_file():
    print('9. JSON e sola lettura')
    m = T.carica_modello()
    check(m is not None and len(m.get('gruppi') or {}) >= 10, 'tempi_laser_modello.json presente con i gruppi')
    if m:
        check(all(g['a'] > 0 and g['b'] >= 0 for g in m['gruppi'].values()), 'coefficienti positivi')
        check(m.get('valido_dal') and m.get('lamiere') and m.get('fattore_foglio'), 'valido_dal, lamiere, fattore_foglio')
        r = T.predici(m, 'S235', 3, 2.0, 4)
        check(r and 5 < r['secondi'] < 60, f'pezzo tipo FERRO 3 mm: {r}')
    for nome in (os.path.join(_APP, 'backend', 'preventivi', 'tempi_laser.py'),
                 os.path.join(_APP, 'tools', 'calibra_tempi_laser.py')):
        testo = open(nome, encoding='utf-8').read()
        sql = re.findall(r"cur\.execute\(\s*['\"](\w+)", testo)
        check(sql and all(s.upper() == 'SELECT' for s in sql), f'{os.path.basename(nome)}: solo SELECT ({sql})')
        check(not re.search(r'\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|EXEC)\s', testo), f'{os.path.basename(nome)}: niente scritture')


if __name__ == '__main__':
    for f in (t_fonti, t_lantek_spento_e_cache, t_modello, t_ordine, t_taratura, t_file):
        f()
    print('\n%s' % ('TUTTO OK' if not FALLITI else '%d FALLITI' % len(FALLITI)))
    sys.exit(1 if FALLITI else 0)
