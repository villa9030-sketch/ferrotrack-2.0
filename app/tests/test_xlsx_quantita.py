"""Test dell'IMPORTAZIONE XLSX (sezione 10.7 della specifica).

L'importatore leggeva la quantita' dal file per ricavare il costo unitario e
poi la buttava via: ogni riga diventava 1 pezzo. Una riga da 50 pezzi entrava
nel preventivo come 1, e il totale usciva 50 volte piu' basso.

Copre:
 1. la quantita' del file arriva nell'articolo
 2. righe duplicate: si sommano le quantita', non si contano le righe
 3. costo unitario e costo totale restano coerenti fra loro
 4. quantita' assenti o illeggibili non fanno saltare l'importazione
 5. file senza articoli: errore chiaro

Gira su FILE TEMPORANEI. Esecuzione: python app/tests/test_xlsx_quantita.py
"""
import os
import sys
import tempfile
import uuid

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

import openpyxl  # noqa: E402

from backend.preventivi.xlsx_importer import importa_xlsx  # noqa: E402

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


def crea_xlsx(righe):
    """File nel formato "multi" (ordine di produzione): codice in colonna 2,
    quantita' in colonna 22, costo totale di riga in colonna 23.
    Gli indici nel codice sono 0-based, in openpyxl le colonne partono da 1.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    intestazioni = [''] * 23
    intestazioni[1] = 'Articolo'
    intestazioni[21] = 'Qta'
    intestazioni[22] = 'Costo'
    ws.append(intestazioni)
    for codice, qta, costo_totale in righe:
        r = [None] * 23
        r[1] = codice
        r[21] = qta
        r[22] = costo_totale
        ws.append(r)
    path = os.path.join(tempfile.gettempdir(), f'test_{uuid.uuid4().hex[:8]}.xlsx')
    wb.save(path)
    wb.close()
    return path


def per_codice(articoli):
    return {a['codice']: a for a in articoli}


def main():
    creati = []

    # =====================================================================
    print('\n1) La quantita del file arriva nell articolo')
    p = crea_xlsx([('PZ-A', 50, 1000.0)])
    creati.append(p)
    art = per_codice(importa_xlsx(p))
    a = art.get('PZ-A')
    check('articolo importato', a is not None, list(art))
    check('quantita 50 (non 1)', a and a['quantita'] == 50, a)
    check('costo unitario 20,00', a and round(a['costo'], 2) == 20.0, a)
    check('costo totale 1000,00', a and round(a['costo_totale'], 2) == 1000.0, a)

    # =====================================================================
    print('\n2) Righe duplicate: si sommano le quantita')
    p = crea_xlsx([('PZ-B', 20, 400.0), ('PZ-B', 20, 400.0), ('PZ-B', 20, 400.0)])
    creati.append(p)
    a = per_codice(importa_xlsx(p))['PZ-B']
    check('60 pezzi, non 3', a['quantita'] == 60, a['quantita'])
    check('costo totale 1200,00', round(a['costo_totale'], 2) == 1200.0, a)
    check('costo unitario ancora 20,00', round(a['costo'], 2) == 20.0, a)

    print('\n   duplicati con quantita e prezzi diversi')
    p = crea_xlsx([('PZ-C', 10, 100.0), ('PZ-C', 5, 75.0)])
    creati.append(p)
    a = per_codice(importa_xlsx(p))['PZ-C']
    check('15 pezzi in totale', a['quantita'] == 15, a['quantita'])
    check('costo totale 175,00', round(a['costo_totale'], 2) == 175.0, a)
    check('costo unitario medio 11,67',
          round(a['costo'], 2) == 11.67, round(a['costo'], 2))

    # =====================================================================
    print('\n3) Coerenza fra unitario e totale')
    p = crea_xlsx([('PZ-D', 7, 91.0), ('PZ-E', 1, 33.0), ('PZ-F', 3, 45.0)])
    creati.append(p)
    art = importa_xlsx(p)
    coerenti = all(
        abs(x['costo'] * x['quantita'] - x['costo_totale']) < 0.01 for x in art)
    check('unitario x quantita = totale, per ogni articolo', coerenti,
          [(x['codice'], x['costo'], x['quantita'], x['costo_totale']) for x in art])
    somma = sum(x['costo_totale'] for x in art)
    check('somma dei totali = somma delle righe del file',
          round(somma, 2) == 169.0, somma)

    # =====================================================================
    print('\n4) Quantita assenti o illeggibili')
    p = crea_xlsx([('PZ-G', None, 50.0), ('PZ-H', 'tre', 60.0),
                   ('PZ-I', 0, 70.0), ('PZ-L', -4, 80.0)])
    creati.append(p)
    art = per_codice(importa_xlsx(p))
    check('quantita mancante -> 1 pezzo', art['PZ-G']['quantita'] == 1, art.get('PZ-G'))
    check('quantita non numerica -> 1 pezzo', art['PZ-H']['quantita'] == 1, art.get('PZ-H'))
    check('quantita zero -> 1 pezzo', art['PZ-I']['quantita'] == 1, art.get('PZ-I'))
    check('quantita negativa -> 1 pezzo', art['PZ-L']['quantita'] == 1, art.get('PZ-L'))
    check('nessun costo negativo o assurdo',
          all(x['costo'] >= 0 for x in art.values()), art)

    # =====================================================================
    print('\n5) File senza articoli')
    p = crea_xlsx([])
    creati.append(p)
    try:
        importa_xlsx(p)
        check('errore chiaro su file vuoto', False, 'nessun errore sollevato')
    except ValueError as e:
        check('errore chiaro su file vuoto', 'Nessun articolo' in str(e), str(e))

    for f in creati:
        try:
            os.remove(f)
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
