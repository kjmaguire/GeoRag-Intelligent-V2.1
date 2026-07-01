"""§04p ingest-helper — bridge between Hatchet ingest_pdf persist step and the
orchestrator + persistence chain.

**Master-plan §3 Step 7 (part C — doc-phase 57).** Internal module
(leading underscore): only the Hatchet `ingest_pdf` workflow + tests
should import it.

The Hatchet `ingest_pdf.persist` step calls this helper AFTER the
existing v1.49 writes (silver.reports + silver.document_passages +
audit). The helper is wrapped in try/except at the call site so any
§04p failure logs a warning but does not break the existing ingest
contract.

This dual-write pattern is the deliberate safety net for doc-phase
57's cutover:
- v1.49 path remains canonical for downstream retrieval (existing
  consumers unchanged).
- §04p path populates the 8 new silver tables (§9.3 + §9.6) for the
  Silver Review UI + §9.8 XGBoost classifier substrate.

When the §04p stack proves out on the 50-PDF acceptance corpus (Step 9),
the v1.49 writes can be retired in doc-phase 58+ and §04p becomes
the sole canonical source.
"""
from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import asyncpg

from app.ocr._orchestrator import orchestrate
from app.ocr._persist import (
    _dsn,
    persist_orchestrator_result,
    transactional_workspace_session,
)

log = logging.getLogger("georag.ocr.ingest_helper")


async def run_p04p_for_ingest(
    workspace_id: str,
    report_id: str,
    pdf_body: bytes,
    bronze_s3_key: str | None = None,
) -> dict[str, Any]:
    """Run the §04p stack + persist on a PDF body, keyed against the
    given workspace + report.

    The function writes a temporary file (orchestrator wants a Path),
    invokes the orchestrator chain, then writes to the 8 silver tables
    inside a single transaction with the workspace_id GUC set.

    Args:
        workspace_id: UUID string of the workspace.
        report_id: UUID string of the silver.reports row (must already
            exist — the FK constraint requires the report row to be
            present before any §04p silver-table writes).
        pdf_body: raw PDF bytes (already preflighted by the Hatchet
            preflight step, but the orchestrator re-preflights for
            defense in depth).

    Returns:
        A telemetry dict:
            {
                "ok": bool,
                "counts": dict[str, int],          # per-table row counts
                "document_profile": str | None,
                "recommended_action": str | None,
                "error": str | None,
            }

    The function does NOT raise — caller wraps in try/except to keep
    the dual-write semantic explicit, but defense in depth here means
    even an unexpected exception inside an inner call is caught.
    """
    telemetry: dict[str, Any] = {
        "ok": False,
        "counts": {},
        "document_profile": None,
        "recommended_action": None,
        "error": None,
    }

    tmp_path: Path | None = None
    pool: asyncpg.Pool | None = None
    try:
        # Write the PDF body to a temp file. tempfile.NamedTemporaryFile
        # with delete=False so we control cleanup explicitly inside the
        # finally block (vs the async wrapper potentially leaking).
        fd, tmp_name = tempfile.mkstemp(suffix=".pdf", prefix="p04p_")
        os.close(fd)
        tmp_path = Path(tmp_name)
        tmp_path.write_bytes(pdf_body)

        # Run the orchestrator chain. Pure async; no DB access.
        result = await orchestrate(tmp_path)
        telemetry["document_profile"] = (
            result.get("profile", {}) or {}
        ).get("document_profile")
        telemetry["recommended_action"] = (
            result.get("document_summary", {}) or {}
        ).get("recommended_action")

        # Persist to silver tables. New pool per call — same pattern as
        # the existing v1.49 persist step (Hatchet step instances are
        # short-lived; pool reuse across steps adds lifecycle complexity
        # for marginal gain).
        pool = await asyncpg.create_pool(
            _dsn(), min_size=1, max_size=2, statement_cache_size=0
        )
        async with transactional_workspace_session(
            pool, workspace_id
        ) as conn:
            counts = await persist_orchestrator_result(
                conn, workspace_id, report_id, result,
                bronze_s3_key=bronze_s3_key,
            )
        telemetry["counts"] = counts
        telemetry["ok"] = True
        log.info(
            "p04p ingest helper ok report=%s profile=%s action=%s counts=%s",
            report_id,
            telemetry["document_profile"],
            telemetry["recommended_action"],
            counts,
        )
    except Exception as exc:  # pragma: no cover — defensive
        telemetry["error"] = f"{type(exc).__name__}: {exc}"
        log.exception("p04p ingest helper failed report=%s err=%s", report_id, exc)
    finally:
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.close()
        if tmp_path is not None and tmp_path.exists():
            with contextlib.suppress(Exception):
                tmp_path.unlink()

    return telemetry
