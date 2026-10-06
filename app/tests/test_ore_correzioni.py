"""Correzioni dell'area ore (verifica di ottobre 2026).

 1. Una giornata con un cliente poi disattivato si puo' ancora ri-salvare
    (un cliente nuovo e disattivato resta invece rifiutato).
 2. Il dettaglio di un cliente conta le stesse ore del riepilogo anche se il
    nome e' scritto in modi diversi ("Deca" / "DECA S.r.l.").
 3. Un giorno senza tariffa rende incompleto solo il cliente che ci ha ore,
    non tutti.
 4. Le anomalie aperte si chiudono quando la giornata non e' piu' dovuta
    (persona messa "non tenuta").
 5. Una notifica non arrivata non si perde: si riprova al passo dopo.

Gira su DATABASE TEMPORANEO. Esecuzione: python app/tests/test_ore_correzioni.py
"""
import os
import sys
import tempfile
import uuid
from datetime import date, datetime, timedelta

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User  # noqa: E402
from backend import models_ore  # noqa: E402,F401
from backend.models_ore import AnomaliaOre, Cliente, OreAttese  # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), f'test_orecorr_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend import ore_service as svc       # noqa: E402
from backend import riepilogo_service as ri  # noqa: E402
from backend import anomalie_service as an   # noqa: E402

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


_oggi = date.today()
FINE_PREC = date(_oggi.year, _oggi.month, 1) - timedelta(days=1)
ANNO, MESE = FINE_PREC.year, FINE_PREC.month
G1 = date(ANNO, MESE, 10)
G2 = date(ANNO, MESE, 20)


def _lunedi_passato(settimane=2):
    d = _oggi - timedelta(weeks=settimane)
    return d - timedelta(days=d.isoweekday() - 1)


LUN = _lunedi_passato()


def sessione():
    return models.SessionLocal()


def setup():
    s = sessione()
    try:
        da = datetime.now() - timedelta(days=400)
        s.add(User(id='op1', name='Mario Rossi', role='Operaio Officina', is_active=True, created_at=da))
        s.add(User(id='op2', name='Luca Bianchi', role='Operaio Officina', is_active=True, created_at=da))
        s.add(User(id='elena', name='Elena', role='Impiegata', is_active=True))
        for n in ('Deca', 'DECA S.r.l.', 'Cliente Y', 'Vecchio Spa', 'Spento Srl'):
            s.add(Cliente(id=str(uuid.uuid4()), nome=n, attivo=(n != 'Spento Srl')))
        for op in ('op1', 'op2'):
            s.add(OreAttese(id=str(uuid.uuid4()), operatore_id=op, tenuto_alla_compilazione=True,
                            minuti_attesi=480, giorni_settimana=[1, 2, 3, 4, 5]))
        s.commit()
    finally:
        s.close()


def salva(op, d, righe, rev=None):
    return svc.salva_giornata(op, d.isoformat(), righe, origine='ufficio',
                              modificata_da='elena', revisione_attesa=rev,
                              richiesta_id=uuid.uuid4().hex)


def main():
    setup()

    print('1) Cliente disattivato dopo: la giornata si corregge ancora')
    r = salva('op1', G1, [{'cliente': 'Vecchio Spa', 'minuti': 240}])
    check('salvata con Vecchio Spa', r.get('success'), r)
    s = sessione()
    s.query(Cliente).filter(Cliente.nome == 'Vecchio Spa').update({Cliente.attivo: False})
    s.commit(); s.close()
    r2 = salva('op1', G1, [{'cliente': 'Vecchio Spa', 'minuti': 300}], rev=r['giornata']['revisione'])
    check('ri-salvata con il cliente ora disattivato', r2.get('success'), r2)
    r3 = salva('op1', G1, [{'cliente': 'Vecchio Spa', 'minuti': 300},
                           {'cliente': 'Spento Srl', 'minuti': 60}], rev=r2.get('giornata', {}).get('revisione'))
    check('un cliente disattivato NUOVO resta rifiutato', not r3.get('success') and 'Spento' in (r3.get('error') or ''), r3)

    print('\n2) Dettaglio cliente = stesse ore del riepilogo')
    salva('op2', G1, [{'cliente': 'Deca', 'minuti': 120}])
    salva('op2', G2, [{'cliente': 'DECA S.r.l.', 'minuti': 180}])
    rp = ri.riepilogo(anno=ANNO, mese=MESE)
    deca = [c for c in rp['clienti'] if 'deca' in c['cliente'].lower()]
    check('nel riepilogo una sola riga Deca da 5 h', len(deca) == 1 and deca[0]['minuti'] == 300, deca)
    det = ri.dettaglio_cliente(deca[0]['cliente'], anno=ANNO, mese=MESE) if deca else {'ore': []}
    check('il dettaglio conta le stesse 5 h', sum(x['minuti'] for x in det['ore']) == 300, det['ore'])

    print('\n3) Giorno senza tariffa: incompleto solo chi ci ha ore')
    ri.imposta_tariffa(date(ANNO, MESE, 15), 30.0, da='elena')   # G1 scoperto, G2 coperto
    salva('op1', G2, [{'cliente': 'Cliente Y', 'minuti': 60}])
    rp = ri.riepilogo(anno=ANNO, mese=MESE)
    y = next((c for c in rp['clienti'] if c['cliente'] == 'Cliente Y'), None)
    vecchio = next((c for c in rp['clienti'] if c['cliente'] == 'Vecchio Spa'), None)
    check('Cliente Y (ore solo dopo la tariffa) ha il costo completo',
          y and not y['costo_ore_incompleto'] and y['costo_ore'] == 30.0, y)
    check('Vecchio Spa (ore nel giorno scoperto) e\' incompleto', vecchio and vecchio['costo_ore_incompleto'], vecchio)
    check('i totali dicono che il costo e\' incompleto', rp['totali'].get('costo_ore_incompleto') is True, rp['totali'])

    print('\n4) Anomalia chiusa quando la giornata non e\' piu\' dovuta')
    an.controlla_periodo(LUN, LUN)
    aperte = [a for a in an.elenco_anomalie(stato=None, dal=LUN, al=LUN) if a['operatore_id'] == 'op2']
    check('op2 ha la giornata mancante', aperte and aperte[0]['stato'] == 'aperta', aperte)
    s = sessione()
    s.query(OreAttese).filter(OreAttese.operatore_id == 'op2').update({OreAttese.tenuto_alla_compilazione: False})
    s.commit(); s.close()
    an.controlla_periodo(LUN, LUN)
    dopo = [a for a in an.elenco_anomalie(stato=None, dal=LUN, al=LUN) if a['operatore_id'] == 'op2']
    check('messo "non tenuto": l\'anomalia si chiude', dopo and dopo[0]['stato'] == 'risolta', dopo)

    print('\n5) Notifica non arrivata: si riprova')
    s = sessione()
    s.query(AnomaliaOre).update({AnomaliaOre.notificata: False}); s.commit(); s.close()
    from backend.database import NotificationManager as NM
    vero = NM.create_notification
    NM.create_notification = staticmethod(lambda **k: None)   # fallisce senza sollevare
    try:
        n = an.notifica_anomalie()
    finally:
        NM.create_notification = vero
    s = sessione()
    ancora = s.query(AnomaliaOre).filter(AnomaliaOre.stato == 'aperta',
                                         AnomaliaOre.notificata == False).count()  # noqa: E712
    s.close()
    check('nessuna notifica contata come inviata', n == 0, n)
    check('le anomalie restano da notificare', ancora > 0, ancora)
    n2 = an.notifica_anomalie()
    check('al passo dopo arrivano', n2 > 0, n2)

    print(f'\nPASSATI: {OK}   FALLITI: {len(KO)}')
    return 0 if not KO else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    finally:
        try:
            _ENG.dispose(); os.remove(_TMP)
        except Exception:
            pass
