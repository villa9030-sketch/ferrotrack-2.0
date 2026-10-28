/* Pagina laser — l'elenco degli ordini da tagliare.

   16/10/2026 (ricerca "Design FerroTrack meno AI"): Stefano ragiona a
   lamiere e pacchi, non un ordine alla volta. L'elenco e' una tabella a
   tutta larghezza RAGGRUPPATA PER LAMIERA (materiale + spessore):
   - ogni gruppo ha la sua testata: lamiera, quanti ordini, pezzi, il primo
     "taglia entro";
   - dentro il gruppo le righe vanno per "taglia entro" (poi consegna);
   - un ordine con piu' spessori sta in ogni suo gruppo: la prima volta e'
     la riga "vera" (data-id), le altre dicono "anche in Inox 2 mm" e
     portano allo stesso ordine (data-rif);
   - le lamiere gia' segnate tagliate non tengono l'ordine nel gruppo;
   - in cima "Da smistare" (si decide prima se passa dal laser), in fondo
     i tagliati nelle ultime 24 ore;
   - il cliente e' una colonna e un filtro, non il raggruppamento.
   Sopra la tabella UNA riga dice cosa fare adesso. Aperto un ordine, la
   tabella si stringe a sinistra e l'ordine sta nel pannello a destra.
   Le caselle servono a lavorare piu' ordini insieme: "Controlla N disegni"
   e "Manda a Lantek (K ordini)".

   28/10/2026 (Stefano: "capisco poco"; tela "Laser, più chiaro"): per
   lamiera lo stesso ordine compariva in ogni suo spessore (5 ordini, 15
   righe). Ora UN ORDINE = UNA RIGA, divisi per cosa c'e' da fare (Da
   mandare a Lantek, In Lantek da tagliare); gli spessori sono una colonna.
   Il raggruppamento per lamiera resta nella vista "Per lamiera" del menu
   (S.vista = 'lamiere'): una riga per lamiera con gli ordini che la usano.
   Rosso solo per il taglio in ritardo e la consegna passata. */
(function () {
  'use strict';
  const LZ = window.LZ;
  const S = LZ.s;
  const esc = LZ.esc;
  const L = LZ.lista = {};
  S.filtroCliente = S.filtroCliente || '';
  S.filtroStato = S.filtroStato || '';

  let costruita = false;
  function costruisci() {
    const el = document.getElementById('lz-lista');
    el.innerHTML = `<div class="lz-lista-testa" id="lz-l-testa"></div>
      <div class="lz-righe" id="lz-l-righe"></div>
      <div id="lz-l-az"></div>
      <p class="lz-tasti-nota"><kbd>↑</kbd> <kbd>↓</kbd> scorri, <kbd>Spazio</kbd> spunta, <kbd>P</kbd> crea il pacco, <kbd>C</kbd> controlla i disegni, <kbd>Esc</kbd> chiudi</p>`;
    el.addEventListener('click', clic);
    el.addEventListener('change', e => {
      const c = e.target.closest('input[data-sp]');
      if (c) { L.spunta(c.dataset.sp, c.checked); return; }
      const f = e.target.closest('select[data-filtro]');
      if (f) { S[f.dataset.filtro] = f.value; LZ.ridisegna(); }
    });
    costruita = true;
  }

  /** Si possono spuntare gli ordini da mettere in Lantek. */
  L.spuntabile = o => LZ.statoLaser(o) === 'coda';
  /** Gia' in un pacco aperto (laser-pacchi.js): "Seleziona i N" lo salta. */
  L.inPacco = o => !!(LZ.pacchi && LZ.pacchi.diOrdine(o.id));

  // ── Date: giorni lavorativi e formati italiani (Intl) ─────────────────
  const fmtGiorno = new Intl.DateTimeFormat('it-IT', { weekday: 'short', day: '2-digit', month: '2-digit' });
  const fmtGiornoLungo = new Intl.DateTimeFormat('it-IT', { weekday: 'long', day: 'numeric', month: 'long' });
  /** "gio 15/10" */
  LZ.giornoCorto = iso => { const d = iso && FT.data(String(iso).slice(0, 10)); return d ? fmtGiorno.format(d).replace(',', '') : ''; };
  /** "giovedì 15 ottobre" */
  LZ.giornoLungo = iso => { const d = iso && FT.data(String(iso).slice(0, 10)); return d ? fmtGiornoLungo.format(d) : ''; };
  /** Giorni lavorativi (lun-ven) da oggi alla data: 0 oggi, negativo se passata. */
  LZ.lavorativiA = function (iso) {
    const g = iso ? FT.giorniDa(String(iso).slice(0, 10)) : null;
    if (g == null) return null;
    if (g <= 0) return g;
    let n = 0;
    const d = new Date(); d.setHours(12, 0, 0, 0);
    for (let i = 0; i < g; i++) { d.setDate(d.getDate() + 1); const w = d.getDay(); if (w !== 0 && w !== 6) n++; }
    return n;
  };

  /** La cella "Taglia entro": rossa se passata, ambra se entro 2 giorni
      lavorativi, altrimenti testo normale. Sempre con la parola. */
  function quando(o, st) {
    if (st === 'tagliato') return `<span class="lz-q fatto"><b>tagliato</b> <small>${esc(FT.fmtOra(o.data_taglio_completato))}</small></span>`;
    const g = LZ.entro(o);
    if (!g) return '<span class="lz-q vicino"><b>da decidere</b></span>';
    const ge = LZ.giorniEntro(o);
    const wd = LZ.lavorativiA(g);
    let cls = '', sotto = '';
    if (ge != null && ge < 0) { cls = 'indietro'; sotto = `in ritardo ${-ge} gg`; }
    else if (wd != null && wd <= 2) { cls = 'vicino'; sotto = ge === 0 ? 'oggi' : ge === 1 ? 'domani' : `tra ${wd} gg lav.`; }
    else sotto = `tra ${ge} gg`;
    return `<span class="lz-q ${cls}" title="Taglia entro ${esc(LZ.giornoLungo(g))}"><b>${esc(LZ.giornoCorto(g))}</b> <small>${esc(sotto)}</small></span>`;
  }

  /** La consegna: grigia. La parola rossa solo se e' passata, o vicina
      (a rischio) quando il taglia entro non e' gia' rosso. */
  function consegna(o, st) {
    const g = LZ.giorno(o);
    if (!g) return '<span class="lz-cons">—</span>';
    const al = st !== 'tagliato' && LZ.allarme(o);
    if (al && al.k === 'rischio') {
      const gc = LZ.giorniA(o), ge = LZ.giorniEntro(o);
      const parola = gc < 0 ? 'passata' : ge != null && ge < 0 ? '' : 'a rischio';
      if (parola) return `<span class="lz-cons" title="${esc(al.s)}">${esc(LZ.giornoCorto(g))} <span class="lz-al rischio">${parola}</span></span>`;
    }
    return `<span class="lz-cons">${esc(LZ.giornoCorto(g))}</span>`;
  }

  /** Lo stato accanto al numero: solo se c'e' qualcosa da fare o che non va
      (ambra da controllare, rosso bloccato). Il passo normale lo dice il gruppo. */
  function stato(o) {
    const e = LZ.etichetta(o);
    if (e.c !== 'att' && e.c !== 'err') return '';
    const t = e.corto || e.t;
    return `<small class="lz-sot ${e.c}"${t !== e.t ? ` title="${esc(e.t)}"` : ''}>${esc(t)}</small>`;
  }

  /** Le lamiere ancora da tagliare, corte: "Inox 2 · 2,5 · 4   S235 12" (mm). */
  function lamiereTesto(o) {
    const per = new Map();
    for (const x of lamiereAperte(o)) {
      const m = String(x.materiale || '').replace(/_/g, ' ').trim().toLowerCase();
      const mat = m ? m.charAt(0).toUpperCase() + m.slice(1) : '?';
      if (!per.has(mat)) per.set(mat, []);
      const s = x.spessore != null && x.spessore !== '' && !isNaN(Number(x.spessore)) ? Number(x.spessore) : null;
      per.get(mat).push(s);
    }
    return [...per.entries()].map(([mat, sp]) => `${mat}: ${sp.sort((a, b) => (a == null) - (b == null) || a - b)
      .map(s => (s == null ? '?' : FT.numero(s, s % 1 ? 1 : 0))).join(' · ')}`).join('   ');
  }

  /** Ore di laser dell'ordine (stesso conto della riga del carico). */
  function ore(o, st) {
    if (st === 'tagliato' || st === 'smistare') return '';
    const d = LZ.sat.oreOrdine(o);
    if (!d || d.min == null) return '<span class="lz-ore-n" title="Durata non nota">—</span>';
    return esc((d.min / 60).toLocaleString('it-IT', { minimumFractionDigits: 1, maximumFractionDigits: 1 }));
  }

  // ── I gruppi per lamiera ──────────────────────────────────────────────
  /** Le lamiere ancora da tagliare di un ordine: [{chiave, materiale, spessore, n_pezzi, n_codici}]. */
  function lamiereAperte(o) {
    const lm = LZ.lamiere(o);
    const aperte = lm.l.filter(x => !lm.tg[x.chiave]);
    return aperte.length ? aperte : lm.l;
  }
  const passaFiltri = o => (!S.filtroCliente || (o.cliente || '') === S.filtroCliente)
    && (!S.filtroStato || LZ.statoLaser(o) === S.filtroStato || LZ.statoLaser(o) === 'tagliato' && S.filtroStato === 'tagliato');

  /** Le lamiere da tagliare, ognuna con gli ordini che la usano (vista
      "Per lamiera"): [{k, t, lam, righe:[{o, lam}], primo, pezzi}]. */
  L.gruppiLamiera = function () {
    const perLam = new Map();
    for (const o of LZ.ordiniLaser().filter(passaFiltri)) {
      const st = LZ.statoLaser(o);
      if (st === 'smistare' || st === 'tagliato' || st === 'escluso') continue;
      for (const x of lamiereAperte(o)) {
        if (!perLam.has(x.chiave)) perLam.set(x.chiave, { k: x.chiave, t: LZ.nomeLamiera(x), lam: x, righe: [] });
        perLam.get(x.chiave).righe.push({ o, lam: x });
      }
    }
    const chiave = o => (LZ.entro(o) || '9999') + (LZ.giorno(o) || '9999') + (o.cliente || '') + o.id;
    const lams = [...perLam.values()];
    for (const g of lams) {
      g.righe.sort((a, b) => chiave(a.o).localeCompare(chiave(b.o)));
      g.primo = LZ.entro(g.righe[0].o);
      g.pezzi = g.righe.reduce((s, r) => s + (Number(r.lam.n_pezzi) || 0), 0);
    }
    // prima la lamiera che scade prima; a parita', piu' ordini insieme
    return lams.sort((a, b) => (a.primo || '9999').localeCompare(b.primo || '9999') || b.righe.length - a.righe.length || a.t.localeCompare(b.t));
  };

  /** I gruppi dell'elenco, per cosa c'e' da fare: [{k, t, nota, righe:[{o, prima}]}]. */
  L.gruppi = function () {
    const ordini = LZ.ordiniLaser().filter(passaFiltri);
    const out = [];
    const smi = ordini.filter(o => LZ.statoLaser(o) === 'smistare');
    if (smi.length) out.push({ k: 'smistare', t: window.FTStati.laser.da_smistare, nota: smi.length === 1 ? 'decidi se passa dal laser' : 'decidi se passano dal laser', righe: smi.map(o => ({ o })) });
    const coda = ordini.filter(o => LZ.statoLaser(o) === 'coda');
    if (coda.length) out.push({ k: 'coda', t: 'Da mandare a Lantek', nota: 'controlla i disegni, poi Manda a Lantek', righe: coda.map(o => ({ o })) });
    const inl = ordini.filter(o => LZ.statoLaser(o) === 'in_lantek');
    if (inl.length) out.push({ k: 'in_lantek', t: 'In Lantek, da tagliare', nota: 'quando hai tagliato, segna Taglio fatto', righe: inl.map(o => ({ o })) });
    // gia' tagliati ma con un problema dall'officina aperto (pezzo da rifare...): in cima
    const tagliati = ordini.filter(o => LZ.statoLaser(o) === 'tagliato');
    // (e quelli che non passano dal laser: ci sono solo per il problema)
    const segn = ordini.filter(o => ['tagliato', 'escluso'].includes(LZ.statoLaser(o)) && LZ.segn && LZ.segn.haAperte(o.id));
    if (segn.length) out.unshift({ k: 'segnalati', t: 'Segnalati dall’officina', nota: segn.every(o => LZ.statoLaser(o) === 'tagliato') ? (segn.length === 1 ? 'già tagliato' : 'già tagliati') : 'già tagliati o fuori dal laser', righe: segn.map(o => ({ o })) });
    const tg = tagliati.filter(o => !segn.includes(o));
    if (tg.length) out.push({ k: 'tagliato', t: 'Tagliati nelle ultime 24 ore', righe: tg.map(o => ({ o })) });
    // la prima volta che un ordine compare e' la riga "vera"
    const visti = new Set();
    for (const g of out) for (const r of g.righe) { r.prima = !visti.has(r.o.id); visti.add(r.o.id); }
    return out;
  };
  /** Gli ordini nell'ordine in cui si vedono (una volta sola): frecce, ricerca. */
  L.visti = () => L.gruppi().flatMap(g => g.righe.filter(r => r.prima).map(r => r.o));

  function riga(r, g) {
    const o = r.o;
    const st = LZ.statoLaser(o);
    const on = S.aperto === o.id;
    const sp = L.spuntabile(o);
    const cons = LZ.giorno(o), te = LZ.te(o), en = LZ.entro(o);
    const tit = `${LZ.numero(o)}, ${o.cliente || ''}: taglia entro ${en ? FT.fmtData(en) : 'da decidere'}${te && te.motivo ? ' (' + te.motivo + ')' : ''}, consegna ${cons ? FT.fmtData(cons) : '—'}`;
    const id = esc(o.id);
    const pezzi = (S.riep[o.id] || {}).n_pezzi;
    return `<tr class="lz-r${on ? ' on' : ''}${st === 'tagliato' ? ' spento' : ''}" tabindex="0" data-id="${id}" data-fuoco="r-${id}"
        ${on ? 'aria-current="true"' : ''} title="${esc(tit)}">
      <td class="c-sp">${sp ? `<input type="checkbox" data-sp="${id}" ${S.spuntati.has(o.id) ? 'checked' : ''}
        aria-label="Seleziona ${esc(o.cliente || '')} ${esc(LZ.numero(o))}" tabindex="-1">` : ''}</td>
      <td class="c-num"><div class="lz-cli"><span class="lz-num" title="${esc(LZ.numero(o))}">${esc(LZ.numeroBreve(o))}</span>${stato(o)}${LZ.pacchi ? LZ.pacchi.nota(o.id) : ''}${procura(o)}${LZ.segn ? LZ.segn.parolaLista(o) : ''}${LZ.quad ? LZ.quad.parolaLista(o) : ''}</div></td>
      <td class="c-cli"><div class="lz-cli"><b>${esc(o.cliente || 'Cliente non indicato')}</b></div></td>
      <td class="c-lam" title="${esc(lamiereTesto(o))}">${esc(st === 'tagliato' ? '' : lamiereTesto(o))}</td>
      <td class="c-pz r">${pezzi != null ? esc(FT.numero(pezzi)) : '—'}</td>
      <td class="c-ore r">${ore(o, st)}</td>
      <td class="c-entro">${quando(o, st)}</td>
      <td class="c-cons">${consegna(o, st)}</td>
    </tr>`;
  }

  function testaGruppo(g) {
    const n = g.righe.length;
    const sotto = g.nota ? `${LZ.plur(n, 'ordine', 'ordini')} · ${g.nota}` : LZ.plur(n, 'ordine', 'ordini');
    return `<tr class="lz-gruppo" data-gruppo="${esc(g.k)}">
      <td colspan="8"><span class="lz-g-tit"><b>${esc(g.t)}</b><span>${esc(sotto)}</span></span></td>
    </tr>`;
  }

  /** Vista "Per lamiera": una riga per lamiera, dentro gli ordini che la
      usano (clic: apre l'ordine). "Seleziona i N da mandare" per il pacco. */
  function rigaLamiera(g) {
    const daMandare = g.righe.filter(r => L.spuntabile(r.o) && !L.inPacco(r.o)).map(r => r.o.id);
    const tutti = daMandare.length && daMandare.every(id => S.spuntati.has(id));
    const sel = daMandare.length ? `<button class="lz-link" type="button" data-az="sel-lam" data-lam="${esc(g.k)}">${tutti ? 'Togli la selezione' : `Seleziona ${daMandare.length === 1 ? 'l’ordine' : `i ${daMandare.length}`} da mandare`}</button>` : '';
    const ordini = g.righe.map(r => `<button class="lz-lam-o${S.aperto === r.o.id ? ' on' : ''}" type="button" data-apri="${esc(r.o.id)}"
        title="${esc(`${LZ.numero(r.o)}, ${r.o.cliente || ''}`)}"><span class="lz-num">${esc(LZ.numeroBreve(r.o))}</span><small>${esc(FT.numero(r.lam.n_pezzi || 0))}</small></button>`).join('');
    const ge = g.primo ? FT.giorniDa(g.primo) : null;
    const primo = g.primo ? `<span class="lz-q${ge != null && ge < 0 ? ' indietro' : ''}"><b>${esc(LZ.giornoCorto(g.primo))}</b>${ge != null && ge < 0 ? ` <small>in ritardo ${-ge} gg</small>` : ''}</span>` : '';
    return `<tr class="lz-lam" data-gruppo="${esc(g.k)}">
      <td class="c-lamn"><b>${esc(g.t)}</b></td>
      <td class="c-lamo"><div class="lz-lam-ordini">${ordini}</div></td>
      <td class="c-pz r">${esc(FT.numero(g.pezzi))}</td>
      <td class="c-entro">${primo}</td>
      <td class="c-lamsel">${sel}</td>
    </tr>`;
  }

  /** Pezzi da procurare non ancora arrivati: la parola, rossa se in ritardo. */
  function procura(o) {
    const p = (S.riep[o.id] || {}).procura;
    if (!p || !p.n) return '';
    return `<small class="lz-pc-mini${p.n_scaduti ? ' rit' : ''}" title="${esc(`Da procurare: ${p.n}${p.n_scaduti ? `, ${p.n_scaduti} in ritardo` : ''}`)}">${p.n} da procurare</small>`;
  }

  /** I numeri del menu a sinistra (cornice). */
  function menu(attivi) {
    const set = (k, n) => { const el = document.getElementById('lzm-' + k); if (el) el.textContent = n ? String(n) : ''; };
    set('tutti', attivi.length);
    set('coda', attivi.filter(o => LZ.statoLaser(o) === 'coda').length);
    set('in_lantek', attivi.filter(o => LZ.statoLaser(o) === 'in_lantek').length);
    set('controllo', attivi.filter(o => L.spuntabile(o)).reduce((s, o) => s + ((S.riep[o.id] || {}).n_controllare || 0), 0));
    set('correggere', attivi.reduce((s, o) => s + ((S.riep[o.id] || {}).n_correggere || 0), 0));
    set('lamiere', L.gruppiLamiera().length);
    const attivo = S.vista === 'lamiere' ? 'lamiere' : ['coda', 'in_lantek'].includes(S.filtroStato) ? S.filtroStato : 'tutti';
    document.querySelectorAll('#lz-menu .ft-tab[data-lz-vai]').forEach(b => b.classList.toggle('active', b.dataset.lzVai === attivo));
  }

  /** "Cosa fare adesso": UNA riga corta. Il prossimo ordine e cosa farne; Apri (Invio). */
  function adesso() {
    if (S.primoCarico) return '<div class="lz-adesso"><span class="lz-skel" style="height:12px;width:50%"></span></div>';
    const p = L.prossimo();
    if (!p) return `<div class="lz-adesso"><span>Niente da tagliare. Quando l'ufficio carica un ordine, compare qui.</span></div>`;
    const st = LZ.statoLaser(p);
    const chi = `il <b>${esc(LZ.numeroBreve(p))}</b>${p.cliente ? ` di ${esc(p.cliente)}` : ''}`;
    const en = LZ.entro(p), ge = LZ.giorniEntro(p);
    const giorno = en ? esc(LZ.giornoCorto(en)) : '';
    const quando = !en ? '' : ge < 0 ? `: andava tagliato entro <span class="err">${giorno}</span>` : `: va tagliato entro ${giorno}`;
    const frase = st === 'smistare' ? `decidi se ${chi} passa dal laser.`
      : st === 'coda' ? `manda a Lantek ${chi}${quando}.`
      : `taglia ${chi}${quando}.`;
    return `<div class="lz-adesso" aria-label="Cosa fare adesso"><span class="lz-ad-t">Adesso</span><span class="lz-ad-f">${frase}</span>
      ${S.aperto === p.id ? '' : `<button class="lz-btn pri" type="button" data-az="apri-prossimo" data-id="${esc(p.id)}" data-fuoco="apri-prossimo">Apri ${esc(LZ.numeroBreve(p))}${LZ.tasto('Invio')}</button>`}</div>`;
  }

  /** Il filtro per cliente (lo stato lo sceglie il menu a sinistra). */
  function filtri() {
    const clienti = [...new Set(LZ.ordiniLaser().map(o => o.cliente || '').filter(Boolean))].sort((a, b) => a.localeCompare(b, 'it'));
    if (S.filtroCliente && !clienti.includes(S.filtroCliente)) S.filtroCliente = '';
    const opz = (v, t, cur) => `<option value="${esc(v)}"${v === cur ? ' selected' : ''}>${esc(t)}</option>`;
    return `<div class="lz-filtri">
      <label>Cliente <select data-filtro="filtroCliente" aria-label="Filtra per cliente">${opz('', 'Tutti', S.filtroCliente)}${clienti.map(c => opz(c, c, S.filtroCliente)).join('')}</select></label>
    </div>`;
  }

  L.disegna = function () {
    if (!costruita) costruisci();
    const tutti = LZ.ordiniLaser();
    // le caselle restano solo sugli ordini ancora spuntabili
    for (const id of [...S.spuntati]) { const o = LZ.trova(id); if (!o || !L.spuntabile(o)) S.spuntati.delete(id); }
    const main = document.querySelector('.lz-main');
    if (main) main.classList.toggle('con-ordine', !!(S.aperto || S.pacco));

    const attivi = tutti.filter(o => LZ.statoLaser(o) !== 'tagliato');
    const lamiere = S.vista === 'lamiere';
    const gruppi = lamiere ? [] : L.gruppi();
    const glam = lamiere ? L.gruppiLamiera() : [];
    const nDaCtl = attivi.filter(o => L.spuntabile(o) && (S.riep[o.id] || {}).n_controllare).length;
    const selCtl = nDaCtl && !S.spuntati.size ? ` <button class="lz-link" type="button" data-az="sel-ctl">Seleziona ${nDaCtl === 1 ? 'quello' : `i ${nDaCtl}`} con disegni da controllare</button>` : '';
    // UNA frase: quanti ordini, il taglio in ritardo e il carico dei prossimi
    // giorni, coi conti del server uguali all'ufficio (laser-saturazione.js)
    const frase = lamiere
      ? `${LZ.plur(glam.length, 'lamiera', 'lamiere')} da tagliare, con gli ordini che le usano: il primo è quello che scade prima.`
      : `${LZ.plur(attivi.length, 'ordine', 'ordini')} da tagliare.${LZ.sat.ritardoHtml()} ${LZ.sat.html()}`;
    LZ.metti(document.getElementById('lz-l-testa'), `<div class="lz-l-tit"><h1>${lamiere ? 'Per lamiera' : 'Ordini al laser'}</h1>
        <p class="lz-riass">${frase}${lamiere ? '' : selCtl}</p>${filtri()}</div>
      ${lamiere ? '' : adesso()}${promemoria(attivi)}`);
    menu(attivi);

    const righe = document.getElementById('lz-l-righe');
    let html = '';
    const testa = `<thead><tr><th class="c-sp"><span class="lz-nascosto">Seleziona</span></th><th class="c-num">Ordine</th><th class="c-cli">Cliente</th>
      <th class="c-lam">Lamiere (mm)</th><th class="c-pz r">Pezzi</th><th class="c-ore r">Ore laser</th><th class="c-entro">Taglia entro</th><th class="c-cons">Consegna</th></tr></thead>`;
    if (S.primoCarico) {
      html = `<table class="lz-tabella">${testa}<tbody>${Array.from({ length: 7 }, () => `<tr class="lz-r" aria-hidden="true"><td></td>
        <td><div class="lz-skel" style="height:12px;width:40px"></div></td><td><div class="lz-skel" style="height:12px;width:70%"></div></td>
        <td><div class="lz-skel" style="height:12px;width:60%"></div></td><td></td><td></td><td><div class="lz-skel" style="height:12px;width:56px"></div></td><td></td></tr>`).join('')}</tbody></table>`;
    } else if (!tutti.length) {
      html = `<div class="lz-vuoto-lista"><b>Nessun ordine al laser</b>Quando l'ufficio carica un ordine, compare qui.</div>`;
    } else if (lamiere) {
      html = glam.length ? `<table class="lz-tabella lz-tab-lam"><thead><tr><th class="c-lamn">Lamiera</th><th class="c-lamo">Ordini · pezzi</th>
          <th class="c-pz r">Pezzi</th><th class="c-entro">Primo taglia entro</th><th class="c-lamsel"><span class="lz-nascosto">Seleziona</span></th></tr></thead>
          <tbody>${glam.map(rigaLamiera).join('')}</tbody></table>`
        : `<div class="lz-vuoto-lista"><b>Nessuna lamiera da tagliare</b>${S.filtroCliente ? '<button class="lz-link" type="button" data-az="via-filtri">Togli i filtri</button>' : ''}</div>`;
    } else if (!gruppi.length) {
      html = `<div class="lz-vuoto-lista"><b>Nessun ordine con questi filtri</b><button class="lz-link" type="button" data-az="via-filtri">Togli i filtri</button></div>`;
    } else {
      // in cima i pacchi (laser-pacchi.js), poi gli ordini per cosa c'e' da fare
      html = `<table class="lz-tabella">${testa}<tbody>${LZ.pacchi ? LZ.pacchi.righeLista() : ''}${gruppi.map(g => testaGruppo(g) + g.righe.map(r => riga(r, g)).join('')).join('')}</tbody></table>`;
    }
    LZ.metti(righe, html, righe);
    disegnaAzione();
  };

  /** Promemoria fisso: i codici "da correggere in Lantek" non si dimenticano
      (clic: apre il primo ordine che ne ha). Resta finche' non sono risolti. */
  function promemoria(attivi) {
    const conta = k => attivi.reduce((s, o) => s + ((S.riep[o.id] || {})[k] || 0), 0);
    const nDa = conta('n_correggere'), nOk = conta('n_corretti');
    // i problemi nuovi dall'officina (laser-segnala.js), anche sugli ordini gia' tagliati
    const nuove = LZ.segn ? LZ.ordiniLaser().reduce((s, o) => s + LZ.segn.nuove(o.id).length, 0) : 0;
    const grave = nuove && LZ.ordiniLaser().some(o => LZ.segn.nuove(o.id).some(x => x.grave));
    if (!nDa && !nOk && !nuove) return '';
    return `<div class="lz-promemoria">${nuove ? `<button class="lz-ricorda${grave ? '' : ' att'}" type="button" data-az="vai-segnalati" data-fuoco="vai-segnalati"
        title="Problemi segnalati dal tablet dell'officina: aprili e segnali Visto o Risolto"><span>${nuove === 1 ? '1 problema segnalato' : `${nuove} problemi segnalati`} dall’officina</span></button>` : ''}${nDa ? `<button class="lz-ricorda" type="button" data-az="vai-correggere" data-fuoco="vai-correggere"
        title="Codici tenuti fuori da Lantek perché diversi dall'ordine: correggili in Lantek"><span>${LZ.plur(nDa, 'codice', 'codici')} da correggere in Lantek</span></button>` : ''}
      ${nOk ? `<button class="lz-ricorda pronti" type="button" data-az="vai-corretti" data-fuoco="vai-corretti"
        title="Corretti in Lantek: si possono mandare"><span>${LZ.plur(nOk, 'codice corretto', 'codici corretti')}: da mandare</span></button>` : ''}</div>`;
  }
  /** Il primo ordine (nell'ordine dell'elenco) con codici da correggere / corretti. */
  L.apriCorrezione = function (k) {
    const o = LZ.ordiniVisti().find(x => LZ.statoLaser(x) !== 'tagliato' && (S.riep[x.id] || {})[k]);
    if (o) L.apri(o.id);
    else FT.toast(k === 'n_correggere' ? 'Nessun codice da correggere in Lantek' : 'Nessun codice corretto da mandare', '');
  };

  /** Il primo ordine (nell'ordine dell'elenco) con un problema nuovo dall'officina. */
  L.apriSegnalato = function () {
    const o = LZ.ordiniVisti().find(x => LZ.segn && LZ.segn.nuove(x.id).length);
    if (o) L.apri(o.id); else FT.toast('Nessun problema nuovo dall’officina', '');
  };

  function disegnaAzione() {
    const box = document.getElementById('lz-l-az');
    const sel = [...S.spuntati].map(LZ.trova).filter(Boolean);
    if (!sel.length) { LZ.metti(box, ''); return; }
    const nCtl = sel.reduce((s, o) => s + ((S.riep[o.id] || {}).n_controllare || 0), 0);
    const pronti = sel.filter(o => (S.riep[o.id] || {}).pronto);
    const altri = sel.length - pronti.length;
    // il pacco (laser-pacchi.js) e' il modo di lavorare: "Crea pacco" e' il
    // pulsante principale; gli ordini gia' in un pacco aperto non rientrano
    const liberi = sel.filter(o => !(LZ.pacchi && LZ.pacchi.diOrdine(o.id)));
    const inPacco = sel.length - liberi.length;
    LZ.metti(box, `<div class="lz-azione">
      <div class="q"><span>${LZ.plur(sel.length, 'ordine selezionato', 'ordini selezionati')}</span>
        <button class="lz-link" type="button" data-az="sel-via">Togli la selezione</button></div>
      <button class="lz-btn${liberi.length ? ' pri' : ''}" type="button" data-az="crea-pacco" data-fuoco="crea-pacco" ${liberi.length ? '' : 'disabled'}>
        <span>Crea pacco${liberi.length ? ` (${LZ.plur(liberi.length, 'ordine', 'ordini')})` : ''}</span>${liberi.length ? LZ.tasto('P') : ''}</button>
      <button class="lz-btn" type="button" data-az="ctl-multi" ${nCtl ? '' : 'disabled'}>
        <span>Controlla ${LZ.plur(nCtl, 'disegno', 'disegni')}</span>${nCtl ? LZ.tasto('C') : ''}</button>
      <button class="lz-btn" type="button" data-az="lantek-multi" ${pronti.length ? '' : 'disabled'}>
        <span>Manda a Lantek${pronti.length ? ` (${LZ.plur(pronti.length, 'ordine', 'ordini')})` : ''}</span>${pronti.length ? LZ.tasto('L') : ''}</button>
      ${inPacco ? `<span class="nota">${inPacco === 1 ? 'Uno è già' : `${inPacco} sono già`} in un pacco aperto: non entra${inPacco === 1 ? '' : 'no'} nel pacco nuovo.</span>` : ''}
      ${altri && pronti.length ? `<span class="nota">${altri === 1 ? 'Un altro ha' : `Gli altri ${altri} hanno`} ancora qualcosa da fare prima di Lantek.</span>`
        : !pronti.length ? '<span class="nota">Per mandarli a Lantek, prima controlla i disegni e sistema i blocchi.</span>' : ''}
    </div>`);
  }

  function clic(e) {
    const az = e.target.closest('[data-az]');
    if (az) {
      const a = az.dataset.az;
      if (a === 'sel-ctl') L.selezionaDaControllare();
      else if (a === 'sel-via') { S.spuntati.clear(); LZ.ridisegna(); }
      else if (a === 'sel-lam') L.selezionaLamiera(az.dataset.lam);
      else if (a === 'ctl-multi') L.controllaSpuntati();
      else if (a === 'crea-pacco') L.creaPacco();
      else if (a === 'lantek-multi') L.mandaSpuntati();
      else if (a === 'vai-correggere') L.apriCorrezione('n_correggere');
      else if (a === 'vai-corretti') L.apriCorrezione('n_corretti');
      else if (a === 'vai-segnalati') L.apriSegnalato();
      else if (a === 'via-filtri') { S.filtroCliente = ''; S.filtroStato = ''; LZ.ridisegna(); }
      else if (a === 'apri-prossimo') { const id = az.dataset.id; if (id) L.apri(id); }
      return;
    }
    if (e.target.closest('input[data-sp], select, label')) return;
    const ap = e.target.closest('[data-apri]');
    if (ap) { L.apri(ap.dataset.apri); return; }
    const rp = e.target.closest('.lz-r[data-pacco]');
    if (rp) { LZ.pacchi.apri(rp.dataset.pacco); return; }
    const r = e.target.closest('.lz-r');
    const id = r && (r.dataset.id || r.dataset.rif);
    if (id) L.apri(id);
  }

  /** "Seleziona i N da mandare" di una lamiera: e' il pacco di oggi, finche'
      il pacco non e' un oggetto suo (fase 2). Di nuovo: toglie la selezione. */
  L.selezionaLamiera = function (k) {
    const g = L.gruppiLamiera().find(x => x.k === k);
    if (!g) return;
    const ids = g.righe.filter(r => L.spuntabile(r.o) && !L.inPacco(r.o)).map(r => r.o.id);
    const tutti = ids.every(id => S.spuntati.has(id));
    ids.forEach(id => (tutti ? S.spuntati.delete(id) : S.spuntati.add(id)));
    LZ.ridisegna();
  };

  /** Il menu della cornice: filtra per stato, porta al controllo, alle correzioni. */
  L.vai = function (k) {
    const righe = document.getElementById('lz-l-righe');
    if (k === 'tutti') { S.filtroStato = ''; S.vista = ''; LZ.ridisegna(); if (righe) righe.scrollTo({ top: 0 }); }
    else if (k === 'lamiere') { S.filtroStato = ''; S.vista = 'lamiere'; LZ.ridisegna(); if (righe) righe.scrollTo({ top: 0 }); }
    else if (k === 'coda' || k === 'in_lantek') {
      const n = LZ.ordiniLaser().filter(o => LZ.statoLaser(o) === k).length;
      if (!n) { FT.toast(k === 'coda' ? 'Nessun ordine da mandare a Lantek' : 'Nessun ordine in Lantek da tagliare', ''); return; }
      S.filtroStato = k;
      S.vista = '';
      LZ.ridisegna();
      if (righe) righe.scrollTo({ top: 0 });
      const primo = document.querySelector('#lz-l-righe .lz-r[data-id]');
      if (primo) primo.focus({ preventScroll: true });
    } else if (k === 'controllo') {
      const ids = LZ.ordiniVisti().filter(o => L.spuntabile(o) && (S.riep[o.id] || {}).n_controllare).map(o => o.id);
      if (ids.length) LZ.controllo.inizia(ids); else FT.toast('Nessun disegno da controllare', '');
    } else if (k === 'correggere') L.apriCorrezione('n_correggere');
  };
  document.addEventListener('click', e => {
    const b = e.target.closest('#lz-menu [data-lz-vai]');
    if (b) L.vai(b.dataset.lzVai);
  });

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
    const sel = LZ.ordiniVisti().filter(o => S.spuntati.has(o.id)).map(o => o.id);
    return sel.length ? sel : (S.aperto ? [S.aperto] : []);
  };
  L.controllaSpuntati = function () { LZ.controllo.inizia(L.bersaglio()); };
  /** "Crea pacco" (P): gli ordini spuntati, nell'ordine dell'elenco. */
  L.creaPacco = function () {
    const ids = LZ.ordiniVisti().filter(o => S.spuntati.has(o.id)).map(o => o.id);
    if (!ids.length || !LZ.pacchi) return;
    S.vista = '';            // il pacco nuovo si vede in cima all'elenco degli ordini
    LZ.pacchi.crea(ids);
  };
  /** Tutti i selezionati nella conferma: chi non parte dice perche'. */
  L.mandaSpuntati = function () {
    const ids = L.bersaglio();
    if (ids.some(id => (S.riep[id] || {}).pronto)) LZ.lantek.apri(ids);
  };

  /** Apre un ordine nel pannello a destra (solo su richiesta: clic, frecce, Apri). */
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
  /** Chiude il pannello dell'ordine: la tabella torna a tutta larghezza
      (o al pacco, se l'ordine era stato aperto dal pacco). */
  L.chiudi = function () {
    if (!S.aperto) return LZ.pacchi ? LZ.pacchi.chiudi() : false;
    const id = S.aperto;
    S.aperto = null;
    LZ.ridisegna();
    const r = document.querySelector(`.lz-r[data-id="${CSS.escape(id)}"]`);
    if (r) r.focus({ preventScroll: false });
    return true;
  };
  /** Frecce: nell'ordine in cui le righe si vedono (gruppi). */
  L.sposta = function (passo) {
    const l = LZ.ordiniVisti();
    if (!l.length) return;
    const i = l.findIndex(o => o.id === S.aperto);
    const j = i < 0 ? (passo > 0 ? 0 : l.length - 1) : Math.max(0, Math.min(l.length - 1, i + passo));
    L.apri(l[j].id);
    const r = document.querySelector(`.lz-r[data-id="${CSS.escape(l[j].id)}"]`);
    if (r) r.focus({ preventScroll: true });
  };

  /** Il prossimo da fare (riga "Adesso", Invio senza ordine aperto): prima
      la consegna a rischio, poi il taglia entro passato, poi l'elenco. */
  L.prossimo = function () {
    const l = LZ.ordiniVisti().filter(o => LZ.statoLaser(o) !== 'tagliato');
    const pr = o => { const a = LZ.allarme(o); return a && a.k === 'rischio' ? 0 : a ? 1 : 2; };
    return l.slice().sort((a, b) => pr(a) - pr(b))[0] || null;
  };

  // La ricerca della barra (Ctrl K): numero, cliente, codice
  if (window.FTC) {
    window.FTC.cerca = function (q) {
      const t = q.toLowerCase();
      const W = window.FTStati.laser;
      // i pacchi: "pacco 42", "42"
      const nPacco = (t.match(/^\s*(?:pacco\s*)?(\d{1,5})\s*$/) || [])[1];
      const pacchi = (S.pacchi || []).filter(p => (nPacco && String(p.numero) === nPacco) || (t.trim().length >= 3 && ('pacco ' + p.numero).startsWith(t.trim())))
        .map(p => ({ t: p.nome, s: LZ.plur(p.ordini.length, 'ordine', 'ordini'), st: `<span class="lz-sot">${esc(p.parola)}</span>`,
                     fn: () => { S.filtroCliente = ''; S.filtroStato = ''; LZ.pacchi.apri(p.id); } }));
      return pacchi.concat(LZ.ordiniLaser().filter(o => [LZ.numero(o), o.numero_ordine, o.cliente].some(x => String(x || '').toLowerCase().includes(t)))
        .map(o => ({ t: `${LZ.numeroBreve(o)}  ${o.cliente || ''}`, s: `${LZ.numero(o)}, consegna ${LZ.giorno(o) ? FT.fmtData(LZ.giorno(o)) : '—'}`,
                     st: `<span class="lz-sot">${esc({ smistare: W.da_smistare, coda: W.da_importare, in_lantek: W.in_lantek, tagliato: W.tagliato }[LZ.statoLaser(o)] || '')}</span>`,
                     fn: () => { S.filtroCliente = ''; S.filtroStato = ''; L.apri(o.id); } })));
    };
  }
})();
