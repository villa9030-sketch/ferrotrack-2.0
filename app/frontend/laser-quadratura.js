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
    return FQ.html(v.q, { id: o.id, caricando: !v.pronto && !v.errore, errore: v.errore });
  };

  // ── prima di mandare / tagliare ───────────────────────────────────────
  QQ.prima = async function (ids, azione) {
    const ok = await FQ.prima(ids, azione, { nome: id => { const o = LZ.trova(id); return o ? (o.cliente || '') : ''; } });
    for (const id of ids) { if (QQ.det[id]) QQ.det[id].t = 0; }
    return ok;
  };
})();
