"""Laser tubo: il materiale (barre) lo compra Stefano, il fornitore fa solo il taglio.

Stefano, 14/10/2026: "anche se il tubo va ordinato al laser tubi, solitamente
il materiale lo ordino io cosi' risparmio sul materiale e faccio fare solo la
lavorazione di taglio, percio' anche in caso di taglio laser devo ordinare il
materiale".

Esempio: DECA 26T30SA0233-00 x 3, i 5 tubi tagliati al laser tubo.

Copre (database temporaneo, Lantek simulato):
 A. le voci L vanno nelle barre con i tubi T dello stesso profilo + materiale:
    quadro 50x50 inox 12 pz -> 2 barre, rett. 100x50 inox 6 pz -> 1 barra
 B. materiale e taglio hanno stati distinti: ordino le barre, il taglio resta
    da ordinare (e viceversa); annulla rimette com'era
 C. chip / taglia entro: il taglio e' lavorazione esterna (+5) finche' non
    torna; il materiale da comprare conta come una voce in piu'
 D. "materiale fornito dal laser tubo": fuori dalle barre, non conta
 E. richiesta d'offerta: al laser tubo i disegni senza barre ("solo
    taglio"), al fornitore del materiale le barre senza disegni
 F. API /api/procura: filtro per stato sul materiale, conteggi
 G. voce L classificata senza profilo (come A 001221 BIS): da completare;
    scritto il profilo, entra nelle barre
 H. (server di prova 5057 con la copia) Playwright: "Materiale da comprare"
    e "Lavorazione al laser tubo" separati; ordino le barre, il taglio resta

python app/tests/test_barre_laser_tubo.py [url del server di prova]
"""
import io
import json
import os
import sys
import tempfile
import uuid
import zipfile

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)
os.environ['FERROTRACK_SKIP_DB_INIT'] = '1'
os.environ['FERROTRACK_ESEMPI_DIR'] = tempfile.mkdtemp(prefix='ft_lt_esempi_')
_CFG = os.path.join(tempfile.mkdtemp(prefix='ft_lt_cfg_'), 'app_config.json')
with open(_CFG, 'w', encoding='utf-8') as _f:
    json.dump({'azienda': {'nome': 'Carpenteria di prova'}}, _f)
os.environ['FERROTRACK_CONFIG'] = _CFG

import werkzeug  # noqa: E402
if not hasattr(werkzeug, '__version__'):
    werkzeug.__version__ = '3.0.0'

from sqlalchemy import create_engine  # noqa: E402

from backend import models  # noqa: E402
from backend.models import Base, User, Preventivo, DaProcurare  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_lt_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend import lantek as L  # noqa: E402
from backend import api_laser as AL  # noqa: E402
from backend import procura as PC  # noqa: E402
from backend.database import PreventivoManager  # noqa: E402
from backend.preventivi import tempi_laser as TL  # noqa: E402

_CARTELLE = tempfile.mkdtemp(prefix='ft_lt_')
A.UPLOAD_FOLDER = os.path.join(_CARTELLE, 'uploads')
A.DRAWINGS_FOLDER = os.path.join(A.UPLOAD_FOLDER, 'drawings')
os.makedirs(A.DRAWINGS_FOLDER, exist_ok=True)

TL._leggi_lantek = lambda codici: None
L.pezzi_in_lantek = lambda codici: {'disponibile': True, 'errore': None, 'pezzi': {
    c: {'esiste': False, 'codice_lantek': None, 'materiale': None, 'spessore': None, 'revisioni': []} for c in codici}}
L.clienti_lantek = lambda: ['DECA']
L.ordini_in_lantek = lambda commessa: {}
L.ordini_fatti_in_lantek = lambda commessa: {}
L.pezzi_dell_ordine = lambda commessa: {}
L.procesos_disponibile = lambda: True
L.materiali_lantek_noti = lambda: {'FERRO', 'INOX'}
L.xmlimporter_disponibile = lambda: True
L.importa_pezzi_dxf = lambda *a, **k: {'eseguito': False, 'errore': 'bloccato nel test'}
L.importa_xml = lambda *a, **k: {'eseguito': False, 'errore': 'bloccato nel test'}

BASE = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5057'
ASS = '26T30SA0233-00'
OK, KO = 0, []


def check(nome, cond, extra=''):
    global OK
    if cond:
        OK += 1
        print(f'  [OK] {nome}')
    else:
        KO.append(nome)
        print(f'  [KO] {nome}  {extra}')


def _tubo(L_, q, kgm, peso, mat, cod, profilo='Quadro 50x50 sp.3mm', tipo='SHS'):
    return {'codice_assieme': ASS, 'profilo': profilo, 'tipo': tipo, 'materiale': 'inox', 'materiale_testo': 'AISI304',
            'euro_kg': 3.3, 'kg_m': kgm, 'qty': q, 'lunghezza_m': L_ / 1000, 'lunghezza_pezzo_mm': L_,
            'peso_kg': peso, 'costo_materiale': mat, 'costo_taglio_totale': 2.0 * q, 'costo_esterno': 0,
            'n_tagli_dritti': 2 * q, 'n_tagli_obliqui': 0, 'origine': 'pezzo', 'codice_pezzo': cod,
            'posizione': cod, 'nota_esterno': 'Taglio laser tubo esterno'}


TUBI = [_tubo(581, 1, 4.435, 2.58, 8.5, '26CCSA0038-00'), _tubo(304, 1, 4.435, 1.35, 4.45, '26CCSA0041-00'),
        _tubo(595, 1, 6.93, 4.12, 13.61, '26CCSA0042-00', 'Rett. 100x50 sp.3mm', 'RHS'),
        _tubo(595, 1, 6.93, 4.12, 13.61, '26CCSA0040-00', 'Rett. 100x50 sp.3mm', 'RHS'),
        _tubo(895, 2, 4.435, 7.94, 26.2, '26CCSA0039-00')]


def nuovo_ordine(numero, articoli, tubolari=(), assiemi=()):
    pr = PreventivoManager.create('DECA S.r.l.', 'paolo', quantita=1, numero_ordine_cliente=numero)
    pid = pr['id'] if isinstance(pr, dict) else pr
    base = {'quantita': 1, 'materiale': 'INOX_304', 'spessore_mm': 3, 'costo_base_override': 5,
            'perimetro_taglio_m': 0.8, 'n_forature': 0, 'area_dm2': 3.0, 'bbox_w_mm': 200, 'bbox_h_mm': 150}
    PreventivoManager.replace_articoli(pid, [dict(base, **a) for a in articoli])
    if assiemi:
        assert PreventivoManager.replace_assiemi(pid, list(assiemi)).get('success')
    if tubolari:
        assert PreventivoManager.replace_tubolari(pid, list(tubolari)).get('success')
    s = models.SessionLocal()
    s.query(Preventivo).filter(Preventivo.id == pid).first().status = 'INVIATO'
    s.commit(); s.close()
    oid = PreventivoManager.accetta_e_crea_ordine(pid, 'paolo').get('order_id')
    A.OrderManager.smista(oid, True, user_id='laser')
    return oid, pid


def riep(c, oid):
    AL.invalida(oid)
    PC._SYNC['t'] = 0
    return ((c.get('/api/laser/ordini').get_json() or {}).get('ordini') or {}).get(oid) or {}


def gruppi_ordine(oid):
    return {g['profilo']: g for g in PC.gruppi_tubi(PC.elenco('tutti', ordine=oid))}


def test_backend():
    from tests.accesso_aiuto import postazioni, entra, modalita, persona, PIN_UFFICIO
    s = models.SessionLocal()
    s.add(User(id='paolo', name='Paolo Capo', role='Capo Officina', is_active=True, is_capo=True))
    s.commit(); s.close()
    modalita('protetto')
    postazioni()
    persona('elena', 'Elena Ufficio', pin=PIN_UFFICIO)
    laser = entra(A.app, 'laser')
    uff = entra(A.app, 'ufficio', pin=PIN_UFFICIO)

    print('A) Le barre comprendono il materiale dei laser tubo')
    oid, pid = nuovo_ordine('1221', [{'codice': '191700045-00', 'codice_assieme': ASS, 'quantita': 2}],
                            tubolari=TUBI, assiemi=[{'codice_assieme': ASS, 'qty': 3}])
    PC.sincronizza_righe(oid, A._distinta_da_preventivo(PreventivoManager.get(pid, include_children=True), {}))
    vv = PC.elenco('tutti', ordine=oid)
    lt = [v for v in vv if v['tipo'] == 'laser_tubo']
    check('5 righe tubo -> 4 voci laser tubo (i due 595 uguali sommati), 18 pz',
          len(lt) == 4 and sum(v['quantita'] for v in lt) == 18, [(v['codice'], v['quantita']) for v in lt])
    g = gruppi_ordine(oid)
    q, r = g.get('Tubo quadro 50x50 sp.3mm'), g.get('Tubo rett. 100x50 sp.3mm')
    check('quadro 50x50 inox: 12 pz, 8,025 m, 2 barre da 6 m',
          q and q['n_pezzi'] == 12 and abs(q['metri'] - 8.025) < 1e-6 and q['n_barre'] == 2, q and {k: q[k] for k in ('n_pezzi', 'metri', 'n_barre')})
    check('rett. 100x50 inox: 6 pz, 1 barra', r and r['n_pezzi'] == 6 and r['n_barre'] == 1)
    check('il gruppo sa che e\' materiale per il laser tubo', q and len(q['voci_laser_tubo']) == 3)
    check('ogni L ha il suo acquisto (materiale) da ordinare', all((v['acquisto'] or {}).get('stato') == 'da_ordinare' for v in lt))

    print('\nB) Materiale e taglio: stati distinti')
    ids_q = q['voci']
    cambi = PC.cambia_stato([], 'ordinato', fornitore='Inox Rossi', data_prevista='2026-10-20', chi='Elena',
                            ids_materiale=ids_q)
    vv = {v['id']: v for v in PC.elenco('tutti', ordine=oid)}
    check('barre ordinate a Inox Rossi', all(vv[i]['acquisto']['stato'] == 'ordinato'
                                             and vv[i]['acquisto']['fornitore'] == 'Inox Rossi' for i in ids_q))
    check('il taglio resta da ordinare', all(vv[i]['stato'] == 'da_ordinare' and not vv[i]['fornitore'] for i in ids_q))
    PC.ripristina([p for p, _d in cambi], 'Elena')
    vv = {v['id']: v for v in PC.elenco('tutti', ordine=oid)}
    check('annulla: materiale di nuovo da ordinare', all(vv[i]['acquisto']['stato'] == 'da_ordinare'
                                                         and not vv[i]['acquisto']['fornitore'] for i in ids_q))
    PC.cambia_stato([lt[0]['id']], 'ordinato', fornitore='Laser Tubi Bianchi', chi='Elena')
    v0 = next(v for v in PC.elenco('tutti', ordine=oid) if v['id'] == lt[0]['id'])
    check('taglio ordinato al laser tubo, materiale ancora da ordinare',
          v0['stato'] == 'ordinato' and v0['fornitore'] == 'Laser Tubi Bianchi' and v0['acquisto']['stato'] == 'da_ordinare')
    # stessa voce in tutte e due le liste: due cambi distinti, un annulla per tutti e due
    cambi = PC.cambia_stato([lt[0]['id']], 'arrivato', chi='Elena', ids_materiale=[lt[0]['id']])
    check('la stessa voce come taglio e come materiale: 2 cambi', len(cambi) == 2
          and {d['parte'] for _p, d in cambi} == {'voce', 'materiale'})
    PC.ripristina([p for p, _d in cambi], 'Elena')
    v0 = next(v for v in PC.elenco('tutti', ordine=oid) if v['id'] == lt[0]['id'])
    check('annulla di tutti e due', v0['stato'] == 'ordinato' and v0['acquisto']['stato'] == 'da_ordinare')

    print('\nC) Chip e taglia entro')
    d = riep(laser, oid)
    pc = d.get('procura') or {}
    check('chip: 4 tagli + 4 materiali = 8, di cui 4 materiale', pc.get('n') == 8 and pc.get('n_materiale') == 4, pc)
    check('laser tubo = lavorazione esterna nel taglia entro (+5)',
          'laser tubo dal fornitore' in (d.get('taglia_entro') or {}).get('motivo', ''), d.get('taglia_entro'))
    ids_lt = [v['id'] for v in lt]
    PC.cambia_stato(ids_lt, 'arrivato', chi='Elena')
    d = riep(laser, oid)
    check('tagli tornati: restano le 4 barre da comprare, niente piu\' lavorazione esterna',
          (d.get('procura') or {}).get('n') == 4
          and 'laser tubo dal fornitore' not in (d.get('taglia_entro') or {}).get('motivo', ''), (d.get('procura'), d.get('taglia_entro')))
    PC.cambia_stato(ids_lt, 'da_ordinare', chi='Elena')

    print('\nD) Materiale fornito dal laser tubo (caso raro)')
    r = uff.post(f"/api/procura/{lt[0]['id']}", json={'materiale_fornito': True})
    check('si segna dalla voce', r.status_code == 200 and (r.get_json() or {}).get('voce', {}).get('materiale_fornito') is True, r.get_json())
    g = gruppi_ordine(oid)
    tot = sum(x['n_pezzi'] for x in g.values())
    check('fuori dalle barre', tot == 18 - lt[0]['quantita'], tot)
    d = riep(laser, oid)
    check('il materiale non conta piu\' come voce', (d.get('procura') or {}).get('n_materiale') == 3, d.get('procura'))
    r = uff.post(f"/api/procura/{lt[0]['id']}", json={'materiale_fornito': 'si'})
    check('valore non booleano rifiutato', r.status_code == 400)
    uff.post(f"/api/procura/{lt[0]['id']}", json={'materiale_fornito': False})

    print('\nE) Richiesta d\'offerta')
    r = uff.post('/api/procura/richiesta-offerta', json={'ids': ids_lt})
    z = zipfile.ZipFile(io.BytesIO(r.data)) if r.status_code == 200 else None
    testo = _testo_pdf(z) if z else ''
    check('al laser tubo: "solo taglio", senza barre', z and 'Solo taglio laser tubo' in testo and 'in barre' not in testo, testo[:300])
    r = uff.post('/api/procura/richiesta-offerta', json={'ids_materiale': ids_lt})
    z = zipfile.ZipFile(io.BytesIO(r.data)) if r.status_code == 200 else None
    testo = _testo_pdf(z) if z else ''
    check('al fornitore del materiale: le barre, senza la tabella dei pezzi',
          z and 'Materiale: tubi e profili in barre' in testo and 'Lavorazione' not in testo
          and not [n for n in z.namelist() if n.startswith('disegni/')], (testo[:400], z and z.namelist()))

    print('\nF) API: filtri e conteggi')
    PC.cambia_stato([], 'ordinato', fornitore='Inox Rossi', chi='Elena', ids_materiale=ids_q)
    j = uff.get(f'/api/procura?stato=ordinato&ordine={oid}').get_json()
    check('fra gli ordinati: il gruppo quadro (barre ordinate)', [x['profilo'] for x in j['gruppi']] == ['Tubo quadro 50x50 sp.3mm'],
          [x['profilo'] for x in j['gruppi']])
    j = uff.get(f'/api/procura?stato=da_ordinare&ordine={oid}').get_json()
    check('fra i da ordinare: le voci col taglio da ordinare restano, il gruppo quadro no',
          len([v for v in j['voci'] if v['tipo'] == 'laser_tubo']) == 4
          and [x['profilo'] for x in j['gruppi']] == ['Tubo rett. 100x50 sp.3mm'], [x['profilo'] for x in j['gruppi']])
    c = j['conteggi']
    check('conteggi: 4 tagli + 1 materiale da ordinare, 3 materiali ordinati',
          c['da_ordinare'] == 5 and c['ordinato'] == 3, c)
    j = uff.post('/api/procura/stato', json={'ids_materiale': ids_q, 'stato': 'arrivato'}).get_json()
    check('API: segna arrivato il materiale', j.get('success') and j.get('n') == 3
          and all(p.get('parte') == 'materiale' for p in j['prima']), j)
    j2 = uff.post('/api/procura/annulla', json={'prima': j['prima']}).get_json()
    check('API: annulla', j2.get('success'))

    print('\nG) Voce L senza profilo (classificata col tasto N)')
    oid2, pid2 = nuovo_ordine('1221 BIS', [{'codice': '26CCSA0099-00', 'codice_assieme': ASS, 'quantita': 2}],
                              assiemi=[{'codice_assieme': ASS, 'qty': 3}])
    aid = next(a['id'] for a in PreventivoManager.get(pid2, include_children=True)['articoli'])
    v = PC.classifica(oid2, aid, tipo='laser_tubo', codice='26CCSA0099-00', quantita=6, materiale='INOX 304')
    check('senza profilo: da completare, fuori dalle barre', v['acquisto'] and not gruppi_ordine(oid2)
          and PC.da_completare(v) == 'manca profilo e lunghezza', v)
    PC.modifica(v['id'], {'profilo': 'Tubo quadro 50x50 sp.3mm', 'lunghezza_mm': 895}, 'Elena')
    g = gruppi_ordine(oid2)
    check('scritti profilo e lunghezza: 6 pz -> 1 barra', g.get('Tubo quadro 50x50 sp.3mm', {}).get('n_barre') == 1, g)


def _testo_pdf(z):
    try:
        import pymupdf
    except ImportError:
        return ''
    nome = next(n for n in z.namelist() if n.endswith('.pdf'))
    d = pymupdf.open(stream=z.read(nome), filetype='pdf')
    t = ' '.join(p.get_text() for p in d).replace('\n', ' ')
    d.close()
    return t


def test_browser():
    print('\nH) Pagina (server di prova con la copia)')
    import urllib.request
    try:
        urllib.request.urlopen(BASE + '/api/accesso/io', timeout=3)
    except Exception:
        print(f'  server di prova non raggiungibile su {BASE}: sezione saltata')
        return
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('  Playwright non installato: sezione saltata')
        return
    from tests.accesso_server import contesto_stazione, Sessione
    s = Sessione(BASE, 'ufficio')
    c, d = s.get('/api/procura?stato=tutti&tipo=laser_tubo&fresco=1')
    lt = [v for v in (d or {}).get('voci', []) if v['tipo'] == 'laser_tubo' and v.get('profilo')
          and (v.get('acquisto') or {}).get('stato') == 'da_ordinare' and v['stato'] == 'da_ordinare']
    if not lt:
        print('  nessun laser tubo da ordinare nella copia: sezione saltata')
        return
    ordine = lt[0]['order_id']
    prima = None
    with sync_playwright() as p:
        b = p.chromium.launch()
        try:
            ctx = contesto_stazione(b.new_context(viewport={'width': 1500, 'height': 1000}), BASE, 'ufficio')
            pg = ctx.new_page()
            err = []
            pg.on('pageerror', lambda e: err.append(str(e)))
            pg.goto(BASE + '/impiegata.html'); pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(1000)
            pg.evaluate('o => LZ.procura.apri({ordine: o})', ordine)
            pg.wait_for_selector('#lz-pc .lz-pc-mat', timeout=30000)
            mat = pg.locator('#lz-pc .lz-pc-mat').inner_text()
            lav = pg.locator('#lz-pc .lz-pc-lav').inner_text()
            check('"Materiale da comprare" con le barre', 'Materiale da comprare' in mat and 'da 6' in mat, mat[:300])
            check('"Lavorazione al laser tubo" a parte, coi codici', 'Lavorazione al laser tubo' in lav
                  and all(v['codice'] in lav for v in lt if v['order_id'] == ordine), lav[:300])
            pg.screenshot(path=os.path.join(tempfile.gettempdir(), 'ft_barre_laser_tubo.png'))
            g = pg.locator('#lz-pc tr.lz-pc-g').first
            g.locator('[data-pc-gruppo]').check()
            pg.evaluate('document.activeElement && document.activeElement.blur()')
            pg.keyboard.press('o')
            pg.wait_for_selector('#lz-pc [data-pc-forn]', timeout=10000)
            pg.fill('#lz-pc [data-pc-forn]', 'Inox di prova')
            pg.click('#lz-pc [data-pc="ordinato-ok"]')
            pg.locator('.ft-toast:has-text("Inox di prova")').last.wait_for(timeout=15000)
            # con la sessione del browser (entrare di nuovo con lo stesso PIN chiuderebbe l'altra)
            d = ctx.request.get(f'{BASE}/api/procura?stato=tutti&ordine={ordine}').json()
            vv = [v for v in d['voci'] if v['tipo'] == 'laser_tubo']
            ordinati = [v for v in vv if (v.get('acquisto') or {}).get('stato') == 'ordinato']
            check('barre del gruppo ordinate (materiale), il taglio resta da ordinare',
                  ordinati and all(v['acquisto']['fornitore'] == 'Inox di prova' and v['stato'] == 'da_ordinare' for v in ordinati),
                  [(v['codice'], v['stato'], v['acquisto']) for v in vv])
            prima = [{'id': v['id'], 'parte': 'materiale', 'stato': 'da_ordinare'} for v in ordinati]
            pg.wait_for_timeout(800)
            lav = pg.locator('#lz-pc .lz-pc-lav').inner_text()
            check('nella lavorazione: il materiale "Inox di prova", il taglio "da ordinare"',
                  'Inox di prova' in lav and 'da ordinare' in lav, lav[:400])
            check('nessun errore nella pagina', not err, err)
        finally:
            if prima:   # la copia torna com'era
                r = ctx.request.post(f'{BASE}/api/procura/annulla', data={'prima': prima})
                check('copia rimessa come prima', r.status == 200, r.status)
            b.close()


def main():
    test_backend()
    test_browser()
    print('\n' + '=' * 60)
    print(f'{OK} OK, {len(KO)} KO')
    if KO:
        print('KO: ' + ', '.join(KO))
    print('=' * 60)
    return 1 if KO else 0


if __name__ == '__main__':
    code = main()
    try:
        _ENG.dispose()
        os.remove(_TMP)
    except OSError:
        pass
    sys.exit(code)
