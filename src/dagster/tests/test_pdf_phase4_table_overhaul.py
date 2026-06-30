"""Phase 4 (2026-05-22) — table extraction overhaul tests.

Verifies:
  - _classify_page_table_type: line + rect thresholds, env overrides
  - _classify_pages_from_pdf: handles missing fitz gracefully
  - _extract_tables_via_docling_only: do_ocr=False, generate_picture_images=False
  - _extract_all_tables_as_sections per-page routing:
      • bordered → docling-tables-only (or pdfplumber-lines fallback)
      • borderless → pdfplumber-text ONLY (no expensive lines pass)
      • classification_failed → legacy dual-pass
      • existing_docling_tables reused without re-invoking docling
  - cross-engine dedupe via _table_signature stable across paths
  - pdfplumber unavailable → still return docling tables (no data loss)

Run with:
    pytest src/dagster/tests/test_pdf_phase4_table_overhaul.py -v
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest


sys.modules.setdefault("boto3", MagicMock())
sys.modules.setdefault("botocore", MagicMock())
sys.modules.setdefault("botocore.config", MagicMock())


@pytest.fixture
def parser_module():
    import importlib
    from georag_dagster.parsers import pdf_report
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
# 11. _extract_tables_via_docling_only — docling missing → []
# ---------------------------------------------------------------------------

def test_extract_tables_via_docling_only_handles_missing_docling(
    parser_module, monkeypatch,
):
    # Ensure docling import raises ImportError
    for key in list(sys.modules):
        if key.startswith("docling"):
            monkeypatch.setitem(sys.modules, key, MagicMock(side_effect=ImportError))
    # Inject a stub that raises ImportError when its attrs are accessed
    class _ImportTrap:
        def __getattr__(self, name):
            raise ImportError("docling not installed")
    monkeypatch.setitem(sys.modules, "docling.document_converter", _ImportTrap())

    out = parser_module._extract_tables_via_docling_only("/tmp/fake.pdf")
    assert out == []


# ---------------------------------------------------------------------------
# 12. _extract_tables_via_docling_only — happy path returns ReportSections
# ---------------------------------------------------------------------------

def test_extract_tables_via_docling_only_happy_path(parser_module, monkeypatch):
    fake_table = MagicMock()
    fake_table.export_to_markdown = MagicMock(
        return_value="| h1 | h2 |\n|---|---|\n| a | b |"
    )
    prov = MagicMock()
    prov.page_no = 4
    fake_table.prov = [prov]

    fake_doc = MagicMock()
    fake_doc.tables = [fake_table]
    fake_result = MagicMock()
    fake_result.document = fake_doc

    converter = MagicMock()
    converter.convert = MagicMock(return_value=fake_result)
    DocumentConverter = MagicMock(return_value=converter)

    captured_opts = {}

    class _PdfFormatOption:
        def __init__(self, pipeline_options=None):
            captured_opts["pipeline_options"] = pipeline_options

    class _PdfPipelineOptions:
        def __init__(self):
            self.do_ocr = True       # docling default — must be overridden
            self.do_table_structure = False
            self.generate_picture_images = True

    class _AcceleratorOptions:
        def __init__(self, device=None):
            self.device = device

    class _AcceleratorDevice:
        CUDA = "cuda"

    monkeypatch.setitem(sys.modules, "docling.document_converter",
                        types.SimpleNamespace(
                            DocumentConverter=DocumentConverter,
                            PdfFormatOption=_PdfFormatOption,
                        ))
    monkeypatch.setitem(sys.modules, "docling.datamodel.base_models",
                        types.SimpleNamespace(InputFormat=types.SimpleNamespace(PDF="pdf")))
    monkeypatch.setitem(sys.modules, "docling.datamodel.pipeline_options",
                        types.SimpleNamespace(
                            AcceleratorDevice=_AcceleratorDevice,
                            AcceleratorOptions=_AcceleratorOptions,
                            PdfPipelineOptions=_PdfPipelineOptions,
                        ))

    sections = parser_module._extract_tables_via_docling_only("/tmp/fake.pdf")
    assert len(sections) == 1
    assert sections[0].section_title == "Table (docling, page 4)"
    assert sections[0].page_first == 4

    # Verify the pipeline options were configured for tables-only mode
    pipe = captured_opts["pipeline_options"]
    assert pipe.do_ocr is False
    assert pipe.do_table_structure is True
    assert pipe.generate_picture_images is False


# ---------------------------------------------------------------------------
# 13. _extract_tables_via_docling_only — docling exception returns []
# ---------------------------------------------------------------------------

def test_extract_tables_via_docling_only_handles_convert_exception(
    parser_module, monkeypatch,
):
    converter = MagicMock()
    converter.convert = MagicMock(side_effect=RuntimeError("simulated docling crash"))
    DocumentConverter = MagicMock(return_value=converter)

    class _PdfFormatOption:
        def __init__(self, pipeline_options=None): pass

    class _PdfPipelineOptions:
        def __init__(self):
            self.do_ocr = False
            self.do_table_structure = True
            self.generate_picture_images = False

    monkeypatch.setitem(sys.modules, "docling.document_converter",
                        types.SimpleNamespace(
                            DocumentConverter=DocumentConverter,
                            PdfFormatOption=_PdfFormatOption,
                        ))
    monkeypatch.setitem(sys.modules, "docling.datamodel.base_models",
                        types.SimpleNamespace(InputFormat=types.SimpleNamespace(PDF="pdf")))
    monkeypatch.setitem(sys.modules, "docling.datamodel.pipeline_options",
                        types.SimpleNamespace(
                            AcceleratorDevice=types.SimpleNamespace(CUDA="cuda"),
                            AcceleratorOptions=lambda **k: types.SimpleNamespace(**k),
                            PdfPipelineOptions=_PdfPipelineOptions,
                        ))

    out = parser_module._extract_tables_via_docling_only("/tmp/fake.pdf")
    assert out == []


# ---------------------------------------------------------------------------
# 14. _extract_all_tables_as_sections — existing_docling_tables reused (no re-invoke)
# ---------------------------------------------------------------------------

def test_extract_all_tables_reuses_existing_docling_tables(
    parser_module, monkeypatch,
):
    existing = [
        parser_module.ReportSection(
            section_number=None,
            section_title="Table (docling, page 5)",
            text="| col1 | col2 |\n|---|---|\n| x | y |",
            page_first=5, page_last=5,
        ),
    ]
    # Spy on _extract_tables_via_docling_only — should NEVER be called
    spy = MagicMock()
    monkeypatch.setattr(parser_module, "_extract_tables_via_docling_only", spy)
    # Stub the page classifier to skip pdfplumber-lines invocation
    monkeypatch.setattr(parser_module, "_classify_pages_from_pdf", lambda p: {1: "borderless"})
    # Stub pdfplumber (raises ImportError → my graceful fallback kicks in)
    monkeypatch.setitem(sys.modules, "pdfplumber", None)

    out = parser_module._extract_all_tables_as_sections(
        "/tmp/fake.pdf", existing_docling_tables=existing,
    )
    spy.assert_not_called()
    assert len(out) == 1
    assert out[0].section_title == "Table (docling, page 5)"


# ---------------------------------------------------------------------------
# 15. _extract_all_tables_as_sections — bordered pages → docling tables-only invoked
# ---------------------------------------------------------------------------

def test_extract_all_tables_invokes_docling_when_bordered_found(
    parser_module, monkeypatch,
):
    monkeypatch.setenv("PDF_PARSER_DOCLING_ENABLED", "true")
    # Test fixture has 1 bordered page; lower threshold so docling fires.
    monkeypatch.setenv("PDF_PARSER_DOCLING_TABLES_MIN_BORDERED_PAGES", "1")
    monkeypatch.setattr(
        parser_module, "_classify_pages_from_pdf",
        lambda p: {1: "bordered", 2: "borderless"},
    )

    docling_table = parser_module.ReportSection(
        section_number=None,
        section_title="Table (docling, page 1)",
        text="| a | b |\n|---|---|\n| 1 | 2 |",
        page_first=1, page_last=1,
    )
    invocations = []

    def _fake_docling(path):
        invocations.append(path)
        return [docling_table]

    monkeypatch.setattr(parser_module, "_extract_tables_via_docling_only", _fake_docling)
    monkeypatch.setitem(sys.modules, "pdfplumber", None)

    out = parser_module._extract_all_tables_as_sections("/tmp/fake.pdf")
    assert invocations == ["/tmp/fake.pdf"]
    assert any("page 1" in s.section_title for s in out)


# ---------------------------------------------------------------------------
# 16. _extract_all_tables_as_sections — borderless-only PDF skips docling
# ---------------------------------------------------------------------------

def test_extract_all_tables_skips_docling_when_no_bordered_pages(
    parser_module, monkeypatch,
):
    monkeypatch.setenv("PDF_PARSER_DOCLING_ENABLED", "true")
    monkeypatch.setattr(
        parser_module, "_classify_pages_from_pdf",
        lambda p: {1: "borderless", 2: "borderless"},
    )
    spy = MagicMock()
    monkeypatch.setattr(parser_module, "_extract_tables_via_docling_only", spy)
    monkeypatch.setitem(sys.modules, "pdfplumber", None)

    parser_module._extract_all_tables_as_sections("/tmp/fake.pdf")
    spy.assert_not_called()


# ---------------------------------------------------------------------------
# 17. _extract_all_tables_as_sections — DOCLING off + bordered → pdfplumber-lines fallback
# ---------------------------------------------------------------------------

def test_extract_all_tables_falls_back_to_pdfplumber_lines_when_docling_off(
    parser_module, monkeypatch,
):
    monkeypatch.setenv("PDF_PARSER_DOCLING_ENABLED", "false")
    monkeypatch.setattr(
        parser_module, "_classify_pages_from_pdf",
        lambda p: {1: "bordered"},
    )
    spy_docling = MagicMock()
    monkeypatch.setattr(parser_module, "_extract_tables_via_docling_only", spy_docling)

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
    spy_docling.assert_not_called()
    # 'lines' must run on the bordered page (fallback). 'text' also
    # runs (catches borderless tables co-existing on bordered pages).
    assert "lines" in strategies_used
    assert "text" in strategies_used


# ---------------------------------------------------------------------------
# 18. _extract_all_tables_as_sections — borderless page → text strategy ONLY
# ---------------------------------------------------------------------------

def test_extract_all_tables_borderless_uses_text_strategy_only(
    parser_module, monkeypatch,
):
    monkeypatch.setenv("PDF_PARSER_DOCLING_ENABLED", "true")
    monkeypatch.setattr(
        parser_module, "_classify_pages_from_pdf",
        lambda p: {1: "borderless"},
    )
    monkeypatch.setattr(
        parser_module, "_extract_tables_via_docling_only",
        lambda p: [],
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
# 19. _extract_all_tables_as_sections — pdfplumber unavailable → docling-only returned
# ---------------------------------------------------------------------------

def test_extract_all_tables_pdfplumber_unavailable_returns_docling_only(
    parser_module, monkeypatch,
):
    existing = [
        parser_module.ReportSection(
            section_number=None,
            section_title="Table (docling, page 3)",
            text="| h1 | h2 |\n|---|---|\n| x | y |",
            page_first=3, page_last=3,
        ),
    ]
    monkeypatch.setattr(
        parser_module, "_classify_pages_from_pdf",
        lambda p: {1: "bordered"},
    )
    # Force pdfplumber.open to raise
    fake_pp = types.ModuleType("pdfplumber")
    fake_pp.open = MagicMock(side_effect=ImportError("no pdfplumber"))
    monkeypatch.setitem(sys.modules, "pdfplumber", fake_pp)

    out = parser_module._extract_all_tables_as_sections(
        "/tmp/fake.pdf", existing_docling_tables=existing,
    )
    assert len(out) == 1
    assert out[0].section_title == "Table (docling, page 3)"


# ---------------------------------------------------------------------------
# 20. Cross-engine dedupe — same table from docling + pdfplumber → one entry
# ---------------------------------------------------------------------------

def test_extract_all_tables_dedupes_across_engines(parser_module, monkeypatch):
    # Both engines find the same table body on page 1 → only one section
    shared_md = "| col1 | col2 |\n|---|---|\n| a | b |"
    docling_section = parser_module.ReportSection(
        section_number=None,
        section_title="Table (docling, page 1)",
        text=shared_md,
        page_first=1, page_last=1,
    )

    monkeypatch.setattr(
        parser_module, "_classify_pages_from_pdf",
        lambda p: {1: "bordered"},
    )

    # Stub pdfplumber to return a table that produces the SAME markdown
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

    out = parser_module._extract_all_tables_as_sections(
        "/tmp/fake.pdf", existing_docling_tables=[docling_section],
    )
    # Dedupe: 1 entry, docling's title (came first in merge order)
    assert len(out) == 1
    assert "docling" in out[0].section_title


# ---------------------------------------------------------------------------
# 21. Classification-failed fallback runs legacy dual-pass
# ---------------------------------------------------------------------------

def test_docling_threshold_skips_docling_below_min_bordered_pages(
    parser_module, monkeypatch,
):
    """Phase 4 perf-protect: when bordered page count is below
    PDF_PARSER_DOCLING_TABLES_MIN_BORDERED_PAGES (default 30), don't
    invoke docling-tables-only — pdfplumber-lines is faster on small
    docs. This protects small-PDF wall clock from docling's startup
    overhead."""
    monkeypatch.setenv("PDF_PARSER_DOCLING_ENABLED", "true")
    monkeypatch.setenv("PDF_PARSER_DOCLING_TABLES_MIN_BORDERED_PAGES", "30")

    monkeypatch.setattr(
        parser_module, "_classify_pages_from_pdf",
        # Only 5 bordered pages — below the threshold
        lambda p: {n: "bordered" for n in range(1, 6)},
    )
    spy_docling = MagicMock()
    monkeypatch.setattr(parser_module, "_extract_tables_via_docling_only", spy_docling)

    # Pdfplumber stub — needs to satisfy the lines+text fallback walk
    fake_table = [["a", "b"], ["1", "2"]]

    class _FakePage:
        def extract_tables(self, table_settings):
            return [fake_table]

    class _FakePdf:
        pages = [_FakePage() for _ in range(5)]
        def __enter__(self): return self
        def __exit__(self, *a): return False

    fake_pp = types.ModuleType("pdfplumber")
    fake_pp.open = MagicMock(return_value=_FakePdf())
    monkeypatch.setitem(sys.modules, "pdfplumber", fake_pp)

    parser_module._extract_all_tables_as_sections("/tmp/fake.pdf")
    # Threshold prevents docling invocation
    spy_docling.assert_not_called()


def test_docling_threshold_fires_when_bordered_pages_exceed_min(
    parser_module, monkeypatch,
):
    """Inverse of the above — large bordered-page count clears the
    threshold and docling-tables-only fires."""
    monkeypatch.setenv("PDF_PARSER_DOCLING_ENABLED", "true")
    monkeypatch.setenv("PDF_PARSER_DOCLING_TABLES_MIN_BORDERED_PAGES", "5")

    monkeypatch.setattr(
        parser_module, "_classify_pages_from_pdf",
        lambda p: {n: "bordered" for n in range(1, 11)},  # 10 ≥ 5
    )
    spy_docling = MagicMock(return_value=[])
    monkeypatch.setattr(parser_module, "_extract_tables_via_docling_only", spy_docling)
    monkeypatch.setitem(sys.modules, "pdfplumber", None)

    parser_module._extract_all_tables_as_sections("/tmp/fake.pdf")
    spy_docling.assert_called_once()


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
