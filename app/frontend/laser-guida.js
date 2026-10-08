/* Laser: "Metti in Lantek" passo per passo (Stefano, 07/10/2026: "cosi' e'
   troppo caotico, se sbaglio qua mando in crisi il taglio").

   Si apre a destra quando si clicca un ordine DA IMPORTARE:
     1. Controllo  - cosa c'e' gia', cosa crea FerroTrack, cosa va sistemato
                     prima (BLOCCA finche' non e' tutto pronto) e le differenze
                     fra ordine e Lantek (decide Stefano, una per una)
     2. Conferma   - riepilogo di cosa verra' creato in Lantek
     3. Fatto      - risultato controllato in Lantek; l'ordine passa da solo
                     fra quelli "In Lantek" e si propone il prossimo

   Usa le funzioni della pagina (laser.html): _getJson, _post, esc, FT,
   numeroOrdine, giornoConsegna, lvProssimo, loadOrders, selectOrder. */

const _guida = {};            // per ordine: { passo, dati, decisioni, esito }
const _guidaDisegni = {};     // ordini aperti "coi disegni" (vista classica)

function guidaAttiva(o) {
  return o && faseLaser(o) === 'importare' && !_guidaDisegni[o.id];
}

function guidaApri(id) {
  delete _guidaDisegni[id];
  const o = ordineAperto();
  if (o && o.id === id) renderWork(o);
}

function guidaDisegni(id) {
  _guidaDisegni[id] = true;
  const o = ordineAperto();
  if (o && o.id === id) renderWork(o);
}

/* I codici divisi per cosa bisogna farci. */
function guidaAnalisi(d) {
  const creare = new Set((d.nuovi_da_creare || []).map(x => x.codice));
  const fuori = new Map((d.nuovi_esclusi || []).map(x => [x.codice, x.motivo]));
  const daVerif = new Map((d.nuovi_esclusi || []).filter(x => x.verifica).map(x => [x.codice, x]));
  const auto = d.invio_automatico && (d.lantek || {}).disponibile;
  const bloccati = [], decidere = [], nuovi = [], pronti = [], aperti = [], verificare = [];
  // ordine solo PDF: righe che in Lantek non ci sono col loro codice (assiemi)
  const abbinare = (d.pdf || {}).da_abbinare || [];
  const daAbbinare = new Set(abbinare.map(x => x.codice));
  for (const r of d.righe || []) {
    if (r.stato === 'nuovo' && daAbbinare.has(r.codice_ft || r.codice)) continue;
    if (r.stato === 'nuovo' && daVerif.has(r.codice)) {
      // disegno dubbio (verifica al caricamento): si guarda e si conferma qui
      verificare.push({ r, x: daVerif.get(r.codice) });
      continue;
    }
    if (r.stato === 'nuovo' && !creare.has(r.codice)) {
      // "25CCPA0026-00 (2)": file scaricato piu' volte, il codice e' sbagliato
      const copia = /\s\(\d+\)$/.test(String(r.codice_ft || r.codice));
      bloccati.push(copia
        ? { r, motivo: 'codice con «(2)»: è una copia scaricata più volte',
            cosa: "correggi il codice nell'ordine (togli « (2)»), poi premi Ricontrolla" }
        : { r, motivo: fuori.get(r.codice) || 'non ancora in Lantek',
            cosa: 'importalo dal MES (Importa → Files DXF), poi premi Ricontrolla' });
      continue;
    }
    const avvisi = (r.avvisi || []).filter(a => !/^non ancora in Lantek|scegli quella giusta/.test(a));
    if (avvisi.length) decidere.push({ r, avvisi });
    if (r.in_produzione) aperti.push(r);
    else if (creare.has(r.codice)) nuovi.push(r);
    else pronti.push(r);
  }
  return { bloccati, decidere, nuovi, pronti, aperti, verificare, abbinare, auto };
}

async function guidaCarica(o, fresco) {
  const g = _guida[o.id] || (_guida[o.id] = { passo: 1, decisioni: {} });
  g.caricamento = true;
  guidaDisegna(o);
  const { ok, d } = await _getJson(`${API_URL}/api/orders/${o.id}/lantek-quantita${fresco ? '?fresco=1' : ''}`);
  if (selectedOrderId !== o.id) return;
  g.caricamento = false;
  g.dati = ok && d && d.success ? d : null;
  g.errore = ok ? null : (d && d.error) || 'il server non ha risposto';
  if (g.passo !== 3) g.passo = 1;
  guidaDisegna(o);
}

function renderGuida(o) {
  const w = document.getElementById('lz-work');
  w.classList.remove('pdf');
  document.querySelector('.lv').classList.remove('largo');
  w.innerHTML = `<div class="lg" id="lg"></div>`;
  const g = _guida[o.id];
  if (g && g.dati && g.passo === 3) guidaDisegna(o);
  else guidaCarica(o, false);
}

function guidaPassi(passo) {
  const nomi = ['Controllo', 'Conferma', 'Fatto'];
  return `<ol class="lg-passi">${nomi.map((n, i) => `<li class="${i + 1 < passo ? 'fatto' : i + 1 === passo ? 'ora' : ''}">
    <span>${i + 1 < passo ? '✓' : i + 1}</span>${n}</li>`).join('')}</ol>`;
}

function guidaTesta(o) {
  const cons = giornoConsegna(o);
  return `<div class="lg-testa">
      <div><h2>${esc(o.cliente || 'Cliente non indicato')} <span class="ft-mono">#${esc(numeroOrdine(o))}</span></h2>
        <small>consegna ${cons ? FT.fmtData(cons) : '—'}</small></div>
      <div class="lg-testa-az">
        <button class="ft-btn" onclick="guidaDisegni('${o.id}')" title="Vedi disegni, PDF e pezzi"><i data-lucide="drafting-compass"></i> Disegni e PDF</button>
        <button class="ft-icon-btn" onclick="deselectOrder()" title="Chiudi" aria-label="Chiudi"><i data-lucide="x"></i></button>
      </div>
    </div>`;
}

function guidaDisegna(o) {
  const box = document.getElementById('lg');
  if (!box) return;
  const g = _guida[o.id] || { passo: 1, decisioni: {} };
  let corpo = '', piede = '';
  if (g.caricamento && !g.dati) {
    corpo = `<div class="lg-attesa"><div class="ft-skel lz-skel"></div><div class="ft-skel lz-skel"></div><p>Guardo cosa c'è già in Lantek…</p></div>`;
  } else if (!g.dati) {
    corpo = guidaMsg('err', 'Non riesco a leggere i dati', esc(g.errore || ''));
    piede = `<button class="ft-btn lg primary" onclick="guidaCarica(ordineAperto(), true)"><i data-lucide="refresh-cw"></i> Riprova</button>`;
  } else if (g.passo === 3) {
    ({ corpo, piede } = guidaPasso3(o, g));
  } else if (g.passo === 2) {
    ({ corpo, piede } = guidaPasso2(o, g));
  } else {
    ({ corpo, piede } = guidaPasso1(o, g));
  }
  box.innerHTML = guidaTesta(o) + guidaPassi(g.passo || 1)
    + `<div class="lg-corpo">${corpo}</div>` + (piede ? `<div class="lg-piede">${piede}</div>` : '');
  FT.icone();
}

function guidaMsg(tipo, titolo, testo) {
  const ic = { ok: 'check-circle-2', err: 'x-circle', warn: 'alert-triangle', info: 'info' }[tipo] || 'info';
  return `<div class="lg-msg ${tipo}"><i data-lucide="${ic}"></i><div><b>${titolo}</b>${testo ? `<p>${testo}</p>` : ''}</div></div>`;
}

function guidaElenco(righe, colonna) {
  if (!righe.length) return '';
  return `<ul class="lg-el">${righe.map(r => `<li><span class="ft-mono">${esc(r.codice)}</span>
      <span>${colonna(r)}</span></li>`).join('')}</ul>`;
}

/* ── Passo 1: controllo ─────────────────────────────────────────────── */
function guidaPasso1(o, g) {
  const d = g.dati;
  const lt = d.lantek || {};
  if (!d.n_codici && d.pdf && d.pdf.n_righe && !(d.pdf.da_abbinare || []).length) {
    return {
      corpo: guidaMsg('info', 'Nessuna riga va al laser', `Le ${d.pdf.n_righe} righe del PDF sono segnate «non va al laser».`)
        + guidaAbbinatiFatti(o, d.pdf),
      piede: `<button class="ft-btn lg primary" onclick="guidaSegnaFatto('${o.id}')"><i data-lucide="check"></i> Segna in Lantek e vai avanti</button>`,
    };
  }
  if (!d.n_codici && !(d.pdf && (d.pdf.da_abbinare || []).length)) {
    return {
      corpo: guidaMsg('info', 'Quest’ordine non ha l’elenco dei pezzi con i codici',
        'Va messo in Lantek a mano dal MES, guardando il PDF dell’ordine. Quando hai finito, premi il pulsante qui sotto.'),
      piede: `<button class="ft-btn lg" onclick="guidaDisegni('${o.id}')"><i data-lucide="file-text"></i> Apri il PDF</button>
        <button class="ft-btn lg primary" onclick="guidaSegnaFatto('${o.id}')"><i data-lucide="check"></i> È in Lantek</button>`,
    };
  }
  if (!lt.disponibile) {
    return {
      corpo: guidaMsg('err', 'Lantek non risponde', 'Senza Lantek non posso controllare niente: verifica che il PC di Lantek sia acceso e riprova.'),
      piede: `<button class="ft-btn lg primary" onclick="guidaCarica(ordineAperto(), true)"><i data-lucide="refresh-cw"></i> Riprova</button>`,
    };
  }
  const a = guidaAnalisi(d);
  const daDecidere = a.decidere.filter(x => g.decisioni[x.r.codice] !== 'ok');
  const correggo = a.decidere.filter(x => g.decisioni[x.r.codice] === 'correggo');
  const nMandare = (d.da_mandare || []).length + (d.nuovi_da_creare || []).length;
  let corpo = '';
  // 0) ordine solo PDF: righe da abbinare ai pezzi di Lantek
  if (a.abbinare.length) corpo += guidaSezAbbina(o, g, a.abbinare, d.pdf);
  // 1) cosa blocca
  if (a.bloccati.length) {
    corpo += `<section class="lg-sez err"><h3><i data-lucide="x-circle"></i> Da sistemare prima di andare avanti <em>${a.bloccati.length}</em></h3>
      <p>Questi codici non sono in Lantek e non posso crearli io. Finché non ci sono, l’ordine non parte.</p>
      ${guidaElenco(a.bloccati.map(x => ({ ...x.r, _m: x.motivo, _c: x.cosa })), r => `<b>${esc(r._m)}</b> → ${esc(r._c)}`)}</section>`;
  }
  // 1b) disegni dubbi dei pezzi nuovi: li guardi e confermi qui
  if (a.verificare.length) {
    corpo += `<section class="lg-sez warn"><h3><i data-lucide="eye"></i> Disegni da guardare <em>${a.verificare.length}</em></h3>
      <p>Pezzi nuovi che creo io in Lantek: il controllo automatico ha un dubbio. Guarda il disegno:
        <b style="color:#16a34a">in verde</b> il contorno che taglierei, <b style="color:#dc2626">rosso tratteggiato</b> quello letto prima (se l'ho corretto). Se il verde è il pezzo giusto, confermalo.</p>
      <ul class="lg-ver">${a.verificare.map(({ r, x }) => {
        // il disegno con sopra il contorno preso da FerroTrack (verde) e, se
        // corretto in automatico, quello di prima (rosso tratteggiato)
        const svg = x.articolo_id && x.disegno && /\.dxf$/i.test(x.disegno)
          ? `${API_URL}/api/orders/${o.id}/pezzi/${encodeURIComponent(x.articolo_id)}/contorno.svg` : null;
        const motivi = String(x.motivo || '').replace(/^disegno da verificare: /, '').split('; ');
        return `<li>${svg ? `<a href="${esc(svg)}" target="_blank" title="Apri grande"><img src="${esc(svg)}" alt="" loading="lazy"></a>` : '<div class="lg-ver-noimg">nessuna anteprima</div>'}
          <div><span class="ft-mono">${esc(r.codice)}</span> <small>${esc(r.materiale || '')} ${r.spessore != null ? esc(String(r.spessore)) + ' mm' : ''} · ${esc(String(r.quantita))} pz</small>
            ${motivi.map(m => `<p>${esc(m)}</p>`).join('')}
            <div class="lg-dec-az"><button class="ft-btn sm primary" onclick="guidaConfermaPezzo('${o.id}', ${esc(JSON.stringify(String(x.articolo_id || '')))})"><i data-lucide="check"></i> Il contorno è giusto</button>
              ${svg ? `<button class="ft-btn sm" onclick="guidaApriCad('${o.id}', ${esc(JSON.stringify(String(x.articolo_id || '')))}, ${esc(JSON.stringify(String(x.disegno || '')))})"><i data-lucide="mouse-pointer-click"></i> Scegli il contorno</button>` : ''}</div>
            <small class="lg-ver-no">Se il verde non è il pezzo: «Scegli il contorno» e clicca quello giusto sul disegno.</small></div></li>`;
      }).join('')}</ul></section>`;
  }
  // 2) cosa decidere
  if (a.decidere.length) {
    corpo += `<section class="lg-sez warn"><h3><i data-lucide="help-circle"></i> Da decidere <em>${a.decidere.length}</em></h3>
      <p>L’ordine e Lantek non dicono la stessa cosa. Scegli tu, uno per uno.</p>
      <ul class="lg-dec">${a.decidere.map(x => {
        const sc = g.decisioni[x.r.codice];
        return `<li class="${sc || ''}"><div><span class="ft-mono">${esc(x.r.codice)}</span>
            ${x.avvisi.map(t => `<p>${esc(t)}</p>`).join('')}</div>
          <div class="lg-dec-az">
            <button class="ft-btn sm ${sc === 'ok' ? 'primary' : ''}" onclick="guidaDecidi('${o.id}', ${esc(JSON.stringify(x.r.codice))}, 'ok')">Va bene così</button>
            <button class="ft-btn sm ${sc === 'correggo' ? 'danger' : ''}" onclick="guidaDecidi('${o.id}', ${esc(JSON.stringify(x.r.codice))}, 'correggo')">Lo correggo io</button>
          </div>${sc === 'correggo' ? '<small>Correggilo (in Lantek o nell’ordine), poi premi Ricontrolla.</small>' : ''}</li>`;
      }).join('')}</ul></section>`;
  }
  // 3) cosa va bene
  const ok = [];
  if (a.nuovi.length) ok.push(`<li><b>${a.nuovi.length}</b> pezzi nuovi: <b>li creo io</b> in Lantek col loro disegno</li>`);
  if (a.pronti.length) {
    const rifatti = a.pronti.filter(r => r.gia_fatti).length;
    ok.push(`<li><b>${a.pronti.length}</b> codici già nell’archivio di Lantek: creo il loro ordine di produzione`
      + (rifatti ? `<br><small>di cui <b>${rifatti}</b> già fatti in passato con questo ordine: si rifanno</small>` : '') + '</li>');
  }
  if (a.aperti.length) ok.push(`<li><b>${a.aperti.length}</b> codici hanno già l’ordine di produzione aperto in Lantek: <b>non li rimando</b></li>`);
  if (ok.length) {
    corpo += `<section class="lg-sez ok"><h3><i data-lucide="check-circle-2"></i> A posto</h3><ul class="lg-ok">${ok.join('')}</ul>
      ${d.pdf ? guidaAbbinatiFatti(o, d.pdf) : ''}</section>`;
  }
  const bloccato = a.bloccati.length || daDecidere.length || a.verificare.length || a.abbinare.length;
  let piede = `<button class="ft-btn lg" onclick="guidaCarica(ordineAperto(), true)" title="Rileggi Lantek (dopo aver importato o corretto qualcosa)"><i data-lucide="refresh-cw"></i> Ricontrolla</button>`;
  if (!nMandare && !bloccato) {
    corpo = guidaMsg('ok', 'È già tutto in Lantek', `Tutti i ${d.n_codici} codici hanno il loro ordine di produzione aperto in Lantek.`) + corpo;
    piede += `<button class="ft-btn lg primary" onclick="guidaSegnaFatto('${o.id}')"><i data-lucide="check"></i> Segna in Lantek e vai avanti</button>`;
  } else {
    const perche = a.abbinare.length ? `prima abbina le ${a.abbinare.length} righe dell’ordine`
      : a.bloccati.length ? `prima sistema i ${a.bloccati.length} codici in rosso`
      : a.verificare.length ? `prima guarda i ${a.verificare.length} disegni dubbi`
      : correggo.length ? `prima correggi i ${correggo.length} codici e premi Ricontrolla`
      : daDecidere.length ? `prima decidi i ${daDecidere.length} codici in giallo` : '';
    piede += `<button class="ft-btn lg primary" ${bloccato ? 'disabled' : ''} onclick="guidaVai('${o.id}', 2)" title="${esc(perche)}">Avanti <i data-lucide="arrow-right"></i></button>`;
    if (bloccato) piede = `<span class="lg-perche">${esc(perche)}</span>` + piede;
  }
  if (!bloccato && nMandare) {
    corpo = guidaMsg('ok', 'Tutto pronto', `Puoi andare avanti: vedrai il riepilogo prima di mandare.`) + corpo;
  }
  return { corpo, piede };
}

/* "Scegli il contorno": lo strumento CAD in modalita' ordine (dxf-editor.html
   ?ordine=...): si clicca il contorno giusto, il server ricalcola il pezzo e
   rifa' il DXF pulito per Lantek; poi si ricontrolla. */
function guidaApriCad(id, articoloId, disegno) {
  if (!articoloId) return;
  const url = `/dxf-editor.html?ordine=${encodeURIComponent(id)}&articolo_id=${encodeURIComponent(articoloId)}`
    + `&file=${encodeURIComponent(disegno)}&embed=1`;
  const bg = document.createElement('div');
  bg.className = 'lg-cad-bg';
  bg.innerHTML = `<div class="lg-cad"><div class="lg-cad-testa"><b>Scegli il contorno del pezzo</b>
      <span>Clicca il bordo del pezzo da tagliare, poi «Conferma pezzo».</span>
      <button class="ft-icon-btn" aria-label="Chiudi" title="Chiudi senza salvare"><i data-lucide="x"></i></button></div>
    <iframe src="${esc(url)}" title="Scegli il contorno"></iframe></div>`;
  const chiudi = () => { window.removeEventListener('message', ascolta); bg.remove(); };
  const ascolta = (ev) => {
    const m = ev.data || {};
    if (m.type === 'cad-ordine-salvato') {
      const e = m.esito || {};
      if (e.success) {
        FT.toast(e.pronto_lantek ? 'Contorno salvato: il pezzo è pronto per Lantek'
          : 'Contorno salvato. ' + (e.motivo || 'Il DXF per Lantek va preparato a mano'), e.pronto_lantek ? 'ok' : 'warn', null, 8000);
      } else {
        FT.toast('Contorno non salvato: ' + (e.error || e.motivo || 'errore'), 'err', null, 8000);
      }
      const o = ordineAperto();
      if (o && o.id === id) guidaCarica(o, true);
    } else if (m.type === 'cad-close') {
      chiudi();
    }
  };
  window.addEventListener('message', ascolta);
  bg.querySelector('.ft-icon-btn').onclick = chiudi;
  document.body.appendChild(bg);
  FT.icone();
}

async function guidaConfermaPezzo(id, articoloId) {
  if (!articoloId) { FT.toast('Pezzo non trovato', 'err'); return; }
  let r;
  try { r = await _post(`${API_URL}/api/orders/${id}/pezzi/${articoloId}/conferma`, {}); }
  catch (e) { r = { success: false, error: 'Server non raggiungibile' }; }
  if (!r.success) { FT.toast('Non riuscito: ' + (r.error || 'errore'), 'err'); return; }
  FT.toast('Contorno confermato', 'ok');
  const o = ordineAperto();
  if (o && o.id === id) guidaCarica(o, true);
}

function guidaDecidi(id, codice, scelta) {
  const g = _guida[id];
  if (!g) return;
  g.decisioni[codice] = g.decisioni[codice] === scelta ? undefined : scelta;
  const o = ordineAperto();
  if (o && o.id === id) guidaDisegna(o);
}

function guidaVai(id, passo) {
  const g = _guida[id];
  if (!g) return;
  g.passo = passo;
  const o = ordineAperto();
  if (o && o.id === id) guidaDisegna(o);
}

/* ── Passo 2: conferma ──────────────────────────────────────────────── */
function guidaPasso2(o, g) {
  const d = g.dati;
  const nuovi = d.nuovi_da_creare || [];
  const el = d.da_mandare || [];
  const tutti = [...nuovi.map(x => ({ ...x, nuovo: true })), ...el];
  const rifatti = el.filter(x => x.gia_fatti);
  const cons = d.consegna ? FT.data(d.consegna).toLocaleDateString('it-IT') : 'senza data';
  const corpo = `<div class="lg-riep">
      <p>Sto per creare in Lantek:</p>
      <div class="lg-num">${nuovi.length ? `<div><b>${nuovi.length}</b><span>pezzi nuovi<br>col disegno</span></div>` : ''}
        <div><b>${tutti.length}</b><span>ordini di<br>produzione</span></div></div>
      <dl><dt>Ordine</dt><dd>${esc(d.commessa || '—')}</dd><dt>Cliente</dt><dd>${esc(d.cliente_lantek || '—')}</dd>
        <dt>Consegna</dt><dd>${esc(cons)}</dd><dt>Macchina</dt><dd>CY Laser 3015 · taglio 2D</dd></dl>
    </div>
    ${rifatti.length ? guidaMsg('warn', `${rifatti.length} codici erano già stati fatti in passato con l’ordine ${esc(d.commessa || '')}`, 'Verranno rifatti: se non è così, torna indietro.') : ''}
    <table class="lg-tab"><thead><tr><th>Codice</th><th>Materiale</th><th class="n">Pezzi</th></tr></thead><tbody>
      ${tutti.map(x => {
        const r = (d.righe || []).find(y => y.codice === x.codice) || {};
        return `<tr><td class="ft-mono">${esc(x.codice)}${x.nuovo ? ' <span class="lt-tag da">nuovo</span>' : ''}${x.gia_fatti ? ' <span class="lt-tag">già fatto</span>' : ''}</td>
          <td>${esc(r.materiale || x.materiale || '')} ${r.spessore != null ? esc(String(r.spessore)) : ''}</td><td class="n">${esc(String(x.quantita))}</td></tr>`;
      }).join('')}</tbody></table>`;
  const piede = `<button class="ft-btn lg" onclick="guidaVai('${o.id}', 1)"><i data-lucide="arrow-left"></i> Indietro</button>
    <button class="ft-btn lg primary" id="lg-manda" onclick="guidaManda('${o.id}')"><i data-lucide="send"></i> Manda a Lantek</button>`;
  return { corpo, piede };
}

/* ── Invio ─────────────────────────────────────────────────────────── */
async function guidaManda(id) {
  const g = _guida[id];
  if (!g || !g.dati || g.invio) return;
  const d = g.dati;
  g.invio = true;
  g.dopo = lvProssimo(id);
  const b = document.getElementById('lg-manda');
  if (b) { b.disabled = true; b.innerHTML = '<i data-lucide="loader-2"></i> Lantek sta lavorando…'; FT.icone(); }
  const corpo = document.querySelector('#lg .lg-corpo');
  if (corpo) corpo.insertAdjacentHTML('afterbegin', guidaMsg('info', 'Lantek sta creando pezzi e ordini…', 'Non chiudere la pagina: può volerci qualche minuto.'));
  FT.icone();
  let r;
  try {
    r = await _post(`${API_URL}/api/orders/${id}/lantek-invia`,
      { codici: (d.da_mandare || []).map(x => x.codice), nuovi: (d.nuovi_da_creare || []).map(x => x.codice) });
  } catch (e) {
    r = { success: false, error: 'Server non raggiungibile' };
  }
  g.invio = false;
  g.esito = r;
  const rap = r.rapporto || {};
  g.riuscito = !!(r.success && !rap.con_errore && r.verificati === r.mandati && r.pezzi_creati === r.pezzi_nuovi);
  if (g.riuscito) {
    // tutto in Lantek: l'ordine passa fra quelli da tagliare
    try { await _post(`${API_URL}/api/orders/${id}/importato`, { annulla: false }); } catch (e) { g.nonSegnato = true; }
    loadOrders(true);
  }
  g.passo = 3;
  const o = ordineAperto();
  if (o && o.id === id) guidaDisegna(o);
}

/* ── Passo 3: fatto ─────────────────────────────────────────────────── */
function guidaPasso3(o, g) {
  const r = g.esito || {};
  const rap = r.rapporto || {};
  const prossimo = g.dopo && (allOrders.find(x => x.id === g.dopo) || null);
  let corpo, piede;
  if (g.riuscito) {
    corpo = guidaMsg('ok', 'Fatto: l’ordine è in Lantek', 'Ho ricontrollato in Lantek: c’è tutto.')
      + `<ul class="lg-ok grande">${r.pezzi_nuovi ? `<li>✓ <b>${r.pezzi_creati}</b> pezzi nuovi creati col disegno</li>` : ''}
          <li>✓ <b>${r.verificati}</b> ordini di produzione creati</li>
          <li>✓ l’ordine è passato fra quelli <b>In Lantek</b>${g.nonSegnato ? ' (non riuscito: premi «Importato in Lantek» dai Disegni)' : ''}</li></ul>`
      + guidaMsg('info', 'Adesso nel MES', 'Lancia in produzione gli ordini nuovi come fai sempre.');
    piede = prossimo
      ? `<button class="ft-btn lg primary" onclick="selectOrder('${prossimo.id}')">Prossimo: ${esc(prossimo.cliente || '')} #${esc(numeroOrdine(prossimo))} <i data-lucide="arrow-right"></i></button>`
      : `<span class="lg-perche">Non ci sono altri ordini da mettere in Lantek.</span>`;
  } else {
    const err = (rap.errori || []).map(e => `<li><span class="ft-mono">${esc(e.pezzo || '?')}</span><span>${esc(e.messaggio || 'errore')}</span></li>`)
      .concat((r.pezzi_non_creati || []).map(c => `<li><span class="ft-mono">${esc(c)}</span><span>pezzo non creato</span></li>`))
      .concat((r.mancanti || []).filter(c => !(rap.errori || []).some(e => e.pezzo === c)).map(c => `<li><span class="ft-mono">${esc(c)}</span><span>ordine di produzione non trovato in Lantek</span></li>`));
    corpo = guidaMsg('err', r.success ? 'Lantek non ha creato tutto' : 'Non mandato a Lantek', esc(r.error || ''))
      + (r.success ? `<p class="lg-nota">Creati: ${r.pezzi_creati || 0} di ${r.pezzi_nuovi || 0} pezzi nuovi, ${r.verificati || 0} di ${r.mandati || 0} ordini di produzione. L’ordine resta fra quelli da importare.</p>` : '')
      + (err.length ? `<ul class="lg-el">${err.join('')}</ul>` : '')
      + guidaMsg('info', 'Cosa fare', 'Premi Ricontrolla: quello che è già in Lantek non viene rimandato (niente doppioni).');
    piede = `<button class="ft-btn lg primary" onclick="guidaRicomincia('${o.id}')"><i data-lucide="refresh-cw"></i> Ricontrolla</button>`;
  }
  return { corpo, piede };
}

function guidaRicomincia(id) {
  const g = _guida[id];
  if (g) { g.passo = 1; g.esito = null; }
  const o = ordineAperto();
  if (o && o.id === id) guidaCarica(o, true);
}

/* Ordini senza codici (solo PDF) o gia' tutti in Lantek: si segna e si va avanti. */
async function guidaSegnaFatto(id) {
  const dopo = lvProssimo(id);
  let d;
  try { d = await _post(`${API_URL}/api/orders/${id}/importato`, { annulla: false }); }
  catch (e) { FT.toast('Server non raggiungibile', 'err'); return; }
  if (!d.success) { FT.toast('Non riuscito: ' + (d.error || 'errore del server'), 'err'); return; }
  FT.toast('In Lantek: ora è fra quelli da tagliare', 'ok', { testo: 'Annulla', fn: () => segnaImportato(id, true) });
  await loadOrders(true);
  if (dopo) selectOrder(dopo); else deselectOrder();
}

/* ── Ordine solo PDF: righe da abbinare ai pezzi di Lantek ─────────────
   Stefano (07/10/2026): nel PDF le righe hanno lo stesso codice dei pezzi in
   Lantek; quelle che in Lantek non ci sono sono assiemi. Si abbinano UNA
   volta ai loro pezzi (o si segnano "non va al laser"): dalla fornitura dopo
   FerroTrack le scompone da solo. */
function guidaSezAbbina(o, g, righe, pdf) {
  g.abbina = g.abbina || {};
  const liberi = (pdf && pdf.liberi) || [];
  return `<section class="lg-sez err"><h3><i data-lucide="link"></i> Righe dell’ordine da abbinare <em>${righe.length}</em></h3>
    <p>Queste righe del PDF non sono in Lantek col loro codice. Dimmi tu cosa sono: me lo ricordo per le prossime forniture.</p>
    <ul class="lg-abb">${righe.map(r => {
      const ed = g.abbina[r.codice];
      const cod = esc(JSON.stringify(r.codice));
      return `<li><div class="lg-abb-riga"><div><span class="ft-mono">${esc(r.codice)}</span> <small>${esc(String(r.quantita))} pz</small>
          <p>${esc(r.descrizione || '')}</p></div>
        <div class="lg-dec-az">
          <button class="ft-btn sm ${ed ? 'primary' : ''}" onclick="guidaAbbinaApri('${o.id}', ${cod})"><i data-lucide="layers"></i> È un assieme</button>
          <button class="ft-btn sm" onclick="guidaAbbinaNonLaser('${o.id}', ${cod})">Non va al laser</button>
        </div></div>
        ${ed ? guidaAbbinaEditor(o, r, ed, liberi) : `<small class="lg-ver-no">Se invece è un pezzo singolo: disegnalo in Lantek con lo stesso codice, poi premi Ricontrolla.</small>`}</li>`;
    }).join('')}</ul></section>`;
}

function guidaAbbinaEditor(o, r, ed, liberi) {
  const cod = esc(JSON.stringify(r.codice));
  const scelti = ed.pezzi;
  const righePezzo = (c, qLantek) => {
    const q = scelti[c];
    const cc = esc(JSON.stringify(c));
    return `<label class="lg-abb-p"><input type="checkbox" ${q != null ? 'checked' : ''} onchange="guidaAbbinaSpunta('${o.id}', ${cod}, ${cc}, this.checked)">
      <span class="ft-mono">${esc(c)}</span>
      <small>${qLantek != null ? `in Lantek ${esc(String(qLantek))} pz` : ''}</small>
      <span><input class="ft-input" type="number" min="1" max="9999" value="${esc(String(q != null ? q : 1))}" ${q != null ? '' : 'disabled'}
        aria-label="Pezzi per un assieme" onchange="guidaAbbinaQta('${o.id}', ${cod}, ${cc}, this.value)"> per assieme</span></label>`;
  };
  const liberiCod = new Set(liberi.map(x => x.codice));
  const extra = Object.keys(scelti).filter(c => !liberiCod.has(c));
  const trovati = (ed.trovati || []).filter(x => !liberiCod.has(x.codice) && scelti[x.codice] == null);
  const n = Object.keys(scelti).length;
  return `<div class="lg-abb-ed">
    <h4>Di quali pezzi di Lantek è fatto ${esc(r.codice)}? (quanti per UN assieme)</h4>
    ${liberi.length ? `<p>Pezzi che hai messo in Lantek con quest’ordine e che nessuna riga spiega:</p>${liberi.map(x => righePezzo(x.codice, x.quantita)).join('')}`
      : `<p>In Lantek non ci sono pezzi di quest’ordine senza riga: cercali per codice.</p>`}
    ${extra.map(c => righePezzo(c, null)).join('')}
    <div class="lg-abb-cerca"><input class="ft-input" placeholder="Cerca un pezzo in Lantek (almeno 3 caratteri del codice)"
        value="${esc(ed.q || '')}" onkeydown="if(event.key==='Enter')guidaAbbinaCerca('${o.id}', ${cod}, this.value)">
      <button class="ft-btn sm" onclick="guidaAbbinaCerca('${o.id}', ${cod}, this.previousElementSibling.value)"><i data-lucide="search"></i> Cerca</button></div>
    ${ed.cercaErr ? `<small class="lg-ver-no">${esc(ed.cercaErr)}</small>` : ''}
    ${trovati.map(x => `<div class="lg-abb-p"><span></span><span class="ft-mono">${esc(x.codice)}</span>
        <small>${esc(x.materiale || '')} ${x.spessore != null ? esc(String(x.spessore)) + ' mm' : ''}</small>
        <button class="ft-btn sm" onclick="guidaAbbinaSpunta('${o.id}', ${cod}, ${esc(JSON.stringify(x.codice))}, true)">Aggiungi</button></div>`).join('')}
    <div class="lg-dec-az">
      <button class="ft-btn sm" onclick="guidaAbbinaChiudi('${o.id}', ${cod})">Annulla</button>
      <button class="ft-btn sm primary" ${n ? '' : 'disabled'} onclick="guidaAbbinaSalva('${o.id}', ${cod})"><i data-lucide="check"></i> Salva: ${n} pezzi per assieme</button>
    </div></div>`;
}

/* Gia' abbinati (si possono rifare se sbagliati). */
function guidaAbbinatiFatti(o, pdf) {
  const ass = pdf.assiemi || [], nl = pdf.non_laser || [];
  if (!ass.length && !nl.length) return '';
  const li = (x, testo) => `<li><div><span class="ft-mono">${esc(x.codice)}</span> <small>${esc(String(x.quantita))} pz</small><br><small>${testo}</small></div>
    <button class="ft-btn sm" onclick="guidaAbbinaDimentica('${o.id}', ${esc(JSON.stringify(x.codice))})">Rifai</button></li>`;
  return `<details class="lg-abb-fatti"><summary>${ass.length ? `<b>${ass.length}</b> assiemi scomposti nei loro pezzi Lantek` : ''}${ass.length && nl.length ? ' · ' : ''}${nl.length ? `<b>${nl.length}</b> righe che non vanno al laser` : ''}</summary>
    <ul class="lg-ok">${ass.map(x => li(x, x.pezzi.map(p => `${esc(p.codice)} ×${esc(String(p.quantita))}`).join(', '))).join('')}
      ${nl.map(x => li(x, 'non va al laser')).join('')}</ul></details>`;
}

function _guidaRidisegna(id) {
  const o = ordineAperto();
  if (o && o.id === id) guidaDisegna(o);
}

function guidaAbbinaApri(id, codice) {
  const g = _guida[id];
  if (!g) return;
  g.abbina = g.abbina || {};
  if (g.abbina[codice]) { delete g.abbina[codice]; _guidaRidisegna(id); return; }
  const r = ((g.dati.pdf || {}).da_abbinare || []).find(x => x.codice === codice) || {};
  const pezzi = {};
  for (const p of r.proposta || []) pezzi[p.codice] = p.quantita;
  g.abbina[codice] = { pezzi };
  _guidaRidisegna(id);
}

function guidaAbbinaChiudi(id, codice) {
  const g = _guida[id];
  if (g && g.abbina) delete g.abbina[codice];
  _guidaRidisegna(id);
}

function guidaAbbinaSpunta(id, codice, pezzo, si) {
  const ed = ((_guida[id] || {}).abbina || {})[codice];
  if (!ed) return;
  if (si) {
    const r = ((_guida[id].dati.pdf || {}).da_abbinare || []).find(x => x.codice === codice) || {};
    const lib = ((_guida[id].dati.pdf || {}).liberi || []).find(x => x.codice === pezzo);
    ed.pezzi[pezzo] = lib && r.quantita ? Math.max(1, Math.round(lib.quantita / r.quantita)) : 1;
  } else delete ed.pezzi[pezzo];
  _guidaRidisegna(id);
}

function guidaAbbinaQta(id, codice, pezzo, v) {
  const ed = ((_guida[id] || {}).abbina || {})[codice];
  const n = parseInt(v, 10);
  if (ed && ed.pezzi[pezzo] != null && n >= 1) ed.pezzi[pezzo] = n;
}

async function guidaAbbinaCerca(id, codice, testo) {
  const ed = ((_guida[id] || {}).abbina || {})[codice];
  if (!ed) return;
  ed.q = String(testo || '').trim();
  if (ed.q.length < 3) { ed.cercaErr = 'Scrivi almeno 3 caratteri del codice'; ed.trovati = []; _guidaRidisegna(id); return; }
  const { ok, d } = await _getJson(`${API_URL}/api/lantek/pezzi?q=${encodeURIComponent(ed.q)}`);
  ed.trovati = ok && d && d.success ? d.pezzi : [];
  ed.cercaErr = !(ok && d && d.success) ? ((d && d.error) || 'Lantek non risponde')
    : !ed.trovati.length ? 'Nessun pezzo in Lantek con questo codice' : null;
  _guidaRidisegna(id);
}

async function _guidaAbbinaManda(id, corpo, messaggio) {
  let r;
  try { r = await _post(`${API_URL}/api/orders/${id}/lantek-abbina`, corpo); }
  catch (e) { r = { success: false, error: 'Server non raggiungibile' }; }
  if (!r.success) { FT.toast('Non salvato: ' + (r.error || 'errore'), 'err'); return; }
  const g = _guida[id];
  if (g && g.abbina) delete g.abbina[corpo.codice];
  FT.toast(messaggio, 'ok');
  const o = ordineAperto();
  if (o && o.id === id) guidaCarica(o, true);
}

function guidaAbbinaSalva(id, codice) {
  const ed = ((_guida[id] || {}).abbina || {})[codice];
  if (!ed) return;
  const pezzi = Object.entries(ed.pezzi).map(([c, q]) => ({ codice: c, quantita: q }));
  _guidaAbbinaManda(id, { codice, pezzi }, `${codice}: me lo ricordo per le prossime forniture`);
}

function guidaAbbinaNonLaser(id, codice) {
  _guidaAbbinaManda(id, { codice, non_laser: true }, `${codice}: non va al laser, la prossima volta la salto`);
}

function guidaAbbinaDimentica(id, codice) {
  _guidaAbbinaManda(id, { codice, dimentica: true }, `${codice}: da abbinare di nuovo`);
}
