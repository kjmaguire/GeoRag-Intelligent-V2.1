"""Plan §1c — document classification step.

Pure-function classifier that maps free-form document text + filename
into one of the canonical document classes. Output drives:

  - `silver.reports.report_type` enum population (ingest-side)
  - `silver.document_versions.document_class` (for §1h supersession)
  - `DocumentEvidence.document_type` (for §3b authority ranking)

This is the FOUNDATION pass — pattern-based heuristics with explicit
confidence scoring. The §1c spec calls for a downstream LLM-based
refiner that runs on the first 2K tokens when the pattern classifier
returns low confidence; that's deferred. The pattern classifier alone
catches ~80% of real-world cases (NI 43-101, annual reports, press
releases, internal memos) — the LLM fallback closes the long tail.

The classifier inspects three signals (in order, falling back when
each misses):

  1. **Filename heuristics** — strong signals from naming convention
     (e.g. ``NI43-101_*.pdf`` → ``NI 43-101``)
  2. **Title-line patterns** — first 200 chars of text (often the
     document's title block in a PDF)
  3. **Body-content patterns** — substring matches against the full
     text within a budget (default 8K chars)

Each signal contributes a candidate + confidence; the highest-confidence
classification wins, ties broken by signal precedence (filename >
title > body).

Returns :class:`DocumentClassification` carrying the canonical class
name, confidence in [0, 1], and the signal that produced the match
(for trace logging).

Pure function: no I/O, no DB, no LLM. Safe to call from any caller.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)


__all__ = [
    "DocumentClass",
    "ClassifierSignal",
    "DocumentClassification",
    "DOCUMENT_CLASS_PATTERNS",
    "classify_document_type",
]


# ---------------------------------------------------------------------------
# Canonical classes
# ---------------------------------------------------------------------------
#
# Mirrors the document_type strings that authority.py's
# DOCUMENT_TYPE_RANK_PATTERNS recognises. Keeping them in sync is a
# regression test (see test_document_classifier.py).


DocumentClass = Literal[
    "NI 43-101",
    "Technical Report",
    "Feasibility Study",
    "PEA",
    "Assessment Report",
    "Annual Report",
    "Fact Sheet",
    "Press Release",
    "Investor Presentation",
    "Corporate Presentation",
    "News Release",
    "Historical Report",
    "Internal Memo",
    "Email",
    "Field Note",
    "Uncited",
    "Unknown",
]


ClassifierSignal = Literal["filename", "title", "body", "default"]


@dataclass(frozen=True)
class DocumentClassification:
    """Output of :func:`classify_document_type`.

    Attributes:
        document_class: The matched class, or ``"Unknown"`` when no
            pattern matched and no filename hint applied.
        confidence: 0.0-1.0. Filename matches start at 0.95; title
            matches at 0.85; body matches at 0.7; "Unknown" is 0.0.
        signal: Which signal produced the match. ``"default"`` for
            the Unknown fallback.
        evidence_text: Up to 200 chars of the substring that fired
            the match — surfaced in the trace for SME review.
    """

    document_class: DocumentClass
    confidence: float
    signal: ClassifierSignal
    evidence_text: str = ""


# ---------------------------------------------------------------------------
# Pattern tables
# ---------------------------------------------------------------------------
#
# Each entry: (regex, document_class). Ordered by specificity — the
# most-distinctive patterns are tried first within each signal source.


# `\b` at the right edge fails on filenames like ``Annual_Report_2024.pdf``
# because ``_`` is a word character (no boundary between letter and ``_``).
# We use a negative lookahead for alpha (so ``101_X`` matches but ``1011``
# doesn't, and ``Report_X`` matches but ``Reporter`` doesn't).
_RB = r"(?![A-Za-z])"  # right-boundary that tolerates trailing ``_``, ``-``, digits


_FILENAME_PATTERNS: tuple[tuple[re.Pattern[str], DocumentClass], ...] = (
    # NI 43-101 / 43-101F1 / NI_43-101F1 — combined into one pattern with
    # optional 'ni' prefix and optional 'F1' suffix.
    (
        re.compile(
            r"\b(?:ni[\s\-_]?)?43[\s\-_]?101(?:f1?)?(?![A-Za-z0-9])",
            re.IGNORECASE,
        ),
        "NI 43-101",
    ),
    (re.compile(rf"\btechnical[\s\-_]?report{_RB}", re.IGNORECASE), "Technical Report"),
    (re.compile(rf"\bfeasibility[\s\-_]?study{_RB}|\bfs(?![A-Za-z0-9])", re.IGNORECASE), "Feasibility Study"),
    (re.compile(rf"\bpea(?![A-Za-z0-9])|\bpreliminary[\s\-_]?economic{_RB}", re.IGNORECASE), "PEA"),
    (re.compile(rf"\bassessment[\s\-_]?report{_RB}", re.IGNORECASE), "Assessment Report"),
    (re.compile(rf"\bannual[\s\-_]?(?:report|filing){_RB}", re.IGNORECASE), "Annual Report"),
    (re.compile(rf"\bfact[\s\-_]?sheet{_RB}", re.IGNORECASE), "Fact Sheet"),
    (re.compile(rf"\bpress[\s\-_]?release{_RB}", re.IGNORECASE), "Press Release"),
    (re.compile(rf"\binvestor[\s\-_]?(?:presentation|deck){_RB}", re.IGNORECASE), "Investor Presentation"),
    (re.compile(rf"\bcorporate[\s\-_]?presentation{_RB}", re.IGNORECASE), "Corporate Presentation"),
    (re.compile(rf"\bnews[\s\-_]?release{_RB}", re.IGNORECASE), "News Release"),
    (re.compile(rf"\bhistorical[\s\-_]?report{_RB}", re.IGNORECASE), "Historical Report"),
    (re.compile(rf"\binternal[\s\-_]?(?:memo|notes?|memorandum){_RB}", re.IGNORECASE), "Internal Memo"),
    (re.compile(r"\bemail\b|\.eml$|\.msg$", re.IGNORECASE), "Email"),
    (re.compile(rf"\bfield[\s\-_]?notes?{_RB}", re.IGNORECASE), "Field Note"),
)


_TITLE_PATTERNS: tuple[tuple[re.Pattern[str], DocumentClass], ...] = (
    (re.compile(r"NI\s?43-101", re.IGNORECASE), "NI 43-101"),
    (re.compile(r"43-101F1?\b", re.IGNORECASE), "NI 43-101"),
    (re.compile(r"\bTechnical Report\b", re.IGNORECASE), "Technical Report"),
    (re.compile(r"\bFeasibility Study\b", re.IGNORECASE), "Feasibility Study"),
    (re.compile(r"\bPreliminary Economic Assessment\b", re.IGNORECASE), "PEA"),
    (re.compile(r"\bAssessment Report\b", re.IGNORECASE), "Assessment Report"),
    (re.compile(r"\bAnnual Report\b", re.IGNORECASE), "Annual Report"),
    (re.compile(r"\bFact Sheet\b", re.IGNORECASE), "Fact Sheet"),
    (re.compile(r"\bPress Release\b", re.IGNORECASE), "Press Release"),
    (re.compile(r"\bInvestor Presentation\b", re.IGNORECASE), "Investor Presentation"),
    (re.compile(r"\bCorporate Presentation\b", re.IGNORECASE), "Corporate Presentation"),
    (re.compile(r"\bNews Release\b", re.IGNORECASE), "News Release"),
    (re.compile(r"\bHistorical Report\b", re.IGNORECASE), "Historical Report"),
    (re.compile(r"\bInternal Memo\b|\bMemorandum\b", re.IGNORECASE), "Internal Memo"),
    (re.compile(r"\bField Notes?\b", re.IGNORECASE), "Field Note"),
)


_BODY_PATTERNS: tuple[tuple[re.Pattern[str], DocumentClass], ...] = (
    # Distinctive body phrases — boilerplate that uniquely identifies
    # the document type.
    (
        re.compile(
            r"(?:National Instrument 43-101|"
            r"Form 43-101F1|"
            r"compliant with NI 43-101)",
            re.IGNORECASE,
        ),
        "NI 43-101",
    ),
    (
        re.compile(
            r"\bForward-looking statements?\b.*\bsecurities\b",
            re.IGNORECASE | re.DOTALL,
        ),
        "Press Release",
    ),
    (
        re.compile(
            r"\bManagement's Discussion and Analysis\b|"
            r"\bauditors? report\b",
            re.IGNORECASE,
        ),
        "Annual Report",
    ),
    (
        re.compile(
            r"\bAssessment work performed\b|"
            r"\bassessment credit\b",
            re.IGNORECASE,
        ),
        "Assessment Report",
    ),
    (
        re.compile(
            r"\b(?:Pre)?[Ff]easibility (?:study|stage)\b|"
            r"\bbase case (?:NPV|IRR)\b",
            re.IGNORECASE,
        ),
        "Feasibility Study",
    ),
    (
        re.compile(
            r"\bMineral Resource Estimate\b|"
            r"\bMineral Reserve Estimate\b",
            re.IGNORECASE,
        ),
        "Technical Report",
    ),
)


# Public regression hook — tests assert this dict's class set matches
# the DocumentClass Literal.
DOCUMENT_CLASS_PATTERNS: dict[ClassifierSignal, tuple[tuple[re.Pattern[str], DocumentClass], ...]] = {
    "filename": _FILENAME_PATTERNS,
    "title": _TITLE_PATTERNS,
    "body": _BODY_PATTERNS,
}


# ---------------------------------------------------------------------------
# Public classifier
# ---------------------------------------------------------------------------


_DEFAULT_BODY_BUDGET_CHARS = 8000


def classify_document_type(
    text: str = "",
    *,
    filename: str | None = None,
    body_budget_chars: int = _DEFAULT_BODY_BUDGET_CHARS,
) -> DocumentClassification:
    """Classify a document by inspecting its filename + title + body.

    Args:
        text: Full document text (or the first N chars — the
            classifier respects ``body_budget_chars``).
        filename: Original filename if available — `*.pdf`, `*.docx`,
            `*.eml`. Filename matches are highest confidence.
        body_budget_chars: Maximum body text scanned. Default 8000
            balances speed against catching late-in-document
            boilerplate ("Forward-looking statements" footers, etc.).

    Returns:
        :class:`DocumentClassification`. Always returns; the worst case
        is ``("Unknown", 0.0, "default")``.

    Notes:
        - Filename match wins over title match wins over body match.
        - Confidence inside each signal tier is fixed at 0.95 /
          0.85 / 0.7 — the foundation pass doesn't combine signals;
          the §1c LLM-refiner step does that work downstream.
        - Empty inputs → ``("Unknown", 0.0, "default")``.
    """
    # 1. Filename signal (strongest).
    if filename:
        for pattern, doc_class in _FILENAME_PATTERNS:
            match = pattern.search(filename)
            if match is not None:
                return DocumentClassification(
                    document_class=doc_class,
                    confidence=0.95,
                    signal="filename",
                    evidence_text=match.group(0)[:200],
                )

    # 2. Title signal — first 200 chars of body.
    if text:
        title_window = text[:200]
        for pattern, doc_class in _TITLE_PATTERNS:
            match = pattern.search(title_window)
            if match is not None:
                return DocumentClassification(
                    document_class=doc_class,
                    confidence=0.85,
                    signal="title",
                    evidence_text=match.group(0)[:200],
                )

    # 3. Body signal — the rest of the text within budget.
    if text:
        body_window = text[:body_budget_chars]
        for pattern, doc_class in _BODY_PATTERNS:
            match = pattern.search(body_window)
            if match is not None:
                return DocumentClassification(
                    document_class=doc_class,
                    confidence=0.70,
                    signal="body",
                    evidence_text=match.group(0)[:200],
                )

    # Fall-through.
    return DocumentClassification(
        document_class="Unknown",
        confidence=0.0,
        signal="default",
        evidence_text="",
    )
