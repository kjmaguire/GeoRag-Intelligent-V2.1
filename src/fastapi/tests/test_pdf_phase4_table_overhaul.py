"""Phase 4 (2026-05-22) — table extraction overhaul tests.

Verifies:
  - _classify_page_table_type: line + rect thresholds, env overrides
  - _classify_pages_from_pdf: handles missing fitz gracefully
  - _extract_all_tables_as_sections per-page routing:
      • bordered → pdfplumber-lines AND pdfplumber-text
      • borderless → pdfplumber-text ONLY (no expensive lines pass)
      • classification_failed → legacy dual-pass
  - dedupe via _table_signature stable when the lines + text passes both
    find the same table

docling's TableFormer (_extract_tables_via_docling_only, the
existing_docling_tables reuse path, and the docling-vs-pdfplumber
cross-engine dedupe) was removed 2026-07-29 along with the rest of
docling — it never ran in production (PDF_PARSER_DOCLING_ENABLED was
false in every live deployment). pdfplumber-lines is now the sole
bordered-table extraction method; see
app.services.ingest.pdf_report._extract_all_tables_as_sections.

Run with:
    pytest src/fastapi/tests/test_pdf_phase4_table_overhaul.py -v
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

# Stub boto3/botocore ONLY when genuinely absent (running pytest on a dev host
# rather than in the container, where they are installed for real). This used to
# be an unconditional sys.modules.setdefault, which left a MagicMock in
# sys.modules that unrelated modules tripped over: aioboto3 does
# `import boto3.session`, and that raises "'boto3' is not a package" against a
# mock. Once this file moved into the FastAPI suite it sorted ahead of
# test_pg_partman_maintenance / test_section11_* / test_workspace_export_restore
# and broke their collection.
try:  # pragma: no cover - environment probe
    import boto3  # noqa: F401
    import botocore.config  # noqa: F401
except ImportError:  # pragma: no cover - dev host without the AWS SDKs
    sys.modules.setdefault("boto3", MagicMock())
    sys.modules.setdefault("botocore", MagicMock())
    sys.modules.setdefault("botocore.config", MagicMock())


@pytest.fixture
def parser_module():
    import importlib

    from app.services.ingest import pdf_report
    importlib.reload(pdf_report)
    return pdf_report


def _make_point(x, y):
    p = MagicMock()
    p.x = x
    p.y = y
    return p


def _drawing(items):
    return {"items": items}


# ---------------------------------------------------------------------------
# 1. Classifier — zero drawings → borderless
# ---------------------------------------------------------------------------

def test_classify_empty_drawings_is_borderless(parser_module):
    assert parser_module._classify_page_table_type([]) == "borderless"
    assert parser_module._classify_page_table_type([{"items": []}]) == "borderless"


# ---------------------------------------------------------------------------
# 2. Classifier — line threshold (default 3)
# ---------------------------------------------------------------------------

def test_classify_horizontal_lines_above_threshold_is_bordered(parser_module):
    items = []
    for _ in range(5):
        items.append(("l", _make_point(0, 100), _make_point(200, 100)))
    drawings = [_drawing(items)]
    assert parser_module._classify_page_table_type(drawings) == "bordered"


def test_classify_horizontal_lines_below_threshold_is_borderless(parser_module):
    items = [("l", _make_point(0, 100), _make_point(200, 100))]
    drawings = [_drawing(items)]
    assert parser_module._classify_page_table_type(drawings) == "borderless"


# ---------------------------------------------------------------------------
# 3. Classifier — line length filter (short lines don't count)
# ---------------------------------------------------------------------------

def test_classify_ignores_short_horizontal_lines(parser_module):
    # 5 very short lines (< 30 points) — should not classify as bordered
    items = [
        ("l", _make_point(0, 100), _make_point(5, 100)) for _ in range(5)
    ]
    drawings = [_drawing(items)]
    assert parser_module._classify_page_table_type(drawings) == "borderless"


# ---------------------------------------------------------------------------
# 4. Classifier — vertical lines don't count as horizontal
# ---------------------------------------------------------------------------

def test_classify_vertical_lines_dont_count(parser_module):
    # 5 vertical lines (Δx ~ 0)
    items = [
        ("l", _make_point(100, 0), _make_point(100, 200)) for _ in range(5)
    ]
    drawings = [_drawing(items)]
    assert parser_module._classify_page_table_type(drawings) == "borderless"


# ---------------------------------------------------------------------------
# 5. Classifier — rectangle threshold (default 20)
# ---------------------------------------------------------------------------

def test_classify_rectangles_above_rect_threshold_is_bordered(parser_module):
    items = [("re", MagicMock()) for _ in range(25)]
    drawings = [_drawing(items)]
    assert parser_module._classify_page_table_type(drawings) == "bordered"


def test_classify_rectangles_below_rect_threshold_is_borderless(parser_module):
    items = [("re", MagicMock()) for _ in range(15)]
    drawings = [_drawing(items)]
    assert parser_module._classify_page_table_type(drawings) == "borderless"


# ---------------------------------------------------------------------------
# 6. Classifier — both line + rect together
# ---------------------------------------------------------------------------

def test_classify_mixed_lines_and_rects_is_bordered(parser_module):
    items = [
        ("l", _make_point(0, 100), _make_point(200, 100)),
        ("l", _make_point(0, 200), _make_point(200, 200)),
        ("l", _make_point(0, 300), _make_point(200, 300)),
        ("re", MagicMock()),
    ]
    drawings = [_drawing(items)]
    # 3 horizontal lines exactly hits the line threshold
    assert parser_module._classify_page_table_type(drawings) == "bordered"


# ---------------------------------------------------------------------------
# 7. Classifier — non-line / non-rect items ignored ('qu', 'c')
# ---------------------------------------------------------------------------

def test_classify_ignores_quads_and_curves(parser_module):
    items = [("qu", MagicMock()) for _ in range(50)]
    items += [("c", MagicMock(), MagicMock(), MagicMock(), MagicMock())
              for _ in range(50)]
    drawings = [_drawing(items)]
    assert parser_module._classify_page_table_type(drawings) == "borderless"


# ---------------------------------------------------------------------------
# 8. Classifier — explicit threshold args override defaults
# ---------------------------------------------------------------------------

def test_classify_custom_thresholds(parser_module):
    items = [("re", MagicMock()) for _ in range(5)]
    drawings = [_drawing(items)]
    # Default rect threshold 20 → borderless
    assert parser_module._classify_page_table_type(drawings) == "borderless"
    # Tighten to 3 → bordered
    assert parser_module._classify_page_table_type(
        drawings, rect_threshold=3,
    ) == "bordered"


# ---------------------------------------------------------------------------
# 9. _classify_pages_from_pdf — env vars honored
# ---------------------------------------------------------------------------

def test_classify_pages_env_thresholds_honored(parser_module, monkeypatch):
    monkeypatch.setenv("TABLE_BORDER_LINE_THRESHOLD", "1")
    monkeypatch.setenv("TABLE_BORDER_RECT_THRESHOLD", "1")

    # Stub fitz to return one page with 1 horizontal line
    fake_page = MagicMock()
    fake_page.get_drawings = MagicMock(return_value=[_drawing([
        ("l", _make_point(0, 100), _make_point(200, 100)),
    ])])

    class _FakeDoc:
        def __iter__(self): return iter([fake_page])
        def __enter__(self): return self
        def __exit__(self, *a): return False

    fake_pymupdf = types.ModuleType("pymupdf")
    fake_pymupdf.open = MagicMock(return_value=_FakeDoc())
    monkeypatch.setitem(sys.modules, "pymupdf", fake_pymupdf)

    result = parser_module._classify_pages_from_pdf("/tmp/fake.pdf")
    assert result == {1: "bordered"}


# ---------------------------------------------------------------------------
# 10. _classify_pages_from_pdf — fitz unavailable returns empty
# ---------------------------------------------------------------------------

def test_classify_pages_fitz_open_failure_returns_empty(parser_module, monkeypatch):
    fake_pymupdf = types.ModuleType("pymupdf")
    fake_pymupdf.open = MagicMock(side_effect=RuntimeError("can't open"))
    monkeypatch.setitem(sys.modules, "pymupdf", fake_pymupdf)
    assert parser_module._classify_pages_from_pdf("/tmp/fake.pdf") == {}


# ---------------------------------------------------------------------------
# 11. _extract_all_tables_as_sections — bordered pages use lines AND text
# ---------------------------------------------------------------------------

def test_extract_all_tables_bordered_uses_lines_and_text_strategy(
    parser_module, monkeypatch,
):
    monkeypatch.setattr(
        parser_module, "_classify_pages_from_pdf",
        lambda p: {1: "bordered"},
    )

    # Build a fake pdfplumber that records which strategies fire
    strategies_used = []
    fake_table = [["a", "b"], ["1", "2"]]

    class _FakePage:
        def extract_tables(self, table_settings):
            strategies_used.append(table_settings["vertical_strategy"])
            return [fake_table]

    class _FakePdf:
        pages = [_FakePage()]
        def __enter__(self): return self
        def __exit__(self, *a): return False

    fake_pp = types.ModuleType("pdfplumber")
    fake_pp.open = MagicMock(return_value=_FakePdf())
    monkeypatch.setitem(sys.modules, "pdfplumber", fake_pp)

    parser_module._extract_all_tables_as_sections("/tmp/fake.pdf")
    # 'lines' runs on the bordered page. 'text' also runs (catches
    # borderless tables co-existing on bordered pages).
    assert "lines" in strategies_used
    assert "text" in strategies_used


# ---------------------------------------------------------------------------
# 12. _extract_all_tables_as_sections — borderless page → text strategy ONLY
# ---------------------------------------------------------------------------

def test_extract_all_tables_borderless_uses_text_strategy_only(
    parser_module, monkeypatch,
):
    monkeypatch.setattr(
        parser_module, "_classify_pages_from_pdf",
        lambda p: {1: "borderless"},
    )

    strategies_used = []
    fake_table = [["a", "b"], ["1", "2"]]

    class _FakePage:
        def extract_tables(self, table_settings):
            strategies_used.append(table_settings["vertical_strategy"])
            return [fake_table]

    class _FakePdf:
        pages = [_FakePage()]
        def __enter__(self): return self
        def __exit__(self, *a): return False

    fake_pp = types.ModuleType("pdfplumber")
    fake_pp.open = MagicMock(return_value=_FakePdf())
    monkeypatch.setitem(sys.modules, "pdfplumber", fake_pp)

    parser_module._extract_all_tables_as_sections("/tmp/fake.pdf")
    # Only the text strategy fires on borderless pages
    assert strategies_used == ["text"]


# ---------------------------------------------------------------------------
# 13. _extract_all_tables_as_sections — pdfplumber unavailable → no sections,
#     no crash
# ---------------------------------------------------------------------------

def test_extract_all_tables_pdfplumber_unavailable_returns_empty(
    parser_module, monkeypatch,
):
    monkeypatch.setattr(
        parser_module, "_classify_pages_from_pdf",
        lambda p: {1: "bordered"},
    )
    # Force pdfplumber.open to raise
    fake_pp = types.ModuleType("pdfplumber")
    fake_pp.open = MagicMock(side_effect=ImportError("no pdfplumber"))
    monkeypatch.setitem(sys.modules, "pdfplumber", fake_pp)

    out = parser_module._extract_all_tables_as_sections("/tmp/fake.pdf")
    assert out == []


# ---------------------------------------------------------------------------
# 14. Dedupe — the lines pass and the text pass both find the same table
#     on a bordered page → only one section survives
# ---------------------------------------------------------------------------

def test_extract_all_tables_dedupes_lines_and_text_pass(parser_module, monkeypatch):
    shared_md = "| col1 | col2 |\n|---|---|\n| a | b |"

    monkeypatch.setattr(
        parser_module, "_classify_pages_from_pdf",
        lambda p: {1: "bordered"},
    )

    # Both the 'lines' and 'text' strategy calls return a table that
    # produces the SAME markdown — the lines+text dual pass on a
    # bordered page should not double-count it.
    fake_table = [["col1", "col2"], ["a", "b"]]
    monkeypatch.setattr(parser_module, "_table_to_markdown", lambda t: shared_md)
    monkeypatch.setattr(parser_module, "_table_has_data", lambda t: True)

    class _FakePage:
        def extract_tables(self, table_settings):
            return [fake_table]

    class _FakePdf:
        pages = [_FakePage()]
        def __enter__(self): return self
        def __exit__(self, *a): return False

    fake_pp = types.ModuleType("pdfplumber")
    fake_pp.open = MagicMock(return_value=_FakePdf())
    monkeypatch.setitem(sys.modules, "pdfplumber", fake_pp)

    out = parser_module._extract_all_tables_as_sections("/tmp/fake.pdf")
    assert len(out) == 1


def test_extract_all_tables_classification_failure_runs_dual_pass(
    parser_module, monkeypatch,
):
    # Classifier returns empty dict (failed) → both strategies run
    monkeypatch.setattr(parser_module, "_classify_pages_from_pdf", lambda p: {})

    strategies_used = []
    fake_table = [["a"], ["1"]]

    class _FakePage:
        def extract_tables(self, table_settings):
            strategies_used.append(table_settings["vertical_strategy"])
            return [fake_table]

    class _FakePdf:
        pages = [_FakePage()]
        def __enter__(self): return self
        def __exit__(self, *a): return False

    fake_pp = types.ModuleType("pdfplumber")
    fake_pp.open = MagicMock(return_value=_FakePdf())
    monkeypatch.setitem(sys.modules, "pdfplumber", fake_pp)
    monkeypatch.setattr(parser_module, "_table_has_data", lambda t: True)
    monkeypatch.setattr(parser_module, "_table_to_markdown", lambda t: "md")

    parser_module._extract_all_tables_as_sections("/tmp/fake.pdf")
    assert "lines" in strategies_used
    assert "text" in strategies_used
