"""§11.10 — nightly cold-tier archive of audit.audit_ledger rows.

Schedule: ``0 4 * * *`` UTC (04:00 UTC — after the §11.1 backup
window closes at 03:00).

What this workflow does
=======================

1. Compute the cutoff = `now() - retention_days days`.
2. Call `app.audit.cold_tier_archive.archive_window` with the
   SeaweedFS S3 destination bucket.
3. Verify chain integrity over the window (the function already
   does this inline — failure aborts the upload).
4. Emit `audit.cold_tier.archive.completed` (or `.failed`) audit row.

What it does NOT do
===================

**Pruning is operator-gated.** The cron only writes to cold tier;
deleting hot-tier rows requires a separate operator confirmation
(via the admin endpoint's "prune archived window" action). This is
a deliberate safety boundary — automatic deletion of audit rows is
an irrecoverable operation.

Defaults
========

- `retention_days=90` per §11 kickoff (30d hot / 90d warm / indef cold).
  At 04:00 UTC each night the cron archives everything older than 90
  days. The same row may be archived multiple times across runs —
  the per-run object_key is timestamped, so cold-tier objects don't
  collide; the archive_window function's chunking writes a single
  manifest per run.
- `archive_bucket="audit-cold-tier"` per §11 kickoff locked default.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

import aioboto3
import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.audit.cold_tier_archive import ArchiveRun, archive_window
from app.hatchet_workflows import hatchet

log = logging.getLogger("georag.hatchet.cold_tier_archive")


class ColdTierArchiveInput(BaseModel):
    retention_days: int = Field(
        default=90, ge=1, le=3650,
        description="Hot-tier retention. Rows older than now()-N days are archived.",
    )
    archive_bucket: str = Field(
        default="audit-cold-tier",
        description="SeaweedFS bucket receiving the gzipped JSONL chunks + manifest.",
    )
    chunk_rows: int = Field(
        default=10_000, ge=100, le=1_000_000,
        description="Max rows per JSONL.gz chunk. archive_window writes "
                    "multiple chunks for large windows.",
    )
    workspace_id_scope: str | None = Field(
        default=None,
        description="Optional — archive only one workspace's chain. None = global.",
    )


class ColdTierArchiveOutput(BaseModel):
    status: str
    rows_archived: int
    cold_tier_uri: str
    hot_tier_remaining: int
    verification_passed: bool
    failure_reason: str | None = None
    manifest_key: str = ""
    duration_s: float = 0.0


cold_tier_archive_workflow = hatchet.workflow(
    name="cold_tier_archive",
    on_crons=["0 4 * * *"],
    input_validator=ColdTierArchiveInput,
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


class _SeaweedFsColdTierStore:
    """Implements the _ColdTierStore Protocol via SeaweedFS S3.

    The archive_window function calls `put(key, content)` once per
    chunk + once for the manifest. We use a per-run aioboto3 client
    so concurrent runs (if any) don't share connection pools.
    """

    def __init__(self, bucket: str):
        self._bucket = bucket
        self._session = aioboto3.Session()

    async def put(self, key: str, content: bytes) -> str:
        async with self._session.client("s3", **_s3_session_kwargs()) as s3:
            await s3.put_object(Bucket=self._bucket, Key=key, Body=content)
        return f"s3://{self._bucket}/{key}"


@cold_tier_archive_workflow.task(execution_timeout="60m")
async def run_archive(
    input: ColdTierArchiveInput, ctx: Context,
) -> ColdTierArchiveOutput:
    started_at = datetime.now(tz=UTC)
    cutoff = started_at - timedelta(days=input.retention_days)

    dsn = _build_dsn()
    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        cold_tier = _SeaweedFsColdTierStore(input.archive_bucket)
        try:
            run: ArchiveRun = await archive_window(
                conn,
                cutoff_before=cutoff,
                archive_bucket=input.archive_bucket,
                cold_tier=cold_tier,
                workspace_id_scope=input.workspace_id_scope,
                chunk_rows=input.chunk_rows,
                dry_run=False,
            )
        except Exception as exc:  # noqa: BLE001
            duration_s = (datetime.now(tz=UTC) - started_at).total_seconds()
            await emit_audit(
                conn,
                action_type="audit.cold_tier.archive.failed",
                workspace_id=input.workspace_id_scope,
                actor_id=None,
                actor_kind="workflow",
                target_schema="audit",
                target_table="audit_ledger",
                target_id=None,
                payload={
                    "cutoff_before": cutoff.isoformat(),
                    "archive_bucket": input.archive_bucket,
                    "reason": repr(exc)[:1000],
                    "duration_s": duration_s,
                },
            )
            log.exception("cold_tier_archive failed cutoff=%s", cutoff)
            raise

        completed_at = datetime.now(tz=UTC)
        duration_s = (completed_at - started_at).total_seconds()

        await emit_audit(
            conn,
            action_type=(
                "audit.cold_tier.archive.completed"
                if run.verification_passed
                else "audit.cold_tier.archive.failed"
            ),
            workspace_id=input.workspace_id_scope,
            actor_id=None,
            actor_kind="workflow",
            target_schema="audit",
            target_table="audit_ledger",
            target_id=run.manifest_key or None,
            payload={
                "cutoff_before":       cutoff.isoformat(),
                "rows_archived":       run.rows_archived,
                "cold_tier_uri":       run.cold_tier_uri,
                "hot_tier_remaining":  run.hot_tier_remaining,
                "verification_passed": run.verification_passed,
                "failure_reason":      run.failure_reason,
                "manifest_key":        run.manifest_key,
                "chunks":              len(run.chunks),
                "duration_s":          duration_s,
            },
        )
        log.info(
            "cold_tier_archive %s rows=%d uri=%s verified=%s",
            "OK" if run.verification_passed else "FAIL",
            run.rows_archived, run.cold_tier_uri, run.verification_passed,
        )

        # Phase 2 admin surface push — Admin/AuditFindings displays the
        # archive_runs list; Admin/WorkflowRuns gets the workflow row.
        # Best-effort.
        try:
            from app.services.laravel_bridge import post_admin_surface_updated
            admin_payload = {
                "workflow_kind": "cold_tier_archive",
                "workspace_id_scope": input.workspace_id_scope,
                "status": "success" if run.verification_passed else "failure",
                "rows_archived": run.rows_archived,
                "manifest_key": run.manifest_key,
                "verification_passed": run.verification_passed,
                "failure_reason": run.failure_reason,
            }
            await post_admin_surface_updated(
                surface="workflow-runs",
                affected_props=["workflow_runs"],
                payload=admin_payload,
            )
            await post_admin_surface_updated(
                surface="audit-findings",
                affected_props=["archive_runs"],
                payload=admin_payload,
            )
            # Phase 5 — cold_tier_archive also touches the backup-side
            # operator surface (cold_tier_runs is a column on the
            # backups dashboard). One extra dispatch, same payload.
            await post_admin_surface_updated(
                surface="backups",
                affected_props=["cold_tier_runs"],
                payload=admin_payload,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "cold_tier_archive: admin surface broadcasts failed err=%s", exc,
            )

        return ColdTierArchiveOutput(
            status="completed" if run.verification_passed else "failed",
            rows_archived=run.rows_archived,
            cold_tier_uri=run.cold_tier_uri,
            hot_tier_remaining=run.hot_tier_remaining,
            verification_passed=run.verification_passed,
            failure_reason=run.failure_reason,
            manifest_key=run.manifest_key,
            duration_s=duration_s,
        )
    finally:
        await conn.close()


__all__ = [
    "cold_tier_archive_workflow",
    "ColdTierArchiveInput",
    "ColdTierArchiveOutput",
]
