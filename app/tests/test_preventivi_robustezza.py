"""Test di ROBUSTEZZA del preventivatore (sezioni 10.6, 10.7, 10.8).

Copre i rilievi dell'audit ancora aperti dopo il calcolo autorevole:
 1. validazione: costi negativi, numeri non finiti, quantita' assurde
 2. una bozza incompleta si puo' comunque salvare
 3. disegni omonimi: il secondo non cancella il primo
 4. prezzo congelato all'invio: cambiare la configurazione non lo muove
 5. l'accettazione rispetta il prezzo comunicato al cliente

Gira su DATABASE e FILE TEMPORANEI. Esecuzione:
    python app/tests/test_preventivi_robustezza.py
"""
import json
import os
import shutil
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

_TMP = os.path.join(tempfile.gettempdir(), f'test_rob_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend.database import PreventivoManager, BarcodeManager  # noqa: E402
from backend.preventivi.calcolo import calcola  # noqa: E402
from backend.app import _nome_disegno_libero, _preventivo_to_pdf_dati  # noqa: E402

_CFG = os.path.join(tempfile.gettempdir(), f'test_rob_cfg_{uuid.uuid4().hex[:8]}.json')
BarcodeManager._CONFIG_PATH = _CFG


def scrivi_config(generali_pct):
    with open(_CFG, 'w', encoding='utf-8') as f:
        json.dump({'preventivi_config': {'costo_generali_pct': generali_pct}}, f)


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


def nuovo_preventivo(cliente='Cliente Alfa', quantita=1):
    r = PreventivoManager.create(cliente, 'commerciale', quantita=quantita)
    return r['id'] if isinstance(r, dict) else r


def main():
    scrivi_config(0)
    s = models.SessionLocal()
    try:
        s.add(User(id='commerciale', name='Marco', role='Commerciale', is_active=True))
        s.commit()
    finally:
        s.close()

    # =====================================================================
    print('\n1) Validazione dei dati economici')
    pid = nuovo_preventivo()
    casi = [
        ('costo negativo', [{'codice': 'A', 'costo_piega': -50}]),
        ('costo non numerico', [{'codice': 'A', 'costo_saldatura': 'cinquanta'}]),
        ('costo fuori scala', [{'codice': 'A', 'costo_materiale': 9_000_000}]),
        ('quantita zero', [{'codice': 'A', 'quantita': 0}]),
        ('quantita negativa', [{'codice': 'A', 'quantita': -3}]),
        ('quantita assurda', [{'codice': 'A', 'quantita': 999999}]),
        ('override negativo', [{'codice': 'A', 'costo_base_override': -1}]),
    ]
    for nome, righe in casi:
        r = PreventivoManager.replace_articoli(pid, righe)
        check(f'respinto: {nome}', bool(r.get('error')), r)
    r = PreventivoManager.replace_articoli(pid, [{'codice': 'A', 'costo_piega': -50}])
    check('l errore dice quale riga e quale campo',
          'riga 1' in (r.get('error') or '') and 'costo_piega' in (r.get('error') or ''),
          r.get('error'))
    check('nessun articolo salvato dopo il rifiuto',
          len(PreventivoManager.get(pid).get('articoli') or []) == 0)

    print('\n   anche assiemi, tubolari e piastre')
    check('assieme con qty zero',
          bool(PreventivoManager.replace_assiemi(
              pid, [{'codice_assieme': 'A1', 'qty': 0}]).get('error')))
    check('tubolare con costo negativo',
          bool(PreventivoManager.replace_tubolari(
              pid, [{'profilo': 'T1', 'costo_materiale': -5}]).get('error')))
    check('piastra con costo non finito',
          bool(PreventivoManager.replace_piastre(
              pid, [{'costo': float('inf')}]).get('error')))

    # =====================================================================
    print('\n2) Una bozza incompleta si puo salvare')
    r = PreventivoManager.replace_articoli(pid, [
        {'codice': 'DA-PREZZARE'},                       # nessun costo
        {'codice': 'B', 'quantita': 2, 'costo_piega': 0},  # zeri espliciti
    ])
    check('salvataggio riuscito', r.get('success') is True, r)
    check('due articoli salvati',
          len(PreventivoManager.get(pid).get('articoli') or []) == 2)

    # =====================================================================
    print('\n3) Disegni omonimi non si sovrascrivono')
    cartella = os.path.join(tempfile.gettempdir(), 'test_disegni_' + uuid.uuid4().hex[:6])
    os.makedirs(cartella, exist_ok=True)
    try:
        n1 = _nome_disegno_libero(cartella, 'flangia.dxf')
        open(os.path.join(cartella, n1), 'w').write('primo')
        n2 = _nome_disegno_libero(cartella, 'flangia.dxf')
        open(os.path.join(cartella, n2), 'w').write('secondo')
        n3 = _nome_disegno_libero(cartella, 'flangia.dxf')
        open(os.path.join(cartella, n3), 'w').write('terzo')
        check('primo file col suo nome', n1 == 'flangia.dxf', n1)
        check('secondo rinominato', n2 == 'flangia (2).dxf', n2)
        check('terzo rinominato', n3 == 'flangia (3).dxf', n3)
        check('sono tre file distinti', len({n1, n2, n3}) == 3)
        check('il primo non e stato toccato',
              open(os.path.join(cartella, n1)).read() == 'primo')
        check('anche il percorso viene ripulito',
              _nome_disegno_libero(cartella, '../../fuga.dxf') == 'fuga.dxf')
    finally:
        shutil.rmtree(cartella, ignore_errors=True)

    # =====================================================================
    print('\n4) Il prezzo si congela all invio')
    pid2 = nuovo_preventivo(quantita=2)
    PreventivoManager.replace_articoli(pid2, [
        {'codice': 'PZ', 'quantita': 1, 'costo_base_stimato': 100.0}])
    PreventivoManager.update(pid2, {'margine_pct': 20.0})
    scrivi_config(10)                                   # generali al 10%
    prima = calcola(PreventivoManager.get(pid2), {'costo_generali_pct': 10})['totale_lotto']
    check('prima dell invio vale 264 (100 x1,10 x1,20 x2)', prima == 264.0, prima)

    PreventivoManager.transition_status(pid2, 'INVIATO', 'commerciale')
    p = PreventivoManager.get(pid2)
    snap = p.get('snapshot_economico')
    check('fotografia scattata', bool(snap), snap)
    check('registra le percentuali del momento',
          snap and snap['costo_generali_pct'] == 10 and snap['ricarico_pct'] == 20.0, snap)
    check('e il totale comunicato', snap and snap['totale_lotto'] == 264.0, snap)
    check('salvato anche sul preventivo', p['totale_lotto'] == 264.0, p['totale_lotto'])

    print('\n   ora si cambiano i costi generali in configurazione')
    scrivi_config(50)                                   # ritocco pesante
    dati_pdf = _preventivo_to_pdf_dati(PreventivoManager.get(pid2))
    check('il PDF dell offerta gia inviata NON cambia',
          round(dati_pdf['totale_lotto'], 2) == 264.0, dati_pdf['totale_lotto'])
    check('le righe continuano a sommare al totale',
          abs(sum(float(x.get('importo') or 0) for x in (dati_pdf.get('righe_cliente') or []))
              - dati_pdf['totale_lotto']) < 0.5, dati_pdf.get('righe_cliente'))

    print('\n   mentre una bozza nuova usa la configurazione aggiornata')
    pid3 = nuovo_preventivo(quantita=1)
    PreventivoManager.replace_articoli(pid3, [
        {'codice': 'PZ', 'quantita': 1, 'costo_base_stimato': 100.0}])
    PreventivoManager.update(pid3, {'margine_pct': 0.0})
    check('la bozza segue i nuovi generali (150)',
          round(_preventivo_to_pdf_dati(PreventivoManager.get(pid3))['totale_lotto'], 2) == 150.0,
          _preventivo_to_pdf_dati(PreventivoManager.get(pid3))['totale_lotto'])

    # =====================================================================
    print('\n5) L accettazione rispetta il prezzo comunicato')
    r = PreventivoManager.accetta_e_crea_ordine(pid2, 'commerciale')
    check('accettazione riuscita', r.get('success') is True, r)
    check('il preventivo resta a 264', PreventivoManager.get(pid2)['totale_lotto'] == 264.0,
          PreventivoManager.get(pid2)['totale_lotto'])
    s = models.SessionLocal()
    try:
        o = s.query(Order).filter(Order.preventivo_id_origine == pid2).first()
        prezzo = o.prezzo_quotato if o else None
    finally:
        s.close()
    check('e l ordine nasce con quel prezzo', prezzo == 264.0, prezzo)

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
