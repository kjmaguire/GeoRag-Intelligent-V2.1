"""
PDF parser — pypdfium2-first dispatch with pdfplumber as the structural
fallback and Tesseract / Azure Document Intelligence OCR for scanned or
image-only pages. (The primary native-text path is historically called
"fitz"; it was PyMuPDF until that was removed for its AGPL license and is
now backed by pypdfium2 — see _parse_with_fitz.)

**Canonical path**: this module (`pdf_report.py`, §04p) is the live, primary
PDF parser for NI 43-101 technical reports — it is not a fallback for
anything. RAGFlow was replaced by this in-process stack per ADR-0002; there
is no other parser in front of it. Extraction order: pypdfium2 (fitz) native
text first, pdfplumber as the structural fallback when native text is
insufficient, and per-page OCR (Tesseract by default, or Azure Document
Intelligence when `OCR_ENGINE=azure_document_intelligence`) for scanned/image
pages. See `_attempt_ocr`, `_attempt_ocr_document_intelligence`, and
`document_intelligence_client` for the OCR dispatch.

NOTE ON THE ENV VALUE: the selector is the exact string
`azure_document_intelligence` — see `document_intelligence_client.is_engine_selected`.
These docstrings previously said `document_intelligence`, which is NOT
matched and silently leaves the engine on the Tesseract default.

---

NI 43-101 PDF Report Parser — Bronze → Silver ingestion for technical reports.

Accepts a path to a PDF file and extracts structured metadata and section text
from NI 43-101 technical reports. NI 43-101 mandates a specific table of
contents structure (up to 27 sections; 17 is the typical baseline) which this
parser exploits for high-confidence section boundary detection.

Primary extraction engine: pypdfium2 (PDFium) for native text + per-page OCR
routing to Tesseract (default) or Azure Document Intelligence (when
`OCR_ENGINE=azure_document_intelligence`) for image pages. Fallback engine:
pdfplumber, used when the primary can't extract sufficient structure.

Parse quality is reported as a float 0.0–1.0 representing the fraction of the
17 expected NI 43-101 sections identified. The caller (silver_reports asset)
records this in Dagster materialisation metadata.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import difflib
import hashlib
import json
import logging
import os
import re
import statistics
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

# langdetect is stochastic by default — seed for deterministic output across
# runs so that per-page language tags are reproducible in tests.
try:
    from langdetect import DetectorFactory as _DetectorFactory
    _DetectorFactory.seed = 0
except ImportError:
    pass  # langdetect is optional; absence handled at call site

# Phase 5 Step 4 (R-P3-7) — per-stage OTel spans. get_tracer falls back
# to a null tracer when the SDK isn't installed, so this import is
# zero-cost. The TracerProvider itself is installed at worker startup
# (Phase 6 Step 1) so the service.name resource attribute reflects the
# worker pool rather than the parser module.
from app.observability import get_tracer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# NI 43-101 structural constants
# ---------------------------------------------------------------------------

# Typical NI 43-101 report has 17 numbered sections (some have up to 27).
# Quality is expressed relative to this baseline so a score ≥ 1.0 is possible
# for unusually detailed reports — that is intentional and acceptable.
PARSER_VERSION = "2.0.0"

# Tracer shared by every stage of parse_pdf_report. get_tracer returns a
# no-op tracer if the OTel SDK isn't installed, so this module remains
# importable in minimal envs.
_tracer = get_tracer("georag.pdf_report", PARSER_VERSION)

NI43_BASELINE_SECTIONS = 17

# Regex to detect section headings of the form "1. Summary" or "14. MINERAL RESOURCE"
# Anchored at start of a line, section number 1–27 only.
SECTION_HEADING_RE = re.compile(
    r"^(\d{1,2})\.\s+([^\n]{2,120})$",
    re.MULTILINE,
)

# F15 (2026-08-11) — table-of-contents entry shape: a "heading" line ending
# in dot leaders + page number ("1. Summary ........ 3") or >=2 spaces
# followed by a bare page number ("1. Summary   3"). SECTION_HEADING_RE
# matches these too, which inflated parse_quality_pct (TOC hits counted as
# detected sections). Line-shape rejection only — deliberately no TOC-page
# detection (too fragile).
_TOC_LINE_TAIL_RE = re.compile(r"(?:\.{2,}\s*\d{1,4}|\s{2,}\d{1,4})\s*$")

# Subsection headings: "14.1 Resource Classification" or "14.1.2 Block Model"
SUBSECTION_HEADING_RE = re.compile(
    r"^(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)\s+([^\n]{2,120})$",
    re.MULTILINE,
)

# Minimum text length to consider a PDF as having extractable text.
# Below this, the PDF is likely scanned/image-only.
MIN_EXTRACTABLE_TEXT_CHARS = 200

# Per-page text threshold below which we attempt OCR on THAT page. Catches
# the common case of NI 43-101 reports where most pages are text but maps,
# figures, scanned drill log inserts, etc. arrive as page-sized images.
# Without per-page OCR these pages contribute zero text to the index.
PER_PAGE_MIN_CHARS = 80

# F16 (2026-08-11) — native text-layer quality screen. A page can clear the
# PER_PAGE_MIN_CHARS length gate and still be garbage embedded OCR or a
# header-only scanned page whose only text layer is repeated boilerplate.
# Thresholds are deliberately conservative (well above normal prose levels)
# because a false positive here sends the page to OCR — a cost, not a loss.
NATIVE_TEXT_MAX_GIBBERISH_RATIO = 0.4
NATIVE_TEXT_MAX_REPEATED_CHAR_RATIO = 0.3
NATIVE_TEXT_BOILERPLATE_SIMILARITY = 0.70
NATIVE_TEXT_BOILERPLATE_MAX_CHARS = 300

# Maximum file size for PDF processing — bumped from 100 MB to 2 GB to
# match the Octane + PHP upload caps (the four-layer stack already accepts
# 2 GB; this was the last cap silently dropping large NI 43-101 reports).
MAX_PDF_SIZE_BYTES = 2 * 1024 * 1024 * 1024

# ---------------------------------------------------------------------------
# Resource table extraction constants
# ---------------------------------------------------------------------------

# Page-level trigger phrases that mark a page as a resource-table candidate.
_RESOURCE_TABLE_TRIGGERS = [
    "mineral resource",
    "mineral reserve",
    "resource estimate",
    "reserve estimate",
    "contained metal",
    "measured + indicated",
    "measured and indicated",
    "indicated + inferred",
]

# Column-header tokens used to score whether a row is a header row.
_COLUMN_HEADER_TOKENS = {
    "tonnes", "tonnage", "grade", "g/t", "ppm", "%",
    "contained", "category", "au", "ag", "cu", "pb",
    "zn", "ni", "u3o8", "oz",
}

# ---------------------------------------------------------------------------
# Metadata extraction patterns
# ---------------------------------------------------------------------------

COMPANY_PATTERNS = [
    re.compile(r"Prepared\s+for\s*:\s*([^\n]{3,80})", re.IGNORECASE),
    re.compile(r"Prepared\s+for\s+([^\n]{3,80})", re.IGNORECASE),
    re.compile(r"\bfor\s+(Fission\s+Uranium[^\n,]*)", re.IGNORECASE),
    re.compile(r"\bfor\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+(?:Corp|Inc|Ltd|Limited|Minerals|Resources|Mining|Energy)[^\n,]*)", re.IGNORECASE),
]

FILING_DATE_PATTERNS = [
    re.compile(
        r"(?:Report|Effective|Filing|Dated?)\s+Date\s*:\s*([A-Z][a-z]+\.?\s+\d{1,2},?\s*\d{4})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:Report|Effective|Filing|Dated?)\s+Date\s*:\s*(\d{1,2}\s+[A-Z][a-z]+\.?\s+\d{4})",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:dated?|as of)\s+([A-Z][a-z]+\.?\s+\d{1,2},?\s*\d{4})",
        re.IGNORECASE,
    ),
]

# Month name → number mapping for manual date parsing
_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

COMMODITY_KEYWORDS = [
    "uranium", "gold", "copper", "lithium", "silver", "zinc", "lead",
    "nickel", "cobalt", "iron", "molybdenum", "tungsten", "vanadium",
    "rare earth", "platinum", "palladium",
]

QP_PATTERNS = [
    re.compile(r"Qualified\s+Persons?\s*:\s*([^\n]{3,200})", re.IGNORECASE),
    re.compile(r"Qualified\s+Persons?\s+include\s*([^\n]{3,200})", re.IGNORECASE),
]

PROJECT_NAME_PATTERNS = [
    re.compile(r"Technical\s+Report\s+on\s+the\s+([^,\n]{3,80})", re.IGNORECASE),
    re.compile(r"Technical\s+Report\s+for\s+the\s+([^,\n]{3,80})", re.IGNORECASE),
    re.compile(r"Technical\s+Report\s+on\s+([^,\n]{3,80})", re.IGNORECASE),
]

REGION_KEYWORDS = [
    "Athabasca Basin", "Athabasca", "Saskatchewan", "British Columbia",
    "Ontario", "Quebec", "Yukon", "Northwest Territories", "Nunavut",
    "Alberta", "Manitoba", "Nevada", "Chile", "Peru", "Mexico",
    "Australia", "Kazakhstan", "Mongolia", "Namibia", "Canada",
]


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class ReportSection:
    """A single numbered section extracted from a NI 43-101 report."""

    section_number: str | None   # "1", "2", ..., "17"
    section_title: str              # e.g. "Summary", "Introduction"
    text: str                       # Body text of the section
    page_first: int | None = None  # First 1-indexed page this section spans.
    page_last: int | None = None   # Last 1-indexed page this section spans.
    # Phase 3 (2026-05-22) — OCR confidence + method per chunk. NULL
    # means the chunk came from the PDF text layer (no OCR). 0.0–1.0
    # means an OCR engine produced the text. ocr_method records which
    # engine: fitz_native, pdfplumber_native, tesseract, document_intelligence.
    # When a chunk spans multiple pages with mixed methods, the minimum
    # confidence is recorded and the first-page method wins (kickoff
    # min-confidence-per-chunk semantics).
    ocr_confidence: float | None = None
    ocr_method: str | None = None


@dataclass
class ReportParseResult:
    """Complete result of parsing a NI 43-101 PDF technical report."""

    title: str | None
    authors: list[str]
    company: str | None
    filing_date: str | None      # ISO 8601 string: YYYY-MM-DD
    commodity: str | None
    project_name: str | None
    region: str | None
    sections: list[ReportSection]
    parse_quality_pct: float        # Fraction of expected sections found (0.0–1.0+)
    # Fraction of the document's pages that produced any text at all.
    #
    # This is the extraction question, and the one people believe
    # parse_quality_pct answers. It does not: parse_quality_pct is NI
    # 43-101 heading coverage, so a 1970s government geophysics survey
    # extracted flawlessly scores 0.0 for having no numbered sections,
    # and a report whose table of contents yielded 17 headings while 300
    # pages OCR'd to nothing scores 1.0. Both numbers travel together now
    # so the second cannot be read as the first.
    text_page_coverage_pct: float = 0.0
    parser_used: str = "unknown"
    skipped_elements: int = 0
    warnings: list = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    resource_tables: list[dict] = field(default_factory=list)
    page_languages: list[str] = field(default_factory=list)
    # Figure manifest. Each entry would be a dict {idx, page, bbox, caption,
    # pending_key, bucket, sha256}, consumed by the persist Hatchet task
    # (copies each PNG to its final figures/{report_id}/... key and removes
    # the pending object). Currently always empty — docling (the only
    # producer of this manifest) was removed 2026-07-29; figure extraction
    # now goes through app.agent.figure_extractor instead, which does not
    # populate this field. See figure_extractor.py for the current path.
    figure_manifest: list[dict] = field(default_factory=list)
    # True when native text extraction (fitz/pdfplumber) came up short and
    # the whole-document OCR fallback ran, OR any individual page needed
    # fitz's internal per-page tesseract recovery. Was previously read via
    # getattr(result, "is_scanned", False) in ingest_pdf.py against a
    # dataclass with no such field at all — silently always False, so
    # every silver.reports row claimed "not scanned" regardless of reality.
    is_scanned: bool = False
    # Composite 0.0–1.0 extraction confidence (section coverage 50% + text
    # volume 30% + metadata completeness 20%), computed near the end of
    # parse_pdf_report. Same bug class as is_scanned above: it was computed
    # and logged but had no field to land in, so it never reached the
    # silver.reports INSERT and extraction_confidence was NULL on every row
    # in production — leaving the OCR review-routing signal permanently
    # blank. None only on the early-return path for an unparseable document.
    extraction_confidence: float | None = None


@dataclass(frozen=True, slots=True)
class OcrPageAttempt:
    """One page produced by a full-document OCR attempt."""

    page_number: int
    text: str
    mean_confidence: float
    assessment: dict[str, Any]
    # Scanned-table support (2026-08-11) — DI prebuilt-layout table grids
    # (tables[t][row][col]); always () on the tesseract path.
    tables: tuple[list[list[str]], ...] = ()


@dataclass(frozen=True, slots=True)
class OcrAttemptResult:
    """Full-document OCR output with truthful engine and page provenance."""

    text: str
    parser_used: str
    pages: tuple[OcrPageAttempt, ...] = ()


# ---------------------------------------------------------------------------
# Date parsing helpers
# ---------------------------------------------------------------------------

def _parse_date_string(raw: str) -> str | None:
    """Convert a free-text date string to ISO 8601 (YYYY-MM-DD).

    Handles formats like "January 15, 2024", "15 January 2024", "Jan 15 2024".
    Returns None when the string cannot be reliably parsed.
    """
    raw = raw.strip().rstrip(".")
    # Try dateutil-style month-name parsing
    parts = re.split(r"[\s,]+", raw)
    parts = [p for p in parts if p]

    year_val = None
    month_val = None
    day_val = None

    for part in parts:
        part_lower = part.lower().rstrip(".")
        if part_lower in _MONTH_MAP:
            month_val = _MONTH_MAP[part_lower]
        elif re.match(r"^\d{4}$", part):
            year_val = int(part)
        elif re.match(r"^\d{1,2}$", part):
            day_val = int(part)

    if year_val and month_val and day_val:
        try:
            return datetime(year_val, month_val, day_val).date().isoformat()
        except ValueError:
            pass

    # Fallback: try stdlib strptime with known formats
    for fmt in (
        "%B %d, %Y", "%B %d %Y", "%d %B %Y",
        "%b %d, %Y", "%b %d %Y", "%d %b %Y",
        "%B %Y",  # month + year only — day defaults to 1
    ):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.date().isoformat()
        except ValueError:
            continue

    return None


# ---------------------------------------------------------------------------
# Metadata extraction from leading document text
# ---------------------------------------------------------------------------

def _extract_company(text: str) -> str | None:
    for pattern in COMPANY_PATTERNS:
        m = pattern.search(text)
        if m:
            value = m.group(1).strip().rstrip(".,")
            if len(value) > 2:
                return value
    return None


def _extract_filing_date(text: str) -> str | None:
    for pattern in FILING_DATE_PATTERNS:
        m = pattern.search(text)
        if m:
            parsed = _parse_date_string(m.group(1))
            if parsed:
                return parsed
    return None


def _extract_commodity(text: str) -> str | None:
    text_lower = text.lower()
    for kw in COMMODITY_KEYWORDS:
        if kw in text_lower:
            return kw
    return None


def _extract_authors(text: str) -> list[str]:
    """Extract Qualified Persons from the QP declaration block."""
    for pattern in QP_PATTERNS:
        m = pattern.search(text)
        if m:
            raw = m.group(1).strip()
            # Split on "and", semicolons, or newlines to get individual names
            names = re.split(r"\s+and\s+|;\s*|\n", raw)
            names = [n.strip().rstrip(".,") for n in names if len(n.strip()) > 3]
            if names:
                return names[:6]  # cap at 6 to avoid grabbing paragraph text
    return []


def _extract_project_name(text: str, title: str | None) -> str | None:
    for pattern in PROJECT_NAME_PATTERNS:
        m = pattern.search(text)
        if m:
            name = m.group(1).strip().rstrip(".,")
            if len(name) > 3:
                return name
    # Fall back to document title (trimmed)
    if title:
        return title[:100]
    return None


def _extract_region(text: str) -> str | None:
    for kw in REGION_KEYWORDS:
        if re.search(re.escape(kw), text, re.IGNORECASE):
            return kw
    return None


# ---------------------------------------------------------------------------
# Section splitting
# ---------------------------------------------------------------------------

def _assign_ocr_metadata(
    sections: list[ReportSection],
    per_page_method: dict[int, str],
    per_page_confidence: dict[int, float | None],
) -> None:
    """Phase 3 (2026-05-22) — backfill ocr_method + ocr_confidence on each
    section using the per-page maps built during dispatch.

    Rules (per kickoff):
      - ocr_method: the method of the FIRST page the section spans (first-
        page-wins). Falls back to ``None`` when no page mapping exists
        (e.g. fallback parsers that didn't track methods).
      - ocr_confidence: the MIN confidence across all pages the section
        spans, treating ``None`` as "no OCR confidence applicable" (text-
        layer extraction). If every spanned page is None, the section
        confidence is None. If any page has a confidence number, it
        propagates as the chunk's confidence (worst-case wins).

    Mutates ``sections`` in place. No-op when a section has no
    ``page_first`` (preamble blocks).
    """
    if not per_page_method and not per_page_confidence:
        return
    for s in sections:
        if s.page_first is None:
            continue
        last = s.page_last if s.page_last is not None else s.page_first
        page_range = list(range(s.page_first, last + 1))
        # First-page-wins for method (skip pages with no entry)
        for p in page_range:
            if p in per_page_method:
                s.ocr_method = per_page_method[p]
                break
        # Min-confidence across spanned pages (treat None as "skip")
        confidences = [
            per_page_confidence[p] for p in page_range
            if p in per_page_confidence and per_page_confidence[p] is not None
        ]
        if confidences:
            s.ocr_confidence = float(min(confidences))


def _build_page_index(
    per_page_text: list[tuple[int, str]],
    joiner_len: int = 1,
) -> list[tuple[int, int, int]]:
    """Return [(char_start, char_end_exclusive, page_num), ...] for full_text.

    full_text is built via "\\n".join(pages_text) in the fitz/pdfplumber
    paths (joiner_len=1) but "\\n\\n".join(texts) in the whole-document OCR
    paths (joiner_len=2). Mirror the actual joiner width here so char
    offsets line up with what the section regex sees.
    """
    index: list[tuple[int, int, int]] = []
    cursor = 0
    for i, (page_num, text) in enumerate(per_page_text):
        start = cursor
        end = start + len(text)
        index.append((start, end, page_num))
        cursor = end + (joiner_len if i < len(per_page_text) - 1 else 0)  # the joiner
    return index


def _pages_for_range(
    page_index: list[tuple[int, int, int]],
    char_start: int,
    char_end: int,
) -> tuple[int | None, int | None]:
    """Find the first and last pages overlapping [char_start, char_end)."""
    if not page_index:
        return None, None
    page_first: int | None = None
    page_last: int | None = None
    for ps, pe, pn in page_index:
        if pe <= char_start:
            continue
        if ps >= char_end:
            break
        if page_first is None:
            page_first = pn
        page_last = pn
    return page_first, page_last


# Sliding-window chunking parameters for non-NI-43-101 documents (slide decks,
# fact sheets, prospectuses, anything without "1. Summary" / "2. Introduction"
# section headers).
#
# 2026-08-20 — raised 1500/200 -> 5000/500. The old numbers were sized for
# bge-small-en-v1.5, which truncates at 512 tokens (~2000 chars), so a
# 1500-char window "landed well inside the truncation limit". That model has
# not been the embedder since the 2026-06-03 Qwen swap and is two backends
# ago now: the live embedder is Cohere Embed v4, whose context is 128K
# tokens. We were sizing chunks against a constraint that no longer exists,
# and paying for it three times over:
#
#   - a 1500-char window is ~375 tokens, so the retrieved evidence reaching
#     the model was ~1,900 tokens against a MAX_CONTEXT_TOKENS_AZURE budget
#     of 100,000 (MAX_CONTEXT_DOC_CHUNKS is 5). The model was starved by two
#     orders of magnitude;
#   - a geological argument — a drill result, its QA/QC caveat, and the
#     conclusion drawn from it — routinely spans more than 1500 characters,
#     so the reasoning got cut across chunks that then had to both be
#     retrieved to answer anything;
#   - more chunks per document is more vectors to store and rerank.
#
# 5000 chars (~1250 tokens) fits under Cohere Rerank v4's ~4096-token
# document limit (tools.py truncates foundry rerank input at 8000 chars) and
# lands 5 chunks at ~6,250 tokens — still only 6% of the Azure context
# budget. The 10% overlap is proportional to the old 13%.
#
# CAVEAT: chunk boundaries are decided at parse time and stored in
# silver.document_passages, so changing these does nothing to already-
# ingested documents — they need re-ingesting, not just re-embedding.
# `sections_text` is a partial extract (measured 2026-08-20: ~2.9k chars
# against ~10.4k chars of passages per report), so it is NOT a shortcut
# around re-parsing the source PDFs.
_DEFAULT_WINDOW_CHARS = 5000
_DEFAULT_WINDOW_OVERLAP_CHARS = 500


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


# Env-driven so the corpus can be re-ingested in stages, and rolled back to
# the old sizing, without a redeploy.
WINDOW_CHARS = max(200, _int_env("PDF_CHUNK_WINDOW_CHARS", _DEFAULT_WINDOW_CHARS))
# Clamped to half the window: an overlap at or above the window would make
# `_emit_windows` stop advancing.
WINDOW_OVERLAP_CHARS = min(
    max(0, _int_env("PDF_CHUNK_OVERLAP_CHARS", _DEFAULT_WINDOW_OVERLAP_CHARS)),
    WINDOW_CHARS // 2,
)

# 2026-08-20 — how far a window boundary may slide backwards to land on a
# line break instead of mid-line. The windows used to be cut at raw
# character offsets, which on a rendered table meant cutting through a row:
# `| DDH-22-001 | 145.2 | 1` ends one chunk and `48.0 | 2.31 |` starts the
# next, so neither chunk can answer "what grade did DDH-22-001 return" and
# the second one offers bare numbers under no header at all. Snapping to
# the preceding newline keeps every row whole. 300 chars is roughly 2-4
# table rows or two prose lines — enough to reach a boundary, small enough
# that windows stay near WINDOW_CHARS. Clamped to a third of the window so a
# small configured window can still reach a boundary without collapsing.
WINDOW_SNAP_CHARS = min(500, WINDOW_CHARS // 3)


def _snap_window_end(text: str, start: int, end: int) -> int:
    """Pull ``end`` back to just after the last line break before it.

    Returns ``end`` unchanged when there is no line break within the snap
    budget — one line longer than the budget (OCR word-soup, a very wide
    table row) has no boundary to snap to, and a hard cut is the honest
    fallback. The floor also guarantees the window stays longer than the
    overlap, which is what keeps the caller's loop advancing.
    """
    floor = max(start + WINDOW_OVERLAP_CHARS + 1, end - WINDOW_SNAP_CHARS)
    if floor >= end:
        return end
    line_break = text.rfind("\n", floor, end)
    return line_break + 1 if line_break != -1 else end


def _emit_windows(
    full_text: str,
    abs_start: int,
    abs_end: int,
    section_number: str | None,
    section_title: str,
    page_index: list[tuple[int, int, int]],
) -> list[ReportSection]:
    """Emit sliding-window ReportSections over a contiguous segment.

    Every emitted chunk has len(text) ≤ WINDOW_CHARS. Adjacent chunks
    overlap by WINDOW_OVERLAP_CHARS so split sentences still match
    retrieval queries. Boundaries snap to line breaks where they can, so a
    rendered table is never cut mid-row (see `_snap_window_end`).

    Page metadata (page_first / page_last) is derived from each chunk's
    absolute char range via page_index, so citations deep-link correctly
    even when one logical section spans many pages.
    """
    seg_len = abs_end - abs_start
    if seg_len <= 0 or not full_text[abs_start:abs_end].strip():
        return []

    out: list[ReportSection] = []

    if seg_len <= WINDOW_CHARS:
        chunk = full_text[abs_start:abs_end].strip()
        if chunk:
            p_first, p_last = _pages_for_range(page_index, abs_start, abs_end)
            out.append(ReportSection(
                section_number=section_number,
                section_title=section_title,
                text=chunk,
                page_first=p_first,
                page_last=p_last,
            ))
        return out

    # 2026-08-20 — a `range(0, seg_len, step)` walk can't express this any
    # more: snapping moves each window's end, so the next window's start
    # depends on where the previous one actually landed rather than on a
    # fixed stride.
    a = abs_start
    while a < abs_end:
        b = min(a + WINDOW_CHARS, abs_end)
        if b < abs_end:
            b = _snap_window_end(full_text, a, b)
        chunk = full_text[a:b].strip()
        if chunk:
            p_first, p_last = _pages_for_range(page_index, a, b)
            out.append(ReportSection(
                section_number=section_number,
                section_title=section_title,
                text=chunk,
                page_first=p_first,
                page_last=p_last,
            ))
        if b >= abs_end:
            break

        # `_snap_window_end`'s floor guarantees b - a > WINDOW_OVERLAP_CHARS,
        # so this is strictly greater than `a` and the loop advances.
        next_a = max(a + 1, b - WINDOW_OVERLAP_CHARS)
        # Open the overlap on a line boundary too, so the next chunk does
        # not start halfway through a table row. Only ever moves forward to
        # a break BEFORE `b`, so nothing between the windows is skipped —
        # everything up to `b` is already inside the chunk just emitted.
        line_break = full_text.find("\n", next_a, b)
        if line_break != -1 and line_break + 1 < b:
            next_a = line_break + 1
        a = next_a

    return out


def _split_into_sections(
    full_text: str,
    per_page_text: list[tuple[int, str]] | None = None,
    joiner_len: int = 1,
) -> list[ReportSection]:
    """Chunk the document with sliding windows; tag chunks with section
    metadata when NI 43-101 headings are detected.

    Every emitted ReportSection has ``len(text) ≤ WINDOW_CHARS``, which
    sits inside the reranker's per-document input budget (the embedder,
    Cohere Embed v4, has a 128K-token context and is not the binding
    constraint). Section structure is preserved as *metadata* on each
    chunk:

      * Chunks inside "N. Title" inherit ``section_number=N`` and
        ``section_title=Title``.
      * Chunks before the first detected heading get
        ``section_number=None`` and ``section_title="Preamble"`` —
        and are themselves windowed, so a 100-KB preamble (common when
        a report doesn't follow NI 43-101 numbering at the top) becomes
        many retrievable chunks instead of one truncated mega-passage.
      * When no headings are detected at all, every chunk is labelled
        ``section_title="Document"``.

    Page mapping (page_first / page_last) is computed per chunk from the
    chunk's absolute char range, so citations resolve to the right page
    even within long sections.
    """
    text = full_text.strip()
    if not text:
        return []

    page_index = _build_page_index(per_page_text or [], joiner_len=joiner_len)
    # F15 — SECTION_HEADING_RE is line-anchored (^...$ MULTILINE), so
    # group(0) is the whole line; drop TOC entries by line shape.
    matches = [
        m for m in SECTION_HEADING_RE.finditer(full_text)
        if not _TOC_LINE_TAIL_RE.search(m.group(0))
    ]

    if not matches:
        logger.info(
            "pdf_report: no NI 43-101 section headings — windowing whole "
            "document (window=%d, overlap=%d)",
            WINDOW_CHARS, WINDOW_OVERLAP_CHARS,
        )
        return _emit_windows(
            full_text, 0, len(full_text), None, "Document", page_index,
        )

    sections: list[ReportSection] = []

    # Preamble: everything before the first detected heading.
    if matches[0].start() > 0:
        sections.extend(_emit_windows(
            full_text, 0, matches[0].start(), None, "Preamble", page_index,
        ))

    # One contiguous segment per detected heading.
    for i, match in enumerate(matches):
        section_num = match.group(1)
        section_title = match.group(2).strip()
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        sections.extend(_emit_windows(
            full_text, body_start, body_end, section_num, section_title, page_index,
        ))

    return sections


# ---------------------------------------------------------------------------
# Resource table extraction
# ---------------------------------------------------------------------------

def _score_header_row(row: list) -> int:
    """Count how many cells in a row match column-header tokens."""
    count = 0
    for cell in row:
        if cell is None:
            continue
        cell_lower = str(cell).lower()
        for token in _COLUMN_HEADER_TOKENS:
            if token in cell_lower:
                count += 1
                break
    return count


def _classify_header(table: list[list]) -> tuple[int, list[str]]:
    """Find the best header row among the first 3 rows of a table.

    Returns (header_row_index, cleaned_header_list).
    """
    candidates = table[:3]
    best_idx = 0
    best_score = -1
    for i, row in enumerate(candidates):
        score = _score_header_row(row)
        if score > best_score:
            best_score = score
            best_idx = i

    raw_header = candidates[best_idx] if candidates else []
    cleaned = [
        (str(cell).strip() if cell is not None else f"col_{i}")
        for i, cell in enumerate(raw_header)
    ]
    cleaned = [c if c else f"col_{i}" for i, c in enumerate(cleaned)]
    return best_idx, cleaned


def _table_confidence(header: list[str], data_rows: list[list]) -> float:
    """Compute a 0.0–1.0 confidence score for a resource table.

    Formula:
        0.3 * (matched_header_tokens / len(header))
      + 0.3 * (1 - stddev(row_lengths) / mean(row_lengths))  [row consistency]
      + 0.4 * min(1.0, n_rows / 10)
    """
    if not header:
        return 0.0

    # Header token score
    matched = sum(
        1 for h in header
        if any(tok in h.lower() for tok in _COLUMN_HEADER_TOKENS)
    )
    header_score = matched / len(header)

    # Row length consistency
    if data_rows:
        lengths = [len(r) for r in data_rows]
        mean_len = sum(lengths) / len(lengths)
        if mean_len > 0 and len(lengths) > 1:
            try:
                sd = statistics.stdev(lengths)
            except statistics.StatisticsError:
                sd = 0.0
            consistency = max(0.0, 1.0 - sd / mean_len)
        else:
            consistency = 1.0
    else:
        consistency = 0.0

    # Row volume score
    row_score = min(1.0, len(data_rows) / 10)

    confidence = 0.3 * header_score + 0.3 * consistency + 0.4 * row_score
    return round(min(1.0, confidence), 4)


def _extract_resource_tables(
    pdf_path: str, progress_file: str | None = None,
) -> list[dict]:
    """Extract mineral resource / reserve tables from a NI 43-101 PDF.

    Opens the PDF with pdfplumber, identifies candidate pages via trigger
    phrases, and attempts two extraction strategies (lines-based, then
    text-based). Returns a list of structured table dicts.

    Each entry contains:
        page, table_index_on_page, trigger_phrase, header, rows,
        extraction_method, confidence.

    Progress (2026-08-14): ticks the 'tables' phase over the FIRST half of
    a 2×pages span — `_extract_all_tables_as_sections` (which always runs
    right after this in parse_pdf_report) ticks the second half, so the
    relayed pct stays monotonic across both pdfplumber walks.
    """
    import pdfplumber  # noqa: PLC0415

    results: list[dict] = []

    with pdfplumber.open(pdf_path) as pdf:
        _total_pages = len(pdf.pages)
        for page_num, page in enumerate(pdf.pages, start=1):
            _tick_progress(progress_file, "tables", page_num, 2 * _total_pages)
            try:
                page_text = (page.extract_text() or "").lower()
            except Exception:
                continue

            # Determine which trigger phrase (if any) matches this page
            matched_trigger: str | None = None
            for trigger in _RESOURCE_TABLE_TRIGGERS:
                if trigger in page_text:
                    matched_trigger = trigger
                    break

            if matched_trigger is None:
                continue

            # Strategy 1: line-ruled tables
            tables = page.extract_tables(
                table_settings={
                    "vertical_strategy": "lines",
                    "horizontal_strategy": "lines",
                }
            )
            method = "lines"

            # Strategy 2: text-aligned tables (fallback)
            if not tables:
                tables = page.extract_tables(
                    table_settings={
                        "vertical_strategy": "text",
                        "horizontal_strategy": "text",
                    }
                )
                method = "text"

            for tbl_idx, table in enumerate(tables or []):
                if not table:
                    continue

                header_row_idx, header = _classify_header(table)
                data_rows = [
                    [str(c) if c is not None else "" for c in row]
                    for row in table[header_row_idx + 1:]
                ]

                confidence = _table_confidence(header, data_rows)

                results.append({
                    "page": page_num,
                    "table_index_on_page": tbl_idx,
                    "trigger_phrase": matched_trigger,
                    "header": header,
                    "rows": data_rows,
                    "extraction_method": method,
                    "confidence": confidence,
                })

    return results


# Minimum table size to bother indexing — drops layout-tables and
# header/footer artifacts that pdfplumber sometimes catches.
_MIN_TABLE_ROWS = 3
_MIN_TABLE_COLS = 2


def _markdown_cell(value: Any) -> str:
    """Flatten one cell to something safe to sit between two pipes.

    A literal ``|`` opens a new column and a newline ends the row, so a
    cell containing either would silently change the table's shape — a
    grade column shifted one place left is a wrong answer with a citation
    attached, which is worse than no answer.
    """
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").replace("|", r"\|")
    return " ".join(text.split())


#: Characters stripped before asking "is this cell a number?" — currency,
#: grouping, percent and the units that ride along in a table cell.
_NUMERIC_CELL_STRIP = str.maketrans(
    "", "", "$%,()<>\u00b1\u2264\u2265\u2013\u2014",
)


def _cell_is_numeric(cell: str) -> bool:
    """True when a table cell holds a measurement rather than a label.

    Tolerant on purpose: "1,250", "2.31%", "<0.01", "145.20 m" and "(3.4)"
    are all numbers as far as a table's shape is concerned.
    """
    text = (cell or "").strip()
    if not text:
        return False
    text = text.translate(_NUMERIC_CELL_STRIP).strip()
    if not text:
        return False
    # Drop a trailing unit token: "145.20 m", "2.31 g/t".
    head = text.split()[0] if " " in text else text
    try:
        float(head)
    except ValueError:
        return False
    return True


def _numeric_fraction(row: list[str]) -> float:
    """Fraction of a row's non-empty cells that read as numbers."""
    filled = [cell for cell in row if (cell or "").strip()]
    if not filled:
        return 0.0
    return sum(1 for cell in filled if _cell_is_numeric(cell)) / len(filled)


#: A first row is demoted from header to data only on POSITIVE evidence:
#: it must be numerically dense AND statistically indistinguishable from
#: the body. Absent that, the existing promote-row-0 behaviour stands, so
#: an all-text table (lithology descriptions, QP tables) is unaffected.
_HEADER_MIN_NUMERIC_DENSITY = 0.4
_HEADER_MAX_BODY_DIVERGENCE = 0.25


def _first_row_is_data(rows: list[list[str]]) -> bool:
    """Does row 0 look like another data row rather than column labels?

    Tables are extracted PER PAGE. On page 2+ of a table that spans a page
    break there is no header row at all, so `_table_to_markdown` used to
    promote the first *data* row to the header and follow it with a
    ``| --- |`` delimiter. A 6-page assay table starting on page 88 with
    ``Hole ID | From (m) | To (m) | Au (g/t)`` renders pages 89-93 as::

        | DDH-22-041 | 145.20 | 148.00 | 2.31 |
        | --- | --- | --- | --- |
        | DDH-22-042 | 151.00 | 154.00 | 0.87 |

    telling the reader — and the LLM — that "DDH-22-041" and "2.31" are
    column names. Every answer drawn from those pages carries unlabelled
    numbers with no units, and one real assay row is consumed as a label.

    The test is deliberately conservative. Column labels are words;
    measurements are numbers. Only when row 0 is itself numerically dense
    AND matches the body's density is it treated as data.
    """
    if len(rows) < 2:
        # A single-row table has nothing to compare against. Keep the
        # historical reading: it is a header.
        return False

    head_density = _numeric_fraction(rows[0])
    if head_density < _HEADER_MIN_NUMERIC_DENSITY:
        return False

    body = rows[1:]
    body_density = sum(_numeric_fraction(row) for row in body) / len(body)
    if body_density < _HEADER_MIN_NUMERIC_DENSITY:
        return False

    return abs(head_density - body_density) <= _HEADER_MAX_BODY_DIVERGENCE


def _table_to_markdown(
    table: list[list[str | None]],
    has_header: bool | None = None,
) -> str:
    """Render a table-of-lists as a GitHub-Flavored Markdown table.

    2026-08-20 — this used to join cells with ``" | "`` and stop there:
    no delimiter row, no leading/trailing pipes, no escaping, no column
    padding. That is row-per-line text that *resembles* Markdown without
    being it, which cost us three things:

      - nothing downstream (chunker, LLM, or a future Markdown-aware
        splitter) could recognise a table as a table;
      - ragged rows stayed ragged, so a row missing its last cell shifted
        every value in it under the wrong header;
      - a cell containing a pipe silently split into two columns.

    Real Markdown fixes all three and costs ~4 characters per row. The
    original goal is unchanged and still met: each cell stays on a
    recognisable row/column so BM25 + dense retrieval can match "Au grade
    at hole MAD-22-001" against a value that lives in a cell rather than
    in flowing prose.

    2026-08-21 -- row 0 is no longer promoted to the header
    unconditionally. See `_first_row_is_data`: tables are extracted per
    page, so every continuation page of a table that spans a page break
    arrives with no header row and its first DATA row was being labelled
    as the column names.

    Args:
        table: rows of cells.
        has_header: authoritative override when the extractor knows --
            Document Intelligence reports `cell.kind == "columnHeader"`.
            None means infer.
    """
    if not table:
        return ""
    rows: list[list[str]] = []
    for row in table:
        cells = [_markdown_cell(cell) for cell in row]
        if any(cells):
            rows.append(cells)
    if not rows:
        return ""

    # Markdown requires every row to have the delimiter row's column
    # count. Pad rather than truncate: a short row is missing trailing
    # cells, and dropping the overflow of a long one would lose data.
    width = max(len(row) for row in rows)
    padded = [row + [""] * (width - len(row)) for row in rows]

    header_present = (
        has_header if has_header is not None else not _first_row_is_data(padded)
    )
    delimiter = "| " + " | ".join(["---"] * width) + " |"

    if header_present:
        lines = ["| " + " | ".join(padded[0]) + " |", delimiter]
        body = padded[1:]
    else:
        # GFM has no headerless table, so emit an EMPTY header row. Blank
        # column names are honest — the labels are on a previous page —
        # where promoting a data row states something false and eats a row.
        lines = ["| " + " | ".join([""] * width) + " |", delimiter]
        body = padded

    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def _is_markdown_delimiter_row(line: str) -> bool:
    """True for a ``| --- | --- |`` style Markdown delimiter row."""
    segments = [seg.strip() for seg in line.strip().strip("|").split("|")]
    if not segments:
        return False
    return all(
        seg and set(seg) <= {"-", ":"} and "-" in seg
        for seg in segments
    )


def _split_table_markdown(md: str) -> list[str]:
    """F13 (2026-08-11) — split an oversize table's markdown into chunks of
    at most WINDOW_CHARS on row boundaries, repeating the header at the top
    of every chunk so each part stays independently interpretable.

    2026-08-20: the repeated header is now the header row *and* the
    delimiter row beneath it, since `_table_to_markdown` emits real
    Markdown. Repeating only the first line would leave every part after
    the first as a headerless run of pipe-separated lines.
    """
    lines = md.splitlines()
    if len(lines) <= 1:
        return [md]
    # Defensive about the shape: a caller (or an older stored value) may
    # still hand us header-only markdown with no delimiter row.
    header_depth = 2 if len(lines) > 2 and _is_markdown_delimiter_row(lines[1]) else 1
    if len(lines) <= header_depth:
        return [md]

    header = lines[:header_depth]
    header_len = sum(len(line) + 1 for line in header) - 1
    chunks: list[str] = []
    current: list[str] = list(header)
    current_len = header_len
    for line in lines[header_depth:]:
        if current_len + 1 + len(line) > WINDOW_CHARS and len(current) > header_depth:
            chunks.append("\n".join(current))
            current = list(header)
            current_len = header_len
        current.append(line)
        current_len += 1 + len(line)
    if len(current) > header_depth:
        chunks.append("\n".join(current))
    return chunks or [md]


def _table_has_data(table: list[list]) -> bool:
    """Heuristic: distinguish a real data table from page-layout artifacts.

    Cover pages, tables of contents, and multi-column page layouts get
    detected as tables by pdfplumber's text-strategy. They should be
    excluded so we don't flood the index with layout noise (a 395-page
    PFS would otherwise yield 500+ "tables" most of which are cover/TOC
    fragments).

    A real data table:
      - ≥3 rows, ≥2 cols
      - ≥40% of cells non-empty (TOC pages have very sparse fills)
      - ≥20% of non-empty cells contain a digit (data tables are numeric)
      - Column count is reasonably consistent (layout tables jitter)
    """
    if not table or len(table) < _MIN_TABLE_ROWS:
        return False
    row_widths = [len(r) for r in table if r]
    if not row_widths:
        return False
    max_cols = max(row_widths)
    if max_cols < _MIN_TABLE_COLS:
        return False
    # Column-count consistency: ≥70% of rows share the same width.
    from collections import Counter
    width_counts = Counter(row_widths)
    most_common_width, count = width_counts.most_common(1)[0]
    if count / len(row_widths) < 0.7:
        return False
    # Fill ratio + numeric ratio.
    total_cells = 0
    non_empty = 0
    numeric_cells = 0
    for row in table:
        for cell in row or []:
            total_cells += 1
            if cell is None:
                continue
            s = str(cell).strip()
            if not s:
                continue
            non_empty += 1
            if re.search(r"\d", s):
                numeric_cells += 1
    if total_cells == 0:
        return False
    fill_ratio = non_empty / total_cells
    if fill_ratio < 0.4:
        return False
    numeric_ratio = numeric_cells / max(non_empty, 1)
    if numeric_ratio < 0.2:
        return False

    # TOC detection: table-of-contents pages look table-like (consistent
    # column count, page-number numerics) but are noise. Signals:
    #   - "leader dots" (lines like "1. Summary ........ 12") show up as
    #     cells containing 3+ consecutive dots
    #   - cells are long (full section titles), not short codes/numbers
    leader_dot_cells = 0
    long_cells = 0
    total_text_chars = 0
    for row in table:
        for cell in row or []:
            if not cell:
                continue
            s = str(cell)
            if re.search(r"\.{3,}", s):
                leader_dot_cells += 1
            if len(s.strip()) > 30:
                long_cells += 1
            total_text_chars += len(s.strip())
    if non_empty and leader_dot_cells / non_empty > 0.15:
        return False
    avg_cell_len = total_text_chars / max(non_empty, 1)
    if avg_cell_len > 60:
        # Real data tables have short cells (numbers, codes, short labels).
        # Long average cell length is a TOC / narrative pasted as table.
        return False
    return True


def _table_signature(table: list[list]) -> str:
    """Hash of table cell contents — used to dedupe the same table caught
    by both `lines` and `text` extraction strategies on the same page."""
    cells = []
    for row in table[:5]:  # sample first 5 rows for speed
        for c in (row or [])[:8]:  # and first 8 cols
            cells.append(str(c or "").strip())
    return hashlib.sha1("|".join(cells).encode("utf-8", "ignore")).hexdigest()[:16]


def _classify_page_table_type(
    items: list[tuple],
    line_threshold: int = 3,
    rect_threshold: int = 20,
    min_horizontal_line_length: float = 30.0,
) -> str:
    """Phase 4 (2026-05-22) — classify a PDF page as 'bordered' or 'borderless'.

    Walks a flat list of vector-drawing primitives produced by
    `_iter_pdfium_path_items` (one per straight-line or closed-polygon
    subpath found on the page).

    Heuristic:
      - Count horizontal lines longer than ``min_horizontal_line_length``
        points. ≥ ``line_threshold`` → bordered.
      - Count closed-polygon ('re'-equivalent) subpaths. ≥ ``rect_threshold``
        → bordered. Real-world prospectuses commonly use rectangles (not
        lines) for table borders — counted separately so the kickoff's
        TABLE_BORDER_LINE_THRESHOLD threshold doesn't miss them.

    Each item is either:
      - ("l", (x1, y1), (x2, y2)) — a 2-point straight-line subpath
      - ("re",) — a closed-polygon subpath (≥ 4 points, closed)

    A page that has ≥ either threshold is bordered. Pages below both
    thresholds are classified borderless. Returns "bordered" or
    "borderless"; never None.

    Engine note (2026-08-15): originally walked fitz's (PyMuPDF's)
    `page.get_drawings()` output. PyMuPDF was removed for its AGPL license
    (2026-05-27, see pyproject.toml), so the `import pymupdf` this function
    depended on ALWAYS failed at runtime — `_classify_pages_from_pdf` has
    been silently returning {} on every call since, permanently disabling
    both TABLE_BORDER_* env knobs. Ported to pypdfium2 (Apache 2.0, already
    a dependency — see `_iter_pdfium_path_items`, which replaces
    `get_drawings()` by walking each PATH page-object's raw path segments).
    """
    h_lines = 0
    rects = 0
    for it in items or []:
        kind = it[0] if it else None
        if kind == "l":
            # Line: ("l", (x1,y1), (x2,y2)). Count near-horizontal lines
            # only (Δy ~ 0 within 1 point), of meaningful length.
            try:
                (x1, y1), (x2, y2) = it[1], it[2]
                if (
                    abs(y1 - y2) < 1.0
                    and abs(x1 - x2) >= min_horizontal_line_length
                ):
                    h_lines += 1
            except Exception:
                continue
        elif kind == "re":
            # Closed polygon (rectangle-equivalent). Counted regardless of
            # size; real-world bordered table cells can be tiny.
            rects += 1
        # Open polygons and bezier-containing subpaths never reach here —
        # `_iter_pdfium_path_items` drops them, mirroring fitz's ignoring
        # of 'qu' (quad) / 'c' (curve) primitives.
    if h_lines >= line_threshold:
        return "bordered"
    if rects >= rect_threshold:
        return "bordered"
    return "borderless"


def _iter_pdfium_path_items(path_object, pdfium_raw) -> list[tuple]:
    """Walk one pypdfium2 PATH page-object's segments into the same
    ('l', p1, p2) / ('re',) primitives fitz's `get_drawings()['items']`
    used to produce for a single drawing operation.

    A PDF path object can contain multiple disjoint subpaths (multiple
    MOVETOs — e.g. a whole table grid stroked in one operation as many
    `m l` pairs sharing one paint op). Each subpath is classified
    independently:
      - exactly 2 points, not closed → ("l", (x1, y1), (x2, y2))
      - ≥ 4 points, closed → ("re",) — PDFium decomposes the `re` operator
        into MOVETO + 3× LINETO + close-on-last-segment, so this also
        catches other ruled-cell polygons drawn the same way.
      - anything else (a single point, an open polygon, any subpath
        containing a bezier curve) → dropped, mirroring fitz's ignoring of
        'c' (curve) / 'qu' (quad) primitives.

    Verified against synthetic pypdfium2-rendered fixtures covering: lines
    drawn as separate stroke ops, lines drawn as one combined multi-subpath
    stroke op, `re`-style bordered cells, and small decorative vector
    shapes that must NOT trip the classifier.
    """
    items: list[tuple] = []
    n = pdfium_raw.FPDFPath_CountSegments(path_object)
    if n <= 0:
        return items

    pts: list[tuple[float, float]] = []
    has_bezier = False
    closed = False

    def _flush() -> None:
        if has_bezier or len(pts) < 2:
            return
        if closed and len(pts) >= 4:
            items.append(("re",))
        elif not closed and len(pts) == 2:
            items.append(("l", pts[0], pts[1]))

    for i in range(n):
        seg = pdfium_raw.FPDFPath_GetPathSegment(path_object, i)
        seg_type = pdfium_raw.FPDFPathSegment_GetType(seg)
        x = ctypes.c_float()
        y = ctypes.c_float()
        pdfium_raw.FPDFPathSegment_GetPoint(seg, ctypes.byref(x), ctypes.byref(y))
        seg_closed = bool(pdfium_raw.FPDFPathSegment_GetClose(seg))

        if seg_type == pdfium_raw.FPDF_SEGMENT_MOVETO and pts:
            # New subpath starting — flush the one just finished.
            _flush()
            pts = []
            has_bezier = False
            closed = False

        if seg_type == pdfium_raw.FPDF_SEGMENT_BEZIERTO:
            has_bezier = True
        pts.append((x.value, y.value))
        closed = closed or seg_closed

    _flush()
    return items


def _classify_pages_from_pdf(pdf_path: str) -> dict[int, str]:
    """Open the PDF once via pypdfium2 and return {page_no: 'bordered'|'borderless'}.

    Reads thresholds from env vars (with the defaults in kickoff):
      TABLE_BORDER_LINE_THRESHOLD (default 3)
      TABLE_BORDER_RECT_THRESHOLD (default 20)

    Returns an empty dict on any failure (caller falls back to legacy
    behavior — defensive).
    """
    try:
        import pypdfium2 as pdfium  # noqa: PLC0415
        import pypdfium2.raw as pdfium_raw  # noqa: PLC0415
    except ImportError:
        return {}
    try:
        line_thr = int(os.environ.get("TABLE_BORDER_LINE_THRESHOLD", "3"))
    except ValueError:
        line_thr = 3
    try:
        rect_thr = int(os.environ.get("TABLE_BORDER_RECT_THRESHOLD", "20"))
    except ValueError:
        rect_thr = 20

    try:
        pdf = pdfium.PdfDocument(pdf_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pdf_report: page classification failed for '%s' (%s) — "
            "callers will treat every page as borderless",
            pdf_path, exc,
        )
        return {}

    result: dict[int, str] = {}
    try:
        for n, page in enumerate(pdf, start=1):
            try:
                path_objects = list(
                    page.get_objects(filter=(pdfium_raw.FPDF_PAGEOBJ_PATH,)),
                )
            except Exception:
                path_objects = []
            items: list[tuple] = []
            for obj in path_objects:
                try:
                    items.extend(_iter_pdfium_path_items(obj, pdfium_raw))
                except Exception:
                    continue
            result[n] = _classify_page_table_type(
                items,
                line_threshold=line_thr,
                rect_threshold=rect_thr,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pdf_report: page classification failed for '%s' (%s) — "
            "callers will treat every page as borderless",
            pdf_path, exc,
        )
        return {}
    finally:
        pdf.close()
    return result


def _extract_all_tables_as_sections(
    pdf_path: str, progress_file: str | None = None,
) -> list[ReportSection]:
    """Walk every page and extract every data-table-like table.

    Each page is classified as 'bordered' (has table borders/lines/
    rectangles) or 'borderless' (whitespace-delimited) using fitz drawing
    primitives. Bordered pages use pdfplumber's "lines" strategy;
    borderless pages get the pdfplumber "text" strategy ONLY (skipping
    the more expensive lines pass on pages that won't have ruled tables
    anyway). This pdfplumber-lines path is the sole table-extraction
    method — there is no alternate engine for bordered pages.

    Each surviving table becomes its own ReportSection (one chunk per
    table; the persist + chunking layer handles sub-chunking if a table
    is bigger than the embedding window). Tables embedded in prose are
    captured both via pdfplumber's text path (mangled flowing text) AND
    here (preserved row/column structure). Duplication is OK — retrieval
    fusion will pick the better match for the query.

    Why not lean on _extract_resource_tables: that function only fires
    on pages matching resource-trigger phrases ("mineral resource"
    etc.). Assay tables, drill collar tables, geochemistry tables, QP
    certificate tables all live elsewhere. This function is the
    "everything table" net.
    """
    page_class = _classify_pages_from_pdf(pdf_path)
    bordered_pages = {p for p, t in page_class.items() if t == "bordered"}
    borderless_pages = {p for p, t in page_class.items() if t == "borderless"}
    # When classification failed (empty dict), treat every page as
    # borderless — pdfplumber-text covers most NI 43-101 styles and
    # falls back to dual-pass below if requested via env override.
    _classification_failed = not page_class

    # ------------------------------------------------------------------
    # Open pdfplumber once + walk every page. Run the strategies the
    # classifier indicated:
    #   - bordered → pdfplumber lines AND text (safety)
    #   - borderless → pdfplumber TEXT only
    #   - classification_failed → run dual-pass (default)
    # ------------------------------------------------------------------
    pdfplumber_sections: list[ReportSection] = []
    try:
        import pdfplumber  # noqa: PLC0415
        _pdf_ctx = pdfplumber.open(pdf_path)
    except Exception as pdfp_exc:  # noqa: BLE001
        logger.warning(
            "pdf_report: pdfplumber.open failed for '%s' (%s) — returning "
            "no table sections",
            pdf_path, pdfp_exc,
        )
        return []

    with _pdf_ctx as pdf:
        _total_pages = len(pdf.pages)
        for page_num, page in enumerate(pdf.pages, start=1):
            # 2026-08-14 — second half of the 'tables' progress span; see
            # the note on _extract_resource_tables (which ticks the first).
            _tick_progress(
                progress_file, "tables", _total_pages + page_num, 2 * _total_pages,
            )
            run_lines = (
                _classification_failed
                or page_num in bordered_pages
            )
            run_text = (
                _classification_failed
                or page_num in borderless_pages
                or page_num in bordered_pages  # bordered pages can still
                                              # have borderless sub-tables
            )

            tables: list[list] = []
            if run_lines:
                try:
                    t = page.extract_tables(table_settings={
                        "vertical_strategy": "lines",
                        "horizontal_strategy": "lines",
                    })
                    if t:
                        tables.extend(t)
                except Exception:
                    pass
            if run_text:
                try:
                    t = page.extract_tables(table_settings={
                        "vertical_strategy": "text",
                        "horizontal_strategy": "text",
                    })
                    if t:
                        tables.extend(t)
                except Exception:
                    pass

            seen_sigs: set[str] = set()
            for idx, tbl in enumerate(tables):
                if not _table_has_data(tbl):
                    continue
                sig = _table_signature(tbl)
                if sig in seen_sigs:
                    continue
                seen_sigs.add(sig)
                md = _table_to_markdown(tbl)
                if not md.strip():
                    continue
                # F13 — table sections previously bypassed the window
                # chunker entirely; split oversize tables on row
                # boundaries so no emitted chunk exceeds WINDOW_CHARS.
                base_title = f"Table (page {page_num}, #{idx + 1})"
                chunks = (
                    _split_table_markdown(md)
                    if len(md) > WINDOW_CHARS else [md]
                )
                for part_num, chunk in enumerate(chunks, start=1):
                    pdfplumber_sections.append(
                        ReportSection(
                            section_number=None,
                            section_title=(
                                base_title if len(chunks) == 1
                                else f"{base_title} (part {part_num})"
                            ),
                            text=chunk,
                            page_first=page_num,
                            page_last=page_num,
                        )
                    )

    # ------------------------------------------------------------------
    # 3. Final dedupe pass via a content signature, in case the lines
    #    and text strategies both captured the same table on a page.
    # ------------------------------------------------------------------
    out: list[ReportSection] = []
    seen_keys: set[tuple[int | None, str]] = set()
    for s in pdfplumber_sections:
        # Signature comes from a re-parse of the markdown — fast + good
        # enough as a stable dedupe key per (page, table-content) pair.
        body = s.text or ""
        sig = hashlib.sha1(body.encode("utf-8", "ignore")).hexdigest()[:16]
        key = (s.page_first, sig)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        out.append(s)

    return out


def _di_tables_to_sections(
    tables: list[list[list[str]]],
    page_num: int,
    mean_confidence: float | None = None,
) -> list[ReportSection]:
    """Render Document Intelligence table grids into ReportSections.

    Scanned-table support (2026-08-11): pages with no text layer never
    yield pdfplumber tables, so `_extract_all_tables_as_sections` is blind
    to them. When the DI prebuilt-layout model OCRs such a page, its
    `tables` grids (``tables[t][row][col]``, see PageOcrResult.tables) are
    the only structured capture — render each to the same ' | '-joined
    row-per-line markdown shape `_table_to_markdown` produces, split
    oversize tables on row boundaries via the F13 splitter (header
    repeated per part), and mirror `_extract_all_tables_as_sections`'
    ReportSection construction. ocr_method is always
    'document_intelligence'; ocr_confidence carries the page's mean OCR
    confidence when available.
    """
    out: list[ReportSection] = []
    for idx, grid in enumerate(tables):
        md = _table_to_markdown(grid)
        if not md.strip():
            continue
        base_title = f"Table (OCR, page {page_num}, #{idx + 1})"
        chunks = (
            _split_table_markdown(md)
            if len(md) > WINDOW_CHARS else [md]
        )
        for part_num, chunk in enumerate(chunks, start=1):
            out.append(
                ReportSection(
                    section_number=None,
                    section_title=(
                        base_title if len(chunks) == 1
                        else f"{base_title} (part {part_num})"
                    ),
                    text=chunk,
                    page_first=page_num,
                    page_last=page_num,
                    ocr_confidence=mean_confidence,
                    ocr_method="document_intelligence",
                )
            )
    return out


# ---------------------------------------------------------------------------
# Two-column layout detection and extraction
# ---------------------------------------------------------------------------

def _detect_page_columns(page) -> int:
    """Return 2 if the page appears to use a two-column layout, else 1.

    Heuristic: cluster word x0 positions into bins of width page.width/20.
    If two bins each hold >20% of words AND are >30% of page width apart,
    treat the page as two-column.
    """
    try:
        words = page.extract_words()
    except Exception:
        return 1

    if not words:
        return 1

    x0_values = [w["x0"] for w in words]
    page_width = page.width
    if page_width <= 0:
        return 1

    bin_width = page_width / 20
    bins: dict[int, int] = {}
    for x in x0_values:
        b = int(x / bin_width)
        bins[b] = bins.get(b, 0) + 1

    total = len(x0_values)
    heavy_bins = [
        (b, count) for b, count in bins.items() if count / total > 0.20
    ]

    if len(heavy_bins) < 2:
        return 1

    # Check whether any two heavy bins are >30% of page width apart
    bin_centers = [b * bin_width + bin_width / 2 for b, _ in heavy_bins]
    bin_centers.sort()
    for i in range(len(bin_centers) - 1):
        if bin_centers[i + 1] - bin_centers[i] > page_width * 0.3:
            return 2

    return 1


def _extract_text_column_aware(page) -> str:
    """Extract page text respecting two-column layouts.

    If the page is detected as two-column, crops it into left and right halves
    and concatenates their text. Falls back to standard extract_text() for
    single-column pages.
    """
    if _detect_page_columns(page) == 2:
        half = page.width / 2
        left = page.crop((0, 0, half, page.height))
        right = page.crop((half, 0, page.width, page.height))
        left_text = left.extract_text() or ""
        right_text = right.extract_text() or ""
        return left_text + "\n\n" + right_text

    return page.extract_text() or ""


# ---------------------------------------------------------------------------
# Per-page language detection
# ---------------------------------------------------------------------------

def _detect_page_language(text: str) -> str:
    """Detect the language of a page's text.

    Returns a BCP-47-style tag normalised to one of:
        "en", "fr", "es", "de", "zh-cn", "other", "unknown"

    "unknown" is returned for empty/too-short text or when langdetect raises.
    """
    if not text or len(text.strip()) < 20:
        return "unknown"

    try:
        from langdetect import detect  # noqa: PLC0415
        from langdetect.lang_detect_exception import LangDetectException  # noqa: F401, PLC0415
    except ImportError:
        return "unknown"

    _KNOWN_LANGS = {"en", "fr", "es", "de", "zh-cn"}
    try:
        lang = detect(text)
        return lang if lang in _KNOWN_LANGS else "other"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Fast text extractor: PyMuPDF (fitz)
# ---------------------------------------------------------------------------

_PROGRESS_TICK_INTERVAL_S = 2.0
_progress_last_write: dict[str, float] = {}


def _tick_progress(
    progress_file: str | None,
    phase: str,
    done: int,
    total: int,
    *,
    force: bool = False,
) -> None:
    """Best-effort page-level progress beacon for the parse subprocess.

    The parent workflow (ingest_pdf.parse) polls this file and relays it
    into silver.ingest_progress.stage_pct so the UI bar moves during a
    multi-minute parse instead of sitting on the step boundary. Never
    raises; time-gated so hot loops don't turn into fsync storms.
    """
    if not progress_file:
        return
    import time as _time
    now = _time.monotonic()
    if not force and now - _progress_last_write.get(progress_file, 0.0) < _PROGRESS_TICK_INTERVAL_S:
        return
    _progress_last_write[progress_file] = now
    try:
        with open(progress_file, "w", encoding="utf-8") as fh:
            json.dump({"phase": phase, "done": done, "total": total}, fh)
    except Exception:
        pass


def _native_text_screen_reason(
    stripped_txt: str,
    prev_accepted_stripped: str | None,
) -> str | None:
    """F16 — screen a native text-layer page that passed the length gate.

    Returns a reason string when the page should be routed to OCR instead
    of accepted as fitz_native, else None. Reuses ocr_quality's gibberish /
    repeated-character word ratios (calculate_ocr_quality works with empty
    confidences — only the text-derived signals are read here), plus a
    repeated-boilerplate check against the previous accepted page.
    """
    from .ocr_quality import calculate_ocr_quality

    signals = calculate_ocr_quality(
        stripped_txt, [], detected_region_count=0,
    )
    if signals.gibberish_word_ratio > NATIVE_TEXT_MAX_GIBBERISH_RATIO:
        return "gibberish_word_ratio"
    if signals.repeated_character_ratio > NATIVE_TEXT_MAX_REPEATED_CHAR_RATIO:
        return "repeated_character_ratio"
    if (
        prev_accepted_stripped
        and len(stripped_txt) < NATIVE_TEXT_BOILERPLATE_MAX_CHARS
        and difflib.SequenceMatcher(
            None, stripped_txt, prev_accepted_stripped,
        ).ratio() >= NATIVE_TEXT_BOILERPLATE_SIMILARITY
    ):
        return "repeated_boilerplate"
    return None


def _parse_with_fitz(
    path: str,
    apply_ocr_fallback: bool = True,
    progress_file: str | None = None,
) -> tuple[
    str, str, int, list, list[str], list[tuple[int, str]], list[int],
    dict[int, str], dict[int, float | None], dict[int, list[list[list[str]]]],
]:
    """Extract full text using pypdfium2 (PDFium). Faster than pdfplumber.

    Engine history: originally PyMuPDF (fitz); swapped to pypdfium2 when PyMuPDF
    was removed for its AGPL license. The `fitz` name/labels are retained as
    stable identifiers (see the engine note in the body).

    Returns (full_text, title, skipped, warnings, page_languages,
             per_page_text, image_page_nums, per_page_method,
             per_page_confidence, per_page_tables).

    Scanned-table support (2026-08-11) — `per_page_tables` maps 1-indexed
    page number → the Document Intelligence table grids that page's OCR
    returned (``tables[t][row][col]``; only ever populated when OCR_ENGINE
    routes `_ocr_single_page` to DI prebuilt-layout). The caller renders
    them into table ReportSections via `_di_tables_to_sections`.

    `image_page_nums` — list of 1-indexed pages where fitz returned
    less than PER_PAGE_MIN_CHARS (i.e. needs OCR). Always populated
    regardless of `apply_ocr_fallback`.

    Phase 3 (2026-05-22) — `per_page_method` and `per_page_confidence`
    are page-keyed dicts recording which engine produced the text on
    each page and (for OCR'd pages) the mean engine confidence. Used
    by `_assign_ocr_metadata` to backfill ReportSection.ocr_*.
    - text-layer page → method='fitz_native', confidence=None
    - internal tesseract recovery → method='tesseract',
      confidence=mean_conf in [0, 1]

    Per-page OCR fallback (when `apply_ocr_fallback=True`, the default):
    runs tesseract on each short page and inserts the recovered text
    into per_page_text. Image pages in an otherwise text-dense doc
    (scanned drill logs, map figures with embedded text) don't get
    silently dropped. When `apply_ocr_fallback=False`, the caller
    leaves those pages unfilled in `image_page_nums`.

    Used as the primary parser when PDF_PARSER_FITZ_ENABLED=true (default).
    Falls back to pdfplumber when fitz returns suspiciously little text.
    """
    # Engine note: this parser was PyMuPDF (fitz) until PyMuPDF was removed for
    # its AGPL license. It is now backed by pypdfium2 (Apache-2.0 — already a
    # dependency, used by the figure extractor). The "fitz"/"fitz_native" labels
    # kept below are STABLE wire-identifiers, not engine names: parse_pdf_report
    # gates on `parser_used == "fitz"`, and per_page_method feeds the
    # Qdrant/observability payload — so the engine swap deliberately does
    # not churn those contracts.
    import pypdfium2 as pdfium  # noqa: PLC0415

    pages_text: list[str] = []
    per_page_text: list[tuple[int, str]] = []
    page_languages: list[str] = []
    warnings: list[dict] = []
    short_page_nums: list[int] = []  # candidates for per-page OCR
    short_page_native: dict[int, str] = {}  # sub-threshold native text, kept for salvage
    _prev_native_stripped: str | None = None  # F16 — boilerplate comparison anchor
    # Phase 3 (2026-05-22) — per-page engine + confidence tracking
    per_page_method: dict[int, str] = {}
    per_page_confidence: dict[int, float | None] = {}
    # Scanned-table support (2026-08-11) — DI table grids per OCR'd page
    per_page_tables: dict[int, list[list[list[str]]]] = {}

    # PDFium returns these sentinels for unset metadata fields — treat as "no
    # title" so the first-line fallback below can supply a real one.
    _META_SENTINELS = {"", "(anonymous)", "(unspecified)"}

    pdf = pdfium.PdfDocument(path)
    try:
        meta = pdf.get_metadata_dict() or {}
        _raw_title = (meta.get("Title") or "").strip()
        meta_title = "" if _raw_title in _META_SENTINELS else _raw_title
        _total_pages = len(pdf)
        for n in range(1, _total_pages + 1):
            _tick_progress(progress_file, "extract", n, _total_pages)
            page = pdf[n - 1]
            try:
                # get_text_bounded() returns the full page's text in PDFium's
                # reading order (top-to-bottom, left-to-right) — the pypdfium2
                # equivalent of fitz's get_text("text", sort=True), which matters
                # for two-column pages where naive ordering interleaves columns.
                txt = page.get_textpage().get_text_bounded()
            except Exception as e:
                warnings.append({
                    "code": "pdf_extraction_partial",
                    "page": n,
                    "message": str(e),
                })
                page_languages.append("unknown")
                short_page_nums.append(n)
                continue
            _stripped = (txt or "").strip()
            # F16 — length alone isn't quality: garbage embedded OCR and
            # header-only boilerplate pages that clear the 80-char gate are
            # screened and routed to OCR like short pages.
            _screen_reason = (
                _native_text_screen_reason(_stripped, _prev_native_stripped)
                if len(_stripped) >= PER_PAGE_MIN_CHARS else None
            )
            if len(_stripped) >= PER_PAGE_MIN_CHARS and _screen_reason is None:
                pages_text.append(txt)
                per_page_text.append((n, txt))
                page_languages.append(_detect_page_language(txt))
                # Phase 3 — text-layer page, no OCR involved
                per_page_method[n] = "fitz_native"
                per_page_confidence[n] = None
                _prev_native_stripped = _stripped
            else:
                # Page came back short (or failed the F16 native-text
                # screen) — queue it for OCR below.
                if _screen_reason is not None:
                    logger.info(
                        "pdf_report: native text on page %d failed quality "
                        "screen (%s) — routing to OCR",
                        n, _screen_reason,
                    )
                page_languages.append("unknown")
                short_page_nums.append(n)
                short_page_native[n] = txt or ""
    finally:
        with contextlib.suppress(Exception):
            pdf.close()

    # Per-page OCR for any pages fitz returned <PER_PAGE_MIN_CHARS on.
    # Runs the same tesseract pipeline as pdfplumber's fallback, so image
    # pages in an otherwise text-dense doc don't get silently dropped.
    # When `apply_ocr_fallback=False`, skip this loop and leave those
    # pages in `image_page_nums` for the caller.
    if short_page_nums and apply_ocr_fallback:
        logger.info(
            "pdf_report: fitz returned <%d chars on %d pages — running per-page OCR",
            PER_PAGE_MIN_CHARS, len(short_page_nums),
        )
        ocr_recovered = 0

        # Perf audit 2026-08-15 (item 3) — this used to be a fully serial
        # for-loop: one _ocr_single_page call, fully awaited, then the
        # next. Each call is either a network round-trip to Azure Document
        # Intelligence or a tesseract subprocess invocation (pytesseract
        # shells out to the `tesseract` binary) — both release the GIL
        # while blocked, so a small bounded thread pool lets several
        # pages' OCR genuinely overlap within this one parse subprocess
        # (still one document per subprocess — see _run_parser_subprocess
        # in hatchet_workflows/ingest_pdf.py). _ocr_single_page's shared
        # mutable state (the cached pikepdf.Pdf object + the DI page
        # budget) is guarded by _PIKEPDF_LOCK — see _get_cached_pikepdf's
        # docstring. Only the OCR calls themselves run concurrently; the
        # per-page bookkeeping below (pages_text, per_page_text,
        # per_page_method, warnings, ...) is applied sequentially, in
        # short_page_nums order (asyncio.gather preserves input order
        # regardless of completion order), so it stays exactly as
        # single-threaded/order-independent as the old code — and
        # per_page_text is re-sorted by page number a few lines down
        # regardless.
        _OCR_PAGE_CONCURRENCY = max(
            1, int(os.environ.get("PDF_OCR_PAGE_CONCURRENCY", "4"))
        )

        def _ocr_one_page(n: int):
            try:
                # Phase 3 — capture mean_conf from tesseract per-word data.
                # return_tables — DI prebuilt-layout table grids (always []
                # on the tesseract path).
                return n, _ocr_single_page(
                    path,
                    n,
                    return_confidence=True,
                    return_assessment=True,
                    return_tables=True,
                ), None
            except Exception as _ocr_exc:  # noqa: BLE001
                return n, None, _ocr_exc

        async def _run_ocr_fanout():
            loop = asyncio.get_event_loop()
            sem = asyncio.Semaphore(_OCR_PAGE_CONCURRENCY)
            with ThreadPoolExecutor(max_workers=_OCR_PAGE_CONCURRENCY) as executor:
                async def _bounded(n: int):
                    async with sem:
                        return await loop.run_in_executor(executor, _ocr_one_page, n)

                return await asyncio.gather(*(_bounded(n) for n in short_page_nums))

        # _parse_with_fitz is a plain sync function running inside its own
        # parse subprocess (no asyncio event loop already running there —
        # see _run_parser_subprocess), so a fresh, local asyncio.run() here
        # is safe: nothing else in this process touches asyncio.
        ocr_page_results = asyncio.run(_run_ocr_fanout())

        _ocr_done = 0
        for n, _ocr_result, _ocr_exc in ocr_page_results:
            _ocr_done += 1
            _tick_progress(progress_file, "ocr", _ocr_done, len(short_page_nums), force=True)
            if _ocr_exc is not None:
                # F30 — an OCR exception must not drop sub-threshold native
                # text: salvage it with the same bookkeeping as the
                # short-text salvage branch below.
                _native_txt = short_page_native.get(n, "")
                if _native_txt.strip():
                    pages_text.append(_native_txt)
                    per_page_text.append((n, _native_txt))
                    per_page_method[n] = "fitz_native"
                    per_page_confidence[n] = None
                    warnings.append({
                        "code": "page_ocr_exception_native_salvaged",
                        "page": n,
                        "chars": len(_native_txt.strip()),
                    })
                continue
            ocr_text, mean_conf, assessment, ocr_tables = _ocr_result
            if ocr_tables:
                # Collected regardless of whether the page's TEXT clears
                # PER_PAGE_MIN_CHARS — a scanned table page can carry
                # structure worth indexing even when its prose is thin.
                per_page_tables[n] = ocr_tables
            warnings.append(
                _ocr_quality_warning(
                    page_number=n,
                    text=ocr_text,
                    assessment=assessment,
                )
            )
            if ocr_text and len(ocr_text.strip()) >= PER_PAGE_MIN_CHARS:
                ocr_recovered += 1
                pages_text.append(ocr_text)
                per_page_text.append((n, ocr_text))
                # 2026-08-14 — _ocr_single_page may have routed to Azure
                # Document Intelligence; the assessment carries the true
                # engine. Hard-coding "tesseract" mislabeled DI pages in
                # silver.document_passages.ocr_method and the Qdrant
                # confidence weighting.
                per_page_method[n] = str(assessment.get("ocr_method") or "tesseract")
                per_page_confidence[n] = mean_conf
                # Note: not sorting pages_text — order matters for
                # downstream section detection, but OCR'd image pages
                # are usually self-contained (figures, drill logs) so
                # appending at the end is fine. per_page_text is
                # re-sorted below for char-offset → page index.
                warnings.append({
                    "code": "page_ocr_recovered_fitz",
                    "page": n,
                    "ocr_confidence": round(mean_conf, 4),
                })
            else:
                # Neither native text nor OCR cleared PER_PAGE_MIN_CHARS.
                # Keep the longer non-empty candidate instead of dropping
                # the page entirely — short pages often carry section-
                # heading anchors ("SECTION 14 — ...") that downstream
                # section splitting depends on.
                native_txt = short_page_native.get(n, "")
                salvage = max(native_txt, ocr_text or "", key=len)
                if salvage.strip():
                    pages_text.append(salvage)
                    per_page_text.append((n, salvage))
                    if salvage == native_txt:
                        per_page_method[n] = "fitz_native"
                        per_page_confidence[n] = None
                    else:
                        # 2026-08-14 — truthful engine label (see above).
                        per_page_method[n] = str(
                            assessment.get("ocr_method") or "tesseract"
                        )
                        per_page_confidence[n] = mean_conf
                    warnings.append({
                        "code": "page_short_text_salvaged",
                        "page": n,
                        "chars": len(salvage.strip()),
                    })
        if ocr_recovered:
            logger.info(
                "pdf_report: fitz+OCR recovered %d/%d short pages",
                ocr_recovered, len(short_page_nums),
            )
        # Re-sort per_page_text by page number so the
        # _build_page_index calculation in _split_into_sections gets a
        # monotonic char-offset → page mapping.
        per_page_text.sort(key=lambda x: x[0])
        # Rebuild pages_text in page-number order to match.
        pages_text = [t for _n, t in per_page_text]

    full_text = "\n".join(pages_text)

    # Title: prefer doc metadata, else first non-empty line.
    title_candidate = meta_title
    if not title_candidate:
        for line in full_text.splitlines():
            line = line.strip()
            if line:
                title_candidate = line[:200]
                break

    # Phase 2.1: short_page_nums after the OCR loop may differ from the
    # original (pages recovered by tesseract are no longer "short"). Recompute
    # the unfilled set so the caller (parse_pdf_report) knows exactly which
    # pages still need OCR — relevant only when apply_ocr_fallback=False,
    # but populate consistently in both modes for return-shape stability.
    _filled_pages = {n for n, t in per_page_text if t and len(t.strip()) >= PER_PAGE_MIN_CHARS}
    image_page_nums = [n for n in short_page_nums if n not in _filled_pages]
    return (
        full_text, title_candidate, 0, warnings, page_languages,
        per_page_text, image_page_nums,
        per_page_method, per_page_confidence, per_page_tables,
    )


# Phase 10 (2026-05-22) — _parse_with_unstructured removed.
# Phase 2.1 made fitz-first dispatch the only path; unstructured was never
# invoked from the dispatch tree. The dependency on `unstructured[pdf]` is
# also dropped from pyproject.toml + the worker bootstrap.


# ---------------------------------------------------------------------------
# Fallback parser: pdfplumber
# ---------------------------------------------------------------------------

# F11 (2026-08-11) — cache the opened pikepdf document per path.
# _ocr_single_page runs once per short page; re-opening (a full reparse
# of) a large PDF for every page is O(pages) reparses. Parsing runs in a
# single-process subprocess handling one document at a time, so a simple
# dict with evict-on-different-path is enough — no locking, and memory
# doesn't accumulate across documents.
_PIKEPDF_CACHE: dict[str, Any] = {}

# Perf audit 2026-08-15 (item 3) — guards _PIKEPDF_CACHE here AND
# _DI_PAGES_USED / _DI_CAP_LOGGED further below. Both were written assuming
# a single-threaded, one-document-per-subprocess caller, which was true
# before this change. The per-page OCR loop in _parse_with_fitz now fans
# pages out across a small in-process ThreadPoolExecutor (still just one
# document per parse subprocess — see _run_parser_subprocess in
# hatchet_workflows/ingest_pdf.py), so concurrent threads can now race on:
#  (a) the cache dict itself,
#  (b) the shared pikepdf.Pdf object it hands out — pikepdf (a qpdf
#      binding) is not documented as safe for concurrent reads from
#      multiple threads, so this is real shared mutable state, not just a
#      dict-keyed-by-page-number result, and
#  (c) the DI budget's check-and-increment.
# One lock covers all three: they're all fast, non-blocking, in-memory
# operations (dict ops + a single-page PDF slice), so serializing them
# costs nothing next to the network/OCR work that runs outside the lock.
_PIKEPDF_LOCK = threading.Lock()


def _get_cached_pikepdf(pdf_path: str):
    import pikepdf as _pikepdf

    cached = _PIKEPDF_CACHE.get(pdf_path)
    if cached is not None:
        return cached
    for _stale_path in list(_PIKEPDF_CACHE):
        with contextlib.suppress(Exception):
            _PIKEPDF_CACHE.pop(_stale_path).close()
    pdf = _pikepdf.open(pdf_path)
    _PIKEPDF_CACHE[pdf_path] = pdf
    return pdf


def _slice_single_page_pdf_bytes(pdf_path: str, page_num: int) -> bytes:
    """Extract one page into its own in-memory PDF, for DI single-page upload.

    Perf audit 2026-08-15: the whole cache-get + slice + save sequence runs
    under _PIKEPDF_LOCK because _get_cached_pikepdf hands back a SHARED
    pikepdf.Pdf object that every concurrent OCR-page thread would otherwise
    read from at once — see the lock's docstring above for why that's
    unsafe. This used to be inlined directly in _ocr_single_page, where it
    ran with no concurrency at all; pulling it into its own function just
    gives the lock a single, obvious acquisition point.
    """
    import io as _io

    import pikepdf as _pikepdf

    with _PIKEPDF_LOCK:
        _src = _get_cached_pikepdf(pdf_path)
        _single = _pikepdf.Pdf.new()
        _single.pages.append(_src.pages[page_num - 1])
        _buf = _io.BytesIO()
        _single.save(_buf)
        return _buf.getvalue()


def _slice_page_block_pdf_bytes(pdf_path: str, first_page: int, page_count: int) -> bytes:
    """Extract a contiguous run of pages into one in-memory PDF.

    Batching 2026-08-20: the batched Document Intelligence path uploads
    blocks, not pages. Slicing rather than sending the whole file keeps
    the original per-page argument intact — a 40 MB report re-uploaded
    once per block would still push O(size x blocks) bytes — while cutting
    the number of uploads by the block size. Same `_PIKEPDF_LOCK`
    discipline as `_slice_single_page_pdf_bytes`: the cached
    ``pikepdf.Pdf`` is shared across the OCR threads.
    """
    import io as _io

    import pikepdf as _pikepdf

    with _PIKEPDF_LOCK:
        _src = _get_cached_pikepdf(pdf_path)
        _block = _pikepdf.Pdf.new()
        for _offset in range(page_count):
            _block.pages.append(_src.pages[first_page - 1 + _offset])
        _buf = _io.BytesIO()
        _block.save(_buf)
        return _buf.getvalue()


# 2026-08-14 — per-document Azure DI page budget. `_ocr_single_page` fires
# one DI request per short page (plus one per tile on the tiled-escalation
# path), unmetered until now: a pathological scan (thousands of image
# pages) could silently burn the subscription quota — and on exhaustion
# Azure 403s (not retryable), which used to disappear into the tesseract
# fallback. Beyond the budget, remaining pages route straight to
# tesseract with one WARNING per document. Keyed per path with
# evict-on-new-document, mirroring _PIKEPDF_CACHE (one document per parse
# subprocess). The cross-run Prometheus counter (DI_OCR_PAGES_TOTAL)
# lives in the DI client itself.
_DI_PAGE_BUDGET_ENV = "AZURE_DI_MAX_PAGES_PER_DOC"
_DI_PAGES_USED: dict[str, int] = {}
_DI_CAP_LOGGED: set[str] = set()

#: Documents whose DI budget ran out, so the parse result can say so.
#:
#: Hitting the cap is not an error — it is the cost control working. What was
#: wrong is that it was invisible: a 400-page scanned NI 43-101 had pages
#: 1-300 read by Document Intelligence and pages 301-400 read by tesseract,
#: which extracts no table structure at all. The two halves of one document
#: were extracted by different engines to different standards, the tail lost
#: every assay and resource table it contained, and the only trace was a
#: single WARNING line in a log with no alert rule attached to it.
#:
#: `_di_budget_warning` turns that into a warning on the parse result, which
#: travels the same channel as every other page warning: counted into
#: `warnings_count`, and enough on its own to land the ingestion run in
#: `partial` rather than `completed`, where the UI shows it.
_DI_CAP_EXHAUSTED: dict[str, dict[str, int]] = {}

#: How many documents' budgets stay resident. See the eviction note in
#: _di_budget_take.
_DI_BUDGET_REGISTRY_MAX = 32


def _di_max_pages_per_doc() -> int:
    try:
        return int(os.environ.get(_DI_PAGE_BUDGET_ENV, "300"))
    except ValueError:
        return 300


def _di_budget_take(pdf_path: str, pages: int = 1) -> bool:
    """Consume ``pages`` from the document's DI budget; False when exhausted.

    Perf audit 2026-08-15: guarded by _PIKEPDF_LOCK (shared with the pikepdf
    cache above — see its docstring) now that the per-page OCR loop calls
    this from multiple concurrent threads. The check-and-increment must be
    atomic: without the lock, two threads could both read
    _DI_PAGES_USED[pdf_path] before either writes it back, letting the
    budget overrun by up to (concurrency - 1) pages, and both could race on
    the "log once" _DI_CAP_LOGGED set.
    """
    with _PIKEPDF_LOCK:
        if pdf_path not in _DI_PAGES_USED:
            # Bounded FIFO, not "clear everything the moment a new document
            # appears". A Hatchet worker runs several ingest_pdf tasks in one
            # process, and the old eviction meant document B's first page
            # wiped document A's counter: A's remaining pages then drew a
            # fresh 300-page budget, so the per-document cap could be
            # overrun by a multiple of itself, and A's exhaustion record was
            # gone before its own parse could report it. Thirty-two entries
            # is thirty-two ints; the memory this was guarding was never the
            # constraint.
            while len(_DI_PAGES_USED) >= _DI_BUDGET_REGISTRY_MAX:
                _stale = next(iter(_DI_PAGES_USED))
                _DI_PAGES_USED.pop(_stale, None)
                _DI_CAP_LOGGED.discard(_stale)
                _DI_CAP_EXHAUSTED.pop(_stale, None)
            _DI_PAGES_USED[pdf_path] = 0
        cap = _di_max_pages_per_doc()
        if _DI_PAGES_USED[pdf_path] + pages > cap:
            if pdf_path not in _DI_CAP_LOGGED:
                _DI_CAP_LOGGED.add(pdf_path)
                _DI_CAP_EXHAUSTED[pdf_path] = {
                    "used": _DI_PAGES_USED[pdf_path],
                    "cap": cap,
                }
                logger.warning(
                    "pdf_report: document_intelligence page budget exhausted for "
                    "'%s' (used=%d, cap=%d via %s) — remaining short pages fall "
                    "back to tesseract",
                    pdf_path, _DI_PAGES_USED[pdf_path], cap, _DI_PAGE_BUDGET_ENV,
                )
            return False
        _DI_PAGES_USED[pdf_path] += pages
        return True


def _di_budget_warning(pdf_path: str) -> dict[str, Any] | None:
    """A parse-result warning when this document exhausted its DI budget.

    Returns None when the budget was never hit, which is the usual case —
    the default cap is 300 pages and most reports are shorter.
    """
    with _PIKEPDF_LOCK:
        record = _DI_CAP_EXHAUSTED.get(pdf_path)
    if not record:
        return None

    return {
        "code": "document_intelligence_page_budget_exhausted",
        "cap": record["cap"],
        "env": _DI_PAGE_BUDGET_ENV,
        "message": (
            f"Document Intelligence was capped at {record['cap']} page(s) for "
            f"this document ({_DI_PAGE_BUDGET_ENV}). Pages past the cap were "
            f"read by tesseract, which extracts no table structure — any "
            f"assay, resource or drill table in them was lost. Raise the cap "
            f"for this document or split it, then re-ingest."
        ),
    }


def _tesseract_data_to_words_and_text(data: dict) -> tuple[list[tuple[str, int]], str]:
    """2026-08-14 — rebuild line structure from ``image_to_data`` output.

    The old code joined every accepted word with a single space, producing
    one giant line per page. SECTION_HEADING_RE (and the other structural
    regexes) are ``^...$`` MULTILINE, so headings never matched on OCR'd
    pages — parse_quality sat at 0 and every chunk was labeled "Document".

    Groups accepted words by ``(block_num, par_num, line_num)`` and joins
    lines with ``\\n``. Falls back to a single line when the grouping
    columns are absent (defensive: malformed output, legacy fixtures).

    Returns ``(words, text)`` where ``words`` is ``[(word, conf_int)]`` in
    reading order — identical filtering to the old inline comprehension
    (non-empty, confidence >= 0), so confidence math is unchanged.
    """
    raw_words = data.get("text", []) or []
    raw_confs = data.get("conf", []) or []
    n = len(raw_words)
    blocks = data.get("block_num") or [0] * n
    pars = data.get("par_num") or [0] * n
    line_nums = data.get("line_num") or [0] * n

    words: list[tuple[str, int]] = []
    line_texts: list[str] = []
    current_key: tuple | None = None
    current_words: list[str] = []
    for w, c, blk, par, ln in zip(
        raw_words, raw_confs, blocks, pars, line_nums, strict=False,
    ):
        if not (w and str(w).strip()):
            continue
        try:
            conf = int(c)
        except (TypeError, ValueError):
            continue
        if conf < 0:
            continue
        token = str(w).strip()
        words.append((token, conf))
        key = (blk, par, ln)
        if key != current_key:
            if current_words:
                line_texts.append(" ".join(current_words))
            current_words = []
            current_key = key
        current_words.append(token)
    if current_words:
        line_texts.append(" ".join(current_words))
    return words, "\n".join(line_texts)


def _di_single_page_request(_di, pdf_path: str, page_num: int):
    """One page, one Document Intelligence request.

    Slices the target page into its own PDF before upload. Two reasons
    this is not an optimisation but a correctness fix:
      (1) Document Intelligence F0 rejects ``pages=N`` for N > 2
          ("InvalidRequest") — a 1-page document sidesteps the free
          tier's first-two-pages analysis window entirely;
      (2) sending the full file per page uploads O(size x pages) bytes —
          a 40 MB / 200-page report would push ~8 GB.
    F11: the source doc is opened once per path (cached), and a slice
    failure does NOT fall back to a whole-file upload — one structural
    defect must not turn a 300-page doc into 300 full-file uploads. It
    returns a failed PageOcrResult instead, so the caller falls through
    to tesseract for this page.
    """
    try:
        pdf_bytes = _slice_single_page_pdf_bytes(pdf_path, page_num)
    except Exception as slice_exc:  # noqa: BLE001
        logger.warning(
            "pdf_report: single-page slice failed for page %d of '%s' "
            "(%s) — skipping document_intelligence, falling back to "
            "tesseract",
            page_num, pdf_path, slice_exc,
        )
        return _di.PageOcrResult(
            "",
            0.0,
            request_succeeded=False,
            error=f"single_page_slice_failed: {slice_exc}",
        )
    return _di.ocr_page_sync(pdf_bytes, 1)


def _di_block_plan(total_pages: int, block_size: int) -> list[tuple[int, int]]:
    """Split a page count into ``(first_page, page_count)`` blocks."""
    return [
        (first, min(block_size, total_pages - first + 1))
        for first in range(1, total_pages + 1, block_size)
    ]


def _ocr_page_block_di(pdf_path: str, first_page: int, page_count: int) -> dict[int, Any]:
    """OCR one contiguous block of pages in a single DI request.

    Returns ``{absolute_page_number: PageOcrResult}``. An empty mapping
    means the block never produced an answer — budget exhausted, the
    slice failed, or the request failed — and the caller must drive those
    pages individually. A page the block *did* answer for but with no
    text is present with empty text, which is a different (and cheaper to
    recover from) situation.
    """
    from . import document_intelligence_client as _di

    if not _di_budget_take(pdf_path, page_count):
        return {}
    try:
        pdf_bytes = _slice_page_block_pdf_bytes(pdf_path, first_page, page_count)
    except Exception as slice_exc:  # noqa: BLE001
        logger.warning(
            "pdf_report: page-block slice failed for pages %d-%d of '%s' (%s) "
            "— falling back to per-page document_intelligence",
            first_page, first_page + page_count - 1, pdf_path, slice_exc,
        )
        return {}
    block = _di.ocr_page_block_sync(pdf_bytes, page_count)
    # DI numbers pages relative to the uploaded block, which starts at 1.
    return {first_page + local - 1: result for local, result in block.items()}


def _ocr_single_page(
    pdf_path: str,
    page_num: int,
    return_confidence: bool = False,
    return_assessment: bool = False,
    return_tables: bool = False,
    *,
    skip_di_page_request: bool = False,
):
    """Render one PDF page and run Tesseract on it.

    Phase 3 (2026-05-22): when `return_confidence=True`, returns
    ``(text, mean_confidence)`` where mean_confidence is the average
    of per-word confidences reported by Tesseract (rescaled 0.0–1.0).
    When `return_confidence=False` (legacy default), returns just
    ``text`` for back-compatibility with the existing pdfplumber
    fallback path.

    When ``return_assessment=True``, a third value contains the serialized
    multi-signal quality assessment used by review routing.

    Scanned-table support (2026-08-11): when ``return_tables=True``, one
    extra trailing element carries the Document Intelligence table grids
    (``tables[t][row][col]``, see PageOcrResult.tables) — always ``[]``
    on the tesseract/tiled/failure paths, which extract no tables.

    Batching 2026-08-20: ``skip_di_page_request=True`` means a batched
    block already asked Document Intelligence about this page and got
    nothing back. Re-asking page-at-a-time would burn a second billed
    page to get the same empty answer, so the DI branch below starts at
    the escalation it would have reached anyway (bounded raster tiles,
    then tesseract). The block already paid this page's budget, so it is
    not charged again either.

    Returns ``""`` (or the corresponding empty tuple) on any failure.
    """
    from . import document_intelligence_client as _di

    di_selected = _di.is_engine_selected()
    # 2026-08-14 — per-document DI page budget (AZURE_DI_MAX_PAGES_PER_DOC,
    # default 300). Beyond it, this page skips DI and goes straight to the
    # tesseract fallback below.
    if di_selected and not skip_di_page_request and not _di_budget_take(pdf_path):
        di_selected = False
    if di_selected:
        try:
            if skip_di_page_request:
                # Succeeded-but-empty is exactly what the batched block
                # reported, so reuse that sentinel and let the branch
                # below escalate to tiles without a second DI request.
                result = _di.PageOcrResult("", 0.0)
            else:
                result = _di_single_page_request(_di, pdf_path, page_num)
        except _di.DocumentIntelligenceNotConfigured as exc:
            # A configuration error, not a page that would not OCR. Its own
            # docstring calls it "a startup/config error a caller should
            # surface loudly rather than swallow", and both call sites
            # swallowed it into a generic except and fell through to
            # tesseract — so a rotated docintel-key silently downgraded the
            # ENTIRE corpus to the fallback engine, losing every table, and
            # nothing said so. CRITICAL because CRITICAL now pages
            # (georag-fastapi-critical, added 2026-08-21).
            logger.critical(
                "pdf_report: OCR_ENGINE selects Document Intelligence but it "
                "is not configured (%s). EVERY page from now on falls back to "
                "tesseract, which extracts no tables. Check the docintel-key "
                "secret reference on the worker.",
                exc,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "pdf_report: document_intelligence OCR failed on page %d of "
                "'%s': %s — falling back to tesseract",
                page_num, pdf_path, exc,
            )
        else:
            if not result.request_succeeded:
                # Transport/throttle failure (NOT merely empty text): the
                # tiled escalation below would fire 4+ more doomed DI calls
                # against the same broken endpoint — go straight to the
                # tesseract fallback instead.
                logger.warning(
                    "pdf_report: di_request_failed on page %d of '%s': %s — "
                    "skipping tiled escalation, falling back to tesseract",
                    page_num, pdf_path, result.error or "unknown error",
                )
            else:
                # `ocr_page`/`ocr_page_sync` fail soft internally (e.g. Azure's
                # InvalidContentDimensions on an out-of-range scan resolution —
                # confirmed against a real 1940s-era TIFF in the corpus 2026-07-29)
                # and return an empty PageOcrResult rather than raising. Without
                # this check, that soft failure would look identical to "page is
                # genuinely blank" and skip tesseract entirely, silently losing
                # a page tesseract might actually be able to read.
                #
                # 2026-08-21 — escalation used to key on `result.text.strip()`
                # alone, so it fired on an EMPTY page and never on an UNUSABLE
                # one. The assessment was computed on the line above and then
                # only attached to the return value: a page that came back as
                # fragmented tokens at catastrophic confidence — the ordinary
                # outcome for a skewed 1970s plan sheet — was accepted, stored,
                # embedded and cited, while the escalation that exists for
                # exactly that page sat one branch away. The router already has
                # a word for it, `catastrophic_failure`; this now reads it.
                di_assessment: dict[str, Any] | None = None
                if result.text.strip():
                    di_assessment = _assess_ocr_result(
                        result.text,
                        [word.confidence for word in result.words]
                        or [result.mean_confidence],
                        detected_region_count=result.detected_region_count,
                        ocr_method="document_intelligence",
                    )
                    if not _is_catastrophic_assessment(di_assessment):
                        return _format_ocr_page_return(
                            result.text,
                            result.mean_confidence,
                            di_assessment,
                            return_confidence=return_confidence,
                            return_assessment=return_assessment,
                            return_tables=return_tables,
                            tables=result.tables,
                        )
                    logger.info(
                        "pdf_report: document_intelligence returned "
                        "catastrophic-tier text for page %d of '%s' (%s) — "
                        "trying bounded raster tiles",
                        page_num, pdf_path,
                        ", ".join(di_assessment.get("reasons") or ()) or "no reason",
                    )
                else:
                    logger.info(
                        "pdf_report: document_intelligence returned empty text for "
                        "page %d of '%s' — trying bounded raster tiles",
                        page_num, pdf_path,
                    )

                tiled_result = None
                tiled_assessment: dict[str, Any] | None = None
                try:
                    tiled_result, tiled_assessment = _ocr_tiled_pdf_page(
                        pdf_path,
                        page_num,
                    )
                except Exception as tiled_exc:  # noqa: BLE001
                    logger.warning(
                        "pdf_report: tiled document_intelligence OCR failed on "
                        "page %d of '%s': %s — falling back to tesseract",
                        page_num,
                        pdf_path,
                        tiled_exc,
                    )

                if tiled_result is not None and tiled_result.text.strip():
                    # When DI came back EMPTY, any tiled text is an
                    # improvement and is taken unconditionally — that is the
                    # pre-existing contract and it stays. When DI came back
                    # unusable, tiles have to actually beat it to displace it.
                    if di_assessment is None or not _is_catastrophic_assessment(
                        tiled_assessment
                    ):
                        # Tiled reconstruction is word-level only — no
                        # table grids survive tiling, so tables stays [].
                        return _format_ocr_page_return(
                            tiled_result.text,
                            tiled_result.mean_confidence,
                            tiled_assessment,
                            return_confidence=return_confidence,
                            return_assessment=return_assessment,
                            return_tables=return_tables,
                        )

                if di_assessment is not None:
                    # Tiles did no better. Keep Document Intelligence's own
                    # read rather than falling through to tesseract: it is
                    # poor, but it is the stronger engine's answer for this
                    # page and it is the only one of the two that carries
                    # table structure. It travels with its catastrophic tier,
                    # so the quality router still routes it to review and
                    # retrieval still demotes it.
                    logger.warning(
                        "pdf_report: page %d of '%s' stays at catastrophic OCR "
                        "quality after tiled escalation — keeping the "
                        "document_intelligence result and routing it to review",
                        page_num, pdf_path,
                    )
                    return _format_ocr_page_return(
                        result.text,
                        result.mean_confidence,
                        di_assessment,
                        return_confidence=return_confidence,
                        return_assessment=return_assessment,
                        return_tables=return_tables,
                        tables=result.tables,
                    )

    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError:
        # Truthful provenance: when the DI branch already ran and came back
        # empty, this empty page is a DI result; otherwise tesseract simply
        # is not installed.
        return _empty_ocr_page_return(
            return_confidence,
            return_assessment,
            method="document_intelligence" if di_selected else "unavailable",
            return_tables=return_tables,
        )
    try:
        images = convert_from_path(
            pdf_path,
            dpi=250,
            first_page=page_num,
            last_page=page_num,
            thread_count=1,
        )
        if not images:
            return _empty_ocr_page_return(
                return_confidence, return_assessment, return_tables=return_tables,
            )
        processed = _preprocess_image_for_ocr(images[0])
        # Phase 3: image_to_data carries per-word confidence in the
        # `conf` column (range -1..100, where -1 = no detection).
        # Compute the mean of positive confidences and rescale to 0-1.
        if return_confidence or return_assessment:
            try:
                data = pytesseract.image_to_data(
                    processed,
                    lang="eng",
                    config="--psm 3 --oem 3",
                    output_type=pytesseract.Output.DICT,
                )
                # strict=False (inside the helper) deliberately:
                # pytesseract's DICT output should give equal-length
                # text/conf lists, but a malformed OCR result must degrade
                # to fewer words rather than raise mid-ingest. 2026-08-14 —
                # text is now line-structured (grouped by block/par/line)
                # so section headings survive OCR; see the helper.
                words, text = _tesseract_data_to_words_and_text(data)
                if words:
                    mean_conf = sum(c for _w, c in words) / len(words) / 100.0
                    mean_conf = max(0.0, min(1.0, mean_conf))
                else:
                    mean_conf = 0.0
                processed_text = (
                    _postprocess_ocr_text(text)
                    if text and text.strip()
                    else ""
                )
                assessment = _assess_ocr_result(
                    processed_text,
                    [confidence / 100.0 for _word, confidence in words],
                    detected_region_count=len(data.get("text", [])),
                    ocr_method="tesseract",
                )
                return _format_ocr_page_return(
                    processed_text,
                    mean_conf,
                    assessment,
                    return_confidence=return_confidence,
                    return_assessment=return_assessment,
                    return_tables=return_tables,
                )
            except Exception as conf_exc:  # noqa: BLE001
                logger.debug(
                    "pdf_report: tesseract confidence capture failed on page "
                    "%d (%s) — falling back to text-only",
                    page_num, conf_exc,
                )
                # Fall through to legacy image_to_string path below
        text = pytesseract.image_to_string(
            processed,
            lang="eng",
            config="--psm 3 --oem 3",
        )
        out_text = _postprocess_ocr_text(text) if text and text.strip() else ""
        # When confidence was requested but image_to_data raised, return
        # 0.0 to signal "unknown" rather than fabricating a number.
        assessment = _assess_ocr_result(
            out_text,
            [],
            detected_region_count=0,
            ocr_method="tesseract",
        )
        return _format_ocr_page_return(
            out_text,
            0.0,
            assessment,
            return_confidence=return_confidence,
            return_assessment=return_assessment,
            return_tables=return_tables,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pdf_report: per-page OCR failed on page %d of '%s': %s",
            page_num,
            pdf_path,
            exc,
        )
        return _empty_ocr_page_return(
            return_confidence, return_assessment, return_tables=return_tables,
        )


def _ocr_tiled_pdf_page(pdf_path: str, page_num: int):
    """Render one PDF page and OCR it as bounded, overlapping image tiles."""

    from pdf2image import convert_from_path

    from . import document_intelligence_client as _di
    from .image_tiling import (
        TileWord,
        encode_tile_png,
        reconstruct_words,
        split_image,
    )

    images = convert_from_path(
        pdf_path,
        dpi=250,
        first_page=page_num,
        last_page=page_num,
        thread_count=1,
    )
    if not images:
        return _di.PageOcrResult("", 0.0), _assess_ocr_result(
            "",
            [],
            detected_region_count=0,
        )

    tiles = split_image(images[0])
    # 2026-08-14 — each tile is one billed DI page; charge them against the
    # per-document budget. When it can't cover the whole tile set, raise so
    # the caller's except falls through to tesseract (a partial tile pass
    # would be rejected anyway — see the partial-failure guard below).
    if not _di_budget_take(pdf_path, pages=len(tiles)):
        raise RuntimeError(
            "document_intelligence per-document page budget exhausted "
            "before tiled OCR"
        )
    tile_words: list[TileWord] = []
    detected_region_count = 0
    fallback_tile_texts: list[str] = []

    for tile in tiles:
        result = _di.ocr_image_sync(encode_tile_png(tile))
        if not result.request_succeeded:
            raise RuntimeError(
                f"document_intelligence tile {tile.tile_id} failed: "
                f"{result.error or 'unknown error'}"
            )
        if result.text.strip() and (
            not result.words or any(not word.polygon for word in result.words)
        ):
            raise RuntimeError(
                f"document_intelligence tile {tile.tile_id} returned text "
                "without complete word polygons"
            )
        detected_region_count += result.detected_region_count
        if result.text.strip():
            fallback_tile_texts.append(result.text.strip())
        tile_words.extend(
            TileWord(
                text=word.text,
                confidence=word.confidence,
                polygon=word.polygon,
                tile_id=tile.tile_id,
            )
            for word in result.words
            if word.polygon
        )

    reconstruction = reconstruct_words(tiles, tile_words)
    text = reconstruction.text or " ".join(fallback_tile_texts).strip()
    confidences = [word.confidence for word in reconstruction.words]
    mean_confidence = (
        statistics.fmean(confidences)
        if confidences
        else 0.0
    )
    assessment = _assess_ocr_result(
        text,
        confidences,
        detected_region_count=detected_region_count,
        seam_duplicate_count=reconstruction.seam_duplicate_count,
        ocr_method="document_intelligence",
    )
    words = tuple(
        _di.OcrWord(word.text, word.confidence, word.polygon)
        for word in reconstruction.words
    )
    logger.info(
        "pdf_report: tiled document_intelligence page=%d tiles=%d words=%d "
        "seam_duplicates=%d quality_tier=%s",
        page_num,
        len(tiles),
        len(words),
        reconstruction.seam_duplicate_count,
        assessment["tier"],
    )
    return (
        _di.PageOcrResult(
            text=text,
            mean_confidence=mean_confidence,
            words=words,
            detected_region_count=detected_region_count,
        ),
        assessment,
    )


def _assess_ocr_result(
    text: str,
    word_confidences: list[float],
    *,
    detected_region_count: int,
    seam_duplicate_count: int = 0,
    ocr_method: str | None = None,
) -> dict[str, Any]:
    """Score one page's OCR output and route it.

    ``ocr_method`` names the engine that produced ``text``. It does two
    things, and the first is new as of 2026-08-21: it selects the engine's
    threshold set, if OCR_ROUTING_THRESHOLDS_JSON supplies one. Document
    Intelligence and Tesseract do not report confidence on a comparable
    scale -- DI sits at 0.95-0.99 even when confidently wrong, Tesseract at
    0.70-0.85 when it is fine -- so one shared cut-off auto-accepts nearly
    every DI page and reviews nearly every Tesseract page.

    Second, it lands in the returned dict, which every caller used to do
    itself on the following line. Doing it here means a caller cannot
    forget, and a review_queue row cannot arrive without naming its engine.
    """
    from .ocr_quality import (
        assess_ocr_quality,
        calculate_ocr_quality,
        load_routing_thresholds_from_env,
    )

    signals = calculate_ocr_quality(
        text,
        word_confidences,
        detected_region_count=detected_region_count,
        seam_duplicate_count=seam_duplicate_count,
    )
    assessment = assess_ocr_quality(
        signals,
        load_routing_thresholds_from_env(ocr_method),
    )
    result = {
        "tier": assessment.tier.value,
        "routing_decision": assessment.review_queue_routing_decision,
        "reasons": list(assessment.reasons),
        "thresholds_calibrated": assessment.thresholds_calibrated,
        "signals": {
            "mean_confidence": signals.mean_confidence,
            "median_confidence": signals.median_confidence,
            "low_confidence_word_ratio": signals.low_confidence_word_ratio,
            "output_coverage_ratio": signals.output_coverage_ratio,
            "empty_output": signals.empty_output,
            "seam_duplicate_ratio": signals.seam_duplicate_ratio,
            "gibberish_word_ratio": signals.gibberish_word_ratio,
            "repeated_character_ratio": signals.repeated_character_ratio,
            "word_count": signals.word_count,
            "detected_region_count": signals.detected_region_count,
        },
    }
    if ocr_method:
        result["ocr_method"] = ocr_method
    return result


def _is_catastrophic_assessment(assessment: dict[str, Any] | None) -> bool:
    """True when the quality router judged this OCR output unusable.

    `CatastrophicFailure` is reached by exactly three routes in
    ocr_quality.assess_ocr_quality: empty output, mean confidence at or
    below `catastrophic_max_mean_confidence`, or output coverage at or
    below `catastrophic_max_coverage_ratio`. The latter two are only
    evaluated when routing thresholds are calibrated
    (OCR_ROUTING_THRESHOLDS_JSON), so on an uncalibrated deployment this
    reduces to "empty" and the escalation ladder behaves exactly as it did
    before 2026-08-21.

    Deliberately NOT `MandatoryReview`: an uncalibrated deployment routes
    every page there by design ("defaulting safely to review"), and
    escalating on it would fire the tiled path -- four or more billed
    Document Intelligence requests -- on every page of every document.
    """
    if not assessment:
        return False

    from .ocr_quality import OcrRoutingTier

    return assessment.get("tier") == OcrRoutingTier.CatastrophicFailure.value


def _ocr_quality_warning(
    *,
    page_number: int,
    text: str,
    assessment: dict[str, Any],
) -> dict[str, Any]:
    """Build the persisted page-quality warning for every OCR attempt."""

    # Truncate the text excerpt: warnings travel through Hatchet task
    # outputs, and shipping every full page (~1MB on a 300-page scanned
    # doc) duplicates text that is already persisted elsewhere.
    excerpt = text if len(text) <= 512 else text[:512] + "…[truncated]"
    return {
        "code": "ocr_quality_assessment",
        "page": page_number,
        "parser_version": PARSER_VERSION,
        "ocr_method": str(assessment.get("ocr_method") or "unknown"),
        "extracted_text": excerpt,
        **{
            key: value
            for key, value in assessment.items()
            if key != "ocr_method"
        },
    }


def _format_ocr_page_return(
    text: str,
    mean_confidence: float,
    assessment: dict[str, Any],
    *,
    return_confidence: bool,
    return_assessment: bool,
    return_tables: bool = False,
    tables: list[list[list[str]]] | None = None,
):
    if return_assessment:
        out: Any = (text, mean_confidence, assessment)
    elif return_confidence:
        out = (text, mean_confidence)
    else:
        out = text
    if return_tables:
        # Scanned-table support (2026-08-11) — append the DI table grids
        # as one extra trailing element, leaving every legacy tuple shape
        # untouched for callers that don't opt in.
        if not isinstance(out, tuple):
            out = (out,)
        return (*out, list(tables) if tables else [])
    return out


def _empty_ocr_page_return(
    return_confidence: bool,
    return_assessment: bool,
    method: str = "tesseract",
    return_tables: bool = False,
):
    assessment = _assess_ocr_result("", [], detected_region_count=0)
    assessment["ocr_method"] = method
    return _format_ocr_page_return(
        "",
        0.0,
        assessment,
        return_confidence=return_confidence,
        return_assessment=return_assessment,
        return_tables=return_tables,
    )


# Parallel pdfplumber page worker (must be module-level for multiprocessing
# pickling). Each subprocess opens the PDF independently, extracts ONE
# page, optionally OCRs it, and returns a small result tuple. Cheap to
# fan out — typical NI 43-101 page is ~10-30ms of pdfplumber work, so
# 4-8 workers parallelize the bottleneck cleanly.
def _extract_page_worker(args: tuple) -> dict:
    """Process a single PDF page. Returns a dict ready to fold back into
    the per-page accumulators in `_parse_with_pdfplumber`.
    """
    pdf_path, page_num, ocr_fallback_enabled = args
    out = {
        "page_num": page_num,
        "text": "",
        "lang": "unknown",
        "warnings": [],
        "two_column": False,
        "ocr_recovered": False,
    }
    try:
        import pdfplumber as _pp  # noqa: PLC0415
        with _pp.open(pdf_path) as pdf:
            page = pdf.pages[page_num - 1]
            text = _extract_text_column_aware(page) or ""
            if _detect_page_columns(page) == 2:
                out["two_column"] = True
                out["warnings"].append({
                    "code": "two_column_layout_detected",
                    "page": page_num,
                })

            if ocr_fallback_enabled and len(text.strip()) < PER_PAGE_MIN_CHARS:
                ocr_text, _mean_conf, assessment = _ocr_single_page(
                    pdf_path,
                    page_num,
                    return_confidence=True,
                    return_assessment=True,
                )
                out["warnings"].append(
                    _ocr_quality_warning(
                        page_number=page_num,
                        text=ocr_text,
                        assessment=assessment,
                    )
                )
                if len(ocr_text.strip()) > len(text.strip()):
                    text = ocr_text
                    out["ocr_recovered"] = True
                    out["warnings"].append({
                        "code": "page_ocr_recovered",
                        "page": page_num,
                    })

            if text and text.strip():
                out["text"] = text
                out["lang"] = _detect_page_language(text)
    except Exception as e:
        out["warnings"].append({
            "code": "pdf_extraction_partial",
            "page": page_num,
            "message": str(e),
        })
    return out


def _parse_with_pdfplumber(
    path: str,
) -> tuple[str, str, int, list, list[str], list[tuple[int, str]]]:
    """Extract full text using pdfplumber as a fallback.

    Returns:
        (full_text, document_title, skipped_elements, page_warnings,
         page_languages, per_page_text)

    Parallelized across CPU cores via multiprocessing.Pool for big PDFs.
    Tunable via PDF_PARSE_PAGE_WORKERS env (default: min(8, cpu_count())).
    Set to 1 to disable parallelism for debugging.
    """
    import pdfplumber  # noqa: PLC0415

    pages_text: list[str] = []
    per_page_text: list[tuple[int, str]] = []
    page_warnings: list[dict] = []
    page_languages: list[str] = []

    # Per-page OCR fallback is ALWAYS enabled — an earlier optimization
    # used a first-10-pages "is doc text-dense" probe and disabled OCR
    # for the whole document if true. That dropped data: NI 43-101 PDFs
    # routinely have a text-dense front (cover/TOC/letter) and scanned
    # drill-log pages at page 100+ that need OCR. Per-page check is
    # cheap (just a length test on already-extracted text), so we run
    # it unconditionally now. 2026-05-22.
    ocr_fallback_enabled = True
    with pdfplumber.open(path) as pdf:
        total_pages = len(pdf.pages)

    # Decide page-worker count. The parse task already runs in its own
    # subprocess (heartbeat-safety) so this is a NESTED pool — use spawn
    # context to avoid forking inside the parse subprocess.
    #
    # Default = 4 page workers per parse. With Hatchet's 20-slot worker,
    # 2 concurrent parses × 4 page workers = 8 cores (matches typical
    # workstation). Override via PDF_PARSE_PAGE_WORKERS for headless
    # boxes with more cores, or set to 1 for serial debug.
    import multiprocessing as _mp
    cpu = _mp.cpu_count()
    env_workers = os.environ.get("PDF_PARSE_PAGE_WORKERS")
    if env_workers:
        try:
            n_workers = max(1, min(int(env_workers), cpu))
        except ValueError:
            n_workers = min(4, cpu)
    else:
        n_workers = min(4, cpu)
    # Very small PDFs aren't worth parallelizing.
    if total_pages <= 4:
        n_workers = 1

    args_list = [(path, n, ocr_fallback_enabled) for n in range(1, total_pages + 1)]
    page_results: list[dict] = []
    if n_workers <= 1:
        for a in args_list:
            page_results.append(_extract_page_worker(a))
    else:
        ctx = _mp.get_context("spawn")
        # imap (not imap_unordered) so results come back in page order
        # — avoids a sort step and keeps the per_page_text ordering
        # consistent with single-threaded behavior.
        with ctx.Pool(processes=n_workers) as pool:
            for r in pool.imap(_extract_page_worker, args_list, chunksize=4):
                page_results.append(r)

    ocr_recovered_count = 0
    for r in page_results:
        n = r["page_num"]
        text = r["text"]
        page_warnings.extend(r.get("warnings", []))
        if r.get("ocr_recovered"):
            ocr_recovered_count += 1
        if text and text.strip():
            pages_text.append(text)
            per_page_text.append((n, text))
            page_languages.append(r.get("lang", "unknown"))
        else:
            page_languages.append("unknown")

    if n_workers > 1:
        logger.info(
            "pdf_report: parallel pdfplumber (n=%d) processed %d pages, "
            "%d OCR-recovered", n_workers, total_pages, ocr_recovered_count,
        )
    elif ocr_recovered_count:
        logger.info(
            "pdf_report: serial pdfplumber processed %d pages, %d OCR-recovered",
            total_pages, ocr_recovered_count,
        )

    full_text = "\n".join(pages_text)

    # Derive a title from the first non-empty line
    title_candidate = ""
    for line in full_text.splitlines():
        line = line.strip()
        if line:
            title_candidate = line[:200]
            break

    return full_text, title_candidate, 0, page_warnings, page_languages, per_page_text


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

#: How far off-square a scan may be and still be worth correcting. A 1970s
#: plan sheet fed through a flatbed routinely lands 2-5 degrees out; beyond
#: about 8 the page is probably rotated rather than skewed, which is a
#: different problem and not one a shear correction fixes.
_MAX_DESKEW_DEGREES = 8.0

#: Below this the rotation costs more in resampling blur than it recovers.
_MIN_DESKEW_DEGREES = 0.3

#: Width the skew search runs at. The angle of a page does not depend on its
#: resolution, and searching 33 angles on a 5,000 px raster is seconds of
#: pointless work per page.
_DESKEW_SEARCH_WIDTH_PX = 1000


def _estimate_skew_degrees(binary) -> float:
    """Estimate a page's skew from its horizontal projection profile.

    When text lines are horizontal, summing dark pixels across each row
    produces sharp peaks at the lines and near-zero between them, so the
    profile has high variance. Tilt the page and the lines smear across
    rows, flattening the profile. So: rotate through candidate angles and
    keep the one whose profile varies most.

    Projection-profile rather than Hough: no OpenCV in this image, and this
    needs only numpy plus PIL's rotate.
    """
    import numpy as np
    from PIL import Image as PILImage

    height, width = binary.shape
    if width < 50 or height < 50:
        return 0.0

    # Search on a small copy. The angle is scale-invariant.
    if width > _DESKEW_SEARCH_WIDTH_PX:
        scale = _DESKEW_SEARCH_WIDTH_PX / width
        small = PILImage.fromarray(binary).resize(
            (_DESKEW_SEARCH_WIDTH_PX, max(1, int(height * scale))),
            PILImage.BILINEAR,
        )
    else:
        small = PILImage.fromarray(binary)

    def _profile_variance(angle: float) -> float:
        rotated = np.asarray(
            small.rotate(angle, resample=PILImage.BILINEAR, fillcolor=0)
        )
        return float(rotated.sum(axis=1, dtype=np.float64).var())

    # Upright is the incumbent, and a candidate has to beat it. Seeding the
    # search with -infinity instead means a page with a FLAT profile — blank,
    # or nearly so — is "won" by whichever angle happens to be tried first,
    # and a blank page comes back needing 8 degrees of correction.
    best_angle = 0.0
    best_score = _profile_variance(0.0)

    if best_score <= 0.0:
        return 0.0  # nothing on the page to align

    # 0.5 degree steps: finer than the resampling blur can reward, coarser
    # than the search cost justifies.
    angle = -_MAX_DESKEW_DEGREES
    while angle <= _MAX_DESKEW_DEGREES + 1e-9:
        score = _profile_variance(angle)
        if score > best_score:
            best_score, best_angle = score, angle
        angle += 0.5

    return best_angle


def _preprocess_image_for_ocr(img):
    """Preprocess a page image to maximize Tesseract accuracy.

    Steps:
      1. Convert to grayscale
      2. Upscale small images (below 2000px width)
      3. Binarize, for the skew estimate
      4. Deskew — straightens rotated scans
      5. Denoise — removes scanner speckle
      6. Sharpen

    All six of those used to be listed here and only two were implemented:
    the function converted to grayscale, upscaled, computed a `binary` array
    that was then discarded, carried a comment about denoising with no
    denoise code, and applied one SHARPEN. There was no deskew anywhere in
    the stack, and tesseract is invoked at every call site with `--psm 3`,
    which assumes upright text. A 4-degree scan therefore had every line
    straddling two text rows, and the output was fragmented tokens that
    `_assess_ocr_result` correctly tiered `mandatory_review`.

    Returns a PIL Image ready for pytesseract.
    """
    try:
        import numpy as np
    except ImportError:
        return img  # numpy not available, return as-is

    from PIL import Image as PILImage
    from PIL import ImageFilter

    gray = img.convert("L")
    arr = np.array(gray)

    # Upscale if too small (tesseract works best at 300+ DPI equivalent)
    height, width = arr.shape
    if width < 2000:
        scale = 2000 / width
        gray = gray.resize((int(width * scale), int(height * scale)), PILImage.LANCZOS)
        arr = np.array(gray)

    # Binarize. Used for the skew estimate, NOT fed to tesseract — which
    # does its own thresholding and does better on the grayscale original.
    # This array used to be computed, inverted, and dropped on the floor.
    threshold = arr.mean() * 0.85  # slightly below mean catches faint text
    binary = ((arr < threshold) * 255).astype(np.uint8)  # dark text -> white

    try:
        skew = _estimate_skew_degrees(binary)
    except Exception:  # noqa: BLE001 — a page must not fail to OCR over this
        skew = 0.0

    result = gray
    if abs(skew) >= _MIN_DESKEW_DEGREES:
        # White fill: the corners exposed by the rotation must read as page,
        # not as a black border tesseract will try to interpret.
        result = result.rotate(
            skew, resample=PILImage.BICUBIC, fillcolor=255, expand=True,
        )
        logger.debug("pdf_report: deskewed page by %.1f degrees", skew)

    # Real denoise, replacing the comment that described one. A 3x3 median
    # removes isolated scanner speckle while leaving stroke edges alone —
    # which is what morphological opening was meant to approximate.
    result = result.filter(ImageFilter.MedianFilter(size=3))

    # Sharpen to improve edge definition
    return result.filter(ImageFilter.SHARPEN)


def _postprocess_ocr_text(text: str) -> str:
    """Fix common OCR artifacts in geological text.

    Corrects known Tesseract misreadings for geological terms and cleans
    up formatting artifacts from the page rendering.
    """
    import re

    # Strip page markers injected by our OCR pipeline
    text = re.sub(r'^---\s*Page\s+\d+\s*---\n?', '', text, flags=re.MULTILINE)

    # Common geological OCR corrections
    corrections = {
        # Mineral/element misreads
        r'\bU3O8\b': 'U3O8',     # already correct, normalize case
        r'\bU308\b': 'U3O8',     # zero vs O
        r'\bu3o8\b': 'U3O8',
        r'\bU30s\b': 'U3O8',     # 8 → s
        r'\bAu\b': 'Au',
        r'\bCu\b': 'Cu',

        # QP title misreads
        r'\bP\.Gea\b': 'P.Geo.',
        r'\bP\.Ge0\b': 'P.Geo.',
        r'\bP\.Ceo\b': 'P.Geo.',
        r'\bP\. Geo\b': 'P.Geo.',
        r'\bP\.Eng\b': 'P.Eng.',
        r'\bP\.Eng,\b': 'P.Eng.',

        # NI 43-101 misreads
        r'\bNI 43-10[1l]\b': 'NI 43-101',
        r'\bN143-101\b': 'NI 43-101',
        r'\bNl 43-101\b': 'NI 43-101',  # l vs I

        # Common word misreads in geological text
        r'\btonncs\b': 'tonnes',
        r'\btonnes\s*at\b': 'tonnes at',
        r'\btonnesat\b': 'tonnes at',
        r'\bdrillhole\b': 'drill hole',
        r'\bdrillholes\b': 'drill holes',
        r'\bde posit\b': 'deposit',
        r'\bmin eral\b': 'mineral',
        r'\bmin eralization\b': 'mineralization',
        r'\bmin eralisation\b': 'mineralisation',
        r'\bun conformity\b': 'unconformity',
        r'\bre port\b': 'report',
        r'\bRe port\b': 'Report',
        r'\bpre pared\b': 'prepared',
        r'\bPre pared\b': 'Prepared',
        r'\bex ploration\b': 'exploration',
        r'\bEx ploration\b': 'Exploration',
        r'\bfor mation\b': 'formation',
        r'\bFor mation\b': 'Formation',
        r'\besti mate\b': 'estimate',
        r'\bEsti mate\b': 'Estimate',
        r'\btech nical\b': 'technical',
        r'\bTech nical\b': 'Technical',
        r'\bindi cated\b': 'indicated',
        r'\bIndi cated\b': 'Indicated',
        r'\binfer red\b': 'inferred',
        r'\bInfer red\b': 'Inferred',
    }

    for pattern, replacement in corrections.items():
        # Case-sensitive on purpose: patterns encode exact-case misreads
        # (e.g. 'Re port' vs 're port'), and IGNORECASE would corrupt
        # proper nouns (e.g. "Mount Isa" via a \bisa\b rule).
        text = re.sub(pattern, replacement, text, flags=0)

    # Clean up multiple blank lines
    text = re.sub(r'\n{3,}', '\n\n', text)

    # Clean up spaces before punctuation
    text = re.sub(r'\s+([.,;:])', r'\1', text)

    return text.strip()


def _attempt_ocr_document_intelligence(path: str) -> OcrAttemptResult:
    """Full-document OCR via Azure Document Intelligence, one call per page.

    Mirrors `_attempt_ocr`'s page-count discovery, no-page-cap policy, and
    progress/low-confidence logging so callers see the same log shape
    regardless of which engine produced the text — the only difference is
    no local rasterisation (no `convert_from_path`): Document Intelligence
    takes the raw PDF bytes directly per page.
    """
    from pdf2image import pdfinfo_from_path

    try:
        info = pdfinfo_from_path(path)
        total_pages = info.get("Pages", 0)
    except Exception:
        total_pages = 0

    if total_pages == 0:
        logger.warning(
            "pdf_report: could not determine page count for "
            "document_intelligence OCR; aborting to tesseract fallback"
        )
        raise RuntimeError("page count unavailable")

    logger.info(
        "pdf_report: starting document_intelligence OCR on %d pages "
        "(concurrency=%s)",
        total_pages, os.environ.get("PDF_OCR_PAGE_CONCURRENCY", "4"),
    )

    texts: list[str] = []
    page_confidences: list[float] = []
    low_confidence_pages: list[int] = []
    page_attempts: list[OcrPageAttempt] = []

    # Perf 2026-08-18 — this loop used to be strictly serial: one page's
    # full network round-trip to Document Intelligence, awaited, then the
    # next. On a fully scanned report (the ONLY case that reaches this
    # function) that made it the slowest path in the pipeline — a 300-page
    # document did 300 sequential round-trips at ~1-3 s each.
    #
    # The per-page fallback in `_parse_with_fitz` already solved this; this
    # path simply never got the same treatment. Same fix, same knob
    # (PDF_OCR_PAGE_CONCURRENCY), same safety argument: `_ocr_single_page`
    # blocks on network or on a tesseract subprocess, both of which release
    # the GIL, and its shared mutable state (the cached pikepdf handle and
    # the per-document DI page budget) is already guarded by _PIKEPDF_LOCK
    # precisely because the other path calls it from several threads.
    #
    # Only the OCR calls run concurrently. All bookkeeping below stays
    # sequential and in page order — executor.map yields results in INPUT
    # order regardless of completion order — so texts, page_attempts,
    # page_confidences and low_confidence_pages come out byte-identical to
    # the serial version.
    _ocr_concurrency = max(1, int(os.environ.get("PDF_OCR_PAGE_CONCURRENCY", "4")))

    # Batching 2026-08-20 — concurrency alone only hid the round-trip cost;
    # it didn't remove it. Document Intelligence analyzes a multi-page PDF
    # in one request (the one-page-per-request shape is a leftover from the
    # F0 tier's 2-page limit), so this pass asks for blocks first and only
    # falls back to per-page work for pages a block couldn't answer. On a
    # 200-page scan at the default block size that is 8 submissions instead
    # of 200. AZURE_DI_PAGES_PER_BATCH=1 disables it.
    from . import document_intelligence_client as _di_mod

    _block_size = _di_mod.pages_per_batch()
    _batched: dict[int, Any] = {}
    if _block_size > 1:
        _plan = _di_block_plan(total_pages, _block_size)
        logger.info(
            "pdf_report: document_intelligence batching %d pages into %d "
            "block(s) of up to %d",
            total_pages, len(_plan), _block_size,
        )
        with ThreadPoolExecutor(
            max_workers=max(1, min(_ocr_concurrency, len(_plan)))
        ) as _block_executor:
            for _mapping in _block_executor.map(
                lambda block: _ocr_page_block_di(path, block[0], block[1]), _plan
            ):
                _batched.update(_mapping)
        _batched_hits = sum(1 for _r in _batched.values() if _r.text.strip())
        logger.info(
            "pdf_report: document_intelligence batch pass resolved %d/%d "
            "pages; %d page(s) need individual handling",
            _batched_hits, total_pages, total_pages - _batched_hits,
        )

    def _ocr_one(page_num: int):
        try:
            batched = _batched.get(page_num)
            if batched is not None and batched.text.strip():
                assessment = _assess_ocr_result(
                    batched.text,
                    [word.confidence for word in batched.words]
                    or [batched.mean_confidence],
                    detected_region_count=batched.detected_region_count,
                    ocr_method="document_intelligence",
                )
                return page_num, (
                    batched.text,
                    batched.mean_confidence,
                    assessment,
                    list(batched.tables),
                ), None
            return page_num, _ocr_single_page(
                path,
                page_num,
                return_confidence=True,
                return_assessment=True,
                return_tables=True,
                # The block already asked DI about this page and got
                # nothing; skip the duplicate billed request and start at
                # the raster-tile escalation instead.
                skip_di_page_request=page_num in _batched,
            ), None
        except Exception as exc:  # noqa: BLE001
            # Fail soft per page, matching ocr_page_sync's own contract: one
            # bad page must not abort a 300-page document. The empty result
            # simply contributes nothing, and the "no text at all" guard
            # below still fires if every page fails.
            return page_num, None, exc

    _page_numbers = list(range(1, total_pages + 1))
    with ThreadPoolExecutor(max_workers=_ocr_concurrency) as _executor:
        _ocr_results = list(_executor.map(_ocr_one, _page_numbers))

    for page_num, _outcome, _exc in _ocr_results:
        if _outcome is None:
            # Second of the two call sites that swallowed a configuration
            # error as though it were a page-level OCR failure. See the
            # handler in _ocr_page_di for the reasoning.
            from app.services.ingest.document_intelligence_client import (  # noqa: PLC0415
                DocumentIntelligenceNotConfigured,
            )

            if isinstance(_exc, DocumentIntelligenceNotConfigured):
                logger.critical(
                    "pdf_report: OCR_ENGINE selects Document Intelligence but "
                    "it is not configured (%s). EVERY page falls back to "
                    "tesseract, which extracts no tables.",
                    _exc,
                )
            else:
                logger.warning(
                    "pdf_report: document_intelligence OCR failed on page %d of "
                    "'%s': %s", page_num, path, _exc,
                )
            page_text, mean_confidence, assessment, page_tables = (
                "", 0.0, _assess_ocr_result("", [], detected_region_count=0), [],
            )
        else:
            page_text, mean_confidence, assessment, page_tables = _outcome
        cleaned = _postprocess_ocr_text(page_text) if page_text.strip() else ""
        page_attempts.append(
            OcrPageAttempt(
                page_number=page_num,
                text=cleaned,
                mean_confidence=mean_confidence,
                assessment=assessment,
                tables=tuple(page_tables),
            )
        )
        if page_text.strip():
            page_confidences.append(mean_confidence)
            if assessment["routing_decision"] == "review_required":
                low_confidence_pages.append(page_num)
                logger.warning(
                    "pdf_report: OCR page %d quality tier=%s reasons=%s",
                    page_num,
                    assessment["tier"],
                    ",".join(assessment["reasons"]),
                )
            texts.append(cleaned)


    result_text = "\n\n".join(texts)
    avg_confidence = (
        sum(page_confidences) / len(page_confidences) if page_confidences else 0.0
    )
    logger.info(
        "pdf_report: document_intelligence OCR complete — %d pages, %d chars, "
        "avg confidence %.0f%%, %d low-confidence pages",
        total_pages, len(result_text), avg_confidence * 100,
        len(low_confidence_pages),
    )
    if not result_text.strip():
        # Every page came back empty — e.g. Azure's InvalidContentDimensions
        # on an out-of-range scan resolution (confirmed against a real
        # 1940s-era TIFF in the corpus 2026-07-29), or a full-document
        # outage. `ocr_page_sync` fails soft per page, so this loop never
        # raises on its own; raising here is what lets the caller's
        # try/except fall through to the tesseract path instead of
        # silently returning an empty document.
        raise RuntimeError(
            f"document_intelligence produced no text across {total_pages} pages"
        )
    output_methods = {
        str(page.assessment.get("ocr_method") or "unknown")
        for page in page_attempts
        if page.text.strip()
    }
    if output_methods == {"document_intelligence"}:
        parser_used = "ocr_document_intelligence"
    elif output_methods == {"tesseract"}:
        parser_used = "ocr_tesseract"
    else:
        parser_used = "ocr_mixed"
    return OcrAttemptResult(
        text=result_text,
        parser_used=parser_used,
        pages=tuple(page_attempts),
    )


def _attempt_ocr(path: str) -> OcrAttemptResult:
    """Attempt OCR on a scanned PDF using Tesseract via pdf2image + pytesseract.

    Strategy:
      1. Convert PDF pages to images at adaptive DPI (200 for speed, 300 for quality)
      2. OCR each page with Tesseract English language pack
      3. NO PAGE CAP — process every page so we don't silently drop data
      4. Log progress every 10 pages

    Returns extracted text, truthful engine provenance, and per-page quality.
    """
    from . import document_intelligence_client as _di

    if _di.is_engine_selected():
        try:
            return _attempt_ocr_document_intelligence(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "pdf_report: document_intelligence full-document OCR failed "
                "on '%s': %s — falling back to tesseract",
                path, exc,
            )

    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError:
        logger.info(
            "pdf_report: OCR libraries (pdf2image, pytesseract) not installed — "
            "install with: pip install pdf2image pytesseract"
        )
        return OcrAttemptResult("", "ocr_unavailable")

    # 2026-05-22 — removed the MAX_OCR_PAGES=100 cap. A 500-page scanned
    # NI 43-101 lost pages 101-500 silently before this change. The
    # user's no-data-loss requirement supersedes the perf concern. A
    # full 500-page OCR run is ~8 min at 1s/page; acceptable for the
    # ingestion pipeline (parse task's execution_timeout is 60m).
    OCR_DPI = 250          # balance between speed and accuracy

    try:
        # First pass: get page count without rendering
        from pdf2image import pdfinfo_from_path
        try:
            info = pdfinfo_from_path(path)
            total_pages = info.get("Pages", 0)
        except Exception:
            total_pages = 0

        pages_to_process = total_pages if total_pages > 0 else 0
        if pages_to_process == 0:
            logger.warning(
                "pdf_report: could not determine page count for OCR; "
                "rendering up to first 1000 pages defensively"
            )
            pages_to_process = 1000  # very generous fallback to avoid silent skips

        logger.info(
            "pdf_report: starting OCR on %d pages at %d DPI",
            pages_to_process, OCR_DPI,
        )

        # F18 (2026-08-11) — render in blocks of 10 pages instead of
        # materialising every page image at once (a whole-document render
        # of a long scanned report at 250 DPI is multiple GB of RAM).
        # Each block is rendered, processed page by page, then discarded
        # when the next block replaces it.
        _OCR_RENDER_BLOCK_PAGES = 10

        def _iter_page_images():
            for _block_start in range(
                1, pages_to_process + 1, _OCR_RENDER_BLOCK_PAGES,
            ):
                block = convert_from_path(
                    path,
                    dpi=OCR_DPI,
                    first_page=_block_start,
                    last_page=min(
                        _block_start + _OCR_RENDER_BLOCK_PAGES - 1,
                        pages_to_process,
                    ),
                    thread_count=2,  # parallel page rendering
                )
                if not block:
                    # Past the real last page (page-count probe failed and
                    # the defensive 1000-page ceiling overshot).
                    return
                yield from block

        texts = []
        page_confidences = []
        low_confidence_pages = []
        page_attempts: list[OcrPageAttempt] = []

        for i, img in enumerate(_iter_page_images()):
            # Preprocess image for better OCR accuracy
            processed_img = _preprocess_image_for_ocr(img)

            try:
                data = pytesseract.image_to_data(
                    processed_img,
                    lang="eng",
                    config="--psm 3 --oem 3",
                    output_type=pytesseract.Output.DICT,
                )
                detected_region_count = len(data.get("text", []))
                # 2026-08-14 — line-structured OCR text (grouped by
                # block/par/line) so section headings survive; see
                # _tesseract_data_to_words_and_text.
                detected_words, page_text = _tesseract_data_to_words_and_text(data)
                word_confidences = [
                    confidence / 100.0
                    for _word, confidence in detected_words
                ]
            except Exception:  # noqa: BLE001
                page_text = pytesseract.image_to_string(
                    processed_img,
                    lang="eng",
                    config="--psm 3 --oem 3",
                )
                word_confidences = []
                detected_region_count = 0

            # Post-process to fix common OCR artifacts, then assess even an
            # empty page so catastrophic OCR failures reach review routing.
            cleaned = _postprocess_ocr_text(page_text) if page_text.strip() else ""
            assessment = _assess_ocr_result(
                cleaned,
                word_confidences,
                detected_region_count=detected_region_count,
                ocr_method="tesseract",
            )
            page_attempts.append(
                OcrPageAttempt(
                    page_number=i + 1,
                    text=cleaned,
                    mean_confidence=float(
                        assessment["signals"]["mean_confidence"]
                    ),
                    assessment=assessment,
                )
            )
            if cleaned:
                conf = float(assessment["signals"]["mean_confidence"])
                page_confidences.append(conf)

                if assessment["routing_decision"] == "review_required":
                    low_confidence_pages.append(i + 1)
                    logger.warning(
                        "pdf_report: OCR page %d quality tier=%s reasons=%s",
                        i + 1,
                        assessment["tier"],
                        ",".join(assessment["reasons"]),
                    )

                texts.append(cleaned)

            if (i + 1) % 10 == 0 or i == 0:
                logger.info(
                    "pdf_report: OCR progress %d/%d pages",
                    i + 1, pages_to_process,
                )

        result = "\n\n".join(texts)
        avg_confidence = (
            sum(page_confidences) / len(page_confidences)
            if page_confidences else 0.0
        )
        logger.info(
            "pdf_report: OCR complete — %d pages, %d chars, avg confidence %.0f%%, "
            "%d low-confidence pages",
            len(page_attempts), len(result), avg_confidence * 100,
            len(low_confidence_pages),
        )
        return OcrAttemptResult(
            text=result,
            parser_used="ocr_tesseract",
            pages=tuple(page_attempts),
        )

    except Exception as exc:
        logger.warning("pdf_report: OCR failed: %s", exc)
        return OcrAttemptResult("", "ocr_tesseract")



def _text_page_coverage(
    per_page_text: list[tuple[int, str]] | None,
) -> tuple[int, int, float]:
    """(pages, pages that produced text, fraction) for a parsed document.

    Split out so both return paths of parse_pdf_report can report it --
    the empty-text early return is precisely the case where "0% of pages
    produced text" is the whole story, and it used to return a result
    carrying only parse_quality_pct=0.0, which reads as "not a technical
    report" rather than "we got nothing".
    """
    total = len(per_page_text) if per_page_text else 0
    with_text = sum(
        1 for _page, text in (per_page_text or []) if text and text.strip()
    )
    return total, with_text, (round(with_text / total, 4) if total else 0.0)


def parse_pdf_report(path: str, progress_file: str | None = None) -> ReportParseResult:
    """Parse a NI 43-101 PDF technical report and return a :class:`ReportParseResult`.

    Parameters
    ----------
    path:
        Absolute path to the PDF file on disk.
    progress_file:
        Optional path this (sub)process writes page-level progress JSON to
        (see :func:`_tick_progress`); the parent workflow polls it into
        silver.ingest_progress for the live UI bar.

    Returns
    -------
    ReportParseResult
        Extracted metadata, sections, parse quality metrics, resource tables,
        and per-page language tags.

    Notes
    -----
    Dispatch tree:
      fitz (pypdfium2) → always runs first for native text extraction
        → per-page OCR for image pages routes to tesseract
          (PDF_PARSER_TESSERACT_FALLBACK_ENABLED)
      pdfplumber → only fires when fitz crashes completely; whole-doc
        text + table extraction as a defensive last resort
      Whole-document OCR (when extraction is still below the minimum
      char threshold) routes through `_attempt_ocr`, which dispatches to
      tesseract or Azure Document Intelligence per `OCR_ENGINE`.
    The ``parser_used`` field on the result records which engine ran.
    """
    with _tracer.start_as_current_span("pdf_report.preflight") as _span:
        if not Path(path).is_file():
            raise FileNotFoundError(f"parse_pdf_report: file not found at '{path}'")

        file_size = Path(path).stat().st_size
        if file_size > MAX_PDF_SIZE_BYTES:
            raise ValueError(
                f"parse_pdf_report: file '{path}' is {file_size / 1024 / 1024:.1f} MB, "
                f"exceeds {MAX_PDF_SIZE_BYTES / 1024 / 1024:.0f} MB limit"
            )

        # Provenance: hash the raw PDF bytes once at entry
        with open(path, "rb") as _fh:
            _pdf_raw = _fh.read()
        _sha256_hex = hashlib.sha256(_pdf_raw).hexdigest()
        _provenance: dict[str, Any] = {
            "source_file": path,
            "source_file_sha256": _sha256_hex,
            "parser_name": "pdf_report",
            "parser_version": PARSER_VERSION,
            "source_col_map": {},
        }

        magic = _pdf_raw[:5]
        if magic != b"%PDF-":
            raise ValueError(
                f"parse_pdf_report: file '{path}' is not a valid PDF (magic bytes: {magic!r})"
            )

        header_bytes = _pdf_raw[:4096]
        if b"/Encrypt" in header_bytes:
            logger.warning("pdf_report: file '%s' appears to be encrypted — extraction may fail", path)

        _span.set_attribute("pdf.size_bytes", file_size)
        _span.set_attribute("pdf.sha256", _sha256_hex)
        _span.set_attribute("pdf.encrypted", b"/Encrypt" in header_bytes)

    # --- Attempt primary extraction ---
    parser_used = "unknown"
    full_text = ""
    raw_title = ""
    skipped_elements = 0
    extraction_warnings: list[dict] = []
    page_languages: list[str] = []
    per_page_text: list[tuple[int, str]] = []
    # Width of the joiner between page texts in full_text: the fitz/
    # pdfplumber paths use "\n".join (1 char), the whole-document OCR
    # paths use "\n\n".join (2 chars). _build_page_index must mirror the
    # actual width or page attribution drifts +1 char per page.
    page_joiner_len = 1

    # Always-fitz-first dispatch: fitz (pypdfium2) runs first for native
    # text extraction. When PDF_PARSER_TESSERACT_FALLBACK_ENABLED is on
    # (the default), `_parse_with_fitz` also runs its internal per-page
    # tesseract loop on any page it returned < PER_PAGE_MIN_CHARS on, so
    # image pages in an otherwise text-dense doc aren't silently dropped.
    _tesseract_fallback_enabled = os.environ.get(
        "PDF_PARSER_TESSERACT_FALLBACK_ENABLED", "true"
    ).lower() == "true"
    fitz_enabled = os.environ.get("PDF_PARSER_FITZ_ENABLED", "true").lower() == "true"

    fitz_failed = False
    image_page_nums: list[int] = []
    is_scanned = False
    # Phase 3 — per-page method + confidence maps accumulated across the
    # dispatch tree, applied to ReportSections at the end via
    # _assign_ocr_metadata.
    per_page_method: dict[int, str] = {}
    per_page_confidence: dict[int, float | None] = {}
    # Scanned-table support (2026-08-11) — DI table grids per OCR'd page,
    # rendered into table ReportSections at the end.
    per_page_tables: dict[int, list[list[list[str]]]] = {}
    if fitz_enabled:
        try:
            with _tracer.start_as_current_span("pdf_report.fitz") as _span:
                (full_text, raw_title, skipped_elements, extraction_warnings,
                 page_languages, per_page_text, image_page_nums,
                 per_page_method, per_page_confidence,
                 per_page_tables) = _parse_with_fitz(
                    path,
                    apply_ocr_fallback=_tesseract_fallback_enabled,
                    progress_file=progress_file,
                )
                _span.set_attribute("pdf.text_chars", len(full_text))
                _span.set_attribute("pdf.page_count", len(page_languages))
                _span.set_attribute("pdf.image_pages", len(image_page_nums))
                # 2026-08-14 — image_page_nums is recomputed post-recovery
                # (only pages still unfilled), so a scan whose every page
                # was successfully OCR'd reported is_scanned=false. Any
                # page whose text came from an OCR engine was an image
                # page pre-recovery — OR those in. (per_page_method holds
                # 'fitz_native' only for true text-layer pages; recovered
                # pages carry 'tesseract'/'document_intelligence'/etc.)
                is_scanned = is_scanned or bool(image_page_nums) or any(
                    m not in ("fitz_native", "pdfplumber_native")
                    for m in per_page_method.values()
                )
                parser_used = "fitz"
                logger.info(
                    "pdf_report: fitz extracted %d chars from '%s' (%d pages, %d image pages)",
                    len(full_text), Path(path).name, len(page_languages),
                    len(image_page_nums),
                )
        except Exception as exc:  # noqa: BLE001
            fitz_failed = True
            logger.warning(
                "pdf_report: fitz failed (%s) — falling through to pdfplumber", exc,
            )

    # Pdfplumber fallback when fitz itself failed completely.
    if fitz_failed or len(full_text.strip()) < MIN_EXTRACTABLE_TEXT_CHARS:
        parser_used = "pdfplumber"
        try:
            with _tracer.start_as_current_span("pdf_report.pdfplumber") as _span:
                (full_text, raw_title, skipped_elements, extraction_warnings,
                 page_languages, per_page_text) = _parse_with_pdfplumber(path)
                _span.set_attribute("pdf.text_chars", len(full_text))
                _span.set_attribute("pdf.page_count", len(page_languages))
                # Phase 3 — pdfplumber path. Whole-doc method tagged as
                # pdfplumber_native; the per-page tesseract recovery that
                # happens inside _parse_with_pdfplumber is currently not
                # surfaced per-page (that worker emits its own warnings).
                # Mark every page as pdfplumber_native with NULL confidence
                # so the qdrant payload reflects "no per-page OCR signal
                # available" rather than fabricating a number.
                per_page_method = {pn: "pdfplumber_native" for pn, _ in per_page_text}
                per_page_confidence = {pn: None for pn, _ in per_page_text}
                logger.info(
                    "pdf_report: pdfplumber extracted %d chars from '%s'",
                    len(full_text),
                    Path(path).name,
                )
        except Exception as fallback_exc:
            logger.error("pdf_report: pdfplumber also failed: %s", fallback_exc)
            raise RuntimeError(
                f"parse_pdf_report: all parsers failed for '{path}'. "
                f"fitz failed: {fitz_failed}; "
                f"pdfplumber error: {fallback_exc}"
            ) from fallback_exc

    # Emit mixed-language warning when more than one distinct language is detected
    if page_languages:
        unique_langs = set(page_languages) - {"unknown"}
        if len(unique_langs) > 1:
            extraction_warnings.append({
                "code": "mixed_language_document",
                "context": {"languages": sorted(unique_langs)},
            })

    # --- Scanned PDF detection + OCR fallback ---
    if len(full_text.strip()) < MIN_EXTRACTABLE_TEXT_CHARS:
        is_scanned = True
        with _tracer.start_as_current_span("pdf_report.ocr") as _span:
            logger.warning(
                "pdf_report: only %d chars extracted from '%s' — attempting OCR fallback",
                len(full_text.strip()),
                Path(path).name,
            )
            ocr_result = _attempt_ocr(path)
            ocr_text = ocr_result.text
            _span.set_attribute("ocr.input_chars", len(full_text.strip()))
            _span.set_attribute("ocr.output_chars", len(ocr_text))
            if ocr_text and len(ocr_text.strip()) > len(full_text.strip()):
                # Only persist per-page OCR quality warnings when the OCR
                # text actually wins — when the native text is kept, the
                # discarded OCR pages' assessments are pure noise.
                extraction_warnings.extend(
                    _ocr_quality_warning(
                        page_number=page.page_number,
                        text=page.text,
                        assessment=page.assessment,
                    )
                    for page in ocr_result.pages
                )
                full_text = ocr_text
                parser_used = ocr_result.parser_used
                page_joiner_len = 2  # OCR paths join pages with "\n\n"
                per_page_text = [
                    (page.page_number, page.text)
                    for page in ocr_result.pages
                    if page.text.strip()
                ]
                per_page_method = {
                    page.page_number: str(
                        page.assessment.get("ocr_method") or "unknown"
                    )
                    for page in ocr_result.pages
                    if page.text.strip()
                }
                per_page_confidence = {
                    page.page_number: page.mean_confidence
                    for page in ocr_result.pages
                    if page.text.strip()
                }
                # Whole-doc OCR re-analyzed every page — its per-page DI
                # table grids supersede anything the fitz loop collected.
                per_page_tables = {
                    page.page_number: list(page.tables)
                    for page in ocr_result.pages
                    if page.tables
                }
                page_languages = [
                    _detect_page_language(page.text)
                    for page in ocr_result.pages
                    if page.text.strip()
                ]
                _span.set_attribute("ocr.recovered", True)
                logger.info(
                    "pdf_report: OCR recovered %d chars from '%s'",
                    len(full_text),
                    Path(path).name,
                )
            else:
                _span.set_attribute("ocr.recovered", False)

    if not full_text.strip():
        logger.warning("pdf_report: extracted text is empty for '%s'", path)
        _budget_warning = _di_budget_warning(path)
        if _budget_warning is not None:
            extraction_warnings.append(_budget_warning)
        return ReportParseResult(
            title=raw_title or Path(path).stem,
            authors=[],
            company=None,
            filing_date=None,
            commodity=None,
            project_name=None,
            region=None,
            sections=[],
            parse_quality_pct=0.0,
            text_page_coverage_pct=_text_page_coverage(per_page_text)[2],
            parser_used=parser_used,
            skipped_elements=skipped_elements,
            warnings=extraction_warnings,
            provenance=_provenance,
            page_languages=page_languages,
            is_scanned=is_scanned,
        )

    # --- Use first ~2000 chars for metadata extraction (title page) ---
    with _tracer.start_as_current_span("pdf_report.metadata") as _span:
        lead_text = full_text[:2000]
        title = raw_title.strip() or full_text[:100].splitlines()[0].strip()
        authors = _extract_authors(lead_text)
        company = _extract_company(lead_text)
        filing_date = _extract_filing_date(lead_text)
        commodity = _extract_commodity(lead_text) or _extract_commodity(full_text[:5000])
        project_name = _extract_project_name(lead_text, title)
        region = _extract_region(lead_text) or _extract_region(full_text[:5000])
        _span.set_attribute(
            "pdf.metadata_fields",
            sum(1 for v in [title, authors, company, filing_date, commodity, project_name, region] if v),
        )

    # --- Split into sections (primary headings + subsections) ---
    with _tracer.start_as_current_span("pdf_report.sections") as _span:
        sections = _split_into_sections(
            full_text, per_page_text, joiner_len=page_joiner_len,
        )
        # Unified sliding-window chunker emits multiple chunks per detected
        # heading. parse_quality_pct measures heading *coverage* against the
        # 17-section NI 43-101 baseline, so dedupe by section_number.
        unique_section_numbers = {
            s.section_number for s in sections if s.section_number is not None
        }
        numbered_sections = [s for s in sections if s.section_number is not None]
        subsection_count = len(SUBSECTION_HEADING_RE.findall(full_text))
        # Clamp to 1.0. The dedupe above stops multi-chunk sections from
        # inflating this, but a report that genuinely carries more numbered
        # sections than the 17-section NI 43-101 baseline still overshoots:
        # a live document with 29 numbered sections stored 1.706 here, i.e.
        # "170.6% quality". This is a coverage-of-baseline ratio, so full
        # coverage is the ceiling.
        parse_quality_pct = min(
            1.0,
            round(len(unique_section_numbers) / NI43_BASELINE_SECTIONS, 4),
        )
        _span.set_attribute("pdf.sections_total", len(sections))
        _span.set_attribute("pdf.sections_numbered", len(numbered_sections))
        _span.set_attribute("pdf.sections_unique_numbered", len(unique_section_numbers))
        _span.set_attribute("pdf.subsections", subsection_count)
        _span.set_attribute("pdf.parse_quality_pct", parse_quality_pct)

        # The number `parse_quality_pct` is mistaken for.
        #
        # `parse_quality_pct` is NI 43-101 heading coverage — it answers
        # "does this document have the shape of a technical report", not
        # "did we get the content". A 1970s government geophysics survey
        # extracted flawlessly scores 0.0 because it has no numbered
        # sections; a report whose TOC yielded 17 headings while 300 pages
        # OCR'd to nothing scores 1.0.
        #
        # This is the extraction question, and `per_page_text` is already
        # in hand. It is now STORED as well as logged --
        # silver.reports.text_page_coverage_pct, carried through ParseOut
        # and shown beside the section-coverage figure in the UI, where
        # the label reads "NI 43-101 sections" rather than "parse
        # quality". That was the actual defect: not that the number was
        # wrong, but that it was presented as the answer to a question it
        # does not answer.
        #
        # Renaming parse_quality_pct to ni43101_section_coverage_pct is
        # still a follow-up. It is on every report row and read by the
        # Dagster assets, the Laravel controllers and two React pages, so
        # it is a data migration with consumers rather than hygiene -- and
        # with the honest number stored beside it, much less urgent.
        # per_page_text is list[tuple[page_number, text]], not list[str].
        _pages_total, _pages_with_text, text_page_coverage = _text_page_coverage(
            per_page_text
        )
        _span.set_attribute("pdf.pages_total", _pages_total)
        _span.set_attribute("pdf.pages_with_text", _pages_with_text)
        _span.set_attribute("pdf.text_page_coverage", text_page_coverage)

        if _pages_total and text_page_coverage < 0.5 <= parse_quality_pct:
            # The dangerous combination, and the reason the two numbers
            # belong in the same place: the report LOOKS well parsed and
            # more than half its pages produced nothing.
            logger.warning(
                "pdf_report: section coverage %.0f%% but only %d of %d pages "
                "produced text — parse_quality_pct measures NI 43-101 "
                "headings, not extraction; this document is largely empty",
                parse_quality_pct * 100, _pages_with_text, _pages_total,
            )

    # Extraction confidence: combines section coverage + text length + metadata completeness
    metadata_fields = sum(1 for v in [title, authors, company, filing_date, commodity, project_name, region] if v)
    extraction_confidence = min(1.0, (
        parse_quality_pct * 0.5 +                         # section coverage (50%)
        min(1.0, len(full_text) / 10000) * 0.3 +         # text volume (30%)
        (metadata_fields / 7) * 0.2                        # metadata completeness (20%)
    ))
    logger.info(
        "pdf_report: extraction_confidence=%.2f (ni43101_section_coverage=%.1f%%, "
        "text_page_coverage=%.1f%% [%d/%d pages], text=%d chars, "
        "metadata=%d/7, subsections=%d)",
        extraction_confidence, parse_quality_pct * 100,
        text_page_coverage * 100, _pages_with_text, _pages_total,
        len(full_text), metadata_fields, subsection_count,
    )

    logger.info(
        "pdf_report: parse complete — parser=%s, sections=%d numbered/%d total, "
        "quality=%.1f%%, title='%s', commodity=%s",
        parser_used,
        len(numbered_sections),
        len(sections),
        parse_quality_pct * 100,
        title[:60] if title else "(none)",
        commodity,
    )

    # --- Resource table extraction (separate pdfplumber pass) ---
    resource_tables: list[dict] = []
    with _tracer.start_as_current_span("pdf_report.resource_tables") as _span:
        try:
            resource_tables = _extract_resource_tables(path, progress_file=progress_file)
            _span.set_attribute("pdf.resource_tables_found", len(resource_tables))
            logger.info(
                "pdf_report: resource table extraction found %d table(s) in '%s'",
                len(resource_tables),
                Path(path).name,
            )
        except Exception as rt_exc:
            _span.record_exception(rt_exc)
            extraction_warnings.append({
                "code": "resource_table_extraction_failed",
                "message": str(rt_exc),
            })
            logger.warning(
                "pdf_report: resource table extraction failed for '%s': %s",
                Path(path).name,
                rt_exc,
        )

    # --- All-page table extraction (assays, drill collars, geochem, etc.) ---
    # Per-page classification routes bordered tables to pdfplumber-lines
    # and borderless tables to pdfplumber-text. Each surviving table
    # becomes a section so it gets chunked + embedded and is searchable
    # from chat. The existing _extract_resource_tables path only catches
    # resource-trigger pages; this is the broader net.
    with _tracer.start_as_current_span("pdf_report.all_tables") as _span:
        try:
            table_sections = _extract_all_tables_as_sections(
                path, progress_file=progress_file,
            )
            _span.set_attribute("pdf.all_tables_found", len(table_sections))
            if table_sections:
                logger.info(
                    "pdf_report: table dispatch added %d table section(s) in '%s'",
                    len(table_sections), Path(path).name,
                )
                sections.extend(table_sections)
        except Exception as at_exc:
            _span.record_exception(at_exc)
            extraction_warnings.append({
                "code": "all_table_extraction_failed",
                "message": str(at_exc),
            })
            logger.warning(
                "pdf_report: all-page table extraction failed for '%s': %s",
                Path(path).name,
                at_exc,
            )

    # Phase 3 (2026-05-22) — backfill ocr_confidence + ocr_method on every
    # ReportSection (narrative sections, table sections, figure sections)
    # using the per-page maps the dispatch tree accumulated.
    _assign_ocr_metadata(sections, per_page_method, per_page_confidence)

    # --- Scanned-table sections from Document Intelligence (2026-08-11) ---
    # Table grids the DI prebuilt-layout model returned for OCR'd pages,
    # appended after the narrative + pdfplumber table sections. Two notes:
    #   - No dedupe against _extract_all_tables_as_sections is needed:
    #     that pass runs pdfplumber against the original PDF, and pages
    #     that produced DI tables have no usable text layer (that is why
    #     they were OCR'd) — pdfplumber's lines/text strategies find
    #     nothing there, so double-extraction cannot occur.
    #   - Appended AFTER _assign_ocr_metadata on purpose: these sections
    #     carry their own truthful provenance (ocr_method =
    #     'document_intelligence' + the page's mean confidence) and must
    #     not be overwritten by the first-page-wins backfill.
    if per_page_tables:
        di_table_sections: list[ReportSection] = []
        for _tbl_page in sorted(per_page_tables):
            di_table_sections.extend(
                _di_tables_to_sections(
                    per_page_tables[_tbl_page],
                    _tbl_page,
                    mean_confidence=per_page_confidence.get(_tbl_page),
                )
            )
        if di_table_sections:
            logger.info(
                "pdf_report: document_intelligence added %d table section(s) "
                "across %d OCR'd page(s) in '%s'",
                len(di_table_sections), len(per_page_tables), Path(path).name,
            )
            sections.extend(di_table_sections)

    # The DI page budget is cost control, not a failure — but a document
    # whose tail was read by a weaker engine has to say so on the way out,
    # or the two halves are indistinguishable downstream.
    _budget_warning = _di_budget_warning(path)
    if _budget_warning is not None:
        extraction_warnings.append(_budget_warning)

    return ReportParseResult(
        title=title or None,
        authors=authors,
        company=company,
        filing_date=filing_date,
        commodity=commodity,
        project_name=project_name,
        region=region,
        sections=sections,
        parse_quality_pct=parse_quality_pct,
        text_page_coverage_pct=text_page_coverage,
        parser_used=parser_used,
        skipped_elements=skipped_elements,
        warnings=extraction_warnings,
        provenance=_provenance,
        resource_tables=resource_tables,
        page_languages=page_languages,
        is_scanned=is_scanned,
        extraction_confidence=extraction_confidence,
    )
