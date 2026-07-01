"""Materialised-view refresh registry + advisory-locked refresh helper.

Used by:
  - The per-completion debounced refresh path (Phase 2 of the
    reliability spec): Laravel's DebounceWorkspaceMvRefresh job
    dispatches to /internal/v1/mv-refresh/run, which calls
    ``refresh_views_with_advisory_lock`` here.
  - The 03:00 nightly cron in mv_refresh_silver.py.
  - The Phase 5 Tier 3 nightly integrity sweep.

Every refresh attempt is logged to ``gold.mv_refresh_log``. Failures are
recorded with ``status='failed'`` instead of being swallowed — alerting
queries can scan that table for production health.

The registry is intentionally small (one view today) but kept as a
declarative dict so additional MVs can be added without touching the
refresh helper itself.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC

import asyncpg

log = logging.getLogger("georag.mv_refresh")


@dataclass(frozen=True)
class MaterializedView:
    """A single registered materialised view.

    Attributes:
        schema:           PostgreSQL schema (silver / gold / etc.).
        name:             Bare view name (without schema prefix).
        dependencies:     Fully-qualified silver tables whose
                          max(created_at) is compared against the last
                          successful refresh to short-circuit no-op
                          refreshes.
        concurrent:       Whether REFRESH MATERIALIZED VIEW CONCURRENTLY
                          is safe (requires a UNIQUE index on the view).
    """
    schema: str
    name: str
    dependencies: tuple[str, ...]
    concurrent: bool = True

    @property
    def qualified(self) -> str:
        return f"{self.schema}.{self.name}"


# ---------------------------------------------------------------------------
# Registry. Add new MVs here as they're created.
# ---------------------------------------------------------------------------
REGISTRY: tuple[MaterializedView, ...] = (
    MaterializedView(
        schema="silver",
        name="mv_collar_summary",
        dependencies=(
            "silver.collars",
            "silver.samples",
            "silver.lithology_logs",
        ),
        concurrent=True,
    ),
)


@dataclass
class ViewRefreshResult:
    view_name: str
    status: str            # 'completed' | 'failed' | 'skipped'
    duration_ms: int
    rows_before: int | None
    rows_after: int | None
    error: str | None


def _record_mv_metrics(
    *, view_name: str, status: str, triggered_by: str, duration_ms: int,
) -> None:
    """Best-effort Prometheus instrumentation for MV refresh outcomes."""
    try:
        from app.metrics import (
            MV_REFRESH_DURATION,
            MV_REFRESH_FAILURES_TOTAL,
        )
        MV_REFRESH_DURATION.labels(
            view_name=view_name, status=status, triggered_by=triggered_by,
        ).observe(max(0.0, duration_ms / 1000.0))
        if status == "failed":
            MV_REFRESH_FAILURES_TOTAL.labels(view_name=view_name).inc()
    except Exception:
        pass


def _to_epoch(ts) -> float:
    """Normalise a datetime (naive or aware) to epoch seconds.

    asyncpg returns timestamp/timestamptz columns as Python `datetime`
    objects — naive when the column is `timestamp without time zone`,
    aware when it's `timestamptz`. Comparing the two raises TypeError;
    epoch seconds is the cheapest tz-agnostic comparator.
    """
    if ts is None:
        return 0.0
    if ts.tzinfo is None:
        # Treat naive timestamps as UTC — Postgres stores them that way
        # in our schemas (all writers use NOW() under SET timezone=UTC).
        ts = ts.replace(tzinfo=UTC)
    return ts.timestamp()


async def _last_successful_refresh(
    conn: asyncpg.Connection, view_name: str, workspace_id: str | None,
) -> object | None:
    """Return finished_at of the most recent completed refresh for this view.

    workspace_id may be NULL (legacy global refreshes). We pull both NULL
    and the requested workspace's rows so the dependency comparison stays
    correct under mixed-mode logging.
    """
    row = await conn.fetchrow(
        """
        SELECT MAX(finished_at) AS last_finished
        FROM gold.mv_refresh_log
        WHERE view_name = $1
          AND status = 'completed'
          AND (workspace_id = $2::uuid OR workspace_id IS NULL)
        """,
        view_name, workspace_id,
    )
    return row["last_finished"] if row else None


async def _max_dependency_change(
    conn: asyncpg.Connection, view: MaterializedView,
) -> object | None:
    """Return MAX(created_at) across the view's silver dependencies.

    None if every dependency table is empty (legitimately nothing to refresh)
    or if every dependency lacks a created_at column (defensive: assume stale).
    """
    max_ts = None
    for dep in view.dependencies:
        try:
            row = await conn.fetchrow(f"SELECT MAX(created_at) AS max_ts FROM {dep}")
            if row and row["max_ts"] is not None:
                if max_ts is None or row["max_ts"] > max_ts:
                    max_ts = row["max_ts"]
        except Exception as exc:
            log.debug(
                "mv_refresh: skipping dependency staleness check dep=%s err=%s",
                dep, exc,
            )
    return max_ts


async def _row_count(conn: asyncpg.Connection, qualified: str) -> int | None:
    """Best-effort COUNT(*). Returns None on error so logging keeps working."""
    try:
        row = await conn.fetchrow(f"SELECT count(*) AS n FROM {qualified}")
        return int(row["n"]) if row else None
    except Exception:
        return None


async def refresh_views_with_advisory_lock(
    *,
    pool: asyncpg.Pool,
    workspace_id: str | None = None,
    triggered_by: str = "ingestion",
    force: bool = False,
) -> list[ViewRefreshResult]:
    """Refresh every registered MV, gated by per-view advisory locks.

    For each view in REGISTRY:
      1. Try pg_try_advisory_lock(hashtext('mv_refresh:' || view.qualified)).
         If not acquired, another refresh is in flight → skip.
      2. Staleness check: if not `force` and no dependency has changed since
         the last successful refresh, skip (status='skipped').
      3. Log a 'started' row to gold.mv_refresh_log.
      4. Run REFRESH MATERIALIZED VIEW [CONCURRENTLY] view.qualified.
      5. Patch the log row with finished/duration/rows + status='completed'
         (or 'failed' with error JSONB).
      6. Release the advisory lock.

    Returns one ViewRefreshResult per registered view. Skipped views are
    included with status='skipped' so the caller can tell that the
    workspace was checked but didn't need work.
    """
    results: list[ViewRefreshResult] = []

    for view in REGISTRY:
        result = await _refresh_one(
            pool=pool,
            view=view,
            workspace_id=workspace_id,
            triggered_by=triggered_by,
            force=force,
        )
        results.append(result)

    return results


async def _refresh_one(
    *,
    pool: asyncpg.Pool,
    view: MaterializedView,
    workspace_id: str | None,
    triggered_by: str,
    force: bool,
) -> ViewRefreshResult:
    lock_key = f"mv_refresh:{view.qualified}"
    started = time.monotonic()

    async with pool.acquire() as conn:
        # Advisory lock — pg_try_advisory_lock takes an int8, so we hash
        # the lock key to a stable bigint via hashtext (text → int4) cast
        # up to int8. Lock is session-scoped; released explicitly below.
        lock_row = await conn.fetchrow(
            "SELECT pg_try_advisory_lock(hashtext($1)::bigint) AS got",
            lock_key,
        )
        if not lock_row or not lock_row["got"]:
            log.info("mv_refresh: skipped (lock not acquired) view=%s", view.qualified)
            return ViewRefreshResult(
                view_name=view.qualified,
                status="skipped",
                duration_ms=int((time.monotonic() - started) * 1000),
                rows_before=None,
                rows_after=None,
                error=None,
            )

        try:
            # Staleness check (cheap; skips an expensive REFRESH when
            # nothing has actually moved). Dependency tables (e.g.
            # silver.collars) carry `timestamp(0) without time zone`
            # while gold.mv_refresh_log uses `timestamptz` — compare
            # in epoch-seconds so the offset-aware/naive mismatch
            # doesn't blow up the comparator.
            if not force:
                last_finished = await _last_successful_refresh(
                    conn, view.qualified, workspace_id,
                )
                max_dep = await _max_dependency_change(conn, view)
                if (
                    last_finished is not None
                    and max_dep is not None
                    and _to_epoch(max_dep) <= _to_epoch(last_finished)
                ):
                    log.info(
                        "mv_refresh: skipped (no dependency changes since %s) view=%s",
                        last_finished, view.qualified,
                    )
                    await conn.execute(
                        """
                        INSERT INTO gold.mv_refresh_log
                            (view_name, workspace_id, started_at, finished_at,
                             duration_ms, rows_before, rows_after, triggered_by, status)
                        VALUES ($1, $2::uuid, now(), now(), 0, NULL, NULL, $3, 'skipped')
                        """,
                        view.qualified, workspace_id, triggered_by,
                    )
                    return ViewRefreshResult(
                        view_name=view.qualified,
                        status="skipped",
                        duration_ms=int((time.monotonic() - started) * 1000),
                        rows_before=None,
                        rows_after=None,
                        error=None,
                    )

            rows_before = await _row_count(conn, view.qualified)

            log_row = await conn.fetchrow(
                """
                INSERT INTO gold.mv_refresh_log
                    (view_name, workspace_id, started_at, triggered_by, status)
                VALUES ($1, $2::uuid, now(), $3, 'started')
                RETURNING id
                """,
                view.qualified, workspace_id, triggered_by,
            )
            log_id = log_row["id"]

            refresh_sql = (
                f"REFRESH MATERIALIZED VIEW "
                f"{'CONCURRENTLY ' if view.concurrent else ''}{view.qualified}"
            )

            try:
                await conn.execute(refresh_sql)
            except Exception as exc:
                error_text = f"{type(exc).__name__}: {exc}"
                duration_ms = int((time.monotonic() - started) * 1000)
                await conn.execute(
                    """
                    UPDATE gold.mv_refresh_log
                    SET finished_at = now(),
                        duration_ms = $1,
                        status = 'failed',
                        error = jsonb_build_object('message', $2::text)
                    WHERE id = $3
                    """,
                    duration_ms, error_text, log_id,
                )
                log.warning("mv_refresh: failed view=%s err=%s", view.qualified, error_text)
                _record_mv_metrics(
                    view_name=view.qualified, status="failed",
                    triggered_by=triggered_by, duration_ms=duration_ms,
                )
                return ViewRefreshResult(
                    view_name=view.qualified,
                    status="failed",
                    duration_ms=duration_ms,
                    rows_before=rows_before,
                    rows_after=None,
                    error=error_text,
                )

            rows_after = await _row_count(conn, view.qualified)
            duration_ms = int((time.monotonic() - started) * 1000)

            await conn.execute(
                """
                UPDATE gold.mv_refresh_log
                SET finished_at = now(),
                    duration_ms = $1,
                    rows_before = $2,
                    rows_after = $3,
                    status = 'completed'
                WHERE id = $4
                """,
                duration_ms, rows_before, rows_after, log_id,
            )
            log.info(
                "mv_refresh: completed view=%s duration_ms=%d rows=%s→%s",
                view.qualified, duration_ms, rows_before, rows_after,
            )
            _record_mv_metrics(
                view_name=view.qualified, status="completed",
                triggered_by=triggered_by, duration_ms=duration_ms,
            )
            return ViewRefreshResult(
                view_name=view.qualified,
                status="completed",
                duration_ms=duration_ms,
                rows_before=rows_before,
                rows_after=rows_after,
                error=None,
            )
        finally:
            try:  # noqa: SIM105
                await conn.execute(
                    "SELECT pg_advisory_unlock(hashtext($1)::bigint)", lock_key,
                )
            except Exception:
                # Lock auto-releases on connection close anyway.
                pass


__all__ = [
    "MaterializedView",
    "REGISTRY",
    "ViewRefreshResult",
    "refresh_views_with_advisory_lock",
]
