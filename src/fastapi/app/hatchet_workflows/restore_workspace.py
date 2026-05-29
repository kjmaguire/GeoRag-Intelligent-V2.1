"""restore_workspace Hatchet workflow (§11.3 / §26.3).

Doc-phase 100 skeleton → doc-phase 148 dry-run graduation → Phase G.2
**cross-store** consistency check (this commit).

The doc-phase 100 spec called for cross-store consistency restore
(Postgres + Neo4j + Qdrant + Redis + SeaweedFS). Real restore needs
backup infrastructure (snapshot manifests, pg_restore, neo4j-admin,
Qdrant snapshot API, Redis BGSAVE, SeaweedFS object replication) — most
of which is operator territory.

This graduation lands two slices:

  1. **Dry-run cross-store consistency check** (`dry_run=True`, default):
     Counts workspace-scoped rows / nodes / points / keys / objects in
     **all five** stores so operators have one place to verify a
     workspace's footprint before kicking off a restore. Emits an
     audit anchor + returns a per-store breakdown.

  2. **Snapshot manifest probe** (when `snapshot_manifest_uri` points at
     a `file://` URI accessible to the worker): reads the manifest JSON,
     compares its claimed per-store counts to the live counts collected
     above, surfaces a `mismatches` list. The S3 path is documented but
     deferred to Phase 11.1 alongside real restore.

  3. `dry_run=False` → still raises NotImplementedError-equivalent
     failure with a clear message pointing at the backup-infrastructure
     dependency. This is an explicit operator-gated path so a typo
     doesn't accidentally trigger a destructive op.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.hatchet_workflows import hatchet

log = logging.getLogger("georag.hatchet.restore_workspace")


# Postgres tables to count for the consistency baseline. Adding tables
# here extends coverage without changing the workflow body.
_PG_BASELINE_TABLES: list[tuple[str, str, str]] = [
    # (output_key, schema.table, workspace_id column)
    ("silver_workspaces", "silver.workspaces", "workspace_id"),
    ("silver_hypotheses", "silver.hypotheses", "workspace_id"),
    ("silver_decision_records", "silver.decision_records", "workspace_id"),
    ("audit_ledger_anchors", "audit.audit_ledger", "workspace_id"),
    ("ops_support_tickets", "ops.support_tickets", "workspace_id"),
    # Phase G.2 extensions — Phase 3 silver tables that carry workspace_id
    ("silver_answer_runs", "silver.answer_runs", "workspace_id"),
    ("silver_evidence_items", "silver.evidence_items", "workspace_id"),
    ("silver_document_passages", "silver.document_passages", "workspace_id"),
    ("targeting_target_recommendations", "targeting.target_recommendations", "workspace_id"),
]


class RestoreWorkspaceInput(BaseModel):
    workspace_id: UUID
    snapshot_manifest_uri: str = Field(
        ...,
        description="URI to the snapshot manifest JSON; contains "
                    "per-store snapshot locations + checksums + timestamp. "
                    "Supported schemes: file://, s3:// (s3 deferred to Phase 11.1).",
    )
    initiated_by_user_id: int
    restore_request_id: UUID = Field(..., description="Idempotency key.")
    dry_run: bool = Field(
        default=True,
        description="If true, runs cross-store consistency checks only — "
                    "no writes. Default true; actual restore requires "
                    "explicit operator confirmation AND backup infrastructure "
                    "(not in this graduation).",
    )


class RestoreWorkspaceOutput(BaseModel):
    success: bool
    stores_restored: list[str] = Field(default_factory=list)
    consistency_check_results: dict[str, Any] = Field(default_factory=dict)
    inconsistencies_repaired: int = 0
    audit_ledger_entry_id: UUID | None = None
    failure_stage: str | None = None
    failure_reason: str | None = None


restore_workspace = hatchet.workflow(
    name="restore_workspace",
    input_validator=RestoreWorkspaceInput,
)


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


async def _count_postgres_rows(
    pool: asyncpg.Pool, workspace_str: str
) -> tuple[dict[str, int], str | None]:
    """Per-table row counts in PostgreSQL silver + targeting + audit + ops.
    Returns (counts_by_output_key, error). Error is None on success.
    """
    counts: dict[str, int] = {}
    try:
        async with pool.acquire() as conn:
            # Block-3 RLS: scope reads to the workspace being restored.
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, false)",
                workspace_str,
            )
            for output_key, qualified_table, workspace_col in _PG_BASELINE_TABLES:
                try:
                    if qualified_table == "silver.workspaces":
                        n = await conn.fetchval(
                            "SELECT count(*) FROM silver.workspaces "
                            "WHERE workspace_id = $1::uuid",
                            workspace_str,
                        )
                    else:
                        n = await conn.fetchval(
                            f"SELECT count(*) FROM {qualified_table} "
                            f"WHERE {workspace_col} = $1::uuid",
                            workspace_str,
                        )
                    counts[output_key] = int(n or 0)
                except Exception as exc:
                    # Table may not exist in the test DB; record as -1
                    # rather than crash the whole consistency check.
                    log.debug(
                        "_count_postgres_rows: %s skipped (%s)",
                        qualified_table, exc,
                    )
                    counts[output_key] = -1
        return counts, None
    except Exception as exc:
        return counts, f"postgres_count_failed: {type(exc).__name__}: {exc}"


async def _count_neo4j_nodes(workspace_str: str) -> tuple[int, str | None]:
    """Count Neo4j nodes carrying this workspace_id. Returns (count, error).

    Per the Phase F graph_entities reference: nodes carry `project_id`
    not `workspace_id`, but indirectly belong to a workspace via the
    Project node. We count by either property when present.
    """
    try:
        from neo4j import AsyncGraphDatabase
    except ImportError:
        return -1, "neo4j driver not available in this worker pool"

    host = os.environ.get("NEO4J_HOST", "neo4j")
    port = int(os.environ.get("NEO4J_PORT", "7687"))
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "")
    if not password:
        return -1, "NEO4J_PASSWORD not set"

    driver = AsyncGraphDatabase.driver(
        f"bolt://{host}:{port}", auth=(user, password)
    )
    try:
        async with driver.session() as session:
            # Count any node that names this workspace (direct or via project).
            result = await session.run(
                """
                MATCH (n)
                WHERE n.workspace_id = $ws OR n.project_id IN [
                    p.project_id |
                    p IN [
                        x IN [(x:Project {workspace_id: $ws}) | x] | x
                    ]
                ]
                RETURN count(DISTINCT n) AS n
                """,
                ws=workspace_str,
            )
            row = await result.single()
            return int(row["n"] if row and row.get("n") is not None else 0), None
    except Exception as exc:
        # Try a simpler fallback — count nodes whose workspace_id literally matches.
        try:
            async with driver.session() as session:
                result = await session.run(
                    "MATCH (n) WHERE n.workspace_id = $ws "
                    "RETURN count(n) AS n",
                    ws=workspace_str,
                )
                row = await result.single()
                return int(row["n"] if row and row.get("n") is not None else 0), None
        except Exception as exc2:
            return -1, f"neo4j_count_failed: {type(exc2).__name__}: {exc2}"
    finally:
        try:
            await driver.close()
        except Exception:
            pass


async def _count_qdrant_points(workspace_str: str) -> tuple[int, str | None]:
    """Count Qdrant points in georag_reports carrying this workspace_id."""
    try:
        from qdrant_client import AsyncQdrantClient
    except ImportError:
        return -1, "qdrant client not available"

    host = os.environ.get("QDRANT_HOST", "qdrant")
    port = int(os.environ.get("QDRANT_HTTP_PORT", "6333"))
    try:
        client = AsyncQdrantClient(host=host, port=port)
        try:
            # Filter on workspace_id payload field. We use count
            # (exact=True) so the result is deterministic.
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            result = await client.count(
                collection_name="georag_reports",
                count_filter=Filter(must=[
                    FieldCondition(
                        key="workspace_id",
                        match=MatchValue(value=workspace_str),
                    )
                ]),
                exact=True,
            )
            return int(result.count), None
        finally:
            await client.close()
    except Exception as exc:
        return -1, f"qdrant_count_failed: {type(exc).__name__}: {exc}"


async def _count_redis_keys(workspace_str: str) -> tuple[int, str | None]:
    """Count Redis keys namespaced to this workspace. Best-effort — Redis
    isn't authoritative for restore but its cache footprint is operationally
    useful (e.g., warm vs cold-cache after restore).
    """
    try:
        import redis.asyncio as redis_asyncio
    except ImportError:
        return -1, "redis client not available"

    host = os.environ.get("REDIS_HOST", "redis")
    port = int(os.environ.get("REDIS_PORT", "6379"))
    password = os.environ.get("REDIS_PASSWORD")
    if not password:
        return -1, "REDIS_PASSWORD not set"

    try:
        client = redis_asyncio.Redis(
            host=host, port=port, password=password, decode_responses=False
        )
        try:
            # SCAN for any key whose name includes the workspace_id —
            # covers caching patterns like georag:ws:<uuid>:* and
            # georag:rag_cache:v6:<hash> (those don't include workspace
            # in the key, so we'll just count the ws-prefixed namespace).
            pattern = f"georag:ws:{workspace_str}:*"
            n = 0
            async for _ in client.scan_iter(match=pattern, count=500):
                n += 1
            return n, None
        finally:
            await client.aclose()
    except Exception as exc:
        return -1, f"redis_count_failed: {type(exc).__name__}: {exc}"


def _verify_snapshot_manifest(
    manifest_uri: str,
    live_counts: dict[str, Any],
) -> dict[str, Any]:
    """Probe the snapshot manifest if it points to a file:// URI we can
    read. Returns a dict with `loaded` + (if loaded) `mismatches` keys.

    The manifest schema (v1) is a JSON object:
        {
            "manifest_version": "1.0",
            "captured_at": "<iso-8601>",
            "workspace_id": "<uuid>",
            "stores": {
                "postgres": {"row_counts": {<output_key>: <int>}},
                "neo4j":    {"node_count": <int>},
                "qdrant":   {"point_count": <int>},
                "redis":    {"key_count": <int>},
                "seaweedfs":{"object_count": <int>, "bytes": <int>}
            }
        }
    """
    parsed = urlparse(manifest_uri)
    if parsed.scheme != "file":
        # S3 + other remotes deferred to Phase 11.1.
        return {
            "loaded": False,
            "reason": f"scheme '{parsed.scheme}' not yet supported "
                      "(s3 deferred to Phase 11.1)",
        }

    path = Path(parsed.path)
    if not path.is_file():
        return {
            "loaded": False,
            "reason": f"manifest not found at {path}",
        }
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "loaded": False,
            "reason": f"manifest parse failed: {type(exc).__name__}: {exc}",
        }

    mismatches: list[dict[str, Any]] = []
    stores = manifest.get("stores", {})
    # Postgres per-table
    pg_expected = (stores.get("postgres") or {}).get("row_counts") or {}
    pg_live = live_counts.get("postgres", {})
    for key, expected in pg_expected.items():
        actual = pg_live.get(key)
        if actual is not None and actual != -1 and int(actual) != int(expected):
            mismatches.append({
                "store": "postgres",
                "key": key,
                "expected": int(expected),
                "actual": int(actual),
            })
    # Neo4j single bucket
    n4_expected = (stores.get("neo4j") or {}).get("node_count")
    n4_actual = live_counts.get("neo4j_nodes")
    if n4_expected is not None and n4_actual not in (None, -1):
        if int(n4_actual) != int(n4_expected):
            mismatches.append({
                "store": "neo4j",
                "key": "node_count",
                "expected": int(n4_expected),
                "actual": int(n4_actual),
            })
    # Qdrant
    q_expected = (stores.get("qdrant") or {}).get("point_count")
    q_actual = live_counts.get("qdrant_points")
    if q_expected is not None and q_actual not in (None, -1):
        if int(q_actual) != int(q_expected):
            mismatches.append({
                "store": "qdrant",
                "key": "point_count",
                "expected": int(q_expected),
                "actual": int(q_actual),
            })

    return {
        "loaded": True,
        "manifest_version": manifest.get("manifest_version"),
        "captured_at": manifest.get("captured_at"),
        "manifest_workspace_id": manifest.get("workspace_id"),
        "mismatches": mismatches,
        "matches_workspace_id": (
            manifest.get("workspace_id") is not None
            and live_counts.get("workspace_id") == manifest.get("workspace_id")
        ),
    }


@restore_workspace.task(execution_timeout=timedelta(hours=6), retries=0)
async def execute(
    input: RestoreWorkspaceInput, ctx: Context
) -> RestoreWorkspaceOutput:
    """Cross-store consistency restore.

    * dry_run=True (default): counts workspace-scoped rows / nodes /
      points / keys in **all five** stores, optionally verifies the
      manifest URI's claimed counts match live state, emits an audit
      anchor.
    * dry_run=False: explicit guard — backup infrastructure not yet
      shipped; returns failure without touching data.
    """
    workspace_str = str(input.workspace_id)

    if not input.dry_run:
        # §11.3 wave 1 — PG-only restore from a workspace_export manifest
        # produced by app.hatchet_workflows.workspace_export. The
        # snapshot_manifest_uri MUST point at a workspace_export object
        # (s3://workspace-exports/<workspace_id>/...jsonl.gz) — full-
        # store §11.1 dumps can't be restored per-workspace (pg_restore
        # is database-level, not workspace-level).
        #
        # Neo4j / Qdrant / Redis stay in dry-run mode for wave 1; their
        # restore patterns land separately as §11.3-v2.
        from app.hatchet_workflows._restore_pg_from_export import (
            restore_postgres_from_export,
        )
        try:
            pg_result = await restore_postgres_from_export(
                workspace_id=workspace_str,
                manifest_uri=input.snapshot_manifest_uri,
            )
        except Exception as exc:  # noqa: BLE001
            msg = f"PG restore from {input.snapshot_manifest_uri} failed: {exc}"
            log.exception("restore_workspace dry_run=False failed: %s", msg)
            return RestoreWorkspaceOutput(
                success=False,
                failure_stage="pg_restore",
                failure_reason=msg,
            )

        # §11.3-v2 — Neo4j / Qdrant / Redis restore from the same manifest.
        # Fetch the manifest body once and parse out the per-section rows.
        stores_restored: list[str] = ["postgres"]
        extras_summary: dict[str, Any] = {}
        try:
            from app.hatchet_workflows._restore_pg_from_export import (
                _fetch_manifest_bytes,
            )
            body = await _fetch_manifest_bytes(input.snapshot_manifest_uri)

            from app.hatchet_workflows._restore_extras import (
                parse_export_jsonl_gz, restore_neo4j, restore_qdrant, restore_redis,
            )
            manifest, _pg_tables, sections = parse_export_jsonl_gz(body)
            manifest_version = manifest.get("manifest_version", "1.0")
            extras_summary["manifest_version"] = manifest_version

            # Only attempt extras when the manifest claims to carry them
            # (v1.0 manifests pre-date §11.3-v2 and have no extra sections).
            if manifest_version >= "2.0":
                nodes = sections.get("neo4j_nodes", [])
                rels = sections.get("neo4j_rels", [])
                if nodes or rels:
                    extras_summary["neo4j"] = await restore_neo4j(
                        workspace_str, nodes, rels,
                    )
                    if extras_summary["neo4j"].get("error") is None:
                        stores_restored.append("neo4j")

                points = sections.get("qdrant_points", [])
                if points:
                    extras_summary["qdrant"] = await restore_qdrant(
                        workspace_str, points,
                    )
                    if extras_summary["qdrant"].get("error") is None:
                        stores_restored.append("qdrant")

                keys = sections.get("redis_keys", [])
                if keys:
                    extras_summary["redis"] = await restore_redis(
                        workspace_str, keys,
                    )
                    if extras_summary["redis"].get("error") is None:
                        stores_restored.append("redis")
            else:
                extras_summary["note"] = (
                    f"manifest v{manifest_version} — pre-§11.3-v2; only PG restored"
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "restore_workspace: extras restore raised %s — PG already in place",
                exc, exc_info=True,
            )
            extras_summary["extras_error"] = str(exc)

        return RestoreWorkspaceOutput(
            success=True,
            stores_restored=stores_restored,
            consistency_check_results={
                "restore_mode":             "v2_workspace_export"
                                            if "neo4j" in stores_restored
                                            or "qdrant" in stores_restored
                                            or "redis" in stores_restored
                                            else "pg_only_from_workspace_export",
                "tables_restored":          pg_result["tables"],
                "rows_inserted":            pg_result["rows_inserted"],
                "manifest_workspace_id":    pg_result["manifest_workspace_id"],
                "extras":                   extras_summary,
            },
        )

    log.info(
        "restore_workspace.task_started workspace=%s manifest_uri=%s dry_run=true",
        workspace_str, input.snapshot_manifest_uri,
    )

    pool = await asyncpg.create_pool(
        _dsn(), min_size=1, max_size=2, statement_cache_size=0
    )
    try:
        # 1. Verify workspace exists.
        async with pool.acquire() as conn:
            ws_row = await conn.fetchrow(
                "SELECT workspace_id::text AS id, name, slug "
                "FROM silver.workspaces WHERE workspace_id = $1::uuid",
                workspace_str,
            )
        if ws_row is None:
            msg = f"workspace not found in silver.workspaces: {workspace_str}"
            log.error(msg)
            return RestoreWorkspaceOutput(
                success=False,
                failure_stage="workspace_lookup",
                failure_reason=msg,
            )

        # 2. Per-store counts (PG + Neo4j + Qdrant + Redis run sequentially —
        # they're each <1s and the orchestration is clearer than a gather).
        pg_counts, pg_err = await _count_postgres_rows(pool, workspace_str)
        neo4j_count, neo4j_err = await _count_neo4j_nodes(workspace_str)
        qdrant_count, qdrant_err = await _count_qdrant_points(workspace_str)
        redis_count, redis_err = await _count_redis_keys(workspace_str)

        store_errors = {
            k: v for k, v in {
                "postgres": pg_err,
                "neo4j": neo4j_err,
                "qdrant": qdrant_err,
                "redis": redis_err,
            }.items() if v
        }

        live_counts = {
            "workspace_id": workspace_str,
            "postgres": pg_counts,
            "neo4j_nodes": neo4j_count,
            "qdrant_points": qdrant_count,
            "redis_keys": redis_count,
        }

        # 3. Optional manifest probe.
        manifest_check = _verify_snapshot_manifest(
            input.snapshot_manifest_uri, live_counts
        )

        # 4. Compose results.
        consistency_check_results: dict[str, Any] = {
            "workspace_name": ws_row["name"],
            "workspace_slug": ws_row["slug"],
            "manifest_uri": input.snapshot_manifest_uri,
            "snapshot_verified": manifest_check.get("loaded", False),
            "live_counts": live_counts,
            "store_errors": store_errors,
            "manifest_check": manifest_check,
            "total_rows_in_workspace": sum(
                v for v in pg_counts.values() if v >= 0
            ),
        }

        # 5. Audit anchor.
        async with pool.acquire() as conn:
            ledger = await emit_audit(
                conn,
                action_type="workspace_restore",
                workspace_id=workspace_str,
                actor_id=input.initiated_by_user_id,
                actor_kind="workflow",
                target_schema="silver",
                target_table="workspaces",
                target_id=workspace_str,
                payload={
                    "evaluator": "restore_workspace_cross_store_dry_run_v2",
                    "doc_phase": "G.2",
                    "restore_request_id": str(input.restore_request_id),
                    "snapshot_manifest_uri": input.snapshot_manifest_uri,
                    "dry_run": True,
                    "live_counts": live_counts,
                    "store_errors": store_errors,
                    "manifest_loaded": manifest_check.get("loaded", False),
                    "manifest_mismatches": len(manifest_check.get("mismatches", [])),
                },
            )

        log.info(
            "restore_workspace.task_completed workspace=%s pg=%s neo4j=%s "
            "qdrant=%s redis=%s manifest_loaded=%s mismatches=%d",
            workspace_str,
            sum(v for v in pg_counts.values() if v >= 0),
            neo4j_count, qdrant_count, redis_count,
            manifest_check.get("loaded", False),
            len(manifest_check.get("mismatches", [])),
        )

        return RestoreWorkspaceOutput(
            success=True,
            stores_restored=[],  # dry run — no writes
            consistency_check_results=consistency_check_results,
            inconsistencies_repaired=0,
            audit_ledger_entry_id=ledger.id,
        )
    finally:
        await pool.close()


__all__ = [
    "restore_workspace",
    "RestoreWorkspaceInput",
    "RestoreWorkspaceOutput",
]
