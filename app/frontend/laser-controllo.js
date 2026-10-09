/* Pagina laser — controllo dei disegni a tutto schermo (la schermata piu'
   importante). Il FOGLIO INTERO, grande, col contorno che il motore taglierebbe
   in verde; a destra i dati del pezzo e "Perche' te lo chiedo".

   Tasti: Invio = giusto · S = lo scelgo io (clic sul bordo del pezzo, sullo
   stesso disegno) · V = va sviluppato · P = piu' pezzi · N = non e' da laser
   · ←/→ = indietro/salta · Esc = chiudi e torna esattamente dov'eri.
   Rotellina = ingrandisci, trascina = sposta, 0 = foglio intero.

   Una coda sola anche su piu' ordini: nella barra le tacche, con un
   separatore fra un ordine e l'altro, e il nome dell'ordine sempre visibile. */
(function () {
  'use strict';
  const LZ = window.LZ;
  const S = LZ.s;
  const esc = LZ.esc;
  const C = LZ.controllo = {};
  const SCELTE = { giusto: 'è il pezzo giusto', scelto: 'contorno scelto a mano', sviluppo: 'va sviluppato',
                   piu_pezzi: 'più pezzi: a mano in Lantek', non_laser: 'non è da laser' };
  let st = null;          // stato del controllo aperto (null = chiuso)

  C.aperto = () => !!st;

  // ── Apertura ────────────────────────────────────────────────────────
  /** Coda dei disegni da controllare degli ordini `ids` (nell'ordine dell'elenco). */
  C.inizia = async function (ids) {
    if (st) return;
    const ordine = LZ.ordiniLaser().map(o => o.id).filter(id => ids.includes(id));
    const mancano = ordine.filter(id => !(S.dett[id] && S.dett[id].dati));
    if (mancano.length) {
      FT.toast('Preparo i disegni…', '', null, 1500);
      await Promise.all(mancano.map(id => LZ.ordine.carica(id)));
    }
    const coda = [];
    for (const id of ordine) {
      for (const p of ((S.dett[id] || {}).dati || {}).pezzi || []) {
        if (p.da_controllare && p.articolo_id && coda.every(x => !(x.id === id && x.aid === p.articolo_id))) coda.push({ id, aid: p.articolo_id });
      }
    }
    if (!coda.length) { FT.toast('Niente da controllare', ''); return; }
    apri(coda, false);
  };
  /** Un disegno solo (clic su una riga dei pezzi o su una miniatura). */
  C.vedi = function (id, aid) { if (!st) apri([{ id, aid }], true); };

  function apri(coda, solo) {
    LZ.chiudiMenu();
    st = { coda, i: 0, solo, foto: LZ.fotografa(), letture: {}, risposte: {}, modo: 'vedi', cand: [], sel: 0,
           attesa: false, vb: null, trascina: null, fatte: 0 };
    const box = document.getElementById('lz-sopra');
    const el = document.createElement('div');
    el.className = 'lz-ctl';
    el.id = 'lz-ctl';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-modal', 'true');
    el.setAttribute('aria-label', 'Controllo disegni');
    el.innerHTML = `<div class="lz-ctl-barra" id="lz-ctl-barra"></div>
      <div class="lz-ctl-corpo"><div class="lz-foglio" id="lz-ctl-foglio" tabindex="-1"></div><div class="lz-lato" id="lz-ctl-lato"></div></div>`;
    box.appendChild(el);
    collega(el.querySelector('#lz-ctl-foglio'));
    el.addEventListener('click', clic);
    mostra();
  }

  C.chiudi = function () {
    if (!st) return;
    const foto = st.foto, ids = [...new Set(st.coda.map(x => x.id))], fatte = st.fatte, solo = st.solo;
    st = null;
    const el = document.getElementById('lz-ctl');
    if (el) el.remove();
    LZ.ripristina(foto);
    if (fatte && !solo) FT.toast(`Controllo finito: ${LZ.plur(fatte, 'risposta salvata', 'risposte salvate')}`, 'ok');
    // gli ordini toccati si rileggono (stato e conteggi nuovi)
    if (fatte) for (const id of ids) { LZ.avvio.caricaRiep(id); if (S.aperto === id) LZ.ordine.carica(id); }
  };

  // ── Dati del pezzo corrente ─────────────────────────────────────────
  const voce = () => st.coda[st.i];
  const chiave = v => v.id + '|' + v.aid;
  function pezzo(v) {
    return (((S.dett[v.id] || {}).dati || {}).pezzi || []).find(p => String(p.articolo_id) === String(v.aid)) || {};
  }
  async function lettura(v) {
    const k = chiave(v);
    if (st.letture[k]) return st.letture[k].prom;
    const rec = { stato: 'carica' };
    rec.prom = LZ.get(`/api/orders/${encodeURIComponent(v.id)}/pezzi/${encodeURIComponent(v.aid)}/lettura`).then(r => {
      rec.stato = r.ok ? 'ok' : 'errore';
      rec.d = r.d;
      return rec;
    });
    st.letture[k] = rec;
    return rec.prom;
  }

  // ── Disegno ─────────────────────────────────────────────────────────
  function mostra() {
    if (!st) return;
    st.modo = 'vedi'; st.cand = []; st.sel = 0; st.vb = null;
    barra();
    lato();
    const v = voce();
    const f = document.getElementById('lz-ctl-foglio');
    f.classList.remove('scegli');
    f.innerHTML = '<div class="lz-foglio-msg"><div class="lz-skel"></div></div>';
    const k = chiave(v);
    lettura(v).then(rec => {
      if (!st || chiave(voce()) !== k) return;
      if (rec.stato !== 'ok') {
        f.innerHTML = `<div class="lz-foglio-msg"><div><b>Non riesco a mostrare il disegno</b><p>${esc(rec.d.error || 'Errore del server')}</p></div></div>`;
      } else {
        f.innerHTML = rec.d.svg + `<div class="lz-leg">${rec.d.ha_contorno ? '<i></i>In verde: il pezzo che taglierei' : 'Il motore non ha trovato un contorno: sceglilo tu (S)'}</div>
          <div class="lz-zoom"><button class="lz-btn" type="button" data-z="piu" title="Ingrandisci" aria-label="Ingrandisci">${LZ.ico('plus')}</button>
          <button class="lz-btn" type="button" data-z="meno" title="Rimpicciolisci" aria-label="Rimpicciolisci">${LZ.ico('minus')}</button>
          <button class="lz-btn" type="button" data-z="tutto" title="Foglio intero (0)" aria-label="Foglio intero">${LZ.ico('scan')}</button></div>`;
        LZ.icone(f);
        const svg = f.querySelector('svg');
        if (svg) { const b = svg.viewBox.baseVal; st.vb = { x: b.x, y: b.y, w: b.width, h: b.height, base: [b.x, b.y, b.width, b.height] }; }
      }
      lato();
    });
    // il prossimo intanto si prepara
    if (st.coda[st.i + 1]) lettura(st.coda[st.i + 1]);
  }

  function barra() {
    const v = voce();
    const o = LZ.trova(v.id) || {};
    const ordini = [...new Set(st.coda.map(x => x.id))];
    let tacche = '';
    if (!st.solo) {
      st.coda.forEach((x, k) => {
        if (k && x.id !== st.coda[k - 1].id) tacche += '<i class="sep"></i>';
        const r = st.risposte[chiave(x)];
        tacche += `<i class="${k === st.i ? 'c' : r ? 'f' : ''}" title="${esc((LZ.trova(x.id) || {}).cliente || '')}"></i>`;
      });
    }
    LZ.metti(document.getElementById('lz-ctl-barra'), `
      <span class="tit">${st.solo ? 'Disegno' : 'Controllo disegni'}</span>
      <span class="dove" title="${esc(o.cliente || '')} ${esc(LZ.numero(o))}">${esc(o.cliente || '')} · <span class="mono">${esc(LZ.numero(o))}</span>${ordini.length > 1 ? ` <span>(ordine ${ordini.indexOf(v.id) + 1} di ${ordini.length})</span>` : ''}</span>
      <span class="sp"></span>
      ${st.solo ? '' : `<span class="lz-prog"><span>${st.i + 1} di ${st.coda.length}</span><span class="tacche" aria-hidden="true">${tacche}</span></span>`}
      <button class="lz-btn" type="button" data-c="chiudi" data-fuoco="ctl-chiudi">Chiudi${LZ.tasto('Esc')}</button>`);
  }

  function lato() {
    if (!st) return;
    const v = voce();
    const p = pezzo(v);
    const o = LZ.trova(v.id) || {};
    const rec = st.letture[chiave(v)];
    const lt = rec && rec.stato === 'ok' ? rec.d : null;
    const perche = (lt && lt.perche && lt.perche.length ? lt.perche : p.perche) || [];
    const risposta = st.risposte[chiave(v)] || (p.decisione ? p.decisione : null);
    const nuovo = p.lantek === 'nuovo' || p.lantek === 'sconosciuto';
    const dati = `<div class="lz-dati">
      <div><span>Quantità</span><b>${p.quantita != null ? FT.numero(p.quantita) + ' pz' : '—'}</b></div>
      <div><span>Materiale</span><b>${esc(String(p.materiale || '—').replace(/_/g, ' '))}</b></div>
      <div><span>Spessore</span><b>${esc(LZ.mm(p.spessore) || '—')}</b></div>
      <div><span>Misure</span><b>${esc(LZ.misure(p.misure) || '—')}</b></div>
      <div><span>In Lantek</span><b>${p.lantek === 'in_lantek' ? "c'è già" : nuovo ? 'nuovo: lo creo io' : '—'}</b></div></div>`;
    let corpo;
    if (st.modo === 'scegli') {
      corpo = `<div class="lz-scegli-guida"><b>Clicca il bordo del pezzo giusto</b>sul disegno a sinistra. ${st.cand.length > 1 ? 'Ci sono più contorni possibili: scegli quello giusto (1, 2, 3…).' : ''}</div>
        ${st.cand.length ? `<div class="lz-decidi">${st.cand.map((c, k) => `<button class="lz-btn${k === st.sel ? ' pri' : ''}" type="button" data-cand="${k}">
            <span>${k + 1}. ${esc(LZ.misure([c.bbox_width_mm, c.bbox_height_mm]))}</span><span class="num">${FT.numero(c.area_dm2, 2)} dm²</span></button>`).join('')}</div>` : ''}
        <div class="lz-decidi">
          <button class="lz-btn pri" type="button" data-c="conferma-scelta" ${st.cand.length ? '' : 'disabled'}><span>${LZ.ico('check')}Conferma questo contorno</span>${LZ.tasto('Invio')}</button>
          <button class="lz-btn" type="button" data-c="annulla-scelta"><span>${LZ.ico('undo-2')}Torna al contorno del motore</span>${LZ.tasto('Esc')}</button></div>`;
    } else {
      corpo = `${perche.length ? `<div class="lz-perche"><b>Perché te lo chiedo</b><ul>${perche.map(x => `<li>${esc(x)}</li>`).join('')}</ul></div>`
          : p.motore === 'sicuro' ? `<div class="lz-sicuro">${LZ.ico('circle-check')}Il motore è sicuro di questo disegno.</div>` : ''}
        ${lt && lt.suggerimento && !risposta ? `<div class="lz-risposta">Il motore pensa: <b>${esc(SCELTE[lt.suggerimento])}</b>. Decidi tu col tasto.</div>` : ''}
        ${risposta ? `<div class="lz-risposta">Hai risposto: <b>${esc(SCELTE[risposta] || risposta)}</b></div>` : ''}
        <div class="lz-decidi">
          <button class="lz-btn pri" type="button" data-c="giusto" ${lt && !lt.ha_contorno && nuovo ? 'disabled' : ''}><span>${LZ.ico('check')}Sì, è il pezzo giusto</span>${LZ.tasto('Invio')}</button>
          <button class="lz-btn" type="button" data-c="scegli"><span>${LZ.ico('mouse-pointer-click')}No, lo scelgo io sul disegno</span>${LZ.tasto('S')}</button>
          <button class="lz-btn${lt && lt.suggerimento === 'sviluppo' ? ' suggerito' : ''}" type="button" data-c="sviluppo"><span>${LZ.ico('move-diagonal')}Va sviluppato</span>${LZ.tasto('V')}</button>
          <button class="lz-btn${lt && lt.suggerimento === 'piu_pezzi' ? ' suggerito' : ''}" type="button" data-c="piu_pezzi"><span>${LZ.ico('copy')}Ci sono più pezzi</span>${LZ.tasto('P')}</button>
          <button class="lz-btn${lt && lt.suggerimento === 'non_laser' ? ' suggerito' : ''}" type="button" data-c="non_laser"><span>${LZ.ico('circle-minus')}Non è da laser</span>${LZ.tasto('N')}</button></div>`;
    }
    LZ.metti(document.getElementById('lz-ctl-lato'), `
      <div class="ord"><b>${esc(o.cliente || '')}</b><span class="mono">${esc(LZ.numero(o))}</span></div>
      <div class="cod">${esc(p.codice || '')}</div>
      ${dati}${corpo}
      ${st.attesa ? '<div class="lz-sicuro"><span class="lz-gira" style="display:inline-grid">' + LZ.ico('loader-circle') + '</span>Salvo…</div>' : ''}
      ${st.solo ? '' : `<div class="lz-fondo"><button class="lz-btn" type="button" data-c="indietro" ${st.i ? '' : 'disabled'}>${LZ.ico('arrow-left')}Indietro</button>
        <button class="lz-btn" type="button" data-c="salta" ${st.i < st.coda.length - 1 ? '' : 'disabled'}>Salta${LZ.ico('arrow-right')}</button></div>`}`);
  }

  // ── Ingrandire, spostare, scegliere col clic ────────────────────────
  function svgEl() { const f = document.getElementById('lz-ctl-foglio'); return f && f.querySelector('svg'); }
  function applicaVista() {
    const s = svgEl();
    if (s && st.vb) s.setAttribute('viewBox', `${st.vb.x} ${st.vb.y} ${st.vb.w} ${st.vb.h}`);
  }
  /** Punto dello schermo -> punto dell'SVG (u, v); il disegno e' (u, -v). */
  function puntoSvg(cx, cy) {
    const s = svgEl();
    if (!s) return null;
    const m = s.getScreenCTM();
    if (!m) return null;
    const p = new DOMPoint(cx, cy).matrixTransform(m.inverse());
    return { u: p.x, v: p.y };
  }
  function zoom(f, cx, cy) {
    if (!st || !st.vb) return;
    const s = svgEl();
    const r = s.getBoundingClientRect();
    const p = puntoSvg(cx == null ? r.left + r.width / 2 : cx, cy == null ? r.top + r.height / 2 : cy);
    if (!p) return;
    const w = Math.max(st.vb.base[2] / 60, Math.min(st.vb.base[2] * 1.5, st.vb.w * f));
    const k = w / st.vb.w;
    st.vb.x = p.u - (p.u - st.vb.x) * k;
    st.vb.y = p.v - (p.v - st.vb.y) * k;
    st.vb.w *= k; st.vb.h *= k;
    applicaVista();
  }
  function tutto() {
    if (!st || !st.vb) return;
    [st.vb.x, st.vb.y, st.vb.w, st.vb.h] = st.vb.base;
    applicaVista();
  }

  function collega(f) {
    f.addEventListener('wheel', e => { if (!st) return; e.preventDefault(); zoom(e.deltaY > 0 ? 1.18 : 1 / 1.18, e.clientX, e.clientY); }, { passive: false });
    f.addEventListener('mousedown', e => {
      if (!st || e.button !== 0 || e.target.closest('.lz-zoom') || !st.vb) return;
      st.trascina = { x: e.clientX, y: e.clientY, vb: { ...st.vb }, mosso: false };
    });
    window.addEventListener('mousemove', e => {
      if (!st || !st.trascina) return;
      const t = st.trascina;
      const dx = e.clientX - t.x, dy = e.clientY - t.y;
      if (!t.mosso && Math.hypot(dx, dy) < 4) return;
      t.mosso = true;
      f.classList.add('trascina');
      const s = svgEl();
      const m = s && s.getScreenCTM();
      if (!m) return;
      st.vb.x = t.vb.x - dx / m.a;
      st.vb.y = t.vb.y - dy / m.d;
      applicaVista();
    });
    window.addEventListener('mouseup', e => {
      if (!st || !st.trascina) return;
      const t = st.trascina;
      st.trascina = null;
      f.classList.remove('trascina');
      if (t.mosso) return;
      // un clic, non un trascinamento
      const c = e.target.closest && e.target.closest('.fg-cand');
      if (st.modo === 'scegli' && c) { st.sel = Number(c.dataset.k); disegnaCandidati(); lato(); return; }
      if (st.modo === 'scegli' && f.contains(e.target)) cercaCandidati(e.clientX, e.clientY);
    });
    f.addEventListener('click', e => {
      const z = e.target.closest('[data-z]');
      if (!z) return;
      if (z.dataset.z === 'piu') zoom(1 / 1.4); else if (z.dataset.z === 'meno') zoom(1.4); else tutto();
    });
  }

  async function cercaCandidati(cx, cy) {
    const p = puntoSvg(cx, cy);
    if (!p) return;
    const v = voce(), k = chiave(v);
    const x = p.u, y = -p.v;                       // nel disegno la y va in su
    const g = document.querySelector('#lz-ctl-foglio .fg-scelte');
    if (g) g.innerHTML = `<circle class="fg-clic" cx="${p.u}" cy="${p.v}" r="${(st.vb ? st.vb.w : 100) / 160}"/>`;
    st.attesa = true; lato();
    const r = await LZ.post(`/api/orders/${encodeURIComponent(v.id)}/pezzi/${encodeURIComponent(v.aid)}/cad/pick-candidates`, { x, y });
    if (!st || chiave(voce()) !== k) return;
    st.attesa = false;
    if (!r.ok || !(r.d.candidates || []).length) {
      st.cand = []; disegnaCandidati(); lato();
      FT.toast(r.d.error && r.status !== 200 ? r.d.error : 'Qui non trovo un contorno chiuso: clicca proprio sul bordo del pezzo', 'warn');
      return;
    }
    st.cand = r.d.candidates.slice(0, 9);
    st.sel = 0;
    disegnaCandidati();
    lato();
  }
  function disegnaCandidati() {
    const g = document.querySelector('#lz-ctl-foglio .fg-scelte');
    if (!g) return;
    const d = c => [c.outer_xy].concat(c.holes_xy || []).map(an => 'M' + an.map(([x, y]) => `${x} ${-y}`).join('L') + 'Z').join('');
    // il selezionato per ultimo: sta sopra
    const ord = st.cand.map((c, k) => k).sort((a, b) => (a === st.sel) - (b === st.sel));
    g.innerHTML = ord.map(k => `<path class="fg-cand${k === st.sel ? ' on' : ''}" data-k="${k}" d="${d(st.cand[k])}" fill-rule="evenodd" vector-effect="non-scaling-stroke"/>`).join('');
  }

  function entraScegli() {
    if (!st || st.attesa) return;
    st.modo = 'scegli'; st.cand = []; st.sel = 0;
    const f = document.getElementById('lz-ctl-foglio');
    f.classList.add('scegli');
    const leg = f.querySelector('.lz-leg');
    if (leg) { leg.classList.add('blu'); leg.innerHTML = '<i></i>Clicca il bordo del pezzo giusto'; }
    lato();
  }
  function esciScegli() {
    st.modo = 'vedi'; st.cand = [];
    const f = document.getElementById('lz-ctl-foglio');
    f.classList.remove('scegli');
    const g = f.querySelector('.fg-scelte'); if (g) g.innerHTML = '';
    const rec = st.letture[chiave(voce())];
    const leg = f.querySelector('.lz-leg');
    if (leg) { leg.classList.remove('blu'); leg.innerHTML = rec && rec.d && rec.d.ha_contorno ? '<i></i>In verde: il pezzo che taglierei' : 'Il motore non ha trovato un contorno: sceglilo tu (S)'; }
    lato();
  }

  // ── Risposte ────────────────────────────────────────────────────────
  async function rispondi(scelta) {
    if (!st || st.attesa) return;
    const v = voce(), p = pezzo(v), k = chiave(v);
    st.attesa = true; lato();
    let r;
    if (scelta === 'giusto') {
      r = await LZ.post(`/api/orders/${encodeURIComponent(v.id)}/pezzi/${encodeURIComponent(v.aid)}/giusto`,
        { serve_pulito: p.lantek === 'nuovo' || p.lantek === 'sconosciuto' });
    } else if (scelta === 'scelto') {
      const c = st.cand[st.sel];
      r = await LZ.post(`/api/orders/${encodeURIComponent(v.id)}/pezzi/${encodeURIComponent(v.aid)}/contorno`,
        { outer_xy: c.outer_xy, holes_xy: c.holes_xy || [], pagina: 'laser' });
    } else {
      r = await LZ.post(`/api/orders/${encodeURIComponent(v.id)}/pezzi/${encodeURIComponent(v.aid)}/decisione`, { scelta });
    }
    if (!st) return;
    st.attesa = false;
    if (!r.ok) {
      FT.toast('Non salvato: ' + (r.d.error || 'errore del server'), 'err', null, 7000);
      lato();
      return;
    }
    if (r.d.pronto_lantek === false && r.d.motivo) FT.toast(`${p.codice}: ${r.d.motivo}`, 'warn', null, 9000);
    st.risposte[k] = scelta;
    st.fatte++;
    p.decisione = scelta;                 // la riga dei pezzi lo mostra subito (poi si rilegge)
    p.da_controllare = false;
    if (st.solo || chiave(voce()) !== k) { C.chiudi(); return; }
    if (st.i >= st.coda.length - 1) {
      // fine coda: si torna dov'eri solo se e' tutto risposto
      if (st.coda.every(x => st.risposte[chiave(x)])) { C.chiudi(); return; }
      lato(); barra();
      FT.toast('Restano disegni saltati: tornaci con ←, o chiudi con Esc', '');
      return;
    }
    st.i++;
    mostra();
  }

  function sposta(d) {
    if (!st || st.solo) return;
    const j = Math.max(0, Math.min(st.coda.length - 1, st.i + d));
    if (j === st.i) return;
    st.i = j;
    mostra();
  }

  function clic(e) {
    const c = e.target.closest('[data-c]');
    const cd = e.target.closest('[data-cand]');
    if (cd) { st.sel = Number(cd.dataset.cand); disegnaCandidati(); lato(); return; }
    if (!c) return;
    const a = c.dataset.c;
    if (a === 'chiudi') C.chiudi();
    else if (a === 'indietro') sposta(-1);
    else if (a === 'salta') sposta(1);
    else if (a === 'scegli') entraScegli();
    else if (a === 'annulla-scelta') esciScegli();
    else if (a === 'conferma-scelta') { if (st.cand.length) rispondi('scelto'); }
    else rispondi(a);
  }

  /** Tasti del controllo. true = gestito. */
  C.tasto = function (e) {
    if (!st) return false;
    if (e.ctrlKey || e.altKey || e.metaKey) return false;
    const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    if (k === 'Escape') { if (st.modo === 'scegli') esciScegli(); else C.chiudi(); return true; }
    if (st.attesa) return true;
    if (st.modo === 'scegli') {
      if (k === 'Enter') { if (st.cand.length) rispondi('scelto'); return true; }
      if (/^[1-9]$/.test(k) && st.cand[Number(k) - 1]) { st.sel = Number(k) - 1; disegnaCandidati(); lato(); return true; }
    } else {
      if (k === 'Enter') {
        const b = document.querySelector('#lz-ctl-lato [data-c="giusto"]');
        if (b && !b.disabled) rispondi('giusto');
        return true;
      }
      if (k === 's') { entraScegli(); return true; }
      if (k === 'v') { rispondi('sviluppo'); return true; }
      if (k === 'p') { rispondi('piu_pezzi'); return true; }
      if (k === 'n') { rispondi('non_laser'); return true; }
    }
    if (k === 'ArrowRight') { sposta(1); return true; }
    if (k === 'ArrowLeft') { sposta(-1); return true; }
    if (k === '0') { tutto(); return true; }
    if (k === '+' || k === '=') { zoom(1 / 1.4); return true; }
    if (k === '-') { zoom(1.4); return true; }
    return k === ' ' || k === 'Tab' ? false : true;     // gli altri tasti non vanno alla pagina sotto
  };
})();
