"""Test nel BROWSER delle CONFERME del pezzo (preventivatore, 14/10/2026).

Il problema, dalle parole di chi lo usa: "ne faccio una, poi mi riappare il
pezzo chiedendomene un'altra... ci sono 5 conferme in un disegno, ne faccio
una e due spariscono". Riprodotto sui disegni veri del preventivo DECA 1153:
 - "Usa 4 mm" su "Spessore 5 o 4 mm?" faceva comparire DOPO "Peso diverso dal
   cartiglio" e "Contorno da verificare", senza dire perche';
 - "Ho verificato" sul peso faceva sparire anche "Contorno da verificare";
 - alla riapertura "Materiale/Spessore da confermare" sparivano senza risposta;
 - un ricalcolo (taglio, avvisi della stima) rimetteva domande gia' chiuse.

Qui, con pezzi costruiti apposta (niente file di disegno), si prova che:
 1. le domande del pezzo stanno tutte in un pannello, col conteggio
 2. una risposta che ne chiude un'altra la lascia scritta "risolta da …"
 3. una domanda nata da una risposta dice perche' ("è cambiato lo spessore")
 4. "Spessore 5 o 4?" dice prima cosa succede al peso con l'una o l'altra
 5. le domande sul cartiglio incerto sopravvivono a salvataggio e riapertura
 6. "Conferma tutto" applica le proposte; dopo la riapertura restano chiuse
 7. un ricalcolo del taglio non riapre "Ho verificato" sul peso; un cambio
    di spessore si', e dice perche'
 8. un avviso della stima chiuso non torna quando la stima lo riscrive
 9. mentre i controlli in sottofondo girano il pezzo dice "controllo in
    corso" invece di mostrare domande a meta'
10. il conteggio del preventivo: "Conferme: N da fare in M pezzi"
11. (15/10) a pagina ferma il pannello resta fermo: prima la verifica in
    sottofondo si riprogrammava a ogni ridisegno e ridisegnava, ogni secondo,
    e le domande "riapparivano" (45 ridisegni in 40 s sul preventivo 90619ad6)
12. (15/10) la conferma a mano nel CAD chiude le domande sul contorno
    ("risolta da «Confermato a mano nel CAD»") e la correzione automatica col
    peso non torna piu', neanche dopo un cambio di spessore o la riapertura

Serve un server su una COPIA del database (tools/server_prova.py):
    python app/tests/test_preventivi_conferme_browser.py [url]
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
_QUI = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.dirname(_QUI)
if _APP not in sys.path:
    sys.path.insert(0, _APP)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print('Playwright non installato: test saltato.')
    sys.exit(0)

from tests.accesso_server import contesto_stazione  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:5056'
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


# Pezzi costruiti come li lascia l'import: verifica gia' fatta (peso del
# cartiglio), cosi' non si chiede nulla al server per file che non esistono.
PEZZI = """(V) => {
  const ver = (cod, kg) => ({ versione: V, _chiave: cod + '.dxf|' + cod, peso_cartiglio: kg ? { peso_kg: kg, confidence: 0.9, testo: String(kg) } : null, step: null });
  const base = (cod) => ({ codice: cod, quantita: 1, materiale: 'S235', dxf_filename: cod + '.dxf', costo_base_stimato: 40,
                           perimetro_taglio_m: 1.2, n_forature: 2, pieghe: 0, bbox_w_mm: 300, bbox_h_mm: 200 });
  return [
    // A: lo spessore 5 torna col peso del cartiglio, il 4 no
    Object.assign(base('PZ-A'), { spessore_mm: 5, area_dm2: 2.8322, dxf_confidence: 0.65, verifica: ver('PZ-A', 1.11),
      avvisi_spessore: ['Spessore discordante: 5.0 mm (testo) vs 4.0 mm (peso_area)'] }),
    // B: contorno incerto e peso diverso (non grave)
    Object.assign(base('PZ-B'), { spessore_mm: 5, area_dm2: 1.96, dxf_confidence: 0.65, verifica: ver('PZ-B', 0.62) }),
    // C: materiale e spessore letti dal cartiglio con poca sicurezza
    Object.assign(base('PZ-C'), { spessore_mm: 3, area_dm2: 1.0, dxf_confidence: 0.95, verifica: ver('PZ-C', null),
      _materiale_conf: 0.5, _spessore_conf: 0.5 }),
    // E: contorno corretto in automatico col peso, da confermare
    Object.assign(base('PZ-E'), { spessore_mm: 3, area_dm2: 4.0, dxf_confidence: 0.4, verifica: ver('PZ-E', 0.2),
      contorno_auto: { stato: 'da_confermare', chiave: 'PZ-E.dxf|3|S235', scarto: 0.02,
                       prima: { area_dm2: 0.8, perimetro_taglio_m: 0.5, n_forature: 0, bbox_w_mm: 100, bbox_h_mm: 80, geometry_source: null, confermato: false } } }),
    // D: avviso della stima
    Object.assign(base('PZ-D'), { spessore_mm: 2, area_dm2: 1.0, dxf_confidence: 0.95, verifica: ver('PZ-D', null),
      avvisi_stima: ['Spessore 2.0 mm fuori tabella: velocità del più vicino'] }),
  ];
}"""

PANNELLO = """() => [...document.querySelectorAll('#wb-pc-alerts .pa')].map(d => ({
  stato: d.classList.contains('done') ? 'chiusa' : (d.classList.contains('info') ? 'info' : 'aperta'),
  k: d.dataset.k || '', t: (d.querySelector('b') || {}).textContent || '',
  s: (d.querySelector('.pa-main span') || {}).textContent || '',
  tag: (d.querySelector('.pa-tag') || {}).textContent || '' }))"""


def main():
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(BASE + '/api/health', timeout=4) as r:
            salute = json.load(r) or {}
    except Exception as e:
        print(f'Server non raggiungibile su {BASE}: {e}\nAvvia un\'istanza di prova e riesegui. Test saltato.')
        return 0
    if not salute.get('istanza_di_prova'):
        print(f'{BASE} non è un\'istanza di prova (database vero): il test crea e cancella un preventivo. Saltato.')
        return 0
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = contesto_stazione(b.new_context(viewport={'width': 1500, 'height': 950}), BASE, 'commerciale')
        pg = ctx.new_page()
        errori_js = []
        pg.on('pageerror', lambda e: errori_js.append(str(e)))
        pg.on('dialog', lambda d: d.accept())
        pg.goto(BASE + '/preventivi.html'); pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(1200)
        pg.evaluate('newPreventivo()'); pg.wait_for_timeout(500)
        pg.fill('#ed-cliente', 'PROVA CONFERME'); pg.evaluate('wbNewEmpty()'); pg.wait_for_timeout(2000)
        pid = pg.evaluate('() => currentPreventivo && currentPreventivo.id')
        check('preventivo di prova creato', bool(pid))
        pg.evaluate(f'V => {{ currentArticoli = ({PEZZI})(V); renderSplitTree(); recalcTotali(); }}',
                    pg.evaluate('() => _WB_VERIFICA_VER'))

        def vai(cod):
            pg.evaluate("() => document.querySelectorAll('.modal-overlay.active').forEach(o => o.classList.remove('active'))")
            pg.evaluate("c => wbVai(currentArticoli.findIndex(a => a.codice === c))", cod)
            pg.wait_for_timeout(700)
            return pg.evaluate(PANNELLO)

        def clic(cod, k, bottone):
            vai(cod)
            pg.locator(f'#wb-pc-alerts .pa[data-k="{k}"] button', has_text=bottone).first.click()
            pg.wait_for_timeout(1500)
            return vai(cod)

        def per_k(pan, k):
            return next((x for x in pan if x['k'] == k), None)

        # ── 1 + 4: pannello unico, conseguenza sul peso scritta prima ──
        pan = vai('PZ-A')
        sp = per_k(pan, 'spessore')
        check('A: domanda sullo spessore aperta', sp and sp['stato'] == 'aperta', pan)
        check('A: dice prima cosa succede al peso', sp and 'con 4 mm' in sp['s'] and 'torna col cartiglio' in sp['s'], sp)
        testa = pg.evaluate("() => (document.querySelector('#wb-pc-alerts .pa-head') || {}).textContent || ''")
        check('A: intestazione "Conferme di questo disegno" col conteggio', 'Conferme di questo disegno' in testa and 'da fare' in testa, testa)

        # ── 3: la risposta che crea domande dice perche' ──
        pan = clic('PZ-A', 'spessore', 'Usa 4')
        sp = per_k(pan, 'spessore')
        check('A: "Usa 4 mm" resta scritto come risposta', sp and sp['stato'] == 'chiusa' and 'Usa 4' in sp['s'], pan)
        nuove = [x for x in pan if x['stato'] == 'aperta']
        check('A: le domande nate dalla risposta ci sono', len(nuove) >= 1, pan)
        check('A: e dicono perché (è cambiato lo spessore)', nuove and all('spessore' in x['tag'] for x in nuove), nuove)

        # ── 2: una risposta che ne chiude un'altra non la fa sparire ──
        pan = vai('PZ-B')
        aperte_b = [x['k'] for x in pan if x['stato'] == 'aperta']
        check('B: contorno e peso aperti insieme', 'contorno_da_verificare' in aperte_b and 'peso_diverso_dal_cartiglio' in aperte_b, aperte_b)
        pan = clic('PZ-B', 'peso_diverso_dal_cartiglio', 'Ho verificato')
        co = per_k(pan, 'contorno_da_verificare')
        check('B: il contorno NON sparisce: "risolta da «Ho verificato»"',
              co and co['stato'] == 'chiusa' and 'Ho verificato' in co['s'], pan)
        check('B: niente piu\' da fare sul pezzo', not [x for x in pan if x['stato'] == 'aperta'], pan)

        # ── 7: ricalcolo del taglio non riapre; cambio di spessore si' ──
        pg.evaluate("() => { const a = currentArticoli.find(x => x.codice === 'PZ-B'); a.perimetro_taglio_m += 0.25; a.n_forature += 3; }")
        pan = vai('PZ-B')
        check('B: ricalcolo del taglio: il peso resta verificato', not [x for x in pan if x['stato'] == 'aperta'], pan)

        # ── 8: avviso della stima chiuso non torna ──
        pan = clic('PZ-D', [x for x in vai('PZ-D') if x['stato'] == 'aperta'][0]['k'], 'Ho verificato')
        pg.evaluate("() => { const a = currentArticoli.find(x => x.codice === 'PZ-D'); a.avvisi_stima = ['Spessore 2.0 mm fuori tabella: velocità del più vicino']; }")
        pan = vai('PZ-D')
        check('D: l\'avviso riscritto dalla stima non torna', not [x for x in pan if x['stato'] == 'aperta'], pan)

        # ── 5 + 10: salvataggio, riapertura, conteggio ──
        riga = pg.evaluate("() => (document.getElementById('wb-pl-conf') || {}).textContent || ''")
        check('conteggio del preventivo in vista', 'Conferme:' in riga and 'da fare in' in riga, riga)
        pg.evaluate('salvaSubitoEAttendi()'); pg.wait_for_timeout(2500)
        pg.reload(); pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(1200)
        pg.evaluate('id => openPreventivo(id)', pid); pg.wait_for_timeout(5000)
        pan = vai('PZ-C')
        aperte_c = [x['k'] for x in pan if x['stato'] == 'aperta']
        check('C: dopo la riapertura materiale e spessore incerti ci sono ancora',
              'materiale_conf' in aperte_c and 'spessore' in aperte_c, pan)
        pan = vai('PZ-B')
        check('B: dopo la riapertura il pezzo non torna a chiedere', not [x for x in pan if x['stato'] == 'aperta'], pan)
        co = per_k(pan, 'contorno_da_verificare')
        check('B: e "risolta da …" e\' ancora scritto', co and 'Ho verificato' in co['s'], pan)
        pan = vai('PZ-A')
        sp = per_k(pan, 'spessore')
        check('A: la risposta allo spessore resta dopo la riapertura', sp and sp['stato'] == 'chiusa', pan)

        # ── 6: Conferma tutto ──
        vai('PZ-C')
        pg.locator('#wb-pc-alerts .pa-head button', has_text='Conferma tutto').first.click()
        pg.wait_for_timeout(2000)
        pan = vai('PZ-C')
        check('C: "Conferma tutto" chiude materiale e spessore', not [x for x in pan if x['stato'] == 'aperta'], pan)
        check('C: le due risposte sono scritte', len([x for x in pan if x['stato'] == 'chiusa']) >= 2, pan)
        pg.evaluate('salvaSubitoEAttendi()'); pg.wait_for_timeout(2500)
        pg.reload(); pg.wait_for_load_state('networkidle'); pg.wait_for_timeout(1200)
        pg.evaluate('id => openPreventivo(id)', pid); pg.wait_for_timeout(5000)
        pan = vai('PZ-C')
        check('C: dopo la riapertura le conferme restano fatte', not [x for x in pan if x['stato'] == 'aperta'], pan)

        # ── 7 (seconda parte): cambio di spessore → riaperta, col perche' ──
        pg.evaluate("() => { const a = currentArticoli.find(x => x.codice === 'PZ-B'); a.spessore_mm = 3; }")
        pan = vai('PZ-B')
        rip = [x for x in pan if x['stato'] == 'aperta']
        check('B: cambiato lo spessore, il peso si richiede', any(x['k'] == 'peso_diverso_dal_cartiglio' for x in rip), pan)
        check('B: e dice "riaperta perché è cambiato lo spessore"',
              any('Riaperta' in x['tag'] and 'spessore' in x['tag'] for x in rip), rip)

        # ── 9: controllo in corso ──
        pg.evaluate("""() => { const a = currentArticoli.find(x => x.codice === 'PZ-D');
                               a.verifica = null; _wbVerAttesa = true; }""")
        pan = vai('PZ-D')
        testo = pg.evaluate("() => document.getElementById('wb-pc-alerts').textContent")
        check('D: controllo in corso: niente domande a meta\'', 'controllo in corso' in testo
              and not [x for x in pan if x['stato'] == 'aperta'], testo[:200])
        pg.evaluate('() => { _wbVerAttesa = false; }')

        # ── 12: conferma a mano nel CAD ──
        pan = vai('PZ-E')
        check('E: prima della conferma nel CAD chiede del contorno automatico',
              any(x['stato'] == 'aperta' and x['k'] == 'contorno_corretto_in_automatico' for x in pan), pan)
        pg.evaluate("""() => { const a = currentArticoli.find(x => x.codice === 'PZ-E');
          window.postMessage({ type: 'cad-confirm', payload: { preventivo_id: currentPreventivo.id, articolo_id: a.id || undefined,
            dxf_filename: a.dxf_filename, area_dm2: 3.5, perimetro_taglio_m: 1.1, n_forature: 1, bbox_width_mm: 300, bbox_height_mm: 200,
            materiale: 'S235', spessore_mm: 3, geometry_source: 'manual-click' } }, '*'); }""")
        pg.wait_for_timeout(3000)
        pan = vai('PZ-E')
        aperte_e = [x for x in pan if x['stato'] == 'aperta' and x['k'] in
                    ('contorno_corretto_in_automatico', 'contorno_da_verificare', 'peso_diverso_dal_cartiglio', 'ingombro_senza_riscontro_nelle_quote')]
        check('E: confermato nel CAD, nessuna domanda sul contorno', not aperte_e, pan)
        ca = per_k(pan, 'contorno_corretto_in_automatico')
        check('E: la domanda resta scritta "risolta da «Confermato a mano nel CAD»"', ca and 'CAD' in ca['s'], pan)
        corregge = pg.evaluate("""() => { const a = currentArticoli.find(x => x.codice === 'PZ-E');
          a.spessore_mm = 4; const r = _wbDaCorreggereAuto(a); a.spessore_mm = 3; return r; }""")
        check('E: niente correzione automatica dopo la conferma nel CAD (anche cambiando spessore)', corregge is False)

        # ── 11: pagina ferma, pannello fermo ──
        vai('PZ-B'); pg.wait_for_timeout(3000)
        pg.evaluate("""() => { window._idle = { sync: 0, cambi: 0 }; const o = wbPieceSync;
          wbPieceSync = function () { _idle.sync++; return o.apply(this, arguments); };
          const box = document.getElementById('wb-pc-alerts');
          new MutationObserver(() => { _idle.cambi++; }).observe(box, { childList: true, subtree: true }); }""")
        pg.wait_for_timeout(30000)
        idle = pg.evaluate('() => _idle')
        check('pagina ferma 30 s: il pannello non si ridisegna', idle['sync'] <= 1 and idle['cambi'] == 0, idle)

        check('nessun errore JavaScript', not errori_js, errori_js[:3])
        # pulizia: il preventivo di prova si toglie
        try:
            pg.evaluate("id => fetch(API_URL + '/api/preventivi/' + id, {method: 'DELETE'})", pid)
        except Exception:
            pass
        b.close()
    print(f'\n{OK} ok, {len(KO)} falliti')
    if KO:
        print('FALLITI:', KO)
    return 1 if KO else 0


if __name__ == '__main__':
    sys.exit(main())
