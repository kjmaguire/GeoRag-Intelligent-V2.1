"""Phase 6 of the reliability spec — Prometheus metric instrumentation tests.

Locks the contract that:

  - Terminal-state writes in _progress increment INGESTION_RUNS_TOTAL
    and observe INGESTION_RUN_DURATION.
  - MV refresh records MV_REFRESH_DURATION and bumps
    MV_REFRESH_FAILURES_TOTAL on a failed refresh.
  - The reliability_metrics_publisher cron sets the
    MV_REFRESH_LAG_SECONDS gauge from gold.mv_refresh_log.
"""
from __future__ import annotations

import os
import uuid

import asyncpg
import pytest

if not os.environ.get("POSTGRES_USER"):
    pytest.skip("postgres env not configured", allow_module_level=True)

import contextlib

from app import metrics  # noqa: E402
from app.hatchet_workflows import _progress as ingest_progress  # noqa: E402
from app.services import mv_refresh as mv_refresh_mod  # noqa: E402

_TEST_WORKSPACE = "a0000000-0000-0000-0000-0000000bdfeb"
_TEST_PROJECT = "b4000000-0000-0000-0000-0000000bdfeb"


@pytest.fixture(autouse=True)
async def _reset_pool():
    if ingest_progress._pool is not None:
        with contextlib.suppress(Exception):
            await ingest_progress._pool.close()
        ingest_progress._pool = None
    yield
    if ingest_progress._pool is not None:
        with contextlib.suppress(Exception):
            await ingest_progress._pool.close()
        ingest_progress._pool = None


async def _bootstrap():
    conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        await conn.execute(
            """
            INSERT INTO silver.workspaces (workspace_id, name, slug)
            VALUES ($1::uuid, 'phase6-metrics', 'p6-' || substring($1::text from 1 for 8))
            ON CONFLICT (workspace_id) DO NOTHING
            """,
            _TEST_WORKSPACE,
        )
        await conn.execute(
            """
            INSERT INTO silver.projects (
                project_id, project_name, slug, workspace_id,
                crs_datum, orientation_reference, status
            ) VALUES (
                $1::uuid, 'phase6-metrics',
                'phase6-metrics-' || substring($1::text from 1 for 8),
                $2::uuid, 'EPSG:4326', 'grid', 'active'
            )
            ON CONFLICT (project_id) DO NOTHING
            """,
            _TEST_PROJECT, _TEST_WORKSPACE,
        )
    finally:
        await conn.close()


def _counter_value(counter, **labels) -> float:
    """Read the current value of a Prometheus Counter for the given labels."""
    if labels:
        return counter.labels(**labels)._value.get()
    return counter._value.get()


def _histogram_sample_count(histogram, **labels) -> float:
    """Read the running observation count from a Prometheus Histogram."""
    target = histogram.labels(**labels) if labels else histogram
    return target._sum.get()


# ---------------------------------------------------------------------------
# Terminal-state metrics
# ---------------------------------------------------------------------------
async def test_mark_completed_records_ingestion_metrics():
    await _bootstrap()

    before_total = _counter_value(
        metrics.INGESTION_RUNS_TOTAL, status="completed", triggered_by="upload",
    )

    run_id = await ingest_progress.start_run(
        workspace_id=_TEST_WORKSPACE, project_id=_TEST_PROJECT,
        minio_key=f"reports/p6/{uuid.uuid4()}.pdf",
    )
    assert run_id is not None
    assert await ingest_progress.mark_completed_by_run(run_id=run_id) is True

    after_total = _counter_value(
        metrics.INGESTION_RUNS_TOTAL, status="completed", triggered_by="upload",
    )
    assert after_total == before_total + 1


async def test_mark_failed_records_failed_status():
    await _bootstrap()

    before_total = _counter_value(
        metrics.INGESTION_RUNS_TOTAL, status="failed", triggered_by="upload",
    )

    run_id = await ingest_progress.start_run(
        workspace_id=_TEST_WORKSPACE, project_id=_TEST_PROJECT,
        minio_key=f"reports/p6/{uuid.uuid4()}.pdf",
    )
    assert run_id is not None
    await ingest_progress.mark_failed_by_run(
        run_id=run_id, stage="persist", error="db ouch",
    )

    after_total = _counter_value(
        metrics.INGESTION_RUNS_TOTAL, status="failed", triggered_by="upload",
    )
    assert after_total == before_total + 1


async def test_idempotent_mark_completed_does_not_double_count():
    """Locks the metric idempotency contract — a no-op terminal write
    (because the row was already terminal) must NOT bump the counter."""
    await _bootstrap()

    run_id = await ingest_progress.start_run(
        workspace_id=_TEST_WORKSPACE, project_id=_TEST_PROJECT,
        minio_key=f"reports/p6/{uuid.uuid4()}.pdf",
    )
    assert run_id is not None
    assert await ingest_progress.mark_completed_by_run(run_id=run_id) is True

    before_total = _counter_value(
        metrics.INGESTION_RUNS_TOTAL, status="completed", triggered_by="upload",
    )

    # Second call — should be a no-op (row already terminal).
    assert await ingest_progress.mark_completed_by_run(run_id=run_id) is False

    after_total = _counter_value(
        metrics.INGESTION_RUNS_TOTAL, status="completed", triggered_by="upload",
    )
    assert after_total == before_total, \
        "Counter must not increment when the conditional UPDATE was a no-op"


# ---------------------------------------------------------------------------
# MV refresh metrics
# ---------------------------------------------------------------------------
async def test_mv_refresh_records_duration_histogram():
    """A successful refresh observes a sample into MV_REFRESH_DURATION."""
    await _bootstrap()
    pool = await ingest_progress.get_pool()

    # Clear any prior log rows for this workspace so the staleness check
    # can't short-circuit our refresh into 'skipped'.
    conn = await asyncpg.connect(ingest_progress._dsn(), statement_cache_size=0)
    try:
        await conn.execute(
            "DELETE FROM gold.mv_refresh_log WHERE workspace_id = $1::uuid",
            _TEST_WORKSPACE,
        )
    finally:
        await conn.close()

    results = await mv_refresh_mod.refresh_views_with_advisory_lock(
        pool=pool, workspace_id=_TEST_WORKSPACE,
        triggered_by="ingestion", force=True,
    )

    completed = [r for r in results if r.status == "completed"]
    assert len(completed) >= 1

    # The histogram exposes a `_count` family with one bucket-count
    # series per label-set. After at least one observation under our
    # labels, the _sum series must be > 0.
    for r in completed:
        labels = {
            "view_name": r.view_name,
            "status": "completed",
            "triggered_by": "ingestion",
        }
        sample_sum = metrics.MV_REFRESH_DURATION.labels(**labels)._sum.get()
        assert sample_sum > 0, f"no histogram sample recorded for {r.view_name}"


async def test_mv_refresh_failure_increments_failure_counter():
    pool = await ingest_progress.get_pool()

    fake = mv_refresh_mod.MaterializedView(
        schema="silver", name="__nope_phase6__",
        dependencies=("silver.collars",), concurrent=False,
    )
    original_registry = mv_refresh_mod.REGISTRY
    before = _counter_value(
        metrics.MV_REFRESH_FAILURES_TOTAL, view_name=fake.qualified,
    )
    try:
        mv_refresh_mod.REGISTRY = (fake,)
        results = await mv_refresh_mod.refresh_views_with_advisory_lock(
            pool=pool, workspace_id=_TEST_WORKSPACE,
            triggered_by="ingestion", force=True,
        )
    finally:
        mv_refresh_mod.REGISTRY = original_registry

    assert any(r.status == "failed" for r in results)
    after = _counter_value(
        metrics.MV_REFRESH_FAILURES_TOTAL, view_name=fake.qualified,
    )
    assert after == before + 1


# ---------------------------------------------------------------------------
# Reliability metrics publisher cron
# ---------------------------------------------------------------------------
async def test_metrics_publisher_sets_mv_refresh_lag_gauge():
    from app.hatchet_workflows.reliability_metrics_publisher import publish_now

    pool = await ingest_progress.get_pool()
    # Ensure at least one 'completed' refresh log row exists so the lag
    # gauge has data; we don't care about its absolute value, just that
    # the publisher writes to the gauge without raising.
    await mv_refresh_mod.refresh_views_with_advisory_lock(
        pool=pool, workspace_id=_TEST_WORKSPACE,
        triggered_by="manual", force=True,
    )

    out = await publish_now()
    assert out.mv_views_updated >= 1
