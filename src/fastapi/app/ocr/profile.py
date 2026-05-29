"""§04p PDF profiler — classify documents into one of 5 parser-strategy bins.

**Master-plan §9.4 reference.** Classifies each PDF (and within mixed
PDFs, each page) as one of: ``native``, ``scanned``, ``mixed``,
``map_heavy``, ``table_heavy``. The result drives parser dispatch
in the Hatchet ingest_pdf step.

**Status:** Step 3 implementation (doc-phase 51).

**Heuristic thresholds** (initial; tuning happens in Step 9 against
the 50-PDF acceptance corpus). All thresholds are exposed as module
constants for easy adjustment.

Output schema (locked here):
    {
        "document_profile": PdfProfile,
        "per_page_profiles": list[PdfProfile],
        "heuristic_scores": list[dict],   # one per page, with thresholds applied
    }
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

PdfProfile = Literal["native", "scanned", "mixed", "map_heavy", "table_heavy"]


# ---------------------------------------------------------------------------
# Heuristic thresholds — tuned against the smoke-bench PLS-2024 fixture
# (modern NI 43-101, clean text layer). Step 9 will retune against the
# 50-PDF acceptance corpus.
# ---------------------------------------------------------------------------
# text_density = chars_extracted / page_area
# A clean native NI 43-101 page is ~0.005-0.020 chars/sq-pt.
# A scanned page with no text layer is ~0.0
NATIVE_TEXT_DENSITY_MIN = 0.005
SCANNED_TEXT_DENSITY_MAX = 0.0005

# Tables per page threshold for table_heavy classification
TABLE_HEAVY_TABLES_PER_PAGE_MIN = 3

# Document-level: if ≥ this fraction of pages are table_heavy → document is table_heavy
DOC_TABLE_HEAVY_PAGE_FRACTION = 0.5

# Document-level: if ≥ this fraction of pages are scanned → document is scanned
DOC_SCANNED_PAGE_FRACTION = 0.8


async def profile(pdf_path: Path) -> dict[str, Any]:
    """Classify a preflighted PDF into a parser-strategy bin.

    Args:
        pdf_path: Local filesystem path to a PDF that has passed preflight.

    Returns:
        Profile result dict (see module docstring for schema).
    """
    return await asyncio.to_thread(_profile_sync, pdf_path)


def _profile_sync(pdf_path: Path) -> dict[str, Any]:
    """Synchronous implementation; called via asyncio.to_thread."""
    import pdfplumber

    per_page_profiles: list[str] = []
    heuristic_scores: list[dict[str, Any]] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            chars = len(text)
            page_area = float((page.width or 1) * (page.height or 1))
            text_density = chars / page_area if page_area > 0 else 0.0

            # pdfplumber.Page.find_tables() returns Table objects.
            try:
                tables = page.find_tables() or []
            except Exception:
                # Some PDFs trip pdfplumber's table-finding internals; treat as zero.
                tables = []
            table_count = len(tables)

            page_profile = _classify_page(text_density, table_count)
            per_page_profiles.append(page_profile)
            heuristic_scores.append({
                "page": page.page_number - 1,  # 0-indexed
                "text_density": round(text_density, 6),
                "char_count": chars,
                "page_area": round(page_area, 2),
                "table_count": table_count,
                "page_profile": page_profile,
            })

    document_profile = _classify_document(per_page_profiles)

    return {
        "document_profile": document_profile,
        "per_page_profiles": per_page_profiles,
        "heuristic_scores": heuristic_scores,
    }


def _classify_page(text_density: float, table_count: int) -> str:
    """Per-page profile classifier."""
    if text_density <= SCANNED_TEXT_DENSITY_MAX:
        return "scanned"
    if table_count >= TABLE_HEAVY_TABLES_PER_PAGE_MIN:
        return "table_heavy"
    if text_density >= NATIVE_TEXT_DENSITY_MIN:
        return "native"
    return "mixed"


def _classify_document(per_page_profiles: list[str]) -> str:
    """Document-level classifier from per-page profile list."""
    total = max(len(per_page_profiles), 1)
    counts = {
        p: per_page_profiles.count(p)
        for p in ("native", "scanned", "mixed", "table_heavy", "map_heavy")
    }

    scanned_fraction = counts["scanned"] / total
    table_fraction = counts["table_heavy"] / total
    native_fraction = counts["native"] / total

    if scanned_fraction >= DOC_SCANNED_PAGE_FRACTION:
        return "scanned"
    if table_fraction >= DOC_TABLE_HEAVY_PAGE_FRACTION:
        return "table_heavy"
    if scanned_fraction > 0 and native_fraction > 0:
        # Has both → mixed
        return "mixed"
    if native_fraction >= 0.5:
        return "native"
    if scanned_fraction > 0:
        return "scanned"
    return "mixed"
