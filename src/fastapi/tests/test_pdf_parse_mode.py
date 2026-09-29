"""PDF_PARSE_MODE — which pages of a text-layer PDF the remote engine reads.

Three modes (see the pdf_report module docstring):

  ocr_only (default)  today's behaviour; the engine reads only image pages
  tables              prose stays native; text-layer pages with a table are
                      ALSO read by the engine and its grids replace the
                      pdfplumber tables for that page
  all                 every page goes to the engine; native text is only the
                      fallback when the engine cannot answer

The engine is faked at the ``cohere_parse_client`` seam
(``ocr_page_block_sync`` / ``ocr_page_sync``), so the real grouping, the real
per-document page budget and the real fallback ladder all run.
"""

from __future__ import annotations

import logging
import sys
import types
from unittest.mock import MagicMock

import pytest

from app.services.ingest import cohere_parse_client as cpc
from app.services.ingest import ocr_engine, pdf_report
from app.services.ingest.ocr_types import PageOcrResult

_BASE_PROSE = (
    "The Madsen gold deposit lies within the Red Lake greenstone belt "
    "and hosts quartz vein mineralization across the Austin and McVeigh "
    "zones with grades near seven grams per tonne. "
)
_GRID = [["Class", "Tonnes", "Au g/t"], ["Indicated", "1,200,000", "2.45"], ["Inferred", "300,000", "1.10"]]


def _native(n: int) -> str:
    """Realistic prose (passes the F16 native-text screen), distinct per page."""
    return f"{_BASE_PROSE * 2}Drill hole DH-{n:03d} intersected the zone at depth."


def _engine_text(n: int) -> str:
    return f"COHERE page {n}. {_BASE_PROSE * 2}"


def _result(n: int, *, tables=None, text: str | None = None) -> PageOcrResult:
    return PageOcrResult(
        text if text is not None else _engine_text(n),
        0.0,
        tables=list(tables or []),
        confidence_reported=False,
    )


class FakeEngine:
    """Records every request; answers per page from ``answers``.

    ``answers[page]`` is a PageOcrResult, or absent/None to fail that page
    (absent from a group mapping; ``request_succeeded=False`` per page).
    """

    def __init__(self, answers: dict[int, PageOcrResult | None]) -> None:
        self.answers = answers
        self.block_calls: list[list[int]] = []
        self.single_calls: list[int] = []

    def block(self, pdf_path, page_numbers):
        pages = sorted(page_numbers)
        self.block_calls.append(pages)
        return {p: self.answers[p] for p in pages if self.answers.get(p) is not None}

    def single(self, pdf_path, page_num):
        self.single_calls.append(page_num)
        found = self.answers.get(page_num)
        if found is None:
            return PageOcrResult(
                "", 0.0, request_succeeded=False, error="boom", confidence_reported=False
            )
        return found

    @property
    def pages_requested(self) -> set[int]:
        return {p for grp in self.block_calls for p in grp} | set(self.single_calls)


def _install_fake_pypdfium2(monkeypatch, page_texts):
    class _TextPage:
        def __init__(self, text):
            self._text = text

        def get_text_bounded(self):
            return self._text

    class _Page:
        def __init__(self, text):
            self._text = text

        def get_textpage(self):
            return _TextPage(self._text)

    class _Doc:
        def __init__(self):
            self._pages = [_Page(t) for t in page_texts]

        def get_metadata_dict(self):
            return {"Title": "T"}

        def __len__(self):
            return len(self._pages)

        def __getitem__(self, i):
            return self._pages[i]

        def close(self):
            pass

    fake = types.ModuleType("pypdfium2")
    fake.PdfDocument = MagicMock(return_value=_Doc())
    monkeypatch.setitem(sys.modules, "pypdfium2", fake)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("OCR_ENGINE", "cohere_parse")
    monkeypatch.setenv("COHERE_API_KEY", "test-only-not-a-real-cohere-key")
    monkeypatch.delenv("PDF_PARSE_MODE", raising=False)
    monkeypatch.delenv("OCR_PAGES_PER_BATCH", raising=False)
    monkeypatch.delenv("BEDROCK_PARSE_MODEL_ID", raising=False)
    monkeypatch.setenv("OCR_MAX_PAGES_PER_DOC", "300")
    monkeypatch.setenv("PDF_OCR_PAGE_CONCURRENCY", "1")
    for name in (
        "AZURE_FOUNDRY_ENDPOINT",
        "AZURE_FOUNDRY_API_KEY",
        "AZURE_FOUNDRY_PARSE_DEPLOYMENT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ocr_engine, "_WARNED", set())
    monkeypatch.setattr(ocr_engine, "_PARSE_MODE_WARNED", set())
    monkeypatch.setattr(pdf_report, "_PARSE_MODE_UNCONFIGURED_WARNED", False)
    monkeypatch.setattr(pdf_report, "_OCR_PAGES_USED", {})
    monkeypatch.setattr(pdf_report, "_OCR_CAP_LOGGED", set())
    monkeypatch.setattr(pdf_report, "_OCR_CAP_EXHAUSTED", {})


class _Tripwire:
    """Records tesseract use. It raises nothing: `_ocr_single_page` swallows
    exceptions, so a raising tripwire would pass whether or not it fired."""

    def __init__(self) -> None:
        self.convert = MagicMock(return_value=[])

    @property
    def called(self) -> bool:
        return self.convert.called


@pytest.fixture
def tesseract_tripwire(monkeypatch):
    """Tesseract stand-in; every test using it asserts `.called` is False."""
    tripwire = _Tripwire()
    fake = types.ModuleType("pytesseract")
    fake.Output = types.SimpleNamespace(DICT="dict")
    fake.image_to_data = MagicMock(return_value={})
    fake.image_to_string = MagicMock(return_value="")
    monkeypatch.setitem(sys.modules, "pytesseract", fake)
    pdf2image = types.ModuleType("pdf2image")
    pdf2image.convert_from_path = tripwire.convert
    monkeypatch.setitem(sys.modules, "pdf2image", pdf2image)
    return tripwire


def _install_engine(monkeypatch, engine: FakeEngine) -> FakeEngine:
    monkeypatch.setattr(cpc, "ocr_page_block_sync", engine.block)
    monkeypatch.setattr(cpc, "ocr_page_sync", engine.single)
    return engine


def _summary(warnings):
    found = [w for w in warnings if isinstance(w, dict) and w.get("code") == "pdf_parse_mode_summary"]
    assert len(found) <= 1
    return found[0] if found else None


# ---------------------------------------------------------------------------
# Selecting the mode
# ---------------------------------------------------------------------------


class TestSelectedParseMode:
    def test_unset_and_blank_are_ocr_only_and_silent(self, monkeypatch, caplog) -> None:
        with caplog.at_level(logging.DEBUG, logger="georag.ingest.ocr_engine"):
            assert ocr_engine.selected_parse_mode() == "ocr_only"
            monkeypatch.setenv("PDF_PARSE_MODE", "  ")
            assert ocr_engine.selected_parse_mode() == "ocr_only"
            monkeypatch.setenv("PDF_PARSE_MODE", "OCR_ONLY")
            assert ocr_engine.selected_parse_mode() == "ocr_only"
        assert caplog.records == []

    @pytest.mark.parametrize("raw", ["tables", "TABLES", " all "])
    def test_tables_and_all_are_selected_with_the_remote_engine(self, monkeypatch, raw) -> None:
        monkeypatch.setenv("PDF_PARSE_MODE", raw)
        assert ocr_engine.selected_parse_mode() == raw.strip().lower()

    @pytest.mark.parametrize("raw", ["tables", "all"])
    def test_tesseract_engine_warns_once_and_behaves_as_ocr_only(
        self, monkeypatch, caplog, raw
    ) -> None:
        monkeypatch.setenv("OCR_ENGINE", "tesseract")
        monkeypatch.setenv("PDF_PARSE_MODE", raw)
        with caplog.at_level(logging.WARNING, logger="georag.ingest.ocr_engine"):
            assert ocr_engine.selected_parse_mode() == "ocr_only"
            assert ocr_engine.selected_parse_mode() == "ocr_only"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "PDF_PARSE_MODE" in warnings[0].getMessage()

    def test_unknown_value_logs_critical_once_and_behaves_as_ocr_only(
        self, monkeypatch, caplog
    ) -> None:
        monkeypatch.setenv("PDF_PARSE_MODE", "everything")
        with caplog.at_level(logging.CRITICAL, logger="georag.ingest.ocr_engine"):
            assert ocr_engine.selected_parse_mode() == "ocr_only"
            assert ocr_engine.selected_parse_mode() == "ocr_only"
        critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
        assert len(critical) == 1
        assert "everything" in critical[0].getMessage()

    def test_no_api_key_is_ocr_only_with_one_critical(self, monkeypatch, caplog) -> None:
        monkeypatch.setenv("PDF_PARSE_MODE", "all")
        monkeypatch.delenv("COHERE_API_KEY")
        with caplog.at_level(logging.CRITICAL, logger="app.services.ingest.pdf_report"):
            assert pdf_report._effective_parse_mode() == "ocr_only"
            assert pdf_report._effective_parse_mode() == "ocr_only"
        assert len([r for r in caplog.records if r.levelno == logging.CRITICAL]) == 1


# ---------------------------------------------------------------------------
# Table hints
# ---------------------------------------------------------------------------


class TestTableHints:
    @pytest.mark.parametrize(
        "text",
        [
            "Summary of the Mineral Resource Estimate at 0.5 g/t cut-off",
            "The Mineral Reserve statement is effective 2024-01-01.",
            "Some intro line\nTable 14.1 Indicated and Inferred Resources\nrow",
            "  TABLE 2-3 Drill collars",
        ],
    )
    def test_hints_that_send_a_page_to_the_engine(self, text) -> None:
        assert pdf_report._page_has_table_keywords(text)

    @pytest.mark.parametrize(
        "text",
        [
            "Plain geology prose with no tabular content at all.",
            "As shown in Table 3 the grades improve with depth.",  # cross-reference, mid-line
        ],
    )
    def test_prose_and_cross_references_do_not(self, text) -> None:
        assert not pdf_report._page_has_table_keywords(text)

    def test_candidates_are_text_layer_pages_only(self) -> None:
        per_page_text = [(1, "prose"), (2, "prose"), (3, "Mineral Resource Estimate"), (4, "x")]
        method = {1: "fitz_native", 2: "fitz_native", 3: "fitz_native", 4: "cohere_parse"}

        pages, reasons = pdf_report._tables_mode_candidate_pages(per_page_text, method, {2, 4})

        assert pages == [2, 3]  # 4 was already read by the engine
        assert reasons == {"table_detected": [2], "keyword_only": [3]}


# ---------------------------------------------------------------------------
# ocr_only — the default must not move
# ---------------------------------------------------------------------------


class TestOcrOnlyIsUnchanged:
    def test_text_layer_pages_never_reach_the_engine(self, monkeypatch) -> None:
        engine = _install_engine(monkeypatch, FakeEngine({}))
        _install_fake_pypdfium2(monkeypatch, [_native(1), _native(2), _native(3)])

        out = pdf_report._parse_with_fitz("/data/default.pdf")
        text, _t, _skip, warnings, _langs, per_page, image_pages, method, conf, tables = out

        assert engine.pages_requested == set()
        assert method == {1: "fitz_native", 2: "fitz_native", 3: "fitz_native"}
        assert [t for _n, t in per_page] == [_native(1), _native(2), _native(3)]
        assert image_pages == []
        assert tables == {}
        assert _summary(warnings) is None

    def test_an_image_page_is_still_read_by_the_engine_alone(self, monkeypatch) -> None:
        engine = _install_engine(monkeypatch, FakeEngine({2: _result(2)}))
        _install_fake_pypdfium2(monkeypatch, [_native(1), ""])

        *_, method, _conf, _tables = pdf_report._parse_with_fitz("/data/default2.pdf")

        assert engine.pages_requested == {2}
        assert method == {1: "fitz_native", 2: "cohere_parse"}

    def test_tables_and_all_behave_as_ocr_only_on_tesseract(self, monkeypatch) -> None:
        engine = _install_engine(monkeypatch, FakeEngine({}))
        monkeypatch.setenv("OCR_ENGINE", "tesseract")
        monkeypatch.setenv("PDF_PARSE_MODE", "all")
        _install_fake_pypdfium2(monkeypatch, [_native(1), _native(2)])

        *_, method, _conf, _tables = pdf_report._parse_with_fitz("/data/tess.pdf")

        assert engine.pages_requested == set()
        assert method == {1: "fitz_native", 2: "fitz_native"}


# ---------------------------------------------------------------------------
# all
# ---------------------------------------------------------------------------


class TestAllMode:
    @pytest.fixture(autouse=True)
    def _mode(self, monkeypatch):
        monkeypatch.setenv("PDF_PARSE_MODE", "all")

    def test_every_page_goes_through_the_engine_and_is_labelled_truthfully(
        self, monkeypatch, tesseract_tripwire
    ) -> None:
        engine = _install_engine(
            monkeypatch,
            FakeEngine({1: _result(1), 2: _result(2, tables=[_GRID]), 3: _result(3), 4: _result(4)}),
        )
        # Page 4 has no usable text layer (an image page).
        _install_fake_pypdfium2(monkeypatch, [_native(1), _native(2), _native(3), ""])

        out = pdf_report._parse_with_fitz("/data/all.pdf")
        _text, _t, _skip, warnings, _langs, per_page, image_pages, method, conf, tables = out

        assert engine.pages_requested == {1, 2, 3, 4}
        assert method == {n: "cohere_parse" for n in (1, 2, 3, 4)}
        assert dict(per_page)[1] == _engine_text(1)
        assert dict(per_page)[4] == _engine_text(4)
        assert all(conf[n] is None for n in (1, 2, 3, 4))  # Parse reports no confidence
        assert tables == {2: [_GRID]}
        assert image_pages == []
        # Only the image page (4) may ever reach tesseract, and it is answered
        # by the engine here, so tesseract stays untouched.
        assert not tesseract_tripwire.called

        summary = _summary(warnings)
        assert summary is not None
        assert summary["mode"] == "all"
        assert summary["engine_text_pages"] == [1, 2, 3]  # the text-layer pages it re-read
        assert summary["native_fallback_pages"] == []

    def test_no_review_queue_warning_for_text_layer_pages(self, monkeypatch) -> None:
        _install_engine(monkeypatch, FakeEngine({1: _result(1), 2: _result(2)}))
        _install_fake_pypdfium2(monkeypatch, [_native(1), _native(2)])

        *_, warnings, _langs, _pp, _img, _m, _c, _t = pdf_report._parse_with_fitz("/data/rq.pdf")

        assert not [w for w in warnings if w.get("code") == "ocr_quality_assessment"]

    def test_a_failed_page_keeps_its_native_text_and_never_touches_tesseract(
        self, monkeypatch, tesseract_tripwire
    ) -> None:
        engine = _install_engine(
            monkeypatch, FakeEngine({1: _result(1), 2: None, 3: _result(3)})
        )
        _install_fake_pypdfium2(monkeypatch, [_native(1), _native(2), _native(3)])

        out = pdf_report._parse_with_fitz("/data/fail.pdf")
        _text, _t, _skip, warnings, _langs, per_page, image_pages, method, _conf, _tables = out

        assert method == {1: "cohere_parse", 2: "fitz_native", 3: "cohere_parse"}
        assert dict(per_page)[2] == _native(2)
        assert image_pages == []
        assert [n for n, _t2 in per_page] == [1, 2, 3]  # order restored, nothing dropped
        assert not tesseract_tripwire.called
        assert _summary(warnings)["native_fallback_pages"] == [2]
        # Grouped request AND the per-page retry both ran for page 2.
        assert 2 in engine.single_calls

    def test_engine_text_shorter_than_the_page_minimum_falls_back_to_native(
        self, monkeypatch, tesseract_tripwire
    ) -> None:
        _install_engine(monkeypatch, FakeEngine({1: _result(1, text="tiny")}))
        _install_fake_pypdfium2(monkeypatch, [_native(1)])

        *_, warnings, _langs, per_page, _img, method, _c, _t = pdf_report._parse_with_fitz(
            "/data/short.pdf"
        )

        assert method == {1: "fitz_native"}
        assert dict(per_page)[1] == _native(1)
        assert not tesseract_tripwire.called
        assert _summary(warnings)["native_fallback_pages"] == [1]

    def test_over_budget_pages_fall_back_to_native_and_the_warning_says_so(
        self, monkeypatch, tesseract_tripwire
    ) -> None:
        """400-page report vs OCR_MAX_PAGES_PER_DOC, in miniature: 6 vs 3."""
        monkeypatch.setenv("OCR_MAX_PAGES_PER_DOC", "3")
        answers = {n: _result(n) for n in range(1, 7)}
        engine = _install_engine(monkeypatch, FakeEngine(answers))
        _install_fake_pypdfium2(monkeypatch, [_native(n) for n in range(1, 7)])
        path = "/data/long.pdf"

        out = pdf_report._parse_with_fitz(path)
        _text, _t, _skip, warnings, _langs, per_page, image_pages, method, _conf, _tables = out

        # No page is dropped, and none is read by tesseract.
        assert [n for n, _t2 in per_page] == [1, 2, 3, 4, 5, 6]
        assert image_pages == []
        by_engine = [n for n, m in method.items() if m == "cohere_parse"]
        by_native = [n for n, m in method.items() if m == "fitz_native"]
        assert len(by_engine) == 3
        assert len(by_native) == 3
        for n in by_native:
            assert dict(per_page)[n] == _native(n)
        # The budget was actually honoured: 3 billed pages, not 6.
        assert len(engine.pages_requested) == 3
        assert pdf_report._OCR_PAGES_USED[path] == 3
        assert not tesseract_tripwire.called

        summary = _summary(warnings)
        assert sorted(summary["native_fallback_pages"]) == by_native
        assert sorted(summary["engine_text_pages"]) == by_engine

        budget = pdf_report._ocr_budget_warning(path)
        assert budget is not None
        assert budget["code"] == "ocr_page_budget_exhausted"
        assert budget["parse_mode"] == "all"
        assert "native text layer" in budget["message"]
        assert "read by tesseract, which extracts no table structure — any" not in budget["message"]

    def test_image_only_pages_are_first_in_line_for_the_budget(
        self, monkeypatch, tesseract_tripwire
    ) -> None:
        """A text-layer page that loses the budget keeps its text; an image
        page that loses it falls to tesseract — so image pages go first."""
        monkeypatch.setenv("OCR_MAX_PAGES_PER_DOC", "1")
        _install_engine(
            monkeypatch, FakeEngine({1: _result(1), 2: _result(2), 3: _result(3)})
        )
        # Page 3 is the image-only page (last, so page order alone would starve it).
        _install_fake_pypdfium2(monkeypatch, [_native(1), _native(2), ""])
        # No tesseract in this sandbox: the loser image page would come back empty,
        # which is fine — the assertion is that the SINGLE budgeted page went to it.

        *_, method, _conf, _tables = pdf_report._parse_with_fitz("/data/prio.pdf")

        assert method[3] == "cohere_parse"
        assert method[1] == "fitz_native"
        assert method[2] == "fitz_native"
        assert not tesseract_tripwire.called

    def test_apply_ocr_fallback_false_switches_the_mode_off(self, monkeypatch) -> None:
        engine = _install_engine(monkeypatch, FakeEngine({1: _result(1)}))
        _install_fake_pypdfium2(monkeypatch, [_native(1)])

        *_, method, _conf, _tables = pdf_report._parse_with_fitz(
            "/data/nofallback.pdf", apply_ocr_fallback=False
        )

        assert engine.pages_requested == set()
        assert method == {1: "fitz_native"}


# ---------------------------------------------------------------------------
# tables — through the public entry point
# ---------------------------------------------------------------------------


def _pdf_file(tmp_path) -> str:
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.4\n" + b"%" + b"x" * 64 + b"\n%%EOF\n")
    return str(path)


def _stub_dispatch(monkeypatch, pages: list[str], *, pdfplumber_tables_on: dict[int, str] | None = None):
    """Stub everything in parse_pdf_report that opens the file, keep the rest."""
    per_page = [(n, t) for n, t in enumerate(pages, start=1)]

    def _fitz(path, apply_ocr_fallback=True, progress_file=None):
        return (
            "\n".join(pages), "Test Report", 0, [], ["en"] * len(pages), list(per_page), [],
            {n: "fitz_native" for n, _ in per_page}, {n: None for n, _ in per_page}, {},
        )

    monkeypatch.setattr(pdf_report, "_parse_with_fitz", _fitz)
    monkeypatch.setattr(pdf_report, "_extract_resource_tables", lambda *_a, **_k: [])
    sections = [
        pdf_report.ReportSection(
            section_number=None,
            section_title=f"Table (page {pg}, #1)",
            text=md,
            page_first=pg,
            page_last=pg,
        )
        for pg, md in (pdfplumber_tables_on or {}).items()
    ]
    monkeypatch.setattr(pdf_report, "_extract_all_tables_as_sections", lambda *_a, **_k: list(sections))


def _titles(result):
    return [s.section_title for s in result.sections]


class TestTablesMode:
    @pytest.fixture(autouse=True)
    def _mode(self, monkeypatch):
        monkeypatch.setenv("PDF_PARSE_MODE", "tables")

    def _pages(self):
        return [
            _native(1),  # prose only
            _native(2),  # pdfplumber finds a table here
            _native(3) + " Mineral Resource Estimate summary.",  # keyword only
            _native(4),  # prose only
        ]

    def test_detected_and_keyword_pages_are_sent_and_engine_tables_replace_pdfplumber(
        self, monkeypatch, tmp_path
    ) -> None:
        engine = _install_engine(
            monkeypatch,
            FakeEngine({2: _result(2, tables=[_GRID]), 3: _result(3, tables=[_GRID])}),
        )
        _stub_dispatch(monkeypatch, self._pages(), pdfplumber_tables_on={2: "| mangled | table |"})

        result = pdf_report.parse_pdf_report(_pdf_file(tmp_path))

        assert engine.pages_requested == {2, 3}  # prose pages 1 and 4 were not sent
        titles = _titles(result)
        assert "Table (page 2, #1)" not in titles  # replaced, not duplicated
        ocr_tables = [s for s in result.sections if s.section_title.startswith("Table (OCR,")]
        assert sorted(s.page_first for s in ocr_tables) == [2, 3]
        assert {s.ocr_method for s in ocr_tables} == {"cohere_parse"}
        assert "1,200,000" in ocr_tables[0].text
        # Prose is untouched: still the text layer, still labelled native.
        narrative = [s for s in result.sections if not s.section_title.startswith("Table")]
        assert narrative
        assert {s.ocr_method for s in narrative} == {"fitz_native"}
        assert result.is_scanned is False
        assert result.provenance["pdf_parse_mode"] == "tables"

        summary = _summary(result.warnings)
        assert summary["table_pages_sent"] == [2, 3]
        assert summary["engine_table_pages"] == [2, 3]
        assert summary["pdfplumber_fallback_pages"] == []

    def test_a_page_the_engine_cannot_answer_keeps_its_pdfplumber_table(
        self, monkeypatch, tmp_path, tesseract_tripwire
    ) -> None:
        _install_engine(monkeypatch, FakeEngine({3: _result(3, tables=[_GRID])}))  # 2 fails
        _stub_dispatch(monkeypatch, self._pages(), pdfplumber_tables_on={2: "| kept | table |"})

        result = pdf_report.parse_pdf_report(_pdf_file(tmp_path))

        titles = _titles(result)
        assert "Table (page 2, #1)" in titles
        assert "Table (OCR, page 2, #1)" not in titles
        assert "Table (OCR, page 3, #1)" in titles
        assert not tesseract_tripwire.called
        assert _summary(result.warnings)["pdfplumber_fallback_pages"] == [2]

    def test_engine_answering_without_a_table_keeps_the_pdfplumber_table(
        self, monkeypatch, tmp_path
    ) -> None:
        _install_engine(monkeypatch, FakeEngine({2: _result(2), 3: _result(3)}))
        _stub_dispatch(monkeypatch, self._pages(), pdfplumber_tables_on={2: "| kept | table |"})

        result = pdf_report.parse_pdf_report(_pdf_file(tmp_path))

        assert "Table (page 2, #1)" in _titles(result)
        assert not [t for t in _titles(result) if t.startswith("Table (OCR,")]
        assert _summary(result.warnings)["engine_no_table_pages"] == [2, 3]

    def test_budget_bounds_the_pages_sent_and_the_rest_keep_pdfplumber(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setenv("OCR_MAX_PAGES_PER_DOC", "1")
        engine = _install_engine(
            monkeypatch,
            FakeEngine({2: _result(2, tables=[_GRID]), 3: _result(3, tables=[_GRID])}),
        )
        _stub_dispatch(
            monkeypatch,
            self._pages(),
            pdfplumber_tables_on={2: "| a | b |", 3: "| c | d |"},
        )
        path = _pdf_file(tmp_path)

        result = pdf_report.parse_pdf_report(path)

        assert len(engine.pages_requested) == 1
        summary = _summary(result.warnings)
        assert len(summary["engine_table_pages"]) == 1
        assert len(summary["pdfplumber_fallback_pages"]) == 1
        # Exactly one of the two pages kept its pdfplumber table.
        kept = [t for t in _titles(result) if t.startswith("Table (page")]
        assert len(kept) == 1
        budget = [w for w in result.warnings if w.get("code") == "ocr_page_budget_exhausted"]
        assert budget and budget[0]["parse_mode"] == "tables"

    def test_a_scanned_page_is_not_resent_and_engine_pages_are_not_double_counted(
        self, monkeypatch, tmp_path
    ) -> None:
        engine = _install_engine(monkeypatch, FakeEngine({}))
        pages = [_native(1), _native(2)]
        _stub_dispatch(monkeypatch, pages, pdfplumber_tables_on={})

        result = pdf_report.parse_pdf_report(_pdf_file(tmp_path))

        # Nothing detected, no keyword: the engine is not asked at all.
        assert engine.pages_requested == set()
        assert _summary(result.warnings)["table_pages_sent"] == []


class TestDefaultModeThroughTheEntryPoint:
    def test_unset_means_no_engine_call_no_summary_no_provenance_key(
        self, monkeypatch, tmp_path
    ) -> None:
        engine = _install_engine(
            monkeypatch, FakeEngine({2: _result(2, tables=[_GRID]), 3: _result(3, tables=[_GRID])})
        )
        _stub_dispatch(
            monkeypatch,
            [_native(1), _native(2), _native(3) + " Mineral Resource Estimate."],
            pdfplumber_tables_on={2: "| kept | table |"},
        )

        result = pdf_report.parse_pdf_report(_pdf_file(tmp_path))

        assert engine.pages_requested == set()
        assert _summary(result.warnings) is None
        assert "pdf_parse_mode" not in result.provenance
        assert "Table (page 2, #1)" in _titles(result)

    def test_tables_on_tesseract_is_the_default_behaviour(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("OCR_ENGINE", "tesseract")
        monkeypatch.setenv("PDF_PARSE_MODE", "tables")
        engine = _install_engine(monkeypatch, FakeEngine({2: _result(2, tables=[_GRID])}))
        _stub_dispatch(monkeypatch, [_native(1), _native(2)], pdfplumber_tables_on={2: "| x | y |"})

        result = pdf_report.parse_pdf_report(_pdf_file(tmp_path))

        assert engine.pages_requested == set()
        assert _summary(result.warnings) is None
        assert "Table (page 2, #1)" in _titles(result)


class TestAllModeThroughTheEntryPoint:
    def test_a_born_digital_document_is_not_flagged_scanned(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setenv("PDF_PARSE_MODE", "all")
        _install_engine(
            monkeypatch,
            FakeEngine({1: _result(1), 2: _result(2, tables=[_GRID]), 3: _result(3)}),
        )
        _install_fake_pypdfium2(monkeypatch, [_native(1), _native(2), _native(3)])
        monkeypatch.setattr(pdf_report, "_extract_resource_tables", lambda *_a, **_k: [])
        monkeypatch.setattr(
            pdf_report,
            "_extract_all_tables_as_sections",
            lambda *_a, **_k: [
                pdf_report.ReportSection(
                    section_number=None,
                    section_title="Table (page 2, #1)",
                    text="| mangled |",
                    page_first=2,
                    page_last=2,
                )
            ],
        )

        result = pdf_report.parse_pdf_report(_pdf_file(tmp_path))

        assert result.is_scanned is False
        assert result.provenance["pdf_parse_mode"] == "all"
        assert "Table (page 2, #1)" not in _titles(result)  # engine's grid replaced it
        assert "Table (OCR, page 2, #1)" in _titles(result)
        assert {s.ocr_method for s in result.sections if not s.section_title.startswith("Table")} == {
            "cohere_parse"
        }
