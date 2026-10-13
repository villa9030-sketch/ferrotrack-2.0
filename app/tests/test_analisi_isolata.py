"""Disegni enormi: la lettura ha un tempo massimo e non blocca il server.

Dalla prova notturna (12/10): un DXF con 20.000 fori teneva occupata
l'analisi del pacchetto per oltre 5 minuti e rallentava tutto il server.
Ora i disegni si leggono in processi lavoratori (preventivi/analisi_isolata):

 1. stesso risultato di prima su un disegno normale
 2. disegno troppo lento: fermato al tempo massimo, torna "da guardare";
    intanto il server risponde subito; il disegno dopo si legge normalmente
 3. disegno mostruoso: scartato subito dal controllo rapido (elementi/MB)
 4. API: pacchetto con un disegno troppo lento -> 200, la riga dice "da
    guardare: troppo complesso", gli altri pezzi sono letti, niente anteprima

Gira su DATABASE e CARTELLE TEMPORANEI. Esecuzione:
    python app/tests/test_analisi_isolata.py
"""
import io
import json
import os
import random
import shutil
import sys
import tempfile
import threading
import time
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
from backend.models import Base, User  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_isolata_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend.preventivi import dxf_cache  # noqa: E402
from backend.preventivi import analisi_isolata as AI  # noqa: E402
from backend.preventivi.dxf_batch_worker import process_single_dxf  # noqa: E402
from backend import lantek as _LT  # noqa: E402
_LT.pezzi_in_lantek = lambda codici: {'disponibile': False, 'errore': 'simulato', 'pezzi': {}}

_CARTELLE = tempfile.mkdtemp(prefix='ft_isolata_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
A.PDFS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'pdfs')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)
os.makedirs(A.PDFS_FOLDER, exist_ok=True)
dxf_cache._CACHE_DB_PATH = os.path.join(_CARTELLE, 'dxf_cache.db')
_config_vera = A.ConfigManager.load_config
A.ConfigManager.load_config = staticmethod(
    lambda: dict(_config_vera() or {}, disegni_export_root=os.path.join(_CARTELLE, 'rete')))

CFG = {'dxf_colori_piega': [2], 'dxf_colori_saldatura': [1], 'dxf_lunghezza_minima': 15.0,
       'dxf_tolleranza_centro': 1.0, 'dxf_svasatura_ratio_min': 1.8, 'dxf_svasatura_ratio_max': 3.0,
       'dxf_semicerchio_angolo_min': 150.0, 'dxf_semicerchio_angolo_max': 320.0,
       'dxf_filtra_zona_sviluppata': True}

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


def dxf_piastra(w=200, h=100, fori=4):
    import ezdxf
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (w, 0), (w, h), (0, h)], close=True)
    for i in range(fori):
        msp.add_circle((20 + i * 40, 50), 5)
    s = io.StringIO()
    doc.write(s)
    return s.getvalue().encode('utf-8')


def dxf_lamiera_forata(n):
    """Come "enorme.dxf" della prova notturna: lamiera con n fori a caso."""
    import ezdxf
    doc = ezdxf.new('R2018')
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (3000, 0), (3000, 1500), (0, 1500)], close=True)
    rnd = random.Random(1)
    for _ in range(n):
        msp.add_circle((rnd.uniform(10, 2990), rnd.uniform(10, 1490)), rnd.uniform(1, 4))
    s = io.StringIO()
    doc.write(s)
    return s.getvalue().encode('utf-8')


def scrivi(cartella, nome, dati):
    os.makedirs(cartella, exist_ok=True)
    p = os.path.join(cartella, nome)
    with open(p, 'wb') as f:
        f.write(dati)
    return p


def pdf_ordine(righe):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    testo = ['Officine Rossi S.r.l.', 'Via Roma 1 - 20100 Milano', 'Ordine Fornitore 4521 01/09/2026',
             'Codice Commessa Descrizione Q.ta U.M. Prezzo Consegna'] + righe + ['Note', 'fine']
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for riga in testo:
        c.drawString(40, y, riga)
        y -= 18
    c.showPage()
    c.save()
    return buf.getvalue()


def norm(r, cartella):
    s = json.dumps(r, default=str, sort_keys=True)
    return s.replace(json.dumps(cartella)[1:-1], 'X')


def main():
    pesante = dxf_lamiera_forata(6000)

    print('\n1) Disegno normale: stesso risultato di prima')
    da, db = os.path.join(_CARTELLE, 'a'), os.path.join(_CARTELLE, 'b')
    pa, pb = scrivi(da, 'piastra.dxf', dxf_piastra()), scrivi(db, 'piastra.dxf', dxf_piastra())
    cache_vera = dxf_cache._CACHE_DB_PATH
    dxf_cache._CACHE_DB_PATH = os.path.join(_CARTELLE, 'cache_a.db')
    ra = json.loads(json.dumps(process_single_dxf(pa, 'piastra.dxf', CFG), default=str))
    dxf_cache._CACHE_DB_PATH = os.path.join(_CARTELLE, 'cache_b.db')
    rb = AI.analizza(pb, 'piastra.dxf', CFG)
    dxf_cache._CACHE_DB_PATH = cache_vera
    check('letto nel lavoratore', rb.get('success') is True and not rb.get('_cache_hit'), rb.get('error'))
    check('identico alla lettura nel server', norm(ra, da) == norm(rb, db))

    print('\n2) Disegno troppo lento: fermato, e il server risponde intanto')
    pl = scrivi(os.path.join(_CARTELLE, 'c'), 'enorme.dxf', pesante)
    esito = {}

    def lento():
        t0 = time.time()
        esito['r'] = AI.analizza(pl, 'enorme.dxf', CFG, tempo_max=3)
        esito['t'] = time.time() - t0
    th = threading.Thread(target=lento)
    th.start()
    time.sleep(1.0)
    c = A.app.test_client()
    t0 = time.time()
    for _ in range(5):
        c.get('/api/health')
    t_salute = time.time() - t0
    th.join(60)
    r = esito.get('r') or {}
    check('fermato al tempo massimo (non minuti)', esito.get('t', 99) < 15, esito.get('t'))
    check('torna "da guardare: troppo complesso o lento"',
          r.get('success') is False and r.get('codice') == 'troppo_lento'
          and 'troppo complesso' in (r.get('error') or ''), r)
    check('intanto il server risponde subito (5 richieste < 2 s)', t_salute < 2, round(t_salute, 2))
    r2 = AI.analizza(scrivi(os.path.join(_CARTELLE, 'd'), 'piastra2.dxf', dxf_piastra(300, 120)),
                     'piastra2.dxf', CFG)
    check('il disegno dopo si legge normalmente', r2.get('success') is True, r2.get('error'))

    print('\n3) Disegno mostruoso: scartato dal controllo rapido')
    vecchio = AI.ELEMENTI_MAX
    AI.ELEMENTI_MAX = 1000
    t0 = time.time()
    r3 = AI.analizza(pl, 'enorme.dxf', CFG)
    AI.ELEMENTI_MAX = vecchio
    check('scartato subito, con il motivo', r3.get('codice') == 'troppo_grande' and time.time() - t0 < 2
          and 'elementi' in (r3.get('error') or ''), r3)

    print('\n4) API: pacchetto con un disegno troppo lento')
    s = models.SessionLocal()
    s.add(User(id='ufficio', name='Amministrazione', role='Amministrazione', is_active=True))
    s.commit()
    s.close()
    from tests.accesso_aiuto import postazioni, persona, entra, modalita
    modalita('protetto')
    postazioni()
    persona('ufficio', 'Amministrazione', 'Amministrazione', pin='2580')
    uff = entra(A.app, 'ufficio', pin='2580')
    cons = (datetime.now() + timedelta(days=60)).strftime('%d/%m/%y')
    pdf = pdf_ordine([f'PZ-A100-00 C26-101 Piastra base sp.3 S235JR 4,00Nr {cons}',
                      f'PZ-ENORME-00 C26-101 Lamiera forata sp.2 S235JR 1,00Nr {cons}'])
    files = [(io.BytesIO(pdf), 'ORDINE 4521.pdf'), (io.BytesIO(dxf_piastra()), 'PZ-A100-00.dxf'),
             (io.BytesIO(pesante), 'PZ-ENORME-00.dxf')]
    paths = ['P/ORDINE 4521.pdf', 'P/PZ-A100-00.dxf', 'P/PZ-ENORME-00.dxf']
    AI.TEMPO_MAX_S = 3
    t0 = time.time()
    r = uff.post('/api/orders/da-pacchetto/analizza', data={'files': files, 'paths': paths},
                 content_type='multipart/form-data')
    durata = time.time() - t0
    d = r.get_json() or {}
    righe = {x.get('codice'): x for x in d.get('righe') or []}
    en = righe.get('PZ-ENORME-00') or {}
    pz = righe.get('PZ-A100-00') or {}
    check('risponde 200 in pochi secondi', r.status_code == 200 and durata < 40, (r.status_code, round(durata, 1),
                                                                               d.get('error')))
    check('la riga del disegno enorme dice "troppo complesso": da guardare',
          any('troppo complesso' in a for a in (en.get('avvisi_tecnici') or [])), en)
    check('e niente anteprima (la rileggerebbe nel server)', en.get('url_svg') is None, en.get('url_svg'))
    check('l\'altro pezzo e\' letto', (pz.get('area_dm2') or 0) > 0 and pz.get('url_svg'), pz)

    AI.chiudi_tutti()
    print(f'\nRisultato: {OK} ok, {len(KO)} falliti')
    try:
        shutil.rmtree(_CARTELLE, ignore_errors=True)
    except OSError:
        pass
    return 1 if KO else 0


if __name__ == '__main__':
    sys.exit(main())
