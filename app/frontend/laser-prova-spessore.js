/* Pagina laser — PROVA della correzione dello spessore in Lantek
   (backend/lantek_prova_spessore.py, 28/10/2026).

   Una finestra a passi, sul solo pezzo di prova FT-PROVA-30:
     1. crea FT-PROVA-30 a 2 mm      2. reimportalo a 3 mm
   Prima e dopo si legge l'archivio di Lantek e si dice cosa e' successo:
   aggiornato, doppione o rifiutato. Ogni passo parte solo col clic. */
(function () {
  'use strict';
  const LZ = window.LZ;
  const esc = LZ.esc;
  const mm = v => (v == null ? '—' : FT.numero(v, v % 1 ? 1 : 0) + ' mm');

  function tabella(pezzi) {
    if (!pezzi || !pezzi.length) return '<p style="margin:4px 0 0;color:var(--ft-ink-3)">Nessun pezzo FT-PROVA-30 in Lantek.</p>';
    return `<table class="ftq-tab"><thead><tr><th>Codice in Lantek</th><th>Materiale</th><th class="n">Spessore</th><th>Creato</th></tr></thead>
      <tbody>${pezzi.map(p => `<tr><td class="cod">${esc(p.codice)}</td><td>${esc(p.materiale || '—')}</td>
        <td class="n">${mm(p.spessore)}</td><td>${esc(String(p.creato || '').slice(0, 16))}</td></tr>`).join('')}</tbody></table>`;
  }

  async function apri() {
    const bg = document.createElement('div');
    bg.className = 'ft-modal-bg open';
    bg.innerHTML = `<div class="ft-modal lg" role="dialog" aria-modal="true" aria-labelledby="lz-ps-t">
      <div class="ft-modal-head"><div><h3 class="ft-modal-title" id="lz-ps-t">Prova: correggere lo spessore in Lantek</h3>
        <p class="ft-modal-sub">Leggo Lantek…</p></div></div>
      <div class="ft-modal-body"></div>
      <div class="ft-modal-foot"><button class="ft-btn" type="button" data-ps="no">Chiudi</button></div></div>`;
    document.body.appendChild(bg);
    const sub = bg.querySelector('.ft-modal-sub'), body = bg.querySelector('.ft-modal-body'), foot = bg.querySelector('.ft-modal-foot');
    let chiusa = false, passo = null;
    const tasti = e => { if (e.key === 'Escape') { e.preventDefault(); chiudi(); } };
    const chiudi = () => { chiusa = true; bg.remove(); document.removeEventListener('keydown', tasti, true); };
    document.addEventListener('keydown', tasti, true);

    async function leggi(dopo) {
      const r = await LZ.get('/api/lantek/prova-spessore');
      if (chiusa) return;
      if (!r.ok) { sub.textContent = r.d.error || 'Lantek non si legge'; return; }
      const d = r.d;
      passo = d.prossimo;
      const sp = d.spessori || {};
      const ultimo = (d.storico || []).slice(-1)[0];
      sub.textContent = 'Si prova su un pezzo di prova, mai su un pezzo vero. FerroTrack usa Procesos di Lantek, come per i pezzi nuovi; poi rilegge Lantek.';
      const passi = `<ol style="margin:0 0 12px;padding-left:20px;line-height:1.7">
          <li${passo === 'crea' ? ' style="font-weight:600"' : ''}>Crea ${esc(d.codice)} in Lantek: ${esc(d.materiale)} ${mm(sp.crea)}.</li>
          <li${passo === 'reimporta' ? ' style="font-weight:600"' : ''}>Reimporta lo stesso pezzo a ${mm(sp.reimporta)}.</li>
          <li>Guardi nel MES se il pezzo è stato aggiornato, se c'è un doppione, e se disegno e nesting sono a posto.</li></ol>`;
      const prima = `<p style="margin:0 0 12px"><b>Prima del passo 1 fai la copia del database di Lantek</b> (il comando te l'ho scritto in chat).</p>`;
      const es = dopo || (ultimo && ultimo.esito);
      body.innerHTML = passi + (passo === 'crea' ? prima : '')
        + (es ? `<p style="margin:0 0 8px;font-weight:600;${['doppione', 'rifiutato', 'non_creato', 'altro'].includes(es.parola) ? 'color:var(--ft-err)' : ''}">${esc(es.testo)}</p>` : '')
        + `<h4 style="margin:8px 0 4px;font-size:14px">Adesso in Lantek</h4>${d.foto.ok ? tabella(d.foto.pezzi) : `<p>${esc(d.foto.errore || '')}</p>`}`
        + (d.expert_aperto ? '<p style="margin:12px 0 0;color:var(--ft-err);font-weight:600">Lantek Expert è aperto: chiudilo prima del passo.</p>' : '')
        + (!d.procesos ? '<p style="margin:12px 0 0;color:var(--ft-err)">Procesos di Lantek non trovato su questo PC: la prova si fa solo sul server.</p>' : '');
      foot.innerHTML = '<button class="ft-btn" type="button" data-ps="no">Chiudi</button>'
        + (passo && d.procesos ? `<button class="ft-btn primary" type="button" data-ps="si">${passo === 'crea'
          ? `Passo 1: crea ${esc(d.codice)} a ${mm(sp.crea)}` : `Passo 2: reimporta a ${mm(sp.reimporta)}`}</button>` : '');
      (foot.querySelector('[data-ps="si"]') || foot.querySelector('button')).focus();
    }

    bg.addEventListener('click', async e => {
      if (e.target === bg) return chiudi();
      const b = e.target.closest('button[data-ps]');
      if (!b || b.disabled) return;
      if (b.dataset.ps === 'no') return chiudi();
      b.disabled = true;
      b.textContent = 'Lantek sta importando…';
      sub.textContent = 'Procesos di Lantek sta lavorando: può volerci un minuto. Non chiudere la pagina.';
      const r = await LZ.azione(() => LZ.post('/api/lantek/prova-spessore', { passo }));
      if (chiusa) return;
      if (!r.ok) { FT.toast(r.d.error || 'Passo non riuscito', 'err'); leggi(); return; }
      leggi(r.d.esito);
    });
    leggi();
  }

  document.addEventListener('click', e => {
    if (!e.target.closest('[data-lz-prova-spessore]')) return;
    e.preventDefault();
    apri();
  });
})();
