"""Calcolo AUTOREVOLE del prezzo di un preventivo.

Perche' esiste
--------------
Il prezzo veniva calcolato solo nel browser, e il backend salvava il numero che
riceveva senza verificarlo. Peggio: le due formule presenti nel JavaScript non
coincidevano fra loro.

  - l'EDITOR sommava articoli sciolti + assiemi (con i loro componenti,
    tubolari e piastre) + tubolari e piastre sciolti, applicava i costi
    generali e poi il ricarico;
  - l'ACCETTAZIONE sommava TUTTI gli articoli (compresi quelli gia' contati
    dentro un assieme) e ignorava assiemi, tubolari, piastre e costi generali.

Su un preventivo con assiemi il prezzo accettato — quello che finisce
sull'ordine e in fattura — era piu' BASSO di quello mostrato al cliente.

Questo modulo mette la regola in un posto solo, sui dati salvati a database, e
diventa la verita' per salvataggio, PDF, invio e accettazione. Il browser puo'
continuare a fare la sua anteprima reattiva, ma non decide piu' il prezzo.

La regola (decisa con Marco il 2026-08-03, invariata)
----------------------------------------------------
    costo pezzo   = per ogni articolo SCIOLTO: (base + lavorazioni) x qta
    costo assiemi = per ogni assieme: (intrinseco + componenti + tubolari e
                    piastre collegati) x qta assieme
    costo tubolari/piastre sciolti = materiale + taglio  (gia' totali della
                                     riga: una riga STEP con qty N li porta per N)

    L'intrinseco dell'assieme comprende anche apporto (filo+gas) e pulizia dei
    suoi cordoni: metri saldatura assieme x le stesse tariffe EUR/m degli
    articoli (aggiunto 2026-09; prima esisteva solo nel vecchio calcolatore).

    prezzo = costo x (1 + generali%) x (1 + ricarico%)

    totale lotto = prezzo_pezzo x quantita_lotto
                 + prezzo_assiemi + prezzo_tubolari + prezzo_piastre

`margine` nel programma e' un RICARICO sul costo (costo x 1,20 per il 20%), non
un margine sul prezzo di vendita. Non e' stato cambiato: si e' solo scritto qui
com'e', perche' il nome trae in inganno.
"""
import logging

logger = logging.getLogger(__name__)

# Scostamento oltre il quale il totale del browser non e' un arrotondamento
# ma una formula diversa. Un centesimo per riga ci sta; un euro no.
TOLLERANZA_EUR = 0.50

CAMPI_LAVORAZIONE = ('costo_piega', 'costo_saldatura', 'costo_filettatura',
                     'costo_svasatura', 'costo_apporto', 'costo_pulizia')


def _num(v, default=0.0):
    try:
        if v is None or v == '':
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _qta(v, default=1):
    try:
        n = int(v)
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


def costo_base_articolo(a: dict) -> float:
    """Costo del materiale/taglio: l'override manuale batte la stima."""
    if a.get('costo_base_override') is not None:
        return _num(a.get('costo_base_override'))
    return _num(a.get('costo_base_stimato')) or _num(a.get('costo_materiale'))


def costo_lavorazioni_articolo(a: dict) -> float:
    # `costo_mat_apporto` e' il vecchio nome di `costo_apporto`: si accetta
    # ancora per non perdere i dati salvati prima dell'unificazione.
    tot = sum(_num(a.get(c)) for c in CAMPI_LAVORAZIONE)
    if not _num(a.get('costo_apporto')) and _num(a.get('costo_mat_apporto')):
        tot += _num(a.get('costo_mat_apporto'))
    return tot


def costo_articolo(a: dict) -> float:
    """Costo di UN pezzo, senza quantita'."""
    return costo_base_articolo(a) + costo_lavorazioni_articolo(a)


def costo_tubolare(t: dict) -> float:
    """Costo della riga tubolare. Una riga STEP puo' valere N pezzi uguali
    (`qty`), ma costo_materiale/costo_taglio_totale sono gia' il TOTALE della
    riga (qty inclusa, vedi import-step): qui NON si rimoltiplica. Cosi' il
    totale resta giusto anche dopo il salvataggio, che non conserva `qty`."""
    return _num(t.get('costo_materiale')) + _num(t.get('costo_taglio_totale'))


def costo_piastra(p: dict) -> float:
    """Costo della riga piastra: gia' totale della riga (qty inclusa)."""
    return _num(p.get('costo'))


# Consumabili della saldatura (filo+gas) e pulizia dei cordoni, in EUR/metro.
# Sugli articoli li calcola l'editor e li salva in costo_apporto/costo_pulizia;
# sui cordoni di ASSIEME non c'e' una colonna: si calcolano qui dai metri di
# saldatura dell'assieme con le STESSE tariffe degli articoli.
TARIFFE_SALDATURA_ASSIEME = ('costo_materiale_apporto_metro',
                             'costo_pulizia_saldatura_metro')


def tariffe_saldatura_assieme(preventivo: dict, config: dict) -> dict:
    """Tariffe apporto/pulizia da usare per gli assiemi.

    Un preventivo gia' INVIATO si calcola con la configurazione congelata
    all'invio (chi chiama passa solo `costo_generali_pct`): in quel caso le
    tariffe si leggono dalla fotografia; se la fotografia non le ha (inviato
    prima che esistessero) valgono zero, cioe' il prezzo com'era allora.
    """
    snap = preventivo.get('snapshot_economico') if isinstance(preventivo, dict) else None
    snap = snap if isinstance(snap, dict) else {}
    out = {}
    for k in TARIFFE_SALDATURA_ASSIEME:
        if k in (config or {}):
            out[k] = _num(config.get(k))
        else:
            out[k] = _num(snap.get(k))
    return out


def _costo_assieme(assieme: dict, articoli, tubolari, piastre, tariffe=None) -> dict:
    """Costo di un assieme: quello che ha in proprio piu' cio' che contiene."""
    codice = assieme.get('codice_assieme')
    figli_art = [a for a in articoli if a.get('codice_assieme') == codice]
    figli_tub = [t for t in tubolari if t.get('codice_assieme') == codice]
    figli_pia = [p for p in piastre if p.get('codice_assieme') == codice]

    componenti = sum(costo_articolo(a) * _qta(a.get('quantita')) for a in figli_art)
    accessori = (sum(costo_tubolare(t) for t in figli_tub)
                 + sum(costo_piastra(p) for p in figli_pia))
    tariffe = tariffe or {}
    eur_metro = sum(_num(tariffe.get(k)) for k in TARIFFE_SALDATURA_ASSIEME)
    apporto_pulizia = _num(assieme.get('saldatura_mt')) * eur_metro
    intrinseco = (_num(assieme.get('costo'))
                  + _num(assieme.get('costo_puntatura'))
                  + _num(assieme.get('costo_saldatura_assieme'))
                  + apporto_pulizia)
    singolo = intrinseco + componenti + accessori
    qty = _qta(assieme.get('qty'))
    return {
        'codice_assieme': codice,
        'apporto_pulizia': round(apporto_pulizia, 2),
        'intrinseco': round(intrinseco, 2),
        'componenti': round(componenti, 2),
        'accessori': round(accessori, 2),
        'costo_singolo': round(singolo, 2),
        'qty': qty,
        'costo_totale': round(singolo * qty, 2),
        'n_articoli': len(figli_art),
        'n_tubolari': len(figli_tub),
        'n_piastre': len(figli_pia),
    }


def calcola(preventivo: dict, config: dict = None) -> dict:
    """Prezzo di un preventivo a partire dai dati SALVATI.

    `preventivo` e' il dizionario di PreventivoManager.get(): contiene
    quantita, margine_pct, sconto_pct e le liste articoli/assiemi/tubolari/piastre.

    Ritorna sia i totali sia la loro composizione, cosi' l'interfaccia puo'
    mostrare da dove viene il numero invece di chiedere fiducia.
    """
    config = config or {}
    articoli = preventivo.get('articoli') or []
    assiemi = preventivo.get('assiemi') or []
    tubolari = preventivo.get('tubolari') or []
    piastre = preventivo.get('piastre') or []

    quantita = _qta(preventivo.get('quantita'))
    ricarico_pct = _num(preventivo.get('margine_pct'))
    sconto_pct = _num(preventivo.get('sconto_pct'))
    generali_pct = _num(config.get('costo_generali_pct'))

    # --- Costi ------------------------------------------------------------
    # Gli articoli collegati a un assieme NON si contano qui: sono gia' dentro
    # il costo dell'assieme. Contarli due volte era il difetto dell'accettazione.
    art_sciolti = [a for a in articoli if not a.get('codice_assieme')]
    costo_pezzo = sum(costo_articolo(a) * _qta(a.get('quantita')) for a in art_sciolti)

    tariffe = tariffe_saldatura_assieme(preventivo, config)
    dettaglio_assiemi = [_costo_assieme(x, articoli, tubolari, piastre, tariffe)
                         for x in assiemi]
    costo_assiemi = sum(x['costo_totale'] for x in dettaglio_assiemi)

    costo_tubolari = sum(costo_tubolare(t) for t in tubolari if not t.get('codice_assieme'))
    costo_piastre = sum(costo_piastra(p) for p in piastre if not p.get('codice_assieme'))

    # --- Da costo a prezzo -------------------------------------------------
    f_generali = 1 + generali_pct / 100.0
    f_ricarico = 1 + ricarico_pct / 100.0
    f = f_generali * f_ricarico

    prezzo_pezzo = costo_pezzo * f
    prezzo_assiemi = costo_assiemi * f
    prezzo_tubolari = costo_tubolari * f
    prezzo_piastre = costo_piastre * f

    totale_lotto = (prezzo_pezzo * quantita
                    + prezzo_assiemi + prezzo_tubolari + prezzo_piastre)

    scontato = totale_lotto * (1 - sconto_pct / 100.0) if sconto_pct else totale_lotto

    return {
        'quantita': quantita,
        'ricarico_pct': ricarico_pct,
        'costi_generali_pct': generali_pct,
        'sconto_pct': sconto_pct,
        # costi (senza generali ne' ricarico)
        'costo_pezzo': round(costo_pezzo, 2),
        'costo_assiemi': round(costo_assiemi, 2),
        'costo_tubolari': round(costo_tubolari, 2),
        'costo_piastre': round(costo_piastre, 2),
        'costo_totale': round(costo_pezzo * quantita + costo_assiemi
                              + costo_tubolari + costo_piastre, 2),
        # prezzi
        'totale_pezzo': round(costo_pezzo, 2),
        'totale_pezzo_con_margine': round(prezzo_pezzo, 2),
        'totale_lotto': round(scontato, 2),
        'totale_lotto_lordo': round(totale_lotto, 2),
        'prezzo_assiemi': round(prezzo_assiemi, 2),
        'prezzo_tubolari': round(prezzo_tubolari, 2),
        'prezzo_piastre': round(prezzo_piastre, 2),
        # composizione, per poter risalire al numero
        'dettaglio_assiemi': dettaglio_assiemi,
        'tariffe_saldatura_assiemi': tariffe,
        'n_articoli_sciolti': len(art_sciolti),
        'n_articoli_in_assieme': len(articoli) - len(art_sciolti),
    }


def verifica(preventivo: dict, totali_client: dict, config: dict = None) -> dict:
    """Confronta i totali arrivati dal browser con quelli calcolati qui.

    Non serve a dare torto al frontend, ma a non lasciar passare in silenzio
    una divergenza sul prezzo. Ritorna sempre i totali AUTOREVOLI: chi chiama
    deve usare quelli.
    """
    autorevoli = calcola(preventivo, config)
    esito = {'totali': autorevoli, 'coerente': True, 'scostamenti': {}}
    if not totali_client:
        return esito
    for campo in ('totale_pezzo', 'totale_pezzo_con_margine', 'totale_lotto'):
        if campo not in totali_client:
            continue
        atteso = autorevoli.get(campo, 0.0)
        ricevuto = _num(totali_client.get(campo))
        if abs(atteso - ricevuto) > TOLLERANZA_EUR:
            esito['coerente'] = False
            esito['scostamenti'][campo] = {
                'ricevuto': round(ricevuto, 2), 'calcolato': round(atteso, 2),
                'differenza': round(ricevuto - atteso, 2),
            }
    return esito
