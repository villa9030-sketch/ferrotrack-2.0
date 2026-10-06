"""Quantita' per Lantek: il file Excel da importare con iErp.

Copre:
 1. dalla distinta: solo lamiere col codice, quantita' sommate (lotto, assiemi)
 2. con Lantek: materiale e spessore come in Lantek per i codici che ci sono;
    avvisi per codici nuovi, materiale/spessore diversi, revisione piu' nuova
 3. senza Lantek (spento): il file si fa lo stesso coi dati di FerroTrack
 4. il file: Foglio1, intestazione del modello ImportProduzione di iErp,
    data di consegna come data, commessa = numero dell'ordine
 5. API: elenco coi controlli e download per laser e ufficio, non per l'officina
 6. nella cartella dell'ordine ("Crea cartella") c'e' anche il file

Lantek e' SIMULATO: i test non toccano il database vero di Lantek.
Gira su DATABASE TEMPORANEO. Esecuzione: python app/tests/test_lantek_quantita.py
"""
import io
import json
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

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Order, Preventivo  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_ltq_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend import lantek as L  # noqa: E402
from backend.database import PreventivoManager  # noqa: E402

_CARTELLE = tempfile.mkdtemp(prefix='ft_ltq_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)
_RETE = os.path.join(_CARTELLE, 'rete')
_cfg_vera = A.ConfigManager.load_config
A.ConfigManager.load_config = staticmethod(lambda: {**(_cfg_vera() or {}), 'disegni_export_root': _RETE})

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


LANTEK_FINTO = {'disponibile': True, 'errore': None, 'pezzi': {
    'A1-00': {'esiste': True, 'materiale': 'FERRO', 'spessore': 3.0, 'revisioni': []},
    'B1-00': {'esiste': True, 'materiale': 'INOX', 'spessore': 1.5, 'revisioni': ['B1-01']},
    'C1-00': {'esiste': False, 'materiale': None, 'spessore': None, 'revisioni': []},
}}


def preventivo_accettato(cliente, numero_cliente):
    pr = PreventivoManager.create(cliente, 'paolo', quantita=2, numero_ordine_cliente=numero_cliente)
    pid = pr['id'] if isinstance(pr, dict) else pr
    PreventivoManager.replace_articoli(pid, [
        {'codice': 'A1-00', 'quantita': 3, 'materiale': 'S235', 'spessore_mm': 3, 'costo_base_override': 5},
        {'codice': 'B1-00', 'quantita': 2, 'materiale': 'INOX_304', 'spessore_mm': 2,
         'codice_assieme': 'ASS1', 'costo_base_override': 4},
        {'codice': 'C1-00', 'quantita': 1, 'materiale': 'ALU', 'spessore_mm': 4, 'costo_base_override': 3},
    ])
    PreventivoManager.replace_assiemi(pid, [{'codice_assieme': 'ASS1', 'qty': 4, 'costo': 10}])
    PreventivoManager.replace_tubolari(pid, [{'profilo': 'Quadro 40x40 sp.2mm', 'lunghezza_m': 0.5, 'qty': 2}])
    s = models.SessionLocal()
    try:
        s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'INVIATO'
        s.commit()
    finally:
        s.close()
    return PreventivoManager.accetta_e_crea_ordine(pid, 'paolo').get('order_id')


def main():
    s = models.SessionLocal()
    s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
    s.commit(); s.close()

    print('1) Righe dalla distinta')
    distinta = [
        {'tipo': 'lamiera', 'codice': 'A1-00', 'quantita': 6, 'materiale': 'S235', 'spessore_mm': 3},
        {'tipo': 'lamiera', 'codice': 'A1-00', 'quantita': 2, 'materiale': 'S235', 'spessore_mm': 3},
        {'tipo': 'lamiera', 'codice': 'B1-00', 'quantita': 8, 'materiale': 'INOX_304', 'spessore_mm': 2},
        {'tipo': 'lamiera', 'codice': 'C1-00', 'quantita': 2, 'materiale': 'ALU', 'spessore_mm': 4},
        {'tipo': 'tubolare', 'codice': 'Quadro', 'quantita': 2},
        {'tipo': 'assieme', 'codice': 'ASS1', 'quantita': 4},
        {'tipo': 'lamiera', 'codice': '', 'quantita': 1},
    ]
    r = {x['codice']: x for x in L.righe_quantita(distinta, LANTEK_FINTO)}
    check('solo lamiere col codice', sorted(r) == ['A1-00', 'B1-00', 'C1-00'], sorted(r))
    check('quantita sommate per codice (6 + 2 = 8)', r['A1-00']['quantita'] == 8, r['A1-00'])

    print('\n2) Con Lantek')
    check('A1: in Lantek, FERRO 3, nessun avviso', r['A1-00']['stato'] == 'in_lantek'
          and r['A1-00']['materiale'] == 'FERRO' and r['A1-00']['spessore'] == 3.0 and not r['A1-00']['avvisi'], r['A1-00'])
    b = r['B1-00']
    check('B1: si usa lo spessore di Lantek (1,5) e si avvisa (ordine 2)',
          b['spessore'] == 1.5 and any('spessore' in a for a in b['avvisi']), b)
    check('B1: avviso revisione piu nuova (B1-01)', any('B1-01' in a for a in b['avvisi']), b['avvisi'])
    c = r['C1-00']
    check('C1: nuovo, materiale ALLUMINIO dalla tabella', c['stato'] == 'nuovo' and c['materiale'] == 'ALLUMINIO'
          and any('importa prima il disegno' in a for a in c['avvisi']), c)

    print('\n2b) Codice scritto senza revisione, commessa')
    finto = {'disponibile': True, 'errore': None, 'pezzi': {
        'D1': {'esiste': True, 'codice_lantek': 'D1-00', 'materiale': 'FERRO', 'spessore': 2.0, 'revisioni': ['D1-00']},
        'E1': {'esiste': False, 'codice_lantek': None, 'materiale': None, 'spessore': None, 'revisioni': ['E1-00', 'E1-01']},
    }}
    rr = {x['codice_ft']: x for x in L.righe_quantita([
        {'tipo': 'lamiera', 'codice': 'D1', 'quantita': 2, 'materiale': 'S235', 'spessore_mm': 2},
        {'tipo': 'lamiera', 'codice': 'E1', 'quantita': 1, 'materiale': 'S235', 'spessore_mm': 2}], finto)}
    check('D1 -> nel file il codice di Lantek D1-00', rr['D1']['codice'] == 'D1-00' and rr['D1']['stato'] == 'in_lantek'
          and not rr['D1']['avvisi'], rr['D1'])
    check('E1 con due revisioni in Lantek: non si sceglie, si avvisa',
          rr['E1']['codice'] == 'E1' and any('scegli' in a for a in rr['E1']['avvisi']), rr['E1'])
    check('commessa: "A 001252" -> "1252", "061" resta, PREV resta',
          (L.commessa_lantek('A 001252'), L.commessa_lantek('061'), L.commessa_lantek('PREV-2026-0005'))
          == ('1252', '061', 'PREV-2026-0005'))

    print('\n3) Senza Lantek')
    r2 = {x['codice']: x for x in L.righe_quantita(distinta, {'disponibile': False, 'pezzi': {}})}
    check('il file si fa coi dati di FerroTrack', r2['B1-00']['materiale'] == 'INOX' and r2['B1-00']['spessore'] == 2.0
          and r2['B1-00']['stato'] == 'sconosciuto' and not r2['B1-00']['avvisi'], r2['B1-00'])

    print('\n4) Il file Excel')
    buf = io.BytesIO()
    L.scrivi_excel(buf, list(r.values()), '2030-11-06', '1252')
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(buf.getvalue())).active
    check('foglio "Foglio1"', ws.title == 'Foglio1')
    intest = [c.value for c in ws[1]]
    check('intestazione del modello iErp', intest == ['Codice Articolo', 'Quantità', 'Materiale', 'Spessore',
                                                      'Data consegna', 'Commessa'], intest)
    riga = [c.value for c in ws[2]]
    check('riga: codice, quantita, materiale, spessore, data, commessa',
          riga[0] == 'A1-00' and riga[1] == 8 and riga[2] == 'FERRO' and riga[3] == 3.0
          and isinstance(riga[4], datetime) and riga[4].date().isoformat() == '2030-11-06' and riga[5] == '1252', riga)

    print('\n5) API')
    from tests.accesso_aiuto import postazioni, persona, entra, modalita
    modalita('protetto')
    postazioni()
    persona('ufficio', 'Elena Ufficio', 'Amministrazione', pin='2580')
    uff = entra(A.app, 'ufficio', pin='2580')
    laser = entra(A.app, 'laser')
    rep = entra(A.app, 'reparto')
    L.pezzi_in_lantek = lambda codici: {**LANTEK_FINTO, 'pezzi': {k: v for k, v in LANTEK_FINTO['pezzi'].items() if k in codici}}
    oid = preventivo_accettato('DECA S.r.l.', '1252')
    check('ordine creato', bool(oid))
    s = models.SessionLocal()
    o = s.query(Order).filter(Order.id == oid).first()
    o.data_consegna = datetime(2030, 11, 6)
    s.commit(); s.close()
    d = laser.get(f'/api/orders/{oid}/lantek-quantita').get_json() or {}
    check('il laser vede le righe coi controlli', d.get('success') and d.get('n_codici') == 3
          and d.get('n_nuovi') == 1 and d.get('commessa') == '1252', {k: d.get(k) for k in ('n_codici', 'n_nuovi', 'commessa', 'error')})
    q = {x['codice']: x['quantita'] for x in d.get('righe') or []}
    check('quantita: A1 3x2=6, B1 2x4=8, C1 1x2=2', q == {'A1-00': 6, 'B1-00': 8, 'C1-00': 2}, q)
    rx = uff.get(f'/api/orders/{oid}/lantek-quantita.xlsx')
    check('l\'ufficio scarica l\'Excel', rx.status_code == 200 and rx.data[:2] == b'PK', rx.status_code)
    check('nome del file col numero del cliente', '1252' in (rx.headers.get('Content-Disposition') or ''),
          rx.headers.get('Content-Disposition'))
    check('il tablet officina no', rep.get(f'/api/orders/{oid}/lantek-quantita').status_code in (401, 403))

    print('\n6) Nella cartella dell\'ordine')
    os.makedirs(os.path.join(A.DRAWINGS_FOLDER, oid), exist_ok=True)
    import ezdxf as _ez
    _d = _ez.new()
    _d.modelspace().add_lwpolyline([(0, 0), (100, 0), (100, 50), (0, 50)], close=True)
    _d.saveas(os.path.join(A.DRAWINGS_FOLDER, oid, 'A1-00.dxf'))
    e = A._esporta_disegni_per_officina(oid)
    fatti = [f for _b, _d2, fs in os.walk(e.get('percorso') or '') for f in fs]
    check('nella cartella niente Excel delle quantita',
          not any(f.endswith('.xlsx') for f in fatti) and 'quantita_lantek' not in e, (e, fatti))

    print('\n7) Scritte dentro i DXF per l\'importatore di Lantek')
    t = L.testi_dati({'quantita': 4, 'materiale': 'FERRO', 'spessore': 1.5}, '1252', ',', 'DECA S.r.l.', '2030-11-06')
    check('formato provato in Lantek: etichetta, spazio, una parola',
          t == ['QTA 4', 'MAT FERRO', 'SP 1,5', 'ORD 1252', 'CLI DECA_S.r.l.', 'CONS 06/11/2030'], t)
    import ezdxf
    from shapely.geometry import Point, Polygon
    pdxf = os.path.join(_CARTELLE, 'pezzo.dxf')
    doc = ezdxf.new(); doc.header['$INSUNITS'] = 4
    # pezzo a L: il centro dell'ingombro cade FUORI dal pezzo
    forma = [(0, 0), (300, 0), (300, 40), (40, 40), (40, 300), (0, 300)]
    doc.modelspace().add_lwpolyline(forma, close=True)
    doc.saveas(pdxf)
    prima = open(pdxf, 'rb').read()
    out = L.dxf_con_dati(pdxf, t)
    import io as _io
    d2 = ezdxf.read(_io.StringIO(out.decode('utf-8', 'replace')))
    testi = [e for e in d2.modelspace().query('TEXT')]
    check('le scritte ci sono', [e.dxf.text for e in testi] == t, [e.dxf.text for e in testi])
    check('la prima scritta e\' dentro il pezzo (anche a L)',
          Polygon(forma).contains(Point(testi[0].dxf.insert[0], testi[0].dxf.insert[1])), testi[0].dxf.insert)
    check('il file d\'origine non si tocca', open(pdxf, 'rb').read() == prima)
    dati = {'25ab1979': ('25AB1979-00', ['QTA 2'])}
    check('nome del file col codice di Lantek', A._voce_per_lantek('X/S235 - 3 mm/25AB1979.dxf', dati)
          == ('X/S235 - 3 mm/25AB1979-00.dxf', ['QTA 2']))
    check('originali e da preparare non si toccano',
          A._voce_per_lantek('X/_DISEGNI ORIGINALI/25AB1979.dxf', dati) is None
          and A._voce_per_lantek('X/_DA PREPARARE/S235 - 3 mm/25AB1979.dxf', dati) is None)

    print(f'\nPASSATI: {OK}   FALLITI: {len(KO)}')
    return 0 if not KO else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    finally:
        try:
            _ENG.dispose(); os.remove(_TMP)
            shutil.rmtree(_CARTELLE, ignore_errors=True)
        except Exception:
            pass
