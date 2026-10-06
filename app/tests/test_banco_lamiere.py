"""Test del BANCO LAMIERE del laser e della lista ordini leggera.

Il laser vedeva gli ordini uno per uno, ma in macchina si carica una lamiera
(materiale + spessore) e ci si mettono i pezzi di piu' ordini: il banco
raggruppa i pezzi da tagliare cosi', ognuno col suo ingombro, e la pagina li
dispone sui fogli. In piu' le pagine di reparto ricaricavano TUTTI gli ordini
ogni pochi secondi, archivio compreso: ora chiedono solo quelli aperti.

Copre:
 1. /api/laser/banco: gruppi per materiale + spessore, quantita' come la
    distinta (lotto, assiemi), ingombro dal disegno o stimato dall'area,
    tempo di taglio, esclusione di ordini tagliati / archiviati / non laser
 2. ordini senza distinta (caricati a mano) elencati a parte
 3. ?da_smistare=1 aggiunge gli ordini non ancora smistati
 4. /api/orders?aperti=1 senza le pratiche archiviate
 5. indici delle liste: creati e idempotenti

Gira su DATABASE TEMPORANEO e cartelle temporanee. Esecuzione:
    python app/tests/test_banco_lamiere.py
"""
import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine, inspect, text  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Order, Preventivo  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_banco_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')  # il MODULO, non l'oggetto Flask
from backend.database import PreventivoManager  # noqa: E402
from backend.migrations_ore import _indici_prestazioni  # noqa: E402
from tests.accesso_aiuto import postazioni, entra, modalita  # noqa: E402

_CARTELLE = tempfile.mkdtemp(prefix='ft_banco_')
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


def ordine(oid, **campi):
    base = dict(id=oid, numero_ordine=oid.upper(), cliente='Cliente Alfa',
                status='RICEVUTO', data_ricezione=datetime.utcnow() - timedelta(days=2),
                data_consegna=datetime(2026, 10, 15))
    base.update(campi)
    return Order(**base)


def preventivo_accettato(cliente):
    """Un preventivo vero, accettato con la strada vera: torna l'id dell'ordine."""
    pr = PreventivoManager.create(cliente, 'paolo', quantita=2)
    pid = pr['id'] if isinstance(pr, dict) else pr
    PreventivoManager.replace_articoli(pid, [
        # sciolto: 3 x lotto 2 = 6 pezzi, ingombro misurato dal disegno
        {'codice': 'A1', 'quantita': 3, 'materiale': 'S235', 'spessore_mm': 3,
         'area_dm2': 1.8, 'bbox_w_mm': 200, 'bbox_h_mm': 100, 'costo_base_override': 5,
         'stima_dettaglio': {'tempo_totale_min': 0.5}},
        # nell'assieme ASS1 (x4): 2 x 4 = 8 pezzi, solo area -> ingombro stimato
        {'codice': 'B1', 'quantita': 2, 'materiale': 'INOX_304', 'spessore_mm': 2,
         'area_dm2': 1.0, 'codice_assieme': 'ASS1', 'costo_base_override': 4},
    ])
    PreventivoManager.replace_assiemi(pid, [{'codice_assieme': 'ASS1', 'qty': 4, 'costo': 10}])
    PreventivoManager.replace_tubolari(pid, [{'profilo': 'Quadro 40x40 sp.2mm',
                                              'lunghezza_m': 0.5, 'qty': 2}])
    PreventivoManager.replace_piastre(pid, [{'spessore_mm': 10, 'area_dm2': 1.5,
                                             'materiale': 's235', 'costo': 3}])
    s = models.SessionLocal()
    try:
        s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'INVIATO'
        s.commit()
    finally:
        s.close()
    acc = PreventivoManager.accetta_e_crea_ordine(pid, 'paolo')
    return acc.get('order_id')


def imposta(oid, **campi):
    s = models.SessionLocal()
    try:
        o = s.query(Order).filter(Order.id == oid).first()
        for k, v in campi.items():
            setattr(o, k, v)
        s.commit()
    finally:
        s.close()


def main():
    s = models.SessionLocal()
    try:
        s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
        s.add(ordine('o-mano', taglio_richiesto=True, taglio_completato=False))      # solo PDF
        s.add(ordine('o-mano-smistare'))                                              # mai smistato
        s.add(ordine('o-chiuso', status='CHIUSO', taglio_richiesto=True))             # archivio
        s.commit()
    finally:
        s.close()

    da_tagliare = preventivo_accettato('Cliente Beta')
    imposta(da_tagliare, taglio_richiesto=True, taglio_completato=False,
            data_consegna=datetime(2026, 10, 2))
    tagliato = preventivo_accettato('Cliente Gamma')
    imposta(tagliato, taglio_richiesto=True, taglio_completato=True)
    non_laser = preventivo_accettato('Cliente Delta')
    imposta(non_laser, taglio_richiesto=False)
    da_smistare = preventivo_accettato('Cliente Epsilon')
    imposta(da_smistare, taglio_richiesto=None, taglio_completato=False)

    # la stazione Laser: e' lei che legge il banco
    modalita('protetto')
    postazioni()
    c = entra(A.app, 'laser')

    # =====================================================================
    print('\n1) Banco lamiere: gruppi, quantita\', ingombri')
    r = c.get('/api/laser/banco')
    d = r.get_json()
    check('risponde 200', r.status_code == 200 and d.get('success'), d)
    gruppi = {g['chiave']: g for g in d['gruppi']}
    check('tre lamiere: S235 3, INOX 304 2, S235 10 (materiale normalizzato)',
          sorted(gruppi) == ['INOX_304|2.0', 'S235|10.0', 'S235|3.0'], sorted(gruppi))
    ordini_visti = {p['ordine_id'] for g in d['gruppi'] for p in g['pezzi']}
    check('solo l ordine da tagliare (non tagliati, non "non passa", non archiviati)',
          ordini_visti == {da_tagliare}, ordini_visti)
    a1 = gruppi['S235|3.0']['pezzi'][0]
    check('A1: 3 x lotto 2 = 6 pezzi', a1['quantita'] == 6, a1)
    check('A1: ingombro dal disegno 200 x 100', a1['w_mm'] == 200 and a1['h_mm'] == 100
          and a1['ingombro'] == 'disegno', a1)
    check('A1: tempo per pezzo dalla stima', a1['tempo_min'] == 0.5, a1)
    g = gruppi['S235|3.0']
    check('totali del gruppo: pezzi, area, tempo', g['n_pezzi'] == 6
          and abs(g['area_dm2'] - 10.8) < 1e-6 and abs(g['tempo_min'] - 3.0) < 1e-6, g)
    b1 = gruppi['INOX_304|2.0']['pezzi'][0]
    check('B1 nell assieme x4: 8 pezzi', b1['quantita'] == 8, b1)
    check('B1 senza ingombro: quadrato di pari area (100 x 100), segnato stimato',
          b1['w_mm'] == 100 and b1['h_mm'] == 100 and b1['ingombro'] == 'stimato', b1)
    check('il gruppo conta i codici stimati', gruppi['INOX_304|2.0']['n_ingombro_stimato'] == 1)
    pl = gruppi['S235|10.0']['pezzi'][0]
    check('piastra da STEP: stimata dall area (1,5 dm2 -> 122,5 mm)',
          pl['ingombro'] == 'stimato' and abs(pl['w_mm'] - 122.5) < 0.1, pl)
    tub = [p for g in d['gruppi'] for p in g['pezzi'] if 'Quadro' in p['codice']]
    check('i tubolari non sono lamiere', not tub, tub)
    check('formati di lamiera proposti', d['formati'][0] == {'nome': '3000 × 1500', 'w_mm': 3000.0, 'h_mm': 1500.0},
          d['formati'])
    check('consegna piu vicina del gruppo', g['consegna_prima'].startswith('2026-10-02'), g['consegna_prima'])

    # =====================================================================
    print('\n2) Ordini senza distinta')
    senza = {o['ordine_id']: o for o in d['senza_distinta']}
    check('l ordine caricato a mano e da tagliare e elencato a parte',
          list(senza) == ['o-mano'] and 'PDF' in senza['o-mano']['motivo'], d['senza_distinta'])

    # =====================================================================
    print('\n3) Anche da smistare')
    d2 = c.get('/api/laser/banco?da_smistare=1').get_json()
    visti2 = {p['ordine_id'] for g in d2['gruppi'] for p in g['pezzi']}
    check('entra l ordine non ancora smistato', visti2 == {da_tagliare, da_smistare}, visti2)
    check('...e il suo stato e da_smistare', any(p['stato_taglio'] == 'da_smistare'
          for g in d2['gruppi'] for p in g['pezzi']))
    check('...con i pezzi sommati nel gruppo', {g['chiave']: g['n_pezzi'] for g in d2['gruppi']}['S235|3.0'] == 12)
    check('anche l ordine a mano mai smistato va fra quelli senza distinta',
          {o['ordine_id'] for o in d2['senza_distinta']} == {'o-mano', 'o-mano-smistare'}, d2['senza_distinta'])

    # =====================================================================
    print('\n4) Lista ordini leggera')
    tutti = {o['id'] for o in c.get('/api/orders').get_json()['orders']}
    aperti = {o['id'] for o in c.get('/api/orders?aperti=1').get_json()['orders']}
    check('senza filtro ci sono anche gli archiviati', 'o-chiuso' in tutti)
    check('?aperti=1 li toglie', 'o-chiuso' not in aperti and 'o-mano' in aperti
          and da_tagliare in aperti, aperti)

    # =====================================================================
    print('\n5) Indici')
    n1 = _indici_prestazioni(_ENG, inspect(_ENG))
    n2 = _indici_prestazioni(_ENG, inspect(_ENG))
    with _ENG.connect() as conn:
        nomi = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))}
    check('creati', n1 > 0 and 'ix_ft_order_files_order_id' in nomi
          and 'ix_ft_processing_steps_order_id' in nomi, nomi)
    check('idempotenti', n2 == n1)

    print('\n' + '=' * 60)
    print(f'PASSATI: {OK}   FALLITI: {len(KO)}')
    if KO:
        print('Falliti: ' + ', '.join(KO))
    print('=' * 60)
    return 1 if KO else 0


if __name__ == '__main__':
    code = 1
    try:
        code = main()
    finally:
        try:
            _ENG.dispose()
            os.remove(_TMP)
        except Exception:
            pass
        shutil.rmtree(_CARTELLE, ignore_errors=True)
    sys.exit(code)
