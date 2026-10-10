"""§11.10 — unit tests for the cold-tier archive workflow + admin endpoints."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.hatchet_workflows import cold_tier_archive as cta
from app.routers import admin_tier234 as t


# ---------------------------------------------------------------------------
# Workflow registration + schedule contract
# ---------------------------------------------------------------------------
def test_cold_tier_archive_workflow_registered() -> None:
    assert cta.cold_tier_archive_workflow is not None
    assert cta.cold_tier_archive_workflow.name == "cold_tier_archive"


def test_cold_tier_archive_in_ai_pool() -> None:
    from app.hatchet_workflows.worker import POOLS
    names = {wf.name for wf in POOLS["ai"]}
    assert "cold_tier_archive" in names


# ---------------------------------------------------------------------------
# Input model contracts — locked defaults from kickoff
# ---------------------------------------------------------------------------
def test_cold_tier_input_defaults_match_kickoff_lock() -> None:
    inp = cta.ColdTierArchiveInput()
    assert inp.retention_days == 90  # 30/90/indef policy
    assert inp.archive_bucket == "audit-cold-tier"
    assert inp.chunk_rows == 10_000
    assert inp.workspace_id_scope is None


def test_cold_tier_input_validates_retention_bounds() -> None:
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        cta.ColdTierArchiveInput(retention_days=0)
    with pytest.raises(ValidationError):
        cta.ColdTierArchiveInput(retention_days=3651)


def test_cold_tier_input_validates_chunk_rows_bounds() -> None:
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        cta.ColdTierArchiveInput(chunk_rows=99)
    with pytest.raises(ValidationError):
        cta.ColdTierArchiveInput(chunk_rows=1_000_001)


def test_cold_tier_output_round_trip() -> None:
    out = cta.ColdTierArchiveOutput(
        status="completed", rows_archived=42,
        cold_tier_uri="s3://audit-cold-tier/2026/...",
        hot_tier_remaining=0, verification_passed=True,
        manifest_key="2026/05/16T040000Z/manifest.json",
        duration_s=12.3,
    )
    d = out.model_dump()
    assert d["rows_archived"] == 42
    assert d["verification_passed"] is True


# ---------------------------------------------------------------------------
# SeaweedFS cold-tier store — protocol shape
# ---------------------------------------------------------------------------
def test_seaweedfs_cold_tier_store_protocol_compatible() -> None:
    """The _SeaweedFsColdTierStore must satisfy the _ColdTierStore Protocol
    (one async put method). The class itself takes a bucket arg."""
    store = cta._SeaweedFsColdTierStore("audit-cold-tier")
    assert hasattr(store, "put")
    assert callable(store.put)


# ---------------------------------------------------------------------------
# Admin router — backups_router contract
# ---------------------------------------------------------------------------
def test_backups_router_mounted() -> None:
    assert t.backups_router.prefix == "/api/v1/admin/backups"


def test_backups_router_in_module_all() -> None:
    assert "backups_router" in t.__all__


def test_snapshot_run_model_minimum_fields() -> None:
    r = t.SnapshotRun(
        run_id="abc",
        store="postgres",
        started_at=datetime.now(tz=UTC),
        status="running",
    )
    assert r.bytes is None
    assert r.payload == {}


def test_snapshot_run_model_rejects_bad_status() -> None:
    """No constraint at the model level — Pydantic accepts any str. But
    the SQL CHECK constraint enforces the enum. Document that contract."""
    r = t.SnapshotRun(
        run_id="abc",
        store="postgres",
        started_at=datetime.now(tz=UTC),
        status="not-a-real-status",
    )
    # Doesn't raise — the server-side write would, the read path is permissive
    assert r.status == "not-a-real-status"


def test_cold_tier_run_model_minimum_fields() -> None:
    r = t.ColdTierRun(
        audit_id="abc",
        action_type="audit.cold_tier.archive.completed",
        rows_archived=0,
        cold_tier_uri="",
        verification_passed=True,
        created_at=datetime.now(tz=UTC),
    )
    assert r.payload == {}


# ---------------------------------------------------------------------------
# The run: watermark in, failure out (2026-10 Hatchet audit, finding 9)
# ---------------------------------------------------------------------------
from app.audit.cold_tier_archive import ArchiveRun  # noqa: E402
from app.hatchet_workflows.audit_ledger_verify import AUDIT_CHAIN_BREAK_MARKER  # noqa: E402

_WATERMARK = datetime(2026, 7, 1, tzinfo=UTC)


class _Conn:
    def __init__(self, watermark: datetime | None = _WATERMARK, *, fail: bool = False) -> None:
        self.watermark = watermark
        self.fail = fail
        self.fetchval_calls: list[tuple[str, tuple]] = []

    async def fetchval(self, sql: str, *args):
        self.fetchval_calls.append((sql, args))
        if self.fail:
            raise RuntimeError("permission denied for table audit_ledger")
        return self.watermark

    async def close(self) -> None:
        return None


@pytest.fixture
def archive_run(monkeypatch):
    """Drive run_archive with a fake connection, archive_window and anchor writer."""
    seen: dict = {"anchors": [], "window": None, "conn": None}

    async def _noop(**_kw):
        return None

    monkeypatch.setattr("app.services.laravel_bridge.post_admin_surface_updated", _noop)

    async def _run(*, window_result: ArchiveRun, watermark: datetime | None = _WATERMARK, fail_watermark: bool = False):
        conn = _Conn(watermark, fail=fail_watermark)
        seen["conn"] = conn

        async def _connect(*_a, **_kw):
            return conn

        async def _archive_window(_conn, **kwargs):
            seen["window"] = kwargs
            return window_result

        async def _emit(_conn, **kwargs):
            seen["anchors"].append(kwargs)

        monkeypatch.setattr(cta.asyncpg, "connect", _connect)
        monkeypatch.setattr(cta, "archive_window", _archive_window)
        monkeypatch.setattr(cta, "emit_audit", _emit)
        return await cta.run_archive.aio_mock_run(cta.ColdTierArchiveInput())

    return seen, _run


def _ok(rows: int = 3) -> ArchiveRun:
    return ArchiveRun(
        rows_archived=rows, cold_tier_uri="s3://b/m.json", hot_tier_remaining=9,
        verification_passed=True, manifest_key="b/m.json", chunks=({"rows": rows},),
        chain_heads={"ws-A": "0a", "system": "0b"},
    )


async def test_run_archive_continues_from_the_last_completed_run(archive_run) -> None:
    seen, run = archive_run

    out = await run(window_result=_ok())

    assert out.status == "completed" and out.rows_archived == 3
    assert seen["window"]["cutoff_after"] == _WATERMARK
    (anchor,) = seen["anchors"]
    assert anchor["action_type"] == "audit.cold_tier.archive.completed"
    assert anchor["payload"]["window_start"] == _WATERMARK.isoformat()
    assert anchor["payload"]["chains"] == 2
    # ...and that anchor is what the next run reads its watermark from.
    sql, args = seen["conn"].fetchval_calls[0]
    assert "audit.cold_tier.archive.completed" in sql and "max(" in sql
    assert args == (None,)


async def test_run_archive_with_no_earlier_run_archives_from_the_beginning(archive_run) -> None:
    seen, run = archive_run

    await run(window_result=_ok(), watermark=None)

    assert seen["window"]["cutoff_after"] is None
    assert seen["anchors"][0]["payload"]["window_start"] is None


async def test_an_unreadable_watermark_archives_more_not_less(archive_run, caplog) -> None:
    seen, run = archive_run

    with caplog.at_level("WARNING", logger="georag.hatchet.cold_tier_archive"):
        out = await run(window_result=_ok(), fail_watermark=True)

    assert out.status == "completed"
    assert seen["window"]["cutoff_after"] is None
    assert any("could not read the watermark" in r.getMessage() for r in caplog.records)


async def test_a_verification_failure_fails_the_run_and_raises_the_chain_break_alarm(
    archive_run, caplog,
) -> None:
    """It used to return status='failed' as an ordinary result, so Hatchet
    recorded a green run for a ledger that had failed its own hash chain."""
    seen, run = archive_run
    failed = ArchiveRun(
        rows_archived=12, cold_tier_uri="", hot_tier_remaining=9, verification_passed=False,
        failure_reason="chain break in ws-B at id=row-7 created_at=x: previous_hash='ff' != prior.hash='07'",
    )

    with caplog.at_level("ERROR", logger="georag.hatchet.cold_tier_archive"), pytest.raises(
        RuntimeError, match="verification failed, nothing archived.*chain break in ws-B",
    ):
        await run(window_result=failed)

    (anchor,) = seen["anchors"]
    assert anchor["action_type"] == "audit.cold_tier.archive.failed"
    assert anchor["payload"]["verification_passed"] is False
    lines = [r.getMessage() for r in caplog.records if AUDIT_CHAIN_BREAK_MARKER in r.getMessage()]
    assert len(lines) == 1
    assert lines[0].startswith(AUDIT_CHAIN_BREAK_MARKER)
    assert "source=cold_tier_archive" in lines[0] and "chain break in ws-B" in lines[0]


async def test_a_run_that_raises_does_not_advance_the_watermark(archive_run) -> None:
    """The watermark is read from COMPLETED anchors only; a failure writes a
    .failed one, so the next night retries the same window."""
    seen, run = archive_run

    with pytest.raises(RuntimeError):
        await run(window_result=ArchiveRun(
            rows_archived=1, cold_tier_uri="", hot_tier_remaining=0,
            verification_passed=False, failure_reason="chain break in x",
        ))

    assert [a["action_type"] for a in seen["anchors"]] == ["audit.cold_tier.archive.failed"]
    sql, _ = seen["conn"].fetchval_calls[0]
    assert "archive.completed" in sql and "archive.failed" not in sql
