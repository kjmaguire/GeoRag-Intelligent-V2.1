"""sync_silver_to_kg Hatchet workflow (§04i Layer 4 enablement).

Doc-phase 183 — Phase E.1 Track 3.

Wraps `app.services.ingest.kg_sync.sync_silver_project_to_neo4j` as a
Hatchet workflow so cluster ingests can trigger KG population without
out-of-band Python scripts.

Manual invocation:
  sync_silver_to_kg.run({"project_id": "<uuid>"})

Cron-fire (when project_id="*"): walks all silver.projects and syncs
each. Useful for nightly refresh + cold-start KG hydration.

The workflow:
  1. Resolves the project_id (or walks all)
  2. Opens an asyncpg connection
  3. Calls `sync_silver_project_to_neo4j` per project
  4. Returns counts per label + total relationships
  5. (Future) busts the Redis cache key the orchestrator uses
"""
from __future__ import annotations

import logging
import os
from typing import Any

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.hatchet_workflows import hatchet
from app.services.ingest.kg_sync import sync_silver_project_to_neo4j

log = logging.getLogger("georag.hatchet.sync_silver_to_kg")


class SyncSilverToKGInput(BaseModel):
    """Input — single project_id or '*' for all projects."""

    project_id: str = Field(
        default="*",
        description="Silver project_id (UUID) or '*' to sync all projects.",
    )
    bust_cache: bool = Field(
        default=True,
        description="If True, clear the orchestrator's Redis entity cache "
                    "after sync so freshly-pushed entities are picked up.",
    )


class SyncSilverToKGOutput(BaseModel):
    projects_synced: int
    total_nodes: int = 0
    total_relationships: int = 0
    errors: list[str] = Field(default_factory=list)


def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


async def _bust_entity_cache(project_ids: list[str]) -> None:
    """Clear `georag:graph_entities:v1:{project_id}` keys so the
    orchestrator re-queries Neo4j on next request."""
    try:
        import redis.asyncio as aioredis
        host = os.environ.get("REDIS_HOST", "redis")
        port = os.environ.get("REDIS_PORT", "6379")
        password = os.environ.get("REDIS_PASSWORD", "")
        url = (
            f"redis://:{password}@{host}:{port}/0" if password
            else f"redis://{host}:{port}/0"
        )
        client = aioredis.from_url(url, decode_responses=True)
        try:
            keys = [f"georag:graph_entities:v1:{pid}" for pid in project_ids]
            if keys:
                await client.delete(*keys)
                log.info("kg_sync.cache_busted keys=%d", len(keys))
        finally:
            await client.aclose()
    except Exception as e:
        log.warning("kg_sync.cache_bust_failed err=%s", e)


sync_silver_to_kg = hatchet.workflow(
    name="sync_silver_to_kg",
    # Doc-phase 183 — daily KG refresh at 05:30 UTC (15 min after the
    # eval_real_rag_nightly cron at 05:15). Ensures any silver
    # additions from the last 24h get pushed to Neo4j before the eval
    # cron runs again.
    on_crons=["30 5 * * *"],
    input_validator=SyncSilverToKGInput,
)


@sync_silver_to_kg.task(execution_timeout="30m", retries=0)
async def run(
    input: SyncSilverToKGInput, ctx: Context
) -> SyncSilverToKGOutput:
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        if input.project_id == "*":
            # Block-2 RLS (2026-05-15): silver.projects is now workspace-scoped.
            # Loop through every workspace, set GUC, harvest project_ids.
            ws_rows = await conn.fetch(
                "SELECT workspace_id::text AS wid FROM silver.workspaces"
            )
            # (project_id, workspace_id) tuples — pre-resolved so the
            # downstream kg_sync call sets GUC and finds the project.
            project_pairs: list[tuple[str, str]] = []
            for ws_row in ws_rows:
                await conn.execute(
                    "SELECT set_config('app.workspace_id', $1, false)",
                    ws_row["wid"],
                )
                rows = await conn.fetch(
                    "SELECT project_id::text AS pid FROM silver.projects"
                )
                project_pairs.extend((r["pid"], ws_row["wid"]) for r in rows)
            project_ids = [pid for pid, _ in project_pairs]
            project_workspace_map = dict(project_pairs)
        else:
            project_ids = [input.project_id]
            project_workspace_map = {}

        log.info("sync_silver_to_kg.start projects=%d", len(project_ids))

        total_nodes = 0
        total_rels = 0
        errors: list[str] = []
        synced: list[str] = []

        for pid in project_ids:
            try:
                # Pre-set GUC for projects we resolved via the workspace
                # loop so kg_sync's silver.projects lookup hits the right
                # tenant slice.
                wid = project_workspace_map.get(pid)
                if wid:
                    await conn.execute(
                        "SELECT set_config('app.workspace_id', $1, false)", wid,
                    )
                r = await sync_silver_project_to_neo4j(conn, project_id=pid)
                total_nodes += (
                    r.project_node_count + r.drillhole_node_count +
                    r.formation_node_count + r.deposit_node_count +
                    r.report_node_count
                )
                total_rels += r.relationships
                errors.extend(r.errors)
                synced.append(pid)
            except Exception as e:
                errors.append(f"project={pid}:{type(e).__name__}:{e}")
                log.warning("sync_silver_to_kg.project_failed pid=%s err=%s", pid, e)

        if input.bust_cache and synced:
            await _bust_entity_cache(synced)

        log.info(
            "sync_silver_to_kg.complete projects=%d nodes=%d rels=%d errors=%d",
            len(synced), total_nodes, total_rels, len(errors),
        )
        return SyncSilverToKGOutput(
            projects_synced=len(synced),
            total_nodes=total_nodes,
            total_relationships=total_rels,
            errors=errors,
        )
    finally:
        await conn.close()


__all__ = ["sync_silver_to_kg", "SyncSilverToKGInput", "SyncSilverToKGOutput"]
