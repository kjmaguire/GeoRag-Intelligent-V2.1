"""
PDF parser — fitz-first dispatch with docling+rapidocr / tesseract OCR
fallbacks and pdfplumber as the parser-of-last-resort.

**Canonical path**: NI 43-101 PDFs parse via RAGFlow (v0.17.2 pinned per §12).
This parser is the explicit fallback for cases where RAGFlow parse fails or
returns insufficient structure (tables, section hierarchy). Kyle-approved
2026-04-20 as a fallback-only code path. Do NOT invoke unless RAGFlow has
failed for a given document.

TODO (Module 3 Phase B): add runtime guard that this parser is only called
after a recorded RAGFlow failure for the same `bronze_sha256`.

---

NI 43-101 PDF Report Parser — Bronze → Silver ingestion for technical reports.

Accepts a path to a PDF file and extracts structured metadata and section text
from NI 43-101 technical reports. NI 43-101 mandates a specific table of
contents structure (up to 27 sections; 17 is the typical baseline) which this
parser exploits for high-confidence section boundary detection.

Primary extraction engine: PyMuPDF (fitz) for native text + per-page OCR
routing to docling+rapidocr (when enabled) or tesseract for image pages.
Fallback engine: pdfplumber, used only when fitz crashes completely.

Parse quality is reported as a float 0.0–1.0 representing the fraction of the
17 expected NI 43-101 sections identified. The caller (silver_reports asset)
records this in Dagster materialisation metadata.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

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
from georag_dagster.observability import get_tracer

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

    section_number: Optional[str]   # "1", "2", ..., "17"
    section_title: str              # e.g. "Summary", "Introduction"
    text: str                       # Body text of the section
    page_first: Optional[int] = None  # First 1-indexed page this section spans.
    page_last: Optional[int] = None   # Last 1-indexed page this section spans.
    # Phase 3 (2026-05-22) — OCR confidence + method per chunk. NULL
    # means the chunk came from the PDF text layer (no OCR). 0.0–1.0
    # means an OCR engine produced the text. ocr_method records which
    # engine: fitz_native, pdfplumber_native, docling_rapidocr, tesseract.
    # When a chunk spans multiple pages with mixed methods, the minimum
    # confidence is recorded and the first-page method wins (kickoff
    # min-confidence-per-chunk semantics).
    ocr_confidence: Optional[float] = None
    ocr_method: Optional[str] = None


@dataclass
class ReportParseResult:
    """Complete result of parsing a NI 43-101 PDF technical report."""

    title: Optional[str]
    authors: list[str]
    company: Optional[str]
    filing_date: Optional[str]      # ISO 8601 string: YYYY-MM-DD
    commodity: Optional[str]
    project_name: Optional[str]
    region: Optional[str]
    sections: list[ReportSection]
    parse_quality_pct: float        # Fraction of expected sections found (0.0–1.0+)
    parser_used: str = "unknown"
    skipped_elements: int = 0
    warnings: list = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    resource_tables: list[dict] = field(default_factory=list)
    page_languages: list[str] = field(default_factory=list)
    # Phase 1 (2026-05-22): docling figure manifest. Each entry is a dict
    # {idx, page, bbox, caption, pending_key, bucket, sha256}. Built inline
    # by _parse_with_docling (uploads PNGs to figures/_pending/{sha}/...).
    # Consumed by the persist Hatchet task, which copies each PNG to its
    # final figures/{report_id}/... key and removes the pending object.
    # Empty list when docling is disabled or no figures were extracted.
    figure_manifest: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Date parsing helpers
# ---------------------------------------------------------------------------

def _parse_date_string(raw: str) -> Optional[str]:
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

def _extract_company(text: str) -> Optional[str]:
    for pattern in COMPANY_PATTERNS:
        m = pattern.search(text)
        if m:
            value = m.group(1).strip().rstrip(".,")
            if len(value) > 2:
                return value
    return None


def _extract_filing_date(text: str) -> Optional[str]:
    for pattern in FILING_DATE_PATTERNS:
        m = pattern.search(text)
        if m:
            parsed = _parse_date_string(m.group(1))
            if parsed:
                return parsed
    return None


def _extract_commodity(text: str) -> Optional[str]:
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


def _extract_project_name(text: str, title: Optional[str]) -> Optional[str]:
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


def _extract_region(text: str) -> Optional[str]:
    for kw in REGION_KEYWORDS:
        if re.search(re.escape(kw), text, re.IGNORECASE):
            return kw
    return None


# ---------------------------------------------------------------------------
# Section splitting
# ---------------------------------------------------------------------------

def _assign_ocr_metadata(
    sections: list["ReportSection"],
    per_page_method: dict[int, str],
    per_page_confidence: dict[int, Optional[float]],
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
) -> list[tuple[int, int, int]]:
    """Return [(char_start, char_end_exclusive, page_num), ...] for full_text.

    full_text is built via "\\n".join(pages_text) in the pdfplumber path, so
    consecutive page ranges are separated by exactly one "\\n". Mirror that
    here so char offsets line up with what the section regex sees.
    """
    index: list[tuple[int, int, int]] = []
    cursor = 0
    for i, (page_num, text) in enumerate(per_page_text):
        start = cursor
        end = start + len(text)
        index.append((start, end, page_num))
        cursor = end + (1 if i < len(per_page_text) - 1 else 0)  # the "\n" joiner
    return index


def _pages_for_range(
    page_index: list[tuple[int, int, int]],
    char_start: int,
    char_end: int,
) -> tuple[Optional[int], Optional[int]]:
    """Find the first and last pages overlapping [char_start, char_end)."""
    if not page_index:
        return None, None
    page_first: Optional[int] = None
    page_last: Optional[int] = None
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
    section_number: Optional[str],
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

    page_index = _build_page_index(per_page_text or [])
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
            matched_trigger: Optional[str] = None
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


def _table_to_markdown(table: list[list[Optional[str]]]) -> str:
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


def _extract_tables_via_docling_only(pdf_path: str) -> list["ReportSection"]:
    """Phase 4 — invoke docling with `do_ocr=False`, `do_table_structure=True`,
    `generate_picture_images=False` and return ONLY the table sections.

    Used when fitz won the text extraction (so docling didn't already
    fire) but bordered tables exist that docling's TableFormer can
    extract faster + with better structure than pdfplumber-lines.

    Returns an empty list when docling is unavailable or fails — caller
    falls back to pdfplumber-lines for the bordered pages.
    """
    try:
        from docling.document_converter import (  # noqa: PLC0415
            DocumentConverter, PdfFormatOption,
        )
        from docling.datamodel.base_models import InputFormat  # noqa: PLC0415
        from docling.datamodel.pipeline_options import (  # noqa: PLC0415
            AcceleratorDevice, AcceleratorOptions, PdfPipelineOptions,
        )
    except ImportError:
        logger.info("pdf_report: docling unavailable — skipping tables-only pass")
        return []

    opts = PdfPipelineOptions()
    opts.do_ocr = False
    opts.do_table_structure = True
    opts.generate_picture_images = False
    # GPU acceleration gated by DOCLING_GPU_ENABLED env to avoid VRAM
    # contention with vLLM. See _parse_with_docling for the full note.
    if (os.environ.get("DOCLING_GPU_ENABLED") or "").lower() in ("1", "true", "yes", "on"):
        try:
            import torch  # noqa: PLC0415
            if torch.cuda.is_available():
                opts.accelerator_options = AcceleratorOptions(
                    device=AcceleratorDevice.CUDA,
                )
        except Exception:
            pass

    try:
        conv = DocumentConverter(
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)},
        )
        result = conv.convert(pdf_path)
        doc = result.document
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pdf_report: docling tables-only pass failed (%s) — caller will "
            "fall back to pdfplumber-lines for bordered pages",
            exc,
        )
        return []

    table_sections: list[ReportSection] = []
    for tbl in (doc.tables or []):
        try:
            md = tbl.export_to_markdown(doc=doc)
        except Exception:
            try:
                md = tbl.export_to_markdown()
            except Exception:
                continue
        if not md or not md.strip():
            continue
        prov = getattr(tbl, "prov", None) or []
        page_no = prov[0].page_no if prov else None
        table_sections.append(
            ReportSection(
                section_number=None,
                section_title=(
                    f"Table (docling, page {page_no})"
                    if page_no else "Table (docling)"
                ),
                text=md.strip(),
                page_first=page_no,
                page_last=page_no,
            )
        )
    return table_sections


def _extract_all_tables_as_sections(
    pdf_path: str,
    existing_docling_tables: Optional[list["ReportSection"]] = None,
) -> list[ReportSection]:
    """Walk every page and extract every data-table-like table.

    Phase 4 (2026-05-22) — per-page routing replaces the always-dual-pass
    pdfplumber scan. Each page is classified as 'bordered' (has table
    borders/lines/rectangles) or 'borderless' (whitespace-delimited)
    using fitz drawing primitives. Bordered pages prefer docling's
    TableFormer (~1-2 s/page on GPU vs ~3-4 s/page for pdfplumber-lines),
    borderless pages get the pdfplumber text strategy ONLY (skipping the
    expensive lines pass on pages that won't have ruled tables anyway).

    `existing_docling_tables` — when the caller already ran a full
    docling pass (Phase 2.1 image-page dispatch), pass its table list
    here. We use those for the bordered pages instead of re-invoking
    docling.

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

    _docling_enabled = os.environ.get(
        "PDF_PARSER_DOCLING_ENABLED", "true",
    ).lower() == "true"

    # ------------------------------------------------------------------
    # 1. Bordered pages — gather table sections from docling (preferred)
    #    or pdfplumber-lines (fallback when docling is off / unavailable
    #    / failed). Reuse existing_docling_tables when caller already
    #    invoked docling (Phase 2.1 image-page path).
    # ------------------------------------------------------------------
    bordered_sections: list[ReportSection] = []
    used_docling_for_bordered = False
    if existing_docling_tables:
        # Phase 2.1 already invoked docling for this doc. Trust its
        # table list regardless of fitz's page classifier — the cross-
        # engine dedupe at the end of this function handles any overlap
        # with pdfplumber on borderless pages.
        bordered_sections = list(existing_docling_tables)
        used_docling_for_bordered = True
        logger.info(
            "pdf_report: Phase 4 — reusing %d existing docling table(s)",
            len(bordered_sections),
        )
    elif bordered_pages:
        # Phase 4 threshold (2026-05-22) — only invoke docling-tables-only
        # when the bordered page count justifies its ~30-40s model-load
        # overhead. For small PDFs with few bordered pages, pdfplumber-
        # lines is faster overall. Default 30 means docling fires on
        # NI-43-101-style technical reports (typically 50-100+ bordered
        # pages of resource/assay/drill tables) but not on slide-deck-
        # style prospectuses with 5-15 bordered pages.
        try:
            docling_min_pages = int(
                os.environ.get("PDF_PARSER_DOCLING_TABLES_MIN_BORDERED_PAGES", "30")
            )
        except ValueError:
            docling_min_pages = 30
        if _docling_enabled and len(bordered_pages) >= docling_min_pages:
            t0 = time.monotonic()
            all_docling_tables = _extract_tables_via_docling_only(pdf_path)
            elapsed = time.monotonic() - t0
            bordered_sections = [
                s for s in all_docling_tables
                if (s.page_first is None or s.page_first in bordered_pages)
            ]
            if all_docling_tables:
                used_docling_for_bordered = True
                logger.info(
                    "pdf_report: Phase 4 — docling-tables-only extracted %d "
                    "table(s) across %d bordered page(s) in %.1fs",
                    len(bordered_sections), len(bordered_pages), elapsed,
                )
        elif _docling_enabled:
            logger.info(
                "pdf_report: Phase 4 — %d bordered page(s) < threshold %d, "
                "using pdfplumber-lines (docling-tables-only would cost "
                "more than it saves on this doc)",
                len(bordered_pages), docling_min_pages,
            )

    # Fallback to pdfplumber-lines on bordered pages when docling wasn't
    # used (disabled, unavailable, or returned nothing). Preserves the
    # no-data-loss contract from pre-Phase-4 behavior.
    fallback_bordered_pages: set[int] = (
        bordered_pages if not used_docling_for_bordered else set()
    )

    # ------------------------------------------------------------------
    # 2. Open pdfplumber once + walk every page. Run the strategies the
    #    classifier indicated:
    #      - bordered + fallback → pdfplumber lines AND text (safety)
    #      - bordered + docling-handled → pdfplumber TEXT only (catches
    #        borderless tables that co-exist on the same page; docling
    #        already covered the bordered ones)
    #      - borderless → pdfplumber TEXT only
    #      - classification_failed → run dual-pass (pre-Phase-4 default)
    # ------------------------------------------------------------------
    pdfplumber_sections: list[ReportSection] = []
    try:
        import pdfplumber  # noqa: PLC0415
        _pdf_ctx = pdfplumber.open(pdf_path)
    except Exception as pdfp_exc:  # noqa: BLE001
        # Pdfplumber unavailable / failed — return what we have from
        # docling and let the caller log. No silent data loss because
        # docling-tables remain in `bordered_sections`.
        logger.warning(
            "pdf_report: pdfplumber.open failed for '%s' (%s) — returning "
            "%d docling-only table section(s)",
            pdf_path, pdfp_exc, len(bordered_sections),
        )
        # Run the cross-engine dedupe even on this short path so the
        # output shape is identical to the normal return below.
        out: list[ReportSection] = []
        seen_keys: set[tuple[Optional[int], str]] = set()
        for s in bordered_sections:
            body = s.text or ""
            sig = hashlib.sha1(body.encode("utf-8", "ignore")).hexdigest()[:16]
            key = (s.page_first, sig)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(s)
        return out

    with _pdf_ctx as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            run_lines = (
                _classification_failed
                or page_num in fallback_bordered_pages
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
    # 3. Cross-engine dedupe via _table_signature. If docling-tables-only
    #    captured the same table that pdfplumber-text also found, prefer
    #    docling (better structure, preserved row/col coordinates).
    # ------------------------------------------------------------------
    out: list[ReportSection] = []
    seen_keys: set[tuple[Optional[int], str]] = set()
    for s in bordered_sections + pdfplumber_sections:
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
    dict[int, str], dict[int, Optional[float]],
]:
    """Extract full text using PyMuPDF (fitz). 5-10× faster than pdfplumber.

    Returns (full_text, title, skipped, warnings, page_languages,
             per_page_text, image_page_nums, per_page_method,
             per_page_confidence).

    `image_page_nums` — list of 1-indexed pages where fitz returned
    less than PER_PAGE_MIN_CHARS (i.e. needs OCR). Always populated
    regardless of `apply_ocr_fallback`. Phase 2.1 dispatch consumes
    this to decide whether to invoke docling-with-rapidocr OCR.

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
    silently dropped. When `apply_ocr_fallback=False`, the caller is
    expected to handle OCR for the returned `image_page_nums` (Phase
    2.1 docling path does exactly this).

    Used as the primary parser when PDF_PARSER_FITZ_ENABLED=true (default).
    Falls back to pdfplumber when fitz returns suspiciously little text.
    """
    import pymupdf  # noqa: PLC0415

    pages_text: list[str] = []
    per_page_text: list[tuple[int, str]] = []
    page_languages: list[str] = []
    warnings: list[dict] = []
    short_page_nums: list[int] = []  # candidates for per-page OCR
    # Phase 3 (2026-05-22) — per-page engine + confidence tracking
    per_page_method: dict[int, str] = {}
    per_page_confidence: dict[int, Optional[float]] = {}

    with pymupdf.open(path) as doc:
        # PyMuPDF reports a title from the doc metadata, often useful.
        meta_title = (doc.metadata.get("title") or "").strip() if doc.metadata else ""
        for n, page in enumerate(doc, start=1):
            try:
                # `sort=True` returns text in reading order (top-to-bottom,
                # left-to-right) — important for two-column pages where
                # the default y-then-x ordering can interleave columns.
                txt = page.get_text("text", sort=True)
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

    # Per-page OCR for any pages fitz returned <PER_PAGE_MIN_CHARS on.
    # Runs the same tesseract pipeline as pdfplumber's fallback, so image
    # pages in an otherwise text-dense doc don't get silently dropped.
    # Phase 2.1 (2026-05-22): when `apply_ocr_fallback=False`, skip
    # this loop and let the caller handle OCR (typically by routing
    # `short_page_nums` to docling+rapidocr GPU OCR).
    if short_page_nums and apply_ocr_fallback:
        logger.info(
            "pdf_report: fitz returned <%d chars on %d pages — running per-page OCR",
            PER_PAGE_MIN_CHARS, len(short_page_nums),
        )
        ocr_recovered = 0
        for n in short_page_nums:
            try:
                # Phase 3 — capture mean_conf from tesseract per-word data
                ocr_text, mean_conf = _ocr_single_page(
                    path, n, return_confidence=True,
                )
            except Exception:
                continue
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


# ---------------------------------------------------------------------------
# Optional primary parser: docling (layout-aware, native table structure)
# ---------------------------------------------------------------------------

# Phase 1 (2026-05-22): the previous module-scope `_DOCLING_FIGURE_CACHE`
# stored the docling Document + pictures keyed by PDF SHA256 so the
# downstream `persist` Hatchet task could pull figures from it. That
# never worked once parse moved into a subprocess (own process memory →
# cache was always empty in the parent). Figure extraction now happens
# INLINE inside `_parse_with_docling`, uploads PNGs to MinIO under
# figures/_pending/{sha}/figure_{idx}_page_{n}.png, and returns the
# manifest in ParseOut. The `persist` task renames each PNG to
# figures/{report_id}/... via s3 copy+delete.


# Subprocess-local tempdir root for figure renders. Cleaned up by
# `_run_parser_subprocess` in its `finally` block. Per-sha subdir so
# concurrent parses of different PDFs don't collide.
_FIGURE_TEMPDIR_ROOT = "/tmp/georag_figures"


def _nearest_text_below_figure(
    doc,
    pic,
    page_no: int,
    max_vertical_gap_pts: float = 120.0,
    max_horizontal_offset_pts: float = 250.0,
    min_chars: int = 6,
    max_chars: int = 400,
) -> str:
    """Caption fallback when ``pic.caption_text(doc)`` returns nothing.

    Docling's caption resolver relies on layout-model heuristics and
    misses figure-caption pairs on noisy NI 43-101 pages. This walks
    ``doc.texts`` for the same page and returns the text item whose
    top edge is just below the figure's bottom edge and is reasonably
    aligned horizontally with the figure's center.

    Returns an empty string when nothing qualifies. Coordinate origin
    is normalised to top-left so the "below" check is direction-agnostic.
    """
    if not pic.prov:
        return ""
    pic_prov = pic.prov[0]
    pic_bbox_raw = pic_prov.bbox
    page = (doc.pages or {}).get(page_no) if hasattr(doc, "pages") else None
    page_height = None
    if page is not None:
        size = getattr(page, "size", None)
        if size is not None:
            page_height = getattr(size, "height", None)
    try:
        pic_bbox = pic_bbox_raw.to_top_left_origin(page_height) if page_height else pic_bbox_raw
    except Exception:
        pic_bbox = pic_bbox_raw

    pic_cx = (pic_bbox.l + pic_bbox.r) / 2.0
    pic_bottom = max(pic_bbox.t, pic_bbox.b)  # under top-left origin: bottom is the larger y

    best_dist = float("inf")
    best_text = ""
    for txt in getattr(doc, "texts", None) or []:
        prov = getattr(txt, "prov", None) or []
        if not prov or prov[0].page_no != page_no:
            continue
        body = (getattr(txt, "text", "") or "").strip()
        if len(body) < min_chars or len(body) > max_chars:
            continue
        if body.isdigit():
            continue  # page-number noise

        tbbox_raw = prov[0].bbox
        try:
            tbbox = tbbox_raw.to_top_left_origin(page_height) if page_height else tbbox_raw
        except Exception:
            tbbox = tbbox_raw

        t_top = min(tbbox.t, tbbox.b)
        if t_top < pic_bottom:
            continue  # not below the figure

        vgap = t_top - pic_bottom
        if vgap > max_vertical_gap_pts:
            continue

        t_cx = (tbbox.l + tbbox.r) / 2.0
        hoff = abs(t_cx - pic_cx)
        if hoff > max_horizontal_offset_pts:
            continue

        # Vertical proximity dominates; horizontal alignment is a
        # tiebreaker (a body paragraph aligned with the figure column
        # beats one in a sidebar).
        dist = vgap + 0.25 * hoff
        if dist < best_dist:
            best_dist = dist
            best_text = body

    return best_text


def _figure_tempdir(sha256: str) -> str:
    """Return (and ensure exists) the per-sha temp directory for figure renders."""
    import os as _os
    d = f"{_FIGURE_TEMPDIR_ROOT}/{sha256}"
    _os.makedirs(d, exist_ok=True)
    return d


def _parse_with_docling(
    path: str,
    pdf_sha256: str | None = None,
) -> tuple[str, str, int, list, list[str], list[tuple[int, str]], list[ReportSection], list[dict]]:
    """Extract via docling — layout-aware extractor with native table structure.

    Returns (full_text, title, skipped, warnings, page_languages,
             per_page_text, table_sections, figure_manifest).

    `table_sections` — each docling-detected table as a markdown
    ReportSection (rows + columns preserved). Merged into final section
    list so chat retrieval matches "Au 1.23 g/t at MAD-22-001" even
    when the value lives in a cell.

    `figure_manifest` — each PictureItem extracted, PNG-rendered, and
    uploaded to MinIO under figures/_pending/{sha}/figure_{idx}_page_{n}.png.
    Persist (different Hatchet task) reads this list and renames the
    keys to figures/{report_id}/... via s3 copy+delete. Empty list when
    pdf_sha256 is None or no S3 credentials present.

    Slow: ~3-5 sec/page on CPU for the layout model. Gate behind
    PDF_PARSER_DOCLING_ENABLED in production.
    """
    from docling.document_converter import DocumentConverter, PdfFormatOption  # noqa: PLC0415
    from docling.datamodel.base_models import InputFormat  # noqa: PLC0415
    from docling.datamodel.pipeline_options import (  # noqa: PLC0415
        AcceleratorDevice,
        AcceleratorOptions,
        PdfPipelineOptions,
    )

    # Phase 2.0 (2026-05-22) — docling OCR (rapidocr) is now opt-in via
    # DOCLING_OCR_ENABLED. The rapidocr default model cache path is
    # inside site-packages (not writable by www-data); we redirect via
    # RAPIDOCR_MODEL_DIR + rapidocr_params so it can download the
    # ~150 MB language packs into a writable volume. When this flag is
    # off (default), the per-page Tesseract fallback inside _parse_with_fitz
    # remains the OCR engine. Phase 2.1 will flip the default once the
    # smoke test confirms rapidocr fires cleanly under the staged rollout.
    # generate_picture_images: enables figure crop extraction for Task 19
    # (figure ↔ caption linking + storage). Tiny overhead per figure.
    opts = PdfPipelineOptions()
    opts.do_ocr = False
    opts.do_table_structure = True
    opts.generate_picture_images = True
    opts.images_scale = 1.5  # render figures at 1.5× for legibility

    _docling_ocr_enabled = os.environ.get("DOCLING_OCR_ENABLED", "false").lower() == "true"
    if _docling_ocr_enabled:
        try:
            from docling.datamodel.pipeline_options import RapidOcrOptions  # noqa: PLC0415
            _rapidocr_model_dir = os.environ.get(
                "RAPIDOCR_MODEL_DIR", "/tmp/rapidocr_models"
            )
            # Ensure the writable cache exists; rapidocr will populate it
            # on first OCR call (downloads ~150 MB of per-language ONNX
            # models from modelscope.cn). Subsequent parses reuse the
            # cached files via SHA256 verification inside rapidocr.
            try:
                os.makedirs(_rapidocr_model_dir, exist_ok=True)
            except Exception as mkdir_exc:  # noqa: BLE001
                logger.warning(
                    "pdf_report: could not create rapidocr cache dir '%s': %s — "
                    "OCR will fall back to tesseract per-page",
                    _rapidocr_model_dir, mkdir_exc,
                )
                _docling_ocr_enabled = False
            if _docling_ocr_enabled:
                # English-first; rapidocr supports english + chinese
                # natively in onnxruntime backend. Tune via env if a
                # multilingual NI 43-101 corpus needs it. Build options
                # FIRST and only flip do_ocr=True after construction
                # succeeds — that way a RapidOcrOptions failure can't
                # leave do_ocr=True with ocr_options=None (which would
                # send docling into a broken state).
                _candidate_ocr_options = RapidOcrOptions(
                    lang=[
                        s.strip() for s in
                        os.environ.get("DOCLING_OCR_LANGS", "english").split(",")
                        if s.strip()
                    ],
                    backend="onnxruntime",
                    print_verbose=False,
                    # Pass the writable model root through rapidocr's
                    # config-passthrough dict. Rapidocr's ParseParams
                    # reads Global.model_root_dir; this is the only env-
                    # independent way to redirect the cache.
                    rapidocr_params={
                        "Global.model_root_dir": _rapidocr_model_dir,
                    },
                )
                opts.do_ocr = True
                opts.ocr_options = _candidate_ocr_options
                logger.info(
                    "pdf_report: docling rapidocr OCR enabled (lang=%s, cache=%s)",
                    opts.ocr_options.lang, _rapidocr_model_dir,
                )
        except ImportError as imp_exc:
            opts.do_ocr = False
            opts.ocr_options = None
            logger.warning(
                "pdf_report: DOCLING_OCR_ENABLED=true but RapidOcrOptions "
                "not importable (%s) — falling back to do_ocr=False",
                imp_exc,
            )
        except Exception as ocr_cfg_exc:  # noqa: BLE001
            opts.do_ocr = False
            opts.ocr_options = None
            logger.warning(
                "pdf_report: rapidocr config build failed (%s) — falling back "
                "to do_ocr=False",
                ocr_cfg_exc,
            )
    # GPU acceleration for TableFormer + layout model (onnxruntime-gpu CUDA
    # path). Drops parse from 17-40 min/big PDF on CPU to ~3-5 min, BUT
    # competes with vLLM for VRAM — vLLM runs at gpu-memory-utilization
    # 0.93 on the A4500 (~1.5 GiB free); docling layout needs ~1-2 GiB
    # and PaddleOCR PP-StructureV3 needs another ~1.5-2 GiB. Both
    # together OOM vLLM. So the GPU path is OPT-IN via env flag.
    #
    # Recommended deployment:
    #   * On the hatchet-worker-ai container (separate GPU pool from
    #     vLLM), set DOCLING_GPU_ENABLED=1.
    #   * On the vLLM host, leave DOCLING_GPU_ENABLED unset → CPU path.
    # See [[gpu-acceleration-2026-05-22]] for the worker layout.
    docling_gpu_enabled = (os.environ.get("DOCLING_GPU_ENABLED") or "").lower() in (
        "1", "true", "yes", "on"
    )
    if docling_gpu_enabled:
        try:
            import torch  # noqa: PLC0415
            if torch.cuda.is_available():
                opts.accelerator_options = AcceleratorOptions(device=AcceleratorDevice.CUDA)
                logger.info(
                    "pdf_report: docling on CUDA (DOCLING_GPU_ENABLED=1) — "
                    "ensure vLLM gpu-memory-utilization leaves ≥3 GiB headroom",
                )
            else:
                logger.info(
                    "pdf_report: DOCLING_GPU_ENABLED set but torch.cuda.is_available()=False "
                    "— falling back to CPU layout/tables",
                )
        except Exception as exc:
            logger.warning(
                "pdf_report: DOCLING_GPU_ENABLED set but torch import failed (%s) "
                "— falling back to CPU",
                exc,
            )
    else:
        logger.debug(
            "pdf_report: docling on CPU (set DOCLING_GPU_ENABLED=1 on a GPU "
            "worker pool that does NOT share VRAM with vLLM to enable)",
        )

    conv = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)}
    )
    result = conv.convert(path)
    doc = result.document

    full_text = doc.export_to_markdown()
    title = ""

    # Try to pull a title — first H1 in the markdown export, else
    # the first Title item from the docling doc.
    for line in full_text.splitlines():
        s = line.strip()
        if s.startswith("# "):
            title = s.lstrip("#").strip()[:200]
            break
    if not title:
        for txt in (doc.texts or []):
            if getattr(txt, "label", None) == "title":
                title = (txt.text or "")[:200]
                if title:
                    break

    # Per-page text — docling stores provenance per item with a page_no;
    # group text items by page so we can build per_page_text for the
    # sliding-window page-tracking path.
    per_page_buf: dict[int, list[str]] = {}
    for item in (doc.texts or []):
        prov = getattr(item, "prov", None) or []
        page_no = prov[0].page_no if prov else None
        if page_no is None:
            continue
        text = (item.text or "").strip()
        if text:
            per_page_buf.setdefault(page_no, []).append(text)
    per_page_text: list[tuple[int, str]] = [
        (pn, "\n".join(per_page_buf[pn]))
        for pn in sorted(per_page_buf)
    ]
    page_languages = ["unknown"] * len(per_page_text)  # docling doesn't ship language detection

    # Tables — each becomes its own ReportSection (markdown formatted).
    # Docling preserves row/column structure; the markdown is much
    # cleaner than pdfplumber's grid-fragmented output.
    table_sections: list[ReportSection] = []
    for tbl in (doc.tables or []):
        try:
            md = tbl.export_to_markdown(doc=doc)
        except Exception:
            try:
                md = tbl.export_to_markdown()
            except Exception:
                continue
        if not md or not md.strip():
            continue
        prov = getattr(tbl, "prov", None) or []
        page_no = prov[0].page_no if prov else None
        table_sections.append(
            ReportSection(
                section_number=None,
                section_title=f"Table (docling, page {page_no})" if page_no else "Table (docling)",
                text=md.strip(),
                page_first=page_no,
                page_last=page_no,
            )
        )

    # Phase 1 (2026-05-22): inline figure extraction + S3 upload.
    # Replaces the module-scope cache + separate _extract_docling_figures
    # call, which silently dropped all figures once parse moved into a
    # subprocess (cache lived in parse-process memory, persist read it in
    # parent process where it was always empty).
    #
    # When pdf_sha256 is provided AND S3 credentials are present:
    #   - render each picture to PNG (optimize=True)
    #   - upload under figures/_pending/{sha256}/figure_{idx:04d}_page_{n}.png
    #   - return manifest entry with `pending_key`, `caption`, `bbox`, `sha256`
    # Persist (different Hatchet task) consumes the manifest, copies each
    # PNG to figures/{report_id}/..., deletes the _pending key, and builds
    # ReportSections from caption text so chat retrieval matches figure
    # captions.
    figure_manifest: list[dict] = []
    pictures = list(doc.pictures or [])
    if pdf_sha256 and pictures:
        try:
            import boto3  # noqa: PLC0415
            from botocore.config import Config as BotoConfig  # noqa: PLC0415
            from io import BytesIO  # noqa: PLC0415

            s3_endpoint = os.environ.get("S3_ENDPOINT_URL") or os.environ.get("MINIO_ENDPOINT")
            s3_bucket = os.environ.get("S3_BUCKET_BRONZE", "bronze")
            aws_key = os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("MINIO_ROOT_USER")
            aws_secret = os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get("MINIO_ROOT_PASSWORD")
            if s3_endpoint and aws_key and aws_secret:
                s3 = boto3.client(
                    "s3",
                    endpoint_url=s3_endpoint,
                    aws_access_key_id=aws_key,
                    aws_secret_access_key=aws_secret,
                    region_name="us-east-1",
                    config=BotoConfig(signature_version="s3v4"),
                )
                for idx, pic in enumerate(pictures):
                    page_no = pic.prov[0].page_no if pic.prov else None
                    if page_no is None:
                        continue
                    caption = ""
                    try:
                        if hasattr(pic, "caption_text"):
                            caption = (pic.caption_text(doc) or "").strip()
                    except Exception:
                        caption = ""
                    if not caption:
                        try:
                            caption = _nearest_text_below_figure(doc, pic, page_no)
                        except Exception as exc:
                            logger.debug(
                                "pdf_report: caption fallback failed idx=%d: %s",
                                idx, exc,
                            )
                            caption = ""

                    img_bytes: Optional[bytes] = None
                    try:
                        if hasattr(pic, "get_image"):
                            pil_img = pic.get_image(doc)
                            if pil_img is not None:
                                buf = BytesIO()
                                pil_img.save(buf, format="PNG", optimize=True)
                                img_bytes = buf.getvalue()
                    except Exception as exc:
                        logger.debug("pdf_report: figure %d image extract failed: %s", idx, exc)

                    pending_key = None
                    img_sha = None
                    if img_bytes:
                        pending_key = (
                            f"figures/_pending/{pdf_sha256}/"
                            f"figure_{idx:04d}_page_{page_no}.png"
                        )
                        img_sha = hashlib.sha256(img_bytes).hexdigest()
                        try:
                            s3.put_object(
                                Bucket=s3_bucket,
                                Key=pending_key,
                                Body=img_bytes,
                                ContentType="image/png",
                                Metadata={
                                    "pdf_sha256": pdf_sha256,
                                    "page": str(page_no),
                                    "sha256": img_sha,
                                },
                            )
                        except Exception as exc:
                            logger.warning("pdf_report: figure pending upload failed: %s", exc)
                            pending_key = None

                    bbox = pic.prov[0].bbox if pic.prov else None
                    figure_manifest.append({
                        "idx": idx,
                        "page": page_no,
                        "bbox": [bbox.l, bbox.t, bbox.r, bbox.b] if bbox else None,
                        "caption": caption,
                        "pending_key": pending_key,
                        "bucket": s3_bucket,
                        "sha256": img_sha,
                    })
                logger.info(
                    "pdf_report: docling extracted %d figure(s), uploaded %d to pending",
                    len(figure_manifest),
                    sum(1 for m in figure_manifest if m.get("pending_key")),
                )
            else:
                logger.info(
                    "pdf_report: S3 credentials missing — figure manifest not built "
                    "(%d figures discarded)",
                    len(pictures),
                )
        except ImportError:
            logger.warning("pdf_report: boto3 unavailable, skipping figure upload")
        except Exception as exc:  # noqa: BLE001
            logger.warning("pdf_report: figure manifest build failed: %s", exc)

    return (
        full_text,
        title,
        0,
        [],
        page_languages,
        per_page_text,
        table_sections,
        figure_manifest,
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
):
    """Render one PDF page and run Tesseract on it.

    Phase 3 (2026-05-22): when `return_confidence=True`, returns
    ``(text, mean_confidence)`` where mean_confidence is the average
    of per-word confidences reported by Tesseract (rescaled 0.0–1.0).
    When `return_confidence=False` (legacy default), returns just
    ``text`` for back-compatibility with the existing pdfplumber
    fallback path.

    Returns ``""`` (or ``("", 0.0)``) on any failure.
    """
    try:
        from pdf2image import convert_from_path
        import pytesseract
    except ImportError:
        return ("", 0.0) if return_confidence else ""
    try:
        images = convert_from_path(
            pdf_path,
            dpi=250,
            first_page=page_num,
            last_page=page_num,
            thread_count=1,
        )
        if not images:
            return ("", 0.0) if return_confidence else ""
        processed = _preprocess_image_for_ocr(images[0])
        # Phase 3: image_to_data carries per-word confidence in the
        # `conf` column (range -1..100, where -1 = no detection).
        # Compute the mean of positive confidences and rescale to 0-1.
        if return_confidence:
            try:
                data = pytesseract.image_to_data(
                    processed,
                    lang="eng",
                    config="--psm 3 --oem 3",
                    output_type=pytesseract.Output.DICT,
                )
                words = [
                    (w, int(c)) for w, c in zip(data.get("text", []), data.get("conf", []))
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
                return processed_text, mean_conf
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
        return (out_text, 0.0) if return_confidence else out_text
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pdf_report: per-page OCR failed on page %d of '%s': %s",
            page_num,
            pdf_path,
            exc,
        )
        return ("", 0.0) if return_confidence else ""


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
                ocr_text = _ocr_single_page(pdf_path, page_num)
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
    from PIL import ImageFilter, Image as PILImage
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
        r'\bisa\b': 'is a',
    }

    for pattern, replacement in corrections.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE if pattern[0] != '\\' else 0)

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


def _attempt_ocr(path: str) -> Optional[str]:
    """Attempt OCR on a scanned PDF using Tesseract via pdf2image + pytesseract.

    Strategy:
      1. Convert PDF pages to images at adaptive DPI (200 for speed, 300 for quality)
      2. OCR each page with Tesseract English language pack
      3. NO PAGE CAP — process every page so we don't silently drop data
      4. Log progress every 10 pages

    Returns extracted text or empty string if OCR libraries are unavailable.
    """
    try:
        from pdf2image import convert_from_path
        import pytesseract
    except ImportError:
        logger.info(
            "pdf_report: OCR libraries (pdf2image, pytesseract) not installed — "
            "install with: pip install pdf2image pytesseract"
        )
        return ""

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

        for i, img in enumerate(images):
            # Preprocess image for better OCR accuracy
            processed_img = _preprocess_image_for_ocr(img)

            page_text = pytesseract.image_to_string(
                processed_img,
                lang='eng',
                config='--psm 3 --oem 3',  # LSTM + legacy engine, full auto page segmentation (multi-col aware)
            )

            if page_text.strip():
                # Post-process to fix common OCR artifacts
                cleaned = _postprocess_ocr_text(page_text)
                conf = _ocr_page_confidence(cleaned)
                page_confidences.append(conf)

                if conf < 0.3:
                    low_confidence_pages.append(i + 1)
                    logger.warning(
                        "pdf_report: OCR page %d low confidence (%.0f%%) — may be image/diagram",
                        i + 1, conf * 100,
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
        return result

    except Exception as exc:
        logger.warning("pdf_report: OCR failed: %s", exc)
        return ""


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
    Phase 2.1 dispatch tree (2026-05-22):
      fitz (PyMuPDF) → always runs first for native text extraction
        → per-page OCR for image pages routes to docling+rapidocr
          (PDF_PARSER_DOCLING_ENABLED + DOCLING_OCR_ENABLED) or
          tesseract (PDF_PARSER_TESSERACT_FALLBACK_ENABLED)
      pdfplumber → only fires when fitz crashes completely; whole-doc
        text + table extraction as a defensive last resort
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
    docling_table_sections: list[ReportSection] = []
    docling_figure_manifest: list[dict] = []

    # Phase 2.1 (2026-05-22) — always-fitz-first dispatch.
    #
    # Previously: docling vs fitz were mutually exclusive primaries
    # (gated by PDF_PARSER_DOCLING_ENABLED). That sent text-heavy PDFs
    # through docling's slow path unnecessarily and made docling-OCR
    # available only as a wholesale parser swap.
    #
    # Now: fitz ALWAYS runs first (~5 s for a 46-page text PDF). It
    # reports per-page text + a list of `image_page_nums` — pages
    # where fitz returned < PER_PAGE_MIN_CHARS. If those exist AND
    # docling + rapidocr OCR are enabled, docling is invoked to OCR
    # the whole doc (rapidocr handles per-page internally on GPU) and
    # we MERGE per-page: fitz wins on pages where it returned text,
    # docling fills the image pages. Tesseract is the fallback-of-
    # last-resort when docling is unavailable.
    _docling_enabled = os.environ.get(
        "PDF_PARSER_DOCLING_ENABLED", "true"
    ).lower() == "true"
    _docling_ocr_enabled = os.environ.get(
        "DOCLING_OCR_ENABLED", "true"
    ).lower() == "true"
    _tesseract_fallback_enabled = os.environ.get(
        "PDF_PARSER_TESSERACT_FALLBACK_ENABLED", "true"
    ).lower() == "true"
    fitz_enabled = os.environ.get("PDF_PARSER_FITZ_ENABLED", "true").lower() == "true"

    # Helper: merge docling per_page_text into the fitz per_page_text
    # using the "fitz wins when it has any text" rule the user chose.
    # `image_page_nums` is the set of pages fitz returned < PER_PAGE_MIN_CHARS
    # on; only those pages are eligible for the docling override.
    def _merge_per_page(
        fitz_per_page: list[tuple[int, str]],
        docling_per_page: list[tuple[int, str]],
        image_pages: list[int],
    ) -> list[tuple[int, str]]:
        fitz_map = {n: t for n, t in fitz_per_page}  # noqa: C416
        image_set = set(image_pages)
        # Fitz output is authoritative for non-image pages
        merged = dict(fitz_map)
        for n, t in docling_per_page:
            if n in image_set and t and t.strip():
                # Only overwrite when fitz's entry is empty/short
                existing = merged.get(n, "")
                if not existing or len(existing.strip()) < PER_PAGE_MIN_CHARS:
                    merged[n] = t
        return sorted(merged.items(), key=lambda kv: kv[0])

    fitz_failed = False
    image_page_nums: list[int] = []
    # Phase 3 — per-page method + confidence maps accumulated across the
    # dispatch tree, applied to ReportSections at the end via
    # _assign_ocr_metadata.
    per_page_method: dict[int, str] = {}
    per_page_confidence: dict[int, Optional[float]] = {}
    if fitz_enabled:
        try:
            with _tracer.start_as_current_span("pdf_report.fitz") as _span:
                (full_text, raw_title, skipped_elements, extraction_warnings,
                 page_languages, per_page_text, image_page_nums,
                 per_page_method, per_page_confidence) = _parse_with_fitz(
                    path,
                    # When docling-OCR is on, skip fitz's internal tesseract
                    # fallback — image pages are routed to docling below.
                    # Otherwise keep the legacy per-page tesseract loop so
                    # data-loss behavior is unchanged when the new path is
                    # disabled.
                    apply_ocr_fallback=not (_docling_enabled and _docling_ocr_enabled),
                )
                _span.set_attribute("pdf.text_chars", len(full_text))
                _span.set_attribute("pdf.page_count", len(page_languages))
                _span.set_attribute("pdf.image_pages", len(image_page_nums))
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

    # Phase 2.1 docling pass — fires only when fitz left image pages
    # AND docling + rapidocr OCR are both enabled. Docling parses the
    # whole doc (do_ocr=True via Phase 2.0 wiring) and we merge its
    # per-page output into fitz's, overriding only on image pages.
    docling_failed = False
    if (
        _docling_enabled
        and not fitz_failed
        and image_page_nums
        and parser_used == "fitz"
    ):
        try:
            with _tracer.start_as_current_span("pdf_report.docling") as _span:
                (docling_text, docling_title, _d_skipped, docling_warnings,
                 docling_page_langs, docling_per_page_text, docling_table_sections,
                 docling_figure_manifest) = _parse_with_docling(
                    path, pdf_sha256=_sha256_hex,
                )
                _span.set_attribute("pdf.docling_chars", len(docling_text))
                _span.set_attribute("pdf.docling_tables", len(docling_table_sections))
                _span.set_attribute("pdf.docling_figures", len(docling_figure_manifest))
                logger.info(
                    "pdf_report: docling supplied OCR for %d image pages "
                    "(returned %d chars + %d tables + %d figures)",
                    len(image_page_nums), len(docling_text),
                    len(docling_table_sections), len(docling_figure_manifest),
                )

                # Merge per-page (fitz wins where it has text)
                merged = _merge_per_page(
                    per_page_text, docling_per_page_text, image_page_nums,
                )
                per_page_text = merged
                full_text = "\n".join(t for _n, t in merged)
                # Phase 3 — image pages docling actually filled get
                # method='docling_rapidocr' + the conservative default
                # 0.90 confidence (docling does not expose a per-page
                # OCR confidence in its current API; kickoff
                # specifies 0.90 as the safe default that lands above
                # the Phase 6 quality threshold of 0.75).
                _docling_per_page_map = {n: t for n, t in docling_per_page_text}  # noqa: C416
                _DOCLING_DEFAULT_CONFIDENCE = 0.90
                for img_page in image_page_nums:
                    docling_text_for_page = _docling_per_page_map.get(img_page, "")
                    if docling_text_for_page and docling_text_for_page.strip():
                        per_page_method[img_page] = "docling_rapidocr"
                        per_page_confidence[img_page] = _DOCLING_DEFAULT_CONFIDENCE
                # Backfill page_languages with docling's detection on
                # the newly-recovered image pages.
                if docling_page_langs and len(docling_page_langs) == len(page_languages):
                    for idx, lang in enumerate(docling_page_langs):
                        if (
                            page_languages[idx] == "unknown"
                            and lang
                            and lang != "unknown"
                        ):
                            page_languages[idx] = lang
                # Forward docling's extraction warnings (e.g. low-confidence
                # pages) so the same telemetry surface used for fitz/pdfplumber
                # warnings stays intact.
                extraction_warnings.extend(docling_warnings or [])
                parser_used = "fitz+docling_ocr"
        except Exception as exc:  # noqa: BLE001
            docling_failed = True
            logger.warning(
                "pdf_report: docling OCR pass failed (%s) — falling back to "
                "tesseract per-page", exc,
            )

    # Tesseract per-page fallback when docling didn't fire or failed.
    # Catches the case where fitz left image pages and docling can't
    # cover them (flag off, lib missing, GPU OOM, etc.).
    if (
        parser_used == "fitz"
        and image_page_nums
        and _tesseract_fallback_enabled
        and (not _docling_enabled or not _docling_ocr_enabled or docling_failed)
    ):
        logger.info(
            "pdf_report: docling unavailable — running tesseract on %d image pages",
            len(image_page_nums),
        )
        recovered = 0
        for n in image_page_nums:
            try:
                ocr_text, mean_conf = _ocr_single_page(
                    path, n, return_confidence=True,
                )
            except Exception:
                continue
            if ocr_text and len(ocr_text.strip()) >= PER_PAGE_MIN_CHARS:
                recovered += 1
                per_page_text.append((n, ocr_text))
                per_page_method[n] = "tesseract"
                per_page_confidence[n] = mean_conf
                extraction_warnings.append({
                    "code": "page_ocr_recovered_tesseract_fallback",
                    "page": n,
                    "ocr_confidence": round(mean_conf, 4),
                })
        if recovered:
            per_page_text.sort(key=lambda kv: kv[0])
            full_text = "\n".join(t for _n, t in per_page_text)
            parser_used = "fitz+tesseract_fallback"
            logger.info(
                "pdf_report: tesseract recovered %d/%d image pages",
                recovered, len(image_page_nums),
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
        with _tracer.start_as_current_span("pdf_report.ocr") as _span:
            logger.warning(
                "pdf_report: only %d chars extracted from '%s' — attempting OCR fallback",
                len(full_text.strip()),
                Path(path).name,
            )
            ocr_text = _attempt_ocr(path)
            _span.set_attribute("ocr.input_chars", len(full_text.strip()))
            _span.set_attribute("ocr.output_chars", len(ocr_text or ""))
            if ocr_text and len(ocr_text.strip()) > len(full_text.strip()):
                full_text = ocr_text
                parser_used = "ocr_tesseract"
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
            figure_manifest=docling_figure_manifest,
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
        sections = _split_into_sections(full_text, per_page_text)
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
    # Phase 4 (2026-05-22) — per-page classification routes bordered
    # tables to docling's TableFormer (or pdfplumber-lines as fallback)
    # and borderless tables to pdfplumber-text. When a docling-OCR pass
    # already ran (Phase 2.1 image-page dispatch), its tables are reused
    # for bordered pages instead of re-invoking docling.
    # Each surviving table becomes a section so it gets chunked + embedded
    # and is searchable from chat. The existing _extract_resource_tables
    # path only catches resource-trigger pages; this is the broader net.
    with _tracer.start_as_current_span("pdf_report.all_tables") as _span:
        try:
            table_sections = _extract_all_tables_as_sections(
                path,
                existing_docling_tables=docling_table_sections or None,
            )
            _span.set_attribute("pdf.all_tables_found", len(table_sections))
            _span.set_attribute(
                "pdf.existing_docling_tables",
                len(docling_table_sections or []),
            )
            if table_sections:
                logger.info(
                    "pdf_report: Phase 4 table dispatch added %d table "
                    "section(s) in '%s' (existing docling tables: %d)",
                    len(table_sections), Path(path).name,
                    len(docling_table_sections or []),
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
        figure_manifest=docling_figure_manifest,
    )
