"""Test del percorso DATI CAD → PREZZO del preventivatore.

Copre i difetti per cui i dati arrivati dal CAD (DXF, cartiglio, STEP, XLSX
Lantek) non arrivavano giusti al prezzo:

 P3  materiale originale passato alla ricetta (INOX_316L, ALU_6082, S235JR…
     → costo base 0 "riuscito"); ricetta mancante = costo materiale perso
 P8  resa del nesting (sfrido) configurabile, default 1 = nessun cambiamento
 P9  spessore fuori tabella segnalato e conservato sull'articolo
 M8  velocita' di taglio a 0 nelle Impostazioni → ricetta di fabbrica
 P7  verifica prima dell'invio: spessore mancante, perimetro 0, materiale
     sconosciuto, ricetta mancante, costo 0 con geometria (anche in assieme)
 P7  validazione misure (saldatura, spessore, area, perimetro)
 M7  apporto e pulizia sui cordoni di ASSIEME, anche col prezzo congelato
 P10 gas_taglio, avvisi_stima, origine_campi salvati e riletti
 B2  PDF: base con ripiego su costo_materiale, ricarico congelato a 0, righe
     che sommano al totale; /calcola usa il calcolo autorevole
 B6  stima tempi di consegna con i nomi di campo veri
 P2  (lato server) cost_calculator: saldatura a tempo senza minuti → a metro
 JS  la logica dell'editor (salvataggio, stima, origine campi, cartiglio,
     sconto, impostazioni) in Chrome headless, se Chrome c'e'

Gira su DATABASE e CONFIG TEMPORANEI. Esecuzione:
    python app/tests/test_costi_cad.py
"""
import json
import os
import shutil
import subprocess
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
from backend.models import Base, User  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_cad_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend.app import app, _preventivo_to_pdf_dati  # noqa: E402
from backend.database import PreventivoManager, BarcodeManager  # noqa: E402
from backend.preventivi.calcolo import calcola  # noqa: E402
from backend.preventivi.verifica import verifica  # noqa: E402
from backend.preventivi.validazione import valida_articoli  # noqa: E402
from backend.preventivi.laser_cost_estimator import stima_base, DEFAULT_LASER_CONFIG  # noqa: E402
from backend.preventivi.lantek_lookup import normalizza_materiale, lookup_ricetta  # noqa: E402
from backend.preventivi.delivery_estimator import stima_tempi_consegna  # noqa: E402
from backend.preventivi.cost_calculator import calcola_preventivo  # noqa: E402

PCFG = {'costo_generali_pct': 8, 'saldatura_a_tempo': True, 'tariffa_oraria': 45,
        'costo_saldatura_metro': 25, 'costo_materiale_apporto_metro': 3,
        'costo_pulizia_saldatura_metro': 5, 'costo_singola_piega': 1,
        'costo_setup_piega': 10, 'soglia_setup_pieghe': 5, 'costo_filettatura': 0.8,
        'costo_svasatura': 0.3}
_CFG = os.path.join(tempfile.gettempdir(), f'test_cad_cfg_{uuid.uuid4().hex[:8]}.json')
BarcodeManager._CONFIG_PATH = _CFG
with open(_CFG, 'w', encoding='utf-8') as _f:
    json.dump({'preventivi_config': PCFG, 'laser_config': DEFAULT_LASER_CONFIG}, _f)

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


PEZZO = {'spessore_mm': 3, 'area_dm2': 10, 'perimetro_taglio_m': 1.5, 'n_forature': 3}


def test_materiali_e_ricette():
    print('\nP3) Materiale canonico per la ricetta, alias CAD/Lantek')
    base_s235 = stima_base({**PEZZO, 'materiale': 'S235'})['base']
    for m in ('S235JR', 'ACCIAIO', 'C45', 'S355J2+N', 'FE'):
        r = stima_base({**PEZZO, 'materiale': m})
        check(f'{m} stimato come S235', r['base'] == base_s235 and not r['ricetta_mancante'], r)
    base_inox = stima_base({**PEZZO, 'materiale': 'INOX_304'})['base']
    for m in ('INOX_316L', 'Inox 316L', 'AISI 316'):
        r = stima_base({**PEZZO, 'materiale': m})
        check(f'{m} stimato come inox', r['base'] == base_inox and r['base'] > 0, r)
    for m in ('ALU_6082', 'ALU_6061', 'alu-5754'):
        r = stima_base({**PEZZO, 'materiale': m})
        check(f'{m} stimato come alluminio', r['base'] > 0 and r['_ricetta']['materiale'] == 'ALU', r)
    r = stima_base({**PEZZO, 'materiale': 'DX51D'})
    check('DX51D stimato come zincato', r['base'] > 0 and r['_ricetta']['materiale'] == 'ZINCATO', r)
    for m in ('CU', 'ALTRO'):
        r = stima_base({**PEZZO, 'materiale': m})
        check(f'{m}: materiale sconosciuto dichiarato, non uno zero silenzioso',
              r['materiale_sconosciuto'] and r['ricetta_mancante'] and r['warnings'], r)
    check('normalizzazione: CU/ALTRO non riconducibili',
          normalizza_materiale('CU') is None and normalizza_materiale('ALTRO') is None)

    print('\n   ricetta mancante: resta il costo materiale, con flag e avviso')
    cfg = {'laser_config': {**DEFAULT_LASER_CONFIG,
                            'materiali': {**DEFAULT_LASER_CONFIG['materiali'],
                                          'TITANIO': {'densita_kg_dm3': 4.5, 'euro_kg': 30}}}}
    r = stima_base({**PEZZO, 'materiale': 'TITANIO'}, cfg)
    check('ricetta_mancante=True', r['ricetta_mancante'] is True, r)
    check('costo materiale NON perso', r['costo_materiale'] > 0 and r['base'] >= r['costo_materiale'], r)
    check('avviso chiaro', any('taglio' in w for w in r['warnings']), r['warnings'])


def test_resa_e_ricette_config():
    print('\nP8) Resa nesting')
    base = stima_base({**PEZZO, 'materiale': 'S235'})
    cfg = {'laser_config': {**DEFAULT_LASER_CONFIG, 'resa_nesting': 0.8}}
    r = stima_base({**PEZZO, 'materiale': 'S235'}, cfg)
    check('materiale pagato sul lordo (netto / resa)',
          abs(r['costo_materiale'] - base['costo_materiale'] / 0.8) < 0.001, (r, base))
    check('peso netto invariato, lordo esposto',
          r['peso_kg'] == base['peso_kg'] and abs(r['peso_lordo_kg'] - base['peso_kg'] / 0.8) < 0.001)
    check('default 1.0 = nessun cambiamento', DEFAULT_LASER_CONFIG['resa_nesting'] == 1.0
          and base['resa_nesting'] == 1.0)
    with open(os.path.join(_APP, 'app_config.json'), encoding='utf-8') as f:
        vero = json.load(f)
    check('app_config.json: resa_nesting 1.0', vero['laser_config'].get('resa_nesting') == 1.0)
    r = stima_base({**PEZZO, 'materiale': 'S235'},
                   {'laser_config': {**DEFAULT_LASER_CONFIG, 'resa_nesting': 7}})
    check('resa impossibile → 1 con avviso', r['costo_materiale'] == base['costo_materiale']
          and any('Resa' in w for w in r['warnings']), r)

    print('\nM8) Velocita\' a 0 nelle Impostazioni → ricetta di fabbrica')
    cfg = {'laser_config': {**DEFAULT_LASER_CONFIG, 'ricette_taglio': [
        {'materiale': 'S235', 'gas': 'N2', 'spessore_mm': 3.0, 'velocita_mm_min': 0, 'pierce_time_s': 0.5}]}}
    r = stima_base({**PEZZO, 'materiale': 'S235'}, cfg)
    check('costo di taglio non azzerato', abs(r['base'] - base['base']) < 0.01, (r['base'], base['base']))
    check('avviso "ricetta di fabbrica"', any('fabbrica' in w for w in r['warnings']), r['warnings'])
    rec = lookup_ricetta('S235', 3.0, None, ricette_override=[
        {'materiale': 'S235', 'gas': 'N2', 'spessore_mm': 3.0, 'velocita_mm_min': 1234, 'pierce_time_s': 0.4}])
    check('ricetta configurata valida usata', rec and rec['velocita_mm_min'] == 1234, rec)

    print('\nP9) Spessore fuori tabella')
    r = stima_base({**PEZZO, 'materiale': 'S235', 'spessore_mm': 40})
    check('flag spessore_fuori_tabella', r['spessore_fuori_tabella'] is True, r)
    check('avviso con lo spessore', any('40' in w and 'fuori tabella' in w for w in r['warnings']), r['warnings'])


def _prev(**kw):
    p = {'cliente': 'Alfa', 'quantita': 1, 'margine_pct': 0, 'data_consegna_proposta': '2026-10-01',
         'articoli': [], 'assiemi': [], 'tubolari': [], 'piastre': []}
    p.update(kw)
    return p


def test_verifica():
    print('\nP7) Verifica prima dell\'invio')
    art = {'id': 'x', 'codice': 'PZ-9', 'quantita': 1, 'materiale': 'S235',
           'area_dm2': 10, 'perimetro_taglio_m': 1.2, 'spessore_mm': 3, 'costo_base_stimato': 5}
    e = verifica(_prev(articoli=[{**art, 'spessore_mm': None}]), PCFG)
    msg = [x for x in e['errori'] if x['tipo'] == 'spessore_mancante']
    check('spessore mancante su pezzo laser = errore', msg and 'PZ-9' in msg[0]['messaggio'], e['errori'])
    e = verifica(_prev(articoli=[{**art, 'perimetro_taglio_m': 0}]), PCFG)
    check('perimetro 0 con area = errore', 'perimetro_nullo' in tipi(e['errori']), e['errori'])
    e = verifica(_prev(articoli=[{**art, 'materiale': 'CU'}]), PCFG)
    check('materiale sconosciuto = errore', 'materiale_sconosciuto' in tipi(e['errori']), e['errori'])
    e = verifica(_prev(articoli=[{**art, 'ricetta_mancante': True}]), PCFG)
    check('ricetta mancante = errore', 'ricetta_mancante' in tipi(e['errori']), e['errori'])
    e = verifica(_prev(articoli=[{**art, 'costo_base_stimato': 0}]), PCFG)
    check('costo base 0 con perimetro = errore (preciso, non generico)',
          'costo_base_zero' in tipi(e['errori']) and 'pezzo_senza_prezzo' not in tipi(e['errori']), e['errori'])
    e = verifica(_prev(articoli=[{**art, 'costo_base_stimato': 0, 'codice_assieme': 'A1'}],
                       assiemi=[{'codice_assieme': 'A1', 'qty': 1, 'costo': 100}]), PCFG)
    check('anche un componente di assieme con geometria e costo 0', 'costo_base_zero' in tipi(e['errori']),
          e['errori'])
    e = verifica(_prev(articoli=[{**art, 'materiale': 'CU', 'costo_base_override': 40}]), PCFG)
    check('prezzo manuale: materiale sconosciuto solo avviso', e['pronto'] and
          'materiale_sconosciuto' in tipi(e['avvisi']), (e['errori'], e['avvisi']))
    e = verifica(_prev(articoli=[{**art, 'spessore_mm': 40}]), PCFG)
    check('spessore fuori tabella = avviso', e['pronto'] and 'spessore_fuori_tabella' in tipi(e['avvisi']),
          (e['errori'], e['avvisi']))
    e = verifica(_prev(articoli=[{**art, 'saldatura_ml': 1.5, 'saldatura_min': 0, 'costo_saldatura': 37.5}]), PCFG)
    check('saldatura stimata a metro = avviso', 'saldatura_stimata_a_metro' in tipi(e['avvisi']), e['avvisi'])
    e = verifica(_prev(articoli=[art]), PCFG)
    check('pezzo in ordine: pronto, nessun avviso CAD', e['pronto'] and not (
        tipi(e['avvisi']) & {'spessore_fuori_tabella', 'materiale_sconosciuto', 'ricetta_mancante'}), e)

    print('\nP7) Validazione misure')
    err = valida_articoli([{'codice': 'N', 'saldatura_ml': -1}])
    check('saldatura negativa rifiutata', err and 'saldatura_ml' in err[0], err)
    err = valida_articoli([{'codice': 'N', 'saldatura_min': -5}])
    check('minuti negativi rifiutati', err and 'saldatura_min' in err[0], err)
    err = valida_articoli([{'codice': 'N', 'spessore_mm': 3000}])
    check('spessore assurdo rifiutato', err and 'spessore_mm' in err[0], err)
    err = valida_articoli([{'codice': 'N', 'area_dm2': -2}, {'codice': 'M', 'perimetro_taglio_m': 'abc'}])
    check('area negativa e perimetro non numerico rifiutati', len(err) == 2, err)
    check('bozza con zeri e vuoti ammessa',
          valida_articoli([{'codice': 'Z', 'spessore_mm': None, 'area_dm2': 0, 'saldatura_ml': ''}]) == [])


def test_calcolo_assiemi():
    print('\nM7) Apporto + pulizia sui cordoni di assieme')
    p = _prev(assiemi=[{'codice_assieme': 'A1', 'qty': 2, 'costo': 10, 'saldatura_mt': 2.0}])
    t = calcola(p, {'costo_generali_pct': 0, 'costo_materiale_apporto_metro': 3,
                    'costo_pulizia_saldatura_metro': 5})
    check('assieme: 2 m × (3+5) €/m in piu', t['dettaglio_assiemi'][0]['apporto_pulizia'] == 16
          and t['costo_assiemi'] == (10 + 16) * 2, t['dettaglio_assiemi'])
    snap = {'costo_generali_pct': 0, 'costo_materiale_apporto_metro': 3, 'costo_pulizia_saldatura_metro': 5}
    t2 = calcola({**p, 'snapshot_economico': snap}, {'costo_generali_pct': 0})
    check('prezzo congelato: tariffe dalla fotografia', t2['costo_assiemi'] == t['costo_assiemi'], t2)
    t3 = calcola({**p, 'snapshot_economico': {'costo_generali_pct': 0}}, {'costo_generali_pct': 0})
    check('fotografia vecchia (senza tariffe): prezzo com\'era', t3['costo_assiemi'] == 20, t3)
    t4 = calcola(_prev(tubolari=[{'costo_materiale': 30, 'costo_taglio_totale': 5, 'qty': 4}],
                       piastre=[{'costo': 12, 'qty': 3}]), {'costo_generali_pct': 0})
    check('qty delle righe STEP non rimoltiplica (costi gia\' totali)',
          t4['costo_tubolari'] == 35 and t4['costo_piastre'] == 12, t4)


def test_database_e_pdf():
    print('\nP10) Campi senza colonna: salvati e riletti')
    s = models.SessionLocal()
    try:
        s.add(User(id='commerciale', name='Marco', role='Commerciale', is_active=True))
        s.commit()
    finally:
        s.close()
    p = PreventivoManager.create('Beta', 'commerciale', quantita=2, margine_pct=20)
    pid = p['id']
    articoli = [
        {'codice': 'P1', 'quantita': 1, 'materiale': 'INOX_316L', 'spessore_mm': 3, 'area_dm2': 10,
         'perimetro_taglio_m': 1.5, 'n_forature': 2, 'costo_base_stimato': 8.31, 'gas_taglio': 'n2',
         'avvisi_stima': ['Spessore fuori tabella'], 'spessore_fuori_tabella': True,
         'origine_campi': {'pieghe': 'manuale', 'area_dm2': 'cad', 'x': 'boh'},
         'saldatura_ml': 1.2, 'saldatura_min': 0, 'costo_saldatura': 30, 'costo_apporto': 3.6,
         'costo_pulizia': 6},
        {'codice': 'P2', 'quantita': 3, 'materiale': 'S235', 'spessore_mm': 2, 'costo_materiale': 17.5,
         'costo_base_override': 17.5, 'fonte_costo_base': 'lantek'},
        {'codice': 'P3', 'quantita': 1, 'materiale': 'S235', 'costo_materiale': 4.0},
    ]
    r = PreventivoManager.replace_articoli(pid, articoli)
    check('salvataggio riuscito', r.get('success'), r)
    g = PreventivoManager.get(pid)
    by = {a['codice']: a for a in g['articoli']}
    check('gas_taglio salvato (normalizzato)', by['P1'].get('gas_taglio') == 'N2', by['P1'])
    check('avvisi_stima salvati', by['P1'].get('avvisi_stima') == ['Spessore fuori tabella'])
    check('origine_campi salvata (solo valori ammessi)',
          by['P1'].get('origine_campi') == {'pieghe': 'manuale', 'area_dm2': 'cad'}, by['P1'].get('origine_campi'))
    check('flag spessore_fuori_tabella salvato', by['P1'].get('spessore_fuori_tabella') is True)
    check('fonte del costo base salvata', by['P2'].get('fonte_costo_base') == 'lantek')
    check('articolo senza extra: nessun campo inventato', 'gas_taglio' not in by['P3'])
    r = PreventivoManager.replace_articoli(pid, [a for a in g['articoli']])
    g2 = PreventivoManager.get(pid)
    check('secondo giro salva/rileggi stabile',
          {a['codice']: a.get('avvisi_stima') for a in g2['articoli']}['P1'] == ['Spessore fuori tabella'])

    print('\nB2) PDF allineato al calcolo autorevole')
    PreventivoManager.replace_assiemi(pid, [{'codice_assieme': 'ASS', 'qty': 1, 'costo': 10,
                                             'saldatura_mt': 2.0}])
    g = PreventivoManager.get(pid)
    dati = _preventivo_to_pdf_dati(g)
    tot = calcola(g, PCFG)
    check('totale PDF = calcolo autorevole', abs(dati['totale_lotto'] - tot['totale_lotto']) < 0.01,
          (dati['totale_lotto'], tot['totale_lotto']))
    somma = round(sum(x['importo'] for x in dati['righe_cliente']), 2)
    check('le righe cliente sommano al totale (assieme con apporto/pulizia)',
          abs(somma - dati['totale_lotto']) <= 0.05, (somma, dati['totale_lotto'], dati['righe_cliente']))
    p3 = [x for x in dati['articoli'] if x['codice'] == 'P3'][0]
    check('base con ripiego su costo_materiale', p3['costo_base'] == 4.0 and p3['costo'] == 4.0, p3)
    check('fattore prezzo passato alla distinta interna',
          abs(dati['fattore_prezzo'] - 1.08 * 1.2) < 1e-9, dati.get('fattore_prezzo'))
    c = app.test_client()
    for interno in ('', '?interno=1'):
        resp = c.get(f'/api/preventivi/{pid}/pdf{interno}')
        check(f'PDF {"interno" if interno else "cliente"} generato', resp.status_code == 200
              and resp.data[:4] == b'%PDF', resp.status_code)

    print('\n   /calcola usa il calcolo autorevole')
    resp = c.post(f'/api/preventivi/{pid}/calcola', json={'admin_id': 'commerciale'})
    d = resp.get_json() or {}
    check('/calcola: totali del calcolo autorevole', d.get('success') and
          abs(d['totali']['totale_lotto'] - tot['totale_lotto']) < 0.01, d)

    print('\n   invio: la fotografia conserva tariffe e il ricarico 0 vale')
    PreventivoManager.update(pid, {'margine_pct': 0})
    esito = PreventivoManager.transition_status(pid, 'INVIATO', 'commerciale')
    g = PreventivoManager.get(pid)
    snap = g.get('snapshot_economico') or {}
    check('tariffe apporto/pulizia nella fotografia', snap.get('costo_materiale_apporto_metro') == 3
          and snap.get('costo_pulizia_saldatura_metro') == 5, (esito, snap))
    dati = _preventivo_to_pdf_dati(g)
    check('ricarico congelato a 0 non sostituito', dati['margine'] == 0, dati['margine'])
    congelato = calcola({**g, 'margine_pct': snap.get('ricarico_pct')},
                        {'costo_generali_pct': snap.get('costo_generali_pct')})
    check('totale dopo l\'invio = totale fotografato', abs(congelato['totale_lotto'] - snap['totale_lotto']) < 0.01,
          (congelato['totale_lotto'], snap.get('totale_lotto')))


def test_vari():
    print('\nB6) Stima tempi di consegna con i campi veri')
    r = stima_tempi_consegna([{'pieghe': 2, 'saldatura_ml': 12.5, 'filettatura_pz': 1, 'svasatura_pz': 0,
                               'quantita': 1}], {}, {}, {}, {'velocita_saldatura_mt_ora': 12.5}, 1)
    check('saldatura_ml in metri conteggiata (1 h per 12.5 m)', r['breakdown']['ore_saldatura'] == 1.0, r)
    check('filettature conteggiate', r['breakdown']['ore_foratura'] > 0, r)

    print('\nP2) Calcolatore: saldatura a tempo senza minuti → stima a metro')
    cfg = {**PCFG, 'costo_singola_piega': 1}
    r = calcola_preventivo([], cfg, 1, 0, {}, {}, {}, saldatura=2.0, saldatura_min=0)
    check('manodopera non a zero', r['costo_saldatura'] == 50, r['costo_saldatura'])
    r = calcola_preventivo([], cfg, 1, 0, {}, {}, {}, saldatura=2.0, saldatura_min=30)
    check('con i minuti resta a tempo', r['costo_saldatura'] == 22.5, r['costo_saldatura'])


# ---------------------------------------------------------------------------
# Logica JS dell'editor, in Chrome headless (se disponibile)
# ---------------------------------------------------------------------------
_STUB_JS = r"""
localStorage.setItem('currentUser', JSON.stringify({id:'u1', name:'Test Uno', role:'Commerciale'}));
window.__calls = [];
window.CFG = {saldatura_a_tempo:true, costo_saldatura_metro:25, tariffa_oraria:45, costo_materiale_apporto_metro:3,
  costo_pulizia_saldatura_metro:5, costo_generali_pct:8, costo_singola_piega:1, soglia_setup_pieghe:5, costo_setup_piega:10,
  costo_filettatura:0.8, costo_svasatura:0.3};
window.fetch = async function (url, opts) {
  opts = opts || {};
  __calls.push({url: String(url), method: (opts.method || 'GET'), body: (typeof opts.body === 'string') ? opts.body : null});
  const J = (o, ok) => ({ ok: ok !== false, status: ok === false ? 500 : 200, json: async () => o,
                          text: async () => '', blob: async () => new Blob(), headers: {get: () => ''} });
  const u = String(url);
  if (u.indexOf('/api/preventivi/config') >= 0 && opts.method === 'PUT') return J({success:true, preventivi_config: CFG});
  if (u.indexOf('/api/preventivi/config') >= 0) return J({success:true, preventivi_config: CFG, laser_config:{}});
  if (u.indexOf('stima-base') >= 0) return J({success:true, stima:{base:12.34,
      warnings:['Spessore 40 mm fuori tabella Lantek'], spessore_fuori_tabella:true, ricetta_mancante:false}});
  if (u.indexOf('/calcola') >= 0) return J({success:true, totali:{totale_lotto:999.99, totale_pezzo:1, totale_pezzo_con_margine:2}});
  if (u.indexOf('import-xlsx') >= 0) return J({success:true, count:2, articoli:[
     {codice:'X1', quantita:2, materiale:'S355', spessore_mm:3, area_dm2:4, perimetro_taglio_m:1, costo:17.5, pieghe:2},
     {codice:'X2', quantita:1, materiale:'CU', spessore_mm:2, area_dm2:1, perimetro_taglio_m:0.5, costo:0}]});
  if (opts.method === 'PUT') return J({success:true, preventivo:{id:'p1', status:'BOZZA', cliente:'C'}});
  return J({}, false);
};
"""

_TEST_JS = r"""
window.addEventListener('load', () => setTimeout(async () => {
  const R = [];
  const ok = (nome, cond, extra) => R.push((cond ? 'OK  ' : 'KO  ') + nome + (cond ? '' : ' :: ' + JSON.stringify(extra)));
  try {
    _prevCfg = CFG;
    let a = {saldatura_ml: 2, saldatura_min: 0};
    recalcLavorazioniArt(a);
    ok('P2 saldatura a tempo senza minuti -> stima a metro', a.costo_saldatura === 50 && a._sald_stima_metro === true, a);
    a.saldatura_min = 30; recalcLavorazioniArt(a);
    ok('P2 con minuti -> a tempo', a.costo_saldatura === 22.5 && !a._sald_stima_metro, a);
    currentUser = {id:'u1'};
    currentPreventivo = {id:'p1', status:'BOZZA', cliente:'C', sconto_pct:0, quantita:1, margine_pct:0};
    currentArticoli = [{id:'a1', codice:'A', quantita:1, materiale:'S235', spessore_mm:2, area_dm2:1, perimetro_taglio_m:1,
                        pieghe:0, saldatura_ml:0, costo_base_stimato:5, costo_base_override:null}];
    currentAssiemi = []; currentTubolari = []; currentPiastre = [];
    updateArt(0, 'pieghe', '3', true);
    ok('P1 modifica lavorazione messa in coda', !!_salva.articoli.inSospeso);
    ok('M3 origine manuale registrata', (currentArticoli[0].origine_campi || {}).pieghe === 'manuale');
    ok('P1 beforeunload vede la modifica', salvataggioInSospeso() === true);
    __calls.length = 0;
    const salvato = await salvaSubitoEAttendi();
    const put = __calls.find(c => c.method === 'PUT' && c.url.includes('/articoli'));
    ok('P1 salvaSubitoEAttendi manda gli articoli', salvato && put && JSON.parse(put.body).articoli[0].pieghe === 3, __calls);
    ok('P1 dopo il flush niente in sospeso', salvataggioInSospeso() === false);
    const edM = document.getElementById('ed-margine');
    edM.value = '15'; edM.dispatchEvent(new Event('input'));
    ok('P1 testata modificata segnata', _testataModificata === true && salvataggioInSospeso());
    document.getElementById('ed-cliente').value = 'C';
    __calls.length = 0;
    await salvaSubitoEAttendi();
    ok('P1 testata salvata al flush', _testataModificata === false && __calls.some(c => c.method === 'PUT' && /\/api\/preventivi\/p1$/.test(c.url)));
    edM.value = '0';
    updateArt(0, 'area_dm2', '5', true);
    ok('P6 area modificata -> ristima pianificata', _realtimeStimaTimers.has(0));
    updateArtPerimMm(0, '2500');
    ok('P6 perimetro modificato -> ristima', _realtimeStimaTimers.has(0) && currentArticoli[0].perimetro_taglio_m === 2.5);
    currentArticoli[0].costo_base_override = 50;
    await stimaArt(0, {silent: true});
    ok('P4 la stima non cancella il prezzo manuale', currentArticoli[0].costo_base_override === 50);
    ok('P4 la stima aggiorna costo_base_stimato', currentArticoli[0].costo_base_stimato === 12.34);
    ok('P9 avvisi stima sull articolo e nello stato', (currentArticoli[0].avvisi_stima || []).length === 1
       && computeArticleStatus(currentArticoli[0]).reasons.some(r => r.includes('fuori tabella')));
    ripristinaStimaBase(0);
    ok('P4 reset esplicito del prezzo manuale', currentArticoli[0].costo_base_override === null);
    currentArticoli[0].perimetro_taglio_m = 1;
    currentArticoli[0].origine_campi.perimetro_taglio_m = 'cad'; currentArticoli[0].origine_campi.area_dm2 = 'cad';
    _ultimoIdxDxf = -1;
    const esito = applyDxfResultToUI({success:true, filename:'A.dxf', lavorazioni:{pieghe:7, saldatura_ml:1.5},
      geometria:{perimetro_taglio_m:2, area_dm2:4, n_forature:3}}, 'A.dxf');
    const A0 = currentArticoli[0];
    ok('M3 re-upload: correzione manuale conservata', esito === 'merged' && A0.pieghe === 3);
    ok('M3 re-upload: valori CAD e geometria aggiornati', A0.saldatura_ml === 1.5 && A0.perimetro_taglio_m === 2 && A0.area_dm2 === 4);
    ok('M3 stima sull articolo aggiornato', _ultimoIdxDxf === 0);
    applyDxfResultToUI({success:true, filename:'B.dxf', cartiglio:{materiale:'INOX_304', confidence:0.5},
      lavorazioni:{}, geometria:{perimetro_taglio_m:1, area_dm2:1}}, 'B.dxf');
    const B = currentArticoli[currentArticoli.length - 1];
    ok('M6 cartiglio incerto non riempie il materiale', B.materiale === '' && B._materiale_suggerito === 'INOX_304');
    currentArticoli = [{codice:'S', quantita:1, costo_base_stimato:100}];
    currentPreventivo.sconto_pct = 10;
    document.getElementById('ed-quantita').value = '1'; document.getElementById('ed-margine').value = '0';
    ok('B2 anteprima applica generali e sconto', Math.abs(calcolaTotaliAnteprima().totaleLotto - 97.2) < 0.001);
    currentPreventivo.sconto_pct = 0;
    ok('M7 apporto+pulizia cordoni assieme', Math.abs(computeAssiemePricing({codice_assieme:'Z', saldatura_mt:2, costo:10}).costoIntrinseco - 26) < 1e-9);
    ok('STEP qty non rimoltiplica', _costoTubolare({costo_materiale:30, costo_taglio_totale:5, qty:4}) === 35);
    _cfgRicetteDefault = [{materiale:'S235', gas:'N2', spessore_mm:1, velocita_mm_min:1000, pierce_time_s:0.5}];
    _cfgRicette = JSON.parse(JSON.stringify(_cfgRicetteDefault));
    const inp = document.createElement('input'); inp.dataset.ridx = '0'; inp.dataset.rfield = 'velocita_mm_min'; inp.value = '';
    _cfgRicetteEdit(inp);
    ok('M8 cella vuota -> fabbrica, mai 0', _cfgRicette[0].velocita_mm_min === 1000 && !_ricetteModificate());
    // salvataggio impostazioni: ricette non modificate non scritte, chiavi nascoste conservate
    _cfgCaricata = {laser: {euro_h_macchina: 75, chiave_ignota: 1, materiali: {}},
                    prev: {sconto_quantita: {'10': 3}, costo_movimentazione_kg: 0.05, margine_su_montaggio: true}};
    __calls.length = 0;
    await saveImpostazioni();
    const pc = __calls.find(c => c.method === 'PUT' && c.url.includes('/config'));
    const body = pc ? JSON.parse(pc.body) : {};
    ok('M8 ricette di fabbrica non salvate come personalizzate', pc && !('ricette_taglio' in body.laser_config), body.laser_config);
    ok('P8 resa nesting salvata', pc && body.laser_config.resa_nesting === 1);
    ok('M7 chiavi nascoste conservate', pc && body.preventivi_config.sconto_quantita['10'] === 3
       && body.preventivi_config.costo_movimentazione_kg === 0.05 && body.laser_config.chiave_ignota === 1, body);
    currentArticoli = [];
    currentPreventivo = {id:'p1', status:'BOZZA', cliente:'C', sconto_pct:0};
    await uploadXlsx(new Blob(['x']));
    const X1 = currentArticoli.find(x => x.codice === 'X1'), X2 = currentArticoli.find(x => x.codice === 'X2');
    ok('P5 costo Lantek nel prezzo manuale con fonte', X1 && X1.costo_base_override === 17.5 && X1.fonte_costo_base === 'lantek');
    ok('P5 materiale Lantek conservato', X1 && X2 && X1.materiale === 'S355' && X2.materiale === 'CU');
    ok('P5 import XLSX salvato', !!_salva.articoli.inSospeso);
    const tot = await _totaliServer();
    ok('B2 totale accettazione dal server', tot && tot.totale_lotto === 999.99);
    const f0 = window.fetch; window.fetch = async () => { throw new Error('rete giu'); };
    _prevCfg = null; await fetchPreventiviCfg(true);
    ok('B2 banner se i coefficienti non arrivano', !!document.getElementById('banner-cfg-errore'));
    window.fetch = f0;
  } catch (e) { R.push('KO  ECCEZIONE ' + e.name + ': ' + e.message); }
  const pre = document.createElement('pre'); pre.id = 'test-out'; pre.textContent = R.join('\n');
  document.body.appendChild(pre);
}, 300));
"""


def _trova_chrome():
    for p in (r'C:\Program Files\Google\Chrome\Application\chrome.exe',
              r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
              r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
              r'C:\Program Files\Microsoft\Edge\Application\msedge.exe'):
        if os.path.exists(p):
            return p
    return shutil.which('chrome') or shutil.which('google-chrome') or shutil.which('chromium')


def test_js_editor():
    print('\nJS) Logica dell\'editor preventivi (Chrome headless)')
    chrome = _trova_chrome()
    if not chrome:
        print('  Chrome/Edge non trovato: sezione saltata.')
        return
    import html as _html
    import re
    src = open(os.path.join(_APP, 'frontend', 'preventivi.html'), encoding='utf-8').read()
    src = src.replace('<head>', '<head><script>' + _STUB_JS + '</script>', 1)
    i = src.rfind('</body>')
    src = src[:i] + '<script>' + _TEST_JS + '</script>' + src[i:]
    cartella = tempfile.mkdtemp(prefix='test_cad_js_')
    try:
        pagina = os.path.join(cartella, 'h.html')
        with open(pagina, 'w', encoding='utf-8') as f:
            f.write(src)
        uscita = os.path.join(cartella, 'dump.txt')
        with open(uscita, 'w', encoding='utf-8') as fo:
            subprocess.run([chrome, '--headless=new', '--disable-gpu', '--no-sandbox',
                            '--user-data-dir=' + os.path.join(cartella, 'prof'),
                            '--allow-file-access-from-files', '--virtual-time-budget=20000',
                            '--dump-dom', 'file:///' + pagina.replace('\\', '/')],
                           stdout=fo, stderr=subprocess.DEVNULL, timeout=120)
        testo = open(uscita, encoding='utf-8', errors='replace').read()
        m = re.search(r'<pre id="test-out">(.*?)</pre>', testo, re.S)
        if not m:
            check('la pagina ha eseguito i test JS (nessun errore di sintassi)', False, testo[:300])
            return
        for riga in _html.unescape(m.group(1)).splitlines():
            check(riga[4:], riga.startswith('OK'), '')
    finally:
        shutil.rmtree(cartella, ignore_errors=True)


def main():
    try:
        test_materiali_e_ricette()
        test_resa_e_ricette_config()
        test_verifica()
        test_calcolo_assiemi()
        test_database_e_pdf()
        test_vari()
        test_js_editor()
    finally:
        try:
            _ENG.dispose()
            os.remove(_TMP)
            os.remove(_CFG)
        except OSError:
            pass
    print(f'\n{OK} controlli OK, {len(KO)} falliti')
    for k in KO:
        print('  KO:', k)
    return 1 if KO else 0


if __name__ == '__main__':
    sys.exit(main())
