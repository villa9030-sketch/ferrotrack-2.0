"""Test della VERIFICA prima delle operazioni definitive (10.3 e 10.6).

Invio e accettazione partivano senza chiedersi se il documento fosse in ordine:
si scopriva dopo, dal cliente o in officina, che mancava il prezzo di un pezzo.

Copre:
 1. errori che FERMANO (cliente, righe, prezzo, totale) con il riferimento
 2. avvisi che NON fermano (data di consegna, geometria stimata, sconto)
 3. un preventivo completo risulta pronto
 4. l'invio viene rifiutato finche' ci sono errori
 5. anche l'accettazione lo e'
 6. non si dichiara "pronto" con errori aperti

Gira su DATABASE TEMPORANEO. Esecuzione:
    python app/tests/test_preventivi_verifica.py
"""
import json
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
from backend.models import Base, User, Preventivo  # noqa: E402
from backend import models_ore  # noqa: E402

_TMP = os.path.join(tempfile.gettempdir(), f'test_ver_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend.app import app  # noqa: E402
from backend.database import PreventivoManager, BarcodeManager  # noqa: E402
from backend.preventivi.verifica import verifica  # noqa: E402

_CFG = os.path.join(tempfile.gettempdir(), f'test_ver_cfg_{uuid.uuid4().hex[:8]}.json')
BarcodeManager._CONFIG_PATH = _CFG
with open(_CFG, 'w', encoding='utf-8') as _f:
    json.dump({'preventivi_config': {'costo_generali_pct': 0}}, _f)

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


def tipi(lista):
    return {x['tipo'] for x in lista}


def main():
    s = models.SessionLocal()
    try:
        s.add(User(id='commerciale', name='Marco', role='Commerciale', is_active=True))
        s.commit()
    finally:
        s.close()
    c = app.test_client()

    # =====================================================================
    print('\n1) Errori che fermano')
    vuoto = {'cliente': '', 'quantita': 1, 'margine_pct': 0,
             'articoli': [], 'assiemi': [], 'tubolari': [], 'piastre': []}
    e = verifica(vuoto)
    check('non pronto', e['pronto'] is False)
    check('segnala il cliente mancante', 'cliente_mancante' in tipi(e['errori']), e['errori'])
    check('segnala che non c e nulla da quotare',
          'nessuna_riga' in tipi(e['errori']), e['errori'])
    check('segnala il totale nullo', 'totale_nullo' in tipi(e['errori']))

    senza_prezzo = {'cliente': 'Alfa', 'quantita': 1, 'margine_pct': 0,
                    'articoli': [{'id': 'a1', 'codice': 'PZ-1', 'quantita': 1},
                                 {'id': 'a2', 'codice': 'PZ-2', 'quantita': 1,
                                  'costo_base_stimato': 50.0}],
                    'assiemi': [], 'tubolari': [], 'piastre': []}
    e = verifica(senza_prezzo)
    senza = [x for x in e['errori'] if x['tipo'] == 'pezzo_senza_prezzo']
    check('segnala il pezzo senza prezzo', len(senza) == 1, e['errori'])
    check('e dice QUALE pezzo', senza and senza[0]['riferimento']['codice'] == 'PZ-1',
          senza)
    check('col suo id, per poterci andare',
          senza and senza[0]['riferimento']['id'] == 'a1', senza)
    check('non si lamenta del pezzo prezzato',
          all(x.get('riferimento', {}).get('codice') != 'PZ-2' for x in e['errori']))

    print('\n   un pezzo dentro un assieme puo avere costo proprio zero')
    con_assieme = {'cliente': 'Alfa', 'quantita': 1, 'margine_pct': 0,
                   'articoli': [{'id': 'a1', 'codice': 'FIGLIO', 'quantita': 2,
                                 'codice_assieme': 'ASS-1'}],
                   'assiemi': [{'codice_assieme': 'ASS-1', 'qty': 1, 'costo': 100.0}],
                   'tubolari': [], 'piastre': []}
    e = verifica(con_assieme)
    check('nessun errore sul componente', 'pezzo_senza_prezzo' not in tipi(e['errori']),
          e['errori'])
    check('preventivo pronto', e['pronto'] is True, e['errori'])

    print('\n   dati impossibili')
    e = verifica({**senza_prezzo,
                  'articoli': [{'codice': 'PZ', 'quantita': 1,
                                'costo_base_stimato': -10}]})
    check('costo negativo bloccante', 'dato_non_valido' in tipi(e['errori']), e['errori'])

    # =====================================================================
    print('\n2) Avvisi che non fermano')
    completo = {'cliente': 'Alfa', 'quantita': 2, 'margine_pct': 20.0,
                'sconto_pct': 5.0,
                'articoli': [{'id': 'a1', 'codice': 'PZ-1', 'quantita': 1,
                              'costo_base_stimato': 100.0,
                              'dxf_filename': 'pz1.dxf',
                              'geometria_manuale_confermata': False,
                              'area_stimata_piega': True}],
                'assiemi': [], 'tubolari': [], 'piastre': []}
    e = verifica(completo)
    check('nonostante gli avvisi, e pronto', e['pronto'] is True, e['errori'])
    check('avvisa sulla data di consegna',
          'data_consegna_mancante' in tipi(e['avvisi']), e['avvisi'])
    check('avvisa sulla geometria non confermata',
          'geometria_non_confermata' in tipi(e['avvisi']))
    check('avvisa sull area stimata', 'area_stimata' in tipi(e['avvisi']))
    check('avvisa sullo sconto applicato', 'sconto_applicato' in tipi(e['avvisi']))

    # =====================================================================
    print('\n3) Il riepilogo dice cosa si sta per mandare')
    r = e['riepilogo']
    check('cliente', r['cliente'] == 'Alfa')
    check('quantita di lotto', r['quantita'] == 2)
    check('quanti pezzi', r['n_articoli'] == 1)
    check('totale definitivo', r['totale_lotto'] == e['totali']['totale_lotto'])
    check('e il totale tiene conto dello sconto',
          r['totale_lotto'] == 228.0, r['totale_lotto'])   # 100 x1,20 x2 = 240, -5%

    # =====================================================================
    print('\n4) L invio viene rifiutato finche ci sono errori')
    pid = PreventivoManager.create('', 'commerciale', quantita=1)
    pid = pid['id'] if isinstance(pid, dict) else pid
    r = c.post(f'/api/preventivi/{pid}/invia', json={'user_id': 'commerciale'})
    check('invio rifiutato (400)', r.status_code == 400, r.status_code)
    d = r.get_json()
    check('elenca gli errori', len(d.get('errori') or []) > 0, d)
    check('il messaggio e leggibile', 'risolvere' in (d.get('error') or ''), d)
    s = models.SessionLocal()
    try:
        stato = s.query(Preventivo).filter(Preventivo.id == pid).first().status
    finally:
        s.close()
    check('resta in BOZZA', stato == 'BOZZA', stato)

    print('\n   sistemando i dati, l invio passa')
    PreventivoManager.update(pid, {'cliente': 'Cliente Alfa'})
    PreventivoManager.replace_articoli(pid, [
        {'codice': 'PZ-1', 'quantita': 1, 'costo_base_stimato': 100.0}])
    r = c.get(f'/api/preventivi/{pid}/verifica')
    check('la verifica ora dice pronto', r.get_json().get('pronto') is True,
          r.get_json().get('errori'))
    r = c.post(f'/api/preventivi/{pid}/invia', json={'user_id': 'commerciale'})
    check('invio riuscito', r.status_code == 200, r.get_json())

    # =====================================================================
    print('\n5) Anche l accettazione controlla')
    pid2 = PreventivoManager.create('Cliente Beta', 'commerciale', quantita=1)
    pid2 = pid2['id'] if isinstance(pid2, dict) else pid2
    PreventivoManager.replace_articoli(pid2, [
        {'codice': 'SENZA-PREZZO', 'quantita': 1}])
    s = models.SessionLocal()
    try:
        p = s.query(Preventivo).filter(Preventivo.id == pid2).first()
        p.status = 'INVIATO'
        s.commit()
    finally:
        s.close()
    r = c.post(f'/api/preventivi/{pid2}/accetta', json={'user_id': 'commerciale'})
    check('accettazione rifiutata (400)', r.status_code == 400, r.status_code)
    check('spiega perche', 'risolvere' in (r.get_json().get('error') or ''),
          r.get_json())

    # =====================================================================
    print('\n6) Non si dichiara pronto con errori aperti')
    r = c.get(f'/api/preventivi/{pid2}/verifica')
    d = r.get_json()
    check('pronto = False', d['pronto'] is False)
    check('e gli errori sono elencati', len(d['errori']) > 0)
    r = c.get('/api/preventivi/non-esiste/verifica')
    check('preventivo inesistente -> 404', r.status_code == 404)

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
