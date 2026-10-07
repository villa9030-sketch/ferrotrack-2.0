"""Contorno di un pezzo scelto a mano nel CAD, sul disegno dell'ordine.

Caso: il riconoscimento ha preso la CORNICE del foglio invece del pezzo. Dal
laser si apre il CAD sul DXF dell'ordine, si sceglie il contorno giusto e:
 1. il CAD legge il disegno dell'ordine (geometry-json, pick-candidates,
    trace-waypoints) senza la cartella del preventivo
 2. alla conferma area/perimetro/fori si ricalcolano sul server
 3. la riga del pezzo e' aggiornata (colonne + campi extra), esito confermato
 4. il DXF pulito per Lantek va in LANTEK/<lamiera>/ e sparisce da _DA PREPARARE
 5. contorni non validi: errore in italiano, niente salvato
 6. l'officina (e chi non e' registrato) non puo'

Gira su DATABASE TEMPORANEO. Esecuzione: python app/tests/test_contorno_ordine.py
"""
import json
import math
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

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

import ezdxf  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Preventivo  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_cto_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend.database import PreventivoManager  # noqa: E402
from backend.api_contorno_ordine import bp_contorno_ordine  # noqa: E402

if 'contorno_ordine' not in A.app.blueprints:
    A.app.register_blueprint(bp_contorno_ordine)

_CARTELLE = tempfile.mkdtemp(prefix='ft_cto_')
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
        print(f'  [KO] {nome}  {extra}')


# Il pezzo vero: 200 x 150 mm con due fori da 20 mm, dentro una cornice 400 x 300
PEZZO = [[100, 50], [300, 50], [300, 200], [100, 200]]
FORI = [(150, 125, 10.0), (250, 125, 10.0)]
AREA_PEZZO = (200 * 150 - 2 * math.pi * 10 ** 2) / 1e4          # dm2, ~2.937
AREA_CORNICE = 400 * 300 / 1e4                                   # 12 dm2: quello sbagliato


def scrivi_dxf(percorso):
    doc = ezdxf.new('R2010', units=4)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (400, 0), (400, 300), (0, 300)], close=True)     # cornice
    msp.add_lwpolyline([tuple(p) for p in PEZZO], close=True)                     # pezzo
    for cx, cy, r in FORI:
        msp.add_circle((cx, cy), r)
    doc.saveas(percorso)


def cerchio(cx, cy, r, n=72):
    return [[cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)] for i in range(n)]


def riga_pezzo(aid):
    s = models.SessionLocal()
    try:
        r = s.execute(text('SELECT area_dm2, perimetro_taglio_m, n_forature, geometry_source, '
                           'geometria_manuale_confermata, extra_campi FROM preventivo_articoli WHERE id = :i'),
                      {'i': aid}).fetchone()
        return r[0], r[1], r[2], r[3], r[4], json.loads(r[5] or '{}')
    finally:
        s.close()


def main():
    from tests.accesso_aiuto import postazioni, persona, entra, modalita
    s = models.SessionLocal()
    s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
    s.commit(); s.close()
    modalita('protetto')
    postazioni()
    persona('ufficio', 'Elena Ufficio', 'Amministrazione', pin='2580')
    laser = entra(A.app, 'laser')
    uff = entra(A.app, 'ufficio', pin='2580')
    rep = entra(A.app, 'reparto')
    estraneo = A.app.test_client()

    # Ordine con un pezzo il cui contorno automatico e' la cornice (da guardare)
    pr = PreventivoManager.create('DECA S.r.l.', 'paolo', quantita=1, numero_ordine_cliente='4242')
    pid = pr['id'] if isinstance(pr, dict) else pr
    PreventivoManager.replace_articoli(pid, [
        {'codice': 'P1-00', 'quantita': 2, 'materiale': 'S235', 'spessore_mm': 3, 'costo_base_override': 5,
         'dxf_filename': 'P1-00.dxf', 'area_dm2': AREA_CORNICE, 'perimetro_taglio_m': 1.4, 'n_forature': 1,
         'bbox_w_mm': 400, 'bbox_h_mm': 300, 'dxf_confidence': 0.4, 'dxf_needs_verify': True,
         'esito_verifica': {'stato': 'da_guardare', 'motivi': ['area 4 volte il peso del cartiglio']},
         'contorno_auto': {'stato': 'da_confermare', 'chiave': 'x'},
         'abbinamento': {'tipo': 'somiglianza', 'file': 'P1-00.dxf', 'score': 0.8, 'confermato': False}}])
    s = models.SessionLocal()
    s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'INVIATO'
    s.commit(); s.close()
    oid = PreventivoManager.accetta_e_crea_ordine(pid, 'paolo').get('order_id')
    check('ordine creato', bool(oid))
    aid = (PreventivoManager.get(pid, include_children=True) or {})['articoli'][0]['id']

    cartella = os.path.join(A.DRAWINGS_FOLDER, oid)
    os.makedirs(cartella, exist_ok=True)
    scrivi_dxf(os.path.join(cartella, 'P1-00.dxf'))
    lamiera = A._nome_lamiera('S235', 3)
    da_prep = os.path.join(cartella, 'LANTEK', A.CARTELLA_DA_PREPARARE, lamiera)
    os.makedirs(da_prep, exist_ok=True)
    shutil.copy2(os.path.join(cartella, 'P1-00.dxf'), os.path.join(da_prep, 'P1-00.dxf'))
    base = f'/api/orders/{oid}/pezzi/{aid}'

    print('1) Il CAD legge il disegno dell\'ordine')
    r = laser.get(base + '/cad/geometry-json')
    d = r.get_json(silent=True) or {}
    check('geometry-json 200 con estensione e polilinee', r.status_code == 200 and d.get('extents')
          and d.get('polylines'), (r.status_code, str(d)[:200]))
    check('anche l\'ufficio', uff.get(base + '/cad/geometry-json').status_code == 200)
    r = laser.post(base + '/cad/pick-candidates', json={'x': 200, 'y': 50})
    d = r.get_json() or {}
    # su questo disegno finto il riconoscimento propone la cornice (coi fori
    # del pezzo dentro): e' proprio il caso da correggere a mano
    check('pick-candidates sul disegno dell\'ordine: candidati col contorno',
          r.status_code == 200 and d.get('success') and d.get('candidates')
          and all(c.get('outer_xy') for c in d['candidates']), (r.status_code, str(d)[:200]))
    # i fori come li manda il CAD (cerchi spezzati da pick_part), presi dai candidati
    fori_cad = [h for c in d.get('candidates') or [] for h in c.get('holes_xy') or []
                if max(p[0] for p in h) - min(p[0] for p in h) < 25]
    check('i due fori tondi letti dal CAD', len(fori_cad) == 2, len(fori_cad))
    r = laser.post(base + '/cad/trace-waypoints', json={'points': [[100, 50], [300, 50], [300, 200]]})
    check('trace-waypoints risponde (200)', r.status_code == 200 and 'success' in (r.get_json() or {}),
          (r.status_code, r.get_json()))
    r = laser.post(base + '/cad/pick-candidates', json={'x': 'a'})
    check('clic senza coordinate: 400 col messaggio', r.status_code == 400 and 'coordinate' in r.get_json()['error'])
    check('pezzo che non c\'e\': 404', laser.get(f'/api/orders/{oid}/pezzi/nessuno/cad/geometry-json').status_code == 404)
    check('ordine che non c\'e\': 404', laser.post('/api/orders/nessuno/pezzi/1/contorno',
                                                    json={'outer_xy': PEZZO}).status_code == 404)

    print('\n2) Contorni non validi')
    r = laser.post(base + '/contorno', json={'outer_xy': [[0, 0], [1, 1]]})
    check('meno di 3 punti: 400 in italiano', r.status_code == 400 and 'Contorno' in r.get_json()['error'])
    r = laser.post(base + '/contorno', json={'outer_xy': [[0, 0], [10, 10], [10, 0], [0, 10]], 'holes_xy': []})
    check('contorno che si incrocia: 400', r.status_code == 400, (r.status_code, r.get_json()))
    r = laser.post(base + '/contorno', json={'outer_xy': [[5000, 5000], [5200, 5000], [5200, 5100]]})
    check('contorno fuori dal disegno: 400', r.status_code == 400 and 'disegno' in r.get_json()['error'],
          r.get_json())
    check('niente salvato', riga_pezzo(aid)[0] == AREA_CORNICE)

    print('\n3) Ruoli')
    corpo = {'outer_xy': PEZZO, 'holes_xy': fori_cad or [cerchio(*f) for f in FORI]}
    check('tablet officina: no', rep.post(base + '/contorno', json=corpo).status_code in (401, 403))
    check('tablet officina non legge il disegno', rep.get(base + '/cad/geometry-json').status_code in (401, 403))
    check('browser non registrato: no', estraneo.post(base + '/contorno', json=corpo).status_code in (401, 403))
    check('ancora niente salvato', riga_pezzo(aid)[0] == AREA_CORNICE)

    print('\n4) Conferma del contorno giusto')
    r = laser.post(base + '/contorno', json=corpo)
    d = r.get_json() or {}
    check('200 success', r.status_code == 200 and d.get('success'), (r.status_code, d))
    check(f'area ricalcolata sul server ~{AREA_PEZZO:.3f} dm2', abs((d.get('area_dm2') or 0) - AREA_PEZZO) < 0.005, d)
    check('perimetro ~0,825 m (700 mm + 2 fori)',
          abs((d.get('perimetro_taglio_m') or 0) - (0.7 + 2 * 2 * math.pi * 0.01)) < 0.003, d)
    check('inneschi 3 (contorno + 2 fori)', d.get('n_forature') == 3, d)
    check('ingombro 200 x 150', abs(d.get('bbox_w_mm', 0) - 200) < 0.5 and abs(d.get('bbox_h_mm', 0) - 150) < 0.5, d)
    check('pronto per Lantek', d.get('pronto_lantek') is True, d.get('motivo'))
    area, per, nf, src, man, extra = riga_pezzo(aid)
    check('riga: area/perimetro/inneschi aggiornati', abs(area - AREA_PEZZO) < 0.005 and nf == 3
          and abs(per - d.get('perimetro_taglio_m', 0)) < 1e-3, (area, per, nf))
    check('riga: manual-click, confermata a mano', src == 'manual-click' and bool(man), (src, man))
    ev = extra.get('esito_verifica') or {}
    check('esito verifica confermato col motivo "contorno scelto a mano"',
          ev.get('stato') == 'confermato' and any('contorno scelto a mano' in m for m in ev.get('motivi') or []), ev)
    check('contorno automatico confermato, abbinamento confermato',
          (extra.get('contorno_auto') or {}).get('stato') == 'confermato'
          and (extra.get('abbinamento') or {}).get('confermato') is True, extra)
    check('ingombro nei campi extra', abs(extra.get('bbox_w_mm', 0) - 200) < 0.5
          and abs(extra.get('bbox_h_mm', 0) - 150) < 0.5, extra)
    art = next(a for a in PreventivoManager.get(pid, include_children=True)['articoli'] if a['id'] == aid)
    check('il pezzo non e\' piu\' da verificare', A._controllo_pezzo(art) is None, A._controllo_pezzo(art))
    pronto = os.path.join(cartella, 'LANTEK', lamiera, 'P1-00.dxf')
    check('DXF pulito in LANTEK/<lamiera>/', os.path.isfile(pronto) and d.get('file_lantek') == f'LANTEK/{lamiera}/P1-00.dxf',
          (d.get('file_lantek'), os.listdir(cartella)))
    check('tolto da _DA PREPARARE', not os.path.exists(os.path.join(da_prep, 'P1-00.dxf')))
    check('nessun file in piu\' fra i disegni dell\'ordine',
          sorted(x['nome'] for x in A._disegni_ordine(A._ordine_esistente(oid))) == ['P1-00.dxf'])
    from backend.preventivi.dxf_cleanup import verifica_lantek
    v = verifica_lantek(pronto, {'bbox_w_mm': 200, 'bbox_h_mm': 150, 'area_dm2': AREA_PEZZO})
    check('il pulito passa la verifica Lantek (solo il pezzo coi fori)', v.get('stato') == 'pronto', v)
    voci = [arc for _p, arc in A._struttura_zip('X', cartella, A._disegni_ordine(A._ordine_esistente(oid)), [])]
    check('nello zip/cartella condivisa: il pulito per lamiera, niente da preparare',
          f'X/{lamiera}/P1-00.dxf' in voci and not any(A.CARTELLA_DA_PREPARARE in v_ for v_ in voci), voci)

    print('\n5) Contorno sbagliato di nuovo (la cornice): salvato ma non pronto')
    r = laser.post(base + '/contorno', json={'outer_xy': [[0, 0], [400, 0], [400, 300], [0, 300]],
                                            'holes_xy': [PEZZO]})
    d = r.get_json() or {}
    check('200: area della cornice meno il pezzo', r.status_code == 200 and abs(d.get('area_dm2', 0) - 9.0) < 0.01, d)
    check('esito Lantek detto nella risposta', isinstance(d.get('pronto_lantek'), bool) and d.get('motivo'), d)

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
