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

import contextlib
import logging
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.db import bind_workspace_scope, fetch_per_workspace
from app.db.dsn import build_dsn
from app.hatchet_workflows import hatchet

log = logging.getLogger("georag.hatchet.cost_burn_watcher")


# Hours in a 30-day month — used to derive an hourly threshold from
# the monthly ceiling. 30 * 24 = 720. Tighter than the 730.5 calendar
# average so workspaces with a hard monthly ceiling don't overrun.
_HOURS_PER_BUDGETED_MONTH = 720


#: Log marker that carries a cost-burn alert out of the database.
#:
#: The detector's ledger row and admin broadcast both require someone to
#: be watching a screen. This string is what a Log Analytics scheduled
#: query rule matches to send email through `georag-alerts-ag` (rule 5d
#: in deploy/aws/terraform/alerts.tf).
#:
#: Distinctive on purpose — upper case, underscored, long enough that it
#: cannot appear in ordinary prose or in a stack trace.
COST_BURN_ALERT_MARKER = "COST_BURN_THRESHOLD_EXCEEDED"


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
    #: Workspaces this run newly suspended (§35.1 hard stop).
    workspaces_suspended: int = 0
    #: Workspaces at 2x their threshold that the hard stop COULD NOT be applied
    #: to, because they have no usage.workspace_cost_ceilings row to suspend.
    hard_stop_unenforceable: int = 0
    window_minutes: int
    sampled_at: datetime


cost_burn_watcher = hatchet.workflow(
    name="cost_burn_watcher",
    on_crons=["*/5 * * * *"],
    input_validator=CostBurnWatcherInput,
)


# One DSN builder for the whole service — see app/db/dsn.py for why
# sixty copies of this existed and what the drift cost.
_build_dsn = build_dsn


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


@contextlib.asynccontextmanager
async def _in_workspace(
    conn: asyncpg.Connection, workspace_id: str,
) -> AsyncIterator[asyncpg.Connection]:
    """One short transaction with ``workspace_id``'s scope bound (HAT-1)."""
    async with conn.transaction():
        await bind_workspace_scope(
            conn, workspace_id=workspace_id, site="cost_burn_watcher",
        )
        yield conn


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
    suspended = 0
    hard_stop_unenforceable = 0
    try:
        # Per-workspace hourly cost in the trailing window. NULL workspace
        # rows skipped (system-level LLM calls aren't workspace-scoped).
        #
        # HAT-1 (2026-09-29): read once per workspace with the scope bound.
        # The worker connects as georag_app (NOBYPASSRLS) on AWS, and an
        # unscoped read of usage.usage_events sees spend only while that
        # table keeps phase0/95's fail-open branch (2026_08_14_030000 made
        # it strict; db:apply-raw re-opens it after every migrate). Under
        # the strict policy this watcher never saw spend and never alerted.
        rows = await fetch_per_workspace(
            conn,
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
            site="cost_burn_watcher.spend",
        )

        for r in rows:
            workspaces_checked += 1
            ws_id = r["workspace_id"]
            spent = float(r["hourly_spent_usd"])
            async with _in_workspace(conn, ws_id):
                threshold = await _resolve_threshold_for_workspace(
                    conn, ws_id, env_default,
                )
            if spent <= threshold:
                continue
            over_threshold += 1

            async with _in_workspace(conn, ws_id):
                already_alerted = await _has_recent_unacked_alert(
                    conn, ws_id, input.window_minutes,
                )
            if already_alerted:
                alerts_suppressed += 1
            else:
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

                # The ledger row above reaches nobody on its own: it surfaces
                # on an admin screen a human has to already be looking at.
                # This line is the egress. Log Analytics matches the marker
                # and `georag-alerts-ag` turns it into email — the same shape
                # answer_quality_watch uses, and the only outbound path the
                # platform actually has. (services/dispatchers/pagerduty.py
                # looked like a second one; it was never wired, and was
                # deleted 2026-08-28.)
                #
                # No workspace name, no query text: a workspace id, two
                # dollar figures and a window. Enough to act on, nothing that
                # should not sit in a 30-day log store.
                log.error(
                    "%s workspace=%s spent_usd=%.4f threshold_usd=%.4f "
                    "window_minutes=%d events=%d",
                    COST_BURN_ALERT_MARKER,
                    ws_id,
                    round(spent, 4),
                    round(threshold, 4),
                    input.window_minutes,
                    int(r["event_count"]),
                )
                log.warning(
                    "cost.burn.alert ws=%s spent=$%.4f threshold=$%.4f window=%dmin",
                    ws_id, spent, threshold, input.window_minutes,
                )

            # Hard-stop §35.1: when hourly spend is 2× the threshold —
            # i.e. the workspace has been burning past the cap for at
            # least a window's worth of LLM calls — suspend further
            # LLM activity until admin override or period rollover.
            # The 2× factor is a guard against transient bursts; a
            # single big query that puts a workspace 5% over does NOT
            # trigger suspension, only sustained overrun.
            #
            # Evaluated on EVERY tick that finds the workspace over 2×,
            # whether or not this tick raised the alert. It used to sit
            # after the `already_alerted` early-continue, so the first
            # over-threshold reading (say 1.2×) wrote the alert and, until
            # somebody acknowledged it, both the alert and the suspension
            # were suppressed: a workspace could climb from 1.2× to 10× with
            # the hard stop unreachable. `_suspend_workspace` is idempotent
            # (it only updates a row that is not already suspended and has
            # no admin override), so re-checking each tick costs one UPDATE
            # that matches nothing.
            if spent >= threshold * 2.0:
                async with _in_workspace(conn, ws_id):
                    outcome = await _suspend_workspace(conn, ws_id, spent, threshold)
                if outcome == _SUSPENDED:
                    suspended += 1
                elif outcome == _NO_CEILING_ROW:
                    hard_stop_unenforceable += 1

        log.info(
            "cost_burn_watcher checked=%d over=%d emitted=%d suppressed=%d "
            "suspended=%d hard_stop_unenforceable=%d",
            workspaces_checked, over_threshold, alerts_emitted, alerts_suppressed,
            suspended, hard_stop_unenforceable,
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
            workspaces_suspended=suspended,
            hard_stop_unenforceable=hard_stop_unenforceable,
            window_minutes=input.window_minutes,
            sampled_at=sampled_at,
        )
    finally:
        await conn.close()


#: What `_suspend_workspace` did. Strings, not a bool: "nothing to do" has two
#: very different meanings.
_SUSPENDED = "suspended"
_ALREADY_SUSPENDED = "already_suspended_or_override"
_NO_CEILING_ROW = "no_ceiling_row"

#: Log marker for a workspace that is past its hard stop and cannot be stopped.
HARD_STOP_UNENFORCEABLE_MARKER = "COST_BURN_HARD_STOP_UNENFORCEABLE"


async def _suspend_workspace(
    conn: asyncpg.Connection,
    workspace_id: str,
    spent_usd: float,
    threshold_usd: float,
) -> str:
    """Hard-stop §35.1 — set suspended_at and write the Redis flag.

    The DB row is source-of-truth; Redis is the fast-path cache that
    the pre-LLM-call check reads. If Redis is unavailable, the check
    falls back to a DB read (slower but still correct).

    Returns ``_SUSPENDED`` when this call suspended the workspace,
    ``_ALREADY_SUSPENDED`` when the row is already suspended or an admin
    override is active (both mean: nothing to do), and ``_NO_CEILING_ROW``
    when the workspace has no ``usage.workspace_cost_ceilings`` row at all.

    That last case used to be indistinguishable from the second: the UPDATE
    matched nothing and the function returned silently. A hard stop is
    configured per workspace (``hard_stop_threshold_pct``), so a workspace
    that is only measured against the env-default threshold has nothing to
    suspend. Creating a ceiling row for it here would put a monthly cap on a
    customer -- ``COST_BURN_THRESHOLD_USD_PER_HOUR`` x 720, a figure nobody
    set (``monthly_ceiling_usd`` is NOT NULL) -- and stop their chat when
    they pass it, which is a policy decision rather than a repair. It is not
    made here; the gap is made loud instead (``HARD_STOP_UNENFORCEABLE_MARKER``
    and the run's ``hard_stop_unenforceable`` count). The alert for the same
    overrun has already gone out.
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
        has_row = await conn.fetchval(
            "SELECT 1 FROM usage.workspace_cost_ceilings WHERE workspace_id = $1::uuid",
            workspace_id,
        )
        if has_row is None:
            log.error(
                "%s workspace=%s spent_usd=%.4f threshold_usd=%.4f -- the hard "
                "stop was NOT applied: the workspace has no "
                "usage.workspace_cost_ceilings row to suspend, so it is "
                "measured against the env-default threshold only. Create a "
                "ceiling for it if it should be stoppable.",
                HARD_STOP_UNENFORCEABLE_MARKER, workspace_id, spent_usd, threshold_usd,
            )
            return _NO_CEILING_ROW
        # Already suspended, or an admin override is active.
        return _ALREADY_SUSPENDED
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
    return _SUSPENDED


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
