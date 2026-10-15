/* Carico del laser: quante ore di taglio pesano su ogni giorno.
   Il CONTO lo fa il server (backend/carico_laser.py, mandato con
   /api/laser/banco): minuti di ogni ordine, giorno su cui pesa (il lavoro non
   tagliato col "taglia entro" gia' passato pesa su OGGI), "In ritardo" e
   "Taglio in ritardo". Qui si sommano soltanto: l'ufficio (impiegata.html) e
   il laser (laser-saturazione.js) leggono gli stessi numeri con le stesse
   parole. Prima ogni pagina contava a modo suo (ufficio "0 ore, 0 in
   ritardo", laser "5 h, 3 in ritardo" sullo stesso database). */
window.CaricoLaser = (() => {
  const S = { banco: null, cfg: null, t: 0 };
  const DEF = { ore_turno: 8, ore_riserva: 2, giorni: [1, 2, 3, 4, 5], carico_min_lamiera: 5, scarico_s_pezzo: 3, fattore_tempo: 1 };

  const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  const giornoConsegna = o => (o && o.data_consegna ? String(o.data_consegna).slice(0, 10) : null);

  async function carica(forza) {
    if (!forza && S.banco && Date.now() - S.t < 55000) return true;
    const dal = new Date(); dal.setDate(dal.getDate() - 62);
    try {
      const [b, c] = await Promise.all([
        fetch(`/api/laser/banco?da_smistare=1&dal=${iso(dal)}`).then(r => r.json()),
        fetch('/api/laser/calendario-config').then(r => r.json()),
      ]);
      if (b && b.success) S.banco = b;
      if (c && c.success) S.cfg = c.config;
      S.t = Date.now();
      return !!S.banco;
    } catch (e) { return false; }
  }
  function cfg() { return S.cfg || DEF; }

  /** tagliato | escluso | coda | smistare (come la pagina laser) */
  function stato(o) {
    if (o.taglio_completato || o.taglio_fatto) return 'tagliato';
    if (o.taglio_richiesto === false) return 'escluso';
    if (o.taglio_richiesto === true) return 'coda';
    return 'smistare';
  }
  const daServer = (campo, id) => (S.banco && S.banco[campo] ? S.banco[campo][id] : undefined);
  /** Giorno su cui l'ordine pesa sul laser (dal server). */
  function giorno(o) {
    const g = daServer('giorni_carico', o.id);
    if (g !== undefined) return g;
    return stato(o) === 'escluso' ? null : giornoConsegna(o);
  }
  /** Minuti utili del giorno (turno meno riserva; eccezioni del calendario). */
  function capacita(ds) {
    const c = cfg();
    const e = (c.eccezioni || {})[ds];
    if (e) return Math.max(0, e.ore) * 60;
    const g = ((FT.data(ds).getDay() + 6) % 7) + 1;
    return (c.giorni || []).includes(g) ? Math.max(0, c.ore_turno - c.ore_riserva) * 60 : 0;
  }
  /** Minuti di laser dell'ordine (dal server): {min|null, fonte: stima|mano|ignota|nessuna} */
  function durata(o) {
    if (stato(o) === 'escluso') return { min: 0, fonte: 'nessuna' };
    const d = daServer('durate', o.id);
    if (d) return d;
    if (o.durata_laser_manuale_min > 0) return { min: o.durata_laser_manuale_min, fonte: 'mano' };
    return { min: null, fonte: 'ignota' };
  }
  /** Gli ordini che pesano sul laser: quelli che manda il server (stessi per
      ogni pagina, qualunque elenco abbia la pagina). */
  function ordiniServer() {
    return Object.keys((S.banco && S.banco.durate) || {}).map(id => ({ id }));
  }
  /** Il riepilogo del server: periodo (prossimi 5 giorni lavorativi), in
      ritardo (consegna), taglio in ritardo ("taglia entro"). */
  function riepilogo() { return (S.banco && S.banco.riepilogo) || null; }

  /** Carico di un giorno sugli `ordini` dati: {min, cap, ignoti, n, pct} */
  function caricoGiorno(ds, _ordini) {
    let min = 0, ignoti = 0, n = 0;
    for (const o of ordiniServer()) {
      if (stato(o) === 'escluso' || giorno(o) !== ds) continue;
      n++;
      const d = durata(o);
      if (d.min == null) ignoti++; else min += d.min;
    }
    const cap = capacita(ds);
    return { min, cap, ignoti, n, pct: cap ? min / cap : (min ? 9 : 0) };
  }
  /** I prossimi `n` giorni lavorativi da `dal` (compreso), come 'AAAA-MM-GG'. */
  function giorniLavorativi(dal, n, indietro) {
    const out = [];
    const d = FT.data(dal || FT.oggiISO());
    for (let k = 0; out.length < n && k < 120; k++) {
      const ds = iso(d);
      if (capacita(ds) > 0) out.push(ds);
      d.setDate(d.getDate() + (indietro ? -1 : 1));
    }
    return indietro ? out.reverse() : out;
  }
  /** Primo giorno lavorativo da oggi in cui il laser ha ancora spazio (< soglia). */
  function primoGiornoLibero(ordini, soglia = 0.8) {
    for (const ds of giorniLavorativi(FT.oggiISO(), 60)) if (caricoGiorno(ds, ordini).pct < soglia) return ds;
    return null;
  }
  function fmtOre(min) {
    if (min == null) return '?';
    const h = min / 60;
    return h < 1 ? `${Math.round(min)} min` : `${h.toLocaleString('it-IT', { maximumFractionDigits: 1 })} h`;
  }
  function livello(pct) { return pct > 1 ? 'over' : pct > 0.8 ? 'warn' : 'ok'; }
  return { carica, cfg, stato, giorno, capacita, durata, caricoGiorno, giorniLavorativi, primoGiornoLibero, fmtOre, livello, iso, riepilogo };
})();
