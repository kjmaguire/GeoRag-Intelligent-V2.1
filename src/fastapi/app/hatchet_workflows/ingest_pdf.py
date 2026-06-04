"""Phase 1 Step 4 — Hatchet ``ingest_pdf`` workflow (Step 4C refactor).

Shadow-replacement of the v1.49 PDF ingestion path documented in
``docs/phase1_v149_ingest_pdf_survey.md``. Decomposed into 3 steps that
mirror the v1.49 contract:

    1. preflight    — S3 GET, magic bytes, sha256, page count, size cap
    2. parse        — calls georag_dagster.parsers.pdf_report.parse_pdf_report()
                      which is the canonical v1.49 entry point — runs the
                      full pipeline (fitz → docling/tesseract OCR routing,
                      OCR if scanned, metadata, sections, resource tables)
    3. persist      — writes silver.reports + silver.shadow_runs + audit

Step 4A originally decomposed parse into 5 sub-steps; that was unnecessary
since the v1.49 contract IS the monolithic ``parse_pdf_report()``. The
3-step shape mirrors v1.49 exactly so the diff contract has cleanly
comparable outputs. Per-stage observability comes from the parser's own
logging + future Phase 11 instrumentation.

Pool: ``ingestion``. Action: ``ingest_pdf``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from concurrent.futures.process import BrokenProcessPool
from typing import Any
from uuid import UUID

import asyncpg
from hatchet_sdk import (
    ConcurrencyExpression,
    ConcurrencyLimitStrategy,
    Context,
)
from pydantic import BaseModel, Field

from app.agent.workspace_context import LEGACY_DEFAULT_TENANT_UUID
from app.audit import emit_audit
from app.db import bind_workspace_scope
from app.hatchet_workflows import hatchet
from app.hatchet_workflows import _progress as ingest_progress
from app.metrics import WORKSPACE_RESOLUTION_FAILURES


log = logging.getLogger("georag.hatchet.ingest_pdf")


# Subprocess pool used to run the (CPU-heavy, GIL-holding) PDF parse work
# off the main asyncio loop. With `asyncio.to_thread`, parses like docling
# or pdfplumber+OCR on 500-page PDFs hold the GIL so long that Hatchet's
# heartbeat handler (4s deadline) can't fire and the worker gets marked
# dead, in-flight tasks get cancelled, and we lose progress. A subprocess
# pool gives each parse its own GIL → main loop stays responsive.
#
# Phase 5 (2026-05-22) — subprocess pool sizing + memory guard.
#
# Pool sizing:
#   PARSE_SUBPROCESS_MAX_WORKERS env var, default min(os.cpu_count(), 4).
#   Previously hardcoded to 1 — meant every Hatchet slot beyond the first
#   queued behind one running parse. With 4 workers, a 12-PDF batch
#   completes ~3-4× faster (matches available core count without
#   oversubscribing the GPU pipeline).
#
# Memory guard:
#   Before submitting a parse to the pool, the parse task awaits
#   _wait_for_memory_headroom() which polls psutil.virtual_memory().available.
#   If RAM < PARSE_MIN_FREE_RAM_MB (default 1500), the task waits up to
#   PARSE_MEMORY_WAIT_MAX_S (default 30) then raises MemoryError so
#   Hatchet retries on a freer worker.
_PARSE_POOL: Any = None


def _compute_parse_max_workers() -> int:
    """Resolve the subprocess pool size from env + system characteristics.

    Order of precedence:
      1. PARSE_SUBPROCESS_MAX_WORKERS env var (if a positive integer)
      2. min(os.cpu_count() or 1, 4) — safe default that scales with the
         host but caps at 4 (peak ~6 GB combined RSS for 4 parallel parses,
         leaves headroom for the AI worker + vLLM + system).
      3. Falls back to 1 if psutil is unavailable so the pool still works
         in stripped-down environments.
    """
    env = os.environ.get("PARSE_SUBPROCESS_MAX_WORKERS")
    if env:
        try:
            v = int(env)
            return max(1, v)
        except ValueError:
            log.warning(
                "ingest_pdf: PARSE_SUBPROCESS_MAX_WORKERS=%r is not an int; "
                "using computed default",
                env,
            )
    try:
        import psutil  # noqa: F401, PLC0415
    except ImportError:
        log.warning(
            "ingest_pdf: psutil unavailable — memory guard disabled, pool "
            "size locked to 1 (legacy behavior)"
        )
        return 1
    return max(1, min(os.cpu_count() or 1, 4))


async def _wait_for_memory_headroom(
    min_free_mb: int,
    max_wait_s: int,
    poll_interval_s: float = 2.0,
) -> None:
    """Block until ``psutil.virtual_memory().available`` ≥ ``min_free_mb``,
    or raise MemoryError after ``max_wait_s``.

    Used before submitting a parse to the subprocess pool so concurrent
    parses don't pile on and OOM the worker. Polls every
    ``poll_interval_s`` and logs at attempt + retry boundaries.

    When psutil isn't available the function returns immediately
    (degrade gracefully — caller still gets the parse done; OOM risk
    falls back to OS oom-killer, which is what we had pre-Phase 5
    anyway).
    """
    try:
        import psutil  # noqa: PLC0415
    except ImportError:
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait_s
    waited = 0.0
    while True:
        vm = psutil.virtual_memory()
        avail_mb = vm.available / (1024 * 1024)
        if avail_mb >= min_free_mb:
            if waited > 0:
                log.info(
                    "ingest_pdf.memory_guard: cleared after %.1fs "
                    "(available=%.0fMB ≥ threshold=%dMB)",
                    waited, avail_mb, min_free_mb,
                )
            return
        if loop.time() >= deadline:
            raise MemoryError(
                f"ingest_pdf.memory_guard: still {avail_mb:.0f}MB available "
                f"after {waited:.1f}s wait (threshold {min_free_mb}MB) — "
                f"Hatchet will retry on a freer worker"
            )
        log.info(
            "ingest_pdf.memory_guard: waiting for RAM "
            "(available=%.0fMB < threshold=%dMB, waited=%.1fs)",
            avail_mb, min_free_mb, waited,
        )
        await asyncio.sleep(poll_interval_s)
        waited += poll_interval_s


def _get_parse_pool():
    """Lazily create a multi-worker ProcessPoolExecutor for PDF parsing.

    spawn-method to avoid forking a process with a heavy live worker
    state (open sockets, qdrant clients, etc.) — fork would copy them
    and break asyncio internals on Linux. Phase 5 sizes the pool via
    _compute_parse_max_workers() so a batch of N PDFs from one worker
    completes in ~ceil(N / pool_size) × parse_time instead of N ×
    parse_time.
    """
    global _PARSE_POOL
    if _PARSE_POOL is None:
        from concurrent.futures import ProcessPoolExecutor
        import multiprocessing as mp
        ctx = mp.get_context("spawn")
        workers = _compute_parse_max_workers()
        _PARSE_POOL = ProcessPoolExecutor(max_workers=workers, mp_context=ctx)
        log.info(
            "ingest_pdf: created ProcessPoolExecutor max_workers=%d (cpu_count=%s)",
            workers, os.cpu_count(),
        )
    return _PARSE_POOL


def _reset_parse_pool() -> None:
    """Tear down the cached ProcessPoolExecutor so the next parse rebuilds.

    Called when a worker dies (BrokenProcessPool) — the pool is poisoned;
    subsequent submit() calls would also fail. We shutdown(wait=False)
    so the next _get_parse_pool() invocation creates a fresh pool with
    fresh workers.
    """
    global _PARSE_POOL
    if _PARSE_POOL is not None:
        try:
            _PARSE_POOL.shutdown(wait=False, cancel_futures=True)
        except Exception as exc:  # pragma: no cover — best-effort
            log.warning("ingest_pdf: parse pool shutdown raised %s — ignoring", exc)
        _PARSE_POOL = None


# Shared cache directory for PDF bodies. Parse writes the PDF here keyed
# by SHA so downstream Hatchet tasks (§04p) can re-use it without
# re-downloading from S3. /tmp is tmpfs in most container setups so this
# is essentially free; entries get cleaned up by container restart or the
# explicit cleanup hook below.
_PDF_BODY_CACHE_DIR = "/tmp/georag_ingest_pdf_cache"


def _cached_pdf_path(sha256: str) -> str:
    """Return the path where a PDF body lives in the local body cache."""
    import os as _os
    _os.makedirs(_PDF_BODY_CACHE_DIR, exist_ok=True)
    return f"{_PDF_BODY_CACHE_DIR}/{sha256}.pdf"


def _run_parser_subprocess(body_bytes: bytes, sha256: str) -> dict:
    """Module-level wrapper for the parse so ProcessPoolExecutor can pickle it.

    Returns a plain dict (not a Pydantic model) — easier to pickle across
    process boundaries; caller reconstitutes ParseOut.

    Side effect: writes body_bytes to _PDF_BODY_CACHE_DIR/{sha256}.pdf
    so the §04p task can re-use the same file without re-downloading.

    Phase 1 (2026-05-22): the parser returns a `figure_manifest` listing
    each docling-extracted figure already uploaded to
    figures/_pending/{sha256}/... The manifest is propagated to the
    persist task via the returned dict; persist renames each pending key
    to figures/{report_id}/... before recording the section.
    """
    import os as _os
    import shutil as _shutil
    import time as _time
    from georag_dagster.parsers.pdf_report import (
        parse_pdf_report,
        _FIGURE_TEMPDIR_ROOT,
    )

    _os.makedirs(_PDF_BODY_CACHE_DIR, exist_ok=True)
    cached_path = f"{_PDF_BODY_CACHE_DIR}/{sha256}.pdf"
    # Write the PDF to the persistent (tmpfs) cache so §04p can re-use
    # it. Using a temp file inside the cache dir + rename for atomicity.
    tmp_path = cached_path + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(body_bytes)
    _os.replace(tmp_path, cached_path)

    try:
        t_start = _time.monotonic()
        result = parse_pdf_report(cached_path)
        elapsed_ms = int((_time.monotonic() - t_start) * 1000)

        return {
            "sha256": sha256,
            "title": getattr(result, "title", None),
            "authors": list(getattr(result, "authors", []) or []),
            "company": getattr(result, "company", None),
            "filing_date": getattr(result, "filing_date", None),
            "commodity": getattr(result, "commodity", None),
            "project_name": getattr(result, "project_name", None),
            "region": getattr(result, "region", None),
            "sections": [
                {
                    "section_number": getattr(s, "section_number", None),
                    "section_title": getattr(s, "section_title", None),
                    "text": getattr(s, "text", None),
                    "page_first": getattr(s, "page_first", None),
                    "page_last": getattr(s, "page_last", None),
                    # Phase 3 (2026-05-22) — OCR provenance per chunk.
                    # None for chunks that came from the PDF text layer
                    # (fitz_native, pdfplumber_native); 0.0–1.0 for
                    # OCR'd chunks. Travels through ParseOut → persist
                    # → silver.document_passages → qdrant payload.
                    "ocr_confidence": getattr(s, "ocr_confidence", None),
                    "ocr_method": getattr(s, "ocr_method", None),
                }
                for s in (getattr(result, "sections", None) or [])
            ],
            "parse_quality_pct": float(getattr(result, "parse_quality_pct", 0.0) or 0.0),
            "parser_used": str(getattr(result, "parser_used", "unknown") or "unknown"),
            "skipped_elements": int(getattr(result, "skipped_elements", 0) or 0),
            "warnings": [
                w if isinstance(w, dict) else {"message": str(w)}
                for w in (getattr(result, "warnings", None) or [])
            ],
            "page_languages": list(getattr(result, "page_languages", []) or []),
            "resource_tables": list(getattr(result, "resource_tables", []) or []),
            "figures": list(getattr(result, "figure_manifest", []) or []),
            "parse_duration_ms": elapsed_ms,
            "is_scanned": bool(getattr(result, "is_scanned", False)),
        }
    finally:
        # Best-effort cleanup of any per-sha figure tempdir the parser
        # may have created. PNGs were already uploaded to S3 (figures/
        # _pending/{sha}/...) so the on-disk copy is no longer needed.
        try:
            _shutil.rmtree(f"{_FIGURE_TEMPDIR_ROOT}/{sha256}", ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass


# =============================================================================
# Input + per-step output models
# =============================================================================
class IngestPdfInput(BaseModel):
    """The Laravel ShadowRouter (Step 5) sends this when dual-writing.

    `project_id` is typed `str` (not `UUID`) so the many downstream
    `str(input.project_id)` call sites stay no-ops, but a Pydantic
    field_validator rejects non-UUID input at the boundary —
    defence in depth against the SQL-injection shape the
    2026-06-02/03 audit caught on the sibling ingest_zip_archive
    trigger. The shadow_trigger router uses parameter binding so a
    malformed string can't actually inject, but this guard prevents
    malformed rows from ever landing in silver.ingest_progress.
    """

    workspace_id: UUID = Field(..., description="Workspace context for RLS.")
    project_id: str = Field(..., description="Project the upload belongs to.")
    minio_key: str = Field(..., description="Bronze S3 key (reports/{projectId}/...).")
    file_size: int = Field(..., description="Bytes (from Laravel multipart upload).")
    vendor_profile_id: int | None = Field(default=None)
    correlation_token: str = Field(
        ..., description="Shared token for shadow_runs row pairing — also the dedupe key."
    )
    actor_id: int | None = Field(default=None, description="public.users.id of uploader.")

    # Defence-in-depth UUID guard; see class docstring.
    from pydantic import field_validator as _fv

    @_fv("project_id")
    @classmethod
    def _validate_project_id_uuid(cls, v: str) -> str:
        import re as _re
        if not _re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            v,
            _re.IGNORECASE,
        ):
            raise ValueError(
                "IngestPdfInput.project_id must be a UUID (canonical 8-4-4-4-12 form)."
            )
        return v


class PreflightOut(BaseModel):
    sha256: str
    page_count: int
    file_size: int
    encrypted: bool
    valid: bool
    error: str | None = None


class ParseOut(BaseModel):
    """Mirror of the relevant v1.49 ReportParseResult fields, serialised."""

    sha256: str
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    company: str | None = None
    filing_date: str | None = None
    commodity: str | None = None
    project_name: str | None = None
    region: str | None = None
    sections: list[dict] = Field(default_factory=list)
    parse_quality_pct: float = 0.0
    parser_used: str = ""
    skipped_elements: int = 0
    warnings: list[dict] = Field(default_factory=list)
    page_languages: list[str] = Field(default_factory=list)
    resource_tables: list[dict] = Field(default_factory=list)
    # Phase 1 (2026-05-22): docling figure manifest. Each entry is a
    # dict {idx, page, bbox, caption, pending_key, bucket, sha256}.
    # Populated by _run_parser_subprocess from
    # ReportParseResult.figure_manifest. Persist task copies each
    # pending_key to figures/{report_id}/... then deletes the pending
    # object, and builds a ReportSection per figure for chat retrieval.
    figures: list[dict] = Field(default_factory=list)
    parse_duration_ms: int = 0
    is_scanned: bool = False


class IngestPdfFinalOut(BaseModel):
    """The final output emitted to Hatchet's run record. Mirrors the v1.49
    ReportParseResult shape so the diff harness can compare apples-to-apples.
    """

    sha256: str
    parser_used: str
    parse_quality_pct: float
    page_count: int
    title: str | None
    authors: list[str]
    company: str | None
    filing_date: str | None
    commodity: str | None
    project_name: str | None
    region: str | None
    sections_count: int
    resource_tables_count: int
    is_scanned: bool
    warnings_count: int
    parse_duration_ms: int
    persist_duration_ms: int
    report_id: str | None = None
    shadow_runs_id: str | None = None
    # Phase 1 R-P1-4 — silver.document_passages writer. One row per
    # parsed section (chunk_kind='narrative'), ordinal-ordered. Layout-
    # aware chunking (page_first/last + bbox + chunk_kind='table' /
    # 'caption_figure') is Phase 2 ingestion-pipeline work.
    passages_written: int = 0

    # Doc-phase 57 / master-plan §3 Step 7c — §04p dual-write telemetry.
    # Populated when the §04p chain runs successfully alongside the v1.49
    # writes; None or {"ok": False} if §04p failed (existing v1.49 contract
    # unaffected either way). Keys: ok, counts, document_profile,
    # recommended_action, error.
    p04p_telemetry: dict | None = None


# =============================================================================
# Helpers
# =============================================================================
def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _s3_endpoint() -> str:
    return os.environ.get(
        "S3_ENDPOINT_URL",
        os.environ.get("MINIO_ENDPOINT", "http://minio:8333"),
    )


def _s3_credentials() -> tuple[str, str]:
    return (
        os.environ.get("AWS_ACCESS_KEY_ID")
        or os.environ.get("MINIO_ROOT_USER", "georag-admin"),
        os.environ.get("AWS_SECRET_ACCESS_KEY")
        or os.environ.get("MINIO_ROOT_PASSWORD", ""),
    )


async def _download_from_s3(minio_key: str) -> bytes:
    import aioboto3
    sess = aioboto3.Session(
        aws_access_key_id=_s3_credentials()[0],
        aws_secret_access_key=_s3_credentials()[1],
        region_name="us-east-1",
    )
    bucket = os.environ.get("MINIO_BUCKET_BRONZE", "bronze")
    async with sess.client("s3", endpoint_url=_s3_endpoint()) as s3:
        resp = await s3.get_object(Bucket=bucket, Key=minio_key)
        body = await resp["Body"].read()
        return body


def _sections_to_dict(sections) -> dict:
    """Mirror of v1.49 _build_sections_dict — keyed by section_number string."""
    result: dict = {}
    for s in sections:
        n = getattr(s, "section_number", None)
        title = getattr(s, "section_title", "") or ""
        text = getattr(s, "text", "") or ""
        key = str(n) if n is not None else (title.lower() or "section")
        if key in result:
            key = f"{key}_dup"
        result[key] = text
    return result


# =============================================================================
# Workflow + steps
# =============================================================================
ingest_pdf = hatchet.workflow(
    name="ingest_pdf",
    input_validator=IngestPdfInput,
    # 2026-05-23 — per-workspace singleton. The parse step loads
    # docling/PaddleOCR/RapidOCR models (~3-4 GB resident); running
    # multiple concurrent parses on the 36 GB host pushes total memory
    # over the edge and the OOM killer fires SIGKILL on the youngest
    # docling subprocess. Confirmed root cause of the
    # "A child process terminated abruptly" failures observed during
    # the 2026-05-23 TIFF smoke (see [[tiff-smoke-2026-05-23]]).
    #
    # GROUP_ROUND_ROBIN queues rather than cancels — a long real PDF
    # parse can't be interrupted by a smaller upload behind it.
    # Different workspaces still parse in parallel; only same-workspace
    # uploads serialise.
    #
    # IMPORTANT: every task in this workflow MUST set schedule_timeout
    # ≥ "2h" (see decorators below). Hatchet's default schedule_timeout
    # is 5 minutes — under this per-workspace serialisation any workflow
    # that waits in the queue longer than 5 min gets silently CANCELLED
    # (no error message, retry_count=0, worker_id=null). With 5+ PDFs
    # triggered in one burst that's roughly half the batch lost. The
    # 2026-05-24 Ontario Gold re-ingest hit this — runs 5–9 of 9 all
    # cancelled at exactly the 5-min mark. schedule_timeout="2h" gives
    # space for ~80 sequential parses before the tail starts expiring.
    concurrency=ConcurrencyExpression(
        expression="input.workspace_id",
        max_runs=1,
        limit_strategy=ConcurrencyLimitStrategy.GROUP_ROUND_ROBIN,
    ),
)


# ---- Step 1: preflight -------------------------------------------------------
@ingest_pdf.task(execution_timeout="60s", schedule_timeout="2h", retries=2)
async def preflight(input: IngestPdfInput, ctx: Context) -> PreflightOut:
    """Download from S3, compute sha256, validate magic bytes + size + encryption."""
    log.info("ingest_pdf.preflight start key=%s", input.minio_key)

    # CC-03 Item 8 — lifecycle guard. If the project is not active, skip
    # the whole workflow by returning a synthetic PreflightOut that marks
    # the run as invalid. We don't raise — Hatchet would retry a raise,
    # burning retries for something that won't change until the project
    # is reactivated. Returning early with valid=False causes parse() to
    # short-circuit and persist() to produce an empty report with a
    # descriptive warning; the operator can see the skip_reason in the
    # Hatchet run record.
    if input.project_id:
        _skip_reason: str | None = None
        try:
            _lc_pool = await asyncpg.create_pool(
                _dsn(), min_size=1, max_size=1, statement_cache_size=0
            )
            try:
                async with _lc_pool.acquire() as _lc_conn:
                    async with _lc_conn.transaction():
                        if input.workspace_id:
                            await bind_workspace_scope(
                                _lc_conn, workspace_id=str(input.workspace_id), site="hatchet.ingest_pdf"
                            )
                        _lc_row = await _lc_conn.fetchrow(
                            "SELECT lifecycle_state FROM silver.projects "
                            "WHERE project_id = $1::uuid",
                            str(input.project_id),
                        )
                        if _lc_row is not None:
                            _state = _lc_row["lifecycle_state"]
                            if _state != "active":
                                _skip_reason = f"project_not_active:{_state}"
            finally:
                await _lc_pool.close()
        except Exception as _lc_err:
            # Connection failure or table-not-found (e.g. first-run before
            # migration). Log and proceed — fail open is safer than silently
            # dropping every ingest on startup errors.
            log.warning(
                "ingest_pdf.preflight: lifecycle check failed (non-fatal): %s", _lc_err
            )

        if _skip_reason:
            log.info(
                "ingest_pdf.preflight: skipping workflow — project=%s reason=%s",
                input.project_id,
                _skip_reason,
            )
            return PreflightOut(
                sha256="",
                page_count=0,
                file_size=0,
                encrypted=False,
                valid=False,
                error=_skip_reason,
            )

    if input.project_id and input.workspace_id:
        await ingest_progress.mark_started(
            workspace_id=str(input.workspace_id),
            project_id=str(input.project_id),
            minio_key=input.minio_key,
            step="preflight",
            workflow_run_id=getattr(ctx, "workflow_run_id", None),
        )
    body = await _download_from_s3(input.minio_key)
    file_size = len(body)
    sha256 = hashlib.sha256(body).hexdigest()

    # Hard cap raised 2026-05-22 from 100MB to 2GB to match the upload
    # stack (OCTANE_MAX_REQUEST_SIZE / PHP_UPLOAD_MAX_FILESIZE / Laravel
    # validator). Below this is just a sanity check against runaway memory.
    if file_size > 2 * 1024 * 1024 * 1024:
        return PreflightOut(
            sha256=sha256, page_count=0, file_size=file_size,
            encrypted=False, valid=False,
            error=f"PDF exceeds 2 GB (got {file_size})",
        )
    if not body.startswith(b"%PDF-"):
        return PreflightOut(
            sha256=sha256, page_count=0, file_size=file_size,
            encrypted=False, valid=False,
            error="missing %PDF- magic bytes",
        )

    # Encryption detection: bare `/Encrypt` substring match (the old logic)
    # rejected NI 43-101 PDFs that merely had a "no copy" permission flag
    # but extracted fine (Madsen PFS was a casualty). Real test: can we
    # actually open + count pages?  If pikepdf can read it, downstream
    # fitz/pdfplumber can extract from it.
    encrypted_flag = b"/Encrypt" in body[:8192]

    def _count_pages() -> tuple[int, bool, str | None]:
        from io import BytesIO
        import pikepdf
        try:
            with pikepdf.open(BytesIO(body)) as pdf:
                return len(pdf.pages), False, None
        except pikepdf.PasswordError as e:
            return 0, True, f"password-protected: {e}"
        except Exception as e:
            return 0, encrypted_flag, f"pikepdf open failed: {e}"

    page_count, password_protected, open_error = await asyncio.to_thread(_count_pages)

    # Only reject when the PDF is genuinely password-protected (can't open
    # without a passphrase). Permission-flagged PDFs that pikepdf opens
    # successfully proceed to extraction.
    if password_protected:
        return PreflightOut(
            sha256=sha256, page_count=0, file_size=file_size,
            encrypted=True, valid=False,
            error=open_error or "PDF is password-protected",
        )

    return PreflightOut(
        sha256=sha256,
        page_count=page_count,
        file_size=file_size,
        encrypted=encrypted_flag,
        valid=True,
        error=None,
    )


# ---- Step 2: parse — single call to v1.49 parse_pdf_report ------------------
@ingest_pdf.task(execution_timeout="60m", schedule_timeout="2h", retries=1, parents=[preflight])
async def parse(input: IngestPdfInput, ctx: Context) -> ParseOut:
    """Call the canonical v1.49 ``parse_pdf_report`` end to end.

    The parser owns: fitz-first → docling/tesseract OCR routing → pdfplumber fallback → OCR (if
    scanned) → metadata extraction → section split → resource table extract.
    Returns a ReportParseResult; we serialise it into ParseOut.
    """
    if input.project_id and input.workspace_id:
        await ingest_progress.mark_started(
            workspace_id=str(input.workspace_id),
            project_id=str(input.project_id),
            minio_key=input.minio_key,
            step="parse",
        )
    pre = ctx.task_output(preflight)
    pre = pre.model_dump() if hasattr(pre, "model_dump") else dict(pre)

    if not pre.get("valid"):
        return ParseOut(
            sha256=pre.get("sha256", ""),
            parser_used="skipped",
            warnings=[{"code": "preflight_rejected", "message": pre.get("error", "")}],
        )

    # Reliability spec Fix 1d — heartbeat every 30s so the stale-run
    # detector knows we're alive on multi-minute parses. The async ctxmgr
    # cancels the ticker on exit (normal + exception path).
    async with ingest_progress.heartbeat_loop(
        workspace_id=str(input.workspace_id) if input.workspace_id else "",
        minio_key=input.minio_key,
    ):
        return await _parse_body(input, pre)


async def _parse_body(input: IngestPdfInput, pre: dict) -> ParseOut:
    """Inner body of parse() — wrapped so heartbeat_loop can manage the
    ticker around the entire blocking-subprocess section."""
    body = await _download_from_s3(input.minio_key)

    log.info("ingest_pdf.parse start key=%s", input.minio_key)
    # Run in subprocess (separate GIL) so heartbeats stay alive even on
    # heavy 500-page parses with per-page OCR. Fallback to thread on
    # subprocess failure (e.g. unpicklable internal state) since the
    # work still has to happen.
    #
    # Phase 5 (2026-05-22) — pre-fork memory guard. Blocks until the
    # worker has enough free RAM to safely run a parse alongside any
    # already-running ones. Raises MemoryError on timeout; Hatchet's
    # retries=1 will retry on a freer worker.
    # 2026-05-23 — defaults raised from (1500, 30) to (4500, 120).
    # The 1500 MB threshold was tuned in Phase 5 for the v1.49 fitz-only
    # parser; the 5/22 overhaul made docling+PaddleOCR+RapidOCR the
    # primary path and those each load ~3-4 GB of model weights. On the
    # 36 GB host with the rest of the platform (vLLM cache, Neo4j,
    # Postgres, Qdrant, Langfuse, dagster containers) eating ~32 GB
    # baseline, only ~4 GB is genuinely free. Starting a parse with
    # 1.5 GB free is a guaranteed OOM. The 120 s wait budget gives a
    # transient pressure spike room to clear before the workflow gives
    # up and lets Hatchet retry. See [[tiff-smoke-2026-05-23]] for the
    # root-cause analysis.
    try:
        _min_free_mb = int(os.environ.get("PARSE_MIN_FREE_RAM_MB", "4500"))
    except ValueError:
        _min_free_mb = 4500
    try:
        _max_wait_s = int(os.environ.get("PARSE_MEMORY_WAIT_MAX_S", "120"))
    except ValueError:
        _max_wait_s = 120
    await _wait_for_memory_headroom(
        min_free_mb=_min_free_mb, max_wait_s=_max_wait_s,
    )

    loop = asyncio.get_running_loop()
    pool = _get_parse_pool()
    try:
        result_dict = await loop.run_in_executor(
            pool, _run_parser_subprocess, body, pre.get("sha256", ""),
        )
    except BrokenProcessPool as exc:
        # 2026-05-23 — kill the in-process fallback. The original
        # fallback ran the parser on the default asyncio thread pool,
        # which:
        #   1. loaded docling/PaddleOCR/RapidOCR models in the SAME
        #      process that just got its subprocess OOM-killed — guaranteed
        #      to push memory over the edge again, often killing the
        #      whole worker (cf. [[tiff-smoke-2026-05-23]] root cause);
        #   2. blocked the Hatchet event loop on a multi-minute parse,
        #      starving heartbeats and getting the task re-queued by
        #      Hatchet's dead-worker detection.
        #
        # New behaviour: also rebuild the pool (the BrokenProcessPool
        # error means our cached pool is poisoned) and raise so Hatchet's
        # retries=1 backoff kicks in — by then memory pressure may have
        # eased. The per-workspace concurrency cap above means we won't
        # find ourselves in the same OOM situation on retry within the
        # same workspace.
        log.error(
            "ingest_pdf.parse: subprocess pool broken (%s) — likely OOM. "
            "Rebuilding pool; Hatchet will retry the step (retries=1). "
            "If retry also fails, raise PARSE_MIN_FREE_RAM_MB or reduce "
            "host memory pressure.", exc,
        )
        _reset_parse_pool()
        raise
    return ParseOut(**result_dict)


# ---- Step 3: persist ---------------------------------------------------------
# Mirrors v1.49 silver.reports INSERT (silver_reports.py:42-72) verbatim and
# also writes silver.shadow_runs + audit.audit_ledger.
INSERT_REPORT_SQL = """
INSERT INTO silver.reports (
    report_id, title, authors, company, filing_date, commodity,
    project_name, region, resource_estimate, sections_text,
    embedding_ids, parse_quality_pct, parser_used,
    is_scanned, source_file_sha256, project_id, workspace_id
)
VALUES (
    $1, $2, $3::text[], $4, $5::date, $6,
    $7, $8, $9::jsonb, $10::jsonb,
    ARRAY[]::text[], $11, $12,
    $13, $14, $16::uuid, $15::uuid
)
ON CONFLICT (report_id) DO UPDATE SET
    sections_text  = EXCLUDED.sections_text,
    parser_used    = EXCLUDED.parser_used,
    parse_quality_pct = EXCLUDED.parse_quality_pct,
    updated_at     = NOW()
"""

# Phase 1 R-P1-4 — write one passage per non-empty parsed section.
# UNIQUE (document_id, revision_number, text_hash) makes re-runs idempotent:
# the same parsed section yields the same text_hash; ON CONFLICT DO NOTHING
# ⇒ no duplicate passage on persist retry.
INSERT_PASSAGE_SQL = """
INSERT INTO silver.document_passages (
    document_id, workspace_id, revision_number,
    text, text_hash, ordinal, chunk_kind,
    page_first, page_last,
    ocr_confidence, ocr_method, ocr_status,
    created_at, updated_at
)
VALUES ($1, $2::uuid, 1, $3, $4, $5, 'narrative', $6, $7, $8, $9, 'accepted', NOW(), NOW())
ON CONFLICT (document_id, revision_number, text_hash) DO UPDATE SET
    page_first     = COALESCE(EXCLUDED.page_first,     silver.document_passages.page_first),
    page_last      = COALESCE(EXCLUDED.page_last,      silver.document_passages.page_last),
    -- Phase 3 (2026-05-22): preserve existing OCR provenance on retry,
    -- only fill in if currently NULL. Avoids overwriting a real captured
    -- confidence with NULL on a Hatchet retry of the same parse.
    ocr_confidence = COALESCE(silver.document_passages.ocr_confidence, EXCLUDED.ocr_confidence),
    ocr_method     = COALESCE(silver.document_passages.ocr_method,     EXCLUDED.ocr_method),
    -- Phase 6 (2026-05-22): on retry, do NOT reset ocr_status — if the
    -- quality agent already flagged it as pending_reocr, the retry should
    -- preserve that. The default 'accepted' only applies on first insert.
    updated_at     = NOW()
"""


@ingest_pdf.task(execution_timeout="15m", schedule_timeout="2h", retries=2, parents=[parse])
async def persist(input: IngestPdfInput, ctx: Context) -> IngestPdfFinalOut:
    """Write silver.reports + silver.shadow_runs + audit.audit_ledger."""
    if input.project_id and input.workspace_id:
        await ingest_progress.mark_started(
            workspace_id=str(input.workspace_id),
            project_id=str(input.project_id),
            minio_key=input.minio_key,
            step="persist",
        )
    # Reliability spec Fix 1d — keep last_heartbeat_at fresh while the
    # potentially-slow persist transaction runs.
    async with ingest_progress.heartbeat_loop(
        workspace_id=str(input.workspace_id) if input.workspace_id else "",
        minio_key=input.minio_key,
    ):
        return await _persist_body(input, ctx)


async def _persist_body(input: IngestPdfInput, ctx: Context) -> IngestPdfFinalOut:
    """Inner body of persist() — wrapped so heartbeat_loop can manage the
    ticker around the slow §04p dual-write + Postgres transaction."""
    pre = ctx.task_output(preflight)
    pre = pre.model_dump() if hasattr(pre, "model_dump") else dict(pre)
    parsed = ctx.task_output(parse)
    parsed = parsed.model_dump() if hasattr(parsed, "model_dump") else dict(parsed)

    t_start = time.monotonic()
    report_id = str(uuid.uuid4())

    # Build resource_estimate payload to match v1.49 exactly.
    resource_estimate: dict = {}
    if parsed.get("resource_tables"):
        resource_estimate["pdfplumber_v1"] = {
            "tables": parsed["resource_tables"],
            "source": "pdfplumber_v1",
        }

    # Phase 1 (2026-05-22): figure manifest consumption.
    # The parse task uploaded each docling figure to
    # figures/_pending/{sha}/figure_{idx:04d}_page_{n}.png and returned
    # a manifest in ParseOut.figures. Here we rename each PNG to its
    # final figures/{report_id}/... key (S3 copy+delete), and create
    # one ReportSection per figure so the caption text is chunked +
    # embedded alongside narrative sections (caption hits in chat
    # surface the figure citation).
    #
    # This replaces the previous module-scope cache + cross-process
    # _extract_docling_figures call, which silently returned [] once
    # parse moved into a subprocess (cache lived in parse-process
    # memory, persist read it in parent process where it was always
    # empty → all figures were lost regardless of upload success).
    figure_manifest_final: list[dict] = []
    pending_manifest = parsed.get("figures") or []
    if pending_manifest:
        try:
            import boto3
            from botocore.config import Config as BotoConfig

            s3_endpoint = _s3_endpoint()
            aws_key, aws_secret = _s3_credentials()
            s3 = boto3.client(
                "s3",
                endpoint_url=s3_endpoint,
                aws_access_key_id=aws_key,
                aws_secret_access_key=aws_secret,
                region_name="us-east-1",
                config=BotoConfig(signature_version="s3v4"),
            )

            figure_sections_out: list[dict] = []
            for entry in pending_manifest:
                idx = entry.get("idx")
                page_no = entry.get("page")
                caption = (entry.get("caption") or "").strip()
                pending_key = entry.get("pending_key")
                bucket = entry.get("bucket") or os.environ.get(
                    "MINIO_BUCKET_BRONZE", "bronze"
                )
                img_sha = entry.get("sha256")

                final_key = None
                if pending_key:
                    final_key = (
                        f"figures/{report_id}/"
                        f"figure_{int(idx):04d}_page_{page_no}.png"
                    )
                    try:
                        s3.copy_object(
                            Bucket=bucket,
                            Key=final_key,
                            CopySource={"Bucket": bucket, "Key": pending_key},
                            MetadataDirective="REPLACE",
                            ContentType="image/png",
                            Metadata={
                                "report_id": str(report_id),
                                "project_id": (
                                    str(input.project_id) if input.project_id else ""
                                ),
                                "page": str(page_no),
                                "sha256": str(img_sha or ""),
                            },
                        )
                        try:
                            s3.delete_object(Bucket=bucket, Key=pending_key)
                        except Exception as del_exc:  # noqa: BLE001
                            log.warning(
                                "ingest_pdf.persist: pending figure delete "
                                "failed (key=%s): %s",
                                pending_key, del_exc,
                            )
                    except Exception as copy_exc:  # noqa: BLE001
                        log.warning(
                            "ingest_pdf.persist: figure copy failed "
                            "(pending=%s → final=%s): %s",
                            pending_key, final_key, copy_exc,
                        )
                        final_key = None

                section_lines = [f"Figure on page {page_no}."]
                if caption:
                    section_lines.append(f"Caption: {caption}")
                if final_key:
                    section_lines.append(f"Image: s3://{bucket}/{final_key}")
                figure_sections_out.append({
                    "section_number": None,
                    "section_title": f"Figure (page {page_no}, #{int(idx) + 1})",
                    "text": "\n".join(section_lines),
                    "page_first": page_no,
                    "page_last": page_no,
                })

                figure_manifest_final.append({
                    "idx": idx,
                    "page": page_no,
                    "bbox": entry.get("bbox"),
                    "caption": caption,
                    "minio_key": final_key,
                    "sha256": img_sha,
                })

            if figure_sections_out:
                log.info(
                    "ingest_pdf.persist: registered %d figure section(s) for "
                    "report=%s (uploaded=%d)",
                    len(figure_sections_out),
                    report_id,
                    sum(1 for m in figure_manifest_final if m.get("minio_key")),
                )
                parsed.setdefault("sections", []).extend(figure_sections_out)
        except ImportError:
            log.warning("ingest_pdf.persist: boto3 unavailable, skipping figure rename")
        except Exception as fig_err:  # noqa: BLE001
            log.warning("ingest_pdf.persist: figure manifest consumption failed: %s", fig_err)

    # Surface the final figure manifest in resource_estimate so the UI /
    # future query layer can render figures inline with citations.
    if figure_manifest_final:
        resource_estimate["figures"] = {
            "items": figure_manifest_final,
            "source": "docling_v1",
        }

    # Build sections_text dict (v1.49 _build_sections_dict shape).
    sections_text: dict = {}
    for s in parsed.get("sections", []) or []:
        n = s.get("section_number")
        title = (s.get("section_title") or "")
        key = str(n) if n is not None else (title.lower() or "section")
        if key in sections_text:
            key = f"{key}_dup"
        sections_text[key] = s.get("text") or ""

    title = parsed.get("title") or "(untitled)"  # silver.reports.title is NOT NULL

    # filing_date arrives as an ISO 8601 string from the parser; asyncpg
    # binds Python `date` to a Postgres `date` column.
    filing_date_raw = parsed.get("filing_date")
    filing_date_obj = None
    if filing_date_raw:
        from datetime import date as _date
        try:
            filing_date_obj = _date.fromisoformat(filing_date_raw[:10])
        except Exception:
            filing_date_obj = None

    # Hatchet step-boundary deserialization can drop UUID fields if the
    # input model has been re-validated upstream. Fall back to the bronze-
    # manifest workspace_id when input.workspace_id arrives None.
    workspace_id_str: str
    if input.workspace_id:
        workspace_id_str = str(input.workspace_id)
    else:
        # Recover from the bronze manifest or default workspace.
        # Audit item B4 — centralised legacy default + metric so Phase-2
        # cutover (raise instead of fallback) sees this as a single search.
        workspace_id_str = LEGACY_DEFAULT_TENANT_UUID
        try:
            WORKSPACE_RESOLUTION_FAILURES.labels(
                site="ingest_pdf.persist"
            ).inc()
        except Exception:
            pass
        log.warning(
            "ingest_pdf.persist: input.workspace_id was null; "
            "falling back to default workspace. minio_key=%s",
            input.minio_key,
        )

    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2, statement_cache_size=0)
    try:
        async with pool.acquire() as conn:
            # --- silver.reports + silver.document_passages (atomic) ---
            # 2026-05-22: merged the previously-separate report and passage
            # transactions into ONE transaction. Before this, big PDFs that
            # exceeded the persist task's 2-minute execution_timeout during
            # the slow §04p dual-write left silver.reports rows with ZERO
            # passages — the user's "437 KB sections_text, 0 chunks" bug.
            # With a single transaction, either the whole report + all its
            # passages land together, or neither does and Hatchet retries.
            passages_written = 0
            async with conn.transaction():
                await bind_workspace_scope(
                    conn, workspace_id=workspace_id_str, site="hatchet.ingest_pdf"
                )
                await conn.execute(
                    INSERT_REPORT_SQL,
                    report_id,
                    title,
                    parsed.get("authors") or [],
                    parsed.get("company"),
                    filing_date_obj,
                    parsed.get("commodity"),
                    parsed.get("project_name"),
                    parsed.get("region"),
                    json.dumps(resource_estimate),
                    json.dumps(sections_text),
                    float(parsed.get("parse_quality_pct", 0.0) or 0.0),
                    (parsed.get("parser_used") or "unknown")[:30],
                    bool(parsed.get("is_scanned", False)),
                    pre.get("sha256"),
                    workspace_id_str,
                    str(input.project_id) if input.project_id else None,
                )
                for ordinal, section in enumerate(parsed.get("sections") or []):
                    text = (section.get("text") or "").strip()
                    if not text:
                        continue
                    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    # Phase 3 (2026-05-22) — OCR provenance. Default to
                    # None when the parser didn't supply values (e.g. older
                    # parsers or sections built outside _assign_ocr_metadata).
                    ocr_conf_raw = section.get("ocr_confidence")
                    ocr_method = section.get("ocr_method")
                    ocr_conf = float(ocr_conf_raw) if ocr_conf_raw is not None else None
                    status = await conn.execute(
                        INSERT_PASSAGE_SQL,
                        report_id,
                        workspace_id_str,
                        text,
                        text_hash,
                        ordinal,
                        section.get("page_first"),
                        section.get("page_last"),
                        ocr_conf,
                        ocr_method,
                    )
                    if status.endswith(" 1"):
                        passages_written += 1

            # silver.shadow_runs was dropped in Phase 4 Step 6 (sunset of the
            # v1.49 shadow-diff harness). The persist step previously
            # INSERTed a row here; that block is removed. `final.shadow_runs_id`
            # stays as the model default (None) for backward-compat with any
            # downstream consumer reading the field.

            # §04p dual-write moved to its own task (see `p04p_dual_write`
            # below). The previous inline block ran paddleocr + layout
            # models inside persist, starving Hatchet's asyncio loop and
            # causing heartbeat timeouts → cascading retry storms. The
            # new task runs after persist and isolates that cost.
            p04p_telemetry: dict | None = None

            # Wire the §04p telemetry into the final return.
            # Doc-phase 66 fix: this was previously a separate
            # assignment AFTER the IngestPdfFinalOut construction;
            # but the §04p block runs BEFORE final is built, so the
            # assignment hit UnboundLocalError. Now passed as kwarg.
            persist_ms = int((time.monotonic() - t_start) * 1000)
            final = IngestPdfFinalOut(
                sha256=pre.get("sha256", ""),
                parser_used=parsed.get("parser_used") or "unknown",
                parse_quality_pct=float(parsed.get("parse_quality_pct", 0.0) or 0.0),
                page_count=int(pre.get("page_count", 0) or 0),
                title=parsed.get("title"),
                authors=list(parsed.get("authors") or []),
                company=parsed.get("company"),
                filing_date=parsed.get("filing_date"),
                commodity=parsed.get("commodity"),
                project_name=parsed.get("project_name"),
                region=parsed.get("region"),
                sections_count=len(parsed.get("sections") or []),
                resource_tables_count=len(parsed.get("resource_tables") or []),
                is_scanned=bool(parsed.get("is_scanned", False)),
                warnings_count=len(parsed.get("warnings") or []),
                parse_duration_ms=int(parsed.get("parse_duration_ms", 0) or 0),
                persist_duration_ms=persist_ms,
                report_id=report_id,
                passages_written=passages_written,
                p04p_telemetry=p04p_telemetry,
            )

            # --- audit.audit_ledger ---
            # Two action_types per run, matching Phase 1 §10.3:
            #   - ingest_pdf.parse.complete   (the parse stage)
            #   - silver.reports.write        (the row insert)
            # The diff classifier's CRITICAL_ACTION_TYPES check requires
            # both to be present on each side; emitting them here closes
            # R-P1-1 from the Phase 1 handoff.
            try:
                await emit_audit(
                    conn,
                    action_type="ingest_pdf.parse.complete",
                    workspace_id=input.workspace_id,
                    actor_id=input.actor_id,
                    actor_kind="workflow",
                    target_schema="silver",
                    target_table="reports",
                    target_id=report_id,
                    payload={
                        "minio_key": input.minio_key,
                        "sha256": final.sha256,
                        "parser_used": final.parser_used,
                        "parse_quality_pct": final.parse_quality_pct,
                        "page_count": final.page_count,
                        "sections_count": final.sections_count,
                        "resource_tables_count": final.resource_tables_count,
                        "is_scanned": final.is_scanned,
                        "warnings_count": final.warnings_count,
                        "parse_duration_ms": final.parse_duration_ms,
                        "persist_duration_ms": final.persist_duration_ms,
                        "report_id": report_id,
                        "shadow_runs_id": None,
                        "passages_written": final.passages_written,
                    },
                    trace_id=ctx.workflow_run_id,
                )
                await emit_audit(
                    conn,
                    action_type="silver.reports.write",
                    workspace_id=input.workspace_id,
                    actor_id=input.actor_id,
                    actor_kind="workflow",
                    target_schema="silver",
                    target_table="reports",
                    target_id=report_id,
                    payload={
                        "minio_key": input.minio_key,
                        "sha256": final.sha256,
                        "report_id": report_id,
                        "title": final.title,
                        "company": final.company,
                        "filing_date": final.filing_date,
                        "side": "hatchet",
                    },
                    trace_id=ctx.workflow_run_id,
                )
            except Exception as e:
                log.warning("audit emit failed: %s", e)
    finally:
        await pool.close()

    log.info(
        "ingest_pdf.persist done report_id=%s parser=%s sections=%d tables=%d total=%dms",
        report_id, final.parser_used,
        final.sections_count, final.resource_tables_count,
        final.parse_duration_ms + final.persist_duration_ms,
    )

    # Trigger embedding for this project so chunks land in qdrant
    # immediately, instead of waiting for the 05:45 UTC daily cron.
    # Fire-and-forget — embedding can take minutes for big PDFs; we don't
    # block persist on it. The workflow's own retries/idempotency handle
    # transient failures.
    #
    # Passing the typed input model (not a dict) so pydantic validation
    # succeeds on the worker side. Earlier dict-form raised a
    # PydanticSerializationUnexpectedValue warning and the wrapped run
    # silently no-op'd.
    if input.project_id:
        try:
            from app.hatchet_workflows.embed_pending_passages import (
                EmbedPendingPassagesInput,
                embed_pending_passages_wf,
            )
            embed_input = EmbedPendingPassagesInput(
                workspace_id=str(input.workspace_id) if input.workspace_id else workspace_id_str,
                project_id=str(input.project_id),
                batch_size=32,
            )
            await embed_pending_passages_wf.aio_run_no_wait(embed_input)
            log.info("ingest_pdf.persist: embed_pending_passages dispatched for project=%s", input.project_id)
        except Exception as embed_err:
            log.warning(
                "ingest_pdf.persist: failed to dispatch embed workflow: %s — "
                "chunks will be picked up by daily cron at 05:45 UTC",
                embed_err,
            )

    # Phase 6 (2026-05-22) — OCR Quality Agent dispatch. Gated on
    # OCR_QUALITY_AGENT_ENABLED env (default false). Fire-and-forget;
    # the agent reads the just-committed passages and flags / re-OCRs
    # low-confidence ones without blocking persist's return.
    if input.project_id and os.environ.get(
        "OCR_QUALITY_AGENT_ENABLED", "false",
    ).lower() == "true":
        try:
            from app.hatchet_workflows.ocr_quality_check import (
                OcrQualityCheckInput,
                ocr_quality_check_wf,
            )
            qc_input = OcrQualityCheckInput(
                workspace_id=input.workspace_id,
                project_id=input.project_id,
                report_id=report_id,
                actor_id=input.actor_id,
            )
            await ocr_quality_check_wf.aio_run_no_wait(qc_input)
            log.info(
                "ingest_pdf.persist: ocr_quality_check dispatched for "
                "report=%s",
                report_id,
            )
        except Exception as qc_err:
            log.warning(
                "ingest_pdf.persist: failed to dispatch ocr_quality_check: "
                "%s — passages stay ocr_status='accepted'",
                qc_err,
            )

    return final


# ---- Step 4: embed-verify ----------------------------------------------------
# Safety net for the BattleNorth-style race where the inline embed dispatch
# from persist gets lost between Hatchet retries. Quickly polls the project's
# unembedded passage count and re-dispatches the embed workflow if anything
# is still pending. This is belt-and-suspenders alongside the every-10-min
# cron — gives users near-realtime "I just uploaded this and chat sees it".
@ingest_pdf.task(execution_timeout="60s", schedule_timeout="2h", retries=1, parents=[persist])
async def embed_verify(input: IngestPdfInput, ctx: Context) -> dict:
    """Phase 8 (2026-05-22) — single check + dispatch, no polling loop.

    Previously this task polled the unembedded passage count every 15 s
    for up to 90 s before redispatching. Under heavy concurrent load
    (Phase 5 enabled 4× parallel parses → embed queue depth can exceed
    90 s), the polling caused noisy retries on every parse and burned
    PG round-trips for no benefit — the inline embed dispatched from
    `persist` is the primary path; this task is just a backstop.

    The simplification:
      1. One SELECT to count unembedded passages for this project.
      2. If zero, exit (inline already finished — rare but possible).
      3. Otherwise dispatch `embed_pending_passages_wf` and exit.
         Safe because that workflow is idempotent: it SELECTs
         `embedding_id IS NULL` and qdrant upserts on `point_id`.
         Concurrent runs race on the same rows; the loser does a few
         duplicate encodes but produces identical points.

    Worst-case wasted work is ~20-40 s of duplicate encodes per batch,
    which is less than the polling overhead it replaces. The 10-min
    `*/10 * * * *` cron remains the durability backstop.
    """
    if not input.project_id:
        return {"ok": True, "skipped": True, "reason": "no project_id"}

    if input.project_id and input.workspace_id:
        await ingest_progress.mark_started(
            workspace_id=str(input.workspace_id),
            project_id=str(input.project_id),
            minio_key=input.minio_key,
            step="embed_verify",
        )

    pool = await asyncpg.create_pool(
        _dsn(), min_size=1, max_size=1, statement_cache_size=0,
    )
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT count(*) AS unembedded
                FROM silver.document_passages p
                JOIN silver.reports r ON r.report_id = p.document_id
                WHERE r.project_id = $1::uuid
                  AND p.embedding_id IS NULL
                """,
                str(input.project_id),
            )
            unembedded = int(row["unembedded"] or 0)

        if unembedded == 0:
            if input.workspace_id:
                await ingest_progress.mark_completed(
                    workspace_id=str(input.workspace_id),
                    minio_key=input.minio_key,
                )
                # Reliability spec — broadcast terminal completion event so
                # the IngestionRuns UI can flip immediately instead of
                # waiting for its next poll. Best-effort.
                try:
                    from app.services.laravel_bridge import post_ingestion_progress
                    run_id = await ingest_progress.lookup_active_run_id(
                        workspace_id=str(input.workspace_id),
                        minio_key=input.minio_key,
                    )
                    # lookup_active_run_id returns None for terminal rows,
                    # so re-query by (workspace, key) for the just-completed
                    # row if needed.
                    if run_id is None:
                        pool2 = await ingest_progress.get_pool()
                        async with pool2.acquire() as _c:
                            _r = await _c.fetchrow(
                                "SELECT run_id::text AS run_id FROM "
                                "silver.ingest_progress WHERE workspace_id = "
                                "$1::uuid AND minio_key = $2 "
                                "ORDER BY attempt_number DESC, started_at DESC "
                                "LIMIT 1",
                                str(input.workspace_id), input.minio_key,
                            )
                        run_id = _r["run_id"] if _r else None
                    if run_id and input.project_id:
                        await post_ingestion_progress(
                            workspace_id=str(input.workspace_id),
                            project_id=str(input.project_id),
                            run_id=run_id,
                            stage="embedding",
                            status="completed",
                            message="Ingestion complete; all chunks embedded.",
                        )
                except Exception as exc:
                    log.warning(
                        "embed_verify: completion broadcast failed key=%s err=%s",
                        input.minio_key, exc,
                    )
            return {"ok": True, "unembedded_final": 0}

        # Unembedded passages remain — advance the progress to the final
        # 'embedding' step so the UI shows the bar is in the home stretch.
        if input.workspace_id:
            await ingest_progress.mark_started(
                workspace_id=str(input.workspace_id),
                project_id=str(input.project_id),
                minio_key=input.minio_key,
                step="embedding",
            )

        # Unembedded passages remain — dispatch embed_pending_passages.
        try:
            from app.hatchet_workflows.embed_pending_passages import (
                EmbedPendingPassagesInput,
                embed_pending_passages_wf,
            )
            if input.workspace_id:
                wsid = str(input.workspace_id)
            else:
                wsid = LEGACY_DEFAULT_TENANT_UUID
                try:
                    WORKSPACE_RESOLUTION_FAILURES.labels(
                        site="ingest_pdf.dispatch_embed"
                    ).inc()
                except Exception:
                    pass
            embed_input = EmbedPendingPassagesInput(
                workspace_id=wsid,
                project_id=str(input.project_id),
                batch_size=64,
            )
            await embed_pending_passages_wf.aio_run_no_wait(embed_input)
            log.info(
                "ingest_pdf.embed_verify: dispatched embed for project=%s "
                "(unembedded_observed=%d)",
                input.project_id, unembedded,
            )
            return {
                "ok": True,
                "redispatched": True,
                "unembedded_observed": unembedded,
            }
        except Exception as exc:
            log.warning(
                "ingest_pdf.embed_verify: dispatch failed: %s — "
                "10-min cron will pick up", exc,
            )
            return {
                "ok": False,
                "error": str(exc),
                "unembedded_observed": unembedded,
            }
    finally:
        await pool.close()


# ---- Step 5: §04p dual-write -------------------------------------------------
# Runs the §04p stack (unstructured + paddleocr + layout regions) AFTER the
# atomic v1.49 persist completes. Isolated as its own Hatchet task so its
# heavy synchronous work cannot starve the persist task's heartbeat. Gated
# by P04P_DUAL_WRITE_ENABLED so it can be flipped on without touching code.
@ingest_pdf.task(execution_timeout="45m", schedule_timeout="2h", retries=1, parents=[persist])
async def p04p_dual_write(input: IngestPdfInput, ctx: Context) -> dict:
    """Populate the §04p silver tables from the bronze PDF.

    Returns telemetry as a dict ({"ok": bool, "counts": {...}, ...}) so the
    workflow's run record carries the §04p outcome alongside the v1.49
    persist result. Errors are caught and downgraded to telemetry so a
    failed §04p doesn't fail the whole ingest_pdf workflow.
    """
    if os.environ.get("P04P_DUAL_WRITE_ENABLED", "false").lower() != "true":
        log.debug("ingest_pdf.p04p_dual_write: disabled via P04P_DUAL_WRITE_ENABLED")
        return {"ok": False, "skipped": True, "reason": "disabled"}

    persist_out = ctx.task_output(persist)
    persist_dict = persist_out.model_dump() if hasattr(persist_out, "model_dump") else dict(persist_out)
    report_id = persist_dict.get("report_id")
    if not report_id:
        return {"ok": False, "error": "persist returned no report_id"}

    try:
        from app.ocr._ingest_helper import run_p04p_for_ingest
        # 2026-05-22: try the local cache first (populated by parse
        # subprocess); fall back to S3 re-download if not present.
        # Saves ~10-30s per PDF on the common path.
        import os as _os
        sha = persist_dict.get("sha256") or ""
        cached_path = _cached_pdf_path(sha) if sha else ""
        body: bytes | None = None
        if cached_path and _os.path.exists(cached_path):
            try:
                with open(cached_path, "rb") as _f:
                    body = _f.read()
                log.info(
                    "p04p_dual_write: served PDF body from cache (%s)", sha[:12],
                )
            except Exception as _cache_exc:
                log.debug("p04p body cache read failed: %s", _cache_exc)
                body = None
        if body is None:
            body = await _download_from_s3(input.minio_key)
        telemetry = await run_p04p_for_ingest(
            workspace_id=str(input.workspace_id),
            report_id=report_id,
            pdf_body=body,
            bronze_s3_key=input.minio_key,
        )
        try:
            from app.metrics import P04P_DUAL_WRITE_FAILURES, P04P_DUAL_WRITE_SUCCESS
            if telemetry.get("ok"):
                P04P_DUAL_WRITE_SUCCESS.inc()
            else:
                err = (telemetry.get("error") or "").lower()
                if "preflight" in err or "magic" in err:
                    kind = "preflight_invalid"
                elif "persist" in err:
                    kind = "persist_failed"
                elif err:
                    kind = "other"
                else:
                    kind = "exception"
                P04P_DUAL_WRITE_FAILURES.labels(error_kind=kind).inc()
        except Exception:
            pass

        if telemetry.get("ok"):
            log.info(
                "p04p_dual_write ok report=%s profile=%s counts=%s",
                report_id, telemetry.get("document_profile"), telemetry.get("counts"),
            )
        else:
            log.warning(
                "p04p_dual_write skipped report=%s err=%s",
                report_id, telemetry.get("error"),
            )
        return telemetry
    except Exception as exc:
        log.warning("p04p_dual_write threw report=%s err=%s", report_id, exc)
        try:
            from app.metrics import P04P_DUAL_WRITE_FAILURES
            P04P_DUAL_WRITE_FAILURES.labels(error_kind="exception").inc()
        except Exception:
            pass
        return {"ok": False, "error": f"exception: {exc}"}
    finally:
        # Clean up the cached PDF body now that both parse and §04p
        # have run. Leaving it on /tmp wastes space on long-running
        # workers. Best-effort delete — re-runs will recreate it.
        try:
            import os as _os
            sha = persist_dict.get("sha256") or ""
            if sha:
                cached = _cached_pdf_path(sha)
                if _os.path.exists(cached):
                    _os.unlink(cached)
        except Exception:
            pass


# =============================================================================
# Workflow-level failure hook — reliability spec Fix 1c.
# =============================================================================
# Runs whenever the workflow reaches a failed terminal state (task retries
# exhausted, worker crash with clean signal, explicit cancellation). Its job:
#
#   1. Resolve the run_id from (workspace_id, minio_key) — preflight may not
#      have written it yet if the failure happened during input validation.
#   2. Conditional-update silver.ingest_progress.status to 'failed' (or
#      'cancelled' if the task event explicitly says so).
#   3. POST the terminal event to Laravel for Reverb broadcast.
#   4. Do NOT call mv_refresh_silver.
#   5. Do NOT bump data_version in Redis.
#
# The conditional update ensures we don't overwrite a state that mark_failed
# inside the task body already wrote, and we don't double-fire broadcasts if
# Hatchet retries the on_failure task itself (retries=2).
@ingest_pdf.on_failure_task(
    name="on_failure",
    execution_timeout="30s",
    schedule_timeout="30m",
    retries=2,
)
async def on_failure(input: IngestPdfInput, ctx: Context) -> dict:
    """Workflow-level failure hook.

    Fires from every failure path that can leave the run in 'started':
      - All retries exhausted on any task
      - Worker crash with a clean SIGTERM (SIGKILL is caught by stale_run_sweep)
      - Explicit workflow cancellation (concurrency-queue expiry,
        manual cancel via the Hatchet UI)
    """
    from app.hatchet_workflows import _progress as ingest_progress
    from app.services.laravel_bridge import post_ingestion_progress

    workspace_id = str(input.workspace_id)
    project_id = str(input.project_id) if input.project_id else None
    minio_key = input.minio_key

    # Resolve the active run for this file. start_run is called by the
    # preflight task, so a run_id should exist unless we failed in input
    # validation before preflight ever ran. In that edge case there's
    # nothing to update.
    run_id = await ingest_progress.lookup_active_run_id(
        workspace_id=workspace_id, minio_key=minio_key,
    )
    if run_id is None:
        log.warning(
            "ingest_pdf.on_failure: no active run found for (ws=%s, key=%s) — "
            "skipping terminal update", workspace_id, minio_key,
        )
        return {"updated": False, "reason": "no_active_run"}

    # Fetch current_stage so the IngestionRuns UI can show "failed at parse"
    # instead of just "failed". get_run is a single SELECT against the pool.
    row = await ingest_progress.get_run(run_id=run_id)
    current_stage = (row or {}).get("current_stage") or "unknown"

    transitioned = await ingest_progress.mark_failed_by_run(
        run_id=run_id,
        stage=current_stage,
        error="ingest_pdf workflow failure hook fired",
    )

    if transitioned and project_id:
        try:
            await post_ingestion_progress(
                workspace_id=workspace_id,
                project_id=project_id,
                run_id=run_id,
                stage=current_stage,
                status="failed",
                message="Workflow exhausted retries or was cancelled.",
            )
        except Exception as exc:
            log.warning("ingest_pdf.on_failure: broadcast failed run=%s: %s", run_id, exc)

    return {
        "updated": transitioned,
        "run_id": run_id,
        "current_stage": current_stage,
    }


__all__ = ["ingest_pdf", "IngestPdfInput", "ParseOut", "IngestPdfFinalOut"]
