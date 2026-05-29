"""CC-04 — Lightweight auto-classifier for the data-domain taxonomy.

Inputs a filename + mime_type + optional content snippet, returns a list
of ``DomainAssignment`` tuples — one per applicable top-level domain
with a confidence score and matched-pattern rationale.

Multi-domain returns are normal — a single drill program legitimately
spans Geology + Geochemistry + Geophysics. The classifier never picks a
single "winner"; it returns every domain that scored above
``MIN_CONFIDENCE`` (default 0.30, tunable).

This is deliberately rule-based and small — no ML, no LLM, no remote
calls. The output is treated as a hint, not ground truth: every
``DomainAssignment`` carries ``assigned_by='auto'`` for the persistence
layer, and a reviewer can override it in the Silver Review Queue.

Invocation
----------
The Dagster bronze asset that lands a new file in ``bronze.source_files``
should call ``classify_document(...)`` and INSERT one
``silver.document_domain_tag`` row per returned assignment. When the
function returns an empty list (no patterns matched), the caller
should insert a single ``data_domain.code = 'unclassified'`` tag so
the document still surfaces in the Silver Review Queue.

Confidence calibration
----------------------
Confidence is the sum of matching signal weights, capped at 1.0. The
weights are tuned so:
  - filename + content match → ~0.8 (strong)
  - filename only            → ~0.4 (medium)
  - extension only           → ~0.25 (weak; many extensions cross domains)
  - content keyword only     → ~0.3 (medium-weak)

Adding new patterns
-------------------
Each pattern carries a (domain_id, sub_type_id_or_none, weight) tuple.
sub_type_id is optional — when set, the assigned tag carries it; when
None, the assignment is top-level only (the MVP scope).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

# Domain IDs from migration 2026_05_23_040000_create_data_domain_taxonomy.
DOMAIN_REPORTS: Final[int] = 1
DOMAIN_GEOLOGY: Final[int] = 2
DOMAIN_GEOCHEMISTRY: Final[int] = 3
DOMAIN_GEOPHYSICS: Final[int] = 4
DOMAIN_UNCLASSIFIED: Final[int] = 99

# Sub-type IDs — selected high-leverage ones the classifier can name.
SUB_NI_43_101: Final[int] = 106
SUB_ASSESSMENT_FILED: Final[int] = 104
SUB_LOGGED_LITHOLOGY: Final[int] = 208
SUB_DRILL_CORE_ASSAY: Final[int] = 305
SUB_SOIL_SAMPLE: Final[int] = 301
SUB_AIRBORNE_MAG: Final[int] = 408
SUB_AIRBORNE_EM: Final[int] = 404

#: Minimum total confidence for a domain to be reported. Below this we
#: assume the signal is noise.
MIN_CONFIDENCE: Final[float] = 0.30


@dataclass(frozen=True)
class DomainAssignment:
    """One auto-classifier output — what to INSERT into document_domain_tag."""

    domain_id: int
    sub_type_id: int | None
    confidence: float
    matched_patterns: tuple[str, ...]


@dataclass(frozen=True)
class _Pattern:
    """Internal compiled pattern with its signal weight."""

    pattern: re.Pattern[str]
    domain_id: int
    sub_type_id: int | None
    weight: float
    label: str


# ---------------------------------------------------------------------------
# Filename patterns
# ---------------------------------------------------------------------------


_FILENAME_PATTERNS: tuple[_Pattern, ...] = (
    # Reports
    _Pattern(re.compile(r"ni[\s_-]*43[\s_-]*101", re.IGNORECASE),
             DOMAIN_REPORTS, SUB_NI_43_101, 0.5, "filename:ni43-101"),
    _Pattern(re.compile(r"\btechnical[\s_-]*report\b", re.IGNORECASE),
             DOMAIN_REPORTS, SUB_NI_43_101, 0.35, "filename:technical-report"),
    _Pattern(re.compile(r"\bassessment[\s_-]*report\b", re.IGNORECASE),
             DOMAIN_REPORTS, SUB_ASSESSMENT_FILED, 0.4, "filename:assessment-report"),
    _Pattern(re.compile(r"\b(feasibility|pre[-\s]?feas|pfs|pea)\b", re.IGNORECASE),
             DOMAIN_REPORTS, 107, 0.4, "filename:feasibility-study"),

    # Geology
    _Pattern(re.compile(r"\b(litho|lithology)[\s_-]*log\b", re.IGNORECASE),
             DOMAIN_GEOLOGY, SUB_LOGGED_LITHOLOGY, 0.5, "filename:lithology-log"),
    _Pattern(re.compile(r"\bgeologic[\s_-]*map\b", re.IGNORECASE),
             DOMAIN_GEOLOGY, 202, 0.45, "filename:geologic-map"),
    _Pattern(re.compile(r"\bstructural[\s_-]*map", re.IGNORECASE),
             DOMAIN_GEOLOGY, 203, 0.45, "filename:structural-map"),
    _Pattern(re.compile(r"\bleapfrog\b|\bmicromine\b", re.IGNORECASE),
             DOMAIN_GEOLOGY, 205, 0.5, "filename:3d-model-export"),

    # Geochemistry
    _Pattern(re.compile(r"\bassay\b", re.IGNORECASE),
             DOMAIN_GEOCHEMISTRY, SUB_DRILL_CORE_ASSAY, 0.45, "filename:assay"),
    _Pattern(re.compile(r"\bsoil[\s_-]*sample\b", re.IGNORECASE),
             DOMAIN_GEOCHEMISTRY, SUB_SOIL_SAMPLE, 0.5, "filename:soil-sample"),
    _Pattern(re.compile(r"\bstream[\s_-]*sed\b", re.IGNORECASE),
             DOMAIN_GEOCHEMISTRY, 303, 0.5, "filename:stream-sediment"),
    _Pattern(re.compile(r"\brock[\s_-]*chip\b|\bgrab[\s_-]*sample\b", re.IGNORECASE),
             DOMAIN_GEOCHEMISTRY, 302, 0.45, "filename:rock-chip"),
    _Pattern(re.compile(r"\bgeochem(?:istry)?\b", re.IGNORECASE),
             DOMAIN_GEOCHEMISTRY, None, 0.3, "filename:geochem"),

    # Geophysics
    _Pattern(re.compile(r"\bairborne[\s_-]*(?:mag(?:netic)?|em)\b", re.IGNORECASE),
             DOMAIN_GEOPHYSICS, SUB_AIRBORNE_MAG, 0.5, "filename:airborne-geophysics"),
    _Pattern(re.compile(r"\b(mt|magnetotellurics?)\b", re.IGNORECASE),
             DOMAIN_GEOPHYSICS, 401, 0.4, "filename:mt"),
    _Pattern(re.compile(r"\bip[\s_-]*survey\b|\binduced[\s_-]*polarization\b", re.IGNORECASE),
             DOMAIN_GEOPHYSICS, 402, 0.5, "filename:ip-survey"),
    _Pattern(re.compile(r"\bresistivity\b", re.IGNORECASE),
             DOMAIN_GEOPHYSICS, 403, 0.35, "filename:resistivity"),
    _Pattern(re.compile(r"\bgravity[\s_-]*survey\b", re.IGNORECASE),
             DOMAIN_GEOPHYSICS, 405, 0.5, "filename:gravity"),
    _Pattern(re.compile(r"\bseismic\b", re.IGNORECASE),
             DOMAIN_GEOPHYSICS, 406, 0.4, "filename:seismic"),
    _Pattern(re.compile(r"\bradiometric\b|\bgamma[\s_-]*ray\b", re.IGNORECASE),
             DOMAIN_GEOPHYSICS, 407, 0.45, "filename:radiometric"),
    _Pattern(re.compile(r"\bdownhole\b.*\b(em|magnetics?|resistivity)\b", re.IGNORECASE),
             DOMAIN_GEOPHYSICS, 411, 0.5, "filename:downhole-geophys"),
    _Pattern(re.compile(r"\bgeophys(?:ics)?\b", re.IGNORECASE),
             DOMAIN_GEOPHYSICS, None, 0.3, "filename:geophys"),
)


# ---------------------------------------------------------------------------
# Extension / mime-type patterns
# ---------------------------------------------------------------------------


_EXTENSION_DOMAIN: dict[str, tuple[int, float, str]] = {
    # .las is the LAS well-log format — strong-enough single-signal
    # evidence to land above MIN_CONFIDENCE on extension alone.
    ".las":  (DOMAIN_GEOPHYSICS, 0.35, "ext:las"),
    ".segy": (DOMAIN_GEOPHYSICS, 0.40, "ext:segy"),
    ".sgy":  (DOMAIN_GEOPHYSICS, 0.40, "ext:sgy"),
    # .grd is more ambiguous (used for both gravity and DEM rasters) so
    # leave under threshold — needs corroborating signal.
    ".grd":  (DOMAIN_GEOPHYSICS, 0.20, "ext:grd"),
    ".dwg":  (DOMAIN_GEOLOGY,    0.15, "ext:dwg"),
    ".dxf":  (DOMAIN_GEOLOGY,    0.15, "ext:dxf"),
    ".shp":  (DOMAIN_GEOLOGY,    0.15, "ext:shp"),
    ".kml":  (DOMAIN_GEOLOGY,    0.15, "ext:kml"),
}


# ---------------------------------------------------------------------------
# Content-snippet patterns (first-pass keyword scan)
# ---------------------------------------------------------------------------


_CONTENT_PATTERNS: tuple[_Pattern, ...] = (
    _Pattern(re.compile(r"NI[\s-]*43[\s-]*101", re.IGNORECASE),
             DOMAIN_REPORTS, SUB_NI_43_101, 0.35, "content:ni43-101"),
    _Pattern(re.compile(r"\bQualified Person\b", re.IGNORECASE),
             DOMAIN_REPORTS, None, 0.2, "content:qualified-person"),
    _Pattern(re.compile(r"\b(IP|Induced Polarization) survey\b", re.IGNORECASE),
             DOMAIN_GEOPHYSICS, 402, 0.3, "content:ip-survey"),
    _Pattern(re.compile(r"\bsoil sample\b", re.IGNORECASE),
             DOMAIN_GEOCHEMISTRY, SUB_SOIL_SAMPLE, 0.3, "content:soil-sample"),
    _Pattern(re.compile(r"\bcore log\b|\blogged interval\b", re.IGNORECASE),
             DOMAIN_GEOLOGY, SUB_LOGGED_LITHOLOGY, 0.3, "content:core-log"),
    _Pattern(re.compile(r"\bassay (?:result|certificate|report)\b", re.IGNORECASE),
             DOMAIN_GEOCHEMISTRY, SUB_DRILL_CORE_ASSAY, 0.35, "content:assay-cert"),
    _Pattern(re.compile(r"\baeromagnetic\b|\bairborne magnetic\b", re.IGNORECASE),
             DOMAIN_GEOPHYSICS, SUB_AIRBORNE_MAG, 0.35, "content:aeromag"),
    _Pattern(re.compile(r"\benvironmental impact\b", re.IGNORECASE),
             DOMAIN_REPORTS, 101, 0.3, "content:environmental-impact"),
)


# ---------------------------------------------------------------------------
# Classifier entry point
# ---------------------------------------------------------------------------


def classify_document(
    filename: str,
    mime_type: str | None = None,
    content_snippet: str | None = None,
) -> list[DomainAssignment]:
    """Return the list of DomainAssignments this document earns.

    Empty list = no signal above MIN_CONFIDENCE. Caller should fall back
    to the 'unclassified' domain tag in that case.

    Args:
        filename: Original filename (basename or full path — extension
            extracted via the last dot).
        mime_type: Optional MIME hint. Currently unused but reserved
            for future signal weights (e.g. application/vnd.las+xml).
        content_snippet: Optional first few KB of decoded text. The
            classifier scans this with a small keyword pattern set.
    """
    del mime_type  # reserved; not yet weighted

    # Per-(domain_id, sub_type_id) confidence accumulator.
    scores: dict[tuple[int, int | None], list[str]] = {}
    confidences: dict[tuple[int, int | None], float] = {}

    def _add(domain_id: int, sub_type_id: int | None, weight: float, label: str) -> None:
        key = (domain_id, sub_type_id)
        scores.setdefault(key, []).append(label)
        confidences[key] = min(1.0, confidences.get(key, 0.0) + weight)

    # Filename — normalise underscore/hyphen as word separators so \b in
    # the regex patterns actually fires on filenames like "PLS-22-08_assay".
    # Python's \b treats _ as a word char, so without this every pattern
    # that uses \b would silently miss underscore-separated terms.
    fname_norm = re.sub(r"[_\-]+", " ", filename)
    for pat in _FILENAME_PATTERNS:
        if pat.pattern.search(fname_norm):
            _add(pat.domain_id, pat.sub_type_id, pat.weight, pat.label)

    # Extension
    lower = filename.lower()
    for ext, (domain_id, weight, label) in _EXTENSION_DOMAIN.items():
        if lower.endswith(ext):
            _add(domain_id, None, weight, label)
            break

    # Content
    if content_snippet:
        # Cap scan to first 8KB so multi-MB OCR outputs don't blow latency.
        scan = content_snippet[:8192]
        for pat in _CONTENT_PATTERNS:
            if pat.pattern.search(scan):
                _add(pat.domain_id, pat.sub_type_id, pat.weight, pat.label)

    # Collapse per-domain — when a sub-type fired, prefer the sub-type
    # entry. When only top-level fired, keep that.
    by_domain: dict[int, list[tuple[int | None, float, list[str]]]] = {}
    for (domain_id, sub_type_id), conf in confidences.items():
        by_domain.setdefault(domain_id, []).append(
            (sub_type_id, conf, scores[(domain_id, sub_type_id)])
        )

    assignments: list[DomainAssignment] = []
    for domain_id, entries in by_domain.items():
        # Sum the per-domain confidence across sub-type and top-level
        # signals so a noisy domain doesn't fragment its score.
        total_conf = min(1.0, sum(c for _, c, _ in entries))
        if total_conf < MIN_CONFIDENCE:
            continue
        # Pick the highest-confidence sub_type as the assignment's
        # sub_type_id; if every entry was top-level only, sub_type_id stays None.
        best = max(entries, key=lambda e: (e[0] is not None, e[1]))
        best_sub, _, _ = best
        merged_labels = tuple(sorted({lbl for _, _, lbls in entries for lbl in lbls}))
        assignments.append(
            DomainAssignment(
                domain_id=domain_id,
                sub_type_id=best_sub,
                confidence=round(total_conf, 3),
                matched_patterns=merged_labels,
            )
        )

    # Sort highest confidence first for caller convenience.
    assignments.sort(key=lambda a: -a.confidence)
    return assignments


__all__ = [
    "DOMAIN_GEOCHEMISTRY",
    "DOMAIN_GEOLOGY",
    "DOMAIN_GEOPHYSICS",
    "DOMAIN_REPORTS",
    "DOMAIN_UNCLASSIFIED",
    "DomainAssignment",
    "MIN_CONFIDENCE",
    "classify_document",
]
