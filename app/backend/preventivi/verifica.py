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

from .calcolo import calcola, costo_articolo
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

    # --- pezzi senza prezzo ----------------------------------------------
    # Un articolo dentro un assieme puo' avere costo proprio a zero (lo porta
    # l'assieme): si guardano solo quelli sciolti.
    for i, a in enumerate(articoli):
        if a.get('codice_assieme'):
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
