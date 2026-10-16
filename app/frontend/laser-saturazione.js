/* Pagina laser — la saturazione in UNA riga: "Laser, prossimi 5 giorni lavorativi 26 / 30 h".
   Il calendario e il banco lamiere non stanno piu' qui: la saturazione la
   tiene l'ufficio. Il conto e' quello di carico-laser.js (lo stesso
   dell'ufficio), con i tempi tarati su Lantek che manda il server. */
(function () {
  'use strict';
  const LZ = window.LZ;
  const S = LZ.s;
  const sat = LZ.sat = { pronto: false, t: 0 };

  sat.carica = async function (forza) {
    if (!window.CaricoLaser) return;
    const ok = await CaricoLaser.carica(!!forza);
    if (ok) { sat.pronto = true; sat.t = Date.now(); }
  };

  function lunedi() {
    const d = new Date(); d.setHours(12, 0, 0, 0);
    d.setDate(d.getDate() - ((d.getDay() + 6) % 7));
    return d;
  }

  /** {min, cap, ignoti, ritardo, minRitardo, etichetta}: il riepilogo del
      server (backend/carico_laser.py), lo stesso che legge l'ufficio. */
  sat.conti = function () {
    const rp = window.CaricoLaser.riepilogo();
    if (!rp) return { min: 0, cap: 0, ignoti: 0, ritardo: 0, minRitardo: 0, etichetta: '' };
    return { min: rp.periodo.min, cap: rp.periodo.cap, ignoti: rp.periodo.ignoti, etichetta: rp.periodo.etichetta,
             ritardo: rp.taglio_in_ritardo.n, minRitardo: rp.taglio_in_ritardo.min };
  };

  const h = m => (m / 60).toLocaleString('it-IT', { maximumFractionDigits: m >= 600 ? 0 : 1 });

  /** L'HTML della riga (dentro la testata dell'elenco). */
  sat.html = function () {
    if (!sat.pronto || !window.CaricoLaser) {
      return '<span class="lz-sat" aria-busy="true"><span class="lz-skel skel"></span></span>';
    }
    const k = sat.conti();
    const pct = k.cap ? k.min / k.cap : 0;
    const pieno = k.cap && pct > 1;
    const tit = `${k.etichetta}: ore di laser sulle ore utili del turno; il taglio in ritardo pesa su oggi. Tempi tarati su Lantek. Lo stesso numero lo vede l'ufficio.`
      + (k.ignoti ? ` ${k.ignoti} ordini senza durata non sono contati.` : '');
    return `<span class="lz-sat${pieno ? ' pieno' : ''}" title="${LZ.esc(tit)}">
      <span class="riga"><span>${LZ.esc(k.etichetta)}:</span><b class="ore num">${h(k.min)} su ${h(k.cap)} h</b></span>
      ${k.ritardo ? `<span class="rit">Taglio in ritardo: ${LZ.plur(k.ritardo, 'ordine', 'ordini')}${k.minRitardo ? `, ${h(k.minRitardo)} h di laser` : ''}</span>` : ''}
    </span>`;
  };

  /** Ore di laser di un ordine (stesso conto della settimana). */
  sat.oreOrdine = function (o) {
    if (!sat.pronto || !window.CaricoLaser) return null;
    return CaricoLaser.durata(o);
  };
})();
