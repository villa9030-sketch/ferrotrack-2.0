"""Registro degli esempi del motore: una riga per decisione, mai un errore che
blocchi il salvataggio vero.

Esecuzione: python app/tests/test_registro_esempi.py
"""
import json
import os
import sys
import tempfile

_QUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_QUI))
os.environ['FERROTRACK_ESEMPI_DIR'] = tempfile.mkdtemp(prefix='test_esempi_')

from backend.preventivi import registro_esempi as R  # noqa: E402

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


def main():
    d = tempfile.mkdtemp()
    f = os.path.join(d, 'pezzo.dxf')
    open(f, 'w').write('0\nEOF\n')
    check('cartella dalla variabile d\'ambiente (i test non scrivono nei dati veri)',
          R.CARTELLA == os.environ['FERROTRACK_ESEMPI_DIR'], R.CARTELLA)
    ok = R.registra('contorno_scelto_a_mano', f, codice='X1', chi='Prova',
                    motore={'area_dm2': 1.0}, decisione={'area_dm2': 1.2, 'outer_xy': [[0, 0], [1, 0], [1, 1]]})
    check('scritto', ok)
    R.registra('contorno_confermato', None, codice='X2')
    righe = [json.loads(l) for l in open(os.path.join(R.CARTELLA, R.FILE), encoding='utf-8')]
    check('due righe, in ordine', [r['codice'] for r in righe] == ['X1', 'X2'], righe)
    check('impronta del file', righe[0]['sha256'] and len(righe[0]['sha256']) == 64, righe[0].get('sha256'))
    check('motore e decisione salvati', righe[0]['motore']['area_dm2'] == 1.0 and righe[0]['decisione']['area_dm2'] == 1.2)
    check('file mancante: niente impronta, nessun errore', righe[1]['sha256'] is None)
    vecchia = R.CARTELLA
    R.CARTELLA = os.path.join(f, 'non_una_cartella')      # dentro un file: non si puo' creare
    check('errore di scrittura: torna False, non solleva', R.registra('x', None) is False)
    R.CARTELLA = vecchia
    print(f'\nPASSATI: {OK}   FALLITI: {len(KO)}')
    return 1 if KO else 0


if __name__ == '__main__':
    sys.exit(main())
