"""Verifica di un preventivo prima delle operazioni definitive (10.3 e 10.6).

Prima l'invio e l'accettazione partivano senza chiedersi se il documento fosse
in ordine: si scopriva dopo, dal cliente o in officina, che mancava il prezzo di
un pezzo o la data di consegna.

Qui si separano due cose che non vanno confuse:

  ERRORI  fermano l'invio e l'accettazione. Sono dati mancanti o impossibili:
          senza cliente non si manda niente, un pezzo senza prezzo non si
          quota, un totale a zero non e' un'offerta.
  AVVISI  meritano attenzione ma non fermano nulla: una data di consegna non
          ancora decisa, una geometria stimata e non confermata.

Ogni segnalazione porta il riferimento della riga, cosi' nell'interfaccia si
puo' andare direttamente al pezzo da sistemare invece di cercarlo a mano in una
distinta da cento.
"""
import logging

from .calcolo import calcola, costo_articolo, costo_base_articolo
from .lantek_lookup import lookup_ricetta, normalizza_materiale
from .validazione import (valida_articoli, valida_assiemi, valida_piastre,
                          valida_testata, valida_tubolari)

logger = logging.getLogger(__name__)


def _riferimento(riga, indice):
    return {
        'id': riga.get('id'),
        'codice': (riga.get('codice') or riga.get('codice_assieme')
                   or riga.get('profilo') or f'riga {indice + 1}'),
        'indice': indice,
    }


def _num(v):
    try:
        return float(v) if v not in (None, '') else 0.0
    except (TypeError, ValueError):
        return 0.0


def _controlli_cad(a: dict, config: dict):
    """Controlli su geometria, materiale e ricetta di UN articolo.

    Ritorna (errori, avvisi) come liste di (tipo, messaggio). Errore solo
    quando il prezzo e' di sicuro sbagliato (costo zero con geometria, manca
    lo spessore o la ricetta di un pezzo da tagliare); il resto e' avviso.
    Un prezzo manuale (costo_base_override) e' una scelta del commerciale:
    toglie gli errori sul costo base, restano gli avvisi.
    """
    errs, avv = [], []
    cod = a.get('codice') or 'senza codice'
    perim = _num(a.get('perimetro_taglio_m'))
    area = _num(a.get('area_dm2'))
    spess = _num(a.get('spessore_mm'))
    mat = (a.get('materiale') or '').strip()
    manuale = a.get('costo_base_override') is not None and a.get('costo_base_override') != ''
    base = costo_base_articolo(a)

    # Saldatura stimata a metro (vale anche senza geometria di taglio)
    if (config or {}).get('saldatura_a_tempo') and _num(a.get('saldatura_ml')) > 0 \
            and _num(a.get('saldatura_min')) <= 0:
        avv.append(('saldatura_stimata_a_metro',
                    f'Pezzo {cod}: saldatura stimata a metro '
                    f'({_num(a.get("saldatura_ml")):g} m) perche\' mancano i minuti. '
                    'Inserisci i minuti di saldatura per il costo a tempo.'))

    laser = perim > 0 or area > 0
    if not laser:
        return errs, avv

    if not manuale:
        if spess <= 0:
            errs.append(('spessore_mancante',
                         f'Pezzo {cod}: manca lo spessore. Senza, materiale e '
                         'taglio non si calcolano: inseriscilo e ristima.'))
        if perim <= 0 < area:
            errs.append(('perimetro_nullo',
                         f'Pezzo {cod}: area {area:g} dm² ma perimetro di taglio 0, '
                         'il taglio non e\' conteggiato. Conferma il contorno nel CAD '
                         'o inserisci il perimetro.'))
        if not mat:
            errs.append(('materiale_mancante',
                         f'Pezzo {cod}: manca il materiale. Sceglilo e ristima.'))

    famiglia = normalizza_materiale(mat) if mat else None
    if mat and not famiglia:
        (avv if manuale else errs).append((
            'materiale_sconosciuto',
            f'Pezzo {cod}: materiale "{mat}" sconosciuto, non ha prezzo al kg '
            'ne\' ricetta di taglio. Scegli un materiale dell\'elenco'
            + (' (ora vale il prezzo manuale).' if manuale else ' o inserisci un prezzo manuale.')))
    elif famiglia and spess > 0:
        ricetta = lookup_ricetta(famiglia, spess, (a.get('gas_taglio') or None))
        if not ricetta or a.get('ricetta_mancante'):
            (avv if manuale else errs).append((
                'ricetta_mancante',
                f'Pezzo {cod}: nessuna ricetta di taglio per {mat} {spess:g} mm, '
                'il costo non comprende il taglio laser. Inserisci un prezzo manuale'
                ' o correggi materiale/spessore.'))
        elif ricetta.get('source') in ('clamped_min', 'clamped_max'):
            avv.append(('spessore_fuori_tabella',
                        f'Pezzo {cod}: spessore {spess:g} mm fuori tabella Lantek '
                        f'per {famiglia}: velocita\' di taglio presa dallo spessore limite, '
                        'verifica il costo.'))

    if not manuale and base <= 0 and perim > 0 and not errs:
        errs.append(('costo_base_zero',
                     f'Pezzo {cod}: ha {perim * 1000:.0f} mm di taglio ma costo base 0. '
                     'Premi "Stima" o inserisci un prezzo manuale.'))
    return errs, avv


def verifica(preventivo: dict, config: dict = None) -> dict:
    """Stato di completezza del preventivo.

    Ritorna {pronto, errori[], avvisi[], totali}. `pronto` e' False se c'e'
    almeno un errore: in quel caso l'interfaccia non deve dire "tutto pronto".
    """
    config = config or {}
    errori, avvisi = [], []

    articoli = preventivo.get('articoli') or []
    assiemi = preventivo.get('assiemi') or []
    tubolari = preventivo.get('tubolari') or []
    piastre = preventivo.get('piastre') or []

    # --- dati impossibili -------------------------------------------------
    for msg in (valida_testata(preventivo) + valida_articoli(articoli)
                + valida_assiemi(assiemi) + valida_tubolari(tubolari)
                + valida_piastre(piastre)):
        errori.append({'tipo': 'dato_non_valido', 'messaggio': msg})

    # --- dati mancanti ----------------------------------------------------
    if not (preventivo.get('cliente') or '').strip():
        errori.append({'tipo': 'cliente_mancante',
                       'messaggio': 'Manca il cliente: senza non si puo\' inviare.'})

    if not (articoli or assiemi or tubolari or piastre):
        errori.append({'tipo': 'nessuna_riga',
                       'messaggio': 'Il preventivo non contiene nulla da quotare.'})

    # --- dati CAD che rendono il prezzo sbagliato -------------------------
    # Controlli sulla geometria e sul materiale di ogni pezzo, anche dentro un
    # assieme: un componente con perimetro ma costo zero e' un buco nel prezzo
    # dell'assieme, non un "costo portato dall'assieme".
    gia_segnalati = set()
    for i, a in enumerate(articoli):
        errs, avv = _controlli_cad(a, config)
        rif = _riferimento(a, i) if (errs or avv) else None
        for tipo, msg in errs:
            errori.append({'tipo': tipo, 'messaggio': msg, 'riferimento': rif})
            gia_segnalati.add(i)
        for tipo, msg in avv:
            avvisi.append({'tipo': tipo, 'messaggio': msg, 'riferimento': rif})

    # --- pezzi senza prezzo ----------------------------------------------
    # Un articolo dentro un assieme puo' avere costo proprio a zero (lo porta
    # l'assieme) se non ha geometria di taglio: quello si e' gia' visto sopra.
    for i, a in enumerate(articoli):
        if a.get('codice_assieme') or i in gia_segnalati:
            continue
        if costo_articolo(a) <= 0:
            rif = _riferimento(a, i)
            errori.append({
                'tipo': 'pezzo_senza_prezzo',
                'messaggio': f"Il pezzo {rif['codice']} non ha ancora un prezzo.",
                'riferimento': rif,
            })

    # --- totale -----------------------------------------------------------
    totali = calcola(preventivo, config)
    if totali['totale_lotto'] <= 0:
        errori.append({'tipo': 'totale_nullo',
                       'messaggio': 'Il totale e\' zero: controlla prezzi e quantita\'.'})

    # --- cose da guardare, senza fermare ----------------------------------
    if not preventivo.get('data_consegna_proposta'):
        avvisi.append({'tipo': 'data_consegna_mancante',
                       'messaggio': 'Non hai indicato una data di consegna proposta.'})

    for i, a in enumerate(articoli):
        if a.get('dxf_filename') and not a.get('geometria_manuale_confermata'):
            rif = _riferimento(a, i)
            avvisi.append({
                'tipo': 'geometria_non_confermata',
                'messaggio': f"Geometria di {rif['codice']} stimata dal disegno, "
                             'non confermata a mano.',
                'riferimento': rif,
            })
        if a.get('area_stimata_piega'):
            rif = _riferimento(a, i)
            avvisi.append({
                'tipo': 'area_stimata',
                'messaggio': f"Area di {rif['codice']} stimata dallo sviluppo piega.",
                'riferimento': rif,
            })

    if (preventivo.get('sconto_pct') or 0) > 0:
        avvisi.append({
            'tipo': 'sconto_applicato',
            'messaggio': f"E\' applicato uno sconto del {preventivo['sconto_pct']:g}%.",
        })

    return {
        'pronto': not errori,
        'errori': errori,
        'avvisi': avvisi,
        'totali': totali,
        'riepilogo': {
            'cliente': preventivo.get('cliente') or '',
            'quantita': totali['quantita'],
            'n_articoli': len(articoli),
            'n_assiemi': len(assiemi),
            'n_tubolari': len(tubolari),
            'n_piastre': len(piastre),
            'totale_lotto': totali['totale_lotto'],
        },
    }
