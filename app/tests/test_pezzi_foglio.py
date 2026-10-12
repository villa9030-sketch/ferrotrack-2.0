"""Fogli con piu' pezzi (backend/api_pezzi_foglio.py, tipo_disegno.
pezzi_del_foglio_da_file): dal disegno unico ai pezzi per Lantek.

Copre:
 1. lettura su richiesta (classificatore spento): un foglio sintetico con 3
    pezzi diversi e 2 copie di uno -> 3 pezzi, quello ripetuto x2, in ordine
    di lettura (dall'alto a sinistra); con la cornice del foglio attorno
 2. GET /pezzi-foglio: codici proposti (codice del foglio -1, -2...; la
    scritta col codice accanto a un pezzo vince), quantita' = ordine x copie
 3. POST: errori (codice doppio, gia' nell'ordine, caratteri non validi,
    contorno fuori dal disegno); conferma con un pezzo tolto e un codice
    cambiato: DXF pulito per pezzo in LANTEK/<lamiera>/<codice>.dxf, pronto
    per Lantek; il pulito del foglio va in _DA PREPARARE; campi extra +
    decisione; registro degli esempi
 4. l'ordine: le righe sono i pezzi (non il foglio), con quantita' e tempi;
    Lantek simulato riceve i pezzi nuovi coi loro DXF e gli ordini di
    produzione con le quantita'; un codice gia' in Lantek ha solo l'ordine
 5. saturazione (/api/laser/banco): i pezzi, non il foglio
 6. annulla: via i DXF dei pezzi, il foglio torna da controllare

Gira su DATABASE TEMPORANEO, cartelle temporanee, LANTEK SIMULATO.
Esecuzione: python app/tests/test_pezzi_foglio.py
"""
import json
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
_ESEMPI = tempfile.mkdtemp(prefix='ft_pf_esempi_')
os.environ['FERROTRACK_ESEMPI_DIR'] = _ESEMPI

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

import ezdxf  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Preventivo  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_pf_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend import lantek as L  # noqa: E402
from backend.database import PreventivoManager  # noqa: E402
from backend.preventivi import tempi_laser as TL  # noqa: E402
from backend.preventivi import registro_esempi as RE  # noqa: E402
from backend.preventivi import tipo_disegno as TD  # noqa: E402
from backend.preventivi import dxf_polygon_detector_v3 as DET  # noqa: E402

_CARTELLE = tempfile.mkdtemp(prefix='ft_pf_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)

# ── Lantek simulato ─────────────────────────────────────────────────────
TL._leggi_lantek = lambda codici: None
PEZZI_LANTEK = {'ALTRO-00': {'esiste': True, 'codice_lantek': 'ALTRO-00', 'materiale': 'FERRO', 'spessore': 3.0,
                             'revisioni': []},
                'FOGLIO-B01': {'esiste': True, 'codice_lantek': 'FOGLIO-B01', 'materiale': 'FERRO', 'spessore': 3.0,
                              'revisioni': []}}
CHIAMATE = []
ORDINI_LT = {}


def pezzi_in_lantek(codici):
    vuoto = {'esiste': False, 'codice_lantek': None, 'materiale': None, 'spessore': None, 'revisioni': []}
    return {'disponibile': True, 'errore': None, 'pezzi': {c: dict(PEZZI_LANTEK.get(c) or vuoto) for c in codici}}


def pezzi_finti(pezzi, cartella, user_data=None, attesa_s=900):
    CHIAMATE.append(('pezzi', [(p['codice'], os.path.basename(p['dxf']), p['quantita']) for p in pezzi]))
    for p in pezzi:
        PEZZI_LANTEK[p['codice']] = {'esiste': True, 'codice_lantek': p['codice'], 'materiale': 'FERRO',
                                     'spessore': 3.0, 'revisioni': []}
    return {'eseguito': True, 'errore': None, 'cartella': cartella}


def xml_finto(contenuto, nome, cartella, attesa_s=300):
    CHIAMATE.append(('xml', contenuto))
    for c in list(PEZZI_LANTEK):
        if ('"%s"' % c).encode() in contenuto:
            ORDINI_LT[c.upper()] = 1.0
    return {'eseguito': True, 'errore': None, 'file_rapporto': 'x',
            'rapporto': {'totale': 1, 'ok': 1, 'avvisi': 0, 'con_errore': 0, 'errori': []}}


L.pezzi_in_lantek = pezzi_in_lantek
L.clienti_lantek = lambda: ['DECA']
L.ordini_in_lantek = lambda commessa: dict(ORDINI_LT)
L.ordini_fatti_in_lantek = lambda commessa: {}
L.pezzi_dell_ordine = lambda commessa: {}
L.procesos_disponibile = lambda: True
L.materiali_lantek_noti = lambda: {'FERRO', 'INOX'}
L.xmlimporter_disponibile = lambda: True
L.importa_pezzi_dxf = pezzi_finti
L.importa_xml = xml_finto

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


def rett(msp, x, y, w, h):
    msp.add_lwpolyline([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], close=True)


def scrivi_foglio(percorso, cornice=True):
    """Foglio con: A 300x200 con 2 fori (in alto a sinistra), B 150x100 con
    un foro (in alto a destra, scritta "FOGLIO-B01" accanto), C 120x80 senza
    fori due volte (in basso). Cornice 1000x700 col cartiglio."""
    doc = ezdxf.new('R2010', units=4)
    msp = doc.modelspace()
    if cornice:
        rett(msp, 0, 0, 1000, 700)
        rett(msp, 700, 0, 300, 60)            # cartiglio
        msp.add_text('FOGLIO-PROVA', dxfattribs={'height': 8, 'insert': (720, 20)})
    rett(msp, 50, 400, 300, 200)              # A
    msp.add_circle((120, 500), 15)
    msp.add_circle((280, 500), 15)
    rett(msp, 500, 450, 150, 100)             # B
    msp.add_circle((575, 500), 10)
    msp.add_text('FOGLIO-B01', dxfattribs={'height': 8, 'insert': (500, 430)})
    rett(msp, 50, 120, 120, 80)               # C
    rett(msp, 300, 120, 120, 80)              # C bis
    doc.saveas(percorso)


def esempi():
    p = os.path.join(RE.CARTELLA, RE.FILE)
    return [json.loads(x) for x in open(p, encoding='utf-8')] if os.path.isfile(p) else []


def extra(aid):
    s = models.SessionLocal()
    try:
        r = s.execute(text('SELECT extra_campi FROM preventivo_articoli WHERE id = :i'), {'i': aid}).fetchone()
        return json.loads(r[0] or '{}')
    finally:
        s.close()


def main():
    print('1) Lettura del foglio, su richiesta')
    check('classificatore del tipo di disegno spento', DET.TIPO_DISEGNO_ATTIVO is False)
    d1 = os.path.join(_CARTELLE, 'foglio.dxf')
    scrivi_foglio(d1)
    pz = TD.pezzi_del_foglio_da_file(d1, {})
    misure = [(round(p['bbox_width_mm']), round(p['bbox_height_mm']), p['quantita']) for p in pz]
    check('3 pezzi diversi, quello ripetuto x2', sorted(misure) == sorted([(300, 200, 1), (150, 100, 1), (120, 80, 2)]), misure)
    check("in ordine di lettura: A, B (in alto), poi C", misure == [(300, 200, 1), (150, 100, 1), (120, 80, 2)], misure)
    check('fori: A 2, B 1, C nessuno', [len(p['fori']) for p in pz] == [2, 1, 0], [len(p['fori']) for p in pz])
    check("copie: la seconda C c'e' per disegnarla", len(pz[2]['copie']) == 1 and not pz[0]['copie'])
    check('in mm, area di A coi fori', abs(pz[0]['area_dm2'] - (300 * 200 - 2 * 3.14159 * 225) / 1e4) < 0.02, pz[0]['area_dm2'])
    d2 = os.path.join(_CARTELLE, 'senza_cornice.dxf')
    scrivi_foglio(d2, cornice=False)
    check('anche senza cornice', len(TD.pezzi_del_foglio_da_file(d2, {})) == 3)

    from tests.accesso_aiuto import postazioni, entra, modalita
    s = models.SessionLocal()
    s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
    s.commit(); s.close()
    modalita('protetto')
    postazioni()
    laser = entra(A.app, 'laser')
    rep = entra(A.app, 'reparto')
    base = {'quantita': 3, 'materiale': 'FERRO', 'spessore_mm': 3, 'costo_base_override': 5,
            'perimetro_taglio_m': 3.4, 'n_forature': 1, 'area_dm2': 70, 'bbox_w_mm': 1000, 'bbox_h_mm': 700,
            'dxf_confidence': 0.4, 'dxf_needs_verify': True}
    pr = PreventivoManager.create('DECA S.r.l.', 'paolo', quantita=1, numero_ordine_cliente='PF-1')
    pid = pr['id'] if isinstance(pr, dict) else pr
    PreventivoManager.replace_articoli(pid, [
        dict(base, codice='FOGLIO-00', dxf_filename='FOGLIO-00.dxf'),
        dict(base, codice='ALTRO-00', quantita=1, cleaned_status='auto', dxf_confidence=0.9, dxf_needs_verify=False),
    ])
    s = models.SessionLocal()
    s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'INVIATO'
    s.commit(); s.close()
    oid = PreventivoManager.accetta_e_crea_ordine(pid, 'paolo').get('order_id')
    A.OrderManager.smista(oid, True, user_id='laser')
    aid = next(a['id'] for a in PreventivoManager.get(pid, include_children=True)['articoli'] if a['codice'] == 'FOGLIO-00')
    cartella = os.path.join(A.DRAWINGS_FOLDER, oid)
    os.makedirs(cartella)
    scrivi_foglio(os.path.join(cartella, 'FOGLIO-00.dxf'))
    lamiera = A._nome_lamiera('FERRO', 3)
    pulito_foglio = os.path.join(cartella, 'LANTEK', lamiera, 'FOGLIO-00.dxf')
    os.makedirs(os.path.dirname(pulito_foglio))
    shutil.copy2(os.path.join(cartella, 'FOGLIO-00.dxf'), pulito_foglio)
    url = f'/api/orders/{oid}/pezzi/{aid}/pezzi-foglio'

    print('\n2) Pezzi proposti')
    r = laser.get(url)
    d = r.get_json() or {}
    pp = d.get('pezzi') or []
    check('200, non ancora confermati, 3 pezzi', r.status_code == 200 and d.get('confermati') is False and len(pp) == 3, d.get('error'))
    check('codici: dal foglio -1 / scritta accanto / -3',
          [p['codice'] for p in pp] == ['FOGLIO-00-1', 'FOGLIO-B01', 'FOGLIO-00-3']
          and [p['codice_da'] for p in pp] == ['proposto', 'disegno', 'proposto'], [(p['codice'], p['codice_da']) for p in pp])
    check("quantita' = 3 fogli x copie (3, 3, 6)", [p['qta'] for p in pp] == [3, 3, 6] and d.get('qta_foglio') == 3,
          [p['qta'] for p in pp])
    check('le copie da disegnare', len(pp[2]['copie']) == 1)
    check('il tablet officina no', rep.get(url).status_code in (401, 403))

    print('\n3) Conferma')
    def corpo(lista):
        return {'pezzi': [{'codice': p['codice'], 'outer_xy': p['outer_xy'], 'holes_xy': p['holes_xy'],
                           'qta_per_foglio': p['qta_per_foglio'], 'qta': p['qta']} for p in lista]}
    c = corpo(pp)
    c['pezzi'][1]['codice'] = 'FOGLIO-00-1'
    r = laser.post(url, json=c)
    check('codice doppio: 400', r.status_code == 400 and r.get_json().get('codice') == 'codice_doppio', r.get_json())
    c = corpo(pp)
    c['pezzi'][0]['codice'] = 'ALTRO-00'
    r = laser.post(url, json=c)
    check("codice gia' nell'ordine: 400", r.status_code == 400 and 'ALTRO-00' in r.get_json().get('error', ''), r.get_json())
    c = corpo(pp)
    c['pezzi'][0]['codice'] = 'A/B?'
    r = laser.post(url, json=c)
    check('codice con caratteri non validi: 400', r.status_code == 400 and r.get_json().get('codice') == 'codice_non_valido')
    c = corpo(pp)
    c['pezzi'][0]['outer_xy'] = [[5000, 5000], [5100, 5000], [5100, 5100], [5000, 5100]]
    r = laser.post(url, json=c)
    check('contorno fuori dal disegno: 400', r.status_code == 400, r.get_json())
    check('niente scritto dopo gli errori', not os.path.isdir(os.path.join(cartella, 'LANTEK', lamiera))
          or sorted(os.listdir(os.path.join(cartella, 'LANTEK', lamiera))) == ['FOGLIO-00.dxf'])
    check('il tablet officina no', rep.post(url, json=corpo(pp)).status_code in (401, 403))
    # tolto il pezzo A (era una vista), codice di C cambiato, quantita' di B a mano
    c = corpo(pp[1:])
    c['pezzi'][1]['codice'] = 'STAFFA-C'
    c['pezzi'][0]['qta'] = 4
    r = laser.post(url, json=c)
    j = r.get_json() or {}
    check('confermati 2 pezzi', r.status_code == 200 and j.get('n_pezzi') == 2, j)
    file_b = os.path.join(cartella, 'LANTEK', lamiera, 'FOGLIO-B01.dxf')
    file_c = os.path.join(cartella, 'LANTEK', lamiera, 'STAFFA-C.dxf')
    check('un DXF per pezzo in LANTEK/<lamiera>/<codice>.dxf', os.path.isfile(file_b) and os.path.isfile(file_c))
    from backend.preventivi import dxf_cleanup as DXC
    v = DXC.verifica_lantek(file_c, {'bbox_w_mm': 120, 'bbox_h_mm': 80, 'area_dm2': 0.96})
    check('DXF del pezzo pronto per Lantek', v.get('stato') == 'pronto', v)
    check('il pulito del foglio non parte: in _DA PREPARARE', not os.path.exists(pulito_foglio) and os.path.isfile(
        os.path.join(cartella, 'LANTEK', A.CARTELLA_DA_PREPARARE, lamiera, 'FOGLIO-00.dxf')))
    ex = extra(aid)
    check('campi extra: pezzi_foglio + decisione', [p['codice'] for p in ex.get('pezzi_foglio') or []] == ['FOGLIO-B01', 'STAFFA-C']
          and (ex.get('decisione_laser') or {}).get('scelta') == 'piu_pezzi_confermati', ex.get('decisione_laser'))
    ev = [e for e in esempi() if e['evento'] == 'pezzi_del_foglio']
    check('nel registro degli esempi: letti 3, confermati 2', ev and len(ev[-1]['motore']['pezzi']) == 3
          and [p['codice'] for p in ev[-1]['decisione']['pezzi']] == ['FOGLIO-B01', 'STAFFA-C'], ev[-1:])
    d = laser.get(url).get_json()
    check('GET dopo la conferma: quelli confermati', d.get('confermati') and [p['codice'] for p in d['pezzi']] == ['FOGLIO-B01', 'STAFFA-C'])

    print("\n4) L'ordine e Lantek")
    o = laser.get(f'/api/orders/{oid}/laser').get_json() or {}
    pz = {p['codice']: p for p in o.get('pezzi') or []}
    check('righe: i pezzi, non il foglio', sorted(pz) == ['ALTRO-00', 'FOGLIO-B01', 'STAFFA-C'], sorted(pz))
    check("quantita': B a mano 4, C 3 fogli x 2 = 6", pz['FOGLIO-B01']['quantita'] == 4 and pz['STAFFA-C']['quantita'] == 6,
          (pz['FOGLIO-B01']['quantita'], pz['STAFFA-C']['quantita']))
    check('da dove vengono: il foglio', (pz['STAFFA-C'].get('foglio') or {}).get('articolo_id') == aid)
    check("niente da controllare e niente bloccato", o.get('n_controllare') == 0 and not o.get('bloccati'), (o.get('n_controllare'), o.get('bloccati')))
    inv = o.get('invio') or {}
    check("parte: STAFFA-C nuovo da creare, FOGLIO-B01 gia' in Lantek (solo ordine)",
          inv.get('nuovi') == ['STAFFA-C'] and sorted(inv.get('codici') or []) == ['ALTRO-00', 'FOGLIO-B01'], inv)
    r = laser.post(f'/api/orders/{oid}/lantek-invia', json={'codici': inv['codici'], 'nuovi': inv['nuovi']})
    j = r.get_json() or {}
    xml = next((c[1] for c in CHIAMATE if c[0] == 'xml'), b'')
    pezzi_mandati = next((c[1] for c in CHIAMATE if c[0] == 'pezzi'), [])
    check('mandato', r.status_code == 200 and j.get('pezzi_creati') == 1, j)
    check('Lantek crea STAFFA-C col suo DXF e la sua quantita\'', pezzi_mandati == [('STAFFA-C', 'STAFFA-C.dxf', 6)], pezzi_mandati)
    check('ordini di produzione: i pezzi, non il foglio', b'"STAFFA-C"' in xml and b'"FOGLIO-B01"' in xml
          and b'FOGLIO-00' not in xml)
    import re as _re
    q = {m.group(1).decode(): int(m.group(2)) for m in _re.finditer(
        rb'FldRef="Product" FldValue="([^"]+)".*?FldRef="Quantity" FldValue="(\d+)"', xml, _re.S)}
    check("con le quantita' dei pezzi (STAFFA-C 6, FOGLIO-B01 4)", q.get('STAFFA-C') == 6 and q.get('FOGLIO-B01') == 4, q)

    print('\n5) Saturazione')
    b = laser.get('/api/laser/banco').get_json() or {}
    cod = [p['codice'] for g in b.get('gruppi') or [] for p in g['pezzi'] if p['ordine_id'] == oid]
    check('il banco conta i pezzi, non il foglio', sorted(cod) == ['ALTRO-00', 'FOGLIO-B01', 'STAFFA-C'], cod)
    tempi = [p.get('tempo_min') for g in b.get('gruppi') or [] for p in g['pezzi'] if p['codice'] == 'STAFFA-C']
    check('col tempo del pezzo', tempi and tempi[0] and tempi[0] > 0, tempi)

    print('\n5b) Il foglio diviso non prende altre decisioni')
    for u2, c2 in ((f'/api/orders/{oid}/pezzi/{aid}/giusto', {'serve_pulito': True}),
                   (f'/api/orders/{oid}/pezzi/{aid}/decisione', {'scelta': 'non_laser'}),
                   (f'/api/orders/{oid}/pezzi/{aid}/contorno', {'outer_xy': [[50, 120], [170, 120], [170, 200], [50, 200]]})):
        r = laser.post(u2, json=c2)
        check(f"{u2.rsplit('/', 1)[-1]}: 409, prima si annullano i pezzi", r.status_code == 409
              and (r.get_json() or {}).get('codice') == 'pezzi_foglio', (r.status_code, r.get_json()))

    print('\n6) Annulla')
    r = laser.delete(url)
    check('annullato', r.status_code == 200 and r.get_json().get('tolti') == 2, r.get_json())
    check('via i DXF dei pezzi', not os.path.exists(file_b) and not os.path.exists(file_c))
    ex = extra(aid)
    check('campi extra tolti', 'pezzi_foglio' not in ex and 'decisione_laser' not in ex, ex.keys())
    o = laser.get(f'/api/orders/{oid}/laser').get_json() or {}
    pz = {p['codice']: p for p in o.get('pezzi') or []}
    check('il foglio torna da controllare', 'FOGLIO-00' in pz and pz['FOGLIO-00']['da_controllare'], pz.get('FOGLIO-00'))
    check('annullare di nuovo: 409', laser.delete(url).status_code == 409)
    check('nel registro: annullati', any(e['evento'] == 'pezzi_del_foglio_annullati' for e in esempi()))

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
