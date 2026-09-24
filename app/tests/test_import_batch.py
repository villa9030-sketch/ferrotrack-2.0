"""Import di PIU' DXF insieme (endpoint import-dxf-batch).

Il batch preparava terne (percorso, nome salvato, nome originale) e poi le
spacchettava a coppie: ogni import di due o piu' disegni rispondeva 500 e non
creava nulla. Nessun test lo chiamava. Qui si carica un gruppo di DXF veri e
si controlla che arrivino tutti, con geometria.

Gira su DATABASE e CARTELLE TEMPORANEI. Esecuzione:
    python app/tests/test_import_batch.py
"""
import glob
import io
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

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_batch_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend.database import PreventivoManager  # noqa: E402
import importlib  # noqa: E402
# Il modulo, non l'oggetto Flask che backend/__init__ riesporta con lo stesso nome
A = importlib.import_module('backend.app')  # noqa: E402

_UP = tempfile.mkdtemp(prefix='test_batch_up_')
A.UPLOAD_FOLDER = _UP

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


def main():
    s = models.SessionLocal()
    try:
        s.add(User(id='commerciale', name='Marco', role='Commerciale', is_active=True))
        s.commit()
    finally:
        s.close()
    r = PreventivoManager.create('Cliente prova batch', 'commerciale', quantita=1)
    pid = r['id'] if isinstance(r, dict) else r

    radice = os.path.join(os.path.dirname(_APP), '_test_input', 'regression')
    disegni = sorted(glob.glob(os.path.join(radice, '*', 'pezzo.dxf')))[:3]
    if len(disegni) < 2:
        print('Servono almeno 2 DXF in _test_input/regression. Test saltato.')
        return 0

    client = A.app.test_client()
    dati = {'admin_id': 'commerciale', 'files': [], 'paths': []}
    for d in disegni:
        nome = os.path.basename(os.path.dirname(d)) + '.dxf'
        with open(d, 'rb') as f:
            dati['files'].append((io.BytesIO(f.read()), nome))
        dati['paths'].append(nome)

    print('\nImport di piu\' DXF in un colpo')
    resp = client.post(f'/api/preventivi/{pid}/import-dxf-batch?admin_id=commerciale',
                       data=dati, content_type='multipart/form-data')
    check('risponde 200 (prima: 500)', resp.status_code == 200, resp.status_code)
    j = resp.get_json(silent=True) or {}
    check('esito positivo', j.get('success') is True, j.get('error'))
    risultati = j.get('results') or []
    check(f'un risultato per ogni disegno ({len(disegni)})', len(risultati) == len(disegni), len(risultati))
    riusciti = [x for x in risultati if x.get('success')]
    check('tutti i disegni elaborati', len(riusciti) == len(disegni),
          [x.get('error') for x in risultati if not x.get('success')])
    con_geo = [x for x in riusciti
               if ((x.get('geometria') or x.get('geometry') or x).get('area_dm2') or 0) > 0]
    check('ogni disegno ha un\'area', len(con_geo) == len(riusciti),
          [list(x.keys()) for x in riusciti][:1])

    print('\n' + '=' * 60)
    print(f'PASSATI: {OK}   FALLITI: {len(KO)}')
    if KO:
        print('Falliti: ' + ', '.join(KO))
    print('=' * 60)
    return 1 if KO else 0


if __name__ == '__main__':
    try:
        code = main()
    finally:
        try:
            _ENG.dispose()
            os.remove(_TMP)
        except Exception:
            pass
        shutil.rmtree(_UP, ignore_errors=True)
    sys.exit(code)
