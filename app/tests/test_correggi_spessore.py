"""«Correggi in Lantek e manda» (28/10/2026): spessore sbagliato in Lantek.

Provato su FT-PROVA-30 (lantek_prova_spessore.py): reimportato con Procesos
e un altro spessore, Lantek AGGIORNA il pezzo, niente doppioni. Qui:

 1. la conferma sa quali codici si possono correggere (spessore diverso,
    stesso materiale, disegno pronto) e perche' no: lamiera nestata non
    chiusa, lamiere non lette;
 2. "correggi" non possibile -> 409, Procesos mai lanciato; deve essere
    fra i codici da mandare;
 3. corretto: Procesos col DXF e lo spessore dell'ordine, poi l'ordine di
    produzione, audit;
 4. Lantek non cambia lo spessore: quel codice NON parte, resta "da
    correggere", gli altri partono.

Database TEMPORANEO e Lantek SIMULATO (riusa test_lantek_correzioni):
    python app/tests/test_correggi_spessore.py
"""
import os
import shutil
import sys

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from tests import test_lantek_correzioni as T  # noqa: E402

A, L, check = T.A, T.L, T.check
for _c, _sp in (('C-SPESS-00', 2.0), ('D-SPESS-00', 2.0), ('E-OK-00', 3.0)):
    T.PEZZI_LANTEK[_c] = {'esiste': True, 'codice_lantek': _c, 'materiale': 'FERRO', 'spessore': _sp, 'revisioni': []}

# il DXF pronto per Lantek di ogni pezzo (FERRO 3 mm)
_DXF = {}
for _c in ('P-OK-00', 'C-SPESS-00', 'D-SPESS-00', 'E-OK-00'):
    import ezdxf
    _p = os.path.join(T._CARTELLE, _c + '.dxf')
    _d = ezdxf.new()
    _d.modelspace().add_lwpolyline([(0, 0), (90, 0), (90, 40), (0, 40)], close=True)
    _d.saveas(_p)
    _DXF[_c] = _p
A._struttura_zip = lambda radice, cartella, disegni, righe: [(p, f'X/FERRO - 3 mm/{c}.dxf') for c, p in _DXF.items()]
A._disegni_ordine = lambda order: [{'nome': c + '.dxf', 'percorso': p} for c, p in _DXF.items()]

NEST = {'v': {}}
L.pezzi_in_nesting_aperti = lambda codici, giorni=180: NEST['v']
PROCESOS = []
CORREGGE = {'si': True}


def procesos(pezzi, cartella, user_data=None, attesa_s=900):
    PROCESOS.append([(p['codice'], p['materiale'], p['spessore'], p['dxf']) for p in pezzi])
    if CORREGGE['si']:
        for p in pezzi:
            T.PEZZI_LANTEK[p['codice']]['spessore'] = float(p['spessore'])
    return {'eseguito': True, 'errore': None, 'cartella': cartella}


L.importa_pezzi_dxf = procesos


def xml_mandato():
    return next((c[1] for c in reversed(T.CHIAMATE) if c[0] == 'xml'), b'')


def main():
    from tests.accesso_aiuto import postazioni, entra, modalita
    from backend.models import User
    s = T.models.SessionLocal()
    s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
    s.commit(); s.close()
    modalita('protetto')
    postazioni()
    laser = entra(A.app, 'laser')

    print('1) La conferma: si puo\' correggere?')
    oid = T.nuovo_ordine('6001', ['P-OK-00', 'C-SPESS-00'])
    o = laser.get(f'/api/orders/{oid}/laser?fresco=1').get_json() or {}
    cg = o.get('correggibili') or {}
    check('C-SPESS-00: correggibile da 2 a 3 mm', cg.get('C-SPESS-00') == {'possibile': True, 'motivo': None, 'da': 2.0, 'a': 3.0}, cg)
    check('P-OK-00 (uguale): non c\'e\'', 'P-OK-00' not in cg, cg)
    NEST['v'] = {'C-SPESS-00': ['Nestings\\10\\10148']}
    cg = (laser.get(f'/api/orders/{oid}/laser?fresco=1').get_json() or {}).get('correggibili') or {}
    check('in una lamiera nestata non chiusa: no, col perche\'',
          cg.get('C-SPESS-00', {}).get('possibile') is False and 'Nestings\\10\\10148' in (cg['C-SPESS-00'].get('motivo') or ''), cg)
    u = f'/api/orders/{oid}/lantek-invia'
    corpo = {'codici': ['C-SPESS-00', 'P-OK-00'], 'nuovi': [], 'esclusi': [], 'correggi': ['C-SPESS-00']}
    r = laser.post(u, json=corpo)
    check('mandare lo stesso con "correggi": 409, Procesos mai lanciato, niente XML',
          r.status_code == 409 and not PROCESOS and not xml_mandato(), (r.status_code, r.get_json()))
    NEST['v'] = None
    cg = (laser.get(f'/api/orders/{oid}/laser?fresco=1').get_json() or {}).get('correggibili') or {}
    check('lamiere di Lantek non lette: no', cg.get('C-SPESS-00', {}).get('possibile') is False, cg)
    NEST['v'] = {}

    print('\n2) Regole')
    r = laser.post(u, json={'codici': ['P-OK-00'], 'nuovi': [], 'esclusi': ['C-SPESS-00'], 'correggi': ['C-SPESS-00']})
    check('"correggi" fuori dai codici da mandare: rifiutato', r.status_code == 400 and not PROCESOS, (r.status_code, r.get_json()))

    print('\n3) Corretto e mandato')
    T.CHIAMATE.clear()
    r = laser.post(u, json=corpo)
    j = r.get_json() or {}
    check('Procesos: C-SPESS-00, FERRO, 3 mm, col suo DXF',
          PROCESOS == [[('C-SPESS-00', 'FERRO', 3.0, _DXF['C-SPESS-00'])]], PROCESOS)
    x = xml_mandato()
    check('poi gli ordini di produzione di tutti e due', r.status_code == 200 and b'"C-SPESS-00"' in x and b'"P-OK-00"' in x,
          (r.status_code, j))
    check('risposta: corretti C-SPESS-00, nessun problema', j.get('corretti') == ['C-SPESS-00'] and not j.get('non_corretti')
          and not j.get('problemi') and not j.get('da_correggere'), j)
    check('audit: CORRETTI IN LANTEK', any('CORRETTI IN LANTEK' in (d or '') and 'C-SPESS-00 3 mm' in (d or '')
                                           for _, d in T.audit('LANTEK_INVIA', oid)), T.audit('LANTEK_INVIA', oid))

    print('\n4) Lantek non cambia lo spessore')
    oid2 = T.nuovo_ordine('6002', ['E-OK-00', 'D-SPESS-00'])
    CORREGGE['si'] = False
    PROCESOS.clear()
    T.CHIAMATE.clear()
    r = laser.post(f'/api/orders/{oid2}/lantek-invia',
                   json={'codici': ['D-SPESS-00', 'E-OK-00'], 'nuovi': [], 'esclusi': [], 'correggi': ['D-SPESS-00']})
    j = r.get_json() or {}
    x = xml_mandato()
    check('Procesos lanciato, ma D-SPESS-00 resta a 2 mm: non corretto', PROCESOS and j.get('non_corretti') == ['D-SPESS-00'], j)
    check('D-SPESS-00 NON mandato, E-OK-00 si\'', b'D-SPESS-00' not in x and b'"E-OK-00"' in x, x[:300])
    cc = T.correzioni(oid2)
    check('resta "da correggere in Lantek" (3 mm)', j.get('da_correggere') == ['D-SPESS-00']
          and len(cc) == 1 and cc[0]['codice'] == 'D-SPESS-00' and cc[0]['sp'] == 3.0 and not cc[0]['chiuso'], (j, cc))


if __name__ == '__main__':
    try:
        main()
    finally:
        try:
            T._ENG.dispose()
            os.remove(T._TMP)
        except Exception:
            pass
        shutil.rmtree(T._CARTELLE, ignore_errors=True)
        shutil.rmtree(T._ESEMPI, ignore_errors=True)
    print(f'\nPASSATI: {T.OK}   FALLITI: {len(T.KO)}')
    sys.exit(1 if T.KO else 0)
