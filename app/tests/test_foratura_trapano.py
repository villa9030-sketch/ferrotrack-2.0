"""Test della FORATURA AL TRAPANO nel preventivo (15/10/2026).

Regola di Stefano: il laser taglia un foro tondo solo da 2/3 dello spessore in
su (arrotondato per difetto al mm); i fori piu' piccoli il laser li segna con
una croce e si fanno al trapano. Costo deciso da Stefano: 0,50 EUR a foro
(Impostazioni, "Foratura trapano").

Controlla che:
  - il motore tolga i fori al trapano da perimetro e inneschi
    (FORI_TRAPANO_NEL_COSTO = False) e li elenchi;
  - la riga "Foratura trapano" entri nel calcolo autorevole, nel salvataggio,
    nel PDF interno, nella stima dei tempi e nelle lavorazioni dell'ordine;
  - l'editor (Chrome headless) conti i fori, ricalcoli perimetro e inneschi
    quando cambia lo spessore e usi il prezzo delle Impostazioni.

Gira su DATABASE e CONFIG TEMPORANEI:
    python app/tests/test_foratura_trapano.py
"""
import json
import math
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
from backend.models import Base  # noqa: E402
from backend import models_ore  # noqa: E402,F401

_TMP = os.path.join(tempfile.gettempdir(), f'test_trapano_{uuid.uuid4().hex[:8]}.db')
_ENG = create_engine('sqlite:///' + _TMP.replace(chr(92), '/'),
                     connect_args={'check_same_thread': False})
models.engine = _ENG
models.SessionLocal.configure(bind=_ENG)
Base.metadata.create_all(bind=_ENG)

from backend.app import app, _preventivo_to_pdf_dati  # noqa: E402
from backend.database import PreventivoManager, ConfigManager  # noqa: E402
from backend.preventivi.calcolo import calcola, CAMPI_LAVORAZIONE  # noqa: E402
from backend.preventivi.validazione import valida_articoli  # noqa: E402
from backend.preventivi.delivery_estimator import stima_tempi_consegna  # noqa: E402
from backend.preventivi.laser_cost_estimator import DEFAULT_LASER_CONFIG  # noqa: E402
from backend.preventivi import dxf_batch_worker as W  # noqa: E402

PCFG = {'costo_generali_pct': 0, 'costo_filettatura': 0.8, 'costo_svasatura': 0.3,
        'costo_foratura_trapano': 0.5}
_CFG = os.path.join(tempfile.gettempdir(), f'test_trapano_cfg_{uuid.uuid4().hex[:8]}.json')
ConfigManager._CONFIG_PATH = _CFG
with open(_CFG, 'w', encoding='utf-8') as _f:
    json.dump({'preventivi_config': PCFG, 'laser_config': DEFAULT_LASER_CONFIG}, _f)

OK = 0
KO = 0


def check(nome, cond, extra=None):
    global OK, KO
    if cond:
        OK += 1
        print(f'  OK  {nome}')
    else:
        KO += 1
        print(f'  KO  {nome} :: {extra!r}'[:600])


def test_motore():
    print('\n1) Motore: i fori al trapano escono dal taglio')
    check('FORI_TRAPANO_NEL_COSTO spento (il costo della foratura esiste)', W.FORI_TRAPANO_NEL_COSTO is False)
    check('diametro minimo laser: 10 mm -> 6, 5 -> 3, 3 -> 2',
          (W.diametro_min_laser(10), W.diametro_min_laser(5), W.diametro_min_laser(3)) == (6, 3, 2))
    fori = [{'d_mm': 5.0, 'x': 0, 'y': 0, 'area_mm2': math.pi * 6.25, 'perim_mm': math.pi * 5},
            {'d_mm': 5.0, 'x': 9, 'y': 0, 'area_mm2': math.pi * 6.25, 'perim_mm': math.pi * 5},
            {'d_mm': 8.0, 'x': 20, 'y': 0, 'area_mm2': math.pi * 16, 'perim_mm': math.pi * 8}]
    geo = {'area_dm2': 2.0, 'perimetro_taglio_m': 1.0, 'n_pierce': 4, 'n_fori': 3, 'fori_tondi': fori}
    g = W.applica_fori_trapano(geo, {'spessore_mm': 10.0, 'confidence': 0.9})
    check('due Ø5 al trapano su 10 mm', len(g.get('fori_trapano') or []) == 2, g.get('fori_trapano'))
    check('perimetro senza i due fori', abs(g['perimetro_taglio_m'] - (1.0 - 2 * math.pi * 5 / 1000)) < 1e-4,
          g['perimetro_taglio_m'])
    check('inneschi e fori -2', g['n_pierce'] == 2 and g['n_fori'] == 1, (g['n_pierce'], g['n_fori']))
    check('area invariata', g['area_dm2'] == 2.0)
    g2 = W.applica_fori_trapano(geo, {'spessore_mm': 10.0, 'confidence': 0.5})
    check('spessore incerto: niente trapano, solo il dubbio',
          not g2.get('fori_trapano') and g2.get('fori_trapano_dubbio') and g2['perimetro_taglio_m'] == 1.0, g2)


def _art(**kw):
    a = {'codice': 'P1', 'quantita': 2, 'materiale': 'S235', 'spessore_mm': 10, 'area_dm2': 2,
         'perimetro_taglio_m': 0.97, 'n_forature': 2, 'costo_base_stimato': 10.0,
         'foratura_pz': 4, 'costo_foratura': 2.0, 'fori_tondi': [5.0, 5.0, 5.0, 5.0, 8.0],
         'fori_trapano': [5.0, 5.0, 5.0, 5.0]}
    a.update(kw)
    return a


def test_calcolo_e_db():
    print('\n2) Calcolo autorevole, salvataggio, PDF, tempi')
    check('costo_foratura tra le lavorazioni', 'costo_foratura' in CAMPI_LAVORAZIONE)
    p = {'quantita': 1, 'margine_pct': 0, 'articoli': [_art()]}
    r = calcola(p, {})
    check('costo pezzo = (base 10 + 4 fori x 0,50) x 2 pz = 24', abs(r['costo_pezzo'] - 24.0) < 1e-9, r['costo_pezzo'])
    check('validazione accetta foratura_pz e costo_foratura', not valida_articoli([_art()]))
    check('validazione rifiuta fori negativi', bool(valida_articoli([_art(foratura_pz=-1)])))

    pid = PreventivoManager.create('Cliente Prova', 'commerciale', quantita=1, margine_pct=0)['id']
    res = PreventivoManager.replace_articoli(pid, [_art()])
    check('articoli salvati', res.get('success'), res)
    g = PreventivoManager.get(pid, include_children=True)
    a = (g.get('articoli') or [{}])[0]
    check('foratura_pz e costo_foratura riletti', a.get('foratura_pz') == 4 and a.get('costo_foratura') == 2.0, a)
    check('fori tondi e al trapano riletti (per ricalcolare se cambia lo spessore)',
          a.get('fori_tondi') == [5.0, 5.0, 5.0, 5.0, 8.0] and a.get('fori_trapano') == [5.0] * 4,
          (a.get('fori_tondi'), a.get('fori_trapano')))
    dati = _preventivo_to_pdf_dati(g)
    check('PDF: costo foratura totale = 2 pz x 2,00', abs(dati.get('costo_foratura', 0) - 4.0) < 1e-9, dati.get('costo_foratura'))
    check('PDF: 8 fori al trapano', dati.get('n_fori_trapano') == 8, dati.get('n_fori_trapano'))
    art_pdf = dati['articoli'][0]
    check('PDF: prezzo del pezzo con la foratura', abs(art_pdf['costo'] - 12.0) < 1e-9, art_pdf['costo'])
    from tests.accesso_aiuto import stazione_pronta, modalita
    modalita('protetto')
    c = stazione_pronta(app, 'commerciale')
    for interno in ('', '?interno=1'):
        resp = c.get(f'/api/preventivi/{pid}/pdf{interno}')
        check(f'PDF {"interno" if interno else "cliente"} generato dal server',
              resp.status_code == 200 and resp.data[:4] == b'%PDF', resp.status_code)
    from backend.preventivi.pdf_exporter import PDFPreventivo
    out = os.path.join(tempfile.gettempdir(), f'test_trapano_{uuid.uuid4().hex[:6]}.pdf')
    try:
        PDFPreventivo({}).genera_pdf(out, dati, interno=True)
        check('PDF interno scritto', open(out, 'rb').read()[:4] == b'%PDF')
        try:
            import pdfplumber
        except ImportError:
            pdfplumber = None
        if pdfplumber:
            with pdfplumber.open(out) as pdf:
                testo = chr(10).join((pg.extract_text() or '') for pg in pdf.pages)
            check('PDF interno: riga "Foratura trapano (8 fori)"', 'Foratura trapano (8 fori)' in testo, testo[-800:])
    finally:
        if os.path.exists(out):
            os.remove(out)

    t = stima_tempi_consegna([{'foratura_pz': 10, 'quantita': 3}], {}, {}, {}, {'tempo_medio_foro_min': 0.5}, 2)
    check('tempi: 10 fori x 3 pz x 2 lotti x 0,5 min = 0,5 h', t['breakdown']['ore_foratura'] == 0.5, t)
    check('tempi: un setup per la foratura', t['breakdown']['ore_setup'] > 0, t)


def test_officina():
    print('\n3) Officina: la foratura porta il pezzo al reparto filettatura')
    from backend import tablet_officina as T
    from backend import taglia_entro as TE
    r = [x for x, _ in T.REPARTI]
    check('reparti invariati', 'filettatura' in r)
    parole = dict(T._PAROLE)
    check('"Foratura trapano" riconosciuta', any(p in T._piano('Foratura trapano (4 fori)') for p in parole['filettatura']))
    check('taglia_entro conta il trapano come filettatura', 'trapan' in open(TE.__file__, encoding='utf-8').read())


# ---------------------------------------------------------------------------
# Logica JS dell'editor (Chrome headless, se c'e')
# ---------------------------------------------------------------------------
_STUB_JS = r"""
localStorage.setItem('currentUser', JSON.stringify({id:'u1', name:'Test Uno', role:'Commerciale'}));
window.__calls = [];
window.CFG = {costo_filettatura:0.8, costo_svasatura:0.3};
window.fetch = async function (url, opts) {
  opts = opts || {};
  __calls.push({url: String(url), method: (opts.method || 'GET'), body: (typeof opts.body === 'string') ? opts.body : null});
  const J = (o, ok) => ({ ok: ok !== false, status: ok === false ? 500 : 200, json: async () => o,
                          text: async () => '', blob: async () => new Blob(), headers: {get: () => ''} });
  const u = String(url);
  if (u.indexOf('/api/preventivi/config') >= 0 && opts.method === 'PUT') return J({success:true, preventivi_config: CFG});
  if (u.indexOf('/api/preventivi/config') >= 0) return J({success:true, preventivi_config: CFG, laser_config:{}});
  if (u.indexOf('stima-base') >= 0) return J({success:true, stima:{base:10, warnings:[]}});
  if (opts.method === 'PUT') return J({success:true, preventivo:{id:'p1', status:'BOZZA', cliente:'C'}});
  return J({}, false);
};
"""

_TEST_JS = r"""
window.addEventListener('load', () => setTimeout(async () => {
  const R = [];
  const ok = (nome, cond, extra) => R.push((cond ? 'OK  ' : 'KO  ') + nome + (cond ? '' : ' :: ' + JSON.stringify(extra)));
  const vicino = (x, y) => Math.abs(x - y) < 1e-3;
  try {
    _prevCfg = CFG;
    let a = {foratura_pz: 4};
    recalcLavorazioniArt(a);
    ok('senza prezzo nelle Impostazioni: 0,50 a foro', a.costo_foratura === 2, a);
    _prevCfg = Object.assign({}, CFG, {costo_foratura_trapano: 0.7});
    recalcLavorazioniArt(a);
    ok('prezzo dalle Impostazioni', a.costo_foratura === 2.8, a);
    _prevCfg = Object.assign({}, CFG, {costo_foratura_trapano: 0.5});
    ok('la foratura entra nelle lavorazioni del pezzo', _lavArt({costo_foratura: 2, costo_piega: 1}) === 3);
    currentUser = {id:'u1'};
    currentPreventivo = {id:'p1', status:'BOZZA', cliente:'C', sconto_pct:0, quantita:1, margine_pct:0};
    currentAssiemi = []; currentTubolari = []; currentPiastre = [];
    currentArticoli = [];
    // import: il server ha gia' tolto i due Ø5 dal taglio (spessore 10 sicuro)
    const P0 = 1.0 - 2 * Math.PI * 5 / 1000;
    applyDxfResultToUI({success:true, filename:'T1.dxf', cartiglio:{materiale:'S235', confidence:0.9},
      spessore:{spessore_mm:10, confidence:0.9}, lavorazioni:{},
      geometria:{perimetro_taglio_m:P0, area_dm2:2, n_forature:2,
                 fori_tondi:[{d_mm:5},{d_mm:5},{d_mm:8}], fori_trapano:[{d_mm:5},{d_mm:5}]}}, 'T1.dxf');
    const T = currentArticoli[0];
    ok('import: 2 fori al trapano', T.foratura_pz === 2 && T.fori_trapano.length === 2, T);
    ok('import: perimetro del server non ritoccato', vicino(T.perimetro_taglio_m, P0) && T.n_forature === 2, T);
    ok('import: costo foratura 2 x 0,50', T.costo_foratura === 1, T.costo_foratura);
    // spessore 15 -> laser da Ø10: anche l'Ø8 va al trapano
    updateArt(0, 'spessore_mm', '15', true);
    ok('spessore 15: tre fori al trapano', T.foratura_pz === 3 && T.costo_foratura === 1.5, T);
    ok('spessore 15: perimetro senza Ø8', vicino(T.perimetro_taglio_m, P0 - Math.PI * 8 / 1000) && T.n_forature === 1, T);
    // spessore 3 -> laser da Ø2: tutti al laser
    updateArt(0, 'spessore_mm', '3', true);
    ok('spessore 3: nessun foro al trapano', T.foratura_pz === 0 && T.costo_foratura === 0, T);
    ok('spessore 3: perimetro con tutti i fori', vicino(T.perimetro_taglio_m, 1.0) && T.n_forature === 4, T);
    // correzione a mano del numero di fori: resta
    updateArt(0, 'foratura_pz', '6', true);
    ok('correzione a mano del numero di fori', T.foratura_pz === 6 && T.costo_foratura === 3, T);
    updateArt(0, 'spessore_mm', '10', true);
    ok('dopo la correzione a mano il numero non si tocca', T.foratura_pz === 6, T);
    // geometria nuova senza elenco fori (click sul pezzo, STEP...): tutto al laser
    const U = {perimetro_taglio_m: 1, n_forature: 3, foratura_pz: 2, fori_trapano: [5, 5], fori_tondi: [5, 5]};
    _foriDaGeometria(U, null);
    ok('geometria senza fori: niente trapano', U.foratura_pz === 0 && U.fori_trapano.length === 0 && U.fori_tondi === null, U);
    // conferma CAD: contorno con tutti i fori, spessore 10
    const V = {perimetro_taglio_m: 1, n_forature: 4, spessore_mm: 10, origine_campi: {perimetro_taglio_m: 'cad'}};
    _foriDaGeometria(V, [5, 5, 8], []);
    ok('conferma CAD: due Ø5 escono dal taglio', V.foratura_pz === 2 && vicino(V.perimetro_taglio_m, P0) && V.n_forature === 2, V);
    // perimetro scritto a mano: non si tocca
    const M = {perimetro_taglio_m: 1, n_forature: 4, spessore_mm: 10, origine_campi: {perimetro_taglio_m: 'manuale'}};
    _foriDaGeometria(M, [5, 5, 8], []);
    ok('perimetro a mano: nessun ricalcolo', M.perimetro_taglio_m === 1 && !M.foratura_pz, M);
    // Impostazioni: il prezzo a foro si salva
    _cfgCaricata = {laser: {materiali: {}}, prev: {}};
    document.getElementById('cfg-trapano-costo').value = '0.6';
    __calls.length = 0;
    await saveImpostazioni();
    const pc = __calls.find(c => c.method === 'PUT' && c.url.includes('/config'));
    const body = pc ? JSON.parse(pc.body) : {};
    ok('Impostazioni: costo foratura trapano salvato', pc && body.preventivi_config.costo_foratura_trapano === 0.6, body.preventivi_config);
  } catch (e) { R.push('KO  ECCEZIONE ' + e.name + ': ' + e.message + ' ' + (e.stack || '').slice(0, 300)); }
  const pre = document.createElement('pre'); pre.id = 'test-out'; pre.textContent = R.join('\n');
  document.body.appendChild(pre);
}, 300));
"""

_CAD_JS = r"""
window.addEventListener('load', () => setTimeout(() => {
  const R = [];
  const ok = (nome, cond, extra) => R.push((cond ? 'OK  ' : 'KO  ') + nome + (cond ? '' : ' :: ' + JSON.stringify(extra)));
  try {
    const cerchio = (cx, cy, r, n) => Array.from({length: n}, (_, i) => [cx + r * Math.cos(2 * Math.PI * i / n), cy + r * Math.sin(2 * Math.PI * i / n)]);
    const per = pts => pts.reduce((s, p, i) => { const q = pts[(i + 1) % pts.length]; return s + Math.hypot(q[0] - p[0], q[1] - p[1]); }, 0);
    const outer = [[0, 0], [100, 0], [100, 50], [0, 50]];
    const holes = [cerchio(20, 20, 2.5, 36), cerchio(50, 20, 4, 36), [[70, 10], [80, 10], [80, 20], [70, 20]]];
    const tot = per(outer) + holes.reduce((s, h) => s + per(h), 0);
    let d = foriTondiMm({outer_xy: outer, holes_xy: holes, perimetro_taglio_m: tot / 1000});
    ok('CAD: due fori tondi (Ø5, Ø8), il quadrato no', d.length === 2 && Math.abs(d[0] - 5) < 0.05 && Math.abs(d[1] - 8) < 0.05, d);
    d = foriTondiMm({outer_xy: outer, holes_xy: holes, perimetro_taglio_m: 2 * tot / 1000});
    ok('CAD: disegno in scala 1:2 -> diametri al vero', Math.abs(d[0] - 10) < 0.1, d);
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


def _esegui_pagina(nome_pagina, stub, test):
    chrome = _trova_chrome()
    if not chrome:
        print('  Chrome/Edge non trovato: sezione saltata.')
        return
    import html as _html
    import re
    src = open(os.path.join(_APP, 'frontend', nome_pagina), encoding='utf-8').read()
    src = src.replace('<head>', '<head><script>' + stub + '</script>', 1)
    i = src.rfind('</body>')
    src = src[:i] + '<script>' + test + '</script>' + src[i:]
    cartella = tempfile.mkdtemp(prefix='test_trapano_js_')
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
            check(f'{nome_pagina}: la pagina ha eseguito i test JS', False, testo[:300])
            return
        for riga in _html.unescape(m.group(1)).splitlines():
            check(riga[4:], riga.startswith('OK'), '')
    finally:
        shutil.rmtree(cartella, ignore_errors=True)


def main():
    try:
        test_motore()
        test_calcolo_e_db()
        test_officina()
        print('\n4) Editor preventivi (Chrome headless)')
        _esegui_pagina('preventivi.html', _STUB_JS, _TEST_JS)
        print('\n5) CAD: fori tondi dal contorno confermato')
        _esegui_pagina('dxf-editor.html', "window.fetch = async () => ({ok:false, json: async () => ({})});", _CAD_JS)
    finally:
        try:
            _ENG.dispose()
            os.remove(_TMP)
            os.remove(_CFG)
        except OSError:
            pass
    print(f'\n{OK} OK, {KO} KO')
    sys.exit(1 if KO else 0)


if __name__ == '__main__':
    main()
