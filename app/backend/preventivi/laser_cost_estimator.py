"""Stimatore costo taglio laser — MODELLO FISICO (post-import Lantek).

Calibrato sui dati reali del cliente (2026-07-02):

- Costo macchina laser: 75 €/h
- Costo operaio: 25 €/h (sommato → 100 €/h totale)
- Costi materiali (€/kg): S235 0.80, ZINCATO 1.10, INOX_304 3.20, ALU 6.80,
  OTTONE 12.00 (default, cliente non l'ha specificato — poco usato)
- Velocità taglio + tempi pierce: `lantek_lookup.lookup_ricetta()` (116 ricette
  reali del cliente estratte da `parametri_taglio_completi_aggiornati.xlsx`)
- Regola gas: `lantek_lookup.default_gas()` (S235 ≤3mm→N2, >3mm→O2; altri→N2)

Formula:

    peso_kg          = area_dm2 × spessore_mm/100 × densità[materiale]
    tempo_taglio_s   = perimetro_m × 1000 / velocità_mm_min × 60
    tempo_pierce_s   = n_forature × pierce_time_s
    costo_lavoro     = (tempo_taglio_s + tempo_pierce_s)/3600 × (€/h_macchina + €/h_operaio)
    costo_materiale  = peso_kg × €/kg_materiale
    base             = costo_lavoro + costo_materiale + setup_pezzo

Tutti i coefficienti (€/h, €/kg per materiale, densità) sono editabili da admin
via `config['laser_config']` (persistenza in `app_config.json` sezione laser).
Il commerciale può sempre sovrascrivere il base con `costo_base_override` sul
singolo articolo se serve.
"""
from __future__ import annotations

import logging

from .lantek_lookup import lookup_ricetta, default_gas, normalizza_materiale

logger = logging.getLogger(__name__)


# === COEFFICIENTI DI DEFAULT (fonte cliente, 2026-07-02) ===
DEFAULT_LASER_CONFIG = {
    # Costi orari
    'euro_h_macchina': 75.0,    # laser fibra
    'euro_h_operaio': 25.0,     # operaio a bordo macchina
    # Setup fisso per pezzo (carico foglio, avviamento) — modesto per il modello
    # attuale, il grosso del setup è già dentro il tempo pierce
    'setup_eur_default': 0.10,
    # Resa del nesting (0-1]: quota di lamiera che diventa pezzo. Il materiale
    # si paga sul LORDO = netto / resa. 1.0 = nessuno sfrido (comportamento
    # storico); 0.8 = il 20% del foglio va in sfrido.
    'resa_nesting': 1.0,
    # €/kg materie prime + densità fisica
    'materiali': {
        'S235':     {'densita_kg_dm3': 7.85, 'euro_kg': 0.80, 'setup_eur': 0.10},
        'ZINCATO':  {'densita_kg_dm3': 7.85, 'euro_kg': 1.10, 'setup_eur': 0.10},
        'INOX_304': {'densita_kg_dm3': 8.00, 'euro_kg': 3.20, 'setup_eur': 0.10},
        'ALU':      {'densita_kg_dm3': 2.70, 'euro_kg': 6.80, 'setup_eur': 0.10},
        'OTTONE':   {'densita_kg_dm3': 8.50, 'euro_kg': 12.00, 'setup_eur': 0.10},
    },
}


# Alias map: il frontend dropdown offre varianti (INOX_316, ALU_5754, ALU_5083)
# che non sono nella tabella coefficienti default. Mappa a materiale "canonico"
# con densità/costo simili (INOX_316 ~ INOX_304 fisicamente identici per taglio
# laser, ALU_5754/ALU_5083 si comportano come ALU generico).
# BUG FIX #1: senza questo, articoli con materiale non-canonico avevano
# costo_laser=0 silente (preventivo regalato).
_MATERIAL_ALIASES = {
    'INOX_316': 'INOX_304',
    'INOX_316L': 'INOX_304',
    'ALU_5754': 'ALU',
    'ALU_5083': 'ALU',
    'ALU_6082': 'ALU',
    'ALLUMINIO': 'ALU',
    'ACCIAIO': 'S235',
    'FERRO': 'S235',
}


def _resolve_material(materiale: str, materiali_map: dict) -> tuple[str, dict | None]:
    """Cerca il materiale nella mappa dei coefficienti risolvendo gli alias.
    Ritorna (nome_canonico, dict_coeff) oppure (materiale, None) se non trovato."""
    if not materiale:
        return materiale, None
    mat_upper = materiale.strip().upper()
    # 1. Match esatto
    if mat_upper in materiali_map:
        return mat_upper, materiali_map[mat_upper]
    # 2. Alias diretto
    canonical = _MATERIAL_ALIASES.get(mat_upper)
    if canonical and canonical in materiali_map:
        return canonical, materiali_map[canonical]
    # 3. Prefix match (es. "ALU_5754_H111" → matcha "ALU_5754" → alias → "ALU")
    for prefix in _MATERIAL_ALIASES:
        if mat_upper.startswith(prefix):
            canonical = _MATERIAL_ALIASES[prefix]
            if canonical in materiali_map:
                return canonical, materiali_map[canonical]
    # 4. Famiglia di ricetta (S235JR → S235, INOX_316L → INOX_304, ALU_6082 → ALU…):
    #    la stessa tabella di sinonimi usata per le ricette di taglio.
    famiglia = normalizza_materiale(mat_upper)
    if famiglia and famiglia in materiali_map:
        return famiglia, materiali_map[famiglia]
    # 5. Fallback: cerca il primo canonical che è prefix del richiesto
    for canonical in materiali_map:
        if mat_upper.startswith(canonical):
            return canonical, materiali_map[canonical]
    return mat_upper, None


def _empty_result(warnings: list[str], **flag) -> dict:
    out = {
        'peso_kg': 0.0, 'costo_materiale': 0.0, 'costo_lavoro': 0.0,
        'tempo_taglio_s': 0.0, 'tempo_pierce_s': 0.0, 'tempo_totale_min': 0.0,
        'setup_eur': 0.0, 'base': 0.0, 'warnings': warnings, 'notes': [],
        'ricetta_mancante': False, 'materiale_sconosciuto': False,
        'spessore_fuori_tabella': False,
    }
    out.update(flag)
    return out


def _resa_nesting(cfg: dict, warnings: list) -> float:
    """Resa del nesting valida in (0, 1]. Valori impossibili → 1 con avviso."""
    try:
        resa = float(cfg.get('resa_nesting', 1.0) or 1.0)
    except (TypeError, ValueError):
        resa = 1.0
    if resa <= 0 or resa > 1:
        warnings.append(f'Resa nesting {resa} non valida (deve stare tra 0 e 1): usata 1.')
        return 1.0
    return resa


def stima_base(articolo: dict, config: dict | None = None) -> dict:
    """Stima costo base pezzo con modello fisico (materiale + laser + operaio).

    Args:
        articolo: dict con almeno {materiale, spessore_mm, area_dm2, perimetro_taglio_m}.
                  n_forature default 0, gas_taglio opzionale (auto-derivato se assente),
                  peso_kg override opzionale.
        config: dict con `laser_config`. Se None usa DEFAULT_LASER_CONFIG.

    Returns:
        {peso_kg, costo_materiale, costo_lavoro, tempo_taglio_s, tempo_pierce_s,
         tempo_totale_min, setup_eur, base, warnings, _euro_kg,
         _euro_h_macchina, _euro_h_operaio, _ricetta}
    """
    cfg = (config or {}).get('laser_config') or DEFAULT_LASER_CONFIG
    materiali = cfg.get('materiali') or DEFAULT_LASER_CONFIG['materiali']
    eur_h_macchina = float(cfg.get('euro_h_macchina', 75.0))
    eur_h_operaio = float(cfg.get('euro_h_operaio', 25.0))
    eur_h_tot = eur_h_macchina + eur_h_operaio

    warnings: list[str] = []
    notes: list[str] = []  # Info operative, NON allarmanti (interpolazione ricette, ecc.)

    area_dm2 = float(articolo.get('area_dm2') or 0.0)
    perimetro_m = float(articolo.get('perimetro_taglio_m') or 0.0)
    n_forature = int(articolo.get('n_forature') or 0)
    spessore_mm = float(articolo.get('spessore_mm') or 0.0)
    materiale = (articolo.get('materiale') or '').strip().upper()
    gas_richiesto = (articolo.get('gas_taglio') or '').strip().upper() or None
    peso_kg_dato = articolo.get('peso_kg')

    if not materiale:
        warnings.append('Materiale non specificato')
    if spessore_mm <= 0:
        warnings.append('Spessore non specificato')
    if perimetro_m <= 0:
        warnings.append('Perimetro di taglio non valido (importa DXF)')
    if warnings:
        return _empty_result(warnings)

    # Risolvi alias (INOX_316 → INOX_304, ALU_5754 → ALU, S235JR → S235, ecc.)
    canonical, mat = _resolve_material(materiale, materiali)
    if not mat:
        return _empty_result(
            [f'Materiale "{materiale}" sconosciuto: non ci sono prezzo al kg ne\' '
             'ricetta di taglio. Scegli un materiale dall\'elenco o inserisci un '
             'prezzo manuale.'],
            materiale_sconosciuto=True, ricetta_mancante=True)
    if canonical != materiale:
        notes.append(f'Materiale "{materiale}" mappato su "{canonical}" per coefficienti')

    densita = float(mat.get('densita_kg_dm3', 7.85))
    euro_kg = float(mat.get('euro_kg', 0.0))
    # Setup pezzo unico globale (editabile da tab Impostazioni). Il valore
    # per-materiale è mantenuto solo come fallback backward-compat.
    setup_eur = float(cfg.get('setup_eur_default', mat.get('setup_eur', 0.10)))

    # --- Peso: se non passato, calcolo da area × spessore × densità ---
    if peso_kg_dato and float(peso_kg_dato) > 0:
        peso_kg = float(peso_kg_dato)
        peso_source = 'override'
    else:
        if area_dm2 <= 0:
            warnings.append('Area non valida (non calcolabile peso)')
            return _empty_result(warnings)
        peso_kg = area_dm2 * (spessore_mm / 100.0) * densita
        peso_source = 'calcolato'

    # Sfrido di nesting: si compra il foglio, non il pezzo. Con resa 1 (default)
    # il lordo coincide col netto e il prezzo non cambia.
    resa = _resa_nesting(cfg, warnings)
    peso_lordo_kg = peso_kg / resa
    costo_materiale = peso_lordo_kg * euro_kg

    # --- Ricetta Lantek per velocità + pierce ---
    # Se l'utente ha configurato le velocità nelle Impostazioni (laser_config.ricette_taglio)
    # usiamo quelle; altrimenti il fallback è il file JSON calibrato di default.
    # La ricetta si cerca per FAMIGLIA (S235JR → S235, INOX_316L → INOX_304):
    # prima si passava la sigla originale e per le varianti non si trovava nulla.
    famiglia = normalizza_materiale(materiale) or canonical
    ricette_cfg = cfg.get('ricette_taglio') or None
    ricetta = lookup_ricetta(famiglia, spessore_mm, gas_richiesto, ricette_override=ricette_cfg)
    if ricetta and float(ricetta.get('velocita_mm_min') or 0) <= 0:
        ricetta = None
    if not ricetta:
        # Senza ricetta il TAGLIO non e' stimabile, ma il materiale si': meglio
        # un costo parziale dichiarato che uno zero spacciato per stima.
        base_parziale = costo_materiale + setup_eur
        warnings.append(
            f'Ricetta di taglio non trovata per {materiale} {spessore_mm:g} mm '
            f'(gas {gas_richiesto or default_gas(famiglia, spessore_mm)}): '
            'il costo NON comprende il taglio laser. Inserisci un prezzo manuale.')
        return {
            'peso_kg': round(peso_kg, 4),
            'peso_lordo_kg': round(peso_lordo_kg, 4),
            'resa_nesting': resa,
            'costo_materiale': round(costo_materiale, 4),
            'costo_lavoro': 0.0,
            'tempo_taglio_s': 0.0, 'tempo_pierce_s': 0.0, 'tempo_totale_min': 0.0,
            'setup_eur': round(setup_eur, 4),
            'base': round(base_parziale, 2),
            'warnings': warnings, 'notes': notes,
            'ricetta_mancante': True, 'materiale_sconosciuto': False,
            'spessore_fuori_tabella': False,
            '_peso_source': peso_source, '_euro_kg': euro_kg,
            '_euro_h_macchina': eur_h_macchina, '_euro_h_operaio': eur_h_operaio,
            '_euro_h_tot': eur_h_tot,
        }
    if ricetta.get('ricetta_di_fabbrica'):
        warnings.append(
            f'Velocita\' di taglio configurata non valida per {famiglia} '
            f'{spessore_mm:g} mm: usata la ricetta Lantek di fabbrica.')

    vel_mm_min = float(ricetta['velocita_mm_min'])
    pierce_s = float(ricetta['pierce_time_s'])

    # --- Tempi ---
    perim_mm = perimetro_m * 1000.0
    tempo_taglio_s = perim_mm / vel_mm_min * 60.0        # (mm / (mm/min)) × 60 = s
    tempo_pierce_s = n_forature * pierce_s
    tempo_totale_s = tempo_taglio_s + tempo_pierce_s

    # --- Costi ---
    costo_lavoro = (tempo_totale_s / 3600.0) * eur_h_tot
    base = costo_lavoro + costo_materiale + setup_eur

    # Segnalazione ricetta: clamp e fallback sono warning veri (fuori tabella
    # o gas non disponibile). Interpolazione è NOTA informativa: lo spessore
    # utente non viene modificato, sono solo velocità e pierce derivati per
    # interpolazione lineare tra le due ricette Lantek adiacenti (prassi
    # standard CAM). Non deve allarmare l'utente.
    src = ricetta.get('source', 'exact')
    fuori_tabella = src in ('clamped_min', 'clamped_max')
    if fuori_tabella:
        warnings.append(
            f'Spessore {spessore_mm:g} mm fuori tabella Lantek '
            f'({famiglia}/{ricetta["gas"]}): usata la velocita\' dello spessore '
            f'limite {"minimo" if src == "clamped_min" else "massimo"}, '
            'stima del taglio da verificare.'
        )
    elif src == 'interpolated':
        notes.append(
            f'Ricetta {materiale}/{ricetta["gas"]} derivata per interpolazione lineare '
            f'tra RID {ricetta.get("rid_riferimento_min")} e {ricetta.get("rid_riferimento_max")} '
            f'(spessore {spessore_mm}mm non presente in tabella).'
        )
    elif src == 'fallback':
        warnings.append(
            f'Gas {ricetta.get("gas_richiesto")} non disponibile per {materiale} — usato {ricetta["gas"]}.'
        )

    # Sanity check: area sospettamente grande rispetto al perimetro (cartiglio nel DXF?)
    if perimetro_m > 0 and area_dm2 > 0:
        perim_mm_local = perimetro_m * 1000.0
        area_max_plausibile_dm2 = ((perim_mm_local / 4) ** 2) / 10000.0
        if area_dm2 > area_max_plausibile_dm2 * 3:
            warnings.append(
                'Area sospettamente grande rispetto al perimetro: verifica che il DXF non '
                'includa il cartiglio. Usa "Trova pezzo" nel modal per selezionare il contorno corretto.'
            )

    return {
        'peso_kg': round(peso_kg, 4),
        'peso_lordo_kg': round(peso_lordo_kg, 4),
        'resa_nesting': resa,
        'ricetta_mancante': False,
        'materiale_sconosciuto': False,
        'spessore_fuori_tabella': fuori_tabella,
        'costo_materiale': round(costo_materiale, 4),
        'costo_lavoro': round(costo_lavoro, 4),
        'tempo_taglio_s': round(tempo_taglio_s, 2),
        'tempo_pierce_s': round(tempo_pierce_s, 2),
        'tempo_totale_min': round(tempo_totale_s / 60.0, 2),
        'setup_eur': round(setup_eur, 4),
        'base': round(base, 2),
        'warnings': warnings,
        'notes': notes,
        # Debug/UI breakdown
        '_peso_source': peso_source,
        '_euro_kg': euro_kg,
        '_euro_h_macchina': eur_h_macchina,
        '_euro_h_operaio': eur_h_operaio,
        '_euro_h_tot': eur_h_tot,
        '_ricetta': {
            'materiale': ricetta['materiale'],
            'gas': ricetta['gas'],
            'velocita_mm_min': ricetta['velocita_mm_min'],
            'pierce_time_s': ricetta['pierce_time_s'],
            'source': src,
            'rid': ricetta.get('rid_riferimento') or ricetta.get('rid_riferimento_min'),
        },
    }
