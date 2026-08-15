#!/usr/bin/env python3
"""CI money-path smoke test -- ingest leg.

Drives the REAL parse -> persist -> embed pipeline against a fixture PDF,
without a live Hatchet engine or S3/SeaweedFS bronze storage. Companion to
scripts/ci/e2e_smoke_query.py (query leg) and tests/e2e_smoke/stub_backend.py
(stands in for Azure AI Foundry). See the "E2E money-path smoke (ADVISORY)"
job in .github/workflows/ci.yml for how these fit together.

Why not just call the ingest_pdf Hatchet workflow directly
------------------------------------------------------------
app.hatchet_workflows.ingest_pdf.preflight() and .parse() both start with a
bronze-bucket S3 GET (georag_object_storage) -- standing up SeaweedFS/S3 in
CI just to serve one local fixture file is unjustified infra for a smoke
test. Both steps' *actual* work is otherwise plain, S3-free functions:

  - preflight's page-count/validity check is a few lines of pikepdf +
    hashlib -- replicated inline below.
  - parse's real work is entirely inside the pure function
    ingest_pdf._run_parser_subprocess(body_bytes, sha256, ...), called
    directly on the fixture's bytes (no subprocess pool needed for one
    small PDF).

persist() is NOT reimplemented -- that function is ~500 lines of real
business logic (silver.reports/document_passages INSERTs, OCR-review
routing, stale-passage GC, audit-ledger writes) that changes independently
of this smoke test. Reimplementing it here would silently drift from the
real pipeline and defeat the point of an E2E check. Instead this script
builds a minimal fake Hatchet `Context` (duck-typed: only `.task_output()`
and `.workflow_run_id` are used by persist) and calls the REAL
`ingest_pdf._persist_body(input, ctx)` so any future change to persist()
logic is automatically exercised here too. persist()'s own fire-and-forget
dispatch of the embed_pending_passages Hatchet workflow will fail (no
Hatchet engine) and log a warning -- harmless, because this script embeds
explicitly afterwards via the plain async function
app.services.ingest.passage_embedder.embed_pending_passages(), pointed at
the stub Foundry backend.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # -> src/fastapi

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("e2e_smoke.ingest")

FIXTURE_PDF = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "ocr" / "PLS-2024-Technical-Report.pdf"
)

# Default seeded workspace (2026_04_20_100000_create_workspaces_and_data_version.php).
DEFAULT_WORKSPACE_ID = "a0000000-0000-0000-0000-000000000001"


def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "localhost")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag_test")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _slugify(value: str) -> str:
    """Mirror Laravel's Str::slug() default behavior (ascii, '-' separator)."""
    return re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()


async def _ensure_project(pool, *, project_id: str, workspace_id: str, project_name: str) -> None:
    """Insert the CI smoke project row (idempotent).

    silver.projects.slug is NOT NULL + UNIQUE. This INSERT bypasses Eloquent
    (and its Project::booted() creating-hook that normally derives slug), so
    it replicates that same convention here -- slugified name + first 8 chars
    of project_id (app/Models/Project.php) -- to stay unique across CI runs,
    which each generate a fresh project_id.
    """
    slug = f"{_slugify(project_name)}-{project_id[:8]}"
    await pool.execute(
        """
        INSERT INTO silver.projects (
            project_id, project_name, orientation_reference, workspace_id, slug
        ) VALUES ($1::uuid, $2, $3, $4::uuid, $5)
        ON CONFLICT (project_id) DO NOTHING
        """,
        project_id, project_name, "grid-north", workspace_id, slug,
    )


class _FakeHatchetContext:
    """Duck-typed stand-in for hatchet_sdk.Context.

    Only implements what ingest_pdf._persist_body actually reads:
    `.task_output(step)` (keyed by the real `preflight`/`parse` task
    objects imported from the module) and `.workflow_run_id` (used as an
    audit-ledger trace_id -- any string is fine).
    """

    def __init__(self, outputs: dict, workflow_run_id: str) -> None:
        self._outputs = outputs
        self.workflow_run_id = workflow_run_id

    def task_output(self, step):
        return self._outputs[step]


async def main() -> int:
    import asyncpg

    from app.hatchet_workflows.ingest_pdf import (
        IngestPdfInput,
        ParseOut,
        PreflightOut,
        _persist_body,
        _run_parser_subprocess,
        parse,
        preflight,
    )
    from app.services.ingest.passage_embedder import embed_pending_passages

    if not FIXTURE_PDF.exists():
        log.error("fixture PDF missing: %s", FIXTURE_PDF)
        return 1

    project_id = str(uuid.uuid4())
    workspace_id = DEFAULT_WORKSPACE_ID
    body_bytes = FIXTURE_PDF.read_bytes()
    sha256 = hashlib.sha256(body_bytes).hexdigest()

    import pikepdf
    with pikepdf.open(io.BytesIO(body_bytes)) as pdf:
        page_count = len(pdf.pages)

    log.info("ingest: fixture=%s sha256=%s pages=%d", FIXTURE_PDF.name, sha256[:12], page_count)

    pre_out = PreflightOut(
        sha256=sha256, page_count=page_count, file_size=len(body_bytes),
        encrypted=False, valid=True, error=None,
    )

    log.info("ingest: running parse_pdf_report (native-text fixture -- no OCR expected)")
    parsed_dict = _run_parser_subprocess(body_bytes, sha256)
    parse_out = ParseOut(**parsed_dict)
    log.info(
        "ingest: parsed sections=%d parser=%s is_scanned=%s",
        len(parse_out.sections), parse_out.parser_used, parse_out.is_scanned,
    )
    if parse_out.is_scanned or not parse_out.sections:
        log.error(
            "ingest: fixture did not take the expected native-text path "
            "(is_scanned=%s sections=%d) -- smoke test assumptions broken",
            parse_out.is_scanned, len(parse_out.sections),
        )
        return 1

    ingest_input = IngestPdfInput(
        workspace_id=workspace_id,
        project_id=project_id,
        minio_key=f"reports/{project_id}/{sha256}.pdf",
        file_size=len(body_bytes),
        correlation_token=f"e2e-smoke-{uuid.uuid4().hex}",
        actor_id=None,
    )
    ctx = _FakeHatchetContext(
        outputs={preflight: pre_out, parse: parse_out},
        workflow_run_id=f"e2e-smoke-{uuid.uuid4().hex}",
    )

    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2, statement_cache_size=0)
    try:
        await _ensure_project(
            pool, project_id=project_id, workspace_id=workspace_id,
            project_name="CI E2E Smoke Project",
        )

        log.info("ingest: calling REAL ingest_pdf._persist_body (silver.reports + document_passages)")
        final = await _persist_body(ingest_input, ctx)
        log.info(
            "ingest: persist done report_id=%s sections=%d passages_written=%d",
            final.report_id, final.sections_count, final.passages_written,
        )
        if not final.report_id or final.passages_written < 1:
            log.error("ingest: persist produced no report/passages -- aborting")
            return 1
    finally:
        await pool.close()

    log.info("ingest: embedding pending passages via stub Foundry backend")
    result = await embed_pending_passages(
        workspace_id=workspace_id, project_id=project_id, batch_size=32,
    )
    log.info(
        "ingest: embed done seen=%d embedded=%d upserted=%d errors=%s",
        result.passages_seen, result.passages_embedded,
        result.qdrant_points_upserted, result.errors,
    )
    if result.passages_embedded < 1 or result.errors:
        log.error("ingest: embed step did not fully succeed -- aborting")
        return 1

    # Hand the project_id to the query leg via GITHUB_OUTPUT (or stdout as
    # a fallback for local runs).
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as f:
            f.write(f"project_id={project_id}\n")
            f.write(f"workspace_id={workspace_id}\n")
    print(f"E2E_SMOKE_PROJECT_ID={project_id}")
    print(f"E2E_SMOKE_WORKSPACE_ID={workspace_id}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
