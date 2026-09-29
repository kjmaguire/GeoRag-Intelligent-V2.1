"""The Parse-vs-native comparison harness (scripts/ops/parse_vs_native.py).

No AWS, no database, no Cohere key: the metric helpers are pure, Parse is faked
at ``cohere_parse_client.ocr_page_sync`` (the function the harness calls, and
the one the pipeline's per-page path calls), and Postgres is a scripted fake.
The end-to-end tests run the real native side (pypdfium2 + pdfplumber) over
the small PDF fixture the Cohere probe also uses.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "ops"))

import parse_vs_native as pvn  # noqa: E402

from app.services.ingest import cohere_parse_client as cpc  # noqa: E402
from app.services.ingest.ocr_types import PageOcrResult  # noqa: E402

FIXTURE_PDF = Path(__file__).resolve().parent / "fixtures" / "ocr" / "PLS-2024-Technical-Report.pdf"
FAKE_KEY = "test-only-not-a-real-cohere-key"


# ---------------------------------------------------------------------------
# Text normalisation and similarity
# ---------------------------------------------------------------------------


class TestNormalisation:
    def test_html_table_becomes_words(self) -> None:
        html_table = '<table border="1"><tr><td colspan="2">Indicated</td><td>1,200,000</td></tr></table>'
        assert pvn.normalize_text(html_table) == "indicated 1,200,000"

    def test_markdown_table_furniture_is_dropped(self) -> None:
        md = "| Class | Tonnes |\n| --- | ---: |\n| Indicated | 1,200 |"
        assert pvn.normalize_text(md) == "class tonnes indicated 1,200"

    def test_headings_emphasis_links_and_images_lose_their_syntax(self) -> None:
        md = "## **Summary**\nSee [the map](http://x/y) ![fig](a.png) `Au`"
        assert pvn.normalize_text(md) == "summary see the map au"

    def test_nfkc_folds_ligatures_and_case(self) -> None:
        assert pvn.normalize_text("Signiﬁcant  RESULTS") == "significant results"

    def test_empty(self) -> None:
        assert pvn.normalize_text("") == ""
        assert pvn.normalize_text(None) == ""  # type: ignore[arg-type]


class TestSimilarity:
    def test_markup_alone_does_not_lower_the_score(self) -> None:
        native = "Indicated 1,200 2.45\nInferred 300 1.10"
        parse = "| Indicated | 1,200 | 2.45 |\n| --- | --- | --- |\n| Inferred | 300 | 1.10 |"
        ratio, truncated = pvn.char_similarity(native, parse)
        assert ratio == pytest.approx(1.0)
        assert truncated is False

    def test_a_misread_number_lowers_it(self) -> None:
        ratio, _ = pvn.char_similarity("grade 2.45 g/t over 12.0 m", "grade 2.46 g/t over 12.0 m")
        assert 0.9 < ratio < 1.0

    def test_empty_cases(self) -> None:
        assert pvn.char_similarity("", "") == (1.0, False)
        assert pvn.char_similarity("text", "") == (0.0, False)
        assert pvn.char_similarity("", "text") == (0.0, False)

    def test_very_long_pages_say_they_were_cut(self) -> None:
        long_text = "abc " * (pvn.MAX_SIMILARITY_CHARS // 2)
        ratio, truncated = pvn.char_similarity(long_text, long_text)
        assert truncated is True
        assert ratio == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------


class TestExtractNumbers:
    def test_the_shapes_a_43_101_carries(self) -> None:
        text = "Indicated 1,234.5 kt at 0.45 g/t Au (12%), depth 300 m, cut-off 0.30 %."
        assert pvn.extract_numbers(text) == ["1234.5", "0.45", "12%", "300", "0.30%"]

    def test_negatives_and_unicode_minus(self) -> None:
        assert pvn.extract_numbers("dip -60 and −45.5") == ["-60", "-45.5"]

    def test_digits_inside_identifiers_are_not_signed_numbers(self) -> None:
        # "DH-001": the hyphen belongs to the identifier, not to the number.
        # ("1-2 m" is a range: both ends are numbers.)
        assert pvn.extract_numbers("hole DH-001 and 1-2 m") == ["001", "1", "2"]

    def test_trailing_zeros_are_kept(self) -> None:
        assert pvn.extract_numbers("0.450 vs 0.45") == ["0.450", "0.45"]

    def test_sentence_punctuation_is_not_part_of_the_number(self) -> None:
        assert pvn.extract_numbers("Total 1,200. Next 2.5, then 3.") == ["1200", "2.5", "3"]

    def test_html_attributes_are_not_numbers(self) -> None:
        assert pvn.extract_numbers('<td colspan="2" width="40">7.5</td>') == ["7.5"]

    def test_significant_only_drops_page_furniture(self) -> None:
        text = "Page 7 of 300. Item 2. Grade 0.45, 12%, 1,200 t, year 2024."
        # 7 and 2 go; 300 (three digits), 2024 and every decimal / % / thousands value stay.
        assert pvn.extract_numbers(text, significant_only=True) == ["300", "0.45", "12%", "1200", "2024"]


class TestNumberRecall:
    def test_multiset_counts_repeats(self) -> None:
        # 2.45 appears three times in the reference; the candidate found it once.
        assert pvn.number_recall(["2.45", "2.45", "2.45", "1.1"], ["2.45", "1.1"]) == pytest.approx(2 / 4)

    def test_direction_matters(self) -> None:
        native, parse = ["1.0", "2.0"], ["1.0", "2.0", "9.9"]
        assert pvn.number_recall(native, parse) == pytest.approx(1.0)  # nothing native was lost
        assert pvn.number_recall(parse, native) == pytest.approx(2 / 3)  # Parse invented 9.9

    def test_no_reference_numbers_is_none_not_perfect(self) -> None:
        assert pvn.number_recall([], ["1.0"]) is None
        assert pvn.unique_number_recall([], ["1.0"]) is None

    def test_unique_recall_is_set_based(self) -> None:
        assert pvn.unique_number_recall(["2.45", "2.45", "1.1"], ["2.45"]) == pytest.approx(0.5)


class TestTables:
    def test_shape_and_numbers(self) -> None:
        grid = [["Class", "Tonnes", "Au g/t"], ["Indicated", "1,200,000", "2.45"], ["Inferred", "300,000", "1.10"]]
        assert pvn.table_shape(grid) == (3, 3)
        assert pvn.tables_numbers([grid], significant_only=True) == ["1200000", "2.45", "300000", "1.10"]

    def test_ragged_and_empty(self) -> None:
        assert pvn.table_shape([["a"], ["a", "b", "c"]]) == (2, 3)
        assert pvn.table_shape([]) == (0, 0)


# ---------------------------------------------------------------------------
# Sampling
# ---------------------------------------------------------------------------


class TestEvenlySpaced:
    def test_includes_the_ends(self) -> None:
        assert pvn.evenly_spaced(list(range(1, 11)), 3) == [1, 5, 10] or pvn.evenly_spaced(list(range(1, 11)), 3) == [1, 6, 10]

    def test_asking_for_more_than_exists_returns_all(self) -> None:
        assert pvn.evenly_spaced([4, 8], 5) == [4, 8]

    def test_zero_and_one(self) -> None:
        assert pvn.evenly_spaced([1, 2, 3], 0) == []
        assert pvn.evenly_spaced([1, 2, 3], 1) == [2]


class TestSelectPages:
    def test_respects_the_budget_and_the_half_for_tables(self) -> None:
        pages = list(range(1, 201))
        chosen = pvn.select_pages(pages, table_pages=range(1, 101), keyword_pages=range(101, 151), budget=30)
        assert len(chosen) == 30
        by = {}
        for page, reason in chosen:
            by.setdefault(reason, []).append(page)
        assert len(by["table"]) == 15  # half the budget
        assert len(by["keyword"]) == 7  # half of what is left
        assert len(by["prose"]) == 8
        assert [p for p, _ in chosen] == sorted(p for p, _ in chosen)
        assert len({p for p, _ in chosen}) == 30

    def test_no_tables_means_the_whole_budget_goes_to_prose(self) -> None:
        chosen = pvn.select_pages(list(range(1, 101)), [], [], 10)
        assert len(chosen) == 10
        assert {r for _p, r in chosen} == {"prose"}

    def test_a_small_document_is_taken_whole(self) -> None:
        chosen = pvn.select_pages([1, 2, 3], [2], [3], 30)
        assert chosen == [(1, "prose"), (2, "table"), (3, "keyword")]

    def test_a_table_page_without_native_text_is_not_sampled(self) -> None:
        # Page 9 has a table but no native text layer: nothing to compare against.
        chosen = pvn.select_pages([1, 2, 3], [9], [], 5)
        assert 9 not in {p for p, _ in chosen}

    def test_shortfall_in_one_category_is_topped_up(self) -> None:
        # Only 2 table pages but a big budget: the rest is not left empty.
        chosen = pvn.select_pages(list(range(1, 51)), [5, 6], [], 20)
        assert len(chosen) == 20
        assert {p for p, r in chosen if r == "table"} == {5, 6}

    def test_zero_budget(self) -> None:
        assert pvn.select_pages([1, 2], [1], [2], 0) == []


# ---------------------------------------------------------------------------
# Redaction and cost
# ---------------------------------------------------------------------------


class TestRedact:
    def test_the_key_itself_and_bearer_tokens(self) -> None:
        text = f"http_401: invalid key {FAKE_KEY}; sent Authorization: Bearer abcDEF1234567890xyz"
        out = pvn.redact(text, secrets=[FAKE_KEY])
        assert FAKE_KEY not in out
        assert "abcDEF1234567890xyz" not in out
        assert out.count("[REDACTED]") == 2

    def test_key_value_pairs(self) -> None:
        out = pvn.redact('{"api_key": "sk_live_abcdef123456", "model": "parse-v5.0"}')
        assert "sk_live_abcdef123456" not in out
        assert "parse-v5.0" in out

    def test_ordinary_errors_pass_through_verbatim(self) -> None:
        msg = "http_400: parameter 'document.image_url' is of type object but should be of type string"
        assert pvn.redact(msg, secrets=[FAKE_KEY]) == msg

    def test_a_short_secret_is_not_replaced_everywhere(self) -> None:
        # Replacing "ab" would mangle every word; refuse to redact trivially short strings.
        assert pvn.redact("about", secrets=["ab"]) == "about"


class TestCost:
    def test_price(self) -> None:
        assert pvn.estimated_cost_usd(1000) == pytest.approx(1.5)
        assert pvn.estimated_cost_usd(120) == pytest.approx(0.18)
        assert pvn.estimated_cost_usd(0) == 0


# ---------------------------------------------------------------------------
# One page, and the summary
# ---------------------------------------------------------------------------

_PL_TABLE = [["Class", "Tonnes", "Au g/t"], ["Indicated", "1,200,000", "2.45"], ["Inferred", "300,000", "1.10"]]
_NATIVE_TABLE_PAGE = "Mineral Resource Estimate\nIndicated 1,200,000 2.45 g/t\nInferred 300,000 1.10 g/t\nContained 94,500 oz"


class TestComparePage:
    def test_parse_captures_a_number_pdfplumber_missed(self) -> None:
        # pdfplumber dropped the Inferred row; Parse has all of it.
        pl = [[["Class", "Tonnes", "Au g/t"], ["Indicated", "1,200,000", "2.45"]]]
        parse = pvn.ParseOutcome(
            ok=True,
            text="Mineral Resource Estimate\n\n" + "| Indicated | 1,200,000 | 2.45 |\n| Inferred | 300,000 | 1.10 |\nContained 94,500 oz",
            tables=[_PL_TABLE],
            latency_s=1.5,
        )
        out = pvn.compare_page(12, "table", _NATIVE_TABLE_PAGE, pl, parse)

        m = out["metrics"]
        assert out["parse"]["ok"] is True
        assert out["parse"]["table_count"] == 1
        assert out["parse"]["shapes"] == ["3x3"]
        assert out["pdfplumber"]["shapes"] == ["2x3"]
        assert m["native_sig_in_parse"] == pytest.approx(1.0)
        assert m["parse_sig_in_native"] == pytest.approx(1.0)
        # The page has 5 significant numbers; pdfplumber's table holds 2 of them
        # (it dropped the Inferred row), Parse's holds 4 (all but "94,500 oz", which is prose).
        assert m["native_sig_numbers"] == 5
        assert m["sig_captured_by_pdfplumber"] == pytest.approx(2 / 5)
        assert m["sig_captured_by_parse"] == pytest.approx(4 / 5)
        assert m["similarity"] > 0.9

    def test_an_engine_error_is_an_error_not_a_score(self) -> None:
        out = pvn.compare_page(3, "prose", "some native text " * 10, [], pvn.ParseOutcome(ok=False, error="http_429: slow down"))
        assert out["parse"]["ok"] is False
        assert out["parse"]["error"] == "http_429: slow down"
        assert out["metrics"] == {}

    def test_dry_run_is_skipped_not_failed(self) -> None:
        out = pvn.compare_page(3, "prose", "text", [], None)
        assert out["parse"].get("skipped") is True
        assert pvn.summarize_pages([out])["parse_errors"] == 0
        assert pvn.summarize_pages([out])["parse_skipped"] == 1


class TestSummarize:
    def _page(self, reason, native, parse, *, ok=True, latency=1.0):
        outcome = pvn.ParseOutcome(ok=ok, text=parse, latency_s=latency, error=None if ok else "boom")
        return pvn.compare_page(1, reason, native, [], outcome)

    def test_failures_do_not_drag_the_quality_numbers(self) -> None:
        good = self._page("prose", "grade 2.45 over 12.0 m at 300 m", "grade 2.45 over 12.0 m at 300 m")
        failed = self._page("prose", "grade 9.99 over 1.0 m", "", ok=False)

        s = pvn.summarize_pages([good, failed])

        assert s["pages"] == 2
        assert s["parse_answered"] == 1
        assert s["parse_errors"] == 1
        assert s["similarity_mean"] == pytest.approx(1.0)
        assert s["native_sig_numbers_in_parse_micro"] == pytest.approx(1.0)

    def test_micro_average_weights_by_numbers(self) -> None:
        dense = self._page("prose", "1.1 2.2 3.3 4.4 5.5 6.6 7.7 8.8 9.9 10.1", "1.1 2.2 3.3 4.4 5.5 6.6 7.7 8.8 9.9 10.1")
        sparse = self._page("prose", "value 1.5", "value 9.5")  # lost its only number

        s = pvn.summarize_pages([dense, sparse])

        assert s["native_sig_numbers_in_parse_micro"] == pytest.approx(10 / 11)

    def test_latency_percentiles(self) -> None:
        pages = [self._page("prose", "text 1.5", "text 1.5", latency=v) for v in (1.0, 2.0, 3.0, 4.0, 10.0)]
        s = pvn.summarize_pages(pages)
        assert s["latency_p50_s"] == 3.0
        assert s["latency_p95_s"] == 10.0

    def test_empty_group(self) -> None:
        s = pvn.summarize_pages([])
        assert s["pages"] == 0
        assert s["similarity_mean"] is None


# ---------------------------------------------------------------------------
# Parse invocation (the pipeline's client path, faked at its seam)
# ---------------------------------------------------------------------------


class TestParsePages:
    def test_success_failure_and_exception_are_captured_and_redacted(self, monkeypatch) -> None:
        def fake(pdf_path, page):
            if page == 1:
                return PageOcrResult("text " * 30, 0.0, tables=[[["a", "1.5"]]], confidence_reported=False)
            if page == 2:
                return PageOcrResult(
                    "", 0.0, request_succeeded=False, error=f"http_401: {FAKE_KEY} rejected", confidence_reported=False
                )
            raise RuntimeError(f"connection reset (Bearer {FAKE_KEY}zzzz)")

        monkeypatch.setattr(cpc, "ocr_page_sync", fake)

        out = asyncio.run(pvn.parse_pages("/x.pdf", [1, 2, 3], secrets=[FAKE_KEY]))

        assert out[1].ok and out[1].tables == [[["a", "1.5"]]] and out[1].latency_s is not None
        assert not out[2].ok and FAKE_KEY not in (out[2].error or "") and "rejected" in (out[2].error or "")
        assert not out[3].ok and FAKE_KEY not in (out[3].error or "") and "RuntimeError" in (out[3].error or "")

    def test_concurrency_is_bounded(self, monkeypatch) -> None:
        live, peak, lock = 0, 0, threading.Lock()

        def fake(pdf_path, page):
            nonlocal live, peak
            with lock:
                live += 1
                peak = max(peak, live)
            threading.Event().wait(0.02)
            with lock:
                live -= 1
            return PageOcrResult("x" * 100, 0.0, confidence_reported=False)

        monkeypatch.setattr(cpc, "ocr_page_sync", fake)
        asyncio.run(pvn.parse_pages("/x.pdf", list(range(1, 13)), secrets=[], concurrency=3))
        assert 1 < peak <= 3


# ---------------------------------------------------------------------------
# CLI validation
# ---------------------------------------------------------------------------


class TestCli:
    def test_defaults(self) -> None:
        args = pvn.build_parser().parse_args([])
        assert (args.project_slug, args.max_reports, args.max_pages_per_report, args.dry_run) == (None, 3, 30, False)

    @pytest.mark.parametrize(
        "argv",
        [
            ["--project-slug", "Has-Upper"],
            ["--project-slug", "a b"],
            ["--project-slug", "x;rm -rf"],
            ["--project-slug", "a" * 65],
            ["--project-slug", "ok\n"],  # `$` alone would let a trailing newline through
            ["--project-slug", ""],
            ["--max-reports", "0"],
            ["--max-reports", str(pvn.MAX_REPORTS_LIMIT + 1)],
            ["--max-pages-per-report", "0"],
            ["--max-pages-per-report", str(pvn.MAX_PARSE_PAGES_PER_RUN + 1)],
            ["--workspace-id", "not-a-uuid"],
        ],
    )
    def test_bad_values_are_refused(self, argv, capsys) -> None:
        with pytest.raises(SystemExit) as exc:
            pvn.build_parser().parse_args(argv)
        assert exc.value.code == 2

    def test_good_values(self) -> None:
        args = pvn.build_parser().parse_args(
            ["--project-slug", "pls-2024", "--max-reports", "5", "--max-pages-per-report", "40", "--dry-run"]
        )
        assert args.project_slug == "pls-2024" and args.max_reports == 5 and args.max_pages_per_report == 40


# ---------------------------------------------------------------------------
# Selection: scripted Postgres, and the read-only session
# ---------------------------------------------------------------------------

_WS_A = "a0000000-0000-0000-0000-000000000001"
_WS_B = "b0000000-0000-0000-0000-000000000002"


class FakeConn:
    """Answers the harness's three queries; scope-aware like RLS."""

    def __init__(self, projects_by_scope: dict[str | None, dict | None], workspaces=(), reports=()) -> None:
        self.projects_by_scope = projects_by_scope
        self.workspaces = list(workspaces)
        self.reports = list(reports)
        self.scope: str | None = None
        self.executed: list[tuple] = []

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        if "set_config('app.workspace_id'" in sql:
            self.scope = (args[0] if args else "") or None

    async def fetchrow(self, sql, *args):
        return self.projects_by_scope.get(self.scope)

    async def fetch(self, sql, *args):
        if "silver.workspaces" in sql:
            return [{"workspace_id": w} for w in self.workspaces]
        return self.reports

    def is_in_transaction(self) -> bool:
        return False


def _project(ws, count=3, slug="pls"):
    return {"project_id": "p" * 8 + "-0000-0000-0000-000000000000", "slug": slug, "workspace_id": ws, "report_count": count, "total_pages": 250}


def _report_row(i, *, pages=100, scanned=False):
    return {
        "report_id": f"r{i:07d}-0000-0000-0000-000000000000",
        "title": f"Report {i}",
        "source_object_key": f"reports/{i}.pdf",
        "page_count": pages,
        "is_scanned": scanned,
        "workspace_id": _WS_A,
    }


class TestSelectReports:
    def test_unscoped_read_that_works_needs_no_workspace_walk(self) -> None:
        conn = FakeConn({None: _project(_WS_A)}, reports=[_report_row(1)])
        sel = asyncio.run(pvn.select_reports(conn, slug=None, workspace_id=None, max_reports=3))
        assert sel.project_slug == "pls" and len(sel.reports) == 1 and sel.project_total_pages == 250

    def test_rls_hiding_the_rows_falls_back_to_walking_workspaces_and_picks_the_biggest(self) -> None:
        conn = FakeConn(
            {None: None, _WS_A: _project(_WS_A, count=2, slug="small"), _WS_B: _project(_WS_B, count=9, slug="big")},
            workspaces=[_WS_A, _WS_B],
            reports=[_report_row(1)],
        )
        sel = asyncio.run(pvn.select_reports(conn, slug=None, workspace_id=None, max_reports=3))
        assert sel.project_slug == "big"
        assert conn.scope == _WS_B  # the report query ran scoped to the chosen tenant

    def test_born_digital_first_then_longest_and_capped(self) -> None:
        rows = [
            _report_row(1, pages=500, scanned=True),
            _report_row(2, pages=40),
            _report_row(3, pages=300),
            _report_row(4, pages=120),
        ]
        conn = FakeConn({None: _project(_WS_A)}, reports=rows)
        sel = asyncio.run(pvn.select_reports(conn, slug="pls", workspace_id=None, max_reports=3))
        assert [r.title for r in sel.reports] == ["Report 3", "Report 4", "Report 2"]

    def test_explicit_workspace_is_used_and_validated(self) -> None:
        conn = FakeConn({None: _project(_WS_A), _WS_B: _project(_WS_B, slug="mine")}, reports=[_report_row(1)])
        sel = asyncio.run(pvn.select_reports(conn, slug=None, workspace_id=_WS_B, max_reports=1))
        assert sel.project_slug == "mine"
        with pytest.raises(Exception, match="non-UUID"):
            asyncio.run(pvn.select_reports(conn, slug=None, workspace_id="1; DROP TABLE x", max_reports=1))

    def test_nothing_visible_says_so_and_suggests_the_fix(self) -> None:
        conn = FakeConn({None: None}, workspaces=[])
        with pytest.raises(pvn.SelectionError) as exc:
            asyncio.run(pvn.select_reports(conn, slug="nope", workspace_id=None, max_reports=3))
        assert "nope" in str(exc.value) and "--workspace-id" in str(exc.value) and "fresh deploy" in str(exc.value)


class TestReadOnlySession:
    def test_the_connection_is_made_read_only(self, monkeypatch) -> None:
        import asyncpg

        executed: list[str] = []

        class _Conn:
            async def execute(self, sql, *a):
                executed.append(sql)

        async def fake_connect(dsn, **kw):
            return _Conn()

        monkeypatch.setattr(asyncpg, "connect", fake_connect)
        asyncio.run(pvn.open_readonly_connection())
        assert "SET default_transaction_read_only = on" in executed

    def test_the_sql_the_harness_sends_never_writes(self) -> None:
        for sql in (pvn._PROJECT_SQL, pvn._REPORTS_SQL):
            head = sql.strip().split(None, 1)[0].upper()
            assert head == "SELECT"
            for verb in ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "DROP", "ALTER", "CREATE"):
                assert verb not in sql.upper().split()


# ---------------------------------------------------------------------------
# End to end on a real PDF (native side real, Parse faked)
# ---------------------------------------------------------------------------

pytestmark_fixture = pytest.mark.skipif(not FIXTURE_PDF.exists(), reason="fixture PDF not present")


def _args(*extra: str) -> argparse.Namespace:
    return pvn.build_parser().parse_args(["--local-pdf", str(FIXTURE_PDF), "--max-pages-per-report", "5", *extra])


@pytestmark_fixture
class TestEndToEnd:
    @staticmethod
    def _faithful_parse(monkeypatch, *, fail_page: int | None = None, leak_key: bool = False):
        import pypdfium2 as pdfium

        lock = threading.Lock()  # PDFium is not thread-safe; the real client renders under its own lock

        def fake(pdf_path, page):
            with lock:
                doc = pdfium.PdfDocument(pdf_path)
                text = doc[page - 1].get_textpage().get_text_bounded()
                doc.close()
            if page == fail_page:
                detail = f"http_400: rejected {FAKE_KEY}" if leak_key else "http_400: rejected"
                return PageOcrResult("", 0.0, request_succeeded=False, error=detail, confidence_reported=False)
            return PageOcrResult("## Page\n\n" + text, 0.0, confidence_reported=False)

        monkeypatch.setattr(cpc, "ocr_page_sync", fake)

    def _run(self, args, capsys):
        rc = asyncio.run(pvn.run(args))
        out = capsys.readouterr()
        return rc, out.out, out.err

    def _json(self, stdout: str) -> dict:
        body = stdout.split(pvn.BEGIN_JSON, 1)[1].split(pvn.END_JSON, 1)[0]
        return json.loads(body)

    def test_a_live_run_prints_both_blocks_and_the_json_parses(self, monkeypatch, capsys) -> None:
        monkeypatch.setenv("COHERE_API_KEY", FAKE_KEY)
        self._faithful_parse(monkeypatch)

        rc, out, err = self._run(_args(), capsys)

        assert rc == 0, err
        assert out.index(pvn.BEGIN_SUMMARY) < out.index(pvn.END_SUMMARY) < out.index(pvn.BEGIN_JSON) < out.index(pvn.END_JSON)
        result = self._json(out)
        assert result["meta"]["parse_pages_sent"] == 5
        assert result["meta"]["estimated_cost_usd"] == pytest.approx(0.0075)
        assert result["overall"]["parse_answered"] == 5
        assert result["overall"]["similarity_mean"] > 0.95
        assert result["overall"]["native_sig_numbers_in_parse_micro"] in (None, 1.0) or result["overall"][
            "native_sig_numbers_in_parse_micro"
        ] > 0.99
        # The JSON is metrics, not page text: no page body appears in it.
        assert "Patterson Lake South" not in json.dumps({k: v for k, v in result.items() if k != "samples"})
        assert FAKE_KEY not in out

    def test_a_dry_run_needs_no_key_and_sends_nothing(self, monkeypatch, capsys) -> None:
        monkeypatch.delenv("COHERE_API_KEY", raising=False)

        def boom(*_a, **_k):
            raise AssertionError("Parse must not be called in a dry run")

        monkeypatch.setattr(cpc, "ocr_page_sync", boom)

        rc, out, err = self._run(_args("--dry-run"), capsys)

        assert rc == 0, err
        result = self._json(out)
        assert result["meta"]["dry_run"] is True
        assert result["meta"]["parse_pages_sent"] == 0
        assert result["meta"]["parse_pages_planned"] > 0
        assert result["overall"]["parse_errors"] == 0

    def test_a_live_run_without_a_key_refuses(self, monkeypatch, capsys) -> None:
        monkeypatch.delenv("COHERE_API_KEY", raising=False)
        rc, out, err = self._run(_args(), capsys)
        assert rc == 2
        assert "COHERE_API_KEY" in err
        assert pvn.BEGIN_JSON not in out

    def test_a_partial_failure_is_reported_verbatim_with_the_key_redacted(self, monkeypatch, capsys) -> None:
        monkeypatch.setenv("COHERE_API_KEY", FAKE_KEY)
        self._faithful_parse(monkeypatch, fail_page=2, leak_key=True)

        rc, out, err = self._run(_args(), capsys)

        assert rc == 0
        assert "http_400: rejected [REDACTED]" in out
        assert FAKE_KEY not in out and FAKE_KEY not in err
        assert self._json(out)["overall"]["parse_errors"] == 1

    def test_every_request_failing_is_exit_3_after_the_report_prints(self, monkeypatch, capsys) -> None:
        monkeypatch.setenv("COHERE_API_KEY", FAKE_KEY)
        monkeypatch.setattr(
            cpc,
            "ocr_page_sync",
            lambda *_a, **_k: PageOcrResult("", 0.0, request_succeeded=False, error="http_401: unauthorized", confidence_reported=False),
        )

        rc, out, err = self._run(_args(), capsys)

        assert rc == 3
        assert pvn.END_JSON in out
        assert "http_401: unauthorized" in out
        assert "EVERY PARSE REQUEST FAILED" in err

    def test_run_wide_page_cap_holds(self, monkeypatch, capsys) -> None:
        monkeypatch.setenv("COHERE_API_KEY", FAKE_KEY)
        self._faithful_parse(monkeypatch)
        args = _args()  # built first: the flag's upper bound is read from the cap
        monkeypatch.setattr(pvn, "MAX_PARSE_PAGES_PER_RUN", 3)

        rc, out, _err = self._run(args, capsys)

        assert rc == 0
        assert self._json(out)["meta"]["parse_pages_sent"] == 3

    def test_nothing_samplable_is_exit_2(self, monkeypatch, capsys, tmp_path) -> None:
        monkeypatch.setenv("COHERE_API_KEY", FAKE_KEY)
        blank = tmp_path / "blank.pdf"
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument.new()
        doc.new_page(612, 792)
        doc.save(str(blank))
        args = pvn.build_parser().parse_args(["--local-pdf", str(blank)])

        rc, out, err = self._run(args, capsys)

        assert rc == 2
        assert "NOTHING COMPARED" in err


class TestScanNative:
    @pytestmark_fixture
    def test_reads_every_page_and_finds_keyword_pages(self) -> None:
        scan = pvn.scan_native(str(FIXTURE_PDF))
        assert scan.total_pages == len(scan.text) >= 2
        assert all(isinstance(t, str) for t in scan.text.values())
        # The fixture is a mock NI 43-101; at least one page mentions the resource estimate.
        assert scan.keyword_pages
