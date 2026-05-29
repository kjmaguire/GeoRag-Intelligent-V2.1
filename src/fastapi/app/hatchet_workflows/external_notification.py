"""Phase 2 Step 5a — ``external_notification`` Hatchet workflow.

Inbound webhook bridge. An external integration layer (Kestra flow,
upstream SaaS webhook, etc.) authenticates the sender at its edge and
forwards the normalised payload to
``/internal/v1/integrations/external_notification/trigger``.

Shape: ``{notification_id, source, kind, payload, received_at}``.

This Hatchet workflow:

  1. checks the platform feature flag
     ``flows.external_notification.enabled``;
  2. de-duplicates by ``notification_id`` (idempotency — re-deliveries
     by the upstream are silently skipped);
  3. emits ``external_notification.received`` to ``audit.audit_ledger``
     with a structured payload.

Phase 2 deliberately does NOT build a dedicated notifications table —
``audit.audit_ledger`` already gives an append-only hash-chained
record + the Step 6 dashboard can read it directly. A structured
table moves to Phase 3 when downstream consumers need joinable shape.

Pool: ``ai`` (no I/O — fast). Action: ``external_notification``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time

import asyncpg
import redis.asyncio as aioredis
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.hatchet_workflows import hatchet


log = logging.getLogger("georag.hatchet.external_notification")

# Phase 3 Step 5 (R-P2-4) — HMAC sender authentication.
EXTERNAL_HMAC_SECRET_ENV = "EXTERNAL_NOTIFICATION_HMAC_SECRET"

# Phase 5 Step 1 (R-P4-1) — Redis token-bucket rate limit per sender.
RATE_LIMIT_BUCKET_PREFIX = "rl:external_notification:"
RATE_LIMIT_WINDOW_SECONDS = 60  # token bucket refill window


# =============================================================================
# IO models
# =============================================================================
class ExternalNotificationInput(BaseModel):
    """Sent by an external integration layer (Kestra flow / upstream SaaS webhook).

    The schema is intentionally generic — different external senders
    have different payload shapes. We keep ``payload`` as opaque dict
    and capture the small handful of fields the audit row needs to be
    queryable (notification_id, source, kind, received_at).
    """

    notification_id: str = Field(..., min_length=1, max_length=128,
        description="Idempotency key. Upstream-supplied; we de-dupe by this.")
    source: str = Field(..., min_length=1, max_length=128,
        description="The external sender's identifier (slack-app, partner-X, …).")
    kind: str = Field(..., min_length=1, max_length=64,
        description="The payload kind / event type (report_filed, ingest_request, …).")
    payload: dict = Field(default_factory=dict,
        description="Opaque payload from the sender. Recorded as-is.")
    received_at: str | None = Field(default=None,
        description="ISO-8601 timestamp the orchestrator stamped on receipt.")
    # Phase 3 Step 5 — sender HMAC. The orchestrator-side flow forwards
    # the upstream sender's signature unchanged; this Hatchet workflow
    # verifies before any side-effects (R-P2-4).
    signature: str | None = Field(default=None,
        description="HMAC-SHA256 hex over canonical JSON of "
                    "{notification_id, source, kind, payload, received_at}.")


class ExternalNotificationOut(BaseModel):
    skipped: bool = False
    reason: str | None = None
    notification_id: str
    source: str
    kind: str
    audit_id: str | None = None
    duration_ms: int


# =============================================================================
# Helpers
# =============================================================================
def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def canonical_json_for_hmac(input_data: "ExternalNotificationInput") -> bytes:
    """Phase 3 Step 5 — canonical-JSON serialisation used as the HMAC
    plaintext. Sorted keys, no whitespace, UTF-8. Senders MUST produce
    identical bytes; otherwise verify fails. Test fixture committed
    under tests/ as the cross-language source of truth."""
    payload = {
        "notification_id": input_data.notification_id,
        "source": input_data.source,
        "kind": input_data.kind,
        "payload": input_data.payload,
        "received_at": input_data.received_at,
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _hmac_hex(secret: str, payload: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_hmac_signature(input_data: "ExternalNotificationInput") -> tuple[bool, str | None]:
    """Sync verify against the env-var single secret. Phase 3 path —
    kept for backward compat + tests. Phase 4's
    ``verify_hmac_signature_async`` consults the per-sender registry
    first."""
    secret = os.environ.get(EXTERNAL_HMAC_SECRET_ENV, "")
    if not secret:
        return False, "hmac_secret_not_configured"
    if not input_data.signature:
        return False, "hmac_signature_missing"
    expected = _hmac_hex(secret, canonical_json_for_hmac(input_data))
    if hmac.compare_digest(expected, input_data.signature.lower()):
        return True, None
    return False, "hmac_signature_mismatch"


async def _lookup_sender_rate_limit(
    conn: asyncpg.Connection, source: str
) -> int | None:
    """Phase 5 Step 1 — return the per-minute rate limit for a sender,
    or None if unconfigured / no limit. Reads
    ``usage.external_notification_senders.rate_limit_per_minute``.
    Disabled rows return None (rate limit doesn't apply; the HMAC
    verifier will already reject them with a registry-mismatch reason)."""
    row = await conn.fetchrow(
        """
        SELECT rate_limit_per_minute
          FROM usage.external_notification_senders
         WHERE source = $1 AND disabled_at IS NULL
         ORDER BY created_at DESC
         LIMIT 1
        """,
        source,
    )
    return row["rate_limit_per_minute"] if row else None


def _redis_url() -> str:
    host = os.environ.get("REDIS_HOST", "redis")
    port = os.environ.get("REDIS_PORT", "6379")
    password = os.environ.get("REDIS_PASSWORD", "")
    if password:
        return f"redis://:{password}@{host}:{port}/0"
    return f"redis://{host}:{port}/0"


async def check_rate_limit(source: str, limit_per_minute: int) -> tuple[bool, int]:
    """Phase 5 Step 1 — fixed-window counter (simpler than a true token
    bucket; sufficient for "noisy sender DoSes the receive path"
    protection). Returns ``(allowed, current_count)``.

    Key: ``rl:external_notification:{source}:{minute_bucket}`` —
    minute_bucket = ``int(time.time() / 60)``. Redis INCR + EXPIRE 60s.

    Returns allowed=True if the increment lands at or below the limit;
    allowed=False otherwise. The counter still increments on rejection
    — over-limit senders see their count keep climbing until the
    minute rolls."""
    bucket = int(time.time() / RATE_LIMIT_WINDOW_SECONDS)
    key = f"{RATE_LIMIT_BUCKET_PREFIX}{source}:{bucket}"
    client = aioredis.from_url(_redis_url(), decode_responses=True)
    try:
        # Pipeline: INCR + EXPIRE (only effective on first INCR of bucket).
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, RATE_LIMIT_WINDOW_SECONDS + 5)
            results = await pipe.execute()
        count = int(results[0])
        return count <= limit_per_minute, count
    finally:
        await client.aclose()


async def _lookup_sender_secrets(
    conn: asyncpg.Connection, source: str
) -> list[tuple[str, str]]:
    """Phase 4 Step 1 — fetch the active per-sender HMAC secrets from
    ``usage.external_notification_senders`` via the
    ``lookup_external_notification_sender_secrets`` SECURITY DEFINER
    function. Returns list of ``(secret_kid, secret_plain)`` tuples,
    most-recent first; one row per active key (rotation can have
    multiple).

    The audit-encryption key GUC must be set on the connection before
    calling — the helper function reads it inside its own session,
    so the worker passes ``app.audit_encryption_key`` once before the
    SELECT and the function picks it up.
    """
    enc_key = os.environ.get("AUDIT_ENCRYPTION_KEY", "")
    if not enc_key:
        return []
    # `set_config(..., true)` is transaction-local; asyncpg auto-commits
    # each statement so the GUC vanishes before the next call. Wrap in an
    # explicit transaction so the SECURITY DEFINER function inherits it.
    async with conn.transaction():
        await conn.execute(
            "SELECT set_config('app.audit_encryption_key', $1, true)", enc_key,
        )
        rows = await conn.fetch(
            "SELECT secret_kid, secret_plain "
            "FROM usage.lookup_external_notification_sender_secrets($1)",
            source,
        )
    return [(r["secret_kid"], r["secret_plain"]) for r in rows]


async def verify_hmac_signature_async(
    conn: asyncpg.Connection,
    input_data: "ExternalNotificationInput",
) -> tuple[bool, str | None]:
    """Phase 4 Step 1 — multi-sender HMAC verify.

    Strategy:
      1. Look up active per-sender secrets in the registry. If any row
         matches the inbound signature, the request verifies.
      2. If the registry has no rows for this ``source`` (legacy /
         unconfigured), fall back to the env-var single secret. This
         backward-compat path is removed once every sender is
         registered.

    Returns ``(ok, reason)``. ``reason`` distinguishes the failure
    modes so the caller can audit + alert appropriately.
    """
    if not input_data.signature:
        return False, "hmac_signature_missing"

    canonical = canonical_json_for_hmac(input_data)
    inbound = input_data.signature.lower()

    try:
        sender_secrets = await _lookup_sender_secrets(conn, input_data.source)
    except Exception as e:
        log.warning("sender registry lookup failed for source=%s: %s",
                    input_data.source, e)
        sender_secrets = []

    if sender_secrets:
        for kid, plain in sender_secrets:
            if hmac.compare_digest(_hmac_hex(plain, canonical), inbound):
                return True, None
        return False, "hmac_signature_mismatch_registry"

    # Registry-empty fallback — Phase 3 single-secret path.
    env_secret = os.environ.get(EXTERNAL_HMAC_SECRET_ENV, "")
    if not env_secret:
        return False, "hmac_no_sender_registered"
    if hmac.compare_digest(_hmac_hex(env_secret, canonical), inbound):
        return True, None
    return False, "hmac_signature_mismatch_env_fallback"


async def _flag_enabled(conn: asyncpg.Connection) -> bool:
    # Phase 3 Step 3 — namespace `flows.<flow>.enabled` (legacy
    # activepieces.* keys dropped at Phase 3 Step 7 sunset).
    row = await conn.fetchrow(
        """
        SELECT bool_value
          FROM workspace.feature_flags
         WHERE workspace_id IS NULL
           AND flag_name = 'flows.external_notification.enabled'
        """
    )
    return bool(row and row["bool_value"])


async def _already_recorded(
    conn: asyncpg.Connection, notification_id: str
) -> str | None:
    """Return the audit_ledger.id for a prior recording of the same
    notification_id, or None if this is a first-time delivery."""
    row = await conn.fetchrow(
        """
        SELECT id::text AS id
          FROM audit.audit_ledger
         WHERE action_type = 'external_notification.received'
           AND payload->>'notification_id' = $1
         ORDER BY created_at DESC
         LIMIT 1
        """,
        notification_id,
    )
    return row["id"] if row else None


# =============================================================================
# Workflow
# =============================================================================
external_notification = hatchet.workflow(
    name="external_notification",
    input_validator=ExternalNotificationInput,
)


@external_notification.task(execution_timeout="30s", retries=1)
async def receive(
    input: ExternalNotificationInput, ctx: Context
) -> ExternalNotificationOut:
    t_start = time.monotonic()

    # Phase 5 Step 1 — rate limit BEFORE HMAC + flag checks so a noisy
    # sender can't DoS by burning CPU on HMAC verification.
    # Phase 4 Step 1 — HMAC verify against the per-sender registry
    # (with env-var fallback during the co-existence window). Verify
    # BEFORE the flag check so a tampered/unsigned payload doesn't
    # consult the flag and no audit row is written.
    _verify_pool = await asyncpg.create_pool(
        _dsn(), min_size=1, max_size=2, statement_cache_size=0,
    )
    try:
        async with _verify_pool.acquire() as _vc:
            limit = await _lookup_sender_rate_limit(_vc, input.source)
            if limit is not None:
                allowed, count = await check_rate_limit(input.source, limit)
                if not allowed:
                    log.warning(
                        "external_notification RATE LIMIT reject — "
                        "source=%s limit=%d count=%d",
                        input.source, limit, count,
                    )
                    return ExternalNotificationOut(
                        skipped=True,
                        reason=f"rate_limited:count={count}>{limit}",
                        notification_id=input.notification_id,
                        source=input.source,
                        kind=input.kind,
                        duration_ms=int((time.monotonic() - t_start) * 1000),
                    )
            hmac_ok, hmac_reason = await verify_hmac_signature_async(_vc, input)
    except Exception as e:
        log.warning("rate_limit / verify_hmac_signature_async errored: %s", e)
        hmac_ok, hmac_reason = verify_hmac_signature(input)
    finally:
        await _verify_pool.close()

    if not hmac_ok:
        log.warning(
            "external_notification HMAC reject — notification_id=%s source=%s reason=%s",
            input.notification_id, input.source, hmac_reason,
        )
        return ExternalNotificationOut(
            skipped=True,
            reason=f"hmac_verification_failed:{hmac_reason}",
            notification_id=input.notification_id,
            source=input.source,
            kind=input.kind,
            duration_ms=int((time.monotonic() - t_start) * 1000),
        )

    pool = await asyncpg.create_pool(
        _dsn(), min_size=1, max_size=2, statement_cache_size=0,
    )
    try:
        async with pool.acquire() as conn:
            if not await _flag_enabled(conn):
                return ExternalNotificationOut(
                    skipped=True,
                    reason="feature flag flows.external_notification.enabled = false",
                    notification_id=input.notification_id,
                    source=input.source,
                    kind=input.kind,
                    duration_ms=int((time.monotonic() - t_start) * 1000),
                )

            # Idempotency — same notification_id already audited?
            existing = await _already_recorded(conn, input.notification_id)
            if existing is not None:
                log.info(
                    "external_notification idempotent skip — notification_id=%s "
                    "already audited as %s",
                    input.notification_id, existing,
                )
                return ExternalNotificationOut(
                    skipped=True,
                    reason="duplicate notification_id",
                    notification_id=input.notification_id,
                    source=input.source,
                    kind=input.kind,
                    audit_id=existing,
                    duration_ms=int((time.monotonic() - t_start) * 1000),
                )

            # First delivery — emit_audit returns an AuditLedgerEntry.
            audit_id: str | None = None
            try:
                entry = await emit_audit(
                    conn,
                    action_type="external_notification.received",
                    workspace_id=None,
                    actor_id=None,
                    actor_kind="external",
                    target_schema="audit",
                    target_table="audit_ledger",
                    target_id=None,
                    payload={
                        "notification_id": input.notification_id,
                        "source": input.source,
                        "kind": input.kind,
                        "received_at": input.received_at,
                        "payload": input.payload,
                    },
                    trace_id=ctx.workflow_run_id,
                )
                audit_id = str(entry.id)
            except Exception as e:
                log.warning("external_notification audit emit failed: %s", e)

            log.info(
                "external_notification recorded notification_id=%s source=%s kind=%s",
                input.notification_id, input.source, input.kind,
            )

    finally:
        await pool.close()

    return ExternalNotificationOut(
        skipped=False,
        notification_id=input.notification_id,
        source=input.source,
        kind=input.kind,
        audit_id=audit_id,
        duration_ms=int((time.monotonic() - t_start) * 1000),
    )


__all__ = [
    "external_notification",
    "ExternalNotificationInput",
    "ExternalNotificationOut",
]
