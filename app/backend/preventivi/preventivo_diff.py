"""Comparison / diff module for preventivi.

Provides functions to compare two preventivi loaded via
``database.carica_preventivo`` and produce a structured diff dict,
as well as a human-readable text summary.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def confronta_preventivi(prev_a: dict, prev_b: dict) -> dict:
    """Compare two preventivi and return detailed differences.

    Args:
        prev_a: first preventivo dict (from database.carica_preventivo)
        prev_b: second preventivo dict (from database.carica_preventivo)

    Returns:
        dict with:
            id_a, id_b: int
            cliente_a, cliente_b: str
            data_a, data_b: str

            totale_lotto_a, totale_lotto_b: float
            delta_totale: float (b - a)
            delta_totale_pct: float (percentage change)

            totale_pezzo_a, totale_pezzo_b: float
            delta_pezzo, delta_pezzo_pct: float

            margine_a, margine_b: float
            delta_margine: float

            quantita_a, quantita_b: int

            costi_confronto: list of {
                categoria, valore_a, valore_b, delta, delta_pct
            }

            articoli_solo_a: list[str]   (codes only in A)
            articoli_solo_b: list[str]   (codes only in B)
            articoli_comuni: list of {
                codice, costo_a, costo_b, delta, delta_pct
            }

            n_articoli_a, n_articoli_b: int
            variazione: str  ("aumento", "diminuzione", "invariato")
    """
    # Basic info
    id_a = prev_a.get("id", 0)
    id_b = prev_b.get("id", 0)

    totale_a = float(prev_a.get("totale_lotto", 0))
    totale_b = float(prev_b.get("totale_lotto", 0))
    delta_totale = totale_b - totale_a
    delta_totale_pct = (delta_totale / totale_a * 100) if totale_a != 0 else 0

    pezzo_a = float(prev_a.get("totale_pezzo", 0))
    pezzo_b = float(prev_b.get("totale_pezzo", 0))
    delta_pezzo = pezzo_b - pezzo_a
    delta_pezzo_pct = (delta_pezzo / pezzo_a * 100) if pezzo_a != 0 else 0

    margine_a = float(prev_a.get("margine_pct", 0))
    margine_b = float(prev_b.get("margine_pct", 0))

    # Cost categories
    categorie = [
        ("Montaggio", "costo_montaggio"),
        ("Tubolari", "costo_tubolari"),
        ("Piastre", "costo_piastre"),
    ]
    costi_confronto: list[dict[str, Any]] = []
    for nome, chiave in categorie:
        va = float(prev_a.get(chiave, 0))
        vb = float(prev_b.get(chiave, 0))
        delta = vb - va
        pct = (delta / va * 100) if va != 0 else (100.0 if vb > 0 else 0.0)
        costi_confronto.append({
            "categoria": nome,
            "valore_a": round(va, 2),
            "valore_b": round(vb, 2),
            "delta": round(delta, 2),
            "delta_pct": round(pct, 1),
        })

    # Article comparison
    art_a = {a.get("codice", ""): a for a in prev_a.get("articoli", [])}
    art_b = {a.get("codice", ""): a for a in prev_b.get("articoli", [])}

    codici_a = set(art_a.keys())
    codici_b = set(art_b.keys())

    solo_a = sorted(codici_a - codici_b)
    solo_b = sorted(codici_b - codici_a)
    comuni = sorted(codici_a & codici_b)

    articoli_comuni: list[dict[str, Any]] = []
    for codice in comuni:
        ca = _costo_totale_articolo(art_a[codice])
        cb = _costo_totale_articolo(art_b[codice])
        delta = cb - ca
        pct = (delta / ca * 100) if ca != 0 else 0.0
        articoli_comuni.append({
            "codice": codice,
            "costo_a": round(ca, 2),
            "costo_b": round(cb, 2),
            "delta": round(delta, 2),
            "delta_pct": round(pct, 1),
        })

    # Sort by absolute delta descending (biggest changes first)
    articoli_comuni.sort(key=lambda x: abs(x["delta"]), reverse=True)

    # Summary
    if abs(delta_totale) < 0.01:
        variazione = "invariato"
    elif delta_totale > 0:
        variazione = "aumento"
    else:
        variazione = "diminuzione"

    logger.info(
        "Confronto preventivi #%d vs #%d: %s (%.1f%%)",
        id_a, id_b, variazione, delta_totale_pct,
    )

    return {
        "id_a": id_a,
        "id_b": id_b,
        "cliente_a": prev_a.get("cliente", ""),
        "cliente_b": prev_b.get("cliente", ""),
        "data_a": prev_a.get("data_creazione", ""),
        "data_b": prev_b.get("data_creazione", ""),
        "totale_lotto_a": round(totale_a, 2),
        "totale_lotto_b": round(totale_b, 2),
        "delta_totale": round(delta_totale, 2),
        "delta_totale_pct": round(delta_totale_pct, 1),
        "totale_pezzo_a": round(pezzo_a, 2),
        "totale_pezzo_b": round(pezzo_b, 2),
        "delta_pezzo": round(delta_pezzo, 2),
        "delta_pezzo_pct": round(delta_pezzo_pct, 1),
        "margine_a": margine_a,
        "margine_b": margine_b,
        "delta_margine": round(margine_b - margine_a, 1),
        "quantita_a": prev_a.get("quantita", 0),
        "quantita_b": prev_b.get("quantita", 0),
        "costi_confronto": costi_confronto,
        "articoli_solo_a": solo_a,
        "articoli_solo_b": solo_b,
        "articoli_comuni": articoli_comuni,
        "n_articoli_a": len(codici_a),
        "n_articoli_b": len(codici_b),
        "variazione": variazione,
    }


def _costo_totale_articolo(art: dict) -> float:
    """Calculate total cost of an article from its components."""
    return (
        float(art.get("costo_materiale", 0))
        + float(art.get("costo_piega", 0))
        + float(art.get("costo_saldatura", 0))
        + float(art.get("costo_filettatura", 0))
        + float(art.get("costo_svasatura", 0))
        + float(art.get("costo_foratura", 0) or 0)
        + float(art.get("costo_mat_apporto", 0))
        + float(art.get("costo_pulizia", 0))
    )


def formatta_confronto_testo(diff: dict) -> str:
    """Format a comparison dict as human-readable text."""
    lines: list[str] = []
    lines.append(f"CONFRONTO PREVENTIVI #{diff['id_a']} vs #{diff['id_b']}")
    lines.append("=" * 50)
    lines.append(f"Cliente A: {diff['cliente_a']} ({diff['data_a'][:10]})")
    lines.append(f"Cliente B: {diff['cliente_b']} ({diff['data_b'][:10]})")
    lines.append("")

    # Arrow indicator
    arrow = "+" if diff["delta_totale"] > 0 else ("" if diff["delta_totale"] < 0 else "=")
    lines.append(f"Totale A: EUR {diff['totale_lotto_a']:,.2f}")
    lines.append(f"Totale B: EUR {diff['totale_lotto_b']:,.2f}")
    lines.append(
        f"Delta:    {arrow}EUR {diff['delta_totale']:,.2f} "
        f"({arrow}{diff['delta_totale_pct']:.1f}%)"
    )
    lines.append("")

    if diff["articoli_solo_a"]:
        lines.append(f"Articoli solo in A: {', '.join(diff['articoli_solo_a'])}")
    if diff["articoli_solo_b"]:
        lines.append(f"Articoli solo in B: {', '.join(diff['articoli_solo_b'])}")

    if diff["articoli_comuni"]:
        lines.append("")
        lines.append("Variazioni prezzo articoli:")
        for art in diff["articoli_comuni"][:10]:
            if abs(art["delta"]) > 0.01:
                sign = "+" if art["delta"] > 0 else ""
                lines.append(
                    f"  {art['codice']}: {sign}{art['delta']:.2f} EUR "
                    f"({sign}{art['delta_pct']:.1f}%)"
                )

    return "\n".join(lines)
