"""§5 — hourly cost-burn watcher.

Schedule: ``*/5 * * * *`` UTC (every 5 minutes).

What this workflow does
=======================

For each workspace with LLM usage in the last hour:

1. Sum ``usage.usage_events.projected_cost_usd`` over the trailing
   1 h window.
2. Compare against the per-workspace threshold. Resolution order:
   a. ``usage.workspace_cost_ceilings.monthly_ceiling_usd / 720``
      (730.5 hours per month rounded to 720 for a tight ceiling)
   b. fall back to ``COST_BURN_THRESHOLD_USD_PER_HOUR`` env var
   c. fall back to the hard-coded $5.00/h dev default
3. If above threshold AND no unacknowledged ``cost.burn.alert`` exists
   for this workspace_id in the last hour → emit one.

Idempotency
===========

A workspace that's been over-budget for 30 minutes should produce ONE
alert, not six (one per 5-minute cron firing). The idempotency check
queries ``audit.audit_ledger`` for any ``cost.burn.alert`` row where
``target_id = workspace_id::text`` AND ``created_at > now() - 1h``
AND no matching ``cost.burn.alert.acknowledged`` counter row exists.

Operators acknowledge via the existing Phase H4 alerts inbox; once
acked the watcher will re-emit on the next over-threshold reading.

Severity
========

Severity is fixed at ``high`` for now. A future iteration could
escalate to ``critical`` based on multiplier-of-threshold (e.g. 3x =
critical, 5x = critical + pager) — out of scope for v1.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.hatchet_workflows import hatchet

log = logging.getLogger("georag.hatchet.cost_burn_watcher")


# Hours in a 30-day month — used to derive an hourly threshold from
# the monthly ceiling. 30 * 24 = 720. Tighter than the 730.5 calendar
# average so workspaces with a hard monthly ceiling don't overrun.
_HOURS_PER_BUDGETED_MONTH = 720


class CostBurnWatcherInput(BaseModel):
    window_minutes: int = Field(
        default=60, ge=15, le=1440,
        description="Sliding window over which spend is summed. Default 1h.",
    )
    default_threshold_usd: float = Field(
        default=5.0, ge=0.01,
        description="Per-hour threshold when no workspace_cost_ceilings row "
                    "exists. Override via COST_BURN_THRESHOLD_USD_PER_HOUR.",
    )


class CostBurnWatcherOutput(BaseModel):
    workspaces_checked: int
    workspaces_over_threshold: int
    alerts_emitted: int
    alerts_suppressed_idempotent: int
    window_minutes: int
    sampled_at: datetime


cost_burn_watcher = hatchet.workflow(
    name="cost_burn_watcher",
    on_crons=["*/5 * * * *"],
    input_validator=CostBurnWatcherInput,
)


def _build_dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _env_threshold(default: float) -> float:
    raw = os.environ.get("COST_BURN_THRESHOLD_USD_PER_HOUR")
    if not raw:
        return default
    try:
        v = float(raw)
        return v if v > 0 else default
    except ValueError:
        log.warning("COST_BURN_THRESHOLD_USD_PER_HOUR=%r not parseable; using %s", raw, default)
        return default


async def _resolve_threshold_for_workspace(
    conn: asyncpg.Connection, workspace_id: str, env_default: float,
) -> float:
    """Per-workspace hourly threshold.

    Priority:
      1. usage.workspace_cost_ceilings.monthly_ceiling_usd / 720
      2. env var COST_BURN_THRESHOLD_USD_PER_HOUR
      3. hard-coded fallback
    """
    row = await conn.fetchrow(
        """
        SELECT monthly_ceiling_usd
          FROM usage.workspace_cost_ceilings
         WHERE workspace_id = $1::uuid
        """,
        workspace_id,
    )
    if row and row["monthly_ceiling_usd"] and row["monthly_ceiling_usd"] > 0:
        return float(row["monthly_ceiling_usd"]) / _HOURS_PER_BUDGETED_MONTH
    return env_default


async def _has_recent_unacked_alert(
    conn: asyncpg.Connection, workspace_id: str, window_minutes: int,
) -> bool:
    """True if a `cost.burn.alert` exists for this workspace_id within
    the window AND no matching `.acknowledged` counter row exists.

    Implements the watcher's idempotency contract: don't spam alerts
    while the operator hasn't yet acked.
    """
    row = await conn.fetchrow(
        f"""
        SELECT 1
          FROM audit.audit_ledger a
         WHERE a.action_type = 'cost.burn.alert'
           AND a.target_id   = $1::text
           AND a.created_at  > now() - interval '{int(window_minutes)} minutes'
           AND NOT EXISTS (
                 SELECT 1 FROM audit.audit_ledger ack
                  WHERE ack.action_type = 'cost.burn.alert.acknowledged'
                    AND ack.target_id   = a.target_id
                    AND ack.created_at  > a.created_at
           )
         LIMIT 1
        """,
        workspace_id,
    )
    return row is not None


@cost_burn_watcher.task(execution_timeout="2m")
async def run_watch(input: CostBurnWatcherInput, ctx: Context) -> CostBurnWatcherOutput:
    sampled_at = datetime.now(tz=UTC)
    env_default = _env_threshold(input.default_threshold_usd)

    conn = await asyncpg.connect(_build_dsn(), statement_cache_size=0)
    workspaces_checked = 0
    over_threshold = 0
    alerts_emitted = 0
    alerts_suppressed = 0
    try:
        # Per-workspace hourly cost in the trailing window. NULL workspace
        # rows skipped (system-level LLM calls aren't workspace-scoped).
        rows = await conn.fetch(
            f"""
            SELECT workspace_id::text     AS workspace_id,
                   SUM(projected_cost_usd)::float AS hourly_spent_usd,
                   COUNT(*)               AS event_count
              FROM usage.usage_events
             WHERE workspace_id IS NOT NULL
               AND created_at > now() - interval '{int(input.window_minutes)} minutes'
             GROUP BY workspace_id
            HAVING SUM(projected_cost_usd) > 0
            """,
        )

        for r in rows:
            workspaces_checked += 1
            ws_id = r["workspace_id"]
            spent = float(r["hourly_spent_usd"])
            threshold = await _resolve_threshold_for_workspace(
                conn, ws_id, env_default,
            )
            if spent <= threshold:
                continue
            over_threshold += 1

            if await _has_recent_unacked_alert(conn, ws_id, input.window_minutes):
                alerts_suppressed += 1
                continue

            await emit_audit(
                conn,
                action_type="cost.burn.alert",
                workspace_id=ws_id,
                actor_id=None,
                actor_kind="workflow",
                target_schema="usage",
                target_table="usage_events",
                target_id=ws_id,
                payload={
                    "severity":          "high",
                    "spent_usd":         round(spent, 4),
                    "threshold_usd":     round(threshold, 4),
                    "window_minutes":    input.window_minutes,
                    "event_count":       int(r["event_count"]),
                    "watcher_sampled":   sampled_at.isoformat(),
                    "source":            (
                        "workspace_cost_ceilings"
                        if threshold != env_default
                        else "env_default"
                    ),
                },
            )
            alerts_emitted += 1

            # Hard-stop §35.1: when hourly spend is 2× the threshold —
            # i.e. the workspace has been burning past the cap for at
            # least a window's worth of LLM calls — suspend further
            # LLM activity until admin override or period rollover.
            # The 2× factor is a guard against transient bursts; a
            # single big query that puts a workspace 5% over does NOT
            # trigger suspension, only sustained overrun.
            if spent >= threshold * 2.0:
                await _suspend_workspace(conn, ws_id, spent, threshold)
            log.warning(
                "cost.burn.alert ws=%s spent=$%.4f threshold=$%.4f window=%dmin",
                ws_id, spent, threshold, input.window_minutes,
            )

        log.info(
            "cost_burn_watcher checked=%d over=%d emitted=%d suppressed=%d",
            workspaces_checked, over_threshold, alerts_emitted, alerts_suppressed,
        )

        # Phase 3 — admin.llm-cost surface. cost_burn_watcher writes audit
        # rows that the per-query cost path also feeds; either way the
        # LlmCost dashboard's usage_aggregates_daily totals change. The
        # cost.burn.alert path already broadcasts to admin.alerts-inbox via
        # the AuditEmitter hook (Phase 2) — this is the parallel signal for
        # the cost dashboard itself. Fires regardless of whether alerts were
        # emitted, because the run sampled fresh usage data either way.
        try:
            from app.services.laravel_bridge import post_admin_surface_updated
            await post_admin_surface_updated(
                surface="llm-cost",
                affected_props=["totals", "by_day", "by_agent"],
                payload={
                    "workspaces_checked": workspaces_checked,
                    "alerts_emitted": alerts_emitted,
                    "window_minutes": input.window_minutes,
                    "sampled_at": sampled_at.isoformat() if sampled_at else None,
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "cost_burn_watcher: admin.llm-cost broadcast failed err=%s", exc,
            )

        return CostBurnWatcherOutput(
            workspaces_checked=workspaces_checked,
            workspaces_over_threshold=over_threshold,
            alerts_emitted=alerts_emitted,
            alerts_suppressed_idempotent=alerts_suppressed,
            window_minutes=input.window_minutes,
            sampled_at=sampled_at,
        )
    finally:
        await conn.close()


async def _suspend_workspace(
    conn: asyncpg.Connection,
    workspace_id: str,
    spent_usd: float,
    threshold_usd: float,
) -> None:
    """Hard-stop §35.1 — set suspended_at and write the Redis flag.

    The DB row is source-of-truth; Redis is the fast-path cache that
    the pre-LLM-call check reads. If Redis is unavailable, the check
    falls back to a DB read (slower but still correct).
    """
    row = await conn.fetchrow(
        """
        UPDATE usage.workspace_cost_ceilings
           SET suspended_at = NOW(),
               suspended_reason = $2
         WHERE workspace_id = $1::uuid
           AND suspended_at IS NULL
           AND admin_override_enabled = false
        RETURNING workspace_id
        """,
        workspace_id,
        f"hourly_spend ${spent_usd:.4f} >= 2x threshold ${threshold_usd:.4f}",
    )
    if row is None:
        # Either already suspended or admin override is active.
        return
    log.error(
        "cost_burn_watcher: SUSPENDING workspace=%s "
        "spent=$%.4f threshold=$%.4f (admin_override clears)",
        workspace_id, spent_usd, threshold_usd,
    )
    try:
        await _write_redis_suspension_flag(workspace_id)
    except Exception:
        log.warning(
            "cost_burn_watcher: Redis suspension flag write failed for "
            "workspace=%s — DB row is authoritative",
            workspace_id, exc_info=True,
        )


async def _write_redis_suspension_flag(workspace_id: str) -> None:
    """Write workspace:{ws}:llm_suspended=1 with a 1h TTL.

    Short TTL because the pre-LLM-call check re-reads the DB on cache
    miss; if the DB says suspended_at IS NULL (admin cleared the
    override), the flag stays gone and traffic resumes immediately.
    """
    import redis.asyncio as aioredis  # noqa: PLC0415

    host = os.environ.get("REDIS_HOST", "redis")
    port = int(os.environ.get("REDIS_PORT", "6379"))
    password = os.environ.get("REDIS_PASSWORD") or None
    client = aioredis.Redis(
        host=host, port=port, password=password, decode_responses=True,
    )
    try:
        await client.setex(
            f"workspace:{workspace_id}:llm_suspended", 3600, "1",
        )
    finally:
        await client.aclose()


__all__ = [
    "cost_burn_watcher",
    "CostBurnWatcherInput",
    "CostBurnWatcherOutput",
]
