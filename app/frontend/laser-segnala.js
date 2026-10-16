/* Pagina laser — i problemi segnalati dal tablet dell'officina (16/10/2026).

   Stefano: "devono arrivare al laser". Dal tablet un ragazzo segnala "manca
   materiale", "disegno sbagliato", "pezzo da rifare" o altro; qui:
   - nell'elenco, sulla riga dell'ordine, UNA parola: "Segnalato: pezzo da
     rifare" (rossa per pezzo da rifare e disegno, ambra per il resto);
     sparisce quando la segni "Visto" o "Risolto";
   - gli ordini gia' tagliati da piu' di un giorno, con un problema aperto,
     tornano nell'elenco (gruppo "Segnalati dall'officina"): "pezzo da
     rifare" arriva quasi sempre dopo il taglio;
   - nell'ordine aperto, sopra i pezzi: chi, quando, la nota, e i pulsanti
     "Visto" e "Risolto"; per "pezzo da rifare" il campo "quanti da rifare"
     (facoltativo) e, se l'ordine e' gia' tagliato o in Lantek, "da
     rimettere in Lantek" (lo fai tu: FerroTrack non tocca Lantek);
   - sulla riga del pezzo segnalato, la stessa parola con chi e quando.
   "Risolto" si annulla dal messaggio in basso (Annulla), senza conferme
   prima. Server: GET /api/laser/segnalazioni,
   POST /api/segnalazioni-officina/<id>/gestisci (backend/app.py). */
(function () {
  'use strict';
  const LZ = window.LZ;
  const S = LZ.s;
  const esc = LZ.esc;
  const G = LZ.segn = {};
  S.segn = S.segn || {};            // {order_id: [segnalazione non risolta]}
  let impronta = '';

  G.carica = async function () {
    const r = await LZ.get('/api/laser/segnalazioni');
    if (!r.ok) return false;
    const nuove = r.d.ordini || {};
    const imp = JSON.stringify(nuove);
    if (imp === impronta) return false;
    impronta = imp;
    S.segn = nuove;
    LZ.ridisegna();
    return true;
  };

  G.aperte = id => S.segn[id] || [];
  G.nuove = id => G.aperte(id).filter(x => x.stato === 'nuova');
  G.haAperte = id => G.aperte(id).length > 0;

  const etichetta = x => {
    const t = String(x.etichetta || '').toLowerCase();
    return x.tipo === 'disegno' ? 'disegno da guardare' : t;
  };
  const tono = x => (x.grave ? 'err' : 'att');
  const CORTO = { da_rifare: 'da rifare', disegno: 'disegno', manca_materiale: 'manca materiale', altro: 'segnalato' };
  /** "10:42" oggi, "15/10 10:42" gli altri giorni */
  const ora = iso => {
    const d = FT.data(iso);
    if (!d) return '';
    return FT.giorniDa(d) === 0 ? FT.fmtOra(d) : `${FT.fmtData(d).slice(0, 5)} ${FT.fmtOra(d)}`;
  };
  /** "Luca, 10:42 · bava sul foro" */
  const chiQuando = x => [`${x.da || 'Officina'}, ${ora(x.quando)}`, x.nota].filter(Boolean).join(' · ');

  /** Nell'elenco: la parola sulla riga (solo le nuove). */
  G.parolaLista = function (o) {
    const l = G.nuove(o.id);
    if (!l.length) return '';
    const grave = l.some(x => x.grave);
    const t = l.length === 1 ? `Segnalato: ${etichetta(l[0])}` : `Segnalati ${l.length} problemi`;
    const tit = l.map(x => `${x.etichetta}${x.codice_pezzo ? ' (' + x.codice_pezzo + ')' : ''}: ${chiQuando(x)}`).join('\n');
    // con l'ordine aperto la tabella e' stretta: la parola corta
    const corto = l.length === 1 ? (CORTO[l[0].tipo] || 'segnalato') : `${l.length} problemi`;
    return `<small class="lz-segn-l ${grave ? 'err' : 'att'}" title="${esc(tit)}"><span class="lz-largo">${esc(t)}</span><span class="lz-stretto">${esc(corto)}</span></small>`;
  };

  /** Sulla riga del pezzo: le segnalazioni aperte di quel codice. */
  G.pezzo = function (id, codice) {
    if (!codice) return '';
    const l = G.aperte(id).filter(x => x.codice_pezzo && x.codice_pezzo === codice);
    return l.map(x => `<small class="lz-segn-pz ${x.stato === 'nuova' ? tono(x) : ''}"><b>${esc(capo(etichetta(x)))}</b> · ${esc(chiQuando(x))}${x.stato === 'vista' ? ' · visto' : ''}</small>`).join('');
  };

  const capo = s => { s = String(s || ''); return s.charAt(0).toUpperCase() + s.slice(1); };

  /** Nell'ordine aperto, sopra le schede: tutte le non risolte. */
  G.sezione = function (o) {
    const l = G.aperte(o.id);
    if (!l.length) return '';
    const st = LZ.statoLaser(o);
    const righe = l.map(x => {
      const sid = esc(x.id);
      const rifare = x.tipo === 'da_rifare';
      const lantek = rifare && (st === 'tagliato' ? 'Ordine già tagliato: il pezzo nuovo è da rimettere in Lantek.'
        : st === 'in_lantek' ? 'Ordine già in Lantek: se il pezzo è già tagliato, è da rimettere in Lantek.' : '');
      const parola = x.stato === 'nuova'
        ? `<b class="lz-segn-t ${tono(x)}">${esc(capo(etichetta(x)))}</b>`
        : `<b class="lz-segn-t">${esc(capo(etichetta(x)))}</b>`;
      return `<li data-segn="${sid}">
        <span class="mono">${x.codice_pezzo ? esc(x.codice_pezzo) : '—'}</span>
        <span class="mot">${parola}<small>${esc(chiQuando(x))}${x.stato === 'vista' ? ` · visto ${esc(ora(x.visto_il))}` : ''}</small>
          ${lantek ? `<small class="lz-segn-lantek">${esc(lantek)}</small>` : ''}
          <span class="lz-dati">
            ${rifare ? `<label class="lz-segn-q">Quanti da rifare <input type="text" inputmode="numeric" maxlength="4" data-segn-quanti="${sid}"
              data-fuoco="segn-q-${sid}" placeholder="—" aria-label="Quanti pezzi da rifare (facoltativo)"></label>` : ''}
            ${x.stato === 'nuova' ? `<button class="lz-btn piccolo" type="button" data-az="segn-visto" data-segn-id="${sid}" data-fuoco="segn-visto-${sid}">Visto</button>` : ''}
            <button class="lz-btn piccolo" type="button" data-az="segn-risolto" data-segn-id="${sid}" data-fuoco="segn-risolto-${sid}">Risolto</button>
          </span></span></li>`;
    }).join('');
    return `<section class="lz-segn" aria-label="Problemi segnalati dall'officina">
      <h3>Dall'officina</h3><ul>${righe}</ul></section>`;
  };

  /** Visto / Risolto / Riaperto. */
  G.gestisci = (sid, esito, quanti) => LZ.azione(async () => {
    const corpo = { esito };
    if (quanti != null && quanti !== '') corpo.quanti = quanti;
    const r = await LZ.post(`/api/segnalazioni-officina/${encodeURIComponent(sid)}/gestisci`, corpo);
    if (!r.ok) { FT.toast(r.d.error || 'Non riuscito', 'err'); return false; }
    impronta = '';
    await G.carica();
    LZ.ridisegna();
    return r.d;
  });

  G.visto = async function (sid) {
    const d = await G.gestisci(sid, 'visto');
    if (d) FT.toast('Segnato visto: l’officina lo vede', 'ok');
  };
  G.risolto = async function (sid) {
    const inp = document.querySelector(`input[data-segn-quanti="${CSS.escape(sid)}"]`);
    const v = inp ? String(inp.value || '').trim() : '';
    if (v && !/^\d{1,4}$/.test(v)) { FT.toast('Quanti da rifare: scrivi un numero, o lascia vuoto', 'warn'); if (inp) inp.focus(); return; }
    if (v && Number(v) < 1) { FT.toast('Quanti da rifare: da 1 in su, o lascia vuoto', 'warn'); if (inp) inp.focus(); return; }
    const d = await G.gestisci(sid, 'risolto', v ? Number(v) : null);
    if (d) FT.toast('Risolto: l’ufficio riceve l’avviso', 'ok', { testo: 'Annulla', fn: () => G.gestisci(sid, 'riaperto') }, 10000);
  };
})();
