"""stale_run_detector: the recovery is dispatched before Laravel is asked
anything, and the sweep stops by itself instead of being killed.

A row marked ``timed_out`` is never selected again, so the gap between "mark
timed_out" and "dispatch the recovery" is the one place a kill loses a run for
good. The sweep pushed each row's ``timed_out`` to Laravel inside that gap (a
Laravel that is down costs ~11 s of retries per row) under a 2 minute
``execution_timeout``, so a backlog from the nightly stop was killed after about
ten rows, in that gap (Hatchet audit 2026-10, finding 11).

No database: every collaborator the tick calls is faked, and the order they are
called in is recorded. The DB-backed behaviour of each resolution is in
test_stale_run_detector.py.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.hatchet_workflows import stale_run_detector as srd

WORKSPACE = "a0000000-0000-0000-0000-000000000001"
PROJECT = "b1000000-0000-0000-0000-0000000000a0"


def _row(n: int) -> dict:
    return {
        "run_id": f"run-{n}",
        "workspace_id": WORKSPACE,
        "project_id": PROJECT,
        "report_id": None,
        "workflow_run_id": None,
        "minio_key": f"reports/{n}.pdf",
        "filename": f"{n}.pdf",
        "current_stage": "parse",
        "current_step": "parse",
        "attempt_number": 1,
        "triggered_by": "upload",
        "dispatch_params": None,
    }


class _Pool:
    def acquire(self) -> _Pool:
        return self

    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _Tick:
    """One detector tick over scripted stale rows; ``events`` is the order the
    collaborators were called in, as (kind, run_id)."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.events: list[tuple[str, str]] = []

    def kinds(self) -> list[str]:
        return [kind for kind, _run in self.events]

    def count(self, kind: str) -> int:
        return self.kinds().count(kind)

    async def run(self) -> srd.StaleRunDetectorOutput:
        detect = getattr(srd.detect, "_fn", srd.detect)
        return await detect(srd.StaleRunDetectorInput(), MagicMock())


@pytest.fixture
def tick(monkeypatch: pytest.MonkeyPatch):
    def _install(
        n_rows: int, *, broadcast_delay: float = 0.0, alive_delay: float = 0.0,
    ) -> _Tick:
        rows = [_row(i) for i in range(n_rows)]
        t = _Tick(rows)

        async def _get_pool() -> _Pool:
            return _Pool()

        async def _fetch(_conn: object, sql: str, *_a: object, **_k: object) -> list[dict]:
            return [{"n": len(rows)}] if "count(*)" in sql else list(rows)

        async def _alive(_workflow_run_id: str | None) -> bool:
            if alive_delay:
                await asyncio.sleep(alive_delay)
            return False

        async def _mark(*, run_id: str, reason: str) -> bool:
            t.events.append(("mark", run_id))
            return True

        async def _dispatch(*, stale_row: dict) -> str:
            t.events.append(("dispatch", stale_row["run_id"]))
            return f"child-of-{stale_row['run_id']}"

        async def _post(**kwargs: object) -> None:
            if broadcast_delay:
                await asyncio.sleep(broadcast_delay)
            t.events.append(("broadcast", str(kwargs["run_id"])))

        monkeypatch.setattr(srd.ingest_progress, "get_pool", _get_pool)
        monkeypatch.setattr(srd, "fetch_per_workspace", _fetch)
        monkeypatch.setattr(srd, "_workflow_run_is_alive", _alive)
        monkeypatch.setattr(srd.ingest_progress, "mark_timed_out", _mark)
        monkeypatch.setattr(srd, "_dispatch_recovery_run", _dispatch)
        monkeypatch.setattr(srd, "post_ingestion_progress", _post)
        return t

    return _install


# ---------------------------------------------------------------------------
# Order: the recovery first, Laravel last
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_every_recovery_is_dispatched_before_any_laravel_push(tick) -> None:
    t = tick(3)

    out = await t.run()

    assert t.kinds() == ["mark", "dispatch"] * 3 + ["broadcast"] * 3
    assert out.runs_marked_timed_out == 3
    assert out.recovery_runs_dispatched == 3
    assert out.broadcasts_emitted == 3 and out.broadcasts_dropped == 0


@pytest.mark.asyncio
async def test_a_laravel_that_hangs_cannot_cost_a_recovery(tick) -> None:
    """Hatchet's execution_timeout cancels the task wherever it is. With Laravel
    unreachable that used to be inside the first row's push, after its
    ``timed_out`` mark and before its dispatch: the run was lost and every row
    behind it was not reached. Now every row is marked and dispatched before the
    first push is attempted."""
    t = tick(5, broadcast_delay=30.0)

    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(t.run(), timeout=1.0)  # stands in for the kill

    assert t.count("mark") == 5
    assert t.count("dispatch") == 5


# ---------------------------------------------------------------------------
# The time-box
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_sweep_stops_starting_rows_when_its_budget_is_spent(
    tick, monkeypatch,
) -> None:
    monkeypatch.setattr(srd, "_loop_budget_seconds", lambda: 0.25)
    t = tick(10, alive_delay=0.1)

    out = await t.run()

    assert out.runs_scanned == 10
    assert 0 < out.runs_deferred < 10, "the budget should have cut the sweep short"
    # Every row it started it finished: no row is marked without its dispatch.
    assert out.runs_marked_timed_out + out.runs_deferred == 10
    assert t.count("mark") == t.count("dispatch") == out.runs_marked_timed_out


@pytest.mark.asyncio
async def test_a_sweep_inside_its_budget_defers_nothing(tick) -> None:
    out = await tick(4).run()

    assert out.runs_deferred == 0 and out.runs_marked_timed_out == 4


@pytest.mark.asyncio
async def test_held_pushes_that_do_not_fit_are_dropped_not_sent_late(
    tick, monkeypatch,
) -> None:
    monkeypatch.setattr(srd, "_BROADCAST_GRACE_SECONDS", -10_000.0)  # no time left
    t = tick(3)

    out = await t.run()

    assert t.count("dispatch") == 3, "the work that matters is done regardless"
    assert t.count("broadcast") == 0
    assert out.broadcasts_emitted == 0 and out.broadcasts_dropped == 3


@pytest.mark.asyncio
async def test_a_hung_engine_status_reads_as_unknown(monkeypatch) -> None:
    async def _hang(_workflow_run_id: str) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(
        srd, "hatchet", SimpleNamespace(runs=SimpleNamespace(aio_get_status=_hang)),
    )
    monkeypatch.setattr(srd, "_ENGINE_STATUS_TIMEOUT_SECONDS", 0.05)

    assert await asyncio.wait_for(srd._hatchet_run_status("wf-run"), timeout=5.0) is None


# ---------------------------------------------------------------------------
# The numbers agree with each other
# ---------------------------------------------------------------------------
def test_the_loop_and_push_budgets_fit_inside_the_task_timeout() -> None:
    text = (Path(srd.__file__)).read_text(encoding="utf-8")
    match = re.search(
        r'@stale_run_detector\.task\([^)]*execution_timeout="(\d+)m"', text,
    )
    assert match, "execution_timeout of detect() not found"
    timeout_s = int(match.group(1)) * 60
    # Loop + pushes + 30 s for the row in hand and the output.
    assert srd._loop_budget_seconds() + srd._BROADCAST_GRACE_SECONDS + 30 < timeout_s


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("30", 30.0), ("0.5", 0.5), ("junk", 420.0), ("0", 420.0), ("-5", 420.0)],
)
def test_the_loop_budget_can_be_set_and_falls_back_on_junk(
    monkeypatch, raw: str, expected: float,
) -> None:
    monkeypatch.setenv("STALE_RUN_DETECTOR_BUDGET_SECONDS", raw)
    assert srd._loop_budget_seconds() == expected


def test_the_default_loop_budget_is_seven_minutes(monkeypatch) -> None:
    monkeypatch.delenv("STALE_RUN_DETECTOR_BUDGET_SECONDS", raising=False)
    assert srd._loop_budget_seconds() == 420.0
