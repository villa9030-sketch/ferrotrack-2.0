"""Prova la migrazione su una COPIA di un database esistente, senza toccare
quello di lavoro.

Da eseguire PRIMA di attivare il nuovo sistema su una macchina:

    python app/tools/verifica_migrazione.py

Di default parte dal piu' recente backup di agosto (pre-refactoring). Per
provare su un altro file basta passarlo come argomento.

Verifica che: nessun record vada perso, le tabelle si aggiungano soltanto, le
colonne nuove ci siano, la migrazione sia ripetibile e gli ordini restino
leggibili dopo.
"""
import os, shutil, sqlite3, sys, tempfile
APP = r'c:\Users\sv304\Documents\PROGETTI\schedulatore-laser\app'
sys.path.insert(0, APP)
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'

# Database PRE-REFACTORING (agosto): il test vero della migrazione.
import glob
if len(sys.argv) > 1:
    SRC = sys.argv[1]
else:
    _cand = sorted(glob.glob(os.path.join(APP, 'database', 'backups', '*.db')))
    if not _cand:
        raise SystemExit('Nessun backup trovato: passa un file come argomento.')
    SRC = _cand[0]
DST = os.path.join(tempfile.gettempdir(), 'verifica_migrazione.db')
shutil.copy2(SRC, DST)

def conta(db):
    c = sqlite3.connect(db)
    out = {}
    for t in ('orders', 'users', 'preventivi', 'order_files', 'officina_scans',
              'notifications', 'audit_log'):
        try:
            out[t] = c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
        except sqlite3.OperationalError:
            out[t] = 'assente'
    out['_tabelle'] = len([r for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")])
    c.close()
    return out

prima = conta(DST)
print('PRIMA :', prima)

from sqlalchemy import create_engine
from backend import models
from backend.models import Base
from backend import models_ore                     # noqa: F401
from backend.migrations_ore import migrate_ore

eng = create_engine('sqlite:///' + DST.replace(chr(92), '/'),
                    connect_args={'check_same_thread': False})
models.engine = eng
models.SessionLocal.configure(bind=eng)
Base.metadata.create_all(bind=eng)
esito = migrate_ore(eng)
print('MIGRAZIONE:', esito)

# idempotenza: rieseguirla non deve fare danni
esito2 = migrate_ore(eng)
print('RIESEGUITA:', esito2)

dopo = conta(DST)
print('DOPO  :', dopo)

ok = True
for t, n in prima.items():
    if t == '_tabelle':
        continue
    if dopo[t] != n:
        print(f'  [KO] {t}: {n} -> {dopo[t]}')
        ok = False
print('  [OK] nessun record perso' if ok else '  [KO] RECORD PERSI')
print(f'  [OK] tabelle: {prima["_tabelle"]} -> {dopo["_tabelle"]} (solo aggiunte)'
      if dopo['_tabelle'] >= prima['_tabelle'] else '  [KO] tabelle rimosse')

c = sqlite3.connect(DST)
cols = [r[1] for r in c.execute('PRAGMA table_info(orders)')]
attese = ['data_completamento_operativo', 'completato_operativo_da',
          'data_consegna_effettiva', 'consegna_registrata_da',
          'ddt_numero', 'ddt_data', 'consegna_parziale', 'note_consegna']
mancanti = [x for x in attese if x not in cols]
print('  [OK] colonne ordine aggiunte' if not mancanti else f'  [KO] mancano {mancanti}')
nuove = ['device_tokens', 'clienti', 'giornate_ore', 'righe_ore', 'ore_attese',
         'eccezioni_giorno', 'anomalie_ore', 'costo_orario', 'fatturato_cliente',
         'costi_materiali']
tab = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
assenti = [t for t in nuove if t not in tab]
print('  [OK] tabelle nuove create' if not assenti else f'  [KO] mancano {assenti}')
# l'app deve funzionare dopo la migrazione
from backend.database import OrderManager
ordini = OrderManager.get_all_orders_dict()
print(f'  [OK] {len(ordini)} ordini leggibili dopo la migrazione'
      if ordini else '  [KO] ordini non leggibili')
c.close(); eng.dispose()
print('\nDatabase di lavoro NON toccato:', SRC)
