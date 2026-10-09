/* Pagina laser — ordini arrivati solo in PDF: le righe che in Lantek non ci
   sono col loro codice (di solito assiemi) si abbinano UNA volta ai pezzi di
   Lantek, o si segnano "non va al laser". FerroTrack se lo ricorda per le
   forniture dopo (Stefano, 07/10/2026). Stessa logica di prima
   (laser-guida.js), dentro la scheda Pezzi. Server: /lantek-abbina,
   /api/lantek/pezzi. */
(function () {
  'use strict';
  const LZ = window.LZ;
  const S = LZ.s;
  const esc = LZ.esc;
  const A = LZ.abbina = {};
  S.abbina = S.abbina || {};          // editor aperti: {ordine: {codice: {pezzi, q, trovati, cercaErr}}}

  const ed = (id, cod) => ((S.abbina[id] || {})[cod]);
  const pdfDi = id => (((S.dett[id] || {}).dati || {}).pdf || {});

  A.html = function (o, d) {
    const pdf = d.pdf || {};
    const righe = pdf.da_abbinare || [];
    const liberi = pdf.liberi || [];
    return `<section class="lz-abb" id="lz-abb" aria-label="Righe da abbinare">
      <div class="lz-abb-testa"><b>${LZ.plur(righe.length, 'riga da abbinare', 'righe da abbinare')}</b>
        <p>Queste righe del PDF non sono in Lantek col loro codice. Dimmi cosa sono: me lo ricordo per le prossime forniture.</p></div>
      <ul>${righe.map(r => {
        const e = ed(o.id, r.codice);
        const c = esc(r.codice);
        return `<li><div class="lz-abb-riga"><div><span class="mono">${c}</span> <span class="num" style="color:var(--lz-ink-3)">· ${esc(String(r.quantita))} pz</span>
            <p>${esc(r.descrizione || '')}</p></div>
          <div style="display:flex;gap:8px">
            <button class="lz-btn piccolo${e ? ' pri' : ''}" type="button" data-abb="apri" data-cod="${c}">${LZ.ico('layers')}È un assieme</button>
            <button class="lz-btn piccolo" type="button" data-abb="nonlaser" data-cod="${c}">Non va al laser</button></div></div>
          ${e ? editor(o, r, e, liberi) : '<p style="margin:6px 0 0;font-size:12.5px;color:var(--lz-ink-3)">Se è un pezzo singolo: disegnalo in Lantek con lo stesso codice, poi premi Ricontrolla.</p>'}</li>`;
      }).join('')}</ul></section>`;
  };

  function editor(o, r, e, liberi) {
    const c = esc(r.codice);
    const riga = (cod, qLantek) => {
      const q = e.pezzi[cod];
      return `<label class="lz-abb-p"><input type="checkbox" data-abb="spunta" data-cod="${c}" data-pz="${esc(cod)}" ${q != null ? 'checked' : ''}>
        <span class="mono">${esc(cod)}</span><span style="color:var(--lz-ink-3);font-size:12.5px">${qLantek != null ? `in Lantek ${esc(String(qLantek))} pz` : ''}</span>
        <span><input type="number" min="1" max="9999" value="${esc(String(q != null ? q : 1))}" ${q != null ? '' : 'disabled'}
          data-abb="qta" data-cod="${c}" data-pz="${esc(cod)}" aria-label="Pezzi per un assieme"> per assieme</span></label>`;
    };
    const liberiCod = new Set(liberi.map(x => x.codice));
    const extra = Object.keys(e.pezzi).filter(k => !liberiCod.has(k));
    const trovati = (e.trovati || []).filter(x => !liberiCod.has(x.codice) && e.pezzi[x.codice] == null);
    const n = Object.keys(e.pezzi).length;
    return `<div class="lz-abb-ed">
      <h4>Di quali pezzi di Lantek è fatto ${c}? (quanti per UN assieme)</h4>
      ${liberi.length ? `<p>Pezzi che hai messo in Lantek con quest'ordine e che nessuna riga spiega:</p>${liberi.map(x => riga(x.codice, x.quantita)).join('')}`
        : '<p>In Lantek non ci sono pezzi di quest\'ordine senza riga: cercali per codice.</p>'}
      ${extra.map(k => riga(k, null)).join('')}
      <div class="lz-abb-cerca"><input type="search" placeholder="Cerca un pezzo in Lantek (almeno 3 caratteri del codice)" value="${esc(e.q || '')}"
          data-abb="testo" data-cod="${c}" aria-label="Cerca un pezzo in Lantek">
        <button class="lz-btn piccolo" type="button" data-abb="cerca" data-cod="${c}">${LZ.ico('search')}Cerca</button></div>
      ${e.cercaErr ? `<p style="margin:4px 0;color:var(--lz-ink-3);font-size:12.5px">${esc(e.cercaErr)}</p>` : ''}
      ${trovati.map(x => `<div class="lz-abb-p"><span></span><span class="mono">${esc(x.codice)}</span>
          <span style="color:var(--lz-ink-3);font-size:12.5px">${esc(LZ.lamiera(x.materiale, x.spessore))}</span>
          <button class="lz-btn piccolo" type="button" data-abb="aggiungi" data-cod="${c}" data-pz="${esc(x.codice)}">Aggiungi</button></div>`).join('')}
      <div class="lz-abb-az">
        <button class="lz-btn piccolo" type="button" data-abb="chiudi" data-cod="${c}">Annulla</button>
        <button class="lz-btn piccolo pri" type="button" data-abb="salva" data-cod="${c}" ${n ? '' : 'disabled'}>${LZ.ico('check')}Salva: ${LZ.plur(n, 'pezzo', 'pezzi')} per assieme</button>
      </div></div>`;
  }

  function ridisegna() {
    const a = document.activeElement;
    if (a && a.closest && a.closest('.lz-abb')) a.blur();     // l'editor si ridisegna: il campo non e' piu' lui
    LZ.ordine.disegna();
  }

  A.vai = function (id) {
    setTimeout(() => {
      const s = document.getElementById('lz-abb');
      if (!s) return;
      s.scrollIntoView({ block: 'start', behavior: 'smooth' });
      const b = s.querySelector('button');
      if (b) b.focus({ preventScroll: true });
    }, 30);
  };

  function apri(id, cod) {
    S.abbina[id] = S.abbina[id] || {};
    if (S.abbina[id][cod]) { delete S.abbina[id][cod]; ridisegna(); return; }
    const r = (pdfDi(id).da_abbinare || []).find(x => x.codice === cod) || {};
    const pezzi = {};
    for (const p of r.proposta || []) pezzi[p.codice] = p.quantita;
    S.abbina[id][cod] = { pezzi };
    ridisegna();
  }
  function spunta(id, cod, pz, si) {
    const e = ed(id, cod);
    if (!e) return;
    if (si) {
      const r = (pdfDi(id).da_abbinare || []).find(x => x.codice === cod) || {};
      const lib = (pdfDi(id).liberi || []).find(x => x.codice === pz);
      e.pezzi[pz] = lib && r.quantita ? Math.max(1, Math.round(lib.quantita / r.quantita)) : 1;
    } else delete e.pezzi[pz];
    ridisegna();
  }
  async function cerca(id, cod, testo) {
    const e = ed(id, cod);
    if (!e) return;
    e.q = String(testo || '').trim();
    if (e.q.length < 3) { e.cercaErr = 'Scrivi almeno 3 caratteri del codice'; e.trovati = []; ridisegna(); return; }
    const r = await LZ.get(`/api/lantek/pezzi?q=${encodeURIComponent(e.q)}`);
    e.trovati = r.ok ? (r.d.pezzi || []) : [];
    e.cercaErr = !r.ok ? (r.d.error || 'Lantek non risponde') : (!e.trovati.length ? 'Nessun pezzo in Lantek con questo codice' : null);
    ridisegna();
  }
  async function manda(id, corpo, messaggio) {
    await LZ.azione(async () => {
      const r = await LZ.post(`/api/orders/${id}/lantek-abbina`, corpo);
      if (!r.ok) { FT.toast('Non salvato: ' + (r.d.error || 'errore'), 'err'); return; }
      if (S.abbina[id]) delete S.abbina[id][corpo.codice];
      FT.toast(messaggio, 'ok');
      await LZ.ordine.dopo(id);
    });
  }

  document.addEventListener('click', e => {
    const b = e.target.closest('.lz-abb [data-abb]');
    if (!b || b.tagName === 'INPUT') return;
    const id = S.aperto, cod = b.dataset.cod;
    const a = b.dataset.abb;
    if (a === 'apri') apri(id, cod);
    else if (a === 'nonlaser') manda(id, { codice: cod, non_laser: true }, `${cod}: non va al laser, la prossima volta la salto`);
    else if (a === 'chiudi') { if (S.abbina[id]) delete S.abbina[id][cod]; ridisegna(); }
    else if (a === 'cerca') cerca(id, cod, b.parentElement.querySelector('input').value);
    else if (a === 'aggiungi') spunta(id, cod, b.dataset.pz, true);
    else if (a === 'salva') {
      const x = ed(id, cod);
      if (x) manda(id, { codice: cod, pezzi: Object.entries(x.pezzi).map(([c, q]) => ({ codice: c, quantita: q })) },
        `${cod}: me lo ricordo per le prossime forniture`);
    }
  });
  document.addEventListener('change', e => {
    const i = e.target.closest('.lz-abb input[data-abb]');
    if (!i) return;
    const id = S.aperto, cod = i.dataset.cod;
    if (i.dataset.abb === 'spunta') spunta(id, cod, i.dataset.pz, i.checked);
    else if (i.dataset.abb === 'qta') {
      const x = ed(id, cod), n = parseInt(i.value, 10);
      if (x && x.pezzi[i.dataset.pz] != null && n >= 1) x.pezzi[i.dataset.pz] = n;
    }
  });
  document.addEventListener('keydown', e => {
    const i = e.target.closest && e.target.closest('.lz-abb input[data-abb="testo"]');
    if (i && e.key === 'Enter') { e.preventDefault(); cerca(S.aperto, i.dataset.cod, i.value); }
  });
})();
