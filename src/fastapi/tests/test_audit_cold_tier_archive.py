"""§11.10 audit ledger cold-tier archival tests (Phase H4)."""
from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.audit.cold_tier_archive import (
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

    async def put(self, key: str, content: bytes) -> str:
        self.puts[key] = content
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


class FakeConn:
    """Minimal asyncpg.Connection stand-in for the archive_window path.

    Supports `fetchval` and `fetch` with a per-query lookup table.
    The actual archiver issues 3 queries in order:
      1. count eligible rows
      2. count total rows (for hot_tier_remaining)
      3. SELECT rows ORDER BY created_at, id
    """
    def __init__(self, eligible_rows: list[dict[str, Any]],
                 total_rows: int) -> None:
        self._eligible = eligible_rows
        self._total = total_rows
        self.calls: list[tuple[str, tuple]] = []

    async def fetchval(self, sql: str, *args):
        self.calls.append((sql, args))
        if "count(*)" in sql and "WHERE created_at <" in sql:
            return len(self._eligible)
        if "count(*)" in sql:
            return self._total
        raise NotImplementedError(sql)

    async def fetch(self, sql: str, *args):
        self.calls.append((sql, args))
        return [FakeRecord(r) for r in self._eligible]

    async def execute(self, sql: str, *args):
        self.calls.append((sql, args))
        return f"DELETE {len(self._eligible)}"


def _make_row(i: int, *, prev_hash: bytes | None, h: bytes,
              ws: str = "ws-1") -> dict[str, Any]:
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
        "created_at":    datetime(2025, 1, 1, tzinfo=timezone.utc)
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


# ──────────────────── helper unit tests ────────────────────────────


def test_row_to_dict_hexifies_bytes() -> None:
    r = FakeRecord({"hash": b"\xde\xad\xbe\xef"})
    d = _row_to_dict(r)
    assert d["hash"] == "deadbeef"


def test_row_to_dict_iso_datetimes() -> None:
    ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
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


# ──────────────────── archive_window integration-ish ────────────────


async def test_archive_window_zero_eligible_short_circuit() -> None:
    conn = FakeConn(eligible_rows=[], total_rows=10)
    store = FakeColdTierStore()
    run = await archive_window(
        conn,
        cutoff_before=datetime(2026, 1, 1, tzinfo=timezone.utc),
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
        cutoff_before=datetime(2026, 1, 1, tzinfo=timezone.utc),
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
        cutoff_before=datetime(2026, 1, 1, tzinfo=timezone.utc),
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
        cutoff_before=datetime(2026, 1, 1, tzinfo=timezone.utc),
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
        cutoff_before=datetime(2026, 1, 1, tzinfo=timezone.utc),
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
        cutoff_before=datetime(2026, 1, 1, tzinfo=timezone.utc),
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
        cutoff_before=datetime(2026, 1, 1, tzinfo=timezone.utc),
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
