"""A platform outbox row that dead-letters must be seen (2026-10 Hatchet audit, #21).

The tenant-isolation auditor reports a cross-tenant leak by enqueueing an
``external_webhook`` row on the ``security_critical`` channel with
``workspace_id`` NULL. In AWS no ``EXTERNAL_WEBHOOK_URL_*`` or HMAC secret is
provisioned, so the row dead-letters on its first attempt. The dispatcher's only
durable record of a dead letter is a ``silver.store_reconciliation_findings`` row,
whose ``workspace_id`` is NOT NULL, so a row with no workspace got an attempt row
and an audit anchor and nothing anyone looks at: the alarm for a tenancy leak
failed to ring, and said nothing about it.

Now: ``OUTBOX_PLATFORM_DEAD_LETTER`` is logged (after the commit) and a finding is
filed under the platform workspace. A workspace row is unchanged. Needs the
migrated outbox, findings and audit tables and a superuser login; skips otherwise.
"""
from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import asyncpg
import pytest

from app.hatchet_workflows import outbox_dispatcher as ob

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

PLATFORM = "a0000000-0000-0000-0000-000000000001"
LOGGER = "georag.hatchet.outbox_dispatcher"


@pytest.fixture
async def admin():  # noqa: ANN201
    try:
        conn = await asyncpg.connect(PG_DSN, timeout=5)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no Postgres at PG_DSN: {exc}")
    try:
        ok = await conn.fetchval(
            "SELECT to_regclass('outbox.pending_propagations') IS NOT NULL "
            "AND to_regclass('silver.store_reconciliation_findings') IS NOT NULL "
            "AND to_regclass('audit.audit_ledger') IS NOT NULL "
            "AND (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)"
        )
        if not ok:
            pytest.skip("needs the migrated outbox, findings and audit tables and a superuser PG_DSN")
        await conn.execute(
            "INSERT INTO silver.workspaces (workspace_id, name, slug) VALUES ($1::uuid, 'platform', 'platform') "
            "ON CONFLICT (workspace_id) DO NOTHING", PLATFORM,
        )
        yield conn
    finally:
        await conn.close()


@pytest.fixture
async def pool(admin):  # noqa: ANN201
    p = await asyncpg.create_pool(PG_DSN, min_size=1, max_size=2, statement_cache_size=0)
    try:
        yield p
    finally:
        await p.close()


@pytest.fixture(autouse=True)
def _no_webhook_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The AWS state: nothing provisions a webhook channel."""
    for name in list(os.environ):
        if name.startswith("EXTERNAL_WEBHOOK_"):
            monkeypatch.delenv(name, raising=False)
    ob._TARGET_SEMAPHORES.clear()


@pytest.fixture
async def workspace(admin):  # noqa: ANN201
    ws = str(uuid.uuid4())
    await admin.execute(
        "INSERT INTO silver.workspaces (workspace_id, name, slug) VALUES ($1::uuid, $2, $2)", ws, f"dl-{ws[:8]}",
    )
    try:
        yield ws
    finally:
        await _scrub(admin, workspace_id=ws)
        await admin.execute("DELETE FROM silver.workspaces WHERE workspace_id = $1::uuid", ws)


async def _scrub(admin: asyncpg.Connection, *, workspace_id: str | None = None) -> None:
    async with admin.transaction():
        await admin.execute("SET LOCAL session_replication_role = replica")
        await admin.execute(
            "DELETE FROM silver.store_reconciliation_findings WHERE discovered_by = 'outbox_dispatcher' "
            "AND details->>'propagation_id' IN (SELECT id::text FROM outbox.pending_propagations "
            "                                    WHERE idempotency_key LIKE 'dl-test:%')"
        )
        await admin.execute(
            "DELETE FROM audit.audit_ledger WHERE target_id IN (SELECT id::text FROM outbox.pending_propagations "
            "WHERE idempotency_key LIKE 'dl-test:%')"
        )
        await admin.execute(
            "DELETE FROM outbox.propagation_attempts WHERE propagation_id IN "
            "(SELECT id FROM outbox.pending_propagations WHERE idempotency_key LIKE 'dl-test:%')"
        )
        await admin.execute("DELETE FROM outbox.pending_propagations WHERE idempotency_key LIKE 'dl-test:%'")
        if workspace_id:
            await admin.execute("DELETE FROM silver.store_reconciliation_findings WHERE workspace_id = $1::uuid", workspace_id)


@pytest.fixture(autouse=True)
async def _clean(admin):  # noqa: ANN201
    await _scrub(admin)
    yield
    await _scrub(admin)


async def _claimed_row(admin: asyncpg.Connection, workspace_id: str | None, channel: str) -> asyncpg.Record:
    """An outbox row as the dispatcher holds it after the claim: in_flight."""
    return await admin.fetchrow(
        "INSERT INTO outbox.pending_propagations (workspace_id, source_schema, source_table, source_id, "
        "target_store, target_collection, operation, payload, idempotency_key, status, last_attempted_at) "
        "VALUES ($1::uuid, 'audit', 'tenant_isolation', '2031-06-01', 'external_webhook', $2, 'upsert', "
        "$3::jsonb, $4, 'in_flight', now()) "
        "RETURNING id, workspace_id, source_schema, source_table, source_id, target_store, target_collection, "
        "operation, payload, idempotency_key, target_store_concurrency_hint",
        workspace_id, channel, '{"severity": "critical", "violations": 2}', f"dl-test:{uuid.uuid4()}",
    )


async def _state(admin: asyncpg.Connection, row: asyncpg.Record) -> dict[str, Any]:
    status = await admin.fetchval("SELECT status FROM outbox.pending_propagations WHERE id = $1", row["id"])
    findings = await admin.fetch(
        "SELECT workspace_id::text AS ws, severity, drift_type, target_store, details "
        "FROM silver.store_reconciliation_findings WHERE details->>'propagation_id' = $1", str(row["id"]),
    )
    attempts = await admin.fetchval(
        "SELECT count(*) FROM outbox.propagation_attempts WHERE propagation_id = $1", row["id"],
    )
    return {"status": status, "findings": findings, "attempts": attempts}


def _markers(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if ob.OUTBOX_PLATFORM_DEAD_LETTER_MARKER in r.getMessage()]


async def test_a_platform_security_row_with_no_webhook_is_a_visible_dead_letter(
    admin, pool, caplog: pytest.LogCaptureFixture,
) -> None:
    row = await _claimed_row(admin, None, "security_critical")

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        outcome = await ob._dispatch_one(pool, row, dead_letter_after=3)

    assert outcome == "dead_lettered"
    state = await _state(admin, row)
    assert (state["status"], state["attempts"]) == ("dead_lettered", 1)
    (finding,) = state["findings"]
    assert finding["ws"] == PLATFORM
    assert (finding["severity"], finding["drift_type"], finding["target_store"]) == (
        "critical", "outbox_dead_letter", "external_webhook",
    )
    assert '"scope": "platform"' in finding["details"]
    assert "SECURITY_CRITICAL" in finding["details"], "the last error names the channel nothing configures"

    (line,) = _markers(caplog)
    assert line.startswith(ob.OUTBOX_PLATFORM_DEAD_LETTER_MARKER)
    assert str(row["id"]) in line and "channel=security_critical" in line
    assert "no external webhook configured" in line


async def test_the_marker_is_logged_once_per_dead_letter_not_per_attempt(
    admin, pool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """A webhook that is configured but down is a transient failure first. No
    marker, no finding, until the row finally dead-letters."""
    monkeypatch.setenv("EXTERNAL_WEBHOOK_URL_DEFAULT", "http://127.0.0.1:9/hook")  # nothing listens
    row = await _claimed_row(admin, None, "default")

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        first = await ob._dispatch_one(pool, row, dead_letter_after=2)
    assert first == "transient_failure"
    assert _markers(caplog) == []
    assert (await _state(admin, row))["findings"] == []

    await admin.execute("UPDATE outbox.pending_propagations SET status = 'in_flight' WHERE id = $1", row["id"])
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        second = await ob._dispatch_one(pool, row, dead_letter_after=2)
    assert second == "dead_lettered"
    assert len(_markers(caplog)) == 1
    state = await _state(admin, row)
    assert (state["status"], len(state["findings"]), state["findings"][0]["severity"]) == ("dead_lettered", 1, "high")


async def test_a_workspace_row_is_unchanged_filed_under_its_own_workspace_with_no_marker(
    admin, pool, workspace: str, caplog: pytest.LogCaptureFixture,
) -> None:
    row = await _claimed_row(admin, workspace, "support_packet")

    with caplog.at_level(logging.ERROR, logger=LOGGER):
        outcome = await ob._dispatch_one(pool, row, dead_letter_after=3)

    assert outcome == "dead_lettered"
    (finding,) = (await _state(admin, row))["findings"]
    assert (finding["ws"], finding["severity"]) == (workspace, "medium")
    assert '"scope"' not in finding["details"]
    assert _markers(caplog) == []


async def test_a_finding_that_cannot_be_written_does_not_undo_the_dead_letter(
    admin, pool, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """If the platform workspace cannot take the finding (no such row), the
    transition still commits, or the row would be reclaimed and fail forever,
    and the marker still goes out."""
    monkeypatch.setattr(ob, "LEGACY_DEFAULT_TENANT_UUID", str(uuid.uuid4()))
    row = await _claimed_row(admin, None, "security_critical")

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        outcome = await ob._dispatch_one(pool, row, dead_letter_after=3)

    assert outcome == "dead_lettered"
    state = await _state(admin, row)
    assert (state["status"], state["attempts"], state["findings"]) == ("dead_lettered", 1, [])
    assert len(_markers(caplog)) == 1
    assert any("could not file the platform dead-letter finding" in r.getMessage() for r in caplog.records)
