"""Un disegno lentissimo non blocca piu' la pagina laser ne' il server.

Prova notturna 22-23/10, 19:14-19:20: sullo stesso pezzo (DXF da 16 MB,
geometria 122 s + motore 192 s misurati) /lettura, /giusto e /pezzi-foglio
non hanno risposto per 120 s, 7 volte: ogni richiesta rileggeva il disegno
DENTRO il server, e "giusto", scaduto il lavoratore, rifaceva il contorno nel
server ("si rifa' nel server"). Ora (backend/preparazione_disegni.py):

 1. la lettura del foglio si fa nel lavoratore, una volta: la richiesta
    aspetta al massimo un po' e risponde 202 "in_preparazione" (la pagina
    riprova da sola); niente calcolo nel server
 2. scaduto il tempo massimo: 422 "troppo_lento" subito, a ogni richiesta
    dopo (lettura, foglio.svg, pezzi del foglio, giusto), senza rileggerlo
 3. tante richieste insieme sullo stesso disegno: UNA lettura
 4. posti dei lavoratori pieni: Occupato entro il limite, non un'attesa infinita
 5. contorno preciso scaduto: None, MAI rifatto nel server
 6. un disegno normale: stessa risposta di prima (stesso SVG, stesso contorno)

Gira su DATABASE TEMPORANEO, cartelle temporanee, LANTEK SIMULATO.
Esecuzione: python app/tests/test_disegno_lento.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
_ESEMPI = tempfile.mkdtemp(prefix='ft_lento_esempi_')
os.environ['FERROTRACK_ESEMPI_DIR'] = _ESEMPI
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

import ezdxf  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Preventivo  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_lento_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend import lantek as L  # noqa: E402
from backend import api_laser as AL  # noqa: E402
from backend import api_pezzi_foglio as APF  # noqa: E402
from backend import preparazione_disegni as pd  # noqa: E402
from backend.database import PreventivoManager  # noqa: E402
from backend.preventivi import tempi_laser as TL  # noqa: E402
from backend.preventivi import analisi_isolata as ai  # noqa: E402
from backend.preventivi import pick_part, dxf_polygon_detector_v3, tipo_disegno  # noqa: E402

_CARTELLE = tempfile.mkdtemp(prefix='ft_lento_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)

TL._leggi_lantek = lambda codici: None
_VUOTO = {'esiste': False, 'codice_lantek': None, 'materiale': None, 'spessore': None, 'revisioni': []}
L.pezzi_in_lantek = lambda codici: {'disponibile': True, 'errore': None,
                                    'pezzi': {c: dict(_VUOTO) for c in codici}}
L.clienti_lantek = lambda: ['DECA']
L.ordini_in_lantek = lambda commessa: {}
L.ordini_fatti_in_lantek = lambda commessa: {}
L.pezzi_dell_ordine = lambda commessa: {}
L.procesos_disponibile = lambda: True
L.materiali_lantek_noti = lambda: {'FERRO', 'INOX', 'ALLUMINIO', 'ZINCATO'}
L.xmlimporter_disponibile = lambda: True

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


def piastra(path):
    """Piastra 200 x 100 con 4 fori da 10."""
    doc = ezdxf.new('R2010', units=4)
    doc.header['$INSUNITS'] = 4
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (200, 0), (200, 100), (0, 100)], close=True)
    for x, y in ((20, 20), (180, 20), (20, 80), (180, 80)):
        msp.add_circle((x, y), 5)
    doc.saveas(path)


def pesante(path, n=40000):
    """Un disegno che il motore legge in molti secondi: una piastra con una
    griglia fitta di linee e cerchi (come i DWG convertiti della prova)."""
    doc = ezdxf.new('R2010', units=4)
    doc.header['$INSUNITS'] = 4
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (4000, 0), (4000, 2000), (0, 2000)], close=True)
    lato = int(n ** 0.5)
    for i in range(lato):
        for j in range(lato):
            x, y = 10 + i * 3990 / lato, 10 + j * 1990 / lato
            if (i + j) % 2:
                msp.add_circle((x, y), 0.8)
            else:
                msp.add_line((x, y), (x + 1.5, y + 1.0))
    doc.saveas(path)


# In questo processo (il "server") la geometria pesante NON si deve calcolare:
# se succede, e' il comportamento di prima.
_NEL_SERVER = []
_veri = {'geometry_json': pick_part.geometry_json,
         'detect': dxf_polygon_detector_v3.detect_pezzo_geometry_v3,
         'pezzi': tipo_disegno.pezzi_del_foglio_da_file,
         'contorno': ai.contorno_preciso}


def _vietato(nome):
    def f(*a, **k):
        _NEL_SERVER.append((nome, os.path.basename(str(a[0])) if a else ''))
        return _veri[{'geometry_json': 'geometry_json', 'detect': 'detect', 'pezzi': 'pezzi',
                      'contorno': 'contorno'}[nome]](*a, **k)
    return f


def vieta_nel_server():
    pick_part.geometry_json = _vietato('geometry_json')
    dxf_polygon_detector_v3.detect_pezzo_geometry_v3 = _vietato('detect')
    tipo_disegno.pezzi_del_foglio_da_file = _vietato('pezzi')
    ai.contorno_preciso = _vietato('contorno')


def nel_server(nome_file):
    return [x for x in _NEL_SERVER if x[1] == nome_file]


def prepara_ordine():
    from tests.accesso_aiuto import postazioni, entra, modalita
    s = models.SessionLocal()
    s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
    s.commit()
    s.close()
    modalita('protetto')
    postazioni()
    laser = entra(A.app, 'laser')
    base = {'quantita': 2, 'materiale': 'S235', 'spessore_mm': 3, 'costo_base_override': 5,
            'perimetro_taglio_m': 0.8, 'n_forature': 3, 'area_dm2': 2.9, 'bbox_w_mm': 200, 'bbox_h_mm': 100,
            'dxf_confidence': 0.45, 'dxf_needs_verify': True,
            'esito_verifica': {'stato': 'da_guardare', 'motivi': ['contorno incerto']}}
    codici = ['LT-NORM-00', 'LT-PES-00', 'LT-PES2-00', 'LT-PES3-00']
    pr = PreventivoManager.create('DECA S.r.l.', 'paolo', quantita=1, numero_ordine_cliente='888')
    pid = pr['id'] if isinstance(pr, dict) else pr
    PreventivoManager.replace_articoli(pid, [dict(base, codice=c, dxf_filename=c + '.dxf') for c in codici])
    s = models.SessionLocal()
    s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'INVIATO'
    s.commit()
    s.close()
    oid = PreventivoManager.accetta_e_crea_ordine(pid, 'paolo').get('order_id')
    A.OrderManager.smista(oid, True, user_id='laser')
    arts = {a['codice']: a['id'] for a in (PreventivoManager.get(pid, include_children=True) or {})['articoli']}
    cartella = os.path.join(A.DRAWINGS_FOLDER, oid)
    os.makedirs(cartella, exist_ok=True)
    piastra(os.path.join(cartella, 'LT-NORM-00.dxf'))
    pesante(os.path.join(cartella, 'LT-PES-00.dxf'))
    shutil.copy2(os.path.join(cartella, 'LT-PES-00.dxf'), os.path.join(cartella, 'LT-PES2-00.dxf'))
    shutil.copy2(os.path.join(cartella, 'LT-PES-00.dxf'), os.path.join(cartella, 'LT-PES3-00.dxf'))
    # come un ordine caricato col pacchetto: disegni "da preparare", niente
    # DXF pulito per Lantek (cosi' "giusto" deve leggere il disegno)
    lamiera = A._nome_lamiera('S235', 3)
    os.makedirs(os.path.join(cartella, 'LANTEK', lamiera))
    da_prep = os.path.join(cartella, 'LANTEK', A.CARTELLA_DA_PREPARARE, lamiera)
    os.makedirs(da_prep)
    for c in codici:
        shutil.copy2(os.path.join(cartella, c + '.dxf'), os.path.join(da_prep, c + '.dxf'))
    return laser, oid, arts, cartella


def aspetta_stato(tipo, percorso, voluto, secondi=60):
    fine = time.time() + secondi
    while time.time() < fine:
        if pd.stato(tipo, percorso, AL._cfg_riconoscimento()) == voluto:
            return True
        time.sleep(0.1)
    return False


def main_prove():
    laser, oid, arts, cartella = prepara_ordine()
    u = lambda cod, coda='lettura': f'/api/orders/{oid}/pezzi/{arts[cod]}/{coda}'  # noqa: E731
    norm = os.path.join(cartella, 'LT-NORM-00.dxf')

    print('\n6) Disegno normale: stessa risposta di prima')
    # come prima: tutto calcolato nel server (funzioni vere, cache vuote)
    gj = json.loads(json.dumps(_veri['geometry_json'](norm, AL._cfg_riconoscimento())))
    det = _veri['detect'](norm, AL._cfg_riconoscimento()) or {}
    contorno = AL._contorno_motore(gj, det)
    svg_prima, vista_prima = AL.svg_foglio(gj, contorno)
    vieta_nel_server()
    t = time.perf_counter()
    r = laser.get(u('LT-NORM-00'))
    ms = (time.perf_counter() - t) * 1000
    d = r.get_json() or {}
    if r.status_code == 202:                       # primo avvio del lavoratore lento: si riprova come la pagina
        for _ in range(60):
            time.sleep(0.5)
            r = laser.get(u('LT-NORM-00'))
            d = r.get_json() or {}
            if r.status_code != 202:
                break
    check('lettura 200', r.status_code == 200, (r.status_code, d))
    check('stesso SVG del foglio di prima', d.get('svg') == svg_prima)
    check('stessa vista', d.get('vista') == vista_prima, (d.get('vista'), vista_prima))
    check('stesso contorno del motore', d.get('ha_contorno') == bool(contorno))
    check('niente geometria calcolata nel server', not nel_server('LT-NORM-00.dxf'), nel_server('LT-NORM-00.dxf'))
    print(f'     prima lettura {ms:.0f} ms')

    # tempo massimo piccolo: il disegno pesante "scade" nel lavoratore
    ai.TEMPO_MAX_S = 1.0
    pd.ATTESA_PAGINA_S = 0.3
    pes = os.path.join(cartella, 'LT-PES-00.dxf')

    print('\n1) Disegno pesante: la richiesta non aspetta il calcolo')
    t = time.perf_counter()
    r = laser.get(u('LT-PES-00'))
    ms = (time.perf_counter() - t) * 1000
    d = r.get_json() or {}
    check('202 in preparazione', r.status_code == 202 and d.get('codice') == 'in_preparazione', (r.status_code, d))
    check('risponde entro il limite (< 1,5 s)', ms < 1500, ms)
    check('dice quando riprovare', d.get('riprova_tra_s'), d)

    print('\n2) Scaduto il tempo massimo: "troppo lento", subito, senza rileggerlo')
    check('la lettura nel lavoratore si ferma', aspetta_stato('foglio', pes, 'errore', 30))
    t = time.perf_counter()
    r = laser.get(u('LT-PES-00'))
    ms = (time.perf_counter() - t) * 1000
    d = r.get_json() or {}
    check('422 troppo_lento', r.status_code == 422 and d.get('codice') == 'troppo_lento', (r.status_code, d))
    check('con un messaggio per il laser', 'da guardare a mano' in (d.get('error') or ''), d)
    check('subito (< 300 ms)', ms < 300, ms)
    r = laser.get(u('LT-PES-00', 'foglio.svg'))
    check('foglio.svg: 422 troppo_lento', r.status_code == 422, r.status_code)
    t = time.perf_counter()
    r = laser.post(u('LT-PES-00', 'giusto'), json={'serve_pulito': True})
    ms = (time.perf_counter() - t) * 1000
    d = r.get_json() or {}
    check('giusto: 422 troppo_lento', r.status_code == 422 and d.get('codice') == 'troppo_lento', (r.status_code, d))
    check('giusto: subito (< 2 s)', ms < 2000, ms)
    r = laser.get(u('LT-PES-00', 'pezzi-foglio'))
    for _ in range(100):
        if r.status_code != 202:
            break
        time.sleep(0.2)
        r = laser.get(u('LT-PES-00', 'pezzi-foglio'))
    check('pezzi del foglio: 422 troppo_lento', r.status_code == 422 and (r.get_json() or {}).get('codice') == 'troppo_lento',
          (r.status_code, r.get_json()))
    check('MAI calcolato nel server', not nel_server('LT-PES-00.dxf'), nel_server('LT-PES-00.dxf'))

    print('\n3) Tante richieste insieme sullo stesso disegno: una lettura sola')
    compiti = []
    vero_esegui = ai.esegui_per_pagina

    def conta(compito, *a, **k):
        compiti.append((compito.get('tipo'), os.path.basename(compito.get('path') or '')))
        return vero_esegui(compito, *a, **k)
    ai.esegui_per_pagina = conta
    try:
        barriera = threading.Barrier(8)
        codici = []

        def una():
            c = A.app.test_client()
            c.set_cookie(A.accesso.COOKIE_DISP, laser.segreto)
            barriera.wait(10)
            t0 = time.perf_counter()
            r = c.get(u('LT-PES2-00'))
            codici.append((r.status_code, (time.perf_counter() - t0) * 1000))
        th = [threading.Thread(target=una) for _ in range(8)]
        for x in th:
            x.start()
        for x in th:
            x.join(30)
        check('tutte rispondono 202 in fretta', len(codici) == 8 and all(c == 202 and ms < 1500 for c, ms in codici), codici)
        check('fino alla fine', aspetta_stato('foglio', os.path.join(cartella, 'LT-PES2-00.dxf'), 'errore', 30))
        check('un solo compito al lavoratore', compiti.count(('foglio', 'LT-PES2-00.dxf')) == 1, compiti)
    finally:
        ai.esegui_per_pagina = vero_esegui

    print('\n4) Posti dei lavoratori pieni: Occupato entro il limite')
    presi = 0
    while ai._posti_pagina.acquire(blocking=False):
        presi += 1
    try:
        t = time.perf_counter()
        try:
            ai.esegui_per_pagina({'tipo': 'foglio', 'path': norm, 'cfg': {}}, attesa_posto_s=0.3)
            occ = False
        except ai.Occupato:
            occ = True
        ms = (time.perf_counter() - t) * 1000
        check('Occupato', occ)
        check('entro il limite (< 1 s)', ms < 1000, ms)
        pd.ATTESA_PAGINA_S = 0.3
        vecchia = ai.ATTESA_POSTO_S
        ai.ATTESA_POSTO_S = 0.5
        t = time.perf_counter()
        r = laser.get(u('LT-PES3-00'))
        check('la pagina riceve "in preparazione", non aspetta', r.status_code == 202, r.status_code)
        check('in fretta (< 1,5 s)', (time.perf_counter() - t) < 1.5)
        time.sleep(1.5)                  # la lettura in sottofondo rinuncia (posti ancora pieni)
        check('"occupato" non resta in memoria: la volta dopo si riprova davvero',
              pd.stato('foglio', os.path.join(cartella, 'LT-PES3-00.dxf'), AL._cfg_riconoscimento()) is None)
    finally:
        for _ in range(presi):
            ai._posti_pagina.release()
        ai.ATTESA_POSTO_S = vecchia

    print('\n5) Contorno preciso scaduto nel lavoratore: None, mai nel server')
    det_n = _veri['detect'](norm, AL._cfg_riconoscimento()) or {}
    sel = next((c for c in det_n.get('candidates') or [] if c.get('is_selected')), None)
    ai.TEMPO_MAX_S = 0.05
    ai.chiudi_tutti()                     # lavoratore da avviare: non fa in tempo
    prima = len(_NEL_SERVER)
    t = time.perf_counter()
    res = ai.contorno_preciso_isolato(norm, sel['geometry'], float(sel['area_dm2']), 1.0, AL._cfg_riconoscimento())
    ms = (time.perf_counter() - t) * 1000
    check('None', res is None, res)
    check('non rifatto nel server', len(_NEL_SERVER) == prima, _NEL_SERVER[prima:])
    check('in fretta (< 2 s)', ms < 2000, ms)


def main():
    try:
        main_prove()
    finally:
        ai.chiudi_tutti()
    print(f'\nPASSATI: {OK}   FALLITI: {len(KO)}')
    for k in KO:
        print('  -', k)
    shutil.rmtree(_CARTELLE, ignore_errors=True)
    shutil.rmtree(_ESEMPI, ignore_errors=True)
    try:
        _ENG.dispose()
        os.remove(_TMP)
    except OSError:
        pass
    return 1 if KO else 0


if __name__ == '__main__':
    sys.exit(main())
