"""@georag_agent decorator — the core operational-contract wrapper.

The decorator turns a plain async function into a fully-instrumented agent
invocation. Order of operations on each call:

    1.  Build AgentContext from kwargs + decorator metadata
    2.  Read agent_timeouts row (cached in-process, TTL 60s)
    3.  Check circuit breaker (Redis-backed per-workspace + global)
    4.  Compute idempotency key for R2+ (skip for R0/R1)
    5.  Look up idempotency_keys row → if hit, return stored result
    6.  Run the agent under asyncio.wait_for(hard_timeout)
    7.  Persist idempotency record on success (R2+)
    8.  Write usage_events if ctx.usage is non-empty
    9.  Update circuit breaker counters (success → reset; failure → increment)
    10. Emit audit_ledger entry
    11. Return AgentResult(value, outcome, ctx)

Failure-recovery dispatch is per the agent's risk tier, declared at
decoration time. Refusals (AgentRefusalError) are NOT counted as failures
for the circuit breaker — only timeouts, exceptions, and explicit
AgentError subclasses do that.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, TypeVar
from uuid import UUID, uuid4

import asyncpg

from app.audit import emit_audit
from .context import AgentContext, AgentOutcome
from .exceptions import (
    AgentCircuitOpenError,
    AgentError,
    AgentRefusalError,
    AgentTimeoutError,
)
from .runtime import get_runtime


T = TypeVar("T")
RiskTier = Literal["R0", "R1", "R2", "R3", "R4", "R5"]


@dataclass(slots=True)
class AgentResult[T]:
    """What the wrapper returns to the caller."""

    value: T | None
    outcome: AgentOutcome
    ctx: AgentContext
    duration_ms: int
    deduped: bool = False
    error: str | None = None


# In-process cache of agent_timeouts rows. Keyed by agent_name. TTL 60s.
_TIMEOUT_CACHE: dict[str, tuple[dict[str, Any], float]] = {}
_TIMEOUT_CACHE_TTL = 60.0


async def _load_timeout_policy(agent_name: str) -> dict[str, Any]:
    """Read agent_timeouts row, with short in-process cache."""
    now = time.monotonic()
    cached = _TIMEOUT_CACHE.get(agent_name)
    if cached and now - cached[1] < _TIMEOUT_CACHE_TTL:
        return cached[0]

    rt = get_runtime()
    row = await rt.pg_pool.fetchrow(
        """
        SELECT agent_name, risk_tier,
               soft_timeout_ms, hard_timeout_ms, retry_count,
               circuit_breaker_scope, failure_threshold, cool_down_seconds
        FROM workspace.agent_timeouts WHERE agent_name = $1
        """,
        agent_name,
    )

    # Sensible defaults if no row — Phase 0 seeder should populate every
    # agent; this fallback exists so tests + new-in-flight agents don't blow up.
    policy: dict[str, Any] = (
        dict(row)
        if row
        else {
            "agent_name": agent_name,
            "risk_tier": "R0",
            "soft_timeout_ms": 30_000,
            "hard_timeout_ms": 120_000,
            "retry_count": 0,
            "circuit_breaker_scope": "workspace",
            "failure_threshold": 5,
            "cool_down_seconds": 300,
        }
    )
    _TIMEOUT_CACHE[agent_name] = (policy, now)
    return policy


def _circuit_key(agent_name: str, workspace_id: UUID | None, scope: str) -> str:
    if scope == "global" or workspace_id is None:
        return f"georag:cb:{agent_name}:_global"
    return f"georag:cb:{agent_name}:{workspace_id}"


async def _circuit_check(agent_name: str, workspace_id: UUID | None, policy: dict[str, Any]) -> None:
    """Raise AgentCircuitOpenError if the breaker is open."""
    if policy["circuit_breaker_scope"] == "none":
        return
    rt = get_runtime()
    key = _circuit_key(agent_name, workspace_id, policy["circuit_breaker_scope"])
    val = await rt.redis.get(key)
    if val is None:
        return
    failures = int(val)
    if failures >= policy["failure_threshold"]:
        raise AgentCircuitOpenError(
            f"circuit open for {agent_name} (failures={failures}, "
            f"threshold={policy['failure_threshold']}, "
            f"cool_down_seconds={policy['cool_down_seconds']})"
        )


async def _circuit_record(
    agent_name: str,
    workspace_id: UUID | None,
    policy: dict[str, Any],
    *,
    success: bool,
) -> None:
    if policy["circuit_breaker_scope"] == "none":
        return
    rt = get_runtime()
    key = _circuit_key(agent_name, workspace_id, policy["circuit_breaker_scope"])
    if success:
        # Reset on any success.
        await rt.redis.delete(key)
    else:
        # Increment with TTL = cool_down_seconds (sliding).
        new_count = await rt.redis.incr(key)
        await rt.redis.expire(key, policy["cool_down_seconds"])
        # The breaker check happens BEFORE the next call; we don't trip here.


def _compute_idempotency_key(ctx: AgentContext) -> tuple[bytes, dict[str, Any]] | None:
    """Tier-specific idempotency key recipe. Returns (sha256, components) or None for R0/R1."""
    parts: list[str] = []
    components: dict[str, Any] = {}

    tier = ctx.risk_tier
    if tier in ("R0", "R1"):
        return None

    if tier == "R2":
        if not ctx.workspace_id or not ctx.document_id:
            raise ValueError(
                f"R2 idempotency requires ctx.workspace_id and ctx.document_id "
                f"(agent={ctx.agent_name})"
            )
        components = {
            "workspace_id": str(ctx.workspace_id),
            "document_id": ctx.document_id,
            "agent_name": ctx.agent_name,
            "agent_version": ctx.agent_version,
        }
    elif tier == "R3":
        if not ctx.workspace_id or not ctx.export_request_id:
            raise ValueError(
                f"R3 idempotency requires ctx.workspace_id and ctx.export_request_id "
                f"(agent={ctx.agent_name})"
            )
        components = {
            "workspace_id": str(ctx.workspace_id),
            "export_request_id": ctx.export_request_id,
            "agent_name": ctx.agent_name,
        }
    elif tier == "R4":
        if not (ctx.workspace_id and ctx.sync_target and ctx.sync_request_id):
            raise ValueError(
                f"R4 idempotency requires workspace_id, sync_target, sync_request_id "
                f"(agent={ctx.agent_name})"
            )
        components = {
            "workspace_id": str(ctx.workspace_id),
            "sync_target": ctx.sync_target,
            "sync_request_id": ctx.sync_request_id,
        }
    elif tier == "R5":
        if not (ctx.workspace_id and ctx.target_id and ctx.signoff_session_id):
            raise ValueError(
                f"R5 idempotency requires workspace_id, target_id, signoff_session_id "
                f"(agent={ctx.agent_name})"
            )
        components = {
            "workspace_id": str(ctx.workspace_id),
            "target_id": ctx.target_id,
            "signoff_session_id": ctx.signoff_session_id,
        }
    else:
        raise ValueError(f"unknown risk_tier: {tier}")

    serialized = json.dumps(components, sort_keys=True)
    digest = hashlib.sha256(serialized.encode("utf-8")).digest()
    return digest, components


async def _idempotency_lookup(key_hash: bytes) -> dict[str, Any] | None:
    rt = get_runtime()
    row = await rt.pg_pool.fetchrow(
        "SELECT id, result_summary, outcome, created_at "
        "FROM workspace.idempotency_keys WHERE key_hash = $1",
        key_hash,
    )
    return dict(row) if row else None


async def _idempotency_store(
    key_hash: bytes,
    components: dict[str, Any],
    ctx: AgentContext,
    result_summary: dict[str, Any] | None,
    outcome: AgentOutcome,
) -> None:
    rt = get_runtime()
    # TTL per risk tier — R2 caches a per-document result (30d window
    # covers a typical ingest-then-revisit cycle); R3+ caches per-request
    # results (90d window covers SOX-grade re-export windows). The
    # nightly idempotency_keys_cleanup workflow drops rows whose
    # expires_at < now() so the table doesn't grow unbounded.
    ttl_days = {"R2": 30, "R3": 90, "R4": 90, "R5": 90}.get(ctx.risk_tier, 30)
    await rt.pg_pool.execute(
        """
        INSERT INTO workspace.idempotency_keys
            (key_hash, key_components, risk_tier, workspace_id, agent_name,
             agent_version, invocation_id, result_summary, outcome,
             expires_at)
        VALUES ($1, $2::jsonb, $3, $4, $5, $6, $7, $8::jsonb, $9,
                now() + ($10::int * interval '1 day'))
        ON CONFLICT (key_hash) DO NOTHING
        """,
        key_hash,
        json.dumps(components),
        ctx.risk_tier,
        str(ctx.workspace_id) if ctx.workspace_id else None,
        ctx.agent_name,
        ctx.agent_version,
        str(ctx.invocation_id),
        json.dumps(result_summary or {}, default=str),
        outcome,
        ttl_days,
    )


async def _record_dry_run(
    ctx: AgentContext,
    target: str,
    payload: dict[str, Any],
) -> None:
    """Helper exposed via ctx-bound shim — not called directly here. The
    wrapper publishes ctx.dry_run; agents whose side-effect adapters detect it
    should call this function instead of executing.
    """
    rt = get_runtime()
    await rt.pg_pool.execute(
        """
        INSERT INTO workspace.dry_run_outputs
            (invocation_id, workspace_id, agent_name, target, payload)
        VALUES ($1, $2, $3, $4, $5::jsonb)
        """,
        str(ctx.invocation_id),
        str(ctx.workspace_id),
        ctx.agent_name,
        target,
        json.dumps(payload, default=str),
    )


# Process-local cache for Langfuse SDK presence. None = not yet probed.
# False = SDK unavailable or unconfigured (skip fast). True = ready.
_LANGFUSE_READY: bool | None = None


async def _emit_langfuse_trace(
    ctx: AgentContext,
    name: str,
    version: str,
    risk_tier: str,
    duration_ms: int,
    outcome: AgentOutcome,
    error: str | None,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    value: Any,
) -> None:
    """§35.1 contract — emit a Langfuse trace with all 11 required fields.

    Fields emitted:
      trace_id, agent_name, agent_version, workspace_id,
      parent_workflow_run_id, parent_graph_run_id, input_summary,
      output_summary, latency_ms, cost_attribution, outcome

    Implementation notes:
      * The Langfuse SDK is optional — if it isn't importable (dev box
        without the package installed, airgap deployment), this function
        silently no-ops. Audit-ledger + usage_events writes remain the
        primary record of every invocation and ALWAYS run.
      * `input_summary` / `output_summary` are PII-redacted: we record
        SHAPE + small primitive previews, never raw text/UUIDs that
        could carry tenant data. Agents that need richer payloads can
        attach them under `ctx.usage['langfuse_metadata']`.
      * The call is best-effort — any failure (network, SDK shape drift)
        is swallowed. Operational-contract correctness must not depend
        on Langfuse availability.
    """
    global _LANGFUSE_READY
    # Fast-path: if we've already determined Langfuse isn't usable in this
    # process, skip the import + env-probe entirely. Saves ~30ms per call
    # on workstations without Langfuse env vars set.
    if _LANGFUSE_READY is False:
        return

    rt = get_runtime()
    client = getattr(rt, "langfuse", None)
    if client is None:
        # First-time probe — try the import + lazy-init.
        try:
            from langfuse import Langfuse  # noqa: PLC0415
        except ImportError:
            _LANGFUSE_READY = False
            return
        import os  # noqa: PLC0415
        public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
        secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
        host = os.environ.get("LANGFUSE_HOST", "").strip() or None
        if not public_key or not secret_key:
            _LANGFUSE_READY = False
            return
        # langfuse-python 3.x deprecated the `host=` kwarg in favour of
        # `base_url=`. Passing the deprecated arg with v3 silently falls back
        # to the env var LANGFUSE_BASE_URL (browser-facing in our compose), which
        # isn't reachable from inside the container — the explicit `base_url=`
        # kwarg overrides any env-var conflict.
        client = Langfuse(public_key=public_key, secret_key=secret_key, base_url=host)
        try:
            rt.langfuse = client  # cache on runtime for next call
        except Exception:  # pragma: no cover
            pass
        _LANGFUSE_READY = True

    # PII-redacted input / output summaries — shape + small primitives only.
    def _summarise(obj: Any) -> dict[str, Any]:
        if obj is None:
            return {"type": "none"}
        if isinstance(obj, dict):
            return {
                "type": "dict",
                "keys": sorted(list(obj.keys()))[:20],
                "n_keys": len(obj),
            }
        if isinstance(obj, (list, tuple)):
            return {"type": type(obj).__name__, "len": len(obj)}
        if isinstance(obj, (int, float, bool)):
            return {"type": type(obj).__name__, "value": obj}
        return {"type": type(obj).__name__}

    input_summary = {
        "args_n": len(args),
        "kwargs": _summarise(kwargs),
    }
    output_summary = _summarise(value)

    cost_attribution = {
        "tokens_prompt": int(ctx.usage.get("tokens_prompt", 0)) if ctx.usage else 0,
        "tokens_completion": int(ctx.usage.get("tokens_completion", 0)) if ctx.usage else 0,
        "model": ctx.usage.get("model_id", "n/a") if ctx.usage else "n/a",
        "projected_cost_usd": float(ctx.usage.get("projected_cost_usd", 0)) if ctx.usage else 0.0,
    }

    metadata = {
        "agent_name": name,
        "agent_version": version,
        "risk_tier": risk_tier,
        "workspace_id": str(ctx.workspace_id) if ctx.workspace_id else None,
        "parent_workflow_run_id": (
            str(ctx.parent_workflow_run_id)
            if ctx.parent_workflow_run_id else None
        ),
        "parent_graph_run_id": (
            str(ctx.parent_graph_run_id)
            if ctx.parent_graph_run_id else None
        ),
        "input_summary": input_summary,
        "output_summary": output_summary,
        "latency_ms": duration_ms,
        "cost_attribution": cost_attribution,
        "outcome": outcome,
        "error": error,
    }

    try:
        # Wrap in asyncio.to_thread — the Langfuse SDK's create_event /
        # generation methods are sync; we don't want to block the event
        # loop on a network call inside the agent hot-path.
        #
        # langfuse-python 3.x signature change: `trace_id` is no longer a
        # direct kwarg — it lives inside the `trace_context` TypedDict.
        # `input` and `output` are now first-class kwargs (instead of being
        # nested in metadata) so the Langfuse UI's input/output panes render
        # them natively.
        await asyncio.to_thread(
            client.create_event,
            name=f"agent.{name}",
            trace_context={"trace_id": ctx.trace_id},
            input=input_summary,
            output=output_summary,
            metadata=metadata,
            level="DEFAULT" if outcome == "success" else "WARNING",
        )
    except Exception:  # pragma: no cover — best-effort
        pass


async def _write_usage_event(ctx: AgentContext, latency_ms: int, outcome: AgentOutcome) -> None:
    if not ctx.usage:
        return
    rt = get_runtime()
    u = ctx.usage
    await rt.pg_pool.execute(
        """
        INSERT INTO usage.usage_events
            (workspace_id, agent_name, agent_version, model_profile, model_id,
             tokens_prompt, tokens_completion, projected_cost_usd,
             latency_ms, outcome, trace_id, invocation_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        """,
        str(ctx.workspace_id) if ctx.workspace_id else None,
        ctx.agent_name,
        ctx.agent_version,
        u.get("model_profile", "n/a"),
        u.get("model_id"),
        int(u.get("tokens_prompt", 0)),
        int(u.get("tokens_completion", 0)),
        float(u.get("projected_cost_usd", 0)),
        latency_ms,
        outcome,
        ctx.trace_id,
        str(ctx.invocation_id),
    )


def georag_agent(
    *,
    name: str,
    risk_tier: RiskTier,
    version: str,
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[AgentResult[Any]]]]:
    """Decorator — wraps an async function in the operational contract.

    Required:
        name        — canonical agent name; matches workspace.agent_timeouts.agent_name
        risk_tier   — one of R0..R5
        version     — agent code/prompt version (semver-ish)
    """
    if risk_tier not in ("R0", "R1", "R2", "R3", "R4", "R5"):
        raise ValueError(f"invalid risk_tier: {risk_tier}")

    def decorator(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[AgentResult[Any]]]:
        @functools.wraps(fn)
        async def invoke(*args: Any, **kwargs: Any) -> AgentResult[Any]:
            # Pull AgentContext from kwargs (or build a fresh one).
            ctx: AgentContext = kwargs.pop("ctx", None) or AgentContext()
            ctx.agent_name = name
            ctx.agent_version = version
            ctx.risk_tier = risk_tier

            policy = await _load_timeout_policy(name)

            # Pre-flight: circuit breaker. We catch the exception here rather
            # than letting it propagate so the caller always gets an
            # AgentResult — never a raised exception just because the breaker
            # is open. The circuit check happens BEFORE the inner function
            # runs (so a tripped breaker doesn't pay the function's cost).
            t0 = time.monotonic()
            outcome: AgentOutcome
            value: Any = None
            error: str | None = None
            id_key: bytes | None = None
            id_components: dict[str, Any] | None = None

            try:
                await _circuit_check(name, ctx.workspace_id, policy)

                # Idempotency check (R2+ unless explicitly bypassed).
                if not ctx.bypass_idempotency:
                    idemp = _compute_idempotency_key(ctx)
                    if idemp is not None:
                        id_key, id_components = idemp
                        cached = await _idempotency_lookup(id_key)
                        if cached:
                            return AgentResult(
                                value=cached["result_summary"],
                                outcome="deduped",
                                ctx=ctx,
                                duration_ms=0,
                                deduped=True,
                            )

                value = await asyncio.wait_for(
                    fn(ctx, *args, **kwargs),
                    timeout=policy["hard_timeout_ms"] / 1000.0,
                )
                outcome = "success"
            except asyncio.TimeoutError:
                outcome = "timeout"
                error = f"hard_timeout_ms={policy['hard_timeout_ms']} exceeded"
            except AgentRefusalError as e:
                outcome = "refusal"
                error = str(e)
            except AgentCircuitOpenError as e:
                outcome = "circuit_open"
                error = str(e)
            except (AgentError, Exception) as e:
                outcome = "failure"
                error = f"{type(e).__name__}: {e}"

            duration_ms = int((time.monotonic() - t0) * 1000)

            # Update circuit breaker — failures count, refusals don't.
            await _circuit_record(
                name,
                ctx.workspace_id,
                policy,
                success=(outcome in ("success", "refusal")),
            )

            # Persist idempotency record on success (R2+).
            if outcome == "success" and id_key is not None and id_components is not None:
                result_summary = (
                    value
                    if isinstance(value, dict)
                    else {"value_repr": repr(value)[:200]}
                )
                try:
                    await _idempotency_store(id_key, id_components, ctx, result_summary, outcome)
                except Exception:  # pragma: no cover — best-effort
                    pass

            # Write usage event if the agent populated ctx.usage.
            try:
                await _write_usage_event(ctx, duration_ms, outcome)
            except Exception:  # pragma: no cover
                pass

            # Audit-ledger entry — every invocation, every outcome.
            try:
                rt = get_runtime()
                await emit_audit(
                    rt.pg_pool,
                    action_type=f"agent.invoke.{outcome}",
                    workspace_id=ctx.workspace_id,
                    actor_id=ctx.actor_id,
                    actor_kind="agent",
                    target_schema="workspace",
                    target_table="agent_timeouts",
                    target_id=name,
                    payload={
                        "agent_name": name,
                        "agent_version": version,
                        "risk_tier": risk_tier,
                        "invocation_id": str(ctx.invocation_id),
                        "duration_ms": duration_ms,
                        "outcome": outcome,
                        "dry_run": ctx.is_dry_run,
                        "error": error,
                    },
                    trace_id=ctx.trace_id,
                )
            except Exception:  # pragma: no cover
                pass

            # §35.1 Langfuse trace — fire-and-forget when Langfuse is
            # wired; skip entirely when the SDK isn't configured (env
            # vars missing). The audit_ledger entry above is the durable
            # record of truth; Langfuse is purely observability and must
            # not be in the agent hot-path.
            if _LANGFUSE_READY is not False:
                try:
                    asyncio.create_task(_emit_langfuse_trace(
                        ctx=ctx,
                        name=name,
                        version=version,
                        risk_tier=risk_tier,
                        duration_ms=duration_ms,
                        outcome=outcome,
                        error=error,
                        args=args,
                        kwargs=kwargs,
                        value=value,
                    ))
                except Exception:  # pragma: no cover
                    pass

            return AgentResult(
                value=value,
                outcome=outcome,
                ctx=ctx,
                duration_ms=duration_ms,
                deduped=False,
                error=error,
            )

        # Expose metadata so callers can introspect the wrapped function.
        invoke.agent_name = name  # type: ignore[attr-defined]
        invoke.risk_tier = risk_tier  # type: ignore[attr-defined]
        invoke.agent_version = version  # type: ignore[attr-defined]
        invoke.__georag_agent__ = True  # type: ignore[attr-defined]
        return invoke

    return decorator
