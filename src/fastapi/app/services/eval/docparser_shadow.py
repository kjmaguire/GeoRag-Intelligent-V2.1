"""ADR-0016 Phase 2 step 4 — PaddleOCR-VL vs Docling shadow-eval.

Runs both full-page §04p parsers on the SAME PDF and compares the signals
ADR-0016 step 4 calls out — table row count, figure-detection recall, heading
hierarchy, and per-page latency — to decide whether PaddleOCR-VL can be promoted
over Docling for the mixed/full-page parse slot (the slot flag-gated by
`PDF_DOCPARSER_BACKEND` in app.ocr._orchestrator).

Both parsers emit the same dict schema (passages / tables[cells] /
layouts[layout_label] / page_count), so every metric is extracted identically
from each side — the comparison is apples-to-apples.

Shape, mirroring the Qwen3-VL gate in services/eval/pdf_vl_shadow.py:
- pure metric extractors + `DocparserShadowObservation.from_parse_results`
- `assess_docparser_shadow(observations) -> DocparserShadowAssessment` (the
  promote/hold decision)
- `run_docparser_shadow_pair(pdf_path, …)` — the dual-run orchestration that
  lazily imports the two parsers, times each, and returns one observation.
  Promotion (ADR-0016 step 5) and VRAM profiling remain the operator's step; the
  dual-run + gate are the analytical core.
"""
from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Layout labels (silver enum) that count as figures / headings. Both parsers
# normalise into this enum, so the same sets apply to each.
_FIGURE_LABELS = {"figure"}
_HEADING_LABELS = {"title", "section_header"}


# ── Gate thresholds (ADR-0016 step 4/5) ──────────────────────────────
MIN_OBSERVATIONS_FOR_GATE: int = 20
"""ADR's "golden 20-PDF corpus" — below this the comparison is too small to
recommend a promotion; the gate reports `insufficient_data` rather than promote."""

REGRESSION_TOLERANCE_FRAC: float = 0.05
"""VL may detect up to 5% fewer tables/figures/headings than Docling in aggregate
before it counts as a regression (absorbs per-PDF noise). A real shortfall beyond
this on a value-add signal blocks promotion."""


# ── Metric extractors (identical for either parser's output dict) ────

def _table_count(parse: dict[str, Any]) -> int:
    return len(parse.get("tables") or [])


def _table_row_count(parse: dict[str, Any]) -> int:
    return sum(len(t.get("cells") or []) for t in (parse.get("tables") or []))


def _figure_count(parse: dict[str, Any]) -> int:
    return sum(
        1 for layout in (parse.get("layouts") or [])
        if layout.get("layout_label") in _FIGURE_LABELS
    )


def _heading_count(parse: dict[str, Any]) -> int:
    return sum(
        1 for layout in (parse.get("layouts") or [])
        if layout.get("layout_label") in _HEADING_LABELS
    )


def _text_region_count(parse: dict[str, Any]) -> int:
    return len(parse.get("passages") or [])


@dataclass(frozen=True, slots=True)
class DocparserShadowObservation:
    """One PDF's paired Docling-vs-PaddleOCR-VL parse comparison.

    Latencies are wall-clock for the whole-document parse; ``None`` when that
    parser errored before timing.
    """

    pdf_id: str
    page_count: int
    docling_tables: int
    docling_table_rows: int
    docling_figures: int
    docling_headings: int
    docling_text_regions: int
    vl_tables: int
    vl_table_rows: int
    vl_figures: int
    vl_headings: int
    vl_text_regions: int
    docling_latency_ms: float | None = None
    vl_latency_ms: float | None = None

    @classmethod
    def from_parse_results(
        cls,
        *,
        pdf_id: str,
        docling: dict[str, Any],
        vl: dict[str, Any],
        docling_latency_ms: float | None = None,
        vl_latency_ms: float | None = None,
    ) -> DocparserShadowObservation:
        """Build an observation from the two parsers' output dicts.

        Either dict may be ``{}`` (parser failed) — its metrics then read 0,
        recording the failure rather than raising.
        """
        return cls(
            pdf_id=pdf_id,
            page_count=max(
                int(docling.get("page_count") or 0),
                int(vl.get("page_count") or 0),
            ),
            docling_tables=_table_count(docling),
            docling_table_rows=_table_row_count(docling),
            docling_figures=_figure_count(docling),
            docling_headings=_heading_count(docling),
            docling_text_regions=_text_region_count(docling),
            vl_tables=_table_count(vl),
            vl_table_rows=_table_row_count(vl),
            vl_figures=_figure_count(vl),
            vl_headings=_heading_count(vl),
            vl_text_regions=_text_region_count(vl),
            docling_latency_ms=docling_latency_ms,
            vl_latency_ms=vl_latency_ms,
        )


@dataclass(frozen=True, slots=True)
class DocparserShadowAssessment:
    """Outcome of ``assess_docparser_shadow`` — the recommendation + metrics."""

    recommendation: str  # "promote" | "hold" | "insufficient_data"
    dominates: bool       # VL >= Docling on every value-add signal (strict on >=1)
    n: int
    # Aggregate VL/Docling ratios (1.0 = parity; <1 = VL detected fewer).
    table_ratio: float
    table_row_ratio: float
    figure_ratio: float
    heading_ratio: float
    text_ratio: float
    docling_latency_p95_per_page_ms: float | None
    vl_latency_p95_per_page_ms: float | None
    regressions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "recommendation": self.recommendation,
            "dominates": self.dominates,
            "n": self.n,
            "min_observations_for_gate": MIN_OBSERVATIONS_FOR_GATE,
            "regression_tolerance_frac": REGRESSION_TOLERANCE_FRAC,
            "ratios_vl_over_docling": {
                "tables": round(self.table_ratio, 4),
                "table_rows": round(self.table_row_ratio, 4),
                "figures": round(self.figure_ratio, 4),
                "headings": round(self.heading_ratio, 4),
                "text_regions": round(self.text_ratio, 4),
            },
            "docling_latency_p95_per_page_ms": (
                round(self.docling_latency_p95_per_page_ms, 1)
                if self.docling_latency_p95_per_page_ms is not None else None
            ),
            "vl_latency_p95_per_page_ms": (
                round(self.vl_latency_p95_per_page_ms, 1)
                if self.vl_latency_p95_per_page_ms is not None else None
            ),
            "regressions": list(self.regressions),
        }


def _ratio(vl_total: int, docling_total: int) -> float:
    """VL/Docling ratio; 1.0 when Docling found nothing (no basis to regress)."""
    if docling_total == 0:
        return 1.0
    return vl_total / docling_total


def assess_docparser_shadow(
    observations: Sequence[DocparserShadowObservation],
) -> DocparserShadowAssessment:
    """Assess a PaddleOCR-VL shadow run against the ADR-0016 step-4 signals.

    Promotion logic: VL must not REGRESS (beyond REGRESSION_TOLERANCE_FRAC) on
    the value-add signals — table rows AND figures AND headings — in aggregate.
    Text regions and table *count* are reported but not gated (VL's region
    splitting can differ without being worse). Latency is reported, not gated
    (ADR-0016: VL is slower per page; the cost is the operator's promote-time
    call / per-document-class routing).

      - recommendation "promote"           — no value-add regression, enough data
      - recommendation "hold"              — VL regresses on a value-add signal
      - recommendation "insufficient_data" — n < MIN_OBSERVATIONS_FOR_GATE
    """
    n = len(observations)

    sum_d_tables = sum(o.docling_tables for o in observations)
    sum_v_tables = sum(o.vl_tables for o in observations)
    sum_d_rows = sum(o.docling_table_rows for o in observations)
    sum_v_rows = sum(o.vl_table_rows for o in observations)
    sum_d_figs = sum(o.docling_figures for o in observations)
    sum_v_figs = sum(o.vl_figures for o in observations)
    sum_d_head = sum(o.docling_headings for o in observations)
    sum_v_head = sum(o.vl_headings for o in observations)
    sum_d_text = sum(o.docling_text_regions for o in observations)
    sum_v_text = sum(o.vl_text_regions for o in observations)

    table_ratio = _ratio(sum_v_tables, sum_d_tables)
    table_row_ratio = _ratio(sum_v_rows, sum_d_rows)
    figure_ratio = _ratio(sum_v_figs, sum_d_figs)
    heading_ratio = _ratio(sum_v_head, sum_d_head)
    text_ratio = _ratio(sum_v_text, sum_d_text)

    floor = 1.0 - REGRESSION_TOLERANCE_FRAC
    regressions: list[str] = []
    if table_row_ratio < floor:
        regressions.append("table_rows")
    if figure_ratio < floor:
        regressions.append("figures")
    if heading_ratio < floor:
        regressions.append("headings")

    # Dominates: VL >= Docling on every value-add signal, strictly greater on >=1.
    value_add = (table_row_ratio, figure_ratio, heading_ratio)
    dominates = all(r >= 1.0 for r in value_add) and any(r > 1.0 for r in value_add)

    if n < MIN_OBSERVATIONS_FOR_GATE:
        recommendation = "insufficient_data"
    elif regressions:
        recommendation = "hold"
    else:
        recommendation = "promote"

    return DocparserShadowAssessment(
        recommendation=recommendation,
        dominates=dominates,
        n=n,
        table_ratio=table_ratio,
        table_row_ratio=table_row_ratio,
        figure_ratio=figure_ratio,
        heading_ratio=heading_ratio,
        text_ratio=text_ratio,
        docling_latency_p95_per_page_ms=_per_page_latency_p95(observations, "docling"),
        vl_latency_p95_per_page_ms=_per_page_latency_p95(observations, "vl"),
        regressions=regressions,
    )


async def run_docparser_shadow_pair(
    pdf_path: Path,
    *,
    pdf_id: str | None = None,
    pages: Sequence[int] | None = None,
) -> DocparserShadowObservation:
    """Parse one PDF with BOTH parsers, time each, return a paired observation.

    Lazily imports the §04p parsers so this module stays import-light (and keeps
    PaddleOCR/Docling out of resident memory until a shadow run actually runs). A
    parser that raises is recorded as an empty result (all-zero metrics) rather
    than aborting the run — a shadow eval must survive individual failures.
    """
    from app.ocr.parse_docparser_vl import parse_docparser_vl  # noqa: PLC0415
    from app.ocr.parse_mixed import parse_mixed  # noqa: PLC0415

    async def _timed(parser) -> tuple[dict[str, Any], float]:
        start = time.perf_counter()
        try:
            result = await parser(pdf_path, pages)
        except Exception:  # noqa: BLE001 — record the failure as empty, don't abort
            result = {}
        return result, (time.perf_counter() - start) * 1000.0

    docling, docling_ms = await _timed(parse_mixed)
    vl, vl_ms = await _timed(parse_docparser_vl)

    return DocparserShadowObservation.from_parse_results(
        pdf_id=pdf_id or str(pdf_path),
        docling=docling,
        vl=vl,
        docling_latency_ms=docling_ms,
        vl_latency_ms=vl_ms,
    )


def _per_page_latency_p95(
    observations: Sequence[DocparserShadowObservation],
    side: str,
) -> float | None:
    """p95 of per-page latency (latency_ms / page_count) for one parser."""
    per_page: list[float] = []
    for o in observations:
        latency = o.docling_latency_ms if side == "docling" else o.vl_latency_ms
        if latency is None:
            continue
        per_page.append(latency / max(o.page_count, 1))
    return _percentile(per_page, 0.95)


def _percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank-with-interpolation percentile; None for empty input."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = q * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)
