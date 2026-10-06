"""A PDF run that delivered nothing, or delivered with complaints, ends `partial`.

Before 2026-10-04 ingest_pdf closed every run through paths that passed neither
``rows_written`` nor ``warnings`` (``embed_verify``'s legacy ``mark_completed``,
the embed completion sweep, stale_run_detector's race recovery), and
``_progress.terminal_status`` answers ``completed`` when both are absent. So the
Parse/OCR warnings never reached silver.ingest_progress, and a 400-page scan
whose OCR failed wrote a report with zero text passages while ``embed_verify``
broadcast "Ingestion complete; all chunks embedded."

These tests pin the new contract at each hop: what persist decides
(``build_run_warnings``), what it stores, what each closer does with it, and
what the broadcast says.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.hatchet_workflows import _progress
from app.hatchet_workflows import ingest_pdf as mod

WS = "a0000000-0000-0000-0000-000000000001"
PROJECT = "11111111-2222-3333-4444-555555555555"


# ---------------------------------------------------------------------------
# build_run_warnings — the decision
# ---------------------------------------------------------------------------


def _ocr_assessment(page: int, tier: str = "spot_check") -> dict:
    return {"code": "ocr_quality_assessment", "page": page, "tier": tier,
            "extracted_text": "x" * 100}


def _codes(warnings: list[dict]) -> list[str]:
    return [w["code"] for w in warnings]


def _blocking(warnings: list[dict]) -> list[dict]:
    return [w for w in warnings if w.get("severity") != "info"]


class TestBuildRunWarnings:
    def test_a_clean_parse_has_nothing_to_report(self) -> None:
        parsed = {"warnings": [], "text_page_coverage_pct": 1.0}
        assert mod.build_run_warnings(parsed, passages_written=40, page_count=40) == []

    def test_ocr_warnings_are_aggregated_not_listed_per_page(self) -> None:
        """400 `ocr_quality_assessment` entries (each with a text excerpt) must
        not become 400 lines on the Ingestion Runs page."""
        parsed = {
            "warnings": [
                *(_ocr_assessment(p, "spot_check") for p in range(1, 390)),
                *(_ocr_assessment(p, "mandatory_review") for p in range(390, 401)),
            ],
            "text_page_coverage_pct": 1.0,
        }
        out = mod.build_run_warnings(parsed, passages_written=400, page_count=400)
        assert _codes(out) == ["ocr_pages_need_review"]
        assert out[0]["count"] == 11
        assert out[0]["pages"] == list(range(390, 401))
        assert out[0]["detail"]  # the UI prints `detail`
        assert len(json.dumps(out)) < 2000

    def test_the_page_list_is_capped(self) -> None:
        parsed = {
            "warnings": [_ocr_assessment(p, "catastrophic_failure") for p in range(1, 301)],
            "text_page_coverage_pct": 1.0,
        }
        (w,) = mod.build_run_warnings(parsed, passages_written=300, page_count=300)
        assert w["count"] == 300
        assert len(w["pages"]) == 25

    def test_budget_exhaustion_is_carried_with_its_message(self) -> None:
        parsed = {
            "warnings": [{
                "code": "ocr_page_budget_exhausted", "cap": 300,
                "message": "Remote OCR was capped at 300 page(s).",
            }],
            "text_page_coverage_pct": 1.0,
        }
        (w,) = mod.build_run_warnings(parsed, passages_written=10, page_count=400)
        assert w["code"] == "ocr_page_budget_exhausted"
        assert "capped at 300" in w["detail"]
        assert w["cap"] == 300

    def test_zero_passages_for_a_document_with_pages_is_flagged(self) -> None:
        parsed = {
            "warnings": [{"code": "ocr_page_budget_exhausted", "cap": 300, "message": "m"}],
            "text_page_coverage_pct": 0.0,
        }
        out = mod.build_run_warnings(parsed, passages_written=0, page_count=400)
        assert "no_text_passages" in _codes(out)
        assert "low_text_page_coverage" not in _codes(out)  # one finding, not two

    def test_zero_passages_with_no_pages_is_not_flagged(self) -> None:
        """An empty file is the preflight's business, not this check's."""
        out = mod.build_run_warnings(
            {"warnings": [], "text_page_coverage_pct": 0.0}, passages_written=0, page_count=0,
        )
        assert out == []

    def test_coverage_below_the_threshold_is_flagged_with_it_named(self) -> None:
        assert mod.MIN_TEXT_PAGE_COVERAGE == 0.5
        parsed = {"warnings": [], "text_page_coverage_pct": 0.3}
        out = mod.build_run_warnings(parsed, passages_written=30, page_count=100)
        (w,) = out
        assert w["code"] == "low_text_page_coverage"
        assert "30%" in w["detail"] and "50%" in w["detail"]

    def test_coverage_at_the_threshold_is_not_flagged(self) -> None:
        parsed = {"warnings": [], "text_page_coverage_pct": 0.5}
        assert mod.build_run_warnings(parsed, passages_written=50, page_count=100) == []

    def test_a_missing_coverage_figure_is_not_treated_as_zero(self) -> None:
        assert mod.build_run_warnings({"warnings": []}, passages_written=5, page_count=10) == []

    def test_informational_entries_are_stored_but_do_not_count(self) -> None:
        parsed = {
            "warnings": [
                {"code": "page_ocr_recovered_fitz", "page": 3, "ocr_confidence": None},
                {"code": "page_ocr_recovered_fitz", "page": 9, "ocr_confidence": None},
                {"code": "mixed_language_document", "context": {"languages": ["en", "fr"]}},
                {"code": "pdf_parse_mode_summary", "severity": "info", "mode": "all",
                 "engine_text_pages": list(range(1, 39)), "native_fallback_pages": [39, 40]},
                {"code": "two_column_layout_detected", "page": 4},
                {"code": "page_ocr_recovered", "page": 5},
            ],
            "text_page_coverage_pct": 1.0,
        }
        out = mod.build_run_warnings(parsed, passages_written=40, page_count=40)
        assert set(_codes(out)) == {
            "page_ocr_recovered_fitz", "mixed_language_document", "pdf_parse_mode_summary",
        }
        assert all(w["severity"] == "info" for w in out)
        assert _progress.terminal_status(rows_written=40, warnings=out) == "completed"

    def test_a_failing_engine_across_many_pages_is_a_warning(self) -> None:
        """One flaky page is information; a quarter of the pages is an outage."""
        parsed = {
            "warnings": [{
                "code": "pdf_parse_mode_summary", "severity": "info", "mode": "all",
                "engine_text_pages": list(range(1, 31)),
                "native_fallback_pages": list(range(31, 41)),  # 10 of 40 = 25%
            }],
            "text_page_coverage_pct": 1.0,
        }
        (w,) = mod.build_run_warnings(parsed, passages_written=40, page_count=40)
        assert w["code"] == "parse_engine_degraded"
        assert w["severity"] == "warning"

    def test_parse_under_reads_are_aggregated(self) -> None:
        parsed = {
            "warnings": [
                {"code": "page_parse_under_read", "page": 7, "native_chars": 900, "parse_chars": 120},
                {"code": "page_parse_under_read", "page": 12, "native_chars": 700, "parse_chars": 90},
            ],
            "text_page_coverage_pct": 1.0,
        }
        (w,) = mod.build_run_warnings(parsed, passages_written=20, page_count=20)
        assert w["code"] == "page_parse_under_read"
        assert w["pages"] == [7, 12] and w["count"] == 2

    def test_page_image_warnings_from_staging_are_carried(self) -> None:
        parsed = {
            "warnings": [],
            "text_page_coverage_pct": 1.0,
            "page_image_warnings": [
                {"code": "page_images_capped", "detail": "dropped 5", "count": 5},
                {"code": "page_image_staging_failed", "detail": "2 failed", "pages": [3, 4]},
            ],
        }
        out = mod.build_run_warnings(parsed, passages_written=10, page_count=10)
        assert _codes(out) == ["page_images_capped", "page_image_staging_failed"]
        assert all(w["severity"] == "warning" for w in out)

    def test_an_unknown_non_info_code_is_still_reported_once(self) -> None:
        parsed = {
            "warnings": [
                {"code": "all_table_extraction_failed", "message": "boom"},
                {"code": "all_table_extraction_failed", "message": "boom again"},
            ],
            "text_page_coverage_pct": 1.0,
        }
        (w,) = mod.build_run_warnings(parsed, passages_written=10, page_count=10)
        assert w["code"] == "all_table_extraction_failed"
        assert w["count"] == 2


class TestTerminalStatusIgnoresInfo:
    def test_info_only_warnings_complete(self) -> None:
        w = [{"code": "x", "severity": "info", "detail": "fyi"}]
        assert _progress.terminal_status(rows_written=5, warnings=w) == "completed"

    def test_one_real_warning_among_info_makes_partial(self) -> None:
        w = [{"code": "x", "severity": "info"}, {"code": "y", "detail": "bad"}]
        assert _progress.terminal_status(rows_written=5, warnings=w) == "partial"

    def test_zero_rows_is_partial_even_with_only_info(self) -> None:
        w = [{"code": "x", "severity": "info"}]
        assert _progress.terminal_status(rows_written=0, warnings=w) == "partial"

    def test_other_workflows_warnings_without_severity_still_count(self) -> None:
        assert _progress.terminal_status(
            rows_written=3, warnings=[{"code": "no_matching_collar"}]
        ) == "partial"

    def test_the_message_names_the_first_blocking_warning_not_an_info_one(self) -> None:
        w = [
            {"code": "i", "severity": "info", "detail": "fyi"},
            {"code": "b", "detail": "the real problem"},
        ]
        msg = _progress.terminal_message(rows_written=0, warnings=w, noun="passage")
        assert "No passages written" in msg and "the real problem" in msg
        assert "fyi" not in msg


# ---------------------------------------------------------------------------
# mark_completed_by_run — reads the stored verdict when the caller has none
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, stored: dict | None) -> None:
        self.stored = stored
        self.update_args: tuple | None = None

    async def fetchrow(self, sql: str, *args: Any):
        if "SELECT rows_written, warnings" in sql:
            return self.stored
        if "UPDATE silver.ingest_progress" in sql and "status        = $4" in sql:
            self.update_args = args
            return {"run_id": args[0], "triggered_by": "upload", "duration_seconds": 1.0}
        raise AssertionError(f"unexpected SQL: {sql[:80]}")


def _pool_for(conn: _FakeConn):
    @asynccontextmanager
    async def _acquire():
        yield conn

    return SimpleNamespace(acquire=_acquire, is_closing=lambda: False)


async def _complete(stored: dict | None, **kwargs: Any) -> tuple[bool | None, _FakeConn]:
    conn = _FakeConn(stored)
    with patch.object(_progress, "get_pool", AsyncMock(return_value=_pool_for(conn))):
        result = await _progress.mark_completed_by_run(run_id="r1", **kwargs)
    return result, conn


@pytest.mark.asyncio
async def test_a_run_with_stored_warnings_ends_partial_with_them_persisted() -> None:
    stored = {
        "rows_written": 30,
        "warnings": json.dumps([{"code": "ocr_pages_need_review", "detail": "11 pages"}]),
    }
    result, conn = await _complete(stored)

    assert result is True
    assert conn.update_args is not None
    _run_id, _report, rows_written, status, warnings_json = conn.update_args
    assert status == "partial"
    assert rows_written == 30
    assert json.loads(warnings_json)[0]["code"] == "ocr_pages_need_review"


@pytest.mark.asyncio
async def test_a_zero_row_stored_run_ends_partial() -> None:
    result, conn = await _complete({"rows_written": 0, "warnings": "[]"})
    assert result is True
    assert conn.update_args[3] == "partial"


@pytest.mark.asyncio
async def test_a_clean_stored_run_still_completes() -> None:
    result, conn = await _complete({"rows_written": 12, "warnings": "[]"})
    assert result is True
    assert conn.update_args[3] == "completed"
    assert json.loads(conn.update_args[4]) == []


@pytest.mark.asyncio
async def test_a_row_with_no_stored_diagnostics_completes_as_before() -> None:
    """Recovery rows and rows that predate this change carry NULLs."""
    result, conn = await _complete({"rows_written": None, "warnings": None})
    assert result is True
    assert conn.update_args[3] == "completed"


@pytest.mark.asyncio
async def test_explicit_arguments_win_over_the_stored_verdict() -> None:
    conn = _FakeConn({"rows_written": 0, "warnings": "[]"})
    with patch.object(_progress, "get_pool", AsyncMock(return_value=_pool_for(conn))):
        await _progress.mark_completed_by_run(run_id="r1", rows_written=9, warnings=[])
    assert conn.update_args[3] == "completed"


@pytest.mark.asyncio
async def test_terminal_outcome_names_the_partial_and_keeps_the_clean_message() -> None:
    partial = {
        "status": "partial", "rows_written": 0,
        "warnings": json.dumps([{"code": "no_text_passages", "detail": "No text passages were written"}]),
    }
    with patch.object(_progress, "get_run", AsyncMock(return_value=partial)):
        status, message = await _progress.terminal_outcome(
            run_id="r1", default_message="Ingestion complete; all chunks embedded.",
        )
    assert status == "partial"
    assert "No passages written" in message and "No text passages were written" in message

    with patch.object(_progress, "get_run", AsyncMock(return_value={"status": "completed"})):
        status, message = await _progress.terminal_outcome(
            run_id="r1", default_message="Ingestion complete; all chunks embedded.",
        )
    assert (status, message) == ("completed", "Ingestion complete; all chunks embedded.")


# ---------------------------------------------------------------------------
# persist — stores the verdict
# ---------------------------------------------------------------------------


class _Txn:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def __aenter__(self):
        self._events.append("begin")

    async def __aexit__(self, *exc: object) -> None:
        self._events.append("commit" if exc[0] is None else "rollback")


class _PersistConn:
    def __init__(self, events: list[str], *, fail_image_insert: bool = False) -> None:
        self.events = events
        self.image_inserts = 0
        self.fail_image_insert = fail_image_insert

    def transaction(self) -> _Txn:
        return _Txn(self.events)

    async def execute(self, sql: str, *args: Any) -> str:
        if "'page_image'" in sql or "image_object_key" in sql and "INSERT INTO" in sql:
            self.image_inserts += 1
            if self.fail_image_insert:
                raise RuntimeError("insert failed")
        return "INSERT 0 1"

    async def fetch(self, *a: Any, **k: Any) -> list:
        return []


def _fake_pool(conn: _PersistConn):
    @asynccontextmanager
    async def _acquire():
        yield conn

    return SimpleNamespace(acquire=_acquire, close=AsyncMock())


def _persist_ctx(pre: dict, parsed: dict) -> MagicMock:
    ctx = MagicMock()
    ctx.workflow_run_id = "wf-1"

    def _task_output(task: Any) -> dict:
        return pre if task is mod.preflight else parsed

    ctx.task_output.side_effect = _task_output
    return ctx


def _persist_input() -> Any:
    return mod.IngestPdfInput(
        workspace_id=WS, project_id=PROJECT, minio_key="reports/x/scan.pdf",
        file_size=1024, correlation_token="tok",
    )


async def _run_persist(
    parsed: dict, *, page_count: int, store: MagicMock | None = None,
    fail_image_insert: bool = False, diagnostics_ok: bool = True,
) -> tuple[Any, AsyncMock, list[str]]:
    events: list[str] = []
    conn = _PersistConn(events, fail_image_insert=fail_image_insert)

    async def _diagnostics(**kwargs: Any) -> bool:
        # Recorded in the transaction's event log so a test can say WHERE the
        # write happened relative to the commit. The in-transaction call is
        # the one that carries ``conn``; the post-commit fallback has none.
        events.append("diagnostics_in_txn" if kwargs.get("conn") is not None else "diagnostics")
        return diagnostics_ok or kwargs.get("conn") is None

    diagnostics = AsyncMock(side_effect=_diagnostics)
    pre = {"sha256": "ab" * 32, "page_count": page_count, "file_size": 10,
           "encrypted": False, "valid": True}
    parsed = {"sha256": "ab" * 32, "parser_used": "ocr_cohere_parse", "title": "Scan",
              "authors": [], "sections": [], "warnings": [], "resource_tables": [],
              "figures": [], "parse_quality_pct": 0.0, "is_scanned": True, **parsed}
    patches = [
        patch.object(mod.asyncpg, "create_pool", AsyncMock(return_value=_fake_pool(conn))),
        patch.object(mod, "bind_workspace_scope", AsyncMock()),
        patch.object(mod, "emit_audit", AsyncMock()),
        patch.object(mod.ingest_progress, "mark_report_id", AsyncMock()),
        patch.object(mod.ingest_progress, "mark_run_diagnostics", diagnostics),
        patch.object(mod, "get_storage_client", MagicMock(return_value=store or MagicMock())),
    ]
    for p in patches:
        p.start()
    try:
        final = await mod._persist_body(_persist_input(), _persist_ctx(pre, parsed))
    finally:
        for p in reversed(patches):
            p.stop()
    return final, diagnostics, events


@pytest.mark.asyncio
async def test_a_zero_passage_persist_stores_a_partial_verdict() -> None:
    """A 400-page scan whose OCR failed: report row, no text passages."""
    final, diagnostics, _ = await _run_persist(
        {"text_page_coverage_pct": 0.0, "warnings": [
            {"code": "ocr_page_budget_exhausted", "cap": 300, "message": "capped"}]},
        page_count=400,
    )

    assert final.passages_written == 0
    assert "no_text_passages" in _codes(final.run_warnings)
    diagnostics.assert_awaited_once()
    kwargs = diagnostics.await_args.kwargs
    assert kwargs["rows_written"] == 0
    assert kwargs["workspace_id"] == WS and kwargs["minio_key"] == "reports/x/scan.pdf"
    assert _progress.terminal_status(
        rows_written=kwargs["rows_written"], warnings=kwargs["warnings"],
    ) == "partial"


@pytest.mark.asyncio
async def test_a_persist_with_ocr_warnings_stores_them_and_rows_written() -> None:
    sections = [{"section_number": None, "section_title": "Preamble",
                 "text": f"Drill hole DDH-{i} intersected 12.4 m at 3.2 g/t Au.",
                 "page_first": i, "page_last": i, "ocr_method": "cohere_parse"}
                for i in range(1, 6)]
    final, diagnostics, _ = await _run_persist(
        {"sections": sections, "text_page_coverage_pct": 1.0,
         "warnings": [_ocr_assessment(2, "mandatory_review")]},
        page_count=5,
    )

    assert final.passages_written == 5
    kwargs = diagnostics.await_args.kwargs
    assert kwargs["rows_written"] == 5
    assert _codes(kwargs["warnings"]) == ["ocr_pages_need_review"]
    assert _progress.terminal_status(
        rows_written=5, warnings=kwargs["warnings"]) == "partial"


@pytest.mark.asyncio
async def test_a_clean_persist_stores_a_completing_verdict() -> None:
    sections = [{"section_number": None, "section_title": "Preamble",
                 "text": f"Section {i} text, a sentence long enough to count.",
                 "page_first": i, "page_last": i, "ocr_method": "fitz_native"}
                for i in range(1, 4)]
    final, diagnostics, _ = await _run_persist(
        {"sections": sections, "text_page_coverage_pct": 1.0, "is_scanned": False,
         "parser_used": "fitz"},
        page_count=3,
    )

    assert final.run_warnings == []
    kwargs = diagnostics.await_args.kwargs
    assert kwargs["rows_written"] == 3 and kwargs["warnings"] == []
    assert _progress.terminal_status(rows_written=3, warnings=[]) == "completed"


# ---------------------------------------------------------------------------
# embed_verify — closes the run with the verdict, and says so
# ---------------------------------------------------------------------------


class _VerifyConn:
    async def fetchrow(self, *a: Any, **k: Any) -> dict:
        return {"unembedded": 0}


def _verify_pool():
    @asynccontextmanager
    async def _acquire():
        yield _VerifyConn()

    return SimpleNamespace(acquire=_acquire, close=AsyncMock())


@asynccontextmanager
async def _passthrough_scope(pool, workspace_id, site):
    """embed_verify's workspace bind, minus the real connection it needs."""
    async with pool.acquire() as conn:
        yield conn


async def _run_embed_verify(persisted: dict, *, row_after: dict, transitioned: bool = True):
    def _task_output(task: Any) -> dict:
        return persisted if task is mod.persist else {"parser_used": "ocr_cohere_parse"}

    ctx = MagicMock()
    ctx.task_output.side_effect = _task_output
    complete = AsyncMock(return_value=transitioned)
    post = AsyncMock()
    with (
        patch.object(mod.asyncpg, "create_pool", AsyncMock(return_value=_verify_pool())),
        patch.object(mod, "_scoped_acquire", _passthrough_scope),
        patch.object(mod.ingest_progress, "mark_started", AsyncMock()),
        patch.object(mod.ingest_progress, "lookup_active_run_id", AsyncMock(return_value="run-1")),
        patch.object(mod.ingest_progress, "mark_completed_by_run", complete),
        patch.object(mod.ingest_progress, "get_run", AsyncMock(return_value=row_after)),
        patch("app.services.laravel_bridge.post_ingestion_progress", post),
    ):
        out = await mod.embed_verify.fn(_persist_input(), ctx)
    return out, complete, post


@pytest.mark.asyncio
async def test_embed_verify_closes_a_failed_ocr_run_as_partial_and_says_so() -> None:
    warnings = [{"code": "no_text_passages", "severity": "warning",
                 "detail": "No text passages were written for this 400-page document"}]
    out, complete, post = await _run_embed_verify(
        {"passages_written": 0, "run_warnings": warnings},
        row_after={"status": "partial", "rows_written": 0, "warnings": json.dumps(warnings)},
    )

    assert out == {"ok": True, "unembedded_final": 0}
    complete.assert_awaited_once_with(run_id="run-1", rows_written=0, warnings=warnings)
    post.assert_awaited_once()
    sent = post.await_args.kwargs
    assert sent["status"] == "partial"
    assert "all chunks embedded" not in sent["message"]
    assert "No text passages were written" in sent["message"]


@pytest.mark.asyncio
async def test_embed_verify_still_announces_a_clean_run_as_completed() -> None:
    out, complete, post = await _run_embed_verify(
        {"passages_written": 40, "run_warnings": []},
        row_after={"status": "completed", "rows_written": 40, "warnings": "[]"},
    )

    complete.assert_awaited_once_with(run_id="run-1", rows_written=40, warnings=[])
    sent = post.await_args.kwargs
    assert sent["status"] == "completed"
    assert sent["message"] == "Ingestion complete; all chunks embedded."


@pytest.mark.asyncio
async def test_embed_verify_does_not_rebroadcast_a_run_a_sweep_already_closed() -> None:
    _, _, post = await _run_embed_verify(
        {"passages_written": 40, "run_warnings": []},
        row_after={"status": "completed"}, transitioned=False,
    )
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_embed_verify_falls_back_to_the_stored_verdict_when_persist_output_is_unreadable() -> None:
    ctx = MagicMock()

    def _task_output(task: Any) -> dict:
        if task is mod.persist:
            raise RuntimeError("no output")
        return {"parser_used": "fitz"}

    ctx.task_output.side_effect = _task_output
    complete = AsyncMock(return_value=True)
    with (
        patch.object(mod.asyncpg, "create_pool", AsyncMock(return_value=_verify_pool())),
        patch.object(mod, "_scoped_acquire", _passthrough_scope),
        patch.object(mod.ingest_progress, "mark_started", AsyncMock()),
        patch.object(mod.ingest_progress, "lookup_active_run_id", AsyncMock(return_value="run-1")),
        patch.object(mod.ingest_progress, "mark_completed_by_run", complete),
        patch.object(mod.ingest_progress, "get_run", AsyncMock(return_value={"status": "completed"})),
        patch("app.services.laravel_bridge.post_ingestion_progress", AsyncMock()),
    ):
        await mod.embed_verify.fn(_persist_input(), ctx)

    # Neither argument: mark_completed_by_run reads what persist stored on the row.
    complete.assert_awaited_once_with(run_id="run-1", rows_written=None, warnings=None)


# ---------------------------------------------------------------------------
# The two sweeps pass nothing and rely on the stored verdict
# ---------------------------------------------------------------------------


def test_the_sweeps_close_runs_through_the_stored_verdict_and_broadcast_the_real_status() -> None:
    import inspect

    from app.hatchet_workflows import embed_pending_passages as sweep
    from app.hatchet_workflows import stale_run_detector as stale

    for module in (sweep, stale):
        src = inspect.getsource(module)
        assert "terminal_outcome" in src, (
            f"{module.__name__} must broadcast the status the row earned, "
            "not a hard-coded 'completed'"
        )
        assert 'status="completed",\n                    message="Ingestion complete' not in src
