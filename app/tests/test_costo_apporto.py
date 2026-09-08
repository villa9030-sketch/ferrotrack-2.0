"""Test della COERENZA DEI CAMPI ECONOMICI (sezione 10.5 e criterio 4).

Il costo del materiale d'apporto (filo + gas) veniva scritto dal frontend in
`costo_mat_apporto`, mentre tutte le somme e la colonna sul database usano
`costo_apporto`. Risultato: il costo veniva calcolato e poi perso, in editor,
in PDF e nell'ordine.

Copre:
 1. il calcolatore include il costo d'apporto nel totale
 2. accetta anche il vecchio nome, per i dati gia' in giro
 3. i campi economici sopravvivono a salvataggio e riapertura
 4. lo stesso articolo da' lo stesso totale nel calcolatore e nella
    composizione usata per il PDF

Gira su DATABASE TEMPORANEO. Esecuzione: python app/tests/test_costo_apporto.py
"""
import os
import sys
import tempfile
import uuid

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Preventivo  # noqa: E402
from backend import models_ore  # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), f'test_app_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend.database import PreventivoManager  # noqa: E402
from backend.preventivi.cost_calculator import calcola_preventivo  # noqa: E402

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


CONFIG = {
    'costo_singola_piega': 1.0, 'costo_setup_piega': 0.0, 'soglia_setup_pieghe': 5,
    'costo_saldatura_metro': 10.0, 'costo_filettatura': 1.0, 'costo_svasatura': 1.0,
    'costo_materiale_apporto_metro': 4.0, 'costo_pulizia_saldatura_metro': 2.0,
    'tariffa_oraria': 45.0, 'costi_generali_pct': 0, 'costo_movimentazione': 0,
}


def articolo(nome_campo_apporto):
    """Articolo con 100 EUR di base e 25 EUR di apporto, sotto il nome indicato."""
    a = {'codice': 'PZ-1', 'costo': 100.0, 'quantita': 1,
         'costo_piega': 10.0, 'costo_saldatura': 20.0,
         'costo_filettatura': 0.0, 'costo_svasatura': 0.0,
         'costo_pulizia': 5.0}
    a[nome_campo_apporto] = 25.0
    return a


def calcola(art):
    # ATTENZIONE: `calcola_preventivo` oggi non e' collegata all'applicazione —
    # il calcolo vero avviene nel JavaScript e il backend salva quello che
    # riceve. La si prova comunque perche' e' il candidato naturale a diventare
    # il calcolo autorevole richiesto dalla sezione 10.5.
    # Il costo base NON viene sommato dagli articoli: va passato a parte.
    return calcola_preventivo([art], CONFIG, quantita=1, margine=0,
                              costi_montaggio={}, tubolari_per_assieme={},
                              piastre_per_assieme={},
                              base=float(art.get('costo') or 0))


def main():
    s = models.SessionLocal()
    try:
        s.add(User(id='commerciale', name='Marco', role='Commerciale', is_active=True))
        s.commit()
    finally:
        s.close()

    # =====================================================================
    print('\n1) Il costo d apporto entra nel totale')
    senza = calcola({**articolo('costo_apporto'), 'costo_apporto': 0.0})
    con = calcola(articolo('costo_apporto'))
    check('senza apporto: 135,00', round(senza['totale_pezzo'], 2) == 135.0,
          senza['totale_pezzo'])
    check('con 25 di apporto: 160,00', round(con['totale_pezzo'], 2) == 160.0,
          con['totale_pezzo'])
    check('la differenza e esattamente l apporto',
          round(con['totale_pezzo'] - senza['totale_pezzo'], 2) == 25.0)
    check('e compare come voce a se', round(con['costo_mat_apporto'], 2) == 25.0,
          con['costo_mat_apporto'])

    # =====================================================================
    print('\n2) Il vecchio nome continua a funzionare')
    vecchio = calcola(articolo('costo_mat_apporto'))
    check('stesso totale col nome vecchio',
          round(vecchio['totale_pezzo'], 2) == round(con['totale_pezzo'], 2),
          (vecchio['totale_pezzo'], con['totale_pezzo']))

    print('\n   e il nome nuovo ha la precedenza se ci sono entrambi')
    doppio = articolo('costo_apporto')
    doppio['costo_mat_apporto'] = 999.0
    r = calcola(doppio)
    check('vince costo_apporto (160, non 1134)',
          round(r['totale_pezzo'], 2) == 160.0, r['totale_pezzo'])

    # =====================================================================
    print('\n3) I campi economici sopravvivono al salvataggio')
    pid = PreventivoManager.create('Cliente Alfa', 'commerciale', quantita=2)
    pid = pid['id'] if isinstance(pid, dict) else pid
    originale = {
        'codice': 'PZ-1', 'quantita': 2, 'costo_base_stimato': 100.0,
        'costo_piega': 10.0, 'costo_saldatura': 20.0, 'costo_filettatura': 3.0,
        'costo_svasatura': 4.0, 'costo_apporto': 25.0, 'costo_pulizia': 5.0,
    }
    PreventivoManager.replace_articoli(pid, [originale])
    letto = PreventivoManager.get(pid)
    art = (letto.get('articoli') or [{}])[0]
    campi = ['costo_piega', 'costo_saldatura', 'costo_filettatura',
             'costo_svasatura', 'costo_apporto', 'costo_pulizia']
    diversi = {c: (originale[c], art.get(c)) for c in campi
               if round(float(art.get(c) or 0), 2) != round(originale[c], 2)}
    check('tutti i costi rileggono uguali', not diversi, diversi)
    check('in particolare il costo d apporto (25,00)',
          round(float(art.get('costo_apporto') or 0), 2) == 25.0,
          art.get('costo_apporto'))
    check('la quantita non si perde', art.get('quantita') == 2, art.get('quantita'))

    # =====================================================================
    print('\n4) Stesso totale nel calcolatore e nella composizione per il PDF')
    art_letto = {
        'codice': art.get('codice'), 'costo': float(art.get('costo_base_stimato') or 0),
        'quantita': art.get('quantita'),
        'costo_piega': float(art.get('costo_piega') or 0),
        'costo_saldatura': float(art.get('costo_saldatura') or 0),
        'costo_filettatura': float(art.get('costo_filettatura') or 0),
        'costo_svasatura': float(art.get('costo_svasatura') or 0),
        'costo_apporto': float(art.get('costo_apporto') or 0),
        'costo_pulizia': float(art.get('costo_pulizia') or 0),
    }
    calcolato = calcola(art_letto)['totale_pezzo']

    # Stessa somma che app.py compone per il PDF
    base_pdf = (art.get('costo_base_override')
                if art.get('costo_base_override') is not None
                else (art.get('costo_base_stimato') or 0))
    totale_pdf = (float(base_pdf or 0)
                  + float(art.get('costo_piega') or 0)
                  + float(art.get('costo_saldatura') or 0)
                  + float(art.get('costo_filettatura') or 0)
                  + float(art.get('costo_svasatura') or 0)
                  + float(art.get('costo_apporto') or 0)
                  + float(art.get('costo_pulizia') or 0))
    check('calcolatore e composizione PDF danno lo stesso numero',
          round(calcolato, 2) == round(totale_pdf, 2), (calcolato, totale_pdf))
    check('e vale 167,00 (100+10+20+3+4+25+5)',
          round(totale_pdf, 2) == 167.0, totale_pdf)

    print('\n' + '=' * 60)
    print(f'PASSATI: {OK}   FALLITI: {len(KO)}')
    if KO:
        print('Falliti: ' + ', '.join(KO))
    print('=' * 60)
    return 1 if KO else 0


if __name__ == '__main__':
    code = main()
    try:
        _ENG.dispose()
        os.remove(_TMP)
    except Exception:
        pass
    sys.exit(code)
