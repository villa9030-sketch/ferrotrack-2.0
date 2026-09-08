"""Validazione dei dati economici del preventivo (sezione 10.6).

Prima ogni numero passava per `float(x or 0)`: un costo negativo, un valore
non finito o una quantita' assurda entravano a database senza un fiato, e si
scoprivano solo guardando un totale che non tornava.

Regole:
- si puo' SALVARE UNA BOZZA INCOMPLETA: zeri e campi vuoti non sono errori;
- non si accettano valori IMPOSSIBILI: costi negativi, numeri non finiti,
  quantita' nulle o fuori scala;
- l'errore dice sempre QUALE riga e QUALE campo, altrimenti l'utente deve
  cercarlo a mano in una distinta da cento pezzi.
"""
import math

# Limiti di buon senso per una carpenteria: servono a fermare gli errori di
# battitura (un costo da un milione, 999999 pezzi), non a limitare il lavoro.
COSTO_MAX = 1_000_000.0
QUANTITA_MAX = 100_000
PERCENTUALE_MAX = 1000.0

CAMPI_COSTO_ARTICOLO = (
    'costo_materiale', 'costo_base_stimato', 'costo_base_override',
    'costo_piega', 'costo_saldatura', 'costo_filettatura', 'costo_svasatura',
    'costo_apporto', 'costo_pulizia',
)
CAMPI_COSTO_ASSIEME = ('costo', 'costo_puntatura', 'costo_saldatura_assieme')
CAMPI_COSTO_TUBOLARE = ('costo_materiale', 'costo_taglio_totale')
CAMPI_COSTO_PIASTRA = ('costo',)


def _etichetta(riga: dict, indice: int) -> str:
    codice = (riga.get('codice') or riga.get('codice_assieme')
              or riga.get('profilo') or '').strip()
    return f'riga {indice + 1}' + (f' ({codice})' if codice else '')


def valida_costo(valore, campo: str, dove: str):
    """None se va bene, altrimenti il messaggio d'errore."""
    if valore is None or valore == '':
        return None                      # campo non compilato: legittimo in bozza
    try:
        v = float(valore)
    except (TypeError, ValueError):
        return f'{dove}: "{campo}" non e\' un numero ({valore!r})'
    if not math.isfinite(v):
        return f'{dove}: "{campo}" non e\' un valore finito'
    if v < 0:
        return f'{dove}: "{campo}" non puo\' essere negativo ({v})'
    if v > COSTO_MAX:
        return (f'{dove}: "{campo}" fuori scala ({v:,.2f} EUR). '
                f'Massimo ammesso {COSTO_MAX:,.0f} EUR.')
    return None


def valida_quantita(valore, campo: str, dove: str):
    if valore is None or valore == '':
        return None
    try:
        v = int(valore)
    except (TypeError, ValueError):
        return f'{dove}: "{campo}" non e\' un numero intero ({valore!r})'
    if v < 1:
        return f'{dove}: "{campo}" deve essere almeno 1 (ricevuto {v})'
    if v > QUANTITA_MAX:
        return f'{dove}: "{campo}" fuori scala ({v}). Massimo {QUANTITA_MAX}.'
    return None


def valida_conteggio(valore, campo: str, dove: str):
    """Conteggio di lavorazioni: ZERO e' normale (un pezzo senza pieghe), il
    negativo no. Diverso dalla quantita' di pezzi, che parte da 1."""
    if valore is None or valore == '':
        return None
    try:
        v = int(valore)
    except (TypeError, ValueError):
        return f'{dove}: "{campo}" non e\' un numero intero ({valore!r})'
    if v < 0:
        return f'{dove}: "{campo}" non puo\' essere negativo ({v})'
    if v > QUANTITA_MAX:
        return f'{dove}: "{campo}" fuori scala ({v}). Massimo {QUANTITA_MAX}.'
    return None


def _valida_righe(righe, campi_costo, campi_quantita, campi_conteggio=()):
    errori = []
    for i, r in enumerate(righe or []):
        if not isinstance(r, dict):
            errori.append(f'riga {i + 1}: formato non valido')
            continue
        dove = _etichetta(r, i)
        for c in campi_costo:
            e = valida_costo(r.get(c), c, dove)
            if e:
                errori.append(e)
        for c in campi_quantita:
            e = valida_quantita(r.get(c), c, dove)
            if e:
                errori.append(e)
        for c in campi_conteggio:
            e = valida_conteggio(r.get(c), c, dove)
            if e:
                errori.append(e)
    return errori


def valida_articoli(articoli):
    # `quantita` sono i pezzi: almeno 1. Pieghe, filettature e svasature sono
    # conteggi di lavorazioni: zero e' del tutto normale.
    return _valida_righe(articoli, CAMPI_COSTO_ARTICOLO, ('quantita',),
                         ('pieghe', 'filettatura_pz', 'svasatura_pz'))


def valida_assiemi(assiemi):
    return _valida_righe(assiemi, CAMPI_COSTO_ASSIEME, ('qty',))


def valida_tubolari(tubolari):
    return _valida_righe(tubolari, CAMPI_COSTO_TUBOLARE, ())


def valida_piastre(piastre):
    return _valida_righe(piastre, CAMPI_COSTO_PIASTRA, ())


def valida_testata(dati: dict):
    """Quantita' di lotto, ricarico e sconto del preventivo."""
    errori = []
    e = valida_quantita(dati.get('quantita'), 'quantita', 'testata')
    if e:
        errori.append(e)
    for campo in ('margine_pct', 'sconto_pct'):
        v = dati.get(campo)
        if v is None or v == '':
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            errori.append(f'testata: "{campo}" non e\' un numero ({v!r})')
            continue
        if not math.isfinite(f):
            errori.append(f'testata: "{campo}" non e\' un valore finito')
        elif f < 0:
            errori.append(f'testata: "{campo}" non puo\' essere negativa ({f})')
        elif campo == 'sconto_pct' and f > 100:
            errori.append(f'testata: uno sconto del {f}% renderebbe il prezzo negativo')
        elif f > PERCENTUALE_MAX:
            errori.append(f'testata: "{campo}" fuori scala ({f}%)')
    return errori


def messaggio(errori, limite: int = 5) -> str:
    """Errori raccolti in un messaggio leggibile, senza sommergere l'utente."""
    if not errori:
        return ''
    testo = '; '.join(errori[:limite])
    if len(errori) > limite:
        testo += f' (e altri {len(errori) - limite})'
    return testo
