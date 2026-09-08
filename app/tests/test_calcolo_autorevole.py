"""Test del CALCOLO AUTOREVOLE del prezzo (sezione 10.5).

Il prezzo veniva deciso dal browser e salvato senza verifica. Peggio: le due
formule nel JavaScript non coincidevano. Su un preventivo con assiemi il
prezzo ACCETTATO — quello che finisce sull'ordine — era piu' basso di quello
mostrato al cliente, perche' l'accettazione ignorava assiemi, tubolari,
piastre e costi generali, e contava due volte gli articoli gia' dentro un
assieme.

Copre:
 1. articoli sciolti: costo, ricarico, quantita' di lotto
 2. assiemi: componenti contati UNA volta sola
 3. costi generali applicati prima del ricarico
 4. la verifica segnala uno scostamento invece di subirlo
 5. l'accettazione salva il totale del SERVER, non quello del browser
 6. il prezzo che arriva sull'ordine e' quello autorevole

Gira su DATABASE TEMPORANEO. Esecuzione:
    python app/tests/test_calcolo_autorevole.py
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

_TMP = os.path.join(tempfile.gettempdir(), f'test_calc_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend.database import PreventivoManager, BarcodeManager  # noqa: E402
from backend.preventivi.calcolo import calcola, verifica  # noqa: E402

# Configurazione isolata: il test non deve dipendere dai costi generali
# impostati sulla macchina di chi lo esegue.
_CFG = os.path.join(tempfile.gettempdir(), f'test_calc_cfg_{uuid.uuid4().hex[:8]}.json')
BarcodeManager._CONFIG_PATH = _CFG
with open(_CFG, 'w', encoding='utf-8') as _f:
    _f.write('{"preventivi_config": {"costo_generali_pct": 0}}')

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


def art(codice, base, lav=0.0, qta=1, assieme=None):
    return {'codice': codice, 'quantita': qta, 'costo_base_stimato': base,
            'costo_piega': lav, 'costo_saldatura': 0.0, 'costo_filettatura': 0.0,
            'costo_svasatura': 0.0, 'costo_apporto': 0.0, 'costo_pulizia': 0.0,
            'codice_assieme': assieme}


def main():
    s = models.SessionLocal()
    try:
        s.add(User(id='commerciale', name='Marco', role='Commerciale', is_active=True))
        s.commit()
    finally:
        s.close()

    # =====================================================================
    print('\n1) Articoli sciolti')
    p = {'quantita': 10, 'margine_pct': 20.0, 'sconto_pct': 0.0,
         'articoli': [art('A', 100.0, 10.0, qta=2), art('B', 50.0, 0.0, qta=1)],
         'assiemi': [], 'tubolari': [], 'piastre': []}
    r = calcola(p)
    check('costo pezzo = 110x2 + 50 = 270', r['costo_pezzo'] == 270.0, r['costo_pezzo'])
    check('con ricarico 20% = 324', r['totale_pezzo_con_margine'] == 324.0,
          r['totale_pezzo_con_margine'])
    check('lotto da 10 = 3240', r['totale_lotto'] == 3240.0, r['totale_lotto'])

    print('\n   sconto')
    r2 = calcola({**p, 'sconto_pct': 10.0})
    check('sconto 10% -> 2916', r2['totale_lotto'] == 2916.0, r2['totale_lotto'])

    # =====================================================================
    print('\n2) Assiemi: i componenti si contano una volta sola')
    p = {'quantita': 1, 'margine_pct': 0.0, 'sconto_pct': 0.0,
         'articoli': [art('FIGLIO', 100.0, qta=2, assieme='ASS-1'),
                      art('SCIOLTO', 30.0, qta=1)],
         'assiemi': [{'codice_assieme': 'ASS-1', 'qty': 3, 'costo': 20.0,
                      'costo_puntatura': 5.0, 'costo_saldatura_assieme': 5.0}],
         'tubolari': [{'codice_assieme': 'ASS-1', 'costo_materiale': 40.0,
                       'costo_taglio_totale': 10.0},
                      {'codice_assieme': None, 'costo_materiale': 15.0,
                       'costo_taglio_totale': 5.0}],
         'piastre': [{'codice_assieme': 'ASS-1', 'costo': 25.0}]}
    r = calcola(p)
    d = r['dettaglio_assiemi'][0]
    check('componenti dell assieme: 100x2 = 200', d['componenti'] == 200.0, d)
    check('accessori dell assieme: 50 + 25 = 75', d['accessori'] == 75.0, d)
    check('intrinseco: 20+5+5 = 30', d['intrinseco'] == 30.0, d)
    check('un assieme costa 305', d['costo_singolo'] == 305.0, d)
    check('per 3 assiemi = 915', d['costo_totale'] == 915.0, d)
    check('l articolo dentro l assieme NON e contato fra i sciolti',
          r['costo_pezzo'] == 30.0, r['costo_pezzo'])
    check('lo dichiara: 1 articolo in assieme', r['n_articoli_in_assieme'] == 1, r)
    check('tubolare sciolto contato a parte (20)', r['costo_tubolari'] == 20.0,
          r['costo_tubolari'])
    check('piastra dell assieme NON ricontata fra le sciolte',
          r['costo_piastre'] == 0.0, r['costo_piastre'])
    check('totale = 30 + 915 + 20 = 965', r['totale_lotto'] == 965.0, r['totale_lotto'])

    print('\n   confronto con la vecchia formula dell accettazione')
    # La vecchia formula: TUTTI gli articoli, niente assiemi/tubolari/piastre
    vecchio = sum((a['costo_base_stimato'] + a['costo_piega']) * a['quantita']
                  for a in p['articoli'])
    check('la vecchia formula dava 230 invece di 965', vecchio == 230.0, vecchio)
    check('cioe si perdevano 735 euro su questo preventivo',
          round(r['totale_lotto'] - vecchio, 2) == 735.0)

    # =====================================================================
    print('\n3) Costi generali prima del ricarico')
    p3 = {'quantita': 1, 'margine_pct': 20.0, 'sconto_pct': 0.0,
          'articoli': [art('A', 100.0)], 'assiemi': [], 'tubolari': [], 'piastre': []}
    r = calcola(p3, {'costo_generali_pct': 10.0})
    check('100 -> +10% generali -> +20% ricarico = 132',
          r['totale_lotto'] == 132.0, r['totale_lotto'])
    check('non 130 (ricarico prima dei generali)', r['totale_lotto'] != 130.0)
    check('i generali sono dichiarati', r['costi_generali_pct'] == 10.0)

    # =====================================================================
    print('\n4) La verifica segnala lo scostamento')
    e = verifica(p3, {'totale_lotto': 120.0}, {'costo_generali_pct': 10.0})
    check('divergenza rilevata', e['coerente'] is False, e)
    check('dice ricevuto e calcolato',
          e['scostamenti']['totale_lotto']['ricevuto'] == 120.0
          and e['scostamenti']['totale_lotto']['calcolato'] == 132.0, e)
    e2 = verifica(p3, {'totale_lotto': 132.01}, {'costo_generali_pct': 10.0})
    check('un centesimo non e una divergenza', e2['coerente'] is True, e2)
    e3 = verifica(p3, {}, {'costo_generali_pct': 10.0})
    check('senza totali del client nessun allarme', e3['coerente'] is True)
    check('ma i totali autorevoli ci sono comunque',
          e3['totali']['totale_lotto'] == 132.0)

    # =====================================================================
    print('\n5) L accettazione salva il totale del SERVER')
    pid = PreventivoManager.create('Cliente Alfa', 'commerciale', quantita=1)
    pid = pid['id'] if isinstance(pid, dict) else pid
    PreventivoManager.replace_articoli(pid, [
        {'codice': 'FIGLIO', 'quantita': 2, 'costo_base_stimato': 100.0,
         'codice_assieme': 'ASS-1'},
        {'codice': 'SCIOLTO', 'quantita': 1, 'costo_base_stimato': 30.0},
    ])
    PreventivoManager.replace_assiemi(pid, [
        {'codice_assieme': 'ASS-1', 'qty': 3, 'costo': 20.0,
         'costo_puntatura': 5.0, 'costo_saldatura_assieme': 5.0}])
    PreventivoManager.replace_tubolari(pid, [
        {'codice_assieme': 'ASS-1', 'costo_materiale': 40.0, 'costo_taglio_totale': 10.0},
        {'costo_materiale': 15.0, 'costo_taglio_totale': 5.0}])
    PreventivoManager.replace_piastre(pid, [{'codice_assieme': 'ASS-1', 'costo': 25.0}])

    s = models.SessionLocal()
    try:
        pr = s.query(Preventivo).filter(Preventivo.id == pid).first()
        pr.status = 'INVIATO'
        s.commit()
    finally:
        s.close()

    atteso = calcola(PreventivoManager.get(pid))['totale_lotto']
    check('il server calcola 965 su questo preventivo', atteso == 965.0, atteso)

    # Il browser manda il numero sbagliato della vecchia formula
    r = PreventivoManager.accetta_e_crea_ordine(
        pid, 'commerciale', totali={'totale_pezzo': 230.0,
                                    'totale_pezzo_con_margine': 230.0,
                                    'totale_lotto': 230.0})
    check('accettazione riuscita', r.get('success') is True, r)
    salvato = PreventivoManager.get(pid)
    check('salvato il totale del server (965), non quello del browser (230)',
          salvato['totale_lotto'] == 965.0, salvato['totale_lotto'])

    # =====================================================================
    print('\n6) Il prezzo sull ordine e quello autorevole')
    s = models.SessionLocal()
    try:
        o = s.query(Order).filter(Order.preventivo_id_origine == pid).first()
        prezzo = o.prezzo_quotato if o else None
    finally:
        s.close()
    check('ordine creato col prezzo giusto', prezzo == 965.0, prezzo)

    # =====================================================================
    print('\n7) Editor, PDF e accettazione danno lo stesso numero')
    from backend.app import _preventivo_to_pdf_dati
    completo = PreventivoManager.get(pid)
    dati_pdf = _preventivo_to_pdf_dati(completo)
    autorevole = calcola(completo)['totale_lotto']
    check('il PDF riporta il totale autorevole',
          round(dati_pdf['totale_lotto'], 2) == round(autorevole, 2),
          (dati_pdf['totale_lotto'], autorevole))
    check('e coincide con quello salvato all accettazione',
          round(dati_pdf['totale_lotto'], 2) == round(completo['totale_lotto'], 2),
          (dati_pdf['totale_lotto'], completo['totale_lotto']))
    righe_pdf = dati_pdf.get('righe_cliente') or []
    somma = round(sum(float(x.get('importo') or 0) for x in righe_pdf), 2)
    check('le righe del PDF sommano al totale',
          abs(somma - dati_pdf['totale_lotto']) < 0.5,
          (somma, dati_pdf['totale_lotto']))

    # =====================================================================
    print('\n8) Un calcolo a zero non cancella un prezzo salvato')
    pid2 = PreventivoManager.create('Cliente Beta', 'commerciale', quantita=1)
    pid2 = pid2['id'] if isinstance(pid2, dict) else pid2
    s = models.SessionLocal()
    try:
        pr = s.query(Preventivo).filter(Preventivo.id == pid2).first()
        pr.status = 'INVIATO'
        pr.totale_lotto = 1500.0       # prezzo salvato, ma nessuna riga articolo
        s.commit()
    finally:
        s.close()
    r8 = PreventivoManager.accetta_e_crea_ordine(pid2, 'commerciale')
    check('accettazione riuscita', r8.get('success') is True, r8)
    check('il prezzo salvato NON viene azzerato',
          PreventivoManager.get(pid2)['totale_lotto'] == 1500.0,
          PreventivoManager.get(pid2)['totale_lotto'])
    s = models.SessionLocal()
    try:
        o = s.query(Order).filter(Order.preventivo_id_origine == pid2).first()
        pz = o.prezzo_quotato if o else None
    finally:
        s.close()
    check('e arriva sull ordine', pz == 1500.0, pz)

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
        os.remove(_CFG)
    except Exception:
        pass
    sys.exit(code)
