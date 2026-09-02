"""The stale sweep must not time out a run Hatchet still owns.

The ingest_progress row is inserted at DISPATCH time, so ``started_at`` is
when the run entered Hatchet's queue and a queued row carries no heartbeat.
On a bulk upload (2026-09-02: ~50 files in three minutes) runs wait in that
queue past the sweep's 15-minute window while their ``schedule_timeout``
(30 minutes to 2 hours) says the wait is legitimate. The sweep read every
one of them as dead — ``timed_out`` / ``stale_heartbeat`` at step 0 of 5,
never retried because ``queued`` is outside RETRY_STAGES — and when the
worker eventually ran the workflow, every terminal write no-op'd against
the closed row, leaving a successful ingest red on the Ingestion Runs page.

The detector now asks the engine for the run's status first and leaves
QUEUED/RUNNING runs alone. Unit-level, no database: the pool and the
Hatchet client are both replaced.

Also pins that ``STALE_RUN_DETECTOR_MINUTES`` is actually applied — the
input model's literal default of 15 used to shadow it on every cron firing.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.hatchet_workflows import _progress as ingest_progress
from app.hatchet_workflows import stale_run_detector as srd


class _Status:
    """Shape of hatchet_sdk's V1TaskStatus enum members."""

    def __init__(self, value: str) -> None:
        self.value = value


# ---------------------------------------------------------------------------
# _hatchet_run_status / _workflow_run_is_alive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "alive"),
    [
        ("QUEUED", True),
        ("RUNNING", True),
        ("COMPLETED", False),
        ("FAILED", False),
        ("CANCELLED", False),
    ],
)
async def test_alive_follows_the_engines_status(monkeypatch, status, alive) -> None:
    monkeypatch.setattr(srd, "_hatchet_run_status", AsyncMock(return_value=status))
    assert await srd._workflow_run_is_alive("wr-1") is alive


async def test_a_row_without_a_workflow_run_id_keeps_the_heartbeat_clock(
    monkeypatch,
) -> None:
    lookup = AsyncMock(return_value="RUNNING")
    monkeypatch.setattr(srd, "_hatchet_run_status", lookup)
    assert await srd._workflow_run_is_alive(None) is False
    assert await srd._workflow_run_is_alive("") is False
    lookup.assert_not_awaited()


async def test_an_unanswerable_status_means_not_alive(monkeypatch) -> None:
    """Unknown falls back to the heartbeat clock — the pre-existing behaviour."""
    monkeypatch.setattr(srd, "_hatchet_run_status", AsyncMock(return_value=None))
    assert await srd._workflow_run_is_alive("wr-1") is False


async def test_status_lookup_unwraps_the_enum_and_uppercases(monkeypatch) -> None:
    runs = SimpleNamespace(aio_get_status=AsyncMock(return_value=_Status("running")))
    monkeypatch.setattr(srd, "hatchet", SimpleNamespace(runs=runs))
    assert await srd._hatchet_run_status("wr-1") == "RUNNING"
    runs.aio_get_status.assert_awaited_once_with("wr-1")


async def test_status_lookup_swallows_api_errors(monkeypatch) -> None:
    runs = SimpleNamespace(
        aio_get_status=AsyncMock(side_effect=RuntimeError("engine down"))
    )
    monkeypatch.setattr(srd, "hatchet", SimpleNamespace(runs=runs))
    assert await srd._hatchet_run_status("wr-1") is None


# ---------------------------------------------------------------------------
# detect(): live rows are skipped, dead rows are still swept
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, rows: list[dict], captured: dict) -> None:
        self._rows = rows
        self._captured = captured

    async def fetch(self, sql: str):
        self._captured["select_sql"] = sql
        return self._rows

    async def fetchrow(self, sql: str, *args):
        return {"n": len(self._rows)}


class _FakePool:
    def __init__(self, rows: list[dict], captured: dict) -> None:
        self._conn = _FakeConn(rows, captured)

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield self._conn


def _row(run_id: str, workflow_run_id: str | None, step: str = "queued") -> dict:
    return {
        "run_id": run_id,
        "workspace_id": "ws",
        "project_id": "pj",
        "report_id": None,
        "workflow_run_id": workflow_run_id,
        "minio_key": f"archive/pj/{run_id}.zip",
        "filename": f"{run_id}.zip",
        "current_stage": None,
        "current_step": step,
        "attempt_number": 1,
        "triggered_by": "upload",
    }


@pytest.fixture
def sweep(monkeypatch):
    """Wire detect() to fakes; return the recorded side effects."""
    captured: dict = {}
    rows: list[dict] = []
    timed_out = AsyncMock(return_value=True)
    broadcast = AsyncMock()
    statuses: dict[str, str | None] = {}

    async def _status(workflow_run_id: str) -> str | None:
        return statuses.get(workflow_run_id)

    async def _pool():
        return _FakePool(rows, captured)

    monkeypatch.setattr(ingest_progress, "get_pool", _pool)
    monkeypatch.setattr(ingest_progress, "mark_timed_out", timed_out)
    monkeypatch.setattr(srd, "post_ingestion_progress", broadcast)
    monkeypatch.setattr(srd, "_hatchet_run_status", _status)
    return SimpleNamespace(
        rows=rows,
        statuses=statuses,
        timed_out=timed_out,
        broadcast=broadcast,
        captured=captured,
    )


def _detect():
    return getattr(srd.detect, "_fn", srd.detect)


async def test_live_queued_rows_are_skipped_and_dead_ones_swept(sweep) -> None:
    sweep.rows.extend(
        [
            _row("waiting", "wr-waiting"),
            _row("executing", "wr-executing", step="parse"),
            _row("lost", "wr-lost"),
            _row("legacy", None),
        ]
    )
    sweep.statuses.update(
        {
            "wr-waiting": "QUEUED",
            "wr-executing": "RUNNING",
            "wr-lost": "CANCELLED",
        }
    )

    out = await _detect()(srd.StaleRunDetectorInput(), ctx=None)

    assert out.runs_scanned == 4
    assert out.runs_skipped_alive == 2
    assert out.runs_marked_timed_out == 2
    swept = {call.kwargs["run_id"] for call in sweep.timed_out.await_args_list}
    assert swept == {"lost", "legacy"}, (
        "a CANCELLED run and a pre-workflow_run_id row are carcasses; a "
        "QUEUED or RUNNING one is not"
    )
    assert (
        out.recovery_runs_dispatched == 0
    ), "'queued' stays outside RETRY_STAGES — nothing to re-drive"


async def test_nothing_is_swept_when_every_candidate_is_alive(sweep) -> None:
    sweep.rows.append(_row("waiting", "wr-waiting"))
    sweep.statuses["wr-waiting"] = "QUEUED"

    out = await _detect()(srd.StaleRunDetectorInput(), ctx=None)

    assert out.runs_skipped_alive == 1
    assert out.runs_marked_timed_out == 0
    sweep.timed_out.assert_not_awaited()
    sweep.broadcast.assert_not_awaited()


async def test_stale_minutes_env_is_honoured_when_the_input_is_defaulted(
    sweep,
    monkeypatch,
) -> None:
    monkeypatch.setenv("STALE_RUN_DETECTOR_MINUTES", "7")

    await _detect()(srd.StaleRunDetectorInput(), ctx=None)

    assert "interval '7 minutes'" in sweep.captured["select_sql"], (
        "StaleRunDetectorInput.stale_minutes defaulted to a literal 15, so "
        "`input.stale_minutes or _stale_after_minutes()` never reached the "
        "env var on a cron firing"
    )


async def test_an_explicit_stale_minutes_still_wins(sweep, monkeypatch) -> None:
    monkeypatch.setenv("STALE_RUN_DETECTOR_MINUTES", "7")

    await _detect()(srd.StaleRunDetectorInput(stale_minutes=3), ctx=None)

    assert "interval '3 minutes'" in sweep.captured["select_sql"]


def test_input_default_is_unset_not_fifteen() -> None:
    assert srd.StaleRunDetectorInput().stale_minutes is None
