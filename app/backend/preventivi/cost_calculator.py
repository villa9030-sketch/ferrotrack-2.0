"""Cost calculation engine for preventivo (quote) generation.

Extracted from preventivatore 2.0.py — pure business logic, no UI references.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def _fmt_ore(ore_decimali):
    """Formatta ore decimali in h:mm. Es: 1.5 -> '1:30', 0.75 -> '0:45'."""
    if ore_decimali <= 0:
        return "0:00"
    h = int(ore_decimali)
    m = int(round((ore_decimali - h) * 60))
    return f"{h}:{m:02d}"


def calcola_preventivo(
    articoli: list,
    config: dict,
    quantita: int,
    margine: float,
    costi_montaggio: dict,
    tubolari_per_assieme: dict,
    piastre_per_assieme: dict,
    *,
    base: float = 0.0,
    n_pieghe: int = 0,
    saldatura: float = 0.0,
    filettatura: int = 0,
    svasatura: int = 0,
    saldatura_min: float = 0.0,
) -> dict:
    """Calcola il preventivo completo.

    Esegue il calcolo dei costi di lavorazione (piegatura, saldatura, filettatura,
    svasatura), montaggio assiemi, tubolari, piastre, margine e sconto quantità.
    Genera il testo del report e i dati per il grafico.

    Args:
        articoli: lista di articoli processati (con campi costo, costo_piega, ecc.).
                  Può essere una lista vuota per il calcolo singolo pezzo.
        config: dizionario di configurazione con costi unitari.
        quantita: numero di pezzi nel lotto.
        margine: percentuale di margine (es. 15.0 per 15%).
        costi_montaggio: dict {codice_assieme: dati_montaggio}.
        tubolari_per_assieme: dict {codice_assieme: dati_tubolari}.
        piastre_per_assieme: dict {codice_assieme: dati_piastre}.
        base: costo base taglio/materiale (usato nel calcolo singolo pezzo).
        n_pieghe: numero di pieghe (usato nel calcolo singolo pezzo).
        saldatura: metri lineari saldatura (usato nel calcolo singolo pezzo).
        filettatura: numero filettature (usato nel calcolo singolo pezzo).
        svasatura: numero svasature (usato nel calcolo singolo pezzo).

    Returns:
        dict con chiavi:
            totale_pezzo, totale_pezzo_con_margine, totale_pezzo_scontato,
            totale_lotto, costo_piegatura, costo_saldatura, costo_filettatura,
            costo_svasatura, costo_mat_apporto, costo_pulizia,
            costo_montaggio_totale, costo_montaggio_con_margine,
            costo_tubolari_totale, costo_tubolari_con_margine,
            costo_piastre_totale, costo_piastre_con_margine,
            costo_movimentazione, sconto_pct,
            report_text (str), chart_data (dict).
    """
    # Se ci sono articoli processati, usa i costi già calcolati per-articolo
    if articoli:
        costo_piegatura = sum(a.get('costo_piega', 0) for a in articoli)
        costo_saldatura = sum(a.get('costo_saldatura', 0) for a in articoli)
        costo_filettatura = sum(a.get('costo_filettatura', 0) for a in articoli)
        costo_svasatura = sum(a.get('costo_svasatura', 0) for a in articoli)
        # `costo_apporto` e' il nome canonico (colonna del database). Il
        # ripiego su `costo_mat_apporto` serve ai payload vecchi ancora in giro.
        costo_mat_apporto = sum(
            a.get('costo_apporto', a.get('costo_mat_apporto', 0)) or 0
            for a in articoli)
        costo_pulizia = sum(a.get('costo_pulizia', 0) for a in articoli)
    else:
        # Fallback: calcolo singolo pezzo (senza articoli importati)
        soglia = int(config.get("soglia_setup_pieghe", 5))
        costo_piegatura = n_pieghe * config["costo_singola_piega"]
        if n_pieghe > soglia:
            costo_piegatura += config["costo_setup_piega"]
        # Saldatura a tempo (min/60 × tariffa) se abilitato, altrimenti €/metro
        if config.get("saldatura_a_tempo"):
            costo_saldatura = (saldatura_min / 60.0) * float(config.get("tariffa_oraria", 45))
        else:
            costo_saldatura = saldatura * config["costo_saldatura_metro"]
        costo_filettatura = filettatura * config["costo_filettatura"]
        costo_svasatura = svasatura * config["costo_svasatura"]
        costo_mat_apporto = saldatura * float(config.get("costo_materiale_apporto_metro", 0))
        costo_pulizia = saldatura * float(config.get("costo_pulizia_saldatura_metro", 0))

    totale_pezzo = (base + costo_piegatura + costo_saldatura + costo_filettatura
                    + costo_svasatura + costo_mat_apporto + costo_pulizia)

    # Costi generali (overhead) applicati SUL COSTO, poi ricarico. (Marco 2026-08-03)
    generali_pct = float(config.get("costo_generali_pct", 0))
    gen_f = 1 + generali_pct / 100

    if margine > 0:
        totale_pezzo_con_margine = totale_pezzo * gen_f * (1 + margine / 100)
    else:
        totale_pezzo_con_margine = totale_pezzo * gen_f

    # -- Economia di scala: sconto quantità --
    sconto_qty_config = config.get("sconto_quantita", {})
    sconto_pct = 0
    for soglia_str in sorted(sconto_qty_config.keys(), key=lambda x: int(x)):
        if quantita >= int(soglia_str):
            sconto_pct = sconto_qty_config[soglia_str]
    if sconto_pct > 0:
        totale_pezzo_scontato = totale_pezzo_con_margine * (1 - sconto_pct / 100)
    else:
        totale_pezzo_scontato = totale_pezzo_con_margine

    # -- Costo montaggio assiemi (montaggio + puntatura + saldatura + pulizia assieme) --
    costo_pulizia_sald_metro = float(config.get("costo_pulizia_saldatura_metro", 0))
    costo_mat_apporto_metro = float(config.get("costo_materiale_apporto_metro", 0))
    costo_montaggio_totale = 0.0
    for m in (costi_montaggio or {}).values():
        qty = m.get('qty', 1)
        sald_mt_ass = m.get('saldatura_mt', 0)
        costo_ass = (m['costo']
                     + m.get('costo_puntatura', 0)
                     + m.get('costo_saldatura_assieme', 0)
                     + sald_mt_ass * costo_pulizia_sald_metro    # pulizia cordoni assieme
                     + sald_mt_ass * costo_mat_apporto_metro)    # materiale apporto assieme
        costo_montaggio_totale += costo_ass * qty

    # -- Movimentazione (per assiemi pesanti) --
    costo_movimentazione = 0.0
    soglia_mov_kg = float(config.get("soglia_movimentazione_kg", 25.0))
    costo_mov_kg = float(config.get("costo_movimentazione_kg", 0))
    if costo_mov_kg > 0:
        for codice_ass, m in (costi_montaggio or {}).items():
            peso_kg = m.get('peso_kg', 0)
            if peso_kg > soglia_mov_kg:
                costo_movimentazione += (peso_kg * costo_mov_kg) * m.get('qty', 1)

    # -- Generali + margine su montaggio --
    if config.get("margine_su_montaggio", True) and margine > 0:
        costo_montaggio_con_margine = (costo_montaggio_totale + costo_movimentazione) * gen_f * (1 + margine / 100)
    else:
        costo_montaggio_con_margine = (costo_montaggio_totale + costo_movimentazione) * gen_f

    # -- Costo tubolari --
    costo_tubolari_totale = 0.0
    for codice_ass, tub_data in (tubolari_per_assieme or {}).items():
        costi_tub = tub_data.get('costi', {})
        costo_tubolari_totale += costi_tub.get('totale', 0)
    if config.get("margine_su_montaggio", True) and margine > 0:
        costo_tubolari_con_margine = costo_tubolari_totale * gen_f * (1 + margine / 100)
    else:
        costo_tubolari_con_margine = costo_tubolari_totale * gen_f

    # -- Costo piastre --
    costo_piastre_totale = 0.0
    for codice_ass, pia_data in (piastre_per_assieme or {}).items():
        costi_pia = pia_data.get('costi', {})
        costo_piastre_totale += costi_pia.get('totale', 0)
    if config.get("margine_su_montaggio", True) and margine > 0:
        costo_piastre_con_margine = costo_piastre_totale * gen_f * (1 + margine / 100)
    else:
        costo_piastre_con_margine = costo_piastre_totale * gen_f

    totale_lotto = (totale_pezzo_scontato * quantita
                    + costo_montaggio_con_margine
                    + costo_tubolari_con_margine
                    + costo_piastre_con_margine)

    # ====================================================================
    # Report - Stile Industrial Dashboard (larghezza ottimizzata ~60 caratteri)
    # ====================================================================
    W = 58  # Larghezza utile
    report = []

    # Header
    report.append("")
    report.append("  " + "\u2554" + "\u2550" * W + "\u2557")
    report.append("  \u2551" + " \u2699 PREVENTIVO CARPENTERIA".ljust(W) + "\u2551")
    report.append("  \u2551" + f"   {datetime.now().strftime('%d/%m/%Y  %H:%M')}".ljust(W) + "\u2551")
    report.append("  " + "\u255A" + "\u2550" * W + "\u255D")
    report.append("")

    # Se ci sono articoli importati, mostra il dettaglio in formato tabella
    if articoli:
        W_TABLE = 95  # Larghezza tabella articoli
        report.append("  \u250C" + "\u2500" * W_TABLE + "\u2510")
        report.append("  \u2502" + f" DETTAGLIO ARTICOLI ({len(articoli)}) - Margine: {margine:.1f}%".ljust(W_TABLE) + "\u2502")
        report.append("  \u251C" + "\u2500" * 16 + "\u252C" + "\u2500" * 8 + "\u252C" + "\u2500" * 8 + "\u252C" + "\u2500" * 8 + "\u252C" + "\u2500" * 8 + "\u252C" + "\u2500" * 8 + "\u252C" + "\u2500" * 9 + "\u252C" + "\u2500" * 11 + "\u2524")
        report.append("  \u2502" + " ARTICOLO".ljust(16) + "\u2502" + " MATER.".center(8) + "\u2502" + " PIEGA".center(8) + "\u2502" + " SALD.".center(8) + "\u2502" + " FILET".center(8) + "\u2502" + " SVAS.".center(8) + "\u2502" + " TOT/PZ".center(9) + "\u2502" + "PREZZO/PZ".center(11) + "\u2502")
        report.append("  \u251C" + "\u2500" * 16 + "\u253C" + "\u2500" * 8 + "\u253C" + "\u2500" * 8 + "\u253C" + "\u2500" * 8 + "\u253C" + "\u2500" * 8 + "\u253C" + "\u2500" * 8 + "\u253C" + "\u2500" * 9 + "\u253C" + "\u2500" * 11 + "\u2524")

        totale_mat = 0
        totale_piega = 0
        totale_sald = 0
        totale_filett = 0
        totale_svas = 0
        totale_costo_pz = 0
        totale_prezzo_pz = 0

        for art in articoli:
            codice_trunc = art['codice'][:14] if len(art['codice']) > 14 else art['codice']
            costo_p = art.get('costo_piega', 0)
            costo_s = art.get('costo_saldatura', 0)
            costo_f = art.get('costo_filettatura', 0)
            costo_v = art.get('costo_svasatura', 0)
            costo_art = (art['costo'] + costo_p + costo_s + costo_f + costo_v
                         + (art.get('costo_apporto', art.get('costo_mat_apporto', 0)) or 0)
                         + art.get('costo_pulizia', 0))

            # Calcola prezzo unitario con margine
            if margine > 0:
                prezzo_art = costo_art * (1 + margine / 100)
            else:
                prezzo_art = costo_art

            totale_mat += art['costo']
            totale_piega += costo_p
            totale_sald += costo_s
            totale_filett += costo_f
            totale_svas += costo_v
            totale_costo_pz += costo_art
            totale_prezzo_pz += prezzo_art

            report.append(
                "  \u2502" +
                f" {codice_trunc}".ljust(16) + "\u2502" +
                f"{art['costo']:>6.2f} ".rjust(8) + "\u2502" +
                f"{costo_p:>6.2f} ".rjust(8) + "\u2502" +
                f"{costo_s:>6.2f} ".rjust(8) + "\u2502" +
                f"{costo_f:>6.2f} ".rjust(8) + "\u2502" +
                f"{costo_v:>6.2f} ".rjust(8) + "\u2502" +
                f"{costo_art:>7.2f} ".rjust(9) + "\u2502" +
                f"{prezzo_art:>9.2f} ".rjust(11) + "\u2502"
            )

        report.append("  \u251C" + "\u2500" * 16 + "\u253C" + "\u2500" * 8 + "\u253C" + "\u2500" * 8 + "\u253C" + "\u2500" * 8 + "\u253C" + "\u2500" * 8 + "\u253C" + "\u2500" * 8 + "\u253C" + "\u2500" * 9 + "\u253C" + "\u2500" * 11 + "\u2524")
        report.append(
            "  \u2502" +
            " TOTALE".ljust(16) + "\u2502" +
            f"{totale_mat:>6.2f} ".rjust(8) + "\u2502" +
            f"{totale_piega:>6.2f} ".rjust(8) + "\u2502" +
            f"{totale_sald:>6.2f} ".rjust(8) + "\u2502" +
            f"{totale_filett:>6.2f} ".rjust(8) + "\u2502" +
            f"{totale_svas:>6.2f} ".rjust(8) + "\u2502" +
            f"{totale_costo_pz:>7.2f} ".rjust(9) + "\u2502" +
            f"{totale_prezzo_pz:>9.2f} ".rjust(11) + "\u2502"
        )
        report.append("  \u2514" + "\u2500" * 16 + "\u2534" + "\u2500" * 8 + "\u2534" + "\u2500" * 8 + "\u2534" + "\u2500" * 8 + "\u2534" + "\u2500" * 8 + "\u2534" + "\u2500" * 8 + "\u2534" + "\u2500" * 9 + "\u2534" + "\u2500" * 11 + "\u2518")
        report.append("")

        # Riepilogo assiemi (se presenti)
        assiemi_dict = {}
        for art in articoli:
            codice_assieme = art.get('codice_assieme')
            if codice_assieme:
                if codice_assieme not in assiemi_dict:
                    assiemi_dict[codice_assieme] = []
                assiemi_dict[codice_assieme].append(art['codice'])

        if assiemi_dict:
            W_ASS = 60
            report.append("  \u250C" + "\u2500" * W_ASS + "\u2510")
            report.append("  \u2502" + f" ASSIEMI RILEVATI ({len(assiemi_dict)})".ljust(W_ASS) + "\u2502")
            report.append("  \u251C" + "\u2500" * W_ASS + "\u2524")

            montaggio_info = costi_montaggio or {}
            for assieme, componenti in assiemi_dict.items():
                assieme_trunc = assieme[:28] if len(assieme) > 28 else assieme
                mont = montaggio_info.get(assieme, {})
                if mont and mont['ore'] > 0:
                    mont_str = f" - Mont: {_fmt_ore(mont['ore'])}={mont['costo']:.2f}\u20ac"
                else:
                    mont_str = ""
                report.append("  \u2502" + f" \u25BA {assieme_trunc} ({len(componenti)} comp.){mont_str}".ljust(W_ASS) + "\u2502")
                comp_qty_map = mont.get("componenti_qty", {}) if mont else {}
                comp_unici = list(dict.fromkeys(componenti))
                for comp in comp_unici:
                    comp_trunc = comp[:22] if len(comp) > 22 else comp
                    cq = comp_qty_map.get(comp, 1)
                    report.append("  \u2502" + f"    \u2022 {comp_trunc} x{cq}".ljust(W_ASS) + "\u2502")
            report.append("  \u2514" + "\u2500" * W_ASS + "\u2518")
            report.append("")

    # Riepilogo costi lavorazione
    report.append("  \u250C" + "\u2500" * W + "\u2510")
    report.append("  \u2502" + " RIEPILOGO COSTI LAVORAZIONE".ljust(W) + "\u2502")
    report.append("  \u251C" + "\u2500" * 38 + "\u252C" + "\u2500" * 19 + "\u2524")

    report.append("  \u2502" + f" Taglio / Materiale".ljust(38) + "\u2502" + f"EUR {base:>12.2f} ".rjust(19) + "\u2502")
    report.append("  \u2502" + f" Piegatura ({n_pieghe} pz)".ljust(38) + "\u2502" + f"EUR {costo_piegatura:>12.2f} ".rjust(19) + "\u2502")
    report.append("  \u2502" + f" Saldatura ({saldatura:.1f} ml)".ljust(38) + "\u2502" + f"EUR {costo_saldatura:>12.2f} ".rjust(19) + "\u2502")
    if costo_mat_apporto > 0:
        report.append("  \u2502" + f"   mat. apporto (filo+gas)".ljust(38) + "\u2502" + f"EUR {costo_mat_apporto:>12.2f} ".rjust(19) + "\u2502")
    if costo_pulizia > 0:
        report.append("  \u2502" + f"   pulizia/smerigliatura".ljust(38) + "\u2502" + f"EUR {costo_pulizia:>12.2f} ".rjust(19) + "\u2502")
    report.append("  \u2502" + f" Filettatura ({filettatura} pz)".ljust(38) + "\u2502" + f"EUR {costo_filettatura:>12.2f} ".rjust(19) + "\u2502")
    report.append("  \u2502" + f" Svasatura ({svasatura} pz)".ljust(38) + "\u2502" + f"EUR {costo_svasatura:>12.2f} ".rjust(19) + "\u2502")

    report.append("  \u251C" + "\u2500" * 38 + "\u253C" + "\u2500" * 19 + "\u2524")
    report.append("  \u2502" + " COSTO PEZZO (senza margine)".ljust(38) + "\u2502" + f"EUR {totale_pezzo:>12.2f} ".rjust(19) + "\u2502")

    if margine > 0:
        margine_valore = totale_pezzo_con_margine - totale_pezzo
        report.append("  \u2502" + f" + Margine ({margine:.1f}%)".ljust(38) + "\u2502" + f"EUR {margine_valore:>12.2f} ".rjust(19) + "\u2502")
        report.append("  \u251C" + "\u2500" * 38 + "\u253C" + "\u2500" * 19 + "\u2524")
        report.append("  \u2502" + f" PREZZO UNITARIO".ljust(38) + "\u2502" + f"EUR {totale_pezzo_con_margine:>12.2f} ".rjust(19) + "\u2502")

    if sconto_pct > 0:
        report.append("  \u2502" + f" Sconto quantit\u00E0 (-{sconto_pct}% per {quantita}+ pz)".ljust(38) + "\u2502" + f"EUR {totale_pezzo_scontato:>12.2f} ".rjust(19) + "\u2502")

    report.append("  \u2514" + "\u2500" * 38 + "\u2534" + "\u2500" * 19 + "\u2518")
    report.append("")

    # Montaggio assiemi (se presente)
    if costo_montaggio_totale > 0 or costo_movimentazione > 0:
        report.append("  \u250C" + "\u2500" * W + "\u2510")
        report.append("  \u2502" + " COSTO LAVORAZIONE ASSIEMI".ljust(W) + "\u2502")
        report.append("  \u251C" + "\u2500" * 38 + "\u252C" + "\u2500" * 19 + "\u2524")
        for codice_ass, mont_data in (costi_montaggio or {}).items():
            ass_trunc = codice_ass[:20] if len(codice_ass) > 20 else codice_ass
            qty = mont_data.get('qty', 1)
            # Riga montaggio
            costo_mont = mont_data['costo'] * qty
            report.append("  \u2502" + f" {ass_trunc} montaggio ({_fmt_ore(mont_data['ore'])})".ljust(38) + "\u2502" + f"EUR {costo_mont:>12.2f} ".rjust(19) + "\u2502")
            # Riga puntatura (se presente)
            costo_punt = mont_data.get('costo_puntatura', 0) * qty
            if costo_punt > 0:
                report.append("  \u2502" + f"   puntatura ({_fmt_ore(mont_data.get('ore_puntatura', 0))})".ljust(38) + "\u2502" + f"EUR {costo_punt:>12.2f} ".rjust(19) + "\u2502")
            # Riga saldatura assieme (se presente)
            costo_sald_ass = mont_data.get('costo_saldatura_assieme', 0) * qty
            sald_mt = mont_data.get('saldatura_mt', 0)
            if costo_sald_ass > 0:
                report.append("  \u2502" + f"   saldatura ({sald_mt:.2f} mt)".ljust(38) + "\u2502" + f"EUR {costo_sald_ass:>12.2f} ".rjust(19) + "\u2502")
            # Pulizia + materiale apporto assieme
            costo_pulizia_ass = sald_mt * costo_pulizia_sald_metro * qty
            costo_apporto_ass = sald_mt * costo_mat_apporto_metro * qty
            if costo_pulizia_ass > 0:
                report.append("  \u2502" + f"   pulizia cordoni".ljust(38) + "\u2502" + f"EUR {costo_pulizia_ass:>12.2f} ".rjust(19) + "\u2502")
            if costo_apporto_ass > 0:
                report.append("  \u2502" + f"   mat. apporto".ljust(38) + "\u2502" + f"EUR {costo_apporto_ass:>12.2f} ".rjust(19) + "\u2502")
            # Movimentazione (per assieme pesante)
            peso_kg = mont_data.get('peso_kg', 0)
            if peso_kg > soglia_mov_kg and costo_mov_kg > 0:
                costo_mov_ass = peso_kg * costo_mov_kg * qty
                report.append("  \u2502" + f"   movimentazione ({peso_kg:.0f} kg)".ljust(38) + "\u2502" + f"EUR {costo_mov_ass:>12.2f} ".rjust(19) + "\u2502")
        report.append("  \u251C" + "\u2500" * 38 + "\u253C" + "\u2500" * 19 + "\u2524")
        totale_ass_completo = costo_montaggio_totale + costo_movimentazione
        report.append("  \u2502" + " TOTALE LAVORAZIONE ASSIEMI".ljust(38) + "\u2502" + f"EUR {totale_ass_completo:>12.2f} ".rjust(19) + "\u2502")
        if config.get("margine_su_montaggio", True) and margine > 0:
            report.append("  \u2502" + f" + Margine ({margine:.1f}%)".ljust(38) + "\u2502" + f"EUR {costo_montaggio_con_margine:>12.2f} ".rjust(19) + "\u2502")
        report.append("  \u2514" + "\u2500" * 38 + "\u2534" + "\u2500" * 19 + "\u2518")
        report.append("")

    # Sezione tubolari (se rilevati)
    if tubolari_per_assieme:
        report.append("  \u250C" + "\u2500" * W + "\u2510")
        report.append("  \u2502" + " COSTO TUBOLARI".ljust(W) + "\u2502")
        report.append("  \u251C" + "\u2500" * 38 + "\u252C" + "\u2500" * 19 + "\u2524")
        for codice_ass, tub_data in tubolari_per_assieme.items():
            analisi = tub_data.get('analisi', {})
            costi_tub = tub_data.get('costi', {})
            for tubo in analisi.get('tubi', []):
                profilo_str = tubo.get('profilo', '?')[:28]
                lung_m = tubo.get('lunghezza_m', 0)
                peso = tubo.get('peso_kg', 0)
                report.append("  \u2502" + f" {profilo_str} L={lung_m:.3f}m".ljust(38) + "\u2502" + f"{peso:.1f} kg".rjust(19) + "\u2502")
            report.append("  \u251C" + "\u2500" * 38 + "\u253C" + "\u2500" * 19 + "\u2524")
            report.append("  \u2502" + f"   Materiale ({costi_tub.get('materiale', 'acciaio')})".ljust(38) + "\u2502" + f"EUR {costi_tub.get('costo_materiale', 0):>12.2f} ".rjust(19) + "\u2502")
            if costi_tub.get('costo_taglio_totale', 0) > 0:
                report.append("  \u2502" + f"   Taglio ({costi_tub.get('n_tagli_dritti', 0)} dr. {costi_tub.get('n_tagli_obliqui', 0)} obl.)".ljust(38) + "\u2502" + f"EUR {costi_tub['costo_taglio_totale']:>12.2f} ".rjust(19) + "\u2502")
        report.append("  \u251C" + "\u2500" * 38 + "\u253C" + "\u2500" * 19 + "\u2524")
        report.append("  \u2502" + " TOTALE TUBOLARI".ljust(38) + "\u2502" + f"EUR {costo_tubolari_totale:>12.2f} ".rjust(19) + "\u2502")
        if costo_tubolari_con_margine != costo_tubolari_totale:
            report.append("  \u2502" + f" + Margine ({margine:.1f}%)".ljust(38) + "\u2502" + f"EUR {costo_tubolari_con_margine:>12.2f} ".rjust(19) + "\u2502")
        report.append("  \u2514" + "\u2500" * 38 + "\u2534" + "\u2500" * 19 + "\u2518")
        report.append("")

    # Sezione piastre (se rilevate)
    if piastre_per_assieme:
        report.append("  \u250C" + "\u2500" * W + "\u2510")
        report.append("  \u2502" + " COSTO PIASTRE".ljust(W) + "\u2502")
        report.append("  \u251C" + "\u2500" * 38 + "\u252C" + "\u2500" * 19 + "\u2524")
        for codice_ass, pia_data in piastre_per_assieme.items():
            analisi = pia_data.get('analisi', {})
            costi_pia = pia_data.get('costi', {})
            materiale_pia = costi_pia.get('materiale', 'acciaio')
            dettaglio = costi_pia.get('dettaglio_piastre', [])
            for i, piastra in enumerate(analisi.get('piastre', [])):
                sp = piastra.get('spessore_mm', 0)
                area = piastra.get('area_dm2', 0)
                peso = piastra.get('peso_kg', 0)
                costo_p = dettaglio[i].get('costo', 0) if i < len(dettaglio) else 0
                report.append("  \u2502" + f" Piastra {i+1}: sp.{sp:.1f}mm {area:.2f}dm\u00B2".ljust(38) + "\u2502" + f"EUR {costo_p:>12.2f} ".rjust(19) + "\u2502")
            report.append("  \u251C" + "\u2500" * 38 + "\u253C" + "\u2500" * 19 + "\u2524")
            report.append("  \u2502" + f" Materiale: {materiale_pia}".ljust(38) + "\u2502" + f"peso {analisi.get('peso_totale_kg', 0):.1f} kg".rjust(19) + "\u2502")
        report.append("  \u251C" + "\u2500" * 38 + "\u253C" + "\u2500" * 19 + "\u2524")
        report.append("  \u2502" + " TOTALE PIASTRE".ljust(38) + "\u2502" + f"EUR {costo_piastre_totale:>12.2f} ".rjust(19) + "\u2502")
        if costo_piastre_con_margine != costo_piastre_totale:
            report.append("  \u2502" + f" + Margine ({margine:.1f}%)".ljust(38) + "\u2502" + f"EUR {costo_piastre_con_margine:>12.2f} ".rjust(19) + "\u2502")
        report.append("  \u2514" + "\u2500" * 38 + "\u2534" + "\u2500" * 19 + "\u2518")
        report.append("")

    # Box finale con totale ordine
    report.append("  \u2554" + "\u2550" * W + "\u2557")
    report.append("  \u2551" + f" QUANTITA: {quantita} pz".ljust(W) + "\u2551")
    report.append("  \u2560" + "\u2550" * W + "\u2563")
    if margine > 0:
        report.append("  \u2551" + f" {quantita} pz \u00D7 EUR {totale_pezzo_scontato:.2f}".ljust(W) + "\u2551")
    if sconto_pct > 0:
        report.append("  \u2551" + f"   (sconto quantit\u00E0 -{sconto_pct}% applicato)".ljust(W) + "\u2551")
    if costo_montaggio_con_margine > 0:
        report.append("  \u2551" + f" + Lavorazione assiemi".ljust(30) + f"EUR {costo_montaggio_con_margine:>14.2f}      ".rjust(W - 30) + "\u2551")
    if costo_tubolari_con_margine > 0:
        report.append("  \u2551" + f" + Tubolari".ljust(30) + f"EUR {costo_tubolari_con_margine:>14.2f}      ".rjust(W - 30) + "\u2551")
    if costo_piastre_con_margine > 0:
        report.append("  \u2551" + f" + Piastre".ljust(30) + f"EUR {costo_piastre_con_margine:>14.2f}      ".rjust(W - 30) + "\u2551")
    report.append("  \u2551" + f" \u25A0 TOTALE ORDINE".ljust(30) + f"EUR {totale_lotto:>14.2f}      ".rjust(W - 30) + "\u2551")
    report.append("  \u255A" + "\u2550" * W + "\u255D")

    report_text = "\n".join(report)

    # Chart data
    chart_data = {
        "Materiale": base,
        "Piegatura": costo_piegatura,
        "Saldatura": costo_saldatura,
        "Mat. apporto": costo_mat_apporto,
        "Pulizia": costo_pulizia,
        "Filettatura": costo_filettatura,
        "Svasatura": costo_svasatura,
        "Montaggio": costo_montaggio_totale + costo_movimentazione,
        "Tubolari": costo_tubolari_totale,
        "Piastre": costo_piastre_totale,
    }

    return {
        "totale_pezzo": totale_pezzo,
        "totale_pezzo_con_margine": totale_pezzo_con_margine,
        "totale_pezzo_scontato": totale_pezzo_scontato,
        "totale_lotto": totale_lotto,
        "costo_piegatura": costo_piegatura,
        "costo_saldatura": costo_saldatura,
        "costo_filettatura": costo_filettatura,
        "costo_svasatura": costo_svasatura,
        "costo_mat_apporto": costo_mat_apporto,
        "costo_pulizia": costo_pulizia,
        "costo_montaggio_totale": costo_montaggio_totale,
        "costo_montaggio_con_margine": costo_montaggio_con_margine,
        "costo_tubolari_totale": costo_tubolari_totale,
        "costo_tubolari_con_margine": costo_tubolari_con_margine,
        "costo_piastre_totale": costo_piastre_totale,
        "costo_piastre_con_margine": costo_piastre_con_margine,
        "costo_movimentazione": costo_movimentazione,
        "sconto_pct": sconto_pct,
        "report_text": report_text,
        "chart_data": chart_data,
    }
