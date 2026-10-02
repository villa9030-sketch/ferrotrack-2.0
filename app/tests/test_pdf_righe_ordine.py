"""PDF del preventivo per DECA con le stesse righe dell'ordine del cliente.

DECA ripete lo stesso codice su piu' righe (una per commessa). Nel
preventivatore il pezzo e' uno (un disegno, un prezzo); nel PDF per il
cliente DECA tornano le righe del suo ordine: codice, commessa, quantita'.

Copre:
 1. DECA con righe d'ordine: una riga per commessa, colonna Commessa, stesso
    prezzo unitario, importi che sommano al pezzo
 2. quantita' cambiata a mano (righe che non sommano): una riga sola
 3. altro cliente: una riga sola, nessuna colonna Commessa
 4. il PDF generato contiene davvero le righe e la colonna

Esecuzione: python app/tests/test_pdf_righe_ordine.py
"""
import io
import os
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

import backend.app  # noqa: E402,F401
A = sys.modules['backend.app']
from backend.preventivi import pdf_exporter  # noqa: E402

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


def pezzo(codice, qta, righe=None):
    a = {'id': str(uuid.uuid4()), 'codice': codice, 'quantita': qta, 'materiale': 'INOX_304',
         'spessore_mm': 3, 'area_dm2': 1.0, 'perimetro_taglio_m': 0.5, 'costo_base_stimato': 4.0}
    if righe is not None:
        a['ordine'] = {'stato': 'ok', 'pos': righe[0]['pos'] if righe else None, 'qta': qta, 'file': 'OAFA202601176.pdf',
                       'righe': righe}
    return a


def preventivo(cliente, articoli):
    return {'id': str(uuid.uuid4()), 'cliente': cliente, 'quantita': 1, 'margine_pct': 0, 'sconto_pct': 0,
            'numero_ordine_cliente': 'A 001176', 'articoli': articoli, 'assiemi': [], 'tubolari': [], 'piastre': []}


# righe vere dell'ordine DECA A 001176 (08PA03635: 2 pezzi per 4 commesse)
R35 = [{'pos': 20, 'commessa': 'C26-200-4', 'qta': 2}, {'pos': 21, 'commessa': 'C26-200-7', 'qta': 2},
       {'pos': 22, 'commessa': 'C26-200-12', 'qta': 2}, {'pos': 23, 'commessa': 'C26-201-24', 'qta': 2}]
R90 = [{'pos': 60, 'commessa': 'C26-200-4', 'qta': 2}, {'pos': 61, 'commessa': 'C26-200-7', 'qta': 2},
       {'pos': 62, 'commessa': 'C26-200-12', 'qta': 1}, {'pos': 63, 'commessa': 'C26-201-24', 'qta': 2}]


def main():
    print('\n1) DECA: righe come nell\'ordine')
    d = A._preventivo_to_pdf_dati(preventivo('DECA', [pezzo('08PA03635-00', 8, R35), pezzo('47PA00990-00', 7, R90)]))
    rc = d['righe_cliente']
    r35 = [r for r in rc if r['codice'] == '08PA03635-00']
    check('08PA03635: 4 righe, una per commessa', [r['commessa'] for r in r35] == ['C26-200-4', 'C26-200-7', 'C26-200-12', 'C26-201-24'], r35)
    check('08PA03635: 2 pezzi per riga', [r['quantita'] for r in r35] == [2, 2, 2, 2])
    check('stesso prezzo unitario su tutte le righe', len({r['prezzo_unitario'] for r in r35}) == 1)
    r90 = [r for r in rc if r['codice'] == '47PA00990-00']
    check('47PA00990: 2+2+1+2 = 7', [r['quantita'] for r in r90] == [2, 2, 1, 2])
    pu = r90[0]['prezzo_unitario']
    check('importi = prezzo unitario x quantita\'', all(abs(r['importo'] - round(pu * r['quantita'], 2)) < 1e-9 for r in r90))

    print('\n2) Quantita\' cambiata a mano')
    d2 = A._preventivo_to_pdf_dati(preventivo('DECA S.r.l.', [pezzo('08PA03635-00', 10, R35)]))
    check('righe che non sommano (10 vs 8): una riga sola', len(d2['righe_cliente']) == 1 and d2['righe_cliente'][0]['quantita'] == 10)

    print('\n3) Altro cliente')
    d3 = A._preventivo_to_pdf_dati(preventivo('Todema', [pezzo('08PA03635-00', 8, R35)]))
    check('una riga sola, senza commessa', len(d3['righe_cliente']) == 1 and not d3['righe_cliente'][0].get('commessa'))

    print('\n4) PDF generato')
    tmp = os.path.join(tempfile.gettempdir(), f'test_righe_{uuid.uuid4().hex[:6]}.pdf')
    pdf_exporter.PDFPreventivo(A.BarcodeManager.load_config() or {}).genera_pdf(tmp, d, interno=False)
    import pdfplumber
    with pdfplumber.open(tmp) as f:
        testo = '\n'.join((pg.extract_text() or '') for pg in f.pages)
    check('colonna Commessa nel PDF', 'Commessa' in testo)
    check('tutte le commesse nel PDF', all(c in testo for c in ('C26-200-4', 'C26-200-7', 'C26-200-12', 'C26-201-24')))
    check('08PA03635-00 su 4 righe', testo.count('08PA03635-00') == 4, testo.count('08PA03635-00'))
    tmp3 = tmp.replace('.pdf', '_altro.pdf')
    pdf_exporter.PDFPreventivo(A.BarcodeManager.load_config() or {}).genera_pdf(tmp3, d3, interno=False)
    with pdfplumber.open(tmp3) as f:
        testo3 = '\n'.join((pg.extract_text() or '') for pg in f.pages)
    check('altro cliente: niente colonna Commessa', 'Commessa' not in testo3)
    for t in (tmp, tmp3):
        try:
            os.remove(t)
        except OSError:
            pass

    print('\n' + '=' * 60)
    print(f'PASSATI: {OK}   FALLITI: {len(KO)}')
    if KO:
        print('Falliti: ' + ', '.join(KO))
    print('=' * 60)
    return 1 if KO else 0


if __name__ == '__main__':
    sys.exit(main())
