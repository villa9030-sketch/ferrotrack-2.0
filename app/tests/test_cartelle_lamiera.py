"""Disegni per Lantek divisi per lamiera e ZIP "Cliente - Numero".

Stefano: "quando faccio il download dei particolari ho bisogno che venga
chiamato con il nome del cliente e numero ordine, ma soprattutto i disegni
gia' divisi in cartelle per spessore e materiale".
Esecuzione: python app/tests/test_cartelle_lamiera.py   (exit 0 = tutto ok)
"""
import os
import shutil
import sys
import tempfile
import uuid

_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _APP)
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'
from sqlalchemy import create_engine  # noqa: E402
from backend import models  # noqa: E402
from backend.models import Base  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_DB = os.path.join(tempfile.gettempdir(), f'test_lam_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _DB.replace(chr(92), '/'))
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)
import backend.app  # noqa: E402,F401
A = sys.modules['backend.app']

KO = []
N = 0


def check(nome, cond, extra=''):
    global N
    N += 1
    print(('  [OK] ' if cond else '  [KO] ') + nome + ('' if cond else f' {extra}'))
    if not cond:
        KO.append(nome)


check('nome lamiera inox', A._nome_lamiera('INOX_304', 2) == 'INOX 304 - 2 mm', A._nome_lamiera('INOX_304', 2))
check('spessore decimale con la virgola', A._nome_lamiera('S235', 1.5) == 'S235 - 1,5 mm', A._nome_lamiera('S235', 1.5))
check('dati mancanti', A._nome_lamiera(None, None) == 'MATERIALE NON INDICATO - SPESSORE NON INDICATO', A._nome_lamiera(None, None))

T = tempfile.mkdtemp()
try:
    def f(*p):
        path = os.path.join(T, *p)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, 'w').write('0')
        return path
    # ordine di prima: solo originali, divisi secondo la distinta
    vecchio = os.path.join(T, 'vecchio')
    dis = [{'nome': 'A1.dxf', 'percorso': f('vecchio', 'A1.dxf')},
           {'nome': 'B2.dxf', 'percorso': f('vecchio', 'B2.dxf')},
           {'nome': 'C3.dxf', 'percorso': f('vecchio', 'C3.dxf')}]
    righe = [{'disegno': 'A1.dxf', 'materiale': 'INOX_304', 'spessore_mm': 2},
             {'disegno': 'B2.dxf', 'materiale': 'S235', 'spessore_mm': 5}]
    nomi = sorted(n for _p, n in A._struttura_zip('DECA - 1184', vecchio, dis, righe))
    check('vecchio ordine: per lamiera + senza materiale',
          nomi == ['DECA - 1184/INOX 304 - 2 mm/A1.dxf', 'DECA - 1184/S235 - 5 mm/B2.dxf',
                   'DECA - 1184/_SENZA MATERIALE/C3.dxf'], nomi)
    # ordine nuovo: la cartella LANTEK com'e' + originali a parte
    nuovo = os.path.join(T, 'nuovo')
    f('nuovo', 'LANTEK', 'INOX 304 - 2 mm', 'A1.dxf')
    f('nuovo', 'LANTEK', '_DA PREPARARE', 'S235 - 5 mm', 'B2.dxf')
    dis2 = [{'nome': 'A1.dxf', 'percorso': f('nuovo', 'A1.dxf')}, {'nome': 'B2.dxf', 'percorso': f('nuovo', 'B2.dxf')}]
    nomi = sorted(n for _p, n in A._struttura_zip('DECA - 1184', nuovo, dis2, []))
    check('ordine nuovo: puliti per lamiera, da preparare, originali',
          nomi == ['DECA - 1184/INOX 304 - 2 mm/A1.dxf', 'DECA - 1184/_DA PREPARARE/S235 - 5 mm/B2.dxf',
                   'DECA - 1184/_DISEGNI ORIGINALI/A1.dxf', 'DECA - 1184/_DISEGNI ORIGINALI/B2.dxf'], nomi)
finally:
    shutil.rmtree(T, ignore_errors=True)
print(f'\nRisultato: {N - len(KO)} ok, {len(KO)} ko')
sys.exit(1 if KO else 0)
