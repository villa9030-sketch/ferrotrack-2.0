/* Pagina laser — controllo dei disegni a tutto schermo (la schermata piu'
   importante). Il FOGLIO INTERO, grande, col contorno che il motore taglierebbe
   in verde; a destra i dati del pezzo e "Perche' te lo chiedo".

   Tasti: Invio = giusto · S = lo scelgo io (clic sul bordo del pezzo, sullo
   stesso disegno) · V = va sviluppato · P = piu' pezzi · N = non e' da laser
   · ←/→ = indietro/salta · Esc = chiudi e torna esattamente dov'eri.
   Rotellina = ingrandisci, trascina = sposta, 0 = foglio intero.

   P apre "Pezzi del foglio" (12/10/2026): tutti i pezzi trovati nel foglio,
   numerati e colorati, le copie uguali segnate ×N; a destra l'elenco con
   codice e quantita' (si cambiano), × toglie una vista o una cornice, un clic
   sul bordo di un pezzo mancante lo aggiunge; Invio conferma tutto
   (backend/api_pezzi_foglio.py). "A mano in Lantek" resta possibile (M).

   Una coda sola anche su piu' ordini: nella barra le tacche, con un
   separatore fra un ordine e l'altro, e il nome dell'ordine sempre visibile. */
(function () {
  'use strict';
  const LZ = window.LZ;
  const S = LZ.s;
  const esc = LZ.esc;
  const C = LZ.controllo = {};
  const SCELTE = { giusto: 'è il pezzo giusto', scelto: 'contorno scelto a mano', sviluppo: 'va sviluppato',
                   piu_pezzi: 'più pezzi: a mano in Lantek', non_laser: 'non è da laser',
                   piu_pezzi_confermati: 'più pezzi: confermati uno per uno' };
  // colori dei pezzi del foglio: tenui, distinti fra loro (sul disegno grigio)
  const COLORI_PF = ['#2459D8', '#B45309', '#0F766E', '#9333EA', '#BE123C', '#4D7C0F', '#0369A1', '#A16207', '#6D28D9', '#C2410C'];
  let st = null;          // stato del controllo aperto (null = chiuso)
  // Salvataggi in sottofondo: la risposta passa subito al disegno dopo, il
  // server salva intanto (uno alla volta, nell'ordine delle risposte).
  let codaSalva = Promise.resolve();
  let salvaInCorso = 0;

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
  C.vedi = function (id, aid, modo) {
    if (st) return;
    apri([{ id, aid }], true);
    if (modo === 'pezzi') entraPezzi();         // "Rivedi i pezzi del foglio" dal menu della riga
  };

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
    el.addEventListener('input', alCambio);
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
    if (fatte) codaSalva.then(() => { for (const id of ids) { LZ.avvio.caricaRiep(id); if (S.aperto === id) LZ.ordine.carica(id); } });
  };

  // ── Dati del pezzo corrente ─────────────────────────────────────────
  const voce = () => st.coda[st.i];
  const chiave = v => v.id + '|' + v.aid;
  function pezzo(v) {
    const pezzi = ((S.dett[v.id] || {}).dati || {}).pezzi || [];
    const p = pezzi.find(x => String(x.articolo_id) === String(v.aid));
    if (p) return p;
    // foglio coi pezzi gia' confermati: nella distinta ci sono i suoi pezzi
    const figli = pezzi.filter(x => x.foglio && String(x.foglio.articolo_id) === String(v.aid));
    if (!figli.length) return {};
    const f = figli[0];
    return { codice: f.foglio.codice, articolo_id: v.aid, materiale: f.materiale, spessore: f.spessore,
             quantita: null, misure: null, lantek: f.lantek, decisione: 'piu_pezzi_confermati', figli };
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
    st.modo = 'vedi'; st.cand = []; st.sel = 0; st.vb = null; st.pf = null;
    barra();
    lato();
    const v = voce();
    const f = document.getElementById('lz-ctl-foglio');
    f.classList.remove('scegli');
    const k = chiave(v);
    const pronto = st.letture[k] && st.letture[k].stato !== 'carica';
    if (!pronto) f.innerHTML = '<div class="lz-foglio-msg"><div class="lz-skel"></div></div>';
    const disegna = rec => {
      if (!st || chiave(voce()) !== k) return;
      if (rec.stato !== 'ok') {
        f.innerHTML = `<div class="lz-foglio-msg"><div><b>Non riesco a mostrare il disegno</b><p>${esc(rec.d.error || 'Errore del server')}</p></div></div>`;
      } else {
        f.innerHTML = rec.d.svg + `<div class="lz-leg">${rec.d.contorno_tuo ? '<i></i>In verde: il contorno che hai scelto tu' : rec.d.ha_contorno ? '<i></i>In verde: il pezzo che taglierei' : 'Il motore non ha trovato un contorno: sceglilo tu (S)'}</div>
          <div class="lz-zoom"><button class="lz-btn" type="button" data-z="piu" title="Ingrandisci" aria-label="Ingrandisci">${LZ.ico('plus')}</button>
          <button class="lz-btn" type="button" data-z="meno" title="Rimpicciolisci" aria-label="Rimpicciolisci">${LZ.ico('minus')}</button>
          <button class="lz-btn" type="button" data-z="tutto" title="Foglio intero (0)" aria-label="Foglio intero">${LZ.ico('scan')}</button></div>`;
        LZ.icone(f);
        const svg = f.querySelector('svg');
        if (svg) { const b = svg.viewBox.baseVal; st.vb = { x: b.x, y: b.y, w: b.width, h: b.height, base: [b.x, b.y, b.width, b.height] }; }
        if (st.modo === 'pezzi') disegnaPezzi();
      }
      lato();
    };
    if (pronto) disegna(st.letture[k]); else lettura(v).then(disegna);

    // i prossimi intanto si preparano
    for (let j = 1; j <= 3; j++) if (st.coda[st.i + j]) lettura(st.coda[st.i + j]);
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
      <span class="lz-salva" id="lz-ctl-salva" aria-live="polite">${salvaInCorso ? `Salvo ${salvaInCorso}…` : ''}</span>
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
    const rigaOrd = lt && lt.riga_ordine ? rigaOrdine(lt.riga_ordine) : '';
    let corpo;
    if (st.modo === 'pezzi') {
      corpo = pezziHtml();
    } else if (st.modo === 'scegli') {
      corpo = `<div class="lz-scegli-guida"><b>Clicca il bordo del pezzo giusto</b>sul disegno a sinistra. ${st.cand.length > 1 ? 'Ci sono più contorni possibili: scegli quello giusto (1, 2, 3…).' : ''}</div>
        ${st.cand.length ? `<div class="lz-decidi">${st.cand.map((c, k) => `<button class="lz-btn${k === st.sel ? ' pri' : ''}" type="button" data-cand="${k}">
            <span>${k + 1}. ${esc(LZ.misure([c.bbox_width_mm, c.bbox_height_mm]))}</span><span class="num">${FT.numero(c.area_dm2, 2)} dm²</span></button>`).join('')}</div>` : ''}
        <div class="lz-decidi">
          <button class="lz-btn pri" type="button" data-c="conferma-scelta" ${st.cand.length ? '' : 'disabled'}><span>${LZ.ico('check')}Conferma questo contorno</span>${LZ.tasto('Invio')}</button>
          <button class="lz-btn" type="button" data-c="annulla-scelta"><span>${LZ.ico('undo-2')}Torna al contorno del motore</span>${LZ.tasto('Esc')}</button></div>`;
    } else if (p.figli || st.risposte[chiave(v)] === 'piu_pezzi_confermati') {
      // foglio coi pezzi gia' confermati: si rivedono o si annullano
      const figli = p.figli || [];
      corpo = `<div class="lz-risposta">Pezzi del foglio <b>confermati</b>${figli.length ? `: ${figli.length} pezzi per Lantek` : ''}.</div>
        ${figli.length ? `<ol class="lz-pf-fatti">${figli.map(x => `<li><span class="mono">${esc(x.codice)}</span><span>${FT.numero(x.quantita)} pz</span></li>`).join('')}</ol>` : ''}
        <div class="lz-decidi">
          <button class="lz-btn pri" type="button" data-c="pezzi"><span>${LZ.ico('copy')}Rivedi i pezzi del foglio</span>${LZ.tasto('P')}</button>
          <button class="lz-btn" type="button" data-c="pf-annulla"><span>${LZ.ico('undo-2')}Annulla i pezzi: torna da controllare</span></button></div>`;
    } else {
      corpo = `${perche.length ? `<div class="lz-perche"><b>Perché te lo chiedo</b><ul>${perche.map(x => `<li>${esc(x)}</li>`).join('')}</ul></div>`
          : p.motore === 'sicuro' ? `<div class="lz-sicuro">${LZ.ico('circle-check')}Il motore è sicuro di questo disegno.</div>` : ''}
        ${lt && lt.suggerimento && !risposta ? `<div class="lz-risposta">Il motore pensa: <b>${esc(SCELTE[lt.suggerimento])}</b>. Decidi tu col tasto.</div>` : ''}
        ${risposta ? `<div class="lz-risposta">Hai risposto: <b>${esc(SCELTE[risposta] || risposta)}</b></div>` : ''}
        <div class="lz-decidi">
          <button class="lz-btn pri" type="button" data-c="giusto" ${lt && !lt.ha_contorno && nuovo ? 'disabled' : ''}><span>${LZ.ico('check')}Sì, è il pezzo giusto</span>${LZ.tasto('Invio')}</button>
          <button class="lz-btn" type="button" data-c="scegli"><span>${LZ.ico('mouse-pointer-click')}No, lo scelgo io sul disegno</span>${LZ.tasto('S')}</button>
          <button class="lz-btn${lt && lt.suggerimento === 'sviluppo' ? ' suggerito' : ''}" type="button" data-c="sviluppo"><span>${LZ.ico('move-diagonal')}Va sviluppato</span>${LZ.tasto('V')}</button>
          <button class="lz-btn${lt && lt.suggerimento === 'piu_pezzi' ? ' suggerito' : ''}" type="button" data-c="pezzi"><span>${LZ.ico('copy')}${risposta === 'piu_pezzi_confermati' ? 'Rivedi i pezzi del foglio' : 'Ci sono più pezzi'}</span>${LZ.tasto('P')}</button>
          <button class="lz-btn${lt && lt.suggerimento === 'non_laser' ? ' suggerito' : ''}" type="button" data-c="non_laser"><span>${LZ.ico('circle-minus')}Non è da laser</span>${LZ.tasto('N')}</button></div>`;
    }
    LZ.metti(document.getElementById('lz-ctl-lato'), `
      <div class="ord"><b>${esc(o.cliente || '')}</b><span class="mono">${esc(LZ.numero(o))}</span></div>
      <div class="cod">${esc(p.codice || '')}</div>
      ${st.modo === 'pezzi' ? '' : dati}${rigaOrd}${corpo}
      ${st.attesa ? '<div class="lz-sicuro"><span class="lz-gira" style="display:inline-grid">' + LZ.ico('loader-circle') + '</span>Salvo…</div>' : ''}
      ${st.solo ? '' : `<div class="lz-fondo"><button class="lz-btn" type="button" data-c="indietro" ${st.i ? '' : 'disabled'}>${LZ.ico('arrow-left')}Indietro</button>
        <button class="lz-btn" type="button" data-c="salta" ${st.i < st.coda.length - 1 ? '' : 'disabled'}>Salta${LZ.ico('arrow-right')}</button></div>`}`);
  }

  /** La riga dell'ordine come l'ha scritta il cliente (server: riga_ordine_di).
      Senza righe non si mostra niente. */
  function rigaOrdine(r) {
    const righe = (r.righe || []).filter(x => x && (x.descrizione || x.quantita != null));
    if (!righe.length) return '';
    const num = v => (v == null ? '' : String(FT.numero(Number(v), Number(v) % 1 ? 2 : 0)));
    const ms = [r.materiale ? String(r.materiale).replace(/_/g, ' ') : '', r.spessore_mm ? LZ.mm(r.spessore_mm) : ''].filter(Boolean).join(' · ');
    return `<div class="lz-riga-ord" aria-label="Riga dell'ordine del cliente">
      <div class="tit"><b>Riga dell'ordine</b><span class="mono">${esc(r.codice || '')}</span></div>
      ${righe.map(x => `<div class="r">
        <div class="q">${x.pos != null ? `<span>Pos. ${esc(String(x.pos))}</span>` : ''}${x.quantita != null ? `<b>${esc(num(x.quantita))} pz</b>` : ''}${x.commessa ? `<span>comm. ${esc(x.commessa)}</span>` : ''}</div>
        ${x.descrizione ? `<div class="d">${esc(x.descrizione)}</div>` : ''}</div>`).join('')}
      ${ms ? `<div class="ms">Nell'ordine: ${esc(ms)}</div>` : ''}</div>`;
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
      if (st.modo === 'pezzi') {
        const q = e.target.closest && e.target.closest('.fg-pfp');
        if (q) selezionaPezzo(Number(q.dataset.pf));
        else if (f.contains(e.target) && !e.target.closest('.lz-zoom') && !st.attesa) aggiungiDaClic(e.clientX, e.clientY);
        return;
      }
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
    if (leg) { leg.classList.remove('blu'); leg.innerHTML = rec && rec.d && rec.d.contorno_tuo ? '<i></i>In verde: il contorno che hai scelto tu' : rec && rec.d && rec.d.ha_contorno ? '<i></i>In verde: il pezzo che taglierei' : 'Il motore non ha trovato un contorno: sceglilo tu (S)'; }
    lato();
  }

  // ── Pezzi del foglio (P) ────────────────────────────────────────────
  const urlPf = v => `/api/orders/${encodeURIComponent(v.id)}/pezzi/${encodeURIComponent(v.aid)}/pezzi-foglio`;
  const coloreDi = i => COLORI_PF[i % COLORI_PF.length];

  async function entraPezzi() {
    if (!st || st.attesa || st.modo === 'pezzi') return;
    if (st.modo === 'scegli') esciScegli();
    const v = voce(), k = chiave(v);
    st.modo = 'pezzi';
    st.pf = { carica: true };
    const f = document.getElementById('lz-ctl-foglio');
    f.classList.add('scegli');
    const leg = f.querySelector('.lz-leg');
    if (leg) { leg.classList.add('blu'); leg.innerHTML = '<i></i>Pezzi del foglio: clicca un pezzo per sceglierlo, il bordo di uno mancante per aggiungerlo'; }
    lato();
    const r = await LZ.get(urlPf(v));
    if (!st || chiave(voce()) !== k || st.modo !== 'pezzi') return;
    if (!r.ok) { st.pf = { errore: r.d.error || 'Pezzi non letti' }; lato(); return; }
    let prossimo = 0;
    st.pf = { codiceFoglio: r.d.codice_foglio || '', qtaFoglio: r.d.qta_foglio || 1, confermati: !!r.d.confermati,
              altri: new Set((r.d.altri_codici || []).map(x => String(x).toLowerCase())), sel: null,
              pezzi: (r.d.pezzi || []).map(p => ({ ...p, colore: coloreDi(prossimo++), qtaAMano: !!r.d.confermati })) };
    st.pf.prossimo = prossimo;
    disegnaPezzi();
    lato();
  }
  function esciPezzi() {
    if (!st) return;
    st.modo = 'vedi'; st.pf = null;
    const f = document.getElementById('lz-ctl-foglio');
    f.classList.remove('scegli');
    const g = f.querySelector('.fg-pf'); if (g) g.remove();
    const rec = st.letture[chiave(voce())];
    const leg = f.querySelector('.lz-leg');
    if (leg) { leg.classList.remove('blu'); leg.innerHTML = rec && rec.d && rec.d.contorno_tuo ? '<i></i>In verde: il contorno che hai scelto tu' : rec && rec.d && rec.d.ha_contorno ? '<i></i>In verde: il pezzo che taglierei' : 'Il motore non ha trovato un contorno: sceglilo tu (S)'; }
    lato();
  }

  /** I pezzi sul disegno grande: contorno colorato, numero, copie tratteggiate. */
  function disegnaPezzi() {
    const s = svgEl();
    if (!s || !st || !st.pf || !st.pf.pezzi) return;
    let g = s.querySelector('.fg-pf');
    if (!g) { g = document.createElementNS('http://www.w3.org/2000/svg', 'g'); g.setAttribute('class', 'fg-pf'); s.appendChild(g); }
    const d = an => 'M' + an.map(([x, y]) => `${x} ${-y}`).join('L') + 'Z';
    const fs = (st.vb ? st.vb.base[2] : 500) / 38;
    g.innerHTML = st.pf.pezzi.map((p, i) => {
      const xs = p.outer_xy.map(q => q[0]), ys = p.outer_xy.map(q => q[1]);
      const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cy = (Math.min(...ys) + Math.max(...ys)) / 2;
      const lab = `${i + 1}${p.qta_per_foglio > 1 ? ' ×' + p.qta_per_foglio : ''}`;
      const on = st.pf.sel === i ? ' on' : '';
      return `<path class="fg-pfp${on}" data-pf="${i}" d="${[p.outer_xy].concat(p.holes_xy || []).map(d).join('')}" fill-rule="evenodd"
          style="stroke:${p.colore};fill:${p.colore}" vector-effect="non-scaling-stroke"/>
        ${(p.copie || []).map(c => `<path class="fg-pfc" d="${d(c)}" style="stroke:${p.colore}" vector-effect="non-scaling-stroke"/>`).join('')}
        <text class="fg-pfn" x="${cx}" y="${-cy}" font-size="${fs}" stroke-width="${fs / 7}" text-anchor="middle" dominant-baseline="central" style="fill:${p.colore}">${esc(lab)}</text>`;
    }).join('');
  }

  function pezziHtml() {
    const pf = st.pf || {};
    if (pf.carica) return `<div class="lz-sicuro"><span class="lz-gira" style="display:inline-grid">${LZ.ico('loader-circle')}</span>Cerco i pezzi nel foglio…</div>`;
    if (pf.errore) {
      return `<div class="lz-perche"><b>Pezzi non letti</b>${esc(pf.errore)}</div>
        <div class="lz-decidi"><button class="lz-btn" type="button" data-c="pf-mano"><span>${LZ.ico('hand')}Lo faccio a mano in Lantek</span>${LZ.tasto('M')}</button>
        <button class="lz-btn" type="button" data-c="pf-esci"><span>${LZ.ico('undo-2')}Torna al disegno</span>${LZ.tasto('Esc')}</button></div>`;
    }
    const pz = pf.pezzi || [];
    const tot = pz.reduce((a, p) => a + (Number(p.qta) || 0), 0);
    const lista = pz.length ? `<ol class="lz-pf">${pz.map((p, i) => `<li class="${pf.sel === i ? 'on' : ''}" data-pf-sel="${i}">
        <span class="n" style="background:${p.colore}">${i + 1}</span>
        <span class="campi">
          <input class="cod" type="text" maxlength="60" value="${esc(p.codice)}" data-pf-cod="${i}" data-fuoco="pf-cod-${i}" aria-label="Codice del pezzo ${i + 1}" spellcheck="false">
          <span class="mis">${esc(LZ.misure(p.misure) || '')}${p.codice_da === 'disegno' ? ' · codice letto sul disegno' : ''}</span>
          <span class="qq"><label>per foglio <input type="number" min="1" max="9999" value="${p.qta_per_foglio}" data-pf-qpf="${i}" data-fuoco="pf-qpf-${i}"></label>
            <label>da tagliare <input type="number" min="1" max="999999" value="${p.qta}" data-pf-qta="${i}" data-fuoco="pf-qta-${i}"></label></span>
        </span>
        <button class="lz-btn ghost lz-icona piccolo" type="button" data-pf-togli="${i}" title="Non è un pezzo (vista, cornice): toglilo" aria-label="Togli il pezzo ${i + 1}">${LZ.ico('x')}</button></li>`).join('')}</ol>`
      : '<div class="lz-risposta">Nessun pezzo: clicca il bordo di un pezzo sul disegno per aggiungerlo.</div>';
    return `<div class="lz-scegli-guida"><b>Pezzi del foglio</b>Togli con × le viste e le cornici. Manca un pezzo? Clicca il suo bordo sul disegno.
        ${pf.confermati ? ' <em>Già confermati: se cambi qualcosa, conferma di nuovo.</em>' : ''}</div>
      ${lista}
      ${pz.length ? `<div class="lz-pf-tot">${LZ.plur(pz.length, 'pezzo diverso', 'pezzi diversi')} · ${FT.numero(tot)} da tagliare · ${FT.numero(pf.qtaFoglio)} ${pf.qtaFoglio === 1 ? 'foglio' : 'fogli'} nell'ordine</div>` : ''}
      <div class="lz-decidi">
        <button class="lz-btn pri" type="button" data-c="pf-conferma" ${pz.length ? '' : 'disabled'}><span>${LZ.ico('check')}Conferma ${LZ.plur(pz.length, 'pezzo', 'pezzi')} per Lantek</span>${LZ.tasto('Invio')}</button>
        <button class="lz-btn" type="button" data-c="pf-mano"><span>${LZ.ico('hand')}No, lo faccio a mano in Lantek</span>${LZ.tasto('M')}</button>
        ${pf.confermati ? `<button class="lz-btn" type="button" data-c="pf-annulla"><span>${LZ.ico('undo-2')}Annulla i pezzi: torna da controllare</span></button>` : ''}
        <button class="lz-btn" type="button" data-c="pf-esci"><span>${LZ.ico('arrow-left')}Torna al disegno</span>${LZ.tasto('Esc')}</button></div>`;
  }

  /** Codice e quantita' scritti nell'elenco: si aggiorna lo stato senza
      ridisegnare il pannello (il cursore resta dov'e'). */
  function alCambio(e) {
    if (!st || !st.pf || !st.pf.pezzi) return;
    const t = e.target, d = t.dataset || {};
    const n = v => Math.max(0, Math.round(Number(String(v).replace(',', '.')) || 0));
    if (d.pfCod != null) { const p = st.pf.pezzi[Number(d.pfCod)]; if (p) p.codice = t.value; return; }
    if (d.pfQpf != null) {
      const p = st.pf.pezzi[Number(d.pfQpf)];
      if (!p) return;
      p.qta_per_foglio = n(t.value);
      if (!p.qtaAMano) {
        p.qta = p.qta_per_foglio * st.pf.qtaFoglio;
        const q = document.querySelector(`#lz-ctl-lato [data-pf-qta="${d.pfQpf}"]`);
        if (q) q.value = p.qta;
      }
      disegnaPezzi();
    } else if (d.pfQta != null) {
      const p = st.pf.pezzi[Number(d.pfQta)];
      if (!p) return;
      p.qta = n(t.value);
      p.qtaAMano = true;
    } else return;
    const tot = document.querySelector('#lz-ctl-lato .lz-pf-tot');
    if (tot) {
      const somma = st.pf.pezzi.reduce((a, p) => a + (Number(p.qta) || 0), 0);
      tot.textContent = `${LZ.plur(st.pf.pezzi.length, 'pezzo diverso', 'pezzi diversi')} · ${FT.numero(somma)} da tagliare · ${FT.numero(st.pf.qtaFoglio)} ${st.pf.qtaFoglio === 1 ? 'foglio' : 'fogli'} nell'ordine`;
    }
  }

  function selezionaPezzo(i) {
    if (!st || !st.pf) return;
    st.pf.sel = i;
    disegnaPezzi();
    lato();
    const inp = document.querySelector(`#lz-ctl-lato [data-pf-cod="${i}"]`);
    if (inp) inp.scrollIntoView({ block: 'nearest' });
  }
  function togliPezzo(i) {
    if (!st || !st.pf || !st.pf.pezzi[i]) return;
    const via = st.pf.pezzi.splice(i, 1)[0];
    st.pf.sel = null;
    disegnaPezzi(); lato();
    FT.toast(`Tolto il pezzo ${i + 1} (${via.codice})`, '', { testo: 'Rimetti', fn: () => {
      if (!st || !st.pf) return;
      st.pf.pezzi.splice(Math.min(i, st.pf.pezzi.length), 0, via); disegnaPezzi(); lato();
    } }, 6000);
  }
  /** Un clic sul disegno in "pezzi del foglio": sopra un pezzo lo seleziona,
      sul bordo di un contorno che non c'e' lo aggiunge (come il tasto S). */
  async function aggiungiDaClic(cx, cy) {
    const p = puntoSvg(cx, cy);
    if (!p || !st || !st.pf || !st.pf.pezzi) return;
    const v = voce(), k = chiave(v);
    st.attesa = true; lato();
    const r = await LZ.post(`/api/orders/${encodeURIComponent(v.id)}/pezzi/${encodeURIComponent(v.aid)}/cad/pick-candidates`, { x: p.u, y: -p.v });
    if (!st || chiave(voce()) !== k || st.modo !== 'pezzi') return;
    st.attesa = false;
    const c = r.ok && (r.d.candidates || [])[0];
    if (!c) { lato(); FT.toast('Qui non trovo un contorno chiuso: clicca proprio sul bordo del pezzo', 'warn'); return; }
    const pf = st.pf;
    const usati = new Set(pf.pezzi.map(x => String(x.codice).toLowerCase()));
    let n = pf.pezzi.length + 1, cod = `${pf.codiceFoglio}-${n}`;
    while (usati.has(cod.toLowerCase()) || pf.altri.has(cod.toLowerCase())) cod = `${pf.codiceFoglio}-${++n}`;
    pf.pezzi.push({ codice: cod, codice_da: 'aggiunto', outer_xy: c.outer_xy, holes_xy: c.holes_xy || [], copie: [],
                    qta_per_foglio: 1, qta: pf.qtaFoglio, misure: [c.bbox_width_mm, c.bbox_height_mm],
                    colore: coloreDi(pf.prossimo++) });
    pf.sel = pf.pezzi.length - 1;
    disegnaPezzi(); lato();
    FT.toast(`Aggiunto il pezzo ${pf.pezzi.length}: ${LZ.misure([c.bbox_width_mm, c.bbox_height_mm])}`, 'ok');
  }

  /** Controlli prima di mandare: codici scritti, diversi fra loro e dagli altri dell'ordine. */
  function erroriPezzi() {
    const visti = new Set();
    for (const [i, p] of st.pf.pezzi.entries()) {
      const c = String(p.codice || '').trim();
      if (!c) return { i, t: `Pezzo ${i + 1}: scrivi il codice` };
      if (visti.has(c.toLowerCase())) return { i, t: `Il codice ${c} è su due pezzi: ogni pezzo vuole il suo` };
      if (st.pf.altri.has(c.toLowerCase())) return { i, t: `Il codice ${c} c'è già in quest'ordine` };
      if (!(Number(p.qta) >= 1) || !(Number(p.qta_per_foglio) >= 1)) return { i, t: `Pezzo ${i + 1}: quantità non valida` };
      visti.add(c.toLowerCase());
    }
    return null;
  }
  async function confermaPezzi() {
    if (!st || !st.pf || !st.pf.pezzi || !st.pf.pezzi.length || st.attesa) return;
    const err = erroriPezzi();
    if (err) {
      FT.toast(err.t, 'warn');
      selezionaPezzo(err.i);
      const inp = document.querySelector(`#lz-ctl-lato [data-pf-cod="${err.i}"]`);
      if (inp) inp.focus();
      return;
    }
    const v = voce(), k = chiave(v), p = pezzo(v);
    const corpo = { pezzi: st.pf.pezzi.map(x => ({ codice: String(x.codice).trim(), outer_xy: x.outer_xy, holes_xy: x.holes_xy || [],
                                                  qta_per_foglio: Number(x.qta_per_foglio), qta: Number(x.qta) })) };
    st.attesa = true; lato();
    const r = await LZ.post(urlPf(v), corpo);
    if (!st) return;
    st.attesa = false;
    if (!r.ok) { lato(); FT.toast('Pezzi non confermati: ' + (r.d.error || 'errore del server'), 'err', null, 9000); return; }
    FT.toast(`${p.codice || 'Foglio'}: ${LZ.plur(r.d.n_pezzi, 'pezzo', 'pezzi')} per Lantek`, 'ok');
    st.risposte[k] = 'piu_pezzi_confermati';
    st.fatte++;
    p.decisione = 'piu_pezzi_confermati';
    p.da_controllare = false;
    const g = document.querySelector('#lz-ctl-foglio .fg-pf'); if (g) g.remove();
    st.modo = 'vedi'; st.pf = null;
    avanti(k);
  }
  async function annullaPezzi() {
    if (!st || st.attesa) return;
    const v = voce(), k = chiave(v);
    const ok = await FT.conferma({ titolo: 'Annullare i pezzi del foglio?', ok: 'Sì, annulla',
      testo: 'I DXF dei pezzi si tolgono e il disegno torna da controllare (anche "a mano in Lantek").' });
    if (!ok || !st) return;
    const r = await fetch(urlPf(v), { method: 'DELETE' }).then(async x => ({ ok: x.ok, d: await x.json().catch(() => ({})) }))
      .catch(() => ({ ok: false, d: { error: 'Server non raggiungibile' } }));
    if (!r.ok) { FT.toast('Non annullato: ' + (r.d.error || 'errore'), 'err'); return; }
    FT.toast('Pezzi del foglio annullati: il disegno è di nuovo da controllare', 'ok');
    delete st.risposte[k];
    st.fatte++;
    C.chiudi();
  }

  // ── Risposte ────────────────────────────────────────────────────────
  async function rispondi(scelta) {
    if (!st || st.attesa) return;
    const v = voce(), p = pezzo(v), k = chiave(v);
    const base = `/api/orders/${encodeURIComponent(v.id)}/pezzi/${encodeURIComponent(v.aid)}`;
    let url, corpo;
    if (scelta === 'giusto') {
      url = base + '/giusto'; corpo = { serve_pulito: p.lantek === 'nuovo' || p.lantek === 'sconosciuto' };
    } else if (scelta === 'scelto') {
      const c = st.cand[st.sel];
      if (!c) return;
      url = base + '/contorno'; corpo = { outer_xy: c.outer_xy, holes_xy: c.holes_xy || [], pagina: 'laser' };
    } else {
      url = base + '/decisione'; corpo = { scelta };
    }
    const prima = { decisione: p.decisione, da_controllare: p.da_controllare };
    st.risposte[k] = scelta;
    st.fatte++;
    p.decisione = scelta;                 // la riga dei pezzi lo mostra subito (poi si rilegge)
    p.da_controllare = false;
    accoda(k, p, url, corpo, prima);
    avanti(k);
  }

  /** Dopo una risposta: il disegno dopo (o si chiude). */
  function avanti(k) {
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

  /** Salva una risposta in sottofondo; se non riesce, il disegno torna da controllare. */
  function accoda(k, p, url, corpo, prima) {
    salvaInCorso++;
    segnaSalvataggi();
    codaSalva = codaSalva.then(async () => {
      const r = await LZ.post(url, corpo);
      salvaInCorso--;
      segnaSalvataggi();
      if (!r.ok) {
        p.decisione = prima.decisione;
        p.da_controllare = prima.da_controllare;
        if (st && st.risposte[k]) { delete st.risposte[k]; st.fatte = Math.max(0, st.fatte - 1); barra(); lato(); }
        FT.toast(`${p.codice || 'Pezzo'}: risposta non salvata (${r.d.error || 'errore del server'})`, 'err',
          { testo: 'Riprova', fn: () => accoda(k, p, url, corpo, prima) }, 12000);
        return;
      }
      if (r.d.pronto_lantek === false && r.d.motivo) FT.toast(`${p.codice}: ${r.d.motivo}`, 'warn', null, 9000);
    });
  }
  function segnaSalvataggi() {
    const el = document.getElementById('lz-ctl-salva');
    if (el) el.textContent = salvaInCorso ? `Salvo ${salvaInCorso}…` : '';
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
    const tg = e.target.closest('[data-pf-togli]');
    if (tg) { togliPezzo(Number(tg.dataset.pfTogli)); return; }
    const ps = e.target.closest('[data-pf-sel]');
    if (ps && !e.target.closest('input, button')) { selezionaPezzo(Number(ps.dataset.pfSel)); return; }
    if (!c) return;
    const a = c.dataset.c;
    if (a === 'pezzi') entraPezzi();
    else if (a === 'pf-esci') esciPezzi();
    else if (a === 'pf-conferma') confermaPezzi();
    else if (a === 'pf-annulla') annullaPezzi();
    else if (a === 'pf-mano') { st.modo = 'vedi'; st.pf = null; rispondi('piu_pezzi'); }
    else if (a === 'chiudi') C.chiudi();
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
    const inCampo = e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName || '');
    if (st.modo === 'pezzi') {
      // nei campi (codice, quantita') si scrive; Invio/Esc ne escono
      if (inCampo) {
        if (k === 'Enter' || k === 'Escape') { e.target.blur(); return true; }
        return false;
      }
      if (k === 'Escape') { esciPezzi(); return true; }
      if (st.attesa) return true;
      if (k === 'Enter') { confermaPezzi(); return true; }
      if (k === 'm') { st.modo = 'vedi'; st.pf = null; rispondi('piu_pezzi'); return true; }
      if ((k === 'Delete' || k === 'Backspace') && st.pf && st.pf.sel != null) { togliPezzo(st.pf.sel); return true; }
      if (k === '0') { tutto(); return true; }
      if (k === '+' || k === '=') { zoom(1 / 1.4); return true; }
      if (k === '-') { zoom(1.4); return true; }
      return k === ' ' || k === 'Tab' ? false : true;
    }
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
      if (k === 'p') { entraPezzi(); return true; }
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
