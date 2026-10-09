/* Pagina laser — "Manda a Lantek" per uno o piu' ordini.
   UNA finestra di conferma con il riassunto ordine per ordine: cosa c'e' gia'
   in Lantek, cosa creo col disegno, cosa resta fuori e perche', le
   differenze fra ordine e Lantek da scegliere. Poi si manda un ordine alla
   volta (/lantek-invia, lo stesso di sempre) e per ognuno si vede come e'
   andata: mai il dubbio su cosa e' successo.
   Lantek lo fanno lavorare i suoi programmi sul server, solo dopo "Manda". */
(function () {
  'use strict';
  const LZ = window.LZ;
  const S = LZ.s;
  const esc = LZ.esc;
  const K = LZ.lantek = {};
  let st = null;

  K.aperto = () => !!st;

  K.apri = async function (ids) {
    if (st || !ids.length) return;
    LZ.chiudiMenu();
    st = { ids: LZ.ordiniLaser().map(o => o.id).filter(id => ids.includes(id)), fase: 'carica', dati: {}, errori: {},
           scelte: {}, esiti: {}, foto: LZ.fotografa() };
    const v = document.createElement('div');
    v.className = 'lz-velo';
    v.id = 'lz-lantek';
    v.innerHTML = '<div class="lz-dlg" role="dialog" aria-modal="true" aria-labelledby="lz-lt-tit"></div>';
    v.addEventListener('mousedown', e => { if (e.target === v && st && st.fase !== 'invio') K.chiudi(); });
    v.addEventListener('click', clic);
    v.addEventListener('change', e => {
      const r = e.target.closest('input[data-diff]');
      if (r) { (st.scelte[r.dataset.ordine] = st.scelte[r.dataset.ordine] || {})[r.dataset.codice] = r.value; disegna(); }
    });
    document.getElementById('lz-sopra').appendChild(v);
    disegna();
    // dati freschi: Lantek riletto adesso, per tutti
    await Promise.all(st.ids.map(async id => {
      const r = await LZ.get(`/api/orders/${encodeURIComponent(id)}/laser?fresco=1`);
      if (!st) return;
      if (r.ok) st.dati[id] = r.d; else st.errori[id] = r.d.error || 'il server non ha risposto';
    }));
    if (!st) return;
    st.fase = 'conferma';
    disegna();
    const b = document.querySelector('#lz-lantek [data-k="manda"]');
    if (b && !b.disabled) b.focus();
  };

  K.chiudi = function () {
    if (!st || st.fase === 'invio') return;
    const fatto = st.fase === 'fatto', ids = st.ids, foto = st.foto;
    st = null;
    const v = document.getElementById('lz-lantek');
    if (v) v.remove();
    LZ.ripristina(foto);
    if (fatto) for (const id of ids) LZ.ordine.dopo(id);
  };

  // ── Cosa parte, ordine per ordine ───────────────────────────────────
  function perche(d) {
    if (!d) return 'non letto';
    if (d.lantek && d.lantek.letto && !d.lantek.disponibile) return 'Lantek non risponde';
    if (d.n_abbinare) return `${LZ.plur(d.n_abbinare, 'riga', 'righe')} da abbinare`;
    if (d.n_controllare) return `${LZ.plur(d.n_controllare, 'disegno', 'disegni')} da controllare`;
    if ((d.bloccati || []).length) return `bloccato: ${(d.bloccati || []).map(b => b.codice).slice(0, 3).join(', ')}`;
    if (d.lantek && !d.lantek.invio_automatico) return "su questo PC non c'è l'XML Importer di Lantek";
    if (!d.n_invio) return 'niente da mandare';
    return '';
  }
  const parte = d => d && d.pronto && d.lantek && d.lantek.invio_automatico;
  function diffs(id) {
    const d = st.dati[id] || {};
    const per = {};
    for (const x of d.differenze || []) if (x.tipo === 'spessore' || x.tipo === 'materiale') (per[x.codice] = per[x.codice] || []).push(x);
    return per;
  }
  function daScegliere(id) {
    const s = st.scelte[id] || {};
    return Object.keys(diffs(id)).filter(c => !s[c]);
  }
  function tenutiFuori(id) {
    const s = st.scelte[id] || {};
    return Object.keys(s).filter(c => s[c] === 'ordine');
  }
  const elenco = (xs, n = 6) => xs.slice(0, n).map(esc).join(', ') + (xs.length > n ? ` e altri ${xs.length - n}` : '');

  function blocco(id) {
    const o = LZ.trova(id) || {};
    const d = st.dati[id];
    const testa = `<div class="lz-blocco-testa"><h3>${esc(o.cliente || '')}<span class="mono">${esc(LZ.numero(o))}</span></h3><span class="sp"></span>
      <span class="cons">consegna ${LZ.giorno(o) ? FT.fmtDataBreve(LZ.giorno(o)) : '—'}</span></div>`;
    const es = st.esiti[id];
    if (es) {
      const cls = es.stato === 'ok' ? 'ok' : es.stato === 'err' ? 'err' : 'att';
      const ico = es.stato === 'ok' ? 'circle-check' : es.stato === 'err' ? 'circle-x' : es.stato === 'via' ? 'loader-circle' : 'clock';
      return `<div class="lz-blocco">${testa}<div class="lz-blocco-corpo">
        <div class="lz-esito ${cls}"><span class="${es.stato === 'via' ? 'lz-gira' : ''}" style="display:inline-grid">${LZ.ico(ico)}</span><b>${esc(es.titolo)}</b></div>
        ${es.dett ? `<div class="lz-esito-dett">${es.dett}</div>` : ''}</div></div>`;
    }
    if (st.errori[id]) return `<div class="lz-blocco fuori">${testa}<div class="lz-blocco-corpo"><div class="lz-esito err">${LZ.ico('circle-x')}Non riesco a leggerlo: ${esc(st.errori[id])}</div></div></div>`;
    if (!d) return `<div class="lz-blocco">${testa}<div class="lz-blocco-corpo"><div class="lz-skel" style="height:14px;width:70%"></div><div class="lz-skel" style="height:14px;width:50%"></div></div></div>`;
    if (!parte(d)) return `<div class="lz-blocco fuori">${testa}<div class="lz-blocco-corpo"><div class="lz-voce fuori"><b class="n">—</b><span>Non parte: ${esc(perche(d))}.</span></div></div></div>`;
    const inv = d.invio || {};
    const fuoriScelti = tenutiFuori(id);
    const gia = (inv.da_mandare || []).filter(x => !fuoriScelti.includes(x.codice));
    const nuovi = inv.nuovi_da_creare || [];
    const rifatti = gia.filter(x => x.gia_fatti);
    const esclusi = d.esclusi || [];
    const motivi = Object.entries(esclusi.reduce((m, x) => { (m[x.motivo] = m[x.motivo] || []).push(x.codice); return m; }, {}));
    const df = diffs(id);
    const sc = st.scelte[id] || {};
    let h = `<div class="lz-blocco">${testa}<div class="lz-blocco-corpo">
      <div class="lz-voce"><b class="n">${gia.length}</b><span>già in Lantek: creo l'ordine di produzione con le quantità</span></div>
      ${nuovi.length ? `<div class="lz-voce"><b class="n">${nuovi.length}</b><span>nuovi: creo il pezzo col suo disegno pulito, poi l'ordine<br><span class="mono">${elenco(nuovi.map(x => x.codice))}</span></span></div>` : ''}`;
    if (d.n_in_produzione) h += `<div class="lz-voce fuori"><b class="n">${d.n_in_produzione}</b><span>hanno già l'ordine di produzione in Lantek: non li rimando</span></div>`;
    for (const [m, cod] of motivi) h += `<div class="lz-voce fuori"><b class="n">${cod.length}</b><span>restano fuori (${esc(m)})<br><span class="mono">${elenco(cod)}</span></span></div>`;
    if (fuoriScelti.length) h += `<div class="lz-voce fuori"><b class="n">${fuoriScelti.length}</b><span>restano fuori: li correggi tu in Lantek<br><span class="mono">${elenco(fuoriScelti)}</span></span></div>`;
    if (rifatti.length) h += `<div class="lz-voce"><b class="n">${rifatti.length}</b><span>erano già stati fatti in passato con questo ordine: si rifanno</span></div>`;
    if (Object.keys(df).length) {
      h += `<div class="lz-diffs"><p>Ordine e Lantek non coincidono: quale tagli?</p>
        <small>Se scegli l'ordine, quel codice resta fuori: correggilo in Lantek e rimandalo.</small>
        ${Object.entries(df).map(([c, xs]) => {
          const lt = xs.map(x => `${x.tipo === 'spessore' ? '' : ''}${x.lantek}`).join(' · ');
          const or = xs.map(x => x.ordine).join(' · ');
          const nome = `d-${id}-${c}`;
          return `<div class="lz-diff"><span class="mono" title="${esc(xs.map(x => x.testo).join('\n'))}">${esc(c)}</span>
            <label><input type="radio" name="${esc(nome)}" value="lantek" data-diff="1" data-ordine="${esc(id)}" data-codice="${esc(c)}" ${sc[c] === 'lantek' ? 'checked' : ''}>Lantek ${esc(lt)}</label>
            <label><input type="radio" name="${esc(nome)}" value="ordine" data-diff="1" data-ordine="${esc(id)}" data-codice="${esc(c)}" ${sc[c] === 'ordine' ? 'checked' : ''}>Ordine ${esc(or)}</label></div>`;
        }).join('')}</div>`;
    }
    return h + '</div></div>';
  }

  function disegna() {
    const box = document.querySelector('#lz-lantek .lz-dlg');
    if (!box || !st) return;
    const pronti = st.ids.filter(id => parte(st.dati[id]));
    const mancaScelta = pronti.reduce((s, id) => s + daScegliere(id).length, 0);
    const nOrd = st.ids.length;
    let piede;
    if (st.fase === 'carica') piede = `<span class="sp"></span><button class="lz-btn" type="button" data-k="annulla">Annulla${LZ.tasto('Esc')}</button>
      <button class="lz-btn pri grande" type="button" disabled>Rileggo Lantek…</button>`;
    else if (st.fase === 'conferma') {
      piede = `${mancaScelta ? `<span class="perche">Scegli ${LZ.plur(mancaScelta, 'differenza', 'differenze')} fra ordine e Lantek</span>` : !pronti.length ? '<span class="perche">Nessun ordine pronto da mandare</span>' : ''}
        <span class="sp"></span><button class="lz-btn" type="button" data-k="annulla">Annulla${LZ.tasto('Esc')}</button>
        <button class="lz-btn pri grande" type="button" data-k="manda" ${pronti.length && !mancaScelta ? '' : 'disabled'}>
          Manda a Lantek${pronti.length > 1 ? ` (${pronti.length} ordini)` : ''}${LZ.tasto('Invio')}</button>`;
    } else if (st.fase === 'invio') {
      const fatti = Object.values(st.esiti).filter(e => e.stato === 'ok' || e.stato === 'err').length;
      piede = `<span class="perche" style="color:var(--lz-ink-3)">Non chiudere la pagina: Lantek può metterci qualche minuto.</span><span class="sp"></span>
        <button class="lz-btn pri grande" type="button" disabled><span class="lz-gira" style="display:inline-grid">${LZ.ico('loader-circle')}</span>Sto mandando… (${fatti} di ${st.invio.length})</button>`;
    } else {
      const ok = Object.values(st.esiti).filter(e => e.stato === 'ok').length;
      const ko = Object.values(st.esiti).filter(e => e.stato === 'err').length;
      piede = `<span class="perche" style="color:${ko ? 'var(--lz-red)' : 'var(--lz-green)'}">${ko ? `${ok} riusciti, ${ko} non riusciti` : 'Tutto fatto. Adesso lanciali dal MES come al solito.'}</span>
        <span class="sp"></span><button class="lz-btn pri grande" type="button" data-k="annulla">Chiudi${LZ.tasto('Invio')}</button>`;
    }
    const tit = st.fase === 'fatto' ? 'Mandato a Lantek' : `Manda a Lantek${nOrd > 1 ? ` ${nOrd} ordini` : ''}`;
    const sotto = st.fase === 'carica' ? 'Guardo cosa c\'è già in Lantek…'
      : st.fase === 'fatto' ? 'Ho ricontrollato in Lantek cosa c\'è davvero.'
        : 'Controlla il riassunto: niente parte prima di «Manda a Lantek».';
    LZ.metti(box, `<div class="lz-dlg-testa"><h2 id="lz-lt-tit">${esc(tit)}</h2><p>${esc(sotto)}</p></div>
      <div class="lz-dlg-corpo">${st.ids.map(blocco).join('')}</div>
      <div class="lz-dlg-piede">${piede}</div>`, box.querySelector('.lz-dlg-corpo'));
  }

  function clic(e) {
    const b = e.target.closest('[data-k]');
    if (!b || b.disabled) return;
    if (b.dataset.k === 'annulla') K.chiudi();
    else if (b.dataset.k === 'manda') manda();
  }

  // ── Invio, un ordine alla volta ─────────────────────────────────────
  async function manda() {
    if (!st || st.fase !== 'conferma') return;
    const ids = st.ids.filter(id => parte(st.dati[id]) && !daScegliere(id).length);
    if (!ids.length) return;
    st.fase = 'invio';
    st.invio = ids;
    for (const id of ids) st.esiti[id] = { stato: 'attesa', titolo: 'In attesa' };
    disegna();
    S.occupato++;
    try {
      for (const id of ids) {
        if (!st) return;
        st.esiti[id] = { stato: 'via', titolo: 'Lantek sta creando pezzi e ordini…' };
        disegna();
        st.esiti[id] = await mandaUno(id);
        disegna();
      }
    } finally {
      S.occupato = Math.max(0, S.occupato - 1);
    }
    if (!st) return;
    st.fase = 'fatto';
    disegna();
    const c = document.querySelector('#lz-lantek [data-k="annulla"]');
    if (c) c.focus();
  }

  async function mandaUno(id) {
    const d = st.dati[id];
    const inv = d.invio || {};
    const fuori = tenutiFuori(id).filter(c => (inv.codici || []).includes(c));
    const corpo = { codici: (inv.codici || []).filter(c => !fuori.includes(c)), nuovi: inv.nuovi || [], esclusi: fuori };
    const r = await LZ.post(`/api/orders/${encodeURIComponent(id)}/lantek-invia`, corpo);
    const x = r.d || {};
    if (!r.ok || !x.success) {
      const cambiato = x.codice === 'cambiato';
      return { stato: 'err', titolo: cambiato ? 'Non mandato: l\'elenco è cambiato nel frattempo' : 'Non mandato a Lantek',
               dett: `${esc(x.error || 'errore del server')}${x.pezzi_creati ? `<br>Pezzi nuovi creati prima dell'errore: ${x.pezzi_creati}.` : ''}<br>Riapri l'ordine e premi Ricontrolla: quello che è già in Lantek non si rimanda.` };
    }
    const rap = x.rapporto || {};
    const tutto = !rap.con_errore && x.verificati === x.mandati && x.pezzi_creati === x.pezzi_nuovi;
    const righe = [];
    if (x.pezzi_nuovi) righe.push(`${x.pezzi_creati} di ${x.pezzi_nuovi} pezzi nuovi creati col disegno`);
    righe.push(`${x.verificati} di ${x.mandati} ordini di produzione, ricontrollati in Lantek`);
    const errori = (rap.errori || []).map(e => `<li><span class="mono">${esc(e.pezzo || '?')}</span> ${esc(e.messaggio || 'errore')}</li>`)
      .concat((x.pezzi_non_creati || []).map(c => `<li><span class="mono">${esc(c)}</span> pezzo non creato</li>`))
      .concat((x.mancanti || []).filter(c => !(rap.errori || []).some(e => e.pezzo === c)).map(c => `<li><span class="mono">${esc(c)}</span> ordine di produzione non trovato in Lantek</li>`));
    if (!tutto) {
      return { stato: 'err', titolo: 'Lantek non ha creato tutto: l\'ordine resta da mettere in Lantek',
               dett: righe.map(esc).join('<br>') + (errori.length ? `<ul>${errori.join('')}</ul>` : '') + 'Riapri l\'ordine e premi Ricontrolla.' };
    }
    if (fuori.length) {
      return { stato: 'ok', titolo: 'Mandato, tranne i codici che correggi tu',
               dett: righe.map(esc).join('<br>') + `<br>Restano da mandare: <span class="mono">${elenco(fuori)}</span>. L'ordine resta fra quelli da mettere in Lantek.` };
    }
    const imp = await LZ.post(`/api/orders/${encodeURIComponent(id)}/importato`, { annulla: false });
    return { stato: 'ok', titolo: 'Fatto: l\'ordine è in Lantek',
             dett: righe.map(esc).join('<br>') + (imp.ok ? '<br>Ora è fra quelli da tagliare.' : '<br><b>Non sono riuscito a segnarlo «In Lantek»: fallo dal menu ⋯ dell\'ordine.</b>') };
  }

  /** Tasti della finestra. true = gestito. */
  K.tasto = function (e) {
    if (!st) return false;
    if (e.key === 'Escape') { K.chiudi(); return true; }
    if (e.key === 'Enter') {
      if (e.target && e.target.matches && e.target.matches('input[type=radio]')) return true;
      if (st.fase === 'conferma') { const b = document.querySelector('#lz-lantek [data-k="manda"]'); if (b && !b.disabled) manda(); }
      else if (st.fase === 'fatto') K.chiudi();
      return true;
    }
    return false;
  };
})();
