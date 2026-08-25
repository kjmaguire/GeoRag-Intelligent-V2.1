"""Hatchet worker entrypoint.

The demo compose stack runs the merged ``all`` pool. The narrower pool
selectors remain available for deployments that still split workers:

  ``ingestion``  — registers ``outbox_dispatcher`` + ingestion-class agent
                   workflows (storage tiering, index health, store
                   reconciliation). Subscribes to PDF + secondary-store
                   propagation work.
  ``ai``         — registers ``audit_ledger_verify`` + AI-class agent
                   workflows (tenant isolation, lineage, model watch,
                   vLLM security, cost summary, LLM incident diagnosis,
                   support packet).
  ``all``        — DEFAULT for back-compat — registers every workflow.
                   Used by the legacy single-worker container during the
                   transition. Will be removed after Step 2 lands cleanly.

CLI flags:
  ``--list``   — print registered workflow names + exit (no engine connect).
  default      — register + serve.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from app.hatchet_workflows import hatchet
from app.hatchet_workflows.answer_quality_watch import answer_quality_watch  # OBS-12
from app.hatchet_workflows.audit_ledger_verify import audit_ledger_verify
from app.hatchet_workflows.cold_tier_archive import cold_tier_archive_workflow  # §11.10
from app.hatchet_workflows.continuous_learning_loop import continuous_learning_loop  # doc-phase 102
from app.hatchet_workflows.cost_burn_watcher import cost_burn_watcher  # §5
from app.hatchet_workflows.embed_pending_passages import embed_pending_passages_wf  # doc-phase 183
from app.hatchet_workflows.enrich_passage_context import enrich_passage_context_wf  # contextual retrieval
from app.hatchet_workflows.external_notification import external_notification
from app.hatchet_workflows.field_outcome_learning import field_outcome_learning  # doc-phase 94
from app.hatchet_workflows.flow_jwt_key_reaper import flow_jwt_key_reaper
from app.hatchet_workflows.generate_report import generate_report  # doc-phase 83
from app.hatchet_workflows.idempotency_keys_cleanup import idempotency_keys_cleanup  # §35.1 TTL cleanup
from app.hatchet_workflows.ingest_pdf import ingest_pdf
from app.hatchet_workflows.ingest_spatial import ingest_spatial  # SHP/GeoJSON/GPKG/QGIS vector ingest
from app.hatchet_workflows.ingest_tabular import ingest_tabular  # drill CSV + multi-sheet XLSX
from app.hatchet_workflows.ingest_well_logs import ingest_well_logs  # LAS downhole curves
from app.hatchet_workflows.ingest_zip_archive import ingest_zip_archive  # ZIP archive extraction + fan-out
from app.hatchet_workflows.mv_refresh_silver import mv_refresh_silver
from app.hatchet_workflows.nightly_ingestion_integrity import nightly_ingestion_integrity  # reliability spec Phase 5
from app.hatchet_workflows.nl_summaries import nl_summaries  # ADR-0012
from app.hatchet_workflows.outbox_dispatcher import outbox_dispatcher
from app.hatchet_workflows.pg_partman_maintenance import pg_partman_maintenance
from app.hatchet_workflows.phase0_agents import (
    AI_AGENT_WORKFLOWS,
    INGESTION_AGENT_WORKFLOWS,
)
from app.hatchet_workflows.phase2_smoke import phase2_smoke
from app.hatchet_workflows.promote_silver_to_gold import promote_silver_to_gold  # silver → visual tables
from app.hatchet_workflows.public_geo_sync import public_geo_sync  # weekly ArcGIS refresh
from app.hatchet_workflows.public_geoscience_pull import public_geoscience_pull
from app.hatchet_workflows.qdrant_payload_audit import qdrant_payload_audit_wf  # 2026-06-01 Guard 2
from app.hatchet_workflows.reliability_metrics_publisher import (
    reliability_metrics_publisher,  # reliability spec Phase 6
)
from app.hatchet_workflows.repair_shadow_aggregate import repair_shadow_aggregate
from app.hatchet_workflows.restore_workspace import restore_workspace  # doc-phase 100
from app.hatchet_workflows.retention_sweep import retention_sweep  # 2026-08-14 M1 retention
from app.hatchet_workflows.score_targets import score_targets  # doc-phase 88
from app.hatchet_workflows.stale_run_detector import stale_run_detector  # reliability spec Fix 1e
from app.hatchet_workflows.support_replay import support_replay  # doc-phase 98
from app.hatchet_workflows.tiff_normalize import (
    tiff_normalize,  # ADR-0005: lossless TIFF→PDF wrap, route through ingest_pdf
)
from app.hatchet_workflows.train_source_trust import train_source_trust  # doc-phase 102
from app.hatchet_workflows.train_target_model import train_target_model  # doc-phase 101
from app.hatchet_workflows.verbalize_page_images import verbalize_page_images_wf  # multimodal page descriptions
from app.hatchet_workflows.what_changed_detector import what_changed_detector  # doc-phase 94
from app.hatchet_workflows.what_changed_weekly import what_changed_weekly  # doc-phase 182 (§12 polish)

# §6.2 (bc_minfile_pull) + §6.3 (nrcan_geo_pull) workflows retired on
# 2026-05-25 — superseded by the Dagster Bronze→Silver pipeline
# (silver_pg_ca_bc_minfile / silver_pg_ca_*_bedrock_geology etc.).
# See docs/smdi_ingestion_2026_05_25.md.
from app.hatchet_workflows.workspace_export import workspace_export  # §11.3
from app.logging_config import configure_json_logging

# Pool → workflow list. Phase 1 Step 4 added `ingest_pdf` to the ingestion
# pool. Phase 2 Step 3 added phase2_smoke (placeholder); Step 4 added
# public_geoscience_pull (outbound scheduled-import); Step 5a added
# external_notification (inbound webhook). Phase 4 Step 6 retired the
# Phase 1 shadow_diff + shadow_diff_scan workflows along with the
# silver.shadow_runs table.
# Phase 0 workflow tuples are unpacked directly so the registered boot set
# is visible here. Later ``app.agents`` domain phases are not Hatchet pools.
POOLS = {
    "ingestion": [
        outbox_dispatcher,
        ingest_pdf,
        tiff_normalize,
        stale_run_detector,
        nightly_ingestion_integrity,
        reliability_metrics_publisher,
        ingest_zip_archive,
        # 2026-08-20 — the `spatial` upload category was answered with
        # 422 retired_pipeline from 2026-07-28, when Dagster was removed.
        # The parsers never stopped working; nothing was calling them.
        ingest_spatial,
        ingest_tabular,
        ingest_well_logs,
        *INGESTION_AGENT_WORKFLOWS,
    ],
    "ai": [
        audit_ledger_verify,
        # Plan §4b Stage 1 follow-up — nightly aggregator of repair-loop
        # shadow telemetry (silver.query_traces → gold.repair_shadow_daily).
        # Cron 02:15 UTC, 15 minutes after audit_ledger_verify so they
        # don't contend for connections.
        repair_shadow_aggregate,
        phase2_smoke, public_geoscience_pull, external_notification,
        # Phase 7 Step 2 (R-P6-2) — nightly reaper for expired
        # workflow.flow_jwt_keys rows.
        flow_jwt_key_reaper,
        # §35.1 / v2.0 Dim 4 closure (2026-05-18) — nightly cleanup of
        # expired workspace.idempotency_keys rows so the table doesn't
        # grow unbounded under R2+ agent invocations.
        idempotency_keys_cleanup,
        # 2026-08-14 DB audit M1 — nightly batched purge of expired
        # audit.query_audit_log rows (180d) + terminal
        # silver.ingest_progress rows (90d, always keeping the newest
        # attempt per file for the IngestionRuns UI). 04:45 UTC.
        retention_sweep,
        # Phase 15 Step 1 (R-P14-2) — nightly REFRESH MATERIALIZED VIEW
        # for the agent's silver fact-source MVs. Keeps the agent
        # from drifting back into Phase 14 R-P13-1 refusal state.
        mv_refresh_silver,
        # 2026-08-25 — the silver → gold promotion the Dagster retirement
        # (2026-07-28) removed without replacing. Without it
        # gold.drillhole_intervals_visual, gold.structure_measurements_visual
        # and silver.drill_traces have NO writer, so the Workspace's SECTION,
        # 3D, STRUCTURE, LOGS and COMPARE modes are blank for every project
        # no matter what ingests. Dispatched per-project by ingest_tabular
        # and swept nightly by nightly_ingestion_integrity.
        promote_silver_to_gold,
        # Doc-phase 83 / Master-plan §7.10 — generate_report wraps the
        # §15.1 Report Builder Graph in a durable Hatchet workflow.
        # Runs the report-builder planning pipeline.
        generate_report,
        # Doc-phase 88 / Master-plan §8.6 — score_targets wraps the
        # §18.2 Target Recommendation Graph in a durable Hatchet
        # workflow with R5 sign-off pause-resume. Currently skeleton.
        score_targets,
        # Doc-phase 94 / Master-plan §9.11 — field_outcome_learning
        # folds new drilling outcomes into target-model learning state.
        # Graduated doc-phase 184 — ETL-only (no XGBoost): aggregates
        # hits/misses per workspace + writes targeting.target_backtests +
        # decision_lessons_learned + audit.audit_ledger. Retraining still
        # gated on train_target_model graduation.
        field_outcome_learning,
        # Doc-phase 94 / Master-plan §9.13 — what_changed_detector
        # delta-detects workspace changes; feeds §7.2 what_changed
        # report template. Graduated doc-phase 147 — real audit-ledger
        # + silver.* counts.
        what_changed_detector,
        # Doc-phase 182 / Master-plan §12 polish — what_changed_weekly
        # cron-fans-out the detector across every active workspace
        # every Monday at 06:00 UTC. Emits a workspace.what_changed.
        # weekly_digest audit anchor (system-wide, NULL workspace_id).
        what_changed_weekly,
        # sync_silver_to_kg (doc-phase 183, silver → Neo4j sync) removed
        # 2026-07-28 (B1) along with Neo4j itself.
        # Doc-phase 183 — silver.document_passages → Qdrant embedding
        # sync. Runs BGE + SPLADE++ embeddings + upserts to the
        # georag_reports collection.
        embed_pending_passages_wf,
        # 2026-08-18 — hourly page-image verbalization. Inert unless
        # IMAGE_VERBALIZATION_ENABLED is set (the task returns before
        # touching PG), so registering it is a no-op until opted in.
        verbalize_page_images_wf,
        # 2026-06-01 Guard 2 — hourly payload-shape audit on georag_chunks.
        # Catches silent-degrade writers between FastAPI restarts (the
        # startup healthcheck at app/main.py section 6.5 only fires at
        # boot). Cheap (~50 scrolls + one audit_ledger row) and pages
        # within ~5 minutes of any new write producing minimal payloads.
        qdrant_payload_audit_wf,
        # Contextual retrieval — daily 04:30 UTC, before embed at 05:45 UTC.
        # Generates Qwen3 context headers (contextualized_content) so
        # passage_embedder uses enriched text for better recall.
        enrich_passage_context_wf,
        # ADR-0012 — synthesize a retrievable passage per structured row
        # (assay group, lithology interval, drillhole) so a question about
        # sample IDs, exact intervals or QA/QC flags has something to match
        # on. Registered but NOT scheduled: the first run over an existing
        # corpus writes one passage per row and every one of them is then
        # embedded, which is a spend an operator should agree to rather
        # than a cron start. Trigger it with a workspace_id, or with
        # dry_run to size it first.
        nl_summaries,
        # OBS-12 — compares yesterday's refusal / guard-fire /
        # zero-evidence / confidence signals against the trailing
        # week and logs ANSWER_QUALITY_REGRESSION when they move.
        # Reads silver.answer_runs; no LLM spend, no Azure change.
        answer_quality_watch,
        # Doc-phase 98 / Master-plan §10.10 — support_replay re-
        # executes failed workflows in dry-run mode for diagnosis.
        # Skeleton.
        support_replay,
        # Doc-phase 100 / Master-plan §11.3 — cross-store consistency
        # checks plus manifest-backed workspace restore.
        restore_workspace,
        # Doc-phase 101 / Master-plan §12.3 — train_target_model trains
        # a target-scoring model on accumulated target_outcomes.
        train_target_model,
        # Doc-phase 102 / Master-plan §12.7 — train_source_trust trains
        # per-workspace source-trust weights.
        train_source_trust,
        # Doc-phase 102 / Master-plan §12.10 — continuous_learning_loop
        # cron orchestrator tracks retraining readiness.
        continuous_learning_loop,
        # Master-plan §11.1 nightly backup crons -- backup_postgres,
        # backup_qdrant, backup_redis and backup_seaweedfs -- DELETED
        # 2026-08-23 at Kyle's direction. They wrote to a SeaweedFS
        # substrate that does not exist on Azure, so all four had been a
        # guaranteed nightly failure since the migration: ~35 log lines a
        # day of a capability nobody had.
        #
        # Not a gap. Postgres carries 35-day point-in-time restore from
        # Azure's own automated backups, which these never added to;
        # Qdrant is derived data rebuildable by re-embedding from
        # silver.document_passages; Redis is cache plus Horizon queues.
        # Blob storage is the one irreplaceable copy and is LRS -- three
        # replicas in one datacentre, which covers hardware failure and
        # not deletion. That trade was made deliberately.
        #
        # backup_neo4j went earlier, 2026-08-19, for the same shape of
        # reason: Neo4j was dropped in B1 and the workflow shelled out via
        # `docker exec` to a container that could never exist on Container
        # Apps.
        # 2026-06-27 audit T5 — advance the three monthly-partitioned
        # ledgers before their p_premake=3 window expires. 04:15 UTC.
        pg_partman_maintenance,
        # Master-plan §11.10 — nightly cold-tier archive (04:00 UTC,
        # after the backup window closes). Writes-only; pruning is
        # operator-gated.
        cold_tier_archive_workflow,
        # Master-plan §5 — cost-burn watcher emits cost.burn.alert
        # audit rows when a workspace's hourly LLM spend crosses the
        # per-workspace threshold. Cron every 5 min; idempotent within
        # the window so operators see one alert per breach, not 12.
        cost_burn_watcher,
        # bc_minfile_pull (§6.2) + nrcan_geo_pull (§6.3) retired 2026-05-25 —
        # superseded by Dagster Bronze→Silver pipeline. Stale cron entries on
        # the Hatchet engine side were cleared via the de-registration sweep
        # documented in docs/smdi_ingestion_2026_05_25.md.
        #
        # That Dagster pipeline then went dormant on 2026-07-28, leaving
        # public_geo.* with no writer at all: the local copy went three weeks
        # stale and Azure never received a row. public_geo_sync (03:30 UTC
        # Sundays) takes the job back, pulling the surveys' live ArcGIS
        # services directly instead of via a Bronze staging hop.
        public_geo_sync,
        # Master-plan §11.3 wave 1 — per-workspace logical export
        # (manual trigger; complements the §11.1 full-store backups).
        # Produces the JSONL.gz manifest that restore_workspace
        # dry_run=False consumes.
        workspace_export,
        *AI_AGENT_WORKFLOWS,
    ],
}
POOLS["all"] = POOLS["ingestion"] + POOLS["ai"]


def configure_worker_logging() -> None:
    """Install the JSON formatter and silence the chatty third-party loggers.

    Called from :func:`main`, deliberately NOT at module import.

    `configure_json_logging` goes through `logging.config.dictConfig`,
    which REPLACES the root handler list. This module is imported at
    module scope by tests (tests/test_pg_partman_maintenance.py imports
    POOLS), so configuring on import would tear pytest's capture and
    caplog handlers off the root logger for the rest of the session.
    `logging.basicConfig` — what this used to be — was safe there only
    because it is a documented no-op when root already has handlers.

    Why JSON at all: this worker is where all ingestion happens and where
    almost every observed failure happens, and it was the one tier that
    never called `configure_json_logging()`. Measured 2026-08-21 over the
    trailing 24h in workspace-georag4ad7: hatchet-worker-cc emitted 23,993
    console lines, of which 0 were JSON and 0 carried a trace_id, while
    fastapi-cc emitted 53,438 of which 53,380 were JSON. `Log_s` being a
    bare string is why no Log Analytics query can filter the worker's
    output by level, workspace, run or trace.
    """
    configure_json_logging(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    # Silence chatty third-party libraries that emit hundreds of thousands of
    # DEBUG records per PDF page. When LOG_LEVEL=debug for georag code, these
    # would otherwise flood Hatchet's log queue (`log queue is full, dropping
    # log message`), starve the asyncio event loop, and trigger Hatchet step
    # cancellations ("event loop blocked" / blocked_for=560s+), so uploaded
    # PDFs are received and parsed but never reach silver.reports.
    for _noisy in (
        "pdfminer", "pdfminer.pdfinterp", "pdfminer.psparser",
        "pdfminer.cmapdb", "pdfminer.pdfdocument", "pdfminer.pdfpage",
        "pdfplumber", "pdf2image",
        "PIL", "PIL.Image", "PIL.PngImagePlugin", "PIL.TiffImagePlugin",
        "unstructured", "unstructured.partition",
        "matplotlib", "matplotlib.font_manager",
        "urllib3.connectionpool", "botocore", "boto3", "s3transfer",
        # Azure Blob replaced S3 as the storage backend but this list did not
        # follow. azure.core's http_logging_policy logs a URL plus a full
        # request/response header block at INFO for EVERY blob call: ~9.6k
        # lines a day on this worker, about a third of its total output, all
        # of it burying the errors it sits between. Real failures still
        # surface - they are raised, and our own handlers log them.
        "azure", "azure.core", "azure.core.pipeline.policies",
        "azure.core.pipeline.policies.http_logging_policy", "azure.identity",
        "azure.storage", "azure.storage.blob",
        "grpc", "grpc._cython", "grpc._cython.cygrpc",
    ):
        logging.getLogger(_noisy).setLevel(logging.WARNING)


log = logging.getLogger("georag.hatchet_worker")


def _resolve_pool() -> tuple[str, list]:
    pool_name = os.environ.get("WORKER_POOL", "all").lower()
    if pool_name not in POOLS:
        raise SystemExit(
            f"WORKER_POOL='{pool_name}' is not one of {sorted(POOLS)}"
        )
    return pool_name, POOLS[pool_name]


def main() -> int:
    configure_worker_logging()
    parser = argparse.ArgumentParser(prog="app.hatchet_workflows.worker")
    parser.add_argument(
        "--list",
        action="store_true",
        help="Print registered workflow names and exit (no engine connection).",
    )
    args = parser.parse_args()

    pool_name, workflows = _resolve_pool()

    if args.list:
        for wf in workflows:
            print(wf.name)
        return 0

    default_worker = f"georag-hatchet-worker-{pool_name}"
    worker_name = os.environ.get("HATCHET_WORKER_NAME", default_worker)
    slots = int(os.environ.get("HATCHET_WORKER_SLOTS", "20"))

    # Phase 6 Step 1 (R-P5-1) — bootstrap OTel here, not at parser
    # module-load, so the service.name resource attribute reflects the
    # pool (-ingestion / -ai) and the exporter starts before the first
    # workflow run. install_tracer_provider() is a no-op when
    # OTEL_EXPORTER_OTLP_ENDPOINT isn't set.
    #
    # Moved 2026-07-28 (A1): this used to import from the bind-mounted
    # georag_dagster package, so an ImportError was the normal case when the
    # mount was absent. app.observability is first-party now, so an ImportError
    # here means opentelemetry itself is missing — still non-fatal (tracing is
    # optional), but worth logging as a warning rather than an info.
    try:
        from app.observability import install_tracer_provider
        installed = install_tracer_provider(default_service_name=worker_name)
        log.info("otel: tracer install -> %s", installed)
    except ImportError as exc:
        log.warning("otel: bootstrap unavailable (%s); continuing untraced", exc)

    log.info(
        "starting Hatchet worker pool=%s name=%s slots=%d workflows=[%s]",
        pool_name,
        worker_name,
        slots,
        ", ".join(wf.name for wf in workflows),
    )

    worker = hatchet.worker(worker_name, slots=slots, workflows=workflows)
    worker.start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
