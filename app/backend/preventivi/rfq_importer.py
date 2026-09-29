"""RFQ Importer (senza AI): parsing automatico di un pacchetto ZIP con
PDF ordine cliente + cartella DXF, per pre-compilare un preventivo BOZZA.

Workflow:
1. Lettura dell'ordine SENZA AI: formato DECA (ordine_testo) o codici dei
   DXF cercati nel PDF (ordine_codici). Gemini tolto il 2026-09-29.
2. `match_dxf_to_articoli(articoli, dxf_filenames)` → fuzzy match tra codice
   del PDF e nome file DXF.
3. `build_preventivo_draft(pdf_data, matches)` → dict pronto per PreventivoManager.create()
   + lista warnings (DXF senza articolo, articolo senza DXF, campi mancanti).

Nessun servizio esterno: i PDF dei clienti restano sul server.
"""
from __future__ import annotations

import logging
import os
import re
import time
import zipfile
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from io import BytesIO
from typing import Any

logger = logging.getLogger(__name__)


# ─── Config ────────────────────────────────────────────────────────────────
FUZZY_MATCH_THRESHOLD = 0.55  # ratio SequenceMatcher sotto cui NON matcha


# ─── Data classes ─────────────────────────────────────────────────────────
@dataclass
class ArticoloRFQ:
    """Riga articolo estratta dal PDF ordine."""
    codice: str
    quantita: int = 1
    materiale: str | None = None
    spessore_mm: float | None = None
    descrizione: str = ''
    matched_dxf: str | None = None  # filename DXF matchato (basename)
    _matched_score: float = 0.0     # score fuzzy match (0-1)
    codice_assieme: str | None = None  # se il DXF è dentro una sottocartella-assieme


@dataclass
class RFQParseResult:
    """Risultato del parsing dell'intero pacchetto."""
    success: bool
    cliente: str = ''
    numero_ordine_cliente: str = ''
    data_consegna: str | None = None  # ISO YYYY-MM-DD
    note: str = ''
    articoli: list[ArticoloRFQ] = field(default_factory=list)
    dxf_no_match: list[str] = field(default_factory=list)  # DXF nella cartella senza articolo PDF
    assiemi: list[str] = field(default_factory=list)       # codici assieme rilevati dalle cartelle
    dxf_map: dict = field(default_factory=dict)            # {basename: bytes} per scrittura su disco
    step_map: dict = field(default_factory=dict)           # {basename: bytes} STEP assiemi 3D
    pdf_bytes: bytes | None = None                         # PDF ordine originale (disegni/lavorazioni per Mirko)
    pdf_filename: str | None = None                        # nome del PDF ordine
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


class RFQParseError(Exception):
    """Errore con messaggio user-friendly per il frontend.
    Sostituisce il catch-all che nascondeva il vero motivo (chiave mancante,
    rate limit, PDF illeggibile, JSON invalido, ecc.)."""
    pass


# ─── Lettura dell'ordine SENZA AI ─────────────────────────────────────────
# Gemini tolto il 2026-09-29: i PDF d'ordine dei clienti non escono piu'
# dall'azienda. Formato DECA noto → ordine_testo (esatto); altrimenti si
# cercano nel PDF i codici dei DXF del pacchetto (ordine_codici).

def ordine_da_codici(pdf_bytes: bytes, dxf_names: list) -> dict | None:
    """Righe d'ordine per i DXF i cui codici compaiono nel PDF, nell'ordine
    del PDF. Stesso schema di ordine_testo.leggi_ordine_fornitore."""
    from .ordine_codici import leggi_per_codici, intestazione, chiave
    per_chiave = {}
    for n in dxf_names:
        per_chiave.setdefault(chiave(n), os.path.splitext(os.path.basename(n))[0])
    trovati = leggi_per_codici(pdf_bytes, list(per_chiave))
    if not trovati:
        return None
    articoli, senza_qta = [], []
    for k, t in sorted(trovati.items(), key=lambda kv: kv[1]['pos']):
        if not t['qta']:
            senza_qta.append(per_chiave[k])
        articoli.append({'codice': per_chiave[k], 'quantita': max(1, int(round(t['qta'] or 1))),
                         'descrizione': ''})
    testa = intestazione(pdf_bytes)
    note = []
    if testa.get('numero_ordine'):
        note.append(f"Ordine {testa['numero_ordine']} del {testa.get('data_ordine', '')}".strip())
    if senza_qta:
        note.append("Quantita' non trovata nell'ordine (messa 1, da controllare): " + ', '.join(senza_qta))
    return {'cliente': None, 'numero_ordine_cliente': testa.get('numero_ordine'), 'data_consegna': None,
            'note': ' · '.join(note), 'articoli': articoli, '_fonte': 'codici dei disegni cercati nel PDF'}


# ─── Fuzzy matching PDF articoli ↔ DXF files ──────────────────────────────

def _normalize_code(s: str) -> str:
    """Normalizza codice per matching: uppercase + solo alfanumerici."""
    return re.sub(r'[^A-Z0-9]', '', (s or '').upper())


def _fuzzy_score(a: str, b: str) -> float:
    """Ratio Levenshtein-like tra due codici normalizzati (0-1)."""
    na, nb = _normalize_code(a), _normalize_code(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    # Se uno è prefisso dell'altro (es. '20PA00693' vs '20PA00693-00') → alto
    if na.startswith(nb) or nb.startswith(na):
        min_len = min(len(na), len(nb))
        max_len = max(len(na), len(nb))
        return 0.85 * (min_len / max_len) + 0.15
    return SequenceMatcher(None, na, nb).ratio()


def match_dxf_to_articoli(articoli: list[dict], dxf_filenames: list[str]) -> tuple[list[ArticoloRFQ], list[str]]:
    """Fuzzy match: per ogni articolo PDF cerca il DXF più vicino per codice.

    Args:
        articoli: dict list lette dal PDF d'ordine (chiavi: codice, quantita, ...)
        dxf_filenames: lista basename DXF (es. ['20PA00693-00.dxf', 'ABC.dxf'])

    Returns:
        (articoli_matched, dxf_no_match)
        - articoli_matched: lista ArticoloRFQ con .matched_dxf popolato dove possibile
        - dxf_no_match: DXF nella cartella che non hanno articolo corrispondente nel PDF
    """
    # Prepara stem (senza estensione) per confronto
    dxf_stems = {fn: os.path.splitext(fn)[0] for fn in dxf_filenames}
    used_dxf: set[str] = set()

    matched: list[ArticoloRFQ] = []
    for a in (articoli or []):
        codice = (a.get('codice') or '').strip()
        if not codice:
            continue
        art = ArticoloRFQ(
            codice=codice,
            quantita=int(a.get('quantita') or 1),
            materiale=(a.get('materiale') or None),
            spessore_mm=(float(a['spessore_mm']) if a.get('spessore_mm') is not None else None),
            descrizione=(a.get('descrizione') or ''),
        )
        # Trova best match tra DXF non ancora usati
        best_fn, best_score = None, 0.0
        for fn, stem in dxf_stems.items():
            if fn in used_dxf:
                continue
            score = _fuzzy_score(codice, stem)
            if score > best_score:
                best_score = score
                best_fn = fn
        if best_fn and best_score >= FUZZY_MATCH_THRESHOLD:
            art.matched_dxf = best_fn
            art._matched_score = best_score
            used_dxf.add(best_fn)
        matched.append(art)

    # DXF senza match
    dxf_no_match = [fn for fn in dxf_filenames if fn not in used_dxf]
    return matched, dxf_no_match


# ─── ZIP extraction ────────────────────────────────────────────────────────

def _dirparts(n: str) -> list[str]:
    """Componenti di cartella di un percorso (senza il nome file)."""
    return n.replace('\\', '/').split('/')[:-1]


def _codice_senza_rev(nome: str) -> str:
    """Codice di un disegno o di una cartella, senza descrizione ne' revisione:
    "07SA00182 LEVA" e "07SA00182-00" → "07SA00182"."""
    parti = (nome or '').strip().split()
    return re.sub(r'-\d{1,3}$', '', parti[0].upper()) if parti else ''


def _e_disegno_cartella(stem: str, folder: str) -> bool:
    """Il DXF e' il disegno della cartella che lo contiene (l'assieme intero)?
    Nome identico, oppure stesso codice a meno di descrizione e revisione."""
    return (stem.upper() == folder.upper()
            or (_codice_senza_rev(stem) != '' and _codice_senza_rev(stem) == _codice_senza_rev(folder)))


def _radice_comune_dxf(rel_paths: list[str]) -> tuple[list[str], int]:
    dxf_paths = [p for p in rel_paths
                 if os.path.splitext(p)[1].lower() in ('.dxf', '.dwg')]
    dxf_dirs = [_dirparts(p) for p in dxf_paths]
    common: list[str] = []
    if dxf_dirs:
        for i in range(min(len(d) for d in dxf_dirs)):
            col = {d[i] for d in dxf_dirs}
            if len(col) == 1:
                common.append(next(iter(col)))
            else:
                break
    return dxf_paths, len(common)


def disegni_assieme_da_paths(rel_paths: list[str]) -> set[str]:
    """Basename dei DXF che sono il disegno d'insieme di un assieme o di un
    sotto-assieme: stanno nella cartella che porta il loro codice (a qualsiasi
    livello, es. CASSONE/07SA00180 TELAIO/07SA00180-01.dxf). Non si tagliano:
    l'assieme costa come somma dei componenti + montaggio."""
    dxf_paths, clen = _radice_comune_dxf(rel_paths)
    out: set[str] = set()
    for p in dxf_paths:
        dirs = _dirparts(p)[clen:]
        if dirs and _e_disegno_cartella(os.path.splitext(os.path.basename(p))[0], dirs[-1]):
            out.add(os.path.basename(p))
    return out


def assiemi_from_paths(rel_paths: list[str]) -> dict[str, str | None]:
    """Dato un elenco di percorsi relativi (con eventuali sottocartelle),
    ritorna {basename_dxf: codice_assieme | None}.

    Il primo livello di sottocartella — dopo aver tolto la radice comune,
    calcolata SOLO sui DXF — identifica un ASSIEME. Il DXF il cui nome coincide
    con la cartella (master, es. 13SA0070-00/13SA0070-00.DXF) → None (è il
    disegno dell'assieme, non un componente). Usato sia dallo ZIP che dal
    caricamento di una cartella già estratta (webkitRelativePath).
    """
    dxf_paths, clen = _radice_comune_dxf(rel_paths)
    out: dict[str, str | None] = {}
    for p in dxf_paths:
        base = os.path.basename(p)
        rel_dirs = _dirparts(p)[clen:]
        folder = rel_dirs[0] if rel_dirs else None
        stem = os.path.splitext(base)[0]
        is_master = folder is not None and _e_disegno_cartella(stem, folder)
        out[base] = None if is_master else folder
    return out


def extract_zip_package(zip_bytes: bytes) -> tuple[bytes | None, str | None, dict[str, bytes]]:
    """Estrae un pacchetto ZIP con PDF ordine + DXF.

    Args:
        zip_bytes: contenuto ZIP

    Returns:
        (pdf_bytes, pdf_filename, dxf_files_map)
        - pdf_bytes: primo PDF trovato (None se assente)
        - pdf_filename: nome file PDF
        - dxf_files_map: {basename: bytes} per ogni .dxf/.dwg nel ZIP

    RICONOSCIMENTO ASSIEMI: la struttura a cartelle è significativa. Dopo aver
    tolto la cartella-radice comune (es. "C26-156/"), il PRIMO livello di
    sottocartella identifica un ASSIEME. I DXF dentro quella sottocartella sono
    i componenti dell'assieme. Il DXF con lo stesso nome della cartella
    (es. 13SA0070-00/13SA0070-00.DXF) è il disegno dell'assieme intero (master),
    non un componente.

    Returns:
        (pdf_bytes, pdf_filename, dxf_map, assieme_of)
        - dxf_map: {basename: bytes}
        - assieme_of: {basename: codice_assieme | None}
          codice_assieme = nome sottocartella per i COMPONENTI; None per i DXF
          alla radice o per il master dell'assieme.
    """
    pdf_bytes = None
    pdf_filename = None
    dxf_map: dict[str, bytes] = {}
    assieme_of: dict[str, str | None] = {}
    step_map: dict[str, bytes] = {}

    try:
        with zipfile.ZipFile(BytesIO(zip_bytes), 'r') as zf:
            # Pass 1: raccogli le entry valide (file, non di sistema)
            valid = []
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = info.filename
                base = os.path.basename(name)
                if not base or base.startswith('.') or base.lower() in ('thumbs.db',):
                    continue
                if '__MACOSX' in name:
                    continue
                valid.append((info, name, base))

            dxf_entries = [(i, n, b) for (i, n, b) in valid
                           if os.path.splitext(b)[1].lower() in ('.dxf', '.dwg')]
            pdf_entries = [(i, n, b) for (i, n, b) in valid
                           if os.path.splitext(b)[1].lower() == '.pdf']
            step_entries = [(i, n, b) for (i, n, b) in valid
                            if os.path.splitext(b)[1].lower() in ('.step', '.stp')]
            dxf_stems = {os.path.splitext(b)[0].upper() for _, _, b in dxf_entries}

            # Mappa assiemi dai percorsi DXF (radice comune calcolata sui DXF)
            assieme_by_base = assiemi_from_paths([n for _, n, _ in dxf_entries])

            # Selezione PDF ORDINE: preferisci un PDF il cui nome NON corrisponde a
            # un DXF (i PDF-disegno dei componenti si chiamano come il loro DXF).
            # A parità, il più superficiale (meno cartelle). Fallback: il primo.
            def _pdf_score(entry):
                _, n, b = entry
                stem = os.path.splitext(b)[0].upper()
                is_order = stem not in dxf_stems      # non è un disegno di componente
                depth = len(_dirparts(n))
                return (0 if is_order else 1, depth)   # ordina: ordine-first, poi superficiale
            if pdf_entries:
                best_pdf = min(pdf_entries, key=_pdf_score)
                pdf_bytes = zf.read(best_pdf[0])
                pdf_filename = best_pdf[2]

            # Leggi i DXF; l'assieme viene dalla mappa calcolata dai percorsi
            for info, name, base in dxf_entries:
                dxf_map[base] = zf.read(info)
                assieme_of[base] = assieme_by_base.get(base)

            # Leggi gli STEP (assiemi 3D): li salviamo così il visore 3D li
            # aggancia per nome all'assieme (montaggio esatto, wow reale).
            for info, name, base in step_entries:
                step_map[base] = zf.read(info)
    except zipfile.BadZipFile as e:
        raise ValueError(f'ZIP non valido: {e}')
    except Exception as e:
        raise ValueError(f'Errore estrazione ZIP: {e}')

    return pdf_bytes, pdf_filename, dxf_map, assieme_of, step_map


# ─── Pipeline completo ────────────────────────────────────────────────────

def process_rfq_package(zip_bytes: bytes) -> RFQParseResult:
    """Pipeline end-to-end: ZIP → RFQParseResult.

    NON crea il preventivo (compito del caller). Ritorna solo i dati strutturati
    + warnings, così il caller può decidere se scrivere in DB, chiedere conferma
    all'utente, ecc.
    """
    result = RFQParseResult(success=False)

    # 1. Estrai ZIP (con riconoscimento assiemi dalle sottocartelle)
    try:
        pdf_bytes, pdf_filename, dxf_map, assieme_of, step_map = extract_zip_package(zip_bytes)
    except ValueError as e:
        result.error = str(e)
        return result
    result.dxf_map = dxf_map
    result.step_map = step_map
    # Conserva il PDF ordine: è il documento con i disegni/lavorazioni che Mirko
    # deve vedere in produzione. Va salvato e allegato all'ordine all'accettazione.
    result.pdf_bytes = pdf_bytes
    result.pdf_filename = pdf_filename

    if not pdf_bytes:
        result.error = 'Nessun PDF ordine trovato nel ZIP. Il pacchetto deve contenere almeno un file .pdf'
        return result

    if not dxf_map:
        result.warnings.append('Nessun file DXF trovato nel ZIP: gli articoli verranno creati senza disegno.')

    # 2. Lettura dell'ordine: prima dal TESTO del PDF se il formato e' noto
    #    ("Ordine Fornitore" DECA: esatto), altrimenti i codici dei DXF cercati
    #    nel PDF. (Fino al 2026-09 il ripiego era Gemini, tolto per riservatezza.)
    from .ordine_testo import leggi_ordine_fornitore
    parsed = leggi_ordine_fornitore(pdf_bytes)
    if parsed:
        result.warnings.append(
            f"Ordine letto direttamente dal PDF ({pdf_filename}): "
            f"{len(parsed.get('articoli') or [])} righe, senza AI")
    else:
        try:
            parsed = ordine_da_codici(pdf_bytes, list(dxf_map.keys()))
        except Exception as e:
            logger.warning('ordine_da_codici: %s', e)
            parsed = None
        if parsed:
            result.warnings.append(
                f"Ordine letto cercando i codici dei disegni nel PDF ({pdf_filename}): "
                f"{len(parsed['articoli'])} righe, senza AI")
    if not parsed:
        result.error = (f"Nel PDF d'ordine \"{pdf_filename}\" non ho trovato i codici dei disegni. "
                        "Importa i disegni normalmente: le quantita' le scrivi a mano.")
        return result

    # 3. Popola header
    result.cliente = (parsed.get('cliente') or '').strip() or 'Cliente da specificare'
    result.numero_ordine_cliente = (parsed.get('numero_ordine_cliente') or '').strip()
    result.data_consegna = parsed.get('data_consegna')
    result.note = (parsed.get('note') or '').strip()

    articoli_raw = parsed.get('articoli') or []
    if not articoli_raw:
        result.warnings.append('Nessun articolo estratto dal PDF. Verifica che la tabella sia leggibile.')

    # 4. Fuzzy match articoli PDF ↔ DXF cartella
    articoli, dxf_no_match = match_dxf_to_articoli(articoli_raw, list(dxf_map.keys()))

    # 4b. RICONOSCIMENTO ASSIEMI dalle sottocartelle.
    #  - Assegna codice_assieme agli articoli PDF il cui DXF sta in una sottocartella.
    #  - I DXF di sottocartella NON matchati dal PDF (componenti dell'assieme, non
    #    venduti come voce singola) diventano articoli-componente sotto l'assieme.
    for a in articoli:
        if a.matched_dxf:
            a.codice_assieme = assieme_of.get(a.matched_dxf)

    dxf_no_match_set = set(dxf_no_match)
    componenti_aggiunti = []
    for base, folder in assieme_of.items():
        if folder and base in dxf_no_match_set:
            # componente di assieme senza riga PDF → crea articolo figlio
            stem = os.path.splitext(base)[0]
            componenti_aggiunti.append(ArticoloRFQ(
                codice=stem, quantita=1, matched_dxf=base, codice_assieme=folder,
                descrizione='componente assieme (da cartella)',
            ))
    articoli.extend(componenti_aggiunti)
    # i componenti aggiunti non sono più "orfani"
    dxf_no_match = [d for d in dxf_no_match if d not in {c.matched_dxf for c in componenti_aggiunti}]

    # assiemi distinti rilevati
    result.assiemi = sorted({v for v in assieme_of.values() if v})

    # Master dell'assieme: un articolo PDF il cui codice coincide col nome
    # dell'assieme (es. "13SA0070-00") È l'assieme stesso, non un pezzo standalone.
    # Raggruppalo sotto il suo assieme per non contarlo due volte nel totale.
    _assiemi_set = set(result.assiemi)
    for a in articoli:
        if not a.codice_assieme and a.codice in _assiemi_set:
            a.codice_assieme = a.codice

    result.articoli = articoli
    result.dxf_no_match = dxf_no_match
    if result.assiemi:
        result.warnings.append(
            f'{len(result.assiemi)} assiemi riconosciuti dalle cartelle: {", ".join(result.assiemi[:3])}'
            + ('…' if len(result.assiemi) > 3 else '')
            + (f' (+{len(componenti_aggiunti)} componenti aggiunti)' if componenti_aggiunti else '')
        )

    # 5. Warnings su completezza
    n_no_dxf = sum(1 for a in articoli if not a.matched_dxf)
    if n_no_dxf > 0:
        result.warnings.append(f'{n_no_dxf} articoli del PDF senza DXF corrispondente nella cartella')
    if dxf_no_match:
        result.warnings.append(f'{len(dxf_no_match)} file DXF nella cartella senza riferimento nel PDF: {", ".join(dxf_no_match[:3])}{"..." if len(dxf_no_match) > 3 else ""}')
    n_no_mat = sum(1 for a in articoli if not a.materiale)
    if n_no_mat > 0:
        result.warnings.append(f'{n_no_mat} articoli senza materiale specificato')
    n_no_sp = sum(1 for a in articoli if not a.spessore_mm)
    if n_no_sp > 0:
        result.warnings.append(f'{n_no_sp} articoli senza spessore specificato')

    result.success = True
    # Salva dxf_map dentro result per il caller (che deve scrivere i file su disco)
    # Uso un attributo non-dataclass:
    result.dxf_map = dxf_map  # type: ignore
    return result
