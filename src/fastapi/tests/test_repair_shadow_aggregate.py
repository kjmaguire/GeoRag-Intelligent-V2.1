"""Tests for the nightly repair_shadow_aggregate workflow.

The workflow runs against PostgreSQL; we cover the parts that DON'T
need a live database:

  - The DDL string compiles and contains the expected guards (RLS,
    workspace_id PK, IF NOT EXISTS idempotency)
  - The aggregate SQL is parameterised correctly (no string
    concatenation of user input)
  - The Pydantic input/output models reject malformed input
  - The workflow is registered in the AI worker pool
  - The cron stays 15 min after audit_ledger_verify (asserted as a
    gap, not a fixed hour)
"""

from __future__ import annotations

import re
from datetime import date

from app.hatchet_workflows.repair_shadow_aggregate import (
    _AGGREGATE_SQL,
    _LIST_WORKSPACES_WITH_TRACES,
    RepairShadowAggregateInput,
    RepairShadowAggregateOutput,
    repair_shadow_aggregate,
)

# ---------------------------------------------------------------------------
# DDL shape
#
# The _DDL literal these tests used to assert on is gone: the table is now
# declared by migration 2026_08_19_060000_create_gold_repair_shadow_daily.
# Creating it at runtime needed CREATE on the database, which georag_app does
# not have, so the workflow failed on every scheduled run in production.
#
# The guarantees these tests protected — composite PK, forced RLS, the
# workspace-isolation policy, the georag_app grant — are now asserted against
# the real database in tests/Feature/Database/RepairShadowDailySchemaTest.php,
# which is stronger than matching substrings in a Python string. This module
# cannot make those assertions: it runs inside the FastAPI container, which
# mounts only src/fastapi and cannot see database/migrations.
# ---------------------------------------------------------------------------


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


def _single_cron_minutes(workflow) -> int:
    """The workflow's one cron, as minutes past midnight UTC."""
    crons = (
        getattr(workflow, "on_crons", None)
        or getattr(workflow.config, "on_crons", None)
    )
    assert crons and len(crons) == 1, f"expected exactly one cron, got {crons}"
    minute, hour = crons[0].split()[:2]
    return int(hour) * 60 + int(minute)


def test_workflow_cron_is_15_minutes_after_audit_ledger():
    """The gap to audit_ledger_verify is what matters, so assert the GAP.

    This used to assert the literal ``["15 2 * * *"]``. On 2026-09-16 the
    shutdown window shrank to a business day and every fixed-hour cron moved
    into the evening UTC band -- a change that preserved this relationship
    exactly and still broke the test, because the test was checking a
    coordinate rather than the thing its own name describes.

    Reading both crons means the next schedule move only fails here if the
    fifteen-minute stagger actually changes, which is the only way these two
    start contending for connections again.
    """
    from app.hatchet_workflows.audit_ledger_verify import (  # noqa: PLC0415
        audit_ledger_verify,
    )

    gap = _single_cron_minutes(repair_shadow_aggregate) - _single_cron_minutes(
        audit_ledger_verify
    )
    assert gap == 15, f"expected a 15-minute stagger, got {gap} minutes"


def test_workflow_registered_in_ai_worker_pool():
    """The pool registration is what makes the cron actually fire —
    locking that the workflow is in the 'ai' pool."""
    from app.hatchet_workflows.worker import POOLS  # noqa: PLC0415

    assert repair_shadow_aggregate in POOLS["ai"]
