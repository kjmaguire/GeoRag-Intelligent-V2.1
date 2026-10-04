"""PDF_PARSE_MODE=all quality guards and the Parse-table / chunking fixes.

Audit 2026-10-04:
  * `all` accepted ANY engine reading >= PER_PAGE_MIN_CHARS (80) and discarded
    the text layer with no length comparison, so a vector cross-section whose
    labels live in the text layer lost them when Parse returned only the title
    block. Now: the reading must be >= PARSE_MIN_NATIVE_RATIO of the native
    text, else the native text is kept (`page_parse_under_read`).
  * Parse tables were indexed twice: markdown in the page text AND a
    `Table (OCR, page N, #k)` section.
  * Window boundaries snapped only to a newline and could cut a markdown table.
  * PDF_PARSER_TESSERACT_FALLBACK_ENABLED=false also switched Parse off.
  * Tesseract was `lang="eng"` only although mixed-language documents are
    detected.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from app.services.ingest import cohere_parse_client as cpc
from app.services.ingest import ocr_engine, pdf_report

# `_env` and `tesseract_tripwire` are the parse-mode harness's autouse
# environment pin and its tesseract stand-in, reused as fixtures.
from tests.test_pdf_parse_mode import (  # noqa: F401
    _BASE_PROSE,
    _GRID,
    FakeEngine,
    _env,
    _install_engine,
    _install_fake_pypdfium2,
    _native,
    _pdf_file,
    _result,
    _summary,
    _titles,
    tesseract_tripwire,
)


@pytest.fixture(autouse=True)
def _all_mode(monkeypatch, _env):  # noqa: F811 -- ordered AFTER the harness's env pin
    monkeypatch.setenv("PDF_PARSE_MODE", "all")


_FIGURE_TITLE_BLOCK = (
    "Figure 3. Cross-section A-A' looking north through the Austin zone. "
    "Scale 1:2,000. Plate 4 of 9."
)
# What the text layer of a vector cross-section carries: the labels Parse
# drops with COHERE_PARSE_INCLUDE_IMAGE_DESCRIPTIONS=0.
_CROSS_SECTION_NATIVE = _FIGURE_TITLE_BLOCK + " " + " ".join(
    f"DDH-{n:03d} {n * 3}.2 m @ {n}.4 g/t Au; fault F{n}; 4{n}0 mRL" for n in range(1, 21)
)


class TestParseUnderRead:
    def test_the_threshold_is_a_named_constant(self) -> None:
        assert pdf_report.PARSE_MIN_NATIVE_RATIO == 0.6

    def test_a_reading_that_lost_the_labels_keeps_the_native_text(
        self, monkeypatch, tesseract_tripwire  # noqa: F811
    ) -> None:
        assert len(_FIGURE_TITLE_BLOCK) >= pdf_report.PER_PAGE_MIN_CHARS  # passes the old gate
        assert len(_FIGURE_TITLE_BLOCK) < 0.6 * len(_CROSS_SECTION_NATIVE)
        _install_engine(
            monkeypatch,
            FakeEngine({1: _result(1), 2: _result(2, text=_FIGURE_TITLE_BLOCK)}),
        )
        _install_fake_pypdfium2(monkeypatch, [_native(1), _CROSS_SECTION_NATIVE])

        out = pdf_report._parse_with_fitz("/data/xs.pdf")
        _text, _t, _skip, warnings, _langs, per_page, _img, method, conf, tables = out

        assert method == {1: "cohere_parse", 2: "fitz_native"}
        assert dict(per_page)[2] == _CROSS_SECTION_NATIVE  # labels survive
        assert conf[2] is None and tables == {}
        assert _summary(warnings)["native_fallback_pages"] == [2]
        (w,) = [x for x in warnings if x.get("code") == "page_parse_under_read"]
        assert w["page"] == 2
        assert w["native_chars"] == len(_CROSS_SECTION_NATIVE.strip())
        assert w["parse_chars"] == len(_FIGURE_TITLE_BLOCK)
        assert w["min_ratio"] == 0.6
        assert not tesseract_tripwire.called

    def test_a_faithful_reading_is_still_accepted(self, monkeypatch) -> None:
        _install_engine(monkeypatch, FakeEngine({1: _result(1)}))
        _install_fake_pypdfium2(monkeypatch, [_native(1)])

        *_, warnings, _langs, per_page, _img, method, _c, _t = pdf_report._parse_with_fitz(
            "/data/ok.pdf"
        )

        assert method == {1: "cohere_parse"}
        assert not [w for w in warnings if w.get("code") == "page_parse_under_read"]
        assert _summary(warnings)["native_fallback_pages"] == []

    @pytest.mark.parametrize(
        ("parse_chars", "accepted"), [(120, True), (119, False)],
    )
    def test_the_boundary_is_exactly_the_ratio(
        self, monkeypatch, parse_chars: int, accepted: bool
    ) -> None:
        native = (_BASE_PROSE * 2)[:200].strip()
        assert len(native) == 200
        engine_text = (_BASE_PROSE * 2)[: parse_chars - 1] + "."
        assert len(engine_text.strip()) == parse_chars
        _install_engine(monkeypatch, FakeEngine({1: _result(1, text=engine_text)}))
        _install_fake_pypdfium2(monkeypatch, [native])

        *_, per_page, _img, method, _c, _t = pdf_report._parse_with_fitz("/data/b.pdf")

        assert method == {1: "cohere_parse" if accepted else "fitz_native"}

    def test_table_cells_count_toward_the_engine_reading(self, monkeypatch) -> None:
        """Parse's markdown for a table becomes a placeholder, so the grid's
        cells must be counted or every table page reads as 'under-read'."""
        big_grid = [["Hole", "From", "To", "Au g/t"]] + [
            [f"DDH-{n:03d}", f"{n}.2", f"{n}.9", "1.31"] for n in range(30)
        ]
        native = _FIGURE_TITLE_BLOCK + " " + " | ".join(
            c for row in big_grid for c in row
        )
        _install_engine(
            monkeypatch,
            FakeEngine({1: _result(1, text=_FIGURE_TITLE_BLOCK, tables=[big_grid])}),
        )
        _install_fake_pypdfium2(monkeypatch, [native])

        *_, per_page, _img, method, _c, tables = pdf_report._parse_with_fitz("/data/t.pdf")

        assert method == {1: "cohere_parse"}
        assert tables == {1: [big_grid]}

    def test_the_summary_message_names_the_under_read_cause(self, monkeypatch) -> None:
        _install_engine(monkeypatch, FakeEngine({1: _result(1, text=_FIGURE_TITLE_BLOCK)}))
        _install_fake_pypdfium2(monkeypatch, [_CROSS_SECTION_NATIVE])

        *_, warnings, _langs, _pp, _img, _m, _c, _t = pdf_report._parse_with_fitz("/data/m.pdf")

        assert "60% of the text layer" in _summary(warnings)["message"]


# ---------------------------------------------------------------------------
# Parse tables are indexed once
# ---------------------------------------------------------------------------


def _rendered(grid) -> str:
    return cpc._table_markdown(grid)


class TestTablePlaceholders:
    def test_the_markdown_is_replaced_by_a_numbered_placeholder(self) -> None:
        text = f"Resource summary\n\n{_rendered(_GRID)}\n\nNotes follow."
        out = pdf_report._table_placeholders_for_grids(text, [_GRID], 5)
        assert "[Table 1, page 5]" in out
        assert "Indicated" not in out
        assert out.startswith("Resource summary") and out.endswith("Notes follow.")

    def test_two_tables_are_numbered_in_grid_order(self) -> None:
        other = [["A", "B"], ["1", "2"]]
        text = f"{_rendered(_GRID)}\n\nbetween\n\n{_rendered(other)}"
        out = pdf_report._table_placeholders_for_grids(text, [_GRID, other], 2)
        assert out.index("[Table 1, page 2]") < out.index("between") < out.index("[Table 2, page 2]")

    def test_identical_tables_are_replaced_one_each(self) -> None:
        text = f"{_rendered(_GRID)}\n\nx\n\n{_rendered(_GRID)}"
        out = pdf_report._table_placeholders_for_grids(text, [_GRID, _GRID], 9)
        assert "[Table 1, page 9]" in out and "[Table 2, page 9]" in out
        assert "Indicated" not in out

    def test_without_grids_the_text_is_untouched(self) -> None:
        text = f"keep\n\n{_rendered(_GRID)}"
        assert pdf_report._table_placeholders_for_grids(text, [], 1) == text
        assert pdf_report._table_placeholders_for_grids(text, None, 1) == text

    def test_a_table_the_text_does_not_contain_is_left_alone(self) -> None:
        text = "no table here"
        assert pdf_report._table_placeholders_for_grids(text, [_GRID], 1) == text

    def test_through_all_mode_the_table_is_indexed_once(self, monkeypatch, tmp_path) -> None:
        page_text = f"{_BASE_PROSE * 2}\n\n{_rendered(_GRID)}\n\nClosing remarks."
        _install_engine(
            monkeypatch,
            FakeEngine({1: _result(1, text=page_text, tables=[_GRID])}),
        )
        _install_fake_pypdfium2(monkeypatch, [page_text])
        monkeypatch.setattr(pdf_report, "_extract_resource_tables", lambda *_a, **_k: [])
        monkeypatch.setattr(pdf_report, "_extract_all_tables_as_sections", lambda *_a, **_k: [])

        result = pdf_report.parse_pdf_report(_pdf_file(tmp_path))

        table_sections = [s for s in result.sections if s.section_title.startswith("Table (OCR")]
        assert len(table_sections) == 1
        assert "Indicated" in table_sections[0].text and "1,200,000" in table_sections[0].text
        narrative = [s for s in result.sections if not s.section_title.startswith("Table")]
        joined = "\n".join(s.text for s in narrative)
        assert "[Table 1, page 1]" in joined  # reading order preserved
        assert "1,200,000" not in joined  # and the rows are not there twice
        assert "Closing remarks." in joined
        assert "Table (OCR, page 1, #1)" in _titles(result)

    def test_the_whole_document_scan_path_does_the_same(self, monkeypatch) -> None:
        page_text = f"Scanned report body.\n\n{_rendered(_GRID)}\n\nEnd."
        outcome = (page_text, 0.0, pdf_report._assess_ocr_result(
            page_text, None, detected_region_count=0, ocr_method="cohere_parse"), [_GRID])
        stub = types.ModuleType("pdf2image")
        stub.pdfinfo_from_path = lambda *_a, **_k: {"Pages": 1}
        monkeypatch.setitem(sys.modules, "pdf2image", stub)
        monkeypatch.setenv("OCR_PAGES_PER_BATCH", "1")
        with patch.object(pdf_report, "_ocr_single_page", return_value=outcome):
            result = pdf_report._attempt_ocr_cohere_parse("/scan.pdf")

        (page,) = result.pages
        assert "[Table 1, page 1]" in page.text
        assert "Indicated" not in page.text
        assert page.tables == (_GRID,)

    def test_a_caller_that_drops_the_grids_still_gets_the_rows(self) -> None:
        """The client keeps the markdown in `text`; only callers that also keep
        the grid swap it out (the pdfplumber-fallback OCR path asks for no
        tables)."""
        blocks = [{"type": "table", "table": {"html": (
            "<table><tr><th>Class</th><th>Tonnes</th></tr>"
            "<tr><td>Indicated</td><td>1,200,000</td></tr></table>")}}]
        res = cpc._page_from_blocks(blocks)
        assert "Indicated" in res.text and res.tables


# ---------------------------------------------------------------------------
# Windows never cut a markdown table when they can avoid it
# ---------------------------------------------------------------------------


def _table(rows: int) -> str:
    return pdf_report._table_to_markdown(
        [["Hole", "From", "To", "Au g/t"]]
        + [[f"DDH-{n:04d}", f"{n}.2", f"{n}.9", "1.31"] for n in range(rows)]
    )


def _windows(text: str):
    return pdf_report._emit_windows(text, 0, len(text), None, "Body", [(1, 0, len(text))])


class TestTablesAreNotSplitAcrossWindows:
    def _prose(self, chars: int) -> str:
        sentence = "The Austin zone dips steeply to the north and is cut by late faults. "
        return (sentence * (chars // len(sentence) + 1))[:chars].rstrip() + "\n"

    def test_a_table_that_would_straddle_the_boundary_moves_whole_to_the_next_window(self) -> None:
        small = _table(6)
        assert len(small) < pdf_report.WINDOW_CHARS // 4
        # Prose sized so the table starts in this window and ends in the next.
        lead = self._prose(pdf_report.WINDOW_CHARS - len(small) // 2)
        text = lead + small + "\n" + self._prose(pdf_report.WINDOW_CHARS)

        chunks = _windows(text)

        rows = small.splitlines()
        for row in rows:
            holders = [c for c in chunks if row in c.text.splitlines()]
            assert holders, f"row lost: {row!r}"
        # Some window carries the WHOLE table, header through last row.
        assert any(small in c.text for c in chunks), "the table was split across windows"
        # And the header row never sits in a window without its data rows.
        for c in chunks:
            lines = c.text.splitlines()
            if rows[0] in lines:
                assert rows[-1] in lines

    def test_without_the_fix_the_same_text_would_have_been_cut(self) -> None:
        """Pin the fixture: the old newline-only snap DID split this table."""
        small = _table(6)
        lead = self._prose(pdf_report.WINDOW_CHARS - len(small) // 2)
        text = lead + small + "\n" + self._prose(pdf_report.WINDOW_CHARS)
        end = pdf_report._snap_window_end(text, 0, pdf_report.WINDOW_CHARS)
        assert text[:end].count("|") and small not in text[:end]
        assert text[end:].lstrip().startswith("|")  # the break fell inside the table

    def test_a_table_larger_than_a_window_still_breaks_on_row_boundaries(self) -> None:
        big = _table(int(pdf_report.WINDOW_CHARS * 3 // 36))
        chunks = _windows(big)
        assert len(chunks) > 2
        for chunk in chunks:
            assert all(line.startswith("|") and line.endswith("|") for line in chunk.text.splitlines())
            assert len(chunk.text) <= pdf_report.WINDOW_CHARS

    def test_prose_only_text_is_unaffected(self) -> None:
        text = self._prose(pdf_report.WINDOW_CHARS * 3)
        assert pdf_report._avoid_table_split(text, 0, pdf_report.WINDOW_CHARS) == pdf_report.WINDOW_CHARS

    def test_a_boundary_between_a_table_and_following_prose_is_left_alone(self) -> None:
        small = _table(4)
        text = self._prose(200) + small + "\nAfter the table.\n" + self._prose(400)
        end = text.index("After the table.")
        assert pdf_report._avoid_table_split(text, 0, end) == end

    def test_the_window_stays_longer_than_the_overlap(self) -> None:
        """If the table starts too early to move, the cut stays where it was:
        the termination guard from _snap_window_end must survive."""
        start = 0
        text = _table(40)
        end = pdf_report._snap_window_end(text, start, pdf_report.WINDOW_CHARS)
        assert pdf_report._avoid_table_split(text, start, end) == end
        assert end - start > pdf_report.WINDOW_OVERLAP_CHARS

    def test_every_chunk_is_within_the_size_bound_and_nothing_is_dropped(self) -> None:
        small = _table(8)
        text = (self._prose(pdf_report.WINDOW_CHARS // 2) + small + "\n") * 6
        chunks = _windows(text)
        assert all(len(c.text) <= pdf_report.WINDOW_CHARS for c in chunks)
        emitted = {line for c in chunks for line in c.text.splitlines()}
        assert {ln for ln in text.splitlines() if ln.strip()} <= emitted


# ---------------------------------------------------------------------------
# PDF_PARSER_TESSERACT_FALLBACK_ENABLED is about Tesseract only
# ---------------------------------------------------------------------------


class TestTesseractFlagIsDecoupled:
    def test_with_the_flag_off_parse_still_reads_the_pages(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("PDF_PARSER_TESSERACT_FALLBACK_ENABLED", "false")
        engine = _install_engine(
            monkeypatch,
            FakeEngine({1: _result(1), 2: _result(2, tables=[_GRID])}),
        )
        _install_fake_pypdfium2(monkeypatch, [_native(1), _native(2)])
        monkeypatch.setattr(pdf_report, "_extract_resource_tables", lambda *_a, **_k: [])
        monkeypatch.setattr(pdf_report, "_extract_all_tables_as_sections", lambda *_a, **_k: [])

        result = pdf_report.parse_pdf_report(_pdf_file(tmp_path))

        assert engine.pages_requested == {1, 2}
        assert result.provenance["pdf_parse_mode"] == "all"
        assert "Table (OCR, page 2, #1)" in _titles(result)

    def test_with_the_flag_off_a_page_parse_cannot_read_never_reaches_tesseract(
        self, monkeypatch, tesseract_tripwire  # noqa: F811
    ) -> None:
        monkeypatch.setenv("PDF_PARSER_TESSERACT_FALLBACK_ENABLED", "false")
        _install_engine(monkeypatch, FakeEngine({1: _result(1), 2: None}))
        _install_fake_pypdfium2(monkeypatch, [_native(1), ""])  # page 2 is a scan

        *_, per_page, image_pages, method, _c, _t = pdf_report._parse_with_fitz(
            "/data/s.pdf", apply_ocr_fallback=True, allow_tesseract=False,
        )

        assert method == {1: "cohere_parse"}
        assert image_pages == [2]  # left for the caller, not re-read by Tesseract
        assert not tesseract_tripwire.called

    def test_with_the_flag_on_the_floor_is_still_there(self, monkeypatch) -> None:
        _install_engine(monkeypatch, FakeEngine({1: None}))
        _install_fake_pypdfium2(monkeypatch, [""])
        calls: list[dict] = []

        def fake_single(path, page, *a, **kw):
            calls.append(kw)
            return ("", 0.0, pdf_report._assess_ocr_result("", [], detected_region_count=0), [])

        with patch.object(pdf_report, "_ocr_single_page", side_effect=fake_single):
            pdf_report._parse_with_fitz("/data/f.pdf")

        assert calls and all("allow_tesseract_fallback" not in kw for kw in calls)

    def test_with_the_flag_off_and_no_engine_the_loop_is_off_as_before(
        self, monkeypatch, tmp_path
    ) -> None:
        monkeypatch.setenv("PDF_PARSER_TESSERACT_FALLBACK_ENABLED", "false")
        monkeypatch.delenv("COHERE_API_KEY")
        seen: list[bool] = []

        def fake_fitz(path, apply_ocr_fallback=True, progress_file=None, **kw):
            seen.append(apply_ocr_fallback)
            return ("t " * 200, "T", 0, [], ["en"], [(1, "t " * 200)], [], {1: "fitz_native"}, {1: None}, {})

        with patch.object(pdf_report, "_parse_with_fitz", side_effect=fake_fitz):
            pdf_report.parse_pdf_report(_pdf_file(tmp_path))

        assert seen == [False]


# ---------------------------------------------------------------------------
# Tesseract language hint
# ---------------------------------------------------------------------------


@pytest.fixture
def tess_langs(monkeypatch):
    def _set(*langs: str):
        pdf_report._installed_tesseract_langs.cache_clear()
        fake = types.ModuleType("pytesseract")
        fake.get_languages = MagicMock(return_value=list(langs))
        monkeypatch.setitem(sys.modules, "pytesseract", fake)
        monkeypatch.setattr(pdf_report, "_TESSERACT_LANGS_LOGGED", set())
        return fake

    yield _set
    pdf_report._installed_tesseract_langs.cache_clear()


class TestTesseractLanguage:
    def test_a_hint_with_installed_data_adds_it_ahead_of_english(self, tess_langs) -> None:
        tess_langs("eng", "fra", "osd")
        assert pdf_report._tesseract_lang("fr") == "fra+eng"

    def test_missing_data_falls_back_to_eng_and_says_so_once(self, tess_langs, caplog) -> None:
        tess_langs("eng", "osd")
        with caplog.at_level("WARNING", logger="app.services.ingest.pdf_report"):
            assert pdf_report._tesseract_lang("fr") == "eng"
            assert pdf_report._tesseract_lang("fr") == "eng"
        warned = [r for r in caplog.records if "fra" in r.getMessage()]
        assert len(warned) == 1

    @pytest.mark.parametrize("hint", [None, "en", "other", "unknown", "xx"])
    def test_no_usable_hint_is_eng(self, tess_langs, hint) -> None:
        tess_langs("eng", "fra")
        assert pdf_report._tesseract_lang(hint) == "eng"

    def test_the_language_list_is_asked_for_once_per_process(self, tess_langs) -> None:
        fake = tess_langs("eng", "fra", "deu")
        for hint in ("fr", "de", "fr", "es"):
            pdf_report._tesseract_lang(hint)
        assert fake.get_languages.call_count == 1

    def test_a_failing_listing_means_eng_only(self, monkeypatch) -> None:
        pdf_report._installed_tesseract_langs.cache_clear()
        fake = types.ModuleType("pytesseract")
        fake.get_languages = MagicMock(side_effect=RuntimeError("no binary"))
        monkeypatch.setitem(sys.modules, "pytesseract", fake)
        try:
            assert pdf_report._tesseract_lang("fr") == "eng"
        finally:
            pdf_report._installed_tesseract_langs.cache_clear()

    def test_the_dominant_non_english_language_is_chosen_when_material(self) -> None:
        mixed = ["en"] * 7 + ["fr"] * 3 + ["unknown"] * 10
        assert pdf_report._dominant_ocr_language(mixed) == "fr"

    def test_a_stray_misdetected_page_does_not_steer_the_document(self) -> None:
        assert pdf_report._dominant_ocr_language(["en"] * 30 + ["de"]) is None

    def test_an_english_or_undetected_document_gets_no_hint(self) -> None:
        assert pdf_report._dominant_ocr_language(["en"] * 10) is None
        assert pdf_report._dominant_ocr_language(["unknown", "other"]) is None
        assert pdf_report._dominant_ocr_language([]) is None

    def test_the_tesseract_call_receives_the_resolved_language(self, monkeypatch, tess_langs) -> None:
        fake = tess_langs("eng", "fra")
        fake.Output = types.SimpleNamespace(DICT="dict")
        fake.image_to_data = MagicMock(side_effect=RuntimeError("use the text path"))
        fake.image_to_string = MagicMock(return_value="Texte du rapport technique")
        pdf2image = types.ModuleType("pdf2image")
        pdf2image.convert_from_path = MagicMock(return_value=[object()])
        monkeypatch.setitem(sys.modules, "pdf2image", pdf2image)
        monkeypatch.setenv("OCR_ENGINE", "tesseract")

        with (
            patch.object(pdf_report, "_preprocess_image_for_ocr", side_effect=lambda im: im),
            patch.object(pdf_report, "_meter_ocr_page"),
        ):
            pdf_report._ocr_single_page("/x.pdf", 1, lang_hint="fr")
            pdf_report._ocr_single_page("/x.pdf", 1)

        langs = [c.kwargs["lang"] for c in fake.image_to_string.call_args_list]
        assert langs == ["fra+eng", "eng"]

    def test_the_fitz_loop_passes_the_document_language_only_when_there_is_one(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("OCR_ENGINE", "tesseract")
        monkeypatch.setenv("PDF_PARSE_MODE", "ocr_only")
        french = (
            "Le gisement aurifère de Madsen se trouve dans la ceinture de roches vertes "
            "du lac Rouge et contient des veines de quartz minéralisées dans les zones "
            "Austin et McVeigh, avec des teneurs proches de sept grammes par tonne. "
        ) * 2
        _install_fake_pypdfium2(monkeypatch, [french, french, french, ""])
        seen: list[dict] = []

        def fake_single(path, page, *a, **kw):
            seen.append(kw)
            return ("", 0.0, pdf_report._assess_ocr_result("", [], detected_region_count=0), [])

        with patch.object(pdf_report, "_ocr_single_page", side_effect=fake_single):
            pdf_report._parse_with_fitz("/data/fr.pdf")

        assert seen and all(kw.get("lang_hint") == "fr" for kw in seen)

        seen.clear()
        _install_fake_pypdfium2(monkeypatch, [_native(1), _native(2), ""])
        with patch.object(pdf_report, "_ocr_single_page", side_effect=fake_single):
            pdf_report._parse_with_fitz("/data/en.pdf")
        assert seen and all("lang_hint" not in kw for kw in seen)


# ---------------------------------------------------------------------------
# The upload ceiling
# ---------------------------------------------------------------------------


class TestUploadCeiling:
    def test_the_default_is_the_laravel_default(self, monkeypatch) -> None:
        from app.services.ingest import upload_limits

        monkeypatch.delenv("GEORAG_MAX_UPLOAD_BYTES", raising=False)
        assert upload_limits.max_upload_bytes() == 512 * 1024 * 1024

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("1073741824", 1073741824), ("unlimited", 512 * 1024**2), ("0", 512 * 1024**2),
         ("-5", 512 * 1024**2), ("", 512 * 1024**2), ("  268435456 ", 268435456)],
    )
    def test_it_reads_the_same_variable_with_the_same_rule_as_laravel(
        self, monkeypatch, raw, expected
    ) -> None:
        from app.services.ingest import upload_limits

        monkeypatch.setenv("GEORAG_MAX_UPLOAD_BYTES", raw)
        assert upload_limits.max_upload_bytes() == expected

    def test_human_sizes_for_messages(self) -> None:
        from app.services.ingest.upload_limits import human_bytes

        assert human_bytes(512 * 1024**2) == "512 MB"
        assert human_bytes(2 * 1024**3) == "2 GB"
        assert human_bytes(1536 * 1024**2) == "1.5 GB"

    def test_the_ingest_ceilings_are_the_upload_ceiling_not_2_gb(self) -> None:
        from app.hatchet_workflows import ingest_pdf
        from app.services.ingest import tiff_to_pdf, upload_limits

        assert upload_limits.max_upload_bytes() == ingest_pdf._MAX_PDF_BYTES
        assert upload_limits.max_upload_bytes() == tiff_to_pdf.MAX_TIFF_BYTES
        # No GEORAG_MAX_UPLOAD_BYTES in the test environment: the Laravel default.
        assert ingest_pdf._MAX_PDF_BYTES == 512 * 1024**2


def test_the_default_engine_and_mode_are_the_production_ones(monkeypatch) -> None:
    monkeypatch.delenv("OCR_ENGINE", raising=False)
    monkeypatch.delenv("PDF_PARSE_MODE", raising=False)
    assert ocr_engine.selected_engine() == "cohere_parse"
    assert ocr_engine.selected_parse_mode() == "all"
