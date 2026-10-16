/* Schermo intero sui tablet dell'officina (Officina, Timbratrice, Cartellone).
 *
 * 16/10/2026. Stefano: "mi rimane la barra dei comandi sotto, non e'
 * letteralmente a tutto schermo".
 *
 * Due strade, una per caso:
 *  1. App installata con l'icona nuova: il manifest della pagina
 *     (manifest-officina.json, -ore, -cartellone) chiede "display: fullscreen"
 *     e Android nasconde barra di stato e barra dei comandi da solo.
 *     Qui non serve niente: il pulsante non compare.
 *  2. Pagina aperta in Chrome, o con la vecchia icona (finestra "standalone",
 *     che la barra sotto la lascia): compare un pulsante discreto
 *     "Schermo intero" nella barra in alto. Il browser vuole un tocco per
 *     andare a schermo intero, quindi non si puo' fare da soli all'avvio.
 *     Se poi lo schermo intero si perde (tablet in standby, tasto Indietro),
 *     ci si rientra al primo tocco sulla pagina, qualunque cosa si tocchi.
 *
 * Registra anche sw.js (che di proposito non intercetta la rete): serve a
 * Chrome per offrire "Installa app".
 *
 * Dove va il pulsante: nell'elemento [data-schermo-intero] se la pagina ne ha
 * uno, altrimenti in fondo alla barra (.ft-topbar-end).
 */
(function () {
  'use strict';
  if ('serviceWorker' in navigator) {
    try { navigator.serviceWorker.register('/sw.js').catch(function () {}); } catch (_) {}
  }

  var doc = document, el = doc.documentElement;
  var CHIAVE = 'ft-schermo-intero';
  var supporta = !!(doc.fullscreenEnabled || doc.webkitFullscreenEnabled);
  function mm(q) { try { return window.matchMedia(q).matches; } catch (_) { return false; } }
  // display-mode: fullscreen vale sia per l'app installata a schermo intero
  // sia per una pagina messa a schermo intero col pulsante.
  function aSchermoIntero() {
    return !!(doc.fullscreenElement || doc.webkitFullscreenElement) || mm('(display-mode: fullscreen)');
  }
  function voluto(v) {
    try {
      if (v === undefined) return sessionStorage.getItem(CHIAVE) === '1';
      sessionStorage.setItem(CHIAVE, v ? '1' : '0');
    } catch (_) {}
    return !!v;
  }
  function entra() {
    if (!supporta || aSchermoIntero()) return;
    try {
      var p = el.requestFullscreen ? el.requestFullscreen({ navigationUI: 'hide' }) : el.webkitRequestFullscreen();
      voluto(true);
      if (p && p.catch) p.catch(function () { aggiorna(); });
    } catch (_) { aggiorna(); }
  }

  var btn = null;
  function aggiorna() {
    if (btn) btn.hidden = !supporta || aSchermoIntero();
  }

  function monta() {
    if (!supporta) return;
    var dove = doc.querySelector('[data-schermo-intero]') || doc.querySelector('.ft-topbar-end');
    if (!dove) return;
    btn = doc.createElement('button');
    btn.type = 'button';
    btn.className = 'ft-btn ftt-pieno';
    btn.id = 'schermo-intero';
    btn.textContent = 'Schermo intero';
    btn.title = 'Nasconde le barre di Android';
    btn.addEventListener('click', function (e) { e.stopPropagation(); entra(); });
    dove.appendChild(btn);
    aggiorna();
  }

  // Rientro al primo tocco: un tocco e' l'unico momento in cui il browser
  // lascia tornare a schermo intero. Il tocco fa anche quello che doveva.
  doc.addEventListener('click', function () {
    if (voluto() && !aSchermoIntero()) entra();
  }, true);
  doc.addEventListener('fullscreenchange', aggiorna);
  doc.addEventListener('webkitfullscreenchange', aggiorna);
  doc.addEventListener('visibilitychange', function () { if (!doc.hidden) aggiorna(); });
  try { window.matchMedia('(display-mode: fullscreen)').addEventListener('change', aggiorna); } catch (_) {}

  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', monta);
  else monta();

  window.FTTablet = { entra: entra, aSchermoIntero: aSchermoIntero };
})();
