/* Pagina laser — la saturazione in UNA riga: "Questa settimana 26 / 30 h".
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

  /** {min, cap, ritardo: n, minRitardo} della settimana in corso */
  sat.conti = function () {
    const C = window.CaricoLaser;
    const tutti = S.ordini.filter(o => !o.is_deleted);
    let min = 0, cap = 0, ignoti = 0;
    const d = lunedi();
    for (let i = 0; i < 7; i++) {
      const k = C.caricoGiorno(C.iso(d), tutti);
      min += k.min; cap += k.cap; ignoti += k.ignoti;
      d.setDate(d.getDate() + 1);
    }
    // in ritardo: da tagliare con la consegna gia' passata
    let ritardo = 0, minRitardo = 0;
    for (const o of LZ.ordiniLaser()) {
      const st = LZ.statoLaser(o);
      if (st === 'tagliato') continue;
      const g = LZ.giorniA(o);
      if (g != null && g < 0) { ritardo++; minRitardo += C.durata(o).min || 0; }
    }
    return { min, cap, ignoti, ritardo, minRitardo };
  };

  const h = m => (m / 60).toLocaleString('it-IT', { maximumFractionDigits: m >= 600 ? 0 : 1 });

  /** L'HTML della riga (dentro la testata dell'elenco). */
  sat.html = function () {
    if (!sat.pronto || !window.CaricoLaser) {
      return '<div class="lz-sat" aria-busy="true"><span class="lz-skel skel"></span></div>';
    }
    const k = sat.conti();
    const pct = k.cap ? k.min / k.cap : 0;
    const pieno = k.cap && pct > 1;
    const tit = `Ore di laser della settimana (lunedì-domenica) sulle ore utili del turno. Tempi tarati su Lantek.`
      + (k.ignoti ? ` ${k.ignoti} ordini senza durata non sono contati.` : '');
    return `<div class="lz-sat${pieno ? ' pieno' : ''}" title="${LZ.esc(tit)}">
      <div class="riga"><span>Questa settimana</span><b class="ore num">${h(k.min)} / ${h(k.cap)} h</b>
        <span class="barra" aria-hidden="true"><i style="width:${Math.min(100, Math.round(pct * 100))}%"></i></span></div>
      ${k.ritardo ? `<div class="rit">${LZ.plur(k.ritardo, 'ordine', 'ordini')} in ritardo${k.minRitardo ? ` · ${h(k.minRitardo)} h di laser` : ''}</div>` : ''}
    </div>`;
  };

  /** Ore di laser di un ordine (stesso conto della settimana). */
  sat.oreOrdine = function (o) {
    if (!sat.pronto || !window.CaricoLaser) return null;
    return CaricoLaser.durata(o);
  };
})();
