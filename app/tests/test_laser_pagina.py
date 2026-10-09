"""Pagina laser nuova, parte server (backend/api_laser.py e dintorni).

Copre:
 1. /api/laser/ordini e /api/orders/<id>/laser: stato dell'ordine, disegni da
    controllare (solo i codici che servono: nuovi per Lantek), blocchi con
    cosa fare, differenze ordine/Lantek, cosa partirebbe
 2. decisioni sul pezzo (va sviluppato / piu' pezzi / non da laser): salvate
    nei campi extra, la posizione del pezzo non cambia, permessi, annullabili,
    nel registro degli esempi; il pezzo escluso NON va a Lantek
 3. foglio intero del disegno (/lettura, /foglio.svg): coordinate in mm con la
    y girata, contorno del motore in verde
 4. "giusto": senza DXF pulito si salva il contorno del motore (il DXF pulito
    si crea); col pulito gia' pronto e' la conferma di sempre
 5. /lantek-invia con "esclusi": i codici tenuti fuori non partono; controlli
 6. tempi tarati su Lantek per il calendario, fattore 1
 7. frasi corte dei motivi del motore

Gira su DATABASE TEMPORANEO, cartelle temporanee, LANTEK SIMULATO (nessun
programma di Lantek viene lanciato, nessuna lettura del database di Lantek).
Esecuzione: python app/tests/test_laser_pagina.py
"""
import json
import math
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
_ESEMPI = tempfile.mkdtemp(prefix='ft_lzp_esempi_')
os.environ['FERROTRACK_ESEMPI_DIR'] = _ESEMPI

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

import ezdxf  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Preventivo  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_lzp_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend import lantek as L  # noqa: E402
from backend import api_laser as AL  # noqa: E402
from backend.database import PreventivoManager  # noqa: E402
from backend.preventivi import tempi_laser as TL  # noqa: E402
from backend.preventivi import registro_esempi as RE  # noqa: E402

_CARTELLE = tempfile.mkdtemp(prefix='ft_lzp_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)

# ── Lantek simulato ─────────────────────────────────────────────────────
TL._leggi_lantek = lambda codici: None
PEZZI_LANTEK = {
    'P-OK-00': {'esiste': True, 'codice_lantek': 'P-OK-00', 'materiale': 'FERRO', 'spessore': 3.0, 'revisioni': []},
    'P-SPESS-00': {'esiste': True, 'codice_lantek': 'P-SPESS-00', 'materiale': 'FERRO', 'spessore': 2.0, 'revisioni': []},
}
CHIAMATE = []


def pezzi_in_lantek(codici):
    vuoto = {'esiste': False, 'codice_lantek': None, 'materiale': None, 'spessore': None, 'revisioni': []}
    return {'disponibile': True, 'errore': None, 'pezzi': {c: dict(PEZZI_LANTEK.get(c) or vuoto) for c in codici}}


def pezzi_finti(pezzi, cartella, user_data=None, attesa_s=900):
    CHIAMATE.append(('pezzi', [p['codice'] for p in pezzi]))
    for p in pezzi:
        PEZZI_LANTEK[p['codice']] = {'esiste': True, 'codice_lantek': p['codice'], 'materiale': 'FERRO',
                                     'spessore': 3.0, 'revisioni': []}
    return {'eseguito': True, 'errore': None, 'cartella': cartella}


ORDINI_LT = {}


def xml_finto(contenuto, nome, cartella, attesa_s=300):
    CHIAMATE.append(('xml', contenuto))
    for c in ('P-OK-00', 'P-DUB-00', 'P-SPESS-00', 'P-MANCA-00'):
        if ('"%s"' % c).encode() in contenuto:
            ORDINI_LT[c] = 1.0
    return {'eseguito': True, 'errore': None, 'file_rapporto': 'x',
            'rapporto': {'totale': 2, 'ok': 2, 'avvisi': 0, 'con_errore': 0, 'errori': []}}


L.pezzi_in_lantek = pezzi_in_lantek
L.clienti_lantek = lambda: ['DECA']
L.ordini_in_lantek = lambda commessa: dict(ORDINI_LT)
L.ordini_fatti_in_lantek = lambda commessa: {}
L.pezzi_dell_ordine = lambda commessa: {}
L.procesos_disponibile = lambda: True
L.xmlimporter_disponibile = lambda: True
L.importa_pezzi_dxf = pezzi_finti
L.importa_xml = xml_finto

OK = 0
KO = []


def check(nome, cond, extra=''):
    global OK
    if cond:
        OK += 1
        print(f'  [OK] {nome}')
    else:
        KO.append(nome)
        print(f'  [KO] {nome}  {extra}')


# Il pezzo: piastra 200 x 150 con due fori da 20, unica nel disegno
PEZZO = [(100, 50), (300, 50), (300, 200), (100, 200)]
FORI = [(150, 125, 10.0), (250, 125, 10.0)]


def scrivi_dxf(percorso):
    doc = ezdxf.new('R2010', units=4)
    msp = doc.modelspace()
    msp.add_lwpolyline(PEZZO, close=True)
    for cx, cy, r in FORI:
        msp.add_circle((cx, cy), r)
    doc.saveas(percorso)


def extra(aid):
    s = models.SessionLocal()
    try:
        r = s.execute(text('SELECT extra_campi FROM preventivo_articoli WHERE id = :i'), {'i': aid}).fetchone()
        return json.loads(r[0] or '{}')
    finally:
        s.close()


def esempi():
    p = os.path.join(RE.CARTELLA, RE.FILE)
    if not os.path.isfile(p):
        return []
    return [json.loads(x) for x in open(p, encoding='utf-8')]


def main():
    from tests.accesso_aiuto import postazioni, entra, modalita
    s = models.SessionLocal()
    s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
    s.commit(); s.close()
    modalita('protetto')
    postazioni()
    laser = entra(A.app, 'laser')
    rep = entra(A.app, 'reparto')

    base = {'quantita': 2, 'materiale': 'S235', 'spessore_mm': 3, 'costo_base_override': 5,
            'perimetro_taglio_m': 0.8, 'n_forature': 3, 'area_dm2': 2.9, 'bbox_w_mm': 200, 'bbox_h_mm': 150}
    pr = PreventivoManager.create('DECA S.r.l.', 'paolo', quantita=1, numero_ordine_cliente='4321')
    pid = pr['id'] if isinstance(pr, dict) else pr
    PreventivoManager.replace_articoli(pid, [
        dict(base, codice='P-OK-00', dxf_filename='P-OK-00.dxf', cleaned_status='auto', dxf_confidence=0.9),
        dict(base, codice='P-DUB-00', dxf_filename='P-DUB-00.dxf', dxf_confidence=0.45, dxf_needs_verify=True,
             esito_verifica={'stato': 'da_guardare', 'motivi': ['2 contorni esterni di dimensioni confrontabili nel disegno: verificare quale pezzo quotare']}),
        dict(base, codice='P-MANCA-00'),
        dict(base, codice='P-SPESS-00', dxf_filename='P-SPESS-00.dxf', cleaned_status='auto', dxf_confidence=0.9),
    ])
    s = models.SessionLocal()
    s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'INVIATO'
    s.commit(); s.close()
    oid = PreventivoManager.accetta_e_crea_ordine(pid, 'paolo').get('order_id')
    A.OrderManager.smista(oid, True, user_id='laser')
    arts = {a['codice']: a['id'] for a in (PreventivoManager.get(pid, include_children=True) or {})['articoli']}
    cartella = os.path.join(A.DRAWINGS_FOLDER, oid)
    os.makedirs(cartella, exist_ok=True)
    lamiera = A._nome_lamiera('S235', 3)
    for cod in ('P-OK-00', 'P-DUB-00', 'P-SPESS-00'):
        scrivi_dxf(os.path.join(cartella, cod + '.dxf'))
    # come un ordine caricato col pacchetto: puliti pronti, il dubbio "da preparare"
    pronti = os.path.join(cartella, 'LANTEK', lamiera)
    da_prep = os.path.join(cartella, 'LANTEK', A.CARTELLA_DA_PREPARARE, lamiera)
    os.makedirs(pronti); os.makedirs(da_prep)
    for cod in ('P-OK-00', 'P-SPESS-00'):
        shutil.copy2(os.path.join(cartella, cod + '.dxf'), os.path.join(pronti, cod + '.dxf'))
    shutil.copy2(os.path.join(cartella, 'P-DUB-00.dxf'), os.path.join(da_prep, 'P-DUB-00.dxf'))

    print('1) Riassunto e ordine aperto')
    r = laser.get('/api/laser/ordini')
    d = (r.get_json() or {}).get('ordini', {}).get(oid) or {}
    check('riassunto: 200, ordine da mettere in Lantek', r.status_code == 200 and d.get('stato') == 'coda', d)
    check('un disegno da controllare (il dubbio nuovo)', d.get('n_controllare') == 1, d)
    check('bloccato: P-MANCA senza disegno, con cosa fare',
          [b['codice'] for b in d.get('bloccati') or []] == ['P-MANCA-00']
          and 'MES' in d['bloccati'][0]['cosa'], d.get('bloccati'))
    check('non pronto per Lantek', d.get('pronto') is False, d)
    check('il tablet officina non lo legge', rep.get('/api/laser/ordini').status_code in (401, 403))
    r = laser.get(f'/api/orders/{oid}/laser')
    o = r.get_json() or {}
    pz = {p['codice']: p for p in o.get('pezzi') or []}
    check('ordine aperto: 4 pezzi', r.status_code == 200 and sorted(pz) == ['P-DUB-00', 'P-MANCA-00', 'P-OK-00', 'P-SPESS-00'], sorted(pz))
    check('P-OK: c\'e\' gia\' in Lantek, motore sicuro', pz['P-OK-00']['lantek'] == 'in_lantek' and pz['P-OK-00']['motore'] == 'sicuro')
    check('P-DUB: nuovo, da controllare, motivo in frase corta',
          pz['P-DUB-00']['lantek'] == 'nuovo' and pz['P-DUB-00']['da_controllare']
          and any('viste simili' in m for m in pz['P-DUB-00']['perche']), pz['P-DUB-00'])
    check('differenza di spessore ordine/Lantek per P-SPESS',
          [(x['codice'], x['tipo'], x['ordine'], x['lantek']) for x in o.get('differenze') or []]
          == [('P-SPESS-00', 'spessore', '3 mm', '2 mm')], o.get('differenze'))
    check('cosa partirebbe: i due gia\' in Lantek', sorted(o['invio']['codici']) == ['P-OK-00', 'P-SPESS-00'], o.get('invio'))

    print('\n2) Decisioni sul pezzo')
    url = f'/api/orders/{oid}/pezzi/{arts["P-MANCA-00"]}/decisione'
    check('scelta sbagliata: 400', laser.post(url, json={'scelta': 'boh'}).status_code == 400)
    check('tablet officina: no', rep.post(url, json={'scelta': 'non_laser'}).status_code in (401, 403))
    prima_ordine = [a['codice'] for a in PreventivoManager.get(pid, include_children=True)['articoli']]
    r = laser.post(url, json={'scelta': 'non_laser'})
    check('non e\' da laser: salvato', r.status_code == 200 and r.get_json().get('scelta') == 'non_laser', r.get_json())
    ex = extra(arts['P-MANCA-00'])
    check('nei campi extra del pezzo, con chi e quando',
          (ex.get('decisione_laser') or {}).get('scelta') == 'non_laser' and ex['decisione_laser'].get('quando'), ex)
    check('la posizione dei pezzi non cambia',
          [a['codice'] for a in PreventivoManager.get(pid, include_children=True)['articoli']] == prima_ordine)
    ev = [e for e in esempi() if e['evento'] == 'decisione_laser']
    check('nel registro degli esempi del motore', ev and ev[-1]['codice'] == 'P-MANCA-00'
          and ev[-1]['decisione'] == {'scelta': 'non_laser'}, ev[-1:])
    d = (laser.get('/api/laser/ordini').get_json() or {})['ordini'][oid]
    check('non blocca piu\', resta fuori', not d.get('bloccati') and d.get('n_esclusi') == 1, d)
    o = laser.get(f'/api/orders/{oid}/laser').get_json()
    check('nell\'ordine: P-MANCA fuori col motivo',
          next(p for p in o['pezzi'] if p['codice'] == 'P-MANCA-00')['escluso'] == 'non è da laser'
          and [e['codice'] for e in o['esclusi']] == ['P-MANCA-00'])
    q = laser.get(f'/api/orders/{oid}/lantek-quantita').get_json()
    check('anche /lantek-quantita lo dice e non lo conta', [e['codice'] for e in q.get('esclusi_laser') or []] == ['P-MANCA-00']
          and 'P-MANCA-00' not in [r['codice'] for r in q['righe']], q.get('esclusi_laser'))
    r = laser.post(url, json={'scelta': None})
    check('si toglie (null): torna bloccato', r.status_code == 200
          and (laser.get('/api/laser/ordini').get_json()['ordini'][oid].get('bloccati') or [{}])[0].get('codice') == 'P-MANCA-00')
    laser.post(url, json={'scelta': 'piu_pezzi'})

    print('\n3) Foglio intero del disegno')
    aid = arts['P-DUB-00']
    r = laser.get(f'/api/orders/{oid}/pezzi/{aid}/lettura')
    d = r.get_json() or {}
    svg = d.get('svg') or ''
    check('lettura 200 con l\'SVG del foglio', r.status_code == 200 and svg.startswith('<svg') and 'data-y-invertita="1"' in svg, (r.status_code, svg[:120]))
    check('vista = estensione del disegno in mm', d.get('vista') and abs(d['vista'][0] - 100) < 1
          and abs(d['vista'][2] - 300) < 1 and abs(d['vista'][3] - 200) < 1, d.get('vista'))
    verde = svg.split('class="fg-pezzo" d="')[-1].split('"')[0] if 'class="fg-pezzo"' in svg else ''
    check('contorno del motore in verde, y girata (y 200 del disegno -> -200 nel foglio)',
          verde and ' -200' in verde and ' 200' not in verde.replace(' -200', ''), verde[:200])
    check('perche\' in frasi corte', any('viste simili' in m for m in d.get('perche') or []), d.get('perche'))
    r = laser.get(f'/api/orders/{oid}/pezzi/{aid}/foglio.svg')
    check('foglio.svg come immagine, con la trasformazione', r.status_code == 200 and r.mimetype == 'image/svg+xml'
          and r.headers.get('X-Trasforma'), (r.status_code, r.mimetype))
    check('pezzo senza disegno: 404 chiaro', laser.get(f'/api/orders/{oid}/pezzi/{arts["P-MANCA-00"]}/lettura').status_code == 404)
    check('tablet officina: no', rep.get(f'/api/orders/{oid}/pezzi/{aid}/lettura').status_code in (401, 403))

    print('\n4) "Si\', e\' il pezzo giusto"')
    r = laser.post(f'/api/orders/{oid}/pezzi/{aid}/giusto', json={'serve_pulito': True})
    d = r.get_json() or {}
    check('senza DXF pulito: salvo il contorno del motore', r.status_code == 200 and d.get('modo') == 'contorno', (r.status_code, d))
    check('area del pezzo coi fori', abs((d.get('area_dm2') or 0) - (200 * 150 - 2 * math.pi * 100) / 1e4) < 0.01, d)
    check('DXF pulito pronto per Lantek', d.get('pronto_lantek') is True and os.path.isfile(os.path.join(pronti, 'P-DUB-00.dxf')), d.get('motivo'))
    check('decisione "giusto"', (extra(aid).get('decisione_laser') or {}).get('scelta') == 'giusto')
    check('nel registro: contorno confermato', any(e['evento'] == 'contorno_confermato' and e['codice'] == 'P-DUB-00' for e in esempi()))
    d = laser.get('/api/laser/ordini').get_json()['ordini'][oid]
    check('niente piu\' da controllare: pronto per Lantek', d.get('n_controllare') == 0 and d.get('pronto') is True, d)
    r = laser.post(f'/api/orders/{oid}/pezzi/{arts["P-OK-00"]}/giusto', json={})
    check('col pulito gia\' pronto: la conferma di sempre', r.status_code == 200 and r.get_json().get('modo') == 'conferma', r.get_json())

    print('\n5) Manda a Lantek con un codice tenuto fuori')
    o = laser.get(f'/api/orders/{oid}/laser').get_json()
    inv = o['invio']
    check('parte: 2 gia\' in Lantek + 1 nuovo col disegno', sorted(inv['codici']) == ['P-OK-00', 'P-SPESS-00'] and inv['nuovi'] == ['P-DUB-00'], inv)
    u = f'/api/orders/{oid}/lantek-invia'
    r = laser.post(u, json={'codici': ['P-OK-00', 'P-SPESS-00'], 'nuovi': ['P-DUB-00'], 'esclusi': ['P-SPESS-00']})
    check('un codice sia da mandare sia fuori: 400', r.status_code == 400 and not CHIAMATE, r.get_json())
    r = laser.post(u, json={'codici': ['P-OK-00'], 'nuovi': ['P-DUB-00']})
    check('elenco diverso da quello visto: 409, niente parte', r.status_code == 409 and not CHIAMATE, r.get_json())
    r = laser.post(u, json={'codici': ['P-OK-00'], 'nuovi': ['P-DUB-00'], 'esclusi': ['P-SPESS-00']})
    j = r.get_json() or {}
    xml = next((c[1] for c in CHIAMATE if c[0] == 'xml'), b'')
    check('mandato: prima il pezzo nuovo, poi gli ordini', r.status_code == 200 and [c[0] for c in CHIAMATE] == ['pezzi', 'xml']
          and CHIAMATE[0][1] == ['P-DUB-00'], (r.status_code, j, [c[0] for c in CHIAMATE]))
    check('negli ordini: P-OK e P-DUB, non P-SPESS (tenuto fuori) ne\' P-MANCA (a mano)',
          b'"P-OK-00"' in xml and b'"P-DUB-00"' in xml and b'P-SPESS-00' not in xml and b'P-MANCA-00' not in xml)
    check('verificati in Lantek', j.get('verificati') == j.get('mandati') == 2, j)

    print('\n6) Tempi per il calendario')
    t = A._tempo_pezzo_laser(dict(base, codice='X'), None)
    atteso = TL.minuti_pezzo(None, dict(base))['minuti']
    check('modello tarato su Lantek, col fattore foglio', t['tempo_fonte'] == 'modello' and abs(t['tempo_min'] - atteso) < 1e-6, t)
    t = A._tempo_pezzo_laser(dict(base, codice='X', perimetro_taglio_m=0), 0.5)
    check('modello non applicabile: stima del preventivo x 1,18', t['tempo_fonte'] == 'preventivo' and abs(t['tempo_min'] - 0.59) < 1e-9, t)
    check('calendario: fattore 1', A._cal_config()['fattore_tempo'] == 1.0)

    print('\n7) Frasi corte')
    mb = A.motivo_breve
    check('viste simili', mb('3 contorni esterni di dimensioni confrontabili nel disegno: verificare') == 'Nel foglio ci sono 3 viste simili: è questo il pezzo?')
    check('duplicati: non serve dirlo', mb('6 entità duplicate/sovrapposte ignorate') is None)
    check('contorno scelto dal modello', mb('Contorno scelto dal modello addestrato invece di quello a regole: verificare')
          == 'Contorno scelto dal modello: controlla che sia il pezzo')
    check('piegato', mb('Il disegno mostra il pezzo piegato (profilo...): manca lo sviluppo') == 'Disegnato piegato: forse manca lo sviluppo')
    check('al massimo tre, senza doppioni', len(A.motivi_brevi(['scala 1:4', 'scala 1:2', 'linee aperte', 'nessun riscontro', 'facce dentro'])) == 3)

    print(f'\nPASSATI: {OK}   FALLITI: {len(KO)}')
    if KO:
        print('Falliti: ' + ', '.join(KO))
    return 1 if KO else 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    finally:
        shutil.rmtree(_CARTELLE, ignore_errors=True)
        shutil.rmtree(_ESEMPI, ignore_errors=True)
