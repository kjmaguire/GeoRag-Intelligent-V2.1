"""audit.verify_hash_chain: the first in-window row of a chain is checked
against the row BEFORE the window, not against NULL.

The verifier took ``LAG(hash)`` over the in-window rows only, so the first row
of every chain that already had history got ``expected_prev = NULL`` while its
stored ``previous_hash`` was the (non-NULL) hash of the row before the window.
``stored_prev IS DISTINCT FROM expected_prev`` flagged it, and the nightly 24 h
walk reported one false break per workspace on a perfectly clean ledger. The
fix is migration 2026_10_10_100000 / database/raw/phase0/100-audit-verify-
function.sql; ``audit_ledger_verify`` additionally emits an alarm marker for a
verdict that is not 'clean', which is only worth wiring once the verdict can
be trusted.

Needs a Postgres with the migration chain applied (the ledger, its hash
trigger and the verification functions); skips otherwise. Rows use far-future
timestamps and unique workspace ids, and are removed afterwards.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from app.hatchet_workflows import audit_ledger_verify as alv

REPO = Path(__file__).resolve().parents[3]
MIGRATION = REPO / "database" / "migrations" / "2026_10_10_100000_audit_verify_hash_chain_seeds_window_from_prior_row.php"
RAW = REPO / "database" / "raw" / "phase0" / "100-audit-verify-function.sql"

PG_DSN = os.environ.get("PG_DSN") or (
    "postgresql://{u}:{p}@{h}:{port}/{db}".format(
        u=os.environ.get("POSTGRES_USER", "georag"),
        p=os.environ.get("POSTGRES_PASSWORD", "georag_dev_password"),
        h=os.environ.get("POSTGRES_DIRECT_HOST", os.environ.get("POSTGRES_HOST", "localhost")),
        port=os.environ.get("POSTGRES_DIRECT_PORT", os.environ.get("POSTGRES_PORT", "5432")),
        db=os.environ.get("POSTGRES_DB", "georag"),
    )
)


# ---------------------------------------------------------------------------
# The migration and the raw file carry one function (no database needed)
# ---------------------------------------------------------------------------
def _body(text: str, opener: str, closer: str) -> str:
    start = text.index("CREATE OR REPLACE FUNCTION audit.verify_hash_chain(")
    inner = text[text.index(opener, start) + len(opener):]
    return re.sub(r"\s+", " ", inner[: inner.index(closer)]).strip()


def test_the_migration_and_the_raw_file_install_the_same_function() -> None:
    migration = _body(MIGRATION.read_text(encoding="utf-8"), "$fn$", "$fn$;")
    raw = _body(RAW.read_text(encoding="utf-8"), "AS $$", "$$;")
    # comments differ in wording between the two files; compare the SQL only
    strip_comments = lambda s: re.sub(r"--[^\n]*", "", s)  # noqa: E731
    assert re.sub(r"\s+", " ", strip_comments(migration)) == re.sub(r"\s+", " ", strip_comments(raw))
    assert "seeds" in migration and "row_number() OVER chain = 1" in migration


# ---------------------------------------------------------------------------
# The marker (no database needed)
# ---------------------------------------------------------------------------
def test_the_marker_is_distinctive_and_leaves_the_database() -> None:
    assert alv.AUDIT_CHAIN_BREAK_MARKER == "AUDIT_LEDGER_CHAIN_BREAK"
    assert alv.AUDIT_CHAIN_BREAK_MARKER.isupper() and "_" in alv.AUDIT_CHAIN_BREAK_MARKER
    alerts = (REPO / "deploy" / "aws" / "terraform" / "alerts.tf").read_text(encoding="utf-8")
    assert alv.AUDIT_CHAIN_BREAK_MARKER in alerts, "no metric filter matches the marker"


def test_the_marker_line_names_ids_and_counts_and_nothing_else(caplog: pytest.LogCaptureFixture) -> None:
    ids = [uuid.uuid4() for _ in range(9)]
    row = {"status": "break", "rows_verified": 40, "broken_ids": ids}
    run_id = uuid.uuid4()
    with caplog.at_level(logging.ERROR, logger="georag.hatchet.audit_ledger_verify"):
        alv._log_chain_break(  # type: ignore[arg-type]
            run_id, row, datetime(2031, 5, 2, tzinfo=UTC), datetime(2031, 5, 3, tzinfo=UTC),
        )
    (line,) = [r.getMessage() for r in caplog.records]
    assert line.startswith(alv.AUDIT_CHAIN_BREAK_MARKER)
    assert str(run_id) in line and "breaks=9" in line and "rows_verified=40" in line
    assert str(ids[0]) in line and str(ids[4]) in line and str(ids[5]) not in line, "only a sample of ids"


# ---------------------------------------------------------------------------
# Against Postgres
# ---------------------------------------------------------------------------
pg = pytest.mark.integration

WS_A = "5a0d1f00-0000-4000-8000-0000000a0001"
WS_B = "5a0d1f00-0000-4000-8000-0000000b0002"
ACTION = "verify.window.test"
DAY1 = "2031-05-01"
DAY2 = "2031-05-02"
DAY3 = "2031-05-03"


def _at(day: str, second: int) -> str:
    return f"{day} 00:00:{second:02d}+00"


@pytest.fixture
async def admin():  # noqa: ANN201
    try:
        conn = await asyncpg.connect(PG_DSN, timeout=5)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no Postgres at PG_DSN: {exc}")
    try:
        ok = await conn.fetchval(
            "SELECT to_regclass('audit.audit_ledger') IS NOT NULL "
            "AND to_regprocedure('audit.verify_hash_chain(timestamptz,timestamptz)') IS NOT NULL "
            "AND to_regclass('audit.audit_ledger_verification_runs') IS NOT NULL "
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
            "DELETE FROM audit.audit_ledger WHERE action_type = $1 AND created_at >= '2031-05-01' "
            "AND created_at < '2031-05-04'", ACTION,
        )
        await conn.execute(
            "DELETE FROM audit.audit_ledger_verification_runs WHERE partition_date >= '2031-05-01' "
            "AND partition_date < '2031-05-04'",
        )


@pytest.fixture
async def ledger(admin: asyncpg.Connection):  # noqa: ANN201
    """A ledger with three chains and history on both sides of day 2.

    * A      two rows on day 1, two on day 2
    * NULL   one row on day 1, one on day 2     (the system-wide chain)
    * B      no history: starts on day 2
    """
    await _scrub(admin)
    rows: list[tuple[str | None, str, str]] = [
        (WS_A, _at(DAY1, 1), "a1"), (WS_A, _at(DAY1, 2), "a2"),
        (None, _at(DAY1, 3), "n1"),
        (WS_A, _at(DAY2, 1), "a3"), (None, _at(DAY2, 2), "n2"),
        (WS_A, _at(DAY2, 3), "a4"), (WS_B, _at(DAY2, 4), "b1"),
    ]
    ids: dict[str, str] = {}
    for ws, when, tag in rows:
        ids[tag] = str(await admin.fetchval(
            "INSERT INTO audit.audit_ledger (workspace_id, actor_kind, action_type, payload, created_at) "
            "VALUES ($1::uuid, 'system', $2, jsonb_build_object('tag', $3::text), $4::text::timestamptz) RETURNING id",
            ws, ACTION, tag, when,
        ))
    try:
        yield ids
    finally:
        await _scrub(admin)


async def _breaks(conn: asyncpg.Connection, start: str, end: str) -> list[asyncpg.Record]:
    return await conn.fetch(
        "SELECT audit_id::text AS id, workspace_id::text AS ws, stored_prev, expected_prev "
        "FROM audit.verify_hash_chain($1::text::timestamptz, $2::text::timestamptz) "
        "WHERE created_at >= '2031-05-01' ORDER BY created_at",
        _at(start, 0), _at(end, 0),
    )


async def _tamper(conn: asyncpg.Connection, sql: str, *args: Any) -> None:
    async with conn.transaction():
        await conn.execute("SET LOCAL session_replication_role = replica")
        await conn.execute(sql, *args)


@pg
async def test_a_clean_ledger_has_no_false_break_at_the_window_edge(
    admin: asyncpg.Connection, ledger: dict[str, str],
) -> None:
    """Day 2 alone: chains A and NULL both have a predecessor on day 1. The old
    verifier returned both of their first rows."""
    assert await _breaks(admin, DAY2, DAY3) == []
    # ... and a window that contains the whole history was never a problem.
    assert await _breaks(admin, DAY1, DAY3) == []


@pg
async def test_a_chain_with_no_history_still_expects_a_null_parent(
    admin: asyncpg.Connection, ledger: dict[str, str],
) -> None:
    """B's first row has no predecessor anywhere. Forge a previous_hash on it:
    the seed is NULL, so it must be flagged."""
    await _tamper(admin, "UPDATE audit.audit_ledger SET previous_hash = '\\xdeadbeef' WHERE id = $1::uuid", ledger["b1"])
    broken = await _breaks(admin, DAY2, DAY3)
    assert [r["id"] for r in broken] == [ledger["b1"]]
    assert broken[0]["expected_prev"] is None


@pytest.mark.parametrize("victim,chain_head", [("a2", "a3"), ("n1", "n2")])
@pg
async def test_tampering_with_the_pre_window_parent_is_caught_at_the_first_in_window_row(
    admin: asyncpg.Connection, ledger: dict[str, str], victim: str, chain_head: str,
) -> None:
    """The seed is a real lookup, not a pass: change the parent's hash and the
    child's stored previous_hash no longer matches it."""
    await _tamper(admin, "UPDATE audit.audit_ledger SET hash = '\\xdeadbeef' WHERE id = $1::uuid", ledger[victim])

    broken = await _breaks(admin, DAY2, DAY3)

    assert [r["id"] for r in broken] == [ledger[chain_head]]
    assert bytes(broken[0]["expected_prev"]) == bytes.fromhex("deadbeef")
    assert bytes(broken[0]["stored_prev"]) != bytes.fromhex("deadbeef")


@pg
async def test_a_tampered_payload_inside_the_window_is_still_caught(
    admin: asyncpg.Connection, ledger: dict[str, str],
) -> None:
    await _tamper(
        admin, "UPDATE audit.audit_ledger SET payload = '{\"tag\": \"forged\"}' WHERE id = $1::uuid", ledger["a4"],
    )
    assert [r["id"] for r in await _breaks(admin, DAY2, DAY3)] == [ledger["a4"]]


@pg
async def test_a_deleted_row_breaks_the_chain_it_was_in(admin: asyncpg.Connection, ledger: dict[str, str]) -> None:
    """Remove a1 (day 1): a2 then hangs off a row that is gone, so a day-1 walk
    flags a2. (The day-2 walk is unaffected; day 2 starts at a3, parent a2.)"""
    await _tamper(admin, "DELETE FROM audit.audit_ledger WHERE id = $1::uuid", ledger["a1"])
    assert ledger["a2"] in [r["id"] for r in await _breaks(admin, DAY1, DAY2)]
    assert await _breaks(admin, DAY2, DAY3) == []


@pg
async def test_run_verification_records_clean_for_a_clean_window(
    admin: asyncpg.Connection, ledger: dict[str, str],
) -> None:
    run_id = await admin.fetchval(
        "SELECT audit.run_verification($1::text::timestamptz, $2::text::timestamptz, NULL)", _at(DAY2, 0), _at(DAY3, 0),
    )
    row = await admin.fetchrow(
        "SELECT status, rows_verified, broken_ids FROM audit.audit_ledger_verification_runs WHERE id = $1", run_id,
    )
    assert row["status"] == "clean"
    assert row["broken_ids"] is None


@pg
async def test_the_workflow_is_quiet_when_clean_and_raises_the_marker_on_a_break(
    admin: asyncpg.Connection, ledger: dict[str, str], monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(alv, "_build_dsn", lambda *a, **k: PG_DSN)
    monkeypatch.setattr("app.services.laravel_bridge.post_admin_surface_updated", AsyncMock())
    window = alv.AuditVerifyInput(
        start_at=datetime(2031, 5, 2, tzinfo=UTC), end_at=datetime(2031, 5, 3, tzinfo=UTC),
    )

    with caplog.at_level(logging.ERROR, logger="georag.hatchet.audit_ledger_verify"):
        clean = await alv.run_verification.aio_mock_run(window)
    assert clean.status == "clean"
    assert not [r for r in caplog.records if alv.AUDIT_CHAIN_BREAK_MARKER in r.getMessage()]

    await _tamper(admin, "UPDATE audit.audit_ledger SET hash = '\\xdeadbeef' WHERE id = $1::uuid", ledger["a2"])
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="georag.hatchet.audit_ledger_verify"):
        broken = await alv.run_verification.aio_mock_run(window)

    assert broken.status == "break"
    lines = [r.getMessage() for r in caplog.records if alv.AUDIT_CHAIN_BREAK_MARKER in r.getMessage()]
    assert len(lines) == 1
    assert f"run_id={broken.run_id}" in lines[0] and "status=break" in lines[0]
    assert ledger["a3"] in lines[0], "the first broken row is named"
