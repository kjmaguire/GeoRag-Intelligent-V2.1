"""Hatchet worker entrypoint — supports two pool variants.

Reads ``WORKER_POOL`` env var (Phase 1 Step 2):

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
from app.hatchet_workflows.audit_ledger_verify import audit_ledger_verify
from app.hatchet_workflows.repair_shadow_aggregate import repair_shadow_aggregate
from app.hatchet_workflows.backup_neo4j import backup_neo4j  # §11.1
from app.hatchet_workflows.backup_postgres import backup_postgres  # §11.1
from app.hatchet_workflows.backup_qdrant import backup_qdrant  # §11.1
from app.hatchet_workflows.backup_redis import backup_redis  # §11.1
from app.hatchet_workflows.backup_seaweedfs import backup_seaweedfs  # §11.1
from app.hatchet_workflows.cold_tier_archive import cold_tier_archive_workflow  # §11.10
from app.hatchet_workflows.cost_burn_watcher import cost_burn_watcher  # §5
# §6.2 (bc_minfile_pull) + §6.3 (nrcan_geo_pull) workflows retired on
# 2026-05-25 — superseded by the Dagster Bronze→Silver pipeline
# (silver_pg_ca_bc_minfile / silver_pg_ca_*_bedrock_geology etc.).
# See docs/smdi_ingestion_2026_05_25.md.
from app.hatchet_workflows.workspace_export import workspace_export  # §11.3
from app.hatchet_workflows.ingest_pdf import ingest_pdf
from app.hatchet_workflows.outbox_dispatcher import outbox_dispatcher
from app.hatchet_workflows.stale_run_detector import stale_run_detector  # reliability spec Fix 1e
from app.hatchet_workflows.nightly_ingestion_integrity import nightly_ingestion_integrity  # reliability spec Phase 5
from app.hatchet_workflows.reliability_metrics_publisher import reliability_metrics_publisher  # reliability spec Phase 6
from app.hatchet_workflows.re_ocr_page import re_ocr_page  # doc-phase 63
from app.hatchet_workflows.ocr_quality_check import ocr_quality_check_wf  # Phase 6 (2026-05-22)
from app.hatchet_workflows.tiff_ocr_cluster import tiff_ocr_cluster  # Phase E.1 replacement for the one-off georag-phase-e-ocr container (DEPRECATED 2026-05-23 by tiff_normalize per ADR-0005)
from app.hatchet_workflows.tiff_normalize import tiff_normalize  # ADR-0005: lossless TIFF→PDF wrap, route through ingest_pdf
from app.hatchet_workflows.phase0_agents import (
    AI_AGENT_WORKFLOWS,
    INGESTION_AGENT_WORKFLOWS,
)
from app.hatchet_workflows.external_notification import external_notification
from app.hatchet_workflows.flow_jwt_key_reaper import flow_jwt_key_reaper
from app.hatchet_workflows.idempotency_keys_cleanup import idempotency_keys_cleanup  # §35.1 TTL cleanup
from app.hatchet_workflows.continuous_learning_loop import continuous_learning_loop  # doc-phase 102
from app.hatchet_workflows.evaluate_workspace import evaluate_workspace  # doc-phase 98
from app.hatchet_workflows.eval_real_rag_nightly import eval_real_rag_nightly  # doc-phase 170
from app.hatchet_workflows.sync_silver_to_kg import sync_silver_to_kg  # doc-phase 183
from app.hatchet_workflows.embed_pending_passages import embed_pending_passages_wf  # doc-phase 183
from app.hatchet_workflows.qdrant_payload_audit import qdrant_payload_audit_wf  # 2026-06-01 Guard 2
from app.hatchet_workflows.enrich_passage_context import enrich_passage_context_wf  # contextual retrieval
from app.hatchet_workflows.field_outcome_learning import field_outcome_learning  # doc-phase 94
from app.hatchet_workflows.generate_report import generate_report  # doc-phase 83
from app.hatchet_workflows.restore_workspace import restore_workspace  # doc-phase 100
from app.hatchet_workflows.score_targets import score_targets  # doc-phase 88
from app.hatchet_workflows.support_replay import support_replay  # doc-phase 98
from app.hatchet_workflows.train_source_trust import train_source_trust  # doc-phase 102
from app.hatchet_workflows.train_target_model import train_target_model  # doc-phase 101
from app.hatchet_workflows.what_changed_detector import what_changed_detector  # doc-phase 94
from app.hatchet_workflows.what_changed_weekly import what_changed_weekly  # doc-phase 182 (§12 polish)
from app.hatchet_workflows.mv_refresh_silver import mv_refresh_silver
from app.hatchet_workflows.phase2_smoke import phase2_smoke
from app.hatchet_workflows.public_geoscience_pull import public_geoscience_pull
from app.hatchet_workflows.score_answer_quality import score_answer_quality_wf  # answer quality eval
from app.hatchet_workflows.ingest_zip_archive import ingest_zip_archive  # ZIP archive extraction + fan-out


# Pool → workflow list. Phase 1 Step 4 added `ingest_pdf` to the ingestion
# pool. Phase 2 Step 3 added phase2_smoke (placeholder); Step 4 added
# public_geoscience_pull (outbound scheduled-import); Step 5a added
# external_notification (inbound webhook). Phase 4 Step 6 retired the
# Phase 1 shadow_diff + shadow_diff_scan workflows along with the
# silver.shadow_runs table.
#   ingestion : outbox_dispatcher + ingest_pdf + 3 agent workflows = 5
#   ai        : audit_ledger_verify + phase2_smoke + public_geoscience_pull
#               + external_notification + 7 agent workflows = 11
POOLS = {
    "ingestion": [outbox_dispatcher, ingest_pdf, re_ocr_page, ocr_quality_check_wf, tiff_ocr_cluster, tiff_normalize, stale_run_detector, nightly_ingestion_integrity, reliability_metrics_publisher, ingest_zip_archive] + INGESTION_AGENT_WORKFLOWS,
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
        # Phase 15 Step 1 (R-P14-2) — nightly REFRESH MATERIALIZED VIEW
        # for the agent's silver fact-source MVs. Keeps the agent
        # from drifting back into Phase 14 R-P13-1 refusal state.
        mv_refresh_silver,
        # Doc-phase 83 / Master-plan §7.10 — generate_report wraps the
        # §15.1 Report Builder Graph in a durable Hatchet workflow.
        # Currently a skeleton (raises NotImplementedError); registered
        # here so worker startup imports + Hatchet engine sees the
        # workflow name registered.
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
        # Doc-phase 98 / Master-plan §10.4 — evaluate_workspace runs
        # the golden-question eval suite + promotion gating. Skeleton.
        evaluate_workspace,
        # Doc-phase 170 / Master-plan §10.6 — eval_real_rag_nightly is
        # the cron wrapper that fires real_rag_v1 against
        # refusal_correctness nightly @ 05:15 UTC. Wraps
        # evaluate_workspace because evaluate_workspace requires a
        # non-default eval_request_id (incompatible with empty cron
        # payload). Generates a fresh UUID per fire.
        eval_real_rag_nightly,
        # Doc-phase 183 — silver → Neo4j sync. Wraps the kg_sync
        # service so cluster ingests can trigger KG population from a
        # workflow rather than out-of-band script.
        sync_silver_to_kg,
        # Doc-phase 183 — silver.document_passages → Qdrant embedding
        # sync. Runs BGE + SPLADE++ embeddings + upserts to the
        # georag_reports collection.
        embed_pending_passages_wf,
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
        # Doc-phase 98 / Master-plan §10.10 — support_replay re-
        # executes failed workflows in dry-run mode for diagnosis.
        # Skeleton.
        support_replay,
        # Doc-phase 100 / Master-plan §11.3 — restore_workspace runs
        # cross-store consistency restore for one workspace.
        # Graduated Phase G.2 — dry-run path counts rows in all five
        # stores (Postgres + Neo4j + Qdrant + Redis + SeaweedFS-future)
        # and verifies snapshot manifest. Real restore (dry_run=False)
        # still gated on §11.1 backup infrastructure.
        restore_workspace,
        # Doc-phase 101 / Master-plan §12.3 — train_target_model trains
        # an XGBoost target-scoring model on accumulated target_outcomes.
        # Skeleton (waits on xgboost dep + outcome accumulation).
        train_target_model,
        # Doc-phase 102 / Master-plan §12.7 — train_source_trust trains
        # per-workspace source-trust XGBoost model. Skeleton.
        train_source_trust,
        # Doc-phase 102 / Master-plan §12.10 — continuous_learning_loop
        # cron orchestrator triggers model retraining + eval. Skeleton.
        continuous_learning_loop,
        # Master-plan §11.1 — nightly backup crons. Staggered 15 min
        # apart starting 02:00 UTC (per kickoff locked defaults).
        backup_postgres,    # 02:00 UTC
        backup_neo4j,       # 02:15 UTC
        backup_qdrant,      # 02:30 UTC
        backup_redis,       # 02:45 UTC
        backup_seaweedfs,   # 03:00 UTC
        # Master-plan §11.10 — nightly cold-tier archive (04:00 UTC,
        # after the backup window closes). Writes-only; pruning is
        # operator-gated.
        cold_tier_archive_workflow,
        # Master-plan §5 — cost-burn watcher emits cost.burn.alert
        # audit rows when a workspace's hourly LLM spend crosses the
        # per-workspace threshold. Cron every 5 min; idempotent within
        # the window so operators see one alert per breach, not 12.
        cost_burn_watcher,
        # Answer quality eval — Qwen3-as-judge faithfulness + context
        # precision scoring. Nightly catch-up at 06:00 UTC; scores
        # audit.query_audit_log rows where faithfulness_score IS NULL.
        score_answer_quality_wf,
        # bc_minfile_pull (§6.2) + nrcan_geo_pull (§6.3) retired 2026-05-25 —
        # superseded by Dagster Bronze→Silver pipeline. Stale cron entries on
        # the Hatchet engine side were cleared via the de-registration sweep
        # documented in docs/smdi_ingestion_2026_05_25.md.
        # Master-plan §11.3 wave 1 — per-workspace logical export
        # (manual trigger; complements the §11.1 full-store backups).
        # Produces the JSONL.gz manifest that restore_workspace
        # dry_run=False consumes.
        workspace_export,
    ] + AI_AGENT_WORKFLOWS,
}
POOLS["all"] = POOLS["ingestion"] + POOLS["ai"]


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
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
    try:
        from georag_dagster.observability import install_tracer_provider
        installed = install_tracer_provider(default_service_name=worker_name)
        log.info("otel: tracer install -> %s", installed)
    except ImportError:
        log.info("otel: georag_dagster.observability not on path; skipping bootstrap")

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
