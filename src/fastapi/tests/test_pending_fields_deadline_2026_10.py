"""Audit 2026-10 finding 8 (the part in agent/tools.py): the extraction-pending
probes in ``query_project_summary`` have a deadline.

``_compute_pending_fields`` ran after the tool's real work, with a bare
``pool.acquire()`` and three serial probes and no deadline. With the pool
exhausted the acquire waited indefinitely, so a tool that had already finished held
its result back until the whole-query deadline. The same unbounded
``pool.acquire()`` in ``hallucination/orchestrator_validators.py`` and
``hallucination/layer5_provenance.py`` belongs to another engineer and is reported,
not changed here.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent import tools
from app.agent.tools import _PROJECT_SUMMARY_EXTRACTION_CANDIDATES, _compute_pending_fields
from app.config import settings

WS = "a0000000-0000-0000-0000-000000000001"
PROJECT = "c0000000-0000-0000-0000-000000000001"
ALL_CANDIDATES = list(_PROJECT_SUMMARY_EXTRACTION_CANDIDATES)


class _Conn:
    def __init__(self, populated: set[str], delay_s: float = 0.0) -> None:
        self.populated = populated
        self.delay_s = delay_s
        self.asked: list[str] = []

    async def fetchrow(self, sql: str, *_args: Any) -> dict[str, int] | None:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        # Each probe selects from the table that proves its own field.
        for field, probe in tools._PENDING_FIELD_PROBE_SQL.items():
            if sql == probe:
                self.asked.append(field)
                return {"one": 1} if field in self.populated else None
        raise AssertionError("unexpected SQL")


def _pool(conn: _Conn, *, acquire_delay_s: float = 0.0) -> Any:
    @asynccontextmanager
    async def _acquire() -> Any:
        if acquire_delay_s:
            await asyncio.sleep(acquire_delay_s)
        yield conn

    return SimpleNamespace(acquire=_acquire)


@pytest.fixture(autouse=True)
def _short_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TIMEOUT_POSTGIS_S", 0.2, raising=False)


async def test_a_pool_that_never_hands_out_a_connection_falls_back_in_time() -> None:
    deps = SimpleNamespace(pg_pool=_pool(_Conn(set()), acquire_delay_s=30.0))

    started = time.monotonic()
    pending = await _compute_pending_fields(deps, WS, PROJECT)  # type: ignore[arg-type]

    assert pending == ALL_CANDIDATES  # fail closed: the uncertainty block is never under-stated
    assert time.monotonic() - started < 2.0, "the pool wait was not bounded"


async def test_slow_probes_share_one_deadline_and_fall_back() -> None:
    deps = SimpleNamespace(pg_pool=_pool(_Conn(set(), delay_s=1.0)))  # 3 x 1 s against 0.2 s

    started = time.monotonic()
    pending = await _compute_pending_fields(deps, WS, PROJECT)  # type: ignore[arg-type]

    assert pending == ALL_CANDIDATES
    assert time.monotonic() - started < 1.0, "the probes ran past the deadline"


async def test_the_normal_path_is_unchanged() -> None:
    conn = _Conn(populated={"contractor"})
    deps = SimpleNamespace(pg_pool=_pool(conn))

    pending = await _compute_pending_fields(deps, WS, PROJECT)  # type: ignore[arg-type]

    assert pending == [f for f in ALL_CANDIDATES if f != "contractor"]
    assert conn.asked == list(tools._PENDING_FIELD_PROBE_SQL)


async def test_an_error_still_falls_back_to_the_full_list() -> None:
    class _Broken(_Conn):
        async def fetchrow(self, sql: str, *_args: Any) -> None:
            raise RuntimeError("connection reset")

    deps = SimpleNamespace(pg_pool=_pool(_Broken(set())))

    assert await _compute_pending_fields(deps, WS, PROJECT) == ALL_CANDIDATES  # type: ignore[arg-type]


async def test_no_pool_is_unchanged() -> None:
    assert await _compute_pending_fields(SimpleNamespace(pg_pool=None), WS, PROJECT) == ALL_CANDIDATES  # type: ignore[arg-type]
