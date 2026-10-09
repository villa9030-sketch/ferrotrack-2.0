"""Test del CALENDARIO DEL LASER: saturazione (parte server).

Il laserista pianifica guardando la consegna e raggruppando per spessore, e
ha giorni troppo pieni. Per la saturazione ogni ordine ha una durata e un
giorno di taglio (vuoto = il giorno di consegna).

Copre:
 1. colonne nuove (data_taglio_pianificata, durata_laser_manuale_min),
    migrazione idempotente, campi nella lista ordini
 2. /api/orders/<id>/pianifica-taglio: permessi (operaio no, laser e capo
    si'), data sbagliata o passata rifiutata, null = torna alla consegna,
    durata a mano, audit
 3. tempo di taglio SENZA prezzo: uguale a quello di stima_base per lo stesso
    pezzo; pezzo senza perimetro = "mancante"
 4. banco: pezzo con stima del preventivo = 'preventivo', pezzo di un
    pacchetto senza stima = 'calcolato'; ?dal= aggiunge i tagliati di recente
 5. impostazioni del calendario: default, salvataggio validato, laser_config
    non toccato

Gira su DATABASE TEMPORANEO e configurazione temporanea.
Esecuzione: python app/tests/test_saturazione_laser.py
"""
import json
import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, date

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
_CFG_TMP = os.path.join(tempfile.gettempdir(), f'test_sat_cfg_{uuid.uuid4().hex[:6]}.json')
with open(os.path.join(_APP, 'app_config.json'), encoding='utf-8') as f:
    _cfg_vero = json.load(f)
with open(_CFG_TMP, 'w', encoding='utf-8') as f:
    json.dump(_cfg_vero, f)
os.environ['FERROTRACK_CONFIG'] = _CFG_TMP          # il file vero non si tocca

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine, inspect  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Order, Preventivo, AuditLog  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_sat_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
# Lantek simulato: spento (i tempi vengono dal modello tarato)
from backend.preventivi import tempi_laser as _TL  # noqa: E402
_TL._leggi_lantek = lambda codici: None
from backend.database import PreventivoManager, ConfigManager  # noqa: E402
from backend.preventivi import laser_cost_estimator as L  # noqa: E402
from tests.accesso_aiuto import postazioni, entra, modalita  # noqa: E402

_CARTELLE = tempfile.mkdtemp(prefix='ft_sat_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)

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


PEZZO = {'materiale': 'S235', 'spessore_mm': 3, 'area_dm2': 1.8, 'perimetro_taglio_m': 0.62,
         'n_forature': 3, 'bbox_w_mm': 200, 'bbox_h_mm': 100}


def ordine_da(cliente, articoli, **campi):
    pr = PreventivoManager.create(cliente, 'paolo', quantita=1)
    pid = pr['id'] if isinstance(pr, dict) else pr
    PreventivoManager.replace_articoli(pid, articoli)
    s = models.SessionLocal()
    try:
        s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'INVIATO'
        s.commit()
    finally:
        s.close()
    oid = PreventivoManager.accetta_e_crea_ordine(pid, 'paolo').get('order_id')
    s = models.SessionLocal()
    try:
        o = s.query(Order).filter(Order.id == oid).first()
        for k, v in campi.items():
            setattr(o, k, v)
        s.commit()
    finally:
        s.close()
    return oid


def main():
    s = models.SessionLocal()
    try:
        s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
        s.add(User(id='laser', name='Laser', role='Laser', is_active=True))
        s.add(User(id='enzo', name='Enzo', role='Operaio Officina', is_active=True))
        s.commit()
    finally:
        s.close()
    # Il laser e' il dispositivo registrato come Laser; "l'operaio" e' il
    # tablet d'officina (sola lettura); "senza utente" un browser qualunque.
    modalita('protetto')
    postazioni()
    c = entra(A.app, 'laser')
    tab = entra(A.app, 'reparto')
    anon = A.app.test_client()

    print('\n1) Colonne e lista ordini')
    cols = {x['name'] for x in inspect(_ENG).get_columns('orders')}
    check('colonne nuove', {'data_taglio_pianificata', 'durata_laser_manuale_min'} <= cols)
    from backend.migrations_ore import migrate_ore
    try:
        migrate_ore(_ENG); migrate_ore(_ENG); idem = True
    except Exception as e:
        idem = str(e)
    check('migrazione idempotente', idem is True, idem)

    con_stima = ordine_da('Cliente Beta', [dict(PEZZO, codice='A1', quantita=4, costo_base_override=5,
                                                stima_dettaglio={'tempo_totale_min': 0.5})],
                          taglio_richiesto=True, data_consegna=datetime(2027, 1, 15))
    pacchetto = ordine_da('Cliente Gamma', [dict(PEZZO, codice='P1', quantita=10, costo_base_override=1)],
                          taglio_richiesto=True, data_consegna=datetime(2027, 1, 18))
    rotto = ordine_da('Cliente Delta', [dict(PEZZO, codice='X1', quantita=1, perimetro_taglio_m=0,
                                             costo_base_override=1)],
                      taglio_richiesto=True, data_consegna=datetime(2027, 1, 18))
    lista = c.get('/api/orders?aperti=1').get_json()
    lista = lista if isinstance(lista, list) else lista.get('orders', [])
    o = next(x for x in lista if x['id'] == con_stima)
    check('lista ordini: campi del calendario', 'data_taglio_pianificata' in o and 'durata_laser_manuale_min' in o, list(o)[:5])

    print('\n2) Pianificare il taglio')
    url = f'/api/orders/{con_stima}/pianifica-taglio'
    domani = (date.today() + timedelta(days=1)).isoformat()
    check('tablet officina: 403', tab.post(url, json={'data': domani}).status_code == 403)
    check('browser non registrato: 401', anon.post(url, json={'data': domani}).status_code == 401)
    check('data sbagliata: 400', c.post(url, json={'data': '31/12/2026'}).status_code == 400)
    check('data passata: 400', c.post(url, json={'data': '2020-01-01'}).status_code == 400)
    r = c.post(url, json={'data': domani})
    check('laser pianifica', r.status_code == 200 and r.get_json()['data_taglio_pianificata'] == domani, r.get_json())
    r = c.post(url, json={'data': None})
    check('null = torna alla consegna', r.status_code == 200 and r.get_json()['data_taglio_pianificata'] is None)
    r = c.post(f'/api/orders/{pacchetto}/pianifica-taglio', json={'durata_min': 90})
    check('durata a mano', r.status_code == 200 and r.get_json()['durata_laser_manuale_min'] == 90)
    check('durata assurda: 400', c.post(url, json={'durata_min': -5}).status_code == 400)
    s = models.SessionLocal()
    try:
        n_audit = s.query(AuditLog).filter(AuditLog.action == 'ORDINE_PIANIFICA_TAGLIO').count()
    finally:
        s.close()
    check('audit di ogni modifica', n_audit == 3, n_audit)

    print('\n3) Tempo di taglio senza prezzo')
    cfg = ConfigManager.load_config() or {}
    t, _ = L.tempo_taglio_min(PEZZO, cfg)
    ref = L.stima_base(PEZZO, cfg)['tempo_totale_min']
    check('uguale a stima_base', t is not None and abs(t - ref) < 1e-3, (t, ref))
    check('senza perimetro: non calcolabile', L.tempo_taglio_min(dict(PEZZO, perimetro_taglio_m=0), cfg)[0] is None)

    print('\n4) Banco lamiere per il calendario')
    d = c.get('/api/laser/banco?da_smistare=1').get_json()
    pezzi = {p['codice']: p for g in d['gruppi'] for p in g['pezzi']}
    # tempi tarati su Lantek (preventivi/tempi_laser): il modello vince sulla
    # stima del preventivo, ed e' gia' moltiplicato per il fattore foglio
    from backend.preventivi import tempi_laser as TL
    atteso = TL.minuti_pezzo(None, dict(PEZZO))['minuti']
    check('modello: tempo del pezzo x fattore foglio (~1,03)', atteso and abs(
        atteso - TL.tempo_pezzo(None, dict(PEZZO), usa_lantek=False)['secondi'] / 60 * TL.fattore_foglio('S235')) < 1e-3
        and 1.0 < TL.fattore_foglio('S235') < 1.1, atteso)
    check('pezzo con stima del preventivo: vince il modello tarato', pezzi['A1']['tempo_fonte'] == 'modello'
          and abs(pezzi['A1']['tempo_min'] - atteso) < 1e-3, pezzi['A1'])
    check('pezzo da pacchetto: modello tarato', pezzi['P1']['tempo_fonte'] == 'modello'
          and abs(pezzi['P1']['tempo_min'] - atteso) < 1e-3, pezzi['P1'])
    check('pezzo senza perimetro: mancante', pezzi['X1']['tempo_fonte'] == 'mancante' and pezzi['X1']['tempo_min'] is None)
    s = models.SessionLocal()
    try:
        o = s.query(Order).filter(Order.id == rotto).first()
        o.taglio_completato = True
        o.data_taglio_completato = datetime.utcnow()
        s.commit()
    finally:
        s.close()
    senza = c.get('/api/laser/banco?da_smistare=1').get_json()
    con = c.get(f'/api/laser/banco?da_smistare=1&dal={date.today().isoformat()}').get_json()
    ids = lambda dd: {p['ordine_id'] for g in dd['gruppi'] for p in g['pezzi']}  # noqa: E731
    check('?dal= aggiunge il tagliato di oggi', rotto not in ids(senza) and rotto in ids(con))
    check('?dal= sbagliato: 400', c.get('/api/laser/banco?dal=ieri').status_code == 400)

    print('\n5) Impostazioni del calendario')
    g = c.get('/api/laser/calendario-config').get_json()['config']
    check('default: 8 h, riserva 2, 5 min, 3 s, correzione 1 (i tempi sono già tarati)',
          (g['ore_turno'], g['ore_riserva'], g['carico_min_lamiera'],
           g['scarico_s_pezzo'], g['fattore_tempo']) == (8, 2, 5, 3, 1.0), g)
    r = c.put('/api/laser/calendario-config', json={'fattore_tempo': 1.18})
    check('il vecchio +18% non si rimette più', r.status_code == 200
          and r.get_json()['config']['fattore_tempo'] == 1.0, r.get_json())
    laser_prima = json.dumps((ConfigManager.load_config() or {}).get('laser_config'), sort_keys=True)
    check('il tablet officina non salva', tab.put('/api/laser/calendario-config', json={'ore_turno': 9}).status_code == 403)
    check('riserva >= turno rifiutata', c.put('/api/laser/calendario-config',
                                              json={'ore_riserva': 8}).status_code == 400)
    r = c.put('/api/laser/calendario-config', json={'ore_turno': 9, 'giorni': [1, 2, 3, 4, 5, 6]})
    check('laser salva', r.status_code == 200 and r.get_json()['config']['ore_turno'] == 9
          and r.get_json()['config']['giorni'] == [1, 2, 3, 4, 5, 6], r.get_json())
    fra10 = (date.today() + timedelta(days=10)).isoformat()
    r = c.put('/api/laser/calendario-config', json={'eccezioni': {
        fra10: {'ore': 0, 'nota': 'manutenzione'}, '2020-01-01': {'ore': 3}}})
    ecc = r.get_json()['config']['eccezioni']
    check('giorno speciale salvato, quelli vecchi buttati', ecc == {fra10: {'ore': 0.0, 'nota': 'manutenzione'}}, ecc)
    check('giorno speciale con ore assurde: 400', c.put('/api/laser/calendario-config', json={
        'eccezioni': {fra10: {'ore': 30}}}).status_code == 400)
    check('laser_config non toccato', json.dumps((ConfigManager.load_config() or {}).get('laser_config'), sort_keys=True) == laser_prima)

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
        os.remove(_CFG_TMP)
        shutil.rmtree(_CARTELLE, ignore_errors=True)
    except Exception:
        pass
    sys.exit(code)
