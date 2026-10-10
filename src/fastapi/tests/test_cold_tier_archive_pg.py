"""archive_window against the real audit ledger, its hash trigger and asyncpg.

The unit tests drive ``archive_window`` through a scripted connection. This file
runs the same function against Postgres so three things a fake cannot check are
checked: the ledger really is one hash chain PER workspace (so walking a window
as one sequence cannot work), the watermark window continues each chain from the
row before it, and the cursor / REPEATABLE READ plumbing is valid asyncpg.

Needs a Postgres with the migration chain applied (the ledger and its BEFORE
INSERT hash trigger) and a superuser login; skips otherwise. Rows use far-future
timestamps and unique workspace ids and are removed afterwards.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest

from app.audit.cold_tier_archive import SYSTEM_CHAIN, archive_window

pytestmark = pytest.mark.integration

PG_DSN = os.environ.get("PG_DSN") or (
    "postgresql://{u}:{p}@{h}:{port}/{db}".format(
        u=os.environ.get("POSTGRES_USER", "georag"),
        p=os.environ.get("POSTGRES_PASSWORD", "georag_dev_password"),
        h=os.environ.get("POSTGRES_DIRECT_HOST", os.environ.get("POSTGRES_HOST", "localhost")),
        port=os.environ.get("POSTGRES_DIRECT_PORT", os.environ.get("POSTGRES_PORT", "5432")),
        db=os.environ.get("POSTGRES_DB", "georag"),
    )
)

WS_A = "7c01d000-0000-4000-8000-0000000a0001"
WS_B = "7c01d000-0000-4000-8000-0000000b0002"
ACTION = "cold.tier.archive.test"
DAY1, DAY2, DAY3 = "2031-06-01", "2031-06-02", "2031-06-03"


def _day(day: str) -> datetime:
    return datetime.fromisoformat(f"{day}T00:00:00+00:00").astimezone(UTC)


class _Store:
    def __init__(self) -> None:
        self.puts: dict[str, bytes] = {}

    async def put(self, key: str, content: bytes) -> str:
        self.puts[key] = content
        return f"s3://test/{key}"


@pytest.fixture
async def admin():  # noqa: ANN201
    try:
        conn = await asyncpg.connect(PG_DSN, timeout=5)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no Postgres at PG_DSN: {exc}")
    try:
        ok = await conn.fetchval(
            "SELECT to_regclass('audit.audit_ledger') IS NOT NULL "
            "AND (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)"
        )
        if not ok:
            pytest.skip("needs the migrated audit schema and a superuser PG_DSN")
        yield conn
    finally:
        await conn.close()


async def _scrub(conn: asyncpg.Connection) -> None:
    async with conn.transaction():
        await conn.execute("SET LOCAL session_replication_role = replica")
        await conn.execute(
            "DELETE FROM audit.audit_ledger WHERE action_type = $1 "
            "AND created_at >= '2031-06-01' AND created_at < '2031-06-04'", ACTION,
        )


@pytest.fixture
async def ledger(admin: asyncpg.Connection):  # noqa: ANN201
    """Three chains, interleaved in time, with history on both sides of day 2.

    * A     a1 a2 on day 1, a3 a4 on day 2
    * NULL  n1 on day 1, n2 on day 2   (the system chain)
    * B     b1 on day 2 (no history before it)
    """
    await _scrub(admin)
    rows = [
        (WS_A, f"{DAY1} 00:00:01+00", "a1"), (None, f"{DAY1} 00:00:02+00", "n1"),
        (WS_A, f"{DAY1} 00:00:03+00", "a2"),
        (WS_A, f"{DAY2} 00:00:01+00", "a3"), (None, f"{DAY2} 00:00:02+00", "n2"),
        (WS_B, f"{DAY2} 00:00:03+00", "b1"), (WS_A, f"{DAY2} 00:00:04+00", "a4"),
    ]
    ids: dict[str, str] = {}
    for ws, when, tag in rows:
        ids[tag] = str(await admin.fetchval(
            "INSERT INTO audit.audit_ledger (workspace_id, actor_kind, action_type, payload, created_at) "
            "VALUES ($1::uuid, 'system', $2, jsonb_build_object('tag', $3::text), $4::text::timestamptz) "
            "RETURNING id",
            ws, ACTION, tag, when,
        ))
    try:
        yield ids
    finally:
        await _scrub(admin)


async def _tamper(conn: asyncpg.Connection, sql: str, *args: Any) -> None:
    async with conn.transaction():
        await conn.execute("SET LOCAL session_replication_role = replica")
        await conn.execute(sql, *args)


async def _heads(conn: asyncpg.Connection, before: str) -> dict[str, str]:
    """The newest hash of each chain strictly before ``before``, from the table."""
    rows = await conn.fetch(
        "SELECT DISTINCT ON (workspace_id) workspace_id::text AS ws, hash FROM audit.audit_ledger "
        "WHERE action_type = $1 AND created_at < $2::text::timestamptz "
        "ORDER BY workspace_id, created_at DESC, id DESC", ACTION, f"{before} 00:00:00+00",
    )
    return {(r["ws"] or SYSTEM_CHAIN): bytes(r["hash"]).hex() for r in rows}


async def test_interleaved_workspace_chains_archive_clean(admin, ledger) -> None:
    """The ledger holds one chain per workspace_id. Seven rows from three chains
    in one window used to fail verification on the second row."""
    store = _Store()

    run = await archive_window(
        admin, cutoff_before=_day(DAY3), cutoff_after=_day(DAY1),
        archive_bucket="audit-cold-test", cold_tier=store, chunk_rows=3,
    )

    assert run.verification_passed is True, run.failure_reason
    assert run.rows_archived == 7
    assert len(run.chunks) == 3  # 3 + 3 + 1
    assert run.chain_heads == await _heads(admin, DAY3)
    manifest = json.loads(store.puts[run.manifest_key].decode())
    assert manifest["chain_continuous"] is True
    assert manifest["chain_heads"] == run.chain_heads
    assert manifest["window_start"] == _day(DAY1).isoformat()
    assert sum(c["rows"] for c in manifest["chunks"]) == 7


async def test_the_next_window_continues_each_chain_from_the_one_before(admin, ledger) -> None:
    store = _Store()
    first = await archive_window(
        admin, cutoff_before=_day(DAY2), cutoff_after=_day(DAY1),
        archive_bucket="audit-cold-test", cold_tier=store,
    )
    assert first.verification_passed and first.rows_archived == 3
    assert first.chain_heads == await _heads(admin, DAY2)

    # Day 2 alone: chains A and system hang off day-1 rows; B has no parent.
    second = await archive_window(
        admin, cutoff_before=_day(DAY3), cutoff_after=_day(DAY2),
        archive_bucket="audit-cold-test", cold_tier=store,
    )

    assert second.verification_passed is True, second.failure_reason
    assert second.rows_archived == 4
    assert second.hot_tier_remaining == 0


async def test_a_forged_parent_across_the_watermark_is_caught(admin, ledger) -> None:
    """Day 1's last A row is changed after day 1 was archived. Day 2's first A row
    still names the original hash as its parent, which is no longer there."""
    await _tamper(admin, "UPDATE audit.audit_ledger SET hash = '\\xdeadbeef' WHERE id = $1::uuid", ledger["a2"])
    store = _Store()

    run = await archive_window(
        admin, cutoff_before=_day(DAY3), cutoff_after=_day(DAY2),
        archive_bucket="audit-cold-test", cold_tier=store,
    )

    assert run.verification_passed is False
    assert f"chain break in {WS_A}" in run.failure_reason
    assert ledger["a3"] in run.failure_reason
    assert store.puts == {}


async def test_tampering_inside_the_window_is_caught_and_nothing_is_uploaded(admin, ledger) -> None:
    await _tamper(
        admin, "UPDATE audit.audit_ledger SET previous_hash = '\\xdeadbeef' WHERE id = $1::uuid", ledger["a4"],
    )
    store = _Store()

    run = await archive_window(
        admin, cutoff_before=_day(DAY3), cutoff_after=_day(DAY2),
        archive_bucket="audit-cold-test", cold_tier=store,
    )

    assert run.verification_passed is False
    assert f"chain break in {WS_A}" in run.failure_reason and ledger["a4"] in run.failure_reason
    assert store.puts == {}


async def test_a_deleted_first_row_is_not_mistaken_for_a_break_without_a_watermark(admin, ledger) -> None:
    """Without a watermark nothing precedes the window, so a chain's first row may
    name any parent (history already pruned). Documents what the seed buys."""
    await _tamper(admin, "DELETE FROM audit.audit_ledger WHERE id = $1::uuid", ledger["a1"])
    store = _Store()

    run = await archive_window(
        admin, cutoff_before=_day(DAY3), cutoff_after=None,
        archive_bucket="audit-cold-test", cold_tier=store,
    )

    # a2 now hangs off a row that is gone, but with no watermark its parent is unconstrained.
    assert run.verification_passed is True
    assert run.rows_archived >= 6


async def test_the_watermark_is_the_newest_completed_cutoff_of_the_scope(admin) -> None:
    """The workflow reads where to resume from its own completed anchors: the max
    cutoff of this scope, ignoring failed runs and other workspaces' runs."""
    from app.hatchet_workflows.cold_tier_archive import _last_archived_cutoff

    anchors = [
        # (workspace, action, cutoff_before, second)
        (None, "audit.cold_tier.archive.completed", "2031-06-01T00:00:00+00:00", 1),
        (None, "audit.cold_tier.archive.completed", "2031-06-03T00:00:00+00:00", 2),
        (None, "audit.cold_tier.archive.completed", "2031-06-02T00:00:00+00:00", 3),  # shorter retention
        (None, "audit.cold_tier.archive.failed", "2031-06-09T00:00:00+00:00", 4),
        (WS_A, "audit.cold_tier.archive.completed", "2031-06-05T00:00:00+00:00", 5),
    ]
    await _scrub_anchors(admin)
    try:
        for ws, action, cutoff, second in anchors:
            await admin.execute(
                "INSERT INTO audit.audit_ledger (workspace_id, actor_kind, action_type, payload, created_at) "
                "VALUES ($1::uuid, 'workflow', $2, jsonb_build_object('cutoff_before', $3::text), "
                "$4::text::timestamptz)",
                ws, action, cutoff, f"2031-06-01 00:00:{second:02d}+00",
            )

        assert await _last_archived_cutoff(admin, None) == datetime(2031, 6, 3, tzinfo=UTC)
        assert await _last_archived_cutoff(admin, WS_A) == datetime(2031, 6, 5, tzinfo=UTC)
        assert await _last_archived_cutoff(admin, WS_B) is None
    finally:
        await _scrub_anchors(admin)


async def _scrub_anchors(conn: asyncpg.Connection) -> None:
    async with conn.transaction():
        await conn.execute("SET LOCAL session_replication_role = replica")
        await conn.execute(
            "DELETE FROM audit.audit_ledger WHERE action_type LIKE 'audit.cold_tier.archive.%' "
            "AND created_at >= '2031-06-01' AND created_at < '2031-06-04'",
        )


async def test_the_scope_restricts_the_window_to_one_chain(admin, ledger) -> None:
    run = await archive_window(
        admin, cutoff_before=_day(DAY3), cutoff_after=_day(DAY1), workspace_id_scope=WS_A,
        archive_bucket="audit-cold-test", cold_tier=_Store(),
    )

    assert run.verification_passed is True
    assert run.rows_archived == 4
    assert list(run.chain_heads) == [WS_A]
