"""§11.10 audit ledger cold-tier archival tests (Phase H4)."""
from __future__ import annotations

import contextlib
import gzip
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.audit.cold_tier_archive import (
    SYSTEM_CHAIN,
    ArchiveRun,
    _gzip_jsonl,
    _row_to_dict,
    _verify_chain,
    archive_window,
    prune_archived_window,
)

pytestmark = pytest.mark.asyncio


# ─────────────────────────── fakes ──────────────────────────────────


class FakeColdTierStore:
    def __init__(self) -> None:
        self.puts: dict[str, bytes] = {}
        self.order: list[str] = []

    async def put(self, key: str, content: bytes) -> str:
        self.puts[key] = content
        self.order.append(key)
        return f"s3://fake/{key}"


@dataclass
class FakeRecord:
    """Stand-in for asyncpg.Record that behaves like dict.items()."""
    data: dict[str, Any]

    def items(self):  # noqa: D401
        return self.data.items()

    def __getitem__(self, k):
        return self.data[k]

    def get(self, k, default=None):
        return self.data.get(k, default)


class _FakeCursor:
    """What ``conn.cursor(...)`` returns: lazily yields the rows, noting each."""

    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for r in self._conn._eligible:
            self._conn.events.append(("row", r["id"]))
            yield FakeRecord(r)


class FakeConn:
    """Minimal asyncpg.Connection stand-in for the archive_window path.

    The archiver issues, in order:
      1. count the window's rows
      2. count the rows kept hot (at/after the cutoff)
      3. a cursor over the window ORDER BY created_at, id -- once to verify and
         once to upload, inside one transaction
    plus, when it has a watermark, one ``SELECT hash`` probe per chain.
    """
    def __init__(self, eligible_rows: list[dict[str, Any]],
                 total_rows: int, *, seeds: dict[Any, bytes] | None = None,
                 in_transaction: bool = False) -> None:
        self._eligible = eligible_rows
        self._total = total_rows
        self._seeds = seeds or {}
        self._in_tx = in_transaction
        self.calls: list[tuple[str, tuple]] = []
        self.events: list[tuple[str, Any]] = []
        self.transactions: list[dict[str, Any]] = []

    def is_in_transaction(self) -> bool:
        return self._in_tx

    @contextlib.asynccontextmanager
    async def transaction(self, **kwargs):
        self.transactions.append(kwargs)
        self._in_tx = True
        try:
            yield self
        finally:
            self._in_tx = False

    async def fetchval(self, sql: str, *args):
        self.calls.append((sql, args))
        if "SELECT hash FROM audit.audit_ledger" in sql:
            workspace = args[0] if "workspace_id = $1" in sql else None
            return self._seeds.get(workspace)
        if "count(*)" in sql and "WHERE created_at <" in sql:
            return len(self._eligible)
        if "count(*)" in sql:
            return self._total
        raise NotImplementedError(sql)

    def cursor(self, sql: str, *args, prefetch=None):
        self.calls.append((sql, args))
        self.events.append(("cursor", prefetch))
        return _FakeCursor(self)

    async def execute(self, sql: str, *args):
        self.calls.append((sql, args))
        return f"DELETE {len(self._eligible)}"


def _make_row(i: int, *, prev_hash: bytes | None, h: bytes,
              ws: str | None = "ws-1") -> dict[str, Any]:
    return {
        "id":            f"row-{i}",
        "workspace_id":  ws,
        "actor_id":      42,
        "actor_kind":    "user",
        "action_type":   "test.event",
        "target_schema": "silver",
        "target_table":  "x",
        "target_id":     "1",
        "payload":       {"i": i},
        "previous_hash": prev_hash,
        "hash":          h,
        "trace_id":      f"t-{i}",
        "created_at":    datetime(2025, 1, 1, tzinfo=UTC)
                         + timedelta(seconds=i),
    }


def _chain(n: int) -> list[dict[str, Any]]:
    """Build n rows with a continuous previous_hash → hash chain."""
    out: list[dict[str, Any]] = []
    prev: bytes | None = None
    for i in range(n):
        h = bytes([i + 1, 0, 0, 0])
        out.append(_make_row(i, prev_hash=prev, h=h))
        prev = h
    return out


def _chains(n: int, workspaces: tuple[str | None, ...] = ("ws-A", "ws-B", None)) -> list[dict[str, Any]]:
    """n rows interleaved round-robin over the workspaces, each its own chain.

    Row i's previous_hash is the hash of the previous row IN ITS OWN CHAIN, as
    the ledger's BEFORE INSERT trigger writes it, not of row i-1.
    """
    out: list[dict[str, Any]] = []
    prev: dict[str | None, bytes | None] = {}
    for i in range(n):
        ws = workspaces[i % len(workspaces)]
        h = bytes([i + 1, 0, 0, 0])
        out.append(_make_row(i, prev_hash=prev.get(ws), h=h, ws=ws))
        prev[ws] = h
    return out


def _dicts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_row_to_dict(FakeRecord(r)) for r in rows]


# ──────────────────── helper unit tests ────────────────────────────


def test_row_to_dict_hexifies_bytes() -> None:
    r = FakeRecord({"hash": b"\xde\xad\xbe\xef"})
    d = _row_to_dict(r)
    assert d["hash"] == "deadbeef"


def test_row_to_dict_iso_datetimes() -> None:
    ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    r = FakeRecord({"created_at": ts})
    d = _row_to_dict(r)
    assert d["created_at"].startswith("2026-01-01")


def test_gzip_jsonl_roundtrip() -> None:
    rows = [{"i": 1}, {"i": 2}]
    blob = _gzip_jsonl(rows)
    decoded = gzip.decompress(blob).decode().splitlines()
    assert [json.loads(line) for line in decoded] == rows


def test_verify_chain_continuous() -> None:
    rows = [_row_to_dict(FakeRecord(r)) for r in _chain(5)]
    ok, reason = _verify_chain(rows)
    assert ok and reason is None


def test_verify_chain_break_detected() -> None:
    rows = [_row_to_dict(FakeRecord(r)) for r in _chain(3)]
    # Corrupt row 2's previous_hash
    rows[2]["previous_hash"] = "deadbeef"
    ok, reason = _verify_chain(rows)
    assert not ok
    assert "chain break" in (reason or "")


# ─────────── per-chain verification (2026-10 Hatchet audit, #9) ───────────


def test_verify_chain_walks_each_workspace_chain_on_its_own() -> None:
    """The ledger is one chain per workspace_id (NULL included). Three
    interleaved, individually intact chains used to fail the check at row 2,
    because row i was compared with row i-1 whatever chain it belonged to."""
    rows = _dicts(_chains(12))
    assert len({r["workspace_id"] for r in rows}) == 3

    ok, reason = _verify_chain(rows)

    assert ok and reason is None


def test_verify_chain_breaks_inside_one_chain_name_that_chain() -> None:
    rows = _dicts(_chains(12))
    victim = next(r for r in rows if r["workspace_id"] == "ws-B" and r["previous_hash"] is not None)
    victim["previous_hash"] = "ffffffff"

    ok, reason = _verify_chain(rows)

    assert not ok
    assert "chain break in ws-B" in reason
    assert victim["id"] in reason


def test_verify_chain_names_the_system_chain() -> None:
    rows = _dicts(_chains(9))
    victim = next(r for r in rows if r["workspace_id"] is None and r["previous_hash"] is not None)
    victim["previous_hash"] = "ffffffff"

    _, reason = _verify_chain(rows)

    assert f"chain break in {SYSTEM_CHAIN}" in reason


def test_verify_chain_first_row_continues_from_the_seed_when_there_is_one() -> None:
    """Across the watermark: each chain's first row must hang off the newest row
    of that chain before the window. Without a seed it may carry anything."""
    rows = _dicts(_chains(6))  # every first row has previous_hash None
    seeds = {"ws-A": "0a0a0a0a", "ws-B": None, None: None}

    ok, reason = _verify_chain(rows, seeds)

    assert not ok
    assert "chain break in ws-A" in reason
    assert "newest row before the window" in reason
    # A seed that matches is fine; a missing one (pruned history) constrains nothing.
    rows[0]["previous_hash"] = "0a0a0a0a"
    assert _verify_chain(rows, seeds) == (True, None)
    assert _verify_chain(rows) == (True, None)


# ──────────────────── archive_window integration-ish ────────────────


async def test_archive_window_zero_eligible_short_circuit() -> None:
    conn = FakeConn(eligible_rows=[], total_rows=10)
    store = FakeColdTierStore()
    run = await archive_window(
        conn,
        cutoff_before=datetime(2026, 1, 1, tzinfo=UTC),
        archive_bucket="audit-cold",
        cold_tier=store,
    )
    assert run.rows_archived == 0
    assert run.verification_passed is True
    assert store.puts == {}


async def test_archive_window_dry_run_writes_nothing() -> None:
    conn = FakeConn(eligible_rows=_chain(3), total_rows=10)
    store = FakeColdTierStore()
    run = await archive_window(
        conn,
        cutoff_before=datetime(2026, 1, 1, tzinfo=UTC),
        archive_bucket="audit-cold",
        cold_tier=store,
        dry_run=True,
    )
    assert run.rows_archived == 3
    assert run.verification_passed is True
    assert "(dry-run)" in run.cold_tier_uri
    assert store.puts == {}


async def test_archive_window_writes_chunks_and_manifest() -> None:
    conn = FakeConn(eligible_rows=_chain(25), total_rows=100)
    store = FakeColdTierStore()
    run = await archive_window(
        conn,
        cutoff_before=datetime(2026, 1, 1, tzinfo=UTC),
        archive_bucket="audit-cold",
        cold_tier=store,
        chunk_rows=10,
    )
    assert run.rows_archived == 25
    assert run.verification_passed is True
    # 25 rows / 10 per chunk = 3 chunks + 1 manifest = 4 objects
    assert len(store.puts) == 4
    assert any(k.endswith("manifest.json") for k in store.puts)
    chunk_keys = [k for k in store.puts if "chunk-" in k]
    assert len(chunk_keys) == 3
    # Verify the manifest is valid JSON and points to all chunks.
    manifest_key = next(k for k in store.puts if k.endswith("manifest.json"))
    manifest = json.loads(store.puts[manifest_key].decode())
    assert manifest["rows_archived"] == 25
    assert manifest["chain_continuous"] is True
    assert len(manifest["chunks"]) == 3


async def test_archive_window_aborts_on_chain_break() -> None:
    rows = _chain(5)
    # Corrupt the chain at row 3
    rows[3]["previous_hash"] = b"\xff\xff\xff\xff"
    conn = FakeConn(eligible_rows=rows, total_rows=10)
    store = FakeColdTierStore()
    run = await archive_window(
        conn,
        cutoff_before=datetime(2026, 1, 1, tzinfo=UTC),
        archive_bucket="audit-cold",
        cold_tier=store,
    )
    assert run.verification_passed is False
    assert "chain break" in (run.failure_reason or "")
    # Nothing written on chain break
    assert store.puts == {}


async def test_archive_window_chunk_first_last_hash_recorded() -> None:
    conn = FakeConn(eligible_rows=_chain(15), total_rows=20)
    store = FakeColdTierStore()
    run = await archive_window(
        conn,
        cutoff_before=datetime(2026, 1, 1, tzinfo=UTC),
        archive_bucket="audit-cold",
        cold_tier=store,
        chunk_rows=10,
    )
    # 15 rows / 10 = 2 chunks
    assert len(run.chunks) == 2
    assert run.chunks[0]["rows"] == 10
    assert run.chunks[1]["rows"] == 5
    assert run.chunks[0]["first_hash"] is not None


async def test_archive_window_workspace_scope_passed_through() -> None:
    conn = FakeConn(eligible_rows=_chain(3), total_rows=10)
    store = FakeColdTierStore()
    await archive_window(
        conn,
        cutoff_before=datetime(2026, 1, 1, tzinfo=UTC),
        archive_bucket="audit-cold",
        cold_tier=store,
        workspace_id_scope="ws-tenant-A",
    )
    # First call to fetchval should have ws-tenant-A as the second arg
    first_count_call = conn.calls[0]
    assert "ws-tenant-A" in first_count_call[1]


async def test_prune_archived_window_returns_delete_count() -> None:
    conn = FakeConn(eligible_rows=_chain(7), total_rows=20)
    n = await prune_archived_window(
        conn,
        cutoff_before=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert n == 7
    # Last call should be a DELETE
    assert any("DELETE" in c[0] for c in conn.calls)


def test_archive_run_to_dict_roundtrip() -> None:
    run = ArchiveRun(
        rows_archived=5,
        cold_tier_uri="s3://x/y",
        hot_tier_remaining=10,
        verification_passed=True,
        manifest_key="x/y/manifest.json",
        chunks=({"key": "k", "uri": "u", "rows": 5},),
    )
    d = run.to_dict()
    assert d["rows_archived"] == 5
    assert d["chunks"][0]["rows"] == 5


# ─────────── a window of several workspaces, a watermark, a stream ───────────


async def test_archive_window_archives_interleaved_chains() -> None:
    """The case that failed: rows from three workspaces in one window. The
    manifest says where each chain ended, which is what the next window
    continues from."""
    conn = FakeConn(eligible_rows=_chains(12), total_rows=50)
    store = FakeColdTierStore()

    run = await archive_window(
        conn,
        cutoff_before=datetime(2026, 1, 1, tzinfo=UTC),
        archive_bucket="audit-cold",
        cold_tier=store,
        chunk_rows=5,
    )

    assert run.verification_passed is True and run.failure_reason is None
    assert run.rows_archived == 12
    # The last row of each chain: ws-A i=9, ws-B i=10, system i=11 (hash = bytes([i+1,0,0,0])).
    assert run.chain_heads == {"ws-A": "0a000000", "ws-B": "0b000000", SYSTEM_CHAIN: "0c000000"}
    manifest = json.loads(store.puts[run.manifest_key].decode())
    assert manifest["chain_heads"] == run.chain_heads
    assert manifest["chain_continuous"] is True
    assert manifest["rows_archived"] == 12
    assert manifest["window_start"] is None


async def test_archive_window_starts_at_the_watermark() -> None:
    """A night archives the night's rows, not the whole history again."""
    conn = FakeConn(eligible_rows=_chain(3), total_rows=10)
    store = FakeColdTierStore()
    after = datetime(2025, 12, 1, tzinfo=UTC)
    before = datetime(2026, 1, 1, tzinfo=UTC)

    run = await archive_window(
        conn, cutoff_before=before, archive_bucket="audit-cold",
        cold_tier=store, cutoff_after=after,
    )

    count_sql, count_args = conn.calls[0]
    assert "created_at < $1" in count_sql and "created_at >= $2" in count_sql
    assert count_args == (before, after)
    _, cursor_args = next(c for c in conn.calls if "ORDER BY created_at ASC, id ASC" in c[0])
    assert cursor_args == (before, after)
    assert run.verification_passed is True
    manifest = json.loads(store.puts[run.manifest_key].decode())
    assert manifest["window_start"] == after.isoformat()
    assert manifest["cutoff_before"] == before.isoformat()


async def test_archive_window_scoped_with_watermark_numbers_its_parameters() -> None:
    conn = FakeConn(eligible_rows=_chain(2), total_rows=10)
    before, after = datetime(2026, 1, 1, tzinfo=UTC), datetime(2025, 12, 1, tzinfo=UTC)

    await archive_window(
        conn, cutoff_before=before, archive_bucket="b", cold_tier=FakeColdTierStore(),
        workspace_id_scope="ws-tenant-A", cutoff_after=after,
    )

    sql, args = conn.calls[0]
    assert "workspace_id = $2" in sql and "created_at >= $3" in sql
    assert args == (before, "ws-tenant-A", after)


async def test_archive_window_keeps_only_the_cutoff_onwards_hot() -> None:
    """hot_tier_remaining was 'every row minus the window', which is wrong once
    the window is only the new rows: those before the watermark are archived but
    still in the table."""
    conn = FakeConn(eligible_rows=_chain(3), total_rows=40)

    run = await archive_window(
        conn, cutoff_before=datetime(2026, 1, 1, tzinfo=UTC), archive_bucket="b",
        cold_tier=FakeColdTierStore(), cutoff_after=datetime(2025, 12, 1, tzinfo=UTC),
    )

    remaining_sql, _ = conn.calls[1]
    assert "count(*)" in remaining_sql and "created_at >= $1" in remaining_sql
    assert run.hot_tier_remaining == 40


async def test_archive_window_checks_each_chain_continues_from_before_the_window() -> None:
    rows = _chains(6)  # first rows of ws-A, ws-B, system carry previous_hash None
    # The ledger holds a newer row for ws-B before the window than the first
    # in-window row claims as its parent.
    conn = FakeConn(eligible_rows=rows, total_rows=10, seeds={"ws-B": b"\x0b\x0b\x0b\x0b"})
    store = FakeColdTierStore()

    run = await archive_window(
        conn, cutoff_before=datetime(2026, 1, 1, tzinfo=UTC), archive_bucket="b",
        cold_tier=store, cutoff_after=datetime(2025, 12, 1, tzinfo=UTC),
    )

    assert run.verification_passed is False
    assert "chain break in ws-B" in run.failure_reason
    assert store.puts == {}, "a window that does not continue its chain is not archived"
    probes = [c for c in conn.calls if "SELECT hash FROM audit.audit_ledger" in c[0]]
    assert len(probes) == 2 or len(probes) == 3, "one probe per chain, until the break"


async def test_archive_window_without_a_watermark_does_not_probe_for_seeds() -> None:
    conn = FakeConn(eligible_rows=_chains(6), total_rows=10)

    run = await archive_window(
        conn, cutoff_before=datetime(2026, 1, 1, tzinfo=UTC), archive_bucket="b",
        cold_tier=FakeColdTierStore(),
    )

    assert run.verification_passed is True
    assert not [c for c in conn.calls if "SELECT hash FROM audit.audit_ledger" in c[0]]


async def test_archive_window_streams_and_holds_one_chunk() -> None:
    """Rows are read through a cursor, not fetched into a list, and the upload
    pass writes a chunk as soon as it is full instead of after the last row."""
    conn = FakeConn(eligible_rows=_chain(25), total_rows=100)
    store = FakeColdTierStore()
    puts_seen_at: list[int] = []

    original_put = store.put

    async def _put(key: str, content: bytes) -> str:
        puts_seen_at.append(len([e for e in conn.events if e[0] == "row"]))
        return await original_put(key, content)

    store.put = _put  # type: ignore[method-assign]

    await archive_window(
        conn, cutoff_before=datetime(2026, 1, 1, tzinfo=UTC), archive_bucket="b",
        cold_tier=store, chunk_rows=10,
    )

    # (FakeConn has no `fetch`: a bulk fetch of the window could not have run.)
    assert not hasattr(conn, "fetch")
    assert len([e for e in conn.events if e[0] == "cursor"]) == 2, "one pass to verify, one to upload"
    # Pass 1 read all 25 rows; the first chunk went out after row 10 of pass 2
    # (25 + 10), the second after row 20 (25 + 20), the third after row 25 and
    # then the manifest.
    assert puts_seen_at[:3] == [35, 45, 50]


async def test_archive_window_reads_both_passes_in_one_repeatable_read_snapshot() -> None:
    conn = FakeConn(eligible_rows=_chain(5), total_rows=10)

    await archive_window(
        conn, cutoff_before=datetime(2026, 1, 1, tzinfo=UTC), archive_bucket="b",
        cold_tier=FakeColdTierStore(),
    )

    assert conn.transactions == [{"isolation": "repeatable_read", "readonly": True}]


async def test_archive_window_inside_a_callers_transaction_opens_no_nested_one() -> None:
    """asyncpg refuses a different isolation level nested in a transaction."""
    conn = FakeConn(eligible_rows=_chain(5), total_rows=10, in_transaction=True)

    run = await archive_window(
        conn, cutoff_before=datetime(2026, 1, 1, tzinfo=UTC), archive_bucket="b",
        cold_tier=FakeColdTierStore(),
    )

    assert run.verification_passed is True
    assert conn.transactions == []


async def test_archive_window_refuses_a_manifest_for_rows_that_changed_between_passes() -> None:
    conn = FakeConn(eligible_rows=_chain(5), total_rows=10)
    store = FakeColdTierStore()
    pass_no = {"n": 0}
    real_cursor = conn.cursor

    def _shrinking_cursor(sql, *args, prefetch=None):
        pass_no["n"] += 1
        if pass_no["n"] == 2:
            conn._eligible = conn._eligible[:3]  # two rows vanish before the upload pass
        return real_cursor(sql, *args, prefetch=prefetch)

    conn.cursor = _shrinking_cursor  # type: ignore[method-assign]

    run = await archive_window(
        conn, cutoff_before=datetime(2026, 1, 1, tzinfo=UTC), archive_bucket="b", cold_tier=store,
    )

    assert run.verification_passed is False
    assert "changed during the archive" in run.failure_reason
    assert not any(k.endswith("manifest.json") for k in store.puts)
