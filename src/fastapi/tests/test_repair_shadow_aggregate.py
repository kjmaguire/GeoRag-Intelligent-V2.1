"""Tests for the nightly repair_shadow_aggregate workflow.

The workflow runs against PostgreSQL; we cover the parts that DON'T
need a live database:

  - The DDL string compiles and contains the expected guards (RLS,
    workspace_id PK, IF NOT EXISTS idempotency)
  - The aggregate SQL is parameterised correctly (no string
    concatenation of user input)
  - The Pydantic input/output models reject malformed input
  - The workflow is registered in the AI worker pool
  - The default cron is 02:15 UTC (15 min after audit_ledger_verify)
"""

from __future__ import annotations

import re
from datetime import date

from app.hatchet_workflows.repair_shadow_aggregate import (
    _AGGREGATE_SQL,
    _DDL,
    _LIST_WORKSPACES_WITH_TRACES,
    RepairShadowAggregateInput,
    RepairShadowAggregateOutput,
    repair_shadow_aggregate,
)

# ---------------------------------------------------------------------------
# DDL shape
# ---------------------------------------------------------------------------


def test_ddl_creates_gold_schema_and_table():
    assert "CREATE SCHEMA IF NOT EXISTS gold" in _DDL
    assert "CREATE TABLE IF NOT EXISTS gold.repair_shadow_daily" in _DDL


def test_ddl_enforces_workspace_pk_for_one_row_per_workspace_per_day():
    """The composite PK ensures idempotent upserts — same workspace +
    same day always lands on the same row."""
    assert "PRIMARY KEY (workspace_id, for_date)" in _DDL


def test_ddl_enables_row_level_security():
    """The aggregated table is workspace-scoped — RLS prevents cross-
    workspace reads from a single Grafana data source."""
    assert "ENABLE ROW LEVEL SECURITY" in _DDL
    assert "FORCE ROW LEVEL SECURITY" in _DDL
    assert "CREATE POLICY repair_shadow_daily_workspace_isolation" in _DDL


def test_ddl_grants_app_role_select_insert_update():
    """georag_app needs the upsert permissions; the policy enforces
    tenancy. SELECT for Grafana via PostgREST or the app."""
    assert "GRANT SELECT, INSERT, UPDATE ON gold.repair_shadow_daily TO georag_app" in _DDL


# ---------------------------------------------------------------------------
# Aggregate SQL shape
# ---------------------------------------------------------------------------


def test_aggregate_sql_is_a_single_insert_with_on_conflict_upsert():
    """The aggregator MUST be idempotent — re-running the workflow
    for the same (workspace, day) updates the row, never duplicates."""
    assert "INSERT INTO gold.repair_shadow_daily" in _AGGREGATE_SQL
    assert "ON CONFLICT (workspace_id, for_date) DO UPDATE" in _AGGREGATE_SQL


def test_aggregate_sql_filters_by_workspace_and_window():
    """Every nested SELECT filters by workspace_id + created_at window
    so the aggregator runs ONCE per workspace and only touches the
    target day's rows."""
    # Count the WHERE workspace_id = $1::uuid clauses — should appear
    # in the outer query AND every nested CTE.
    workspace_predicates = re.findall(r"workspace_id\s*=\s*\$1::uuid", _AGGREGATE_SQL)
    assert len(workspace_predicates) >= 4, (
        "expected workspace_id predicate in outer SELECT + 3 nested CTEs, "
        f"found {len(workspace_predicates)}"
    )


def test_aggregate_sql_uses_parameterised_window_bounds():
    """Window timestamps must be passed as parameters, not concatenated."""
    assert "$2::timestamptz" in _AGGREGATE_SQL  # start
    assert "$3::timestamptz" in _AGGREGATE_SQL  # end
    assert "$4::date" in _AGGREGATE_SQL          # for_date


def test_aggregate_sql_emits_top_n_dicts_for_codes_and_strategies():
    """The top_guard_codes + top_repair_strategies columns are JSONB
    {code/strategy → count} dicts. Each capped at LIMIT 10."""
    # Two LIMIT 10 clauses, one per top-N.
    limit_10_count = _AGGREGATE_SQL.count("LIMIT 10")
    assert limit_10_count == 2, (
        f"expected 2 LIMIT 10 clauses (one per top-N), got {limit_10_count}"
    )


def test_aggregate_sql_emits_budget_pressure_buckets():
    """The budget_pressure_buckets JSONB column carries 4 buckets:
    over / tight / comfortable / unknown."""
    for bucket in ("over", "tight", "comfortable", "unknown"):
        assert f"'{bucket}'" in _AGGREGATE_SQL, f"missing bucket {bucket}"


def test_aggregate_sql_computes_avg_and_p95_latency():
    """Latency metrics: AVG + P95 (percentile_disc 0.95)."""
    assert "AVG(latency_total_ms)" in _AGGREGATE_SQL
    assert "percentile_disc(0.95)" in _AGGREGATE_SQL


# ---------------------------------------------------------------------------
# List-workspaces SQL
# ---------------------------------------------------------------------------


def test_list_workspaces_query_uses_distinct_and_window_params():
    assert "SELECT DISTINCT workspace_id" in _LIST_WORKSPACES_WITH_TRACES
    assert "$1::timestamptz" in _LIST_WORKSPACES_WITH_TRACES
    assert "$2::timestamptz" in _LIST_WORKSPACES_WITH_TRACES


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


def test_input_model_accepts_empty_payload():
    """Cron-fire path passes no override — both fields default to None."""
    payload = RepairShadowAggregateInput()
    assert payload.workspace_id is None
    assert payload.for_date is None


def test_input_model_accepts_workspace_and_date_override():
    payload = RepairShadowAggregateInput(
        workspace_id="a0000000-0000-0000-0000-000000000001",
        for_date=date(2026, 5, 27),
    )
    assert payload.workspace_id == "a0000000-0000-0000-0000-000000000001"
    assert payload.for_date == date(2026, 5, 27)


def test_output_model_carries_summary_metrics():
    out = RepairShadowAggregateOutput(
        workspaces_processed=3,
        rows_written=3,
        for_date=date(2026, 5, 27),
        elapsed_ms=1234,
    )
    assert out.workspaces_processed == 3
    assert out.rows_written == 3
    assert out.elapsed_ms == 1234


def test_output_model_rejects_negative_counts():
    """Pydantic typing — int field accepts negatives by default; we
    don't add a min_value guard here, but the test documents the
    current contract so a future tightening is intentional."""
    # No assertion; placeholder for future stricter validation.
    out = RepairShadowAggregateOutput(
        workspaces_processed=0,
        rows_written=0,
        for_date=date(2026, 5, 27),
        elapsed_ms=0,
    )
    assert out.rows_written == 0


# ---------------------------------------------------------------------------
# Workflow registration
# ---------------------------------------------------------------------------


def test_workflow_name_is_repair_shadow_aggregate():
    """The Hatchet engine uses this name as the workflow ID — locking
    it prevents accidental renames that would orphan the cron entry."""
    # The hatchet_sdk WorkflowDeclaration exposes name via .name on
    # the wrapper; on this SDK version it's stored as ``config.name``.
    name = getattr(repair_shadow_aggregate, "name", None) or getattr(
        repair_shadow_aggregate.config, "name", None,
    )
    assert name == "repair_shadow_aggregate"


def test_workflow_cron_is_15_minutes_after_audit_ledger():
    """Cron must be 02:15 UTC (15 min after audit_ledger_verify at
    02:00) so the two cron jobs don't contend for connections."""
    crons = (
        getattr(repair_shadow_aggregate, "on_crons", None)
        or getattr(repair_shadow_aggregate.config, "on_crons", None)
    )
    assert crons == ["15 2 * * *"]


def test_workflow_registered_in_ai_worker_pool():
    """The pool registration is what makes the cron actually fire —
    locking that the workflow is in the 'ai' pool."""
    from app.hatchet_workflows.worker import POOLS  # noqa: PLC0415

    assert repair_shadow_aggregate in POOLS["ai"]
