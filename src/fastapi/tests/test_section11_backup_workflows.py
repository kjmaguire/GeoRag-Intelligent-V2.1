"""§11.1 — unit tests for the backup_* workflows.

backup_neo4j was removed 2026-08-19. Neo4j itself was dropped entirely
2026-07-28 (B1), the workflow was unregistered from the ai pool shortly
after, and the module it kept alive shelled out via `docker exec` to a
container that no longer exists — and never could on Azure Container Apps
regardless, which has no docker daemon access from inside a Container App.
The remaining four backup workflows are unaffected.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.hatchet_workflows import backup_postgres as bpg
from app.hatchet_workflows import backup_qdrant as bqd
from app.hatchet_workflows import backup_redis as brd
from app.hatchet_workflows import backup_seaweedfs as bsw


# ---------------------------------------------------------------------------
# Schedule contract — kickoff locked staggered 02:00/02:15
# ---------------------------------------------------------------------------
def test_backup_postgres_workflow_registered() -> None:
    assert bpg.backup_postgres is not None
    assert bpg.backup_postgres.name == "backup_postgres"


def test_backup_qdrant_workflow_registered() -> None:
    assert bqd.backup_qdrant is not None
    assert bqd.backup_qdrant.name == "backup_qdrant"


def test_backup_redis_workflow_registered() -> None:
    assert brd.backup_redis is not None
    assert brd.backup_redis.name == "backup_redis"


def test_backup_seaweedfs_workflow_registered() -> None:
    assert bsw.backup_seaweedfs is not None
    assert bsw.backup_seaweedfs.name == "backup_seaweedfs"


# ---------------------------------------------------------------------------
# Object-key contract — keys must sort lexicographically by time
# ---------------------------------------------------------------------------
def test_postgres_object_key_layout() -> None:
    when = datetime(2026, 5, 16, 2, 0, 7, tzinfo=UTC)
    key = bpg._build_object_key(
        "postgres", "11111111-1111-1111-1111-111111111111", when,
    )
    assert key == "postgres/2026/05/16/020007-11111111-1111-1111-1111-111111111111.dump"


def test_object_keys_sort_lexicographically_by_time() -> None:
    """A listing of objects under the prefix should naturally come back
    in chronological order — i.e., zero-padded date + time components."""
    rid = "00000000-0000-0000-0000-000000000000"
    keys = [
        bpg._build_object_key("postgres", rid, datetime(2026, 1, 1, 2, 0, 0, tzinfo=UTC)),
        bpg._build_object_key("postgres", rid, datetime(2026, 1, 1, 14, 0, 0, tzinfo=UTC)),
        bpg._build_object_key("postgres", rid, datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)),
        bpg._build_object_key("postgres", rid, datetime(2027, 1, 1, 0, 0, 1, tzinfo=UTC)),
    ]
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# Input model contracts
# ---------------------------------------------------------------------------
def test_postgres_input_defaults() -> None:
    inp = bpg.BackupPostgresInput()
    assert inp.bucket == "georag-backups"
    assert inp.prefix == "postgres"


def test_postgres_input_override() -> None:
    inp = bpg.BackupPostgresInput(bucket="staging-backups", prefix="pg")
    assert inp.bucket == "staging-backups"
    assert inp.prefix == "pg"


# ---------------------------------------------------------------------------
# Output model contracts
# ---------------------------------------------------------------------------
def test_postgres_output_round_trip() -> None:
    now = datetime.now(tz=UTC)
    out = bpg.BackupPostgresOutput(
        run_id="abc",
        status="completed",
        bucket="b",
        object_key="k",
        bytes=42,
        sha256_hex="deadbeef",
        started_at=now,
        completed_at=now,
    )
    d = out.model_dump()
    assert d["bytes"] == 42
    assert d["sha256_hex"] == "deadbeef"


# ---------------------------------------------------------------------------
# Worker registration
# ---------------------------------------------------------------------------
def test_workflows_are_in_ai_pool() -> None:
    from app.hatchet_workflows.worker import POOLS
    names = {wf.name for wf in POOLS["ai"]}
    assert "backup_postgres" in names
    # backup_neo4j is gone entirely as of 2026-08-19 — unregistered from this
    # pool when Neo4j was dropped (2026-07-28, B1), then the module itself
    # deleted once it was clear nothing invoked it. Asserted by absence so a
    # re-add has to be deliberate.
    assert "backup_neo4j" not in names
    assert "backup_qdrant" in names
    assert "backup_redis" in names
    assert "backup_seaweedfs" in names


def test_all_backup_workflows_input_defaults_share_bucket() -> None:
    """Every backup workflow defaults to the SAME bucket so an operator
    rotation only has to touch one env var / config."""
    assert bpg.BackupPostgresInput().bucket == "georag-backups"
    assert bqd.BackupQdrantInput().bucket == "georag-backups"
    assert brd.BackupRedisInput().bucket == "georag-backups"
    assert bsw.BackupSeaweedFsInput().dest_bucket == "georag-backups"


def test_seaweedfs_input_distinguishes_source_and_dest() -> None:
    """SeaweedFS replication is the one workflow that reads from one
    bucket and writes to another. Source defaults to bronze."""
    inp = bsw.BackupSeaweedFsInput()
    assert inp.source_bucket == "georag-bronze"
    assert inp.dest_bucket == "georag-backups"
    assert inp.source_bucket != inp.dest_bucket


def test_qdrant_object_key_includes_collection() -> None:
    when = datetime(2026, 5, 16, 2, 30, 0, tzinfo=UTC)
    key = bqd._build_object_key(
        "qdrant", "33333333-3333-3333-3333-333333333333", when, "georag_reports",
    )
    assert key.endswith("/georag_reports.snapshot")
    assert "/2026/05/16/023000-" in key


def test_redis_object_key_uses_rdb_extension() -> None:
    when = datetime(2026, 5, 16, 2, 45, 0, tzinfo=UTC)
    key = brd._build_object_key(
        "redis", "44444444-4444-4444-4444-444444444444", when,
    )
    assert key.endswith(".rdb")


def test_seaweedfs_snapshot_prefix_layout() -> None:
    when = datetime(2026, 5, 16, 3, 0, 0, tzinfo=UTC)
    prefix = bsw._snapshot_prefix(
        "seaweedfs", "55555555-5555-5555-5555-555555555555", when,
    )
    assert prefix == "seaweedfs/2026/05/16/030000-55555555-5555-5555-5555-555555555555"


# ---------------------------------------------------------------------------
# Schema contract — the migration applied; the helpers can be imported
# without going through Hatchet
# ---------------------------------------------------------------------------
def test_record_start_completion_failure_helpers_exposed() -> None:
    """Public helpers are referenced from restore_workspace + tests; ensure
    they remain importable."""
    assert callable(bpg._record_start)
    assert callable(bpg._record_completion)
    assert callable(bpg._record_failure)


# ---------------------------------------------------------------------------
# DSN builder — direct-host bypass of pgbouncer
# ---------------------------------------------------------------------------
def test_dsn_builder_uses_direct_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_USER", "x")
    monkeypatch.setenv("POSTGRES_PASSWORD", "y")
    monkeypatch.setenv("POSTGRES_DIRECT_HOST", "pg-direct")
    monkeypatch.setenv("POSTGRES_DIRECT_PORT", "5433")
    monkeypatch.setenv("POSTGRES_DB", "georag")
    dsn = bpg._build_dsn()
    assert "@pg-direct:5433/" in dsn
    assert dsn.startswith("postgres://x:y@")
