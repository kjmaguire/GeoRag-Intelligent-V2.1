"""§11.3 wave 1 — per-workspace logical export to cold tier.

Complement to the §11.1 full-store backup crons. Where §11.1 dumps each
store wholesale (pg_dump / neo4j-admin / qdrant snapshot / Redis RDB /
SeaweedFS bucket clone), this workflow walks **one workspace** across
the tenant-scoped Postgres tables and writes a JSONL.gz export to
SeaweedFS that ``restore_workspace.dry_run=False`` can consume.

Why both?
=========

Full-store backups (§11.1) are the production DR primitive: a single
restore brings the platform back from a node-level outage. But they
can't be restored selectively — pg_restore is per-database, not
per-workspace.

Workspace exports (this module) are the operator primitive: ship one
workspace to a new cluster, clone a workspace for an investigation,
recover from a workspace-scoped tenant-isolation incident.

Scope (v1)
==========

Postgres only. Every tenant-scoped table in `_WORKSPACE_TABLES` is
walked under the target workspace's RLS scope (SET app.workspace_id),
serialised to JSONL, gzipped, and uploaded to SeaweedFS under
``workspace-exports/<workspace_id>/<timestamp>-<run_id>.jsonl.gz``.

Neo4j / Qdrant / Redis exports are not in v1 — adding them needs:
  - Neo4j: cypher-shell APOC export-with-filter
  - Qdrant: scroll API with workspace_id payload filter
  - Redis: SCAN + workspace-prefixed keys
Each is its own engineering pass; v1 ships PG to give operators
the biggest win (most workspace state is PG-stored).

Triggering
==========

Manual (no cron) — operators invoke via Hatchet's manual run UI with
``{"workspace_id": "<uuid>"}``. Output run_id is logged + audit-row
anchored.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import aioboto3
import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.hatchet_workflows import hatchet

log = logging.getLogger("georag.hatchet.workspace_export")


# Tenant-scoped tables walked by the export. Kept in sync with the
# restore_workspace consistency-check baseline (`_PG_BASELINE_TABLES`)
# so the export and the dry-run reporter agree on what counts as
# "this workspace's PG footprint". Adding a tenant table here requires
# matching it in restore_workspace + the §11.2 cross-store reporter.
_WORKSPACE_TABLES: list[tuple[str, str]] = [
    # (output_key, qualified_table)
    ("silver_workspaces",                 "silver.workspaces"),
    ("silver_hypotheses",                 "silver.hypotheses"),
    ("silver_decision_records",           "silver.decision_records"),
    ("silver_answer_runs",                "silver.answer_runs"),
    ("silver_evidence_items",             "silver.evidence_items"),
    ("silver_document_passages",          "silver.document_passages"),
    ("audit_ledger_anchors",              "audit.audit_ledger"),
    ("targeting_target_recommendations",  "targeting.target_recommendations"),
    ("ops_support_tickets",               "ops.support_tickets"),
]


class WorkspaceExportInput(BaseModel):
    workspace_id: str = Field(..., description="UUID of the workspace to export.")
    bucket: str = Field(
        default="workspace-exports",
        description="SeaweedFS bucket receiving the export object.",
    )
    include_neo4j: bool = Field(
        default=True,
        description="§11.3-v2 — include Neo4j nodes + relationships scoped to workspace.",
    )
    include_qdrant: bool = Field(
        default=True,
        description="§11.3-v2 — include Qdrant points (vectors + payload) filtered by workspace_id.",
    )
    include_redis: bool = Field(
        default=True,
        description="§11.3-v2 — include Redis keys matching georag:ws:<uuid>:* prefix (cache only).",
    )


class WorkspaceExportOutput(BaseModel):
    run_id: str
    workspace_id: str
    bucket: str
    object_key: str
    bytes: int
    rows_exported: int
    per_table: dict[str, int]
    # §11.3-v2 — per-store extra counts + partial-store failure reasons
    neo4j_node_count: int = 0
    neo4j_rel_count: int = 0
    qdrant_point_count: int = 0
    redis_key_count: int = 0
    partial_stores: dict[str, str] = Field(default_factory=dict)
    started_at: datetime
    completed_at: datetime


workspace_export = hatchet.workflow(
    name="workspace_export",
    input_validator=WorkspaceExportInput,
)


def _build_dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _s3_session_kwargs() -> dict[str, str]:
    return {
        "endpoint_url":          os.environ.get("SEAWEEDFS_S3_ENDPOINT", "http://seaweedfs:8333"),
        "aws_access_key_id":     os.environ.get("SEAWEEDFS_S3_ACCESS_KEY", "georag"),
        "aws_secret_access_key": os.environ.get("SEAWEEDFS_S3_SECRET_KEY", "georag"),
        "region_name":           os.environ.get("SEAWEEDFS_S3_REGION", "us-east-1"),
    }


def _build_object_key(workspace_id: str, run_id: str, when: datetime) -> str:
    return (
        f"{workspace_id}/"
        f"{when.year:04d}-{when.month:02d}-{when.day:02d}T"
        f"{when.hour:02d}{when.minute:02d}{when.second:02d}-{run_id}.jsonl.gz"
    )


def _row_to_dict(row: asyncpg.Record) -> dict[str, Any]:
    """JSON-safe serialisation. bytes → hex, datetime → ISO-8601,
    UUID → str, everything else passes through."""
    import uuid as _u
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, (bytes, bytearray, memoryview)):
            out[k] = bytes(v).hex()
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, _u.UUID):
            out[k] = str(v)
        else:
            out[k] = v
    return out


async def _export_one_table(
    conn: asyncpg.Connection, qualified_table: str, workspace_id: str,
) -> list[dict[str, Any]]:
    """Walk one tenant table for the target workspace + return list of dicts.

    Uses the `set_config('app.workspace_id', $1, false)` GUC contract
    so RLS does the workspace scoping rather than relying on every
    table having a literal workspace_id column (some tables join through
    a parent).
    """
    if qualified_table == "silver.workspaces":
        # Special case — the workspace row keyed on workspace_id PK.
        rows = await conn.fetch(
            "SELECT * FROM silver.workspaces WHERE workspace_id = $1::uuid",
            workspace_id,
        )
    else:
        # Try the simple WHERE workspace_id = $1 first; fall back to
        # RLS-scoped SELECT * if the column doesn't exist.
        try:
            rows = await conn.fetch(
                f"SELECT * FROM {qualified_table} WHERE workspace_id = $1::uuid",
                workspace_id,
            )
        except asyncpg.exceptions.UndefinedColumnError:
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, false)", workspace_id,
            )
            rows = await conn.fetch(f"SELECT * FROM {qualified_table}")
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "workspace_export: skipping %s (err=%r)",
                qualified_table, exc,
            )
            return []
    return [_row_to_dict(r) for r in rows]


def _build_manifest(
    workspace_id: str,
    run_id: str,
    per_table_rows: dict[str, list[dict[str, Any]]],
    *,
    neo4j_nodes: list[dict[str, Any]] | None = None,
    neo4j_rels: list[dict[str, Any]] | None = None,
    qdrant_points: list[dict[str, Any]] | None = None,
    redis_keys: list[dict[str, Any]] | None = None,
    partial_stores: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The manifest is the first JSONL line; subsequent lines are
    `{"table": <output_key>, "row": <row_dict>}` for PG tables and
    `{"section": <neo4j_nodes|neo4j_rels|qdrant_points|redis_keys>,
       "row": <dict>}` for the §11.3-v2 extra stores.

    restore_workspace reads the manifest line first to validate target
    workspace + section list, then streams rows.

    Manifest version bumped from 1.0 to 2.0 with §11.3-v2.
    """
    return {
        "manifest_version":   "2.0",
        "format":             "workspace_export",
        "workspace_id":       workspace_id,
        "run_id":             run_id,
        "captured_at":        datetime.now(tz=timezone.utc).isoformat(),
        "table_row_counts":   {k: len(v) for k, v in per_table_rows.items()},
        "tables":             list(per_table_rows.keys()),
        # §11.3-v2 extras
        "neo4j_node_count":   len(neo4j_nodes or []),
        "neo4j_rel_count":    len(neo4j_rels or []),
        "qdrant_point_count": len(qdrant_points or []),
        "redis_key_count":    len(redis_keys or []),
        "partial_stores":     dict(partial_stores or {}),
    }


def _serialise_jsonl_gz(
    manifest: dict[str, Any],
    per_table_rows: dict[str, list[dict[str, Any]]],
    *,
    neo4j_nodes: list[dict[str, Any]] | None = None,
    neo4j_rels: list[dict[str, Any]] | None = None,
    qdrant_points: list[dict[str, Any]] | None = None,
    redis_keys: list[dict[str, Any]] | None = None,
) -> bytes:
    """Manifest as line 1, then PG rows (table-tagged), then §11.3-v2
    extra-store rows (section-tagged: neo4j_nodes / neo4j_rels /
    qdrant_points / redis_keys)."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
        gz.write(json.dumps(manifest, sort_keys=True, default=str).encode("utf-8"))
        gz.write(b"\n")
        for output_key, rows in per_table_rows.items():
            for row in rows:
                line = json.dumps(
                    {"table": output_key, "row": row},
                    sort_keys=True, default=str,
                ).encode("utf-8")
                gz.write(line)
                gz.write(b"\n")
        # §11.3-v2 extras — each tagged with `section`
        for section, rows in (
            ("neo4j_nodes",   neo4j_nodes or []),
            ("neo4j_rels",    neo4j_rels or []),
            ("qdrant_points", qdrant_points or []),
            ("redis_keys",    redis_keys or []),
        ):
            for row in rows:
                line = json.dumps(
                    {"section": section, "row": row},
                    sort_keys=True, default=str,
                ).encode("utf-8")
                gz.write(line)
                gz.write(b"\n")
    return buf.getvalue()


async def _put_s3(bucket: str, key: str, body: bytes) -> None:
    session = aioboto3.Session()
    async with session.client("s3", **_s3_session_kwargs()) as s3:
        await s3.put_object(Bucket=bucket, Key=key, Body=body)


@workspace_export.task(execution_timeout="30m")
async def run_export(
    input: WorkspaceExportInput, ctx: Context,
) -> WorkspaceExportOutput:
    started_at = datetime.now(tz=timezone.utc)
    workspace_id = str(input.workspace_id)

    conn = await asyncpg.connect(_build_dsn(), statement_cache_size=0)
    try:
        # Verify workspace exists.
        ws_row = await conn.fetchrow(
            "SELECT workspace_id::text AS id FROM silver.workspaces "
            "WHERE workspace_id = $1::uuid",
            workspace_id,
        )
        if ws_row is None:
            raise RuntimeError(f"workspace_id {workspace_id} not found in silver.workspaces")

        # Walk each tenant table.
        per_table_rows: dict[str, list[dict[str, Any]]] = {}
        for output_key, qualified_table in _WORKSPACE_TABLES:
            per_table_rows[output_key] = await _export_one_table(
                conn, qualified_table, workspace_id,
            )

        # §11.3-v2 — walk the 3 extra stores. Each failure is recorded
        # in partial_stores but does NOT fail the export (PG already
        # ran successfully + that's the must-preserve store).
        neo4j_nodes: list[dict[str, Any]] = []
        neo4j_rels: list[dict[str, Any]] = []
        qdrant_points: list[dict[str, Any]] = []
        redis_keys: list[dict[str, Any]] = []
        partial_stores: dict[str, str] = {}

        if input.include_neo4j:
            from app.hatchet_workflows._export_extras import export_neo4j_workspace
            neo4j_nodes, neo4j_rels, n4_err = await export_neo4j_workspace(workspace_id)
            if n4_err:
                partial_stores["neo4j"] = n4_err
        if input.include_qdrant:
            from app.hatchet_workflows._export_extras import export_qdrant_workspace
            qdrant_points, q_err = await export_qdrant_workspace(workspace_id)
            if q_err:
                partial_stores["qdrant"] = q_err
        if input.include_redis:
            from app.hatchet_workflows._export_extras import export_redis_workspace
            redis_keys, r_err = await export_redis_workspace(workspace_id)
            if r_err:
                partial_stores["redis"] = r_err

        # Manifest + serialise.
        from uuid import uuid4
        run_id = str(uuid4())
        manifest = _build_manifest(
            workspace_id, run_id, per_table_rows,
            neo4j_nodes=neo4j_nodes, neo4j_rels=neo4j_rels,
            qdrant_points=qdrant_points, redis_keys=redis_keys,
            partial_stores=partial_stores,
        )
        body = _serialise_jsonl_gz(
            manifest, per_table_rows,
            neo4j_nodes=neo4j_nodes, neo4j_rels=neo4j_rels,
            qdrant_points=qdrant_points, redis_keys=redis_keys,
        )
        object_key = _build_object_key(workspace_id, run_id, started_at)

        # Upload.
        await _put_s3(input.bucket, object_key, body)

        completed_at = datetime.now(tz=timezone.utc)
        rows_exported = sum(len(v) for v in per_table_rows.values())
        per_table_counts = manifest["table_row_counts"]

        # Audit anchor.
        await emit_audit(
            conn,
            action_type="workspace.export.completed",
            workspace_id=workspace_id,
            actor_id=None,
            actor_kind="workflow",
            target_schema="silver",
            target_table="workspaces",
            target_id=workspace_id,
            payload={
                "run_id":            run_id,
                "bucket":            input.bucket,
                "object_key":        object_key,
                "bytes":             len(body),
                "rows_exported":     rows_exported,
                "table_row_counts":  per_table_counts,
                "duration_s":        (completed_at - started_at).total_seconds(),
            },
        )

        log.info(
            "workspace_export OK ws=%s rows=%d bytes=%s key=%s",
            workspace_id, rows_exported, len(body), object_key,
        )

        # Phase 5 admin surface push — drives Admin/ExportGate.
        try:
            from app.services.laravel_bridge import post_admin_surface_updated
            admin_payload = {
                "workflow_kind": "workspace_export",
                "run_id": str(run_id),
                "workspace_id": str(workspace_id),
                "rows_exported": rows_exported,
                "bytes": len(body),
                "status": "success",
            }
            await post_admin_surface_updated(
                surface="workflow-runs",
                affected_props=["workflow_runs"],
                payload=admin_payload,
            )
            await post_admin_surface_updated(
                surface="export-gate",
                affected_props=["results"],
                payload=admin_payload,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "workspace_export: admin surface broadcasts failed run_id=%s err=%s",
                run_id, exc,
            )

        return WorkspaceExportOutput(
            run_id=run_id,
            workspace_id=workspace_id,
            bucket=input.bucket,
            object_key=object_key,
            bytes=len(body),
            rows_exported=rows_exported,
            per_table=per_table_counts,
            neo4j_node_count=len(neo4j_nodes),
            neo4j_rel_count=len(neo4j_rels),
            qdrant_point_count=len(qdrant_points),
            redis_key_count=len(redis_keys),
            partial_stores=partial_stores,
            started_at=started_at,
            completed_at=completed_at,
        )
    finally:
        await conn.close()


__all__ = [
    "workspace_export",
    "WorkspaceExportInput",
    "WorkspaceExportOutput",
]
