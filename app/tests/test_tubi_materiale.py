"""Tubi prezzati per MATERIALE (DECA 26T30SA0233-00, 14/10/2026).

"In questo assieme i tubi sono in inox, dove vedo che mi calcola il costo
dell'inox?": prima ogni tubo era prezzato con UN solo €/kg (Impostazioni →
Tubolari, quello del ferro), anche i tubi AISI304 della distinta.

Copre:
 A. distinta: materiale per intero dalla riga ("AISI 304") e dal cartiglio
    ("Materiale Peso Kg" / "Aisi304 2.496")
 B. database: materiale, da dove e' letto, €/kg usato e base acciaio salvati
    e riletti; ordine e "Da procurare" tengono "AISI304"; PDF: inox e ferro
    dello stesso profilo non si sommano
 C. (se c'e' il pacchetto vero ls 1221) /distinte: i 5 tubi hanno AISI304
 JS la logica dell'editor in Chrome headless: materiale dal testo, peso con
    la densita', €/kg del materiale, cambio materiale, righe di prima non
    ricalcolate da sole, avvisi (materiale non letto, €/kg a 0)

Database e cartelle temporanei. python app/tests/test_tubi_materiale.py
"""
import html as _html
import os
import re
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

_TMP = os.path.join(tempfile.gettempdir(), f'test_tubimat_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'), connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

import importlib  # noqa: E402
A = importlib.import_module('backend.app')
from backend.preventivi import distinte_pdf as DP  # noqa: E402
from backend import procura  # noqa: E402

PACCHETTO = r'C:\Users\Stefano\Desktop\AUTOCAD\TAGLIO\DECA\ORDINI\ls 1221\ls 1221\26T30SA0233-00'

OK, KO = 0, []


def check(nome, cond, extra=''):
    global OK
    if cond:
        OK += 1
        print(f'  [OK] {nome}')
    else:
        KO.append(nome)
        print(f'  [KO] {nome}  {extra}')


def test_distinta():
    print('A) Materiale dalla distinta e dal cartiglio')
    testo = ('Num. art. Codice REV Descrizione Descrizione Aggiuntiva Materiale TrattamentoQtà\n'
             '1 80105L2CB0055 00 T.Quadro Aisi304 50x 50x 3 L. 581 AISI 304 1\n'
             'Materiale Peso Kg Grado di finitura Trattamento\n'
             'Aisi304 2.496\n')
    rr = DP._righe_testo(testo)
    check('riga dal testo: materiale per intero "AISI 304"', rr and rr[0]['materiale'] == 'AISI 304', rr)
    check('cartiglio: "Aisi304"', DP._materiale_cartiglio(testo) == 'Aisi304')
    check('cartiglio senza materiale: None', DP._materiale_cartiglio('Materiale Peso Kg\n2.496\n') is None)
    check('cartiglio S235JR', DP._materiale_cartiglio('Materiale Peso Kg Finitura\nS235JR 12,5\n') == 'S235JR')
    r = DP._riga_da_celle(['2', '26CCSA0039', '00', 'Longherone telaio inferiore MTR Lg 895', 'AISI304', 'Aisi304', '', '2'])
    check('riga a celle: materiale "Aisi304"', r and r['materiale'] == 'Aisi304', r)


def test_database_ordine_pdf():
    print('\nB) Salvataggio, ordine, Da procurare, PDF')
    s = models.SessionLocal()
    s.add(User(id='prova-mat', name='Prova', role='Commerciale', is_active=True))
    s.commit(); s.close()
    pv = A.PreventivoManager.create('DECA', 'prova-mat', numero_ordine_cliente='T-MAT')
    pid = pv['id']
    inox = {'codice_assieme': '26T30SA0233-00', 'profilo': 'Quadro 50x50 sp.3mm', 'tipo': 'SHS', 'materiale': 'inox',
            'materiale_da': 'distinta', 'materiale_testo': 'AISI304', 'euro_kg': 4.5, 'kg_m': 4.435,
            'kg_m_acciaio': 4.39, 'peso_kg_acciaio': 2.551, 'qty': 1, 'lunghezza_m': 0.581, 'lunghezza_pezzo_mm': 581,
            'peso_kg': 2.58, 'costo_materiale': 11.6, 'n_tagli_dritti': 2, 'n_tagli_obliqui': 0,
            'costo_taglio_totale': 2, 'origine': 'distinta', 'distinta': '26CCSA0038', 'posizione': '1'}
    ferro = dict(inox, materiale='acciaio', materiale_da='a mano', materiale_testo=None, euro_kg=1.5,
                 kg_m=4.39, peso_kg=2.55, costo_materiale=3.83, posizione='2', distinta='26CCSA0099')
    A.PreventivoManager.replace_assiemi(pid, [{'codice_assieme': '26T30SA0233-00', 'qty': 2}])
    r = A.PreventivoManager.replace_tubolari(pid, [inox, ferro])
    check('tubi salvati', r.get('success'), r)
    prev = A.PreventivoManager.get(pid)
    t = next((x for x in prev.get('tubolari') or [] if x.get('materiale') == 'inox'), {})
    check('riletti materiale, da dove, testo, €/kg, base acciaio',
          t.get('materiale_da') == 'distinta' and t.get('materiale_testo') == 'AISI304' and t.get('euro_kg') == 4.5
          and t.get('kg_m_acciaio') == 4.39 and t.get('peso_kg_acciaio') == 2.551 and t.get('costo_materiale') == 11.6, t)
    righe = A._distinta_da_preventivo(prev, {})
    rt = sorted([x for x in righe if x['tipo'] == 'tubolare'], key=lambda x: x['codice'])
    check('ordine: la riga tubo inox ha "AISI304"', rt and rt[0]['materiale'] == 'AISI304', rt)
    check('ordine: la riga ferro resta "acciaio"', len(rt) == 2 and rt[1]['materiale'] == 'acciaio', rt)
    voci = procura.voci_tubolari(righe)
    check('Da procurare: inox e ferro sono due voci, con il materiale',
          len(voci) == 2 and sorted(v['materiale'] for v in voci.values()) == ['AISI304', 'acciaio'], voci)
    dati = A._preventivo_to_pdf_dati(prev)
    tub = ((dati.get('tubolari_per_assieme') or {}).get('26T30SA0233-00') or {}).get('analisi', {}).get('tubi', [])
    nomi = sorted(x['profilo'] for x in tub)
    check('PDF distinta di taglio: inox separato dal ferro', nomi == ['Quadro 50x50 sp.3mm', 'Quadro 50x50 sp.3mm · AISI304'], nomi)


def test_pacchetto():
    print('\nC) Pacchetto vero ls 1221: materiale dei tubi letto')
    if not os.path.isdir(PACCHETTO):
        print('  (pacchetto non presente: saltato)')
        return
    copia = tempfile.mkdtemp(prefix='ft_tubimat_')
    try:
        for f in os.listdir(PACCHETTO):
            if f.lower().endswith('.pdf'):
                shutil.copy2(os.path.join(PACCHETTO, f), copia)
        D = DP.distinte_per_codici(copia, ['26T30SA0233-00'])
        for k in ('26CCSA0038', '26CCSA0039', '26CCSA0040', '26CCSA0041', '26CCSA0042'):
            d = D.get(k) or {}
            tubi = [x for x in d.get('righe') or [] if x.get('profilo')]
            check(f'{k}: tubo con materiale AISI 304, cartiglio Aisi304',
                  len(tubi) == 1 and 'AISI' in tubi[0]['materiale'].upper() and (d.get('materiale') or '').upper() == 'AISI304',
                  (tubi, d.get('materiale')))
    finally:
        shutil.rmtree(copia, ignore_errors=True)


# ---------------------------------------------------------------------------
# Logica JS (Chrome headless), come test_costi_cad.py
# ---------------------------------------------------------------------------
_STUB_JS = r"""
localStorage.setItem('currentUser', JSON.stringify({id:'u1', name:'Test Uno', role:'Commerciale'}));
window.fetch = async function () { return { ok: false, status: 500, json: async () => ({}), text: async () => '' }; };
"""

_TEST_JS = r"""
window.addEventListener('load', () => setTimeout(async () => {
  const R = [];
  const ok = (nome, cond, extra) => R.push((cond ? 'OK  ' : 'KO  ') + nome + (cond ? '' : ' :: ' + JSON.stringify(extra)));
  const vicino = (a, b) => Math.abs(a - b) < 0.011;
  try {
    const M = (...t) => (_tubMaterialeDaTesto(...t) || {}).chiave || null;
    ok('Aisi304 = inox', M('Aisi304') === 'inox');
    ok('AISI 316L = inox', M('AISI 316L') === 'inox' && _tubMaterialeDaTesto('AISI 316L').testo === 'AISI316L');
    ok('INOX_304 (materiale del pezzo) = inox', M('INOX_304') === 'inox');
    ok('S235JR = ferro', M('S235JR') === 'acciaio');
    ok('Fe360 = ferro', M('Fe360') === 'acciaio');
    ok('ALU_6082 = alluminio', M('ALU_6082') === 'alluminio');
    ok('Alluminio 6060 = alluminio', M('Alluminio 6060') === 'alluminio');
    ok('descrizione senza materiale = non letto', M('Sostegno per traversa alta telaio') === null);
    ok('"VALUTARE" non e\' alluminio', M('DA VALUTARE') === null);
    ok('prima il testo che lo dice', M('', 'T.Quadro Aisi304 50x 50x 3') === 'inox');

    _prevCfg = { tubolari_euro_kg: 1.5, tubolari_euro_kg_inox: 4.5, tubolari_euro_kg_alluminio: 0,
                 tubolari_taglio_dritto: 1, tubolari_taglio_obliquo: 2.5 };
    const T = _wbTariffeTubi();
    ok('tariffe per materiale', T.kg === 1.5 && T.mat.inox === 4.5 && T.mat.alluminio === 0, T);
    _prevCfg = { tubolari_euro_kg: 2 };
    ok('vecchia config (solo tubolari_euro_kg): ferro 2, inox 4,5 di partenza', _wbTariffeTubi().mat.acciaio === 2 && _wbTariffeTubi().mat.inox === 4.5);
    _prevCfg = { tubolari_euro_kg: 1.5, tubolari_euro_kg_inox: 4.5, tubolari_euro_kg_alluminio: 0,
                 tubolari_taglio_dritto: 1, tubolari_taglio_obliquo: 2.5 };

    const riga = { pos: '1', qta: 1, lunghezza_mm: 581, profilo: { nome: 'Quadro 50x50 sp.3mm', tipo: 'SHS', kg_m: 4.39 },
                   materiale: 'AISI 304', descrizione: 'T.Quadro Aisi304 50x 50x 3 L. 581 AISI 304' };
    const mat = _wbMaterialeRigaTubo(riga, { materiale: 'Aisi304' }, null, null);
    ok('materiale della riga letto dalla distinta', mat && mat.chiave === 'inox' && mat.da === 'distinta', mat);
    const x = _wbTuboDaRiga(riga, '26T30SA0233-00', '26CCSA0038', 1, T, mat);
    const pesoInox = 4.39 * 0.581 * 7.93 / 7.85;
    ok('tubo AISI304: peso con densita\' inox', vicino(x.peso_kg, pesoInox), x);
    ok('tubo AISI304: prezzato col €/kg inox', vicino(x.costo_materiale, pesoInox * 4.5) && x.euro_kg === 4.5 && x.materiale === 'inox', x);
    ok('formula visibile "Inox ... × 4,50 €/kg"', /^Inox AISI304 · 2,58 kg × 4,50 €\/kg = /.test(_tubFormula(x).testo) && !_tubFormula(x).avviso, _tubFormula(x));
    const prima = x.costo_materiale;
    _tubApplicaMateriale(x, 'acciaio', 'a mano', null, T);
    ok('cambio materiale -> ferro: peso e prezzo rifatti', vicino(x.peso_kg, 4.39 * 0.581) && vicino(x.costo_materiale, 4.39 * 0.581 * 1.5)
       && x.costo_materiale < prima && x.materiale_da === 'a mano', x);
    _tubApplicaMateriale(x, 'inox', 'a mano', null, T);
    ok('e di nuovo inox: stesso prezzo di prima', vicino(x.costo_materiale, prima), x);
    _tubApplicaMateriale(x, 'alluminio', 'a mano', null, T);
    ok('alluminio: peso con densita\' 2,70', vicino(x.peso_kg, 4.39 * 0.581 * 2.7 / 7.85), x);
    ok('alluminio a 0 €/kg: avviso', /a 0/.test(_tubFormula(x).avviso), _tubFormula(x));

    const nonLetto = _wbTuboDaRiga(Object.assign({}, riga, { materiale: '', descrizione: 'T.Quadro 50x 50x 3' }), 'A', 'S', 1, T,
                                   _wbMaterialeRigaTubo({ materiale: '', descrizione: 'T.Quadro 50x 50x 3' }, {}, null, null));
    ok('materiale non letto: prezzo del ferro + avviso', nonLetto.materiale === 'acciaio' && nonLetto.euro_kg === 1.5
       && /non letto/.test(_tubFormula(nonLetto).avviso), nonLetto);
    const daCartiglio = _wbMaterialeRigaTubo({ materiale: '', descrizione: 'T.Quadro 50x 50x 3' }, { materiale: 'Aisi304' }, null, null);
    ok('ripiego sul cartiglio del disegno', daCartiglio && daCartiglio.chiave === 'inox' && daCartiglio.da === 'disegno', daCartiglio);
    const daAssieme = _wbMaterialeRigaTubo({ descrizione: 'T.Quadro 50x 50x 3' }, {}, { materiale: '', descrizione_agg: 'MTR - Lg 595 - AISI304' }, null);
    ok('ripiego sulla riga dell\'assieme', daAssieme && daAssieme.chiave === 'inox' && daAssieme.da === 'assieme', daAssieme);

    // riga di prima (salvata senza materiale): il prezzo salvato resta, con l'avviso
    const vecchia = { profilo: 'Quadro 50x50 sp.3mm', materiale: 'acciaio', peso_kg: 2.55, costo_materiale: 3.83, kg_m: 4.39, qty: 1 };
    const fv = _tubFormula(vecchia);
    ok('riga di prima: costo salvato, avviso "rileggi la distinta"', vecchia.costo_materiale === 3.83 && /rileggi/.test(fv.avviso) && /3,83/.test(fv.testo), fv);
    currentTubolari = [vecchia]; currentPreventivo = { id: 'p1', status: 'BOZZA' };
    cambiaMaterialeTub(0, 'inox');
    ok('riga di prima -> inox dalla tabella: ricalcolata', vicino(vecchia.peso_kg, 2.55 * 7.93 / 7.85)
       && vicino(vecchia.costo_materiale, 2.55 * 7.93 / 7.85 * 4.5) && vecchia.kg_m_acciaio === 4.39, vecchia);
    // riga STEP: peso dal 3D, materiale non detto
    const step = { profilo: 'RHS 150x50x3', materiale: 'acciaio', materiale_da: 'step', peso_kg: 5.42, peso_kg_acciaio: 5.42,
                   euro_kg: 1.5, costo_materiale: 8.13, qty: 1 };
    ok('riga STEP: materiale da confermare', !_tubMaterialeNoto(step) && !!_tubFormula(step).avviso);
    currentTubolari = [step];
    cambiaMaterialeTub(0, 'inox');
    ok('riga STEP -> inox: prezzo inox', vicino(step.costo_materiale, 5.42 * 7.93 / 7.85 * 4.5) && _tubMaterialeNoto(step), step);
    ok('_costoTubolare somma materiale + tagli + esterna', _costoTubolare({ costo_materiale: 10, costo_taglio_totale: 2, costo_esterno: 3 }) === 15);
  } catch (e) { R.push('KO  ECCEZIONE ' + e.name + ': ' + e.message + ' ' + (e.stack || '').split('\n')[1]); }
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


def test_js():
    print('\nJS) Editor: materiale e prezzo dei tubi (Chrome headless)')
    chrome = _trova_chrome()
    if not chrome:
        print('  Chrome/Edge non trovato: sezione saltata.')
        return
    src = open(os.path.join(_APP, 'frontend', 'preventivi.html'), encoding='utf-8').read()
    src = src.replace('<head>', '<head><script>' + _STUB_JS + '</script>', 1)
    i = src.rfind('</body>')
    src = src[:i] + '<script>' + _TEST_JS + '</script>' + src[i:]
    cartella = tempfile.mkdtemp(prefix='test_tubimat_js_')
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
    test_distinta()
    test_database_ordine_pdf()
    test_pacchetto()
    test_js()
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
