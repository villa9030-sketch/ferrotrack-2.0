# -*- coding: utf-8 -*-
"""Prima dell'avvio del sistema non si segnalano giornate mancanti.

Nasce da un guaio che si sarebbe visto solo il primo giorno di uso vero: il
controllo guarda 30 giorni indietro, quindi Elena avrebbe aperto la pagina e
trovato una cinquantina di anomalie per giornate in cui a nessuno era stato
chiesto di dichiarare niente. Le anomalie vere sarebbero sparite nel mucchio.

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
from backend.models import Base, User  # noqa: E402
from backend import models_ore  # noqa: E402
from backend.models_ore import AnomaliaOre, OreAttese  # noqa: E402

# Database usa e getta: quello di lavoro non si tocca.
_TMP = os.path.join(tempfile.gettempdir(),
                    'test_avvio_%s.db' % uuid.uuid4().hex[:8])
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend import anomalie_service as A          # noqa: E402
from backend.database import get_session, BarcodeManager  # noqa: E402

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
        s.add(User(id='op-prova', name='Operaio Prova', role='Operaio Officina',
                   is_active=True))
        s.add(OreAttese(id=str(uuid.uuid4()), operatore_id='op-prova',
                        tenuto_alla_compilazione=True, minuti_attesi=480,
                        giorni_settimana=[1, 2, 3, 4, 5]))
        s.commit()
    finally:
        s.close()

    vera = BarcodeManager.load_config

    def con_avvio(valore):
        BarcodeManager.load_config = staticmethod(
            lambda: {'ore_attive_dal': valore})

    # 1. Senza data di avvio: si controlla tutto lo storico, come prima.
    con_avvio('')
    r = A.controlla_periodo()
    check('senza data di avvio controlla tutto lo storico',
          r.get('giorni', 0) > 20, r)

    s = get_session()
    try:
        s.query(AnomaliaOre).delete()
        s.commit()
    finally:
        s.close()

    # 2. Con la data di avvio a ieri: si guarda solo da li' in poi.
    con_avvio((oggi - timedelta(days=1)).isoformat())
    r = A.controlla_periodo()
    check('con la data di avvio guarda solo da li in poi',
          r.get('giorni', 0) <= 1, r)

    # 3. Data di avvio nel futuro: non c'e' niente da controllare.
    con_avvio((oggi + timedelta(days=10)).isoformat())
    r = A.controlla_periodo()
    check('data di avvio futura: nessun giorno da controllare',
          r.get('giorni', 0) == 0, r)
    check('e lo dice, invece di tacere', bool(r.get('nota')), r)

    # 4. Una data scritta male non deve bloccare il controllo.
    con_avvio('non-e-una-data')
    try:
        r = A.controlla_periodo()
        check('una data scritta male non blocca il controllo',
              isinstance(r, dict) and 'giorni' in r, r)
    except Exception as e:
        check('una data scritta male non blocca il controllo', False, str(e)[:80])

    # 5. La data di avvio vale anche quando il periodo e' chiesto a mano:
    #    prima di quel giorno non c'e' niente da controllare, chiunque lo chieda.
    con_avvio(oggi.isoformat())
    r = A.controlla_periodo(dal=(oggi - timedelta(days=5)).isoformat(),
                            al=(oggi - timedelta(days=1)).isoformat())
    check('la data di avvio vale anche su un periodo chiesto a mano',
          r.get('giorni', 0) == 0, r)

    BarcodeManager.load_config = vera

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
        for coda in ('', '-wal', '-shm'):
            try:
                os.remove(_TMP + coda)
            except OSError:
                pass
