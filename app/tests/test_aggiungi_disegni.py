"""Aggiungere a un ordine gia' caricato gli assiemi mancanti (DECA A 001252).

Copre:
 1. il piano: solo gli assiemi che l'ordine non ha; i pezzi sciolti gia'
    caricati non si ripetono; un componente con lo stesso codice di un pezzo
    gia' nell'ordine (anche gia' mandato a Lantek) NON si aggiunge, e' nel
    piano come "gia' nell'ordine"; l'ordine non cambia
 2. la conferma: componenti nell'ordine col loro assieme, la riga-assieme
    senza disegno diventa l'assieme (con la quantita' dell'ordine), i pezzi
    al posto della riga dell'assieme, il preventivo d'appoggio sparisce
 3. una seconda volta: niente da aggiungere (niente doppioni)
 4. scarta: via l'analisi, l'ordine non cambia
 5. la distinta: lo stesso codice in due assiemi prende i pezzi per assieme
    dalla distinta del SUO assieme (prima restava "da confermare")
 6. zip_da_cartella: le sottocartelle restano assiemi; chiave dei codici

DATABASE TEMPORANEO; la lettura del pacchetto e l'analisi dei DXF sono
sostituite da un finto (si prova la logica dell'aggiunta, non il motore).
Esecuzione: python app/tests/test_aggiungi_disegni.py
"""
import io
import json
import os
import sys
import tempfile
import uuid
import zipfile

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
os.environ['FERROTRACK_ESEMPI_DIR'] = tempfile.mkdtemp(prefix='ft_ad_esempi_')
_CFG = os.path.join(tempfile.mkdtemp(prefix='ft_ad_cfg_'), 'app_config.json')
with open(_CFG, 'w', encoding='utf-8') as f:
    json.dump({}, f)
os.environ['FERROTRACK_CONFIG'] = _CFG

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, LantekInvio, Order  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_ad_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend import aggiungi_disegni as AD  # noqa: E402
from backend import assiemi_ordine as AO  # noqa: E402
from backend.database import ConfigManager, PreventivoManager, get_session  # noqa: E402
from backend.preventivi import rfq_importer  # noqa: E402
from backend.preventivi.rfq_importer import ArticoloRFQ, RFQParseResult  # noqa: E402

ConfigManager.load_config()
assert os.path.abspath(ConfigManager._CONFIG_PATH) == os.path.abspath(_CFG), 'impostazioni NON temporanee'
_CART = tempfile.mkdtemp(prefix='ft_ad_')
A.UPLOAD_FOLDER = os.path.join(_CART, 'uploads')
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
        print(f'  [KO] {nome}  {extra}')


# ── finti: pacchetto letto e pezzi preparati ───────────────────────────────
def _pacchetto(_zip, tollerante=False):
    r = RFQParseResult(success=True)
    r.cliente, r.numero_ordine_cliente = 'DECA S.r.l.', 'T 1'
    r.assiemi = ['SA1', 'SA2']
    r.qta_assiemi = {'SA1': 1, 'SA2': 1}
    r.articoli = [
        ArticoloRFQ(codice='PA1', quantita=2, matched_dxf='PA1-00.dxf'),            # gia' nell'ordine
        ArticoloRFQ(codice='SA1', quantita=1, matched_dxf='SA1-00.dxf', codice_assieme='SA1'),
        ArticoloRFQ(codice='C1-00', quantita=2, matched_dxf='C1-00.dxf', codice_assieme='SA1', qta_fonte='distinta'),
        ArticoloRFQ(codice='PA1-00', quantita=1, matched_dxf='PA1-00.dxf', codice_assieme='SA1',
                    qta_fonte='distinta'),                                           # = pezzo gia' mandato
        ArticoloRFQ(codice='SA2', quantita=1, matched_dxf='SA2-00.dxf', codice_assieme='SA2'),  # gia' fatto
        ArticoloRFQ(codice='C9-00', quantita=1, matched_dxf='C9-00.dxf', codice_assieme='SA2', qta_fonte='distinta'),
    ]
    for a in r.articoli:
        if a.codice in ('SA1', 'SA2'):
            a.e_master = True
    r.dxf_no_match = []
    return r


VISTI = {}


def _crea(result, zip_bytes, nome, *, creato_da, da_prezzare=False, solo_tecnico=False):
    VISTI['codici'] = [a.codice for a in result.articoli]
    VISTI['assiemi'] = list(result.assiemi)
    VISTI['qta'] = dict(result.qta_assiemi)
    p = PreventivoManager.create('DECA S.r.l.', creato_da, solo_tecnico=True)
    os.makedirs(A._cartella_preventivo(p['id']), exist_ok=True)
    arts = [{'codice': a.codice, 'quantita': a.quantita, 'codice_assieme': a.codice_assieme,
             'materiale': 'S235', 'spessore_mm': 3, 'dxf_filename': a.matched_dxf,
             'ordine': {'stato': 'ok', 'pos': None, 'qta': a.quantita}}
            for a in result.articoli if not a.e_master]
    PreventivoManager.replace_articoli(p['id'], arts)
    PreventivoManager.replace_assiemi(p['id'], [{'codice_assieme': c, 'qty': result.qta_assiemi.get(c) or 1,
                                                 'componenti_qty': {}, 'ore_montaggio': 0, 'costo': 0}
                                                for c in result.assiemi])
    return {'preventivo_id': p['id'], 'articoli_db': arts, 'saved_tasks': [], 'n_step': 0, 'dxf_results': {}}


COPIE = []
rfq_importer.process_rfq_package = _pacchetto
A._crea_preventivo_da_pacchetto = _crea
A._copy_cleaned_dxf_to_drawings = lambda pid, oid: (COPIE.append(pid) or {'lantek_pronti': 0, 'lantek_da_guardare': []})


def ordine():
    """Ordine come A 001252: PA1 sciolto (mandato a Lantek), SA1 riga senza
    disegno (l'assieme rimasto senza cartella), SA2 gia' assieme col suo C2."""
    p = PreventivoManager.create('DECA S.r.l.', None, solo_tecnico=True)
    PreventivoManager.replace_articoli(p['id'], [
        {'codice': 'PA1', 'quantita': 2, 'materiale': 'S235', 'spessore_mm': 3, 'dxf_filename': 'PA1-00.dxf',
         'ordine': {'stato': 'controllato', 'pos': 1}},
        {'codice': 'SA1', 'quantita': 3, 'materiale': 'S235', 'ordine': {'stato': 'controllato', 'pos': 2,
                                                                          'senza_disegno': True}},
        {'codice': 'C2-00', 'quantita': 1, 'codice_assieme': 'SA2', 'materiale': 'S235', 'spessore_mm': 2,
         'dxf_filename': 'C2-00.dxf'},
        {'codice': 'PZ9', 'quantita': 1, 'materiale': 'S235', 'spessore_mm': 3, 'dxf_filename': 'PZ9-00.dxf',
         'ordine': {'stato': 'controllato', 'pos': 4}},
    ])
    PreventivoManager.replace_assiemi(p['id'], [{'codice_assieme': 'SA2', 'qty': 1, 'componenti_qty': {},
                                                 'ore_montaggio': 0, 'costo': 0}])
    PreventivoManager.aggiorna_tecnico(p['id'], status='ACCETTATO')
    o = A.OrderManager.create_order(cliente='DECA S.r.l.', data_consegna='2026-12-01', numero_ordine='T 1',
                                    note='', origine='PACCHETTO', preventivo_id_origine=p['id'])
    s = get_session()
    s.add(LantekInvio(order_id=o.id, codice='PA1-00', quantita=2, origine='ft'))
    s.commit()
    s.close()
    return o.id, p['id']


def righe(pid):
    r, ass = AD._righe_ordine(pid)
    return [(x['codice'], x['quantita'], x['codice_assieme']) for x in r], ass


def main():
    oid, pid = ordine()
    prima = righe(pid)

    print('1. piano')
    p = AD.analizza(oid, b'zip', 'pacchetto')
    check('si prepara solo l\'assieme che manca (SA1)', VISTI['assiemi'] == ['SA1'], VISTI)
    check('SA2 (gia\' nell\'ordine) e PA1 sciolto non ripassano', 'C9-00' not in VISTI['codici']
          and 'PA1' not in VISTI['codici'], VISTI['codici'])
    check('PA1-00 nell\'assieme = pezzo gia\' mandato: non aggiunto, nel piano',
          'PA1-00' not in VISTI['codici'] and [u['codice'] for u in p['uniti']] == ['PA1-00']
          and p['uniti'][0]['mandato_lantek'], p['uniti'])
    check('quantita\' dell\'assieme dalla riga dell\'ordine (3)', VISTI['qta'].get('SA1') == 3, VISTI['qta'])
    check('piano: SA1 x3 con C1-00 2/assieme = 6', p['assiemi'][0]['codice'] == 'SA1' and p['assiemi'][0]['quantita'] == 3
          and p['assiemi'][0]['pezzi'][0]['totale'] == 6 and p['assiemi'][0]['da_riga'], p['assiemi'])
    check('l\'ordine non e\' cambiato', righe(pid) == prima)
    check('descrivi: testo leggibile', 'ASSIEME SA1 x3' in AD.descrivi(p))

    print('2. conferma')
    r = AD.applica(oid, p['tecnico_id'], 'prova', esporta=False)
    dopo, ass = righe(pid)
    check('esito: 1 pezzo, SA1, riga convertita', r['aggiunti'] == 1 and r['assiemi'] == ['SA1']
          and r['righe_assieme_convertite'] == ['SA1'], r)
    check('righe nell\'ordine del PDF: C1 al posto della riga SA1', [x[0] for x in dopo] == ['PA1', 'C1-00', 'C2-00', 'PZ9'], dopo)
    check('C1-00 nell\'assieme SA1 con 2 per assieme', ('C1-00', 2, 'SA1') in dopo, dopo)
    check('assiemi: SA1 x3 e SA2 x1', ass == {'SA1': 3, 'SA2': 1}, ass)
    check('disegni copiati dal preventivo d\'appoggio', COPIE == [p['tecnico_id']], COPIE)
    check('preventivo d\'appoggio cancellato', PreventivoManager.get(p['tecnico_id']) is None)
    check('PA1 una volta sola', sum(1 for x in dopo if AD.chiave(x[0]) == 'PA1') == 1, dopo)

    print('3. una seconda volta')
    try:
        AD.analizza(oid, b'zip', 'pacchetto')
        check('rifiutata', False)
    except AD.Errore as e:
        check('niente da aggiungere (409)', e.codice == 'niente_da_aggiungere' and e.stato == 409, e.codice)
    try:
        AD.applica(oid, p['tecnico_id'], 'prova', esporta=False)
        check('conferma ripetuta rifiutata', False)
    except AD.Errore as e:
        check('conferma ripetuta rifiutata', e.codice == 'aggiunta_non_trovata', e.codice)

    print('4. scarta')
    oid2, pid2 = ordine()
    prima2 = righe(pid2)
    p2 = AD.analizza(oid2, b'zip', 'pacchetto')
    check('scarta: ok', AD.scarta(oid2, p2['tecnico_id']))
    check('scarta: ordine com\'era e analisi sparita', righe(pid2) == prima2 and PreventivoManager.get(p2['tecnico_id']) is None)
    check('scarta con un altro ordine: no', not AD.scarta(oid, p2['tecnico_id']))

    print('5. distinta, stesso codice in due assiemi')
    res = RFQParseResult(success=True)
    res.assiemi = ['X1', 'X2']
    res.articoli = [ArticoloRFQ(codice='K-00', quantita=1, matched_dxf='K-00.dxf', codice_assieme='X1', qta_fonte='per_assieme'),
                    ArticoloRFQ(codice='K-00', quantita=1, matched_dxf='K-00.dxf', codice_assieme='X2', qta_fonte='per_assieme')]
    res.dxf_no_match = []
    vero = AO.distinta_assieme
    AO.distinta_assieme = lambda d, r, c: {'K': {'qta': 2 if c == 'X1' else 3, 'testo': '', 'dubbio': False}}
    try:
        n = AO.quantita_da_distinte(_CART, res)
    finally:
        AO.distinta_assieme = vero
    check('X1: 2, X2: 3, nessuno da confermare', n == 2 and [(a.codice_assieme, a.quantita, a.qta_fonte) for a in res.articoli]
          == [('X1', 2, 'distinta'), ('X2', 3, 'distinta')], [(a.codice_assieme, a.quantita, a.qta_fonte) for a in res.articoli])

    print('6. cartella e codici')
    d = os.path.join(_CART, 'LS 9', 'SA1-00')
    os.makedirs(d)
    open(os.path.join(_CART, 'LS 9', 'ordine.pdf'), 'wb').write(b'%PDF-1.4')
    open(os.path.join(d, 'C1-00.dxf'), 'w').write('0\nEOF\n')
    open(os.path.join(d, 'Thumbs.db'), 'w').write('x')
    nomi = sorted(zipfile.ZipFile(io.BytesIO(AD.zip_da_cartella(os.path.join(_CART, 'LS 9')))).namelist())
    check('zip con la sottocartella dell\'assieme, senza Thumbs.db', nomi == ['LS 9/SA1-00/C1-00.dxf', 'LS 9/ordine.pdf'], nomi)
    check('chiave', AD.chiave('25NDSPA1979-00.DXF') == AD.chiave('25NDSPA1979') == '25NDSPA1979'
          and AD.chiave('x (2).dxf') == 'X')

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
