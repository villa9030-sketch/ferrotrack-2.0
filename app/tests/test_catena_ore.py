# -*- coding: utf-8 -*-
"""La catena delle ore arriva fino in fondo: operaio -> mancanze -> amministrazione.

Nasce da un guasto che nessun test avrebbe visto. Rinominando le utenze in
postazioni, l'avviso delle giornate mancanti cercava ancora un'utenza chiamata
"Impiegata" — appena spenta — e quindi non avvisava piu' NESSUNO. La funzione
tornava "zero avvisi inviati", che e' anche il risultato giusto quando non c'e'
niente da segnalare: il guasto e il buon funzionamento avevano lo stesso
aspetto.

Qui si controlla la catena intera, e in particolare che ci sia SEMPRE qualcuno
a cui l'avviso possa arrivare.

Il test lavora su un database temporaneo suo: quello di lavoro non si tocca.
"""
import os
import sys
import tempfile
import uuid
from datetime import date, timedelta

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import (Base, Notification, RUOLI_OPERAI, RUOLI_UFFICIO,  # noqa: E402
                            User)
from backend import models_ore  # noqa: E402
from backend.models_ore import AnomaliaOre, OreAttese  # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), 'test_catena_%s.db' % uuid.uuid4().hex[:8])
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend import anomalie_service as A          # noqa: E402
from backend import ore_service as svc             # noqa: E402
from backend.database import get_session           # noqa: E402

OK = 0
KO = []


def check(nome, cond, extra=''):
    global OK
    if cond:
        OK += 1
        print('  [OK] %s' % nome)
    else:
        KO.append(nome)
        print('  [KO] %s %s' % (nome, extra))


def main():
    global OK
    oggi = date.today()

    s = get_session()
    try:
        # L'amministrazione col nome NUOVO: e' il caso che si era rotto.
        s.add(User(id='postazione-amministrazione', name='Amministrazione',
                   role='Amministrazione', is_active=True, e_postazione=True))
        # Un operaio che deve dichiarare, e non dichiara.
        s.add(User(id='op-catena', name='Operaio Catena',
                   role='Operaio Officina', is_active=True, e_postazione=False))
        s.add(OreAttese(id=str(uuid.uuid4()), operatore_id='op-catena',
                        tenuto_alla_compilazione=True, minuti_attesi=480,
                        giorni_settimana=[1, 2, 3, 4, 5]))
        s.commit()
    finally:
        s.close()

    print('1) Chi deve dichiarare compare sulla bacheca della timbratrice')
    ids = {o['id'] for o in svc.elenco_operai()}
    check('l\'operaio c\'e\'', 'op-catena' in ids, sorted(ids))
    check('l\'amministrazione NON c\'e\'',
          'postazione-amministrazione' not in ids, sorted(ids))

    print('\n2) Le giornate non dichiarate vengono segnalate')
    esito = A.controlla_periodo(dal=(oggi - timedelta(days=9)).isoformat(),
                                al=(oggi - timedelta(days=1)).isoformat())
    check('il controllo ha guardato dei giorni', esito.get('giorni', 0) > 0, esito)
    aperte = A.elenco_anomalie(stato='aperta')
    check('ha trovato delle mancanze', len(aperte) > 0, esito)
    check('sono attribuite all\'operaio giusto',
          all(a.get('operatore_id') == 'op-catena' for a in aperte))

    print('\n3) L\'avviso arriva all\'amministrazione')
    # Questo e' il punto che si era rotto in silenzio.
    s = get_session()
    try:
        destinatari = [u.name for u in s.query(User).filter(
            User.is_active == True,  # noqa: E712
            User.role.in_(RUOLI_UFFICIO)).all()]
    finally:
        s.close()
    check('c\'e\' qualcuno a cui mandarlo', bool(destinatari), destinatari)

    inviati = A.notifica_anomalie()
    check('gli avvisi partono davvero', inviati > 0, inviati)

    s = get_session()
    try:
        note = s.query(Notification).filter(
            Notification.user_id == 'postazione-amministrazione').all()
        check('e arrivano all\'amministrazione', len(note) > 0, len(note))
    finally:
        s.close()

    print('\n4) Non si avvisa due volte della stessa cosa')
    di_nuovo = A.notifica_anomalie()
    check('rieseguendo non parte nulla di nuovo', di_nuovo == 0, di_nuovo)

    print('\n5) Senza nessuno in amministrazione lo si dice, non si tace')
    s = get_session()
    try:
        s.query(User).filter(User.id == 'postazione-amministrazione').update(
            {'is_active': False})
        s.query(AnomaliaOre).update({'notificata': False})
        s.commit()
    finally:
        s.close()
    # Non deve esplodere, e non deve fingere di aver avvisato qualcuno.
    check('non manda avvisi nel vuoto', A.notifica_anomalie() == 0)

    print('\n6) I nomi vecchi continuano a valere')
    s = get_session()
    try:
        s.add(User(id='vecchia-impiegata', name='Impiegata di prima',
                   role='Impiegata', is_active=True, e_postazione=True))
        s.query(AnomaliaOre).update({'notificata': False})
        s.commit()
    finally:
        s.close()
    check('un\'utenza col ruolo vecchio riceve comunque',
          A.notifica_anomalie() > 0)

    print('\n7) Gli elenchi dei ruoli sono coerenti fra loro')
    check('un operaio non e\' anche un posto d\'ufficio',
          not (set(RUOLI_OPERAI) & set(RUOLI_UFFICIO)))
    check('il ruolo nuovo e quello vecchio sono entrambi ammessi',
          'Amministrazione' in RUOLI_UFFICIO and 'Impiegata' in RUOLI_UFFICIO)

    print('\n' + '=' * 60)
    print('PASSATI: %d   FALLITI: %d' % (OK, len(KO)))
    if KO:
        print('Falliti: ' + ', '.join(KO))
    print('=' * 60)
    return 1 if KO else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    finally:
        try:
            _ENG.dispose()
        except Exception:
            pass
        for coda in ('', '-wal', '-shm'):
            try:
                os.remove(_TMP + coda)
            except OSError:
                pass
