"""A stale ingest run is re-dispatched whichever stage it died in.

Red Star workspace, 2026-09-30: three scanned drill-log PDFs each had ONE
``ingest_pdf`` attempt, ended ``timed_out`` / ``stale_heartbeat`` and were
never re-dispatched. The sweep only retried rows whose ``current_step`` was
``preflight`` / ``parse`` / ``persist``; a worker lost while the run was still
at ``queued`` (step 0 of 5, nothing recorded yet) was read as "never
progressed, so will never progress" and closed with no child run.

Unit-level, no database: the pool, the Hatchet client and the dispatcher are
replaced. ``retry_block_reason`` is the single predicate behind the decision.
"""

from __future__ import annotations

import contextlib
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.hatchet_workflows import _progress as ingest_progress
from app.hatchet_workflows import stale_run_detector as srd

PDF_KEY = "reports/pj/20260930_120000_C_5_-_Diamond_Drill_Holes_21_-_43.pdf"


def _row(step: str | None, *, attempt: int = 1, key: str = PDF_KEY, run_id: str = "r1") -> dict:
    return {
        "run_id": run_id,
        "workspace_id": "ws",
        "project_id": "pj",
        "report_id": None,
        "workflow_run_id": None,
        "minio_key": key,
        "filename": "x.pdf",
        "current_stage": None,
        "current_step": step,
        "attempt_number": attempt,
        "triggered_by": "upload",
    }


# ---------------------------------------------------------------------------
# retry_block_reason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("step", ["queued", "preflight", "parse", "persist", None, "unknown", "some_future_stage"])
def test_every_non_embed_stage_is_retry_eligible(step) -> None:
    assert srd.retry_block_reason(_row(step), max_attempts=3) is None


@pytest.mark.parametrize("step", ["embed_verify", "embedding"])
def test_embed_stages_are_left_to_the_embed_sweep(step) -> None:
    reason = srd.retry_block_reason(_row(step), max_attempts=3)
    assert reason == f"embed_stage:{step}"


@pytest.mark.parametrize(("attempt", "blocked"), [(1, False), (2, False), (3, True), (4, True)])
def test_the_attempt_cap_is_respected(attempt, blocked) -> None:
    reason = srd.retry_block_reason(_row("parse", attempt=attempt), max_attempts=3)
    assert (reason is not None) is blocked
    if blocked:
        assert reason.startswith("attempts_exhausted")


def test_a_null_attempt_number_counts_as_the_first_attempt() -> None:
    row = _row("queued")
    row["attempt_number"] = None
    assert srd.retry_block_reason(row, max_attempts=3) is None


@pytest.mark.parametrize("field", ["minio_key", "workspace_id", "project_id"])
def test_a_row_missing_its_identity_is_not_retried(field) -> None:
    row = _row("parse")
    row[field] = None
    assert srd.retry_block_reason(row, max_attempts=3) == f"missing_{field}"


def test_an_unroutable_key_is_not_retried() -> None:
    reason = srd.retry_block_reason(_row("parse", key="mystery/pj/a.bin"), max_attempts=3)
    assert reason is not None and reason.startswith("no_recovery_workflow")


def test_the_recovery_cap_is_the_shared_one() -> None:
    assert srd._recovery_max_attempts() == ingest_progress.recovery_max_attempts() == 3


# ---------------------------------------------------------------------------
# detect(): the decision drives the dispatch
# ---------------------------------------------------------------------------


class _FakePool:
    @contextlib.asynccontextmanager
    async def acquire(self):
        yield MagicMock()


@pytest.fixture
def sweep(monkeypatch):
    rows: list[dict] = []
    timed_out = AsyncMock(return_value=True)
    dispatch = AsyncMock(return_value="child-run")

    async def _pool():
        return _FakePool()

    async def _per_workspace(conn, sql, *args, site, workspace_ids=None):
        if "count(*)" in sql:
            return [{"n": len(rows)}]
        return rows

    monkeypatch.setattr(ingest_progress, "get_pool", _pool)
    monkeypatch.setattr(ingest_progress, "mark_timed_out", timed_out)
    monkeypatch.setattr(srd, "fetch_per_workspace", _per_workspace)
    monkeypatch.setattr(srd, "post_ingestion_progress", AsyncMock())
    monkeypatch.setattr(srd, "_hatchet_run_status", AsyncMock(return_value="FAILED"))
    monkeypatch.setattr(srd, "_project_is_fully_embedded", AsyncMock(return_value=False))
    monkeypatch.setattr(srd, "_dispatch_recovery_run", dispatch)
    return SimpleNamespace(rows=rows, timed_out=timed_out, dispatch=dispatch)


def _detect():
    """The raw coroutine behind the task, whichever attribute the SDK uses."""
    for name in ("_fn", "fn"):
        candidate = getattr(srd.detect, name, None)
        if inspect.iscoroutinefunction(candidate):
            return candidate
    return srd.detect


@pytest.mark.parametrize("step", ["queued", "preflight", "parse", "persist", None])
async def test_a_stale_pdf_run_is_redispatched_from_any_pre_embed_stage(sweep, step) -> None:
    sweep.rows.append(_row(step))

    out = await _detect()(srd.StaleRunDetectorInput(), ctx=None)

    assert out.runs_marked_timed_out == 1
    assert out.recovery_runs_dispatched == 1
    stale_row = sweep.dispatch.await_args.kwargs["stale_row"]
    assert stale_row["run_id"] == "r1" and stale_row["minio_key"] == PDF_KEY


async def test_an_exhausted_run_is_timed_out_without_a_child(sweep, caplog) -> None:
    sweep.rows.append(_row("queued", attempt=3))

    with caplog.at_level("WARNING", logger="georag.hatchet.stale_run_detector"):
        out = await _detect()(srd.StaleRunDetectorInput(), ctx=None)

    assert out.runs_marked_timed_out == 1
    assert out.recovery_runs_dispatched == 0
    sweep.dispatch.assert_not_awaited()
    assert "attempts_exhausted:3/3" in caplog.text, "the declining rule must be named in the log"


async def test_an_embedding_run_is_timed_out_without_a_reingest(sweep) -> None:
    sweep.rows.append(_row("embedding"))

    out = await _detect()(srd.StaleRunDetectorInput(), ctx=None)

    assert out.runs_marked_timed_out == 1
    sweep.dispatch.assert_not_awaited()


async def test_a_lost_race_on_the_terminal_write_dispatches_nothing(sweep) -> None:
    sweep.timed_out.return_value = False
    sweep.rows.append(_row("queued"))

    out = await _detect()(srd.StaleRunDetectorInput(), ctx=None)

    assert out.runs_marked_timed_out == 0
    sweep.dispatch.assert_not_awaited()


# ---------------------------------------------------------------------------
# _dispatch_recovery_run: lineage for ingest_pdf
# ---------------------------------------------------------------------------


async def test_the_pdf_recovery_run_carries_its_lineage(monkeypatch) -> None:
    start_run = AsyncMock(return_value="child-1")
    stamp = AsyncMock()
    monkeypatch.setattr(ingest_progress, "start_run", start_run)
    monkeypatch.setattr(ingest_progress, "stamp_workflow_run_id", stamp)
    fake_workflow = SimpleNamespace(
        aio_run_no_wait=AsyncMock(return_value=SimpleNamespace(workflow_run_id="wr-9")),
    )
    monkeypatch.setattr(
        srd, "_build_recovery_payload",
        lambda **kw: (fake_workflow, SimpleNamespace(**kw)),
    )

    child = await srd._dispatch_recovery_run(stale_row=_row("queued"))

    assert child == "child-1"
    kwargs = start_run.await_args.kwargs
    assert kwargs["triggered_by"] == "stale_run_sweep"
    assert kwargs["parent_run_id"] == "r1"
    assert kwargs["recovery_reason"] == "stale_heartbeat"
    stamp.assert_awaited_once_with(run_id="child-1", workflow_run_id="wr-9")


def test_a_reports_key_routes_to_ingest_pdf() -> None:
    assert srd.recovery_workflow_for_key(PDF_KEY) == "ingest_pdf"
