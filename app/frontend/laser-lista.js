/* Pagina laser — l'elenco degli ordini, sempre a sinistra.

   Dal 15/10/2026 (tela "Laser", approvata il 14/10) in gruppi: Da smistare,
   Da mandare a Lantek, In Lantek da tagliare, Tagliati. Prima le spunte
   grigie sulle righe gia' in Lantek non si capiva perche' non andassero:
   ora la spunta c'e' solo dove serve (Da mandare). Nella riga il numero
   d'ordine e' grande (gli ordini DECA erano tutti "DECA S.r.l." e si
   distinguevano da un numero minuscolo), e i due allarmi sono diversi:
   rosso "Consegna a rischio", arancione "Taglia entro passato".
   Dentro ogni gruppo l'ordine resta quello di prima: "taglia entro", poi
   consegna (backend/taglia_entro.py). Le caselle servono a lavorare piu'
   ordini insieme: "Controlla N disegni" e "Manda a Lantek (K ordini)". */
(function () {
  'use strict';
  const LZ = window.LZ;
  const S = LZ.s;
  const esc = LZ.esc;
  const L = LZ.lista = {};

  let costruita = false;
  function costruisci() {
    const el = document.getElementById('lz-lista');
    el.innerHTML = `<div class="lz-lista-testa" id="lz-l-testa"></div>
      <div class="lz-righe" id="lz-l-righe" role="list" aria-label="Ordini"></div>
      <div id="lz-l-az"></div>
      <p class="lz-tasti-nota"><kbd>↑</kbd><kbd>↓</kbd> scorri, <kbd>Spazio</kbd> spunta, <kbd>C</kbd> controlla i disegni</p>`;
    el.addEventListener('click', clic);
    el.addEventListener('change', e => {
      const c = e.target.closest('input[data-sp]');
      if (c) L.spunta(c.dataset.sp, c.checked);
    });
    costruita = true;
  }

  /** Si possono spuntare gli ordini da mettere in Lantek. */
  L.spuntabile = o => LZ.statoLaser(o) === 'coda';

  /** Il lato destro della riga: il "taglia entro" e quanto manca. Arancione
      se e' passato (indietro sul programma), rosso se la consegna e' a rischio. */
  function quando(o, st) {
    if (st === 'tagliato') return `<span class="lz-q"><b>tagliato</b><small>${esc(FT.fmtOra(o.data_taglio_completato))}</small></span>`;
    const g = LZ.entro(o);
    const q = LZ.quandoEntro(o);
    const ge = LZ.giorniEntro(o);
    // il rosso della consegna sta a sinistra (etichetta); qui solo il programma
    const passato = ge != null && ge < 0;
    const cls = passato ? 'indietro' : '';
    const sotto = passato ? `passato da ${-ge} gg` : q.t;
    return `<span class="lz-q ${cls}"><b>entro ${g ? FT.fmtDataBreve(g) : '—'}</b><small>${esc(sotto)}</small></span>`;
  }

  /** La riga sotto il cliente, UNA cosa: l'allarme rosso, o cosa c'e' da
      fare, o la consegna (e le lamiere gia' tagliate). */
  function sotto(o, st) {
    const al = LZ.allarme(o);
    const e = LZ.etichetta(o);
    let h;
    if (al && al.k === 'rischio') h = `<span class="lz-al rischio">${esc(al.t)}</span>`;
    else if (e.c === 'att' || e.c === 'err') h = `<span class="lz-sot ${e.c}">${esc(e.corto || e.t)}</span>`;
    else if (e.c === 'skel') h = '<span class="lz-skel" style="display:inline-block;height:10px;width:90px"></span>';
    else {
      const cons = LZ.giorno(o);
      h = `<span>consegna ${cons ? FT.fmtDataBreve(cons) : '—'}</span>`;
    }
    const lm = LZ.lamiere(o);
    if (st === 'in_lantek' && lm.n > 1 && lm.fatte && !(al && al.k === 'rischio')) h += `<span aria-hidden="true">·</span><span>${lm.fatte}/${lm.n} lamiere</span>`;
    return h + procura(o);
  }

  function riga(o) {
    const st = LZ.statoLaser(o);
    const on = S.aperto === o.id;
    const sp = L.spuntabile(o);
    const g = LZ.entro(o), cons = LZ.giorno(o), te = LZ.te(o);
    const tit = `${LZ.numero(o)} · ${o.cliente || ''} · taglia entro ${g ? FT.fmtData(g) : '—'}${te && te.motivo ? ' (' + te.motivo + ')' : ''} · consegna ${cons ? FT.fmtData(cons) : '—'}`;
    return `<div class="lz-r${on ? ' on' : ''}${st === 'tagliato' ? ' spento' : ''}" role="listitem" tabindex="0" data-id="${esc(o.id)}"
        data-fuoco="r-${esc(o.id)}" ${on ? 'aria-current="true"' : ''} title="${esc(tit)}">
      ${sp ? `<input type="checkbox" data-sp="${esc(o.id)}" ${S.spuntati.has(o.id) ? 'checked' : ''}
        aria-label="Seleziona ${esc(o.cliente || '')} ${esc(LZ.numero(o))}" tabindex="-1">` : '<span aria-hidden="true"></span>'}
      <span class="lz-num">${esc(LZ.numeroBreve(o))}</span>
      <div class="lz-cli"><b>${esc(o.cliente || 'Cliente non indicato')}</b><span class="lz-sotto">${sotto(o, st)}</span></div>
      ${quando(o, st)}
    </div>`;
  }

  /** Pezzi da procurare non ancora arrivati: un segno piccolo sulla riga
      (il chip intero sta sull'ordine aperto). Rosso se in ritardo. */
  function procura(o) {
    const p = (S.riep[o.id] || {}).procura;
    if (!p || !p.n) return '';
    return ` <em class="lz-pc-mini${p.n_scaduti ? ' rit' : ''}" title="${esc(`Da procurare: ${p.n}${p.n_scaduti ? `, ${p.n_scaduti} in ritardo` : ''}`)}">${LZ.ico('package')}${p.n}</em>`;
  }

  /** I numeri del menu a sinistra (cornice). */
  function menu(attivi) {
    const set = (k, n) => { const el = document.getElementById('lzm-' + k); if (el) el.textContent = n ? String(n) : ''; };
    set('tutti', attivi.length);
    set('coda', attivi.filter(o => ['coda', 'smistare'].includes(LZ.statoLaser(o))).length);
    set('in_lantek', attivi.filter(o => LZ.statoLaser(o) === 'in_lantek').length);
    set('controllo', attivi.filter(o => L.spuntabile(o)).reduce((s, o) => s + ((S.riep[o.id] || {}).n_controllare || 0), 0));
    set('correggere', attivi.reduce((s, o) => s + ((S.riep[o.id] || {}).n_correggere || 0), 0));
  }

  L.disegna = function () {
    if (!costruita) costruisci();
    const ordini = LZ.ordiniLaser();
    // le caselle restano solo sugli ordini ancora spuntabili
    for (const id of [...S.spuntati]) { const o = LZ.trova(id); if (!o || !L.spuntabile(o)) S.spuntati.delete(id); }

    const attivi = ordini.filter(o => LZ.statoLaser(o) !== 'tagliato');
    const nRischio = attivi.filter(o => (LZ.allarme(o) || {}).k === 'rischio').length;
    const nIndietro = attivi.filter(o => (LZ.allarme(o) || {}).k === 'indietro').length;
    const al = [nRischio ? `<span class="rischio">${LZ.plur(nRischio, 'consegna a rischio', 'consegne a rischio')}</span>` : '',
      nIndietro ? `<span class="indietro">${nIndietro} con il taglia entro passato</span>` : ''].filter(Boolean);
    LZ.metti(document.getElementById('lz-l-testa'), `<h1>Ordini al laser</h1>
      <p class="lz-riass">${LZ.plur(attivi.length, 'ordine', 'ordini')}${al.length ? '. ' + al.join(', ') : ''}</p>
      ${promemoria(attivi)}${LZ.sat.html()}`);
    menu(attivi);

    const righe = document.getElementById('lz-l-righe');
    let html = '';
    if (S.primoCarico) {
      html = Array.from({ length: 7 }, () => `<div class="lz-r" aria-hidden="true"><span></span>
        <div class="lz-skel" style="height:22px;width:52px"></div><div><div class="lz-skel" style="height:12px;width:70%;margin-bottom:6px"></div>
        <div class="lz-skel" style="height:10px;width:40%"></div></div><div class="lz-skel" style="height:12px;width:56px"></div></div>`).join('');
    } else if (!ordini.length) {
      html = `<div class="lz-vuoto-lista"><b>Nessun ordine al laser</b>Quando l'ufficio carica un ordine, compare qui.</div>`;
    } else {
      for (const g of LZ.GRUPPI) {
        const l = ordini.filter(o => LZ.statoLaser(o) === g.k);
        if (!l.length) continue;
        let dx = '';
        if (g.k === 'coda') {
          const daCtl = l.filter(o => (S.riep[o.id] || {}).n_controllare).length;
          dx = S.spuntati.size ? `<button class="lz-link" type="button" data-az="sel-via">Togli selezione</button>`
            : daCtl ? `<button class="lz-link" type="button" data-az="sel-ctl">Seleziona quelli da controllare</button>`
              : '<span class="nota">spunta per mandarne più</span>';
        }
        html += `<div class="lz-gruppo" data-gruppo="${g.k}" role="presentation"><h2>${esc(g.t)} <span>${l.length}</span></h2>${dx}</div>`;
        html += l.map(riga).join('');
      }
    }
    LZ.metti(righe, html, righe);
    disegnaAzione();
  };

  /** Promemoria fisso: i codici "da correggere in Lantek" non si dimenticano
      (clic: apre il primo ordine che ne ha). Resta finche' non sono risolti. */
  function promemoria(attivi) {
    const conta = k => attivi.reduce((s, o) => s + ((S.riep[o.id] || {})[k] || 0), 0);
    const nDa = conta('n_correggere'), nOk = conta('n_corretti');
    if (!nDa && !nOk) return '';
    return `<div class="lz-promemoria">${nDa ? `<button class="lz-ricorda" type="button" data-az="vai-correggere" data-fuoco="vai-correggere"
        title="Codici tenuti fuori da Lantek perché diversi dall'ordine: correggili in Lantek">${LZ.ico('pencil-ruler')}<span>${LZ.plur(nDa, 'codice', 'codici')} da correggere in Lantek</span></button>` : ''}
      ${nOk ? `<button class="lz-ricorda pronti" type="button" data-az="vai-corretti" data-fuoco="vai-corretti"
        title="Corretti in Lantek: si possono mandare">${LZ.ico('send')}<span>${LZ.plur(nOk, 'codice corretto', 'codici corretti')}: da mandare</span></button>` : ''}</div>`;
  }
  /** Il primo ordine (nell'ordine dell'elenco) con codici da correggere / corretti. */
  L.apriCorrezione = function (k) {
    const o = LZ.ordiniVisti().find(x => LZ.statoLaser(x) !== 'tagliato' && (S.riep[x.id] || {})[k]);
    if (o) L.apri(o.id);
    else FT.toast(k === 'n_correggere' ? 'Nessun codice da correggere in Lantek' : 'Nessun codice corretto da mandare', '');
  };

  function disegnaAzione() {
    const box = document.getElementById('lz-l-az');
    const sel = [...S.spuntati].map(LZ.trova).filter(Boolean);
    if (!sel.length) { LZ.metti(box, ''); return; }
    const nCtl = sel.reduce((s, o) => s + ((S.riep[o.id] || {}).n_controllare || 0), 0);
    const pronti = sel.filter(o => (S.riep[o.id] || {}).pronto);
    const altri = sel.length - pronti.length;
    LZ.metti(box, `<div class="lz-azione">
      <div class="q"><span>${LZ.plur(sel.length, 'ordine selezionato', 'ordini selezionati')}</span></div>
      <button class="lz-btn pri grande lungo" type="button" data-az="ctl-multi" ${nCtl ? '' : 'disabled'}>
        <span>Controlla ${LZ.plur(nCtl, 'disegno', 'disegni')}</span>${nCtl ? LZ.tasto('C') : ''}</button>
      <button class="lz-btn grande lungo" type="button" data-az="lantek-multi" ${pronti.length ? '' : 'disabled'}>
        <span>Manda a Lantek${pronti.length ? ` (${LZ.plur(pronti.length, 'ordine', 'ordini')})` : ''}</span>${pronti.length ? LZ.tasto('L') : ''}</button>
      ${altri && pronti.length ? `<div class="nota">${altri === 1 ? 'Un altro ha' : `Gli altri ${altri} hanno`} ancora qualcosa da fare prima di Lantek.</div>`
        : !pronti.length ? '<div class="nota">Per mandarli a Lantek, prima controlla i disegni e sistema i blocchi.</div>' : ''}
    </div>`);
  }

  function clic(e) {
    const az = e.target.closest('[data-az]');
    if (az) {
      const a = az.dataset.az;
      if (a === 'sel-ctl') L.selezionaDaControllare();
      else if (a === 'sel-via') { S.spuntati.clear(); LZ.ridisegna(); }
      else if (a === 'ctl-multi') L.controllaSpuntati();
      else if (a === 'lantek-multi') L.mandaSpuntati();
      else if (a === 'vai-correggere') L.apriCorrezione('n_correggere');
      else if (a === 'vai-corretti') L.apriCorrezione('n_corretti');
      else if (a === 'apri-prossimo') { const id = az.dataset.id; if (id) L.apri(id); }
      return;
    }
    if (e.target.closest('input[data-sp]')) return;
    const r = e.target.closest('.lz-r[data-id]');
    if (r) L.apri(r.dataset.id);
  }

  /** Il menu della cornice: porta al gruppo, al controllo, alle correzioni. */
  L.vai = function (k) {
    const righe = document.getElementById('lz-l-righe');
    if (k === 'tutti') { if (righe) righe.scrollTo({ top: 0 }); }
    else if (k === 'coda' || k === 'in_lantek') {
      const g = document.querySelector(`.lz-gruppo[data-gruppo="${k}"]`) || (k === 'coda' && document.querySelector('.lz-gruppo[data-gruppo="smistare"]'));
      if (g && righe) righe.scrollTo({ top: g.offsetTop - righe.offsetTop });
      else FT.toast(k === 'coda' ? 'Nessun ordine da mandare a Lantek' : 'Nessun ordine in Lantek da tagliare', '');
      const primo = g && g.nextElementSibling && g.nextElementSibling.matches('.lz-r[data-id]') ? g.nextElementSibling : null;
      if (primo) primo.focus({ preventScroll: true });
    } else if (k === 'controllo') {
      const ids = LZ.ordiniVisti().filter(o => L.spuntabile(o) && (S.riep[o.id] || {}).n_controllare).map(o => o.id);
      if (ids.length) LZ.controllo.inizia(ids); else FT.toast('Nessun disegno da controllare', '');
    } else if (k === 'correggere') L.apriCorrezione('n_correggere');
    // la voce resta accesa solo per i gruppi dell'elenco
    document.querySelectorAll('#lz-menu .ft-tab[data-lz-vai]').forEach(b =>
      b.classList.toggle('active', b.dataset.lzVai === (['coda', 'in_lantek'].includes(k) ? k : 'tutti')));
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
  /** Tutti i selezionati nella conferma: chi non parte dice perche'. */
  L.mandaSpuntati = function () {
    const ids = L.bersaglio();
    if (ids.some(id => (S.riep[id] || {}).pronto)) LZ.lantek.apri(ids);
  };

  /** Apre un ordine a destra (solo su richiesta: clic, frecce, Apri). */
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

  /** "Il prossimo da fare" quando a destra non c'e' niente di aperto: il
      primo ordine dell'elenco che chiede qualcosa (mai aperto da solo). */
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
      return LZ.ordiniVisti().filter(o => [LZ.numero(o), o.numero_ordine, o.cliente].some(x => String(x || '').toLowerCase().includes(t)))
        .map(o => ({ t: `${LZ.numeroBreve(o)}  ${o.cliente || ''}`, s: `${LZ.numero(o)} · consegna ${LZ.giorno(o) ? FT.fmtData(LZ.giorno(o)) : '—'}`,
                     st: `<span class="lz-chip">${esc({ smistare: W.da_smistare, coda: W.da_importare, in_lantek: W.in_lantek, tagliato: W.tagliato }[LZ.statoLaser(o)] || '')}</span>`,
                     fn: () => L.apri(o.id) }));
    };
  }
})();
