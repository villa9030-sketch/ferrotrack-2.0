"""Spessore gia' proposto sulle righe bloccate per "spessore mancante"
(backend/api_laser.py: lettura_spessore / proposta_spessore).

Copre:
 1. disegno con "SP. 3" nel cartiglio: la riga bloccata ha la proposta 3 mm
    ("scritto nel cartiglio"), sicura
 2. disegno con la vista di fianco (striscia lunga quanto il pezzo, larga 4):
    proposta 4 mm "vista di fianco"
 3. disegno che non dice niente: nessuna proposta (come prima)
 4. lettura in cache (il disegno non si rilegge); solo nell'ordine aperto,
    non nel riassunto dell'elenco
 5. salvato lo spessore proposto (/dati): la riga non e' piu' bloccata

Gira su DATABASE TEMPORANEO, cartelle temporanee, LANTEK SIMULATO.
Esecuzione: python app/tests/test_spessore_proposto.py
"""
import os
import shutil
import sys
import tempfile
import uuid

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
_ESEMPI = tempfile.mkdtemp(prefix='ft_spp_esempi_')
os.environ['FERROTRACK_ESEMPI_DIR'] = _ESEMPI

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

import ezdxf  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Preventivo  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_spp_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend import lantek as L  # noqa: E402
from backend import api_laser as AL  # noqa: E402
from backend.database import PreventivoManager  # noqa: E402
from backend.preventivi import tempi_laser as TL  # noqa: E402

_CARTELLE = tempfile.mkdtemp(prefix='ft_spp_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)
from backend.preventivi import dxf_cache as _DC  # noqa: E402
_DC._CACHE_DB_PATH = os.path.join(_CARTELLE, 'dxf_cache.db')     # cache dei disegni temporanea

# ── Lantek simulato: tutti i codici nuovi, nessun programma ─────────────
TL._leggi_lantek = lambda codici: None
L.pezzi_in_lantek = lambda codici: {'disponibile': True, 'errore': None, 'pezzi': {
    c: {'esiste': False, 'codice_lantek': None, 'materiale': None, 'spessore': None, 'revisioni': []} for c in codici}}
L.clienti_lantek = lambda: ['DECA']
L.ordini_in_lantek = lambda commessa: {}
L.ordini_fatti_in_lantek = lambda commessa: {}
L.pezzi_dell_ordine = lambda commessa: {}
L.procesos_disponibile = lambda: True
L.materiali_lantek_noti = lambda: {'FERRO', 'INOX'}
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


def scrivi_dxf(percorso, testo=None, vista=None):
    """Piastra 200 x 150 con un foro; testo nel cartiglio; vista di fianco
    (striscia 200 x vista mm sotto al pezzo)."""
    doc = ezdxf.new('R2010', units=4)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (200, 0), (200, 150), (0, 150)], close=True)
    msp.add_circle((100, 75), 10)
    if vista:
        msp.add_lwpolyline([(0, -40), (200, -40), (200, -40 + vista), (0, -40 + vista)], close=True)
    if testo:
        msp.add_text(testo, dxfattribs={'height': 5, 'insert': (260, 10)})
    doc.saveas(percorso)


def main():
    from tests.accesso_aiuto import postazioni, entra, modalita
    s = models.SessionLocal()
    s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
    s.commit(); s.close()
    modalita('protetto')
    postazioni()
    laser = entra(A.app, 'laser')

    base = {'quantita': 2, 'materiale': 'FERRO', 'spessore_mm': None, 'costo_base_override': 5,
            'perimetro_taglio_m': 0.76, 'n_forature': 1, 'area_dm2': 2.97, 'bbox_w_mm': 200, 'bbox_h_mm': 150,
            'cleaned_status': 'auto', 'dxf_confidence': 0.9}
    pr = PreventivoManager.create('DECA S.r.l.', 'paolo', quantita=1, numero_ordine_cliente='SP-1')
    pid = pr['id'] if isinstance(pr, dict) else pr
    codici = {'P-TESTO-00': {'testo': 'SP. 3'}, 'P-VISTA-00': {'vista': 4}, 'P-NIENTE-00': {}}
    PreventivoManager.replace_articoli(pid, [dict(base, codice=c, dxf_filename=c + '.dxf') for c in codici])
    s = models.SessionLocal()
    s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'INVIATO'
    s.commit(); s.close()
    oid = PreventivoManager.accetta_e_crea_ordine(pid, 'paolo').get('order_id')
    A.OrderManager.smista(oid, True, user_id='laser')
    arts = {a['codice']: a['id'] for a in (PreventivoManager.get(pid, include_children=True) or {})['articoli']}
    cartella = os.path.join(A.DRAWINGS_FOLDER, oid)
    pronti = os.path.join(cartella, 'LANTEK', A._nome_lamiera('FERRO', None))
    os.makedirs(pronti)
    for c, kw in codici.items():
        scrivi_dxf(os.path.join(cartella, c + '.dxf'), **kw)
        shutil.copy2(os.path.join(cartella, c + '.dxf'), os.path.join(pronti, c + '.dxf'))

    print('1-3) Proposte sulle righe bloccate')
    o = laser.get(f'/api/orders/{oid}/laser').get_json() or {}
    bl = {b['codice']: b for b in o.get('bloccati') or []}
    check('tre righe bloccate per lo spessore', sorted(bl) == sorted(codici)
          and all(b.get('campo') == 'spessore' for b in bl.values()), o.get('bloccati'))
    p = (bl.get('P-TESTO-00') or {}).get('proposta') or {}
    check('"SP. 3" nel cartiglio: proposto 3 mm, scritto nel cartiglio', p.get('spessore_mm') == 3
          and 'cartiglio' in (p.get('fonte') or ''), p)
    p = (bl.get('P-VISTA-00') or {}).get('proposta') or {}
    check('vista di fianco larga 4: proposto 4 mm, vista di fianco', p.get('spessore_mm') == 4
          and 'vista di fianco' in (p.get('fonte') or ''), p)
    check('il disegno non dice niente: nessuna proposta', 'proposta' not in (bl.get('P-NIENTE-00') or {}),
          bl.get('P-NIENTE-00'))

    print('\n4) Cache e riassunto')
    chiamate = []
    from backend.preventivi import dxf_scanner as DS
    orig = DS.estrai_spessore_da_cartiglio
    DS.estrai_spessore_da_cartiglio = lambda *a, **k: chiamate.append(1) or orig(*a, **k)
    try:
        laser.get(f'/api/orders/{oid}/laser')
    finally:
        DS.estrai_spessore_da_cartiglio = orig
    check('seconda apertura: lo spessore non si rilegge (cache)', not chiamate, len(chiamate))
    d = (laser.get(f'/api/laser/ordini?ordine={oid}').get_json() or {}).get('ordini', {}).get(oid) or {}
    check("nel riassunto dell'elenco niente proposte (non si leggono i disegni)",
          d.get('bloccati') and not any(b.get('proposta') for b in d['bloccati']), d.get('bloccati'))
    check('proposta_spessore senza disegno o non DXF: None',
          AL.proposta_spessore(None) is None and AL.proposta_spessore('x.pdf') is None)

    print('\n5) Salvato lo spessore proposto')
    r = laser.post(f'/api/orders/{oid}/pezzi/{arts["P-TESTO-00"]}/dati', json={'spessore_mm': 3})
    o = laser.get(f'/api/orders/{oid}/laser').get_json() or {}
    check('P-TESTO non e\' piu\' bloccato', r.status_code == 200
          and 'P-TESTO-00' not in [b['codice'] for b in o.get('bloccati') or []], o.get('bloccati'))

    print(f'\nPASSATI: {OK}   FALLITI: {len(KO)}')
    if KO:
        print('Falliti: ' + ', '.join(KO))
    return 1 if KO else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    finally:
        try:
            _ENG.dispose()
            os.remove(_TMP)
        except OSError:
            pass
        shutil.rmtree(_CARTELLE, ignore_errors=True)
        shutil.rmtree(_ESEMPI, ignore_errors=True)
