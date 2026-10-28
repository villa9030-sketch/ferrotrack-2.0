/* Pagina laser — la QUADRATURA (ft-quadratura.js, backend/quadratura.py).

   - in cima all'ordine aperto la riga "Quadratura: tutto torna" o, rossa,
     "Non quadra: 3 problemi" che si apre sulla tabella dei codici;
   - nell'elenco, la parola rossa "non quadra" sugli ordini che non tornano;
   - prima di "Manda a Lantek", "Taglio fatto" e del pacco (manda / segna
     tagliato): se un ordine non quadra, l'elenco di cosa non arriverebbe al
     laser e la spunta "Ho controllato" (nello storico). Decide chi lavora. */
(function () {
  'use strict';
  const LZ = window.LZ;
  const esc = LZ.esc;
  const QQ = LZ.quad = { tutti: {}, det: {} };
  const FQ = window.FTQuad;
  FQ.ridisegna = () => LZ.ridisegna();

  // ── elenco: tutti gli ordini aperti, da quello che il server ha gia' ──
  let prossimo = null;
  async function caricaTutti() {
    clearTimeout(prossimo);
    const r = await LZ.get('/api/quadratura');
    if (r.ok) {
      const prima = JSON.stringify(QQ.tutti);
      QQ.tutti = r.d.ordini || {};
      if (JSON.stringify(QQ.tutti) !== prima) LZ.ridisegna();
    }
    // qualcuno si sta ancora calcolando: si richiede presto, se no ogni minuto
    prossimo = setTimeout(caricaTutti, r.ok && (r.d.in_corso || []).length ? 8000 : 60000);
  }
  setTimeout(caricaTutti, 1500);
  QQ.aggiorna = caricaTutti;

  QQ.parolaLista = function (o) {
    const x = QQ.tutti[o.id];
    const q = (QQ.det[o.id] && QQ.det[o.id].q) || (x && x.quadratura);
    if (!q || q.esito !== 'NON_QUADRA') return '';
    return `<small class="lz-quad-mini" title="${esc(q.testo)}">non quadra</small>`;
  };

  // ── ordine aperto ─────────────────────────────────────────────────────
  async function carica(id) {
    const v = QQ.det[id] || {};
    if (v.in) return;
    QQ.det[id] = Object.assign({}, v, { in: true });
    const d = await FQ.get(id, false);
    QQ.det[id] = { q: d.quadratura || v.q || null, pronto: !!d.pronto, errore: d.errore || null, t: Date.now(), in: false };
    if (d.quadratura && QQ.tutti[id]) QQ.tutti[id] = Object.assign({}, QQ.tutti[id], { quadratura: d.quadratura });
    LZ.ridisegna();
    if (!d.pronto && !d.errore) setTimeout(() => carica(id), 3000);
  }
  // 28/10: se non quadra, l'elenco delle cose si vede subito (una volta per
  // ordine aperto; se lo richiudi resta chiuso)
  const giaAperti = new Set();
  QQ.riga = function (o) {
    const v = QQ.det[o.id];
    if (!v || (!v.in && Date.now() - (v.t || 0) > 30000)) carica(o.id);
    if (!v || (!v.q && !v.errore)) return FQ.html(null, { id: o.id });
    if (v.q && v.q.esito === 'NON_QUADRA' && !giaAperti.has(o.id)) { giaAperti.add(o.id); FQ.aperti.add(o.id); }
    return FQ.html(v.q, { id: o.id, caricando: !v.pronto && !v.errore, errore: v.errore, differenza: true });
  };

  // ── "Manda la differenza" (28/10): solo i pezzi che in Lantek mancano ──
  // Lantek si rilegge adesso; si vede cosa parte e cosa va corretto a mano;
  // parte solo col clic. Quelli in piu' in Lantek non si toccano mai da qui.
  function finestra(titolo) {
    const bg = document.createElement('div');
    bg.className = 'ft-modal-bg open';
    bg.innerHTML = `<div class="ft-modal lg" role="dialog" aria-modal="true" aria-labelledby="lz-diff-t">
      <div class="ft-modal-head"><div><h3 class="ft-modal-title" id="lz-diff-t">${esc(titolo)}</h3>
        <p class="ft-modal-sub">Rileggo Lantek adesso…</p></div></div>
      <div class="ft-modal-body"></div>
      <div class="ft-modal-foot"><button class="ft-btn" type="button" data-diff="no">Annulla</button></div></div>`;
    document.body.appendChild(bg);
    return bg;
  }

  QQ.differenza = async function (id) {
    const o = LZ.trova(id);
    const bg = finestra(`Manda la differenza a Lantek${o ? ' · ' + (o.cliente || '') + ' ' + LZ.numero(o) : ''}`);
    const sub = bg.querySelector('.ft-modal-sub'), body = bg.querySelector('.ft-modal-body'), foot = bg.querySelector('.ft-modal-foot');
    let righe = [], chiusa = false;
    const chiudi = () => { chiusa = true; bg.remove(); document.removeEventListener('keydown', tasti, true); };
    const tasti = e => { if (e.key === 'Escape') { e.preventDefault(); chiudi(); } };
    document.addEventListener('keydown', tasti, true);
    const fine = () => { if (QQ.det[id]) QQ.det[id].t = 0; LZ.ridisegna(); };
    bg.addEventListener('click', async e => {
      if (e.target === bg) return chiudi();
      const b = e.target.closest('button[data-diff]');
      if (!b || b.disabled) return;
      if (b.dataset.diff === 'no') { chiudi(); fine(); return; }
      if (b.dataset.diff === 'riprova') { leggi(); return; }
      b.disabled = true;
      b.textContent = 'Mando a Lantek…';
      sub.textContent = 'Lantek sta importando gli ordini di produzione: non chiudere la pagina.';
      const r = await LZ.azione(() => LZ.post(`/api/orders/${encodeURIComponent(id)}/lantek-differenza`,
        { righe: righe.map(x => ({ codice: x.codice, quantita: x.quantita })) }));
      if (chiusa) return;
      if (!r.ok) {
        sub.textContent = r.d.error || 'Non mandato';
        foot.innerHTML = `<button class="ft-btn" type="button" data-diff="no">Chiudi</button>
          <button class="ft-btn primary" type="button" data-diff="riprova">Riprova</button>`;
        return;
      }
      const prob = r.d.problemi || [];
      sub.textContent = prob.length ? 'Mandato, ma riletto in Lantek qualcosa non torna:' : `Mandato e riletto in Lantek: ${r.d.mandati} ${r.d.mandati === 1 ? 'codice' : 'codici'}, tutto giusto.`;
      body.innerHTML = prob.length ? `<table class="ftq-tab"><tbody>${prob.map(p => `<tr><td class="cod">${esc(p.codice)}</td><td class="cosa"><b>${esc(p.problema)}</b></td></tr>`).join('')}</tbody></table>` : '';
      foot.innerHTML = '<button class="ft-btn primary" type="button" data-diff="no">Chiudi</button>';
      foot.querySelector('button').focus();
    });

    async function leggi() {
      sub.textContent = 'Rileggo Lantek adesso…';
      body.innerHTML = '';
      foot.innerHTML = '<button class="ft-btn" type="button" data-diff="no">Annulla</button>';
      const r = await LZ.get(`/api/orders/${encodeURIComponent(id)}/lantek-differenza`);
      if (chiusa) return;
      if (!r.ok) {
        sub.textContent = (r.d.error || 'Lantek non si legge') + ' Niente è stato mandato.';
        foot.innerHTML = `<button class="ft-btn" type="button" data-diff="no">Chiudi</button>
          <button class="ft-btn primary" type="button" data-diff="riprova">Riprova</button>`;
        return;
      }
      righe = r.d.manda || [];
      const aMano = r.d.a_mano || [];
      const n = righe.reduce((s, x) => s + x.quantita, 0);
      sub.textContent = righe.length
        ? `Manda a Lantek solo quello che manca: ${righe.length} ${righe.length === 1 ? 'codice' : 'codici'}, ${FT.numero ? FT.numero(n) : n} pezzi in più. Quello che c'è già non si tocca.`
        : 'In Lantek non manca niente da mandare.';
      body.innerHTML = (righe.length ? `<table class="ftq-tab"><thead><tr><th>Codice in Lantek</th><th class="n">Ordinato</th>
          <th class="n">In Lantek</th><th class="n">Si manda</th></tr></thead><tbody>${righe.map(x => `<tr>
          <td class="cod">${esc(x.codice)}${x.riga && x.riga.toUpperCase() !== x.codice.toUpperCase() ? `<small>riga ${esc(x.riga)}</small>` : ''}</td>
          <td class="n">${x.ordinato == null ? '—' : esc(x.ordinato)}</td><td class="n">${esc(x.lantek || 0)}</td>
          <td class="n"><b>+${esc(x.quantita)}</b></td></tr>`).join('')}</tbody></table>` : '')
        + (aMano.length ? `<h4 style="margin:16px 0 4px;font-size:14px">Da correggere a mano in Lantek (da qui non si toglie niente)</h4>
          <table class="ftq-tab"><tbody>${aMano.map(x => `<tr><td class="cod">${esc(x.codice)}</td>
          <td class="cosa"><b>${esc(x.cosa)}</b><small>${esc(x.testo)}</small></td></tr>`).join('')}</tbody></table>` : '');
      foot.innerHTML = '<button class="ft-btn" type="button" data-diff="no">Annulla</button>'
        + (righe.length ? `<button class="ft-btn primary" type="button" data-diff="si">Manda ${righe.length} ${righe.length === 1 ? 'codice' : 'codici'} a Lantek</button>` : '');
      const b = foot.querySelector('[data-diff="si"]') || foot.querySelector('button');
      b.focus();
    }
    leggi();
  };
  document.addEventListener('click', e => {
    const b = e.target.closest('[data-ftq-diff]');
    if (!b) return;
    e.preventDefault();
    QQ.differenza(b.dataset.ftqDiff);
  });

  // ── prima di mandare / tagliare ───────────────────────────────────────
  QQ.prima = async function (ids, azione) {
    const ok = await FQ.prima(ids, azione, { nome: id => { const o = LZ.trova(id); return o ? (o.cliente || '') : ''; } });
    for (const id of ids) { if (QQ.det[id]) QQ.det[id].t = 0; }
    return ok;
  };
})();
