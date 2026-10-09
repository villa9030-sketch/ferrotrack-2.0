/* Pagina laser — l'elenco degli ordini, sempre a sinistra.
   Una riga per ordine: consegna (e quanto manca), cliente, numero, UNA
   etichetta di stato. Le caselle servono a lavorare piu' ordini insieme:
   "Controlla N disegni" (una coda sola) e "Manda a Lantek (K ordini)". */
(function () {
  'use strict';
  const LZ = window.LZ;
  const S = LZ.s;
  const esc = LZ.esc;
  const L = LZ.lista = {};

  let costruita = false;
  function costruisci() {
    const el = document.getElementById('lz-lista');
    el.innerHTML = `<div class="lz-lista-testa" id="lz-l-testa"></div>
      <div class="lz-strumenti" id="lz-l-str"></div>
      <div class="lz-righe" id="lz-l-righe" role="list" aria-label="Ordini"></div>
      <div id="lz-l-az"></div>`;
    el.addEventListener('click', clic);
    el.addEventListener('change', e => {
      const c = e.target.closest('input[data-sp]');
      if (c) L.spunta(c.dataset.sp, c.checked);
    });
    costruita = true;
  }

  /** Si possono spuntare gli ordini da mettere in Lantek. */
  L.spuntabile = o => LZ.statoLaser(o) === 'coda';

  function riga(o) {
    const st = LZ.statoLaser(o);
    const q = LZ.quando(o);
    const g = LZ.giorno(o);
    const on = S.aperto === o.id;
    const tagliato = st === 'tagliato';
    const sp = L.spuntabile(o);
    return `<div class="lz-r${on ? ' on' : ''}${tagliato ? ' spento' : ''}" role="listitem" tabindex="0" data-id="${esc(o.id)}"
        data-fuoco="r-${esc(o.id)}" ${on ? 'aria-current="true"' : ''}>
      <input type="checkbox" data-sp="${esc(o.id)}" ${S.spuntati.has(o.id) ? 'checked' : ''} ${sp ? '' : 'disabled'}
        aria-label="Seleziona ${esc(o.cliente || '')} ${esc(LZ.numero(o))}" tabindex="-1">
      <div class="lz-data">${g ? FT.fmtDataBreve(g) : '—'}<small class="${q.rit && !tagliato ? 'rit' : ''}">${tagliato ? 'tagliato ' + esc(FT.fmtOra(o.data_taglio_completato)) : esc(q.t)}</small></div>
      <div class="lz-cli"><b title="${esc(o.cliente || '')}">${esc(o.cliente || 'Cliente non indicato')}</b><span title="${esc(LZ.numero(o))}">${esc(LZ.numero(o))}</span></div>
      ${LZ.chip(o)}
    </div>`;
  }

  L.disegna = function () {
    if (!costruita) costruisci();
    const ordini = LZ.ordiniLaser();
    // le caselle restano solo sugli ordini ancora spuntabili
    for (const id of [...S.spuntati]) { const o = LZ.trova(id); if (!o || !L.spuntabile(o)) S.spuntati.delete(id); }

    const attivi = ordini.filter(o => LZ.statoLaser(o) !== 'tagliato');
    const rit = attivi.filter(o => (LZ.giorniA(o) ?? 0) < 0).length;
    LZ.metti(document.getElementById('lz-l-testa'), `<h1>Ordini al laser</h1>
      <div class="lz-riass"><span>${LZ.plur(attivi.length, 'ordine', 'ordini')}</span>${rit ? `<span>·</span><span class="rit">${rit} in ritardo</span>` : ''}</div>
      ${LZ.sat.html()}`);

    const daCtl = attivi.filter(o => L.spuntabile(o) && (S.riep[o.id] || {}).n_controllare);
    LZ.metti(document.getElementById('lz-l-str'),
      `<button class="lz-link" type="button" data-az="sel-ctl" ${daCtl.length ? '' : 'disabled'}>Seleziona quelli da controllare</button>`
      + (S.spuntati.size ? `<span>·</span><button class="lz-link" type="button" data-az="sel-via">Togli selezione</button>` : ''));

    const righe = document.getElementById('lz-l-righe');
    let html = '';
    if (S.primoCarico) {
      html = Array.from({ length: 7 }, () => `<div class="lz-r" aria-hidden="true"><span></span>
        <div class="lz-skel" style="height:30px;width:48px"></div><div><div class="lz-skel" style="height:12px;width:70%;margin-bottom:6px"></div>
        <div class="lz-skel" style="height:10px;width:40%"></div></div><span class="lz-chip skel lz-skel"></span></div>`).join('');
    } else if (!ordini.length) {
      html = `<div class="lz-vuoto-lista"><b>Nessun ordine al laser</b>Quando l'ufficio carica un ordine, compare qui.</div>`;
    } else {
      let tagliati = false;
      for (const o of ordini) {
        if (LZ.statoLaser(o) === 'tagliato' && !tagliati) { tagliati = true; html += '<div class="lz-gruppo">Tagliati nelle ultime 24 ore</div>'; }
        html += riga(o);
      }
    }
    LZ.metti(righe, html, righe);
    disegnaAzione();
  };

  function disegnaAzione() {
    const box = document.getElementById('lz-l-az');
    const sel = [...S.spuntati].map(LZ.trova).filter(Boolean);
    if (!sel.length) { LZ.metti(box, ''); return; }
    const nCtl = sel.reduce((s, o) => s + ((S.riep[o.id] || {}).n_controllare || 0), 0);
    const pronti = sel.filter(o => (S.riep[o.id] || {}).pronto);
    const altri = sel.length - pronti.length;
    LZ.metti(box, `<div class="lz-azione">
      <div class="q"><span>${LZ.plur(sel.length, 'ordine selezionato', 'ordini selezionati')}</span></div>
      <button class="lz-btn pri grande lungo" type="button" data-az="ctl-multi" ${nCtl ? '' : 'disabled'}>
        <span>Controlla ${LZ.plur(nCtl, 'disegno', 'disegni')}</span>${nCtl ? LZ.tasto('C') : ''}</button>
      <button class="lz-btn grande lungo" type="button" data-az="lantek-multi" ${pronti.length ? '' : 'disabled'}>
        <span>Manda a Lantek${pronti.length ? ` (${LZ.plur(pronti.length, 'ordine', 'ordini')})` : ''}</span>${pronti.length ? LZ.tasto('L') : ''}</button>
      ${altri && pronti.length ? `<div class="nota">${altri === 1 ? 'Un altro ha' : `Gli altri ${altri} hanno`} ancora qualcosa da fare prima di Lantek.</div>`
        : !pronti.length ? '<div class="nota">Per mandarli a Lantek, prima controlla i disegni e sistema i blocchi.</div>' : ''}
    </div>`);
  }

  function clic(e) {
    const az = e.target.closest('[data-az]');
    if (az) {
      const a = az.dataset.az;
      if (a === 'sel-ctl') L.selezionaDaControllare();
      else if (a === 'sel-via') { S.spuntati.clear(); LZ.ridisegna(); }
      else if (a === 'ctl-multi') L.controllaSpuntati();
      else if (a === 'lantek-multi') L.mandaSpuntati();
      return;
    }
    if (e.target.closest('input[data-sp]')) return;
    const r = e.target.closest('.lz-r[data-id]');
    if (r) L.apri(r.dataset.id);
  }

  L.spunta = function (id, si) {
    const o = LZ.trova(id);
    if (!o || !L.spuntabile(o)) return;
    if (si) S.spuntati.add(id); else S.spuntati.delete(id);
    LZ.ridisegna();
  };
  L.selezionaDaControllare = function () {
    for (const o of LZ.ordiniLaser()) if (L.spuntabile(o) && (S.riep[o.id] || {}).n_controllare) S.spuntati.add(o.id);
    LZ.ridisegna();
  };
  /** Gli ordini su cui lavorare: gli spuntati, o l'ordine aperto. */
  L.bersaglio = function () {
    const sel = LZ.ordiniLaser().filter(o => S.spuntati.has(o.id)).map(o => o.id);
    return sel.length ? sel : (S.aperto ? [S.aperto] : []);
  };
  L.controllaSpuntati = function () { LZ.controllo.inizia(L.bersaglio()); };
  /** Tutti i selezionati nella conferma: chi non parte dice perche'. */
  L.mandaSpuntati = function () {
    const ids = L.bersaglio();
    if (ids.some(id => (S.riep[id] || {}).pronto)) LZ.lantek.apri(ids);
  };

  /** Apre un ordine a destra (solo su richiesta: clic o frecce). */
  L.apri = function (id) {
    if (S.aperto !== id) {
      S.aperto = id;
      S.scheda = 'pezzi';
      const d = document.getElementById('lz-dett');
      if (d) d.scrollTop = 0;
    }
    LZ.ridisegna();
    LZ.ordine.carica(id);
    const r = document.querySelector(`.lz-r[data-id="${CSS.escape(id)}"]`);
    if (r) r.scrollIntoView({ block: 'nearest' });
  };
  L.sposta = function (passo) {
    const l = LZ.ordiniLaser();
    if (!l.length) return;
    const i = l.findIndex(o => o.id === S.aperto);
    const j = i < 0 ? (passo > 0 ? 0 : l.length - 1) : Math.max(0, Math.min(l.length - 1, i + passo));
    L.apri(l[j].id);
    const r = document.querySelector(`.lz-r[data-id="${CSS.escape(l[j].id)}"]`);
    if (r) r.focus({ preventScroll: true });
  };
})();
