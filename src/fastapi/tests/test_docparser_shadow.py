"""ADR-0016 Phase 2 — PaddleOCR-VL vs Docling shadow-eval gate tests.

Pure/synchronous: no parsers loaded (the dual-run's lazy parse_mixed /
parse_docparser_vl imports are monkeypatched with async fakes).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from app.services.eval.docparser_shadow import (
    MIN_OBSERVATIONS_FOR_GATE,
    DocparserShadowObservation,
    _figure_count,
    _heading_count,
    _percentile,
    _table_count,
    _table_row_count,
    _text_region_count,
    assess_docparser_shadow,
    run_docparser_shadow_pair,
)


def _parse(*, tables=0, rows_per_table=0, figures=0, headings=0, texts=0, pages=2):
    """Build a parser-output dict (works for both Docling + PaddleOCR-VL shape)."""
    return {
        "page_count": pages,
        "tables": [
            {"cells": [["c"] for _ in range(rows_per_table)]} for _ in range(tables)
        ],
        "layouts": (
            [{"layout_label": "figure"} for _ in range(figures)]
            + [{"layout_label": "section_header"} for _ in range(headings)]
            + [{"layout_label": "text"} for _ in range(3)]  # noise — not figure/heading
        ),
        "passages": [{"i": i} for i in range(texts)],
    }


# ---------------------------------------------------------------------------
# metric extractors
# ---------------------------------------------------------------------------

def test_metric_extractors() -> None:
    p = _parse(tables=2, rows_per_table=4, figures=3, headings=5, texts=7)
    assert _table_count(p) == 2
    assert _table_row_count(p) == 8           # 2 tables × 4 rows
    assert _figure_count(p) == 3
    assert _heading_count(p) == 5
    assert _text_region_count(p) == 7


def test_extractors_tolerate_empty() -> None:
    assert _table_count({}) == 0
    assert _table_row_count({}) == 0
    assert _figure_count({}) == 0
    assert _heading_count({}) == 0
    assert _text_region_count({}) == 0


# ---------------------------------------------------------------------------
# from_parse_results
# ---------------------------------------------------------------------------

def test_from_parse_results() -> None:
    o = DocparserShadowObservation.from_parse_results(
        pdf_id="p1",
        docling=_parse(tables=1, rows_per_table=3, figures=1, headings=2, texts=4, pages=3),
        vl=_parse(tables=2, rows_per_table=3, figures=2, headings=2, texts=5, pages=3),
        docling_latency_ms=100.0,
        vl_latency_ms=400.0,
    )
    assert o.page_count == 3
    assert (o.docling_tables, o.docling_table_rows, o.docling_figures) == (1, 3, 1)
    assert (o.vl_tables, o.vl_table_rows, o.vl_figures) == (2, 6, 2)
    assert o.docling_latency_ms == 100.0 and o.vl_latency_ms == 400.0


# ---------------------------------------------------------------------------
# assess_docparser_shadow
# ---------------------------------------------------------------------------

def _obs(i=0, *, d, v, d_ms=100.0, v_ms=400.0):
    return DocparserShadowObservation.from_parse_results(
        pdf_id=f"p{i}", docling=d, vl=v, docling_latency_ms=d_ms, vl_latency_ms=v_ms
    )


def test_promote_when_vl_dominates() -> None:
    # VL finds MORE tables/figures/headings on every PDF.
    obs = [
        _obs(i,
             d=_parse(tables=1, rows_per_table=2, figures=1, headings=1),
             v=_parse(tables=2, rows_per_table=3, figures=2, headings=2))
        for i in range(MIN_OBSERVATIONS_FOR_GATE)
    ]
    a = assess_docparser_shadow(obs)
    assert a.recommendation == "promote"
    assert a.dominates is True
    assert a.figure_ratio == 2.0 and a.heading_ratio == 2.0
    # per-page latency p95: docling 100/2=50, vl 400/2=200
    assert a.vl_latency_p95_per_page_ms == 200.0


def test_hold_when_vl_regresses_on_figures() -> None:
    # VL finds figures on only 1 of 20 PDFs vs Docling's 2 each → big shortfall.
    obs = [
        _obs(i,
             d=_parse(tables=1, rows_per_table=3, figures=2, headings=2),
             v=_parse(tables=1, rows_per_table=3, figures=(2 if i == 0 else 0), headings=2))
        for i in range(MIN_OBSERVATIONS_FOR_GATE)
    ]
    a = assess_docparser_shadow(obs)
    assert a.recommendation == "hold"
    assert "figures" in a.regressions
    assert a.dominates is False


def test_small_regression_within_tolerance_promotes() -> None:
    # 100 PDFs, VL misses headings on just 2 → ~ -2% < 5% tolerance.
    obs = [
        _obs(i,
             d=_parse(figures=1, headings=1, rows_per_table=0),
             v=_parse(figures=1, headings=(1 if i >= 2 else 0), rows_per_table=0))
        for i in range(100)
    ]
    a = assess_docparser_shadow(obs)
    assert a.heading_ratio == pytest.approx(0.98)
    assert "headings" not in a.regressions
    assert a.recommendation == "promote"


def test_insufficient_data() -> None:
    obs = [
        _obs(i, d=_parse(figures=1, headings=1), v=_parse(figures=2, headings=2))
        for i in range(MIN_OBSERVATIONS_FOR_GATE - 1)
    ]
    a = assess_docparser_shadow(obs)
    assert a.recommendation == "insufficient_data"


def test_docling_zero_basis_is_not_a_regression() -> None:
    # Docling finds 0 figures everywhere → ratio defaults to 1.0 (no basis).
    obs = [
        _obs(i, d=_parse(figures=0, headings=1, rows_per_table=2),
             v=_parse(figures=0, headings=1, rows_per_table=2))
        for i in range(MIN_OBSERVATIONS_FOR_GATE)
    ]
    a = assess_docparser_shadow(obs)
    assert a.figure_ratio == 1.0
    assert "figures" not in a.regressions


def test_to_dict_shape() -> None:
    obs = [_obs(i, d=_parse(figures=1, headings=1), v=_parse(figures=1, headings=1))
           for i in range(MIN_OBSERVATIONS_FOR_GATE)]
    d = assess_docparser_shadow(obs).to_dict()
    assert d["recommendation"] == "promote"
    assert set(d["ratios_vl_over_docling"]) == {
        "tables", "table_rows", "figures", "headings", "text_regions"
    }
    assert "vl_latency_p95_per_page_ms" in d


# ---------------------------------------------------------------------------
# run_docparser_shadow_pair (dual-run)
# ---------------------------------------------------------------------------

def test_dual_run_builds_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.ocr.parse_mixed  # noqa: F401 — ensure submodules are in sys.modules
    import app.ocr.parse_docparser_vl  # noqa: F401

    async def fake_docling(pdf_path, pages):
        return _parse(tables=1, rows_per_table=3, figures=1, headings=2, pages=2)

    async def fake_vl(pdf_path, pages):
        return _parse(tables=2, rows_per_table=3, figures=3, headings=2, pages=2)

    monkeypatch.setattr(sys.modules["app.ocr.parse_mixed"], "parse_mixed", fake_docling)
    monkeypatch.setattr(sys.modules["app.ocr.parse_docparser_vl"], "parse_docparser_vl", fake_vl)

    o = asyncio.run(run_docparser_shadow_pair(Path("/dev/null"), pdf_id="doc-1"))
    assert o.pdf_id == "doc-1"
    assert o.docling_figures == 1 and o.vl_figures == 3
    assert o.docling_tables == 1 and o.vl_tables == 2
    assert o.docling_latency_ms is not None and o.vl_latency_ms is not None


def test_dual_run_records_parser_failure_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.ocr.parse_mixed  # noqa: F401
    import app.ocr.parse_docparser_vl  # noqa: F401

    async def ok_docling(pdf_path, pages):
        return _parse(tables=1, figures=1, headings=1)

    async def boom_vl(pdf_path, pages):
        raise RuntimeError("paddleocr-vl model not loaded")

    monkeypatch.setattr(sys.modules["app.ocr.parse_mixed"], "parse_mixed", ok_docling)
    monkeypatch.setattr(sys.modules["app.ocr.parse_docparser_vl"], "parse_docparser_vl", boom_vl)

    o = asyncio.run(run_docparser_shadow_pair(Path("/dev/null")))
    assert o.docling_tables == 1
    assert o.vl_tables == 0 and o.vl_figures == 0  # failure → empty metrics, not a crash
    assert o.vl_latency_ms is not None             # latency still recorded


def test_percentile_basics() -> None:
    assert _percentile([], 0.95) is None
    assert _percentile([5.0], 0.95) == 5.0
    assert _percentile([1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.5)
