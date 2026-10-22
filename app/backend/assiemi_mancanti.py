"""Assiemi dell'ordine senza i loro componenti (e righe senza disegno).

Caso vero, DECA A 001252 (20/10/2026): il pacchetto e' stato caricato coi
soli 27 disegni della radice, senza le 7 sottocartelle degli assiemi. Il PDF
d'ordine elencava gli assiemi ("25NDSSA1021 Spalla DX ... 1,00 Nr"):
FerroTrack li ha creati come righe normali senza disegno, nessuno ha avvisato,
l'ordine e' andato in Lantek ed e' stato tagliato senza 45 componenti.

Regola: una riga dell'ordine che E' un assieme deve avere i suoi componenti
(la sottocartella coi disegni). Se non li ha:
  - all'ufficio il caricamento si ferma (si crea solo con "Crea lo stesso",
    scritto e registrato);
  - al laser la riga e' rossa e bloccata: "Manda a Lantek" e "Taglio fatto"
    chiedono una conferma esplicita.

Come si riconosce un assieme (convenzione DECA: SA = assieme, PA =
particolare): il codice ha "SA" seguito da cifre (25NDSSA1021, 08SA01057);
per gli altri clienti, una descrizione da assieme (saldata, struttura,
assieme, spalla, telaio) su una riga senza disegno e senza codice da
particolare. Una riga SA che ha il SUO disegno alla radice del pacchetto
(DECA: 25NDSSA1076 "Carter Dx", un pezzo piegato) e' un pezzo da tagliare,
non un assieme senza componenti.
"""
from __future__ import annotations

import re

RX_ASSIEME = re.compile(r'SA\d{3,}', re.I)
RX_PARTICOLARE = re.compile(r'PA\d{3,}', re.I)
PAROLE_ASSIEME = ('saldat', 'struttura', 'assieme', 'spalla', 'telaio')


def chiave(codice) -> str:
    """'25NDSSA1021-00' -> '25NDSSA1021': la cartella dell'assieme ha la
    revisione, la riga dell'ordine spesso no."""
    c = str(codice or '').strip().upper()
    c = re.sub(r'\.(DXF|DWG|PDF)$', '', c)
    return re.sub(r'[-_ ]\d{1,2}$', '', c)


def e_assieme(codice, descrizione='') -> bool:
    """La riga dell'ordine e' un assieme (codice SA o descrizione da assieme)?"""
    cod = str(codice or '')
    if RX_ASSIEME.search(cod):
        return True
    if RX_PARTICOLARE.search(cod):
        return False
    d = str(descrizione or '').lower()
    return any(p in d for p in PAROLE_ASSIEME)


def messaggio(codici: list) -> str:
    """Il testo del blocco per l'ufficio (e per il laser)."""
    n = len(codici)
    elenco = ', '.join(codici[:12]) + (f' e altri {n - 12}' if n > 12 else '')
    cosa = "dell'assieme" if n == 1 else f'di {n} assiemi'
    return (f'Mancano i disegni {cosa}: {elenco}. Carica la cartella intera '
            '(trascina la cartella dell\'ordine, non i singoli file): '
            'gli assiemi hanno i componenti nelle sottocartelle.')


# ---------------------------------------------------------------------------
# All'ufficio: il pacchetto letto (rfq_importer.RFQParseResult)
# ---------------------------------------------------------------------------
def dal_pacchetto(result, componenti=None) -> list:
    """Le righe dell'ordine che sono assiemi senza componenti nel pacchetto:
    [{codice, descrizione, quantita, motivo}] nell'ordine del PDF.

    Senza componenti = nessun pezzo del pacchetto sta sotto quell'assieme
    (nessuna sottocartella con disegni). `componenti`: codici d'assieme che
    hanno pezzi assegnati DOPO la lettura (distinta del disegno d'insieme:
    DECA A 001221 TER, componenti fuori dalla cartella). Senza `componenti`
    (lettura non ancora finita) la cartella col solo disegno d'insieme non
    si segnala: la distinta puo' ancora trovargli i pezzi. Due casi:
      - riga d'assieme senza nessun disegno (il caso 1252: cartella non caricata);
      - cartella con il solo disegno d'insieme, senza i pezzi.
    """
    articoli = list(getattr(result, 'articoli', None) or [])
    con_componenti = {chiave(a.codice_assieme) for a in articoli
                      if getattr(a, 'codice_assieme', None) and not getattr(a, 'e_master', False)}
    if componenti is not None:
        con_componenti |= {chiave(c) for c in componenti if c}
    out, visti = [], set()
    for a in articoli:
        if not getattr(a, 'righe_ordine', None):
            continue                     # non e' una riga dell'ordine
        k = chiave(a.codice)
        if k in visti:
            continue
        master = bool(getattr(a, 'e_master', False))
        if master and componenti is None:
            continue
        if not master and getattr(a, 'matched_dxf', None):
            continue                     # ha il suo disegno: e' un pezzo
        if not master and not e_assieme(a.codice, getattr(a, 'descrizione', '')):
            continue                     # riga senza disegno qualsiasi (avviso, non blocco)
        if k in con_componenti or chiave(getattr(a, 'codice_assieme', None) or '') in con_componenti:
            continue
        visti.add(k)
        out.append({'codice': a.codice,
                    'descrizione': (getattr(a, 'descrizione', '') or '').strip()[:200],
                    'quantita': int(getattr(a, 'quantita', 0) or 0) or 1,
                    'solo_insieme': master,
                    'motivo': ("c'e' solo il disegno d'insieme, mancano i pezzi" if master
                               else 'nessun disegno: manca la sottocartella coi componenti')})
    return out


# ---------------------------------------------------------------------------
# Al laser: la distinta dell'ordine (righe di app._distinta_lantek)
# ---------------------------------------------------------------------------
def assiemi_senza_componenti(righe: list) -> set:
    """Chiavi dei codici d'assieme della distinta che non hanno nessun pezzo
    sotto (nessuna riga con 'assieme' uguale al loro codice)."""
    righe = [r for r in righe or [] if r.get('codice')]
    con = {chiave(r.get('assieme')) for r in righe if r.get('assieme') and r.get('tipo') != 'assieme'}
    out = set()
    for r in righe:
        if r.get('disegno') or r.get('foglio'):
            if r.get('tipo') != 'assieme':
                continue
        if r.get('tipo') == 'assieme' or e_assieme(r.get('codice'), r.get('descrizione')):
            k = chiave(r['codice'])
            if k not in con:
                out.add(k)
    return out


COSA_ASSIEME = ("Assieme senza componenti: mancano i disegni. Fatti mandare la cartella "
                "dell'assieme (l'ufficio la aggiunge all'ordine). Non tagliare l'ordine "
                "come se fosse completo.")
COSA_SENZA_DISEGNO = ("Riga dell'ordine senza disegno: fatti mandare il disegno, oppure "
                      "segnala «non è da laser» (tasto N) se si compra o si fa altrove.")
