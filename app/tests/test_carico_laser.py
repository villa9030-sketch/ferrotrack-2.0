"""Carico del laser e ritardi: gli STESSI numeri all'ufficio e al laser.

Il 13/10/2026, sullo stesso database, l'ufficio diceva "In ritardo: 0 ·
Laser 0 ore su 60" e il laser "3 in ritardo · 5 h di laser". Due difetti:
  - "in ritardo" voleva dire la consegna all'ufficio e il "taglia entro" al
    laser, con la stessa parola;
  - il lavoro non tagliato col "taglia entro" gia' passato pesava su un giorno
    passato: nei prossimi giorni dell'ufficio non c'era (0 ore).
Ora il conto e' uno (backend/carico_laser.py, mandato da /api/laser/banco) e
le due pagine lo leggono con le stesse parole.

Copre:
 1. funzioni pure: lavoro arretrato su oggi, tagliato sul giorno del taglio,
    "in ritardo" = consegna passata e non consegnato, periodo = prossimi 5
    giorni lavorativi, lamiere (stessa disposizione di prima)
 2. API /api/laser/banco: durate, giorni_carico, riepilogo coi due ritardi
 3. le pagine non contano piu' per conto loro (niente disposizione lamiere
    nel browser, entrambe leggono CaricoLaser.riepilogo)

Gira su DATABASE TEMPORANEO, impostazioni temporanee, LANTEK SIMULATO.
Esecuzione: python app/tests/test_carico_laser.py
"""
import json
import os
import sys
import tempfile
import uuid
from datetime import date, datetime, timedelta

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
os.environ['FERROTRACK_ESEMPI_DIR'] = tempfile.mkdtemp(prefix='ft_cl_esempi_')
_CFG = os.path.join(tempfile.mkdtemp(prefix='ft_cl_cfg_'), 'app_config.json')
with open(_CFG, 'w', encoding='utf-8') as f:
    json.dump({'laser_calendario': {'giorni': [1, 2, 3, 4, 5], 'eccezioni': {}}}, f)
os.environ['FERROTRACK_CONFIG'] = _CFG

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, Preventivo, Order  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_cl_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend import lantek as L  # noqa: E402
from backend import carico_laser as CL  # noqa: E402
from backend.database import ConfigManager, PreventivoManager  # noqa: E402
from backend.orario import oggi_locale  # noqa: E402
from backend.preventivi import tempi_laser as TL  # noqa: E402

ConfigManager.load_config()
assert os.path.abspath(ConfigManager._CONFIG_PATH) == os.path.abspath(_CFG), 'impostazioni NON temporanee: fermo'
_CARTELLE = tempfile.mkdtemp(prefix='ft_cl_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)

TL._leggi_lantek = lambda codici: None
L.pezzi_in_lantek = lambda codici: {'disponibile': False, 'errore': 'prova', 'pezzi': {}}
L.importa_pezzi_dxf = lambda *a, **k: (_ for _ in ()).throw(AssertionError('Lantek non si lancia nei test'))
L.importa_xml = lambda *a, **k: (_ for _ in ()).throw(AssertionError('Lantek non si lancia nei test'))

OK = 0
KO = []


def check(nome, cond, extra=''):
    global OK
    if cond:
        OK += 1
        print(f'  [OK] {nome}')
    else:
        KO.append(nome)
        print(f'  [KO] {nome}  {extra}')


CAL = {'giorni': [1, 2, 3, 4, 5], 'eccezioni': {}, 'ore_turno': 8, 'ore_riserva': 2,
       'carico_min_lamiera': 5, 'scarico_s_pezzo': 3}


def pure():
    print('1) Funzioni pure')
    lun = date(2026, 10, 12)
    g, arr = CL.giorno_carico(False, None, '2026-09-30', date(2026, 10, 14), lun)
    check('da tagliare col taglia entro passato: pesa su OGGI ed e\' arretrato', g == lun and arr, (g, arr))
    g, arr = CL.giorno_carico(False, None, '2026-10-15', date(2026, 10, 20), lun)
    check('taglia entro futuro: quel giorno', g == date(2026, 10, 15) and not arr, (g, arr))
    g, arr = CL.giorno_carico(False, None, None, date(2026, 10, 20), lun)
    check('senza taglia entro: la consegna', g == date(2026, 10, 20))
    g, arr = CL.giorno_carico(True, date(2026, 10, 1), '2026-09-30', None, lun)
    check('tagliato: il giorno del taglio, mai arretrato', g == date(2026, 10, 1) and not arr)
    check('in ritardo: consegna passata e non consegnato',
          CL.in_ritardo('aperto', date(2026, 10, 9), lun) and CL.in_ritardo('pronto_ddt', datetime(2026, 10, 9), lun))
    check('consegnato o consegna oggi: non in ritardo',
          not CL.in_ritardo('consegnato', date(2026, 10, 9), lun) and not CL.in_ritardo('aperto', lun, lun)
          and not CL.in_ritardo('aperto', None, lun))
    gl = CL.giorni_lavorativi(date(2026, 10, 16), 5, CAL)     # venerdi
    check('periodo: 5 giorni lavorativi, il fine settimana saltato',
          [x.isoformat() for x in gl] == ['2026-10-16', '2026-10-19', '2026-10-20', '2026-10-21', '2026-10-22'], gl)
    ordini = [{'id': 'a', 'tagliato': False, 'giorno': lun, 'arretrato': True, 'min': 120},
              {'id': 'b', 'tagliato': False, 'giorno': date(2026, 10, 14), 'arretrato': False, 'min': 60},
              {'id': 'c', 'tagliato': False, 'giorno': date(2026, 11, 30), 'arretrato': False, 'min': 999},
              {'id': 'd', 'tagliato': False, 'giorno': lun, 'arretrato': False, 'min': None}]
    r = CL.riepilogo(ordini, CAL, lun, ['x'])
    p = r['periodo']
    check('riepilogo: ore del periodo (arretrato compreso, fuori periodo no)', p['min'] == 180 and p['cap'] == 5 * 360
          and p['ignoti'] == 1 and p['n'] == 3, p)
    check('riepilogo: taglio in ritardo con le sue ore', r['taglio_in_ritardo']['n'] == 1
          and r['taglio_in_ritardo']['ids'] == ['a'] and r['taglio_in_ritardo']['min'] == 120, r['taglio_in_ritardo'])
    check('riepilogo: in ritardo (consegna) a parte', r['in_ritardo'] == {
        'n': 1, 'ids': ['x'], 'definizione': 'consegna al cliente passata e merce non ancora consegnata'})
    check('etichetta del periodo uguale per tutti', p['etichetta'] == 'Laser, prossimi 5 giorni lavorativi')
    F = [{'w_mm': 3000, 'h_mm': 1500}]
    P = {'margine_mm': 10, 'distanza_mm': 5}
    check('lamiere: 8 pezzi 700x700 in un 3000x1500 = 1 lamiera',
          CL.lamiere([{'w_mm': 700, 'h_mm': 700, 'quantita': 8}], F, P) == 1)
    check('lamiere: 9 pezzi 700x700 = 2 lamiere', CL.lamiere([{'w_mm': 700, 'h_mm': 700, 'quantita': 9}], F, P) == 2)
    check('lamiere: senza ingombro, 1', CL.lamiere([{'w_mm': None, 'h_mm': None, 'quantita': 3}], F, P) == 1)
    d = CL.durata_ordine([[{'w_mm': 700, 'h_mm': 700, 'quantita': 8, 'tempo_min': 2}]], None, CAL, F, P)
    check('durata: taglio + 1 lamiera da caricare + scarico', abs(d['min'] - (16 + 5 + 8 * 3 / 60)) < 0.01
          and d['fonte'] == 'stima', d)
    check('durata a mano se non ci sono pezzi', CL.durata_ordine([], 90, CAL, F, P) == {'min': 90.0, 'fonte': 'mano'})
    check('durata ignota', CL.durata_ordine([], None, CAL, F, P) == {'min': None, 'fonte': 'ignota'})


def api():
    print('\n2) /api/laser/banco')
    from tests.accesso_aiuto import postazioni, entra, modalita
    modalita('protetto')
    postazioni()
    laser = entra(A.app, 'laser')
    oggi = oggi_locale()
    base = {'quantita': 4, 'materiale': 'S235', 'spessore_mm': 3, 'costo_base_override': 5,
            'perimetro_taglio_m': 0.8, 'n_forature': 3, 'area_dm2': 2.9, 'bbox_w_mm': 200, 'bbox_h_mm': 150}
    pr = PreventivoManager.create('DECA S.r.l.', 'paolo', quantita=1, numero_ordine_cliente='CL-1')
    pid = pr['id'] if isinstance(pr, dict) else pr
    PreventivoManager.replace_articoli(pid, [dict(base, codice='P-1-00')])
    s = models.SessionLocal()
    s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'INVIATO'
    s.commit(); s.close()
    oid = PreventivoManager.accetta_e_crea_ordine(pid, 'paolo').get('order_id')
    A.OrderManager.smista(oid, True, user_id='laser')
    s = models.SessionLocal()
    o = s.query(Order).filter(Order.id == oid).first()
    o.data_consegna = datetime.combine(oggi + timedelta(days=1), datetime.min.time())
    o.data_taglio_pianificata = oggi - timedelta(days=3)       # taglia entro passato
    rit = str(uuid.uuid4())
    s.add(Order(id=rit, cliente='Cliente R', numero_ordine='R-1', taglio_richiesto=False,
                data_consegna=datetime.combine(oggi - timedelta(days=2), datetime.min.time())))
    s.commit(); s.close()

    b = laser.get('/api/laser/banco?da_smistare=1').get_json() or {}
    rp = b.get('riepilogo') or {}
    check('banco: durata dell\'ordine dal server', (b.get('durate') or {}).get(oid, {}).get('fonte') == 'stima'
          and b['durate'][oid]['min'] > 0, b.get('durate'))
    check('banco: il taglio in ritardo pesa su oggi', (b.get('giorni_carico') or {}).get(oid) == oggi.isoformat(),
          b.get('giorni_carico'))
    check('riepilogo: taglio in ritardo = l\'ordine col taglia entro passato',
          rp.get('taglio_in_ritardo', {}).get('ids') == [oid], rp.get('taglio_in_ritardo'))
    check('riepilogo: in ritardo = consegna passata (anche senza laser)',
          rp.get('in_ritardo', {}).get('ids') == [rit], rp.get('in_ritardo'))
    check('riepilogo: le ore del periodo contano il lavoro arretrato',
          abs(rp.get('periodo', {}).get('min', 0) - round(b['durate'][oid]['min'], 1)) < 0.11, rp.get('periodo'))


def pagine():
    print('\n3) Le pagine leggono lo stesso conto')
    fe = os.path.join(_APP, 'frontend')
    leggi = lambda n: open(os.path.join(fe, n), encoding='utf-8').read()  # noqa: E731
    cl = leggi('carico-laser.js')
    check('carico-laser.js non dispone piu\' lamiere da se\'', 'function disponi' not in cl and 'riepilogo' in cl)
    check('ufficio: "In ritardo" dal riepilogo del server', 'CaricoLaser.riepilogo()' in leggi('impiegata.html'))
    check('laser: "Taglio in ritardo" e periodo dal riepilogo del server',
          'CaricoLaser.riepilogo()' in leggi('laser-saturazione.js') and 'Taglio in ritardo' in leggi('laser-lista.js'))
    check('niente piu\' "Questa settimana" al laser', 'Questa settimana' not in leggi('laser-saturazione.js'))


def main():
    pure()
    api()
    pagine()
    print(f'\n{OK} ok, {len(KO)} ko')
    if KO:
        print('FALLITI:', *KO, sep='\n  ')
    return 1 if KO else 0


if __name__ == '__main__':
    code = main()
    try:
        _ENG.dispose()
        os.remove(_TMP)
    except OSError:
        pass
    sys.exit(code)
