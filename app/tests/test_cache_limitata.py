"""Cache in memoria con tetto anche in byte (memoria del server).

Dalla prova notturna (12/10): 150 -> 300 MB in 13 ore. Le cache delle
anteprime SVG e della geometria del CAD avevano solo un tetto di 128 voci;
quella dei documenti DXF del CAD 12 documenti di qualunque dimensione.

Esecuzione: python app/tests/test_cache_limitata.py
"""
import os
import sys
import tempfile

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'

from backend.cache_limitata import CacheLimitata  # noqa: E402

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
    print('\n1) CacheLimitata')
    c = CacheLimitata(max_voci=5, max_byte=1000)
    for i in range(10):
        c.put(i, 'x' * 10)
    check('tetto sulle voci (5)', len(c) == 5 and c.get(0) is None and c.get(9) == 'x' * 10)
    c.get(5)                      # usata di recente: resta
    c.put('a', 'y' * 10)
    check('LRU: tolta la meno usata, non quella appena letta', c.get(5) is not None and c.get(6) is None)
    c = CacheLimitata(max_voci=100, max_byte=1000)
    for i in range(20):
        c.put(i, 'z' * 200)
    check('tetto in byte (<= 1000)', c.byte <= 1000 and len(c) <= 5, (c.byte, len(c)))
    c.put('enorme', 'w' * 600)
    check('una voce oltre 1/4 del tetto non si tiene', c.get('enorme') is None)
    c.pop(19)
    check('pop aggiorna i byte', c.byte == sum(len(c.get(k)) for k in c), c.byte)
    check('si scorre come un dict', all(k in c for k in list(c)))

    print('\n2) Cache del server')
    import werkzeug
    if not hasattr(werkzeug, '__version__'):
        werkzeug.__version__ = '3.0.0'
    from sqlalchemy import create_engine
    from backend import models
    tmp = os.path.join(tempfile.gettempdir(), 'test_cache_lim.db')
    models.engine = create_engine('sqlite:///' + tmp.replace(chr(92), '/'))
    models.SessionLocal.configure(bind=models.engine)
    import importlib
    A = importlib.import_module('backend.app')
    check('anteprime SVG e geometria CAD hanno il tetto in byte',
          isinstance(A._SVG_CACHE, CacheLimitata) and isinstance(A._GEOMETRY_CACHE, CacheLimitata)
          and A._SVG_CACHE.max_byte <= 64 * 1024 * 1024)
    from backend.preventivi import pick_part
    import ezdxf
    d = tempfile.mkdtemp()
    grandi = []
    for i in range(4):
        doc = ezdxf.new()
        msp = doc.modelspace()
        for j in range(3000 + i):
            msp.add_line((j, 0), (j, 10))
        p = os.path.join(d, f'g{i}.dxf')
        doc.saveas(p)
        grandi.append(p)
    vecchio = pick_part._DOC_CACHE_MAX_BYTE
    pick_part._DOC_CACHE_MAX_BYTE = max(os.path.getsize(p) for p in grandi) * 2 + 10
    pick_part._DOC_CACHE.clear()
    for p in grandi:
        pick_part._leggi_dxf(p)
    n = len(pick_part._DOC_CACHE)
    pick_part._DOC_CACHE_MAX_BYTE = vecchio
    check('documenti DXF del CAD: tetto sul peso dei file', n == 2, n)

    print(f'\nRisultato: {OK} ok, {len(KO)} falliti')
    return 1 if KO else 0


if __name__ == '__main__':
    sys.exit(main())
