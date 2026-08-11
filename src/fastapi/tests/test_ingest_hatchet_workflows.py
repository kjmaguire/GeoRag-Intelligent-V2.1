"""Hatchet workflow tests for the Phase C/D ingest wrappers.

Doc-phase 183.

Covers:
  - embed_pending_passages: input validation, cron schedule, mock run with empty filter

sync_silver_to_kg coverage removed 2026-07-28 (B1) — the workflow (and the
kg_sync service it wrapped) was deleted along with Neo4j.

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

# ─────────────────────── embed_pending_passages ───────────────────

def test_embed_pending_passages_default_input():
    inp = EmbedPendingPassagesInput(
        workspace_id="a0000000-0000-0000-0000-000000000001",
    )
    assert inp.workspace_id == "a0000000-0000-0000-0000-000000000001"
    assert inp.project_id == "*"
    # Default embed batch_size deliberately raised 32 → 64 (2026-08 weekend perf work).
    assert inp.batch_size == 64
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
