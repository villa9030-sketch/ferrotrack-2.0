/* FerroTrack — funzioni comuni delle pagine (va con ft-ui.css).

   Formattazioni di date, ore ed euro uguali ovunque, toast e conferme.
   Prima ogni pagina aveva le sue: la stessa data usciva "2026-09-30",
   "30/09/2026" o "30/09" a seconda di dove la si guardava.

   DATE: il server salva in UTC. Una data/ora senza fuso ("2026-09-26T20:19:25")
   viene letta come UTC e mostrata nell'ora locale; una data pura
   ("2026-09-30") resta quel giorno, senza spostamenti di fuso.
*/
(function () {
  'use strict';

  const GIORNO_MS = 86400000;

  /** Converte in Date. Accetta Date, ISO con o senza fuso, data pura. */
  function data(v) {
    if (v == null || v === '') return null;
    if (v instanceof Date) return isNaN(v) ? null : v;
    const s = String(v).trim();
    // Data pura: mezzogiorno locale, cosi' nessun fuso la sposta di giorno.
    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) {
      const [a, m, g] = s.split('-').map(Number);
      return new Date(a, m - 1, g, 12, 0, 0);
    }
    // Data/ora senza fuso: il server la scrive in UTC.
    const conFuso = /[zZ]$|[+-]\d{2}:?\d{2}$/.test(s);
    const d = new Date(conFuso ? s : s.replace(' ', 'T') + 'Z');
    return isNaN(d) ? null : d;
  }

  const pad = n => String(n).padStart(2, '0');

  /** 30/09/2026 */
  function fmtData(v) {
    const d = data(v);
    return d ? `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()}` : '—';
  }
  /** 30/09 */
  function fmtDataBreve(v) {
    const d = data(v);
    return d ? `${pad(d.getDate())}/${pad(d.getMonth() + 1)}` : '—';
  }
  /** 22:19 */
  function fmtOra(v) {
    const d = data(v);
    return d ? `${pad(d.getHours())}:${pad(d.getMinutes())}` : '—';
  }
  /** 30/09/2026 22:19 */
  function fmtDataOra(v) {
    const d = data(v);
    return d ? `${fmtData(d)} ${fmtOra(d)}` : '—';
  }
  /** "oggi alle 22:19", "ieri alle 9:05", "3 giorni fa" */
  function fmtRelativo(v) {
    const d = data(v);
    if (!d) return '—';
    const g = giorniDa(d);
    if (g === 0) return `oggi alle ${fmtOra(d)}`;
    if (g === -1) return `ieri alle ${fmtOra(d)}`;
    if (g < 0 && g > -7) return `${-g} giorni fa`;
    return fmtData(d);
  }
  /** Giorni di calendario da oggi (positivo = futuro). */
  function giorniDa(v) {
    const d = data(v);
    if (!d) return null;
    const oggi = new Date(); oggi.setHours(12, 0, 0, 0);
    const x = new Date(d); x.setHours(12, 0, 0, 0);
    return Math.round((x - oggi) / GIORNO_MS);
  }
  /** Data di oggi locale, per i campi <input type=date>. */
  function oggiISO() {
    const d = new Date();
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }
  /** Scadenza leggibile per una consegna: {testo, classe} */
  function scadenza(v) {
    const g = giorniDa(v);
    if (g == null) return { testo: '', classe: '' };
    if (g < 0) return { testo: g === -1 ? 'scaduta ieri' : `scaduta da ${-g} gg`, classe: 'late' };
    if (g === 0) return { testo: 'oggi', classe: 'soon' };
    if (g === 1) return { testo: 'domani', classe: 'soon' };
    if (g <= 3) return { testo: `tra ${g} gg`, classe: 'soon' };
    return { testo: `tra ${g} gg`, classe: '' };
  }

  const _eur = new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR' });
  const _eur0 = new Intl.NumberFormat('it-IT', { style: 'currency', currency: 'EUR', maximumFractionDigits: 0 });
  function euro(v, intero) {
    if (v == null || v === '' || isNaN(Number(v))) return '—';
    return (intero ? _eur0 : _eur).format(Number(v));
  }
  function numero(v, dec) {
    if (v == null || v === '' || isNaN(Number(v))) return '—';
    return Number(v).toLocaleString('it-IT', { minimumFractionDigits: dec || 0, maximumFractionDigits: dec || 0 });
  }

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function iniziali(nome) {
    const p = String(nome || '').trim().split(/\s+/).filter(Boolean);
    return ((p[0] || '?')[0] + (p[1] ? p[1][0] : '')).toUpperCase();
  }

  /** Toast in basso a destra. tipo: '', 'ok', 'warn', 'err'.
      azione facoltativa: {testo, fn} (es. "Annulla"). */
  function toast(msg, tipo, azione, durata) {
    let box = document.querySelector('.ft-toasts');
    if (!box) { box = document.createElement('div'); box.className = 'ft-toasts'; document.body.appendChild(box); }
    const t = document.createElement('div');
    t.className = 'ft-toast ' + (tipo || '') + (tipo === 'ok' ? ' ft-spunta' : '');
    t.setAttribute('role', tipo === 'err' ? 'alert' : 'status');
    t.innerHTML = (tipo === 'ok'
      ? '<svg class="ft-check" viewBox="0 0 18 18" aria-hidden="true"><circle cx="9" cy="9" r="9"/><path d="M5.2 9.3l2.5 2.5 5-5.2"/></svg>'
      : '') + `<span>${esc(msg)}</span>`;
    if (azione && azione.testo) {
      const b = document.createElement('button');
      b.className = 'ft-toast-act'; b.textContent = azione.testo;
      b.onclick = () => { t.remove(); try { azione.fn(); } catch (_) {} };
      t.appendChild(b);
    }
    box.appendChild(t);
    setTimeout(() => t.remove(), durata || (azione ? 7000 : 3800));
    return t;
  }

  /** Conferma con la finestra della casa. Ritorna una Promise<boolean>.
      opz: {titolo, testo, ok, annulla, pericolo} */
  function conferma(opz) {
    opz = opz || {};
    return new Promise(res => {
      const bg = document.createElement('div');
      bg.className = 'ft-modal-bg open';
      bg.innerHTML = `
        <div class="ft-modal" role="dialog" aria-modal="true">
          <div class="ft-modal-head"><div>
            <h3 class="ft-modal-title">${esc(opz.titolo || 'Confermi?')}</h3>
            ${opz.testo ? `<p class="ft-modal-sub">${esc(opz.testo)}</p>` : ''}
          </div></div>
          <div class="ft-modal-foot">
            <button class="ft-btn" data-v="0">${esc(opz.annulla || 'Annulla')}</button>
            <button class="ft-btn ${opz.pericolo ? 'danger' : 'primary'}" data-v="1">${esc(opz.ok || 'Conferma')}</button>
          </div>
        </div>`;
      const chiudi = v => { document.removeEventListener('keydown', tasti); bg.remove(); res(v); };
      const tasti = e => { if (e.key === 'Escape') chiudi(false); if (e.key === 'Enter') chiudi(true); };
      bg.addEventListener('click', e => {
        if (e.target === bg) return chiudi(false);
        const b = e.target.closest('button[data-v]');
        if (b) chiudi(b.dataset.v === '1');
      });
      document.addEventListener('keydown', tasti);
      document.body.appendChild(bg);
      bg.querySelector('button[data-v="1"]').focus();
    });
  }

  /** Apre/chiude una finestra .ft-modal-bg gia' presente nella pagina. */
  function apri(id) {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.add('open');
    const f = el.querySelector('input:not([type=hidden]), select, textarea, button.primary');
    if (f) setTimeout(() => f.focus(), 30);
  }
  function chiudi(id) { const el = document.getElementById(id); if (el) el.classList.remove('open'); }
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    const aperte = document.querySelectorAll('.ft-modal-bg.open[id]');
    if (aperte.length) aperte[aperte.length - 1].classList.remove('open');
  });

  /** Utente salvato nel browser (stesso formato di login.html). */
  /** Chi sta usando la pagina, come lo sa il server (ft-accesso.js): la
      persona entrata col PIN, oppure la stazione. Solo per mostrarlo: i
      permessi li decide il server a ogni richiesta. */
  function utente() {
    const io = window.FTA && window.FTA.io;
    if (io && io.persona) return { id: io.persona.id, name: io.persona.nome, role: io.stazione_nome };
    if (io && io.registrato) return { id: null, name: io.stazione_nome, role: io.stazione_nome };
    // periodo di passaggio: la postazione scelta col vecchio ingresso
    try { return JSON.parse(localStorage.getItem('currentUser') || 'null'); } catch (_) { return null; }
  }
  /** Riempie la pillola utente della testata (.ft-user con #ft-user-*). */
  function mostraUtente(u, area) {
    u = u || utente() || {};
    const nome = u.name || u.nome || u.id || '—';
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    set('ft-user-avatar', iniziali(nome));
    set('ft-user-name', nome);
    set('ft-user-role', area || u.role || '');
  }

  function icone() { try { if (window.lucide) window.lucide.createIcons(); } catch (_) {} }

  // Segno sotto la scheda attiva che scorre alla nuova scheda (invece di
  // spegnersi su una e accendersi sull'altra). Segue la classe "active",
  // quindi funziona con ogni pagina senza toccarne il codice.
  function segnoSchede() {
    document.querySelectorAll('.ft-tabs').forEach(nav => {
      if (nav._ftSegno) return;
      const segno = document.createElement('span');
      segno.className = 'ft-tab-segno';
      nav.appendChild(segno);
      nav._ftSegno = segno;
      nav.classList.add('ft-segno');
      let primo = true;
      const sposta = () => {
        const a = nav.querySelector('.ft-tab.active');
        if (!a) { segno.style.width = '0'; return; }
        if (primo) segno.style.transition = 'none';
        segno.style.width = a.offsetWidth + 'px';
        segno.style.transform = `translateX(${a.offsetLeft}px)`;
        if (primo) { segno.getBoundingClientRect(); segno.style.transition = ''; primo = false; }
      };
      new MutationObserver(sposta).observe(nav, { subtree: true, attributes: true, attributeFilter: ['class'] });
      window.addEventListener('resize', sposta);
      if (document.fonts && document.fonts.ready) document.fonts.ready.then(sposta);
      sposta();
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', segnoSchede);
  else segnoSchede();

  // ── Effetti comuni (vedi "Effetti comuni" in ft-ui.css) ─────────────
  const MUOVI = !(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches);

  /** Il numero scorre dal valore mostrato a `valore`.
      opz: {dec, euro, intero, durata, suffisso}. Ritorna subito; il testo
      finale e' sempre quello giusto anche senza animazione. */
  function conta(el, valore, opz) {
    if (!el) return;
    opz = opz || {};
    const fmt = v => (opz.euro ? euro(v, opz.intero) : numero(v, opz.dec || 0)) + (opz.suffisso || '');
    const fine = Number(valore);
    if (isNaN(fine)) { el.textContent = fmt(valore); return; }
    // La prima volta parte da zero: all'apertura della pagina i numeri salgono.
    const inizio = el.dataset.ftVal === undefined ? 0 : Number(el.dataset.ftVal);
    el.dataset.ftVal = String(fine);
    if (!MUOVI || isNaN(inizio) || inizio === fine || document.hidden) { el.textContent = fmt(fine); return; }
    const t0 = performance.now(), d = opz.durata || 650;
    const passo = t => {
      const k = Math.min(1, (t - t0) / d), e = 1 - Math.pow(1 - k, 3);
      el.textContent = fmt(k < 1 ? inizio + (fine - inizio) * e : fine);
      if (k < 1) requestAnimationFrame(passo);
    };
    requestAnimationFrame(passo);
  }
  /** Evidenzia un elemento appena arrivato (riga, scheda). */
  function evidenzia(el, classe) {
    if (!el) return;
    classe = classe || 'ft-new';
    el.classList.remove(classe); void el.offsetWidth; el.classList.add(classe);
    setTimeout(() => el.classList.remove(classe), 1900);
  }
  /** Numera i figli per l'entrata in sequenza di .ft-stagger (max 14 in
      ritardo: oltre entrano tutti insieme, una lista lunga non deve attendere). */
  function sequenza(box) {
    if (!box) return;
    const figli = Array.from(box.children);
    figli.forEach((c, i) => c.style.setProperty('--i', Math.min(i, 14)));
    box.classList.remove('ft-stagger'); void box.offsetWidth; box.classList.add('ft-stagger');
    // Solo questa volta: i ridisegni successivi (aggiornamento, ricerca) non
    // devono rifare l'entrata.
    clearTimeout(box._ftSeq);
    box._ftSeq = setTimeout(() => box.classList.remove('ft-stagger'), Math.min(figli.length, 14) * 35 + 450);
  }
  /** Come conta(), ma ricorda il valore per chiave fra un ridisegno e
      l'altro: utile quando l'elemento viene ricreato da innerHTML. */
  const _ultimi = {};
  function contaDa(chiave, el, valore, opz) {
    if (!el) return;
    if (chiave in _ultimi) el.dataset.ftVal = String(_ultimi[chiave]);
    _ultimi[chiave] = Number(valore);
    conta(el, valore, opz);
  }
  // Uscita in dissolvenza sui link interni (stessa scheda, stessa origine).
  document.addEventListener('click', e => {
    if (!MUOVI || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest && e.target.closest('a[href]');
    if (!a || (a.target && a.target !== '_self') || a.hasAttribute('download')) return;
    const url = new URL(a.href, location.href);
    if (url.origin !== location.origin || url.pathname.startsWith('/api/') || /\.(pdf|dxf|step|stp|png|jpe?g)$/i.test(url.pathname)) return;
    if (url.pathname === location.pathname && url.search === location.search) return;   // solo #ancora
    if (!document.body.classList.contains('ft')) return;
    e.preventDefault();
    document.body.classList.add('ft-esce');
    setTimeout(() => { location.href = url.href; }, 130);
  });
  // Tornando indietro la pagina puo' arrivare dalla cache ancora "uscita".
  window.addEventListener('pageshow', () => { if (document.body) document.body.classList.remove('ft-esce'); });

  window.FT = {
    data, fmtData, fmtDataBreve, fmtOra, fmtDataOra, fmtRelativo, giorniDa, oggiISO, scadenza,
    euro, numero, esc, iniziali, toast, conferma, apri, chiudi, utente, mostraUtente, icone,
    conta, contaDa, evidenzia, sequenza,
  };
})();
