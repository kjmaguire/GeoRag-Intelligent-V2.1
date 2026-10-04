"""A long parse keeps the run's heartbeat fresh, even through a lookup blip.

The stale sweep times a run out after 15 minutes without a heartbeat. A scanned
PDF parse runs for 1-4 hours in a subprocess pool (not ``asyncio.to_thread``),
so the only thing that keeps ``last_heartbeat_at`` moving is the asyncio ticker
that ``heartbeat_loop`` runs next to it. That ticker used to resolve the run_id
ONCE, at entry: a failed lookup (it returns None on a DB error) made it a
permanent silent no-op for the whole parse.
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock

from app.hatchet_workflows import _progress as ingest_progress
from app.hatchet_workflows import stale_run_detector as srd


async def test_a_lookup_that_fails_at_entry_is_retried_on_the_next_tick(monkeypatch) -> None:
    lookup = AsyncMock(side_effect=[None, "run-7", "run-7", "run-7", "run-7", "run-7"])
    beat = AsyncMock()
    monkeypatch.setattr(ingest_progress, "lookup_active_run_id", lookup)
    monkeypatch.setattr(ingest_progress, "mark_heartbeat", beat)

    async with ingest_progress.heartbeat_loop(
        workspace_id="ws", minio_key="reports/p/a.pdf", interval_seconds=0.01,
    ) as resolved:
        assert resolved is None  # unresolved at entry
        await asyncio.sleep(0.08)

    assert beat.await_count >= 1, "the ticker must pick the run up once the lookup recovers"
    assert all(c.kwargs == {"run_id": "run-7"} for c in beat.await_args_list)
    assert lookup.await_count == 2, "resolution stops once it has stuck"


async def test_a_lookup_that_never_resolves_does_not_crash_the_task(monkeypatch) -> None:
    monkeypatch.setattr(ingest_progress, "lookup_active_run_id", AsyncMock(return_value=None))
    beat = AsyncMock()
    monkeypatch.setattr(ingest_progress, "mark_heartbeat", beat)

    async with ingest_progress.heartbeat_loop(
        workspace_id="ws", minio_key="k", interval_seconds=0.01,
    ):
        await asyncio.sleep(0.04)

    beat.assert_not_awaited()


async def test_no_run_id_and_no_key_warns_instead_of_going_quiet(monkeypatch, caplog) -> None:
    beat = AsyncMock()
    monkeypatch.setattr(ingest_progress, "mark_heartbeat", beat)

    with caplog.at_level("WARNING", logger="georag.hatchet.progress"):
        async with ingest_progress.heartbeat_loop(interval_seconds=0.01):
            await asyncio.sleep(0.03)

    beat.assert_not_awaited()
    assert "will NOT heartbeat" in caplog.text


def test_the_default_interval_is_far_under_the_staleness_window() -> None:
    interval = inspect.signature(ingest_progress.heartbeat_loop.__wrapped__) \
        .parameters["interval_seconds"].default
    window_s = srd._stale_after_minutes() * 60
    assert interval * 10 <= window_s, (
        f"heartbeat every {interval}s against a {window_s}s window leaves too "
        "little margin for a slow tick"
    )


def test_the_pdf_parse_task_runs_its_body_inside_heartbeat_loop() -> None:
    from app.hatchet_workflows import ingest_pdf

    src = inspect.getsource(ingest_pdf)
    parse_src = src[src.index("async def parse(") : src.index("async def _parse_body(")]
    assert "ingest_progress.heartbeat_loop(" in parse_src
    assert "_parse_body(input, pre)" in parse_src
