"""``heartbeat_loop`` can be handed the run_id directly.

Every geology workflow and the ZIP archive already hold the run_id Laravel
minted; resolving it again from (workspace_id, minio_key) is a wasted query
and, for a key with more than one non-terminal row, can pick the wrong one.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from app.hatchet_workflows import _progress as ingest_progress


async def test_run_id_is_used_without_a_lookup(monkeypatch) -> None:
    lookup = AsyncMock(return_value="wrong")
    beat = AsyncMock()
    monkeypatch.setattr(ingest_progress, "lookup_active_run_id", lookup)
    monkeypatch.setattr(ingest_progress, "mark_heartbeat", beat)

    async with ingest_progress.heartbeat_loop(
        run_id="run-1",
        interval_seconds=0.01,
    ) as resolved:
        assert resolved == "run-1"
        await asyncio.sleep(0.05)

    lookup.assert_not_awaited()
    assert beat.await_count >= 1
    assert all(c.kwargs == {"run_id": "run-1"} for c in beat.await_args_list)


async def test_key_lookup_still_works_when_no_run_id_is_given(monkeypatch) -> None:
    lookup = AsyncMock(return_value="run-2")
    beat = AsyncMock()
    monkeypatch.setattr(ingest_progress, "lookup_active_run_id", lookup)
    monkeypatch.setattr(ingest_progress, "mark_heartbeat", beat)

    async with ingest_progress.heartbeat_loop(
        workspace_id="ws",
        minio_key="k",
        interval_seconds=0.01,
    ) as resolved:
        assert resolved == "run-2"
        await asyncio.sleep(0.05)

    lookup.assert_awaited_once_with(workspace_id="ws", minio_key="k")
    assert beat.await_count >= 1


async def test_no_row_means_no_ticker(monkeypatch) -> None:
    beat = AsyncMock()
    monkeypatch.setattr(ingest_progress, "mark_heartbeat", beat)

    async with ingest_progress.heartbeat_loop(
        run_id=None, interval_seconds=0.01
    ) as resolved:
        assert resolved is None
        await asyncio.sleep(0.03)

    beat.assert_not_awaited()
