"""Phase 6 of the reliability spec — periodic gauge publisher.

Some metrics aren't naturally tied to an event (they're "what's the
current state" measurements). Prometheus pulls from /metrics on its
own schedule, so we need a small background cron that updates those
gauges before each scrape window.

Today this updates:
  - georag_mv_refresh_lag_seconds — derived from gold.mv_refresh_log
  - georag_outbox_lag_seconds — derived from outbox.pending_propagations,
    worst lag per target_store across every workspace plus the platform
    rows (the table is fail-closed: a read under one scope sees only that
    scope's rows)

The active 'started' count gauge is updated by stale_run_detector
(every 15 min) and the embed-pending gauge is updated by
embed_pending_passages (every 10 min); both are sufficient. This cron
fills the remaining "no natural event" gauges on a 1-minute tick.

Spec source: docs/georag-ingestion-reliability-spec.md, Phase 6.
"""
from __future__ import annotations

import logging

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel

from app.db import fetch_per_workspace
from app.hatchet_workflows import _progress as ingest_progress
from app.hatchet_workflows import hatchet
from app.services.mv_refresh import REGISTRY as MV_REGISTRY

log = logging.getLogger("georag.hatchet.reliability_metrics_publisher")

#: Lag of the oldest unsent row per target_store, read under ONE scope (a
#: workspace's, or none for platform rows). The explicit predicate pins each pass
#: to its own rows, the way outbox_dispatcher._SCOPE_PREDICATE does, so the
#: platform pass cannot re-read tenant rows under a policy that lets an unset
#: scope see them.
_OUTBOX_LAG_SQL = """
    SELECT target_store,
           EXTRACT(EPOCH FROM (now() - MIN(enqueued_at)))::float AS lag_s
      FROM outbox.pending_propagations
     WHERE status IN ('pending', 'in_flight')
       AND workspace_id IS NOT DISTINCT FROM
           NULLIF(current_setting('app.workspace_id', true), '')::uuid
     GROUP BY target_store
"""

#: Stores this process has published a lag for. A store whose backlog drains
#: has no row in the next read, so without this its gauge would keep the last
#: lag it had until the process restarts.
_published_outbox_stores: set[str] = set()


class ReliabilityMetricsPublisherInput(BaseModel):
    pass


class ReliabilityMetricsPublisherOutput(BaseModel):
    mv_views_updated: int
    outbox_target_stores_updated: int


reliability_metrics_publisher = hatchet.workflow(
    name="reliability_metrics_publisher",
    on_crons=["* * * * *"],  # every minute — keeps gauges fresh between scrapes
    input_validator=ReliabilityMetricsPublisherInput,
)


async def _outbox_lag_by_store(pool: asyncpg.Pool) -> dict[str, float]:
    """Lag of the oldest unsent outbox row per target_store, across EVERY scope.

    outbox.pending_propagations is fail-closed (2026_10_10_100300): a session
    with ``app.workspace_id`` unset sees only platform rows (workspace_id NULL),
    and a bound one sees only its own workspace's. The gauge used to be read
    on a bare pooled connection, so once the policy tightened it ignored every
    tenant's backlog and a tenant whose outbox was stuck for hours read as no
    lag at all. Same shape as outbox_dispatcher: one pass per workspace with the
    scope bound, then one with the scope cleared for platform rows; the lag
    reported per store is the worst of them.
    """
    lag: dict[str, float] = {}
    async with pool.acquire() as conn:
        rows = await fetch_per_workspace(
            conn, _OUTBOX_LAG_SQL, site="reliability_metrics_publisher.outbox_lag",
        )
        async with conn.transaction():
            await conn.execute("SELECT set_config('app.workspace_id', '', true)")
            rows = [*rows, *await conn.fetch(_OUTBOX_LAG_SQL)]
    for r in rows:
        store = r["target_store"]
        lag[store] = max(lag.get(store, 0.0), float(r["lag_s"] or 0.0))
    return lag


async def publish_now() -> ReliabilityMetricsPublisherOutput:
    """Inline body — called by the Hatchet task below + by tests that
    need to drive the publisher without a worker process attached."""
    pool = await ingest_progress.get_pool()
    mv_updated = 0
    outbox_updated = 0

    # --- MV refresh lag ----------------------------------------------------
    try:
        from app.metrics import MV_REFRESH_LAG_SECONDS

        async with pool.acquire() as conn:
            for view in MV_REGISTRY:
                row = await conn.fetchrow(
                    """
                    SELECT EXTRACT(EPOCH FROM (now() - MAX(finished_at)))::float AS lag_s
                    FROM gold.mv_refresh_log
                    WHERE view_name = $1 AND status = 'completed'
                    """,
                    view.qualified,
                )
                # If a view has never been refreshed, report a very large
                # lag so the lag alert can fire — better signal than
                # silently leaving the gauge unset.
                lag = float(row["lag_s"]) if row and row["lag_s"] is not None else 86400.0
                MV_REFRESH_LAG_SECONDS.labels(view_name=view.qualified).set(lag)
                mv_updated += 1
    except Exception as exc:
        log.debug("mv lag publish failed: %s", exc)

    # --- Outbox lag --------------------------------------------------------
    try:
        from app.metrics import OUTBOX_LAG_SECONDS

        lag_by_store = await _outbox_lag_by_store(pool)
        for store, lag in lag_by_store.items():
            OUTBOX_LAG_SECONDS.labels(target_store=store).set(lag)
            outbox_updated += 1
        # A store that drained has no row this time; its lag is zero, not the
        # last value it had.
        for store in _published_outbox_stores - lag_by_store.keys():
            OUTBOX_LAG_SECONDS.labels(target_store=store).set(0.0)
        _published_outbox_stores.update(lag_by_store)
    except Exception as exc:
        log.debug("outbox lag publish failed: %s", exc)

    return ReliabilityMetricsPublisherOutput(
        mv_views_updated=mv_updated,
        outbox_target_stores_updated=outbox_updated,
    )


@reliability_metrics_publisher.task(
    execution_timeout="30s", schedule_timeout="5m", retries=0,
)
async def publish(
    input: ReliabilityMetricsPublisherInput, ctx: Context,
) -> ReliabilityMetricsPublisherOutput:
    return await publish_now()
