"""§11.10 — nightly cold-tier archive of audit.audit_ledger rows.

Schedule: ``0 19 * * *`` UTC. It was 04:00, placed "after the §11.1
backup window closes at 03:00" — that window no longer exists, since the
per-store ``backup_*`` workflows were deleted 2026-08-23 and production
relies on RDS PITR. The constraint the old slot satisfied went with it.

What this workflow does
=======================

1. Compute the cutoff = `now() - retention_days days`, and the watermark: the
   `cutoff_before` of the newest `audit.cold_tier.archive.completed` anchor
   for this scope (what earlier runs already archived).
2. Call `app.audit.cold_tier_archive.archive_window` for the window between
   them with the SeaweedFS S3 destination bucket.
3. Verify chain integrity over the window, one chain per `workspace_id`
   (the function does this inline — failure aborts the upload).
4. Emit `audit.cold_tier.archive.completed` (or `.failed`) audit row. The
   watermark only advances on a completed row.
5. On a verification failure, log `AUDIT_LEDGER_CHAIN_BREAK` (the alarm the
   nightly verifier already has) and FAIL the run: it used to return
   `status="failed"` as a normal result, so Hatchet recorded a green run.

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
  At 19:00 UTC each night the cron archives the rows that crossed the 90-day
  line since the last completed run, not the whole history again (it used to
  re-read and re-upload everything older than 90 days every night). The first
  run with no completed anchor to continue from archives everything older than
  the cutoff, once. Object keys carry the run's cutoff stamp, so cold-tier
  objects of different runs don't collide; archive_window writes a single
  manifest per run.
- `archive_bucket="audit-cold-tier"` per §11 kickoff locked default. It is the
  KEY PREFIX of every object, inside the configured BACKUPS bucket
  (`AWS_BUCKET_BACKUPS`: `georag-backups-<account>` on AWS). It used to name a
  bucket of its own, which Terraform never creates or grants (it provisions
  bronze, bronze-raster, exports and backups), so on AWS every archive run
  ended in NoSuchBucket.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import aioboto3
import asyncpg
from georag_object_storage import Bucket, StorageConfig, async_client_kwargs
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.audit.cold_tier_archive import ArchiveRun, archive_window
from app.db.dsn import build_dsn
from app.hatchet_workflows import hatchet
from app.hatchet_workflows.audit_ledger_verify import AUDIT_CHAIN_BREAK_MARKER

log = logging.getLogger("georag.hatchet.cold_tier_archive")


class ColdTierArchiveInput(BaseModel):
    retention_days: int = Field(
        default=90, ge=1, le=3650,
        description="Hot-tier retention. Rows older than now()-N days are archived.",
    )
    archive_bucket: str = Field(
        default="audit-cold-tier",
        description="Key prefix, inside the bucket below, of the gzipped JSONL "
                    "chunks + manifest.",
    )
    bucket: str | None = Field(
        default=None,
        description="Bucket receiving the objects. Default: the configured "
                    "BACKUPS bucket (AWS_BUCKET_BACKUPS), resolved when the run starts.",
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
    on_crons=["0 19 * * *"],
    input_validator=ColdTierArchiveInput,
)


# One DSN builder for the whole service — see app/db/dsn.py for why
# sixty copies of this existed and what the drift cost.
_build_dsn = build_dsn


class _SeaweedFsColdTierStore:
    """Implements the _ColdTierStore Protocol via SeaweedFS S3.

    The archive_window function calls `put(key, content)` once per
    chunk + once for the manifest. We use a per-run aioboto3 client
    so concurrent runs (if any) don't share connection pools.

    ``bucket`` is the resolved physical name (the configured BACKUPS bucket,
    or an operator override), so this uses the raw-client escape hatch
    (async_client_kwargs) rather than the higher-level AsyncObjectStorage
    interface, which takes a logical Bucket.
    """

    def __init__(self, bucket: str):
        self._bucket = bucket
        self._session = aioboto3.Session()

    async def put(self, key: str, content: bytes) -> str:
        # Credential/endpoint resolution deliberately stays lazy (inside
        # put(), not __init__) — a test constructs this class without any
        # object-storage env vars set to check its Protocol shape, and
        # StorageConfig.from_env() raises if none are present.
        client_kwargs = async_client_kwargs(StorageConfig.from_env())
        async with self._session.client("s3", **client_kwargs) as s3:
            await s3.put_object(Bucket=self._bucket, Key=key, Body=content)
        return f"s3://{self._bucket}/{key}"


async def _last_archived_cutoff(
    conn: asyncpg.Connection, workspace_id_scope: str | None,
) -> datetime | None:
    """The newest cutoff a COMPLETED archive run covered for this scope, or None.

    Read from the run's own audit anchor rather than from the cold tier (the
    store only has ``put``). The anchor is written after the manifest, so a run
    that died between the two is simply done again. ``max`` rather than "the
    latest anchor": a run with a shorter ``retention_days`` completes with an
    earlier cutoff and must not move the watermark back.

    If the anchors cannot be read the answer is None: archiving more than
    needed is safe (it is what every run did before the watermark existed),
    archiving less is not.
    """
    try:
        return await conn.fetchval(
            """
            SELECT max((payload->>'cutoff_before')::timestamptz)
              FROM audit.audit_ledger
             WHERE action_type = 'audit.cold_tier.archive.completed'
               AND workspace_id IS NOT DISTINCT FROM $1::uuid
            """,
            workspace_id_scope,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "cold_tier_archive: could not read the watermark, archiving from the "
            "beginning of the ledger. err=%s", exc,
        )
        return None


@cold_tier_archive_workflow.task(execution_timeout="60m")
async def run_archive(
    input: ColdTierArchiveInput, ctx: Context,
) -> ColdTierArchiveOutput:
    started_at = datetime.now(tz=UTC)
    cutoff = started_at - timedelta(days=input.retention_days)

    dsn = _build_dsn()
    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        cutoff_after = await _last_archived_cutoff(conn, input.workspace_id_scope)
        cold_tier = _SeaweedFsColdTierStore(
            input.bucket or StorageConfig.from_env().bucket_name(Bucket.BACKUPS),
        )
        try:
            run: ArchiveRun = await archive_window(
                conn,
                cutoff_before=cutoff,
                archive_bucket=input.archive_bucket,
                cold_tier=cold_tier,
                workspace_id_scope=input.workspace_id_scope,
                chunk_rows=input.chunk_rows,
                dry_run=False,
                cutoff_after=cutoff_after,
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
                # The window this run covered. The next run starts at
                # cutoff_before of the newest COMPLETED anchor.
                "window_start":        cutoff_after.isoformat() if cutoff_after else None,
                "cutoff_before":       cutoff.isoformat(),
                "rows_archived":       run.rows_archived,
                "cold_tier_uri":       run.cold_tier_uri,
                "hot_tier_remaining":  run.hot_tier_remaining,
                "verification_passed": run.verification_passed,
                "failure_reason":      run.failure_reason,
                "manifest_key":        run.manifest_key,
                "chunks":              len(run.chunks),
                "chains":              len(run.chain_heads),
                "duration_s":          duration_s,
            },
        )
        log.info(
            "cold_tier_archive %s rows=%d uri=%s verified=%s",
            "OK" if run.verification_passed else "FAIL",
            run.rows_archived, run.cold_tier_uri, run.verification_passed,
        )
        if not run.verification_passed:
            # The same alarm the nightly verifier raises: this is the hot ledger
            # failing its own hash chain, and nothing was archived.
            log.error(
                "%s source=cold_tier_archive window=[%s, %s) reason=%s",
                AUDIT_CHAIN_BREAK_MARKER,
                cutoff_after.isoformat() if cutoff_after else "-inf",
                cutoff.isoformat(), run.failure_reason,
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

        if not run.verification_passed:
            # Not a result to return: a verification failure used to come back
            # as status="failed" and Hatchet recorded a green run. The failed
            # anchor and the alarm marker above are written; fail the run too.
            raise RuntimeError(
                f"cold_tier_archive verification failed, nothing archived: {run.failure_reason}"
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
