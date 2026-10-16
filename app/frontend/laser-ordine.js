/* Pagina laser — l'ordine aperto, a destra.
   In alto chi e' e quanto pesa; poi IL PROSSIMO PASSO, con un solo pulsante
   principale (Invio); sotto, in parole semplici, cosa lo blocca; poi le
   schede Pezzi / Disegni / PDF ordine. Le cose che servono di rado stanno
   nel menu "⋯" (es. "L'ho gia' messo in Lantek a mano"). */
(function () {
  'use strict';
  const LZ = window.LZ;
  const S = LZ.s;
  const esc = LZ.esc;
  const O = LZ.ordine = {};

  // ── Dati ────────────────────────────────────────────────────────────
  O.carica = async function (id, fresco) {
    const prima = S.dett[id] || {};
    S.dett[id] = { ...prima, carica: true };
    if (S.aperto === id) O.disegna();
    const r = await LZ.get(`/api/orders/${encodeURIComponent(id)}/laser${fresco ? '?fresco=1' : ''}`);
    S.dett[id] = r.ok ? { dati: r.d, t: Date.now(), carica: false, errore: null }
                      : { ...prima, carica: false, errore: (r.d && r.d.error) || 'Il server non ha risposto', t: Date.now() };
    if (r.ok) {
      // l'elenco vede subito lo stesso stato
      const { pezzi, altri, esclusi, invio, differenze, pdf, usa_lantek, procura_voci, ...rid } = r.d;   // eslint-disable-line no-unused-vars
      LZ.avvisaCorretti(id, S.riep[id], rid);
      S.riep[id] = rid;
    }
    LZ.ridisegna();
    return S.dett[id];
  };

  // ── Il prossimo passo ───────────────────────────────────────────────
  /** {tono, ico, t, s, bottoni: [{t, az, pri, tasto}]} */
  O.passo = function (o) {
    const st = LZ.statoLaser(o);
    const D = S.dett[o.id] || {};
    const info = D.dati || S.riep[o.id];
    if (st === 'smistare') {
      return { tono: '', ico: 'split', t: 'Passa dal laser?', s: 'Se no, va subito in officina.',
               bottoni: [{ t: 'No', az: 'smista-no' }, { t: 'Sì, va tagliato', az: 'smista-si', pri: true, tasto: 'Invio' }] };
    }
    if (st === 'tagliato') {
      return { tono: '', ico: 'circle-check', t: window.FTStati.laser.tagliato,
               s: `Segnato ${esc(FT.fmtRelativo(o.data_taglio_completato))}. Se è uno sbaglio, annullalo.`,
               bottoni: [{ t: 'Annulla taglio', az: 'taglio-annulla' }] };
    }
    if (st === 'in_lantek') {
      // un codice corretto in Lantek dopo che l'ordine e' stato segnato: va mandato
      const corretti = ((info || {}).correzioni || []).filter(c => c.stato === 'corretto').map(c => c.codice);
      if (corretti.length) {
        return { tono: 'pronto', ico: 'send', t: `Manda a Lantek: ${LZ.plur(corretti.length, 'codice', 'codici')}`,
                 s: `${esc(elenco(corretti))} ${corretti.length === 1 ? 'è stato corretto' : 'sono stati corretti'} in Lantek: ora si può mandare.`,
                 bottoni: [{ t: 'Taglio fatto', az: 'taglio' }, { t: 'Manda a Lantek', az: 'lantek', pri: true, tasto: 'Invio' }] };
      }
      // quantita' cambiate dopo l'invio (pezzi per assieme, assiemi)
      // (e gli spessori/materiali tenuti fuori all'invio): si vedono anche qui
      const daCorr = ((info || {}).correzioni || []).filter(c => c.stato === 'da_correggere');
      if (daCorr.length) {
        const soloQ = daCorr.every(c => c.tipo === 'quantita');
        return { tono: 'err', ico: 'pencil-ruler', t: `${LZ.plur(daCorr.length, 'codice', 'codici')} da correggere in Lantek`,
                 s: soloQ ? 'Le quantità sono cambiate dopo l\'invio: correggi l\'ordine di produzione in Lantek come scritto sotto, poi segnalo.'
                   : 'Correggili in Lantek come scritto sotto: FerroTrack lo rilegge da solo e ti avvisa quando si possono mandare.',
                 bottoni: [{ t: 'Taglio fatto', az: 'taglio' }, { t: 'Ricontrolla', az: 'ricontrolla', pri: true, tasto: 'Invio' }] };
      }
      const lm = LZ.lamiere(o);
      if (lm.n > 1) {
        const manca = lm.n - lm.fatte;
        return { tono: '', ico: 'scissors', t: `Da tagliare: ${LZ.plur(manca, 'lamiera', 'lamiere')} su ${lm.n}`,
                 s: lm.fatte ? 'Segna le altre quando le hai tagliate, oppure tutto l\'ordine insieme.'
                   : 'Puoi segnare il taglio lamiera per lamiera (vista «Per lamiera»), oppure tutto l\'ordine insieme.',
                 bottoni: [{ t: 'Taglio fatto, tutto', az: 'taglio', pri: true, tasto: 'Invio' }] };
      }
      return { tono: '', ico: 'scissors', t: 'In Lantek: da tagliare', s: 'Quando hai tagliato tutto, segnalo qui.',
               bottoni: [{ t: 'Taglio fatto', az: 'taglio', pri: true, tasto: 'Invio' }] };
    }
    if (!info) return { tono: '', ico: 'loader-circle', t: 'Guardo cosa c\'è da fare…', s: 'Leggo i pezzi e cosa c\'è già in Lantek.', bottoni: [], carica: true };
    if (info.errore && !D.dati) {
      return { tono: 'err', ico: 'triangle-alert', t: 'Non riesco a leggere quest\'ordine', s: esc(info.errore),
               bottoni: [{ t: 'Riprova', az: 'ricontrolla', pri: true, tasto: 'Invio' }] };
    }
    if (info.origine === 'nessuna') {
      return { tono: '', ico: 'file-text', t: 'Va messo in Lantek a mano',
               s: 'Quest\'ordine non ha l\'elenco dei pezzi: inseriscilo dal MES guardando il PDF, poi segnalo qui.',
               bottoni: [{ t: 'È in Lantek', az: 'importato', pri: true, tasto: 'Invio' }] };
    }
    if (info.n_abbinare) {
      return { tono: 'att', ico: 'link', t: `Abbina ${LZ.plur(info.n_abbinare, 'riga', 'righe')} dell'ordine`,
               s: 'Righe del PDF che in Lantek non ci sono col loro codice: dimmi cosa sono, me lo ricordo per le prossime.',
               bottoni: [{ t: 'Abbina', az: 'abbina', pri: true, tasto: 'Invio' }] };
    }
    if (info.n_controllare) {
      return { tono: 'att', ico: 'scan-eye', t: `Controlla ${LZ.plur(info.n_controllare, 'disegno', 'disegni')}`,
               s: 'Il motore non è sicuro di questi: uno alla volta, a tutto schermo.',
               bottoni: [{ t: 'Inizia il controllo', az: 'controlla', pri: true, tasto: 'Invio' }] };
    }
    const lt = info.lantek || {};
    if (lt.letto && !lt.disponibile) {
      return { tono: 'err', ico: 'unplug', t: 'Lantek non risponde',
               s: 'Senza Lantek non so cosa c\'è già: controlla che il PC di Lantek sia acceso, poi riprova.',
               bottoni: [{ t: 'Riprova', az: 'ricontrolla', pri: true, tasto: 'Invio' }] };
    }
    const bl = info.bloccati || [];
    if (bl.length) {
      // uno spessore proposto da confermare: Invio lo salva (non Ricontrolla)
      const prop = O.primaProposta(info);
      return { tono: 'err', ico: 'octagon-alert', t: `Bloccato: ${LZ.plur(bl.length, 'codice', 'codici')} da sistemare`,
               s: prop ? 'Guarda lo spessore proposto sul disegno e confermalo con Invio, oppure sceglilo tu.'
                 : 'Finché non li sistemi, l\'ordine non parte. Quando hai fatto, premi Ricontrolla.',
               bottoni: [{ t: 'Ricontrolla', az: 'ricontrolla', pri: !prop, tasto: prop ? null : 'Invio' }] };
    }
    const soloQta = (info.correzioni || []).filter(c => c.stato === 'da_correggere').every(c => c.tipo === 'quantita');
    if (info.n_correggere && !info.pronto && soloQta) {
      // quantita' cambiate dopo l'invio: si corregge l'ordine di produzione
      return { tono: 'err', ico: 'pencil-ruler', t: `${LZ.plur(info.n_correggere, 'codice', 'codici')} da correggere in Lantek`,
               s: 'Le quantità sono cambiate dopo l\'invio: correggi l\'ordine di produzione in Lantek come scritto sotto, poi segnalo con «Corretto in Lantek».',
               bottoni: [{ t: 'Ricontrolla', az: 'ricontrolla', pri: true, tasto: 'Invio' }] };
    }
    if (info.n_correggere && !info.pronto) {
      return { tono: 'err', ico: 'pencil-ruler', t: `${LZ.plur(info.n_correggere, 'codice', 'codici')} da correggere in Lantek`,
               s: 'Correggili in Lantek come scritto sotto: FerroTrack lo rilegge da solo e ti avvisa quando si possono mandare. Se invece va bene com\'è in Lantek, scegli «Va bene Lantek».',
               bottoni: [{ t: 'Ricontrolla', az: 'ricontrolla', pri: true, tasto: 'Invio' }] };
    }
    if (info.tutto_in_lantek) {
      return { tono: '', ico: 'circle-check', t: 'È già tutto in Lantek',
               s: 'Ogni codice ha già il suo ordine di produzione. Segnalo: passa fra quelli da tagliare.',
               bottoni: [{ t: 'Segna in Lantek', az: 'importato', pri: true, tasto: 'Invio' }] };
    }
    if (info.pronto) {
      const n = info.n_invio || 0;
      const corretti = (info.correzioni || []).filter(c => c.stato === 'corretto').map(c => c.codice);
      if (corretti.length) {
        return { tono: 'pronto', ico: 'send', t: `Manda a Lantek: ${LZ.plur(n, 'codice', 'codici')}`,
                 s: `${esc(elenco(corretti))} ${corretti.length === 1 ? 'è stato corretto' : 'sono stati corretti'} in Lantek: ora si può mandare.`
                   + (info.n_correggere ? ` ${LZ.plur(info.n_correggere, 'altro resta', 'altri restano')} fuori finché non ${info.n_correggere === 1 ? 'lo' : 'li'} correggi.` : ''),
                 bottoni: [{ t: 'Manda a Lantek', az: 'lantek', pri: true, tasto: 'Invio' }] };
      }
      return { tono: 'pronto', ico: 'send', t: window.FTStati.laser.da_importare,
               s: `${LZ.plur(n, 'codice', 'codici')}: creo i pezzi nuovi col disegno e gli ordini di produzione.`,
               bottoni: [{ t: 'Manda a Lantek', az: 'lantek', pri: true, tasto: 'Invio' }] };
    }
    return { tono: '', ico: 'circle-help', t: 'Niente da mandare a Lantek', s: 'Nessun codice da mandare: se l\'hai già messo a mano, segnalo dal menu ⋯.',
             bottoni: [{ t: 'Ricontrolla', az: 'ricontrolla', pri: true, tasto: 'Invio' }] };
  };

  const elenco = (xs, n = 4) => xs.slice(0, n).join(', ') + (xs.length > n ? ` e altri ${xs.length - n}` : '');

  /** Il primo pezzo bloccato con uno spessore proposto dal motore (articolo_id) o null. */
  O.primaProposta = function (info) {
    const b = ((info || {}).bloccati || []).find(x => x.campo === 'spessore' && x.articolo_id && x.proposta && x.proposta.spessore_mm);
    return b ? b.articolo_id : null;
  };

  /** Invio = il pulsante principale del prossimo passo (o lo spessore proposto da confermare). */
  O.invio = function () {
    const o = LZ.trova(S.aperto);
    if (!o) {
      const p = !S.aperto && LZ.lista.prossimo();
      if (p) { LZ.lista.apri(p.id); return true; }
      return false;
    }
    if (LZ.statoLaser(o) === 'coda') {
      const D = S.dett[o.id] || {};
      const aid = O.primaProposta(D.dati || S.riep[o.id]);
      const box = aid != null && document.querySelector(`#lz-dett .lz-dati[data-dati="${CSS.escape(String(aid))}"]`);
      if (box) { O.salvaDati(o.id, box); return true; }
    }
    const p = O.passo(o);
    const b = (p.bottoni || []).find(x => x.pri);
    if (!b) return false;
    O.esegui(b.az, o.id);
    return true;
  };

  // ── Disegno ─────────────────────────────────────────────────────────
  let costruitoPer = null;
  function scheletro(el, id) {
    el.innerHTML = `<div class="lz-ordine" data-ordine="${esc(id)}">
      <div id="lz-o-testa"></div><div id="lz-o-passo"></div>
      <div class="lz-o-corpo"><aside class="lz-o-pdf" id="lz-o-pdf" aria-label="PDF dell'ordine del cliente"></aside>
        <div class="lz-o-pezzi"><nav class="lz-schede" id="lz-o-schede" role="tablist" aria-label="Viste dell'ordine"></nav>
        <div class="lz-scheda" id="lz-o-scheda"></div></div></div></div>`;
    el._lzHtml = null;
    costruitoPer = id;
  }

  O.disegna = function () {
    const el = document.getElementById('lz-dett');
    if (!el) return;
    const o = S.aperto && LZ.trova(S.aperto);
    if (!o) {
      costruitoPer = null;
      el.classList.remove('con-pdf', 'accanto');
      LZ.metti(el, S.aperto && !S.primoCarico
        ? `<div class="lz-vuoto"><div><b>Quest'ordine non è più al laser</b><p>Scegline un altro a sinistra, o chiudi con Esc.</p></div></div>`
        : '');
      return;
    }
    if (costruitoPer !== o.id || !el.querySelector('#lz-o-testa')) scheletro(el, o.id);
    if (LZ.staScrivendo(el)) return;              // non si ridisegna sotto le dita
    const acc = O.accanto(o);
    if (acc && S.scheda === 'pdf') S.scheda = 'pezzi';
    el.classList.toggle('con-pdf', S.scheda === 'pdf');
    el.classList.toggle('accanto', !!acc);
    el.dataset.allarga = acc ? (S.allarga || '') : '';
    pdfAccanto(el, o, acc);
    LZ.metti(document.getElementById('lz-o-testa'), testa(o));
    O.segnaMeta();
    LZ.metti(document.getElementById('lz-o-passo'), passoHtml(o));
    LZ.metti(document.getElementById('lz-o-schede'), schede(o));
    LZ.metti(document.getElementById('lz-o-scheda'), scheda(o), el);
  };

  /* Niente aperto (16/10/2026): il pannello non c'e' e la tabella prende
     tutta la larghezza; "cosa fare adesso" sta nella riga sopra la tabella
     (laser-lista.js). Niente si apre da solo (Stefano, 14/10). */

  function testa(o) {
    const st = LZ.statoLaser(o);
    const D = S.dett[o.id] || {};
    const info = D.dati || S.riep[o.id] || {};
    const q = LZ.quando(o);
    const g = LZ.giorno(o);
    const d = LZ.sat.oreOrdine(o);
    let ore = '<b class="num">—</b>';
    if (d && d.min != null) ore = `<b class="num">${LZ.ore(d.min)}</b><small>${d.fonte === 'mano' ? 'scritte a mano' : 'tempi Lantek'}</small>`;
    else if (d && st !== 'tagliato') {
      ore = `<span class="lz-ore-mano"><input type="number" min="0" step="0.25" inputmode="decimal" data-durata="${esc(o.id)}"
        aria-label="Ore di laser scritte a mano" placeholder="ore"> h</span><small>senza disegni: scrivile tu</small>`;
    }
    const interno = o.numero_ordine_cliente && o.numero_ordine && o.numero_ordine !== o.numero_ordine_cliente ? o.numero_ordine : '';
    // "Taglia entro": calcolato dalle lavorazioni o scelto a mano (si cambia qui)
    const te = LZ.te(o) || (D.dati && D.dati.taglia_entro) || {};
    const ge = LZ.entro(o);
    const qe = LZ.quandoEntro(o);
    const ritE = qe.rit && st !== 'tagliato';
    const aMano = !!o.data_taglio_pianificata;
    // (il motivo del server vale se dice la stessa cosa della data: a mano o calcolata)
    const motivo = te.motivo && (te.fonte === 'a mano') === aMano ? te.motivo : (aMano ? 'scelta a mano' : '');
    const cambia = st === 'tagliato' ? '' : `<span class="lz-entro-mano">
        <input type="date" data-entro="${esc(o.id)}" data-fuoco="entro-${esc(o.id)}" value="${esc(ge || '')}" min="${esc(FT.oggiISO())}"
          aria-label="Cambia a mano la data entro cui tagliare" title="Cambia a mano la data entro cui tagliare">
        ${aMano ? `<button class="lz-link" type="button" data-az="entro-calcolo" data-fuoco="entro-calcolo">Torna al calcolo</button>` : ''}</span>`;
    const esterna = st === 'tagliato' ? '' : campoEsterna(o, te);
    const al = LZ.allarme(o);
    return `<div class="lz-o-testa">
      <div class="lz-o-tit"><h2><span class="lz-o-num">${esc(LZ.numeroBreve(o))}</span><span class="lz-o-cli" title="${esc(o.cliente || '')}">${esc(o.cliente || 'Cliente non indicato')}</span></h2>
        <div class="lz-o-sotto"><span>ordine del cliente <span class="mono">${esc(LZ.numero(o))}</span></span>${interno ? `<span class="mono" title="Numero interno">${esc(interno)}</span>` : ''}${LZ.chip(o)}${al ? `<span class="lz-al ${al.k === 'rischio' ? 'rischio' : 'indietro'}">${esc(al.t)}</span>` : ''}${LZ.procura.chip(o)}</div></div>
      <div class="lz-o-az"><button class="lz-btn ghost piccolo" type="button" data-az="chiudi" data-fuoco="chiudi-ordine" title="Chiudi l'ordine (Esc)">Chiudi</button><button class="lz-btn ghost lz-icona" type="button" data-az="menu" data-fuoco="menu-ordine" title="Altre azioni" aria-label="Altre azioni">${LZ.ico('ellipsis')}</button></div>
      <div class="lz-fatti">
        <div class="lz-entro"><span>Taglia entro${aMano ? ' <em>(a mano)</em>' : ''}</span><b class="num${ritE ? ' rit' : ''}">${ge ? FT.fmtData(ge) : '—'}</b>
          <small class="${ritE ? 'rit' : ''}">${st === 'tagliato' ? 'tagliato' : esc(qe.t)}</small>
          <small class="mot" title="${esc(motivo)}">${esc(motivo)}</small>${cambia}${esterna}</div>
        <div><span>Consegna</span><b class="num${q.rit && st !== 'tagliato' ? ' rit' : ''}">${g ? FT.fmtData(g) : '—'}</b><small class="${q.rit && st !== 'tagliato' ? 'rit' : ''}">${esc(q.t)}</small></div>
        <div><span>Codici</span><b class="num">${info.n_codici != null ? FT.numero(info.n_codici) : '—'}</b><small>${info.n_esclusi ? `${info.n_esclusi} fuori da Lantek` : '&nbsp;'}</small></div>
        <div><span>Pezzi</span><b class="num">${info.n_pezzi != null ? FT.numero(info.n_pezzi) : '—'}</b><small>&nbsp;</small></div>
        <div><span>Ore laser</span>${ore}</div>
      </div></div>`;
  }

  /** "Lavorazione esterna" (zincatura, verniciatura...) dopo il laser: la
      dice il laser e il "taglia entro" se ne fida (+5 giorni). Non detta: si
      deduce dalle note, e la spunta resta a meta' (ambra) finche' non la
      confermi. */
  function campoEsterna(o, te) {
    const v = o.lavorazione_esterna;            // true | false | null (non detta)
    const dedotta = v == null && te && te.esterna_fonte === 'dedotta';
    const id = esc(o.id);
    return `<span class="lz-esterna${dedotta ? ' att' : ''}">
        <label title="Dopo il laser va fuori (zincatura, verniciatura, cataforesi…): +5 giorni lavorativi">
          <input type="checkbox" data-esterna="${id}" data-fuoco="esterna-${id}" ${v === true ? 'checked' : ''} ${dedotta ? 'data-meta="1"' : ''}
            aria-label="Lavorazione esterna dopo il laser">Lavorazione esterna</label>
        ${v === true ? `<input type="text" class="nota" data-esterna-nota="${id}" data-fuoco="esterna-nota-${id}" maxlength="60"
            value="${esc(o.lavorazione_esterna_nota || '')}" placeholder="quale? es. zincatura" aria-label="Quale lavorazione esterna">` : ''}
        ${dedotta ? `<small class="att" title="Trovata nelle note o nelle lavorazioni: spunta se è vero, togli la spunta se no">dedotta dalle note: da confermare</small>
          <button class="lz-link" type="button" data-az="esterna-no" data-fuoco="esterna-no">No, non va fuori</button>` : ''}
        ${v != null ? `<button class="lz-link" type="button" data-az="esterna-togli" data-fuoco="esterna-togli">${v ? 'Togli' : 'Non so'}</button>` : ''}</span>`;
  }
  /** La spunta "a meta'" (dedotta, non ancora confermata) si mette dopo il disegno. */
  O.segnaMeta = function () {
    document.querySelectorAll('#lz-dett input[data-esterna]').forEach(i => { i.indeterminate = !!i.dataset.meta; });
  };

  function passoHtml(o) {
    const p = O.passo(o);
    const D = S.dett[o.id] || {};
    const info = D.dati || S.riep[o.id] || {};
    const st = LZ.statoLaser(o);
    let h = `<section class="lz-passo ${p.tono || ''}" aria-label="Prossimo passo">
      <div class="ico">${p.carica ? `<span class="lz-gira" style="display:grid">${LZ.ico('loader-circle')}</span>` : LZ.ico(p.ico)}</div>
      <div class="cosa"><b>${esc(p.t)}</b><span>${p.s}</span></div>
      <div class="bottoni">${(p.bottoni || []).map(b => `<button class="lz-btn grande${b.pri ? ' pri' : ''}" type="button" data-az="${b.az}"
        data-fuoco="passo-${b.az}">${esc(b.t)}${b.tasto ? LZ.tasto(b.tasto) : ''}</button>`).join('')}</div></section>`;
    const bl = st === 'coda' ? (info.bloccati || []) : [];
    if (bl.length) {
      h += `<ul class="lz-blocchi" aria-label="Cosa blocca l'ordine">${bl.map(b => `<li><span class="mono">${esc(b.codice)}</span>
        <span class="mot">${esc(capo(b.motivo))}<small>${esc(b.cosa)}</small>${campoDati(b, info)}</span></li>`).join('')}</ul>`;
    }
    if ((st === 'coda' || st === 'in_lantek') && (info.correzioni || []).length) h += correzioniHtml(info.correzioni);
    const fermi = new Set((info.correzioni || []).map(c => c.codice));
    const diffNuove = st === 'coda' && D.dati ? (D.dati.differenze || []).filter(x => !fermi.has(x.codice)) : [];
    if (diffNuove.length && !info.n_controllare && !info.n_abbinare) {
      const n = new Set(diffNuove.map(x => x.codice)).size;
      h += `<div class="lz-avviso att">${LZ.ico('scale')}<span>${LZ.plur(n, 'codice è diverso', 'codici sono diversi')} fra ordine e Lantek: lo scegli nella conferma prima di mandare.</span></div>`;
    }
    if (D.errore && D.dati) h += `<div class="lz-avviso">${LZ.ico('wifi-off')}<span>Ultimo aggiornamento non riuscito: ${esc(D.errore)}.</span></div>`;
    return h;
  }
  const capo = s => { s = String(s || ''); return s.charAt(0).toUpperCase() + s.slice(1); };

  /** I codici "da correggere in Lantek" (scelto il valore dell'ordine in
      "Manda a Lantek"): cosa scrivere in Lantek, chi l'ha deciso, e la via
      per cambiare idea ("Va bene Lantek"). Restano finche' non sono mandati. */
  function correzioniHtml(cc) {
    const da = cc.filter(c => c.stato === 'da_correggere');
    const valLt = c => [c.spessore_voluto != null && c.spessore_lantek != null ? LZ.mm(c.spessore_lantek) : '',
      c.materiale_voluto && c.materiale_lantek ? c.materiale_lantek : ''].filter(Boolean).join(' · ');
    return `<ul class="lz-blocchi lz-correggi${da.length ? '' : ' pronti'}" aria-label="Da correggere in Lantek">${cc.map(c => {
      if (c.tipo === 'quantita') {
        // quantita' cambiata dopo l'invio: si corregge l'ordine di produzione
        return `<li data-correzione="${esc(c.codice)}"><span class="mono">${esc(c.codice)}</span>
          <span class="mot">${esc(c.testo || c.cosa || '')}
            <small>${c.fonte === 'lantek' ? 'Letto adesso negli ordini di produzione di Lantek.' : 'Dal file mandato a Lantek.'} Quando l'hai corretto, segnalo qui.</small>
            <span class="lz-dati"><button class="lz-btn piccolo" type="button" data-az="qta-corretta" data-codice="${esc(c.codice)}"
              data-q="${esc(String(c.quantita_voluta))}" data-ql="${esc(String(c.quantita_lantek))}" data-fuoco="qta-corretta-${esc(c.codice)}"
              title="Ho corretto la quantità in Lantek">Corretto in Lantek (${esc(FT.numero(c.quantita_voluta))})</button></span></span></li>`;
      }
      const ok = c.stato === 'corretto';
      const chi = [c.creato_da, c.creato_il ? FT.fmtRelativo(c.creato_il) : ''].filter(Boolean).join(', ');
      return `<li class="${ok ? 'ok' : ''}" data-correzione="${esc(c.codice)}"><span class="mono">${esc(c.codice)}</span>
        <span class="mot">${ok ? 'Corretto in Lantek: pronto da mandare' : `Da correggere in Lantek: ${esc(c.cosa || '')}`}
          <small>${ok ? 'Mandalo con «Manda a Lantek».' : 'FerroTrack lo rilegge da solo e ti avvisa quando è a posto.'}${chi ? ` Deciso da ${esc(chi)}.` : ''}</small>
          ${ok ? '' : `<span class="lz-dati"><button class="lz-btn piccolo" type="button" data-az="usa-lantek" data-codice="${esc(c.codice)}"
            data-fuoco="usa-lantek-${esc(c.codice)}" title="Non correggo Lantek: lo mando com'è in Lantek">Va bene Lantek${valLt(c) ? ` (${esc(valLt(c))})` : ''}</button></span>`}</span></li>`;
    }).join('')}</ul>`;
  }

  // Spessore / materiale di un pezzo nuovo: lo decide il laser guardando il
  // disegno, e FerroTrack lo crea cosi' in Lantek (non si passa dal MES).
  const SPESSORI = [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 12, 15];
  function campoDati(b, info) {
    if (!b.campo || !b.articolo_id) return '';
    const aid = esc(b.articolo_id), cod = esc(b.codice);
    const vedi = b.disegno ? `<button class="lz-btn piccolo ghost" type="button" data-vedi="${aid}" title="Apri il disegno grande">${LZ.ico('maximize-2')}Guarda il disegno</button>` : '';
    if (b.campo === 'pz_assieme') {
      // quanti per assieme: l'ordine non lo dice, lo scrive il laser (Invio salva)
      return `<span class="lz-dati" data-dati="${aid}" data-codice="${cod}" data-campo="pz_assieme" data-qass="${esc(String(b.qta_assieme || ''))}">
        <label class="lz-sp">pezzi per assieme <input type="text" inputmode="numeric" maxlength="5" placeholder="?" data-fuoco="pza-blocco-${aid}"
          aria-label="Pezzi per assieme di ${cod}"></label>
        <span class="lz-pza-tot">${b.qta_assieme ? `× ${esc(FT.numero(b.qta_assieme))} ${b.qta_assieme === 1 ? 'assieme' : 'assiemi'}` : ''}</span>
        <button class="lz-btn piccolo pri" type="button" data-salva-dati data-fuoco="salva-dati-${aid}">Salva</button>${vedi}</span>`;
    }
    if (b.campo === 'spessore') {
      // la lettura migliore del motore (vista di fianco, cartiglio, peso/area…)
      // e' gia' scelta: il laser guarda il disegno e conferma con Invio
      const pr = b.proposta && b.proposta.spessore_mm ? b.proposta : null;
      const valori = pr && !SPESSORI.includes(pr.spessore_mm) ? SPESSORI.concat([pr.spessore_mm]).sort((x, y) => x - y) : SPESSORI;
      const primo = pr && O.primaProposta(info) === b.articolo_id;
      const chip = s => `<button class="lz-chip${pr && pr.spessore_mm === s ? ' on' : ''}" type="button" data-sp="${s}">${String(s).replace('.', ',')}</button>`;
      return `<span class="lz-dati${pr ? ' proposta' : ''}" data-dati="${aid}" data-codice="${cod}">
        ${pr ? `<span class="lz-proposto${pr.sicura ? '' : ' att'}">Proposto: <b>${esc(String(pr.spessore_mm).replace('.', ','))} mm</b> (${esc(pr.fonte || 'letto sul disegno')})${pr.sicura ? '' : ' · da controllare'}${(pr.altre || []).length ? ` · altre letture: ${pr.altre.map(x => String(x).replace('.', ',')).join(', ')} mm` : ''}</span>` : ''}
        <span class="lz-chips">${valori.map(chip).join('')}</span>
        <label class="lz-sp">altro <input type="text" inputmode="decimal" placeholder="mm" aria-label="Spessore in mm di ${cod}"></label>
        <button class="lz-btn piccolo pri" type="button" data-salva-dati data-fuoco="salva-dati-${aid}">Salva${primo ? LZ.tasto('Invio') : ''}</button>${vedi}</span>`;
    }
    const mats = (info.materiali_lantek || []);
    return `<span class="lz-dati" data-dati="${aid}" data-codice="${cod}">
      <select aria-label="Materiale di ${cod}"><option value="">Scegli il materiale…</option>${mats.map(m => `<option${String(b.materiale || '').toUpperCase() === m ? ' selected' : ''}>${esc(m)}</option>`).join('')}</select>
      <button class="lz-btn piccolo pri" type="button" data-salva-dati>Salva</button>${vedi}</span>`;
  }
  O.salvaDati = (id, box) => LZ.azione(async () => {
    const aid = box.dataset.dati, cod = box.dataset.codice;
    if (box.dataset.campo === 'pz_assieme') {
      const inp = box.querySelector('input');
      const n = Number(String((inp && inp.value) || '').trim());
      if (!(Number.isInteger(n) && n >= 1)) { FT.toast('Scrivi quanti pezzi servono per ogni assieme', 'warn'); if (inp) inp.focus(); return; }
      await O._perAssieme(id, aid, n, cod);
      return;
    }
    const sel = box.querySelector('select');
    const inp = box.querySelector('input');
    const chip = box.querySelector('.lz-chip.on');
    const corpo = {};
    if (sel) { if (!sel.value) { FT.toast('Scegli il materiale', 'warn'); sel.focus(); return; } corpo.materiale = sel.value; }
    else {
      const v = (inp && inp.value.trim()) || (chip && chip.dataset.sp) || '';
      const n = parseFloat(String(v).replace(',', '.'));
      if (!(n > 0)) { FT.toast('Scegli o scrivi lo spessore', 'warn'); if (inp) inp.focus(); return; }
      corpo.spessore_mm = n;
    }
    const r = await LZ.post(`/api/orders/${id}/pezzi/${encodeURIComponent(aid)}/dati`, corpo);
    if (!r.ok) { FT.toast('Non salvato: ' + (r.d.error || 'errore'), 'err'); return; }
    FT.toast(`${cod}: ${corpo.spessore_mm != null ? String(corpo.spessore_mm).replace('.', ',') + ' mm' : corpo.materiale} salvato`, 'ok');
    await O.dopo(id);
  });

  /** Il PDF dell'ordine sta ACCANTO ai pezzi (approvato il 14/10) quando
      c'e' posto: meta' schermo il PDF, meta' i pezzi; "Allarga" da' tutto lo
      spazio a uno dei due. Su uno schermo stretto resta la scheda "PDF ordine". */
  O.accanto = function (o) {
    if (!o || !o.pdf_file || S.pdfAccanto === false) return false;
    const el = document.getElementById('lz-dett');
    return !!(el && el.clientWidth >= 1080);
  };
  let pdfDi = null;
  function pdfAccanto(el, o, acc) {
    const box = document.getElementById('lz-o-pdf');
    if (!box) return;
    if (!acc) { if (pdfDi) { box.innerHTML = ''; pdfDi = null; } return; }
    const piena = S.allarga === 'pdf';
    const testa = `<div class="lz-pdf-testa"><b>PDF dell'ordine del cliente</b><span class="sp"></span>
      <button class="lz-btn piccolo ghost" type="button" data-az="allarga-pdf" aria-pressed="${piena}">${LZ.ico(piena ? 'minimize-2' : 'maximize-2')}${piena ? 'Accanto ai pezzi' : 'Allarga'}</button>
      <a class="lz-btn piccolo ghost lz-icona" href="/api/orders/${esc(o.id)}/pdf" target="_blank" rel="noopener" title="Apri in una scheda" aria-label="Apri il PDF in una scheda">${LZ.ico('external-link')}</a></div>`;
    if (pdfDi !== o.id) {
      box.innerHTML = `<div id="lz-pdf-testa"></div><div class="lz-pdf"><iframe id="lz-pdf" title="PDF dell'ordine" src="/api/orders/${esc(o.id)}/pdf#navpanes=0&view=FitH"></iframe></div>`;
      pdfDi = o.id;
    }
    LZ.metti(document.getElementById('lz-pdf-testa'), testa);
  }
  window.addEventListener('resize', () => { clearTimeout(O._rz); O._rz = setTimeout(() => O.disegna(), 150); });

  function schede(o) {
    const D = S.dett[o.id] || {};
    const pezzi = (D.dati && D.dati.pezzi) || [];
    const nDis = pezzi.filter(p => p.disegno).length;
    const t = (k, nome, n) => `<button type="button" role="tab" data-scheda="${k}" class="${S.scheda === k ? 'on' : ''}" aria-selected="${S.scheda === k}"
      data-fuoco="scheda-${k}">${nome}${n != null ? `<span class="n">${n}</span>` : ''}</button>`;
    let dx = '';
    if (S.scheda === 'pdf' && o.pdf_file) {
      dx = `<span class="sp"></span><button class="lz-btn piccolo ghost" type="button" data-az="pdf-stampa">${LZ.ico('printer')}Stampa</button>
        <a class="lz-btn piccolo ghost" href="/api/orders/${esc(o.id)}/pdf" target="_blank" rel="noopener">${LZ.ico('external-link')}Apri in una scheda</a>`;
    } else if (S.scheda === 'disegni' && nDis) {
      dx = `<span class="sp"></span><a class="lz-btn piccolo ghost" href="/api/orders/${esc(o.id)}/disegni.zip" download>${LZ.ico('download')}Scarica tutti</a>`;
    }
    const lm = LZ.lamiere(o);
    const acc = O.accanto(o);
    if (acc && S.scheda !== 'disegni') {
      const piena = S.allarga === 'pezzi';
      dx = `<span class="sp"></span><button class="lz-btn piccolo ghost" type="button" data-az="allarga-pezzi" aria-pressed="${piena}">${LZ.ico(piena ? 'minimize-2' : 'maximize-2')}${piena ? 'Mostra il PDF' : 'Allarga'}</button>`;
    }
    return t('pezzi', 'Pezzi', D.dati ? pezzi.length : null)
      + (lm.n ? t('lamiere', 'Per lamiera', lm.n) : '')
      + t('disegni', 'Disegni', D.dati ? nDis : null) + (acc ? '' : t('pdf', 'PDF ordine', null)) + dx;
  }

  function scheda(o) {
    if (S.scheda === 'pdf') return schedaPdf(o);
    const D = S.dett[o.id] || {};
    if (!D.dati) {
      if (D.errore) {
        return `<div class="lz-errore">${LZ.ico('triangle-alert')}<div><b>Non riesco a leggere i pezzi</b><p>${esc(D.errore)}</p>
          <button class="lz-btn piccolo" type="button" data-az="ricontrolla">Riprova</button></div></div>`;
      }
      return `<div aria-busy="true">${Array.from({ length: 6 }, () => '<div class="lz-skel" style="height:40px;margin-bottom:8px"></div>').join('')}</div>`;
    }
    if (S.scheda === 'lamiere') return schedaLamiere(o, D.dati);
    return S.scheda === 'disegni' ? schedaDisegni(o, D.dati) : schedaPezzi(o, D.dati);
  }

  // ── Pezzi ───────────────────────────────────────────────────────────
  const DECISI = { giusto: 'controllato da te', scelto: 'contorno scelto a mano', sviluppo: 'va sviluppato',
                   piu_pezzi: 'a mano in Lantek', non_laser: 'non è da laser', piu_pezzi_confermati: 'pezzo del foglio' };
  O.DECISI = DECISI;

  function cellaLantek(p) {
    if (p.escluso) return '<span class="lz-st">—</span>';
    if (p.assieme_senza_disegno) return '<span class="lz-st">assieme</span>';
    if (p.lantek === 'in_lantek') return `<span class="lz-st ok">${p.in_produzione ? 'in produzione' : "c'è già"}</span>`;
    if (p.lantek === 'nuovo') return `<span class="lz-st">${p.crea ? 'nuovo: lo creo io' : 'nuovo'}</span>`;
    return '<span class="lz-st">—</span>';
  }
  function cellaDisegno(p) {
    if (p.escluso) return `<span class="lz-st">${LZ.ico('circle-minus')}fuori: ${esc(p.escluso)}</span>`;
    if (p.assieme_senza_disegno) return '<span class="lz-st">non va al laser</span>';
    if (p.blocco) return `<span class="lz-st err" title="${esc(p.blocco.cosa)}">${LZ.ico('octagon-alert')}${esc(capo(p.blocco.motivo).replace(/^Disegno da verificare: .*/, 'Disegno da verificare'))}</span>`;
    if (p.da_controllare) return `<span class="lz-st att">${LZ.ico('scan-eye')}da controllare</span>`;
    if (p.decisione) return `<span class="lz-st ok">${LZ.ico('check')}${esc(DECISI[p.decisione] || p.decisione)}</span>`;
    if (!p.disegno) return `<span class="lz-st">${p.lantek === 'in_lantek' ? 'non serve' : 'senza disegno'}</span>`;
    if (p.motore === 'sicuro') return `<span class="lz-st ok" title="Il motore ha letto il disegno senza dubbi">${LZ.ico('check')}Letto bene</span>`;
    if (p.lantek === 'in_lantek') return '<span class="lz-st" title="Il codice c\'è già in Lantek: il disegno non serve">già in Lantek</span>';
    return '<span class="lz-st">—</span>';
  }

  // Assiemi chiusi nella scheda Pezzi (finche' la pagina resta aperta)
  const assChiusi = new Set();

  /** La voce "Da procurare" di un pezzo (tasto N), se c'e'. */
  const vocePezzo = (d, p) => p.articolo_id ? (d.procura_voci || []).find(v => v.articolo_id === p.articolo_id) : null;
  function statoVoce(v) {
    if (v.stato === 'arrivato') return `<span class="lz-st ok">${LZ.ico('check')}arrivato</span>`;
    if (v.stato === 'ordinato') return `<span class="lz-st${v.scaduta ? ' err' : ''}">${LZ.ico(v.scaduta ? 'clock-alert' : 'truck')}${esc(v.fornitore || 'ordinato')}</span>`;
    return `<span class="lz-st att">${LZ.ico('circle-dot')}da procurare</span>`;
  }

  /** Una riga dei pezzi. i = posizione in d.pezzi (menu ⋯). Nei pezzi di un
      assieme la quantita' e' pezzi per assieme x assiemi: i pezzi per
      assieme si scrivono li' (Invio salva). Un pezzo "non da laser" mostra
      cosa si procura (profilo, lunghezza) e non lo spessore della lamiera. */
  function rigaPezzo(o, d, p, i, ass) {
    const apribile = p.disegno && p.articolo_id && /\.dxf$/i.test(p.disegno);
    // pezzo di un foglio con piu' pezzi: si apre il foglio
    const vedi = apribile ? p.articolo_id : (p.foglio && p.foglio.articolo_id && p.foglio.disegno ? p.foglio.articolo_id : null);
    const v = vocePezzo(d, p);
    let q = `<td class="r q">${p.quantita != null ? FT.numero(p.quantita) : '—'}</td>`;
    if (ass && p.articolo_id) {
      const aid = esc(p.articolo_id);
      const da = p.qta_da_confermare;
      q = `<td class="r q"><span class="lz-pza-q">${da ? '?' : FT.numero(p.quantita)}</span>
        <small class="lz-pza${da ? ' da' : ''}"><input type="text" inputmode="numeric" maxlength="5" data-pza="${aid}" data-codice="${esc(p.codice)}"
          data-v="${da ? '' : esc(String(p.qta_per || ''))}" value="${da ? '' : esc(String(p.qta_per || ''))}" placeholder="?"
          data-fuoco="pza-${aid}" aria-label="Pezzi per assieme di ${esc(p.codice)}" title="Pezzi per ogni assieme (Invio salva)">/ass.</small></td>`;
    }
    let lamiera = esc(LZ.lamiera(p.materiale, p.spessore));
    if (v) {
      // da procurare: conta il profilo, non lo spessore letto come lamiera
      const pl = [v.profilo, v.lunghezza_mm ? `L ${FT.numero(v.lunghezza_mm)} mm` : ''].filter(Boolean).join(' · ');
      lamiera = `<span class="lz-pc-tipo t-${esc(v.tipo)}">${esc(v.tipo_testo)}</span> ${pl ? esc(pl) : ''}`
        + (v.da_completare ? ` <span class="lz-st att">${LZ.ico('pencil')}${esc(v.da_completare)}</span>` : '');
    }
    return `<tr class="${vedi ? 'cl' : ''}${p.escluso || p.assieme_senza_disegno ? ' fuori' : ''}${ass ? ' comp' : ''}" ${vedi ? `data-vedi="${esc(vedi)}"` : ''} data-fuoco="pz-${i}">
      <td class="cod">${esc(p.codice)}${p.codice_lantek ? `<small>in Lantek ${esc(p.codice_lantek)}</small>` : ''}${p.foglio ? `<small>pezzo ${esc(String(p.foglio.n))} del foglio ${esc(p.foglio.codice || '')}${p.foglio.qta_per_foglio > 1 ? ` (×${esc(String(p.foglio.qta_per_foglio))} nel foglio)` : ''}</small>` : ''}</td>
      ${q}
      <td>${lamiera}</td>
      <td class="lav">${esc((p.lavorazioni || []).join(', ')) || '—'}</td>
      <td>${cellaLantek(p)}</td>
      <td>${v ? statoVoce(v) : cellaDisegno(p)}</td>
      <td class="men">${p.articolo_id || p.foglio ? `<button class="lz-btn ghost lz-icona piccolo" type="button" data-pezzo-menu="${i}" aria-label="Azioni su ${esc(p.codice)}" title="Azioni sul pezzo">${LZ.ico('ellipsis')}</button>` : ''}</td></tr>`;
  }

  /** La testata di un assieme: codice, riga dell'ordine, quanti (si cambia: Invio). */
  function rigaAssieme(o, d, a, comp, tubi) {
    const chiuso = assChiusi.has(a.codice);
    const c = esc(a.codice);
    const nonLaser = comp.filter(p => p.escluso).length + tubi.length;
    const info = [LZ.plur(comp.length - comp.filter(p => p.escluso).length, 'pezzo al laser', 'pezzi al laser'),
      nonLaser ? `${nonLaser} da procurare` : ''].filter(Boolean).join(' · ');
    return `<tr class="lz-ass${chiuso ? ' chiuso' : ''}" data-fuoco="ass-${c}"><td colspan="7">
      <span class="lz-ass-riga">
        <button class="lz-btn ghost lz-icona piccolo" type="button" data-ass-apri="${c}" aria-expanded="${!chiuso}"
          title="${chiuso ? 'Mostra' : 'Nascondi'} i pezzi dell'assieme" aria-label="${chiuso ? 'Mostra' : 'Nascondi'} i pezzi di ${c}">${LZ.ico(chiuso ? 'chevron-right' : 'chevron-down')}</button>
        <span class="t">Assieme</span><b class="mono">${c}</b>
        ${a.descrizione ? `<span class="desc" title="${esc(a.descrizione)}">${esc(a.descrizione)}</span>` : ''}
        <label class="lz-assq" title="Quanti assiemi ordina il cliente (Invio salva)">×<input type="text" inputmode="numeric" maxlength="5"
          data-assq="${c}" data-v="${esc(String(a.quantita))}" value="${esc(String(a.quantita))}" data-fuoco="assq-${c}"
          aria-label="Quanti assiemi ${c}"></label>
        <span class="sp"></span>
        <span class="n">${esc(info)}</span>
        ${a.n_da_confermare ? `<span class="lz-st err">${LZ.ico('octagon-alert')}${LZ.plur(a.n_da_confermare, 'pezzo', 'pezzi')} per assieme da confermare</span>` : ''}
      </span></td></tr>`;
  }

  function schedaPezzi(o, d) {
    const pezzi = d.pezzi || [];
    const assiemi = d.assiemi || [];
    const codAss = new Set(assiemi.map(a => a.codice));
    let h = '';
    if (d.pdf && (d.pdf.da_abbinare || []).length && LZ.abbina) h += LZ.abbina.html(o, d);
    h += riepLamiere(o);
    if (!pezzi.length) {
      h += `<div class="lz-pdf-vuoto">${d.origine === 'nessuna'
        ? 'Quest\'ordine non ha l\'elenco dei pezzi: le quantità sono sul <b>PDF ordine</b>.'
        : 'Nessun pezzo di lamiera da tagliare in quest\'ordine.'}</div>`;
    } else {
      const sciolti = pezzi.map((p, i) => [p, i]).filter(([p]) => !codAss.has(p.assieme));
      let corpo = sciolti.map(([p, i]) => rigaPezzo(o, d, p, i, null)).join('');
      for (const a of assiemi) {
        const comp = pezzi.map((p, i) => [p, i]).filter(([p]) => p.assieme === a.codice);
        const tubi = (d.altri || []).filter(x => x.tipo === 'tubolare' && x.assieme === a.codice);
        corpo += rigaAssieme(o, d, a, comp.map(([p]) => p), tubi);
        if (assChiusi.has(a.codice)) continue;
        corpo += comp.map(([p, i]) => rigaPezzo(o, d, p, i, a)).join('');
        corpo += tubi.map(t => `<tr class="fuori comp"><td class="cod">${esc(t.codice || '—')}</td><td class="r q">${FT.numero(t.quantita)}</td>
          <td colspan="3"><span class="lz-pc-tipo t-tubo">Tubolare</span> ${esc(t.descrizione || '')}</td><td><span class="lz-st">in «Da procurare»</span></td><td></td></tr>`).join('');
      }
      h += `<table class="lz-tab"><thead><tr><th>Codice</th><th class="r">Q.tà</th><th>Lamiera</th><th>Lavorazioni</th><th>Lantek</th><th>Disegno</th><th class="men"><span class="sr" style="position:absolute;left:-9999px">Azioni</span></th></tr></thead><tbody>
        ${corpo}</tbody></table>`;
    }
    const ass = (d.altri || []).filter(x => x.tipo === 'assieme' && !codAss.has(x.codice)).map(x => x.codice)
      .concat(pezzi.filter(p => p.assieme_senza_disegno).map(p => p.codice));
    const tub = (d.altri || []).filter(x => x.tipo === 'tubolare' && !codAss.has(x.assieme));
    const note = [];
    if (ass.length) note.push(`${LZ.plur(ass.length, 'assieme', 'assiemi')} (<span class="mono">${ass.slice(0, 6).map(esc).join(', ')}${ass.length > 6 ? '…' : ''}</span>): non vanno al laser, i loro pezzi sono nelle righe sopra.`);
    if (tub.length) note.push(`${LZ.plur(tub.length, 'tubolare', 'tubolari')}: non vanno al laser, sono in «Da procurare» qui sotto.`);
    if (note.length) h += `<p class="lz-sotto-tab">${note.join('<br>')}</p>`;
    h += procuraHtml(o, d);
    return h;
  }

  // ── Per lamiera (14/10) ─────────────────────────────────────────────
  /** Una riga sola sopra i pezzi: le lamiere dell'ordine e quali sono
      tagliate. I pezzi restano nell'ordine del PDF del cliente. */
  function riepLamiere(o) {
    const lm = LZ.lamiere(o);
    if (lm.n < 2) return '';
    return `<div class="lz-lam-riep"><span class="t">${LZ.plur(lm.n, 'lamiera', 'lamiere')}${lm.fatte ? `, ${lm.fatte} tagliat${lm.fatte === 1 ? 'a' : 'e'}` : ''}:</span>
      <span>${lm.l.map(x => `<span class="lz-lam-pill${lm.tg[x.chiave] ? ' ok' : ''}">${esc(LZ.nomeLamiera(x))}${lm.tg[x.chiave] ? ' <em>tagliata</em>' : ''}</span>`).join('; ')}</span>
      <button class="lz-link" type="button" data-scheda="lamiere">Vedi per lamiera</button></div>`;
  }
  /** I pezzi raggruppati per lamiera, coi totali, e "Segna tagliato" per
      ciascuna (a mano: il ferro oggi, l'inox domani). Dentro ogni lamiera
      i pezzi restano nell'ordine del PDF. */
  function schedaLamiere(o, d) {
    const lm = LZ.lamiere(o);
    const pezzi = (d.pezzi || []).map((p, i) => [p, i]).filter(([p]) => p.lamiera);
    if (!lm.n) return '<div class="lz-pdf-vuoto">Nessun pezzo di lamiera da tagliare in quest\'ordine.</div>';
    const st = LZ.statoLaser(o);
    const puoi = st === 'in_lantek' || st === 'coda';
    return `<p class="lz-lam-nota">Le lamiere come in Lantek. I pezzi, dentro ogni lamiera, nell'ordine del PDF.</p>` + lm.l.map(x => {
      const t = lm.tg[x.chiave];
      const righe = pezzi.filter(([p]) => p.lamiera === x.chiave);
      const az = st === 'tagliato' ? '<span class="lz-st ok">tagliato con l\'ordine</span>'
        : t ? `<span class="lz-lam-fatto">${LZ.ico('check')}Tagliata ${esc(FT.fmtRelativo(t.il))}</span>
            <button class="lz-link" type="button" data-az="lamiera-annulla" data-chiave="${esc(x.chiave)}">Annulla</button>`
          : puoi ? `<button class="lz-btn piccolo" type="button" data-az="lamiera-tagliata" data-chiave="${esc(x.chiave)}" data-fuoco="lam-${esc(x.chiave)}">Segna tagliata</button>` : '';
      return `<section class="lz-lam${t ? ' fatta' : ''}" aria-label="${esc(LZ.nomeLamiera(x))}">
        <div class="lz-lam-testa"><div><b>${esc(LZ.nomeLamiera(x))}</b><span>${LZ.plur(x.n_codici, 'codice', 'codici')}, ${LZ.plur(x.n_pezzi, 'pezzo', 'pezzi')}</span></div>${az}</div>
        <table class="lz-tab"><tbody>${righe.map(([p, i]) => `<tr data-fuoco="lpz-${i}" ${p.articolo_id && p.disegno && /\.dxf$/i.test(p.disegno) ? `class="cl" data-vedi="${esc(p.articolo_id)}"` : ''}>
          <td class="cod">${esc(p.codice)}</td><td class="r q">${p.quantita != null ? FT.numero(p.quantita) : '—'}</td>
          <td class="lav">${esc((p.lavorazioni || []).join(', ')) || 'nessuna'}</td><td>${cellaDisegno(p)}</td></tr>`).join('')}</tbody></table></section>`;
    }).join('');
  }
  O.lamiera = (id, chiave, annulla) => LZ.azione(async () => {
    const r = await LZ.post(`/api/orders/${encodeURIComponent(id)}/lamiera-tagliata`, { chiave, annulla: !!annulla });
    if (!r.ok) { FT.toast('Non salvato: ' + (r.d.error || 'errore del server'), 'err'); return; }
    const x = (LZ.lamiere(LZ.trova(id) || { id }).l || []).find(l => l.chiave === chiave);
    const nome = x ? LZ.nomeLamiera(x) : chiave.replace('|', ' ');
    for (const box of [S.riep[id], (S.dett[id] || {}).dati]) if (box) box.lamiere_tagliate = r.d.lamiere_tagliate || {};
    LZ.ridisegna();
    FT.toast(annulla ? `${nome}: di nuovo da tagliare` : `${nome}: tagliata`, 'ok',
      annulla ? null : { testo: 'Annulla', fn: () => O.lamiera(id, chiave, true) });
  });

  /** I pezzi da procurare dell'ordine (tubi, laser tubo, meccanica,
      commerciali) e a che punto sono. Il pannello "Da procurare" fa il resto. */
  function procuraHtml(o, d) {
    const vv = d.procura_voci || [];
    if (!vv.length) return '';
    // laser tubo: il taglio (fornitore) e il materiale (barre comprate da noi) sono due cose
    const matLt = v => v.tipo === 'laser_tubo' && v.acquisto ? v.acquisto : null;
    const mancano = vv.filter(v => v.stato !== 'arrivato').length + vv.filter(v => matLt(v) && matLt(v).stato !== 'arrivato').length;
    const st = v => v.stato === 'arrivato' ? `<span class="lz-st ok">${LZ.ico('check')}arrivato</span>`
      : v.stato === 'ordinato' ? `<span class="lz-st${v.scaduta ? ' err' : ''}">${LZ.ico(v.scaduta ? 'clock-alert' : 'truck')}${esc(v.fornitore || 'ordinato')}${v.data_prevista ? ' · prev. ' + esc(FT.fmtDataBreve(v.data_prevista)) : ''}${v.scaduta ? ' (in ritardo)' : ''}</span>`
        : `<span class="lz-st att">${LZ.ico('circle-dot')}da ordinare</span>`;
    return `<section class="lz-o-procura" aria-label="Da procurare">
      <div class="testa"><b>Da procurare</b><span>${mancano ? `${LZ.plur(mancano, 'voce manca', 'voci mancano')}: l'ordine non è pronto per il montaggio` : 'tutto arrivato'}</span>
        <span class="sp"></span><button class="lz-btn piccolo" type="button" data-az="procura" data-fuoco="o-procura">${LZ.ico('package')}Apri «Da procurare»</button></div>
      <table class="lz-tab"><tbody>${vv.map(v => `<tr><td><span class="lz-pc-tipo t-${esc(v.tipo)}">${esc(v.tipo_testo)}</span></td>
        <td class="cod">${esc(v.codice || '—')}</td>
        <td class="lav">${esc([v.profilo || v.descrizione, v.lunghezza_mm ? FT.numero(v.lunghezza_mm) + ' mm' : ''].filter(Boolean).join(' · ') || '—')}</td>
        <td class="r q">${FT.numero(v.quantita)}</td><td>${matLt(v) ? `<small>taglio</small> ${st(v)}<br><small>materiale</small> ${st(matLt(v))}` : st(v)}</td></tr>`).join('')}</tbody></table></section>`;
  }

  // ── Disegni ─────────────────────────────────────────────────────────
  function schedaDisegni(o, d) {
    const pezzi = (d.pezzi || []).filter(p => p.disegno);
    if (!pezzi.length) return '<div class="lz-pdf-vuoto">Quest\'ordine non ha disegni.</div>';
    return `<div class="lz-griglia">${pezzi.map(p => {
      const dxf = /\.dxf$/i.test(p.disegno);
      const url = `/api/orders/${encodeURIComponent(o.id)}/dxf/${encodeURIComponent(p.disegno)}/svg`;
      return `<button class="lz-mini" type="button" ${dxf && p.articolo_id ? `data-vedi="${esc(p.articolo_id)}"` : 'disabled'} title="${esc(p.codice)}">
        <div class="im">${dxf ? `<img class="lz-disegno" src="${esc(url)}" alt="Disegno ${esc(p.codice)}" loading="lazy" onerror="LZ.ordine.miniaturaKo(this)">` : '<span class="no">anteprima non disponibile</span>'}</div>
        <div class="ri"><span class="mono">${esc(p.codice)}</span>${p.da_controllare ? '<span class="lz-chip att">da controllare</span>' : p.escluso ? '<span class="lz-chip">fuori</span>' : ''}</div></button>`;
    }).join('')}</div>`;
  }

  /** Anteprima non riuscita: si riprova una volta, poi lo si dice. */
  O.miniaturaKo = function (img) {
    if (!img.dataset.riprova) { img.dataset.riprova = '1'; setTimeout(() => { img.src = img.src.split('?')[0] + '?r=1'; }, 1500); return; }
    const s = document.createElement('span');
    s.className = 'no';
    s.textContent = 'anteprima non riuscita: aprilo grande';
    img.replaceWith(s);
  };

  // ── PDF ─────────────────────────────────────────────────────────────
  function schedaPdf(o) {
    if (!o.pdf_file) return '<div class="lz-pdf-vuoto">Quest\'ordine non ha il PDF.</div>';
    return `<div class="lz-pdf"><iframe id="lz-pdf" title="PDF dell'ordine" src="/api/orders/${esc(o.id)}/pdf#navpanes=0&view=FitH"></iframe></div>`;
  }

  // ── Clic e azioni ───────────────────────────────────────────────────
  document.addEventListener('click', e => {
    const dett = document.getElementById('lz-dett');
    if (!dett || !dett.contains(e.target)) return;
    const ap = e.target.closest('[data-az="apri-prossimo"]');
    if (ap) { LZ.lista.apri(ap.dataset.id); return; }
    if (e.target.closest('[data-az="chiudi"]')) { LZ.lista.chiudi(); return; }
    const o = LZ.trova(S.aperto);
    if (!o) return;
    // pezzi per assieme / quanti assiemi: si scrive nel campo, non si apre il disegno
    if (e.target.closest('input[data-pza], input[data-assq], .lz-pza, .lz-assq')) return;
    const aa = e.target.closest('[data-ass-apri]');
    if (aa) {
      const k = aa.dataset.assApri;
      if (assChiusi.has(k)) assChiusi.delete(k); else assChiusi.add(k);
      O.disegna();
      return;
    }
    const chip = e.target.closest('.lz-dati .lz-chip');
    if (chip) {
      const box = chip.closest('.lz-dati');
      box.querySelectorAll('.lz-chip').forEach(c => c.classList.toggle('on', c === chip));
      const inp = box.querySelector('input'); if (inp) inp.value = '';
      return;
    }
    const sv = e.target.closest('[data-salva-dati]');
    if (sv) { O.salvaDati(o.id, sv.closest('.lz-dati')); return; }
    const sc = e.target.closest('[data-scheda]');
    if (sc) { O.mostraScheda(sc.dataset.scheda); return; }
    const pm = e.target.closest('[data-pezzo-menu]');
    if (pm) { e.stopPropagation(); menuPezzo(o, Number(pm.dataset.pezzoMenu), pm); return; }
    const az = e.target.closest('[data-az]');
    if (az && !az.closest('.lz-abb')) { O.esegui(az.dataset.az, o.id, az); return; }
    const v = e.target.closest('[data-vedi]');
    if (v && !e.target.closest('.lz-abb')) LZ.controllo.vedi(o.id, v.dataset.vedi);
  });
  // Invio nei campi dei pezzi per assieme e degli assiemi: si salva; Esc rimette com'era
  document.addEventListener('keydown', e => {
    const t = e.target;
    if (!t || !t.matches || !t.matches('#lz-dett input[data-pza], #lz-dett input[data-assq]')) return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); t.value = t.dataset.v || ''; t.blur(); return; }
    if (e.key !== 'Enter') return;
    e.preventDefault(); e.stopPropagation();
    salvaCampoQta(t);
  }, true);
  document.addEventListener('keydown', e => {
    const box = e.target.closest && e.target.closest('.lz-dati');
    if (!box || e.key !== 'Enter') return;
    e.preventDefault(); e.stopPropagation();
    const o = LZ.trova(S.aperto);
    if (o) O.salvaDati(o.id, box);
  }, true);
  document.addEventListener('input', e => {
    const box = e.target.closest && e.target.closest('.lz-dati');
    if (box && e.target.matches('input')) box.querySelectorAll('.lz-chip.on').forEach(c => c.classList.remove('on'));
    if (box && box.dataset.campo === 'pz_assieme') {
      // il totale si vede mentre si scrive: 2 per assieme x 3 assiemi = 6 pz
      const n = Number(e.target.value), qa = Number(box.dataset.qass) || 0;
      const tot = box.querySelector('.lz-pza-tot');
      if (tot && qa) tot.textContent = `× ${FT.numero(qa)} ${qa === 1 ? 'assieme' : 'assiemi'}` + (Number.isInteger(n) && n > 0 ? ` = ${FT.numero(n * qa)} pz` : '');
    }
  });
  /** Un campo "pezzi per assieme" o "quanti assiemi" della scheda Pezzi. */
  function salvaCampoQta(t) {
    const o = LZ.trova(S.aperto);
    if (!o) return;
    const v = String(t.value || '').trim();
    if (v === (t.dataset.v || '')) { t.blur(); return; }
    const n = Number(v);
    if (!(Number.isInteger(n) && n >= 1)) { FT.toast('Scrivi un numero intero da 1 in su', 'warn'); return; }
    const prima = t.dataset.v;
    t.dataset.v = v;                 // Invio poi l'uscita dal campo: si salva una volta sola
    if (t.dataset.pza) O.perAssieme(o.id, t.dataset.pza, n, t.dataset.codice);
    else O.qtaAssieme(o.id, t.dataset.assq, n, Number(prima) || null);
  }
  document.addEventListener('change', e => {
    const q = e.target.closest && e.target.closest('#lz-dett input[data-pza], #lz-dett input[data-assq]');
    if (q && document.activeElement !== q) { salvaCampoQta(q); return; }
    const inp = e.target.closest('input[data-durata]');
    if (inp) O.durataAMano(inp.dataset.durata, inp.value);
    const es = e.target.closest('input[data-esterna]');
    if (es) O.esterna(es.dataset.esterna, es.checked, null);
    const esn = e.target.closest('input[data-esterna-nota]');
    if (esn) O.esterna(esn.dataset.esternaNota, true, esn.value.trim());
    const en = e.target.closest('input[data-entro]');
    // la data si scrive anche a tasti: si salva solo quando e' una data vera da oggi in poi
    if (en && /^\d{4}-\d{2}-\d{2}$/.test(en.value) && en.value >= FT.oggiISO() && en.value < '2100') O.entroAMano(en.dataset.entro, en.value);
  });

  O.mostraScheda = function (k) {
    if (!['pezzi', 'lamiere', 'disegni', 'pdf'].includes(k)) return;
    S.scheda = k;
    O.disegna();
  };

  O.esegui = function (az, id, ancora) {
    const o = LZ.trova(id);
    if (!o) return;
    switch (az) {
      case 'smista-si': return O.smista(id, true);
      case 'smista-no': return O.smista(id, false);
      case 'taglio': return O.taglioFatto(id);
      case 'taglio-annulla': return O.annullaTaglio(id);
      case 'importato': return O.importato(id, false);
      case 'ricontrolla': return O.ricontrolla(id);
      case 'entro-calcolo': return O.entroAMano(id, null);
      case 'esterna-togli': return O.esterna(id, null, null);
      case 'esterna-no': return O.esterna(id, false, null);
      case 'controlla': return LZ.controllo.inizia([id]);
      case 'lantek': return LZ.lantek.apri([id]);
      case 'abbina': S.scheda = 'pezzi'; O.disegna(); return LZ.abbina && LZ.abbina.vai(id);
      case 'menu': return menuOrdine(o, ancora);
      case 'procura': return LZ.procura.apri({ ordine: id, stato: 'tutti' });
      case 'lamiera-tagliata': return ancora && O.lamiera(id, ancora.dataset.chiave, false);
      case 'lamiera-annulla': return ancora && O.lamiera(id, ancora.dataset.chiave, true);
      case 'allarga-pdf': S.allarga = S.allarga === 'pdf' ? '' : 'pdf'; return O.disegna();
      case 'allarga-pezzi': S.allarga = S.allarga === 'pezzi' ? '' : 'pezzi'; return O.disegna();
      case 'usa-lantek': return ancora && O.usaLantek(id, ancora.dataset.codice);
      case 'qta-corretta': return ancora && O.qtaCorretta(id, ancora.dataset.codice, Number(ancora.dataset.q), Number(ancora.dataset.ql));
      case 'pdf-stampa': {
        const f = document.getElementById('lz-pdf');
        try { f.contentWindow.focus(); f.contentWindow.print(); } catch (_) { window.open(`/api/orders/${id}/pdf`, '_blank', 'noopener'); }
        return;
      }
      default: return;
    }
  };

  function menuOrdine(o, ancora) {
    const st = LZ.statoLaser(o);
    const v = [];
    if (st === 'coda') {
      v.push({ t: "L'ho già messo in Lantek a mano", ico: 'file-check', fn: () => O.importato(o.id, false) });
      v.push({ t: 'Ricontrolla Lantek adesso', ico: 'refresh-cw', fn: () => O.ricontrolla(o.id) });
      v.push({ t: 'Rimetti fra quelli da smistare', ico: 'undo-2', fn: () => O.annullaSmista(o.id) });
    } else if (st === 'in_lantek') {
      v.push({ t: 'Non è ancora in Lantek', ico: 'undo-2', fn: () => O.importato(o.id, true) });
      v.push({ t: 'Ricontrolla Lantek adesso', ico: 'refresh-cw', fn: () => O.ricontrolla(o.id) });
    }
    if (v.length) v.push({ sep: true });
    v.push({ t: 'Crea la cartella dei disegni per Lantek', ico: 'folder-output', fn: () => O.creaCartella(o.id) });
    v.push({ t: 'Scarica tutti i disegni (ZIP)', ico: 'download', href: `/api/orders/${o.id}/disegni.zip`, download: true });
    if (st === 'coda' || st === 'in_lantek') v.push({ t: 'File degli ordini per Lantek (XML)', ico: 'file-code', href: `/api/orders/${o.id}/lantek-ordini.xml`, download: true });
    if (o.pdf_file) v.push({ t: 'Apri il PDF in una scheda', ico: 'external-link', fn: () => window.open(`/api/orders/${o.id}/pdf`, '_blank', 'noopener') });
    v.push({ sep: true }, { t: 'Elimina ordine…', ico: 'trash-2', cls: 'rosso', fn: () => O.elimina(o.id) });
    LZ.menu(ancora, v);
  }

  // ── Elimina ordine (dal menu ⋯) ─────────────────────────────────────
  /** Finestra di conferma: chi e quale ordine; se e' gia' andato a Lantek,
      che gli ordini di produzione restano in Lantek. Esc = annulla; Invio
      non elimina da solo (bisogna andare sul pulsante rosso). */
  O.elimina = function (id) {
    const o = LZ.trova(id);
    if (!o || document.getElementById('lz-elimina')) return;
    const info = (S.dett[id] || {}).dati || S.riep[id] || {};
    const inLantek = !!o.importato_lantek_il || (info.n_in_produzione || 0) > 0;
    const torna = document.activeElement;
    const v = document.createElement('div');
    v.className = 'lz-velo';
    v.id = 'lz-elimina';
    v.innerHTML = `<div class="lz-dlg lz-dlg-piccolo" role="alertdialog" aria-modal="true" aria-labelledby="lz-el-tit" aria-describedby="lz-el-cosa">
      <div class="lz-dlg-testa"><h2 id="lz-el-tit">Eliminare l'ordine?</h2></div>
      <div class="lz-dlg-corpo" id="lz-el-cosa">
        <div class="lz-el-ordine"><b>${esc(o.cliente || 'Cliente non indicato')}</b><span class="mono">${esc(LZ.numero(o))}</span>
          <small>consegna ${LZ.giorno(o) ? FT.fmtData(LZ.giorno(o)) : '—'}</small></div>
        <p>Sparisce dal laser e da tutte le pagine. Subito dopo puoi ancora annullare; più tardi lo recupera l'ufficio.</p>
        ${inLantek ? `<div class="lz-avviso err">${LZ.ico('triangle-alert')}<span><b>È già stato mandato a Lantek.</b> Gli ordini di produzione in Lantek restano:
          se non servono più, toglili dal MES.</span></div>` : ''}
      </div>
      <div class="lz-dlg-piede"><span class="sp"></span>
        <button class="lz-btn" type="button" data-el="no">Annulla${LZ.tasto('Esc')}</button>
        <button class="lz-btn elimina" type="button" data-el="si">${LZ.ico('trash-2')}Elimina ordine</button></div></div>`;
    const chiudi = () => {
      document.removeEventListener('keydown', tasti, true);
      v.remove();
      if (torna && torna.focus && document.contains(torna)) torna.focus();
    };
    const tasti = e => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); chiudi(); }
    };
    v.addEventListener('mousedown', e => { if (e.target === v) chiudi(); });
    v.addEventListener('click', e => {
      const b = e.target.closest('[data-el]');
      if (!b) return;
      chiudi();
      if (b.dataset.el === 'si') O._elimina(id);
    });
    document.addEventListener('keydown', tasti, true);
    document.getElementById('lz-sopra').appendChild(v);
    LZ.icone(v);
    v.querySelector('[data-el="no"]').focus();
  };
  O._elimina = (id) => LZ.azione(async () => {
    const o = LZ.trova(id);
    const nome = o ? `${o.cliente || ''} ${LZ.numero(o)}`.trim() : 'Ordine';
    let r;
    try {
      const x = await fetch(`/api/orders/${encodeURIComponent(id)}`, { method: 'DELETE' });
      r = { ok: x.ok, d: await x.json().catch(() => ({})) };
    } catch (_) { r = { ok: false, d: { error: 'Server non raggiungibile' } }; }
    if (!r.ok || r.d.success === false) { FT.toast('Non eliminato: ' + (r.d.error || 'errore del server'), 'err'); return; }
    S.spuntati.delete(id);
    if (S.aperto === id) S.aperto = null;           // a destra torna "scegli un ordine"
    delete S.dett[id];
    FT.toast(`${nome}: eliminato`, 'ok', { testo: 'Annulla', fn: () => O.ripristina(id) }, 10000);
    await O.dopo(id);
  });
  O.ripristina = (id) => LZ.azione(async () => {
    const r = await LZ.post(`/api/orders/${encodeURIComponent(id)}/ripristina`, {});
    if (!r.ok) { FT.toast('Non ripristinato: ' + (r.d.error || 'errore del server'), 'err'); return; }
    FT.toast('Ordine ripristinato', 'ok');
    S.aperto = id;
    S.scheda = 'pezzi';
    await O.dopo(id);
  });

  /** Menu di un pezzo di un foglio con piu' pezzi: si rivede o si annulla il foglio. */
  function menuFoglio(o, p, ancora) {
    const f = p.foglio;
    const v = [{ tit: `${p.codice} · foglio ${f.codice || ''}` }];
    if (f.disegno) {
      v.push({ t: 'Apri il foglio', ico: 'maximize-2', fn: () => LZ.controllo.vedi(o.id, f.articolo_id) });
      v.push({ t: 'Rivedi i pezzi del foglio', ico: 'copy', fn: () => LZ.controllo.vedi(o.id, f.articolo_id, 'pezzi') });
    }
    v.push({ sep: true }, { t: 'Annulla i pezzi del foglio', ico: 'undo-2', fn: () => O.annullaFoglio(o.id, f) });
    LZ.menu(ancora, v);
  }
  O.annullaFoglio = async function (id, f) {
    const ok = await FT.conferma({ titolo: `Annullare i pezzi del foglio ${f.codice || ''}?`, ok: 'Sì, annulla',
      testo: 'I DXF dei pezzi si tolgono e il disegno torna da controllare.' });
    if (!ok) return;
    await LZ.azione(async () => {
      let r;
      try {
        const x = await fetch(`/api/orders/${id}/pezzi/${encodeURIComponent(f.articolo_id)}/pezzi-foglio`, { method: 'DELETE' });
        r = { ok: x.ok, d: await x.json().catch(() => ({})) };
      } catch (_) { r = { ok: false, d: { error: 'Server non raggiungibile' } }; }
      if (!r.ok) { FT.toast('Non annullato: ' + (r.d.error || 'errore'), 'err'); return; }
      FT.toast(`Foglio ${f.codice || ''}: pezzi annullati, di nuovo da controllare`, 'ok');
      await O.dopo(id);
    });
  };

  function menuPezzo(o, i, ancora) {
    const p = (((S.dett[o.id] || {}).dati || {}).pezzi || [])[i];
    if (p && !p.articolo_id && p.foglio) { menuFoglio(o, p, ancora); return; }
    if (!p || !p.articolo_id) return;
    const v = [{ tit: p.codice }];
    if (p.disegno && /\.dxf$/i.test(p.disegno)) v.push({ t: 'Apri il disegno grande', ico: 'maximize-2', fn: () => LZ.controllo.vedi(o.id, p.articolo_id) });
    v.push({ sep: true }, { tit: 'Tienilo fuori da Lantek' });
    // "non e' da laser": si dice anche cosa procurare (T/L/M/C/A), nel controllo
    v.push({ t: 'Non è da laser: da procurare…', ico: 'circle-minus', fn: () => LZ.controllo.vedi(o.id, p.articolo_id, 'procura') });
    v.push({ t: 'Lo faccio a mano in Lantek', ico: 'hand', fn: () => O.decidi(o.id, p.articolo_id, 'piu_pezzi', p.codice) });
    v.push({ t: 'Va sviluppato', ico: 'move-diagonal', fn: () => O.decidi(o.id, p.articolo_id, 'sviluppo', p.codice) });
    if (p.decisione) v.push({ sep: true }, { t: 'Togli la decisione', ico: 'undo-2', fn: () => O.decidi(o.id, p.articolo_id, null, p.codice) });
    LZ.menu(ancora, v);
  }

  // ── Azioni sul server ───────────────────────────────────────────────
  /** Dopo un'azione: rilegge ordini, riassunto e (se aperto) l'ordine. */
  O.dopo = async function (id) {
    await Promise.all([LZ.avvio.caricaOrdini(true), LZ.avvio.caricaRiep(id)]);
    if (S.aperto === id) await O.carica(id);
    else LZ.ridisegna();
  };

  O.smista = async function (id, si) {
    if (!si) {
      // "non passa dal laser" manda l'ordine in officina: si chiede (07/10/2026)
      const o = LZ.trova(id);
      const ok = await FT.conferma({ titolo: 'Non passa dal laser?', ok: 'Sì, va in officina',
        testo: (o ? `${o.cliente || ''} · ${LZ.numero(o)}. ` : '') + "L'ordine esce dal laser e diventa subito lavorabile in officina." });
      if (!ok) return;
    }
    return O._smista(id, si);
  };
  O._smista = (id, si) => LZ.azione(async () => {
    const r = await LZ.post(`/api/orders/${id}/smistamento`, { va_tagliato: !!si });
    if (!r.ok) { FT.toast('Non riuscito: ' + (r.d.error || 'errore del server'), 'err'); return; }
    FT.toast(si ? 'Va al laser: ora è fra quelli da mettere in Lantek' : 'Non passa dal laser: va subito in officina', 'ok',
      { testo: 'Annulla', fn: () => O.annullaSmista(id, true) });
    if (!si && S.aperto === id) S.aperto = null;      // non e' piu' del laser: a destra torna la scelta
    await O.dopo(id);
  });
  O.annullaSmista = (id, daToast) => LZ.azione(async () => {
    const r = await LZ.post(`/api/orders/${id}/smistamento`, { annulla: true });
    if (!r.ok) { FT.toast('Non riuscito: ' + (r.d.error || 'errore del server'), 'err'); return; }
    FT.toast(daToast ? 'Annullato: di nuovo da smistare' : 'Rimesso fra quelli da smistare', 'ok');
    await O.dopo(id);
  });
  O.importato = (id, annulla) => LZ.azione(async () => {
    const r = await LZ.post(`/api/orders/${id}/importato`, { annulla: !!annulla });
    if (!r.ok) { FT.toast('Non riuscito: ' + (r.d.error || 'errore del server'), 'err'); return; }
    FT.toast(annulla ? 'Di nuovo da mettere in Lantek' : 'In Lantek: ora è fra quelli da tagliare', 'ok',
      annulla ? null : { testo: 'Annulla', fn: () => O.importato(id, true) });
    await O.dopo(id);
  });
  O.taglioFatto = async function (id) {
    const o = LZ.trova(id);
    const ok = await FT.conferma({ titolo: 'Taglio fatto?', ok: 'Sì, taglio fatto',
      testo: (o ? `${o.cliente || ''} · ${LZ.numero(o)}. ` : '') + 'L\'ordine passa in officina.' });
    if (!ok) return;
    await LZ.azione(async () => {
      const r = await LZ.post(`/api/orders/${id}/mark-laser-done`, {});
      if (r.d && r.d.gia_fatto) {
        // Un'altra postazione (o un doppio tocco) l'ha appena segnato: va bene così.
        FT.toast(r.d.error || 'Taglio già segnato come fatto', 'warn');
        await O.dopo(id); return;
      }
      if (!r.ok) { FT.toast('Non riuscito: ' + (r.d.error || 'errore del server'), 'err'); return; }
      FT.toast('Taglio segnato: l\'ordine è passato in officina', 'ok', { testo: 'Annulla', fn: () => O.annullaTaglio(id, true) });
      await O.dopo(id);
    });
  };
  O.annullaTaglio = async function (id, senzaConferma) {
    if (!senzaConferma && !(await FT.conferma({ titolo: 'Annullare il taglio?', testo: 'L\'ordine torna fra quelli da tagliare.', ok: 'Sì, annulla' }))) return;
    await LZ.azione(async () => {
      const r = await LZ.post(`/api/orders/${id}/mark-laser-undone`, {});
      if (!r.ok) { FT.toast('Non riuscito: ' + (r.d.error || 'errore del server'), 'err'); return; }
      FT.toast('Taglio annullato: di nuovo da tagliare', 'ok');
      await O.dopo(id);
    });
  };
  /** Codice da correggere: va bene com'e' in Lantek (si manda cosi'). */
  O.usaLantek = async function (id, codice) {
    if (!codice) return;
    const c = ((S.riep[id] || {}).correzioni || []).find(x => x.codice === codice) || {};
    const voluto = String(c.cosa || '').replace(/ \(in Lantek [^)]*\)/g, '');
    const ok = await FT.conferma({ titolo: `${codice}: va bene com'è in Lantek?`, ok: 'Sì, va bene Lantek',
      testo: `Non lo correggi in Lantek e si manda così com'è${voluto ? ` (non ${voluto} come dice l'ordine)` : ''}. Poi lo mandi con «Manda a Lantek».` });
    if (!ok) return;
    await LZ.azione(async () => {
      const r = await LZ.post(`/api/orders/${encodeURIComponent(id)}/lantek-correzioni`, { codice, azione: 'usa_lantek' });
      if (!r.ok) { FT.toast('Non salvato: ' + (r.d.error || 'errore del server'), 'err'); return; }
      FT.toast(`${codice}: va bene Lantek, pronto da mandare`, 'ok');
      await O.dopo(id);
    });
  };
  /** Pezzi per assieme (o del pezzo sciolto) scritti dal laser: la
      quantita' diventa questo x assiemi, per Lantek e "Da procurare". */
  O._perAssieme = async function (id, aid, n, codice) {
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    const r = await LZ.post(`/api/orders/${encodeURIComponent(id)}/pezzi/${encodeURIComponent(aid)}/per-assieme`, { pezzi: n });
    if (!r.ok) { FT.toast('Non salvato: ' + (r.d.error || 'errore del server'), 'err'); return; }
    FT.toast(`${codice || 'Pezzo'}: ${FT.numero(n)} per assieme, ${FT.numero(r.d.quantita)} in tutto`, 'ok',
      r.d.prima && r.d.prima !== n ? { testo: 'Annulla', fn: () => O.perAssieme(id, aid, r.d.prima, codice) } : null);
    await O.dopo(id);
  };
  O.perAssieme = (id, aid, n, codice) => LZ.azione(() => O._perAssieme(id, aid, n, codice));
  /** Quanti assiemi (la riga dell'ordine): tutti i loro pezzi si ricalcolano. */
  O.qtaAssieme = (id, codice, n, prima) => LZ.azione(async () => {
    if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
    const r = await LZ.post(`/api/orders/${encodeURIComponent(id)}/assiemi/${encodeURIComponent(codice)}/quantita`, { quantita: n });
    if (!r.ok) { FT.toast('Non salvato: ' + (r.d.error || 'errore del server'), 'err'); await O.dopo(id); return; }
    FT.toast(`Assieme ${codice}: ×${FT.numero(n)} (era ×${FT.numero(r.d.prima)}), pezzi ricalcolati`, 'ok',
      { testo: 'Annulla', fn: () => O.qtaAssieme(id, codice, r.d.prima, n) });
    await O.dopo(id);
  });
  /** Quantita' cambiata dopo l'invio: l'ho corretta a mano in Lantek. */
  O.qtaCorretta = async function (id, codice, q, ql) {
    const ok = await FT.conferma({ titolo: `${codice}: corretto in Lantek?`, ok: 'Sì, corretto',
      testo: `Nell'ordine di produzione di Lantek ora ci sono ${FT.numero(q)} pezzi (prima ${FT.numero(ql)}).` });
    if (!ok) return;
    await LZ.azione(async () => {
      const r = await LZ.post(`/api/orders/${encodeURIComponent(id)}/lantek-correzioni`,
        { codice, azione: 'quantita_corretta', quantita: q, quantita_lantek: ql });
      if (!r.ok) { FT.toast('Non salvato: ' + (r.d.error || 'errore del server'), 'err'); return; }
      FT.toast(`${codice}: quantità corretta in Lantek`, 'ok');
      await O.dopo(id);
    });
  };
  O.ricontrolla = async function (id) {
    FT.toast('Rileggo Lantek…', '', null, 1800);
    await LZ.avvio.caricaRiep(id, true);
    await O.carica(id, true);
  };
  O.creaCartella = (id) => LZ.azione(async () => {
    const r = await LZ.post(`/api/orders/${id}/crea-cartella`, {});
    if (!r.ok) {
      FT.toast(r.d.codice === 'non_impostata' ? 'La cartella dei disegni non è impostata: chiedi all\'amministratore (Impostazioni)'
        : 'Cartella non creata: ' + (r.d.error || 'errore'), 'err', null, 8000);
      return;
    }
    FT.toast(`Cartella pronta: ${r.d.percorso} (${r.d.esportati} file)`, 'ok',
      { testo: 'Copia percorso', fn: () => { try { navigator.clipboard.writeText(r.d.percorso); FT.toast('Percorso copiato', 'ok'); } catch (_) { /* niente appunti */ } } }, 10000);
  });
  /** "Taglia entro" scelto a mano (data) o tolto (null = torna al calcolo).
      Usa /pianifica-taglio: la data a mano E' il taglia entro. */
  O.entroAMano = (id, data) => LZ.azione(async () => {
    const r = await LZ.post(`/api/orders/${id}/pianifica-taglio`, { data: data || null });
    if (!r.ok) { FT.toast(r.d.error || 'Data non salvata', 'err'); return; }
    FT.toast(data ? `Taglia entro ${FT.fmtData(data)} (a mano)` : 'Taglia entro: torna al calcolo', 'ok');
    if (document.activeElement) document.activeElement.blur();
    await LZ.avvio.caricaOrdini(true);
    await LZ.avvio.caricaRiep(id);
    await LZ.sat.carica(true);
    LZ.ridisegna();
  });
  /** Lavorazione esterna: true / false / null (non detta: si deduce dalle note).
      nota: cosa (solo con true; null = lascia quella che c'e'). */
  O.esterna = (id, valore, nota) => LZ.azione(async () => {
    const o = LZ.trova(id);
    const corpo = { valore, nota: nota != null ? nota : ((o && o.lavorazione_esterna_nota) || '') };
    const r = await LZ.post(`/api/orders/${id}/lavorazione-esterna`, corpo);
    if (!r.ok) { FT.toast(r.d.error || 'Non salvato', 'err'); LZ.ridisegna(); return; }
    FT.toast(valore ? `Lavorazione esterna${r.d.nota ? ' (' + r.d.nota + ')' : ''}: +5 giorni sul taglia entro`
      : valore === false ? 'Nessuna lavorazione esterna' : 'Lavorazione esterna: torna dedotta dalle note', 'ok');
    if (document.activeElement) document.activeElement.blur();      // il ridisegno non aspetta il campo
    await LZ.avvio.caricaOrdini(true);
    await LZ.avvio.caricaRiep(id);
    await LZ.sat.carica(true);
    LZ.ridisegna();
  });
  O.durataAMano = (id, valore) => LZ.azione(async () => {
    const h = parseFloat(String(valore).replace(',', '.'));
    const min = h > 0 ? Math.round(h * 600) / 10 : null;
    const r = await LZ.post(`/api/orders/${id}/pianifica-taglio`, { durata_min: min });
    if (!r.ok) { FT.toast(r.d.error || 'Durata non salvata', 'err'); return; }
    FT.toast(min ? `Ore di laser: ${LZ.ore(min)}` : 'Ore tolte', 'ok');
    await LZ.avvio.caricaOrdini(true);
    await LZ.sat.carica(true);
    if (document.activeElement) document.activeElement.blur();
    LZ.ridisegna();
  });

  /** Decisione su un pezzo (anche dal menu ⋯ della riga). */
  O.decidi = (id, aid, scelta, codice) => LZ.azione(async () => {
    const r = await LZ.post(`/api/orders/${id}/pezzi/${encodeURIComponent(aid)}/decisione`, { scelta });
    if (!r.ok) { FT.toast('Non salvato: ' + (r.d.error || 'errore'), 'err'); return; }
    const prima = r.d.prima;
    FT.toast(scelta ? `${codice}: ${DECISI[scelta]}, resta fuori da Lantek` : `${codice}: decisione tolta`, 'ok',
      // "non e' da laser" si rimette col controllo (serve cosa procurare): l'annulla toglie la decisione
      { testo: 'Annulla', fn: () => O.decidi(id, aid, ['sviluppo', 'piu_pezzi'].includes(prima) ? prima : null, codice) });
    await O.dopo(id);
  });
})();
