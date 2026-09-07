"""Test del CONTROLLO MANCANZE (anomalie ore).

Copre i criteri 7-10 della sezione 12:
  7. Giorni non lavorativi, assenze e orari ridotti gestiti
  8. Giornata mancante: anomalia presente
  9. Riesecuzione/riavvio: nessuna notifica duplicata, recupero arretrati
 10. Correzione dell'ufficio: anomalia risolta automaticamente
  +  "dichiarata a zero" produce 'sotto', NON 'mancante'
  +  non si assumono 8 ore per tutti

Gira su DATABASE TEMPORANEO. Esecuzione: python app/tests/test_anomalie_service.py
"""
import os
import sys
import tempfile
import uuid
from datetime import date, timedelta

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Notification  # noqa: E402
from backend import models_ore  # noqa: E402
from backend.models_ore import Cliente, OreAttese  # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), f'test_anom_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend import ore_service as svc      # noqa: E402
from backend import anomalie_service as an  # noqa: E402

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


# Lunedi' e domenica di riferimento, nel passato (non oggi: oggi e' in corso)
def _lunedi_passato(settimane=2):
    d = date.today() - timedelta(weeks=settimane)
    return d - timedelta(days=d.isoweekday() - 1)


LUN = _lunedi_passato()
MAR = LUN + timedelta(days=1)
MER = LUN + timedelta(days=2)
DOM = LUN + timedelta(days=6)


def setup():
    s = models.SessionLocal()
    try:
        s.add(User(id='op1', name='Mario Rossi', role='Operaio Officina', is_active=True))
        s.add(User(id='op2', name='Luca Bianchi', role='Operaio Laser', is_active=True))
        s.add(User(id='op3', name='Nino Verdi', role='Operaio Officina', is_active=True))
        s.add(User(id='elena', name='Elena', role='Impiegata', is_active=True))
        s.add(Cliente(id=str(uuid.uuid4()), nome='Cliente Y', attivo=True))
        # op1: 8h lun-ven ; op2: 4h solo lun-mer ; op3: NON tenuto
        s.add(OreAttese(id=str(uuid.uuid4()), operatore_id='op1',
                        tenuto_alla_compilazione=True, minuti_attesi=480,
                        giorni_settimana=[1, 2, 3, 4, 5]))
        s.add(OreAttese(id=str(uuid.uuid4()), operatore_id='op2',
                        tenuto_alla_compilazione=True, minuti_attesi=240,
                        giorni_settimana=[1, 2, 3]))
        s.add(OreAttese(id=str(uuid.uuid4()), operatore_id='op3',
                        tenuto_alla_compilazione=False, minuti_attesi=480,
                        giorni_settimana=[1, 2, 3, 4, 5]))
        s.commit()
    finally:
        s.close()


def anomalia(op, giorno):
    for a in an.elenco_anomalie(stato=None, dal=giorno, al=giorno):
        if a['operatore_id'] == op:
            return a
    return None


def n_notifiche():
    s = models.SessionLocal()
    try:
        return s.query(Notification).filter(
            Notification.notification_type == 'ore_anomalia').count()
    finally:
        s.close()


def main():
    setup()
    print(f'\nPeriodo di prova: {LUN} (lun) .. {DOM} (dom)\n')

    # --- 1. Ore attese diverse per operaio -------------------------------
    print('1) Le ore attese NON sono 8 per tutti')
    check('op1 lunedi -> 480', an.minuti_attesi('op1', LUN) == 480, an.minuti_attesi('op1', LUN))
    check('op2 lunedi -> 240', an.minuti_attesi('op2', LUN) == 240, an.minuti_attesi('op2', LUN))
    check('op2 giovedi -> non dovuta', an.minuti_attesi('op2', LUN + timedelta(days=3)) is None)
    check('op1 domenica -> non dovuta', an.minuti_attesi('op1', DOM) is None)
    check('op3 non tenuto -> non dovuta', an.minuti_attesi('op3', LUN) is None)

    # --- 2. Giornata mancante --------------------------------------------
    print('\n2) Giornata non compilata')
    r = an.controlla_periodo(LUN, DOM)
    check('controllo eseguito', not r.get('error'), r)
    a = anomalia('op1', LUN)
    check('op1 lunedi -> anomalia mancante', a and a['tipo'] == 'mancante', a)
    check('op1 domenica -> nessuna anomalia', anomalia('op1', DOM) is None)
    check('op3 (non tenuto) -> nessuna anomalia', anomalia('op3', LUN) is None)
    check('op2 giovedi -> nessuna anomalia',
          anomalia('op2', LUN + timedelta(days=3)) is None)

    # --- 3. Dichiarata a zero != mancante --------------------------------
    print('\n3) Giornata dichiarata a ZERO')
    svc.salva_giornata('op1', MAR, [], origine='ufficio', richiesta_id='z1')
    an.rivaluta_giornata('op1', MAR)
    a = anomalia('op1', MAR)
    check('dichiarata a zero -> anomalia SOTTO (non mancante)',
          a and a['tipo'] == 'sotto', a)
    check('registra 0 dichiarati su 480 attesi',
          a and a['minuti_dichiarati'] == 0 and a['minuti_attesi'] == 480, a)

    # --- 4. Sotto / sopra / esatto ---------------------------------------
    print('\n4) Ore inferiori, superiori ed esatte')
    svc.salva_giornata('op1', MER, [{'cliente': 'Cliente Y', 'minuti': 300}],
                       origine='ufficio', richiesta_id='s1')
    an.rivaluta_giornata('op1', MER)
    check('300/480 -> sotto', (anomalia('op1', MER) or {}).get('tipo') == 'sotto')

    svc.salva_giornata('op1', MER, [{'cliente': 'Cliente Y', 'minuti': 600}],
                       origine='ufficio', revisione_attesa=1, richiesta_id='s2')
    an.rivaluta_giornata('op1', MER)
    a = anomalia('op1', MER)
    check('600/480 -> sopra (non cancellata)', a and a['tipo'] == 'sopra', a)
    check('le ore in eccesso restano registrate', a['minuti_dichiarati'] == 600)

    # --- 5. Correzione dell'ufficio risolve l'anomalia -------------------
    print('\n5) Correzione dell ufficio')
    svc.salva_giornata('op1', MER, [{'cliente': 'Cliente Y', 'minuti': 480}],
                       origine='ufficio', revisione_attesa=2,
                       modificata_da='elena', richiesta_id='s3')
    an.rivaluta_giornata('op1', MER)
    a = anomalia('op1', MER)
    check('480/480 -> anomalia RISOLTA', a and a['stato'] == 'risolta', a)
    g = svc.leggi_giornata('op1', MER)
    check('traccia della modifica (origine ufficio)', g.get('origine') == 'ufficio', g)

    # --- 6. Assenza e giornata ridotta ------------------------------------
    print('\n6) Assenze e giornate ridotte')
    r = an.salva_eccezione('op1', LUN, 'assenza', nota='ferie', da='elena')
    check('assenza registrata', r.get('success'), r)
    a = anomalia('op1', LUN)
    check('assenza -> anomalia risolta automaticamente',
          a is None or a['stato'] == 'risolta', a)

    giovedi = LUN + timedelta(days=3)
    an.salva_eccezione('op1', giovedi, 'ridotta', minuti_attesi=240, da='elena')
    check('giornata ridotta: attesi 240', an.minuti_attesi('op1', giovedi) == 240)
    svc.salva_giornata('op1', giovedi, [{'cliente': 'Cliente Y', 'minuti': 240}],
                       origine='ufficio', richiesta_id='r1')
    an.rivaluta_giornata('op1', giovedi)
    a = anomalia('op1', giovedi)
    check('ridotta compilata -> nessuna anomalia aperta',
          a is None or a['stato'] == 'risolta', a)

    r = an.salva_eccezione('op1', LUN, 'ridotta', minuti_attesi=None)
    check('ridotta senza minuti -> respinta', r.get('error'), r)

    # --- 7. Notifiche: una sola volta -------------------------------------
    print('\n7) Notifiche interne senza duplicati')
    an.controlla_periodo(LUN, DOM)
    prima = n_notifiche()
    inviate1 = an.notifica_anomalie()
    dopo1 = n_notifiche()
    check('notifiche inviate al primo giro', inviate1 > 0, inviate1)
    inviate2 = an.notifica_anomalie()
    dopo2 = n_notifiche()
    check('seconda esecuzione: nessuna notifica duplicata',
          inviate2 == 0 and dopo2 == dopo1, (inviate2, dopo1, dopo2))
    # Simula due riavvii consecutivi: il primo giro completo puo' trovare
    # anomalie NUOVE (finestra di recupero piu' ampia), il secondo no.
    an.controlla_e_notifica()
    dopo_riavvio1 = n_notifiche()
    an.controlla_e_notifica()
    dopo_riavvio2 = n_notifiche()
    check('riavvio ripetuto: nessuna notifica duplicata',
          dopo_riavvio2 == dopo_riavvio1, (dopo_riavvio1, dopo_riavvio2))
    check('destinatario e l impiegata', dopo1 > prima)

    # --- 8. Recupero arretrati --------------------------------------------
    print('\n8) Recupero dei controlli arretrati')
    # Oltre la finestra di recupero predefinita: davvero mai controllato
    vecchio = date.today() - timedelta(days=60)
    vecchio = vecchio - timedelta(days=vecchio.isoweekday() - 1)   # lunedi
    r = an.controlla_periodo(vecchio, vecchio + timedelta(days=4))
    check('controllo su periodo arretrato eseguito', not r.get('error'), r)
    check('anomalie arretrate rilevate', r.get('creata', 0) > 0, r)
    # Idempotenza: rieseguire lo STESSO periodo non crea nulla di nuovo
    tot = an.controlla_periodo(vecchio, vecchio + timedelta(days=4))
    check('rieseguire lo stesso periodo non duplica', tot.get('creata', 0) == 0, tot)

    # --- 9. Elenco per l'impiegata ----------------------------------------
    print('\n9) Elenco per l impiegata')
    aperte = an.elenco_anomalie(stato='aperta')
    check('elenco anomalie aperte disponibile', isinstance(aperte, list) and aperte)
    date_ordinate = [x['data'] for x in aperte]
    check('ordinate per data (piu recenti prima)',
          date_ordinate == sorted(date_ordinate, reverse=True))
    check('ogni anomalia ha operaio, tipo e attese',
          all(x.get('operatore') and x.get('tipo') and 'minuti_attesi' in x for x in aperte))

    cfg = an.elenco_configurazione()
    check('configurazione operai consultabile', len(cfg) >= 3, len(cfg))
    r = an.salva_configurazione('op3', True, 300, [1, 2, 3, 4, 5], da='elena')
    check('impiegata puo modificare la configurazione', r.get('success'), r)
    check('op3 ora tenuto con 300 min', an.minuti_attesi('op3', LUN) == 300)

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
