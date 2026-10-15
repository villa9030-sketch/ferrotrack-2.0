"""Pulizie del 15/10/2026.

 1. La pagina laser vecchia (laser-vecchio.html + laser-guida.js) non c'e'
    piu': nessuna pagina la richiama e un vecchio segnalibro porta a laser.html.
 2. Le GET di sola lettura su un preventivo/ordine che non esiste rispondono
    404 (prima 200 con elenchi vuoti: voci LIEVI dello stress test 12/10).
 3. Font locali: nessuna pagina dipende da fonts.googleapis.com; il CSS dei
    font e i file .woff2 che cita esistono e partono col tipo giusto.

Database temporaneo:  python app/tests/test_pulizia_1510.py
"""
import os
import re
import sys
import tempfile
import uuid

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

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
from backend.models import Base  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_pulizia_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend.app import app  # noqa: E402
from backend.database import PreventivoManager  # noqa: E402

FE = os.path.join(_APP, 'frontend')
OK = KO = 0


def check(nome, cond, extra=None):
    global OK, KO
    if cond:
        OK += 1
        print(f'  OK  {nome}')
    else:
        KO += 1
        print(f'  KO  {nome} :: {extra!r}'[:500])


def _testi_frontend():
    for radice, _, files in os.walk(FE):
        if 'vendor' in radice:
            continue
        for f in files:
            if f.endswith(('.html', '.js', '.css')):
                p = os.path.join(radice, f)
                yield os.path.relpath(p, FE), open(p, encoding='utf-8', errors='replace').read()


def test_laser_vecchio(c):
    print('\n1) Pagina laser vecchia tolta')
    for f in ('laser-vecchio.html', 'laser-guida.js'):
        check(f'{f} tolto', not os.path.exists(os.path.join(FE, f)))
    chi = [n for n, t in _testi_frontend() if 'laser-vecchio' in t or 'laser-guida' in t]
    check('nessuna pagina la richiama', not chi, chi)
    post = os.path.join(os.path.dirname(_APP), 'app', 'POSTAZIONI')
    if os.path.isdir(post):
        chi = [f for f in os.listdir(post) if 'laser-vecchio' in open(os.path.join(post, f), encoding='utf-8',
                                                                         errors='replace').read()]
        check('nessuno script delle postazioni la apre', not chi, chi)
    r = c.get('/laser-vecchio.html')
    check('vecchio segnalibro -> laser.html', r.status_code == 302 and r.headers.get('Location', '').endswith('/laser.html'),
          (r.status_code, r.headers.get('Location')))


def test_404(c):
    print('\n2) Preventivo/ordine che non esiste: 404')
    pid = PreventivoManager.create('Cliente Prova', 'commerciale', quantita=1, margine_pct=0)['id']
    finto = str(uuid.uuid4())
    for coda in ('step-files', 'verifica-pezzo?dxf=a.dxf&codice=A', 'distinte?codici=A',
                 'file-disegni', 'ordine-cliente'):
        r = c.get(f'/api/preventivi/{finto}/{coda}')
        check(f'{coda.split("?")[0]}: id inventato -> 404', r.status_code == 404, (r.status_code, r.get_data(as_text=True)[:120]))
        r = c.get(f'/api/preventivi/{pid}/{coda}')
        check(f'{coda.split("?")[0]}: preventivo vero -> 200', r.status_code == 200, (r.status_code, r.get_data(as_text=True)[:200]))
    r = c.get(f'/api/orders/{finto}/verify-files')
    check('verify-files: ordine inventato -> 404', r.status_code == 404, (r.status_code, r.get_data(as_text=True)[:120]))


def test_font(c):
    print('\n3) Font locali')
    chi = [n for n, t in _testi_frontend() if 'fonts.googleapis' in t or 'fonts.gstatic' in t]
    check('nessuna pagina chiama Google Fonts', not chi, chi)
    css = open(os.path.join(FE, 'vendor', 'fonts', 'fonts.css'), encoding='utf-8').read()
    fam = set(re.findall(r"font-family: '([^']+)'", css))
    check('Geist, Inter, JetBrains Mono', fam == {'Geist', 'Inter', 'JetBrains Mono'}, fam)
    check('licenza OFL accanto ai font', os.path.exists(os.path.join(FE, 'vendor', 'fonts', 'OFL.txt')))
    r = c.get('/vendor/fonts/fonts.css')
    check('fonts.css servito', r.status_code == 200 and b'@font-face' in r.data, r.status_code)
    for nome in re.findall(r"url\('([^']+\.woff2)'\)", css):
        r = c.get('/vendor/fonts/' + nome)
        check(f'{nome} servito come font/woff2', r.status_code == 200 and r.data[:4] == b'wOF2'
              and r.mimetype == 'font/woff2', (r.status_code, r.mimetype))
    ui = open(os.path.join(FE, 'ft-ui.css'), encoding='utf-8').read()
    check('ft-ui.css importa i font locali', "@import url('/vendor/fonts/fonts.css')" in ui)


def main():
    try:
        from tests.accesso_aiuto import stazione_pronta, modalita
        modalita('protetto')
        c = stazione_pronta(app, 'commerciale')
        test_laser_vecchio(c)
        test_404(c)
        test_font(app.test_client())      # i font servono anche prima di entrare (login)
    finally:
        try:
            _ENG.dispose()
            os.remove(_TMP)
        except OSError:
            pass
    print(f'\n{OK} OK, {KO} KO')
    sys.exit(1 if KO else 0)


if __name__ == '__main__':
    main()
