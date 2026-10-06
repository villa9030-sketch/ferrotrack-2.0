"""Tablet officina: cosa c'e' da fare, per reparto, e a che punto e' il taglio.

Il tablet appeso in officina deve dire in un colpo d'occhio quali ordini
servono a chi (piega, saldatura, filettatura, montaggio) e quanto e' gia'
tagliato. Le lavorazioni pero' sono scritte nella distinta come testi pensati
per le persone ("Piegatura (3 pieghe)", "Filettatura (4)", "Montaggio"...),
e arrivano da tre strade diverse (preventivo, pacchetto dell'ufficio, ordine
a mano). Qui c'e' UNA sola traduzione da quei testi ai reparti: la pagina non
deve indovinare, e se un domani il preventivatore cambia una parola si
corregge in un posto solo.

Solo funzioni pure (niente database, niente Flask): si provano da sole.
"""
import re
import unicodedata

# I reparti dell'officina, nell'ordine in cui li mostra il tablet.
REPARTI = (
    ('piega', 'Piega'),
    ('saldatura', 'Saldatura'),
    ('filettatura', 'Filettatura e svasatura'),
    ('montaggio', 'Montaggio'),
)
ETICHETTA_REPARTO = dict(REPARTI)

# Parole che portano un pezzo in un reparto. Si cerca la RADICE, senza
# accenti e senza maiuscole: "Piegatura", "piega", "PIEGHE" vanno tutte bene.
# Puntatura: la fa chi monta e chi salda, quindi interessa a tutti e due.
_PAROLE = (
    ('piega', ('pieg', 'bend')),
    ('saldatura', ('sald', 'puntat', 'weld')),
    ('filettatura', ('filett', 'svas', 'masch', 'thread')),
    ('montaggio', ('montag', 'assembl', 'puntat')),
)

# Le lavorazioni che sono solo taglio non portano il pezzo in nessun reparto
# dell'officina: le fa il laser (o la sega per i tubi).
_SOLO_TAGLIO = re.compile(r'^\s*taglio\b', re.IGNORECASE)


def _piano(t: str) -> str:
    """Minuscolo e senza accenti: "Saldatura" e "saldatura" sono la stessa cosa."""
    t = unicodedata.normalize('NFKD', str(t or ''))
    return ''.join(c for c in t if not unicodedata.combining(c)).lower()


def testo_lavorazione(l) -> str:
    """Una lavorazione come testo, anche se arriva come dizionario
    (gli ordini a mano possono avere {nome, quantita})."""
    if l is None:
        return ''
    if not isinstance(l, dict):
        return str(l).strip()
    nome = l.get('nome') or l.get('tipo') or l.get('descrizione') or l.get('label') or ''
    n = l.get('quantita', l.get('n', l.get('numero')))
    try:
        n = int(n) if n not in (None, '') else None
    except (TypeError, ValueError):
        n = None
    return f'{nome} ({n})' if n and n != 1 else str(nome).strip()


def reparti_di(lavorazioni, tipo: str = None) -> list:
    """I reparti che devono mettere mano al pezzo, nell'ordine di REPARTI.

    Un assieme va sempre al montaggio, anche se nel preventivo non c'erano
    ore di montaggio: un assieme e' per definizione qualcosa da montare."""
    trovati = set()
    for l in lavorazioni or []:
        t = _piano(testo_lavorazione(l))
        if not t or _SOLO_TAGLIO.match(t):
            continue
        for rep, radici in _PAROLE:
            if any(r in t for r in radici):
                trovati.add(rep)
    if tipo == 'assieme':
        trovati.add('montaggio')
    return [k for k, _ in REPARTI if k in trovati]


def pieghe_di(lavorazioni) -> int:
    """Quante pieghe ha il pezzo: "Piegatura (3 pieghe)" -> 3. Zero se non piega.

    Una piegatura senza numero conta 1: si sa che va piegato, non quante volte."""
    for l in lavorazioni or []:
        t = _piano(testo_lavorazione(l))
        if 'pieg' not in t and 'bend' not in t:
            continue
        m = re.search(r'(\d+)', t)
        return int(m.group(1)) if m else 1
    return 0


def lavorazioni_officina(lavorazioni) -> list:
    """Le lavorazioni da mostrare sul pezzo: tutte tranne il solo taglio."""
    out = []
    for l in lavorazioni or []:
        t = testo_lavorazione(l)
        if t and not _SOLO_TAGLIO.match(t):
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# Taglio dei pezzi
# ---------------------------------------------------------------------------
# I pezzi che passano dal laser. Tubi (sega) e assiemi (si montano) no.
_TIPI_LASER = ('lamiera', 'piastra', 'articolo')


def tagliati_da_lantek(order, righe) -> dict | None:
    """AGGANCIO per il taglio PEZZO PER PEZZO letto dalla lista di Lantek.

    Oggi FerroTrack sa solo se l'ORDINE e' tagliato (taglio_completato), se e'
    in Lantek (importato_lantek_il) o se aspetta il laser: pezzo per pezzo non
    sa niente, e qui non si inventa niente. Torna None, e chi chiama ricava lo
    stato dei pezzi da quello dell'ordine.

    Quando ci sara' l'import della lista dei pezzi tagliati di Lantek
    (PROGETTO_STAZIONI.md, "Da Lantek"), basta far tornare qui
        {codice_pezzo: quantita_tagliata, ...}
    per gli ordini che ce l'hanno: stato_taglio_pezzi lo usa al posto della
    stima, la pagina mostra "tagliati 18 su 22" sul pezzo e "N pezzi su M
    tagliati" sull'ordine senza altre modifiche.
    """
    return None


def stato_taglio_pezzi(order, righe, stato_ordine: str, per_pezzo: dict | None = None):
    """Stato del taglio di ogni riga della distinta + il riepilogo dell'ordine.

    stato_ordine: quello di ordini_service.stato_taglio (tagliato |
    da_tagliare | da_smistare | non_serve).
    per_pezzo: {codice: tagliati} da tagliati_da_lantek, oppure None.

    Ritorna (stati, riepilogo): stati e' una lista parallela a righe con
    {stato, tagliati, totale}; stato e' uno fra
        tagliato | parziale | in_lantek | da_tagliare | non_laser
    riepilogo = {tagliati, totale, fonte}: fonte 'lantek' se i numeri vengono
    pezzo per pezzo, 'ordine' se sono ricavati dallo stato dell'ordine (e
    allora sono "tutti" o "nessuno", mai una via di mezzo inventata).
    """
    in_lantek = bool(getattr(order, 'importato_lantek_il', None))
    stati, tot, fatti = [], 0, 0
    for r in righe:
        q = int(r.get('quantita') or 0)
        if r.get('tipo') not in _TIPI_LASER or stato_ordine == 'non_serve':
            stati.append({'stato': 'non_laser', 'tagliati': None, 'totale': q})
            continue
        tot += q
        if per_pezzo is not None and (r.get('codice') or '') in per_pezzo:
            n = max(0, min(q, int(per_pezzo.get(r.get('codice') or '') or 0)))
            st = 'tagliato' if n >= q else ('parziale' if n > 0 else
                                            ('in_lantek' if in_lantek else 'da_tagliare'))
        elif stato_ordine == 'tagliato':
            n, st = q, 'tagliato'
        else:
            n, st = 0, ('in_lantek' if in_lantek and stato_ordine == 'da_tagliare' else 'da_tagliare')
        fatti += n
        stati.append({'stato': st, 'tagliati': n, 'totale': q})
    return stati, {'tagliati': fatti, 'totale': tot,
                   'fonte': 'lantek' if per_pezzo is not None else 'ordine'}


# ---------------------------------------------------------------------------
# Segnalazioni dall'officina
# ---------------------------------------------------------------------------
TIPI_SEGNALAZIONE = {
    'manca_materiale': 'Manca materiale',
    'disegno': 'Disegno sbagliato o poco chiaro',
    'da_rifare': 'Pezzo da rifare',
    'altro': 'Altro',
}
MAX_NOTA = 300
MAX_CODICE = 80
MAX_DA = 60
# Lo stesso problema sullo stesso pezzo, premuto due volte (o da due ragazzi
# davanti allo stesso tablet), non deve arrivare due volte all'ufficio.
MINUTI_DOPPIONE = 10
AZIONE_AUDIT = 'SEGNALAZIONE_OFFICINA'


def chiave_segnalazione(tipo: str, codice: str) -> str:
    """Inizio del dettaglio nell'audit: identifica il problema per i doppioni."""
    return f"{TIPI_SEGNALAZIONE[tipo]} | pezzo: {codice or '-'} |"


def leggi_segnalazione(detail: str) -> dict | None:
    """Il contrario di chiave_segnalazione: dal testo dell'audit a {tipo, codice, nota, da}."""
    parti = [p.strip() for p in str(detail or '').split(' | ')]
    if not parti:
        return None
    tipo = next((k for k, v in TIPI_SEGNALAZIONE.items() if v == parti[0]), None)
    if not tipo:
        return None
    out = {'tipo': tipo, 'etichetta': parti[0], 'codice_pezzo': None, 'nota': '', 'da': ''}
    for p in parti[1:]:
        if p.startswith('pezzo:'):
            c = p[len('pezzo:'):].strip()
            out['codice_pezzo'] = None if c == '-' else c
        elif p.startswith('nota:'):
            n = p[len('nota:'):].strip()
            out['nota'] = '' if n == '-' else n
        elif p.startswith('da:'):
            out['da'] = p[len('da:'):].strip()
    return out
