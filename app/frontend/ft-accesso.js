/* FerroTrack — accesso, uguale in ogni pagina.

   Chi sta usando la pagina lo decide il SERVER (backend/accesso.py): il
   dispositivo registrato sta in un cookie che JavaScript non legge, e negli
   uffici la persona e' entrata col suo PIN. Questo file non manda identita':
   aiuta solo la pagina a comportarsi bene.

   Si include nel <head>, PRIMA degli script della pagina:

     <script src="/ft-accesso.js" data-stazioni="ufficio"></script>

   data-stazioni  le stazioni per cui e' fatta la pagina (separate da virgola).
                  Un dispositivo di un'altra stazione viene riportato alla sua.
                  "*" = qualunque dispositivo (es. Amministrazione).
   data-pubblica  pagina che si apre anche senza dispositivo (schermo TV).

   Cosa fa:
   - ogni chiamata /api che risponde 401 (dispositivo non registrato, serve il
     PIN, giornata finita) riporta alla pagina iniziale;
   - 403 "serve il PIN di un amministratore": chiede il PIN qui, sul momento,
     e ripete la richiesta; 403 "questa postazione non puo'": lo dice;
   - in transizione, sui dispositivi non registrati, mostra la fascia gialla
     e manda ancora la vecchia postazione scelta all'ingresso (X-User-Id);
   - FTA.io (cosa sa il server), FTA.esci(), FTA.chiediPinAdmin(),
     FTA.cambiaDispositivo() per le pagine.
*/
(function () {
  'use strict';
  const script = document.currentScript || {};
  const ds = script.dataset || {};
  const STAZIONI_PAGINA = (ds.stazioni || '*').split(',').map(s => s.trim()).filter(Boolean);
  const PUBBLICA = 'pubblica' in ds;
  const fetchVero = window.fetch.bind(window);
  let IO = null;

  function vecchiaPostazione() {
    try {
      const u = JSON.parse(localStorage.getItem('currentUser') || 'null');
      return u && u.id ? u : null;
    } catch (_) { return null; }
  }

  function eApi(url) {
    try {
      const u = new URL(url, location.href);
      return u.origin === location.origin && u.pathname.startsWith('/api/');
    } catch (_) { return false; }
  }

  function intestazioni(orig) {
    const h = new Headers(orig || {});
    // Le pagine non mandano piu' identita': se una riga vecchia lo fa ancora,
    // si toglie qui (il server la ignorerebbe comunque).
    h.delete('X-User-Id');
    const v = vecchiaPostazione();
    if (v && !(IO && IO.registrato)) h.set('X-User-Id', v.id);
    return h;
  }

  function vaiAllIngresso(motivo) {
    if (location.pathname === '/' || location.pathname === '/login.html') return;
    const torna = location.pathname + location.search;
    location.href = '/?torna=' + encodeURIComponent(torna) + (motivo ? '&motivo=' + encodeURIComponent(motivo) : '');
  }

  // ── avvisi ────────────────────────────────────────────────────────
  function stile() {
    if (document.getElementById('fta-stile')) return;
    const s = document.createElement('style');
    s.id = 'fta-stile';
    s.textContent = `
.fta-toast{position:fixed;left:0;right:0;margin:0 auto;width:max-content;bottom:28px;z-index:2147483000;background:#0B1220;color:#fff;
  font:500 14px/1.4 Inter,system-ui,sans-serif;padding:11px 16px;border-radius:10px;box-shadow:0 16px 40px rgba(15,23,42,.25);max-width:90vw;
  animation:fta-pop .2s cubic-bezier(.2,.8,.2,1)}
.fta-fascia{position:fixed;left:0;right:0;bottom:0;z-index:2147482000;background:#FFFAEB;color:#93370D;border-top:1px solid #FEDF89;
  font:600 13px/1.3 Inter,system-ui,sans-serif;padding:9px 16px;display:flex;gap:10px;align-items:center;justify-content:center;text-align:center}
.fta-fascia small{font-weight:500;color:#B54708}
.fta-velo{position:fixed;inset:0;z-index:2147483100;background:rgba(11,18,32,.42);backdrop-filter:blur(3px);display:grid;place-items:center;padding:16px;
  animation:fta-fade .16s ease-out}
.fta-pin{background:#fff;color:#0B1220;border-radius:18px;padding:28px 26px 20px;width:min(360px,100%);display:grid;justify-items:center;gap:14px;
  font:14px/1.5 Inter,system-ui,sans-serif;box-shadow:0 24px 60px rgba(15,23,42,.22);animation:fta-pop .22s cubic-bezier(.2,.8,.2,1)}
.fta-pin h3{margin:0;font:800 20px/1.2 Geist,Inter,system-ui,sans-serif;letter-spacing:-.4px;text-align:center}
.fta-pin p{margin:0;color:#667085;text-align:center;font-size:13.5px}
.fta-dots{display:flex;gap:14px;min-height:18px;align-items:center}
.fta-dots i{width:14px;height:14px;border-radius:50%;border:2px solid #C9CFDA}
.fta-dots i.f{background:#4F46E5;border-color:#4F46E5;box-shadow:0 0 0 5px rgba(79,70,229,.18);animation:fta-punto .3s cubic-bezier(.3,1.8,.5,1)}
.fta-keys{display:grid;grid-template-columns:repeat(3,72px);gap:10px}
.fta-keys button{height:56px;border-radius:14px;background:#fff;border:1px solid #E4E7EC;font:600 21px Geist,Inter,system-ui,sans-serif;color:#0B1220;
  cursor:pointer;box-shadow:0 1px 2px rgba(16,24,40,.05);transition:background .12s,border-color .12s,transform .08s}
.fta-keys button:hover{background:#FAFBFC;border-color:#CBD2DC}
.fta-keys button:active{transform:scale(.95)}
.fta-keys button.ok{background:#4F46E5;color:#fff;border-color:#4F46E5;box-shadow:0 6px 20px rgba(79,70,229,.28)}
.fta-keys button.ok:hover{background:#4338CA}
.fta-keys button:disabled{opacity:.4;cursor:default}
.fta-err{color:#D92D20;font-weight:600;min-height:20px;text-align:center;font-size:13px}
.fta-annulla{background:none;border:0;color:#4F46E5;font:600 13.5px Inter,system-ui,sans-serif;cursor:pointer;padding:6px;border-radius:8px}
.fta-annulla:hover{background:#EEF2FF}
@keyframes fta-scuoti{10%,90%{transform:translateX(-2px)}20%,80%{transform:translateX(4px)}30%,50%,70%{transform:translateX(-6px)}40%,60%{transform:translateX(6px)}}
@keyframes fta-pop{from{opacity:0;transform:translateY(8px) scale(.985)}to{opacity:1;transform:none}}
@keyframes fta-fade{from{opacity:0}to{opacity:1}}
@keyframes fta-punto{from{transform:scale(.4)}}
.fta-scuoti{animation:fta-scuoti .4s}
@media (prefers-reduced-motion: reduce){.fta-velo,.fta-pin,.fta-toast,.fta-dots i.f,.fta-scuoti{animation:none}}`;
    (document.head || document.documentElement).appendChild(s);
  }

  let _toastT = null;
  function avviso(testo) {
    stile();
    let el = document.getElementById('fta-toast');
    if (!el) {
      el = document.createElement('div');
      el.id = 'fta-toast'; el.className = 'fta-toast'; el.setAttribute('role', 'alert');
      document.body.appendChild(el);
    }
    el.textContent = testo;
    el.hidden = false;
    clearTimeout(_toastT);
    _toastT = setTimeout(() => { el.hidden = true; }, 4500);
  }

  function fasciaGialla() {
    if (!document.body || document.getElementById('fta-fascia')) return;
    stile();
    const f = document.createElement('div');
    f.id = 'fta-fascia'; f.className = 'fta-fascia'; f.setAttribute('role', 'status');
    f.innerHTML = 'Questo dispositivo non è registrato: chiedi all’amministrazione. <small>(Funziona ancora per il periodo di passaggio.)</small>';
    document.body.appendChild(f);
    document.body.style.paddingBottom = (f.offsetHeight + 4) + 'px';
  }

  // ── tastierino del PIN (amministratore) ───────────────────────────
  // Lo stesso tastierino della pagina iniziale: tasti grandi per il dito e
  // anche la tastiera vera (cifre, Invio, Backspace, Esc).
  function tastierino(opz) {
    stile();
    return new Promise(risolvi => {
      const velo = document.createElement('div');
      velo.className = 'fta-velo';
      velo.innerHTML = `<div class="fta-pin" role="dialog" aria-modal="true" aria-labelledby="fta-pin-t">
        <h3 id="fta-pin-t"></h3><p class="fta-sotto"></p>
        <div class="fta-dots" aria-hidden="true"></div>
        <div class="fta-err" aria-live="polite"></div>
        <div class="fta-keys">${[1, 2, 3, 4, 5, 6, 7, 8, 9].map(n => `<button type="button" data-k="${n}">${n}</button>`).join('')}
          <button type="button" data-k="del" aria-label="Cancella">⌫</button><button type="button" data-k="0">0</button>
          <button type="button" data-k="ok" class="ok" aria-label="Conferma">✓</button></div>
        <button type="button" class="fta-annulla">Annulla</button></div>`;
      velo.querySelector('h3').textContent = opz.titolo || 'PIN dell’amministratore';
      velo.querySelector('.fta-sotto').textContent = opz.sotto || '';
      document.body.appendChild(velo);
      const box = velo.querySelector('.fta-pin');
      const dots = velo.querySelector('.fta-dots');
      const err = velo.querySelector('.fta-err');
      let pin = '', occupato = false;
      const disegna = () => {
        // aggiornati sul posto: scatta solo il puntino nuovo
        const n = Math.max(4, pin.length);
        while (dots.children.length < n) dots.appendChild(document.createElement('i'));
        while (dots.children.length > n) dots.lastElementChild.remove();
        Array.from(dots.children).forEach((d, i) => d.classList.toggle('f', i < pin.length));
      };
      const chiudi = (esito) => {
        document.removeEventListener('keydown', tasto, true);
        velo.remove();
        risolvi(esito);
      };
      const invia = async () => {
        if (occupato || pin.length < 4) { if (pin.length < 4) err.textContent = 'Il PIN ha almeno 4 cifre.'; return; }
        occupato = true;
        const esito = await opz.verifica(pin);
        occupato = false;
        if (esito === true) { chiudi(true); return; }
        pin = ''; disegna();
        err.textContent = esito || 'PIN sbagliato.';
        box.classList.remove('fta-scuoti'); void box.offsetWidth; box.classList.add('fta-scuoti');
      };
      const premi = k => {
        if (k === 'del') { pin = pin.slice(0, -1); err.textContent = ''; }
        else if (k === 'ok') { invia(); return; }
        else if (pin.length < 8) { pin += k; err.textContent = ''; }
        disegna();
      };
      const tasto = e => {
        if (/^[0-9]$/.test(e.key)) { premi(e.key); e.preventDefault(); e.stopPropagation(); }
        else if (e.key === 'Backspace') { premi('del'); e.preventDefault(); e.stopPropagation(); }
        else if (e.key === 'Enter') { invia(); e.preventDefault(); e.stopPropagation(); }
        else if (e.key === 'Escape') { chiudi(false); e.preventDefault(); e.stopPropagation(); }
      };
      velo.querySelectorAll('[data-k]').forEach(b => b.addEventListener('click', () => premi(b.dataset.k)));
      velo.querySelector('.fta-annulla').addEventListener('click', () => chiudi(false));
      document.addEventListener('keydown', tasto, true);
      disegna();
    });
  }

  let _pinAdminInCorso = null;
  function chiediPinAdmin(sotto) {
    if (_pinAdminInCorso) return _pinAdminInCorso;   // piu' richieste insieme: un solo tastierino
    _pinAdminInCorso = tastierino({
      titolo: 'PIN dell’amministratore',
      sotto: sotto || 'Questa operazione la può fare solo un amministratore.',
      verifica: async pin => {
        try {
          const r = await fetchVero('/api/accesso/admin', {
            method: 'POST', credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ pin }) });
          if (r.ok) return true;
          const d = await r.json().catch(() => ({}));
          return d.error || 'PIN sbagliato.';
        } catch (_) { return 'Il server non risponde.'; }
      },
    }).finally(() => { _pinAdminInCorso = null; });
    return _pinAdminInCorso;
  }

  // ── fetch e XMLHttpRequest ────────────────────────────────────────
  async function gestisci(r, url, riprova) {
    if (r.status !== 401 && r.status !== 403) return r;
    if (url.includes('/api/accesso/')) return r;
    let d = null;
    try { d = await r.clone().json(); } catch (_) { d = null; }
    const codice = d && d.codice;
    if (r.status === 403 && codice === 'serve_pin_admin' && riprova) {
      const ok = await chiediPinAdmin();
      if (ok) return riprova();
      return r;
    }
    if (r.status === 401 && (codice === 'serve_pin' || codice === 'non_registrato')) {
      vaiAllIngresso(codice);
    } else if (r.status === 403 && codice === 'stazione_non_ammessa') {
      avviso('Questa postazione non può farlo.');
    }
    return r;
  }

  window.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || String(input);
    if (!eApi(url)) return fetchVero(input, init);
    const opz = Object.assign({}, init || {});
    opz.credentials = 'same-origin';
    opz.headers = intestazioni(opz.headers || (input instanceof Request ? input.headers : undefined));
    const prova = () => fetchVero(input, opz);
    // Dopo il PIN dell'amministratore la stessa richiesta si ripete una volta.
    return prova().then(r => gestisci(r, url, prova));
  };

  const xhrOpen = XMLHttpRequest.prototype.open;
  const xhrSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (metodo, url) {
    this._ftaApi = eApi(url);
    return xhrOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function () {
    if (this._ftaApi) {
      const v = vecchiaPostazione();
      if (v && !(IO && IO.registrato)) { try { this.setRequestHeader('X-User-Id', v.id); } catch (_) {} }
      this.addEventListener('load', () => {
        if (this.status === 401) {
          try { const d = JSON.parse(this.responseText); if (d.codice === 'serve_pin' || d.codice === 'non_registrato') vaiAllIngresso(d.codice); } catch (_) {}
        }
      });
    }
    return xhrSend.apply(this, arguments);
  };

  // ── chi sono ──────────────────────────────────────────────────────
  // Tablet configurati prima della registrazione: il vecchio token stava in
  // localStorage e viaggiava nell'intestazione. Si manda una volta con la
  // prima richiesta: il server lo riconosce e da li' in poi sta nel cookie.
  const VECCHI_TOKEN = { ore: 'ore_device_token', reparto: 'reparto_device_token', ufficio: 'ufficio_device_token' };
  function vecchioToken() {
    const ordine = STAZIONI_PAGINA.concat(Object.keys(VECCHI_TOKEN));
    for (const st of ordine) {
      const k = VECCHI_TOKEN[st];
      if (!k) continue;
      try { const t = localStorage.getItem(k); if (t) return t; } catch (_) {}
    }
    return '';
  }

  const pronto = (async () => {
    try {
      // Indirizzo preparato dall'amministratore (?codice=, o il vecchio
      // ?token= dei QR): il codice diventa il cookie e sparisce dalla barra.
      const qs = new URLSearchParams(location.search);
      const codice = qs.get('codice') || qs.get('token');
      if (codice) {
        qs.delete('codice'); qs.delete('token');
        history.replaceState(null, '', location.pathname + (qs.toString() ? '?' + qs : '') + location.hash);
        await fetchVero('/api/accesso/usa-codice', {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ codice }) }).catch(() => null);
      }
      const v = vecchiaPostazione();
      const h = v ? { 'X-User-Id': v.id } : {};
      const tok = vecchioToken();
      if (tok) h['X-Device-Token'] = tok;
      const r = await fetchVero('/api/accesso/io', { credentials: 'same-origin', headers: h });
      IO = await r.json();
    } catch (_) {
      IO = null;      // server giu': la pagina prova da sola e riprova
    }
    return IO;
  })();

  function quandoBody(fn) {
    if (document.body) fn(); else document.addEventListener('DOMContentLoaded', fn, { once: true });
  }

  /** Periodo di passaggio: la vecchia postazione viaggia anche in un cookie
      (cosi' i PDF e i disegni aperti in una scheda nuova o in un riquadro,
      che non portano intestazioni, si aprono). Il server lo guarda solo in
      transizione e solo per i dispositivi non registrati. */
  function cookieVecchio(id) {
    document.cookie = id
      ? 'ft_postazione=' + encodeURIComponent(id) + '; path=/; SameSite=Strict'
      : 'ft_postazione=; path=/; SameSite=Strict; max-age=0';
  }

  pronto.then(io => {
    if (!io) return;
    const v = vecchiaPostazione();
    cookieVecchio(!io.registrato && io.vecchio && v ? v.id : '');
    if (PUBBLICA) return;
    const qui = location.pathname;
    if (io.registrato) {
      // Il posto giusto per questo dispositivo: un PC del laser che apre la
      // pagina dell'ufficio torna al laser (le API lo fermerebbero comunque).
      if (!STAZIONI_PAGINA.includes('*') && !STAZIONI_PAGINA.includes(io.stazione)) {
        location.replace(io.pagina);
        return;
      }
      if (io.serve_pin) { vaiAllIngresso('serve_pin'); return; }
      try { localStorage.removeItem('currentUser'); } catch (_) {}
      return;
    }
    if (io.modalita === 'transizione' && io.vecchio) {
      quandoBody(fasciaGialla);
      return;
    }
    vaiAllIngresso('non_registrato');
  });

  async function esci() {
    try { await fetchVero('/api/accesso/esci', { method: 'POST', credentials: 'same-origin' }); } catch (_) {}
    try { localStorage.removeItem('currentUser'); } catch (_) {}
    location.href = '/';
  }

  /** Cambiare cosa fa questo dispositivo: sempre col PIN di un amministratore
      (lo chiede la pagina iniziale). Un dispositivo del periodo di passaggio
      esce come prima. */
  function cambiaDispositivo() {
    if (IO && IO.registrato) { location.href = '/?cambia=1'; return; }
    try { localStorage.removeItem('currentUser'); } catch (_) {}
    location.href = '/';
  }

  /** Il nome di chi e' entrato col PIN, o quello della postazione. */
  function nome() {
    if (!IO) { const v = vecchiaPostazione(); return v ? v.name : ''; }
    if (IO.persona) return IO.persona.nome;
    if (IO.vecchio) { const v = vecchiaPostazione(); return v ? v.name : (IO.stazione_nome || ''); }
    return IO.stazione_nome || '';
  }

  window.FTA = {
    pronto,
    get io() { return IO; },
    nome, esci, avviso, chiediPinAdmin, cambiaDispositivo, tastierino,
  };
})();
