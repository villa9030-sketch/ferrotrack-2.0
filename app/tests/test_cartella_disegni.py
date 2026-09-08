# -*- coding: utf-8 -*-
"""L'etichetta della sottocartella esce giusta, e tace quando deve tacere.

Non tocca il database: la configurazione viene sostituita in memoria e la
cartella condivisa e' una finta, creata nella cartella temporanea.
"""
import os
import sys
import tempfile

os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
APP = r'c:\Users\sv304\Documents\PROGETTI\schedulatore-laser\app'
if APP not in sys.path:
    sys.path.insert(0, APP)

# backend/__init__ espone `app` come oggetto Flask: qui serve il MODULO.
import importlib  # noqa: E402
A = importlib.import_module('backend.app')  # noqa: E402

FINTA = os.path.join(tempfile.gettempdir(), 'ferrotrack_prova_disegni')
os.makedirs(os.path.join(FINTA, 'DECA S.r.l', 'PREV-2026-0007'), exist_ok=True)

_vera = A.BarcodeManager.load_config
esiti = []


def controlla(nome, atteso, ottenuto):
    ok = atteso == ottenuto
    esiti.append(ok)
    print('  [%s] %s' % ('OK' if ok else 'NO', nome))
    if not ok:
        print('        atteso  : %r' % (atteso,))
        print('        ottenuto: %r' % (ottenuto,))


ORD = {'id': 'abc-123', 'cliente': 'DECA S.r.l.', 'numero_ordine': 'PREV-2026-0007'}

print('Etichetta cartella disegni')

# 1. cartella condivisa configurata e la sottocartella esiste
A.BarcodeManager.load_config = staticmethod(lambda: {'disegni_export_root': FINTA})
controlla('dice cliente e numero ordine',
          'DECA S.r.l.  \u203a  PREV-2026-0007',
          A._etichetta_cartella_disegni(ORD))

# 2. configurata, ma quest'ordine non c'e' dentro
controlla('tace se la sottocartella non e\' stata preparata',
          '',
          A._etichetta_cartella_disegni(
              {'id': 'x', 'cliente': 'ALTRO', 'numero_ordine': 'PREV-9999'}))

# 3. cartella condivisa non configurata: non c'e' niente da dire
A.BarcodeManager.load_config = staticmethod(lambda: {'disegni_export_root': ''})
controlla('tace se la cartella condivisa non e\' configurata',
          '',
          A._etichetta_cartella_disegni(ORD))

# 4. ordine senza numero: non si puo' costruire un nome
A.BarcodeManager.load_config = staticmethod(lambda: {'disegni_export_root': FINTA})
controlla('tace se l\'ordine non ha numero',
          '',
          A._etichetta_cartella_disegni({'id': 'y', 'cliente': 'DECA S.r.l.',
                                         'numero_ordine': ''}))

# 5. una configurazione rotta non deve buttare giu' la pagina


def _esplode():
    raise RuntimeError('configurazione irraggiungibile')


A.BarcodeManager.load_config = staticmethod(_esplode)
controlla('regge se la configurazione non si legge', '',
          A._etichetta_cartella_disegni(ORD))

A.BarcodeManager.load_config = _vera
print('\nPASSATI: %d   FALLITI: %d' % (sum(esiti), len(esiti) - sum(esiti)))
sys.exit(0 if all(esiti) else 1)
