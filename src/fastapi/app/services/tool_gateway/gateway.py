"""§4.1 Tool Gateway — central dispatch.

Usage from an agent:

    from app.services.tool_gateway import (
        invoke_tool, ToolGatewayContext, register_tool,
    )

    # 1. Register your tool implementation (once, at module load)
    register_tool("retrieve_qdrant", retrieve_qdrant_impl)

    # 2. Invoke through the gateway
    result = await invoke_tool(
        ctx=ToolGatewayContext(
            pg_pool=pool,
            workspace_id=workspace_id,
            actor_user_id=user_id,
            actor_kind="agent",
            parent_run_id=workflow_run_id,
            trace_id=trace_id,
            dry_run=False,
        ),
        tool_name="retrieve_qdrant",
        inputs={"query": "...", "k": 10},
    )
    if not result.allowed:
        raise ToolBlocked(result.block_reason)
    use(result.output)

This is the entire policy + audit funnel. The actual tool
implementation receives only `inputs` and returns the output.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from uuid import UUID, uuid4

import asyncpg

from app.audit import emit_audit
from app.services.tool_gateway.policies import (
    RiskTier, has_approval, is_workspace_allowed, resolve_effective_tier,
)

log = logging.getLogger("georag.tool_gateway")


# In-process tool registry: tool_name → async impl(inputs: dict) -> Any
ToolImpl = Callable[[dict[str, Any]], Awaitable[Any]]
_TOOL_REGISTRY: dict[str, ToolImpl] = {}


def register_tool(name: str, impl: ToolImpl) -> None:
    """Register a tool implementation. The name must match a row in
    workspace.agent_risk_tiers, otherwise invoke_tool() will reject
    calls to it."""
    if name in _TOOL_REGISTRY:
        log.debug("tool_gateway: re-registering tool %s", name)
    _TOOL_REGISTRY[name] = impl


def is_tool_registered(name: str) -> bool:
    return name in _TOOL_REGISTRY


def list_registered_tools() -> list[str]:
    return sorted(_TOOL_REGISTRY.keys())


@dataclass(frozen=True, slots=True)
class ToolGatewayContext:
    """Per-call context passed to every gateway invocation."""

    pg_pool: asyncpg.Pool
    workspace_id: UUID | str
    actor_user_id: int | None = None
    actor_kind: str = "agent"  # user | agent | workflow | system
    parent_run_id: UUID | str | None = None
    trace_id: str | None = None
    dry_run: bool = False
    actor_metadata: dict[str, Any] | None = None  # e.g. {qp_credential_verified: True}


@dataclass(slots=True)
class ToolGatewayResult:
    """Outcome of a gateway invocation."""

    allowed: bool
    invocation_id: str
    risk_tier: str
    outcome: str  # allowed | dry_run | blocked | error
    output: Any = None
    block_reason: str | None = None
    duration_ms: int | None = None
    dry_run_id: str | None = None


def _canonical_hash(data: Any) -> str:
    """SHA-256 of canonical-JSON. Used for invocation idempotency +
    matching dry-run output to real-run output."""
    try:
        payload = json.dumps(data, sort_keys=True, default=str, separators=(",", ":"))
    except Exception:
        payload = repr(data)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _record_invocation(
    conn: asyncpg.Connection,
    *,
    workspace_id: str,
    actor_user_id: int | None,
    actor_kind: str,
    tool_name: str,
    risk_tier: str,
    outcome: str,
    block_reason: str | None,
    parent_run_id: str | None,
    trace_id: str | None,
    input_hash: str | None,
    output_hash: str | None,
    duration_ms: int | None,
) -> str:
    """Insert + return invocation_id."""
    row = await conn.fetchrow(
        """
        INSERT INTO workspace.tool_invocations
            (workspace_id, actor_user_id, actor_kind, tool_name, risk_tier,
             outcome, block_reason, parent_run_id, trace_id,
             input_hash, output_hash, duration_ms)
        VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $8::uuid, $9,
                $10, $11, $12)
        RETURNING invocation_id::text
        """,
        workspace_id, actor_user_id, actor_kind, tool_name, risk_tier,
        outcome, block_reason,
        str(parent_run_id) if parent_run_id else None,
        trace_id, input_hash, output_hash, duration_ms,
    )
    return row["invocation_id"]


async def _capture_dry_run(
    conn: asyncpg.Connection,
    *,
    invocation_id: str,
    workspace_id: str,
    tool_name: str,
    target: str,
    payload: dict[str, Any],
) -> str:
    """Persist what would-have-been-executed into workspace.dry_run_outputs."""
    row = await conn.fetchrow(
        """
        INSERT INTO workspace.dry_run_outputs
            (invocation_id, workspace_id, agent_name, target, payload)
        VALUES ($1::uuid, $2::uuid, $3, $4, $5::jsonb)
        RETURNING id::text
        """,
        invocation_id, workspace_id, tool_name, target, json.dumps(payload, default=str),
    )
    return row["id"]


async def invoke_tool(
    *,
    ctx: ToolGatewayContext,
    tool_name: str,
    inputs: dict[str, Any] | None = None,
) -> ToolGatewayResult:
    """The central dispatch. Single entry point for every agent tool call."""
    inputs = inputs or {}
    workspace_id_str = str(ctx.workspace_id)
    start = time.monotonic()
    input_hash = _canonical_hash(inputs)

    # 1. Resolve risk tier
    async with ctx.pg_pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id_str,
        )
        tier = await resolve_effective_tier(
            conn, workspace_id=workspace_id_str, tool_name=tool_name,
        )

    if tier is None:
        # Unregistered tool
        async with ctx.pg_pool.acquire() as conn:
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, false)", workspace_id_str,
            )
            invocation_id = await _record_invocation(
                conn, workspace_id=workspace_id_str,
                actor_user_id=ctx.actor_user_id, actor_kind=ctx.actor_kind,
                tool_name=tool_name, risk_tier="R5",  # unknown = worst-case
                outcome="blocked",
                block_reason=f"tool '{tool_name}' not registered in agent_risk_tiers",
                parent_run_id=str(ctx.parent_run_id) if ctx.parent_run_id else None,
                trace_id=ctx.trace_id, input_hash=input_hash,
                output_hash=None, duration_ms=0,
            )
        return ToolGatewayResult(
            allowed=False, invocation_id=invocation_id, risk_tier="R5",
            outcome="blocked", block_reason=f"tool not registered: {tool_name}",
        )

    # 2. Per-workspace permission check
    async with ctx.pg_pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id_str,
        )
        allowed, deny_reason = await is_workspace_allowed(
            conn, workspace_id=workspace_id_str, tool_name=tool_name,
        )

    if not allowed:
        async with ctx.pg_pool.acquire() as conn:
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, false)", workspace_id_str,
            )
            invocation_id = await _record_invocation(
                conn, workspace_id=workspace_id_str,
                actor_user_id=ctx.actor_user_id, actor_kind=ctx.actor_kind,
                tool_name=tool_name, risk_tier=tier.value,
                outcome="blocked", block_reason=deny_reason,
                parent_run_id=str(ctx.parent_run_id) if ctx.parent_run_id else None,
                trace_id=ctx.trace_id, input_hash=input_hash,
                output_hash=None, duration_ms=0,
            )
        return ToolGatewayResult(
            allowed=False, invocation_id=invocation_id, risk_tier=tier.value,
            outcome="blocked", block_reason=deny_reason,
        )

    # 3. R4+ approval check
    if tier.requires_approval:
        async with ctx.pg_pool.acquire() as conn:
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, false)", workspace_id_str,
            )
            approved, approval_reason = await has_approval(
                conn, workspace_id=workspace_id_str, tool_name=tool_name,
                actor_user_id=ctx.actor_user_id,
                actor_metadata=ctx.actor_metadata,
            )
        if not approved and not ctx.dry_run:
            async with ctx.pg_pool.acquire() as conn:
                await conn.execute(
                    "SELECT set_config('app.workspace_id', $1, false)", workspace_id_str,
                )
                invocation_id = await _record_invocation(
                    conn, workspace_id=workspace_id_str,
                    actor_user_id=ctx.actor_user_id, actor_kind=ctx.actor_kind,
                    tool_name=tool_name, risk_tier=tier.value,
                    outcome="blocked", block_reason=approval_reason,
                    parent_run_id=str(ctx.parent_run_id) if ctx.parent_run_id else None,
                    trace_id=ctx.trace_id, input_hash=input_hash,
                    output_hash=None, duration_ms=0,
                )
            return ToolGatewayResult(
                allowed=False, invocation_id=invocation_id, risk_tier=tier.value,
                outcome="blocked", block_reason=approval_reason,
            )

    # 4. Dispatch — dry-run OR real execution
    impl = _TOOL_REGISTRY.get(tool_name)
    output: Any = None
    output_hash: str | None = None
    outcome_str: str
    dry_run_id: str | None = None

    if ctx.dry_run or (tier.requires_audit and impl is None):
        # Either explicit dry-run, or impl missing for an audited tier
        # (better to capture intent than to silently no-op).
        outcome_str = "dry_run"
        # Generate the invocation_id up front so dry_run_outputs can point at it
        async with ctx.pg_pool.acquire() as conn:
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, false)", workspace_id_str,
            )
            invocation_id = str(uuid4())
            await conn.execute(
                """
                INSERT INTO workspace.tool_invocations
                    (invocation_id, workspace_id, actor_user_id, actor_kind,
                     tool_name, risk_tier, outcome, parent_run_id, trace_id,
                     input_hash)
                VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, 'dry_run',
                        $7::uuid, $8, $9)
                """,
                invocation_id, workspace_id_str, ctx.actor_user_id, ctx.actor_kind,
                tool_name, tier.value,
                str(ctx.parent_run_id) if ctx.parent_run_id else None,
                ctx.trace_id, input_hash,
            )
            dry_run_id = await _capture_dry_run(
                conn,
                invocation_id=invocation_id,
                workspace_id=workspace_id_str,
                tool_name=tool_name,
                target=tool_name,
                payload={"inputs": inputs, "would_have_run_with_impl": impl is not None},
            )
        duration_ms = int((time.monotonic() - start) * 1000)
        return ToolGatewayResult(
            allowed=True, invocation_id=invocation_id, risk_tier=tier.value,
            outcome=outcome_str, output=None,
            duration_ms=duration_ms, dry_run_id=dry_run_id,
        )

    if impl is None:
        # Non-audited tier with no registered impl is a programmer error.
        log.error("tool_gateway: tool '%s' registered in DB but no impl bound", tool_name)
        async with ctx.pg_pool.acquire() as conn:
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, false)", workspace_id_str,
            )
            invocation_id = await _record_invocation(
                conn, workspace_id=workspace_id_str,
                actor_user_id=ctx.actor_user_id, actor_kind=ctx.actor_kind,
                tool_name=tool_name, risk_tier=tier.value,
                outcome="error",
                block_reason="no implementation registered (gateway misconfigured)",
                parent_run_id=str(ctx.parent_run_id) if ctx.parent_run_id else None,
                trace_id=ctx.trace_id, input_hash=input_hash,
                output_hash=None, duration_ms=0,
            )
        return ToolGatewayResult(
            allowed=False, invocation_id=invocation_id, risk_tier=tier.value,
            outcome="error", block_reason="no implementation registered",
        )

    # Real execution
    try:
        output = await impl(inputs)
        outcome_str = "allowed"
        output_hash = _canonical_hash(output)
    except Exception as exc:  # noqa: BLE001
        log.exception("tool_gateway: tool %s impl raised", tool_name)
        duration_ms = int((time.monotonic() - start) * 1000)
        async with ctx.pg_pool.acquire() as conn:
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, false)", workspace_id_str,
            )
            invocation_id = await _record_invocation(
                conn, workspace_id=workspace_id_str,
                actor_user_id=ctx.actor_user_id, actor_kind=ctx.actor_kind,
                tool_name=tool_name, risk_tier=tier.value,
                outcome="error", block_reason=f"{type(exc).__name__}: {exc}",
                parent_run_id=str(ctx.parent_run_id) if ctx.parent_run_id else None,
                trace_id=ctx.trace_id, input_hash=input_hash,
                output_hash=None, duration_ms=duration_ms,
            )
        return ToolGatewayResult(
            allowed=False, invocation_id=invocation_id, risk_tier=tier.value,
            outcome="error", block_reason=str(exc), duration_ms=duration_ms,
        )

    # 5. Record invocation + emit audit row for R3+
    duration_ms = int((time.monotonic() - start) * 1000)
    async with ctx.pg_pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id_str,
        )
        invocation_id = await _record_invocation(
            conn, workspace_id=workspace_id_str,
            actor_user_id=ctx.actor_user_id, actor_kind=ctx.actor_kind,
            tool_name=tool_name, risk_tier=tier.value,
            outcome=outcome_str, block_reason=None,
            parent_run_id=str(ctx.parent_run_id) if ctx.parent_run_id else None,
            trace_id=ctx.trace_id, input_hash=input_hash,
            output_hash=output_hash, duration_ms=duration_ms,
        )

    if tier.requires_audit:
        try:
            await emit_audit(
                ctx.pg_pool,
                action_type=f"tool.{tool_name}",
                workspace_id=ctx.workspace_id if isinstance(ctx.workspace_id, UUID) else UUID(workspace_id_str),
                actor_id=ctx.actor_user_id,
                actor_kind=ctx.actor_kind,  # type: ignore[arg-type]
                target_schema="workspace",
                target_table="tool_invocations",
                target_id=invocation_id,
                payload={
                    "tool_name": tool_name, "risk_tier": tier.value,
                    "input_hash": input_hash, "output_hash": output_hash,
                    "duration_ms": duration_ms,
                },
                trace_id=ctx.trace_id,
            )
        except Exception:
            log.exception(
                "tool_gateway: audit emission failed for %s/%s",
                tool_name, invocation_id,
            )

    return ToolGatewayResult(
        allowed=True, invocation_id=invocation_id, risk_tier=tier.value,
        outcome=outcome_str, output=output, duration_ms=duration_ms,
    )


__all__ = [
    "ToolGatewayContext",
    "ToolGatewayResult",
    "invoke_tool",
    "register_tool",
    "is_tool_registered",
    "list_registered_tools",
]
