/* Pagina laser — "Da procurare" (12/10/2026): i pezzi che non si tagliano
   al laser e vanno procurati. Tubi e profili (comprati in barre da 6 m e
   tagliati in casa), laser tubo, meccanica, commerciali, altro.

   Un pannello a tutto schermo dal bottone in testata (tasto D); Esc torna
   esattamente dov'eri. Sopra i filtri (stato, tipo, ordine); i tubi sommati
   per profilo + materiale con le barre da 6 m (piano di taglio a richiesta,
   e da quali ordini vengono i pezzi); poi gli altri pezzi. Si spuntano voci
   anche di ordini diversi e poi:
     R  Prepara richiesta d'offerta (ZIP: PDF + disegni dei pezzi)
     O  Segna ordinato (fornitore e data prevista scritti adesso)
     A  Segna arrivato
   ogni cambio si puo' annullare dall'avviso. Le voci nascono da sole (tasto
   N nel controllo dei disegni, righe tubolari della distinta): qui niente
   si sposta da solo, si rilegge dopo un'azione. Server: backend/procura.py. */
(function () {
  'use strict';
  const LZ = window.LZ;
  const S = LZ.s;
  const esc = LZ.esc;
  const P = LZ.procura = {};
  const STATI = [['da_ordinare', 'Da ordinare'], ['ordinato', 'Ordinati'], ['arrivato', 'Arrivati'], ['tutti', 'Tutti']];
  const TIPI = [['', 'Tutti i tipi'], ['tubo', 'Tubi e profili'], ['laser_tubo', 'Laser tubo'], ['meccanica', 'Meccanica'],
                ['commerciale', 'Commerciali'], ['altro', 'Altro']];
  let st = null;
  P.n = null;                      // /api/procura/conteggio

  P.aperto = () => !!st;

  // ── Il bottone in testata: "Da procurare (N)" ───────────────────────
  P.conta = async function () {
    const r = await LZ.get('/api/procura/conteggio');
    if (r.ok) { P.n = r.d; bottone(); }
  };
  function bottone() {
    const b = document.getElementById('lz-procura-btn');
    if (!b) return;
    const n = (P.n && P.n.aperti) || 0;
    b.classList.toggle('att', !!(P.n && P.n.da_ordinare));
    b.classList.toggle('err', !!(P.n && P.n.scaduti));
    const t = `${n ? `${n} da procurare` : 'niente da procurare'}${P.n && P.n.scaduti ? `, ${P.n.scaduti} in ritardo` : ''}`;
    b.title = `Da procurare: ${t} (D)`;
    const c = b.querySelector('.n');
    if (c) c.textContent = n ? `(${n})` : '';
  }

  /** Il chip sull'ordine: voci non arrivate. Ambra; rosso se in ritardo. */
  P.chip = function (o, corto) {
    const r = S.riep[o.id];
    const p = r && r.procura;
    if (!p || !p.n) return '';
    const t = corto ? `Procura ${p.n}` : `Da procurare (${p.n})`;
    return `<button class="lz-chip ${p.n_scaduti ? 'err' : 'att'} lz-pc-chip" type="button" data-az="procura"
      title="${esc(`${p.n_da_ordinare} da ordinare, ${p.n_ordinati} ordinati${p.n_scaduti ? `, ${p.n_scaduti} in ritardo` : ''}`)}"><i class="pt"></i>${esc(t)}</button>`;
  };

  // ── Apri / chiudi ───────────────────────────────────────────────────
  P.apri = function (filtri) {
    if (st || LZ.controllo.aperto() || LZ.lantek.aperto()) return;
    LZ.chiudiMenu();
    st = { foto: LZ.fotografa(), filtri: { stato: 'da_ordinare', tipo: '', ordine: '', ...(filtri || {}) },
           dati: null, carica: true, errore: null, sel: new Set(), aperti: new Set(), modulo: null, lavoro: false };
    const el = document.createElement('div');
    el.className = 'lz-ctl lz-pc';
    el.id = 'lz-pc';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-modal', 'true');
    el.setAttribute('aria-label', 'Da procurare');
    el.innerHTML = `<div class="lz-ctl-barra" id="lz-pc-barra"></div>
      <div class="lz-pc-corpo" id="lz-pc-corpo"></div><div id="lz-pc-az"></div>`;
    document.getElementById('lz-sopra').appendChild(el);
    el.addEventListener('click', clic);
    el.addEventListener('change', cambio);
    disegna();
    carica();
  };
  P.chiudi = function () {
    if (!st) return;
    const foto = st.foto;
    st = null;
    const el = document.getElementById('lz-pc');
    if (el) el.remove();
    LZ.ripristina(foto);
  };

  async function carica() {
    if (!st) return;
    const f = st.filtri;
    const q = new URLSearchParams({ stato: f.stato });
    if (f.tipo) q.set('tipo', f.tipo);
    if (f.ordine) q.set('ordine', f.ordine);
    st.carica = true;
    disegna();
    const r = await LZ.get('/api/procura?' + q.toString());
    if (!st) return;
    st.carica = false;
    if (!r.ok) { st.errore = r.d.error || 'Il server non ha risposto'; disegna(); return; }
    st.errore = null;
    st.dati = r.d;
    // la selezione resta sulle voci ancora visibili (anche di ordini diversi)
    const ids = new Set((r.d.voci || []).map(v => v.id));
    for (const id of [...st.sel]) if (!ids.has(id)) st.sel.delete(id);
    P.n = { aperti: r.d.conteggi.aperti, da_ordinare: r.d.conteggi.da_ordinare, scaduti: r.d.conteggi.scaduti };
    bottone();
    disegna();
  }

  // ── Disegno ─────────────────────────────────────────────────────────
  const voci = () => ((st && st.dati) || {}).voci || [];
  const perId = () => new Map(voci().map(v => [v.id, v]));
  const mm = v => (v ? `${FT.numero(Number(v), Number(v) % 1 ? 1 : 0)} mm` : '');
  const mat = m => String(m || '—').replace(/_/g, ' ');
  const metri = v => `${FT.numero(Number(v), Number(v) % 1 ? 2 : 0)} m`;

  function statoHtml(v) {
    if (v.stato === 'arrivato') return `<span class="lz-st ok">${LZ.ico('check')}arrivato ${v.arrivato_il ? esc(FT.fmtDataBreve(v.arrivato_il)) : ''}</span>`;
    if (v.stato === 'ordinato') {
      const prev = v.data_prevista ? `prev. ${FT.fmtDataBreve(v.data_prevista)}` : 'senza data';
      return `<span class="lz-st${v.scaduta ? ' err' : ''}" title="${esc(`Ordinato${v.fornitore ? ' a ' + v.fornitore : ''}${v.ordinato_il ? ' il ' + FT.fmtData(v.ordinato_il) : ''}`)}">${LZ.ico(v.scaduta ? 'clock-alert' : 'truck')}${esc(v.fornitore || 'ordinato')} · ${esc(prev)}${v.scaduta ? ' (in ritardo)' : ''}</span>`;
    }
    return `<span class="lz-st att">${LZ.ico('circle-dot')}da ordinare</span>`;
  }
  const ordineHtml = o => `<span class="lz-pc-ord" title="${esc(o.etichetta)}"><b>${esc(o.cliente || '')}</b> <span class="mono">${esc(o.numero || '')}</span></span>`;
  const casella = (id, nome) => `<input type="checkbox" data-pc-sel="${esc(id)}" ${st.sel.has(id) ? 'checked' : ''} aria-label="Seleziona ${esc(nome)}">`;

  function barra() {
    const d = st.dati || {};
    const c = d.conteggi || {};
    const f = st.filtri;
    const ordini = d.ordini || [];
    LZ.metti(document.getElementById('lz-pc-barra'), `
      <span class="tit">Da procurare</span>
      <span class="lz-seg" role="group" aria-label="Stato">${STATI.map(([k, t]) => `<button type="button" class="${f.stato === k ? 'on' : ''}" data-pc-stato="${k}"
        aria-pressed="${f.stato === k}">${t}${k !== 'tutti' && c[k] != null ? ` <span class="n">${c[k]}</span>` : ''}</button>`).join('')}</span>
      <select data-pc-f="tipo" aria-label="Tipo">${TIPI.map(([k, t]) => `<option value="${k}"${f.tipo === k ? ' selected' : ''}>${t}</option>`).join('')}</select>
      <select data-pc-f="ordine" aria-label="Ordine"><option value="">Tutti gli ordini</option>${ordini.map(o => `<option value="${esc(o.id)}"${f.ordine === o.id ? ' selected' : ''}>${esc(o.etichetta)}</option>`).join('')}</select>
      <span class="sp"></span>
      ${st.carica && st.dati ? `<span class="lz-gira" style="display:inline-grid">${LZ.ico('loader-circle')}</span>` : ''}
      <button class="lz-btn" type="button" data-pc="chiudi">Chiudi${LZ.tasto('Esc')}</button>`);
  }

  function disegna() {
    if (!st) return;
    barra();
    const corpo = document.getElementById('lz-pc-corpo');
    let h;
    if (!st.dati) {
      h = st.errore ? `<div class="lz-errore">${LZ.ico('triangle-alert')}<div><b>Non riesco a leggere «Da procurare»</b><p>${esc(st.errore)}</p>
          <button class="lz-btn piccolo" type="button" data-pc="riprova">Riprova</button></div></div>`
        : `<div aria-busy="true">${Array.from({ length: 6 }, () => '<div class="lz-skel" style="height:40px;margin-bottom:8px"></div>').join('')}</div>`;
    } else if (!voci().length) {
      const f = st.filtri;
      h = `<div class="lz-vuoto"><div><div class="ico">${LZ.ico('package-check')}</div><b>${f.stato === 'da_ordinare' && !f.tipo && !f.ordine ? 'Niente da ordinare' : 'Nessuna voce con questi filtri'}</b>
        <p>Le voci arrivano da sole: tasto N nel controllo dei disegni («non è da laser»), e righe dei tubolari della distinta.</p></div></div>`;
    } else {
      h = tubiHtml() + altriHtml();
    }
    LZ.metti(corpo, `<div class="lz-pc-dentro">${h}</div>`, corpo);
    azioni();
  }

  function tubiHtml() {
    const gruppi = (st.dati.gruppi || []);
    if (!gruppi.length) return '';
    const ids = perId();
    const cfg = st.dati.config || {};
    return `<section class="lz-pc-sez" aria-label="Tubi e profili">
      <h2>Tubi e profili <small>barre da ${FT.numero((cfg.barra_mm || 6000) / 1000, 1)} m · ${FT.numero(cfg.lama_mm || 3, 1)} mm di lama fra un taglio e l'altro</small></h2>
      <table class="lz-tab lz-pc-tab"><thead><tr><th class="ck"></th><th>Profilo</th><th>Materiale</th><th class="r">Pezzi</th><th class="r">Metri</th><th class="r">Barre</th><th></th><th class="men"></th></tr></thead>
      <tbody>${gruppi.map(g => {
        const tutti = g.voci.every(id => st.sel.has(id)), qualcuno = g.voci.some(id => st.sel.has(id));
        const aperto = st.aperti.has(g.chiave);
        const avvisi = [g.troppo_lunghi.length ? `<span class="lz-st err">${LZ.ico('triangle-alert')}${g.troppo_lunghi.map(t => `${esc(t.codice || '')} ${mm(t.lunghezza_mm)}`).join(', ')}: più lungo di una barra</span>` : '',
          g.senza_lunghezza.length ? `<span class="lz-st att">${LZ.ico('ruler')}${g.senza_lunghezza.reduce((s, t) => s + t.n, 0)} pz senza lunghezza</span>` : ''].filter(Boolean).join('');
        let riga = `<tr class="lz-pc-g${aperto ? ' aperto' : ''}" data-fuoco="pc-g-${esc(g.chiave)}">
          <td class="ck"><input type="checkbox" data-pc-gruppo="${esc(g.chiave)}" ${tutti ? 'checked' : ''} ${!tutti && qualcuno ? 'data-meta="1"' : ''} aria-label="Seleziona ${esc(g.profilo)}"></td>
          <td class="cod">${esc(g.profilo)}</td><td>${esc(mat(g.materiale))}</td>
          <td class="r q">${FT.numero(g.n_pezzi)}</td><td class="r">${metri(g.metri)}</td>
          <td class="r"><b class="lz-pc-barre">${g.n_barre}</b></td><td>${avvisi}</td>
          <td class="men"><button class="lz-btn piccolo ghost" type="button" data-pc-apri="${esc(g.chiave)}" aria-expanded="${aperto}">${LZ.ico(aperto ? 'chevron-up' : 'chevron-down')}Piano di taglio</button></td></tr>`;
        if (aperto) riga += `<tr class="lz-pc-piano"><td></td><td colspan="7">${pianoHtml(g)}${daDoveHtml(g, ids)}</td></tr>`;
        return riga;
      }).join('')}</tbody></table></section>`;
  }

  /** Le barre una per una: i tagli in scala (codice e lunghezza), lo sfrido. */
  function pianoHtml(g) {
    if (!g.piano.length) return '<p class="lz-pc-nota">Nessun pezzo con la lunghezza: niente piano.</p>';
    return `<ol class="lz-pc-barre-el" aria-label="Piano di taglio ${esc(g.profilo)}">${g.piano.map((b, i) => `<li>
        <span class="nb">Barra ${i + 1}</span>
        <span class="bar" title="${esc(`usati ${mm(b.usato_mm)}, sfrido ${mm(b.sfrido_mm)}`)}">${b.tagli.map(t => `<i style="flex:${Number(t.lunghezza_mm) || 1} 0 0" title="${esc(`${t.codice || ''} · ${mm(t.lunghezza_mm)} · ${t.ordine || ''}`)}"><span>${esc(mm(t.lunghezza_mm))}</span></i>`).join('')}${b.sfrido_mm > 0 ? `<i class="sf" style="flex:${b.sfrido_mm} 0 0"></i>` : ''}</span>
        <span class="sf-t">sfrido ${esc(mm(b.sfrido_mm))}</span></li>`).join('')}</ol>`;
  }
  /** Da quali ordini e pezzi vengono i tagli del gruppo. */
  function daDoveHtml(g, ids) {
    const vv = g.voci.map(id => ids.get(id)).filter(Boolean);
    return `<table class="lz-pc-dove"><thead><tr><th class="ck"></th><th>Da dove</th><th>Codice</th><th class="r">Pezzi</th><th class="r">Lunghezza</th><th>Stato</th></tr></thead><tbody>
      ${vv.map(v => `<tr><td class="ck">${casella(v.id, v.codice || v.profilo || '')}</td><td>${ordineHtml(v.ordine)}</td>
        <td class="mono">${esc(v.codice || '—')}${v.origine === 'tubolare' ? '<small>riga tubolare della distinta</small>' : ''}</td>
        <td class="r">${FT.numero(v.quantita)}</td><td class="r">${esc(mm(v.lunghezza_mm) || '—')}</td><td>${statoHtml(v)}</td></tr>`).join('')}</tbody></table>`;
  }

  function altriHtml() {
    const vv = voci().filter(v => v.tipo !== 'tubo');
    if (!vv.length) return '';
    return `<section class="lz-pc-sez" aria-label="Altri pezzi da procurare">
      <h2>Laser tubo, meccanica, commerciali, altro</h2>
      <table class="lz-tab lz-pc-tab"><thead><tr><th class="ck"></th><th>Tipo</th><th>Codice</th><th>Descrizione</th><th class="r">Q.tà</th><th>Ordine</th><th>Disegno</th><th>Stato</th></tr></thead>
      <tbody>${vv.map(v => `<tr data-fuoco="pc-v-${esc(v.id)}">
        <td class="ck">${casella(v.id, v.codice || v.descrizione || '')}</td>
        <td><span class="lz-pc-tipo t-${esc(v.tipo)}">${esc(v.tipo_testo)}</span></td>
        <td class="cod">${esc(v.codice || '—')}</td>
        <td class="lav">${esc([v.descrizione, v.profilo, v.lunghezza_mm ? mm(v.lunghezza_mm) : ''].filter(Boolean).join(' · ') || '—')}${v.nota ? `<small>${esc(v.nota)}</small>` : ''}</td>
        <td class="r q">${FT.numero(v.quantita)}</td>
        <td>${ordineHtml(v.ordine)}</td>
        <td>${v.n_disegni ? `<span class="lz-st ok">${LZ.ico('file-check')}sì (${v.n_disegni})</span>` : '<span class="lz-st">no</span>'}</td>
        <td>${statoHtml(v)}</td></tr>`).join('')}</tbody></table></section>`;
  }

  /** In fondo: le azioni sulle voci spuntate (anche di ordini diversi). */
  function azioni() {
    const box = document.getElementById('lz-pc-az');
    if (!box || !st) return;
    const n = st.sel.size;
    if (!n) { LZ.metti(box, ''); return; }
    const sel = voci().filter(v => st.sel.has(v.id));
    const ordini = new Set(sel.map(v => v.order_id)).size;
    let h;
    if (st.modulo === 'ordinato') {
      h = `<div class="lz-pc-az modulo">
        <span class="q"><b>${LZ.plur(n, 'voce', 'voci')}</b>: segna ordinato</span>
        <label>Fornitore <input type="text" maxlength="80" data-pc-forn data-fuoco="pc-forn" placeholder="a chi l'hai ordinato" value="${esc(st.forn || '')}"></label>
        <label>Data prevista <input type="date" data-pc-data data-fuoco="pc-data" min="${esc(FT.oggiISO())}" value="${esc(st.data || '')}"></label>
        <span class="sp"></span>
        <button class="lz-btn" type="button" data-pc="modulo-via">Annulla${LZ.tasto('Esc')}</button>
        <button class="lz-btn pri" type="button" data-pc="ordinato-ok" ${st.lavoro ? 'disabled' : ''}>Segna ordinato${LZ.tasto('Invio')}</button></div>`;
    } else {
      h = `<div class="lz-pc-az">
        <span class="q"><b>${LZ.plur(n, 'voce selezionata', 'voci selezionate')}</b>${ordini > 1 ? ` da ${ordini} ordini` : ''}</span>
        <button class="lz-link" type="button" data-pc="sel-via">Togli selezione</button>
        <span class="sp"></span>
        <button class="lz-btn pri" type="button" data-pc="richiesta" ${st.lavoro ? 'disabled' : ''}>${LZ.ico('file-archive')}Prepara richiesta d'offerta${LZ.tasto('R')}</button>
        <button class="lz-btn" type="button" data-pc="ordinato" ${st.lavoro ? 'disabled' : ''}>${LZ.ico('truck')}Segna ordinato${LZ.tasto('O')}</button>
        <button class="lz-btn" type="button" data-pc="arrivato" ${st.lavoro ? 'disabled' : ''}>${LZ.ico('package-check')}Segna arrivato${LZ.tasto('A')}</button></div>`;
    }
    LZ.metti(box, h);
    document.querySelectorAll('#lz-pc input[data-meta]').forEach(i => { i.indeterminate = true; });
  }

  // ── Clic e tasti ────────────────────────────────────────────────────
  function clic(e) {
    const b = e.target.closest('[data-pc]');
    const sf = e.target.closest('[data-pc-stato]');
    const ap = e.target.closest('[data-pc-apri]');
    if (sf) { st.filtri.stato = sf.dataset.pcStato; carica(); return; }
    if (ap) { const k = ap.dataset.pcApri; if (st.aperti.has(k)) st.aperti.delete(k); else st.aperti.add(k); disegna(); return; }
    if (!b) return;
    const a = b.dataset.pc;
    if (a === 'chiudi') P.chiudi();
    else if (a === 'riprova') carica();
    else if (a === 'sel-via') { st.sel.clear(); disegna(); }
    else if (a === 'richiesta') richiesta();
    else if (a === 'ordinato') apriOrdinato();
    else if (a === 'ordinato-ok') segnaOrdinato();
    else if (a === 'modulo-via') { st.modulo = null; azioni(); }
    else if (a === 'arrivato') cambiaStato('arrivato');
  }
  function cambio(e) {
    const t = e.target;
    if (t.matches('[data-pc-sel]')) { if (t.checked) st.sel.add(t.dataset.pcSel); else st.sel.delete(t.dataset.pcSel); disegna(); return; }
    if (t.matches('[data-pc-gruppo]')) {
      const g = ((st.dati || {}).gruppi || []).find(x => x.chiave === t.dataset.pcGruppo);
      if (g) for (const id of g.voci) { if (t.checked) st.sel.add(id); else st.sel.delete(id); }
      disegna();
      return;
    }
    if (t.matches('[data-pc-f]')) { st.filtri[t.dataset.pcF] = t.value; carica(); return; }
    if (t.matches('[data-pc-forn]')) st.forn = t.value;
    if (t.matches('[data-pc-data]')) st.data = t.value;
  }
  document.addEventListener('input', e => {
    if (!st) return;
    if (e.target.matches && e.target.matches('#lz-pc [data-pc-forn]')) st.forn = e.target.value;
    if (e.target.matches && e.target.matches('#lz-pc [data-pc-data]')) st.data = e.target.value;
  });

  /** Tasti del pannello. true = gestito. */
  P.tasto = function (e) {
    if (!st) return false;
    if (e.ctrlKey || e.altKey || e.metaKey) return false;
    const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    const t = e.target;
    const inCampo = t && /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName || '') && !/^(checkbox|radio)$/.test(t.type || '');
    if (st.modulo === 'ordinato') {
      if (k === 'Escape') { st.modulo = null; azioni(); return true; }
      if (k === 'Enter') { segnaOrdinato(); return true; }
      return false;
    }
    if (k === 'Escape') { if (inCampo) { t.blur(); return true; } P.chiudi(); return true; }
    if (inCampo) return false;
    if (!st.sel.size || st.lavoro) return false;
    if (k === 'r') { richiesta(); return true; }
    if (k === 'o') { apriOrdinato(); return true; }
    if (k === 'a') { cambiaStato('arrivato'); return true; }
    return false;
  };

  // ── Azioni ──────────────────────────────────────────────────────────
  async function dopo() {
    await carica();
    LZ.avvio.caricaRiep();
    if (S.aperto) LZ.ordine.carica(S.aperto);
  }

  function apriOrdinato() {
    if (!st || !st.sel.size) return;
    st.modulo = 'ordinato';
    // il fornitore gia' scritto su queste voci, se e' uno solo
    const forn = [...new Set(voci().filter(v => st.sel.has(v.id) && v.fornitore).map(v => v.fornitore))];
    st.forn = st.forn || (forn.length === 1 ? forn[0] : '');
    azioni();
    const i = document.querySelector('#lz-pc [data-pc-forn]');
    if (i) i.focus();
  }
  async function segnaOrdinato() {
    if (!st || st.lavoro) return;
    const forn = String(st.forn || '').trim();
    if (!forn) { FT.toast('Scrivi a chi l\'hai ordinato', 'warn'); const i = document.querySelector('#lz-pc [data-pc-forn]'); if (i) i.focus(); return; }
    await cambiaStato('ordinato', { fornitore: forn, data_prevista: st.data || null });
  }

  async function cambiaStato(stato, extra) {
    if (!st || !st.sel.size || st.lavoro) return;
    const ids = [...st.sel];
    st.lavoro = true; azioni();
    const r = await LZ.azione(() => LZ.post('/api/procura/stato', { ids, stato, ...(extra || {}) }));
    if (!st) return;
    st.lavoro = false;
    if (!r.ok) { azioni(); FT.toast('Non salvato: ' + (r.d.error || 'errore del server'), 'err'); return; }
    st.modulo = null; st.sel.clear(); st.data = '';
    const prima = r.d.prima || [];
    const t = stato === 'ordinato' ? `ordinat${r.d.n === 1 ? 'a' : 'e'} (${extra.fornitore})` : stato === 'arrivato' ? `arrivat${r.d.n === 1 ? 'a' : 'e'}` : 'di nuovo da ordinare';
    FT.toast(`${LZ.plur(r.d.n, 'voce', 'voci')} ${t}`, 'ok', { testo: 'Annulla', fn: () => annulla(prima) }, 10000);
    await dopo();
  }
  async function annulla(prima) {
    const r = await LZ.azione(() => LZ.post('/api/procura/annulla', { prima }));
    if (!r.ok) { FT.toast('Non annullato: ' + (r.d.error || 'errore del server'), 'err'); return; }
    FT.toast('Annullato: tornato com\'era', 'ok');
    if (st) await dopo(); else { P.conta(); LZ.avvio.caricaRiep(); if (S.aperto) LZ.ordine.carica(S.aperto); }
  }

  /** ZIP con il PDF della richiesta e i disegni dei pezzi scelti. */
  async function richiesta() {
    if (!st || !st.sel.size || st.lavoro) return;
    const ids = [...st.sel];
    st.lavoro = true; azioni();
    FT.toast('Preparo la richiesta d\'offerta…', '', null, 2000);
    try {
      const r = await fetch('/api/procura/richiesta-offerta', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ids }) });
      if (!r.ok) { const d = await r.json().catch(() => ({})); throw new Error(d.error || 'errore del server'); }
      const blob = await r.blob();
      const cd = r.headers.get('Content-Disposition') || '';
      const nome = (cd.match(/filename="([^"]+)"/) || [])[1] || 'Richiesta offerta.zip';
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = nome;
      document.body.appendChild(a);
      a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 4000);
      const n = Number(r.headers.get('X-File-Zip') || 1) - 1;
      FT.toast(`${nome}: PDF + ${LZ.plur(n, 'disegno', 'disegni')}`, 'ok', null, 7000);
    } catch (e) {
      FT.toast('Richiesta non preparata: ' + e.message, 'err');
    } finally {
      if (st) { st.lavoro = false; azioni(); }
    }
  }
})();
