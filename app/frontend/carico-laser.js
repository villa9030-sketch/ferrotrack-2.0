/* Carico del laser: quante ore di taglio pesano su ogni giorno.
   Stesse regole del calendario della pagina laser (laser.html, modulo
   "saturazione"): taglio dei pezzi x correzione verso Lantek + carico/scarico
   (minuti a lamiera, secondi a pezzo), lamiere stimate col Banco lamiere.
   Lo usa l'ufficio per promettere consegne realistiche e vedere gli ordini a
   rischio. Se cambia il calcolo nella pagina laser, va cambiato anche qui. */
window.CaricoLaser = (() => {
  const S = { banco: null, cfg: null, cache: {}, t: 0 };
  const DEF = { ore_turno: 8, ore_riserva: 2, giorni: [1, 2, 3, 4, 5], carico_min_lamiera: 5, scarico_s_pezzo: 3, fattore_tempo: 1.18 };

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
      S.cache = {}; S.t = Date.now();
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
  /** Giorno in cui l'ordine pesa sul laser. */
  function giorno(o) {
    const fatto = o.data_taglio_completato || o.taglio_il;
    if (stato(o) === 'tagliato' && fatto) { const d = FT.data(fatto); if (d) return iso(d); }
    if (stato(o) !== 'escluso' && o.data_taglio_pianificata) return String(o.data_taglio_pianificata).slice(0, 10);
    return giornoConsegna(o);
  }
  /** Minuti utili del giorno (turno meno riserva; eccezioni del calendario). */
  function capacita(ds) {
    const c = cfg();
    const e = (c.eccezioni || {})[ds];
    if (e) return Math.max(0, e.ore) * 60;
    const g = ((FT.data(ds).getDay() + 6) % 7) + 1;
    return (c.giorni || []).includes(g) ? Math.max(0, c.ore_turno - c.ore_riserva) * 60 : 0;
  }

  // ── lamiere: disposizione per ingombro (copia di lbDisponi/lbPiano del Banco)
  function disponi(pezzi, W, H, marg, gap) {
    const UW = W - 2 * marg + gap, UH = H - 2 * marg + gap;
    const items = [];
    for (const p of pezzi) {
      if (!(p.w_mm > 0 && p.h_mm > 0)) continue;
      const n = Math.min(p.quantita || 1, 2000);
      for (let k = 0; k < n; k++) items.push({ p, w: p.w_mm, h: p.h_mm });
    }
    items.sort((a, b) => Math.max(b.w, b.h) - Math.max(a.w, a.h) || b.w * b.h - a.w * a.h);
    const fogli = [], fuori = [];
    const nuovo = () => ({ free: [{ x: 0, y: 0, w: UW, h: UH }], rects: [], W, H });
    const trova = (f, w, h) => {
      let best = null;
      for (const r of f.free) for (const [ww, hh] of [[w, h], [h, w]]) {
        if (ww > r.w || hh > r.h) continue;
        const destra = r.x + ww, sotto = r.y + hh;
        if (!best || destra < best.destra || (destra === best.destra && sotto < best.sotto)) best = { x: r.x, y: r.y, w: ww, h: hh, destra, sotto };
      }
      return best;
    };
    const piazza = (f, n) => {
      const out = [];
      for (const r of f.free) {
        if (n.x >= r.x + r.w || n.x + n.w <= r.x || n.y >= r.y + r.h || n.y + n.h <= r.y) { out.push(r); continue; }
        if (n.x > r.x) out.push({ x: r.x, y: r.y, w: n.x - r.x, h: r.h });
        if (n.x + n.w < r.x + r.w) out.push({ x: n.x + n.w, y: r.y, w: r.x + r.w - n.x - n.w, h: r.h });
        if (n.y > r.y) out.push({ x: r.x, y: r.y, w: r.w, h: n.y - r.y });
        if (n.y + n.h < r.y + r.h) out.push({ x: r.x, y: n.y + n.h, w: r.w, h: r.y + r.h - n.y - n.h });
      }
      f.free = out.filter((a, i) => !out.some((b, j) => j !== i && a.x >= b.x && a.y >= b.y && a.x + a.w <= b.x + b.w && a.y + a.h <= b.y + b.h
        && (j < i || a.x !== b.x || a.y !== b.y || a.w !== b.w || a.h !== b.h)));
    };
    for (const it of items) {
      const w = it.w + gap, h = it.h + gap;
      if (!((w <= UW && h <= UH) || (h <= UW && w <= UH))) { fuori.push(it.p); continue; }
      let posto = null, foglio = null;
      for (const f of fogli) { posto = trova(f, w, h); if (posto) { foglio = f; break; } }
      if (!posto) { foglio = nuovo(); fogli.push(foglio); posto = trova(foglio, w, h); }
      piazza(foglio, posto);
      foglio.rects.push({ p: it.p, x: marg + posto.x, y: marg + posto.y, w: posto.w - gap, h: posto.h - gap });
    }
    for (const f of fogli) {
      const xMax = Math.max(0, ...f.rects.map(r => r.x + r.w));
      const avanzo = Math.max(0, W - marg - xMax - gap);
      f.usato = avanzo > 50 ? Math.min(W, xMax + gap) / W : 1;
    }
    const consumo = fogli.reduce((s, f) => s + f.usato * W * H, 0);
    const n = fogli.length;
    return { fogli, punteggio: fuori.length ? 1e15 * fuori.length + consumo : consumo * (1 + 0.04 * Math.max(0, n - 1)) };
  }
  function lamiere(pezzi) {
    const B = S.banco;
    if (!B || !pezzi.some(p => p.w_mm > 0 && p.h_mm > 0)) return pezzi.length ? 1 : 0;
    const { margine_mm: m, distanza_mm: d } = B.parametri;
    const piani = B.formati.map(f => disponi(pezzi, f.w_mm, f.h_mm, m, d)).sort((a, b) => a.punteggio - b.punteggio);
    return Math.max(1, piani[0].fogli.length);
  }

  /** Minuti di laser dell'ordine: {min|null, fonte: stima|mano|ignota|nessuna} */
  function durata(o) {
    if (stato(o) === 'escluso') return { min: 0, fonte: 'nessuna' };
    if (S.cache[o.id]) return S.cache[o.id];
    const c = cfg();
    const per = new Map();
    for (const g of (S.banco ? S.banco.gruppi : [])) for (const p of g.pezzi) {
      if (p.ordine_id !== o.id) continue;
      if (!per.has(g.chiave)) per.set(g.chiave, []);
      per.get(g.chiave).push(p);
    }
    let r;
    if (per.size) {
      let tot = 0;
      for (const pezzi of per.values()) {
        const t = pezzi.reduce((s, p) => s + (p.tempo_min || 0) * (p.quantita || 1), 0) * (c.fattore_tempo || 1);
        const n = pezzi.reduce((s, p) => s + (p.quantita || 1), 0);
        tot += t + lamiere(pezzi) * c.carico_min_lamiera + n * c.scarico_s_pezzo / 60;
      }
      r = { min: tot, fonte: 'stima' };
    } else if (o.durata_laser_manuale_min > 0) r = { min: o.durata_laser_manuale_min, fonte: 'mano' };
    else r = { min: null, fonte: 'ignota' };
    S.cache[o.id] = r;
    return r;
  }

  /** Carico di un giorno sugli `ordini` dati: {min, cap, ignoti, n, pct} */
  function caricoGiorno(ds, ordini) {
    let min = 0, ignoti = 0, n = 0;
    for (const o of ordini) {
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
  return { carica, cfg, stato, giorno, capacita, durata, caricoGiorno, giorniLavorativi, primoGiornoLibero, fmtOre, livello, iso };
})();
