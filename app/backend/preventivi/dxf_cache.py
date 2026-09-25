"""Cache dei risultati di parsing DXF, chiavata per hash SHA256 del contenuto file.

Se un DXF con stesso hash è già stato parsato, la seconda chiamata restituisce
il risultato cachato istantaneamente. Utile per:

1. **Commesse ripetute**: cliente rimanda gli stessi disegni per un preventivo
   simile → hash uguali → tutte le extrazioni skip.
2. **Batch grandi (100 DXF)**: molti file possono essere identici tra loro
   (varianti di un template) → parsing una sola volta.
3. **Ricaricamento pagina**: dopo un refresh, il preventivo esistente ha già
   articoli con DXF già visti → riapertura istantanea.

Storage: file SQLite dedicato `database/dxf_cache.db` (non tocca il DB
principale scheduler.db).

Chiave: `chiave_cache()` = SHA256 di (hash dei bytes del DXF + nome file +
config di rilevamento + disponibilità Gemini + versione parser). La colonna
si chiama ancora `file_hash` per compatibilità dello schema.

Payload cachato:
- geometria (area, perimetro, n_forature, bbox)
- materiale + confidence
- peso + confidence
- spessore + confidence
- lavorazioni (pieghe, saldatura_ml, filettatura, svasatura)
- svg_string (per thumbnail)

TTL: nessuno. Il file DXF non cambia mai per un dato hash — se cambia,
cambia anche l'hash.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
from typing import Any

logger = logging.getLogger(__name__)

_CACHE_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    'database', 'dxf_cache.db'
)

_LOCK = threading.Lock()
_INITIALIZED = False

# Bump questa costante ogni volta che il pipeline di parsing (dxf_scanner,
# dxf_batch_worker, ecc.) cambia in modo che invaliderebbe payload cachati.
# Le entry con _parser_version < corrente vengono considerate MISS e riparsate.
# v2 (2026-07-05): fallback cartiglio-spessore disaccoppiato dal fallback area
#                  (20PA00690 aveva spessore null nonostante detector area OK)
# v3 (2026-07-05): cartiglio-descrizione spessore prevale su peso_area quando
#                  confidence maggiore (fonte esplicita vs stima indiretta)
# v4 (2026-07-06): auto-cleanup DXF nel batch worker (nuovo campo `cleanup`
#                  nel payload). Bump per rigenerare i puliti sui file cachati.
# v5 (2026-07-12): render SVG con tema chiaro (BackgroundPolicy.WHITE +
#                  ColorPolicy.MONOCHROME_LIGHT_BG). Bump per rigenerare
#                  gli svg_string cachati con lo sfondo scuro.
# v6 (2026-07-12): save_cleaned_dxf ora applica connected-components post-filter
#                  per scartare cluster isolati (cartiglio residuo dentro bbox).
# v7 (2026-07-12): save_cleaned_dxf refactor "a prova di stupido" — non taglia
#                  al bbox utente, ma trova cluster spaziali su TUTTO il DXF
#                  e prende tutto il cluster che tocca il bbox utente. Robusto
#                  a drag rectangle imprecisi (larghi o stretti di qualche mm).
# v8 (2026-07-12): + assorbimento fori interni (entità isolate il cui bbox è
#                  interamente contenuto nel bbox del cluster vincente).
#                  Risolve fori CIRCLE piccoli persi dal clustering.
# v9 (2026-09-24): revisione estrazione DXF — INSERT espansi, $INSUNITS,
#                  entità duplicate, cornici vs piastre con fori, svasature,
#                  confidence, pieghe/materiale/spessore più rigorosi.
#                  Chiave cache = contenuto + nome file + config + Gemini.
# v10 (2026-09-25): geometria con `lunghezza_vuoto_mm` (spostamenti a vuoto
#                  tra gli sfondamenti, per il tempo laser come in Lantek).
# v11 (2026-09-25): filettature/svasature contate solo dentro il pezzo scelto
#                  (niente doppie viste ne' simboli del cartiglio, anche col
#                  pezzo disegnato piu' volte identico).
# v12 (2026-09-25): scartati come pezzo i contorni con piu' di meta' dei testi
#                  del foglio (celle del cartiglio, cornici in scala); contorno
#                  confermato se ha le misure del cartiglio.
# v13 (2026-09-25): disegni esportati in scala (quote con DIMLFAC, es. 1:8)
#                  riportati al vero: prima aree fino a 64 volte piu' piccole.
PARSER_VERSION = 13


def _init_db() -> None:
    """Crea la tabella cache se non esiste (thread-safe, chiamato lazy)."""
    global _INITIALIZED
    if _INITIALIZED:
        return
    with _LOCK:
        if _INITIALIZED:
            return
        os.makedirs(os.path.dirname(_CACHE_DB_PATH), exist_ok=True)
        con = sqlite3.connect(_CACHE_DB_PATH)
        try:
            con.execute("""
                CREATE TABLE IF NOT EXISTS dxf_parse_cache (
                    file_hash TEXT PRIMARY KEY,
                    filename TEXT,
                    payload_json TEXT NOT NULL,
                    svg_string TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    last_hit_at TEXT NOT NULL DEFAULT (datetime('now')),
                    hit_count INTEGER NOT NULL DEFAULT 1
                )
            """)
            con.execute("""
                CREATE INDEX IF NOT EXISTS idx_dxf_cache_last_hit
                ON dxf_parse_cache(last_hit_at)
            """)
            con.commit()
        finally:
            con.close()
        _INITIALIZED = True


def hash_file(path: str) -> str:
    """Calcola SHA256 dei bytes del file DXF. Chunked per file grandi."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while chunk := f.read(1 << 20):  # 1 MB chunks
            h.update(chunk)
    return h.hexdigest()


def chiave_cache(file_hash: str, filename: str, config: dict | None = None,
                 extra: dict | None = None) -> str:
    """Chiave di cache del parsing (BUG FIX D12).

    Il risultato NON dipende solo dai bytes del DXF:
    - nome file: fonte dello spessore (`_sp3`, `_10mm`)
    - config `dxf_detection` (colori piega/saldatura, soglie svasatura…)
    - disponibilità Gemini (materiale riconosciuto o no)
    - versione del parser
    Quindi la chiave è l'hash di tutti questi elementi insieme all'hash file.
    """
    try:
        from . import llm_material_normalizer
        llm = llm_material_normalizer.is_available()
    except Exception:
        llm = False
    parti = {
        'file': file_hash,
        'nome': os.path.basename(filename or '').lower(),
        'cfg': config or {},
        'llm': llm,
        'v': PARSER_VERSION,
        **(extra or {}),
    }
    blob = json.dumps(parti, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def get(file_hash: str) -> dict | None:
    """Recupera payload cachato (dict) + svg_string. None se miss.

    Aggiorna last_hit_at e hit_count (per LRU-style eviction futura).
    """
    _init_db()
    con = sqlite3.connect(_CACHE_DB_PATH)
    try:
        con.row_factory = sqlite3.Row
        cur = con.execute(
            "SELECT payload_json, svg_string FROM dxf_parse_cache WHERE file_hash = ?",
            (file_hash,)
        )
        row = cur.fetchone()
        if not row:
            return None
        # Aggiorna hit stats (non-blocking sui letti, best-effort)
        try:
            con.execute("""
                UPDATE dxf_parse_cache
                SET last_hit_at = datetime('now'), hit_count = hit_count + 1
                WHERE file_hash = ?
            """, (file_hash,))
            con.commit()
        except Exception:
            pass
        try:
            payload = json.loads(row['payload_json'])
        except json.JSONDecodeError:
            logger.warning('cache payload corrotto per hash %s', file_hash[:16])
            return None
        # Invalidazione by version: se il payload è stato scritto con una versione
        # di parser precedente, ignoralo (il worker riparsa e sovrascrive)
        if payload.get('_parser_version', 1) != PARSER_VERSION:
            return None
        return {'payload': payload, 'svg_string': row['svg_string']}
    finally:
        con.close()


def put(file_hash: str, filename: str, payload: dict, svg_string: str | None = None) -> None:
    """Salva payload nella cache. Idempotente (INSERT OR REPLACE).

    payload deve essere JSON-serializable. Se non lo è, log warning e skip.
    """
    _init_db()
    # Stampigliamo la versione del parser nel payload cachato (usata da get()
    # per invalidazione automatica quando la versione cambia)
    payload = {**payload, '_parser_version': PARSER_VERSION}
    try:
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as e:
        logger.warning('put cache fallito (payload non serializzabile): %s', e)
        return
    con = sqlite3.connect(_CACHE_DB_PATH)
    try:
        con.execute("""
            INSERT OR REPLACE INTO dxf_parse_cache
                (file_hash, filename, payload_json, svg_string,
                 created_at, last_hit_at, hit_count)
            VALUES (?, ?, ?, ?, datetime('now'), datetime('now'),
                    COALESCE((SELECT hit_count FROM dxf_parse_cache WHERE file_hash = ?), 0) + 1)
        """, (file_hash, filename, payload_json, svg_string, file_hash))
        con.commit()
    finally:
        con.close()


def stats() -> dict:
    """Statistiche cache per debug/monitoring."""
    _init_db()
    con = sqlite3.connect(_CACHE_DB_PATH)
    try:
        cur = con.execute("""
            SELECT COUNT(*) as n_entries,
                   SUM(hit_count) as tot_hits,
                   MAX(hit_count) as max_hits_single,
                   SUM(LENGTH(svg_string) + LENGTH(payload_json)) as tot_bytes
            FROM dxf_parse_cache
        """)
        r = cur.fetchone()
        return {
            'n_entries': r[0] or 0,
            'tot_hits': r[1] or 0,
            'max_hits_single': r[2] or 0,
            'tot_bytes': r[3] or 0,
            'db_path': _CACHE_DB_PATH,
        }
    finally:
        con.close()


def clear() -> int:
    """Svuota la cache. Ritorna numero entry rimosse (per admin panel)."""
    _init_db()
    con = sqlite3.connect(_CACHE_DB_PATH)
    try:
        cur = con.execute("SELECT COUNT(*) FROM dxf_parse_cache")
        n = cur.fetchone()[0]
        con.execute("DELETE FROM dxf_parse_cache")
        con.commit()
        return n
    finally:
        con.close()
