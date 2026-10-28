/* Pagina laser — il PACCO DI TAGLIO (16/10/2026).

   Stefano al laser lavora a pacchi: "tutti quelli che posso" con gli stessi
   spessori, dentro l'ordine che scade per primo; controlla i disegni mentre
   importa il pacco in Lantek; taglia; poi l'ufficio deve saperlo.
   Il pacco e' un oggetto con i suoi stati (backend/api_pacchi.py):

     Proposto -> Confermato -> In Lantek -> Tagliato

   - si spuntano gli ordini nella tabella e si preme "Crea pacco" (P): il
     pacco nasce PROPOSTO e si apre nel pannello a destra, coi conti per
     lamiera, gli avvisi e gli ordini con gli stessi spessori da aggiungere;
   - "Conferma il pacco", poi "Prepara e manda a Lantek": i disegni da
     controllare lamiera per lamiera (lo stesso controllo di sempre), poi un
     invio per tutto il pacco (la stessa finestra "Manda a Lantek", ordine
     per ordine, con i doppioni bloccati e le cose a mano da spuntare);
   - "Segna tagliata" per lamiera o "Segna tutto tagliato": l'ordine con
     tutte le lamiere tagliate e' "taglio fatto" e l'ufficio lo vede in
     "Forse finiti". Ogni passo ha Annulla.
   Un ordine si puo' sempre mandare e tagliare da solo, come prima. */
(function () {
  'use strict';
  const LZ = window.LZ;
  const S = LZ.s;
  const esc = LZ.esc;
  const P = LZ.pacchi = {};
  S.pacchi = S.pacchi || [];      // /api/pacchi: aperti e tagliati da poco
  S.pacco = S.pacco || null;      // id del pacco aperto a destra
  S.paccoDett = S.paccoDett || {}; // /api/pacchi/<id>: {d, carica, errore}
  const APERTI = ['proposto', 'confermato', 'in_lantek'];
  const PASSI = [['proposto', 'Proposto'], ['confermato', 'Confermato'], ['in_lantek', 'In Lantek'], ['tagliato', 'Tagliato']];

  // ── Dati ────────────────────────────────────────────────────────────
  P.carica = async function () {
    const r = await LZ.get('/api/pacchi');
    if (!r.ok) return false;
    const prima = JSON.stringify(S.pacchi);
    S.pacchi = r.d.pacchi || [];
    if (S.pacco && !S.pacchi.some(p => p.id === S.pacco) && !S.paccoDett[S.pacco]) S.pacco = null;
    if (JSON.stringify(S.pacchi) !== prima) LZ.ridisegna();
    return true;
  };
  P.dettaglio = async function (id) {
    const prima = S.paccoDett[id] || {};
    S.paccoDett[id] = { ...prima, carica: true };
    const r = await LZ.get(`/api/pacchi/${encodeURIComponent(id)}`);
    S.paccoDett[id] = r.ok ? { d: r.d, carica: false, errore: null } : { ...prima, carica: false, errore: r.d.error || 'Il server non ha risposto' };
    if (r.ok) {
      const i = S.pacchi.findIndex(p => p.id === id);
      if (i >= 0) S.pacchi[i] = r.d.pacco; else if (r.d.pacco) S.pacchi.unshift(r.d.pacco);
    }
    LZ.ridisegna();
  };
  P.trova = id => S.pacchi.find(p => p.id === id) || ((S.paccoDett[id] || {}).d || {}).pacco || null;
  /** Il pacco aperto che contiene l'ordine, o null. */
  P.diOrdine = oid => S.pacchi.find(p => p.aperto && p.ordini.includes(oid)) || null;
  const ordiniDi = p => p.ordini.map(LZ.trova).filter(Boolean);

  // ── Conti dal riassunto dell'elenco ───────────────────────────────
  const lamiereOrdine = o => LZ.lamiere(o).l;
  const nomeChiave = k => { const [m, s] = String(k || '').split('|'); return LZ.nomeLamiera({ materiale: m, spessore: s === '?' ? null : s }); };
  /** Le lamiere del pacco (chiavi) nell'ordine in cui si tagliano: prima
      quella che scade prima, a parita' la piu' grande. */
  function chiaviPacco(p) {
    const d = (S.paccoDett[p.id] || {}).d;
    const m = new Map();
    for (const o of ordiniDi(p)) for (const x of lamiereOrdine(o)) {
      const e = LZ.entro(o) || '9999';
      const g = m.get(x.chiave) || { k: x.chiave, primo: e, pz: 0 };
      if (e < g.primo) g.primo = e;
      g.pz += Number(x.n_pezzi) || 0;
      m.set(x.chiave, g);
    }
    if (d) for (const x of d.lamiere || []) if (!m.has(x.chiave)) m.set(x.chiave, { k: x.chiave, primo: '9999', pz: x.n_pezzi || 0 });
    return [...m.values()].sort((a, b) => a.primo.localeCompare(b.primo) || b.pz - a.pz).map(g => g.k);
  }
  const primoEntro = p => ordiniDi(p).map(LZ.entro).filter(Boolean).sort()[0] || null;
  const primaConsegna = p => ordiniDi(p).map(LZ.giorno).filter(Boolean).sort()[0] || null;
  const pezziPacco = p => ordiniDi(p).reduce((s, o) => s + ((S.riep[o.id] || {}).n_pezzi || 0), 0);

  /** Ordini liberi (da mandare o in Lantek, in nessun pacco aperto) con
      almeno una lamiera del pacco: [{o, lam: [{chiave, n_pezzi}]}] per taglia entro. */
  function suggeriti(p) {
    const ks = new Set(chiaviPacco(p));
    const out = [];
    for (const o of LZ.ordiniLaser()) {
      const st = LZ.statoLaser(o);
      if (!['coda', 'in_lantek'].includes(st) || p.ordini.includes(o.id) || P.diOrdine(o.id)) continue;
      const lam = lamiereOrdine(o).filter(x => ks.has(x.chiave));
      if (lam.length) out.push({ o, lam });
    }
    const k = x => (LZ.entro(x.o) || '9999') + (LZ.giorno(x.o) || '9999') + x.o.id;
    return out.sort((a, b) => k(a).localeCompare(k(b)));
  }

  /** Cosa c'e' da sapere prima di andare avanti: [{tono: ''|att|err, t, az?}] */
  function avvisi(p) {
    const out = [];
    const os = ordiniDi(p);
    const nomi = l => l.map(o => LZ.numeroBreve(o)).join(', ');
    const mancano = p.ordini.filter(id => !LZ.trova(id));
    if (mancano.length) out.push({ tono: 'err', t: `${LZ.plur(mancano.length, 'ordine non è', 'ordini non sono')} più al laser: toglili dal pacco.`, az: { t: 'Togli', k: 'togli-mancanti' } });
    const fuori = os.filter(o => !['coda', 'in_lantek', 'tagliato'].includes(LZ.statoLaser(o)));
    if (fuori.length) out.push({ tono: 'err', t: `${nomi(fuori)}: non è più fra quelli da tagliare.` });
    if (p.stato === 'tagliato') return out;
    const senzaData = os.filter(o => !LZ.entro(o) && LZ.statoLaser(o) !== 'tagliato');
    if (senzaData.length) out.push({ tono: 'att', t: `Taglia entro da decidere: ${nomi(senzaData)}. Aprilo e scegli la data.` });
    const corr = os.filter(o => (S.riep[o.id] || {}).n_correggere);
    if (corr.length) out.push({ tono: 'err', t: `Da correggere in Lantek: ${corr.map(o => `${LZ.numeroBreve(o)} (${LZ.plur(S.riep[o.id].n_correggere, 'codice', 'codici')})`).join(', ')}. Quei pezzi non si tagliano finché non sono a posto.` });
    if (['proposto', 'confermato'].includes(p.stato)) {
      const coda = os.filter(o => LZ.statoLaser(o) === 'coda');
      const bl = coda.filter(o => ((S.riep[o.id] || {}).bloccati || []).length);
      for (const o of bl) {
        const b = S.riep[o.id].bloccati;
        out.push({ tono: 'err', t: `${LZ.numeroBreve(o)} bloccato: ${b.slice(0, 2).map(x => `${x.codice}, ${x.motivo}`).join('; ')}${b.length > 2 ? ` e altri ${b.length - 2}` : ''}.`, az: { t: 'Apri', k: 'apri-ordine', id: o.id } });
      }
      const ab = coda.filter(o => (S.riep[o.id] || {}).n_abbinare);
      if (ab.length) out.push({ tono: 'att', t: `Righe da abbinare a Lantek: ${nomi(ab)}.` });
      const nCtl = coda.reduce((s, o) => s + ((S.riep[o.id] || {}).n_controllare || 0), 0);
      if (nCtl) out.push({ tono: 'att', t: `${LZ.plur(nCtl, 'disegno da controllare', 'disegni da controllare')} prima di Lantek.` });
      // l'ordine che scade per primo deve entrare: chi, fuori, scade prima?
      const primo = primoEntro(p);
      const prima = suggeriti(p).filter(x => LZ.entro(x.o) && (!primo || LZ.entro(x.o) < primo));
      for (const x of prima.slice(0, 2)) {
        out.push({ tono: 'att', t: `Fuori dal pacco ${LZ.numeroBreve(x.o)} ${x.o.cliente || ''} (${x.lam.map(LZ.nomeLamiera).join(', ')}) va tagliato entro ${LZ.giornoLungo(LZ.entro(x.o))}, prima del primo del pacco.`, az: { t: 'Aggiungi', k: 'aggiungi', id: x.o.id } });
      }
    }
    return out;
  }

  /** Il prossimo passo del pacco: {t, s, bottoni: [{t, k, pri, tasto, dis}], link: [{t, k}]} */
  function passo(p) {
    const os = ordiniDi(p);
    const coda = os.filter(o => LZ.statoLaser(o) === 'coda');
    const ore = minutiPacco(p);
    const ks = chiaviPacco(p);
    const pe = primoEntro(p);
    const sotto = `${LZ.plur(os.length, 'ordine', 'ordini')} su ${LZ.plur(ks.length, 'lamiera', 'lamiere')}${ore != null ? `, ${LZ.ore(ore)} di laser` : ''}.`
      + (pe ? ` Il primo va tagliato entro ${LZ.giornoLungo(pe)}.` : '');
    if (p.stato === 'proposto') {
      return { t: 'Pacco proposto: guardalo e confermalo', s: `${sotto} Puoi aggiungere o togliere ordini anche dopo.`,
               bottoni: [{ t: 'Conferma il pacco', k: 'conferma', pri: true, tasto: 'Invio' }], link: [{ t: 'Sciogli il pacco', k: 'sciogli' }] };
    }
    if (p.stato === 'confermato') {
      const nCtl = coda.reduce((s, o) => s + ((S.riep[o.id] || {}).n_controllare || 0), 0);
      const soloCorr = o => { const r = S.riep[o.id] || {}; return r.n_correggere && !r.n_invio; };
      const fermi = coda.filter(o => !(S.riep[o.id] || {}).pronto && !soloCorr(o));
      const daMandare = coda.filter(o => (S.riep[o.id] || {}).pronto);
      const link = [{ t: 'Li ho già messi in Lantek a mano', k: 'a-mano' }, { t: 'Rimettilo proposto', k: 'proposto' }];
      if (nCtl) {
        return { t: 'Prepara e manda a Lantek: prima i disegni', s: `${LZ.plur(nCtl, 'disegno da controllare', 'disegni da controllare')}, lamiera per lamiera come li importi in Lantek. Poi un invio per tutto il pacco.`,
                 bottoni: [{ t: `Controlla ${LZ.plur(nCtl, 'disegno', 'disegni')}`, k: 'controlla', pri: true, tasto: 'Invio' }], link };
      }
      if (fermi.length) {
        return { t: 'Prepara e manda a Lantek: prima sistema i blocchi', tono: 'err',
                 s: `${fermi.map(o => LZ.numeroBreve(o)).join(', ')} non ${fermi.length === 1 ? 'può partire' : 'possono partire'}: guarda sotto cosa manca, o togli${fermi.length === 1 ? 'lo' : 'li'} dal pacco.`,
                 bottoni: [{ t: 'Manda a Lantek', k: 'manda', pri: true, dis: true }], link };
      }
      if (daMandare.length) {
        return { t: 'Prepara e manda a Lantek', s: `Disegni a posto. Un invio per tutto il pacco: ${LZ.plur(daMandare.length, 'ordine', 'ordini')}${os.length > daMandare.length ? `, gli altri sono già in Lantek` : ''}.`,
                 bottoni: [{ t: `Manda a Lantek (${LZ.plur(daMandare.length, 'ordine', 'ordini')})`, k: 'manda', pri: true, tasto: 'Invio' }], link };
      }
      return { t: 'Gli ordini sono già in Lantek', s: 'Segna il pacco In Lantek e taglia.',
               bottoni: [{ t: 'Segna In Lantek', k: 'in-lantek', pri: true, tasto: 'Invio' }], link: [{ t: 'Rimettilo proposto', k: 'proposto' }] };
    }
    if (p.stato === 'in_lantek') {
      return { t: 'In Lantek: taglia', s: 'Segna tagliata ogni lamiera quando l\'hai finita, o tutto insieme. Gli ordini finiti passano all\'ufficio in «Forse finiti».',
               bottoni: [{ t: 'Segna tutto tagliato', k: 'tutto', pri: true, tasto: 'Invio' }], link: [{ t: 'Rimettilo confermato', k: 'confermato' }] };
    }
    if (p.stato === 'tagliato') {
      return { t: `Tagliato ${FT.fmtRelativo(p.tagliato_il)}`, s: 'L\'ufficio lo vede in «Forse finiti» e in «Tagliati oggi»: non serve dirlo a voce.', bottoni: [], link: [] };
    }
    return { t: 'Pacco sciolto', s: 'Gli ordini sono di nuovo liberi.', bottoni: [], link: [{ t: 'Rimettilo su', k: 'proposto' }] };
  }
  function minutiPacco(p) {
    const d = (S.paccoDett[p.id] || {}).d;
    if (!d || !(d.lamiere || []).length) return null;
    return d.lamiere.reduce((s, x) => s + (x.min || 0), 0);
  }

  // ── Nella tabella degli ordini ──────────────────────────────────────
  /** Le righe della sezione "Pacchi" in cima alla tabella (aperti, poi i
      tagliati nelle ultime 24 ore). Stesse colonne degli ordini. */
  P.righeLista = function () {
    const pp = S.pacchi.filter(p => p.aperto || p.stato === 'tagliato');
    if (!pp.length) return '';
    const aperti = pp.filter(p => p.aperto).length;
    let h = `<tr class="lz-gruppo" data-gruppo="pacchi"><td colspan="8"><span class="lz-g-tit"><b>Pacchi</b><span>${aperti ? LZ.plur(aperti, 'aperto', 'aperti') : 'nessuno aperto'}</span></span></td></tr>`;
    for (const p of pp) {
      const on = S.pacco === p.id && !S.aperto;
      const ks = chiaviPacco(p);
      const pe = primoEntro(p), ge = pe ? FT.giorniDa(pe) : null, wd = pe ? LZ.lavorativiA(pe) : null;
      const cls = p.stato === 'tagliato' ? '' : ge != null && ge < 0 ? 'indietro' : wd != null && wd <= 2 ? 'vicino' : '';
      const entro = p.stato === 'tagliato' ? `<span class="lz-q fatto"><b>tagliato</b> <small>${esc(FT.fmtOra(p.tagliato_il))}</small></span>`
        : pe ? `<span class="lz-q ${cls}" title="Il primo da tagliare del pacco: ${esc(LZ.giornoLungo(pe))}"><b>${esc(LZ.giornoCorto(pe))}</b>${cls === 'indietro' ? ` <small>in ritardo ${-ge} gg</small>` : ''}</span>`
          : '<span class="lz-q vicino"><b>da decidere</b></span>';
      const pc = primaConsegna(p);
      h += `<tr class="lz-r lz-pacco-r${on ? ' on' : ''}${p.stato === 'tagliato' ? ' spento' : ''}" tabindex="0" data-pacco="${esc(p.id)}" data-fuoco="pacco-${esc(p.id)}"
          ${on ? 'aria-current="true"' : ''} title="${esc(`${p.nome}: ${p.parola}, ${LZ.plur(p.ordini.length, 'ordine', 'ordini')}`)}">
        <td class="c-sp"></td>
        <td class="c-num"><span class="lz-pk-nome">${esc(p.nome)}</span></td>
        <td class="c-cli"><div class="lz-cli"><b>${esc(LZ.plur(p.ordini.length, 'ordine', 'ordini'))}</b><span class="lz-sot">${esc(p.parola)}</span></div></td>
        <td class="c-lam">${esc(ks.slice(0, 3).map(nomeChiave).join(', '))}${ks.length > 3 ? ` e altre ${ks.length - 3}` : ''}</td>
        <td class="c-pz r">${esc(FT.numero(pezziPacco(p)))}</td>
        <td class="c-ore r"></td>
        <td class="c-entro">${entro}</td>
        <td class="c-cons"><span class="lz-cons">${pc ? esc(LZ.giornoCorto(pc)) : '—'}</span></td></tr>`;
    }
    return h;
  };
  /** "pacco 42" accanto al cliente, nelle righe degli ordini. */
  P.nota = function (oid) {
    const p = P.diOrdine(oid);
    return p ? `<small class="lz-anche lz-in-pacco">nel ${esc(p.nome.toLowerCase())}</small>` : '';
  };

  // ── Pannello del pacco (a destra) ───────────────────────────────────
  P.apri = function (id) {
    S.pacco = id;
    S.aperto = null;
    const d = document.getElementById('lz-dett');
    if (d) d.scrollTop = 0;
    LZ.ridisegna();
    P.dettaglio(id);
    const r = document.querySelector(`.lz-r[data-pacco="${CSS.escape(id)}"]`);
    if (r) { r.scrollIntoView({ block: 'nearest' }); r.focus({ preventScroll: true }); }
  };
  P.chiudi = function () {
    if (!S.pacco) return false;
    const id = S.pacco;
    S.pacco = null;
    LZ.ridisegna();
    const r = document.querySelector(`.lz-r[data-pacco="${CSS.escape(id)}"]`);
    if (r) r.focus({ preventScroll: false });
    return true;
  };

  P.disegnaPannello = function (el) {
    const p = P.trova(S.pacco);
    if (!p) { LZ.metti(el, '<div class="lz-vuoto"><div><b>Pacco non trovato</b><p>Chiudi con Esc.</p></div></div>'); return; }
    if (LZ.staScrivendo(el)) return;
    el.classList.remove('con-pdf', 'accanto');
    LZ.metti(el, pannello(p), el);
  };

  function statiHtml(p) {
    const i = PASSI.findIndex(x => x[0] === p.stato);
    return `<ol class="lz-pk-stati" aria-label="Stati del pacco">${PASSI.map(([k, t], j) =>
      `<li class="${j < i ? 'fatto' : ''}"${j === i ? ' aria-current="step"' : ''}>${esc(t)}</li>`).join('')}</ol>`;
  }

  function pannello(p) {
    const D = S.paccoDett[p.id] || {};
    const d = D.d;
    const os = ordiniDi(p);
    const ks = chiaviPacco(p);
    const ps = passo(p);
    const quando = p.creato_il ? `${p.creato_da ? `da ${p.creato_da}, ` : ''}${FT.fmtRelativo(p.creato_il)}` : '';
    let h = `<div class="lz-ordine lz-pacco" data-pacco="${esc(p.id)}">
      <div class="lz-o-testa">
        <div class="lz-o-tit"><h2><span class="lz-o-num">${esc(p.nome)}</span><span class="lz-o-cli">${esc(ks.map(nomeChiave).join(', ') || 'Lamiere non ancora lette')}</span></h2>
          <div class="lz-o-sotto"><span>${esc(LZ.plur(os.length, 'ordine', 'ordini'))}</span>${quando ? `<span>creato ${esc(quando)}</span>` : ''}</div></div>
        <div class="lz-o-az"><button class="lz-btn ghost piccolo" type="button" data-pk="chiudi" data-fuoco="pc-chiudi" title="Chiudi il pacco (Esc)">Chiudi</button></div>
        ${p.stato === 'annullato' ? '' : statiHtml(p)}
      </div>
      <section class="lz-passo senza-ico ${ps.tono || ''}" aria-label="Prossimo passo del pacco">
        <div class="cosa"><b>${esc(ps.t)}</b><span>${esc(ps.s)}</span>
          ${ps.link.length ? `<span class="lz-pk-link">${ps.link.map(l => `<button class="lz-link" type="button" data-pk="${l.k}" data-fuoco="pc-${l.k}">${esc(l.t)}</button>`).join('')}</span>` : ''}</div>
        <div class="bottoni">${ps.bottoni.map(b => `<button class="lz-btn grande${b.pri ? ' pri' : ''}" type="button" data-pk="${b.k}" data-fuoco="pc-${b.k}" ${b.dis ? 'disabled' : ''}>${esc(b.t)}${b.tasto && !b.dis ? LZ.tasto(b.tasto) : ''}</button>`).join('')}</div>
      </section>`;
    const av = avvisi(p);
    if (av.length) {
      h += `<ul class="lz-pk-avvisi" aria-label="Da sapere">${av.map(a => `<li class="${a.tono}"><span>${esc(a.t)}</span>${a.az ? `<button class="lz-link" type="button" data-pk="${a.az.k}" ${a.az.id ? `data-id="${esc(a.az.id)}"` : ''}>${esc(a.az.t)}</button>` : ''}</li>`).join('')}</ul>`;
    }
    h += lamiereHtml(p, d, D);
    h += ordiniHtml(p);
    if (['proposto', 'confermato'].includes(p.stato)) h += suggeritiHtml(p);
    return h + '</div>';
  }

  function lamiereHtml(p, d, D) {
    const taglia = ['confermato', 'in_lantek'].includes(p.stato);
    if (!d) {
      return `<h3 class="lz-pk-h">Per lamiera</h3>${D.errore ? `<div class="lz-avviso">${esc('Riepilogo non letto: ' + D.errore)}</div>`
        : '<div class="lz-skel" style="height:96px;border-radius:8px"></div>'}`;
    }
    const ord = chiaviPacco(p);
    const lam = (d.lamiere || []).slice().sort((a, b) => ord.indexOf(a.chiave) - ord.indexOf(b.chiave));
    if (!lam.length) return '<h3 class="lz-pk-h">Per lamiera</h3><p class="lz-pk-vuoto">Nessuna lamiera letta negli ordini del pacco (ordini col solo PDF): fa fede il PDF.</p>';
    const tot = lam.reduce((s, x) => ({ pz: s.pz + x.n_pezzi, kg: s.kg + (x.kg || 0), min: s.min + (x.min || 0) }), { pz: 0, kg: 0, min: 0 });
    const kg = v => (v == null ? '—' : FT.numero(v, v < 10 ? 1 : 0));
    return `<h3 class="lz-pk-h">Per lamiera <small>nell'ordine in cui tagliarle: prima quella che scade prima</small></h3>
      <table class="lz-tab lz-pk-tab"><thead><tr><th>Lamiera</th><th>Ordini</th><th class="r">Pezzi</th><th class="r">kg</th><th class="r">Ore</th><th class="r">Fogli</th><th></th></tr></thead><tbody>
      ${lam.map(x => {
        const fatti = x.ordini.filter(y => y.tagliata).length;
        const az = x.tagliata ? '<span class="lz-pk-fatto">tagliata</span>'
          : taglia && p.stato === 'in_lantek' ? `<button class="lz-btn piccolo" type="button" data-pk="lamiera" data-chiave="${esc(x.chiave)}" data-fuoco="pc-lam-${esc(x.chiave)}">Segna tagliata</button>${fatti ? ` <small>${fatti} di ${x.ordini.length} già</small>` : ''}`
            : fatti ? `<small>${fatti} di ${x.ordini.length} tagliate</small>` : '';
        const nomiOrd = x.ordini.map(y => { const o = LZ.trova(y.id); return o ? LZ.numeroBreve(o) : '?'; });
        return `<tr><td><b>${esc(nomeChiave(x.chiave))}</b></td>
          <td class="lz-pk-ord"><span class="mono">${esc(nomiOrd.join(', '))}</span></td>
          <td class="r">${esc(FT.numero(x.n_pezzi))}</td><td class="r" title="${x.kg_mancanti ? esc(`${x.kg_mancanti} codici senza area o spessore: non contati`) : ''}">${esc(kg(x.kg))}</td>
          <td class="r" title="${x.tempo_mancante ? esc(`${x.tempo_mancante} codici senza tempo: stimati come zero`) : 'Taglio tarato su Lantek, carico lamiere e scarico pezzi'}">${x.min != null ? esc(LZ.ore(x.min)) : '—'}</td>
          <td class="r">${x.n_fogli != null ? esc(FT.numero(x.n_fogli)) : '—'}</td><td class="r lz-pk-az">${az}</td></tr>`;
      }).join('')}</tbody>
      <tfoot><tr><td>Totale</td><td></td><td class="r">${esc(FT.numero(tot.pz))}</td><td class="r">${esc(kg(tot.kg))}</td><td class="r">${esc(LZ.ore(tot.min))}</td><td></td><td></td></tr></tfoot></table>`;
  }

  function ordiniHtml(p) {
    const togli = APERTI.includes(p.stato) && p.ordini.length > 1;
    const righe = p.ordini.map(id => {
      const o = LZ.trova(id);
      if (!o) return `<tr><td class="cod">—</td><td colspan="4" class="lz-pk-tenue">Non è più al laser</td><td class="r">${togli ? `<button class="lz-link" type="button" data-pk="togli" data-id="${esc(id)}">Togli</button>` : ''}</td></tr>`;
      const e = LZ.etichetta(o);
      const en = LZ.entro(o), ge = LZ.giorniEntro(o), wd = en ? LZ.lavorativiA(en) : null;
      const cls = LZ.statoLaser(o) === 'tagliato' ? 'fatto' : !en ? 'vicino' : ge < 0 ? 'indietro' : wd <= 2 ? 'vicino' : '';
      return `<tr class="cl" data-pk-ordine="${esc(id)}" tabindex="0" data-fuoco="pc-o-${esc(id)}" title="Apri l'ordine">
        <td class="cod">${esc(LZ.numeroBreve(o))}</td><td>${esc(o.cliente || '')}</td>
        <td><span class="lz-sot ${e.c === 'att' || e.c === 'err' ? e.c : ''}">${esc(e.corto || e.t || '…')}</span></td>
        <td class="r">${esc(FT.numero((S.riep[id] || {}).n_pezzi || 0))}</td>
        <td><span class="lz-q ${cls}"><b>${en ? esc(LZ.giornoCorto(en)) : 'da decidere'}</b></span></td>
        <td class="r">${togli ? `<button class="lz-link" type="button" data-pk="togli" data-id="${esc(id)}">Togli</button>` : ''}</td></tr>`;
    }).join('');
    return `<h3 class="lz-pk-h">Ordini del pacco</h3>
      <table class="lz-tab lz-pk-tab"><thead><tr><th>Ordine</th><th>Cliente</th><th>Stato</th><th class="r">Pezzi</th><th>Taglia entro</th><th></th></tr></thead><tbody>${righe}</tbody></table>`;
  }

  function suggeritiHtml(p) {
    const l = suggeriti(p).slice(0, 6);
    if (!l.length) return '<h3 class="lz-pk-h">Stessi spessori che potresti aggiungere</h3><p class="lz-pk-vuoto">Nessun altro ordine da tagliare ha le lamiere di questo pacco.</p>';
    return `<h3 class="lz-pk-h">Stessi spessori che potresti aggiungere <small>in fila per taglia entro</small></h3>
      <table class="lz-tab lz-pk-tab"><thead><tr><th>Ordine</th><th>Cliente</th><th>Porta</th><th>Taglia entro</th><th></th></tr></thead><tbody>
      ${l.map(x => {
        const en = LZ.entro(x.o), ge = LZ.giorniEntro(x.o), wd = en ? LZ.lavorativiA(en) : null;
        const cls = !en ? 'vicino' : ge < 0 ? 'indietro' : wd <= 2 ? 'vicino' : '';
        return `<tr><td class="cod">${esc(LZ.numeroBreve(x.o))}</td><td>${esc(x.o.cliente || '')}</td>
          <td>${esc(x.lam.map(y => `${LZ.nomeLamiera(y)} (${FT.numero(Number(y.n_pezzi) || 0)} pz)`).join('; '))}</td>
          <td><span class="lz-q ${cls}"><b>${en ? esc(LZ.giornoCorto(en)) : 'da decidere'}</b></span></td>
          <td class="r"><button class="lz-btn piccolo" type="button" data-pk="aggiungi" data-id="${esc(x.o.id)}" data-fuoco="pc-agg-${esc(x.o.id)}">Aggiungi</button></td></tr>`;
      }).join('')}</tbody></table>`;
  }

  // ── Azioni ─────────────────────────────────────────────────────────
  /** Dopo un'azione: ordini, riassunti, pacchi, il pacco aperto. */
  async function dopo(id, ordini) {
    await LZ.avvio.caricaOrdini(true);
    await Promise.all([...(ordini || []).map(o => LZ.avvio.caricaRiep(o)), P.carica()]);
    if (id) await P.dettaglio(id);
    LZ.ridisegna();
  }
  const errore = (r, cosa) => FT.toast(`${cosa}: ${(r.d && r.d.error) || 'errore del server'}`, 'err', null, 9000);

  /** "Crea pacco" (P) con gli ordini spuntati: nasce proposto. */
  P.crea = (ids) => LZ.azione(async () => {
    const liberi = ids.filter(id => !P.diOrdine(id));
    if (!liberi.length) { FT.toast(ids.length === 1 ? `È già nel ${P.diOrdine(ids[0]).nome.toLowerCase()}` : 'Sono già tutti in un pacco', 'warn'); return; }
    const r = await LZ.post('/api/pacchi', { ordini: liberi });
    if (!r.ok) { errore(r, 'Pacco non creato'); return; }
    const p = r.d.pacco;
    S.spuntati.clear();
    S.pacchi.unshift(p);
    S.pacco = p.id;
    S.aperto = null;
    const gia = ids.length - liberi.length;
    FT.toast(`${p.nome} proposto con ${LZ.plur(liberi.length, 'ordine', 'ordini')}${gia ? ` (${gia} già in un altro pacco)` : ''}: guardalo e confermalo`, 'ok',
      { testo: 'Annulla', fn: () => P.stato(p.id, 'annullato', { silenzio: true }) }, 10000);
    await dopo(p.id);
    const b = document.querySelector('#lz-dett [data-pk="conferma"]');
    if (b) b.focus({ preventScroll: true });
  });

  const TESTI = { confermato: 'confermato', proposto: 'di nuovo proposto', in_lantek: 'in Lantek', annullato: 'sciolto' };
  /** Un passo del pacco; il messaggio ha Annulla (il passo all'indietro). */
  P.stato = (id, nuovo, opz) => LZ.azione(async () => {
    opz = opz || {};
    const r = await LZ.post(`/api/pacchi/${encodeURIComponent(id)}/stato`, { stato: nuovo });
    if (!r.ok) { errore(r, 'Non riuscito'); await dopo(id); return false; }
    const p = r.d.pacco, prima = r.d.prima;
    if (nuovo === 'annullato' && S.pacco === id) S.pacco = null;
    if (!opz.silenzio && prima) {
      FT.toast(`${p.nome} ${TESTI[nuovo] || nuovo}`, 'ok', { testo: 'Annulla', fn: () => P.stato(id, prima, { silenzio: true }).then(() => { if (nuovo === 'annullato') P.apri(id); }) }, 10000);
    } else if (opz.silenzio && nuovo === 'annullato') FT.toast(`${p.nome} sciolto`, 'ok');
    await dopo(id);
    return true;
  });

  P.ordini = (id, corpo, silenzio) => LZ.azione(async () => {
    const r = await LZ.post(`/api/pacchi/${encodeURIComponent(id)}/ordini`, corpo);
    if (!r.ok) { errore(r, 'Non riuscito'); return; }
    const p = r.d.pacco;
    if (!silenzio) {
      const agg = r.d.aggiunti || [], tog = r.d.tolti || [];
      const chi = l => l.map(i => { const o = LZ.trova(i); return o ? LZ.numeroBreve(o) : 'un ordine'; }).join(', ');
      FT.toast(agg.length ? `${chi(agg)} nel ${p.nome.toLowerCase()}` : `${chi(tog)} tolto dal ${p.nome.toLowerCase()}`, 'ok',
        { testo: 'Annulla', fn: () => P.ordini(id, { aggiungi: tog, togli: agg }, true) }, 10000);
    }
    await dopo(id, [...(r.d.aggiunti || []), ...(r.d.tolti || [])]);
  });

  /** "Segna tagliata" di una lamiera (chiave) o di tutto il pacco. */
  P.tagliato = (id, chiave) => LZ.azione(async () => {
    const p0 = P.trova(id);
    // un ordine del pacco che non quadra: prima l'elenco e «Ho controllato» (laser-quadratura.js)
    if (LZ.quad && !(await LZ.quad.prima((p0 && p0.ordini) || [], 'pacco_taglio'))) return;
    let r = await LZ.post(`/api/pacchi/${encodeURIComponent(id)}/tagliato`, chiave ? { chiave } : {});
    if (!r.ok) { errore(r, 'Non segnato'); return; }
    // ordini con assiemi senza componenti / righe senza disegno: non si
    // segnano «taglio fatto» senza una conferma esplicita (DECA A 001252)
    const md = r.d.mancano_disegni || [];
    if (md.length) {
      const si = await FT.conferma({ titolo: 'Mancano dei disegni: questi ordini non sono tagliati tutti',
        testo: md.map(x => `${x.numero}: ${x.codici.join(', ')}`).join(' · ') + '. Li ho lasciati da tagliare. Vuoi segnarli «taglio fatto» lo stesso?',
        ok: 'Segna tagliati lo stesso', annulla: 'No, restano da tagliare', pericolo: true });
      if (si) {
        const tutti = md.reduce((a, x) => a.concat(x.codici), []);
        const r2 = await LZ.post(`/api/pacchi/${encodeURIComponent(id)}/tagliato`, Object.assign(chiave ? { chiave } : {}, { conferma_mancanti: tutti }));
        if (!r2.ok) { errore(r2, 'Non segnato'); return; }
        r = { ok: true, d: Object.assign({}, r2.d, { fatto: { lamiere: [...((r.d.fatto || {}).lamiere || []), ...((r2.d.fatto || {}).lamiere || [])],
          ordini: [...((r.d.fatto || {}).ordini || []), ...((r2.d.fatto || {}).ordini || [])], pacco_prima: (r.d.fatto || {}).pacco_prima } }) };
      }
    }
    const p = r.d.pacco, fatto = r.d.fatto || {};
    const nOrd = (fatto.ordini || []).length;
    const tratt = (r.d.trattenuti || []).map(i => { const o = LZ.trova(i); return o ? LZ.numeroBreve(o) : ''; }).filter(Boolean);
    let t = chiave ? `${nomeChiave(chiave)} tagliata` : `${p.nome} tagliato`;
    if (nOrd) t += `: ${LZ.plur(nOrd, 'ordine passato', 'ordini passati')} all'ufficio (Forse finiti)`;
    if (p.stato === 'tagliato' && chiave) t += `. ${p.nome} tagliato`;
    if (tratt.length) t += `. ${tratt.join(', ')} resta da tagliare: ha codici da correggere in Lantek`;
    FT.toast(t, 'ok', { testo: 'Annulla', fn: () => P.annullaTaglio(id, fatto) }, 12000);
    await dopo(id, (p0 || p).ordini);
  });
  P.annullaTaglio = (id, fatto) => LZ.azione(async () => {
    const r = await LZ.post(`/api/pacchi/${encodeURIComponent(id)}/tagliato`, { annulla: true, fatto });
    if (!r.ok) { errore(r, 'Non annullato'); return; }
    FT.toast('Taglio annullato: di nuovo da tagliare', 'ok');
    S.pacco = id;
    await dopo(id, r.d.pacco.ordini);
  });

  /** "Prepara e manda a Lantek": un passo guidato per tutto il pacco.
      1. i disegni da controllare, lamiera per lamiera (lo stesso controllo);
      2. la finestra "Manda a Lantek" con gli ordini del pacco ancora da
         mandare (la stessa di sempre: doppioni, differenze, cose a mano);
      3. quando sono tutti in Lantek, il pacco e' In Lantek. */
  P.prepara = async function (id) {
    const p = P.trova(id);
    if (!p) return;
    const coda = ordiniDi(p).filter(o => LZ.statoLaser(o) === 'coda').map(o => o.id);
    await Promise.all(coda.map(o => LZ.avvio.caricaRiep(o, true)));
    const nCtl = coda.filter(o => (S.riep[o] || {}).n_controllare);
    if (nCtl.length) {
      LZ.controllo.inizia(nCtl, { lamiere: chiaviPacco(p), dopo: () => { S.pacco = id; dopo(id, nCtl); } });
      return;
    }
    const pronti = coda.filter(o => (S.riep[o] || {}).pronto);
    if (!pronti.length) {
      if (!coda.length || coda.every(o => (S.riep[o] || {}).n_correggere && !(S.riep[o] || {}).n_invio)) await P.stato(id, 'in_lantek');
      else { FT.toast('Prima sistema i blocchi del pacco', 'warn'); LZ.ridisegna(); }
      return;
    }
    LZ.lantek.apri(pronti, {
      titolo: p.nome,
      dopo: async () => {
        S.pacco = id;
        await dopo(id, coda);
        const q = P.trova(id);
        const manca = ordiniDi(q).filter(o => LZ.statoLaser(o) === 'coda' && !((S.riep[o.id] || {}).n_correggere && !(S.riep[o.id] || {}).n_invio));
        if (manca.length) {
          FT.toast(`${q.nome} resta Confermato: ${manca.map(o => LZ.numeroBreve(o)).join(', ')} non ${manca.length === 1 ? 'è' : 'sono'} ancora in Lantek`, 'warn', null, 10000);
          return;
        }
        await P.stato(id, 'in_lantek');
      },
    });
  };

  /** "Li ho gia' messi in Lantek a mano": gli ordini da mandare diventano
      In Lantek (lo stesso del menu dell'ordine), poi il pacco. Annulla rimette tutto. */
  P.aMano = (id) => LZ.azione(async () => {
    const p = P.trova(id);
    const coda = ordiniDi(p).filter(o => LZ.statoLaser(o) === 'coda').map(o => o.id);
    const fatti = [];
    for (const o of coda) {
      const r = await LZ.post(`/api/orders/${encodeURIComponent(o)}/importato`, { annulla: false });
      if (r.ok) fatti.push(o);
    }
    await dopo(null, coda);
    const r = await LZ.post(`/api/pacchi/${encodeURIComponent(id)}/stato`, { stato: 'in_lantek' });
    if (!r.ok) { errore(r, 'Pacco non segnato In Lantek'); await dopo(id, coda); return; }
    FT.toast(`${p.nome} in Lantek`, 'ok', { testo: 'Annulla', fn: () => LZ.azione(async () => {
      await LZ.post(`/api/pacchi/${encodeURIComponent(id)}/stato`, { stato: 'confermato' });
      for (const o of fatti) await LZ.post(`/api/orders/${encodeURIComponent(o)}/importato`, { annulla: true });
      FT.toast('Annullato: di nuovo da mandare a Lantek', 'ok');
      await dopo(id, fatti);
    }) }, 12000);
    await dopo(id, coda);
  });

  /** Il pulsante principale del pacco (Invio). */
  P.invio = function () {
    const p = S.pacco && !S.aperto && P.trova(S.pacco);
    if (!p) return false;
    const b = passo(p).bottoni.find(x => x.pri && !x.dis);
    if (!b) return false;
    esegui(b.k, p.id);
    return true;
  };

  function esegui(k, id, el) {
    const p = P.trova(id);
    if (!p) return;
    if (k === 'chiudi') P.chiudi();
    else if (k === 'conferma') P.stato(id, 'confermato');
    else if (k === 'proposto') P.stato(id, 'proposto');
    else if (k === 'confermato') P.stato(id, 'confermato');
    else if (k === 'in-lantek') P.stato(id, 'in_lantek');
    else if (k === 'sciogli') P.stato(id, 'annullato');
    else if (k === 'controlla' || k === 'manda') P.prepara(id);
    else if (k === 'a-mano') P.aMano(id);
    else if (k === 'tutto') P.tagliato(id, null);
    else if (k === 'lamiera') P.tagliato(id, el.dataset.chiave);
    else if (k === 'aggiungi') P.ordini(id, { aggiungi: [el.dataset.id] });
    else if (k === 'togli') P.ordini(id, { togli: [el.dataset.id] });
    else if (k === 'togli-mancanti') P.ordini(id, { togli: p.ordini.filter(x => !LZ.trova(x)) });
    else if (k === 'apri-ordine') LZ.lista.apri(el.dataset.id);
  }
  document.addEventListener('click', e => {
    const dett = e.target.closest('#lz-dett .lz-pacco');
    if (!dett) return;
    const b = e.target.closest('[data-pk]');
    if (b) { if (!b.disabled) esegui(b.dataset.pk, dett.dataset.pacco, b); return; }
    const r = e.target.closest('[data-pk-ordine]');
    if (r) LZ.lista.apri(r.dataset.pkOrdine);
  });
  document.addEventListener('keydown', e => {
    if (e.key !== 'Enter') return;
    const r = e.target && e.target.closest && e.target.closest('#lz-dett [data-pk-ordine]');
    if (r) { e.preventDefault(); e.stopPropagation(); LZ.lista.apri(r.dataset.pkOrdine); }
  }, true);

  // ── Avvio: i pacchi si rileggono ogni 15 s (le altre postazioni) ─────
  P.avvia = function () {
    P.carica();
    setInterval(() => { if (!document.hidden && !S.occupato) P.carica(); }, 15000);
  };
})();
