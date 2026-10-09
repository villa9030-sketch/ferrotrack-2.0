/* Pagina laser — base comune: lo STATO UNICO della pagina, le chiamate al
   server, i formati e i piccoli aiuti di disegno. Gli altri moduli
   (laser-*.js) leggono e scrivono solo LZ.s: nessuno stato doppio.

   Regola della pagina (Stefano, 09/10/2026): niente si apre o si sposta da
   solo. Gli aggiornamenti automatici ridisegnano un pezzo di pagina solo se
   quello che mostra e' cambiato davvero, e senza togliere il punto in cui
   si sta leggendo (scorrimento, campo attivo). */
(function () {
  'use strict';
  const LZ = window.LZ = window.LZ || {};

  // ── Stato ───────────────────────────────────────────────────────────
  LZ.s = {
    ordini: [],            // /api/orders?aperti=1, tutti quelli vivi (anche per la saturazione)
    riep: {},              // /api/laser/ordini: {id: riassunto} (stato, conteggi, blocchi)
    riepPronto: false,
    dett: {},              // /api/orders/<id>/laser: {id: {dati, t, carica, errore}}
    aperto: null,          // id dell'ordine aperto a destra
    scheda: 'pezzi',       // pezzi | disegni | pdf
    spuntati: new Set(),   // ordini spuntati nell'elenco
    inLinea: true,
    primoCarico: true,
    occupato: 0,           // azioni in corso: l'aggiornamento automatico aspetta
  };
  const S = LZ.s;

  LZ.esc = s => FT.esc(s);
  const esc = LZ.esc;

  // ── Server ──────────────────────────────────────────────────────────
  LZ.get = async function (url) {
    try {
      const r = await fetch(url, { cache: 'no-store' });
      let d = null;
      try { d = await r.json(); } catch (_) { /* risposta non JSON */ }
      return { ok: r.ok && !(d && d.success === false), status: r.status, d: d || {} };
    } catch (e) {
      return { ok: false, status: 0, d: { error: 'Server non raggiungibile' } };
    }
  };
  LZ.post = async function (url, corpo) {
    try {
      const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(corpo || {}) });
      let d = {};
      try { d = await r.json(); } catch (_) { /* vuota */ }
      if (d.success === undefined) d.success = r.ok;
      return { ok: r.ok && d.success !== false, status: r.status, d };
    } catch (e) {
      return { ok: false, status: 0, d: { success: false, error: 'Server non raggiungibile' } };
    }
  };
  /** Un'azione che cambia i dati: l'aggiornamento automatico intanto aspetta. */
  LZ.azione = async function (fn) {
    S.occupato++;
    try { return await fn(); } finally { S.occupato = Math.max(0, S.occupato - 1); }
  };

  // ── Ordini ──────────────────────────────────────────────────────────
  const CHIUSI = ['CHIUSO', 'SPEDITO', 'ARCHIVIATO'];
  /** smistare | coda | in_lantek | tagliato | escluso (come il server, api_laser.stato_laser) */
  LZ.statoLaser = function (o) {
    if (!o) return null;
    if (o.taglio_completato) return 'tagliato';
    if (o.taglio_richiesto == null) return 'smistare';
    if (o.taglio_richiesto === false) return 'escluso';
    return o.importato_lantek_il ? 'in_lantek' : 'coda';
  };
  LZ.ordine = LZ.ordine || {};
  LZ.trova = id => S.ordini.find(o => o.id === id) || null;
  LZ.giorno = o => (o && o.data_consegna ? String(o.data_consegna).slice(0, 10) : null);
  LZ.giorniA = o => { const g = LZ.giorno(o); return g ? FT.giorniDa(g) : null; };
  LZ.numero = o => o.numero_ordine_cliente || o.numero_ordine || String(o.id).slice(0, 8);
  /** Gli ordini dell'elenco: vivi, che passano (o forse) dal laser, e i
      tagliati nelle ultime 24 ore. Prima per consegna, i tagliati in fondo. */
  LZ.ordiniLaser = function () {
    const ora = Date.now();
    const l = S.ordini.filter(o => {
      if (o.is_deleted || CHIUSI.includes(o.status)) return false;
      const st = LZ.statoLaser(o);
      if (st === 'escluso') return false;
      if (st === 'tagliato') { const d = FT.data(o.data_taglio_completato); return d && ora - d.getTime() < 864e5; }
      return true;
    });
    const k = o => (LZ.statoLaser(o) === 'tagliato' ? '1' : '0') + (LZ.giorno(o) || '9999') + (o.cliente || '');
    return l.sort((a, b) => k(a).localeCompare(k(b)));
  };

  /** L'etichetta di stato di un ordine (UNA). tono: '' grigio | att ambra | err rosso | forte */
  LZ.etichetta = function (o) {
    const st = LZ.statoLaser(o);
    const r = S.riep[o.id];
    if (st === 'smistare') return { t: 'Da smistare', c: 'forte' };
    if (st === 'tagliato') return { t: 'Tagliato', c: '' };
    if (st === 'in_lantek') return { t: 'In Lantek', c: '' };
    if (!r) return { t: '', c: 'skel' };
    if (r.errore) return { t: 'Da ricontrollare', c: '' };
    if (r.n_abbinare) return { t: `Da abbinare (${r.n_abbinare})`, c: 'att' };
    if (r.n_controllare) return { t: `Da controllare (${r.n_controllare})`, c: 'att' };
    if ((r.bloccati || []).length) return { t: 'Bloccato', c: 'err' };
    if (r.lantek && r.lantek.letto && !r.lantek.disponibile) return { t: 'Lantek non risponde', c: '' };
    if (r.tutto_in_lantek) return { t: 'Già in Lantek', c: '' };
    return { t: 'Pronto per Lantek', c: 'forte' };
  };
  LZ.chip = function (o, extra) {
    const e = LZ.etichetta(o);
    if (e.c === 'skel') return '<span class="lz-chip skel lz-skel" aria-hidden="true">&nbsp;</span>';
    return `<span class="lz-chip ${e.c}${extra ? ' ' + extra : ''}">${e.c === 'att' || e.c === 'err' ? '<i class="pt"></i>' : ''}${esc(e.t)}</span>`;
  };

  // ── Formati ─────────────────────────────────────────────────────────
  LZ.quando = function (o) {
    const g = LZ.giorniA(o);
    if (g == null) return { t: 'senza data', rit: false };
    if (g < 0) return { t: `ritardo ${-g} gg`, rit: true };
    if (g === 0) return { t: 'oggi', rit: false };
    if (g === 1) return { t: 'domani', rit: false };
    return { t: `tra ${g} gg`, rit: false };
  };
  LZ.ore = function (min) {
    if (min == null) return '—';
    if (min < 60) return `${Math.max(1, Math.round(min))} min`;
    return `${(min / 60).toLocaleString('it-IT', { maximumFractionDigits: 1 })} h`;
  };
  LZ.mm = v => (v == null || v === '' || isNaN(Number(v)) ? '' : `${FT.numero(Number(v), Number(v) % 1 ? 1 : 0)} mm`);
  LZ.lamiera = function (mat, sp) {
    const m = String(mat || '').replace(/_/g, ' ').trim();
    const s = LZ.mm(sp);
    return (m + (m && s ? ' · ' : '') + s) || '—';
  };
  LZ.misure = function (m) {
    if (!m || !m[0] || !m[1]) return '';
    return `${FT.numero(Math.round(m[0]))} × ${FT.numero(Math.round(m[1]))} mm`;
  };
  LZ.ico = n => `<i data-lucide="${n}" aria-hidden="true"></i>`;
  LZ.tasto = k => `<span class="lz-tasto" aria-hidden="true">${k}</span>`;
  LZ.plur = (n, uno, tanti) => `${n} ${n === 1 ? uno : tanti}`;

  // ── Disegno ─────────────────────────────────────────────────────────
  LZ.icone = function (radice) {
    try { if (window.lucide) window.lucide.createIcons({ root: radice || document }); } catch (_) { /* icone facoltative */ }
  };
  /** Mette l'HTML solo se e' cambiato; tiene lo scorrimento. true se ha ridisegnato. */
  LZ.metti = function (el, html, scorre) {
    if (!el || el._lzHtml === html) return false;
    const y = scorre ? scorre.scrollTop : 0;
    el.innerHTML = html;
    el._lzHtml = html;
    if (scorre) scorre.scrollTop = y;
    LZ.icone(el);
    return true;
  };
  /** Un campo della pagina sta venendo scritto? Allora non si ridisegna. */
  LZ.staScrivendo = function (el) {
    const a = document.activeElement;
    return !!(a && el && el.contains(a) && /INPUT|TEXTAREA|SELECT/.test(a.tagName)
      && !/^(checkbox|radio|button)$/.test(a.type || ''));
  };

  // ── Menu piccolo (⋯) ────────────────────────────────────────────────
  let _menu = null;
  LZ.chiudiMenu = function () {
    if (_menu) { const t = _menu._torna; _menu.remove(); _menu = null; if (t && t.focus) t.focus(); return true; }
    return false;
  };
  /** voci: [{t, ico, fn, href, download, sep, tit}] */
  LZ.menu = function (ancora, voci) {
    LZ.chiudiMenu();
    const m = document.createElement('div');
    m.className = 'lz-menu';
    m.setAttribute('role', 'menu');
    m.innerHTML = voci.map((v, i) => v.sep ? '<div class="sep"></div>'
      : v.tit ? `<div class="tit">${esc(v.tit)}</div>`
        : v.href ? `<a role="menuitem" href="${esc(v.href)}" ${v.download ? 'download' : ''} data-i="${i}">${v.ico ? LZ.ico(v.ico) : ''}<span>${esc(v.t)}</span></a>`
          : `<button role="menuitem" type="button" data-i="${i}">${v.ico ? LZ.ico(v.ico) : ''}<span>${esc(v.t)}</span></button>`).join('');
    document.body.appendChild(m);
    LZ.icone(m);
    const r = ancora.getBoundingClientRect();
    const w = m.offsetWidth, h = m.offsetHeight;
    m.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, r.right - w)) + 'px';
    m.style.top = (r.bottom + 6 + h > window.innerHeight ? Math.max(8, r.top - h - 6) : r.bottom + 6) + 'px';
    m._torna = ancora;
    m.addEventListener('click', e => {
      const b = e.target.closest('[data-i]');
      if (!b) return;
      const v = voci[Number(b.dataset.i)];
      if (v.fn) { e.preventDefault(); LZ.chiudiMenu(); v.fn(); }
      else setTimeout(LZ.chiudiMenu, 0);
    });
    m.addEventListener('keydown', e => {
      const el = [...m.querySelectorAll('[data-i]')];
      const i = el.indexOf(document.activeElement);
      if (e.key === 'ArrowDown') { e.preventDefault(); (el[i + 1] || el[0]).focus(); }
      if (e.key === 'ArrowUp') { e.preventDefault(); (el[i - 1] || el[el.length - 1]).focus(); }
    });
    _menu = m;
    const primo = m.querySelector('[data-i]');
    if (primo) primo.focus();
  };
  document.addEventListener('mousedown', e => { if (_menu && !_menu.contains(e.target)) LZ.chiudiMenu(); });
  window.addEventListener('resize', () => LZ.chiudiMenu());

  // ── Dove si era (per tornarci con Esc) ──────────────────────────────
  LZ.fotografa = function () {
    const lista = document.querySelector('.lz-righe');
    const dett = document.getElementById('lz-dett');
    const a = document.activeElement;
    return { aperto: S.aperto, scheda: S.scheda, yLista: lista ? lista.scrollTop : 0, yDett: dett ? dett.scrollTop : 0,
             fuoco: a && a.dataset && a.dataset.fuoco ? a.dataset.fuoco : (a && a.id ? '#' + a.id : null) };
  };
  LZ.ripristina = function (f) {
    if (!f) return;
    S.aperto = f.aperto; S.scheda = f.scheda;
    LZ.ridisegna();
    const lista = document.querySelector('.lz-righe');
    const dett = document.getElementById('lz-dett');
    if (lista) lista.scrollTop = f.yLista;
    if (dett) dett.scrollTop = f.yDett;
    let el = null;
    if (f.fuoco) el = f.fuoco[0] === '#' ? document.getElementById(f.fuoco.slice(1)) : document.querySelector(`[data-fuoco="${CSS.escape(f.fuoco)}"]`);
    if (!el && f.aperto) el = document.querySelector(`.lz-r[data-id="${CSS.escape(f.aperto)}"]`);
    if (el && el.focus) el.focus({ preventScroll: true });
  };

  // ── Ridisegno ───────────────────────────────────────────────────────
  LZ.ridisegna = function () {
    if (LZ.lista && LZ.lista.disegna) LZ.lista.disegna();
    if (LZ.ordine && LZ.ordine.disegna) LZ.ordine.disegna();
  };

  LZ.schermoIntero = function () {
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen().catch(() => FT.toast('Il browser non permette lo schermo intero: premi F11', 'warn'));
  };
})();
