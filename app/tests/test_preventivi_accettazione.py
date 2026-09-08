"""Test dell'ACCETTAZIONE del preventivo (sezione 10.8 della specifica).

L'accettazione era il punto piu' pericoloso del preventivatore: il preventivo
veniva marcato ACCETTATO e salvato PRIMA di creare l'ordine. Se la creazione
falliva restava una commessa "presa" di cui in officina non arrivava nulla.

Copre:
 1. accettazione normale: ordine creato e prezzo concordato trasferito
 2. doppio clic / retry: nessun ordine doppio
 3. errore nella creazione dell'ordine: il preventivo resta INVIATO
 4. errore nel passaggio ad ACCETTATO: l'ordine creato viene annullato
 5. recupero di un preventivo ACCETTATO rimasto senza ordine
 6. errore nel salvataggio articoli: il preventivo non resta in BOZZA

Gira su DATABASE TEMPORANEO. Esecuzione:
    python app/tests/test_preventivi_accettazione.py
"""
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

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Order, Preventivo  # noqa: E402
from backend import models_ore  # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), f'test_acc_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend import database as db  # noqa: E402
from backend.database import PreventivoManager, OrderManager  # noqa: E402

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


def crea_inviato(cliente='Cliente Alfa', totale=1500.0, quantita=3):
    """Preventivo pronto per l'accettazione."""
    r = PreventivoManager.create(cliente, 'commerciale', quantita=quantita)
    pid = r['id'] if isinstance(r, dict) else r
    s = models.SessionLocal()
    try:
        p = s.query(Preventivo).filter(Preventivo.id == pid).first()
        p.status = 'INVIATO'
        p.totale_lotto = totale
        p.totale_pezzo_con_margine = round(totale / quantita, 2)
        s.commit()
    finally:
        s.close()
    return pid


def stato(pid):
    s = models.SessionLocal()
    try:
        p = s.query(Preventivo).filter(Preventivo.id == pid).first()
        return p.status if p else None
    finally:
        s.close()


def ordini_di(pid, includi_annullati=False):
    s = models.SessionLocal()
    try:
        q = s.query(Order).filter(Order.preventivo_id_origine == pid)
        if not includi_annullati:
            q = q.filter(Order.is_deleted == False)  # noqa: E712
        return q.all()
    finally:
        s.close()


def main():
    s = models.SessionLocal()
    try:
        s.add(User(id='commerciale', name='Marco Villa', role='Commerciale',
                   is_active=True))
        s.commit()
    finally:
        s.close()

    # =====================================================================
    print('\n1) Accettazione normale')
    pid = crea_inviato(totale=1500.0, quantita=3)
    r = PreventivoManager.accetta_e_crea_ordine(pid, 'commerciale')
    check('accettazione riuscita', r.get('success') is True, r)
    check('preventivo ACCETTATO', stato(pid) == 'ACCETTATO', stato(pid))
    ordini = ordini_di(pid)
    check('un solo ordine creato', len(ordini) == 1, len(ordini))
    check('numero ordine restituito', bool(r.get('numero_ordine')), r)
    check('prezzo concordato sull ordine (1500)',
          ordini and ordini[0].prezzo_quotato == 1500.0,
          ordini[0].prezzo_quotato if ordini else None)
    check('origine tracciata', ordini and ordini[0].origine == 'PREVENTIVO')

    # =====================================================================
    print('\n2) Doppio clic e retry: nessun ordine doppio')
    r2 = PreventivoManager.accetta_e_crea_ordine(pid, 'commerciale')
    check('seconda chiamata non fallisce', r2.get('success') is True, r2)
    check('lo dichiara: gia creato', r2.get('gia_creato') is True, r2)
    check('stesso ordine, non uno nuovo', r2.get('order_id') == r.get('order_id'),
          (r.get('order_id'), r2.get('order_id')))
    check('sempre un solo ordine', len(ordini_di(pid)) == 1, len(ordini_di(pid)))

    # =====================================================================
    print('\n3) Se la creazione dell ordine fallisce, niente resta a meta')
    pid3 = crea_inviato(totale=800.0)
    originale = OrderManager.create_order_from_preventivo

    def esplode(*a, **k):
        raise RuntimeError('disco pieno')

    OrderManager.create_order_from_preventivo = staticmethod(esplode)
    try:
        r3 = PreventivoManager.accetta_e_crea_ordine(pid3, 'commerciale')
    finally:
        OrderManager.create_order_from_preventivo = originale
    check('errore riportato', bool(r3.get('error')), r3)
    check('il preventivo resta INVIATO (riprovabile)', stato(pid3) == 'INVIATO',
          stato(pid3))
    check('nessun ordine creato', len(ordini_di(pid3)) == 0)

    r3b = PreventivoManager.accetta_e_crea_ordine(pid3, 'commerciale')
    check('al secondo tentativo funziona', r3b.get('success') is True, r3b)
    check('e crea un solo ordine', len(ordini_di(pid3)) == 1)

    # =====================================================================
    print('\n4) Se fallisce il passaggio ad ACCETTATO, l ordine viene annullato')
    pid4 = crea_inviato(totale=999.0)
    vera_sessione = db.get_session
    stato_chiamate = {'n': 0}

    class SessioneRotta:
        """Guasto transitorio: fallisce SOLO il commit che marca ACCETTATO.
        Poi la sessione torna a funzionare, altrimenti non potrebbe funzionare
        nemmeno la pulizia e il test non proverebbe nulla."""
        def __init__(self, reale):
            self._r = reale

        def __getattr__(self, nome):
            return getattr(self._r, nome)

        def commit(self):
            stato_chiamate['n'] += 1
            if stato_chiamate['n'] == 1:
                stato_chiamate['armata'] = False   # il guasto e' passato
                raise RuntimeError('connessione persa')
            return self._r.commit()

    def sessione_finta():
        reale = vera_sessione()
        # Solo dopo che l'ordine e' stato creato: la prima parte deve funzionare
        if stato_chiamate.get('armata'):
            return SessioneRotta(reale)
        return reale

    r4 = None
    try:
        # si arma dopo la creazione dell'ordine
        originale_create = OrderManager.create_order_from_preventivo

        def crea_e_arma(*a, **k):
            out = originale_create(*a, **k)
            stato_chiamate['armata'] = True
            db.get_session = sessione_finta
            return out

        OrderManager.create_order_from_preventivo = staticmethod(crea_e_arma)
        r4 = PreventivoManager.accetta_e_crea_ordine(pid4, 'commerciale')
    finally:
        db.get_session = vera_sessione
        OrderManager.create_order_from_preventivo = originale_create
        stato_chiamate['armata'] = False

    check('errore riportato senza promesse', bool(r4 and r4.get('error')), r4)
    check('nessun ordine ATTIVO resta appeso', len(ordini_di(pid4)) == 0,
          [o.id for o in ordini_di(pid4)])
    check('il preventivo non e ACCETTATO a vuoto', stato(pid4) != 'ACCETTATO',
          stato(pid4))

    # =====================================================================
    print('\n5) Recupero di un ACCETTATO rimasto senza ordine')
    pid5 = crea_inviato(totale=250.0)
    s = models.SessionLocal()
    try:
        p = s.query(Preventivo).filter(Preventivo.id == pid5).first()
        p.status = 'ACCETTATO'          # lo stato rotto lasciato dalla vecchia versione
        s.commit()
    finally:
        s.close()
    check('partenza: ACCETTATO senza ordine',
          stato(pid5) == 'ACCETTATO' and len(ordini_di(pid5)) == 0)
    r5 = PreventivoManager.accetta_e_crea_ordine(pid5, 'commerciale')
    check('il recupero crea l ordine mancante', r5.get('success') is True, r5)
    check('ora l ordine c e', len(ordini_di(pid5)) == 1)
    check('e con il suo prezzo', ordini_di(pid5)[0].prezzo_quotato == 250.0)

    # =====================================================================
    print('\n6) Errore nel salvataggio articoli: niente BOZZA appesa')
    pid6 = crea_inviato(totale=300.0)
    originale_repl = PreventivoManager.replace_articoli

    def repl_esplode(*a, **k):
        raise RuntimeError('errore nel salvataggio')

    PreventivoManager.replace_articoli = staticmethod(repl_esplode)
    try:
        r6 = PreventivoManager.accetta_e_crea_ordine(
            pid6, 'commerciale', articoli=[{'codice': 'X', 'quantita': 1}])
    finally:
        PreventivoManager.replace_articoli = originale_repl
    check('errore riportato', bool(r6.get('error')), r6)
    check('il preventivo NON resta in BOZZA', stato(pid6) != 'BOZZA', stato(pid6))
    check('resta INVIATO, quindi riprovabile', stato(pid6) == 'INVIATO', stato(pid6))
    check('nessun ordine creato', len(ordini_di(pid6)) == 0)

    # =====================================================================
    print('\n7) Preventivo in stato sbagliato')
    pid7 = PreventivoManager.create('Cliente Beta', 'commerciale')
    pid7 = pid7['id'] if isinstance(pid7, dict) else pid7
    r7 = PreventivoManager.accetta_e_crea_ordine(pid7, 'commerciale')
    check('una BOZZA non si accetta', bool(r7.get('error')), r7)
    check('e resta BOZZA', stato(pid7) == 'BOZZA', stato(pid7))
    r8 = PreventivoManager.accetta_e_crea_ordine('non-esiste', 'commerciale')
    check('preventivo inesistente respinto', bool(r8.get('error')), r8)

    print('\n' + '=' * 60)
    print(f'PASSATI: {OK}   FALLITI: {len(KO)}')
    if KO:
        print('Falliti: ' + ', '.join(KO))
    print('=' * 60)
    return 1 if KO else 0


if __name__ == '__main__':
    code = main()
    try:
        _ENG.dispose()
        os.remove(_TMP)
    except Exception:
        pass
    sys.exit(code)
