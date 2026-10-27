"""Taglia entro (backend/taglia_entro.py): il giorno entro cui tagliare un ordine.

Copre:
 1. la regola: solo taglio 1, piega 2, saldatura/montaggio 4 (vale il piu'
    lungo), + 1 filettature/svasature, + 5 lavorazione esterna; motivo in frase
 2. giorni lavorativi del calendario del laser: fine settimana saltati, giorni
    speciali (fermo / sabato lavorato), giorni della settimana diversi
 3. data scelta a mano (data_taglio_pianificata): e' lei il taglia entro
 4. ordine solo PDF / senza distinta: 1 giorno, e lo dice
 5. numeri in app_config (laser_calendario.anticipo_giorni) e loro controlli
 7. la spunta "Lavorazione esterna" dell'ordine: si' = +5, no = niente +5
    anche se le parole tornano, non detta = deduzione dalle parole col motivo
    "(dedotto dalle note: conferma)"; /lavorazione-esterna e la modifica
    dell'ordine dall'ufficio
 6. API: /api/laser/ordini e /api/orders/<id>/laser col taglia_entro,
    /pianifica-taglio a mano e "torna al calcolo", /api/laser/banco col
    giorno di ogni ordine per la saturazione

Gira su DATABASE TEMPORANEO, impostazioni temporanee, LANTEK SIMULATO.
Esecuzione: python app/tests/test_taglia_entro.py
"""
import json
import os
import sys
import tempfile
import uuid
from datetime import date, datetime

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
os.environ['FERROTRACK_ESEMPI_DIR'] = tempfile.mkdtemp(prefix='ft_te_esempi_')
_CFG = os.path.join(tempfile.mkdtemp(prefix='ft_te_cfg_'), 'app_config.json')
with open(_CFG, 'w', encoding='utf-8') as f:
    json.dump({'laser_calendario': {'giorni': [1, 2, 3, 4, 5], 'eccezioni': {}}}, f)
os.environ['FERROTRACK_CONFIG'] = _CFG

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Preventivo, Order  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_te_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend import lantek as L  # noqa: E402
from backend import taglia_entro as T  # noqa: E402
from backend.database import ConfigManager, PreventivoManager  # noqa: E402
from backend.preventivi import tempi_laser as TL  # noqa: E402

ConfigManager.load_config()
assert os.path.abspath(ConfigManager._CONFIG_PATH) == os.path.abspath(_CFG), 'impostazioni NON temporanee: fermo'
_CARTELLE = tempfile.mkdtemp(prefix='ft_te_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)

# ── Lantek simulato: nessun programma, nessuna lettura vera ─────────────
TL._leggi_lantek = lambda codici: None
L.pezzi_in_lantek = lambda codici: {'disponibile': True, 'errore': None, 'pezzi': {
    c: {'esiste': True, 'codice_lantek': c, 'materiale': 'FERRO', 'spessore': 3.0, 'revisioni': []} for c in codici}}
L.clienti_lantek = lambda: ['DECA']
L.ordini_in_lantek = lambda commessa: {}
L.ordini_fatti_in_lantek = lambda commessa: {}
L.pezzi_dell_ordine = lambda commessa: {}
L.procesos_disponibile = lambda: True
L.materiali_lantek_noti = lambda: {'FERRO'}
L.xmlimporter_disponibile = lambda: True
L.importa_pezzi_dxf = lambda *a, **k: (_ for _ in ()).throw(AssertionError('Lantek non si lancia nei test'))
L.importa_xml = lambda *a, **k: (_ for _ in ()).throw(AssertionError('Lantek non si lancia nei test'))
L.expert_aperto = lambda: False   # Lantek Expert chiuso (sul PC vero puo' essere aperto)

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


CAL = {'giorni': [1, 2, 3, 4, 5], 'eccezioni': {}}
LAM = lambda *lav: {'tipo': 'lamiera', 'codice': 'X', 'lavorazioni': ['Taglio laser', *lav]}  # noqa: E731


def regola():
    print('1) La regola')
    casi = [
        ('solo taglio', [LAM()], 1, ['taglio'], 'solo taglio laser: 1 giorno lavorativo'),
        ('piega', [LAM('Piegatura (3 pieghe)')], 2, ['piega'], 'piegatura: 2 giorni'),
        ('saldatura', [LAM('Saldatura')], 4, ['saldatura'], 'saldatura: 4 giorni'),
        ('piega + saldatura: vale la piu\' lunga', [LAM('Piegatura (2 pieghe)'), LAM('Saldatura')], 4, ['saldatura'], None),
        ('filettatura', [LAM('Filettatura (4)')], 2, ['taglio', 'filettatura'], 'filettature: 2 giorni'),
        ('svasatura', [LAM('Svasatura (2)')], 2, ['taglio', 'filettatura'], 'svasature'),
        ('piega + filettature', [LAM('Piegatura (3 pieghe)', 'Filettatura (2)')], 3, ['piega', 'filettatura'],
         'piegatura + filettature: 3 giorni lavorativi prima della consegna'),
        ('saldatura + filettature + svasature', [LAM('Saldatura', 'Filettatura (1)', 'Svasatura (1)')], 5,
         ['saldatura', 'filettatura'], 'saldatura + filettature e svasature'),
        ('assieme con montaggio', [LAM(), {'tipo': 'assieme', 'codice': 'SA1', 'lavorazioni': ['Montaggio']}], 4,
         ['montaggio'], 'montaggio: 4 giorni'),
        ('assieme con saldatura', [{'tipo': 'assieme', 'codice': 'SA1', 'lavorazioni': ['Saldatura']}], 4, ['saldatura'], None),
        ('puntatura = saldatura', [LAM('Puntatura')], 4, ['saldatura'], None),
        ('tubo con lav. esterna', [LAM(), {'tipo': 'tubolare', 'codice': 'T1', 'lavorazioni': ['Taglio dritto (2)'],
                                           'lav_esterna': 'laser tubo'}], 6, ['taglio', 'esterna'], 'lavorazione esterna (dedotto dalle note: conferma): 6'),
        ('piega + zincatura scritta nella lavorazione', [LAM('Piegatura (1 pieghe)', 'Zincatura a caldo')], 7, ['piega', 'esterna'],
         'piegatura + lavorazione esterna (dedotto dalle note: conferma): 7'),
        ('saldatura + filett. + esterna', [LAM('Saldatura', 'Filettatura (2)'), {'tipo': 'tubolare', 'lav_esterna': 'x',
                                                                                 'lavorazioni': []}], 10, None, None),
        ('lavorazioni come dizionari (ordini a mano)', [{'tipo': 'articolo', 'lavorazioni': [{'nome': 'Piegatura', 'quantita': 2}]}],
         2, ['piega'], None),
    ]
    for nome, righe, giorni, voci, frase in casi:
        a = T.anticipo(righe, 'distinta', '', CAL)
        ok = a['giorni'] == giorni and (voci is None or a['voci'] == voci) and (frase is None or frase in a['motivo'])
        check(f'{nome}: {giorni} gg', ok, a)
    a = T.anticipo([LAM()], 'distinta', 'Verniciatura RAL 9005 a carico nostro', CAL)
    check('verniciatura nelle note dell\'ordine: + 5', a['giorni'] == 6 and 'esterna' in a['voci'], a)
    a = T.anticipo([LAM()], 'distinta', 'lamiera zincata DX51D', CAL)
    check('"lamiera zincata" (materiale) non e\' una lavorazione esterna', a['giorni'] == 1, a)
    a = T.anticipo([{'tipo': 'lamiera', 'materiale': 'ZINCATO', 'lavorazioni': ['Taglio laser']}], 'distinta', '', CAL)
    check('materiale ZINCATO non conta', a['giorni'] == 1, a)


def calendario():
    print('\n2) Giorni lavorativi')
    ven, lun = date(2026, 10, 9), date(2026, 10, 12)
    check('consegna venerdi, 1 gg -> giovedi', T.indietro(ven, 1, CAL) == date(2026, 10, 8))
    check('consegna venerdi, 3 gg -> martedi', T.indietro(ven, 3, CAL) == date(2026, 10, 6))
    check('consegna lunedi, 1 gg -> venerdi (salta il fine settimana)', T.indietro(lun, 1, CAL) == ven)
    check('consegna lunedi, 4 gg -> martedi prima', T.indietro(lun, 4, CAL) == date(2026, 10, 6))
    check('consegna domenica, 1 gg -> venerdi', T.indietro(date(2026, 10, 11), 1, CAL) == ven)
    check('0 gg: la consegna stessa', T.indietro(ven, 0, CAL) == ven)
    fermo = {'giorni': [1, 2, 3, 4, 5], 'eccezioni': {'2026-10-08': {'ore': 0, 'nota': 'manutenzione'}}}
    check('giovedi fermo (giorno speciale, ore 0): si va a mercoledi', T.indietro(ven, 1, fermo) == date(2026, 10, 7))
    sabato = {'giorni': [1, 2, 3, 4, 5], 'eccezioni': {'2026-10-10': {'ore': 6, 'nota': 'sabato'}}}
    check('sabato lavorato (giorno speciale, ore > 0): conta', T.indietro(lun, 1, sabato) == date(2026, 10, 10))
    sei = {'giorni': [1, 2, 3, 4, 5, 6], 'eccezioni': {}}
    check('calendario lun-sab: il sabato conta', T.indietro(lun, 1, sei) == date(2026, 10, 10))
    check('calendario vuoto: non si blocca', isinstance(T.indietro(ven, 2, {'giorni': [], 'eccezioni': {}}), date))
    te = T.taglia_entro(datetime(2026, 10, 9), T.anticipo([LAM('Piegatura (3 pieghe)', 'Filettatura (2)')], 'distinta', '', CAL), CAL)
    check('taglia_entro: 06/10 per la consegna del 09/10 (piega + filettature)',
          te['data'] == '2026-10-06' and te['fonte'] == 'calcolata' and te['giorni'] == 3 and te['consegna'] == '2026-10-09', te)
    te = T.taglia_entro(None, T.anticipo([LAM()], 'distinta', '', CAL), CAL)
    check('senza consegna: niente data, e lo dice', te['data'] is None and 'senza data' in te['motivo'], te)


def a_mano_e_pdf():
    print('\n3) A mano e solo PDF')
    ant = T.anticipo([LAM('Piegatura (2 pieghe)')], 'distinta', '', CAL)
    te = T.taglia_entro(date(2026, 10, 9), ant, CAL, manuale=date(2026, 10, 5))
    check('a mano: la data e\' quella scelta', te['data'] == '2026-10-05' and te['fonte'] == 'a mano', te)
    check('a mano: resta la data calcolata e il motivo la dice',
          te['calcolata'] == '2026-10-07' and 'a mano' in te['motivo'] and '07/10' in te['motivo'], te)
    te = T.taglia_entro(date(2026, 10, 9), ant, CAL, manuale=None)
    check('tolta la data a mano: torna il calcolo', te['data'] == '2026-10-07' and te['fonte'] == 'calcolata', te)
    a = T.anticipo([{'tipo': 'lamiera', 'codice': 'X', 'lavorazioni': ['Piegatura (2 pieghe)', 'Saldatura']}], 'pdf', '', CAL)
    check('solo PDF: 1 giorno anche con righe lette dal PDF, e lo dice',
          a['giorni'] == 1 and 'solo PDF' in a['motivo'] and not a['noto'], a)
    a = T.anticipo([], 'nessuna', '', CAL)
    check('senza distinta: 1 giorno', a['giorni'] == 1 and 'solo PDF' in a['motivo'], a)
    a = T.anticipo([], 'pdf', 'Zincatura a caldo', CAL)
    check('solo PDF con zincatura nelle note: 1 + 5', a['giorni'] == 6 and 'lavorazione esterna' in a['motivo'], a)
    te = T.taglia_entro(date(2026, 10, 9), None, CAL)
    check('lavorazioni non lette: 1 giorno', te['data'] == '2026-10-08' and 'non lette' in te['motivo'], te)


def configurazione():
    print('\n4) Numeri nelle impostazioni')
    cfg = T.anticipo_config({'anticipo_giorni': {'piega': 3, 'esterna': 'x', 'saldatura': 99}})
    check('valori di partenza + quelli salvati validi',
          cfg == {'taglio': 1, 'piega': 3, 'saldatura': 4, 'filettatura': 1, 'esterna': 5}, cfg)
    a = T.anticipo([LAM('Piegatura (1 pieghe)')], 'distinta', '', {'anticipo_giorni': {'piega': 3}})
    check('la piega a 3 giorni si usa', a['giorni'] == 3, a)
    check('_cal_config ha anticipo_giorni coi valori di partenza',
          A._cal_config()['anticipo_giorni'] == T.ANTICIPO_DEFAULT, A._cal_config())


def api():
    print('\n5) API')
    from tests.accesso_aiuto import postazioni, entra, modalita
    s = models.SessionLocal()
    s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
    s.commit(); s.close()
    modalita('protetto')
    postazioni()
    laser = entra(A.app, 'laser')
    rep = entra(A.app, 'reparto')

    base = {'quantita': 2, 'materiale': 'S235', 'spessore_mm': 3, 'costo_base_override': 5,
            'perimetro_taglio_m': 0.8, 'n_forature': 3, 'area_dm2': 2.9, 'bbox_w_mm': 200, 'bbox_h_mm': 150}
    pr = PreventivoManager.create('DECA S.r.l.', 'paolo', quantita=1, numero_ordine_cliente='TE-1')
    pid = pr['id'] if isinstance(pr, dict) else pr
    PreventivoManager.replace_articoli(pid, [
        dict(base, codice='P-PIEGA-00', pieghe=3, filettatura_pz=2),
        dict(base, codice='P-PIANO-00'),
    ])
    s = models.SessionLocal()
    s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'INVIATO'
    s.commit(); s.close()
    oid = PreventivoManager.accetta_e_crea_ordine(pid, 'paolo').get('order_id')
    A.OrderManager.smista(oid, True, user_id='laser')
    # ordine a mano senza distinta (solo PDF), consegna prima
    pdf_id = str(uuid.uuid4())
    s = models.SessionLocal()
    o = s.query(Order).filter(Order.id == oid).first()
    o.data_consegna = datetime(2027, 1, 15)          # venerdi
    s.add(Order(id=pdf_id, cliente='Cliente PDF', numero_ordine='PDF-1', data_consegna=datetime(2027, 1, 13),
                taglio_richiesto=True))
    s.commit(); s.close()

    r = laser.get('/api/laser/ordini')
    d = (r.get_json() or {}).get('ordini', {})
    te = (d.get(oid) or {}).get('taglia_entro') or {}
    check('/api/laser/ordini: piega + filettature, 3 gg prima del 15/01 -> 12/01',
          r.status_code == 200 and te.get('data') == '2027-01-12' and te.get('fonte') == 'calcolata'
          and te.get('giorni') == 3 and 'piegatura + filettature' in (te.get('motivo') or ''), te)
    tp = (d.get(pdf_id) or {}).get('taglia_entro') or {}
    check('/api/laser/ordini: ordine solo PDF, 1 gg prima del 13/01 -> 12/01',
          tp.get('data') == '2027-01-12' and 'solo PDF' in (tp.get('motivo') or ''), tp)
    r = laser.get(f'/api/orders/{oid}/laser')
    check('/api/orders/<id>/laser: stesso taglia_entro', r.status_code == 200
          and (r.get_json() or {}).get('taglia_entro', {}).get('data') == '2027-01-12', r.get_json())

    r = laser.post(f'/api/orders/{oid}/pianifica-taglio', json={'data': '2027-01-08'})
    check('pianifica-taglio: data a mano salvata', r.status_code == 200, r.get_json())
    te = laser.get('/api/laser/ordini').get_json()['ordini'][oid]['taglia_entro']
    check('subito (senza aspettare la cache): taglia entro a mano 08/01, il calcolo resta 12/01',
          te['data'] == '2027-01-08' and te['fonte'] == 'a mano' and te['calcolata'] == '2027-01-12', te)
    b = laser.get('/api/laser/banco?da_smistare=1').get_json() or {}
    check('/api/laser/banco: il giorno di ogni ordine per la saturazione (a mano e calcolato)',
          (b.get('taglia_entro') or {}).get(oid) == '2027-01-08' and (b.get('taglia_entro') or {}).get(pdf_id) == '2027-01-12',
          b.get('taglia_entro'))
    pz = [p for g in b.get('gruppi') or [] for p in g['pezzi'] if p['ordine_id'] == oid]
    check('banco: i pezzi hanno il taglia entro', pz and all(p.get('taglia_entro') == '2027-01-08' for p in pz))
    r = laser.post(f'/api/orders/{oid}/pianifica-taglio', json={'data': None})
    te = laser.get(f'/api/orders/{oid}/laser').get_json()['taglia_entro']
    check('data null: torna al calcolo', r.status_code == 200 and te['data'] == '2027-01-12' and te['fonte'] == 'calcolata', te)
    r = laser.post(f'/api/orders/{oid}/pianifica-taglio', json={'data': '2020-01-01'})
    check('nel passato: rifiutata', r.status_code == 400)
    r = rep.post(f'/api/orders/{oid}/pianifica-taglio', json={'data': '2027-01-08'})
    check('il reparto non cambia il taglia entro', r.status_code in (401, 403), r.status_code)

    print('\n6) Impostazioni via API')
    r = laser.put('/api/laser/calendario-config', json={'anticipo_giorni': {'piega': 3}})
    cfg = (r.get_json() or {}).get('config') or {}
    check('PUT anticipo_giorni: salvato, gli altri restano',
          r.status_code == 200 and cfg.get('anticipo_giorni', {}).get('piega') == 3
          and cfg['anticipo_giorni']['saldatura'] == 4, r.get_json())
    with open(_CFG, encoding='utf-8') as f:
        check('scritto nelle impostazioni TEMPORANEE', json.load(f)['laser_calendario']['anticipo_giorni']['piega'] == 3)
    te = laser.get('/api/laser/ordini').get_json()['ordini'][oid]['taglia_entro']
    check('la piega a 3 giorni sposta il taglia entro (4 gg -> 11/01)', te['data'] == '2027-01-11' and te['giorni'] == 4, te)
    for corpo, nome in (({'anticipo_giorni': {'piega': -1}}, 'negativo'), ({'anticipo_giorni': {'piega': 1.5}}, 'non intero'),
                        ({'anticipo_giorni': {'verniciatura': 2}}, 'lavorazione sconosciuta'),
                        ({'anticipo_giorni': [1, 2]}, 'non un elenco per lavorazione'),
                        ({'anticipo_giorni': {'piega': True}}, 'vero/falso')):
        r = laser.put('/api/laser/calendario-config', json=corpo)
        check(f'PUT anticipo_giorni {nome}: rifiutato', r.status_code == 400, r.get_json())
    r = laser.put('/api/laser/calendario-config', json={'eccezioni': {'2027-01-11': {'ore': 0, 'nota': 'fermo'}}})
    te = laser.get('/api/laser/ordini').get_json()['ordini'][oid]['taglia_entro']
    check('lunedi 11/01 laser fermo (giorno speciale): taglia entro venerdi 08/01', te['data'] == '2027-01-08', te)


def spunta_esterna():
    print('\n7) Spunta "Lavorazione esterna"')
    piega = [LAM('Piegatura (2 pieghe)')]
    a = T.anticipo(piega, 'distinta', 'Zincatura a caldo', CAL)
    check('non detta + zincatura nelle note: +5 e il motivo chiede di confermare',
          a['giorni'] == 7 and '(dedotto dalle note: conferma)' in a['motivo'] and a['esterna_fonte'] == 'dedotta', a)
    a = T.anticipo(piega, 'distinta', 'Zincatura a caldo', CAL, esterna=False)
    check('detta NO: niente +5 anche se le parole tornano', a['giorni'] == 2 and 'esterna' not in a['voci']
          and 'dedotto' not in a['motivo'] and a['esterna_fonte'] == 'detta', a)
    a = T.anticipo(piega, 'distinta', '', CAL, esterna=True, esterna_nota='zincatura')
    check("detta SI' senza parole: +5, il motivo dice cosa e non chiede conferma",
          a['giorni'] == 7 and 'lavorazione esterna (zincatura)' in a['motivo'] and 'dedotto' not in a['motivo'], a)
    a = T.anticipo(piega, 'distinta', '', CAL, esterna=True)
    check("detta SI' senza nota: lavorazione esterna", a['giorni'] == 7 and 'dedotto' not in a['motivo'], a)
    a = T.anticipo(piega, 'distinta', '', CAL)
    check('non detta e niente parole: niente +5, niente richiesta', a['giorni'] == 2 and a['esterna_fonte'] is None, a)
    a = T.anticipo([], 'pdf', '', CAL, esterna=True, esterna_nota='verniciatura')
    check("solo PDF + detta si': 1 + 5", a['giorni'] == 6 and 'verniciatura' in a['motivo'], a)

    from tests.accesso_aiuto import entra, ufficio_pronto
    laser = entra(A.app, 'laser')
    ufficio = ufficio_pronto(A.app)
    rep = entra(A.app, 'reparto')
    oid = str(uuid.uuid4())
    s = models.SessionLocal()
    s.add(Order(id=oid, cliente='Cliente Zinco', numero_ordine='ZN-1', data_consegna=datetime(2027, 1, 15),
                taglio_richiesto=True, note='Zincatura a caldo a carico nostro'))
    s.commit(); s.close()
    te = laser.get(f'/api/laser/ordini?ordine={oid}').get_json()['ordini'][oid]['taglia_entro']
    check('API, non detta: 6 gg (solo PDF + esterna dedotta), motivo da confermare',
          te['giorni'] == 6 and 'dedotto dalle note' in te['motivo'] and te['esterna_fonte'] == 'dedotta', te)
    r = laser.post(f'/api/orders/{oid}/lavorazione-esterna', json={'valore': False})
    te = laser.get(f'/api/laser/ordini?ordine={oid}').get_json()['ordini'][oid]
    check('API detta NO: subito 1 gg, niente +5', r.status_code == 200 and te['taglia_entro']['giorni'] == 1
          and te['lavorazione_esterna']['valore'] is False, te.get('taglia_entro'))
    r = laser.post(f'/api/orders/{oid}/lavorazione-esterna', json={'valore': True, 'nota': 'zincatura'})
    te = laser.get(f'/api/orders/{oid}/laser').get_json()
    check("API detta SI' (zincatura): 6 gg, nota salvata", r.status_code == 200 and te['taglia_entro']['giorni'] == 6
          and te['lavorazione_esterna'] == {'valore': True, 'nota': 'zincatura'}
          and 'zincatura' in te['taglia_entro']['motivo'], te.get('taglia_entro'))
    b = laser.get('/api/laser/banco?da_smistare=1').get_json() or {}
    check('il banco (saturazione) usa la spunta', (b.get('taglia_entro') or {}).get(oid) == '2027-01-06', b.get('taglia_entro'))
    o = (laser.get(f'/api/orders/{oid}').get_json() or {})
    o = o.get('data') or o
    check('/api/orders/<id> ha la spunta', o.get('lavorazione_esterna') is True and o.get('lavorazione_esterna_nota') == 'zincatura', o)
    r = laser.post(f'/api/orders/{oid}/lavorazione-esterna', json={'valore': None})
    te = laser.get(f'/api/laser/ordini?ordine={oid}').get_json()['ordini'][oid]['taglia_entro']
    check('API null: torna la deduzione', r.status_code == 200 and te['esterna_fonte'] == 'dedotta', te)
    for corpo in ({'valore': 'si'}, {}, {'valore': 1}):
        r = laser.post(f'/api/orders/{oid}/lavorazione-esterna', json=corpo)
        check(f'API valore non valido {corpo}: rifiutato', r.status_code == 400, r.get_json())
    r = laser.post('/api/orders/non-esiste/lavorazione-esterna', json={'valore': True})
    check("ordine che non c'e': 404", r.status_code == 404)
    r = rep.post(f'/api/orders/{oid}/lavorazione-esterna', json={'valore': True})
    check('il reparto non la cambia', r.status_code in (401, 403), r.status_code)
    r = ufficio.put(f'/api/orders/{oid}', json={'lavorazione_esterna': True, 'lavorazione_esterna_nota': 'verniciatura'})
    s = models.SessionLocal()
    o = s.query(Order).filter(Order.id == oid).first()
    check("ufficio (modifica ordine): si' + verniciatura", r.status_code == 200 and o.lavorazione_esterna is True
          and o.lavorazione_esterna_nota == 'verniciatura', r.get_json())
    s.close()
    r = ufficio.put(f'/api/orders/{oid}', json={'lavorazione_esterna': None, 'lavorazione_esterna_nota': ''})
    s = models.SessionLocal()
    o = s.query(Order).filter(Order.id == oid).first()
    check('ufficio: null = non detto, la nota sparisce', r.status_code == 200 and o.lavorazione_esterna is None
          and not o.lavorazione_esterna_nota, r.get_json())
    s.close()
    r = ufficio.put(f'/api/orders/{oid}', json={'lavorazione_esterna': 'forse'})
    check('ufficio: valore non valido rifiutato', r.status_code == 400, r.get_json())


def main():
    regola()
    calendario()
    a_mano_e_pdf()
    configurazione()
    api()
    spunta_esterna()
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
