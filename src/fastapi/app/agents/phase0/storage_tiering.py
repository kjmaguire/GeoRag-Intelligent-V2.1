"""Storage Tiering Agent (Phase 0 agent #3, R2).

Reads ``silver.storage_tier_policy`` for active rules, queries SeaweedFS S3
(via aioboto3 against ``minio:8333``) for objects in ``tier-{source_tier}``
older than the rule's ``age_threshold_days``, and moves each one to
``tier-{target_tier}`` (CopyObject + DeleteObject).

Phase 0 caveats:
  - No live silver.* rows yet reference these objects, so the silver-side
    storage_uri rewrite is a documented NOOP for now.
  - SeaweedFS S3 IAM is known to refuse certain admin verbs (mc mb / mc ls
    quirks per project memory). The agent handles ``ClientError`` per object
    and surfaces the gap in the summary instead of failing the whole run.
    A follow-up R&D ticket may switch to ``weed shell s3.bucket.move`` if
    boto3 cannot make progress.

Risk tier R2 — wrapper requires ``ctx.workspace_id`` AND ``ctx.document_id``
for the idempotency key.  When invoked at the *policy* level (no specific
document) the caller passes ``ctx.bypass_idempotency=True`` so the wrapper
skips the lookup; the agent itself is naturally idempotent because each
object move is gated on the source still existing in tier-{source_tier}.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from app.agents import AgentContext, georag_agent
from app.agents.runtime import get_runtime
from app.audit import emit_audit


logger = logging.getLogger(__name__)


def _s3_endpoint() -> str:
    return os.environ.get("S3_ENDPOINT_URL") or os.environ.get(
        "MINIO_ENDPOINT", "http://minio:8333"
    )


def _s3_credentials() -> tuple[str, str]:
    # Match `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` from .env (SeaweedFS
    # keeps the MINIO_ prefix per ADR-0001 compatibility).
    access = os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get(
        "MINIO_ROOT_USER", "georag-admin"
    )
    secret = os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get(
        "MINIO_ROOT_PASSWORD", ""
    )
    return access, secret


@georag_agent(
    name="Storage Tiering Agent",
    risk_tier="R2",
    version="0.1.0",
)
async def storage_tiering_run(
    ctx: AgentContext,
    *,
    max_objects_per_rule: int = 100,
    dry_run_s3: bool = True,
) -> dict[str, Any]:
    # All-nighter 2026-05-21 — `dry_run_s3` defaults to True per Phase 0
    # audit recommendation. Kestra prod sweep must opt-IN to destructive
    # mode. The wrapper-level `ctx.is_dry_run` also short-circuits this
    # agent if set, so an operator running a global dry-run sweep doesn't
    # need to thread the flag through every per-agent kwarg.
    if getattr(ctx, "is_dry_run", False):
        dry_run_s3 = True
    """Apply every active tier-transition rule.

    Returns a summary the wrapper logs into the audit_ledger payload.

        { rules_evaluated, objects_moved, objects_skipped, errors,
          per_rule: [ { object_class, source, target, moved, skipped }, ... ] }
    """
    rt = get_runtime()
    summary: dict[str, Any] = {
        "rules_evaluated": 0,
        "objects_moved": 0,
        "objects_skipped": 0,
        "errors": 0,
        "silver_uri_rewrites": 0,
        "per_rule": [],
    }

    # Lazy-import aioboto3 so the rest of the FastAPI app boots even if the
    # dep hasn't been re-installed yet (CI sequencing safety).
    try:
        import aioboto3  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover — captured in summary
        summary["errors"] += 1
        summary["fatal"] = f"aioboto3 not installed: {exc}"
        return summary

    rules = await rt.pg_pool.fetch(
        """
        SELECT id, workspace_id, object_class, source_tier, target_tier,
               age_threshold_days, priority
        FROM silver.storage_tier_policy
        WHERE is_active = true
          AND (workspace_id IS NULL OR workspace_id = $1)
        ORDER BY priority ASC, age_threshold_days ASC
        """,
        ctx.workspace_id,
    )

    if not rules:
        summary["note"] = "no active storage_tier_policy rows — nothing to do"
        return summary

    access, secret = _s3_credentials()
    endpoint = _s3_endpoint()

    session = aioboto3.Session(
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name="us-east-1",
    )

    now = datetime.now(timezone.utc)

    async with session.client("s3", endpoint_url=endpoint) as s3:
        for rule in rules:
            summary["rules_evaluated"] += 1
            src_bucket = f"tier-{rule['source_tier']}"
            dst_bucket = f"tier-{rule['target_tier']}"
            per_rule: dict[str, Any] = {
                "object_class": rule["object_class"],
                "source_bucket": src_bucket,
                "target_bucket": dst_bucket,
                "age_threshold_days": rule["age_threshold_days"],
                "moved": 0,
                "skipped": 0,
                "errors": 0,
            }

            # List candidate objects.
            paginator = s3.get_paginator("list_objects_v2")
            try:
                age_seconds = float(rule["age_threshold_days"]) * 86400.0
                seen = 0
                async for page in paginator.paginate(Bucket=src_bucket):
                    for obj in page.get("Contents", []):
                        if seen >= max_objects_per_rule:
                            break
                        seen += 1
                        last_mod = obj.get("LastModified")
                        if not last_mod:
                            per_rule["skipped"] += 1
                            continue
                        # boto3 returns aware datetimes; coerce defensively.
                        if last_mod.tzinfo is None:
                            last_mod = last_mod.replace(tzinfo=timezone.utc)
                        age = (now - last_mod).total_seconds()
                        if age < age_seconds:
                            per_rule["skipped"] += 1
                            continue

                        key = obj["Key"]
                        # Phase 0: object_class prefix match — operators
                        # store objects under `{object_class}/...` so the
                        # rule scope is enforced by key prefix. Skip
                        # objects that don't match.
                        if not key.startswith(f"{rule['object_class']}/"):
                            per_rule["skipped"] += 1
                            continue

                        if dry_run_s3:
                            per_rule["skipped"] += 1
                            continue

                        try:
                            await s3.copy_object(
                                Bucket=dst_bucket,
                                Key=key,
                                CopySource={"Bucket": src_bucket, "Key": key},
                            )
                            await s3.delete_object(Bucket=src_bucket, Key=key)
                        except Exception as exc:  # noqa: BLE001 — surface, don't fail
                            per_rule["errors"] += 1
                            summary["errors"] += 1
                            logger.warning(
                                "storage_tiering: move failed for %s/%s -> %s/%s: %s",
                                src_bucket, key, dst_bucket, key, exc,
                            )
                            continue

                        per_rule["moved"] += 1
                        summary["objects_moved"] += 1

                        # Audit each object move with its own ledger row,
                        # per the kickoff requirement.
                        try:
                            await emit_audit(
                                rt.pg_pool,
                                action_type="storage.tier_transition",
                                workspace_id=rule["workspace_id"] or ctx.workspace_id,
                                actor_kind="agent",
                                target_schema="silver",
                                target_table="storage_tier_policy",
                                target_id=str(rule["id"]),
                                payload={
                                    "object_class": rule["object_class"],
                                    "source": f"{src_bucket}/{key}",
                                    "target": f"{dst_bucket}/{key}",
                                    "age_seconds": age,
                                    "rule_id": str(rule["id"]),
                                },
                                trace_id=ctx.trace_id,
                            )
                        except Exception:  # pragma: no cover
                            logger.exception(
                                "storage_tiering: audit emit failed for %s", key
                            )

                        # Phase 0 NOOP: rewrite silver.* storage_uri rows
                        # that referenced the old path. No live tables
                        # carry tier-prefixed URIs yet; when they do
                        # (Phase 1+), update here.

                    if seen >= max_objects_per_rule:
                        break
            except Exception as exc:  # noqa: BLE001 — bucket missing, IAM gap, etc.
                per_rule["errors"] += 1
                per_rule["error_message"] = f"{type(exc).__name__}: {exc}"
                summary["errors"] += 1
                logger.warning(
                    "storage_tiering: list/scan failed for bucket %s: %s",
                    src_bucket, exc,
                )

            summary["objects_skipped"] += per_rule["skipped"]
            summary["per_rule"].append(per_rule)

    return summary
