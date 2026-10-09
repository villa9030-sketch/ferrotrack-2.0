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
      const { pezzi, altri, esclusi, invio, differenze, pdf, ...rid } = r.d;   // eslint-disable-line no-unused-vars
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
      return { tono: '', ico: 'circle-check', t: 'Tagliato',
               s: `Segnato ${esc(FT.fmtRelativo(o.data_taglio_completato))}. Se è uno sbaglio, annullalo.`,
               bottoni: [{ t: 'Annulla taglio', az: 'taglio-annulla' }] };
    }
    if (st === 'in_lantek') {
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
      return { tono: 'err', ico: 'octagon-alert', t: `Bloccato: ${LZ.plur(bl.length, 'codice', 'codici')} da sistemare`,
               s: 'Finché non li sistemi, l\'ordine non parte. Quando hai fatto, premi Ricontrolla.',
               bottoni: [{ t: 'Ricontrolla', az: 'ricontrolla', pri: true, tasto: 'Invio' }] };
    }
    if (info.tutto_in_lantek) {
      return { tono: '', ico: 'circle-check', t: 'È già tutto in Lantek',
               s: 'Ogni codice ha già il suo ordine di produzione. Segnalo: passa fra quelli da tagliare.',
               bottoni: [{ t: 'Segna in Lantek', az: 'importato', pri: true, tasto: 'Invio' }] };
    }
    if (info.pronto) {
      const n = info.n_invio || 0;
      return { tono: 'pronto', ico: 'send', t: 'Pronto per Lantek',
               s: `${LZ.plur(n, 'codice', 'codici')}: creo i pezzi nuovi col disegno e gli ordini di produzione.`,
               bottoni: [{ t: 'Manda a Lantek', az: 'lantek', pri: true, tasto: 'Invio' }] };
    }
    return { tono: '', ico: 'circle-help', t: 'Niente da mandare a Lantek', s: 'Nessun codice da mandare: se l\'hai già messo a mano, segnalo dal menu ⋯.',
             bottoni: [{ t: 'Ricontrolla', az: 'ricontrolla', pri: true, tasto: 'Invio' }] };
  };

  /** Invio = il pulsante principale del prossimo passo. */
  O.invio = function () {
    const o = LZ.trova(S.aperto);
    if (!o) return false;
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
      <nav class="lz-schede" id="lz-o-schede" role="tablist" aria-label="Viste dell'ordine"></nav>
      <div class="lz-scheda" id="lz-o-scheda"></div></div>`;
    el._lzHtml = null;
    costruitoPer = id;
  }

  O.disegna = function () {
    const el = document.getElementById('lz-dett');
    if (!el) return;
    const o = S.aperto && LZ.trova(S.aperto);
    if (!o) {
      costruitoPer = null;
      el.classList.remove('con-pdf');
      LZ.metti(el, S.aperto && !S.primoCarico
        ? `<div class="lz-vuoto"><div><div class="ico">${LZ.ico('archive')}</div><b>Quest'ordine non è più al laser</b><p>Scegline un altro a sinistra.</p></div></div>`
        : `<div class="lz-vuoto"><div><div class="ico">${LZ.ico('mouse-pointer-click')}</div><b>Scegli un ordine a sinistra</b>
            <p>oppure spuntane più di uno per lavorarli insieme.</p><p style="margin-top:10px"><kbd>↑</kbd> <kbd>↓</kbd> per scorrere gli ordini</p></div></div>`);
      return;
    }
    if (costruitoPer !== o.id || !el.querySelector('#lz-o-testa')) scheletro(el, o.id);
    if (LZ.staScrivendo(el)) return;              // non si ridisegna sotto le dita
    el.classList.toggle('con-pdf', S.scheda === 'pdf');
    LZ.metti(document.getElementById('lz-o-testa'), testa(o));
    LZ.metti(document.getElementById('lz-o-passo'), passoHtml(o));
    LZ.metti(document.getElementById('lz-o-schede'), schede(o));
    LZ.metti(document.getElementById('lz-o-scheda'), scheda(o), el);
  };

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
    return `<div class="lz-o-testa">
      <div class="lz-o-tit"><h2 title="${esc(o.cliente || '')}">${esc(o.cliente || 'Cliente non indicato')}</h2>
        <div class="lz-o-sotto"><span class="mono">${esc(LZ.numero(o))}</span>${interno ? `<span class="mono" title="Numero interno">${esc(interno)}</span>` : ''}${LZ.chip(o)}</div></div>
      <div class="lz-o-az"><button class="lz-btn ghost lz-icona" type="button" data-az="menu" data-fuoco="menu-ordine" title="Altre azioni" aria-label="Altre azioni">${LZ.ico('ellipsis')}</button></div>
      <div class="lz-fatti">
        <div><span>Consegna</span><b class="num${q.rit && st !== 'tagliato' ? ' rit' : ''}">${g ? FT.fmtData(g) : '—'}</b><small class="${q.rit && st !== 'tagliato' ? 'rit' : ''}">${esc(q.t)}</small></div>
        <div><span>Codici</span><b class="num">${info.n_codici != null ? FT.numero(info.n_codici) : '—'}</b><small>${info.n_esclusi ? `${info.n_esclusi} fuori da Lantek` : '&nbsp;'}</small></div>
        <div><span>Pezzi</span><b class="num">${info.n_pezzi != null ? FT.numero(info.n_pezzi) : '—'}</b><small>&nbsp;</small></div>
        <div><span>Ore laser</span>${ore}</div>
      </div></div>`;
  }

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
        <span class="mot">${esc(capo(b.motivo))}<small>${esc(b.cosa)}</small></span></li>`).join('')}</ul>`;
    }
    if (st === 'coda' && D.dati && (D.dati.differenze || []).length && !info.n_controllare && !info.n_abbinare) {
      const n = new Set(D.dati.differenze.map(x => x.codice)).size;
      h += `<div class="lz-avviso att">${LZ.ico('scale')}<span>${LZ.plur(n, 'codice è diverso', 'codici sono diversi')} fra ordine e Lantek: lo scegli nella conferma prima di mandare.</span></div>`;
    }
    if (D.errore && D.dati) h += `<div class="lz-avviso">${LZ.ico('wifi-off')}<span>Ultimo aggiornamento non riuscito: ${esc(D.errore)}.</span></div>`;
    return h;
  }
  const capo = s => { s = String(s || ''); return s.charAt(0).toUpperCase() + s.slice(1); };

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
    return t('pezzi', 'Pezzi', D.dati ? pezzi.length : null) + t('disegni', 'Disegni', D.dati ? nDis : null) + t('pdf', 'PDF ordine', null) + dx;
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
    return S.scheda === 'disegni' ? schedaDisegni(o, D.dati) : schedaPezzi(o, D.dati);
  }

  // ── Pezzi ───────────────────────────────────────────────────────────
  const DECISI = { giusto: 'controllato', scelto: 'contorno scelto a mano', sviluppo: 'va sviluppato',
                   piu_pezzi: 'a mano in Lantek', non_laser: 'non è da laser' };
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
    if (p.motore === 'sicuro') return `<span class="lz-st ok">${LZ.ico('check')}sicuro</span>`;
    if (p.lantek === 'in_lantek') return '<span class="lz-st" title="Il codice c\'è già in Lantek: il disegno non serve">già in Lantek</span>';
    return '<span class="lz-st">—</span>';
  }

  function schedaPezzi(o, d) {
    const pezzi = d.pezzi || [];
    let h = '';
    if (d.pdf && (d.pdf.da_abbinare || []).length && LZ.abbina) h += LZ.abbina.html(o, d);
    if (!pezzi.length) {
      h += `<div class="lz-pdf-vuoto">${d.origine === 'nessuna'
        ? 'Quest\'ordine non ha l\'elenco dei pezzi: le quantità sono sul <b>PDF ordine</b>.'
        : 'Nessun pezzo di lamiera da tagliare in quest\'ordine.'}</div>`;
    } else {
      h += `<table class="lz-tab"><thead><tr><th>Codice</th><th class="r">Q.tà</th><th>Lamiera</th><th>Lavorazioni</th><th>Lantek</th><th>Disegno</th><th class="men"><span class="sr" style="position:absolute;left:-9999px">Azioni</span></th></tr></thead><tbody>
        ${pezzi.map((p, i) => {
          const apribile = p.disegno && p.articolo_id && /\.dxf$/i.test(p.disegno);
          return `<tr class="${apribile ? 'cl' : ''}${p.escluso || p.assieme_senza_disegno ? ' fuori' : ''}" ${apribile ? `data-vedi="${esc(p.articolo_id)}"` : ''} data-fuoco="pz-${i}">
            <td class="cod">${esc(p.codice)}${p.codice_lantek ? `<small>in Lantek ${esc(p.codice_lantek)}</small>` : ''}</td>
            <td class="r q">${p.quantita != null ? FT.numero(p.quantita) : '—'}</td>
            <td>${esc(LZ.lamiera(p.materiale, p.spessore))}</td>
            <td class="lav">${esc((p.lavorazioni || []).join(', ')) || '—'}</td>
            <td>${cellaLantek(p)}</td>
            <td>${cellaDisegno(p)}</td>
            <td class="men">${p.articolo_id ? `<button class="lz-btn ghost lz-icona piccolo" type="button" data-pezzo-menu="${i}" aria-label="Azioni su ${esc(p.codice)}" title="Azioni sul pezzo">${LZ.ico('ellipsis')}</button>` : ''}</td></tr>`;
        }).join('')}</tbody></table>`;
    }
    const ass = (d.altri || []).filter(x => x.tipo === 'assieme').map(x => x.codice)
      .concat(pezzi.filter(p => p.assieme_senza_disegno).map(p => p.codice));
    const tub = (d.altri || []).filter(x => x.tipo === 'tubolare');
    const note = [];
    if (ass.length) note.push(`${LZ.plur(ass.length, 'assieme', 'assiemi')} (<span class="mono">${ass.slice(0, 6).map(esc).join(', ')}${ass.length > 6 ? '…' : ''}</span>): non vanno al laser, i loro pezzi sono nelle righe sopra.`);
    if (tub.length) note.push(`${LZ.plur(tub.length, 'tubolare', 'tubolari')}: si tagliano a parte, non al laser.`);
    if (note.length) h += `<p class="lz-sotto-tab">${note.join('<br>')}</p>`;
    return h;
  }

  // ── Disegni ─────────────────────────────────────────────────────────
  function schedaDisegni(o, d) {
    const pezzi = (d.pezzi || []).filter(p => p.disegno);
    if (!pezzi.length) return '<div class="lz-pdf-vuoto">Quest\'ordine non ha disegni.</div>';
    return `<div class="lz-griglia">${pezzi.map(p => {
      const dxf = /\.dxf$/i.test(p.disegno);
      const url = `/api/orders/${encodeURIComponent(o.id)}/dxf/${encodeURIComponent(p.disegno)}/svg`;
      return `<button class="lz-mini" type="button" ${dxf && p.articolo_id ? `data-vedi="${esc(p.articolo_id)}"` : 'disabled'} title="${esc(p.codice)}">
        <div class="im">${dxf ? `<img class="lz-disegno" src="${esc(url)}" alt="Disegno ${esc(p.codice)}" loading="lazy">` : '<span class="no">anteprima non disponibile</span>'}</div>
        <div class="ri"><span class="mono">${esc(p.codice)}</span>${p.da_controllare ? '<span class="lz-chip att">da controllare</span>' : p.escluso ? '<span class="lz-chip">fuori</span>' : ''}</div></button>`;
    }).join('')}</div>`;
  }

  // ── PDF ─────────────────────────────────────────────────────────────
  function schedaPdf(o) {
    if (!o.pdf_file) return '<div class="lz-pdf-vuoto">Quest\'ordine non ha il PDF.</div>';
    return `<div class="lz-pdf"><iframe id="lz-pdf" title="PDF dell'ordine" src="/api/orders/${esc(o.id)}/pdf#navpanes=0&view=FitH"></iframe></div>`;
  }

  // ── Clic e azioni ───────────────────────────────────────────────────
  document.addEventListener('click', e => {
    const dett = document.getElementById('lz-dett');
    if (!dett || !dett.contains(e.target)) return;
    const o = LZ.trova(S.aperto);
    if (!o) return;
    const sc = e.target.closest('[data-scheda]');
    if (sc) { O.mostraScheda(sc.dataset.scheda); return; }
    const pm = e.target.closest('[data-pezzo-menu]');
    if (pm) { e.stopPropagation(); menuPezzo(o, Number(pm.dataset.pezzoMenu), pm); return; }
    const az = e.target.closest('[data-az]');
    if (az && !az.closest('.lz-abb')) { O.esegui(az.dataset.az, o.id, az); return; }
    const v = e.target.closest('[data-vedi]');
    if (v && !e.target.closest('.lz-abb')) LZ.controllo.vedi(o.id, v.dataset.vedi);
  });
  document.addEventListener('change', e => {
    const inp = e.target.closest('input[data-durata]');
    if (inp) O.durataAMano(inp.dataset.durata, inp.value);
  });

  O.mostraScheda = function (k) {
    if (!['pezzi', 'disegni', 'pdf'].includes(k)) return;
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
      case 'controlla': return LZ.controllo.inizia([id]);
      case 'lantek': return LZ.lantek.apri([id]);
      case 'abbina': S.scheda = 'pezzi'; O.disegna(); return LZ.abbina && LZ.abbina.vai(id);
      case 'menu': return menuOrdine(o, ancora);
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
    LZ.menu(ancora, v);
  }

  function menuPezzo(o, i, ancora) {
    const p = (((S.dett[o.id] || {}).dati || {}).pezzi || [])[i];
    if (!p || !p.articolo_id) return;
    const v = [{ tit: p.codice }];
    if (p.disegno && /\.dxf$/i.test(p.disegno)) v.push({ t: 'Apri il disegno grande', ico: 'maximize-2', fn: () => LZ.controllo.vedi(o.id, p.articolo_id) });
    v.push({ sep: true }, { tit: 'Tienilo fuori da Lantek' });
    v.push({ t: 'Non è da laser', ico: 'circle-minus', fn: () => O.decidi(o.id, p.articolo_id, 'non_laser', p.codice) });
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

  O.smista = (id, si) => LZ.azione(async () => {
    const r = await LZ.post(`/api/orders/${id}/smistamento`, { va_tagliato: !!si });
    if (!r.ok) { FT.toast('Non riuscito: ' + (r.d.error || 'errore del server'), 'err'); return; }
    FT.toast(si ? 'Va al laser: ora è fra quelli da mettere in Lantek' : 'Non passa dal laser: va subito in officina', 'ok',
      { testo: 'Annulla', fn: () => O.annullaSmista(id, true) });
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
      { testo: 'Annulla', fn: () => O.decidi(id, aid, ['sviluppo', 'piu_pezzi', 'non_laser'].includes(prima) ? prima : null, codice) });
    await O.dopo(id);
  });
})();
