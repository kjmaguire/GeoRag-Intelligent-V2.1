"""Hatchet workflow tests for the Phase C/D ingest wrappers.

Doc-phase 183.

Covers:
  - sync_silver_to_kg: input validation, cron schedule, mock run with '*' project filter
  - embed_pending_passages: input validation, cron schedule, mock run with empty filter

Uses `aio_mock_run` pattern (Hatchet's public test API for task bodies).
"""
from __future__ import annotations

import pytest

from app.hatchet_workflows.embed_pending_passages import (
    EmbedPendingPassagesInput,
    embed_pending_passages_wf,
)
from app.hatchet_workflows.embed_pending_passages import (
    run as embed_pending_passages_run,
)
from app.hatchet_workflows.sync_silver_to_kg import (
    SyncSilverToKGInput,
    sync_silver_to_kg,
)
from app.hatchet_workflows.sync_silver_to_kg import (
    run as sync_silver_to_kg_run,
)

# ───────────────────────── sync_silver_to_kg ──────────────────────

def test_sync_silver_to_kg_default_input():
    """Empty input fires `*` (all projects) by default."""
    inp = SyncSilverToKGInput()
    assert inp.project_id == "*"
    assert inp.bust_cache is True


def test_sync_silver_to_kg_input_accepts_uuid_string():
    inp = SyncSilverToKGInput(
        project_id="762b147e-af53-4593-b569-04ee46f31d97",
        bust_cache=False,
    )
    assert inp.project_id == "762b147e-af53-4593-b569-04ee46f31d97"
    assert inp.bust_cache is False


def test_sync_silver_to_kg_cron_schedule():
    """Daily cron at 05:30 UTC. Verified so the slot can't drift."""
    cron_list = (
        getattr(sync_silver_to_kg.config, "on_crons", None)
        or getattr(sync_silver_to_kg, "on_crons", None)
    )
    assert cron_list is not None
    assert "30 5 * * *" in cron_list


@pytest.mark.asyncio
async def test_sync_silver_to_kg_runs_against_real_projects():
    """`*` filter walks all silver.projects and pushes to Neo4j.

    Live test — exercises the full path. Cameco project should have
    been seeded by the Phase B + C overnight run.
    """
    inp = SyncSilverToKGInput(project_id="*", bust_cache=False)
    out = await sync_silver_to_kg_run.aio_mock_run(inp)
    # At minimum, the Cameco project from doc-phase 180 should sync.
    assert out.projects_synced >= 1
    # Cameco contributes >= 60 nodes (1 project + 63 drillholes + ...)
    assert out.total_nodes >= 60
    # Relationships >= 60 (one HAS_HOLE per drillhole)
    assert out.total_relationships >= 60


# ─────────────────────── embed_pending_passages ───────────────────

def test_embed_pending_passages_default_input():
    inp = EmbedPendingPassagesInput()
    assert inp.workspace_id == "a0000000-0000-0000-0000-000000000001"
    assert inp.project_id == "*"
    assert inp.batch_size == 32
    assert inp.max_passages is None


def test_embed_pending_passages_cron_schedule():
    """Daily cron at 05:45 UTC — 15 min after sync_silver_to_kg."""
    cron_list = (
        getattr(embed_pending_passages_wf.config, "on_crons", None)
        or getattr(embed_pending_passages_wf, "on_crons", None)
    )
    assert cron_list is not None
    assert "45 5 * * *" in cron_list


@pytest.mark.asyncio
async def test_embed_pending_passages_unknown_project_returns_zero():
    """A project_id that has no passages → zero counts.

    Uses a deterministic fake UUID that won't match any silver row.
    """
    inp = EmbedPendingPassagesInput(
        workspace_id="a0000000-0000-0000-0000-000000000001",
        project_id="ffffffff-ffff-ffff-ffff-ffffffffffff",
    )
    out = await embed_pending_passages_run.aio_mock_run(inp)
    assert out.total_seen == 0
    assert out.total_embedded == 0
    assert out.total_upserted == 0
