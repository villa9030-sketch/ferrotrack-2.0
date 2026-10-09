/* Pagina laser — avvio, aggiornamento automatico, notifiche, tasti.

   Aggiornamento: ogni 5 s la lista degli ordini (leggera); il riassunto per
   l'elenco (stato, conteggi, Lantek) ogni minuto o quando un ordine cambia;
   l'ordine aperto si rilegge solo se il suo riassunto e' cambiato. Niente
   si apre o si sposta da solo; un ridisegno non tocca cio' che non e'
   cambiato e non toglie il campo su cui si sta scrivendo. */
(function () {
  'use strict';
  const LZ = window.LZ;
  const S = LZ.s;
  const esc = LZ.esc;
  const A = LZ.avvio = {};

  let impronta = '';
  const impr = l => l.map(o => [o.id, o.status, o.cliente, o.numero_ordine, o.numero_ordine_cliente, o.data_consegna,
    o.taglio_richiesto == null ? '-' : o.taglio_richiesto ? 1 : 0, o.taglio_completato ? 1 : 0, o.data_taglio_completato || '',
    o.importato_lantek_il || '', o.pdf_file ? 1 : 0, o.durata_laser_manuale_min || '', o.data_taglio_pianificata || '',
    o.is_deleted ? 1 : 0].join('|')).sort().join(';');

  function inLinea(ok) {
    S.inLinea = ok;
    const b = document.getElementById('conn-badge');
    if (b) { b.classList.toggle('off', !ok); b.textContent = ok ? 'In linea' : 'Non in linea'; }
  }

  /** Gli ordini (5 s). forza: dopo un'azione, anche se c'e' un'azione in corso. */
  A.caricaOrdini = async function (forza) {
    if (S.occupato && !forza) return false;
    const r = await LZ.get('/api/orders?aperti=1');
    if (!r.ok) { inLinea(false); return false; }
    inLinea(true);
    const l = (Array.isArray(r.d) ? r.d : (r.d.orders || [])).filter(o => !o.is_deleted);
    const nuova = impr(l);
    const cambiato = nuova !== impronta;
    impronta = nuova;
    S.ordini = l;
    return cambiato;
  };

  /** Riassunto per l'elenco. id: solo quell'ordine (sempre fresco). */
  A.caricaRiep = async function (id, fresco) {
    const q = id ? `?ordine=${encodeURIComponent(id)}${fresco ? '&fresco=1' : ''}` : (fresco ? '?fresco=1' : '');
    const r = await LZ.get('/api/laser/ordini' + q);
    if (!r.ok) return false;
    const nuovi = r.d.ordini || {};
    let cambiatoAperto = false;
    for (const [k, v] of Object.entries(nuovi)) {
      const prima = S.riep[k];
      if (k === S.aperto && prima && JSON.stringify(prima) !== JSON.stringify(v)) cambiatoAperto = true;
      S.riep[k] = v;
    }
    if (!id) for (const k of Object.keys(S.riep)) if (!(k in nuovi)) delete S.riep[k];
    S.riepPronto = true;
    LZ.ridisegna();
    // l'ordine aperto e' cambiato (da un'altra postazione, da Lantek): si rilegge
    if (cambiatoAperto && !id && !LZ.controllo.aperto() && !LZ.lantek.aperto()) LZ.ordine.carica(S.aperto);
    return true;
  };

  async function giro() {
    if (document.hidden) return;
    const cambiato = await A.caricaOrdini(false);
    if (cambiato) {
      await LZ.sat.carica(true);
      LZ.ridisegna();
      A.caricaRiep();
    }
  }

  // ── Notifiche ───────────────────────────────────────────────────────
  const N = LZ.notifiche = { aperto: false };
  N.apriChiudi = function (e) {
    if (e) e.stopPropagation();
    N.aperto = !N.aperto;
    document.getElementById('notif-panel').classList.toggle('open', N.aperto);
    if (N.aperto) N.carica();
  };
  document.addEventListener('click', e => {
    if (N.aperto && !e.target.closest('.lz-bell')) { N.aperto = false; document.getElementById('notif-panel').classList.remove('open'); }
  });
  N.carica = async function () {
    const r = await LZ.get('/api/notifications?limit=50');
    if (!r.ok || !r.d.data) return;
    N.badge(r.d.data.unread_count || 0);
    const l = r.d.data.notifications || [];
    document.getElementById('notif-clear').style.display = l.length ? '' : 'none';
    const body = document.getElementById('notif-panel-body');
    body.innerHTML = l.length ? l.map(n => `<div class="ft-pop-item lz-notif${n.is_read ? '' : ' unread'}" data-n="${esc(n.id)}" data-ordine="${esc(n.order_id || '')}">
        <div><b>${esc(n.title)}</b><span>${esc(n.message)}</span><small>${esc(FT.fmtRelativo(n.timestamp))}</small></div>
        <button class="ft-icon-btn" data-via="${esc(n.id)}" title="Elimina" aria-label="Elimina">${LZ.ico('x')}</button></div>`).join('')
      : '<div class="ft-empty" style="padding:28px 16px;"><p>Nessuna notifica</p></div>';
    LZ.icone(body);
  };
  N.badge = n => { const b = document.getElementById('notif-badge'); if (b) b.textContent = n > 0 ? (n > 99 ? '99+' : n) : ''; };
  N.conta = async function () {
    const r = await LZ.get('/api/notifications?limit=1');
    if (r.ok && r.d.data) N.badge(r.d.data.unread_count || 0);
  };
  N.svuota = async function () {
    try { await fetch('/api/notifications/clear-all', { method: 'DELETE' }); } catch (_) { /* rete */ }
    N.carica();
  };
  document.addEventListener('click', async e => {
    const via = e.target.closest('#notif-panel-body [data-via]');
    if (via) {
      e.stopPropagation();
      try { await fetch(`/api/notifications/${encodeURIComponent(via.dataset.via)}`, { method: 'DELETE' }); } catch (_) { /* rete */ }
      N.carica();
      return;
    }
    const it = e.target.closest('#notif-panel-body [data-n]');
    if (!it) return;
    try { await fetch(`/api/notifications/${encodeURIComponent(it.dataset.n)}/read`, { method: 'PUT' }); } catch (_) { /* rete */ }
    N.conta();
    if (it.dataset.ordine && LZ.trova(it.dataset.ordine)) { N.apriChiudi(); LZ.lista.apri(it.dataset.ordine); }
  });

  // ── Tasti ───────────────────────────────────────────────────────────
  document.addEventListener('keydown', e => {
    if (document.querySelector('.ft-modal-bg.open, .fts-velo')) return;      // conferme e finestre comuni
    if (LZ.controllo.aperto()) { if (LZ.controllo.tasto(e)) e.preventDefault(); return; }
    if (LZ.lantek.aperto()) { if (LZ.lantek.tasto(e)) e.preventDefault(); return; }
    if (e.key === 'Escape' && LZ.chiudiMenu()) { e.preventDefault(); return; }
    if (document.querySelector('.lz-menu')) return;
    const t = e.target;
    if (t && (/INPUT|TEXTAREA|SELECT/.test(t.tagName) && !/^(checkbox|radio)$/.test(t.type || '')) || (t && t.isContentEditable)) return;
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    if (k === 'ArrowDown' || k === 'ArrowUp') { e.preventDefault(); LZ.lista.sposta(k === 'ArrowDown' ? 1 : -1); return; }
    if (k === ' ' && S.aperto && !(t && t.tagName === 'BUTTON')) {
      const o = LZ.trova(S.aperto);
      if (o && LZ.lista.spuntabile(o)) { e.preventDefault(); LZ.lista.spunta(o.id, !S.spuntati.has(o.id)); }
      return;
    }
    if (k === 'c') {
      const ids = LZ.lista.bersaglio().filter(id => (S.riep[id] || {}).n_controllare);
      if (ids.length) { e.preventDefault(); LZ.controllo.inizia(ids); }
      return;
    }
    if (k === 'l' && S.spuntati.size) { e.preventDefault(); LZ.lista.mandaSpuntati(); return; }
    if (k === 'Enter') {
      // Invio sul pulsante col fuoco lo preme; altrimenti e' il passo principale
      if (t && t.tagName === 'BUTTON' && !t.closest('.lz-r')) return;
      if (t && t.closest && t.closest('.lz-r[data-id]') && t.closest('.lz-r').dataset.id !== S.aperto) {
        e.preventDefault(); LZ.lista.apri(t.closest('.lz-r').dataset.id); return;
      }
      if (S.aperto && LZ.ordine.invio()) e.preventDefault();
      return;
    }
  });

  document.addEventListener('fullscreenchange', () => {
    const b = document.getElementById('btn-fullscreen');
    if (b) b.classList.toggle('on', !!document.fullscreenElement);
  });

  // ── Avvio ───────────────────────────────────────────────────────────
  (async function avvio() {
    LZ.ridisegna();
    await FTA.pronto;
    const u = FT.utente();
    if (!u) return;
    FT.mostraUtente(u, 'Postazione laser');
    LZ.icone();
    await A.caricaOrdini(true);
    S.primoCarico = false;
    LZ.ridisegna();
    LZ.sat.carica(true).then(() => LZ.ridisegna());
    await A.caricaRiep();
    let t5 = setInterval(giro, 5000);
    let t60 = setInterval(() => { if (!document.hidden && !S.occupato) { A.caricaRiep(); LZ.sat.carica(true).then(() => LZ.lista.disegna()); } }, 60000);
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) return;
      giro();
      A.caricaRiep();
    });
    setInterval(N.conta, 30000);
    setTimeout(N.conta, 1500);
    window.addEventListener('beforeunload', () => { clearInterval(t5); clearInterval(t60); });
  })();

  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js');
})();
