"""La riga dell'ordine del cliente accanto al disegno, nel controllo a tutto
schermo (backend/api_laser.py: righe_ordine_cliente / riga_ordine_di, campo
riga_ordine di /lettura).

Copre:
 1. ordine DECA ("Ordine Fornitore"): posizione, quantita', descrizione,
    commessa e materiale/spessore scritti nella descrizione; codice trovato
    anche senza revisione (13PA00434 <-> 13PA00434-00) e ripetuto su due righe
 2. ordine prezzato (Poliform/B&B): descrizione e dettaglio
 3. PDF che non si legge: le righe salvate al caricamento (quantita',
    commessa); niente da nessuna parte: None (la pagina non mostra niente)
 4. /lettura risponde con riga_ordine; lettura del PDF in cache

Il lettore del PDF e' simulato (il formato vero lo provano i suoi test).
Gira su DATABASE TEMPORANEO, cartelle temporanee, LANTEK SIMULATO.
Esecuzione: python app/tests/test_riga_ordine.py
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
_ESEMPI = tempfile.mkdtemp(prefix='ft_rig_esempi_')
os.environ['FERROTRACK_ESEMPI_DIR'] = _ESEMPI

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

import ezdxf  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Preventivo, OrderFile  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_rig_{uuid.uuid4().hex[:8]}.db')
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
from backend.preventivi import ordine_testo as OT  # noqa: E402
from backend.preventivi import ordine_prezzi as OP  # noqa: E402

_CARTELLE = tempfile.mkdtemp(prefix='ft_rig_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)

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

# ── lettori del PDF simulati ────────────────────────────────────────────
LETTURE = []
FORMATO = {'tipo': 'deca'}


def fornitore_finto(dati):
    LETTURE.append('deca')
    if FORMATO['tipo'] != 'deca':
        return None
    return {'articoli': [
        {'codice': 'ALTRO-00', 'quantita': 1, '_qta_letta': 1.0, 'descrizione': 'Staffa', '_commessa': 'C26-100',
         'materiale': None, 'spessore_mm': None},
        {'codice': '13PA00434-00', 'quantita': 4, '_qta_letta': 4.0,
         'descrizione': 'PIASTRA FORATA S235JR SP.3', '_commessa': 'C26-108', 'materiale': 'FERRO', 'spessore_mm': 3.0},
        {'codice': '13PA00434-00', 'quantita': 2, '_qta_letta': 2.0,
         'descrizione': 'PIASTRA FORATA S235JR SP.3', '_commessa': 'C26-109', 'materiale': 'FERRO', 'spessore_mm': 3.0},
    ]}


def prezzato_finto(dati):
    LETTURE.append('prezzato')
    if FORMATO['tipo'] != 'prezzato':
        return None
    return {'righe': [{'pos': 10, 'codice': '13PA00434', 'quantita': 5, 'descrizione': 'TELAIO IN METALLO',
                       'dettaglio': 'L 1200 x P 400'}]}


OT.leggi_ordine_fornitore = fornitore_finto
OP.leggi_ordine_prezzato = prezzato_finto

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


def main():
    from tests.accesso_aiuto import postazioni, entra, modalita
    s = models.SessionLocal()
    s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
    s.commit(); s.close()
    modalita('protetto')
    postazioni()
    laser = entra(A.app, 'laser')

    base = {'quantita': 6, 'materiale': 'FERRO', 'spessore_mm': 3, 'costo_base_override': 5,
            'perimetro_taglio_m': 0.7, 'n_forature': 0, 'area_dm2': 3, 'bbox_w_mm': 200, 'bbox_h_mm': 150}
    pr = PreventivoManager.create('DECA S.r.l.', 'paolo', quantita=1, numero_ordine_cliente='RO-1')
    pid = pr['id'] if isinstance(pr, dict) else pr
    PreventivoManager.replace_articoli(pid, [
        dict(base, codice='13PA00434', dxf_filename='13PA00434-00.dxf'),
        dict(base, codice='SENZA-RIGA-00', dxf_filename='SENZA-RIGA-00.dxf',
             ordine={'stato': 'ok', 'pos': 3, 'qta': 5, 'file': 'ordine.pdf',
                     'righe': [{'pos': 7, 'qta': 3, 'commessa': 'C26-200'}, {'pos': 9, 'qta': 2, 'commessa': 'C26-201'}]}),
        dict(base, codice='NIENTE-00', dxf_filename='NIENTE-00.dxf'),
    ])
    s = models.SessionLocal()
    s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'INVIATO'
    s.commit(); s.close()
    oid = PreventivoManager.accetta_e_crea_ordine(pid, 'paolo').get('order_id')
    arts = {a['codice']: a for a in (PreventivoManager.get(pid, include_children=True) or {})['articoli']}
    cartella = os.path.join(A.DRAWINGS_FOLDER, oid)
    os.makedirs(cartella, exist_ok=True)
    for a in arts.values():
        doc = ezdxf.new('R2010', units=4)
        doc.modelspace().add_lwpolyline([(0, 0), (200, 0), (200, 150), (0, 150)], close=True)
        doc.saveas(os.path.join(cartella, a['dxf_filename']))
    pdf = os.path.join(_CARTELLE, 'ordine.pdf')
    with open(pdf, 'wb') as f:
        f.write(b'%PDF-1.4 finto')
    s = models.SessionLocal()
    s.add(OrderFile(id=str(uuid.uuid4()), order_id=oid, filename='ordine.pdf', filepath=pdf, file_type='PDF'))
    s.commit(); s.close()
    order = A._ordine_esistente(oid)

    print('1) Ordine DECA')
    r = AL.riga_ordine_di(order, arts['13PA00434'])
    check('trovata anche senza revisione, due righe (due commesse)', r and r['codice'] == '13PA00434-00'
          and [(x['pos'], x['quantita'], x['commessa']) for x in r['righe']] == [(2, 4.0, 'C26-108'), (3, 2.0, 'C26-109')], r)
    check('descrizione come la scrive il cliente', r and r['righe'][0]['descrizione'] == 'PIASTRA FORATA S235JR SP.3', r)
    check('materiale e spessore letti nella descrizione', r and r['materiale'] == 'FERRO' and r['spessore_mm'] == 3.0, r)
    check('il nome del PDF', r and r['file'] == 'ordine.pdf', r)

    print('\n2) Ordine prezzato')
    FORMATO['tipo'] = 'prezzato'
    os.utime(pdf, (1, 1))                           # file "cambiato": si rilegge
    r = AL.riga_ordine_di(order, arts['13PA00434'])
    check('posizione, quantita\', descrizione + dettaglio', r and [(x['pos'], x['quantita'], x['descrizione']) for x in r['righe']]
          == [(10, 5, 'TELAIO IN METALLO · L 1200 x P 400')], r)

    print('\n3) PDF che non si legge')
    FORMATO['tipo'] = 'nessuno'
    os.utime(pdf, (2, 2))
    r = AL.riga_ordine_di(order, arts['SENZA-RIGA-00'])
    check('le righe salvate al caricamento: quantita\' e commessa', r and [(x['quantita'], x['commessa']) for x in r['righe']]
          == [(3, 'C26-200'), (2, 'C26-201')] and not r['righe'][0]['descrizione'], r)
    check('niente da nessuna parte: None', AL.riga_ordine_di(order, arts['NIENTE-00']) is None)
    check('pezzo mancante: None', AL.riga_ordine_di(order, None) is None)

    print('\n4) /lettura')
    FORMATO['tipo'] = 'deca'
    os.utime(pdf, (3, 3))
    n0 = len(LETTURE)
    d = laser.get(f'/api/orders/{oid}/pezzi/{arts["13PA00434"]["id"]}/lettura').get_json() or {}
    check('/lettura ha riga_ordine', (d.get('riga_ordine') or {}).get('codice') == '13PA00434-00', d.get('riga_ordine'))
    n1 = len(LETTURE)
    d = laser.get(f'/api/orders/{oid}/pezzi/{arts["NIENTE-00"]["id"]}/lettura').get_json() or {}
    check('/lettura senza riga: riga_ordine null', d.get('success') and d.get('riga_ordine') is None, d.get('riga_ordine'))
    check('il PDF si legge una volta sola (cache)', n1 - n0 == 1 and len(LETTURE) == n1, (n0, n1, len(LETTURE)))

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
