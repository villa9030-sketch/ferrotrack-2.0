"""«Manda la differenza» (28/10/2026, punto 3 della proposta Lantek del 27/10).

 1. la quadratura segna cosa manca in Lantek (riga['manda']):
    DECA: in Lantek meno dell'ordine -> la differenza; in piu' -> niente (a mano);
    FerroTrack diverso dal cliente -> niente; non in Lantek ma nell'archivio ->
    tutto; non nell'archivio -> niente;
    Poliform: abbinati, meno in Lantek -> differenza; mai mandato -> tutto;
    messi a mano dal cliente (senza numero d'ordine) -> niente, non si sa;
 2. lettura di Lantek: ordini col numero + quelli del cliente, senza doppioni;
 3. le rotte: cosa si manderebbe, invio solo se e' quello visto (409 se no),
    Lantek non leggibile 503, registro degli invii, da correggere fuori,
    senza accesso niente.

Lantek e' FINTO; database TEMPORANEO:
    python app/tests/test_differenza_lantek.py
"""
import os
import sys
import tempfile
import uuid

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

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
from backend.models import Base  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_diff_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend import quadratura as Q  # noqa: E402
from backend import lantek as L  # noqa: E402
from backend import lantek_correzioni as LC  # noqa: E402
from backend import lantek_invii as LI  # noqa: E402
import backend.app  # noqa: E402,F401
A = sys.modules["backend.app"]

OK = KO = 0


def check(nome, cond, extra=None):
    global OK, KO
    if cond:
        OK += 1
        print(f'  OK  {nome}')
    else:
        KO += 1
        print(f'  KO  {nome} :: {extra!r}'[:600])


def art(codice, qta):
    return {'id': codice, 'codice': codice, 'quantita': qta, 'codice_assieme': None, 'materiale': 'S235',
            'spessore_mm': 3, 'dxf_filename': codice + '.dxf', 'cleaned_status': 'auto',
            'esito_verifica': {'stato': 'verificato'}}


def lt(**pezzi):
    out = {}
    for c, n in pezzi.items():
        c = c.replace('_', '-')
        out[c] = {'codice': c, 'pianificati': float(n), 'fatti': 0.0, 'stati': {'creato': float(n)}}
    return out


def deca(righe_pdf, articoli, Lt, archivio=None):
    arch = archivio if archivio is not None else {a['codice']: {'esiste': True, 'codice_lantek': a['codice']}
                                                  for a in articoli}
    return {
        'ordine': {'id': 'o1', 'numero': 'A 009999', 'cliente': 'Prova', 'stato_laser': 'in_lantek', 'commessa': '9999'},
        'pdf': {'righe': [{'codice': c, 'quantita': n, 'descrizione': ''} for c, n in righe_pdf], 'formato': 'prova', 'note': []},
        'prev': {'quantita': 1, 'articoli': articoli, 'assiemi': [], 'tubolari': []},
        'procura': {}, 'invii': {},
        'files': {'caricati': {(a['codice'] + '.dxf').lower(): a['codice'] + '.dxf' for a in articoli},
                  'pronti': {Q.k(a['codice']): ['LANTEK/x.dxf'] for a in articoli}, 'da_preparare': {}},
        'lantek': {'ok': True, 'errore': None, 'L': Lt, 'archivio': {'disponibile': True, 'pezzi': arch}},
        'pacchetto': Q.Pacchetto(),
    }


def riga(r, codice):
    return next((x for x in r['righe'] if str(x['codice']).upper() == codice.upper()), None)


def parte_1():
    print('1) La quadratura segna cosa manca')
    d = deca([('P1', 6), ('P2', 3), ('P3', 2), ('P4', 4), ('P5', 1)],
             [art('P1', 6), art('P2', 3), art('P3', 1), art('P4', 4), art('P5', 1)],
             lt(P1=4, P2=5, P3=1),
             archivio={'P1': {'esiste': True, 'codice_lantek': 'P1'}, 'P2': {'esiste': True, 'codice_lantek': 'P2'},
                       'P3': {'esiste': True, 'codice_lantek': 'P3'}, 'P4': {'esiste': True, 'codice_lantek': 'P4-00'},
                       'P5': {'esiste': False}})
    r = Q.confronta(d)
    check('P1: 6 ordinati, 4 in Lantek -> manda +2', (riga(r, 'P1') or {}).get('manda') == {'codice': 'P1', 'quantita': 2},
          riga(r, 'P1'))
    p2 = riga(r, 'P2') or {}
    check('P2: in piu\' in Lantek -> niente da mandare, si corregge a mano',
          not p2.get('manda') and any('a 3' in p['cosa'] for p in p2.get('problemi') or []), p2)
    check('P3: FerroTrack 1 ma ordinati 2 -> niente (prima FerroTrack)', not (riga(r, 'P3') or {}).get('manda'), riga(r, 'P3'))
    check('P4: non in Lantek, nell\'archivio come P4-00 -> manda 4 col codice di Lantek',
          (riga(r, 'P4') or {}).get('manda') == {'codice': 'P4-00', 'quantita': 4}, riga(r, 'P4'))
    check('P5: non nell\'archivio di Lantek -> niente', not (riga(r, 'P5') or {}).get('manda'), riga(r, 'P5'))

    # Poliform (solo PDF): 05H1 = 2 x PEZZO-A, 05H2 = PEZZO-B, 05H3 = PEZZO-C
    Lp = lt(PEZZO_A=15, PEZZO_C=3)
    Lp['PEZZO-C']['da_cliente'] = True
    d = deca([('05H1', 10), ('05H2', 5), ('05H3', 10)], [], Lp)
    d['prev'] = None
    d['lantek']['per_cliente'] = True
    d['solo_pdf'] = {'abb': {'05H1': {'non_laser': False, 'pezzi': [{'codice': 'PEZZO-A', 'quantita': 2}]},
                             '05H2': {'non_laser': False, 'pezzi': [{'codice': 'PEZZO-B', 'quantita': 1}]},
                             '05H3': {'non_laser': False, 'pezzi': [{'codice': 'PEZZO-C', 'quantita': 1}]}}}
    r = Q.confronta(d)
    check('Poliform PEZZO-A: 20 servono, 15 in Lantek -> +5', (riga(r, 'PEZZO-A') or {}).get('manda') == {'codice': 'PEZZO-A', 'quantita': 5},
          riga(r, 'PEZZO-A'))
    check('Poliform PEZZO-B: mai mandato -> 5', (riga(r, 'PEZZO-B') or {}).get('manda') == {'codice': 'PEZZO-B', 'quantita': 5},
          riga(r, 'PEZZO-B'))
    pc = riga(r, 'PEZZO-C') or {}
    check('Poliform PEZZO-C messo a mano (del cliente): niente differenza, nessun problema',
          not pc.get('manda') and not pc.get('problemi'), pc)


def parte_2():
    print('\n2) Lettura di Lantek: col numero d\'ordine + del cliente')
    vecchi = (L._config, L._connessione, L.pezzi_in_lantek, Q._lantek_per_commessa, Q._lantek_per_cliente)

    class Con:
        def close(self):
            pass
    L._config = lambda: {'abilitato': True}
    L._connessione = lambda cfg: (Con(), None)
    L.pezzi_in_lantek = lambda codici: {'disponibile': True, 'pezzi': {}}
    try:
        Q._lantek_per_commessa = lambda cur, c: lt(A=4)
        Q._lantek_per_cliente = lambda cur, like, codici, dal: lt(A=4, B=10)
        r = Q.leggi_lantek('300', [], 'POLI%', ['A', 'B'], '2026-10-01')
        check('A solo col numero d\'ordine: confrontabile', r['L']['A']['pianificati'] == 4 and not r['L']['A'].get('da_cliente'), r['L'])
        check('B del cliente (a mano): da_cliente', r['L']['B'].get('da_cliente'), r['L'])
        Q._lantek_per_cliente = lambda cur, like, codici, dal: lt(A=300)
        r = Q.leggi_lantek('300', [], 'POLI%', ['A'], '2026-10-01')
        check('A col numero 4 ma il cliente ne ha 300 (a mano): si prende il cliente, da_cliente',
              r['L']['A']['pianificati'] == 300 and r['L']['A'].get('da_cliente'), r['L'])
        Q._lantek_per_commessa = lambda cur, c: {}
        Q._lantek_per_cliente = lambda cur, like, codici, dal: {}
        r = Q.leggi_lantek('300', [], 'POLI%', ['A'], '2026-10-01')
        check('niente in Lantek: per_cliente (come prima)', r['per_cliente'] and r['L'] == {}, r)
    finally:
        L._config, L._connessione, L.pezzi_in_lantek, Q._lantek_per_commessa, Q._lantek_per_cliente = vecchi


class _Ord:
    id = 'ord-diff-1'


def parte_3():
    print('\n3) Le rotte')
    from tests.accesso_aiuto import stazione_pronta, modalita
    modalita('protetto')
    c = stazione_pronta(A.app, 'laser')
    stato = {'ris': None, 'xml': [], 'expert': False, 'corr': []}
    righe_q = [
        {'codice': 'P1', 'ordinato': 6, 'lantek': 4, 'manda': {'codice': 'P1', 'quantita': 2},
         'problemi': [{'tipo': 'qta', 'cosa': 'Mancano 2', 'testo': ''}]},
        {'codice': 'P2', 'ordinato': 3, 'lantek': 5,
         'problemi': [{'tipo': 'qta', 'cosa': "Correggi l'ordine di produzione in Lantek a 3.", 'testo': 'In FerroTrack 3, in Lantek 5.'}]},
        {'codice': 'P4', 'ordinato': 4, 'lantek': None, 'manda': {'codice': 'P4-00', 'quantita': 4},
         'problemi': [{'tipo': 'non_mandato', 'cosa': '', 'testo': ''}]},
    ]
    stato['ris'] = {'lantek_ok': True, 'esito': 'NON_QUADRA', 'righe': righe_q, 'non_verificabile': []}
    Q.calcola = lambda oid, forza_lantek=False: stato['ris']
    Q.in_sottofondo = lambda oid: None
    LC.aperte = lambda oid: stato['corr']
    A._ordine_esistente = lambda oid: _Ord() if oid == _Ord.id else None
    A._quantita_lantek = lambda order, righe=None: {'commessa': '9999', 'cliente_lantek': 'PROVA', 'consegna': None,
                                                     'nome_file_xml': 'ORDINI LANTEK - 9999.xml'}
    L.xmlimporter_disponibile = lambda: True
    L.expert_aperto = lambda: stato['expert']

    def importa(xml, nome, cartella, attesa_s=300):
        stato['xml'].append(xml.decode('utf-8'))
        return {'eseguito': True, 'rapporto': {'totale': 2, 'ok': 2}}
    L.importa_xml = importa
    L.ordini_per_riferimento = lambda rifs: {
        r.upper(): {'codice': ('P1' if '-P1-' in r else 'P4-00'), 'quantita': (2 if '-P1-' in r else 4)} for r in rifs}
    url = f'/api/orders/{_Ord.id}/lantek-differenza'

    g = c.get(url).get_json() or {}
    check('GET: si mandano P1 +2 e P4-00 +4',
          [(x['codice'], x['quantita']) for x in g.get('manda') or []] == [('P1', 2), ('P4-00', 4)], g)
    check('GET: P2 (in piu\' in Lantek) fra quelli da correggere a mano',
          [x['codice'] for x in g.get('a_mano') or []] == ['P2'], g)

    r = c.post(url, json={'righe': [{'codice': 'P1', 'quantita': 3}, {'codice': 'P4-00', 'quantita': 4}]})
    check('POST diverso da quello che c\'e\' adesso: 409, niente mandato',
          r.status_code == 409 and not stato['xml'], (r.status_code, r.get_json()))
    stato['expert'] = True
    r = c.post(url, json={'righe': [{'codice': 'P1', 'quantita': 2}, {'codice': 'P4-00', 'quantita': 4}]})
    check('Lantek Expert aperto: 409, niente mandato', r.status_code == 409 and not stato['xml'], (r.status_code, r.get_json()))
    stato['expert'] = False
    stato['ris'] = {'lantek_ok': False, 'righe': [], 'non_verificabile': ['Lantek non risponde: prova']}
    r = c.post(url, json={'righe': [{'codice': 'P1', 'quantita': 2}, {'codice': 'P4-00', 'quantita': 4}]})
    check('Lantek non si legge: 503, niente mandato', r.status_code == 503 and not stato['xml'], (r.status_code, r.get_json()))
    stato['ris'] = {'lantek_ok': True, 'esito': 'NON_QUADRA', 'righe': righe_q, 'non_verificabile': []}

    r = c.post(url, json={'righe': [{'codice': 'p1', 'quantita': 2}, {'codice': 'P4-00', 'quantita': 4}]})
    d = r.get_json() or {}
    check('POST giusto: mandato, riletto, tutto torna', r.status_code == 200 and d.get('mandati') == 2 and d.get('problemi') == [],
          (r.status_code, d))
    x = stato['xml'][0] if stato['xml'] else ''
    check('nel file: P1 con 2 e P4-00 con 4, ordine 9999',
          'FldValue="P1"' in x and 'FldValue="2"' in x and 'FldValue="P4-00"' in x and 'FldValue="4"' in x
          and 'FldValue="9999"' in x, x[:400])
    gia = LI.mandati(_Ord.id)
    check('nel registro degli invii: P1 2, P4-00 4', gia.get('P1', {}).get('quantita') == 2 and gia.get('P4-00', {}).get('quantita') == 4, gia)

    stato['corr'] = [{'codice': 'P4-00'}]
    g = c.get(url).get_json() or {}
    check('codice "da correggere in Lantek": non si manda, va fra quelli a mano',
          [x['codice'] for x in g.get('manda') or []] == ['P1'] and 'P4-00' in [x['codice'] for x in g.get('a_mano') or []], g)

    anon = A.app.test_client()
    check('senza accesso: no', anon.get(url).status_code in (401, 403)
          and anon.post(url, json={'righe': [{'codice': 'P1', 'quantita': 2}]}).status_code in (401, 403))


if __name__ == '__main__':
    try:
        parte_1()
        parte_2()
        parte_3()
    finally:
        try:
            _ENG.dispose()
            os.remove(_TMP)
        except OSError:
            pass
    print(f'\nPASSATI: {OK}   FALLITI: {KO}')
    sys.exit(1 if KO else 0)
