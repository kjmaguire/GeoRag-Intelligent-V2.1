"""§10.12 — cross-workspace access audit emission.

Emits a ``security.cross_workspace_access.alert`` audit row whenever
an authenticated request attempts to touch a workspace that does
not match the workspace derived from the user's JWT (most commonly,
an ``X-Workspace-Id`` header pointing at a different workspace than
the JWT-bound one).

Idempotent within a configurable window (default 1 hour): the same
``(actor_id, target_workspace_id)`` pair emits one row per window
to avoid alert-storming. Window state is held in Redis with
``SET NX EX``. If Redis is unavailable, the audit row is emitted
anyway (fail-open: a duplicate row is preferable to a missed alert).

The ``.alert`` suffix lands the row in the §5 alerts inbox where
operators can ack it. The hash-chain trigger covers integrity.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import asyncpg

from app.audit import emit_audit

logger = logging.getLogger(__name__)

# Locked default per §10 kickoff
DEFAULT_IDEMPOTENCY_WINDOW_S: int = 3600  # 1 hour

_REDIS_KEY_PREFIX = "georag:xworkspace_audit"


def _idempotency_key(actor_id: int | None, target_workspace_id: UUID) -> str:
    return f"{_REDIS_KEY_PREFIX}:{actor_id or 0}:{target_workspace_id}"


async def emit_cross_workspace_alert(
    pool_or_conn: asyncpg.Pool | asyncpg.Connection,
    *,
    actor_user_id: int | None,
    jwt_workspace_id: UUID | None,
    target_workspace_id: UUID,
    request_path: str | None = None,
    trace_id: str | None = None,
    redis_client: Any | None = None,
    window_s: int = DEFAULT_IDEMPOTENCY_WINDOW_S,
) -> bool:
    """Emit a cross-workspace access alert.

    Returns ``True`` if a row was emitted, ``False`` if the call was
    de-duplicated by the Redis idempotency window.

    The alert is best-effort: any exception during emission is logged
    but not re-raised — callers are typically inside a 403 path and
    shouldn't have the alert emission turn a 403 into a 500.
    """
    # Idempotency check (best-effort — fail open if Redis is down).
    if redis_client is not None:
        key = _idempotency_key(actor_user_id, target_workspace_id)
        try:
            # SET NX EX: returns truthy ("OK") if the key was set,
            # None if it already existed within the window.
            set_ok = await redis_client.set(key, "1", ex=window_s, nx=True)
            if not set_ok:
                logger.debug(
                    "cross_workspace_audit: deduped within %ds window "
                    "actor=%s target=%s",
                    window_s, actor_user_id, target_workspace_id,
                )
                return False
        except Exception:
            logger.warning(
                "cross_workspace_audit: Redis idempotency check failed; "
                "emitting alert anyway",
                exc_info=True,
            )

    payload = {
        "actor_user_id": actor_user_id,
        "jwt_workspace_id": str(jwt_workspace_id) if jwt_workspace_id else None,
        "target_workspace_id": str(target_workspace_id),
        "request_path": request_path,
        "idempotency_window_s": window_s,
    }

    # audit.audit_ledger.workspace_id has FK → workspaces; the legitimate
    # anchor is the user's JWT-derived workspace (always a real row).
    # target_workspace_id stays in the payload + target_id only.
    try:
        await emit_audit(
            pool_or_conn,
            action_type="security.cross_workspace_access.alert",
            workspace_id=jwt_workspace_id,  # FK-safe; payload carries target
            actor_id=actor_user_id,
            actor_kind="user" if actor_user_id else "system",
            target_schema="security",
            target_table="cross_workspace_access",
            target_id=str(target_workspace_id),
            payload=payload,
            trace_id=trace_id,
        )
        return True
    except Exception:
        logger.exception(
            "cross_workspace_audit: emit_audit failed actor=%s target=%s",
            actor_user_id, target_workspace_id,
        )
        return False


__all__ = [
    "DEFAULT_IDEMPOTENCY_WINDOW_S",
    "emit_cross_workspace_alert",
]
