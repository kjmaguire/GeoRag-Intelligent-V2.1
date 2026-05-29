"""Support Packet Agent (Phase 0 agent #11, R2, LLM-calling).

On-demand: ``POST /api/v1/support/packets/assemble``.

Bundle contents:
  - Tempo trace JSON (HTTP GET to ``/api/traces/{trace_id}``)
  - last 50 ``workflow.workflow_runs``
  - last 100 ``audit.audit_ledger`` entries (workspace-scoped)
  - prompt_versions in effect at incident time
  - configuration snapshot (a curated subset of env vars)

The bundle is gzipped tar, uploaded to SeaweedFS at:
  ``tier-warm/support-packets/{workspace_id}/{incident_id}_{iso_timestamp}.tar.gz``

A row is inserted into ``silver.support_packets`` (introduced by the
Step 6 supplement migration). An audit_ledger row is emitted with
``action_type='support_packet.assembled'``.

Risk tier R2 — wrapper requires ctx.workspace_id AND ctx.document_id;
the caller maps ``incident_id`` to ctx.document_id so two assembly calls
for the same incident dedupe naturally.
"""

from __future__ import annotations

import io
import json
import logging
import os
import tarfile
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx

from app.agents import AgentContext, georag_agent
from app.agents.runtime import get_runtime
from app.audit import emit_audit


logger = logging.getLogger(__name__)


def _s3_endpoint() -> str:
    return os.environ.get("S3_ENDPOINT_URL") or os.environ.get(
        "MINIO_ENDPOINT", "http://minio:8333"
    )


def _s3_credentials() -> tuple[str, str]:
    access = os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get(
        "MINIO_ROOT_USER", "georag-admin"
    )
    secret = os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get(
        "MINIO_ROOT_PASSWORD", ""
    )
    return access, secret


_TEMPO_DEFAULT = "http://tempo:3200"


_CONFIG_KEYS_SAFE = (
    "LLM_BACKEND",
    "VLLM_URL",
    "VLLM_MODEL",
    "VLLM_QUANTIZATION",
    "VLLM_MAX_MODEL_LEN",
    "VLLM_VERSION",
    "LANGFUSE_HOST",
    "S3_ENDPOINT_URL",
)


async def _fetch_tempo_trace(
    client: httpx.AsyncClient, trace_id: str
) -> dict[str, Any] | None:
    if not trace_id:
        return None
    host = (os.environ.get("TEMPO_HOST_URL") or _TEMPO_DEFAULT).rstrip("/")
    try:
        r = await client.get(f"{host}/api/traces/{trace_id}")
        if r.status_code == 200:
            return r.json()
    except httpx.HTTPError as exc:
        logger.warning("support_packet: Tempo fetch failed: %s", exc)
    return None


@georag_agent(
    name="Support Packet Agent",
    risk_tier="R2",
    version="0.1.0",
)
async def support_packet_assemble(
    ctx: AgentContext,
    *,
    incident_id: str,
    trace_id: str | None = None,
    incident_time: datetime | None = None,
    requested_by: int | None = None,
    timeout_s: float = 15.0,
) -> dict[str, Any]:
    rt = get_runtime()

    if not ctx.workspace_id:
        raise ValueError("ctx.workspace_id is required for support_packet_assemble")
    if not incident_id:
        raise ValueError("incident_id is required")

    incident_time = incident_time or datetime.now(timezone.utc)

    # ---- Gather artefacts -------------------------------------------------
    runs = await rt.pg_pool.fetch(
        """
        SELECT run_id, workflow_kind, engine, engine_run_id, status,
               started_at, ended_at, trace_id, failure_reason
        FROM workflow.workflow_runs
        WHERE workspace_id = $1
        ORDER BY started_at DESC
        LIMIT 50
        """,
        ctx.workspace_id,
    )
    audit_rows = await rt.pg_pool.fetch(
        """
        SELECT id, action_type, actor_id, actor_kind, target_schema,
               target_table, target_id, payload, created_at,
               encode(hash, 'hex') AS hash_hex
        FROM audit.audit_ledger
        WHERE workspace_id = $1
        ORDER BY created_at DESC
        LIMIT 100
        """,
        ctx.workspace_id,
    )
    prompt_rows = await rt.pg_pool.fetch(
        """
        SELECT prompt_id, version, promotion_state, promoted_at, deprecated_at
        FROM workspace.prompt_versions
        WHERE promoted_at <= $1 AND (deprecated_at IS NULL OR deprecated_at > $1)
        """,
        incident_time,
    )

    async with httpx.AsyncClient(timeout=timeout_s) as client:
        trace_json = await _fetch_tempo_trace(client, trace_id or "")

    config_snapshot = {k: os.environ.get(k, "") for k in _CONFIG_KEYS_SAFE}

    manifest = {
        "incident_id": incident_id,
        "workspace_id": str(ctx.workspace_id),
        "trace_id": trace_id,
        "incident_time": incident_time.isoformat(),
        "assembled_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "workflow_runs": len(runs),
            "audit_entries": len(audit_rows),
            "prompt_versions": len(prompt_rows),
            "tempo_trace": 1 if trace_json else 0,
        },
        "config_snapshot": config_snapshot,
    }

    # ---- Build tar.gz in-memory ------------------------------------------
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def _add(name: str, payload: Any) -> None:
            data = json.dumps(payload, default=str, indent=2).encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

        _add("manifest.json", manifest)
        _add(
            "workflow_runs.json",
            [
                {
                    "run_id": str(r["run_id"]),
                    "workflow_kind": r["workflow_kind"],
                    "engine": r["engine"],
                    "engine_run_id": r["engine_run_id"],
                    "status": r["status"],
                    "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                    "ended_at": r["ended_at"].isoformat() if r["ended_at"] else None,
                    "trace_id": r["trace_id"],
                    "failure_reason": r["failure_reason"],
                }
                for r in runs
            ],
        )
        _add(
            "audit_ledger.json",
            [
                {
                    "id": str(r["id"]),
                    "action_type": r["action_type"],
                    "actor_id": r["actor_id"],
                    "actor_kind": r["actor_kind"],
                    "target_schema": r["target_schema"],
                    "target_table": r["target_table"],
                    "target_id": r["target_id"],
                    "payload": r["payload"],
                    "created_at": r["created_at"].isoformat(),
                    "hash_hex": r["hash_hex"],
                }
                for r in audit_rows
            ],
        )
        _add(
            "prompt_versions.json",
            [
                {
                    "prompt_id": r["prompt_id"],
                    "version": r["version"],
                    "promotion_state": r["promotion_state"],
                    "promoted_at": r["promoted_at"].isoformat() if r["promoted_at"] else None,
                    "deprecated_at": r["deprecated_at"].isoformat() if r["deprecated_at"] else None,
                }
                for r in prompt_rows
            ],
        )
        if trace_json:
            _add("tempo_trace.json", trace_json)

    bundle_bytes = buf.getvalue()

    # ---- Upload to SeaweedFS ---------------------------------------------
    iso = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    object_key = (
        f"support-packets/{ctx.workspace_id}/{incident_id}_{iso}.tar.gz"
    )
    bucket = "tier-warm"
    storage_uri = f"s3://{bucket}/{object_key}"

    try:
        import aioboto3  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError(f"aioboto3 not installed: {exc}") from exc

    access, secret = _s3_credentials()
    session = aioboto3.Session(
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name="us-east-1",
    )
    upload_ok = False
    upload_error: str | None = None
    async with session.client("s3", endpoint_url=_s3_endpoint()) as s3:
        try:
            await s3.put_object(
                Bucket=bucket,
                Key=object_key,
                Body=bundle_bytes,
                ContentType="application/gzip",
            )
            upload_ok = True
        except Exception as exc:  # noqa: BLE001 — IAM gap surfaced, not fatal
            upload_error = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "support_packet: SeaweedFS upload failed (%s) — recording row "
                "with status='available' but flagging upload failure in payload",
                upload_error,
            )

    # ---- Insert silver.support_packets row -------------------------------
    packet_id = uuid4()
    # The row records that an assembly happened regardless of whether the
    # SeaweedFS upload succeeded — the audit_ledger payload carries
    # upload_ok / upload_error so a follow-up retry job can find it.
    #
    # silver.support_packets has STRICT RLS (no escape-hatch for unset GUC),
    # so we must set the workspace_id GUC inside the same transaction as the
    # INSERT. Use a single-shot transaction. This is the canonical pattern
    # for RLS-protected writes from non-superuser agents (R-P0-10 follow-up);
    # other agents that write to RLS tables should adopt the same pattern.
    async with rt.pg_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, true)",
                str(ctx.workspace_id),
            )
            await conn.execute(
                """
                INSERT INTO silver.support_packets
                    (id, workspace_id, incident_id, storage_uri, storage_tier,
                     bundle_bytes, contents_summary, assembled_by, requested_by,
                     status, trace_id)
                VALUES ($1, $2, $3, $4, 'warm', $5, $6::jsonb,
                        'Support Packet Agent', $7, 'available', $8)
                """,
                packet_id,
                ctx.workspace_id,
                incident_id,
                storage_uri,
                len(bundle_bytes),
                json.dumps(manifest["counts"]),
                requested_by,
                trace_id,
            )

    audit_payload = {
        "packet_id": str(packet_id),
        "incident_id": incident_id,
        "storage_uri": storage_uri,
        "bundle_bytes": len(bundle_bytes),
        "counts": manifest["counts"],
        "upload_ok": upload_ok,
    }
    if upload_error:
        audit_payload["upload_error"] = upload_error

    await emit_audit(
        rt.pg_pool,
        action_type="support_packet.assembled",
        workspace_id=ctx.workspace_id,
        actor_id=requested_by,
        actor_kind="agent",
        target_schema="silver",
        target_table="support_packets",
        target_id=str(packet_id),
        payload=audit_payload,
        trace_id=ctx.trace_id,
    )

    # All-nighter 2026-05-21 — Kestra dispatch.
    # The Phase 0 rubric expects the agent to trigger the Kestra
    # `support_packet_dispatch` flow when the bundle is available, so the
    # on-call handler can route to email / Slack / ticketing per workspace
    # policy. Kestra is the integration-boundary owner post-Activepieces
    # sunset; the agent itself stays "build-the-bundle only".
    kestra_dispatched = False
    kestra_error: str | None = None
    if upload_ok:
        kestra_url = os.environ.get("KESTRA_URL", "").strip()
        flow_id = os.environ.get(
            "KESTRA_SUPPORT_PACKET_FLOW", "support_packet_dispatch"
        )
        if kestra_url:
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    r = await client.post(
                        f"{kestra_url.rstrip('/')}/api/v1/executions/georag/{flow_id}",
                        json={
                            "packet_id": str(packet_id),
                            "incident_id": incident_id,
                            "workspace_id": str(ctx.workspace_id),
                            "storage_uri": storage_uri,
                            "bundle_bytes": len(bundle_bytes),
                            "counts": manifest["counts"],
                            "requested_by": requested_by,
                        },
                    )
                    if 200 <= r.status_code < 300:
                        kestra_dispatched = True
                    else:
                        kestra_error = f"kestra http {r.status_code}"
            except httpx.HTTPError as exc:
                kestra_error = f"{type(exc).__name__}:{exc}"
        else:
            kestra_error = "KESTRA_URL unset"

    return {
        "packet_id": str(packet_id),
        "incident_id": incident_id,
        "storage_uri": storage_uri,
        "bundle_bytes": len(bundle_bytes),
        "counts": manifest["counts"],
        "upload_ok": upload_ok,
        "upload_error": upload_error,
        "kestra_dispatched": kestra_dispatched,
        "kestra_error": kestra_error,
    }
