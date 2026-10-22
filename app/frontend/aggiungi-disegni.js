/* "Aggiungi disegni / assiemi mancanti" a un ordine gia' caricato
   (DECA A 001252: caricato senza le sottocartelle degli assiemi).
   Si trascina la cartella del cliente (come il pacchetto d'ordine), il server
   prepara i pezzi che mancano e mostra il piano; si aggiunge solo con
   "Aggiungi all'ordine". Usato dall'Ufficio (pannello dell'ordine) e dal
   laser (menu ⋯ dell'ordine). */
(function () {
  'use strict';
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = v => (v == null || v === '' ? '?' : String(v).replace('.', ','));

  /* il lettore delle cartelle trascinate dell'Ufficio, se c'e' (impiegata.html) */
  async function leggiVoci(dt) {
    if (typeof window.leggiVociTrascinate === 'function') return window.leggiVociTrascinate(dt);
    const voci = Array.from((dt && dt.items) || []).map(it => it.webkitGetAsEntry && it.webkitGetAsEntry()).filter(Boolean);
    if (!voci.length) return Array.from((dt && dt.files) || []);
    const out = [];
    const leggiDir = dir => new Promise(res => {
      const r = dir.createReader(), tutte = [];
      const giro = () => r.readEntries(b => { if (!b.length) return res(tutte); tutte.push(...b); giro(); }, () => res(tutte));
      giro();
    });
    const visita = async (e, base) => {
      if (e.isFile) {
        const f = await new Promise(res => e.file(res, () => res(null)));
        if (f) { try { f._relPath = base + f.name; } catch (_) {} out.push(f); }
      } else if (e.isDirectory) for (const x of await leggiDir(e)) await visita(x, base + e.name + '/');
    };
    for (const v of voci) await visita(v, '');
    return out;
  }

  let box = null, stato = null;

  function chiudi() {
    if (stato && stato.tid && !stato.fatto) {
      fetch(`/api/orders/${stato.oid}/aggiungi-disegni/${stato.tid}`, { method: 'DELETE' }).catch(() => {});
    }
    if (box) box.remove();
    box = null; stato = null;
  }

  function guscio(titolo, corpo, piede) {
    if (!box) {
      box = document.createElement('div');
      box.className = 'agd-bg';
      box.setAttribute('role', 'dialog');
      box.setAttribute('aria-modal', 'true');
      box.addEventListener('click', e => { if (e.target === box) chiudi(); });
      document.body.appendChild(box);
    }
    box.innerHTML = `<div class="agd-win"><div class="agd-head"><b>${esc(titolo)}</b>
      <button type="button" class="agd-x" aria-label="Chiudi" data-agd="chiudi">×</button></div>
      <div class="agd-body">${corpo}</div><div class="agd-foot">${piede || ''}</div></div>`;
    box.querySelectorAll('[data-agd="chiudi"]').forEach(b => b.addEventListener('click', chiudi));
  }

  function chiedi() {
    guscio('Aggiungi disegni / assiemi mancanti',
      `<p>Trascina qui la <b>cartella del cliente</b> (PDF d'ordine + disegni, con le sottocartelle degli assiemi), o sceglila.
       Si aggiunge solo quello che manca all'ordine: i codici già caricati o già mandati a Lantek non si ripetono.</p>
       <label class="agd-drop" tabindex="0">Trascina la cartella qui, o clicca per sceglierla
       <input type="file" webkitdirectory multiple hidden></label>
       <p class="agd-err" id="agd-err"></p>`,
      `<button type="button" class="agd-btn" data-agd="chiudi">Annulla</button>`);
    const zona = box.querySelector('.agd-drop'), input = zona.querySelector('input');
    ['dragenter', 'dragover'].forEach(ev => zona.addEventListener(ev, e => { e.preventDefault(); zona.classList.add('su'); }));
    ['dragleave', 'dragend'].forEach(ev => zona.addEventListener(ev, e => { e.preventDefault(); zona.classList.remove('su'); }));
    zona.addEventListener('drop', async e => { e.preventDefault(); zona.classList.remove('su'); manda(await leggiVoci(e.dataTransfer)); });
    input.addEventListener('change', () => manda(Array.from(input.files || [])));
  }

  async function manda(files) {
    files = (files || []).filter(f => !/^\.|^thumbs\.db$|^desktop\.ini$/i.test(f.name));
    if (!files.length) return;
    guscio('Aggiungi disegni / assiemi mancanti', '<p>Leggo i disegni e li controllo come all\'import… (qualche minuto per tanti disegni)</p>', '');
    const fd = new FormData();
    for (const f of files) { fd.append('files', f); fd.append('paths', f._relPath || f.webkitRelativePath || f.name); }
    let r, d = {};
    try { r = await fetch(`/api/orders/${stato.oid}/aggiungi-disegni`, { method: 'POST', body: fd }); d = await r.json(); } catch (_) { r = null; }
    if (!r || !r.ok || d.success === false) {
      chiedi();
      box.querySelector('#agd-err').textContent = (d && d.error) || 'Il server non risponde: riprova.';
      return;
    }
    stato.tid = d.tecnico_id;
    piano(d);
  }

  function piano(p) {
    const righe = [];
    for (const a of p.assiemi || []) {
      righe.push(`<tr class="agd-ass"><td colspan="6"><b>${esc(a.codice)}</b> × ${a.quantita}${a.da_riga ? ' <span class="agd-nota">(la riga dell\'ordine diventa l\'assieme)</span>' : ''} · ${a.pezzi.length} pezzi</td></tr>`);
      for (const x of a.pezzi) righe.push(rigaPezzo(x));
    }
    for (const x of p.sciolti || []) righe.push(rigaPezzo(x));
    const uniti = (p.uniti || []).map(u => `<li>${esc(u.codice)} (assieme ${esc(u.assieme || '—')}) è già nell'ordine come ${esc(u.gia_come)}${u.mandato_lantek ? ', già mandato a Lantek' : ''}: non si aggiunge</li>`).join('');
    const bloc = (p.bloccati || []).length;
    guscio('Cosa si aggiunge all\'ordine',
      `<p><b>${p.n_nuovi}</b> pezzi nuovi${bloc ? `, <span class="agd-anom">${bloc} con i pezzi per assieme da confermare</span> (non vanno a Lantek finché non li scrivi nella pagina laser)` : ''}.
       ${p.gia_presenti ? `${p.gia_presenti} disegni del pacchetto sono già nell'ordine e restano com'erano.` : ''}</p>
       <table class="agd-tab"><thead><tr><th>Codice</th><th>Per assieme</th><th>Totale</th><th>Materiale</th><th>Spessore</th><th>Controllo</th></tr></thead>
       <tbody>${righe.join('')}</tbody></table>
       ${uniti ? `<ul class="agd-uniti">${uniti}</ul>` : ''}
       ${(p.fuori_ordine || []).length ? `<p class="agd-nota">Disegni che l'ordine non chiede (non aggiunti): ${p.fuori_ordine.map(esc).join(', ')}</p>` : ''}
       <p class="agd-err" id="agd-err"></p>`,
      `<button type="button" class="agd-btn" data-agd="chiudi">Annulla</button>
       <button type="button" class="agd-btn primario" id="agd-ok">Aggiungi all'ordine</button>`);
    box.querySelector('#agd-ok').addEventListener('click', conferma);
  }

  function rigaPezzo(x) {
    const ver = x.verifica === 'verificato' ? 'ok' : (x.verifica ? 'da guardare' : '—');
    return `<tr${x.da_confermare ? ' class="agd-anom"' : ''}><td>${esc(x.codice)}</td>
      <td>${x.da_confermare ? 'da confermare' : x.per_assieme}</td><td>${x.da_confermare ? '—' : x.totale}</td>
      <td>${esc(x.materiale || '?')}</td><td>${num(x.spessore_mm)} mm</td>
      <td${x.verifica && x.verifica !== 'verificato' ? ' class="agd-anom"' : ''}>${ver}</td></tr>`;
  }

  async function conferma() {
    const b = box.querySelector('#agd-ok');
    b.disabled = true; b.textContent = 'Aggiungo…';
    let r, d = {};
    try { r = await fetch(`/api/orders/${stato.oid}/aggiungi-disegni/${stato.tid}/conferma`, { method: 'POST' }); d = await r.json(); } catch (_) { r = null; }
    if (!r || !r.ok || d.success === false) {
      b.disabled = false; b.textContent = 'Aggiungi all\'ordine';
      box.querySelector('#agd-err').textContent = (d && d.error) || 'Il server non risponde: riprova.';
      return;
    }
    stato.fatto = true;
    const dopo = stato.dopo;
    guscio('Aggiunti all\'ordine',
      `<p><b>${d.aggiunti}</b> pezzi aggiunti, assiemi: ${(d.assiemi || []).map(esc).join(', ') || '—'}.</p>
       ${(d.bloccati || []).length ? `<p class="agd-anom">Pezzi per assieme da confermare: ${d.bloccati.map(esc).join(', ')}</p>` : ''}
       ${(d.lantek_da_guardare || []).length ? `<p>Da preparare in Lantek (disegno da guardare): ${d.lantek_da_guardare.map(esc).join(', ')}</p>` : ''}
       <p>"Manda a Lantek" ora propone solo i codici nuovi.</p>`,
      `<button type="button" class="agd-btn primario" data-agd="chiudi">Chiudi</button>`);
    if (typeof dopo === 'function') try { dopo(d); } catch (_) {}
  }

  function stile() {
    if (document.getElementById('agd-stile')) return;
    const s = document.createElement('style');
    s.id = 'agd-stile';
    s.textContent = `.agd-bg{position:fixed;inset:0;background:rgba(15,18,30,.45);z-index:9000;display:flex;align-items:flex-start;justify-content:center;padding:5vh 16px;overflow:auto}
.agd-win{background:var(--ft-bg,#fff);color:var(--ft-ink,#1b1d24);width:min(860px,100%);border-radius:10px;box-shadow:0 12px 40px rgba(0,0,0,.25);display:flex;flex-direction:column;max-height:90vh}
.agd-head{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid var(--ft-line,#e3e5ea);font-size:16px}
.agd-x{border:0;background:none;font-size:22px;cursor:pointer;color:inherit}
.agd-body{padding:14px 18px;overflow:auto}.agd-foot{display:flex;gap:8px;justify-content:flex-end;padding:12px 18px;border-top:1px solid var(--ft-line,#e3e5ea)}
.agd-drop{display:block;border:2px dashed var(--ft-line,#c9ccd4);border-radius:8px;padding:34px 16px;text-align:center;cursor:pointer;margin:10px 0}
.agd-drop.su{border-color:#2f2a6b;background:rgba(47,42,107,.05)}
.agd-tab{width:100%;border-collapse:collapse;font-size:13px;margin:8px 0}.agd-tab th,.agd-tab td{text-align:left;padding:4px 8px;border-bottom:1px solid var(--ft-line,#eceef2)}
.agd-tab th{font-weight:600;color:var(--ft-ink-2,#555)}.agd-ass td{background:var(--ft-bg-2,#f5f6f8);padding-top:8px}
.agd-anom{color:#a3361c}.agd-nota{color:var(--ft-ink-2,#666);font-size:12px}.agd-err{color:#a3361c;min-height:1em}
.agd-btn{border:1px solid var(--ft-line,#c9ccd4);background:var(--ft-bg,#fff);color:inherit;border-radius:6px;padding:7px 14px;cursor:pointer;font:inherit}
.agd-btn.primario{background:#2f2a6b;border-color:#2f2a6b;color:#fff}.agd-btn:disabled{opacity:.6}
.agd-uniti{font-size:13px;margin:6px 0 0 18px}`;
    document.head.appendChild(s);
  }

  window.FTAggiungiDisegni = {
    apri(orderId, opzioni) {
      chiudi();
      stile();
      stato = { oid: orderId, tid: null, fatto: false, dopo: (opzioni || {}).dopo };
      chiedi();
    },
  };
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && box) chiudi(); });
})();
