"""Audit 2026-10 finding 19: a failed emit does not suppress the next hour of alerts.

``emit_cross_workspace_alert`` claims its one-hour dedupe window with
``SET NX EX`` BEFORE it writes the audit row. When the write failed the claim
stayed, so the same actor/target pair was de-duplicated for an hour against a row
that was never written: the cross-workspace alert vanished with nothing recorded.

The existing tests of this module need Postgres and Redis; these use fakes.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from app.services import cross_workspace_audit as cwa

JWT_WORKSPACE = UUID("a0000000-0000-0000-0000-000000000001")
TARGET = UUID("b0000000-0000-0000-0000-000000000002")


class _FakeRedis:
    """SET NX EX + DEL over a dict; the window itself is not simulated."""

    def __init__(self, *, fail_delete: bool = False, fail_set: bool = False) -> None:
        self.keys: dict[str, str] = {}
        self.fail_delete = fail_delete
        self.fail_set = fail_set
        self.deleted: list[str] = []

    async def set(self, key: str, value: str, *, ex: int, nx: bool) -> str | None:
        if self.fail_set:
            raise ConnectionError("redis is down")
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return "OK"

    async def delete(self, key: str) -> int:
        if self.fail_delete:
            raise ConnectionError("redis is down")
        self.deleted.append(key)
        return 1 if self.keys.pop(key, None) is not None else 0


async def _emit(redis: _FakeRedis | None) -> bool:
    return await cwa.emit_cross_workspace_alert(
        object(),  # type: ignore[arg-type]
        actor_user_id=7,
        jwt_workspace_id=JWT_WORKSPACE,
        target_workspace_id=TARGET,
        request_path="/internal/queries",
        redis_client=redis,
    )


@pytest.fixture
def audit(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(cwa, "emit_audit", mock)
    return mock


async def test_a_failed_emit_gives_the_window_back_so_the_next_attempt_alerts(
    audit: AsyncMock,
) -> None:
    redis = _FakeRedis()
    audit.side_effect = [RuntimeError("audit ledger unavailable"), None]

    first = await _emit(redis)
    second = await _emit(redis)

    assert first is False
    assert second is True, "the alert stayed suppressed after a write that never happened"
    assert audit.await_count == 2
    # The retry that succeeded now holds the window.
    assert len(redis.keys) == 1


async def test_a_successful_emit_still_deduplicates_the_window(audit: AsyncMock) -> None:
    redis = _FakeRedis()

    assert await _emit(redis) is True
    assert await _emit(redis) is False

    assert audit.await_count == 1
    assert redis.deleted == []


async def test_a_dedupe_hit_does_not_touch_the_claim(audit: AsyncMock) -> None:
    redis = _FakeRedis()
    await _emit(redis)
    audit.side_effect = RuntimeError("would fail, but must not be called")

    assert await _emit(redis) is False

    assert audit.await_count == 1
    assert redis.deleted == [], "a de-duplicated call must not release the first call's window"


async def test_a_redis_that_cannot_release_the_key_does_not_turn_the_failure_into_a_raise(
    audit: AsyncMock,
) -> None:
    redis = _FakeRedis(fail_delete=True)
    audit.side_effect = RuntimeError("audit ledger unavailable")

    assert await _emit(redis) is False  # logged, not raised: callers are inside a 403 path


async def test_redis_down_at_claim_time_emits_anyway_and_has_nothing_to_release(
    audit: AsyncMock,
) -> None:
    redis = _FakeRedis(fail_set=True)

    assert await _emit(redis) is True

    assert redis.deleted == []


async def test_no_redis_client_is_unchanged(audit: AsyncMock) -> None:
    assert await _emit(None) is True

    audit.side_effect = RuntimeError("down")
    assert await _emit(None) is False


async def test_a_cancelled_emit_gives_the_window_back_too(audit: AsyncMock) -> None:
    redis = _FakeRedis()
    started = asyncio.Event()

    async def _hang(*_a: Any, **_kw: Any) -> None:
        started.set()
        await asyncio.sleep(30)

    audit.side_effect = _hang

    task = asyncio.create_task(_emit(redis))
    await started.wait()
    assert len(redis.keys) == 1  # claimed while the write is in flight
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert redis.keys == {}, "a request torn down mid-write left the alert suppressed"
