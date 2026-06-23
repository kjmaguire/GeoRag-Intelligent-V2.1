"""ADR-0016 Phase 2 — PaddleOCR-VL doc-parser behaviour tests.

Fast + deterministic: the ~3-4 GB PaddleOCR-VL model is never loaded. The
`_get_vl_pipeline` seam is monkeypatched with a fake pipeline whose
`.predict()` yields hand-built result objects shaped like real PaddleOCR-VL
output (`.json` with `parsing_res_list`, `.markdown` with `markdown_texts`).

Covers:
- output schema parity with parse_mixed (passages / tables / layouts /
  per_page_* / pages_needing_ocr) + the additive `markdown` field
- block_label → silver enum normalisation
- bbox coercion (flat 4-vector, Nx2 polygon, numpy array)
- table block_content → cell grid (HTML + Markdown)
- the `pages` output filter
- the PDF_DOCPARSER_BACKEND config flag + validator
"""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

import pytest

from app.ocr.parse_docparser_vl import (
    _cells_from_table_content,
    _coerce_bbox,
    _heuristic_header_detected,
    _per_page_count,
    _per_page_mean,
    normalize_vl_label,
    parse_docparser_vl,
)

# `app.ocr.__init__` re-exports the *function* `parse_docparser_vl`, which
# shadows the submodule attribute on the package (the same name collision every
# §04p parser has). Grab the real module object from sys.modules so we can
# monkeypatch its `_get_vl_pipeline` seam.
vl = sys.modules["app.ocr.parse_docparser_vl"]


# ---------------------------------------------------------------------------
# Fakes — shaped like real PaddleOCR-VL result objects
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, json_data: dict, markdown_text: str) -> None:
        self.json = json_data
        self.markdown = {"markdown_texts": markdown_text, "markdown_images": []}


class _FakePipeline:
    def __init__(self, results: list[_FakeResult]) -> None:
        self._results = results

    def predict(self, _input: str) -> list[_FakeResult]:
        return self._results


_TABLE_HTML = (
    "<table><tr><th>Hole</th><th>Au</th></tr>"
    "<tr><td>DDH-1</td><td>1.2</td></tr></table>"
)


def _two_page_results() -> list[_FakeResult]:
    page0 = _FakeResult(
        {
            "page_index": 0,
            "parsing_res_list": [
                {"block_label": "text", "block_bbox": [10, 20, 100, 40],
                 "block_content": "Patterson Lake South", "block_id": 0, "block_order": 0},
                {"block_label": "doc_title", "block_bbox": [10, 5, 200, 18],
                 "block_content": "Technical Report", "block_id": 1, "block_order": 1},
                {"block_label": "paragraph_title", "block_bbox": [10, 45, 120, 60],
                 "block_content": "Summary", "block_id": 2, "block_order": 2},
                {"block_label": "table", "block_bbox": [10, 70, 300, 200],
                 "block_content": _TABLE_HTML, "block_id": 3, "block_order": 3},
                {"block_label": "figure", "block_bbox": [10, 210, 300, 400],
                 "block_content": "", "block_id": 4, "block_order": 4},
            ],
        },
        "# Technical Report\n\nPatterson Lake South",
    )
    page1 = _FakeResult(
        {
            "page_index": 1,
            "parsing_res_list": [
                # Polygon bbox (Nx2) → envelope [5, 5, 50, 20].
                {"block_label": "text",
                 "block_bbox": [[5, 5], [50, 5], [50, 20], [5, 20]],
                 "block_content": "Page two text", "block_id": 0, "block_order": 0},
            ],
        },
        "Page two text",
    )
    return [page0, page1]


# ---------------------------------------------------------------------------
# End-to-end mapping
# ---------------------------------------------------------------------------

def test_maps_paddleocr_vl_output_to_parse_mixed_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vl, "_get_vl_pipeline", lambda: _FakePipeline(_two_page_results()))

    result = asyncio.run(parse_docparser_vl(Path("/dev/null")))

    assert result["parser_used"] == "paddleocr_vl"
    assert result["page_count"] == 2
    assert result["pages_needing_ocr"] == []

    # Passages: every non-table region carrying text (figure has no text).
    passage_labels = [p["layout_label"] for p in result["passages"]]
    assert passage_labels == ["text", "title", "section_header", "text"]
    assert [p["text_content"] for p in result["passages"]] == [
        "Patterson Lake South", "Technical Report", "Summary", "Page two text",
    ]
    assert all(p["source_method"] == "paddleocr_vl" for p in result["passages"])

    # Layouts: every detected region, including the text-less figure.
    assert len(result["layouts"]) == 6
    assert {l["layout_label"] for l in result["layouts"]} >= {"figure", "table"}

    # Polygon bbox on page 1 collapsed to its axis-aligned envelope.
    page1_passage = [p for p in result["passages"] if p["page"] == 1][0]
    assert page1_passage["bbox"] == [5.0, 5.0, 50.0, 20.0]

    # Per-page arrays.
    assert result["per_page_text_region_counts"] == [3, 1]
    assert result["per_page_layout_confidence"] == [vl.VL_TEXT_CONFIDENCE] * 2

    # Additive Markdown field, one entry per page.
    assert result["markdown"][0].startswith("# Technical Report")
    assert result["markdown"][1] == "Page two text"


def test_table_block_becomes_table_with_parsed_cells(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vl, "_get_vl_pipeline", lambda: _FakePipeline(_two_page_results()))

    result = asyncio.run(parse_docparser_vl(Path("/dev/null")))

    assert len(result["tables"]) == 1
    table = result["tables"][0]
    assert table["cells"] == [["Hole", "Au"], ["DDH-1", "1.2"]]
    assert table["header_detected"] is True
    assert table["parser_used"] == "paddleocr_vl"
    assert table["page"] == 0
    # The table region is NOT also emitted as a passage.
    assert all(p["layout_label"] != "table" for p in result["passages"])


def test_pages_filter_restricts_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vl, "_get_vl_pipeline", lambda: _FakePipeline(_two_page_results()))

    result = asyncio.run(parse_docparser_vl(Path("/dev/null"), pages=[0]))

    assert result["page_count"] == 1
    assert {p["page"] for p in result["passages"]} == {0}
    assert len(result["tables"]) == 1
    assert result["per_page_text_region_counts"] == [3]
    assert result["markdown"] == ["# Technical Report\n\nPatterson Lake South"]


# ---------------------------------------------------------------------------
# Label normalisation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("text", "text"),
        ("Doc Title", "title"),          # whitespace collapsed to "_"
        ("paragraph_title", "section_header"),
        ("Figure", "figure"),
        ("image", "figure"),
        ("table", "table"),
        ("formula_number", "formula"),
        ("header", "page_header"),
        ("footer", "page_footer"),
        ("footnote", "footnote"),
        ("list_item", "list_item"),
        ("seal", "other"),               # unmapped → other
        ("totally_unknown", "other"),
        (None, "other"),
        ("", "other"),
    ],
)
def test_normalize_vl_label(raw: str | None, expected: str) -> None:
    assert normalize_vl_label(raw) == expected


# ---------------------------------------------------------------------------
# bbox coercion
# ---------------------------------------------------------------------------

def test_coerce_bbox_flat_vector() -> None:
    assert _coerce_bbox([10, 20, 100, 40]) == [10.0, 20.0, 100.0, 40.0]


def test_coerce_bbox_polygon_envelope() -> None:
    poly = [[5, 30], [50, 5], [48, 22], [7, 20]]
    assert _coerce_bbox(poly) == [5.0, 5.0, 50.0, 30.0]


def test_coerce_bbox_numpy_array() -> None:
    np = pytest.importorskip("numpy")
    assert _coerce_bbox(np.array([10, 20, 100, 40])) == [10.0, 20.0, 100.0, 40.0]


@pytest.mark.parametrize("bad", [None, [], [1, 2]])
def test_coerce_bbox_rejects_unusable(bad: object) -> None:
    assert _coerce_bbox(bad) is None


# ---------------------------------------------------------------------------
# table-cell extraction
# ---------------------------------------------------------------------------

def test_cells_from_html_table() -> None:
    cells = _cells_from_table_content(_TABLE_HTML)
    assert cells == [["Hole", "Au"], ["DDH-1", "1.2"]]


def test_cells_from_markdown_table() -> None:
    md = "| Hole | Au |\n| --- | --- |\n| DDH-1 | 1.2 |\n"
    cells = _cells_from_table_content(md)
    assert cells == [["Hole", "Au"], ["DDH-1", "1.2"]]


@pytest.mark.parametrize("content", ["", "just some prose with no table"])
def test_cells_from_non_table_content(content: str) -> None:
    assert _cells_from_table_content(content) == []


# ---------------------------------------------------------------------------
# small pure helpers
# ---------------------------------------------------------------------------

def test_heuristic_header_detected() -> None:
    assert _heuristic_header_detected([["Hole", "Au"], ["DDH-1", "1.2"]]) is True
    assert _heuristic_header_detected([["1.0", "2.0"], ["3.0", "4.0"]]) is False
    assert _heuristic_header_detected([["only one row"]]) is False


def test_per_page_count_and_mean() -> None:
    items = [
        {"page": 0, "extraction_confidence": 0.9},
        {"page": 0, "extraction_confidence": 0.7},
        {"page": 1, "extraction_confidence": 0.5},
    ]
    assert _per_page_count(items, 2) == [2, 1]
    assert _per_page_mean(items, 2, 0.0) == [0.8, 0.5]
    # Empty page falls back to the default.
    assert _per_page_mean(items, 3, 0.42) == [0.8, 0.5, 0.42]


# ---------------------------------------------------------------------------
# config flag
# ---------------------------------------------------------------------------

def test_docparser_backend_defaults_to_docling() -> None:
    from app.config import settings

    assert settings.PDF_DOCPARSER_BACKEND == "docling"


def test_docparser_backend_accepts_and_normalises_paddleocr_vl() -> None:
    from app.config import Settings

    s = Settings(PDF_DOCPARSER_BACKEND="PaddleOCR-VL")  # type: ignore[call-arg]
    assert s.PDF_DOCPARSER_BACKEND == "paddleocr-vl"


def test_docparser_backend_rejects_unknown_value() -> None:
    from pydantic import ValidationError

    from app.config import Settings

    with pytest.raises(ValidationError):
        Settings(PDF_DOCPARSER_BACKEND="ragflow")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# orchestrator dispatch wiring (mixed slot, flag-gated)
# ---------------------------------------------------------------------------

def _stub_parsers(monkeypatch: pytest.MonkeyPatch, backend: str):
    """Patch the orchestrator's mixed-slot parsers with labelled stubs and pin
    the doc-parser backend flag. Returns the orchestrator module."""
    import app.ocr._orchestrator as orch

    async def _docling_stub(_p: Path) -> dict:
        return {"parser_used": "mixed_docling", "pages_needing_ocr": []}

    async def _vl_stub(_p: Path) -> dict:
        return {"parser_used": "paddleocr_vl", "pages_needing_ocr": []}

    monkeypatch.setattr(orch, "parse_mixed", _docling_stub)
    monkeypatch.setattr(orch, "parse_docparser_vl", _vl_stub)
    monkeypatch.setattr(
        orch, "settings", types.SimpleNamespace(PDF_DOCPARSER_BACKEND=backend)
    )
    return orch


def test_mixed_slot_defaults_to_docling(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = _stub_parsers(monkeypatch, "docling")
    result = asyncio.run(orch._parse_mixed_slot(Path("/dev/null")))
    assert result["parser_used"] == "mixed_docling"


def test_mixed_slot_routes_to_paddleocr_vl_when_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    orch = _stub_parsers(monkeypatch, "paddleocr-vl")
    result = asyncio.run(orch._parse_mixed_slot(Path("/dev/null")))
    assert result["parser_used"] == "paddleocr_vl"
