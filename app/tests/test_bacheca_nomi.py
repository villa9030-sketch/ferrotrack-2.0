# -*- coding: utf-8 -*-
"""I nomi sulla bacheca sono etichette, e i conti si fanno per cliente.

Due cose che, sbagliate, si vedono solo in officina.

La prima: aggiungere una persona deve costare un nome. Se costa cinque campi in
un pannello di amministrazione — identificativo, ruolo, fase — chi arriva oggi
il primo giorno le ore non le segna. E non deve trovarsi addosso le mancanze
dei giorni in cui non c'era: sarebbe una colonna di allarmi su una persona
appena entrata.

La seconda: lo stesso cliente scritto in tre modi non fa tre clienti. "DECA",
"deca" e "DECA S.r.l." finivano in tre righe distinte, e il totale del cliente
principale non compariva da nessuna parte. Ogni riga, presa da sola, era giusta:
per questo non se n'era accorto nessuno.

Il test lavora su un database temporaneo suo: quello di lavoro non si tocca.
"""
import os
import sys
import tempfile
import uuid
from datetime import date, datetime, timedelta

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
from backend.models_ore import AnomaliaOre, Cliente, OreAttese  # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), 'test_nomi_%s.db' % uuid.uuid4().hex[:8])
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend import anomalie_service as an   # noqa: E402
from backend import ore_service as svc       # noqa: E402
from backend.database import get_session     # noqa: E402

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

    print('1) Aggiungere una persona costa un nome, e nient\'altro')
    r = svc.aggiungi_operaio('Mario Rossi')
    check('la persona viene aggiunta', r.get('success'), r)
    mario = r.get('operatore_id')
    check('l\'identificativo se lo costruisce da solo', mario == 'mario-rossi', mario)

    s = get_session()
    try:
        u = s.query(User).filter(User.id == mario).first()
        check('non ha permessi', not (u.permissions or []), u.permissions)
        check('non e\' una postazione: non entra nel programma', not u.e_postazione)
        check('compare sulla bacheca',
              mario in {o['id'] for o in svc.elenco_operai()})
    finally:
        s.close()

    print('\n2) Lo stesso nome non si aggiunge due volte')
    r2 = svc.aggiungi_operaio('  mario   rossi ')
    check('riconosce che c\'e\' gia\'', r2.get('codice') == 'gia_presente', r2)
    check('e dice chi', 'Mario Rossi' in (r2.get('error') or ''), r2)
    r3 = svc.aggiungi_operaio('X')
    check('un nome di una lettera non passa', not r3.get('success'), r3)

    print('\n3) Chi arriva oggi non risponde dei giorni in cui non c\'era')
    an.controlla_periodo(dal=(oggi - timedelta(days=9)).isoformat(),
                         al=(oggi - timedelta(days=1)).isoformat())
    sue = [a for a in an.elenco_anomalie(stato='aperta')
           if a.get('operatore_id') == mario]
    check('nessuna mancanza retroattiva', not sue, '%d mancanze' % len(sue))

    # Uno che invece c'e' da un mese le mancanze le ha, eccome.
    s = get_session()
    try:
        s.add(User(id='vecchio', name='Gia Presente', role='Operaio',
                   is_active=True, e_postazione=False,
                   created_at=datetime.now() - timedelta(days=30)))
        s.add(OreAttese(id=str(uuid.uuid4()), operatore_id='vecchio',
                        tenuto_alla_compilazione=True, minuti_attesi=480,
                        giorni_settimana=[1, 2, 3, 4, 5]))
        s.commit()
    finally:
        s.close()
    an.controlla_periodo(dal=(oggi - timedelta(days=9)).isoformat(),
                         al=(oggi - timedelta(days=1)).isoformat())
    vecchie = [a for a in an.elenco_anomalie(stato='aperta')
               if a.get('operatore_id') == 'vecchio']
    check('chi c\'era invece risponde', len(vecchie) > 0, vecchie)

    print('\n4) Togliere un nome non cancella le ore gia\' dichiarate')
    r4 = svc.togli_operaio(mario)
    check('il nome si toglie', r4.get('success'), r4)
    check('e sparisce dalla bacheca',
          mario not in {o['id'] for o in svc.elenco_operai()})
    s = get_session()
    try:
        check('ma la persona resta in archivio',
              s.query(User).filter(User.id == mario).first() is not None)
    finally:
        s.close()

    print("\n4b) Fra chi compila le ore ci sono solo persone")
    # Le postazioni non dichiarano ore: comparire in quell'elenco le farebbe
    # sembrare gente a cui si puo' chiedere una giornata di lavoro.
    s = get_session()
    try:
        s.add(User(id='postazione-prova', name='Tablet di prova',
                   role='Visione', is_active=True, e_postazione=True))
        s.commit()
    finally:
        s.close()
    elenco = {c['operatore_id'] for c in an.elenco_configurazione()}
    check('le postazioni non compaiono', 'postazione-prova' not in elenco,
          sorted(elenco))
    check("le persone si'", 'vecchio' in elenco, sorted(elenco))


    print('\n5) Lo stesso cliente scritto in modi diversi e\' UN cliente')
    coppie = [('DECA', 120), ('deca', 360), ('DECA S.r.l.', 360),
              ('Urgente Meccanica', 60), ('URGENTE MECCANICA S.p.A.', 60)]
    fuori = svc.raggruppa_per_cliente(coppie)
    per_nome = {v['cliente']: v['minuti'] for v in fuori}
    check('DECA e\' una riga sola', len(fuori) == 2, per_nome)
    check('e somma tutto: 14 ore', per_nome.get('DECA S.r.l.') == 840, per_nome)
    check('si mostra la scrittura piu\' completa',
          'DECA S.r.l.' in per_nome and 'deca' not in per_nome, per_nome)
    check('anche Urgente Meccanica e\' una sola',
          per_nome.get('URGENTE MECCANICA S.p.A.') == 120, per_nome)
    check('l\'ordine e\' dal piu\' lavorato',
          fuori[0]['cliente'] == 'DECA S.r.l.', [v['cliente'] for v in fuori])

    print('\n6) Ma clienti davvero diversi restano diversi')
    check('nomi diversi non si fondono',
          svc.chiave_cliente('DECA') != svc.chiave_cliente('DELTA'))
    check('la forma societaria conta solo in coda',
          svc.chiave_cliente('SRL Costruzioni') != svc.chiave_cliente('Costruzioni Srl'))
    check('un nome vuoto non fa una riga',
          not svc.raggruppa_per_cliente([(None, 60), ('', 60)]))

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
