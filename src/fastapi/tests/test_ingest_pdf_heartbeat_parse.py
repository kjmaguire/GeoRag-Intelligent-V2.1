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


# ---------------------------------------------------------------------------
# 2026-10-04 review follow-ups: heartbeat by the run's own id, loud ticker death
# ---------------------------------------------------------------------------

_WS = "a0000000-0000-0000-0000-000000000001"
_PROJECT = "b1000000-0000-0000-0000-000000000010"
_RUN = "c2000000-0000-0000-0000-000000000007"


def _pdf_input(**overrides):
    from app.hatchet_workflows.ingest_pdf import IngestPdfInput

    payload = {
        "workspace_id": _WS,
        "project_id": _PROJECT,
        "minio_key": f"reports/{_PROJECT}/scan.pdf",
        "file_size": 10,
        "correlation_token": "tok",
    }
    payload.update(overrides)
    return IngestPdfInput(**payload)


def test_ingest_pdf_input_carries_the_claimed_run_id() -> None:
    assert _pdf_input().run_id is None, "tiff_normalize dispatches without one"
    assert _pdf_input(run_id=_RUN).run_id == _RUN


def test_ingest_pdf_input_rejects_a_non_uuid_run_id() -> None:
    import pytest

    with pytest.raises(ValueError, match="run_id must be a UUID"):
        _pdf_input(run_id="not-a-uuid")


def test_tiff_normalize_input_mirrors_the_run_id_field() -> None:
    from app.hatchet_workflows.tiff_normalize import TiffNormalizeInput

    assert TiffNormalizeInput.model_fields["run_id"].default is None


def test_parse_and_persist_heartbeat_by_the_input_run_id() -> None:
    """Resolving per tick from (workspace, key) picks the NEWEST non-terminal
    row for the file - a sweep child or a re-upload - so a 1-4 h parse kept
    a sibling row alive while its own went stale. Both long stages must hand
    the id they were dispatched under straight to the ticker."""
    from app.hatchet_workflows import ingest_pdf

    src = inspect.getsource(ingest_pdf)
    for head, tail in (("async def parse(", "async def _parse_body("),
                       ("async def persist(", "async def _persist_body(")):
        stage_src = src[src.index(head) : src.index(tail)]
        hb = stage_src[stage_src.index("heartbeat_loop(") :]
        hb = hb[: hb.index("):")]
        assert "run_id=input.run_id" in hb, f"{head} does not heartbeat by run_id"
        assert "workspace_id=str(input.workspace_id)," in hb
        assert 'else ""' not in hb, "workspace_id is a required UUID; the ternary was dead"


def test_every_stage_marker_in_ingest_pdf_targets_the_input_run_id() -> None:
    from app.hatchet_workflows import ingest_pdf

    src = inspect.getsource(ingest_pdf)
    calls = src.split("ingest_progress.mark_started(")[1:]
    assert calls, "no mark_started calls found"
    for call in calls:
        # The call's own closing paren is the first line that is nothing
        # but ")"; an earlier ")" belongs to str(input.workspace_id).
        lines = call.splitlines()
        close = next(i for i, ln in enumerate(lines) if ln.strip() == ")")
        body = "\n".join(lines[:close])
        assert "run_id=input.run_id" in body, body


async def test_mark_started_with_a_run_id_upserts_that_row_and_skips_the_lookup(monkeypatch) -> None:
    lookup = AsyncMock(return_value="sibling")
    start = AsyncMock(return_value=_RUN)
    stage = AsyncMock()
    monkeypatch.setattr(ingest_progress, "lookup_active_run_id", lookup)
    monkeypatch.setattr(ingest_progress, "start_run", start)
    monkeypatch.setattr(ingest_progress, "mark_stage_started", stage)

    await ingest_progress.mark_started(
        workspace_id=_WS, project_id=_PROJECT, minio_key="k", step="parse", run_id=_RUN,
    )

    lookup.assert_not_awaited()
    assert start.await_args.kwargs["run_id"] == _RUN
    stage.assert_awaited_once_with(run_id=_RUN, stage="parse")


async def test_mark_started_without_a_run_id_keeps_the_legacy_lookup(monkeypatch) -> None:
    lookup = AsyncMock(return_value="found")
    start = AsyncMock()
    stage = AsyncMock()
    monkeypatch.setattr(ingest_progress, "lookup_active_run_id", lookup)
    monkeypatch.setattr(ingest_progress, "start_run", start)
    monkeypatch.setattr(ingest_progress, "mark_stage_started", stage)

    await ingest_progress.mark_started(
        workspace_id=_WS, project_id=_PROJECT, minio_key="k", step="parse",
    )

    lookup.assert_awaited_once()
    start.assert_not_awaited()
    stage.assert_awaited_once_with(run_id="found", stage="parse")


def test_the_sweep_dispatches_the_pdf_recovery_run_under_its_own_id() -> None:
    _, payload = srd._build_recovery_payload(
        workflow_name="ingest_pdf",
        stale_row={"workspace_id": _WS, "project_id": _PROJECT, "minio_key": "reports/x.pdf"},
        recovery_run_id=_RUN,
        correlation_token="sweep-1",
    )
    assert payload.run_id == _RUN


async def test_a_ticker_that_dies_says_so_in_the_log(monkeypatch, caplog) -> None:
    """The exit path suppresses the task's exception, so without a log line a
    ticker that died on tick one left a 4 h parse unheartbeated in silence."""
    beat = AsyncMock(side_effect=RuntimeError("pool closed"))
    monkeypatch.setattr(ingest_progress, "mark_heartbeat", beat)

    with caplog.at_level("ERROR", logger="georag.hatchet.progress"):
        async with ingest_progress.heartbeat_loop(
            run_id=_RUN, workspace_id=_WS, minio_key="k", interval_seconds=0.01,
        ):
            await asyncio.sleep(0.05)

    died = [r for r in caplog.records if "ticker died" in r.getMessage()]
    assert died, caplog.text
    assert died[0].run_id == _RUN
    assert died[0].workspace_id == _WS
    assert died[0].exc_info is not None, "the traceback is the diagnosis"


def test_the_no_identity_warning_names_what_it_needs(caplog) -> None:
    """The old text said 'no (workspace, key)' even when one of the two was
    present, which the now-visible extra fields would contradict."""
    import logging

    async def _run():
        async with ingest_progress.heartbeat_loop(workspace_id=_WS, interval_seconds=0.01):
            pass

    with caplog.at_level(logging.WARNING, logger="georag.hatchet.progress"):
        asyncio.run(_run())

    rec = [r for r in caplog.records if "will NOT heartbeat" in r.getMessage()]
    assert rec, caplog.text
    assert "BOTH workspace_id and minio_key" in rec[0].getMessage()
    assert rec[0].workspace_id == _WS
    assert rec[0].minio_key is None
    assert not hasattr(rec[0], "interval_seconds"), "config, not a correlation field"


def test_heartbeat_loop_is_typed() -> None:
    import typing

    hints = typing.get_type_hints(ingest_progress.heartbeat_loop.__wrapped__)
    assert "AsyncIterator" in str(hints["return"])
