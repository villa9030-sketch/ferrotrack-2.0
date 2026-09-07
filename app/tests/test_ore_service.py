"""Test del servizio DICHIARAZIONI ORE.

Copre i criteri di accettazione della sezione 12 (1-6) piu' le regole di dominio:
  1. 5 ore cliente Y + 3 ore cliente Z
  2. Riapertura: stessi dati e totale corretto
  3. Correzione: sostituzione, non duplicazione
  4. Doppio invio e retry dopo timeout: nessun doppio conteggio
  6. Input negativi / non finiti / invalidi respinti dal backend
  +  giornata "dichiarata a zero" != giornata "mancante"
  +  concorrenza: nessuna sovrascrittura silenziosa
  +  dal tablet si compila solo oggi

Gira su un DATABASE TEMPORANEO: non tocca mai il database di lavoro.
Esecuzione:  python app/tests/test_ore_service.py
"""
import os
import sys
import tempfile
import uuid

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User  # noqa: E402
from backend import models_ore  # noqa: E402  (registra le tabelle)
from backend.models_ore import Cliente  # noqa: E402

# --- DB temporaneo isolato ---------------------------------------------------
_TMP = os.path.join(tempfile.gettempdir(), f'test_ore_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend import ore_service as svc  # noqa: E402  (dopo il rebind)

OK = 0
KO = []


def check(nome, condizione, dettaglio=''):
    global OK
    if condizione:
        OK += 1
        print(f'  [OK] {nome}')
    else:
        KO.append(nome)
        print(f'  [KO] {nome} {dettaglio}')


def setup():
    s = models.SessionLocal()
    try:
        s.add(User(id='op1', name='Mario Rossi', role='Operaio Officina',
                   initials='MR', is_active=True))
        s.add(User(id='op2', name='Luca Bianchi', role='Operaio Laser',
                   initials='LB', is_active=True))
        for n in ('Cliente Y', 'Cliente Z', 'Cliente W'):
            s.add(Cliente(id=str(uuid.uuid4()), nome=n, attivo=True))
        s.commit()
    finally:
        s.close()


def main():
    setup()
    oggi = svc.oggi_locale()
    print(f'\nData locale (Europe/Rome): {oggi}\n')

    # --- 1. dichiarazione base -------------------------------------------
    print('1) Dichiarazione 5h Cliente Y + 3h Cliente Z')
    r = svc.salva_giornata('op1', oggi, [
        {'cliente': 'Cliente Y', 'minuti': 300},
        {'cliente': 'Cliente Z', 'minuti': 180},
    ], origine='tablet', device_label='Tablet 1', richiesta_id='req-1')
    check('salvataggio riuscito', r.get('success'), r.get('error', ''))
    g = r.get('giornata', {})
    check('totale = 480 minuti (8h)', g.get('totale_minuti') == 480, g.get('totale_minuti'))
    check('due righe', len(g.get('righe', [])) == 2)
    check('revisione = 1', g.get('revisione') == 1)

    # --- 2. riapertura ----------------------------------------------------
    print('\n2) Riapertura della giornata')
    letta = svc.leggi_giornata('op1', oggi)
    check('risulta dichiarata', letta.get('dichiarata') is True)
    check('totale invariato 480', letta.get('totale_minuti') == 480)
    y = [x for x in letta['righe'] if x['cliente'] == 'Cliente Y']
    check('Cliente Y = 300 min', y and y[0]['minuti'] == 300)

    # --- 3. correzione: sostituisce, non duplica --------------------------
    print('\n3) Correzione (Y 5h -> 6h, Z resta 3h)')
    r = svc.salva_giornata('op1', oggi, [
        {'cliente': 'Cliente Y', 'minuti': 360},
        {'cliente': 'Cliente Z', 'minuti': 180},
    ], origine='tablet', revisione_attesa=1, richiesta_id='req-2')
    check('correzione riuscita', r.get('success'), r.get('error', ''))
    g = r.get('giornata', {})
    check('totale 540 (non 1020: nessuna duplicazione)', g.get('totale_minuti') == 540,
          g.get('totale_minuti'))
    check('sempre due righe', len(g.get('righe', [])) == 2, len(g.get('righe', [])))
    check('revisione avanzata a 2', g.get('revisione') == 2)

    # --- 4. idempotenza: doppio invio / retry dopo timeout ----------------
    print('\n4) Doppio invio e retry con la STESSA richiesta')
    r2 = svc.salva_giornata('op1', oggi, [
        {'cliente': 'Cliente Y', 'minuti': 360},
        {'cliente': 'Cliente Z', 'minuti': 180},
    ], origine='tablet', revisione_attesa=1, richiesta_id='req-2')
    check('replay accettato (idempotente)', r2.get('success'), r2.get('error', ''))
    check('contrassegnato idempotente', r2.get('giornata', {}).get('idempotente') is True)
    check('totale invariato 540', r2.get('giornata', {}).get('totale_minuti') == 540)
    check('revisione NON avanzata (resta 2)', r2.get('giornata', {}).get('revisione') == 2)

    # --- concorrenza: revisione superata ---------------------------------
    print('\n5) Concorrenza: salvataggio con revisione vecchia')
    r3 = svc.salva_giornata('op1', oggi, [{'cliente': 'Cliente W', 'minuti': 60}],
                            origine='ufficio', revisione_attesa=1, richiesta_id='req-3')
    check('rifiutato con conflitto', r3.get('codice') == 'conflitto', r3)
    check('restituisce lo stato corrente', r3.get('giornata', {}).get('totale_minuti') == 540)
    dopo = svc.leggi_giornata('op1', oggi)
    check('dati NON sovrascritti', dopo.get('totale_minuti') == 540)

    # --- 6. validazione input --------------------------------------------
    print('\n6) Input invalidi respinti dal backend')
    casi = [
        ('minuti negativi', [{'cliente': 'Cliente Y', 'minuti': -60}]),
        ('minuti non numerici', [{'cliente': 'Cliente Y', 'minuti': 'abc'}]),
        ('minuti infiniti', [{'cliente': 'Cliente Y', 'minuti': float('inf')}]),
        ('minuti NaN', [{'cliente': 'Cliente Y', 'minuti': float('nan')}]),
        ('oltre 24h', [{'cliente': 'Cliente Y', 'minuti': 1500}]),
        ('cliente sconosciuto', [{'cliente': 'Fantasma SpA', 'minuti': 60}]),
        ('cliente mancante', [{'minuti': 60}]),
    ]
    for nome, righe in casi:
        rr = svc.salva_giornata('op2', oggi, righe, origine='tablet',
                                richiesta_id=f'bad-{nome}')
        check(f'respinto: {nome}', not rr.get('success') and rr.get('error'), rr)

    somma = svc.salva_giornata('op2', oggi, [
        {'cliente': 'Cliente Y', 'minuti': 800},
        {'cliente': 'Cliente Z', 'minuti': 800},
    ], origine='tablet', richiesta_id='bad-somma')
    check('respinto: totale giornaliero > 24h', not somma.get('success'), somma)

    check('operatore inesistente respinto',
          not svc.salva_giornata('nessuno', oggi, [], origine='ufficio').get('success'))

    # --- dichiarata a zero != mancante -----------------------------------
    print('\n7) Giornata dichiarata a ZERO diversa da giornata MANCANTE')
    mancante = svc.leggi_giornata('op2', oggi)
    check('op2 non ha ancora dichiarato', mancante.get('dichiarata') is False)
    rz = svc.salva_giornata('op2', oggi, [], origine='tablet', richiesta_id='zero-1')
    check('dichiarazione a zero accettata', rz.get('success'), rz.get('error', ''))
    zero = svc.leggi_giornata('op2', oggi)
    check('ora risulta DICHIARATA', zero.get('dichiarata') is True)
    check('con totale 0', zero.get('totale_minuti') == 0)

    # --- regole di dominio -----------------------------------------------
    print('\n8) Regole di dominio')
    import datetime as _dt
    ieri = oggi - _dt.timedelta(days=1)
    rt = svc.salva_giornata('op1', ieri, [{'cliente': 'Cliente Y', 'minuti': 60}],
                            origine='tablet', richiesta_id='ieri-tablet')
    check('tablet NON puo\' compilare giorni passati',
          rt.get('codice') == 'giorno_non_corrente', rt)
    ru = svc.salva_giornata('op1', ieri, [{'cliente': 'Cliente Y', 'minuti': 60}],
                            origine='ufficio', modificata_da='elena', richiesta_id='ieri-uff')
    check('ufficio PUO\' correggere giorni passati', ru.get('success'), ru.get('error', ''))
    rf = svc.salva_giornata('op1', oggi + _dt.timedelta(days=1), [],
                            origine='ufficio', richiesta_id='futuro')
    check('data futura respinta', rf.get('codice') == 'data_futura', rf)

    # --- attivita' interne separate --------------------------------------
    print('\n9) Attivita interne separate dai clienti')
    ri = svc.salva_giornata('op2', oggi, [
        {'cliente': 'Cliente Y', 'minuti': 240},
        {'attivita_interna': True, 'minuti': 120},
    ], origine='tablet', revisione_attesa=1, richiesta_id='interne-1')
    check('salvataggio con attivita interne', ri.get('success'), ri.get('error', ''))
    per_cliente = svc.minuti_per_cliente(oggi, oggi)
    check('Cliente Y aggregato correttamente', per_cliente.get('Cliente Y') == 240 + 360,
          per_cliente)
    check('attivita interne NON attribuite a clienti', per_cliente.get(None) == 120,
          per_cliente)

    # --- somme esatte (nessun float) -------------------------------------
    print('\n10) Somme esatte in minuti interi')
    tot = svc.leggi_giornata('op2', oggi)['totale_minuti']
    check('totale intero esatto 360', tot == 360 and isinstance(tot, int), tot)

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
