"""Lookup ricette Lantek reali del cliente (velocità taglio + tempo pierce).

Il file `lantek_recipes.json` è generato una tantum dallo xlsx fornito dal
cliente (`_test_input/parametri_taglio_completi_aggiornati.xlsx`). Contiene
116 ricette calibrate sul suo laser (macchina fibra, potenza da mm/min visti).

Regola gas del cliente (2026-07-02, cfr. Stefano):
- S235 (ferro):  N2 se spessore ≤ 3mm, O2 se spessore > 3mm
- INOX/ALU/ZINCATO/OTTONE: sempre N2

Il modello di stima costo taglio userà:
    tempo_taglio_s  = (perimetro_m × 1000) / velocita_mm_min × 60
    tempo_pierce_s  = n_forature × pierce_time_s
    costo_taglio    = (tempo_taglio_s + tempo_pierce_s) × (€/h_macchina / 3600)
"""
from __future__ import annotations

import json
import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)

_RECIPES_PATH = os.path.join(os.path.dirname(__file__), 'lantek_recipes.json')


@lru_cache(maxsize=1)
def load_recipes() -> dict:
    """Carica il JSON ricette (cached). Ritorna {'metadata': ..., 'ricette': [...]}."""
    try:
        with open(_RECIPES_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error('impossibile caricare lantek_recipes.json: %s', e)
        return {'metadata': {}, 'ricette': []}


def default_gas(materiale: str, spessore_mm: float) -> str:
    """Regola di default gas del cliente."""
    if (materiale or '').upper() == 'S235':
        return 'N2' if spessore_mm <= 3.0 else 'O2'
    return 'N2'


# Famiglie di ricette presenti nel file Lantek del cliente. Ogni materiale che
# arriva dal CAD, dall'XLSX o dal menu va ricondotto a una di queste, altrimenti
# la ricetta non si trova e il costo di taglio sparisce.
FAMIGLIE_RICETTE = ('S235', 'ZINCATO', 'INOX_304', 'ALU', 'OTTONE')

# Alias esatti → famiglia di ricetta piu' vicina. Le sigle degli acciai al
# carbonio tagliano come S235 (stessa velocita' a parita' di spessore), il 316
# come il 304, tutte le leghe di alluminio come ALU.
_ALIAS_MATERIALE = {
    # acciaio al carbonio
    'FERRO': 'S235', 'FE': 'S235', 'ACCIAIO': 'S235', 'ACCIAIO_NERO': 'S235',
    'S235JR': 'S235', 'S235J0': 'S235', 'S235J2': 'S235', 'S275': 'S235',
    'S275JR': 'S235', 'S355': 'S235', 'S355JR': 'S235', 'S355J2': 'S235',
    'FE360': 'S235', 'FE430': 'S235', 'ST37': 'S235', 'C45': 'S235',
    'DC01': 'S235', 'DC04': 'S235', 'DD11': 'S235', 'DD13': 'S235',
    'LAMIERA_NERA': 'S235', 'DECAPATO': 'S235',
    # zincato
    'ZINCATO': 'ZINCATO', 'DX51D': 'ZINCATO', 'DX51': 'ZINCATO',
    'SENDZIMIR': 'ZINCATO', 'GALVANIZZATO': 'ZINCATO', 'ELETTROZINCATO': 'ZINCATO',
    # inox (le ricette Lantek non hanno il 316: si usa il 304)
    'INOX': 'INOX_304', 'INOX_304L': 'INOX_304', 'INOX_316': 'INOX_304',
    'INOX_316L': 'INOX_304', 'INOX_430': 'INOX_304', 'AISI_304': 'INOX_304',
    'AISI_316': 'INOX_304', 'AISI304': 'INOX_304', 'AISI316': 'INOX_304',
    # alluminio
    'ALLUMINIO': 'ALU', 'AL': 'ALU', 'ALU_5083': 'ALU', 'ALU_5754': 'ALU',
    'ALU_6082': 'ALU', 'ALU_6061': 'ALU', 'ALU_6060': 'ALU', 'ALU_1050': 'ALU',
    # ottone
    'OTTONE': 'OTTONE', 'CUZN': 'OTTONE', 'CUZN37': 'OTTONE', 'CUZN39PB3': 'OTTONE',
}

# Prefissi per le sigle con suffissi di stato/finitura (es. ALU_5754_H111,
# S355J2+N, INOX_304_2B). L'ordine conta: il piu' specifico prima.
_PREFISSI_MATERIALE = (
    ('INOX', 'INOX_304'), ('AISI', 'INOX_304'),
    ('ALU', 'ALU'), ('ALLUMINIO', 'ALU'),
    ('S235', 'S235'), ('S275', 'S235'), ('S355', 'S235'),
    ('DX51', 'ZINCATO'), ('ZINC', 'ZINCATO'),
    ('OTTONE', 'OTTONE'), ('CUZN', 'OTTONE'),
)


def _pulisci_sigla(mat: str) -> str:
    """'Inox 316L' → 'INOX_316L', 'alu-6082' → 'ALU_6082', 'S355J2+N' → 'S355J2_N'."""
    m = (mat or '').strip().upper()
    for ch in (' ', '-', '.', '/', '+'):
        m = m.replace(ch, '_')
    while '__' in m:
        m = m.replace('__', '_')
    return m.strip('_')


def normalizza_materiale(mat: str) -> str | None:
    """Famiglia di ricetta Lantek per un materiale, o None se non riconducibile.

    None significa davvero "non so come si taglia" (es. rame, 'ALTRO'): chi
    chiama deve dirlo all'utente, non inventare una ricetta.
    """
    m = _pulisci_sigla(mat)
    if not m:
        return None
    if m in FAMIGLIE_RICETTE:
        return m
    if m in _ALIAS_MATERIALE:
        return _ALIAS_MATERIALE[m]
    for prefisso, famiglia in _PREFISSI_MATERIALE:
        if m.startswith(prefisso):
            return famiglia
    return None


def _normalize_mat(mat: str) -> str:
    """Compatibilita': famiglia se riconosciuta, altrimenti la sigla ripulita
    (che non trovera' ricetta — e lookup_ricetta tornera' None)."""
    return normalizza_materiale(mat) or _pulisci_sigla(mat)


def _ricetta_valida(r) -> bool:
    """Una ricetta con velocita' nulla o negativa e' un dato rotto (cella
    lasciata vuota nelle Impostazioni): usarla porterebbe a una divisione per
    zero o, peggio, a un costo di taglio nullo."""
    try:
        return (float(r.get('velocita_mm_min') or 0) > 0
                and float(r.get('pierce_time_s') or 0) >= 0
                and r.get('materiale') and r.get('gas')
                and float(r.get('spessore_mm') or 0) > 0)
    except (TypeError, ValueError, AttributeError):
        return False


def lookup_ricetta(materiale: str, spessore_mm: float, gas: str | None = None,
                   ricette_override: list | None = None) -> dict | None:
    """Trova la ricetta Lantek per (materiale, spessore, gas).

    Se spessore non esatto: interpola linearmente fra i due più vicini nella
    stessa serie (materiale+gas). Fuori range: usa il limite più vicino.

    Se gas non specificato: usa `default_gas(materiale, spessore)`.

    `ricette_override`: se passato (lista non vuota di ricette configurate dall'utente
    nelle Impostazioni), usa QUELLE invece del file JSON di default. Ogni voce deve
    avere materiale/gas/spessore_mm/velocita_mm_min/pierce_time_s (rid opzionale).

    Returns:
        dict {materiale, gas, spessore_mm, velocita_mm_min, pierce_time_s,
              source: 'exact'|'interpolated'|'clamped'|'fallback',
              rid_riferimento: int|None}
        oppure None se materiale/gas non presente in tabella.
    """
    mat = _normalize_mat(materiale)
    if not mat:
        return None
    g = (gas or default_gas(mat, spessore_mm)).upper()

    if ricette_override:
        # Le ricette configurate a mano valgono solo se sensate: una velocita'
        # a zero (cella svuotata) non deve azzerare il costo di taglio. Se per
        # questo materiale non ne resta nessuna valida si torna alla tabella
        # di fabbrica, e chi chiama lo segnala.
        valide = [r for r in ricette_override if _ricetta_valida(r)]
        trovata = _cerca_ricetta(valide, mat, g, spessore_mm)
        if trovata:
            return trovata
        trovata = _cerca_ricetta(load_recipes().get('ricette', []), mat, g, spessore_mm)
        if trovata:
            trovata['ricetta_di_fabbrica'] = True
        return trovata
    return _cerca_ricetta(load_recipes().get('ricette', []), mat, g, spessore_mm)


def _cerca_ricetta(ricette: list, mat: str, g: str, spessore_mm: float) -> dict | None:
    """Ricerca vera e propria in una lista di ricette (vedi lookup_ricetta)."""
    serie = [r for r in ricette if r['materiale'] == mat and r['gas'] == g]

    if not serie:
        # Fallback: cerca stesso materiale con qualsiasi gas
        alt = [r for r in ricette if r['materiale'] == mat]
        if not alt:
            return None
        # Usa la serie N2 se disponibile (default per non-ferro), altrimenti la prima
        alt_gases = sorted({r['gas'] for r in alt})
        g_fb = 'N2' if 'N2' in alt_gases else alt_gases[0]
        serie = [r for r in alt if r['gas'] == g_fb]
        if not serie:
            return None
        _fallback_used = g_fb
    else:
        _fallback_used = None

    serie.sort(key=lambda r: r['spessore_mm'])

    # Match esatto
    for r in serie:
        if abs(r['spessore_mm'] - spessore_mm) < 1e-6:
            return {
                'materiale': mat, 'gas': r['gas'], 'spessore_mm': spessore_mm,
                'velocita_mm_min': r['velocita_mm_min'],
                'pierce_time_s': r['pierce_time_s'],
                'source': 'fallback' if _fallback_used else 'exact',
                'rid_riferimento': r.get('rid'),
                'gas_richiesto': g if _fallback_used else None,
            }

    # Fuori range
    if spessore_mm <= serie[0]['spessore_mm']:
        r = serie[0]
        return {
            'materiale': mat, 'gas': r['gas'], 'spessore_mm': spessore_mm,
            'velocita_mm_min': r['velocita_mm_min'],
            'pierce_time_s': r['pierce_time_s'],
            'source': 'clamped_min', 'rid_riferimento': r.get('rid'),
        }
    if spessore_mm >= serie[-1]['spessore_mm']:
        r = serie[-1]
        return {
            'materiale': mat, 'gas': r['gas'], 'spessore_mm': spessore_mm,
            'velocita_mm_min': r['velocita_mm_min'],
            'pierce_time_s': r['pierce_time_s'],
            'source': 'clamped_max', 'rid_riferimento': r.get('rid'),
        }

    # Interpolazione lineare
    for i in range(len(serie) - 1):
        r1, r2 = serie[i], serie[i + 1]
        s1, s2 = r1['spessore_mm'], r2['spessore_mm']
        if s1 <= spessore_mm <= s2:
            t = (spessore_mm - s1) / (s2 - s1)
            vel = r1['velocita_mm_min'] + t * (r2['velocita_mm_min'] - r1['velocita_mm_min'])
            pierce = r1['pierce_time_s'] + t * (r2['pierce_time_s'] - r1['pierce_time_s'])
            return {
                'materiale': mat, 'gas': r1['gas'], 'spessore_mm': spessore_mm,
                'velocita_mm_min': round(vel, 1),
                'pierce_time_s': round(pierce, 4),
                'source': 'interpolated',
                'rid_riferimento_min': r1.get('rid'), 'rid_riferimento_max': r2.get('rid'),
            }
    return None


def list_materiali() -> list[str]:
    """Materiali disponibili in tabella (per dropdown frontend)."""
    return load_recipes().get('metadata', {}).get('materiali', [])


def list_spessori(materiale: str, gas: str | None = None) -> list[float]:
    """Spessori disponibili per una serie (per suggerimento UI)."""
    mat = _normalize_mat(materiale)
    ricette = load_recipes().get('ricette', [])
    if gas:
        g = gas.upper()
        return sorted({r['spessore_mm'] for r in ricette if r['materiale'] == mat and r['gas'] == g})
    return sorted({r['spessore_mm'] for r in ricette if r['materiale'] == mat})
