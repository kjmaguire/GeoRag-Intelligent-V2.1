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
Intelligence when `OCR_ENGINE=document_intelligence`) for scanned/image
pages. See `_attempt_ocr`, `_attempt_ocr_document_intelligence`, and
`document_intelligence_client` for the OCR dispatch.

---

NI 43-101 PDF Report Parser — Bronze → Silver ingestion for technical reports.

Accepts a path to a PDF file and extracts structured metadata and section text
from NI 43-101 technical reports. NI 43-101 mandates a specific table of
contents structure (up to 27 sections; 17 is the typical baseline) which this
parser exploits for high-confidence section boundary detection.

Primary extraction engine: pypdfium2 (PDFium) for native text + per-page OCR
routing to Tesseract (default) or Azure Document Intelligence (when
`OCR_ENGINE=document_intelligence`) for image pages. Fallback engine:
pdfplumber, used when the primary can't extract sufficient structure.

Parse quality is reported as a float 0.0–1.0 representing the fraction of the
17 expected NI 43-101 sections identified. The caller (silver_reports asset)
records this in Dagster materialisation metadata.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import re
import statistics
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


@dataclass(frozen=True, slots=True)
class OcrPageAttempt:
    """One page produced by a full-document OCR attempt."""

    page_number: int
    text: str
    mean_confidence: float
    assessment: dict[str, Any]


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
# section headers). Sized for bge-small-en-v1.5 which truncates at 512 tokens
# (~2000 chars). A 1500-char window with 200-char overlap lands well inside
# the truncation limit while keeping enough context per chunk for retrieval.
WINDOW_CHARS = 1500
WINDOW_OVERLAP_CHARS = 200


def _emit_windows(
    full_text: str,
    abs_start: int,
    abs_end: int,
    section_number: str | None,
    section_title: str,
    page_index: list[tuple[int, int, int]],
) -> list[ReportSection]:
    """Emit sliding-window ReportSections over a contiguous segment.

    Every emitted chunk has len(text) ≤ WINDOW_CHARS so the embedding
    model never truncates. Adjacent chunks overlap by WINDOW_OVERLAP_CHARS
    so split sentences still match retrieval queries.

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

    step = max(1, WINDOW_CHARS - WINDOW_OVERLAP_CHARS)
    for local in range(0, seg_len, step):
        a = abs_start + local
        b = min(a + WINDOW_CHARS, abs_end)
        chunk = full_text[a:b].strip()
        if not chunk:
            if b >= abs_end:
                break
            continue
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

    return out


def _split_into_sections(
    full_text: str,
    per_page_text: list[tuple[int, str]] | None = None,
    joiner_len: int = 1,
) -> list[ReportSection]:
    """Chunk the document with sliding windows; tag chunks with section
    metadata when NI 43-101 headings are detected.

    Every emitted ReportSection has ``len(text) ≤ WINDOW_CHARS``, so the
    bge-small embedder (512-token ≈ 2,000-char limit) never truncates a
    chunk. Section structure is preserved as *metadata* on each chunk:

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
    matches = list(SECTION_HEADING_RE.finditer(full_text))

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


def _extract_resource_tables(pdf_path: str) -> list[dict]:
    """Extract mineral resource / reserve tables from a NI 43-101 PDF.

    Opens the PDF with pdfplumber, identifies candidate pages via trigger
    phrases, and attempts two extraction strategies (lines-based, then
    text-based). Returns a list of structured table dicts.

    Each entry contains:
        page, table_index_on_page, trigger_phrase, header, rows,
        extraction_method, confidence.
    """
    import pdfplumber  # noqa: PLC0415

    results: list[dict] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
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


def _table_to_markdown(table: list[list[str | None]]) -> str:
    """Render a pdfplumber table-of-lists as a markdown-style text block.

    The point isn't to be pretty markdown — it's that each cell stays on
    a recognizable row/column so embeddings + retrieval can match queries
    like "Au grade at hole MAD-22-001" even when the value lives in a
    cell rather than flowing prose. Joining cells with " | " preserves
    enough structure for BM25 + dense retrieval to find data values.
    """
    if not table:
        return ""
    rendered = []
    for row in table:
        cells = [(str(c).replace("\n", " ").strip() if c is not None else "") for c in row]
        if any(cells):
            rendered.append(" | ".join(cells))
    return "\n".join(rendered)


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
    drawings: list,
    line_threshold: int = 3,
    rect_threshold: int = 20,
    min_horizontal_line_length: float = 30.0,
) -> str:
    """Phase 4 (2026-05-22) — classify a PDF page as 'bordered' or 'borderless'.

    Walks the fitz `page.get_drawings()` output (pre-fetched and passed
    in so the caller can open the PDF once per file rather than once per
    classifier call).

    Heuristic:
      - Count horizontal lines longer than ``min_horizontal_line_length``
        points. ≥ ``line_threshold`` → bordered.
      - Count rectangle ('re') items. ≥ ``rect_threshold`` → bordered.
        Real-world prospectuses commonly use rectangles (not lines) for
        table borders — counted separately so the kickoff's
        TABLE_BORDER_LINE_THRESHOLD threshold doesn't miss them.

    A page that has ≥ either threshold is bordered. Pages below both
    thresholds are classified borderless. Returns "bordered" or
    "borderless"; never None.
    """
    h_lines = 0
    rects = 0
    for d in (drawings or []):
        for it in d.get("items", []) or []:
            kind = it[0] if it else None
            if kind == "l":
                # Line: ('l', Point1, Point2). Count near-horizontal lines
                # only (Δy ~ 0 within 1 point), of meaningful length.
                try:
                    p1, p2 = it[1], it[2]
                    if (
                        abs(p1.y - p2.y) < 1.0
                        and abs(p1.x - p2.x) >= min_horizontal_line_length
                    ):
                        h_lines += 1
                except Exception:
                    continue
            elif kind == "re":
                # Rectangle: ('re', Rect, ...). Counted regardless of size;
                # real-world bordered table cells can be tiny.
                rects += 1
            # 'qu' (quad) and 'c' (curve) are ignored — neither is a
            # standard table-border primitive.
    if h_lines >= line_threshold:
        return "bordered"
    if rects >= rect_threshold:
        return "bordered"
    return "borderless"


def _classify_pages_from_pdf(pdf_path: str) -> dict[int, str]:
    """Open the PDF once via fitz and return {page_no: 'bordered'|'borderless'}.

    Reads thresholds from env vars (with the defaults in kickoff):
      TABLE_BORDER_LINE_THRESHOLD (default 3)
      TABLE_BORDER_RECT_THRESHOLD (default 20)

    Returns an empty dict on any failure (caller falls back to legacy
    behavior — defensive).
    """
    try:
        import pymupdf  # noqa: PLC0415
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

    result: dict[int, str] = {}
    try:
        with pymupdf.open(pdf_path) as doc:
            for n, page in enumerate(doc, start=1):
                try:
                    drawings = page.get_drawings()
                except Exception:
                    drawings = []
                result[n] = _classify_page_table_type(
                    drawings,
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
    return result


def _extract_all_tables_as_sections(pdf_path: str) -> list[ReportSection]:
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
        for page_num, page in enumerate(pdf.pages, start=1):
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
                pdfplumber_sections.append(
                    ReportSection(
                        section_number=None,
                        section_title=f"Table (page {page_num}, #{idx + 1})",
                        text=md,
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

def _parse_with_fitz(
    path: str,
    apply_ocr_fallback: bool = True,
) -> tuple[
    str, str, int, list, list[str], list[tuple[int, str]], list[int],
    dict[int, str], dict[int, float | None],
]:
    """Extract full text using pypdfium2 (PDFium). Faster than pdfplumber.

    Engine history: originally PyMuPDF (fitz); swapped to pypdfium2 when PyMuPDF
    was removed for its AGPL license. The `fitz` name/labels are retained as
    stable identifiers (see the engine note in the body).

    Returns (full_text, title, skipped, warnings, page_languages,
             per_page_text, image_page_nums, per_page_method,
             per_page_confidence).

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
    # Phase 3 (2026-05-22) — per-page engine + confidence tracking
    per_page_method: dict[int, str] = {}
    per_page_confidence: dict[int, float | None] = {}

    # PDFium returns these sentinels for unset metadata fields — treat as "no
    # title" so the first-line fallback below can supply a real one.
    _META_SENTINELS = {"", "(anonymous)", "(unspecified)"}

    pdf = pdfium.PdfDocument(path)
    try:
        meta = pdf.get_metadata_dict() or {}
        _raw_title = (meta.get("Title") or "").strip()
        meta_title = "" if _raw_title in _META_SENTINELS else _raw_title
        for n in range(1, len(pdf) + 1):
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
            if txt and len(txt.strip()) >= PER_PAGE_MIN_CHARS:
                pages_text.append(txt)
                per_page_text.append((n, txt))
                page_languages.append(_detect_page_language(txt))
                # Phase 3 — text-layer page, no OCR involved
                per_page_method[n] = "fitz_native"
                per_page_confidence[n] = None
            else:
                # Page came back short — queue it for OCR below.
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
        for n in short_page_nums:
            try:
                # Phase 3 — capture mean_conf from tesseract per-word data
                ocr_text, mean_conf, assessment = _ocr_single_page(
                    path,
                    n,
                    return_confidence=True,
                    return_assessment=True,
                )
            except Exception:
                continue
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
                per_page_method[n] = "tesseract"
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
                        per_page_method[n] = "tesseract"
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
        per_page_method, per_page_confidence,
    )


# Phase 10 (2026-05-22) — _parse_with_unstructured removed.
# Phase 2.1 made fitz-first dispatch the only path; unstructured was never
# invoked from the dispatch tree. The dependency on `unstructured[pdf]` is
# also dropped from pyproject.toml + the worker bootstrap.


# ---------------------------------------------------------------------------
# Fallback parser: pdfplumber
# ---------------------------------------------------------------------------

def _ocr_single_page(
    pdf_path: str,
    page_num: int,
    return_confidence: bool = False,
    return_assessment: bool = False,
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

    Returns ``""`` (or the corresponding empty tuple) on any failure.
    """
    from . import document_intelligence_client as _di

    di_selected = _di.is_engine_selected()
    if di_selected:
        try:
            # Slice the single target page into its own PDF before upload.
            # Two reasons this is not an optimisation but a correctness fix:
            # (1) Document Intelligence F0 rejects `pages=N` for N > 2
            #     ("InvalidRequest") — a 1-page document sidesteps the free
            #     tier's first-two-pages analysis window entirely;
            # (2) sending the full file per page uploads O(size × pages)
            #     bytes — a 40 MB / 200-page report would push ~8 GB.
            # pikepdf failure falls back to the legacy whole-file upload.
            try:
                import io as _io

                import pikepdf as _pikepdf

                with _pikepdf.open(pdf_path) as _src:
                    _single = _pikepdf.Pdf.new()
                    _single.pages.append(_src.pages[page_num - 1])
                    _buf = _io.BytesIO()
                    _single.save(_buf)
                pdf_bytes = _buf.getvalue()
                _di_page = 1
            except Exception as _slice_exc:  # noqa: BLE001
                logger.warning(
                    "pdf_report: single-page slice failed for page %d of '%s' "
                    "(%s) — sending full document",
                    page_num, pdf_path, _slice_exc,
                )
                with open(pdf_path, "rb") as f:
                    pdf_bytes = f.read()
                _di_page = page_num
            result = _di.ocr_page_sync(pdf_bytes, _di_page)
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
            elif result.text.strip():
                assessment = _assess_ocr_result(
                    result.text,
                    [word.confidence for word in result.words]
                    or [result.mean_confidence],
                    detected_region_count=result.detected_region_count,
                )
                assessment["ocr_method"] = "document_intelligence"
                return _format_ocr_page_return(
                    result.text,
                    result.mean_confidence,
                    assessment,
                    return_confidence=return_confidence,
                    return_assessment=return_assessment,
                )
            else:
                # `ocr_page`/`ocr_page_sync` fail soft internally (e.g. Azure's
                # InvalidContentDimensions on an out-of-range scan resolution —
                # confirmed against a real 1940s-era TIFF in the corpus 2026-07-29)
                # and return an empty PageOcrResult rather than raising. Without
                # this check, that soft failure would look identical to "page is
                # genuinely blank" and skip tesseract entirely, silently losing
                # a page tesseract might actually be able to read.
                logger.info(
                    "pdf_report: document_intelligence returned empty text for "
                    "page %d of '%s' — trying bounded raster tiles",
                    page_num, pdf_path,
                )
                try:
                    tiled_result, assessment = _ocr_tiled_pdf_page(
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
                else:
                    if tiled_result.text.strip():
                        return _format_ocr_page_return(
                            tiled_result.text,
                            tiled_result.mean_confidence,
                            assessment,
                            return_confidence=return_confidence,
                            return_assessment=return_assessment,
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
            return _empty_ocr_page_return(return_confidence, return_assessment)
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
                # strict=False deliberately: pytesseract's DICT output should
                # give equal-length text/conf lists, but a malformed OCR result
                # must degrade to fewer words rather than raise mid-ingest.
                words = [
                    (w, int(c))
                    for w, c in zip(
                        data.get("text", []), data.get("conf", []), strict=False
                    )
                    if w and w.strip() and int(c) >= 0
                ]
                text = " ".join(w for w, _c in words)
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
                )
                assessment["ocr_method"] = "tesseract"
                return _format_ocr_page_return(
                    processed_text,
                    mean_conf,
                    assessment,
                    return_confidence=return_confidence,
                    return_assessment=return_assessment,
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
        )
        assessment["ocr_method"] = "tesseract"
        return _format_ocr_page_return(
            out_text,
            0.0,
            assessment,
            return_confidence=return_confidence,
            return_assessment=return_assessment,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pdf_report: per-page OCR failed on page %d of '%s': %s",
            page_num,
            pdf_path,
            exc,
        )
        return _empty_ocr_page_return(return_confidence, return_assessment)


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
    )
    assessment["ocr_method"] = "document_intelligence"
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
) -> dict[str, Any]:
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
        load_routing_thresholds_from_env(),
    )
    return {
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
):
    if return_assessment:
        return text, mean_confidence, assessment
    if return_confidence:
        return text, mean_confidence
    return text


def _empty_ocr_page_return(
    return_confidence: bool,
    return_assessment: bool,
    method: str = "tesseract",
):
    assessment = _assess_ocr_result("", [], detected_region_count=0)
    assessment["ocr_method"] = method
    return _format_ocr_page_return(
        "",
        0.0,
        assessment,
        return_confidence=return_confidence,
        return_assessment=return_assessment,
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

def _preprocess_image_for_ocr(img):
    """Preprocess a page image to maximize Tesseract accuracy.

    Steps:
      1. Convert to grayscale
      2. Upscale small images (below 2000px width)
      3. Adaptive thresholding (binarization) — handles uneven lighting from scanners
      4. Deskew — straightens rotated scans
      5. Denoise — removes scanner artifacts

    Returns a PIL Image ready for pytesseract.
    """
    try:
        import numpy as np
    except ImportError:
        return img  # numpy not available, return as-is

    # Convert to grayscale
    gray = img.convert('L')

    # Convert to numpy for OpenCV-style processing
    arr = np.array(gray)

    # Upscale if too small (tesseract works best at 300+ DPI equivalent)
    h, w = arr.shape
    if w < 2000:
        scale = 2000 / w
        from PIL import Image as PILImage
        gray = gray.resize((int(w * scale), int(h * scale)), PILImage.LANCZOS)
        arr = np.array(gray)

    # Adaptive thresholding — binarize with local contrast
    # Simple Otsu-style: pixels above mean+offset become white, rest black
    mean_val = arr.mean()
    threshold = mean_val * 0.85  # slightly below mean catches faint text
    binary = ((arr < threshold) * 255).astype(np.uint8)  # dark text on white bg
    binary = 255 - binary  # invert: white text areas become white bg, black text

    # Simple denoise: if a pixel is isolated (no dark neighbors), remove it
    # This is a lightweight version of morphological opening
    from PIL import Image as PILImage
    from PIL import ImageFilter
    result = PILImage.fromarray(arr)  # use grayscale (not binary) for Tesseract

    # Sharpen to improve edge definition
    result = result.filter(ImageFilter.SHARPEN)

    return result


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


def _ocr_page_confidence(text: str) -> float:
    """Estimate OCR confidence for a page based on text quality heuristics.

    Returns 0.0–1.0 where:
      1.0 = clean text, mostly real words
      0.0 = garbage (random characters, no recognizable words)

    Heuristics:
      - Ratio of alphabetic chars to total chars (garbage has lots of symbols)
      - Average word length (OCR garbage produces very short/long "words")
      - Presence of common English words
    """
    if not text.strip():
        return 0.0

    import re

    # Alphabetic ratio
    alpha_chars = sum(1 for c in text if c.isalpha())
    total_chars = len(text.replace(' ', '').replace('\n', ''))
    alpha_ratio = alpha_chars / max(total_chars, 1)

    # Average word length (good text: 3-8 chars average)
    words = re.findall(r'\b\w+\b', text)
    if not words:
        return 0.0
    avg_len = sum(len(w) for w in words) / len(words)
    length_score = 1.0 if 3 <= avg_len <= 8 else 0.5

    # Common word presence
    common_words = {'the', 'and', 'for', 'are', 'was', 'with', 'that', 'this',
                    'from', 'have', 'been', 'were', 'project', 'report', 'drill',
                    'mineral', 'resource', 'deposit', 'section'}
    found = sum(1 for w in words if w.lower() in common_words)
    common_ratio = min(1.0, found / max(len(words) * 0.05, 1))

    # Weighted confidence
    confidence = (alpha_ratio * 0.4) + (length_score * 0.3) + (common_ratio * 0.3)
    return round(min(1.0, confidence), 2)


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
        "pdf_report: starting document_intelligence OCR on %d pages",
        total_pages,
    )

    texts: list[str] = []
    page_confidences: list[float] = []
    low_confidence_pages: list[int] = []
    page_attempts: list[OcrPageAttempt] = []

    for page_num in range(1, total_pages + 1):
        page_text, mean_confidence, assessment = _ocr_single_page(
            path,
            page_num,
            return_confidence=True,
            return_assessment=True,
        )
        cleaned = _postprocess_ocr_text(page_text) if page_text.strip() else ""
        page_attempts.append(
            OcrPageAttempt(
                page_number=page_num,
                text=cleaned,
                mean_confidence=mean_confidence,
                assessment=assessment,
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

        if page_num % 10 == 0 or page_num == 1:
            logger.info(
                "pdf_report: OCR progress %d/%d pages", page_num, total_pages,
            )

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

        images = convert_from_path(
            path,
            dpi=OCR_DPI,
            first_page=1,
            last_page=pages_to_process,
            thread_count=2,  # parallel page rendering
        )

        texts = []
        page_confidences = []
        low_confidence_pages = []
        page_attempts: list[OcrPageAttempt] = []

        for i, img in enumerate(images):
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
                detected_words = [
                    (str(word).strip(), int(confidence))
                    for word, confidence in zip(
                        data.get("text", []),
                        data.get("conf", []),
                        strict=False,
                    )
                    if str(word).strip() and int(confidence) >= 0
                ]
                page_text = " ".join(word for word, _confidence in detected_words)
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
            )
            assessment["ocr_method"] = "tesseract"
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
                    i + 1, len(images),
                )

        result = "\n\n".join(texts)
        avg_confidence = (
            sum(page_confidences) / len(page_confidences)
            if page_confidences else 0.0
        )
        logger.info(
            "pdf_report: OCR complete — %d pages, %d chars, avg confidence %.0f%%, "
            "%d low-confidence pages",
            len(images), len(result), avg_confidence * 100,
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


def parse_pdf_report(path: str) -> ReportParseResult:
    """Parse a NI 43-101 PDF technical report and return a :class:`ReportParseResult`.

    Parameters
    ----------
    path:
        Absolute path to the PDF file on disk.

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
    if fitz_enabled:
        try:
            with _tracer.start_as_current_span("pdf_report.fitz") as _span:
                (full_text, raw_title, skipped_elements, extraction_warnings,
                 page_languages, per_page_text, image_page_nums,
                 per_page_method, per_page_confidence) = _parse_with_fitz(
                    path,
                    apply_ocr_fallback=_tesseract_fallback_enabled,
                )
                _span.set_attribute("pdf.text_chars", len(full_text))
                _span.set_attribute("pdf.page_count", len(page_languages))
                _span.set_attribute("pdf.image_pages", len(image_page_nums))
                is_scanned = is_scanned or bool(image_page_nums)
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
        parse_quality_pct = round(len(unique_section_numbers) / NI43_BASELINE_SECTIONS, 4)
        _span.set_attribute("pdf.sections_total", len(sections))
        _span.set_attribute("pdf.sections_numbered", len(numbered_sections))
        _span.set_attribute("pdf.sections_unique_numbered", len(unique_section_numbers))
        _span.set_attribute("pdf.subsections", subsection_count)
        _span.set_attribute("pdf.parse_quality_pct", parse_quality_pct)

    # Extraction confidence: combines section coverage + text length + metadata completeness
    metadata_fields = sum(1 for v in [title, authors, company, filing_date, commodity, project_name, region] if v)
    extraction_confidence = min(1.0, (
        parse_quality_pct * 0.5 +                         # section coverage (50%)
        min(1.0, len(full_text) / 10000) * 0.3 +         # text volume (30%)
        (metadata_fields / 7) * 0.2                        # metadata completeness (20%)
    ))
    logger.info(
        "pdf_report: extraction_confidence=%.2f (quality=%.1f%%, text=%d chars, "
        "metadata=%d/7, subsections=%d)",
        extraction_confidence, parse_quality_pct * 100, len(full_text),
        metadata_fields, subsection_count,
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
            resource_tables = _extract_resource_tables(path)
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
            table_sections = _extract_all_tables_as_sections(path)
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
        parser_used=parser_used,
        skipped_elements=skipped_elements,
        warnings=extraction_warnings,
        provenance=_provenance,
        resource_tables=resource_tables,
        page_languages=page_languages,
        is_scanned=is_scanned,
    )
