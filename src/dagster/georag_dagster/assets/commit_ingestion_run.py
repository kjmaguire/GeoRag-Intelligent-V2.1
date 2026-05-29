"""Commit asset — data_version bump + post-ingest PostgreSQL tuning.

This asset is the terminal node in the ingestion pipeline.  It depends on
every upstream Silver, Gold, and Index asset.  Because all upstream asset
checks are blocking=True, Dagster will not execute this asset if any upstream
check fails — the blocking-check enforcement is entirely structural, not
hand-coded here.

Responsibilities (in execution order):
  1. Open a single DB transaction.
  2. Execute:
       UPDATE silver.workspaces SET data_version = data_version + 1
         WHERE workspace_id = :workspace_id;
       UPDATE projects SET data_version = data_version + 1
         WHERE id IN (:project_ids);
     Both UPDATEs are inside the same transaction so they are atomic.
  3. Commit the transaction.  If the monotonic trigger fires (rejects a
     decrement) the transaction rolls back and the asset fails — never silent.
  4. Emit post-increment (workspace_data_version, project_data_version) as
     materialization metadata.  Module 7 reads these to broadcast the
     ingestion.progress Reverb event.
  5. After the data_version commit, execute post-ingest-tune.sql for each
     spatial table that was populated this run (CLUSTER + ANALYZE + MV refresh).
     This step runs outside the data_version transaction because CLUSTER
     acquires ACCESS EXCLUSIVE and we do not want to hold that lock while
     also holding the workspace write lock.

Architecture note
-----------------
Dagster's blocking=True check semantics guarantee that if ANY upstream asset
check fails, this asset is skipped entirely by the scheduler.  The asset
dependency chain is:

    bronze_* → silver_* → (check: blocking) → gold_* → index_* → (check: blocking)
                                                                         ↓
                                                               commit_ingestion_run

The dependency on index_reports (the deepest Index asset currently available)
plus gold_placeholder (routing through the collar path) covers the full
private-project asset chain.  Public-geoscience assets are intentionally NOT
in the dependency chain here — they run on their own schedule and have their
own commit semantics (future work).

data_version authority
----------------------
Per addendum §05d and Global Invariant 12:
  - Bumps exactly ONCE per committed run (this asset).
  - Never bumped on Bronze upload, never on parser start.
  - Never decremented — the DB monotonic trigger enforces this.
  - Only this asset may increment data_version for the private-project pipeline.

post-ingest-tune.sql contract
------------------------------
The script (ops/postgis/post-ingest-tune.sql) is executed statement-by-statement
via psycopg2, not via subprocess/psql, so we avoid a shell dependency inside the
Dagster container.  The three SQL statements in the script are:
  1. CLUSTER <table> USING <idx>
  2. ANALYZE <table>
  3. REFRESH MATERIALIZED VIEW CONCURRENTLY <mv> (conditional)

Each statement is executed in its own connection (CLUSTER and REFRESH take their
own locks and are best isolated).  Any failure logs a WARNING but does NOT roll
back the data_version commit — the tune step is best-effort after data is
committed.  Per the architecture doc, CLUSTER requires that Dagster is the sole
writer at this point (pipeline tail).

Known table / index / matview pairs (from post-ingest-tune.sql header comment,
2026-04-19 inventory):
  silver.collars          → idx_collars_geom          → silver.mv_collar_summary
  silver.reports          → idx_reports_geom           → none
  silver.spatial_features → idx_spatial_features_geom  → none

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config/ConfigurableResource classes use Pydantic for type
introspection and that import breaks runtime annotation evaluation.
"""

import os
import time
from typing import Optional

import httpx
import psycopg2.extras
from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.assets.index_reports import index_reports
from georag_dagster.assets.index_neo4j import index_neo4j
from georag_dagster.assets.silver import silver_collars
from georag_dagster.assets.silver_reports import silver_reports
from georag_dagster.assets.silver_spatial import silver_spatial
from georag_dagster.assets.silver_drill_traces import silver_drill_traces
from georag_dagster.assets.silver_cog_rasters import silver_cog_rasters
from georag_dagster.resources import PostgresResource

# ---------------------------------------------------------------------------
# Default workspace — seeded in Phase B1+B2 migration
# (2026_04_20_100000_create_workspaces_and_data_version.php)
# ---------------------------------------------------------------------------
DEFAULT_WORKSPACE_ID = "a0000000-0000-0000-0000-000000000001"

# ---------------------------------------------------------------------------
# Tune targets — table, GIST index, materialized view (or None)
# Source: ops/postgis/post-ingest-tune.sql header comment inventory (2026-04-19)
# ---------------------------------------------------------------------------
_TUNE_TARGETS = [
    {
        "table": "silver.collars",
        "index": "idx_collars_geom",
        "matview": "silver.mv_collar_summary",
    },
    {
        "table": "silver.reports",
        "index": "idx_reports_geom",
        "matview": None,
    },
    {
        "table": "silver.spatial_features",
        "index": "idx_spatial_features_geom",
        "matview": None,
    },
    {
        "table": "silver.drill_traces",
        "index": "idx_drill_traces_geom",
        "matview": None,
    },
]


# ---------------------------------------------------------------------------
# Asset config
# ---------------------------------------------------------------------------

class CommitIngestionRunConfig(Config):
    """Runtime configuration for the commit_ingestion_run asset.

    workspace_id defaults to the seeded default workspace.  For multi-tenant
    deployments, each workspace's ingestion pipeline should pass its own UUID.

    project_ids is a comma-separated list of project UUIDs that received new
    data in this run.  Pass an empty string to skip the projects UPDATE (e.g.
    when only public-geoscience assets ran).
    """
    workspace_id: str = DEFAULT_WORKSPACE_ID
    # Comma-separated list of project UUIDs, or empty string to skip.
    project_ids: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_project_ids(raw: str) -> list[str]:
    """Parse comma-separated project UUID string into a list, stripping blanks."""
    if not raw or not raw.strip():
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _bump_data_version(
    postgres: PostgresResource,
    workspace_id: str,
    project_ids: list[str],
    context: AssetExecutionContext,
) -> dict:
    """Execute the data_version bump in a single atomic transaction.

    Returns a dict with post-increment workspace_data_version and a list of
    (project_id, new_data_version) tuples for metadata emission.

    The monotonic trigger will RAISE and roll back the transaction if anything
    tries a decrement — that case surfaces as an asset failure, never silent.
    """
    result = {
        "workspace_data_version": None,
        "project_versions": [],
    }

    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Bump workspace
            cur.execute(
                """
                UPDATE silver.workspaces
                   SET data_version = data_version + 1
                 WHERE workspace_id = %(workspace_id)s
                RETURNING data_version;
                """,
                {"workspace_id": workspace_id},
            )
            ws_row = cur.fetchone()
            if ws_row is None:
                raise ValueError(
                    f"commit_ingestion_run: workspace_id {workspace_id!r} not found "
                    "in silver.workspaces — cannot bump data_version."
                )
            result["workspace_data_version"] = int(ws_row["data_version"])
            context.log.info(
                "commit_ingestion_run: workspace %s data_version → %d",
                workspace_id,
                result["workspace_data_version"],
            )

            # Bump projects (if any)
            if project_ids:
                cur.execute(
                    """
                    UPDATE silver.projects
                       SET data_version = data_version + 1
                     WHERE project_id::text = ANY(%(ids)s::text[])
                    RETURNING project_id, data_version;
                    """,
                    {"ids": project_ids},
                )
                rows = cur.fetchall()
                result["project_versions"] = [
                    {"project_id": str(r["project_id"]), "data_version": int(r["data_version"])}
                    for r in rows
                ]
                for pv in result["project_versions"]:
                    context.log.info(
                        "commit_ingestion_run: project %s data_version → %d",
                        pv["project_id"],
                        pv["data_version"],
                    )

                found = {pv["project_id"] for pv in result["project_versions"]}
                missing = set(project_ids) - found
                if missing:
                    context.log.warning(
                        "commit_ingestion_run: %d project_ids not found in projects table: %s",
                        len(missing),
                        missing,
                    )

        # Transaction commits here (get_connection() commits on clean exit)

    return result


def _broadcast_ingestion_completed(
    workspace_id: str,
    project_id: str,
    pipeline_run_id: str,
    context: AssetExecutionContext,
) -> bool:
    """POST to Laravel's ingest-progress bridge with status='completed'.

    This is the Dagster-side equivalent of ingest_pdf.embed_verify's
    terminal broadcast (src/fastapi/app/services/laravel_bridge.py ::
    post_ingestion_progress). Drill-data uploads flow through this asset
    instead of Hatchet, so they need the same broadcast plumbing to
    drive the IngestionRuns / DrillReview / Overview / Lakehouse /
    IngestQuality / Targets WorkspaceDataUpdated cascade.

    Per the reliability spec the Laravel side does ALL the heavy lifting:
    bumps silver.workspaces.data_version (SETNX-guarded so retries can't
    double-bump), stamps the per-workspace last-dispatch Redis key, and
    dispatches DebounceWorkspaceMvRefresh (30 s, unique-per-workspace).
    When the MV refresh succeeds the job emits WorkspaceDataUpdated on
    project.{project_id}.ingestion, which is what the SPA pages
    subscribe to.

    Best-effort: a broadcast failure is logged as a warning but the
    asset still succeeds. The durable record is the data_version bump
    already committed above.
    """
    service_key = os.environ.get("FASTAPI_SERVICE_KEY")
    if not service_key:
        context.log.warning(
            "commit_ingestion_run: FASTAPI_SERVICE_KEY not set — "
            "skipping ingestion.progress broadcast for project=%s",
            project_id,
        )
        return False

    laravel_url = os.environ.get(
        "LARAVEL_INTERNAL_URL", "http://laravel.test",
    ).rstrip("/")
    url = f"{laravel_url}/api/internal/v1/ingest-progress/broadcast"

    payload = {
        "workspace_id":    workspace_id,
        "project_id":      project_id,
        "pipeline_run_id": pipeline_run_id,
        "stage":           "commit",
        "status":          "completed",
        "message":         "Dagster commit_ingestion_run completed.",
    }

    try:
        with httpx.Client(timeout=3.0) as client:
            r = client.post(
                url,
                json=payload,
                headers={"X-Service-Key": service_key, "Accept": "application/json"},
            )
        if r.status_code >= 400:
            context.log.warning(
                "commit_ingestion_run: broadcast non-2xx project=%s http=%s body=%s",
                project_id, r.status_code, r.text[:200],
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        context.log.warning(
            "commit_ingestion_run: broadcast failed project=%s err=%s",
            project_id, exc,
        )
        return False


def _broadcast_admin_surface(
    surface: str,
    affected_props: list,
    payload: dict,
    context: AssetExecutionContext,
    surface_id: Optional[str] = None,
) -> bool:
    """POST to Laravel's admin-surface-updated bridge (Phase 2).

    Drives the Admin/ClusterIngest + Admin/WorkflowRuns pages so the
    operator sees Dagster runs land live without manual refresh.
    Best-effort; identical failure semantics to _broadcast_ingestion_completed.
    """
    service_key = os.environ.get("FASTAPI_SERVICE_KEY")
    if not service_key:
        return False

    laravel_url = os.environ.get(
        "LARAVEL_INTERNAL_URL", "http://laravel.test",
    ).rstrip("/")
    url = f"{laravel_url}/api/internal/v1/admin-surface-updated"

    body = {"surface": surface, "affected_props": affected_props, "payload": payload}
    if surface_id is not None:
        body["surface_id"] = surface_id

    try:
        with httpx.Client(timeout=3.0) as client:
            r = client.post(
                url,
                json=body,
                headers={"X-Service-Key": service_key, "Accept": "application/json"},
            )
        if r.status_code >= 400:
            context.log.warning(
                "commit_ingestion_run: admin-surface broadcast non-2xx "
                "surface=%s http=%s body=%s",
                surface, r.status_code, r.text[:200],
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        context.log.warning(
            "commit_ingestion_run: admin-surface broadcast failed surface=%s err=%s",
            surface, exc,
        )
        return False


def _run_tune_target(
    postgres: PostgresResource,
    table: str,
    index: str,
    matview: Optional[str],
    context: AssetExecutionContext,
) -> dict:
    """Execute CLUSTER + ANALYZE + optional MV REFRESH for one table.

    Each statement runs in its own connection so that CLUSTER's ACCESS EXCLUSIVE
    lock and REFRESH CONCURRENTLY's ShareUpdateExclusive lock are not held
    simultaneously longer than needed.

    Returns a timing dict for metadata emission.
    """
    outcome = {"table": table, "cluster_ok": False, "analyze_ok": False, "matview_ok": None}

    # --- CLUSTER ---
    t0 = time.monotonic()
    try:
        # CLUSTER must run outside a transaction block in some PG configurations;
        # psycopg2 with autocommit=False still works from PG13+ but we use a
        # dedicated connection with autocommit=True for CLUSTER to be safe.
        raw_conn = postgres._connect()
        raw_conn.autocommit = True
        with raw_conn.cursor() as cur:
            # Verify index exists before attempting CLUSTER — avoids a hard error
            # if the index was not yet created (e.g. empty table after first run).
            cur.execute(
                """
                SELECT 1 FROM pg_indexes
                WHERE schemaname = %(schema)s
                  AND tablename  = %(tbl)s
                  AND indexname  = %(idx)s;
                """,
                {
                    "schema": table.split(".")[0] if "." in table else "public",
                    "tbl": table.split(".")[-1],
                    "idx": index,
                },
            )
            idx_exists = cur.fetchone()
            if idx_exists:
                cur.execute(f"CLUSTER {table} USING {index};")
                outcome["cluster_ok"] = True
                context.log.info(
                    "commit_ingestion_run: CLUSTER %s USING %s — %.2fs",
                    table,
                    index,
                    time.monotonic() - t0,
                )
            else:
                context.log.warning(
                    "commit_ingestion_run: index %s not found on %s — CLUSTER skipped",
                    index,
                    table,
                )
        raw_conn.close()
    except Exception as exc:
        context.log.warning(
            "commit_ingestion_run: CLUSTER %s failed (non-blocking): %s", table, exc
        )

    # --- ANALYZE ---
    t1 = time.monotonic()
    try:
        with postgres.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(f"ANALYZE {table};")
        outcome["analyze_ok"] = True
        context.log.info(
            "commit_ingestion_run: ANALYZE %s — %.2fs", table, time.monotonic() - t1
        )
    except Exception as exc:
        context.log.warning(
            "commit_ingestion_run: ANALYZE %s failed (non-blocking): %s", table, exc
        )

    # --- REFRESH MATERIALIZED VIEW CONCURRENTLY (if applicable) ---
    if matview:
        t2 = time.monotonic()
        try:
            # REFRESH CONCURRENTLY must also be outside a transaction block.
            raw_conn = postgres._connect()
            raw_conn.autocommit = True
            with raw_conn.cursor() as cur:
                cur.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {matview};")
            raw_conn.close()
            outcome["matview_ok"] = True
            context.log.info(
                "commit_ingestion_run: REFRESH MV %s — %.2fs",
                matview,
                time.monotonic() - t2,
            )
        except Exception as exc:
            outcome["matview_ok"] = False
            context.log.warning(
                "commit_ingestion_run: REFRESH MV %s failed (non-blocking): %s",
                matview,
                exc,
            )

    return outcome


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

@asset(
    group_name="commit",
    deps=[
        # Private-project pipeline terminal assets — all blocking checks on
        # these assets must pass before this asset can execute.
        silver_collars,      # deepest Silver for the collar/survey/litho/sample path
        silver_reports,      # Silver for the document/report path
        silver_spatial,      # Silver for the spatial path
        index_reports,       # Index for Qdrant embeddings
        index_neo4j,         # Index for Neo4j knowledge graph
        # Chunk 2 additions (Module 3 Phase B5/B6)
        silver_drill_traces, # Desurvey traces (blocking check: trace count gate)
        silver_cog_rasters,  # COG normalization (blocking check: cog_readable)
    ],
    description=(
        "Terminal pipeline asset.  Executes the data_version bump for the "
        "workspace and any projects that received new data this run.  Runs "
        "post-ingest PostgreSQL tuning (CLUSTER + ANALYZE + MV refresh) after "
        "the version commit.  Emits post-increment workspace_data_version and "
        "project_data_version as metadata for Module 7 Reverb consumption. "
        "Only executes when ALL upstream blocking asset checks pass — this is "
        "the hard-gate guarantee per addendum §05d and Module 3 spec B1/B9/B10."
    ),
)
def commit_ingestion_run(
    context: AssetExecutionContext,
    config: CommitIngestionRunConfig,
    postgres: PostgresResource,
) -> MaterializeResult:
    """Atomically bump data_version then run post-ingest tuning.

    Dependency chain (Dagster enforces blocking checks before reaching here):
        bronze_* → silver_* -[blocking check]-> gold_* → index_* -[blocking check]-
            → commit_ingestion_run
    """
    project_ids = _parse_project_ids(config.project_ids)

    context.log.info(
        "commit_ingestion_run: bumping data_version for workspace=%s, projects=%s",
        config.workspace_id,
        project_ids or "(none)",
    )

    # -----------------------------------------------------------------------
    # Step 1: Atomic data_version bump
    # -----------------------------------------------------------------------
    t_bump_start = time.monotonic()
    version_result = _bump_data_version(postgres, config.workspace_id, project_ids, context)
    t_bump_elapsed = time.monotonic() - t_bump_start

    workspace_dv = version_result["workspace_data_version"]
    project_versions = version_result["project_versions"]

    context.log.info(
        "commit_ingestion_run: data_version bump complete in %.2fs — "
        "workspace_data_version=%d, projects_bumped=%d",
        t_bump_elapsed,
        workspace_dv,
        len(project_versions),
    )

    # -----------------------------------------------------------------------
    # Step 1.5: Broadcast ingestion.progress 'completed' so Laravel kicks off
    # the data_version bump (SETNX-guarded) + DebounceWorkspaceMvRefresh +
    # WorkspaceDataUpdated cascade. Replaces the "Module 7 reads metadata"
    # placeholder noted in this asset's header docstring.
    #
    # Per-project broadcast: every project in this run gets its own event so
    # the SPA pages (subscribed on project.{projectId}.ingestion) wake up.
    # Done outside the data_version transaction so broadcast failures cannot
    # roll back the bump — best-effort by design.
    #
    # Skipped when no projects updated this run (public_geoscience-only run).
    # -----------------------------------------------------------------------
    broadcast_results: list[dict] = []
    for project_id in project_ids:
        ok = _broadcast_ingestion_completed(
            workspace_id=config.workspace_id,
            project_id=project_id,
            pipeline_run_id=context.run_id,
            context=context,
        )
        broadcast_results.append({"project_id": project_id, "ok": ok})

    # Phase 2 admin surface push — one workflow-runs broadcast + one
    # cluster-ingest broadcast per Dagster materialization. These drive
    # Admin/WorkflowRuns + Admin/HatchetWorkers + Admin/ClusterIngest.
    # Workflow-level (not per-project) — operators care about the run,
    # not per-project breakdowns at the admin tier.
    admin_payload = {
        "workflow_kind": "commit_ingestion_run",
        "workspace_id": config.workspace_id,
        "pipeline_run_id": context.run_id,
        "projects_bumped": len(project_versions),
        "workspace_data_version": workspace_dv,
        "status": "success",
        "engine": "dagster",
    }
    _broadcast_admin_surface(
        surface="workflow-runs",
        affected_props=["workflow_runs"],
        payload=admin_payload,
        context=context,
    )
    _broadcast_admin_surface(
        surface="cluster-ingest",
        affected_props=["kpis", "recent_runs", "per_project"],
        payload=admin_payload,
        context=context,
    )

    # -----------------------------------------------------------------------
    # Step 2: Post-ingest PostgreSQL tuning (best-effort, non-blocking on error)
    # Each table runs independently — failure on one does not skip the others.
    # -----------------------------------------------------------------------
    t_tune_start = time.monotonic()
    tune_outcomes = []
    for target in _TUNE_TARGETS:
        outcome = _run_tune_target(
            postgres,
            table=target["table"],
            index=target["index"],
            matview=target["matview"],
            context=context,
        )
        tune_outcomes.append(outcome)
    t_tune_elapsed = time.monotonic() - t_tune_start

    tables_tuned = [o["table"] for o in tune_outcomes if o["cluster_ok"] or o["analyze_ok"]]
    tables_skipped = [o["table"] for o in tune_outcomes if not o["cluster_ok"] and not o["analyze_ok"]]

    context.log.info(
        "commit_ingestion_run: post-ingest tune complete in %.2fs — "
        "tuned=%s, skipped=%s",
        t_tune_elapsed,
        tables_tuned,
        tables_skipped,
    )

    # -----------------------------------------------------------------------
    # Emit materialization metadata
    # Module 7 reads workspace_data_version + project_data_version from here
    # to broadcast the ingestion.progress Reverb event (Module 7 scope).
    # -----------------------------------------------------------------------
    project_dv_str = ", ".join(
        f"{pv['project_id']}={pv['data_version']}" for pv in project_versions
    ) or "none"

    # Broadcast summary — count how many per-project broadcasts succeeded
    # vs. were swallowed. Surfaces broadcast wiring breakage in the
    # Dagster materialization metadata.
    broadcasts_ok = sum(1 for b in broadcast_results if b["ok"])
    broadcasts_failed = len(broadcast_results) - broadcasts_ok

    return MaterializeResult(
        metadata={
            # data_version fields
            "workspace_id":             MetadataValue.text(config.workspace_id),
            "workspace_data_version":   MetadataValue.int(workspace_dv),
            "project_data_versions":    MetadataValue.text(project_dv_str),
            "projects_bumped":          MetadataValue.int(len(project_versions)),
            # Reverb broadcast outcome — per-project ingestion.progress
            # status='completed' POSTs to /api/internal/v1/ingest-progress/broadcast.
            "broadcasts_ok":            MetadataValue.int(broadcasts_ok),
            "broadcasts_failed":        MetadataValue.int(broadcasts_failed),
            # Timing
            "data_version_bump_sec":    MetadataValue.float(round(t_bump_elapsed, 3)),
            "post_ingest_tune_sec":     MetadataValue.float(round(t_tune_elapsed, 3)),
            # Tune outcome
            "tune_tables_tuned":        MetadataValue.text(str(tables_tuned)),
            "tune_tables_skipped":      MetadataValue.text(str(tables_skipped)),
        }
    )
