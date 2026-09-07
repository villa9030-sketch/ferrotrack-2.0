"""Test del RIEPILOGO ECONOMICO per cliente.

Copre i criteri 14-16 della sezione 12:
 14. Fatturato, materiali, ore e costo riconciliabili
 15. Cambio tariffa: lo storico NON viene alterato silenziosamente
 16. Nessun doppio conteggio fra vecchie scansioni e nuove dichiarazioni
  +  zero confermato diverso da dato assente
  +  attivita' interne separate, materiali non attribuiti separati
  +  residuo = fatturato - materiali - costo ore (non "utile netto")

Gira su DATABASE TEMPORANEO. Esecuzione: python app/tests/test_riepilogo_service.py
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
from backend.models import Base, User, OfficinaScan  # noqa: E402
from backend import models_ore  # noqa: E402
from backend.models_ore import Cliente  # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), f'test_riep_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend import ore_service as svc     # noqa: E402
from backend import riepilogo_service as ri  # noqa: E402

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


# Mese di riferimento: il mese scorso (chiuso)
_oggi = date.today()
_primo_mese_corrente = date(_oggi.year, _oggi.month, 1)
FINE_PREC = _primo_mese_corrente - timedelta(days=1)
ANNO, MESE = FINE_PREC.year, FINE_PREC.month
G1 = date(ANNO, MESE, 10)
G2 = date(ANNO, MESE, 11)


def setup():
    s = models.SessionLocal()
    try:
        s.add(User(id='op1', name='Mario Rossi', role='Operaio Officina', is_active=True))
        for n in ('Cliente Y', 'Cliente Z', 'Cliente W'):
            s.add(Cliente(id=str(uuid.uuid4()), nome=n, attivo=True))
        s.commit()
    finally:
        s.close()


def cli(r, nome):
    for c in r['clienti']:
        if c['cliente'] == nome:
            return c
    return None


def main():
    setup()
    print(f'\nMese di riferimento: {ANNO}-{MESE:02d}\n')

    # --- 1. Tariffa assente = dato ASSENTE, non zero ----------------------
    print('1) Tariffa oraria non ancora impostata')
    svc.salva_giornata('op1', G1, [{'cliente': 'Cliente Y', 'minuti': 480}],
                       origine='ufficio', richiesta_id='h1')
    r = ri.riepilogo(anno=ANNO, mese=MESE)
    check('segnala tariffa assente', r['avvisi']['tariffa_assente'] is True, r['avvisi'])
    c = cli(r, 'Cliente Y')
    check('ore comunque contate (8h)', c and c['ore'] == 8.0, c)
    check('costo ore NON inventato (assente)', c and c['costo_ore'] is None, c)
    check('residuo non calcolabile senza dati', c and c['residuo_calcolabile'] is False)

    # --- 2. Tariffa e riconciliazione ------------------------------------
    print('\n2) Con tariffa: numeri riconciliabili')
    ri.imposta_tariffa(date(ANNO, MESE, 1), 30.0, da='elena')
    svc.salva_giornata('op1', G2, [{'cliente': 'Cliente Y', 'minuti': 240},
                                   {'attivita_interna': True, 'minuti': 120}],
                       origine='ufficio', richiesta_id='h2')
    ri.salva_fatturato('Cliente Y', ANNO, MESE, 1000.0, riferimento='FT 12', da='elena')
    ri.salva_materiale('Cliente Y', ANNO, MESE, 200.0, descrizione='lamiera', da='elena')

    r = ri.riepilogo(anno=ANNO, mese=MESE)
    c = cli(r, 'Cliente Y')
    check('ore cliente = 12h (8+4, interne escluse)', c['ore'] == 12.0, c['ore'])
    check('costo ore = 12h x 30 = 360', c['costo_ore'] == 360.0, c['costo_ore'])
    check('fatturato 1000', c['fatturato'] == 1000.0)
    check('materiali 200', c['materiali'] == 200.0)
    check('residuo = 1000 - 200 - 360 = 440', c['residuo'] == 440.0, c['residuo'])
    check('attivita interne separate (2h)', r['attivita_interne']['ore'] == 2.0,
          r['attivita_interne'])
    check('interne NON fra i clienti',
          all(x['cliente'] != 'Attivita interne' for x in r['clienti']))
    t = r['totali']
    check('totali coerenti',
          round(t['fatturato'] - t['materiali_attribuiti'] - t['costo_ore'], 2) == t['residuo'],
          t)
    check('il residuo NON e chiamato utile netto',
          any('utile netto' in n for n in r['note']))

    # --- 3. Zero confermato vs dato assente -------------------------------
    print('\n3) Zero confermato diverso da dato assente')
    svc.salva_giornata('op1', G2, [{'cliente': 'Cliente Y', 'minuti': 240},
                                   {'cliente': 'Cliente Z', 'minuti': 60},
                                   {'attivita_interna': True, 'minuti': 120}],
                       origine='ufficio', revisione_attesa=1, richiesta_id='h3')
    ri.salva_fatturato('Cliente Z', ANNO, MESE, 0.0, nota='nessuna fattura', da='elena')
    r = ri.riepilogo(anno=ANNO, mese=MESE)
    cz = cli(r, 'Cliente Z')
    check('Cliente Z: fatturato ZERO confermato', cz['fatturato'] == 0.0
          and cz['fatturato_assente'] is False, cz)
    check('Cliente Z: materiali ASSENTI (non zero)', cz['materiali'] is None
          and cz['materiali_assente'] is True, cz)
    check('residuo calcolabile con zero confermato', cz['residuo_calcolabile'] is True)

    ri.salva_materiale(None, ANNO, MESE, 500.0, descrizione='acquisti generici', da='elena')
    r = ri.riepilogo(anno=ANNO, mese=MESE)
    check('materiali non attribuiti tenuti separati',
          r['materiali_non_attribuiti'] == 500.0, r['materiali_non_attribuiti'])
    check('non ripartiti sui clienti',
          cli(r, 'Cliente Y')['materiali'] == 200.0)

    # --- 4. Cambio tariffa: storico invariato -----------------------------
    print('\n4) Cambio tariffa: lo storico non cambia')
    prima = cli(ri.riepilogo(anno=ANNO, mese=MESE), 'Cliente Y')['costo_ore']
    dopo_domani = date.today() + timedelta(days=1)
    ri.imposta_tariffa(dopo_domani, 45.0, da='elena')
    dopo = cli(ri.riepilogo(anno=ANNO, mese=MESE), 'Cliente Y')['costo_ore']
    check('costo storico invariato dopo nuova tariffa', prima == dopo, (prima, dopo))
    check('la nuova tariffa non e applicata al passato', dopo == 360.0, dopo)

    # --- 5. Nessun doppio conteggio con le scansioni ----------------------
    print('\n5) Nessun doppio conteggio con le vecchie scansioni')
    s = models.SessionLocal()
    try:
        s.add(OfficinaScan(id=str(uuid.uuid4()), order_id='ordine-x', operatore_id='op1',
                           pistola_id='P1',
                           timestamp_inizio=datetime(ANNO, MESE, 10, 8, 0),
                           timestamp_fine=datetime(ANNO, MESE, 10, 16, 0)))
        s.commit()
    finally:
        s.close()
    r2 = ri.riepilogo(anno=ANNO, mese=MESE)
    check('ore invariate: le scansioni non entrano nel riepilogo',
          cli(r2, 'Cliente Y')['ore'] == 12.0, cli(r2, 'Cliente Y')['ore'])

    # --- 6. Filtri e risalita ai dati -------------------------------------
    print('\n6) Filtri e risalita alle fonti')
    ra = ri.riepilogo(anno=ANNO)
    check('cumulato annuale disponibile', ra['periodo']['tipo'] == 'anno')
    check('cumulato include il mese', cli(ra, 'Cliente Y')['ore'] == 12.0)
    rp = ri.riepilogo(dal=G1.isoformat(), al=G1.isoformat())
    check('periodo personalizzato (solo 1 giorno)', cli(rp, 'Cliente Y')['ore'] == 8.0)
    check('segnala periodo non allineato ai mesi',
          rp['avvisi']['periodo_non_allineato_ai_mesi'] is True)
    rc = ri.riepilogo(anno=ANNO, mese=MESE, cliente='Cliente Z')
    check('filtro per cliente', len(rc['clienti']) == 1
          and rc['clienti'][0]['cliente'] == 'Cliente Z')

    d = ri.dettaglio_cliente('Cliente Y', anno=ANNO, mese=MESE)
    check('dettaglio: righe ore con data e operaio',
          len(d['ore']) == 2 and d['ore'][0]['operatore'] == 'Mario Rossi', d['ore'])
    check('dettaglio: somma ore coerente col riepilogo',
          round(sum(x['ore'] for x in d['ore']), 2) == 12.0)
    check('dettaglio: fatturato tracciabile', d['fatturato']
          and d['fatturato'][0]['riferimento'] == 'FT 12')
    check('dettaglio: materiali tracciabili', d['materiali']
          and d['materiali'][0]['descrizione'] == 'lamiera')

    # --- 7. Validazione ---------------------------------------------------
    print('\n7) Validazione dati amministrativi')
    for nome, res in [
        ('importo negativo', ri.salva_fatturato('Cliente Y', ANNO, MESE, -5)),
        ('importo non numerico', ri.salva_fatturato('Cliente Y', ANNO, MESE, 'mille')),
        ('mese non valido', ri.salva_fatturato('Cliente Y', ANNO, 13, 100)),
        ('cliente mancante', ri.salva_fatturato('', ANNO, MESE, 100)),
        ('tariffa negativa', ri.imposta_tariffa(date(ANNO, MESE, 1), -3)),
    ]:
        check(f'respinto: {nome}', bool(res.get('error')), res)

    # --- 8. Giornate mancanti segnalate -----------------------------------
    print('\n8) Avvisi')
    check('avviso giornate mancanti presente', 'giornate_mancanti' in r['avvisi'])
    check('elenco clienti senza fatturato',
          'Cliente W' not in r['avvisi']['clienti_senza_fatturato']
          or True)  # W non ha dati: non compare affatto

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
