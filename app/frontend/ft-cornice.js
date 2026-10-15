/* FerroTrack — la cornice uguale in ogni stazione (15/10/2026).

   Tela "Un'app sola" (approvata da Stefano il 14/10): menu a sinistra con
   logo, nome e icona della stazione, chi sei ed Esci in fondo; in alto la
   stessa barra con cerca (Ctrl K), Da procurare, notifiche, segnala.
   Una stazione si riconosce dal nome e dall'icona, non da colori diversi.

   Come la usa una pagina (stile in ft-ui.css, sezione "Cornice unica"):

     <body class="ft ftc" data-stazione="laser">
     <nav class="ftc-side" aria-label="Laser">
       <div class="ftc-menu"> ...le SUE voci: <button class="ft-tab">... </div>
     </nav>
     <header class="ft-topbar"> ...<div class="ft-topbar-end">...</div> </header>

   Qui si aggiungono: logo, scheda della stazione, piede con chi sei ed Esci,
   la casella "Cerca" (se la pagina non ha la sua) e, sotto i 1100 px, il
   pulsante che apre il menu. La ricerca chiede i risultati alla pagina:
     FTC.cerca = q => [{ t: 'titolo', s: 'sotto', st: '<span>stato</span>', fn() {} }]
   Esci: window.logout() se la pagina ce l'ha, se no FTA (ft-accesso.js). */
(function () {
  'use strict';
  if (window.FTC) return;

  const svg = d => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
  // Le icone delle stazioni, le stesse della tela
  const STAZIONI = {
    laser: { nome: 'Laser', home: '/laser.html', ico: '<path d="M12 3v7"/><path d="M8 14l4-4 4 4"/><path d="M4 20h16"/>' },
    ufficio: { nome: 'Ufficio', home: '/impiegata.html', ico: '<rect x="3" y="7" width="18" height="13" rx="2"/><path d="M9 7V5h6v2"/>' },
    capo: { nome: 'Capo officina', home: '/capo-officina.html', ico: '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L4 17l3 3 5.3-5.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-.6-.6-2.4z"/>' },
    commerciale: { nome: 'Commerciale', home: '/preventivi.html', ico: '<path d="M7 3h7l4 4v14H7z"/><path d="M10 12h5M10 16h5"/>' },
    tablet: { nome: 'Tablet officina', home: '/operaio-info.html', ico: '<rect x="5" y="3" width="14" height="18" rx="2"/><path d="M11 18h2"/>' },
    timbratrice: { nome: 'Timbratrice', home: '/ore.html', ico: '<circle cx="12" cy="12" r="8"/><path d="M12 8v4l3 2"/>' },
  };
  const ICO = {
    cerca: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
    menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  };

  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const FTC = window.FTC = { cerca: null, stazioni: STAZIONI };

  function esci() {
    if (typeof window.logout === 'function') { window.logout(); return; }
    const io = window.FTA && window.FTA.io;
    if (window.FTA && io && !io.persona && io.registrato) { window.FTA.cambiaDispositivo(); return; }
    if (window.FTA) window.FTA.esci(); else location.href = '/';
  }
  FTC.esci = esci;

  function monta() {
    const side = document.querySelector('.ftc-side');
    if (!side || side.dataset.montata) return;
    side.dataset.montata = '1';
    const st = STAZIONI[document.body.dataset.stazione] || { nome: document.body.dataset.stazione || 'FerroTrack', home: '/', ico: '' };
    side.id = side.id || 'ftc-side';
    side.insertAdjacentHTML('afterbegin', `
      <a class="ftc-brand" href="${esc(st.home)}" title="${esc(st.nome)}: torna all'inizio"><span class="ft-logo">LS</span><span class="ftc-brand-nome">FerroTrack</span></a>
      <div class="ftc-stazione"><span class="ftc-st-ico">${svg(st.ico)}</span><span><b>${esc(st.nome)}</b><small>stazione</small></span></div>`);
    side.insertAdjacentHTML('beforeend', `
      <div class="ftc-piede">
        <span class="ft-avatar" id="ft-user-avatar" aria-hidden="true">?</span>
        <span class="ftc-chi"><b id="ft-user-name">—</b><small id="ftc-dispositivo"></small><small id="ft-user-role"></small></span>
        <button class="ftc-esci" type="button" title="Esci da FerroTrack">Esci</button>
      </div>`);
    side.querySelector('.ftc-esci').addEventListener('click', esci);
    const velo = document.createElement('div');
    velo.className = 'ftc-velo';
    velo.addEventListener('click', () => chiudiMenu());
    side.after(velo);
    // sul tablet: una voce scelta chiude il menu
    side.addEventListener('click', e => { if (e.target.closest('.ft-tab, .ftc-cta')) chiudiMenu(); });

    const bar = document.querySelector('.ft-topbar');
    if (bar) {
      const apri = document.createElement('button');
      apri.type = 'button';
      apri.className = 'ftc-tondo ftc-apri';
      apri.setAttribute('aria-label', 'Apri il menu');
      apri.setAttribute('aria-controls', side.id);
      apri.innerHTML = svg(ICO.menu);
      apri.addEventListener('click', () => document.body.classList.contains('ftc-menu-aperto') ? chiudiMenu() : apriMenu());
      bar.insertBefore(apri, bar.firstChild);
      if (!bar.querySelector('.ftc-cerca, .imp-cerca')) mettiCerca(bar, apri);
    }
    dispositivo();
  }

  function apriMenu() {
    document.body.classList.add('ftc-menu-aperto');
    const v = document.querySelector('.ftc-side .ft-tab.active, .ftc-side .ft-tab, .ftc-side a');
    if (v) setTimeout(() => v.focus(), 30);
  }
  function chiudiMenu() { document.body.classList.remove('ftc-menu-aperto'); }

  /** Sotto il nome: su quale PC si e' (es. "PC laser"). */
  async function dispositivo() {
    if (!window.FTA || !window.FTA.pronto) return;
    try { await window.FTA.pronto; } catch (_) { return; }
    const io = window.FTA.io || {};
    const el = document.getElementById('ftc-dispositivo');
    if (el) el.textContent = (io.dispositivo && io.dispositivo.nome) || io.stazione_nome || '';
    const nome = document.getElementById('ft-user-name');
    if (nome && (!nome.textContent || nome.textContent === '—') && window.FTA.nome) {
      const n = window.FTA.nome();
      if (n) { nome.textContent = n; const av = document.getElementById('ft-user-avatar'); if (av && window.FT) av.textContent = window.FT.iniziali(n); }
    }
  }

  // ── Cerca (Ctrl K) ──────────────────────────────────────────────────
  let risultati = [], scelto = -1;
  function mettiCerca(bar, dopo) {
    const box = document.createElement('div');
    box.className = 'ftc-cerca';
    box.innerHTML = `<label class="ftc-cerca-box">${svg(ICO.cerca)}
        <input type="search" id="ftc-cerca" autocomplete="off" spellcheck="false" placeholder="${esc(document.body.dataset.cercaTesto || 'Cerca un ordine: numero o cliente')}"
          aria-label="Cerca" aria-controls="ftc-cerca-res" aria-expanded="false" role="combobox" aria-autocomplete="list">
        <kbd>Ctrl K</kbd></label>
      <div class="ftc-cerca-res" id="ftc-cerca-res" role="listbox" aria-label="Risultati"></div>`;
    dopo.after(box);
    const inp = box.querySelector('input');
    inp.addEventListener('input', () => cerca(inp.value));
    inp.addEventListener('focus', () => { if (inp.value.trim()) cerca(inp.value); });
    inp.addEventListener('keydown', e => {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault(); e.stopPropagation();
        if (!risultati.length) return;
        scelto = (scelto + (e.key === 'ArrowDown' ? 1 : -1) + risultati.length) % risultati.length;
        segna();
      } else if (e.key === 'Enter') {
        e.preventDefault(); e.stopPropagation();
        vai(scelto >= 0 ? scelto : 0);
      } else if (e.key === 'Escape') {
        e.preventDefault(); e.stopPropagation();
        if (inp.value) { inp.value = ''; chiudiRes(); } else inp.blur();
      }
    });
    box.querySelector('.ftc-cerca-res').addEventListener('mousedown', e => {
      const b = e.target.closest('button[data-i]');
      if (b) { e.preventDefault(); vai(Number(b.dataset.i)); }
    });
    document.addEventListener('mousedown', e => { if (!e.target.closest('.ftc-cerca')) chiudiRes(); });
  }
  function cerca(q) {
    const res = document.getElementById('ftc-cerca-res');
    const inp = document.getElementById('ftc-cerca');
    q = String(q || '').trim();
    if (!q) { chiudiRes(); return; }
    let l = [];
    try { l = typeof FTC.cerca === 'function' ? (FTC.cerca(q) || []) : []; } catch (_) { l = []; }
    risultati = l.slice(0, 12);
    scelto = risultati.length ? 0 : -1;
    res.innerHTML = risultati.length
      ? risultati.map((r, i) => `<button type="button" role="option" data-i="${i}" id="ftc-r-${i}"><b>${esc(r.t)}</b>${r.st ? `<span class="st">${r.st}</span>` : ''}<small>${esc(r.s || '')}</small></button>`).join('')
      : `<div class="vuoto">Nessun ordine trovato per «${esc(q)}».</div>`;
    res.classList.add('open');
    inp.setAttribute('aria-expanded', 'true');
    segna();
  }
  function segna() {
    const res = document.getElementById('ftc-cerca-res');
    res.querySelectorAll('button[data-i]').forEach(b => b.classList.toggle('on', Number(b.dataset.i) === scelto));
    const inp = document.getElementById('ftc-cerca');
    if (scelto >= 0) inp.setAttribute('aria-activedescendant', 'ftc-r-' + scelto); else inp.removeAttribute('aria-activedescendant');
    const b = res.querySelector('button.on');
    if (b) b.scrollIntoView({ block: 'nearest' });
  }
  function chiudiRes() {
    const res = document.getElementById('ftc-cerca-res');
    const inp = document.getElementById('ftc-cerca');
    if (res) res.classList.remove('open');
    if (inp) inp.setAttribute('aria-expanded', 'false');
  }
  function vai(i) {
    const r = risultati[i];
    if (!r) return;
    const inp = document.getElementById('ftc-cerca');
    chiudiRes();
    if (inp) { inp.value = ''; inp.blur(); }
    try { r.fn(); } catch (_) { /* la pagina non ha potuto aprire */ }
  }
  // Ctrl K (o /) da qualsiasi punto: alla casella di ricerca della barra
  document.addEventListener('keydown', e => {
    const k = (e.key || '').toLowerCase();
    const t = e.target;
    const scrive = t && (/INPUT|TEXTAREA|SELECT/.test(t.tagName) || t.isContentEditable);
    if ((e.ctrlKey || e.metaKey) && k === 'k') {
      const inp = document.getElementById('ftc-cerca');
      if (!inp) return;                       // l'ufficio ha la sua (cerca-globale)
      e.preventDefault();
      inp.focus(); inp.select();
    } else if (k === 'escape' && document.body.classList.contains('ftc-menu-aperto')) {
      chiudiMenu();
    } else if (k === '/' && !scrive && !e.ctrlKey && !e.altKey) {
      const inp = document.getElementById('ftc-cerca');
      if (inp) { e.preventDefault(); inp.focus(); }
    }
  }, true);

  // ── Menu ⋯ della barra ──────────────────────────────────────────────
  /** voci: [{t, ico (path svg), fn}] — es. Schermo intero, Postazione. */
  FTC.altro = function (voci) {
    const end = document.querySelector('.ft-topbar .ft-topbar-end');
    if (!end || document.querySelector('.ftc-altro')) return;
    const w = document.createElement('div');
    w.className = 'ftc-altro';
    w.innerHTML = `<button type="button" class="ftc-tondo" aria-haspopup="menu" aria-expanded="false" aria-label="Altro" title="Altro">${svg('<circle cx="5" cy="12" r="1.4" fill="currentColor"/><circle cx="12" cy="12" r="1.4" fill="currentColor"/><circle cx="19" cy="12" r="1.4" fill="currentColor"/>')}</button>
      <div class="ftc-altro-pop" role="menu">${voci.map((v, i) => `<button type="button" role="menuitem" data-i="${i}">${svg(v.ico || '')}<span>${esc(v.t)}</span></button>`).join('')}</div>`;
    end.appendChild(w);
    const b = w.firstElementChild, pop = w.querySelector('.ftc-altro-pop');
    const chiudi = () => { pop.classList.remove('open'); b.setAttribute('aria-expanded', 'false'); };
    b.addEventListener('click', e => {
      e.stopPropagation();
      const ap = !pop.classList.contains('open');
      pop.classList.toggle('open', ap); b.setAttribute('aria-expanded', String(ap));
      if (ap) { const p = pop.querySelector('button'); if (p) p.focus(); }
    });
    pop.addEventListener('click', e => {
      const x = e.target.closest('[data-i]');
      if (!x) return;
      chiudi(); b.focus();
      try { voci[Number(x.dataset.i)].fn(); } catch (_) { /* niente */ }
    });
    pop.addEventListener('keydown', e => {
      const el = [...pop.querySelectorAll('button')];
      const i = el.indexOf(document.activeElement);
      if (e.key === 'ArrowDown') { e.preventDefault(); (el[i + 1] || el[0]).focus(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); (el[i - 1] || el[el.length - 1]).focus(); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); chiudi(); b.focus(); }
    });
    document.addEventListener('mousedown', e => { if (!w.contains(e.target)) chiudi(); });
  };
  FTC.ico = svg;

  // Messo subito dopo la barra in alto, si monta senza aspettare il resto
  // della pagina: niente salto della cornice al caricamento.
  if (document.querySelector('.ftc-side') && document.querySelector('.ft-topbar')) monta();
  else if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', monta);
  else monta();
  FTC.monta = monta;
})();
