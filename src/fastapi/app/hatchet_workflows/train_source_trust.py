"""train_source_trust Hatchet workflow (§12.7 / §21.5).

Trains a source-trust model on accumulated workspace state:
  - citation accuracy
  - claim ledger consistency
  - recency
  - document type
  - author/issuer reputation

Writes per-source trust scores into ``silver.source_trust_scores``.

Phase H4 graduation — deterministic per-source aggregator. For each
distinct source_document_id in the workspace, computes a trust score
in [0, 1] from these signals:
  * **Citation validation rate** — fraction of cited claims from this
    source that passed ``silver.answer_citation_items`` validation
    (Layer 5 + Layer 3 checks).
  * **Recency** — exponential decay against the source's
    ``filing_date`` (or ``created_at`` fallback). 1.0 at <1y old,
    0.5 at ~5y, 0.2 at ~10y.
  * **Document-type prior** — peer-reviewed > NI 43-101 > assessment >
    unpublished memo.

Result blends the 3 components with weights (0.6 citation,
0.25 recency, 0.15 doc-type). Sources with fewer than
``min_citations_per_source`` citations are skipped (insufficient
signal).

When xgboost ships, the same workflow swaps to an ML model fit on the
fuller feature set (``silver.source_trust_features``). The call
surface + output schema are preserved.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.hatchet_workflows import hatchet


log = logging.getLogger("georag.hatchet.train_source_trust")


class TrainSourceTrustInput(BaseModel):
    workspace_id: UUID
    initiated_by_user_id: int
    min_citations_per_source: int = Field(
        default=3,  # Lowered from 10 — deterministic baseline can
                    # produce useful trust even on small samples.
        description="Skip sources with fewer than this many citations.",
    )
    model_version: str = Field(
        default="weighted_learned_v1",
        description="String tag for this version. xgboost branch "
                    "uses 'xgboost_vN'.",
    )
    train_request_id: UUID = Field(
        default_factory=uuid4, description="Idempotency key.",
    )


class TrainSourceTrustOutput(BaseModel):
    success: bool
    sources_scored: int = 0
    sources_skipped_low_signal: int = 0
    model_version: str | None = None
    training_metrics: dict[str, Any] = Field(default_factory=dict)
    notice: str | None = None
    failure_reason: str | None = None


def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


train_source_trust = hatchet.workflow(
    name="train_source_trust",
    input_validator=TrainSourceTrustInput,
)


# Document-type priors — extend per operator request.
_DOCTYPE_PRIOR: dict[str, float] = {
    "peer_reviewed":     0.9,
    "ni_43_101":         0.8,
    "assessment_file":   0.6,
    "company_internal":  0.5,
    "unpublished_memo":  0.4,
    "blog_post":         0.3,
    "unknown":           0.5,
}


def _recency_factor(filing_date: datetime | None) -> float:
    """Exponential decay over years. 1.0 at 0y, 0.5 at ~5y."""
    if filing_date is None:
        return 0.5
    now = datetime.now(tz=timezone.utc)
    if filing_date.tzinfo is None:
        filing_date = filing_date.replace(tzinfo=timezone.utc)
    age_years = max(0.0, (now - filing_date).days / 365.25)
    # half-life = 5y → decay constant = ln(2)/5
    return float(math.exp(-math.log(2) * age_years / 5.0))


def _compute_trust(
    *,
    citations_total: int,
    citations_validated: int,
    filing_date: datetime | None,
    doctype: str,
) -> tuple[float, dict[str, float]]:
    """Blend the 3 signals into a [0, 1] trust score."""
    citation_rate = (
        citations_validated / citations_total if citations_total > 0 else 0.5
    )
    recency = _recency_factor(filing_date)
    doctype_prior = _DOCTYPE_PRIOR.get((doctype or "unknown").lower(), 0.5)

    score = (
        0.60 * citation_rate
        + 0.25 * recency
        + 0.15 * doctype_prior
    )
    score = max(0.0, min(1.0, score))
    components = {
        "citation_rate": round(citation_rate, 4),
        "recency":       round(recency, 4),
        "doctype_prior": round(doctype_prior, 4),
    }
    return round(score, 3), components


@train_source_trust.task(execution_timeout=timedelta(hours=2), retries=0)
async def execute(
    input: TrainSourceTrustInput, ctx: Context
) -> TrainSourceTrustOutput:
    """Train per-workspace source-trust scores.

    Phase H4 — deterministic per-source aggregator. xgboost branch
    swaps in when the dep + ``silver.source_trust_features`` feedback
    pipeline are both in place.
    """
    ws = str(input.workspace_id)
    log.info(
        "train_source_trust.start workspace=%s request_id=%s",
        ws, input.train_request_id,
    )
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", ws,
        )

        # Aggregate per source_document_id from
        # silver.answer_citation_items joined to silver.reports.
        # citations_validated counts items with rejection_reason IS NULL.
        rows = await conn.fetch(
            """
            SELECT r.report_id::text  AS source_document_id,
                   r.filing_date,
                   COALESCE(r.parser_used, 'unknown') AS doctype_hint,
                   count(ci.answer_citation_item_id) AS citations_total,
                   count(ci.answer_citation_item_id)
                     FILTER (WHERE ci.rejection_reason IS NULL)
                     AS citations_validated
              FROM silver.reports r
              LEFT JOIN silver.answer_citation_items ci
                ON ci.evidence_id::text = r.report_id::text
               AND ci.workspace_id = $1::uuid
             WHERE r.workspace_id = $1::uuid
             GROUP BY r.report_id, r.filing_date, r.parser_used
            """,
            ws,
        )

        sources_scored = 0
        sources_skipped = 0
        component_avgs = {
            "citation_rate": [], "recency": [], "doctype_prior": [],
        }

        for r in rows:
            total = int(r["citations_total"] or 0)
            validated = int(r["citations_validated"] or 0)
            if total < input.min_citations_per_source:
                sources_skipped += 1
                continue

            # Map parser_used → doctype prior. Coarse mapping that
            # real operators can refine.
            parser = (r["doctype_hint"] or "").lower()
            if "pdfminer" in parser or "ni43" in parser:
                doctype = "ni_43_101"
            elif "ocr" in parser or "tesseract" in parser:
                doctype = "assessment_file"
            else:
                doctype = "company_internal"

            score, components = _compute_trust(
                citations_total=total,
                citations_validated=validated,
                filing_date=r["filing_date"],
                doctype=doctype,
            )
            for k, v in components.items():
                component_avgs[k].append(v)

            # Upsert trust score for this workspace + source +
            # model_version (unique constraint).
            await conn.execute(
                """
                INSERT INTO silver.source_trust_scores (
                    trust_score_id, workspace_id, source_document_id,
                    trust_score, model_version, computed_at
                )
                VALUES (gen_random_uuid(), $1::uuid, $2::uuid, $3, $4, NOW())
                ON CONFLICT (workspace_id, source_document_id, model_version)
                DO UPDATE SET
                    trust_score = EXCLUDED.trust_score,
                    computed_at = NOW()
                """,
                ws, r["source_document_id"], score, input.model_version,
            )
            sources_scored += 1

        # Aggregate metrics for the audit ledger.
        def _avg(lst: list[float]) -> float:
            return sum(lst) / len(lst) if lst else 0.0

        metrics = {
            "method":                       "deterministic_weighted_blend",
            "weights":                      {"citation_rate": 0.60, "recency": 0.25, "doctype_prior": 0.15},
            "sources_total_in_workspace":   len(rows),
            "sources_scored":               sources_scored,
            "sources_skipped_low_signal":   sources_skipped,
            "avg_citation_rate":            round(_avg(component_avgs["citation_rate"]), 4),
            "avg_recency":                  round(_avg(component_avgs["recency"]), 4),
            "avg_doctype_prior":            round(_avg(component_avgs["doctype_prior"]), 4),
            "model_version":                input.model_version,
            "train_request_id":             str(input.train_request_id),
        }

        notice: str | None = None
        if sources_scored == 0 and len(rows) == 0:
            notice = (
                "No silver.reports rows in this workspace — nothing to score."
            )
        elif sources_scored == 0 and sources_skipped > 0:
            notice = (
                f"All {sources_skipped} sources had fewer than "
                f"{input.min_citations_per_source} citations — none scored. "
                f"Re-run after more citation activity accumulates."
            )

        # Audit anchor.
        try:
            from app.audit import emit_audit
            await emit_audit(
                conn,
                action_type="source_trust.trained",
                workspace_id=ws,
                actor_id=input.initiated_by_user_id,
                actor_kind="agent",
                target_schema="silver",
                target_table="source_trust_scores",
                target_id=str(input.train_request_id),
                payload=metrics,
                trace_id=ctx.workflow_run_id if ctx else None,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("train_source_trust.audit_emit_failed err=%s", exc)

        log.info(
            "train_source_trust.complete workspace=%s scored=%d skipped=%d",
            ws, sources_scored, sources_skipped,
        )

        # Phase 2 admin surface push — Admin/MlTrainingRuns + Admin/WorkflowRuns
        # refresh on completion. Best-effort.
        try:
            from app.services.laravel_bridge import post_admin_surface_updated
            admin_payload = {
                "workflow_kind": "train_source_trust",
                "workspace_id": ws,
                "model_version": input.model_version,
                "sources_scored": sources_scored,
                "sources_skipped": sources_skipped,
                "status": "success",
            }
            await post_admin_surface_updated(
                surface="workflow-runs",
                affected_props=["workflow_runs"],
                payload=admin_payload,
            )
            await post_admin_surface_updated(
                surface="ml-training",
                affected_props=["runs"],
                payload=admin_payload,
            )
            # Phase 5 — train_source_trust directly writes source-trust
            # scores; the Admin/SourceTrust page reads those rows.
            await post_admin_surface_updated(
                surface="source-trust",
                affected_props=["scores"],
                payload=admin_payload,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "train_source_trust: admin surface broadcasts failed "
                "workspace=%s err=%s", ws, exc,
            )

        return TrainSourceTrustOutput(
            success=True,
            sources_scored=sources_scored,
            sources_skipped_low_signal=sources_skipped,
            model_version=input.model_version,
            training_metrics=metrics,
            notice=notice,
        )

    except Exception as exc:
        log.exception("train_source_trust.failed")
        # Phase 2 admin surface push — failure path too.
        try:
            from app.services.laravel_bridge import post_admin_surface_updated
            admin_payload = {
                "workflow_kind": "train_source_trust",
                "workspace_id": ws,
                "status": "failure",
                "failure_reason": f"{type(exc).__name__}: {str(exc)[:200]}",
            }
            await post_admin_surface_updated(
                surface="workflow-runs",
                affected_props=["workflow_runs"],
                payload=admin_payload,
            )
            await post_admin_surface_updated(
                surface="ml-training",
                affected_props=["runs"],
                payload=admin_payload,
            )
        except Exception as broadcast_exc:  # noqa: BLE001
            log.warning(
                "train_source_trust: admin surface broadcasts failed (in "
                "failure path) workspace=%s err=%s", ws, broadcast_exc,
            )

        return TrainSourceTrustOutput(
            success=False,
            failure_reason=f"{type(exc).__name__}: {str(exc)[:200]}",
        )
    finally:
        await conn.close()


__all__ = [
    "train_source_trust",
    "TrainSourceTrustInput",
    "TrainSourceTrustOutput",
    "_compute_trust",
    "_recency_factor",
]
