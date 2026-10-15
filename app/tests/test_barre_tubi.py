"""Tubi per TUTTE le strutture e barre da ordinare (DECA 26T30SA0233-00 x 3, 14/10/2026).

"26T30SA0233-00 sono 3 strutture da fare ma il materiale da preparare mi dice
x1 pz e in piu' non vedo dove mi dice che servono X barre di tubo da ordinare
oltre alla lavorazione."

Il x1 era nella distinta INTERNA (PDF): la distinta di taglio contava 1 pezzo
per riga (niente qty, niente quantita' dell'assieme) e la BOM scriveva
"Pz/ass 1" anche per 2 pezzi. Prezzo e ordine erano gia' giusti (x3).

Copre:
 A. la regola: riga in assieme = per 1 assieme (tutte le origini), x qty
    dell'assieme; riga sciolta = gia' il totale
 B. prezzo (calcolo.py), ordine e barre dicono gli stessi pezzi e costi
 C. barre: 12 pz quadro 50x50 = 8,025 m -> 2 barre, 6 pz rett. 100x50 -> 1
    barra; kg/€ usati (in prezzo) e delle barre; pezzi oltre la barra
 D. PDF interno: distinta di taglio 18 pz, BOM "Pz/ass" 2, sezione barre
 E. (se gira il server di prova con la copia del preventivo vero, porta
    5057) Playwright: "1 per struttura x 3 = 3 pz" e le barre nell'assieme
    e nella scheda Prezzo

Database e cartelle temporanei. python app/tests/test_barre_tubi.py
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
from backend.models import Base, User  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_barre_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend.preventivi import barre_tubi as BT, calcolo, pdf_exporter  # noqa: E402

BASE = os.environ.get('FT_BASE', 'http://127.0.0.1:5057')
PREV_VERO = '6c060773-cc72-495f-9ca0-2e179d22494d'
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


def _tubo(L, q, kgm, peso, mat, taglio, origine='pezzo', **kw):
    t = {'codice_assieme': ASS, 'profilo': kw.pop('profilo', 'Quadro 50x50 sp.3mm'), 'tipo': 'SHS',
         'materiale': 'inox', 'materiale_da': 'a mano', 'euro_kg': 3.3, 'kg_m': kgm, 'qty': q,
         'lunghezza_m': L / 1000, 'lunghezza_pezzo_mm': L, 'peso_kg': peso, 'costo_materiale': mat,
         'costo_taglio_totale': taglio, 'costo_esterno': 0, 'n_tagli_dritti': 2 * q, 'n_tagli_obliqui': 0,
         'origine': origine, 'nota_esterno': 'Taglio laser tubo esterno'}
    t.update(kw)
    return t


# I 5 tubi del preventivo vero (per 1 assieme), come salvati da "È un tubo"
TUBI = [
    _tubo(581, 1, 4.435, 2.58, 8.5, 2.0, codice_pezzo='26CCSA0038-00'),
    _tubo(304, 1, 4.435, 1.35, 4.45, 2.0, codice_pezzo='26CCSA0041-00'),
    _tubo(595, 1, 6.93, 4.12, 13.61, 2.0, profilo='Rett. 100x50 sp.3mm', tipo='RHS', codice_pezzo='26CCSA0042-00'),
    _tubo(595, 1, 6.93, 4.12, 13.61, 2.0, profilo='Rett. 100x50 sp.3mm', tipo='RHS', codice_pezzo='26CCSA0040-00'),
    _tubo(895, 2, 4.435, 7.94, 26.2, 4.0, codice_pezzo='26CCSA0039-00'),
]
ASSIEMI = [{'codice_assieme': ASS, 'qty': 3, 'ore_montaggio': 1.5, 'costo': 75.0}]


def test_regola():
    print('A) La regola delle quantita\'')
    qa = BT.qta_assiemi(ASSIEMI)
    r1 = BT.riga_totale(TUBI[0], qa)
    check('"È un tubo" qty 1 in assieme x3: 1 per assieme, 3 pz', r1['pz_per_assieme'] == 1 and r1['pz_totali'] == 3, r1)
    r2 = BT.riga_totale(TUBI[4], qa)
    check('qty 2 x3 = 6 pz, peso e costo x3', r2['pz_totali'] == 6 and abs(r2['peso_kg_totale'] - 23.82) < 0.01
          and abs(r2['costo_totale'] - 90.6) < 0.01, r2)
    # "Leggi distinta": telaio x2 dentro UN assieme, riga x1 -> qty 2, sempre per 1 assieme
    dist = _tubo(895, 2, 4.435, 7.94, 26.2, 4.0, origine='distinta', distinta='26CCSA0039', posizione='1')
    check('riga "Leggi distinta" stessa regola (per 1 assieme)', BT.riga_totale(dist, qa)['pz_totali'] == 6)
    sciolta = dict(TUBI[0], codice_assieme=None, qty=4)
    rs = BT.riga_totale(sciolta, qa)
    check('riga sciolta: gia\' il totale (non x assiemi)', rs['pz_totali'] == 4 and rs['pz_per_assieme'] is None, rs)
    check('qty mancante = 1', BT.pezzi_riga({}) == 1 and BT.pezzi_riga({'qty': 0}) == 1)
    check('lunghezza dal lunghezza_m se manca quella del pezzo',
          BT.lunghezza_mm({'lunghezza_m': 0.7}) == 700.0)


def test_prezzo_ordine():
    print('\nB) Prezzo, ordine e barre dicono gli stessi pezzi')
    c = calcolo.calcola({'quantita': 1, 'margine_pct': 0, 'articoli': [], 'assiemi': ASSIEMI,
                         'tubolari': TUBI, 'piastre': []}, {'costo_generali_pct': 0})
    d = c['dettaglio_assiemi'][0]
    check('prezzo: tubi per 1 assieme 78,37 €', abs(d['accessori'] - 78.37) < 0.01, d)
    check('prezzo: assieme x3 (tubi contati 3 volte, non 1 ne\' 9)', abs(d['costo_totale'] - (75 + 78.37) * 3) < 0.02, d)
    qa = BT.qta_assiemi(ASSIEMI)
    tot = sum(BT.riga_totale(t, qa)['costo_totale'] for t in TUBI)
    check('somma righe x3 = 235,11 € = tubi nel prezzo', abs(tot - 235.11) < 0.01, tot)

    s = models.SessionLocal()
    s.add(User(id='prova-barre', name='Prova', role='Commerciale', is_active=True))
    s.commit(); s.close()
    pid = A.PreventivoManager.create('DECA', 'prova-barre', numero_ordine_cliente='T-BARRE')['id']
    A.PreventivoManager.replace_assiemi(pid, ASSIEMI)
    r = A.PreventivoManager.replace_tubolari(pid, TUBI)
    check('tubi salvati', r.get('success'), r)
    prev = A.PreventivoManager.get(pid)
    check('qty salvata e riletta', sorted(t.get('qty') for t in prev['tubolari']) == [1, 1, 1, 1, 2])
    righe = [x for x in A._distinta_da_preventivo(prev, {}) if x['tipo'] == 'tubolare']
    check('ordine: 18 pezzi di tubo (3+3+3+3+6)', sum(x['quantita'] for x in righe) == 18,
          [x['quantita'] for x in righe])
    return prev


def test_barre():
    print('\nC) Barre da ordinare')
    cfg = {'barra_mm': 6000.0, 'lama_mm': 3.0}
    d = BT.barre(TUBI, ASSIEMI, cfg)
    g = {x['profilo']: x for x in d['gruppi']}
    q, rt = g.get('Tubo quadro 50x50 sp.3mm'), g.get('Tubo rett. 100x50 sp.3mm')
    check('due gruppi (profilo + materiale)', q and rt and len(d['gruppi']) == 2, list(g))
    check('quadro 50x50: 12 pz, 8,025 m, 2 barre', q['n_pezzi'] == 12 and abs(q['metri'] - 8.025) < 1e-6
          and q['n_barre'] == 2, {k: q[k] for k in ('n_pezzi', 'metri', 'n_barre')})
    check('rett. 100x50: 6 pz, 3,57 m, 1 barra', rt['n_pezzi'] == 6 and abs(rt['metri'] - 3.57) < 1e-6
          and rt['n_barre'] == 1)
    check('piano: barra 1 = 6 x 895 + 581, avanzo 31 mm',
          [x['lunghezza_mm'] for x in q['piano'][0]['tagli']] == [895.0] * 6 + [581.0]
          and q['piano'][0]['sfrido_mm'] == 31.0, q['piano'][0])
    check('€ usati = costo materiale nel prezzo (117,45 €)', abs(q['eur_usati'] - 117.45) < 0.01, q['eur_usati'])
    check('kg barre = 12 m x 4,435 kg/m, € barre a 3,30 €/kg',
          abs(q['kg_barre'] - 53.22) < 0.01 and abs(q['eur_barre'] - 175.63) < 0.01, (q['kg_barre'], q['eur_barre']))
    check('sfrido 33%', abs(q['sfrido_pct'] - 33.1) < 0.1, q['sfrido_pct'])
    check('totali: 3 barre, 18 pz', d['totali']['n_barre'] == 3 and d['totali']['n_pezzi'] == 18, d['totali'])
    check('pezzi con taglio laser esterno contati anche nelle barre', q['pz_esterni'] == 12)
    # assieme x1: una barra di quadro basta
    d1 = BT.barre(TUBI, [dict(ASSIEMI[0], qty=1)], cfg)
    check('con 1 struttura: 4 pz, 1 barra di quadro',
          [x for x in d1['gruppi'] if x['profilo'].startswith('Tubo quadro')][0]['n_barre'] == 1)
    # solo un assieme
    altro = dict(TUBI[0], codice_assieme='ALTRO')
    d2 = BT.barre(TUBI + [altro], ASSIEMI + [{'codice_assieme': 'ALTRO', 'qty': 5}], cfg, solo_assieme=ASS)
    check('solo l\'assieme chiesto', d2['totali']['n_pezzi'] == 18, d2['totali'])
    # pezzo piu' lungo della barra
    lungo = dict(TUBI[0], lunghezza_pezzo_mm=6500, lunghezza_m=6.5, qty=1)
    d3 = BT.barre([lungo], ASSIEMI, cfg)
    check('pezzo oltre la barra: segnalato, non in una barra', d3['gruppi'][0]['n_barre'] == 0
          and len(d3['gruppi'][0]['troppo_lunghi']) == 3, d3['gruppi'][0])
    check('ferro e inox dello stesso profilo: barre separate',
          len(BT.barre([TUBI[0], dict(TUBI[0], materiale='acciaio')], ASSIEMI, cfg)['gruppi']) == 2)


def test_pdf(prev):
    print('\nD) PDF interno')
    dati = A._preventivo_to_pdf_dati(prev)
    exp = pdf_exporter.PDFPreventivo({})
    profili, grand = exp._group_tubolari(dati['tubolari_per_assieme'])
    check('distinta di taglio: 18 pezzi (prima 5)', grand['qty'] == 18, grand)
    q = [p for p in profili if p['profilo'].startswith('Quadro')][0]
    g895 = [g for g in q['groups'] if abs(g['len_m'] - 0.895) < 1e-6][0]
    check('895 mm: x 6, 23,82 kg, 90,60 €', g895['qty'] == 6 and abs(g895['peso_tot'] - 23.82) < 0.01
          and abs(g895['costo_tot'] - 90.6) < 0.01, g895)
    check('costo distinta = tubi nel prezzo (235,11 €)', abs(grand['cost'] - 235.11) < 0.01, grand['cost'])
    check('barre nel PDF: 3', (dati.get('barre_tubi') or {}).get('totali', {}).get('n_barre') == 3)
    out = os.path.join(tempfile.gettempdir(), f'test_barre_{uuid.uuid4().hex[:6]}.pdf')
    try:
        exp.genera_pdf(out, dati, interno=True)
        try:
            import pymupdf
        except ImportError:
            print('  pymupdf non installato: testo del PDF non controllato')
            return
        doc = pymupdf.open(out)
        testo = ' '.join(p.get_text() for p in doc).replace('\n', ' ')
        doc.close()
        check('PDF: "Distinta di Taglio Tubolari · 18 pezzi"', 'Distinta di Taglio Tubolari · 18 pezzi' in testo)
        check('PDF: "Barre da ordinare · 3 barre da 6 m"', 'Barre da ordinare · 3 barre da 6 m' in testo)
        i = testo.find('(0.90m)')
        check('PDF BOM: il tubo da 895 mm e\' "2" per assieme', i > 0 and testo[i:i + 20].split()[1] == '2',
              testo[i:i + 40])
    finally:
        try:
            os.remove(out)
        except OSError:
            pass


def test_browser():
    print('\nE) Pagina (server di prova con la copia del preventivo vero)')
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
    from tests.accesso_server import contesto_stazione
    with sync_playwright() as p:
        b = p.chromium.launch()
        try:
            ctx = contesto_stazione(b.new_context(viewport={'width': 1600, 'height': 1000}), BASE, 'commerciale')
            # niente scritture: la pagina puo' solo leggere (e calcolare le barre)
            ctx.route('**/*', lambda r: r.abort() if r.request.method in ('PUT', 'DELETE', 'PATCH') or (
                r.request.method == 'POST' and '/api/accesso' not in r.request.url
                and '/barre-tubi' not in r.request.url) else r.continue_())
            pg = ctx.new_page()
            errori = []
            pg.on('pageerror', lambda e: errori.append(str(e)))
            pg.goto(BASE + '/preventivi.html'); pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(1200)
            pg.evaluate('id => openPreventivo(id)', PREV_VERO); pg.wait_for_timeout(5000)
            if not pg.evaluate('() => !!(currentPreventivo && currentTubolari.length)'):
                print('  preventivo vero non nella copia: sezione saltata')
                return
            pg.evaluate('c => selectSplitAssieme(c)', ASS); pg.wait_for_timeout(4000)
            comp = pg.locator('#sa-comp-list').inner_text()
            check('assieme: "1 per struttura × 3 = 3 pz"', '1 per struttura × 3 = 3 pz' in comp, comp[:300])
            check('assieme: "2 per struttura × 3 = 6 pz"', '2 per struttura × 3 = 6 pz' in comp)
            barre = pg.locator('#sa-barre').inner_text()
            check('assieme: "Tubi per 3 strutture"', 'Tubi per 3 strutture' in barre, barre[:200])
            check('assieme: quadro 12 pz → 2 barre da 6 m', '12 pz, 8,03 m → 2 barre da 6 m' in barre, barre[:300])
            check('assieme: rett. 6 pz → 1 barra da 6 m', '6 pz, 3,57 m → 1 barra da 6 m' in barre)
            pg.locator('#sa-barre summary').first.click()
            check('piano di taglio a richiesta: 895 × 6 + 581', '895 × 6 + 581 mm' in pg.locator('#sa-barre').inner_text())
            rail = pg.locator('#wb-barre').inner_text()
            check('scheda Prezzo: "Barre da ordinare 3 barre · 18 pz"', '3 barre · 18 pz' in rail, rail[:200])
            check('nessun errore JavaScript', not errori, errori)
        finally:
            b.close()


def main():
    test_regola()
    prev = test_prezzo_ordine()
    test_barre()
    test_pdf(prev)
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
