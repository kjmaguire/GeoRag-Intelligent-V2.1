"""§11.10 — unit tests for the cold-tier archive workflow + admin endpoints."""
from __future__ import annotations

from datetime import datetime, timezone

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
        started_at=datetime.now(tz=timezone.utc),
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
        started_at=datetime.now(tz=timezone.utc),
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
        created_at=datetime.now(tz=timezone.utc),
    )
    assert r.payload == {}
