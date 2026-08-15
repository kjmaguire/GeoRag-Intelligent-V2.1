"""Phase 4 (2026-05-22) — table extraction overhaul tests.

Verifies:
  - _classify_page_table_type: line + rect thresholds, env overrides
  - _iter_pdfium_path_items: segment-walking primitive extraction
  - _classify_pages_from_pdf: handles missing pypdfium2 gracefully
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

Engine note (2026-08-15): the classifier walked fitz's (PyMuPDF's)
`page.get_drawings()` output until PyMuPDF was removed for its AGPL
license (2026-05-27) — after that, `_classify_pages_from_pdf`'s
`import pymupdf` always raised ImportError and it silently returned {}
on every call, so the "bordered vs borderless" routing below was dead
in production despite these tests passing (the old tests stubbed
`sys.modules["pymupdf"]`, masking the always-fails-for-real import).
Re-ported onto pypdfium2 (already a dependency); `_classify_page_table_type`
now takes a flat list of ("l", p1, p2) / ("re",) primitives produced by
the new `_iter_pdfium_path_items` helper instead of fitz-shaped drawing
dicts.

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


# ---------------------------------------------------------------------------
# 1. Classifier — zero items → borderless
# ---------------------------------------------------------------------------

def test_classify_empty_items_is_borderless(parser_module):
    assert parser_module._classify_page_table_type([]) == "borderless"


# ---------------------------------------------------------------------------
# 2. Classifier — line threshold (default 3)
# ---------------------------------------------------------------------------

def test_classify_horizontal_lines_above_threshold_is_bordered(parser_module):
    items = [("l", (0, 100), (200, 100)) for _ in range(5)]
    assert parser_module._classify_page_table_type(items) == "bordered"


def test_classify_horizontal_lines_below_threshold_is_borderless(parser_module):
    items = [("l", (0, 100), (200, 100))]
    assert parser_module._classify_page_table_type(items) == "borderless"


# ---------------------------------------------------------------------------
# 3. Classifier — line length filter (short lines don't count)
# ---------------------------------------------------------------------------

def test_classify_ignores_short_horizontal_lines(parser_module):
    # 5 very short lines (< 30 points) — should not classify as bordered
    items = [("l", (0, 100), (5, 100)) for _ in range(5)]
    assert parser_module._classify_page_table_type(items) == "borderless"


# ---------------------------------------------------------------------------
# 4. Classifier — vertical lines don't count as horizontal
# ---------------------------------------------------------------------------

def test_classify_vertical_lines_dont_count(parser_module):
    # 5 vertical lines (Δx ~ 0)
    items = [("l", (100, 0), (100, 200)) for _ in range(5)]
    assert parser_module._classify_page_table_type(items) == "borderless"


# ---------------------------------------------------------------------------
# 5. Classifier — rectangle threshold (default 20)
# ---------------------------------------------------------------------------

def test_classify_rectangles_above_rect_threshold_is_bordered(parser_module):
    items = [("re",) for _ in range(25)]
    assert parser_module._classify_page_table_type(items) == "bordered"


def test_classify_rectangles_below_rect_threshold_is_borderless(parser_module):
    items = [("re",) for _ in range(15)]
    assert parser_module._classify_page_table_type(items) == "borderless"


# ---------------------------------------------------------------------------
# 6. Classifier — both line + rect together
# ---------------------------------------------------------------------------

def test_classify_mixed_lines_and_rects_is_bordered(parser_module):
    items = [
        ("l", (0, 100), (200, 100)),
        ("l", (0, 200), (200, 200)),
        ("l", (0, 300), (200, 300)),
        ("re",),
    ]
    # 3 horizontal lines exactly hits the line threshold
    assert parser_module._classify_page_table_type(items) == "bordered"


# ---------------------------------------------------------------------------
# 7. Classifier — unrecognized item kinds ignored (defense in depth; in
#    practice _iter_pdfium_path_items never emits anything but "l"/"re")
# ---------------------------------------------------------------------------

def test_classify_ignores_unrecognized_item_kinds(parser_module):
    items = [("qu",) for _ in range(50)] + [("c",) for _ in range(50)]
    assert parser_module._classify_page_table_type(items) == "borderless"


# ---------------------------------------------------------------------------
# 8. Classifier — explicit threshold args override defaults
# ---------------------------------------------------------------------------

def test_classify_custom_thresholds(parser_module):
    items = [("re",) for _ in range(5)]
    # Default rect threshold 20 → borderless
    assert parser_module._classify_page_table_type(items) == "borderless"
    # Tighten to 3 → bordered
    assert parser_module._classify_page_table_type(
        items, rect_threshold=3,
    ) == "bordered"


# ---------------------------------------------------------------------------
# _iter_pdfium_path_items — segment-walking primitive extraction
#
# These tests fake pypdfium2's raw ctypes surface directly (CountSegments /
# GetPathSegment / SegmentGetType / SegmentGetPoint / SegmentGetClose) so
# they exercise the real segment-walking loop without needing a real
# PDFium-rendered PDF. `FPDFPathSegment_GetPoint` is faked with genuine
# `ctypes.cast(...)`-based pointer writes (no native library involved —
# this is pure in-process pointer manipulation), matching the exact
# `ctypes.byref()` contract the production code relies on.
# ---------------------------------------------------------------------------

class _FakeSegment:
    def __init__(self, seg_type, x, y, closed=False):
        self.type = seg_type
        self.x = x
        self.y = y
        self.closed = closed


def _fake_pdfium_raw(segments: list[_FakeSegment]):
    """Build a fake `pypdfium2.raw`-shaped object driving one PATH object's
    segment list (as `_iter_pdfium_path_items` would see it)."""
    import ctypes as _ctypes

    fake_raw = MagicMock()
    fake_raw.FPDF_SEGMENT_MOVETO = "MOVETO"
    fake_raw.FPDF_SEGMENT_LINETO = "LINETO"
    fake_raw.FPDF_SEGMENT_BEZIERTO = "BEZIERTO"
    fake_raw.FPDF_SEGMENT_UNKNOWN = "UNKNOWN"
    fake_raw.FPDFPath_CountSegments = MagicMock(return_value=len(segments))
    fake_raw.FPDFPath_GetPathSegment = MagicMock(
        side_effect=lambda _obj, i: segments[i],
    )
    fake_raw.FPDFPathSegment_GetType = MagicMock(side_effect=lambda seg: seg.type)
    fake_raw.FPDFPathSegment_GetClose = MagicMock(
        side_effect=lambda seg: 1 if seg.closed else 0,
    )

    def _get_point(seg, x_ref, y_ref):
        _ctypes.cast(x_ref, _ctypes.POINTER(_ctypes.c_float))[0] = float(seg.x)
        _ctypes.cast(y_ref, _ctypes.POINTER(_ctypes.c_float))[0] = float(seg.y)

    fake_raw.FPDFPathSegment_GetPoint = MagicMock(side_effect=_get_point)
    return fake_raw


def test_iter_path_items_two_point_open_subpath_is_line(parser_module):
    segs = [
        _FakeSegment("MOVETO", 0, 100),
        _FakeSegment("LINETO", 200, 100),
    ]
    items = parser_module._iter_pdfium_path_items(object(), _fake_pdfium_raw(segs))
    assert items == [("l", (0.0, 100.0), (200.0, 100.0))]


def test_iter_path_items_closed_polygon_is_rect(parser_module):
    # MOVETO + 3x LINETO with close on the last segment — exactly how
    # PDFium decomposes the `re` operator.
    segs = [
        _FakeSegment("MOVETO", 0, 0),
        _FakeSegment("LINETO", 100, 0),
        _FakeSegment("LINETO", 100, 20),
        _FakeSegment("LINETO", 0, 20, closed=True),
    ]
    items = parser_module._iter_pdfium_path_items(object(), _fake_pdfium_raw(segs))
    assert items == [("re",)]


def test_iter_path_items_bezier_subpath_dropped(parser_module):
    segs = [
        _FakeSegment("MOVETO", 0, 0),
        _FakeSegment("BEZIERTO", 50, 50),
        _FakeSegment("LINETO", 100, 0),
    ]
    items = parser_module._iter_pdfium_path_items(object(), _fake_pdfium_raw(segs))
    assert items == []


def test_iter_path_items_multiple_subpaths_in_one_object(parser_module):
    # One combined path object stroking two disjoint 2-point lines — the
    # "whole grid drawn as one path" case a bbox-only approach can't see.
    segs = [
        _FakeSegment("MOVETO", 0, 100),
        _FakeSegment("LINETO", 200, 100),
        _FakeSegment("MOVETO", 0, 200),
        _FakeSegment("LINETO", 200, 200),
    ]
    items = parser_module._iter_pdfium_path_items(object(), _fake_pdfium_raw(segs))
    assert items == [
        ("l", (0.0, 100.0), (200.0, 100.0)),
        ("l", (0.0, 200.0), (200.0, 200.0)),
    ]


def test_iter_path_items_no_segments_returns_empty(parser_module):
    items = parser_module._iter_pdfium_path_items(object(), _fake_pdfium_raw([]))
    assert items == []


# ---------------------------------------------------------------------------
# 9. _classify_pages_from_pdf — env vars honored
# ---------------------------------------------------------------------------

def test_classify_pages_env_thresholds_honored(parser_module, monkeypatch):
    monkeypatch.setenv("TABLE_BORDER_LINE_THRESHOLD", "1")
    monkeypatch.setenv("TABLE_BORDER_RECT_THRESHOLD", "1")

    fake_path_object = object()
    fake_raw = _fake_pdfium_raw([
        _FakeSegment("MOVETO", 0, 100),
        _FakeSegment("LINETO", 200, 100),
    ])
    fake_raw.FPDF_PAGEOBJ_PATH = "PATH"

    fake_page = MagicMock()
    fake_page.get_objects = MagicMock(return_value=[fake_path_object])

    class _FakeDoc:
        def __iter__(self): return iter([fake_page])
        def close(self): pass

    fake_pdfium = types.ModuleType("pypdfium2")
    fake_pdfium.PdfDocument = MagicMock(return_value=_FakeDoc())
    fake_pdfium.raw = fake_raw
    monkeypatch.setitem(sys.modules, "pypdfium2", fake_pdfium)
    monkeypatch.setitem(sys.modules, "pypdfium2.raw", fake_raw)

    result = parser_module._classify_pages_from_pdf("/tmp/fake.pdf")
    assert result == {1: "bordered"}


# ---------------------------------------------------------------------------
# 10. _classify_pages_from_pdf — pypdfium2 open failure returns empty
# ---------------------------------------------------------------------------

def test_classify_pages_pdfium_open_failure_returns_empty(parser_module, monkeypatch):
    fake_pdfium = types.ModuleType("pypdfium2")
    fake_pdfium.PdfDocument = MagicMock(side_effect=RuntimeError("can't open"))
    fake_pdfium.raw = MagicMock()
    monkeypatch.setitem(sys.modules, "pypdfium2", fake_pdfium)
    monkeypatch.setitem(sys.modules, "pypdfium2.raw", fake_pdfium.raw)
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
