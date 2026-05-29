"""Plan §4b Stage 1 follow-up — nightly aggregation of shadow telemetry.

The shadow wire (`repair_shadow_node`) writes per-query repair
strategies + guard codes + evidence kinds to ``silver.query_traces``.
This Hatchet workflow runs nightly to roll those rows up into a
Grafana-ready summary in ``gold.repair_shadow_daily``:

  - per workspace, per UTC day
  - top-N guard codes that fired
  - top-N repair strategies the dispatcher chose
  - evidence-kind distribution (counts per kind)
  - row totals + budget-pressure histogram

The workflow's value:

  1. Pre-aggregates the SHADOW telemetry before any Grafana query
     issues a SELECT, so the dashboard renders in <100 ms even when
     ``silver.query_traces`` has millions of rows.
  2. Captures the ROLLOUT-decision data — Stage 2 (terminal enable)
     and Stage 3 (low-cost loop enable) per ``repair_loop_spec.md``
     §8 both need this aggregated view to size their cost +
     latency impact.
  3. Is workspace-scoped — uses ``set_config('georag.workspace_id',
     ...)`` so RLS applies. The aggregator runs once per workspace.

Schedule: ``15 2 * * *`` UTC (15 minutes after the audit-ledger
verify so the two cron jobs don't contend for the same DB
connections).

Manually invokable via ``repair_shadow_aggregate.run({"workspace_id":
"...", "for_date": "2026-05-27"})`` for backfills.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.hatchet_workflows import hatchet


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Input / output models
# ---------------------------------------------------------------------------


class RepairShadowAggregateInput(BaseModel):
    """Optional overrides for the aggregation window + scope."""

    workspace_id: str | None = Field(
        default=None,
        description=(
            "When set, aggregate only this workspace. When None "
            "(scheduled-cron path), iterate every workspace with "
            "traces in the window."
        ),
    )
    for_date: date | None = Field(
        default=None,
        description=(
            "UTC date to aggregate (00:00 → 24:00 of that day). "
            "When None, aggregates yesterday (the most recent "
            "complete day at cron-fire time)."
        ),
    )


class RepairShadowAggregateOutput(BaseModel):
    workspaces_processed: int
    rows_written: int
    for_date: date
    elapsed_ms: int


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


repair_shadow_aggregate = hatchet.workflow(
    name="repair_shadow_aggregate",
    on_crons=["15 2 * * *"],
    input_validator=RepairShadowAggregateInput,
)


def _build_dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


# ---------------------------------------------------------------------------
# SQL — workspace-scoped, single-day aggregation
# ---------------------------------------------------------------------------
#
# Reads silver.query_traces between window_start and window_end and
# upserts one row per (workspace_id, for_date) into
# gold.repair_shadow_daily. The target table is created lazily by the
# workflow on first run when it doesn't exist (idempotent — the
# CREATE TABLE IF NOT EXISTS pattern matches the rest of gold.* used
# by other workflows).
#
# IMPORTANT: every write sets georag.workspace_id GUC so RLS applies
# (gold.repair_shadow_daily is workspace-scoped). The cron path
# acquires the workspace list via a separate query first, then loops.

_DDL = """
CREATE SCHEMA IF NOT EXISTS gold;

CREATE TABLE IF NOT EXISTS gold.repair_shadow_daily (
    workspace_id          UUID         NOT NULL,
    for_date              DATE         NOT NULL,
    total_queries         INTEGER      NOT NULL DEFAULT 0,
    guard_pass_count      INTEGER      NOT NULL DEFAULT 0,
    queries_with_failures INTEGER      NOT NULL DEFAULT 0,
    top_guard_codes       JSONB        NOT NULL DEFAULT '{}'::jsonb,
    top_repair_strategies JSONB        NOT NULL DEFAULT '{}'::jsonb,
    evidence_kind_counts  JSONB        NOT NULL DEFAULT '{}'::jsonb,
    budget_pressure_buckets JSONB      NOT NULL DEFAULT '{}'::jsonb,
    avg_latency_ms        INTEGER      NULL,
    p95_latency_ms        INTEGER      NULL,
    created_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workspace_id, for_date)
);

ALTER TABLE gold.repair_shadow_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE gold.repair_shadow_daily FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = 'gold'
          AND tablename = 'repair_shadow_daily'
          AND policyname = 'repair_shadow_daily_workspace_isolation'
    ) THEN
        CREATE POLICY repair_shadow_daily_workspace_isolation
            ON gold.repair_shadow_daily
            USING (
                workspace_id::text = current_setting('georag.workspace_id', true)
            )
            WITH CHECK (
                workspace_id::text = current_setting('georag.workspace_id', true)
            );
    END IF;
END
$$;

GRANT SELECT, INSERT, UPDATE ON gold.repair_shadow_daily TO georag_app;
"""


_LIST_WORKSPACES_WITH_TRACES = """
    SELECT DISTINCT workspace_id::text
    FROM silver.query_traces
    WHERE created_at >= $1::timestamptz
      AND created_at <  $2::timestamptz
"""


_AGGREGATE_SQL = """
INSERT INTO gold.repair_shadow_daily (
    workspace_id, for_date,
    total_queries, guard_pass_count, queries_with_failures,
    top_guard_codes, top_repair_strategies, evidence_kind_counts,
    budget_pressure_buckets, avg_latency_ms, p95_latency_ms
)
SELECT
    $1::uuid AS workspace_id,
    $4::date AS for_date,
    COUNT(*)                                                  AS total_queries,
    COUNT(*) FILTER (WHERE guard_pass)                        AS guard_pass_count,
    COUNT(*) FILTER (WHERE NOT guard_pass)                    AS queries_with_failures,

    -- Top guard codes — unnest the guard_failure_codes array column.
    COALESCE(
        (SELECT jsonb_object_agg(code, cnt)
           FROM (
               SELECT unnest(guard_failure_codes) AS code, COUNT(*) AS cnt
                 FROM silver.query_traces
                WHERE workspace_id = $1::uuid
                  AND created_at  >= $2::timestamptz
                  AND created_at  <  $3::timestamptz
                  AND guard_failure_codes IS NOT NULL
                GROUP BY 1
                ORDER BY cnt DESC
                LIMIT 10
           ) g
        ),
        '{}'::jsonb
    )                                                         AS top_guard_codes,

    -- Top repair strategies — pulled from the trace_payload JSONB
    -- (the shadow stamps strategies in trace_payload.repair_strategies_used
    -- because the denorm column doesn't exist yet).
    COALESCE(
        (SELECT jsonb_object_agg(strat, cnt)
           FROM (
               SELECT jsonb_array_elements_text(
                          trace_payload -> 'repair_strategies_used'
                      ) AS strat,
                      COUNT(*) AS cnt
                 FROM silver.query_traces
                WHERE workspace_id = $1::uuid
                  AND created_at  >= $2::timestamptz
                  AND created_at  <  $3::timestamptz
                  AND trace_payload ? 'repair_strategies_used'
                GROUP BY 1
                ORDER BY cnt DESC
                LIMIT 10
           ) s
        ),
        '{}'::jsonb
    )                                                         AS top_repair_strategies,

    -- Evidence-kind counts — pulled from evidence_types_in_context (a
    -- list[str] stored in trace_payload).
    COALESCE(
        (SELECT jsonb_object_agg(kind, cnt)
           FROM (
               SELECT jsonb_array_elements_text(
                          trace_payload -> 'evidence_types_in_context'
                      ) AS kind,
                      COUNT(*) AS cnt
                 FROM silver.query_traces
                WHERE workspace_id = $1::uuid
                  AND created_at  >= $2::timestamptz
                  AND created_at  <  $3::timestamptz
                  AND trace_payload ? 'evidence_types_in_context'
                GROUP BY 1
           ) e
        ),
        '{}'::jsonb
    )                                                         AS evidence_kind_counts,

    -- Budget-pressure buckets: comfortable / tight / over.
    -- Definitions:
    --   over        : remaining_context_budget < 0
    --   tight       : 0 ≤ remaining_context_budget < 500
    --   comfortable : remaining_context_budget ≥ 500
    --   unknown     : remaining_context_budget IS NULL
    jsonb_build_object(
        'over',        COUNT(*) FILTER (WHERE remaining_context_budget < 0),
        'tight',       COUNT(*) FILTER (WHERE remaining_context_budget BETWEEN 0 AND 499),
        'comfortable', COUNT(*) FILTER (WHERE remaining_context_budget >= 500),
        'unknown',     COUNT(*) FILTER (WHERE remaining_context_budget IS NULL)
    )                                                         AS budget_pressure_buckets,

    AVG(latency_total_ms)::INTEGER                            AS avg_latency_ms,
    -- p95 via PERCENTILE_DISC.
    percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_total_ms)::INTEGER
                                                              AS p95_latency_ms
FROM silver.query_traces
WHERE workspace_id = $1::uuid
  AND created_at  >= $2::timestamptz
  AND created_at  <  $3::timestamptz
ON CONFLICT (workspace_id, for_date) DO UPDATE SET
    total_queries          = EXCLUDED.total_queries,
    guard_pass_count       = EXCLUDED.guard_pass_count,
    queries_with_failures  = EXCLUDED.queries_with_failures,
    top_guard_codes        = EXCLUDED.top_guard_codes,
    top_repair_strategies  = EXCLUDED.top_repair_strategies,
    evidence_kind_counts   = EXCLUDED.evidence_kind_counts,
    budget_pressure_buckets = EXCLUDED.budget_pressure_buckets,
    avg_latency_ms         = EXCLUDED.avg_latency_ms,
    p95_latency_ms         = EXCLUDED.p95_latency_ms
"""


# ---------------------------------------------------------------------------
# Task body
# ---------------------------------------------------------------------------


@repair_shadow_aggregate.task(execution_timeout="15m")
async def aggregate_window(
    input: RepairShadowAggregateInput, ctx: Context,
) -> RepairShadowAggregateOutput:
    """Aggregate the shadow telemetry for ONE day, across the requested
    workspaces.

    Flow:
      1. Compute the window (00:00 → 24:00 of for_date, UTC).
      2. Ensure the gold.repair_shadow_daily table exists (idempotent DDL).
      3. List workspaces that have traces in the window (or use the
         override).
      4. For each workspace: set GUC, run the upsert.
      5. Return summary metrics.
    """
    started = datetime.now(tz=timezone.utc)
    target_date = input.for_date or (started.date() - timedelta(days=1))
    window_start = datetime.combine(
        target_date, datetime.min.time(), tzinfo=timezone.utc,
    )
    window_end = window_start + timedelta(days=1)

    conn = await asyncpg.connect(_build_dsn(), statement_cache_size=0)
    try:
        # Ensure target table + RLS policy exist.
        await conn.execute(_DDL)

        if input.workspace_id is not None:
            workspaces = [input.workspace_id]
        else:
            rows = await conn.fetch(
                _LIST_WORKSPACES_WITH_TRACES, window_start, window_end,
            )
            workspaces = [r["workspace_id"] for r in rows]

        rows_written = 0
        for ws in workspaces:
            try:
                async with conn.transaction():
                    await conn.execute(
                        "SELECT set_config('georag.workspace_id', $1, true)",
                        ws,
                    )
                    result = await conn.execute(
                        _AGGREGATE_SQL,
                        ws,
                        window_start,
                        window_end,
                        target_date,
                    )
                    # asyncpg.execute returns 'INSERT 0 1' or 'UPDATE 1'
                    # — both count as one row written.
                    rows_written += 1
                    logger.info(
                        "repair_shadow_aggregate: workspace=%s for_date=%s result=%s",
                        ws, target_date, result,
                    )
            except Exception:
                logger.exception(
                    "repair_shadow_aggregate: workspace %s failed (continuing)",
                    ws,
                )
    finally:
        await conn.close()

    elapsed_ms = int(
        (datetime.now(tz=timezone.utc) - started).total_seconds() * 1000
    )

    return RepairShadowAggregateOutput(
        workspaces_processed=len(workspaces),
        rows_written=rows_written,
        for_date=target_date,
        elapsed_ms=elapsed_ms,
    )
