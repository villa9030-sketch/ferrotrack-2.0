"""Abbinamenti articoli del cliente -> pezzi Lantek (tabella assiemi_lantek):
copia fuori dal database e ripristino.

Gli abbinamenti li fa Stefano una volta sola (es. articoli Poliform: quale pezzo
Lantek e quanti per articolo) e servono per tutte le forniture dopo: non si
devono perdere se il database viene ripulito o rifatto.

    python tools/abbinamenti_lantek.py esporta       -> dati_clienti/abbinamenti_lantek.json (+ .csv)
    python tools/abbinamenti_lantek.py importa FILE  -> rimette nel database quelli che mancano
                                                        (stesso codice + pezzo Lantek: non duplica)
"""
import csv
import json
import os
import sqlite3
import sys
import uuid

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get('FERROTRACK_DB') or os.path.join(APP, 'database', 'scheduler.db')
CARTELLA = os.path.join(APP, 'dati_clienti')


def esporta():
    c = sqlite3.connect(DB)
    cols = [r[1] for r in c.execute('pragma table_info(assiemi_lantek)')]
    righe = [dict(zip(cols, r)) for r in c.execute('select * from assiemi_lantek order by cliente, codice')]
    os.makedirs(CARTELLA, exist_ok=True)
    json.dump({'tabella': 'assiemi_lantek', 'colonne': cols, 'righe': righe},
              open(os.path.join(CARTELLA, 'abbinamenti_lantek.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1, default=str)
    with open(os.path.join(CARTELLA, 'abbinamenti_lantek.csv'), 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=cols, delimiter=';')
        w.writeheader()
        w.writerows(righe)
    print(f'esportati {len(righe)} abbinamenti in {CARTELLA}')


def importa(percorso):
    dati = json.load(open(percorso, encoding='utf-8'))
    c = sqlite3.connect(DB)
    cols = [r[1] for r in c.execute('pragma table_info(assiemi_lantek)')]
    n = 0
    for r in dati.get('righe') or []:
        if c.execute('select 1 from assiemi_lantek where codice = ? and coalesce(pezzo_lantek, \'\') = ?',
                     (r.get('codice'), r.get('pezzo_lantek') or '')).fetchone():
            continue
        r = {k: v for k, v in r.items() if k in cols}
        r.setdefault('id', str(uuid.uuid4()))
        c.execute(f"insert into assiemi_lantek ({', '.join(r)}) values ({', '.join('?' for _ in r)})", list(r.values()))
        n += 1
    c.commit()
    print(f'ripristinati {n} abbinamenti (gli altri c\'erano gia\')')


if __name__ == '__main__':
    if len(sys.argv) >= 2 and sys.argv[1] == 'esporta':
        esporta()
    elif len(sys.argv) >= 3 and sys.argv[1] == 'importa':
        importa(sys.argv[2])
    else:
        print(__doc__)
