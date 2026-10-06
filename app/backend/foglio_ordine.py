"""Foglio d'ordine A4 da stampare, per gli ordini SENZA il PDF del cliente.

In officina l'ordine si riconosce dal PDF d'ordine stampato e messo nel
faldone (i cartellini A6 col barcode non ci sono piu'). Gli ordini arrivati
col PDF del cliente stampano quello; quelli nati da un preventivo non hanno un
PDF, e per loro si stampa questo foglio: testata (cliente, numero, consegna,
note) e i pezzi raggruppati per lamiera, ognuno con le lavorazioni.

I pezzi sono la stessa distinta che mostra il tablet dell'officina
(_distinta_ordine in app.py) e le lavorazioni passano dalla stessa traduzione
(tablet_officina.lavorazioni_officina): ufficio e officina leggono le stesse
cose con le stesse parole.

Solo funzioni pure (niente database, niente Flask): si provano da sole.
"""
from html import escape

from . import tablet_officina as tab

# Pezzi che passano dal laser: si raggruppano per lamiera (materiale + spessore).
_TIPI_LAMIERA = ('lamiera', 'piastra', 'articolo')


def _num(v):
    """Spessore leggibile: 2.0 -> "2", 1.5 -> "1,5"."""
    try:
        return ('%g' % float(v)).replace('.', ',')
    except (TypeError, ValueError):
        return str(v)


def _data_it(iso) -> str:
    """"2026-10-21..." -> "21/10/2026" (la consegna e' un giorno, non un istante)."""
    t = str(iso or '')[:10]
    if len(t) == 10 and t[4] == '-' and t[7] == '-':
        return f'{t[8:10]}/{t[5:7]}/{t[0:4]}'
    return t


def gruppi_per_lamiera(righe) -> list:
    """[(titolo, [righe])]: prima le lamiere (dallo spessore piu' sottile),
    poi i tubolari, poi gli assiemi da montare, poi il resto."""
    lamiere, tubi, assiemi, altro = {}, [], [], []
    for r in righe or []:
        tipo = r.get('tipo')
        if tipo in _TIPI_LAMIERA:
            chiave = ((r.get('materiale') or '').strip(), r.get('spessore_mm'))
            lamiere.setdefault(chiave, []).append(r)
        elif tipo == 'tubolare':
            tubi.append(r)
        elif tipo == 'assieme':
            assiemi.append(r)
        else:
            altro.append(r)

    def ordine(k):
        mat, sp = k
        try:
            sp = float(sp)
        except (TypeError, ValueError):
            sp = float('inf')
        return (sp, mat.lower())

    out = []
    for mat, sp in sorted(lamiere, key=ordine):
        # INOX_304 -> INOX 304: il codice interno non va sul foglio dell'officina
        leggibile = mat.replace('_', ' ')
        titolo = 'Lamiera' + (f' {leggibile}' if mat else '') + (f' sp. {_num(sp)} mm' if sp else '')
        out.append((titolo, lamiere[(mat, sp)]))
    if tubi:
        out.append(('Tubolari', tubi))
    if assiemi:
        out.append(('Assiemi da montare', assiemi))
    if altro:
        out.append(('Altri pezzi', altro))
    return out


def html_foglio(ordine: dict, righe) -> str:
    """La pagina HTML del foglio d'ordine, pronta per la stampa su A4.

    ordine: {numero, numero_ordine, cliente, data_consegna, note}
    righe: la distinta dell'ordine (codice, descrizione, tipo, quantita,
           materiale, spessore_mm, lavorazioni, assieme).
    """
    e = lambda v: escape(str(v if v is not None else ''))  # noqa: E731
    numero = ordine.get('numero') or ordine.get('numero_ordine') or ''
    nostro = ordine.get('numero_ordine') or ''
    gruppi = gruppi_per_lamiera(righe)
    tot = sum(int(r.get('quantita') or 0) for r in (righe or []) if r.get('tipo') != 'assieme')

    corpo = []
    for titolo, rr in gruppi:
        n = sum(int(r.get('quantita') or 0) for r in rr)
        corpo.append(f'<h2>{e(titolo)} <small>{n} pz</small></h2>')
        corpo.append('<table><thead><tr><th class="cod">Codice</th><th>Descrizione</th>'
                     '<th class="ass">Assieme</th><th class="q">Q.t&agrave;</th>'
                     '<th>Lavorazioni</th></tr></thead><tbody>')
        for r in rr:
            lav = tab.lavorazioni_officina(r.get('lavorazioni'))
            # Il solo taglio non e' una lavorazione d'officina, ma il campo
            # vuoto farebbe pensare a un dato mancante.
            lav_txt = ', '.join(lav) if lav else 'solo taglio'
            corpo.append(
                f'<tr><td class="cod">{e(r.get("codice"))}</td>'
                f'<td>{e(r.get("descrizione"))}</td>'
                f'<td class="ass">{e(r.get("assieme") or "")}</td>'
                f'<td class="q">{int(r.get("quantita") or 0)}</td>'
                f'<td>{e(lav_txt)}</td></tr>')
        corpo.append('</tbody></table>')
    if not gruppi:
        corpo.append('<p class="vuoto">Quest\'ordine non ha una distinta dei pezzi.</p>')

    note = (ordine.get('note') or '').strip()
    rif = (f'<div><span>Nostro numero</span><b>{e(nostro)}</b></div>'
           if nostro and nostro != numero else '')
    return f'''<!DOCTYPE html>
<html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ordine {e(numero)} &middot; {e(ordine.get("cliente"))}</title>
<style>
@page {{ size: A4; margin: 14mm 12mm; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; padding: 24px; background: #fff; color: #111;
       font: 12pt/1.35 Arial, Helvetica, sans-serif; }}
.barra {{ display: flex; justify-content: flex-end; margin-bottom: 12px; }}
.barra button {{ font: 600 14px Arial, sans-serif; padding: 9px 18px; border-radius: 8px;
                 border: 1px solid #1a7a48; background: #1a7a48; color: #fff; cursor: pointer; }}
header {{ border-bottom: 2px solid #111; padding-bottom: 8px; margin-bottom: 10px; }}
h1 {{ font-size: 22pt; margin: 0 0 6px; }}
.testa {{ display: flex; flex-wrap: wrap; gap: 6px 28px; }}
.testa span {{ display: block; font-size: 8.5pt; text-transform: uppercase; color: #555; }}
.testa b {{ font-size: 13pt; }}
.note {{ margin: 8px 0 0; padding: 6px 8px; border: 1px solid #999; white-space: pre-wrap; }}
h2 {{ font-size: 13pt; margin: 16px 0 4px; break-after: avoid; }}
h2 small {{ font-weight: normal; color: #555; }}
table {{ width: 100%; border-collapse: collapse; font-size: 10.5pt; }}
th, td {{ border: 1px solid #999; padding: 3px 6px; text-align: left; vertical-align: top; }}
th {{ background: #eee; font-size: 9pt; }}
tr {{ break-inside: avoid; }}
td.q, th.q {{ text-align: right; width: 48px; font-weight: bold; }}
td.cod {{ font-family: Consolas, monospace; white-space: nowrap; }}
td.ass, th.ass {{ width: 110px; }}
.vuoto {{ color: #555; }}
@media print {{ body {{ padding: 0; }} .barra {{ display: none; }} }}
</style></head>
<body>
<div class="barra"><button type="button" onclick="window.print()">Stampa</button></div>
<header>
  <h1>Ordine {e(numero)}</h1>
  <div class="testa">
    <div><span>Cliente</span><b>{e(ordine.get("cliente"))}</b></div>
    <div><span>Consegna</span><b>{e(_data_it(ordine.get("data_consegna")) or "-")}</b></div>
    {rif}
    <div><span>Pezzi</span><b>{tot}</b></div>
  </div>
  {f'<div class="note">{e(note)}</div>' if note else ''}
</header>
{''.join(corpo)}
</body></html>'''
