"""Z.1 / Appendix C §5 — External-LLM egress profile gate.

This module is the load-bearing pre-check for any LLM call that would
leave the GeoRAG trust boundary (currently: Anthropic Messages API on
the ``LLM_BACKEND=anthropic`` fallback path; other third-party providers
would route through the same gate).

The gate reads the active workspace's ``allow_external_llm`` policy
flag (stored in ``silver.workspace_settings.extra_payload`` as the
``allow_external_llm: bool`` key, per Appendix C §5 wording
``profile.workspace_settings.allow_external_llm``). The gate refuses
the call by raising :class:`ExternalLlmEgressBlocked` when the flag is
absent or false.

Contract
--------
- Default deny. A missing flag, a NULL row, or an unreachable DB all
  fail closed (refuse the call). The only way to *permit* egress is an
  explicit ``true`` in the workspace settings JSONB.
- System-level calls with no workspace (``workspace_id is None``) are
  refused. The caller MUST provide a workspace_id for any path that
  emits user/document text to Anthropic. (Internal admin scripts that
  legitimately need to bypass should use the vLLM backend.)
- The gate is purely Pythonic: no LLM SDK imports, no network beyond
  the one asyncpg call. It is safe to import from any module without
  pulling in the Anthropic dependency.

The raised :class:`ExternalLlmEgressBlocked` carries a
:class:`~app.agent.guards.GuardErrorCode` so the orchestrator's existing
typed-guard render path can surface the user-facing message defined in
``lang/en/guard_errors.php`` (key ``EGRESS_BLOCKED``).

Wiring
------
Production call site: :func:`app.agent.llm_calls._call_anthropic_llm`
invokes :func:`assert_external_llm_allowed` at the top, BEFORE the
Anthropic client is constructed and BEFORE any prompt content is
serialised. Test paths can call :func:`evaluate_external_llm_policy`
directly to inspect the decision without raising.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


__all__ = [
    "ExternalLlmEgressBlocked",
    "evaluate_external_llm_policy",
    "assert_external_llm_allowed",
]


# Default-deny user-facing message text. Mirrors the
# lang/en/guard_errors.php EGRESS_BLOCKED template so the FastAPI side
# can render the message without a Laravel round-trip (the React layer
# still uses the Laravel translation for i18n; this string is the
# fail-safe used by tests and direct API consumers).
_EGRESS_BLOCKED_USER_MESSAGE: str = (
    "External LLM access is disabled for this workspace. "
    "Contact your admin to enable."
)


class ExternalLlmEgressBlocked(RuntimeError):
    """Raised when :func:`assert_external_llm_allowed` refuses an
    external-LLM call because the active workspace has not opted in.

    Carries the :class:`~app.agent.guards.GuardErrorCode.EGRESS_BLOCKED`
    code so the orchestrator's exception handler can surface the typed
    guard error (renders via ``lang/en/guard_errors.php``).

    Attributes:
        workspace_id: The workspace whose policy refused the call.
            May be None when the call was rejected because no workspace
            context was supplied at all.
        reason: A short machine-readable tag. One of:
            ``"missing_workspace"`` — caller provided no workspace_id.
            ``"flag_not_set"`` — ``allow_external_llm`` key absent.
            ``"flag_disabled"`` — flag explicitly false.
            ``"db_error"`` — settings lookup failed; default-deny.
    """

    def __init__(
        self,
        workspace_id: str | None,
        reason: str = "flag_not_set",
        *,
        user_message: str = _EGRESS_BLOCKED_USER_MESSAGE,
    ) -> None:
        # Local import to avoid a hard cycle (guards has no upstream
        # deps; egress_gate is allowed to depend on it).
        from app.agent.guards import GuardErrorCode  # noqa: PLC0415

        self.workspace_id = workspace_id
        self.reason = reason
        self.guard_code = GuardErrorCode.EGRESS_BLOCKED
        self.user_message = user_message
        super().__init__(
            f"external-LLM egress blocked for workspace={workspace_id!r} "
            f"(reason={reason})"
        )


async def _fetch_allow_external_llm_flag(
    pg_pool: Any,
    workspace_id: str,
) -> bool | None:
    """Read ``allow_external_llm`` from ``silver.workspace_settings.extra_payload``.

    Returns ``True`` / ``False`` when the key is present with a boolean
    value; returns ``None`` when:
        - the workspace has no settings row (default-deny),
        - the key is absent from extra_payload (default-deny),
        - the row exists but the value is not a bool (default-deny),
        - the DB lookup fails for any reason (default-deny; logged at WARN).

    The caller treats every ``None`` return as a hard refuse.
    """
    if pg_pool is None:
        return None
    try:
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT extra_payload "
                "FROM silver.workspace_settings "
                "WHERE workspace_id = $1::uuid",
                workspace_id,
            )
    except Exception:
        logger.warning(
            "_fetch_allow_external_llm_flag: settings lookup failed for "
            "workspace=%s — failing closed",
            workspace_id, exc_info=True,
        )
        return None
    if row is None:
        return None
    payload = row["extra_payload"]
    # asyncpg returns jsonb as a dict (with codec) or as a JSON string
    # when no codec is registered. Handle both shapes.
    if isinstance(payload, str):
        import json  # noqa: PLC0415
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("allow_external_llm")
    if isinstance(value, bool):
        return value
    return None


async def evaluate_external_llm_policy(
    *,
    workspace_id: str | None,
    pg_pool: Any = None,
) -> tuple[bool, str]:
    """Decide whether an external-LLM call is permitted for ``workspace_id``.

    Returns a ``(allowed, reason)`` tuple. ``allowed`` is True iff the
    workspace explicitly opted in via ``profile.allow_external_llm = true``
    (currently stored under ``silver.workspace_settings.extra_payload``).

    ``reason`` is one of:

        - ``"flag_enabled"`` — opt-in present and true; call is permitted.
        - ``"missing_workspace"`` — no workspace context supplied.
        - ``"flag_not_set"`` — settings row missing or key absent.
        - ``"flag_disabled"`` — key present and explicitly false.
        - ``"db_error"`` — DB lookup failed; default-deny.

    Pure async decision function: no exceptions, no side effects beyond
    a single read. Used by :func:`assert_external_llm_allowed` and by
    the standalone gate tests.
    """
    if not workspace_id:
        return False, "missing_workspace"

    flag = await _fetch_allow_external_llm_flag(pg_pool, workspace_id)

    if flag is True:
        return True, "flag_enabled"
    if flag is False:
        return False, "flag_disabled"
    # None covers: missing row, missing key, malformed payload, DB error.
    # Per default-deny, we treat all of these as flag_not_set unless
    # we have a positive signal otherwise. (db_error is logged inside
    # _fetch_allow_external_llm_flag but folded into flag_not_set here
    # so the caller's reason tag stays stable for tests.)
    return False, "flag_not_set"


async def assert_external_llm_allowed(
    *,
    workspace_id: str | None,
    pg_pool: Any = None,
) -> None:
    """Refuse the call if external-LLM egress is not permitted for the
    current workspace.

    Raises :class:`ExternalLlmEgressBlocked` on refusal — the
    orchestrator's existing typed-guard handler catches this and
    surfaces ``GuardErrorCode.EGRESS_BLOCKED``. The Anthropic client
    must not be invoked when this function raises.

    No-op (returns None) on the permitted path so the caller can wrap
    every Anthropic entry point with a single ``await assert_…(…)`` line
    without conditional branching.
    """
    allowed, reason = await evaluate_external_llm_policy(
        workspace_id=workspace_id,
        pg_pool=pg_pool,
    )
    if not allowed:
        logger.warning(
            "assert_external_llm_allowed: refusing Anthropic call — "
            "workspace=%s reason=%s",
            workspace_id, reason,
        )
        try:
            from app.metrics import EXTERNAL_LLM_EGRESS_BLOCKED  # noqa: PLC0415
            EXTERNAL_LLM_EGRESS_BLOCKED.labels(reason=reason).inc()
        except (ImportError, AttributeError):
            # Metric is optional — never let metric absence break the gate.
            pass
        raise ExternalLlmEgressBlocked(workspace_id, reason=reason)
