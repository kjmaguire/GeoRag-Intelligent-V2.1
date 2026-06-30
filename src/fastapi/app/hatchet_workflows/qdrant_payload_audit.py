"""Hourly Qdrant payload-shape audit for the georag_chunks collection.

Guard 2 for the 2026-06-01 outage. The startup healthcheck at
[app/main.py — section 6.5] catches schema mismatches at boot, but a
long-running FastAPI process won't notice if an ingest path silently
starts producing degenerate points 4 hours into a multi-day import.

This workflow runs every hour, scrolls a random-ish sample of points
from georag_chunks, and asserts each one carries the retrieval-required
payload keys (text + report_id + workspace_id). Missing keys mean the
point is invisible to the orchestrator — retrieval will return empty
chunks, the LLM will hit the "I don't have data on that in this
project" refusal branch, and the failure won't surface until a user
actually asks a question.

On violation the workflow:
  * Logs an ERROR with the offending point IDs + missing keys so it
    pages via the existing Loki "level=ERROR" alert rule.
  * Increments QDRANT_PAYLOAD_AUDIT_VIOLATIONS so the
    rate(...) > 0 Prom alert fires within ~5 minutes.
  * Emits an audit_ledger row for cross-tier forensics.

Sample size is bounded (default 50) so the run completes in well under
a second against a healthy collection. A non-existent collection or
zero points returns a clean run — that's a valid pre-ingest state and
is handled by the startup check, not here.
"""
from __future__ import annotations

import logging
import os
import time as _t

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field
from qdrant_client import AsyncQdrantClient

from app.audit import emit_audit
from app.hatchet_workflows import hatchet
from app.metrics import (
    QDRANT_PAYLOAD_AUDIT_RUNS,
    QDRANT_PAYLOAD_AUDIT_VIOLATIONS,
)

log = logging.getLogger("georag.hatchet.qdrant_payload_audit")


_COLLECTION = "georag_chunks"
_REQUIRED_KEYS = ("text", "report_id", "workspace_id")
_DEFAULT_SAMPLE = 50


class AuditInput(BaseModel):
    sample_size: int = Field(
        default=_DEFAULT_SAMPLE,
        ge=1,
        le=1000,
        description="Number of points to scroll per audit run.",
    )


class AuditOut(BaseModel):
    sampled: int
    violations: int
    duration_ms: int
    collection: str
    outcome: str  # "ok" | "violations" | "empty" | "missing_collection" | "transient"


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


qdrant_payload_audit_wf = hatchet.workflow(
    name="qdrant_payload_audit",
    on_crons=["0 * * * *"],  # top of every hour, UTC
    input_validator=AuditInput,
)


@qdrant_payload_audit_wf.task(execution_timeout="5m", retries=0)
async def run_audit(input: AuditInput, ctx: Context) -> AuditOut:
    t0 = _t.monotonic()
    sample = input.sample_size

    qclient = AsyncQdrantClient(
        host=os.environ.get("QDRANT_HOST", "qdrant"),
        port=int(os.environ.get("QDRANT_PORT", "6333")),
        api_key=os.environ.get("QDRANT_API_KEY") or None,
    )

    sampled = 0
    violations = 0
    offenders: list[dict] = []
    outcome = "ok"

    try:
        existing = {c.name for c in (await qclient.get_collections()).collections}
        if _COLLECTION not in existing:
            outcome = "missing_collection"
            log.warning(
                "qdrant_payload_audit: collection %s does not exist — "
                "skipping (the startup healthcheck handles this state).",
                _COLLECTION,
            )
        else:
            points, _ = await qclient.scroll(
                collection_name=_COLLECTION,
                limit=sample,
                with_payload=True,
                with_vectors=False,
            )
            sampled = len(points)
            if sampled == 0:
                outcome = "empty"
            else:
                for p in points:
                    payload = p.payload or {}
                    missing = [
                        k for k in _REQUIRED_KEYS
                        if k not in payload or payload[k] in (None, "")
                    ]
                    if missing:
                        violations += 1
                        offenders.append({
                            "point_id": str(p.id),
                            "missing": missing,
                            "have": sorted(payload.keys()),
                        })
                        for key in missing:
                            QDRANT_PAYLOAD_AUDIT_VIOLATIONS.labels(
                                collection=_COLLECTION, missing_key=key,
                            ).inc()
                if violations:
                    outcome = "violations"
                    log.error(
                        "qdrant_payload_audit: %d/%d sampled points violate "
                        "the retrieval payload contract on %s. Sample offenders: %s. "
                        "Chat will refuse on questions that hit these points — "
                        "investigate write paths immediately.",
                        violations, sampled, _COLLECTION, offenders[:5],
                    )
                else:
                    log.info(
                        "qdrant_payload_audit: %d points sampled from %s — "
                        "all carry required keys (%s)",
                        sampled, _COLLECTION, list(_REQUIRED_KEYS),
                    )
    except Exception as exc:  # noqa: BLE001
        outcome = "transient"
        log.exception(
            "qdrant_payload_audit: Qdrant unreachable / scroll failed: %s. "
            "Retry next hour.", exc,
        )
    finally:
        await qclient.close()

    QDRANT_PAYLOAD_AUDIT_RUNS.labels(outcome=outcome).inc()

    duration_ms = int((_t.monotonic() - t0) * 1000)

    # Audit-ledger row for forensics. Best-effort — never block the audit
    # itself on the audit write.
    try:
        pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2, statement_cache_size=0)
        try:
            async with pool.acquire() as conn:
                await emit_audit(
                    conn,
                    action_type="qdrant_payload_audit.run.complete",
                    actor_kind="workflow",
                    target_schema="qdrant",
                    target_table=_COLLECTION,
                    target_id=None,
                    payload={
                        "sampled": sampled,
                        "violations": violations,
                        "outcome": outcome,
                        "duration_ms": duration_ms,
                        "offenders_sample": offenders[:5],
                    },
                )
        finally:
            await pool.close()
    except Exception:  # pragma: no cover
        log.exception("qdrant_payload_audit: emit_audit failed (audit itself succeeded)")

    out = AuditOut(
        sampled=sampled,
        violations=violations,
        duration_ms=duration_ms,
        collection=_COLLECTION,
        outcome=outcome,
    )
    log.info("qdrant_payload_audit complete: %s", out.model_dump())
    return out


__all__ = ["qdrant_payload_audit_wf", "AuditInput", "AuditOut"]
