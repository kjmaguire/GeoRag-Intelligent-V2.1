"""Model Cost Summary Agent (Phase 0 agent #9, R0).

Nightly daily-roll-up:
  1. Read ``usage.usage_events`` for the previous calendar day.
  2. UPSERT into ``usage.usage_aggregates_daily`` keyed by
     (workspace_id, agent_name, model_profile, rollup_date).
  3. For each workspace, compare month-to-date totals to
     ``usage.workspace_cost_ceilings``. If MTD pct >= soft_warn AND the
     last warn we sent was lower (or never sent), emit a soft-warn
     notification to Slack and update last_warn_sent_at + last_warn_pct.

Phase 0 caveat: no real workspace usage yet, so the agent rolls up zero
rows correctly. The acceptance test seeds a synthetic usage_events row to
exercise the UPSERT path.
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta
from typing import Any

import httpx

from app.agents import AgentContext, georag_agent
from app.agents.runtime import get_runtime
from app.audit import emit_audit

logger = logging.getLogger(__name__)


@georag_agent(
    name="Model Cost Summary Agent",
    risk_tier="R0",
    version="0.1.0",
)
async def model_cost_summary_run(
    ctx: AgentContext,
    *,
    rollup_date: date | None = None,
    timeout_s: float = 8.0,
) -> dict[str, Any]:
    rt = get_runtime()
    target = rollup_date or (date.today() - timedelta(days=1))

    summary: dict[str, Any] = {
        "rollup_date": target.isoformat(),
        "rows_aggregated": 0,
        "buckets_upserted": 0,
        "ceilings_evaluated": 0,
        "warnings_emitted": 0,
        "errors": 0,
    }

    # ---- Aggregate the previous day into usage_aggregates_daily ----------
    rows = await rt.pg_pool.fetch(
        """
        SELECT workspace_id,
               agent_name,
               model_profile,
               count(*)                                              AS invocations_total,
               count(*) FILTER (WHERE outcome = 'success')           AS invocations_success,
               count(*) FILTER (WHERE outcome IN ('failure','timeout')) AS invocations_failure,
               coalesce(sum(tokens_prompt), 0)                       AS tokens_prompt_total,
               coalesce(sum(tokens_completion), 0)                   AS tokens_completion_total,
               coalesce(sum(projected_cost_usd), 0)                  AS cost_usd_total
        FROM usage.usage_events
        WHERE workspace_id IS NOT NULL
          AND created_at >= $1::date
          AND created_at <  ($1::date + interval '1 day')
        GROUP BY workspace_id, agent_name, model_profile
        """,
        target,
    )
    summary["rows_aggregated"] = len(rows)

    for r in rows:
        await rt.pg_pool.execute(
            """
            INSERT INTO usage.usage_aggregates_daily
                (workspace_id, agent_name, model_profile, rollup_date,
                 invocations_total, invocations_success, invocations_failure,
                 tokens_prompt_total, tokens_completion_total, cost_usd_total,
                 last_updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, now())
            ON CONFLICT (workspace_id, agent_name, model_profile, rollup_date) DO UPDATE
                SET invocations_total = EXCLUDED.invocations_total,
                    invocations_success = EXCLUDED.invocations_success,
                    invocations_failure = EXCLUDED.invocations_failure,
                    tokens_prompt_total = EXCLUDED.tokens_prompt_total,
                    tokens_completion_total = EXCLUDED.tokens_completion_total,
                    cost_usd_total = EXCLUDED.cost_usd_total,
                    last_updated_at = now()
            """,
            r["workspace_id"],
            r["agent_name"],
            r["model_profile"],
            target,
            r["invocations_total"],
            r["invocations_success"],
            r["invocations_failure"],
            r["tokens_prompt_total"],
            r["tokens_completion_total"],
            r["cost_usd_total"],
        )
        summary["buckets_upserted"] += 1

    # ---- Compare MTD totals to workspace_cost_ceilings -------------------
    month_start = target.replace(day=1)
    ceilings = await rt.pg_pool.fetch(
        """
        SELECT workspace_id, monthly_ceiling_usd, soft_warn_threshold_pct,
               hard_stop_threshold_pct, last_warn_sent_at, last_warn_pct
        FROM usage.workspace_cost_ceilings
        """
    )
    slack_url = os.environ.get("SLACK_NOTIFICATION_WEBHOOK_URL", "").strip()

    for c in ceilings:
        summary["ceilings_evaluated"] += 1
        mtd = await rt.pg_pool.fetchval(
            """
            SELECT coalesce(sum(cost_usd_total), 0)::numeric
            FROM usage.usage_aggregates_daily
            WHERE workspace_id = $1 AND rollup_date >= $2
            """,
            c["workspace_id"],
            month_start,
        )
        ceiling = float(c["monthly_ceiling_usd"] or 0)
        pct = float(mtd) / ceiling * 100.0 if ceiling > 0 else 0.0
        threshold = int(c["soft_warn_threshold_pct"])
        last_warn_pct = c["last_warn_pct"] or 0
        if pct >= threshold and pct > last_warn_pct:
            try:
                await emit_audit(
                    rt.pg_pool,
                    action_type="cost_ceiling.soft_warn",
                    workspace_id=c["workspace_id"],
                    actor_kind="agent",
                    target_schema="usage",
                    target_table="workspace_cost_ceilings",
                    target_id=str(c["workspace_id"]),
                    payload={
                        "mtd_usd": float(mtd),
                        "ceiling_usd": ceiling,
                        "pct": round(pct, 2),
                        "threshold_pct": threshold,
                        "month_start": month_start.isoformat(),
                    },
                    trace_id=ctx.trace_id,
                )
                summary["warnings_emitted"] += 1
            except Exception:  # pragma: no cover
                logger.exception("model_cost_summary: audit emit failed")
                summary["errors"] += 1

            await rt.pg_pool.execute(
                """
                UPDATE usage.workspace_cost_ceilings
                   SET last_warn_sent_at = now(),
                       last_warn_pct = $2
                 WHERE workspace_id = $1
                """,
                c["workspace_id"],
                int(pct),
            )

            if slack_url:
                try:
                    async with httpx.AsyncClient(timeout=timeout_s) as client:
                        await client.post(
                            slack_url,
                            json={
                                "text": (
                                    f":warning: workspace `{c['workspace_id']}` "
                                    f"crossed {threshold}% of monthly cost ceiling — "
                                    f"MTD ${float(mtd):.2f} / ceiling ${ceiling:.2f} "
                                    f"({pct:.1f}%)"
                                )
                            },
                        )
                except httpx.HTTPError as exc:
                    logger.warning("model_cost_summary: Slack post failed: %s", exc)

    return summary
