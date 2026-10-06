"""Lantek: lettura (SOLA LETTURA) del suo database e file delle quantita' per iErp.

Diagnostica del 06/10/2026 (memoria "lantek-database"): Lantek tiene i dati in
SQL Server Express sul PC server, istanza localhost\\LANTEK, database LSDB.
FerroTrack non scrive MAI in Lantek: legge l'archivio pezzi e prepara un file
Excel che Stefano importa con iErp (modulo di Lantek Sistemi, modello
C:\\LantekSistemi\\iErp\\Modelli\\ImportProduzione.xlt):

    Codice Articolo | Quantita' | Materiale | Spessore | Data consegna | Commessa

Oggi le quantita' si scrivono a mano in Lantek guardando il PDF dell'ordine
(ordine 1184: 57 codici). Con questo file si importano in un colpo.

Se Lantek non risponde (spento, aggiornato, configurazione diversa) il file si
prepara lo stesso con i dati di FerroTrack, senza i controlli: FerroTrack
funziona come prima.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date, datetime

logger = logging.getLogger(__name__)

# Collegamento predefinito; si cambia in app_config.json, chiave "lantek_db":
# {"abilitato": true, "server": "localhost\\LANTEK", "database": "LSDB"}
DB_DEFAULT = {'abilitato': True, 'server': r'localhost\LANTEK', 'database': 'LSDB',
              'driver': 'ODBC Driver 17 for SQL Server'}

# Materiali di FerroTrack -> nomi dei materiali in Lantek (DIS_MMTT_MMTT_00000100).
# Vale solo per i codici NUOVI: per quelli gia' in Lantek si usa il suo.
MATERIALI_LANTEK = {
    'S235': 'FERRO', 'S275': 'FERRO', 'S355': 'FERRO', 'FERRO': 'FERRO', 'DC01': 'FERRO',
    'C75': 'FERRO',
    'INOX_304': 'INOX', 'INOX_316': 'INOX', 'INOX': 'INOX',
    'ALU': 'ALLUMINIO', 'ALLUMINIO': 'ALLUMINIO',
    'ZINCATO': 'ZINCATO',
}

INTESTAZIONE = ['Codice Articolo', 'Quantità', 'Materiale', 'Spessore', 'Data consegna', 'Commessa']

_RX_REV = re.compile(r'^(?P<base>.+)-(?P<rev>\d{2})$')


# ---------------------------------------------------------------------------
# Lettura del database di Lantek
# ---------------------------------------------------------------------------
def _config() -> dict:
    try:
        from .database import ConfigManager
        cfg = (ConfigManager.load_config() or {}).get('lantek_db') or {}
    except Exception:
        cfg = {}
    return {**DB_DEFAULT, **(cfg if isinstance(cfg, dict) else {})}


def _connessione(cfg: dict):
    import pyodbc
    cs = (f"DRIVER={{{cfg['driver']}}};SERVER={cfg['server']};DATABASE={cfg['database']};"
          'Trusted_Connection=yes;ApplicationIntent=ReadOnly;APP=FerroTrack-lettura')
    con = pyodbc.connect(cs, timeout=4, readonly=True, autocommit=True)
    cur = con.cursor()
    # mai bloccare Lantek: si legge senza aspettare chi sta scrivendo
    cur.execute('SET TRANSACTION ISOLATION LEVEL READ UNCOMMITTED')
    return con, cur


_CACHE = {}
_LOCK = threading.Lock()
_DURATA_CACHE_S = 60


def pezzi_in_lantek(codici: list) -> dict:
    """Per ogni codice: {'esiste', 'materiale', 'spessore', 'revisioni': [...]}
    letti dall'archivio pezzi di Lantek (PPRR_PPRR_00000100), piu' le altre
    revisioni dello stesso codice (es. -00 e -02).

    Ritorna {'disponibile': bool, 'errore': str|None, 'pezzi': {...}}.
    Non solleva mai: senza Lantek, disponibile=False."""
    codici = sorted({str(c).strip() for c in codici if c and str(c).strip()})
    cfg = _config()
    if not cfg.get('abilitato'):
        return {'disponibile': False, 'errore': 'collegamento a Lantek disattivato', 'pezzi': {}}
    if not codici:
        return {'disponibile': True, 'errore': None, 'pezzi': {}}
    chiave = (cfg['server'], cfg['database'], tuple(codici))
    with _LOCK:
        c = _CACHE.get(chiave)
        if c and time.time() - c[0] < _DURATA_CACHE_S:
            return c[1]
    basi = {}
    for cod in codici:
        m = _RX_REV.match(cod)
        basi[cod] = m.group('base') if m else cod
    try:
        con, cur = _connessione(cfg)
        try:
            revisioni = {}
            for base in sorted(set(basi.values())):
                cur.execute('SELECT PrdRef FROM PPRR_PPRR_00000100 WHERE PrdRef LIKE ?',
                            [base.replace('[', '[[]') + '-[0-9][0-9]'])
                revisioni[base] = sorted(str(r[0]).strip() for r in cur.fetchall())
            # i codici chiesti e tutte le loro revisioni, a blocchi (SQL Server
            # accetta al massimo ~2100 parametri per richiesta)
            esatti = sorted(set(codici) | {r for v in revisioni.values() for r in v})
            trovati = {}
            for i in range(0, len(esatti), 500):
                blocco = esatti[i:i + 500]
                cur.execute('SELECT PrdRef, DIS_MatRef, DIS_Thickness FROM PPRR_PPRR_00000100 '
                            'WHERE PrdRef IN (' + ','.join('?' * len(blocco)) + ')', blocco)
                for prd, mat, sp in cur.fetchall():
                    trovati[str(prd).strip().upper()] = (str(prd).strip(), mat, sp)
        finally:
            con.close()
    except Exception as e:
        logger.info('Lantek non leggibile: %s', e)
        return {'disponibile': False, 'errore': 'Lantek non risponde: ' + str(e)[:160], 'pezzi': {}}
    pezzi = {}
    for cod in codici:
        t = trovati.get(cod.upper())
        altre = [r for r in revisioni.get(basi[cod], []) if r.upper() != cod.upper()]
        # Codice scritto senza revisione (25NDSPA1979) e in Lantek ce n'e' una
        # sola (25NDSPA1979-00): e' quella. Con piu' revisioni non si sceglie.
        if t is None and not _RX_REV.match(cod) and len(altre) == 1:
            t = trovati.get(altre[0].upper())
        pezzi[cod] = {
            'esiste': t is not None,
            'codice_lantek': t[0] if t else None,
            'materiale': (t[1] or None) if t else None,
            'spessore': float(t[2]) if t and t[2] is not None else None,
            'revisioni': altre,
        }
    esito = {'disponibile': True, 'errore': None, 'pezzi': pezzi}
    with _LOCK:
        if len(_CACHE) > 200:
            _CACHE.clear()
        _CACHE[chiave] = (time.time(), esito)
    return esito


# ---------------------------------------------------------------------------
# Righe del file delle quantita'
# ---------------------------------------------------------------------------
def _rev(codice: str):
    m = _RX_REV.match(codice or '')
    return (m.group('base'), int(m.group('rev'))) if m else (codice, None)


def righe_quantita(distinta: list, lantek: dict | None = None) -> list:
    """Dalle righe della distinta (app._distinta_ordine) le righe da importare:
    solo le lamiere col codice, una per codice con le quantita' sommate.

    Con i dati di Lantek (pezzi_in_lantek): materiale e spessore come sono in
    Lantek per i codici che ci sono gia', e gli avvisi (codice nuovo,
    materiale/spessore diversi, altra revisione)."""
    per_codice = {}
    for r in distinta or []:
        if r.get('tipo') != 'lamiera':
            continue
        cod = str(r.get('codice') or '').strip()
        if not cod:
            continue
        q = int(r.get('quantita') or 0)
        if q <= 0:
            continue
        x = per_codice.setdefault(cod, {'codice': cod, 'quantita': 0, 'materiale_ft': r.get('materiale'),
                                        'spessore_ft': r.get('spessore_mm')})
        x['quantita'] += q
    pezzi = (lantek or {}).get('pezzi') or {}
    disponibile = bool((lantek or {}).get('disponibile'))
    out = []
    for cod in sorted(per_codice, key=str.lower):
        x = per_codice[cod]
        mat_ft = (x['materiale_ft'] or '').strip().upper()
        mat_lt = MATERIALI_LANTEK.get(mat_ft)
        sp_ft = x['spessore_ft']
        try:
            sp_ft = float(sp_ft) if sp_ft not in (None, '') else None
        except (TypeError, ValueError):
            sp_ft = None
        avvisi = []
        p = pezzi.get(cod) if disponibile else None
        materiale, spessore = mat_lt or (x['materiale_ft'] or ''), sp_ft
        if p and p['esiste']:
            stato = 'in_lantek'
            if p['materiale']:
                if mat_lt and p['materiale'].upper() != mat_lt.upper() and not p['materiale'].upper().endswith(mat_lt.upper()):
                    avvisi.append(f"materiale: l'ordine dice {x['materiale_ft']}, in Lantek e' {p['materiale']}")
                materiale = p['materiale']
            if p['spessore'] is not None:
                if sp_ft is not None and abs(p['spessore'] - sp_ft) > 0.01:
                    avvisi.append(f"spessore: l'ordine dice {sp_ft:g} mm, in Lantek e' {p['spessore']:g} mm")
                spessore = p['spessore']
        elif disponibile:
            stato = 'nuovo'
            if p and p['revisioni'] and _rev(cod)[1] is None:
                # codice senza revisione e in Lantek ce ne sono piu' d'una
                avvisi.append('in Lantek ci sono piu\' revisioni (' + ', '.join(p['revisioni'])
                              + '): scegli quella giusta')
            else:
                avvisi.append('non ancora in Lantek: importa prima il disegno')
        else:
            stato = 'sconosciuto'
        # nel file va il codice esatto di Lantek (25NDSPA1979 -> 25NDSPA1979-00)
        codice_lt = (p.get('codice_lantek') if p else None) or cod
        if p and p['revisioni'] and _rev(cod)[1] is not None:
            rev = _rev(cod)[1]
            piu_nuove = [r for r in p['revisioni'] if _rev(r)[1] is not None and _rev(r)[1] > rev]
            if piu_nuove:
                avvisi.append('in Lantek c\'e\' una revisione piu\' nuova: ' + ', '.join(piu_nuove))
            elif stato == 'nuovo':
                avvisi.append('in Lantek ci sono altre revisioni: ' + ', '.join(p['revisioni']))
        if not mat_lt and not (p and p['esiste'] and p['materiale']):
            avvisi.append(f"materiale {x['materiale_ft'] or '?'}: da scegliere in Lantek")
        out.append({'codice': codice_lt, 'codice_ft': cod, 'quantita': x['quantita'],
                    'materiale': materiale, 'spessore': spessore, 'stato': stato, 'avvisi': avvisi})
    return out


def commessa_lantek(numero: str) -> str:
    """Il numero d'ordine come lo si scrive in Lantek (campo "riferimento"):
    "A 001252" -> "1252" (DECA: sigla, spazio, zeri). Senza sigla resta com'e'
    ("1184", "061": in Lantek lo zero c'e'); le sigle interne
    (PREV-2026-0005) restano come sono."""
    n = str(numero or '').strip()
    m = re.fullmatch(r'[A-Za-z]{1,3}[\s.\-/]+0*(\d+)', n)
    return m.group(1) if m else n


def scrivi_excel(percorso_o_file, righe: list, data_consegna=None, commessa: str = '') -> None:
    """Il file nel formato del modello iErp "ImportProduzione" (Foglio1, prima
    riga d'intestazione, una riga per codice)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = 'Foglio1'
    ws.append(INTESTAZIONE)
    for cella in ws[1]:
        cella.font = Font(bold=True)
    if isinstance(data_consegna, str):
        try:
            data_consegna = datetime.strptime(data_consegna[:10], '%Y-%m-%d')
        except ValueError:
            data_consegna = None
    elif isinstance(data_consegna, date) and not isinstance(data_consegna, datetime):
        data_consegna = datetime.combine(data_consegna, datetime.min.time())
    for r in righe:
        ws.append([r['codice'], int(r['quantita']), r['materiale'] or '',
                   r['spessore'] if r['spessore'] is not None else None,
                   data_consegna, commessa or ''])
    for riga in ws.iter_rows(min_row=2, min_col=5, max_col=5):
        for cella in riga:
            cella.number_format = 'DD/MM/YYYY'
    for col, larg in zip('ABCDEF', (28, 10, 14, 10, 15, 16)):
        ws.column_dimensions[col].width = larg
    wb.save(percorso_o_file)
