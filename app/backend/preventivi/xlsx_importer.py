"""XLSX (Lantek) importer service.

Extended for FerroTrack merge: oltre a codice/costo/area, legge anche
materiale, spessore, peso, perimetro di taglio, pieghe — tutti dati che
Lantek già calcola e che ci permettono di evitare la stima da DXF approssimativa.

Mapping materiale Lantek (formato libero) → codice del nostro laser_estimator:
  'INOX', 'INOX 304', 'AISI 304', ...      → 'INOX_304'
  'INOX 316', 'AISI 316'                    → 'INOX_316'
  'ACCIAIO', 'S235', 'FERRO'                → 'S235'
  'ALLUMINIO', 'ALU', 'AL'                  → 'ALU_5754'
"""

import logging

import openpyxl

logger = logging.getLogger(__name__)


def _normalize_materiale(raw: str) -> str:
    """Mappa il valore Lantek 'Materiale' al codice del laser_estimator.

    Returns:
        Codice riconosciuto ('S235'|'INOX_304'|'INOX_316'|'ALU_5754') oppure
        il valore raw uppercase se non mappabile (commerciale può scegliere a mano).
    """
    if not raw:
        return ''
    s = str(raw).strip().upper()
    if not s:
        return ''
    if '316' in s:
        return 'INOX_316'
    if 'INOX' in s or 'AISI' in s or 'STAINLESS' in s:
        return 'INOX_304'
    if 'ALLUM' in s or s.startswith('ALU') or s == 'AL':
        return 'ALU_5754'
    if 'ACCI' in s or 'S235' in s or 'FERRO' in s or 'STEEL' in s:
        return 'S235'
    return s  # raw uppercase, commerciale può cambiare via dropdown


# Mappa nome-colonna → indice nel template "singolo" Lantek (header standard).
# Documento solo le colonne che ci interessano. Se Lantek cambia nome colonna,
# si trova lo stesso fallback a indice numerico.
COL_SINGOLO = {
    'codice': 0,           # 'Codice'
    'costo_standard': 12,  # 'Costo standard'
    'peso': 21,            # 'Peso' (kg)
    'materiale': 60,       # 'Materiale' (es. 'INOX')
    'spessore': 63,        # 'Spessore' (mm)
    'area': 64,            # 'Area' (m²)
    'perimetro_taglio': 72,  # 'Perimetro di taglio' (m)
    'pieghe_semplici': 141,  # 'Pieghe semplici'
    'pieghe_speciali': 142,  # 'Pieghe speciali'
}


def importa_xlsx(path: str) -> list[dict]:
    """Importa dati da file XLSX Lantek con tutti i dati materiale + geometria.

    Gestisce template "singolo" (un articolo per riga, standard Lantek) e
    template "multi" (ordine di produzione con quantità + costo totale).
    Per il template singolo legge anche materiale, spessore, area, perimetro,
    peso, pieghe — eliminando la necessità di stima approssimativa per il taglio.
    Aggrega automaticamente i duplicati (stesso codice in più righe).

    Returns:
        Lista di dict con chiavi (oltre alle 4 originali):
        - codice, costo, costo_totale, area, quantita, row
        - materiale (codice normalizzato, es. 'INOX_304') — '' se non riconosciuto
        - materiale_raw (valore originale dal Lantek, es. 'INOX')
        - spessore_mm
        - area_dm2 (= area m² × 100)
        - perimetro_taglio_m
        - peso_kg
        - pieghe (= pieghe_semplici + pieghe_speciali)

    Raises:
        ValueError: se nessun articolo trovato.
    """
    wb = openpyxl.load_workbook(path, data_only=True)

    if "Struttura" in wb.sheetnames:
        ws = wb["Struttura"]
    else:
        ws = wb.active

    headers = [cell.value for cell in ws[1]]

    if headers and headers[0] == "Codice":
        # Template singolo articolo — supporto completo
        template = "singolo"
        cols = COL_SINGOLO
    else:
        # Template multi articolo (ordine di produzione)
        # Solo i campi base: codice + costo + qty (no materiale/spessore qui)
        template = "multi"
        cols = {'codice': 1, 'costo_standard': 22, 'peso': None,
                'materiale': None, 'spessore': None, 'area': None,
                'perimetro_taglio': None, 'pieghe_semplici': None,
                'pieghe_speciali': None}
        quantita_col = 21

    def _get(row, col_idx, default=None):
        if col_idx is None or len(row) <= col_idx:
            return default
        v = row[col_idx]
        return v if v not in (None, '') else default

    articoli = []
    for row_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        codice = _get(row, cols['codice'])
        costo_totale = _get(row, cols['costo_standard'], 0)
        if template == "multi":
            quantita = _get(row, quantita_col, 1) or 1
        else:
            quantita = 1
        try:
            quantita = max(1, int(quantita))
        except (TypeError, ValueError):
            quantita = 1
        try:
            costo_unit = float(costo_totale) / float(quantita) if costo_totale else 0.0
        except (TypeError, ValueError):
            costo_unit = 0.0

        if not codice or not costo_totale:
            continue

        area_m2 = float(_get(row, cols['area'], 0) or 0)
        spessore_mm = float(_get(row, cols['spessore'], 0) or 0)
        peso_kg = float(_get(row, cols['peso'], 0) or 0)
        perimetro_m = float(_get(row, cols['perimetro_taglio'], 0) or 0)
        materiale_raw = _get(row, cols['materiale'], '') or ''
        materiale_norm = _normalize_materiale(materiale_raw)
        pieghe_semplici = int(_get(row, cols['pieghe_semplici'], 0) or 0)
        pieghe_speciali = int(_get(row, cols['pieghe_speciali'], 0) or 0)
        pieghe_tot = pieghe_semplici + pieghe_speciali

        articoli.append({
            "codice": str(codice),
            "costo": costo_unit,
            # La quantita' del file veniva letta per ricavare il costo unitario
            # e poi buttata via: una riga da 50 pezzi diventava 1 pezzo, e il
            # totale del preventivo usciva 50 volte piu' basso.
            "quantita": quantita,
            "area": area_m2,
            "row": row_idx,
            # --- Extension ---
            "materiale": materiale_norm,
            "materiale_raw": str(materiale_raw) if materiale_raw else '',
            "spessore_mm": spessore_mm if spessore_mm > 0 else None,
            "area_dm2": area_m2 * 100,  # m² → dm²
            "perimetro_taglio_m": perimetro_m,
            "peso_kg": peso_kg,
            "pieghe": pieghe_tot,
            "pieghe_semplici": pieghe_semplici,
            "pieghe_speciali": pieghe_speciali,
        })

    wb.close()

    if not articoli:
        raise ValueError("Nessun articolo trovato nel file.")

    # Aggrega i duplicati sommando le QUANTITA' dichiarate nel file, non
    # contando le righe: tre righe da 20 pezzi fanno 60, non 3.
    aggregati = {}
    for art in articoli:
        codice = art['codice']
        qta = int(art.get('quantita') or 1)
        if codice in aggregati:
            a = aggregati[codice]
            a['quantita'] += qta
            a['costo_totale'] += art['costo'] * qta
            a['area'] += art['area']
            a['area_dm2'] += art['area_dm2']
            a['peso_kg'] += art['peso_kg']
            # Il costo unitario resta quello del pezzo: si ricalcola sulla
            # quantita' complessiva per restare coerente col totale.
            a['costo'] = round(a['costo_totale'] / a['quantita'], 6) if a['quantita'] else 0.0
        else:
            aggregati[codice] = {
                **art,
                'costo_totale': art['costo'] * qta,
                'quantita': qta,
            }

    return list(aggregati.values())
