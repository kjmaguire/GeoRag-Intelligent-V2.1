"""Phase 6 of the reliability spec — periodic gauge publisher.

Some metrics aren't naturally tied to an event (they're "what's the
current state" measurements). Prometheus pulls from /metrics on its
own schedule, so we need a small background cron that updates those
gauges before each scrape window.

Today this updates:
  - georag_mv_refresh_lag_seconds — derived from gold.mv_refresh_log
  - georag_outbox_lag_seconds — derived from outbox.pending_propagations

The active 'started' count gauge is updated by stale_run_detector
(every 15 min) and the embed-pending gauge is updated by
embed_pending_passages (every 10 min); both are sufficient. This cron
fills the remaining "no natural event" gauges on a 1-minute tick.

Spec source: docs/georag-ingestion-reliability-spec.md, Phase 6.
"""
from __future__ import annotations

import logging

from hatchet_sdk import Context
from pydantic import BaseModel

from app.hatchet_workflows import _progress as ingest_progress
from app.hatchet_workflows import hatchet
from app.services.mv_refresh import REGISTRY as MV_REGISTRY

log = logging.getLogger("georag.hatchet.reliability_metrics_publisher")


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

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT target_store,
                       EXTRACT(EPOCH FROM (now() - MIN(enqueued_at)))::float AS lag_s
                FROM outbox.pending_propagations
                WHERE status IN ('pending', 'in_flight')
                GROUP BY target_store
                """,
            )
            for r in rows:
                OUTBOX_LAG_SECONDS.labels(target_store=r["target_store"]).set(
                    float(r["lag_s"] or 0.0)
                )
                outbox_updated += 1
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
