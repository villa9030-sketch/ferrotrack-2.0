"""File-based cache for STEP/DXF analysis results.

Avoids re-analyzing unchanged files by keying on SHA256(file_content).
Cache entries are stored as JSON files in a local directory.
"""

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CACHE_DIR = ".preventivatore_cache"
CACHE_VERSION = 16  # v16: aggiunti angolo_taglio_1/2 numerico in gradi nel dict tubo (RHS+CHS) per le schede officina. 0° = dritto, N° = obliquo di N°.
MAX_AGE_DAYS = 30  # Cache entries older than this are cleaned up


class AnalysisCache:
    """File-based cache for STEP/DXF analysis results.

    Cache key = SHA256(file_content) so re-analysis is skipped if file hasn't changed.
    Cache stored as JSON files in .preventivatore_cache/ directory.
    """

    def __init__(self, cache_dir: str | None = None):
        """Initialize cache. Creates cache directory if needed."""
        if cache_dir is None:
            self._cache_dir = Path(CACHE_DIR)
        else:
            self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _file_hash(self, file_path: str) -> str:
        """Compute SHA256 hash of file content. Uses chunked reading for large files."""
        h = hashlib.sha256()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def _cache_path(self, file_hash: str, analysis_type: str) -> Path:
        """Get cache file path for a given hash and analysis type.

        Structure: cache_dir/ab/abcdef1234..._{type}.json
        First 2 chars of hash as subdirectory to avoid too many files in one dir.
        """
        subdir = self._cache_dir / file_hash[:2]
        return subdir / f"{file_hash}_{analysis_type}.json"

    def get(self, file_path: str, analysis_type: str) -> Any | None:
        """Get cached result for a file. Returns None if not cached or expired.

        analysis_type: one of 'step_geometry', 'step_tubolari', 'step_piastre',
                       'step_assieme', 'step_nauo', 'dxf_dettagli'
        """
        try:
            file_hash = self._file_hash(file_path)
        except (IOError, OSError) as e:
            logger.warning("Cannot hash file %s: %s", file_path, e)
            return None

        cache_file = self._cache_path(file_hash, analysis_type)
        if not cache_file.exists():
            return None

        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                envelope = json.load(f)
        except (json.JSONDecodeError, IOError, OSError) as e:
            logger.warning("Corrupt cache entry %s: %s", cache_file, e)
            # Remove corrupt file
            try:
                cache_file.unlink()
            except OSError:
                pass
            return None

        # Validate envelope structure
        if not isinstance(envelope, dict):
            logger.warning("Invalid cache envelope in %s", cache_file)
            try:
                cache_file.unlink()
            except OSError:
                pass
            return None

        # Check version
        if envelope.get("version") != CACHE_VERSION:
            logger.info("Cache version mismatch in %s, ignoring", cache_file)
            try:
                cache_file.unlink()
            except OSError:
                pass
            return None

        # Check age
        timestamp = envelope.get("timestamp", 0)
        age_days = (time.time() - timestamp) / 86400
        if age_days > MAX_AGE_DAYS:
            logger.info("Cache entry expired (%d days): %s", int(age_days), cache_file)
            try:
                cache_file.unlink()
            except OSError:
                pass
            return None

        return envelope.get("result")

    def put(self, file_path: str, analysis_type: str, result: Any) -> None:
        """Store analysis result in cache.

        The result must be JSON-serializable. Tuples in results are stored as lists
        and will be returned as lists (caller should handle this).
        """
        try:
            file_hash = self._file_hash(file_path)
        except (IOError, OSError) as e:
            logger.warning("Cannot hash file %s: %s", file_path, e)
            return

        cache_file = self._cache_path(file_hash, analysis_type)
        cache_file.parent.mkdir(parents=True, exist_ok=True)

        envelope = {
            "version": CACHE_VERSION,
            "timestamp": time.time(),
            "source_file": os.path.basename(file_path),
            "analysis_type": analysis_type,
            "result": result,
        }

        try:
            # Write to temp file then rename for atomicity
            tmp_path = cache_file.with_suffix(".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(envelope, f, ensure_ascii=False)
            # On Windows, replace requires removing target first if it exists
            if cache_file.exists():
                cache_file.unlink()
            tmp_path.rename(cache_file)
        except (IOError, OSError, TypeError, ValueError) as e:
            logger.warning("Cannot write cache for %s: %s", file_path, e)
            # Clean up temp file if it exists
            try:
                tmp_path.unlink()
            except OSError:
                pass

    def invalidate(self, file_path: str, analysis_type: str | None = None) -> int:
        """Invalidate cache for a file. If analysis_type is None, invalidate all types.
        Returns number of entries invalidated."""
        try:
            file_hash = self._file_hash(file_path)
        except (IOError, OSError):
            return 0

        count = 0
        subdir = self._cache_dir / file_hash[:2]
        if not subdir.exists():
            return 0

        if analysis_type is not None:
            cache_file = self._cache_path(file_hash, analysis_type)
            if cache_file.exists():
                try:
                    cache_file.unlink()
                    count = 1
                except OSError:
                    pass
        else:
            prefix = f"{file_hash}_"
            for f in subdir.iterdir():
                if f.name.startswith(prefix) and f.suffix == ".json":
                    try:
                        f.unlink()
                        count += 1
                    except OSError:
                        pass

        return count

    def clear(self) -> int:
        """Clear entire cache. Returns number of entries removed."""
        count = 0
        if not self._cache_dir.exists():
            return 0

        for subdir in list(self._cache_dir.iterdir()):
            if subdir.is_dir():
                for f in list(subdir.iterdir()):
                    try:
                        f.unlink()
                        count += 1
                    except OSError:
                        pass
                try:
                    subdir.rmdir()
                except OSError:
                    pass

        return count

    def cleanup(self, max_age_days: int | None = None) -> int:
        """Remove cache entries older than max_age_days. Returns count removed."""
        if max_age_days is None:
            max_age_days = MAX_AGE_DAYS

        cutoff = time.time() - (max_age_days * 86400)
        count = 0

        if not self._cache_dir.exists():
            return 0

        for subdir in list(self._cache_dir.iterdir()):
            if not subdir.is_dir():
                continue
            for f in list(subdir.iterdir()):
                if f.suffix != ".json":
                    continue
                try:
                    with open(f, "r", encoding="utf-8") as fh:
                        envelope = json.load(fh)
                    if not isinstance(envelope, dict):
                        f.unlink()
                        count += 1
                        continue
                    timestamp = envelope.get("timestamp", 0)
                    if timestamp < cutoff:
                        f.unlink()
                        count += 1
                except (json.JSONDecodeError, IOError, OSError):
                    # Corrupt file, remove it
                    try:
                        f.unlink()
                        count += 1
                    except OSError:
                        pass

        return count

    def stats(self) -> dict:
        """Return cache statistics: n_entries, total_size_mb, oldest_entry, newest_entry."""
        n_entries = 0
        total_size = 0
        oldest = None
        newest = None

        if not self._cache_dir.exists():
            return {
                "n_entries": 0,
                "total_size_mb": 0.0,
                "oldest_entry": None,
                "newest_entry": None,
            }

        for subdir in self._cache_dir.iterdir():
            if not subdir.is_dir():
                continue
            for f in subdir.iterdir():
                if f.suffix != ".json":
                    continue
                n_entries += 1
                try:
                    total_size += f.stat().st_size
                except OSError:
                    continue
                try:
                    with open(f, "r", encoding="utf-8") as fh:
                        envelope = json.load(fh)
                    if isinstance(envelope, dict):
                        ts = envelope.get("timestamp")
                        if ts is not None:
                            if oldest is None or ts < oldest:
                                oldest = ts
                            if newest is None or ts > newest:
                                newest = ts
                except (json.JSONDecodeError, IOError, OSError):
                    pass

        return {
            "n_entries": n_entries,
            "total_size_mb": round(total_size / 1_000_000, 6),
            "oldest_entry": oldest,
            "newest_entry": newest,
        }


# ---------------------------------------------------------------------------
# Convenience functions that wrap services with caching
# ---------------------------------------------------------------------------


def cached_analizza_step_assieme(
    step_path: str, cache: AnalysisCache | None = None
) -> dict:
    """Wrapper around analizza_step_assieme with caching."""
    from .step_assieme import analizza_step_assieme

    if cache:
        cached = cache.get(step_path, "step_assieme")
        if cached is not None:
            logger.info("Cache HIT: step_assieme for %s", os.path.basename(step_path))
            return cached
    result = analizza_step_assieme(step_path)
    if cache and result.get("errore") is None:
        cache.put(step_path, "step_assieme", result)
    return result


def cached_analizza_step_tubolari(
    step_path: str, profili_db: dict, cache: AnalysisCache | None = None
) -> dict:
    """Wrapper around analizza_step_tubolari with caching."""
    from .step_tubolari import analizza_step_tubolari

    if cache:
        cached = cache.get(step_path, "step_tubolari")
        if cached is not None:
            logger.info(
                "Cache HIT: step_tubolari for %s", os.path.basename(step_path)
            )
            return cached
    result = analizza_step_tubolari(step_path, profili_db)
    if cache and result.get("errore") is None:
        cache.put(step_path, "step_tubolari", result)
    return result


def cached_analizza_step_piastre(
    step_path: str, densita: float = 7.85, cache: AnalysisCache | None = None
) -> dict:
    """Wrapper around analizza_step_piastre with caching."""
    from .step_piastre import analizza_step_piastre

    if cache:
        cached = cache.get(step_path, "step_piastre")
        if cached is not None:
            logger.info("Cache HIT: step_piastre for %s", os.path.basename(step_path))
            return cached
    result = analizza_step_piastre(step_path, densita)
    if cache and result.get("errore") is None:
        cache.put(step_path, "step_piastre", result)
    return result


def cached_conta_istanze_nauo(
    step_path: str, cache: AnalysisCache | None = None
) -> dict:
    """Wrapper around conta_istanze_nauo with caching."""
    from .step_assieme import conta_istanze_nauo

    if cache:
        cached = cache.get(step_path, "step_nauo")
        if cached is not None:
            logger.info("Cache HIT: step_nauo for %s", os.path.basename(step_path))
            # JSON salva le chiavi come stringhe: body_id torna intero
            return {int(k): v for k, v in cached.items()}
    result = conta_istanze_nauo(step_path)
    if cache:
        cache.put(step_path, "step_nauo", result)
    return result


def cached_scansiona_dxf(
    path: str, config: dict, cache: AnalysisCache | None = None
) -> tuple:
    """Wrapper around scansiona_dxf_dettagli with caching."""
    from .dxf_scanner import scansiona_dxf_dettagli

    if cache:
        cached = cache.get(path, "dxf_dettagli")
        if cached is not None:
            logger.info("Cache HIT: dxf_dettagli for %s", os.path.basename(path))
            return tuple(cached)  # JSON stores as list, convert back to tuple
    result = scansiona_dxf_dettagli(path, config)
    if cache:
        cache.put(path, "dxf_dettagli", list(result))  # tuple -> list for JSON
    return result


def cached_parse_step_geometry(
    step_path: str, cache: AnalysisCache | None = None
) -> dict:
    """Wrapper around parse_step_geometry with caching.

    Note: geometry data can be large, only cache if file is not huge.
    """
    from .step_parser import parse_step_geometry

    if cache:
        cached = cache.get(step_path, "step_geometry")
        if cached is not None:
            logger.info(
                "Cache HIT: step_geometry for %s", os.path.basename(step_path)
            )
            return cached
    result = parse_step_geometry(step_path)
    if cache and result.get("errore") is None:
        # Only cache if result is not too large (< 10MB when serialized)
        try:
            serialized = json.dumps(result)
            if len(serialized) < 10_000_000:
                cache.put(step_path, "step_geometry", result)
            else:
                logger.info(
                    "Skipping cache for large geometry: %s (%.1f MB)",
                    os.path.basename(step_path),
                    len(serialized) / 1_000_000,
                )
        except (TypeError, ValueError):
            pass  # Not serializable, skip cache
    return result
