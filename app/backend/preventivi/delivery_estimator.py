"""Delivery time estimation module for Preventivatore.

Estimates manufacturing time and delivery date based on
bending, welding, drilling, assembly and tube-cutting operations.
"""

import logging
import math
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def stima_tempi_consegna(articoli: list, costi_montaggio: dict,
                          tubolari_per_assieme: dict, piastre_per_assieme: dict,
                          config: dict, quantita: int = 1) -> dict:
    """Estimate delivery time based on manufacturing operations.

    Args:
        articoli: list of article dicts with pieghe, saldatura, filettatura, svasatura
        costi_montaggio: dict {codice_assieme: {ore, ore_puntatura, saldatura_mt, ...}}
        tubolari_per_assieme: dict with tube analysis data
        piastre_per_assieme: dict with plate analysis data
        config: app config dict
        quantita: number of pieces

    Returns:
        dict with:
            ore_totali: float - total manufacturing hours
            giorni_lavorativi: int - working days needed
            data_consegna_stimata: str - estimated delivery date (dd/mm/yyyy)
            breakdown: dict with hours per operation type:
                ore_piegatura, ore_saldatura, ore_foratura,
                ore_montaggio, ore_taglio_tubo, ore_setup
    """
    ore_lavoro = float(config.get('ore_lavoro_giorno', 8.0))
    tempo_piega = float(config.get('tempo_medio_piega_min', 0.5))  # min per bend
    velocita_sald = float(config.get('velocita_saldatura_mt_ora', 12.5))  # mt/h
    tempo_foro = float(config.get('tempo_medio_foro_min', 0.3))  # min per hole
    tempo_setup = float(config.get('tempo_saldatura_setup_min', 15.0))  # min setup per batch

    # Count operations from articles. Nomi dei campi come nel database
    # (saldatura_ml in METRI, filettatura_pz/svasatura_pz in pezzi), per pezzo:
    # si moltiplica per la quantita' dell'articolo.
    def _somma(campo):
        tot = 0.0
        for a in articoli or []:
            try:
                q = int(a.get('quantita') or 1)
            except (TypeError, ValueError):
                q = 1
            tot += float(a.get(campo) or 0) * max(q, 1)
        return tot

    tot_pieghe = _somma('pieghe')
    tot_saldatura_ml = _somma('saldatura_ml')
    tot_filettatura = _somma('filettatura_pz')
    tot_svasatura = _somma('svasatura_pz')

    # Bending time
    ore_piegatura = (tot_pieghe * tempo_piega * quantita) / 60.0

    # Welding time (from articles - component welding): saldatura_ml e' gia' in metri
    saldatura_mt = tot_saldatura_ml
    ore_saldatura_comp = (saldatura_mt * quantita / velocita_sald) if velocita_sald > 0 else 0

    # Assembly welding time (from costi_montaggio)
    ore_saldatura_ass = 0
    ore_montaggio = 0
    ore_puntatura = 0
    for mont in costi_montaggio.values():
        qty_ass = mont.get('qty', 1)
        ore_montaggio += mont.get('ore', 0) * qty_ass
        ore_puntatura += mont.get('ore_puntatura', 0) * qty_ass
        sald_mt_ass = mont.get('saldatura_mt', 0)
        if sald_mt_ass > 0 and velocita_sald > 0:
            ore_saldatura_ass += (sald_mt_ass / velocita_sald) * qty_ass

    ore_saldatura = ore_saldatura_comp + ore_saldatura_ass

    # Drilling/threading time
    ore_foratura = ((tot_filettatura + tot_svasatura) * tempo_foro * quantita) / 60.0

    # Tube cutting time
    ore_taglio_tubo = 0
    for tub_data in tubolari_per_assieme.values():
        costi = tub_data.get('costi', {})
        analisi = tub_data.get('analisi', {})
        n_dritti = analisi.get('n_tagli_dritti', 0)
        n_obliqui = analisi.get('n_tagli_obliqui', 0)
        n_sagomati = analisi.get('n_tagli_sagomati', 0)
        t_dritto = float(config.get('tempo_taglio_dritto_min', 1.0))
        t_obliquo = float(config.get('tempo_taglio_obliquo_min', 2.5))
        t_sagomato = float(config.get('tempo_taglio_sagomato_min', 5.0))
        ore_taglio_tubo += (n_dritti * t_dritto + n_obliqui * t_obliquo + n_sagomati * t_sagomato) / 60.0

    # Setup time (one setup per distinct operation type needed)
    n_setups = 0
    if tot_pieghe > 0: n_setups += 1
    if ore_saldatura > 0: n_setups += 1
    if tot_filettatura > 0 or tot_svasatura > 0: n_setups += 1
    if ore_taglio_tubo > 0: n_setups += 1
    ore_setup = (n_setups * tempo_setup) / 60.0

    ore_totali = (ore_piegatura + ore_saldatura + ore_foratura +
                  ore_montaggio + ore_puntatura + ore_taglio_tubo + ore_setup)

    # Working days (round up)
    giorni = max(1, math.ceil(ore_totali / ore_lavoro)) if ore_totali > 0 else 1

    # Estimated delivery date (skip weekends)
    data_consegna = _aggiungi_giorni_lavorativi(datetime.now(), giorni)

    logger.info("Stima consegna: %.1f ore totali, %d giorni lavorativi", ore_totali, giorni)

    return {
        'ore_totali': round(ore_totali, 2),
        'giorni_lavorativi': giorni,
        'data_consegna_stimata': data_consegna.strftime('%d/%m/%Y'),
        'breakdown': {
            'ore_piegatura': round(ore_piegatura, 2),
            'ore_saldatura': round(ore_saldatura, 2),
            'ore_foratura': round(ore_foratura, 2),
            'ore_montaggio': round(ore_montaggio + ore_puntatura, 2),
            'ore_taglio_tubo': round(ore_taglio_tubo, 2),
            'ore_setup': round(ore_setup, 2),
        }
    }


def _aggiungi_giorni_lavorativi(data_inizio: datetime, giorni: int) -> datetime:
    """Add working days (skip Saturday=5, Sunday=6)."""
    data = data_inizio
    aggiunti = 0
    while aggiunti < giorni:
        data += timedelta(days=1)
        if data.weekday() < 5:  # Mon-Fri
            aggiunti += 1
    return data
