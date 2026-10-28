/* Pagina laser — "Disegni Lantek per articolo" (Stefano, 28/10/2026).

   Per Poliform e B&B (ordini che arrivano solo in PDF) ogni articolo del
   cliente si abbina UNA volta ai disegni di Lantek, che hanno nomi di
   Stefano ("POLIFORM FONDO 563.5X554"). Prima lo si poteva fare solo da un
   ordine con righe nuove e poi non si vedeva piu': qui c'e' l'elenco di
   tutti gli articoli abbinati, con Cambia, e Aggiungi articolo per farlo
   prima che arrivi l'ordine. Server: GET/POST /api/lantek/abbinamenti,
   ricerca /api/lantek/pezzi. Vista S.vista = 'articoli' (laser-lista.js). */
(function () {
  'use strict';
  const LZ = window.LZ;
  const S = LZ.s;
  const esc = LZ.esc;
  const A = LZ.articoli = { dati: null, errore: null, cliente: '', q: '', ed: null };

  A.carica = async function () {
    if (A.inCarico) return;
    A.inCarico = true;
    const r = await LZ.get('/api/lantek/abbinamenti');
    A.inCarico = false;
    A.dati = r.ok ? (r.d.articoli || []) : (A.dati || []);
    A.errore = r.ok ? null : (r.d.error || 'il server non risponde');
    LZ.ridisegna();
  };

  const clienti = () => [...new Set((A.dati || []).map(a => a.cliente).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'it'));
  const passa = a => {
    if (A.cliente && a.cliente !== A.cliente) return false;
    const q = A.q.trim().toLowerCase();
    if (!q) return true;
    return [a.codice, a.descrizione, ...a.pezzi.map(p => p.codice)].some(x => String(x || '').toLowerCase().includes(q));
  };

  /** La testata e la tabella, al posto dell'elenco degli ordini. */
  A.disegna = function (testa, righe) {
    if (A.dati == null) { A.carica(); }
    // non si ridisegna sotto le dita, tranne per la ricerca (A.forza)
    if (!A.forza && (LZ.staScrivendo(testa) || LZ.staScrivendo(righe))) return;
    A.forza = false;
    const n = (A.dati || []).length;
    const opz = (v, t, cur) => `<option value="${esc(v)}"${v === cur ? ' selected' : ''}>${esc(t)}</option>`;
    LZ.metti(testa, `<div class="lz-l-tit"><h1>Disegni Lantek per articolo</h1>
        <p class="lz-riass">Ogni articolo del cliente diventa questi disegni di Lantek: vale per tutti gli ordini che arrivano solo in PDF (Poliform, B&amp;B).</p>
        <div class="lz-filtri">
          <label>Cliente <select data-art="cliente" aria-label="Filtra per cliente">${opz('', 'Tutti', A.cliente)}${clienti().map(c => opz(c, c, A.cliente)).join('')}</select></label>
          <input type="search" class="lz-art-cerca" data-art="q" data-fuoco="art-q" value="${esc(A.q)}" placeholder="Cerca articolo, descrizione o disegno" aria-label="Cerca">
          <button class="lz-btn pri piccolo" type="button" data-art="nuovo" data-fuoco="art-nuovo">Aggiungi articolo</button>
        </div></div>`);
    let html;
    if (A.dati == null) html = '<div class="lz-vuoto-lista"><b>Li sto leggendo…</b></div>';
    else {
      const l = A.dati.filter(passa);
      const nuovo = A.ed && A.ed.nuovo ? `<tr class="lz-art-ed-r"><td colspan="5">${editor(A.ed)}</td></tr>` : '';
      html = `${A.errore ? `<p class="lz-art-err">Non letti: ${esc(A.errore)}</p>` : ''}
        <table class="lz-tabella lz-tab-art"><thead><tr><th class="c-acod">Articolo</th><th class="c-adesc">Descrizione dell'ordine</th>
          <th class="c-acli">Cliente</th><th class="c-adis">Disegni Lantek (quanti per articolo)</th><th class="c-aaz"><span class="lz-nascosto">Cambia</span></th></tr></thead>
        <tbody>${nuovo}${l.map(riga).join('')}</tbody></table>
        ${!l.length ? `<div class="lz-vuoto-lista"><b>${n ? 'Nessun articolo con questa ricerca' : 'Nessun articolo abbinato'}</b>${n ? '' : 'Premi «Aggiungi articolo», oppure abbina le righe quando arriva un ordine.'}</div>` : ''}`;
    }
    LZ.metti(righe, html, righe);
  };

  function riga(a) {
    const aperto = A.ed && !A.ed.nuovo && A.ed.codice === a.codice;
    const dis = a.non_laser ? '<span class="lz-art-nl">non va al laser</span>'
      : a.pezzi.map(p => `<span class="lz-art-p"><span class="mono">${esc(p.codice)}</span> <small>×${esc(String(p.quantita))}</small></span>`).join('');
    return `<tr class="lz-art${aperto ? ' on' : ''}" data-cod="${esc(a.codice)}">
        <td class="c-acod"><span class="lz-num">${esc(a.codice)}</span></td>
        <td class="c-adesc" title="${esc(a.descrizione)}">${esc(a.descrizione || '—')}</td>
        <td class="c-acli">${esc(a.cliente || '—')}</td>
        <td class="c-adis"><div class="lz-art-dis">${dis || '—'}</div></td>
        <td class="c-aaz">${aperto ? '' : `<button class="lz-btn piccolo" type="button" data-art="cambia" data-cod="${esc(a.codice)}" data-fuoco="art-c-${esc(a.codice)}">Cambia</button>`}</td>
      </tr>${aperto ? `<tr class="lz-art-ed-r"><td colspan="5">${editor(A.ed)}</td></tr>` : ''}`;
  }

  /** Il riquadro per scegliere i disegni: la ricerca in cima, poi quelli scelti. */
  function editor(e) {
    const scelti = Object.keys(e.pezzi);
    const trovati = (e.trovati || []).filter(x => e.pezzi[x.codice] == null);
    const n = scelti.length;
    return `<div class="lz-art-ed">
      ${e.nuovo ? `<div class="lz-art-campi">
          <label>Codice articolo del cliente<input type="text" data-art="f-codice" data-fuoco="art-f-codice" value="${esc(e.codiceNuovo || '')}" maxlength="60" placeholder="es. 05HFOM0700"></label>
          <label>Cliente<input type="text" data-art="f-cliente" data-fuoco="art-f-cliente" value="${esc(e.cliente || '')}" maxlength="120" list="lz-art-clienti" placeholder="es. Poliform"></label>
          <datalist id="lz-art-clienti">${clienti().map(c => `<option value="${esc(c)}">`).join('')}</datalist>
          <label class="largo">Descrizione (come nell'ordine)<input type="text" data-art="f-descrizione" data-fuoco="art-f-descrizione" value="${esc(e.descrizione || '')}" maxlength="200"></label>
        </div>` : `<h4><span class="mono">${esc(e.codice)}</span>: quali disegni di Lantek?</h4>`}
      <div class="lz-abb-cerca"><input type="search" data-art="testo" data-fuoco="art-testo" value="${esc(e.cerca || '')}"
          placeholder="Cerca un disegno in Lantek: un pezzo del nome (es. FONDO 563)" aria-label="Cerca un disegno in Lantek">
        <button class="lz-btn piccolo" type="button" data-art="cerca">${LZ.ico('search')}Cerca</button></div>
      ${e.cercaErr ? `<p class="lz-art-nota">${esc(e.cercaErr)}</p>` : (e.trovati || []).length && !trovati.length ? '<p class="lz-art-nota">I disegni trovati sono già fra quelli scelti.</p>' : ''}
      ${trovati.map(x => `<div class="lz-art-riga"><span class="mono">${esc(x.codice)}</span>
          <span class="lz-art-nota">${esc(LZ.lamiera(x.materiale, x.spessore))}</span>
          <button class="lz-btn piccolo" type="button" data-art="aggiungi" data-pz="${esc(x.codice)}">Aggiungi</button></div>`).join('')}
      <h5>${n ? `Disegni scelti (${n})` : 'Nessun disegno scelto'}</h5>
      ${scelti.map(c => `<div class="lz-art-riga scelto"><span class="mono">${esc(c)}</span>
          <label class="lz-art-q"><input type="number" min="1" max="9999" value="${esc(String(e.pezzi[c]))}" data-art="qta" data-pz="${esc(c)}"
            aria-label="Quanti per un articolo"> per articolo</label>
          <button class="lz-link" type="button" data-art="togli" data-pz="${esc(c)}">Togli</button></div>`).join('')}
      <div class="lz-abb-az">
        ${!e.nuovo ? `<button class="lz-link lz-art-dim" type="button" data-art="dimentica">Togli l'abbinamento</button>` : ''}
        <button class="lz-btn piccolo" type="button" data-art="chiudi">Annulla</button>
        <button class="lz-btn piccolo" type="button" data-art="nonlaser">Non va al laser</button>
        <button class="lz-btn piccolo pri" type="button" data-art="salva" ${n ? '' : 'disabled'}>${LZ.ico('check')}Salva${n ? `: ${LZ.plur(n, 'disegno', 'disegni')}` : ''}</button>
      </div></div>`;
  }

  function apri(codice) {
    if (!codice) {
      A.ed = { nuovo: true, codiceNuovo: '', cliente: A.cliente || '', descrizione: '', pezzi: {} };
    } else {
      const a = (A.dati || []).find(x => x.codice === codice);
      if (!a) return;
      const pezzi = {};
      for (const p of a.pezzi) pezzi[p.codice] = p.quantita;
      A.ed = { codice, pezzi };
    }
    LZ.ridisegna();
    if (!codice) { const r = document.getElementById('lz-l-righe'); if (r) r.scrollTo({ top: 0 }); }
    setTimeout(() => {
      const i = document.querySelector(A.ed && A.ed.nuovo ? '[data-art="f-codice"]' : '[data-art="testo"]');
      if (i) i.focus();
    }, 30);
  }

  async function cerca() {
    const e = A.ed;
    if (!e) return;
    const q = String(e.cerca || '').trim();
    if (q.length < 3) { e.cercaErr = 'Scrivi almeno 3 caratteri del nome'; e.trovati = []; A.forza = true; LZ.ridisegna(); return; }
    const r = await LZ.get(`/api/lantek/pezzi?q=${encodeURIComponent(q)}`);
    e.trovati = r.ok ? (r.d.pezzi || []) : [];
    e.cercaErr = !r.ok ? (r.d.error || 'Lantek non risponde') : (!e.trovati.length ? 'Nessun disegno in Lantek con questo nome' : null);
    A.forza = true;          // il cursore e' ancora nel campo di ricerca: si ridisegna lo stesso
    LZ.ridisegna();
  }

  async function manda(corpo, messaggio) {
    await LZ.azione(async () => {
      const r = await LZ.post('/api/lantek/abbinamenti', corpo);
      if (!r.ok) { FT.toast('Non salvato: ' + (r.d.error || 'errore'), 'err'); return; }
      A.ed = null;
      FT.toast(messaggio, 'ok');
      await A.carica();
      if (LZ.avvio && LZ.avvio.caricaRiep) LZ.avvio.caricaRiep();   // gli ordini al laser si rileggono
    });
  }

  function codiceEd() {
    const e = A.ed;
    return e.nuovo ? String(e.codiceNuovo || '').trim() : e.codice;
  }
  function controllaNuovo() {
    const e = A.ed;
    if (!e.nuovo) return true;
    const c = codiceEd();
    if (!c) { FT.toast("Scrivi il codice dell'articolo del cliente", 'err'); return false; }
    if ((A.dati || []).some(x => x.codice === c.toUpperCase())) { FT.toast(`${c.toUpperCase()} è già abbinato: usa Cambia nella sua riga`, 'err'); return false; }
    return true;
  }

  document.addEventListener('click', ev => {
    const b = ev.target.closest('[data-art]');
    if (!b || b.tagName === 'INPUT' || b.tagName === 'SELECT' || S.vista !== 'articoli') return;
    const a = b.dataset.art;
    const e = A.ed;
    if (a === 'nuovo') apri('');
    else if (a === 'cambia') apri(b.dataset.cod);
    else if (a === 'chiudi') { A.ed = null; LZ.ridisegna(); }
    else if (!e) return;
    else if (a === 'cerca') cerca();
    else if (a === 'aggiungi') { e.pezzi[b.dataset.pz] = 1; LZ.ridisegna(); }
    else if (a === 'togli') { delete e.pezzi[b.dataset.pz]; LZ.ridisegna(); }
    else if (a === 'salva') {
      if (!controllaNuovo()) return;
      const c = codiceEd();
      manda({ codice: c, descrizione: e.descrizione || '', cliente: e.cliente || '',
              pezzi: Object.entries(e.pezzi).map(([k, q]) => ({ codice: k, quantita: q })) },
            `${c.toUpperCase()}: salvato, vale per i prossimi ordini`);
    } else if (a === 'nonlaser') {
      if (!controllaNuovo()) return;
      const c = codiceEd();
      manda({ codice: c, descrizione: e.descrizione || '', cliente: e.cliente || '', non_laser: true },
            `${c.toUpperCase()}: non va al laser`);
    } else if (a === 'dimentica') {
      FT.conferma({ titolo: `Vuoi togliere l'abbinamento di ${e.codice}?`,
                    testo: "Al prossimo ordine con questo articolo te lo richiedo. Gli ordini già in Lantek non cambiano.",
                    ok: "Togli l'abbinamento", annulla: 'Annulla' })
        .then(si => { if (si) manda({ codice: e.codice, dimentica: true }, `${e.codice}: abbinamento tolto`); });
    }
  });
  document.addEventListener('input', ev => {
    const i = ev.target.closest('input[data-art]');
    if (!i || S.vista !== 'articoli') return;
    const k = i.dataset.art;
    if (k === 'q') {
      A.q = i.value;
      clearTimeout(A._t);
      A._t = setTimeout(() => {
        A.forza = true;
        LZ.ridisegna();
        const n = document.querySelector('input[data-art="q"]');
        if (n) { n.focus({ preventScroll: true }); n.setSelectionRange(n.value.length, n.value.length); }
      }, 250);
      return;
    }
    const e = A.ed;
    if (!e) return;
    if (k === 'testo') e.cerca = i.value;
    else if (k === 'f-codice') e.codiceNuovo = i.value;
    else if (k === 'f-cliente') e.cliente = i.value;
    else if (k === 'f-descrizione') e.descrizione = i.value;
    else if (k === 'qta') { const n = parseInt(i.value, 10); if (n >= 1 && n <= 9999) e.pezzi[i.dataset.pz] = n; }
  });
  document.addEventListener('change', ev => {
    const s = ev.target.closest('select[data-art="cliente"]');
    if (!s || S.vista !== 'articoli') return;
    A.cliente = s.value;
    LZ.ridisegna();
  });
  document.addEventListener('keydown', ev => {
    const i = ev.target.closest && ev.target.closest('input[data-art="testo"]');
    if (i && ev.key === 'Enter') { ev.preventDefault(); cerca(); }
  });
})();
