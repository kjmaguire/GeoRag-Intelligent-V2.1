"""Page-image passages are best-effort: they must not hold an ingest run open.

Audit 2026-10-04 (F3b): ``_EMBEDDABLE_OCR_PREDICATE`` had no modality filter, so
ONE image passage whose embed failed (the Embed 5 image request shape is
unverified) kept the run at embed_verify/embedding until stale_run_detector
marked it ``timed_out`` -- although every text passage was embedded. Image rows
are now excluded from the "fully embedded?" predicates; the ones that never
embedded are reported as an INFO warning (``image_passages_unembedded``).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.hatchet_workflows import _progress
from app.hatchet_workflows import embed_pending_passages as embed_wf
from app.hatchet_workflows import ingest_pdf as pdf_wf
from app.hatchet_workflows import stale_run_detector as srd
from tests.test_pdf_run_outcome import _persist_input


class TestThePredicate:
    def test_all_three_copies_are_identical_and_exclude_images(self) -> None:
        copies = {
            pdf_wf._EMBEDDABLE_OCR_PREDICATE,
            embed_wf._EMBEDDABLE_OCR_PREDICATE,
            srd._EMBEDDABLE_OCR_PREDICATE,
        }
        assert len(copies) == 1, "the three copies are documented as 'in lockstep'"
        (predicate,) = copies
        assert "p.modality IS DISTINCT FROM 'image'" in predicate
        # the pre-existing ocr_status exclusion is still there
        assert "'rejected', 'pending_reocr'" in predicate

    def test_it_composes_with_AND_without_changing_the_callers_precedence(self) -> None:
        predicate = pdf_wf._EMBEDDABLE_OCR_PREDICATE
        assert predicate.startswith("(") and predicate.endswith(")")
        assert predicate.count("(") == predicate.count(")")

    def test_the_sweep_counts_unembedded_images_per_run(self) -> None:
        import inspect

        src = inspect.getsource(embed_wf)
        sweep = src[src.index("rows_to_complete"):src.index("return EmbedPendingPassagesOutput")]
        assert "image_unembedded" in sweep
        assert "modality = 'image'" in sweep
        assert (
            sweep.index("await _ingest_progress.append_run_warning")
            < sweep.index("await _ingest_progress.mark_completed_by_run")
        )


class TestTheInfoWarning:
    def test_it_is_info_and_does_not_turn_a_clean_run_amber(self) -> None:
        w = _progress.image_passages_unembedded_warning(3)
        assert w["code"] == "image_passages_unembedded"
        assert w["severity"] == "info" and w["count"] == 3
        assert _progress.terminal_status(rows_written=40, warnings=[w]) == "completed"

    @pytest.mark.asyncio
    async def test_append_is_idempotent_per_code_and_only_on_a_live_row(self) -> None:
        executed: list[tuple[Any, ...]] = []

        class _Conn:
            async def execute(self, sql: str, *args: Any) -> str:
                executed.append((sql, *args))
                return "UPDATE 1"

        @asynccontextmanager
        async def _acquire():
            yield _Conn()

        pool = SimpleNamespace(acquire=_acquire)
        with patch.object(_progress, "get_pool", AsyncMock(return_value=pool)):
            ok = await _progress.append_run_warning(
                run_id="r1", warning=_progress.image_passages_unembedded_warning(2),
            )
        assert ok is True
        sql, run_id, payload, code = executed[0]
        assert run_id == "r1" and code == "image_passages_unembedded"
        assert payload.startswith("[") and "image_passages_unembedded" in payload
        assert "NOT EXISTS" in sql and "w ->> 'code' = $3" in sql
        assert "status NOT IN" in sql, "never writes to a closed run"


class TestStaleRunDetectorCounts:
    @pytest.mark.asyncio
    async def test_counts_the_unembedded_images_of_the_run_document(self) -> None:
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value={"n": 4})

        @asynccontextmanager
        async def _scoped(*_a: Any, **_k: Any):
            yield conn

        with patch.object(srd, "scoped_connection", _scoped):
            assert await srd._unembedded_image_count(
                object(), "rep-1", workspace_id="ws-1") == 4
        sql = conn.fetchrow.await_args.args[0]
        assert "p.modality = 'image'" in sql and "p.embedding_id IS NULL" in sql

    @pytest.mark.asyncio
    async def test_no_report_or_a_failed_read_is_zero_never_an_error(self) -> None:
        assert await srd._unembedded_image_count(object(), None, workspace_id="w") == 0

        @asynccontextmanager
        async def _boom(*_a: Any, **_k: Any):
            raise RuntimeError("db down")
            yield  # pragma: no cover

        with patch.object(srd, "scoped_connection", _boom):
            assert await srd._unembedded_image_count(object(), "r", workspace_id="w") == 0


class _ImgConn:
    def __init__(self, images: int) -> None:
        self.images = images

    async def fetchrow(self, sql: str, *a: Any) -> dict:
        if "modality = 'image'" in sql:
            return {"n": self.images}
        return {"unembedded": 0}


def _pool(images: int):
    @asynccontextmanager
    async def _acquire():
        yield _ImgConn(images)

    return SimpleNamespace(acquire=_acquire, close=AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize(("images", "expect_warning"), [(2, True), (0, False)])
async def test_embed_verify_closes_with_the_info_warning_when_images_did_not_embed(
    images: int, expect_warning: bool,
) -> None:
    persisted = {"passages_written": 40, "run_warnings": [], "report_id": "rep-1"}

    def _task_output(task: Any) -> dict:
        return persisted if task is pdf_wf.persist else {"parser_used": "fitz"}

    ctx = MagicMock()
    ctx.task_output.side_effect = _task_output
    complete = AsyncMock(return_value=True)
    with (
        patch.object(pdf_wf.asyncpg, "create_pool", AsyncMock(return_value=_pool(images))),
        patch.object(pdf_wf.ingest_progress, "mark_started", AsyncMock()),
        patch.object(pdf_wf.ingest_progress, "lookup_active_run_id",
                     AsyncMock(return_value="run-1")),
        patch.object(pdf_wf.ingest_progress, "mark_completed_by_run", complete),
        patch.object(pdf_wf.ingest_progress, "get_run",
                     AsyncMock(return_value={"status": "completed"})),
        patch("app.services.laravel_bridge.post_ingestion_progress", AsyncMock()),
    ):
        out = await pdf_wf.embed_verify.fn(_persist_input(), ctx)

    assert out == {"ok": True, "unembedded_final": 0}
    warnings = complete.await_args.kwargs["warnings"]
    codes = [w["code"] for w in warnings]
    assert ("image_passages_unembedded" in codes) is expect_warning
    # info severity: the verdict is still 'completed'
    assert _progress.terminal_status(rows_written=40, warnings=warnings) == "completed"
