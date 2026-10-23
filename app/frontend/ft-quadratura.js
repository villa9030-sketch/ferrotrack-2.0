/* La QUADRATURA dell'ordine (backend/quadratura.py, 23/10/2026): tutto
   quello che il cliente ha ordinato arriva al laser?

   Una riga sola: "Quadratura: tutto torna" (grigia, normale) oppure
   "Non quadra: 3 problemi" (rossa) che si apre su una tabella
   codice / ordinato / FerroTrack / Lantek / cosa fare. Niente pillole,
   colore solo per i problemi (regole di design, 16/10).

   FTQuad.monta(el, id)         riga dentro un elemento (ufficio)
   FTQuad.get(id, attendi)      {pronto, quadratura} dal server
   FTQuad.html(q, opz)          la riga (e la tabella se aperta)
   FTQuad.prima(ids, azione)    PRIMA di mandare a Lantek / segnare il taglio:
                                se un ordine non quadra, l'elenco di cosa non
                                arriverebbe al laser e la spunta «Ho
                                controllato» (va nello storico). Promise<bool>. */
(function () {
  'use strict';
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const Q = window.FTQuad = { aperti: new Set(), ultimi: {} };

  // ── stile (qui, per non toccare i fogli delle pagine) ─────────────────
  const css = `
.ftq { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px; margin: 0 0 8px; font-size: 13px; color: var(--ft-ink-3, #5b6472); }
.ftq-t { font: 600 13px var(--ft-font, inherit); color: var(--ft-ink-2, #333); background: none; border: 0; padding: 0; cursor: default; }
button.ftq-t { cursor: pointer; text-decoration: underline; text-underline-offset: 3px; }
.ftq.err .ftq-t { color: var(--ft-err, #b42318); }
.ftq.att .ftq-t { color: #9a5b00; font-weight: 400; }
.ftq small { font-size: 12px; color: var(--ft-ink-3, #5b6472); }
.ftq-tab { width: 100%; border-collapse: collapse; font-size: 13px; margin: 4px 0 0; }
.ftq-tab th { text-align: left; font-weight: 600; font-size: 12px; color: var(--ft-ink-3, #5b6472); padding: 4px 8px; border-bottom: 1px solid var(--ft-line, #dde1e7); }
.ftq-tab td { padding: 6px 8px; border-bottom: 1px solid var(--ft-line, #dde1e7); vertical-align: top; }
.ftq-tab .n { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.ftq-tab th.n { text-align: right; }
.ftq-tab .cod { font-family: var(--ft-mono, ui-monospace, monospace); font-weight: 600; white-space: nowrap; }
.ftq-tab .cod small { display: block; font-family: var(--ft-font, inherit); font-weight: 400; color: var(--ft-ink-3, #5b6472); }
.ftq-tab .cosa b { color: var(--ft-err, #b42318); font-weight: 600; }
.ftq-tab .cosa small { display: block; color: var(--ft-ink-3, #5b6472); }
.ftq-scorre { width: 100%; max-height: 360px; overflow: auto; }
.ftq-gate .ftq-ord { margin: 12px 0 0; }
.ftq-gate .ftq-ord h4 { margin: 0 0 4px; font: 600 14px var(--ft-font, inherit); }
.ftq-gate .ftq-ord h4 span { font-family: var(--ft-mono, monospace); font-weight: 400; margin-left: 8px; }
.ftq-gate label.ftq-ok { display: flex; gap: 8px; align-items: flex-start; margin: 8px 0 0; font-size: 14px; cursor: pointer; }
.ftq-gate label.ftq-ok input { margin-top: 3px; }
.lz-quad-mini { flex: none; font-size: 12px; font-weight: 600; color: var(--lz-red, var(--ft-err, #b42318)); }
.lz-cli:has(.lz-quad-mini) b { flex: 0 1 auto; min-width: 48px; }
.lz-main.con-ordine .lz-cli:has(.lz-quad-mini) b { min-width: 16px; }
.ftq-gate .ftq-piu { font-size: 12px; color: var(--ft-ink-3, #5b6472); margin: 4px 0 0; }`;
  const st = document.createElement('style');
  st.textContent = css;
  document.head.appendChild(st);

  // ── dati ──────────────────────────────────────────────────────────────
  Q.get = async function (id, attendi) {
    try {
      const r = await fetch(`/api/orders/${encodeURIComponent(id)}/quadratura${attendi ? '?attendi=1' : ''}`, { credentials: 'same-origin' });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.success) return { errore: d.error || `errore ${r.status}` };
      if (d.quadratura) Q.ultimi[id] = d.quadratura;
      return d;
    } catch (e) {
      return { errore: 'il server non risponde' };
    }
  };

  const num = v => (v == null || v === '' ? '—' : esc(window.FT && FT.numero ? FT.numero(v) : v));
  const ora = iso => { const s = String(iso || ''); return s.length >= 16 ? s.slice(11, 16) : ''; };

  function lantekCella(r) {
    if (r.lantek == null) return '—';
    const f = Number(r.lantek_fatti || 0);
    return `${num(r.lantek)}${f ? `<small> (${num(f)} fatti)</small>` : ''}`;
  }

  /** Le righe con un problema: codice, ordinato, FerroTrack, Lantek, cosa fare. */
  Q.tabella = function (q, max) {
    const righe = (q.problemi || []);
    const vis = max ? righe.slice(0, max) : righe;
    const ord = (q.problemi_ordine || []).map(p => `<tr><td class="cod">—</td><td class="n">—</td><td class="n">—</td><td class="n">—</td>
      <td class="cosa"><b>${esc(p.parola)}</b>: ${esc(p.cosa)}<small>${esc(p.testo)}</small></td></tr>`).join('');
    return `<div class="ftq-scorre"><table class="ftq-tab"><thead><tr><th>Codice</th><th class="n">Ordinato</th><th class="n">FerroTrack</th>
      <th class="n">Lantek</th><th>Cosa fare</th></tr></thead><tbody>${ord}${vis.map(r => {
        const p = r.problemi || [];
        const sotto = r.assieme ? `<small>in ${esc(r.assieme)}</small>` : '';
        return `<tr><td class="cod">${esc(r.codice)}${sotto}</td><td class="n">${num(r.ordinato)}</td><td class="n">${num(r.ferrotrack)}</td>
          <td class="n">${lantekCella(r)}</td>
          <td class="cosa">${p.map(x => `<b>${esc(x.parola)}</b>: ${esc(x.cosa)}<small>${esc(x.testo)}</small>`).join('')}</td></tr>`;
      }).join('')}</tbody></table></div>${max && righe.length > max ? `<p class="ftq-piu">e altri ${righe.length - max}</p>` : ''}`;
  };

  /** La riga della quadratura. opz: {id, caricando, errore} */
  Q.html = function (q, opz) {
    opz = opz || {};
    const id = opz.id || (q && q.order_id) || '';
    if (!q) {
      if (opz.errore) return `<div class="ftq att" data-ftq="${esc(id)}"><span class="ftq-t">Quadratura non controllata: ${esc(opz.errore)}</span></div>`;
      return `<div class="ftq" data-ftq="${esc(id)}"><span class="ftq-t" style="font-weight:400">Quadratura: la sto controllando…</span></div>`;
    }
    const dopo = opz.caricando ? '<small>la ricontrollo…</small>' : (q.calcolato_il ? `<small>controllata alle ${esc(ora(q.calcolato_il))}</small>` : '');
    if (q.esito === 'QUADRA') {
      return `<div class="ftq" data-ftq="${esc(id)}"><span class="ftq-t" title="Tutto quello che il cliente ha ordinato è in FerroTrack e in Lantek con le quantità giuste">${esc(q.testo)}</span>${dopo}</div>`;
    }
    if (q.esito === 'NON_VERIFICABILE') {
      return `<div class="ftq att" data-ftq="${esc(id)}"><span class="ftq-t">${esc(q.testo)}</span>${dopo}</div>`;
    }
    const aperto = Q.aperti.has(id);
    const parz = (q.non_verificabile || []).length ? `<small>${esc('Non controllato tutto: ' + q.non_verificabile[0])}</small>` : '';
    return `<div class="ftq err" data-ftq="${esc(id)}"><button class="ftq-t" type="button" data-ftq-apri="${esc(id)}" data-fuoco="ftq-${esc(id)}" aria-expanded="${aperto}"
        title="${aperto ? 'Nascondi' : 'Mostra'} i codici che non tornano">${esc(q.testo)}</button>${parz}${dopo}
      ${aperto ? Q.tabella(q) : ''}</div>`;
  };

  // apri / chiudi la tabella (in ogni pagina)
  document.addEventListener('click', e => {
    const b = e.target.closest('[data-ftq-apri]');
    if (!b) return;
    e.preventDefault();
    const id = b.dataset.ftqApri;
    if (Q.aperti.has(id)) Q.aperti.delete(id); else Q.aperti.add(id);
    if (typeof Q.ridisegna === 'function') { Q.ridisegna(id); return; }
    const box = b.closest('[data-ftq]');
    if (box && Q.ultimi[id]) box.outerHTML = Q.html(Q.ultimi[id], { id });
  });

  /** La riga dentro un elemento, aggiornata finche' il calcolo non e' pronto (ufficio). */
  Q.monta = async function (el, id) {
    if (!el) return;
    el.dataset.ftqPer = id;
    let giri = 0;
    while (el.isConnected && el.dataset.ftqPer === id) {
      const d = await Q.get(id, false);
      if (!el.isConnected || el.dataset.ftqPer !== id) return;
      if (d.errore) { el.innerHTML = Q.html(null, { id, errore: d.errore }); return; }
      el.innerHTML = Q.html(d.quadratura, { id, caricando: !d.pronto });
      if (d.pronto || ++giri > 30) return;
      await new Promise(r => setTimeout(r, 3000));
    }
  };

  // ── prima di mandare / tagliare ───────────────────────────────────────
  const AZIONE = {
    lantek: ['Manda a Lantek lo stesso', 'Prima di mandare a Lantek'],
    pacco_lantek: ['Manda a Lantek lo stesso', 'Prima di mandare a Lantek'],
    taglio: ['Segna tagliato lo stesso', 'Prima di segnare il taglio'],
    pacco_taglio: ['Segna tagliato lo stesso', 'Prima di segnare il taglio'],
  };

  /** ids: ordini; azione: lantek | taglio | pacco_lantek | pacco_taglio.
      opz.nome(id) -> "A 001252 · DECA" per i titoli. Promise<bool>. */
  Q.prima = function (ids, azione, opz) {
    opz = opz || {};
    ids = (ids || []).filter(Boolean);
    if (!ids.length) return Promise.resolve(true);
    return new Promise(res => {
      const bg = document.createElement('div');
      bg.className = 'ft-modal-bg open ftq-gate';
      bg.innerHTML = `<div class="ft-modal lg" role="dialog" aria-modal="true" aria-labelledby="ftq-g-t">
        <div class="ft-modal-head"><div><h3 class="ft-modal-title" id="ftq-g-t">Controllo la quadratura…</h3>
          <p class="ft-modal-sub">Guardo se tutto quello che il cliente ha ordinato arriva al laser.</p></div></div>
        <div class="ft-modal-body"></div>
        <div class="ft-modal-foot"><button class="ft-btn" type="button" data-v="0">Annulla</button></div></div>`;
      let finito = false;
      const chiudi = v => {
        if (finito) return;
        finito = true;
        document.removeEventListener('keydown', tasti, true);
        bg.remove();
        res(v);
      };
      const tasti = e => {
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); chiudi(false); }
        // Invio: solo sui pulsanti della finestra (mai su quelli della pagina sotto)
        else if (e.key === 'Enter' && !(e.target && bg.contains(e.target) && e.target.matches('button'))) { e.preventDefault(); e.stopPropagation(); }
      };
      document.addEventListener('keydown', tasti, true);
      // se il controllo e' veloce (gia' calcolato) la finestra non si vede nemmeno
      bg.style.visibility = 'hidden';
      setTimeout(() => {
        if (finito || !bg.style.visibility) return;
        bg.style.visibility = '';
        bg.querySelector('button[data-v="0"]').focus();
      }, 400);
      if (document.activeElement && document.activeElement.blur) document.activeElement.blur();
      document.body.appendChild(bg);
      let nonQ = [];
      bg.addEventListener('click', async e => {
        if (e.target === bg) return chiudi(false);
        const b = e.target.closest('button[data-v]');
        if (!b || b.disabled) return;
        if (b.dataset.v === '0') return chiudi(false);
        b.disabled = true;
        // nello storico: chi ha deciso di andare avanti lo stesso, e per cosa
        await Promise.all(nonQ.map(q => fetch(`/api/orders/${encodeURIComponent(q.order_id)}/quadratura/controllato`, {
          method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ azione, codici: (q.problemi || []).map(r => r.codice) }),
        }).catch(() => null)));
        chiudi(true);
      });
      bg.addEventListener('change', () => {
        const ok = bg.querySelector('button[data-v="1"]');
        if (ok) ok.disabled = !!bg.querySelector('input[data-ftq-ok]:not(:checked)');
      });
      (async () => {
        const esiti = await Promise.all(ids.map(id => Q.get(id, true)));
        if (finito) return;
        nonQ = esiti.map(d => d && d.quadratura).filter(q => q && q.esito === 'NON_QUADRA');
        const errori = esiti.filter(d => d && d.errore);
        if (!nonQ.length) {
          // tutto torna (o non verificabile: lo dice la riga sull'ordine) -> si va avanti
          if (errori.length && window.FT && FT.toast) FT.toast('Quadratura non controllata: ' + errori[0].errore, 'warn');
          chiudi(true);
          return;
        }
        const [ok, sopra] = AZIONE[azione] || AZIONE.lantek;
        const n = nonQ.reduce((s, q) => s + (q.n_problemi || 0), 0);
        bg.querySelector('#ftq-g-t').textContent = 'Questi pezzi non arriverebbero al laser';
        bg.querySelector('.ft-modal-sub').textContent = `${sopra}: ${nonQ.length === 1 ? "l'ordine non quadra" : `${nonQ.length} ordini non quadrano`} (${n} ${n === 1 ? 'problema' : 'problemi'}). Controlla ogni riga, poi spunta «Ho controllato».`;
        bg.querySelector('.ft-modal-body').innerHTML = nonQ.map(q => `<section class="ftq-ord">
            <h4>${esc(opz.nome ? opz.nome(q.order_id) : (q.cliente || ''))}<span>${esc(q.numero || '')}</span></h4>
            ${Q.tabella(q)}
            <label class="ftq-ok"><input type="checkbox" data-ftq-ok="${esc(q.order_id)}">
              Ho controllato: so che questi pezzi mancano o sono diversi, vado avanti lo stesso</label></section>`).join('');
        bg.querySelector('.ft-modal-foot').innerHTML = `<button class="ft-btn" type="button" data-v="0">Annulla</button>
          <button class="ft-btn danger" type="button" data-v="1" disabled>${esc(ok)}</button>`;
        bg.style.visibility = '';
        const primo = bg.querySelector('input[data-ftq-ok]');
        if (primo) primo.focus();
      })();
    });
  };
})();
