"""georag_outbox_lag_seconds must see every tenant's backlog, not one scope's.

outbox.pending_propagations is fail-closed after migrate + raw (migration
2026_10_10_100300): its policy is ``workspace_id IS NOT DISTINCT FROM
NULLIF(current_setting('app.workspace_id', true), '')::uuid``. A session with the
scope unset sees ONLY platform rows (workspace_id NULL); a bound one sees only
its own workspace's.

reliability_metrics_publisher computed the gauge on a bare ``pool.acquire()``
with the scope unset. Under that policy the read returns nothing for any tenant,
so a workspace whose outbox had been stuck for hours contributed zero lag and
the gauge looked healthy. The publisher now reads once per workspace with the
scope bound and once more with it cleared (the way outbox_dispatcher claims its
rows) and reports the worst lag per target_store.

The database is faked, but the fake ENFORCES that policy on every read of the
table, so a publisher that reads under the wrong scope fails here exactly as it
would against Postgres. The same behaviour against the real policy, as the real
role, is test_cron_sweeps_under_app_role.py::test_the_outbox_lag_gauge_*.
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any, NamedTuple

import pytest

from app.hatchet_workflows import _progress as ingest_progress
from app.hatchet_workflows import reliability_metrics_publisher as pub
from app.metrics import OUTBOX_LAG_SECONDS

WS_A = "a1000000-0000-0000-0000-00000000000a"
WS_B = "b2000000-0000-0000-0000-00000000000b"
_SENTINEL = -1.0


class _Row(NamedTuple):
    workspace_id: str | None
    target_store: str
    status: str
    age_s: float


class _FakeConn:
    """Just enough asyncpg to run the publisher, with the fail-closed policy."""

    def __init__(self, rows: list[_Row], workspaces: list[str]) -> None:
        self._rows = rows
        self._workspaces = workspaces
        self.scope: str | None = None
        self._depth = 0
        #: The scope every read of the outbox table ran under.
        self.outbox_reads: list[str | None] = []

    def is_in_transaction(self) -> bool:
        return self._depth > 0

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[_FakeConn]:
        self._depth += 1
        try:
            yield self
        finally:
            self._depth -= 1
            if self._depth == 0:
                self.scope = None  # SET LOCAL ends with its transaction

    async def execute(self, sql: str, *args: Any) -> str:
        if "set_config('app.workspace_id'" in sql:
            # bind_workspace_scope passes ($1 = id, $2 = is_local); clearing is
            # a literal '' in the SQL.
            self.scope = (args[0] if args else "") or None
        return "SELECT 1"

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM silver.workspaces" in sql:
            return [{"workspace_id": w} for w in self._workspaces]
        if "FROM outbox.pending_propagations" in sql:
            self.outbox_reads.append(self.scope)
            # The policy: a row is visible only to its own scope (NULL = unbound).
            visible = [
                r for r in self._rows
                if r.workspace_id == self.scope and r.status in ("pending", "in_flight")
            ]
            oldest: dict[str, float] = {}
            for r in visible:
                oldest[r.target_store] = max(oldest.get(r.target_store, 0.0), r.age_s)
            return [{"target_store": s, "lag_s": lag} for s, lag in oldest.items()]
        raise AssertionError(f"unexpected query: {sql}")

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, float]:
        return {"lag_s": 5.0}  # the MV half of the publisher; not under test

    async def fetchval(self, sql: str, *args: Any) -> str:
        return "georag_app"


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self.conn = conn

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncIterator[_FakeConn]:
        yield self.conn


def _gauge(store: str) -> float:
    return OUTBOX_LAG_SECONDS.labels(target_store=store)._value.get()


@pytest.fixture
def run_publisher(monkeypatch):
    """publish_now() against a fake database holding ``rows``."""
    monkeypatch.setattr(pub, "_published_outbox_stores", set())
    for store in ("qdrant", "redis", "external_webhook", "seaweedfs"):
        OUTBOX_LAG_SECONDS.labels(target_store=store).set(_SENTINEL)

    async def _run(rows: list[_Row], workspaces: list[str]):
        conn = _FakeConn(rows, workspaces)

        async def _get_pool() -> _FakePool:
            return _FakePool(conn)

        monkeypatch.setattr(ingest_progress, "get_pool", _get_pool)
        return conn, await pub.publish_now()

    return _run


async def test_the_fake_enforces_the_policy_it_stands_for(run_publisher) -> None:
    """Premise: read with the scope unset, the fake shows ONLY platform rows.
    This is what the unbound publisher used to see."""
    conn = _FakeConn(
        [_Row(WS_A, "qdrant", "pending", 3600.0), _Row(None, "external_webhook", "pending", 45.0)],
        [WS_A],
    )
    unbound = await conn.fetch("SELECT 1 FROM outbox.pending_propagations")
    assert [r["target_store"] for r in unbound] == ["external_webhook"]

    async with conn.transaction():
        await conn.execute("SELECT set_config('app.workspace_id', $1, $2)", WS_A, True)
        bound = await conn.fetch("SELECT 1 FROM outbox.pending_propagations")
    assert [r["target_store"] for r in bound] == ["qdrant"]
    assert conn.scope is None, "the scope must not outlive its transaction"


async def test_the_gauge_reports_the_worst_lag_across_every_workspace_and_the_platform(
    run_publisher,
) -> None:
    rows = [
        _Row(WS_A, "qdrant", "pending", 3600.0),    # a tenant outbox stuck for an hour
        _Row(WS_A, "qdrant", "pending", 30.0),
        _Row(WS_B, "qdrant", "in_flight", 600.0),   # another tenant, same store, less stuck
        _Row(WS_B, "redis", "pending", 120.0),
        _Row(None, "external_webhook", "pending", 45.0),  # platform row
        _Row(WS_A, "redis", "sent", 99999.0),       # delivered: not lag
    ]

    conn, out = await run_publisher(rows, [WS_A, WS_B])

    assert _gauge("qdrant") == pytest.approx(3600.0)
    assert _gauge("redis") == pytest.approx(120.0)
    assert _gauge("external_webhook") == pytest.approx(45.0)
    assert out.outbox_target_stores_updated == 3
    # One bound read per workspace, one with the scope cleared, and none unbound
    # by accident.
    assert sorted(conn.outbox_reads, key=str) == sorted([WS_A, WS_B, None], key=str)


async def test_a_tenant_only_backlog_is_not_reported_as_zero(run_publisher) -> None:
    """The failure itself: nothing on the platform scope, a long backlog in one
    tenant. An unbound read sees no row at all, so the gauge never moved."""
    rows = [_Row(WS_A, "qdrant", "pending", 7200.0)]

    _, out = await run_publisher(rows, [WS_A])

    assert _gauge("qdrant") == pytest.approx(7200.0)
    assert out.outbox_target_stores_updated == 1


async def test_a_store_that_drains_goes_back_to_zero(run_publisher, monkeypatch) -> None:
    await run_publisher([_Row(WS_A, "qdrant", "pending", 900.0)], [WS_A])
    assert _gauge("qdrant") == pytest.approx(900.0)

    # Next tick: delivered, nothing pending. The row is simply absent from the read.
    await run_publisher([], [WS_A])
    assert _gauge("qdrant") == 0.0


async def test_no_scope_is_left_bound_for_the_next_user_of_the_connection(run_publisher) -> None:
    conn, _ = await run_publisher([_Row(WS_A, "qdrant", "pending", 10.0)], [WS_A])
    assert conn.scope is None
