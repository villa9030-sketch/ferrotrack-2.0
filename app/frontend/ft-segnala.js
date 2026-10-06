/* FerroTrack - pulsante "Segnala errore" su ogni pagina.

   Lo carica ft-accesso.js dopo l'ingresso. Sta nella barra in alto delle
   pagine che ce l'hanno (.ft-topbar-end), altrimenti in basso a sinistra.
   Chi trova un problema scrive due parole; con il testo partono da soli:
   pagina, postazione, persona, ora, browser, schermo e il "diario" della
   pagina (ultimi errori JavaScript e chiamate al server andate male).
*/
(function () {
  'use strict';
  if (window.FTSegnala) return;

  const CSS = `
.fts-btn{display:inline-flex;align-items:center;gap:6px;height:34px;padding:0 12px;border-radius:999px;
  border:1px solid var(--ft-line,#E4E7EC);background:var(--ft-panel,#fff);color:var(--ft-ink-2,#3A4556);
  font:600 13px var(--ft-font,system-ui,sans-serif);cursor:pointer;white-space:nowrap}
.fts-btn:hover{border-color:#F59E0B;color:#B45309;background:#FFFBEB}
.fts-btn svg{width:15px;height:15px;flex:none}
.fts-flottante{position:fixed;left:14px;bottom:calc(14px + var(--ft-fascia-h,0px));z-index:900;
  box-shadow:0 4px 14px rgba(15,23,42,.12);opacity:.85}
.fts-flottante:hover{opacity:1}
.fts-velo{position:fixed;inset:0;z-index:3000;display:grid;place-items:center;padding:16px;
  background:rgba(11,18,32,.45);animation:fts-in .15s ease-out}
.fts-box{width:min(560px,100%);background:#fff;border-radius:18px;padding:24px;
  box-shadow:0 24px 60px rgba(0,0,0,.25);font-family:var(--ft-font,system-ui,sans-serif);color:#101828}
.fts-box h3{margin:0 0 4px;font-size:20px}
.fts-box p{margin:0 0 14px;color:#667085;font-size:14px;line-height:1.45}
.fts-box textarea{width:100%;min-height:130px;box-sizing:border-box;border:2px solid #D0D5DD;border-radius:12px;
  padding:12px 14px;font:16px var(--ft-font,system-ui,sans-serif);resize:vertical}
.fts-box textarea:focus{outline:none;border-color:#F59E0B}
.fts-box small{display:block;margin-top:8px;color:#98A2B3;font-size:12.5px}
.fts-az{display:flex;justify-content:flex-end;gap:10px;margin-top:16px}
.fts-az button{min-height:48px;padding:0 20px;border-radius:12px;font:600 15px var(--ft-font,system-ui,sans-serif);cursor:pointer}
.fts-no{background:#fff;border:1px solid #D0D5DD;color:#344054}
.fts-si{background:#0B1220;border:1px solid #0B1220;color:#fff}
.fts-si[disabled]{opacity:.5;cursor:default}
.fts-err{color:#B42318;font-size:13.5px;margin-top:10px}
@keyframes fts-in{from{opacity:0}to{opacity:1}}
.fts-btn.fts-compatto{width:34px;padding:0;justify-content:center}
.fts-btn.fts-compatto .fts-t{display:none}
@media (max-width:700px){.fts-btn .fts-t{display:none}}`;

  const ICONA = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 22V4"/><path d="M4 4h13l-2 4 2 4H4"/></svg>';

  function stile() {
    if (document.getElementById('fts-stile')) return;
    const s = document.createElement('style');
    s.id = 'fts-stile';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  function esc(t) {
    return String(t == null ? '' : t).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function dettagli() {
    const io = (window.FTA && window.FTA.io) || {};
    return {
      url: location.pathname + location.search + location.hash,
      titolo: document.title,
      ora_locale: new Date().toString(),
      stazione: io.stazione || null,
      dispositivo: (io.dispositivo && io.dispositivo.nome) || null,
      persona: (io.persona && io.persona.nome) || null,
      browser: navigator.userAgent,
      schermo: screen.width + 'x' + screen.height + ' finestra ' + innerWidth + 'x' + innerHeight,
      in_linea: navigator.onLine,
      diario: (window.FTA && window.FTA.diario) ? window.FTA.diario() : [],
    };
  }

  function apri() {
    stile();
    if (document.querySelector('.fts-velo')) return;
    const velo = document.createElement('div');
    velo.className = 'fts-velo';
    velo.innerHTML = `<div class="fts-box" role="dialog" aria-modal="true" aria-labelledby="fts-t">
      <h3 id="fts-t">Segnala un errore</h3>
      <p>Cosa stavi facendo e cosa non va? Bastano due parole, es. "salvo le ore e non succede niente".</p>
      <textarea maxlength="2000" placeholder="Scrivi qui…"></textarea>
      <small>Con la segnalazione partono da soli: questa pagina, la postazione, l'ora e gli ultimi errori tecnici. Niente foto dello schermo.</small>
      <div class="fts-err" hidden></div>
      <div class="fts-az"><button type="button" class="fts-no">Annulla</button><button type="button" class="fts-si">Invia</button></div></div>`;
    document.body.appendChild(velo);
    const ta = velo.querySelector('textarea');
    const si = velo.querySelector('.fts-si');
    const err = velo.querySelector('.fts-err');
    setTimeout(() => ta.focus(), 50);
    const chiudi = () => velo.remove();
    velo.addEventListener('click', e => { if (e.target === velo) chiudi(); });
    velo.querySelector('.fts-no').addEventListener('click', chiudi);
    velo.addEventListener('keydown', e => { if (e.key === 'Escape') chiudi(); });
    si.addEventListener('click', async () => {
      const testo = ta.value.trim();
      if (testo.length < 3) { err.textContent = 'Scrivi in due parole cosa non va.'; err.hidden = false; ta.focus(); return; }
      si.disabled = true; si.textContent = 'Invio…'; err.hidden = true;
      let r = null, d = {};
      try {
        r = await fetch('/api/segnalazioni-errore', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ testo, pagina: location.pathname, dettagli: dettagli() }),
        });
        try { d = await r.json(); } catch (_) {}
      } catch (_) { r = null; }
      if (r && r.ok && d.success) {
        chiudi();
        const msg = 'Segnalazione inviata, grazie.';
        if (window.FT && window.FT.toast) window.FT.toast(msg, 'ok');
        else if (window.FTA && window.FTA.avviso) window.FTA.avviso(msg);
        return;
      }
      si.disabled = false; si.textContent = 'Invia';
      err.textContent = (d && d.error) || 'Non sono riuscito a inviarla: il server non risponde. Riprova tra poco.';
      err.hidden = false;
    });
  }

  function metti() {
    if (document.querySelector('.fts-btn')) return;
    stile();
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'fts-btn';
    b.title = 'Segnala un errore';
    b.setAttribute('aria-label', 'Segnala un errore');
    b.innerHTML = ICONA + '<span class="fts-t">Segnala errore</span>';
    b.addEventListener('click', apri);
    // barra in alto: quella comune (.ft-topbar-end) o quella dei preventivi
    const barra = document.querySelector('.ft-topbar-end, .topbar-actions');
    if (!barra) { b.classList.add('fts-flottante'); document.body.appendChild(b); return; }
    barra.insertBefore(b, barra.firstChild);
    // Se con il pulsante le schede non ci stanno piu' (ufficio: "Backup"
    // tagliato), resta solo la bandierina; la scritta e' nel suggerimento.
    const nav = (barra.closest('header, .ft-topbar, .topbar') || document).querySelector('.ft-tabs, nav');
    const adatta = () => {
      b.classList.remove('fts-compatto');
      if (nav && nav.scrollWidth > nav.clientWidth + 1) b.classList.add('fts-compatto');
    };
    adatta();
    window.addEventListener('resize', adatta);
    if (document.fonts && document.fonts.ready) document.fonts.ready.then(adatta);
  }

  window.FTSegnala = { apri, metti, dettagli };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', metti);
  else metti();
})();
