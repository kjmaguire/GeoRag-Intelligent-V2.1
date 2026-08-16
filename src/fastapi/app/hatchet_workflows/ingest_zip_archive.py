"""Hatchet workflow: extract a ZIP archive and fan out to per-file ingesters.

Handles the common field-data ZIP use-case: a geologist drops a 5 GB ZIP
containing hundreds of small files (TIF, LAS, LOG, XLSX, PDF ≤10 MB each)
into the upload UI. This workflow:

  1. Downloads the ZIP from SeaweedFS / MinIO to a temp directory.
  2. Extracts every entry with Python's ``zipfile`` module.
  3. Routes each extracted file by extension:
       .las / .LAS  →  las_ingester.ingest_las_file
       .log         →  cameco_log_ingester (parse header + upsert collar)
       .csv         →  csv_collar_ingester.ingest_csv_collar_file
       .tif / .tiff →  re-uploads to bronze tiff/ prefix + triggers tiff_normalize
       .xlsx / .xls →  xlsx_ingester.ingest_xlsx_file
       .pdf         →  re-uploads to bronze reports/ prefix + triggers ingest_pdf
  4. Logs progress every 10 files.
  5. Returns a summary dict with per-extension counts and error tally.

Individual file errors are caught, logged, and skipped — a corrupt LAS
file should not abort the 600 other files in the same ZIP.

Execution timeout is 4 h to accommodate large archives on slow storage.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
from georag_object_storage import Bucket, ObjectStorage, get_storage_client
from hatchet_sdk import Context
from pydantic import BaseModel, Field, field_validator

from app.db import bind_workspace_scope
from app.hatchet_workflows import hatchet
from app.hatchet_workflows.ingest_pdf import IngestPdfInput, ingest_pdf
from app.hatchet_workflows.tiff_normalize import TiffNormalizeInput, tiff_normalize

# Ingester imports are deferred to _ingest_one() to avoid pulling optional
# heavy deps (lasio, openpyxl) at module load time — the ingestion worker
# image may not have all of them installed, and we don't want an ImportError
# to prevent the worker from registering the other workflows.

log = logging.getLogger("georag.hatchet.ingest_zip_archive")


class IngestZipArchiveInput(BaseModel):
    """Payload handed to us by Laravel's UploadController.

    UUID validation note (2026-06-02 audit pass 5+): workspace_id /
    project_id / run_id stay typed as ``str`` (not ``UUID``) for
    downstream-string-comparison ergonomics, but a Pydantic validator
    rejects non-UUID input at the trigger boundary. The trigger
    router uses parameter-bound ``set_config('app.workspace_id', $1, true)``
    instead of f-string SET LOCAL — the validator is defence-in-depth
    against the SQL-injection shape that an f-string would have
    exposed if Laravel ever forwarded malformed input.
    """

    minio_key: str = Field(..., description="SeaweedFS/MinIO key of the uploaded ZIP.")
    workspace_id: str = Field(..., description="UUID of the owning workspace (RLS scope).")
    project_id: str = Field(..., description="UUID of the owning project.")
    run_id: str = Field(..., description="Caller-supplied correlation ID (uuid4 string).")

    @field_validator("workspace_id", "project_id", "run_id")
    @classmethod
    def _must_be_uuid(cls, v: str) -> str:
        import re
        if not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            v,
            re.IGNORECASE,
        ):
            raise ValueError(
                "IngestZipArchiveInput: workspace_id / project_id / run_id "
                "must be UUIDs (lowercase canonical form). The field is "
                "typed as str for downstream string-comparison ergonomics "
                "but the shape is still validated."
            )
        return v



def _build_dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------

ingest_zip_archive = hatchet.workflow(
    name="ingest_zip_archive",
    input_validator=IngestZipArchiveInput,
)


@ingest_zip_archive.task(execution_timeout="4h", retries=0)
async def run_zip_ingest(
    input: IngestZipArchiveInput, ctx: Context
) -> dict[str, Any]:
    """Download, extract, and fan-out every file in the ZIP archive.

    Observability — 2026-06-03 audit item C
    ----------------------------------------
    Previously this workflow had retries=0 + no on_failure_task + no
    progress surface. A mid-extraction crash returned a 201 to the
    user and then silently vanished from operator view (same shape as
    [[cameco-recovery-2026-06-02]]). Now wraps the body in
    ``_archive_progress.archive_lifecycle`` which writes a parent row
    in ``silver.archive_ingest_runs`` at start + closes it on
    completion (or on exception via the context manager). The
    on_failure_task hook (defined at the bottom of this file) is the
    second backstop for cancellation / worker crash paths the body
    never reaches.
    """
    from app.hatchet_workflows import _archive_progress  # noqa: PLC0415

    log.info(
        "ingest_zip_archive.start run_id=%s ws=%s project=%s key=%s",
        input.run_id,
        input.workspace_id,
        input.project_id,
        input.minio_key,
    )

    store = get_storage_client()

    async with _archive_progress.archive_lifecycle(
        workspace_id=input.workspace_id,
        project_id=input.project_id,
        minio_key=input.minio_key,
        run_id=input.run_id,
        triggered_by="upload",
        workflow_run_id=getattr(ctx, "workflow_run_id", None),
    ) as archive_run_id:
        # ── 1. Download ZIP to a temp directory ──────────────────────────────
        with tempfile.TemporaryDirectory(prefix="georag_zip_") as tmpdir:
            zip_path = Path(tmpdir) / "archive.zip"

            log.info("ingest_zip_archive: downloading %s", input.minio_key)
            if archive_run_id:
                await _archive_progress.mark_extracting(archive_run_id=archive_run_id)
            # Hard rule 2 — boto3 is sync; keep it off the asyncio event loop.
            await asyncio.to_thread(store.get_file, Bucket.BRONZE, input.minio_key, str(zip_path))

            # ── 2. Extract all entries ────────────────────────────────────────
            extract_dir = Path(tmpdir) / "extracted"
            extract_dir.mkdir()

            # Audit 2026-06-28: safe extraction. A bare zf.extractall() is
            # vulnerable to (a) zip-bombs (unbounded decompressed size / entry
            # count exhausts disk) and (b) zip-slip path traversal (an entry
            # named '../../etc/x' escapes extract_dir). Guard both: cap entry
            # count + total declared uncompressed size, and verify every
            # resolved destination stays inside extract_dir before writing.
            _MAX_ENTRIES = 50_000
            _MAX_TOTAL_UNCOMPRESSED = 5 * 1024 ** 3  # 5 GiB
            extract_root = extract_dir.resolve()
            with zipfile.ZipFile(zip_path, "r") as zf:
                infos = zf.infolist()
                if len(infos) > _MAX_ENTRIES:
                    raise ValueError(
                        f"ingest_zip_archive: {len(infos)} entries exceeds "
                        f"{_MAX_ENTRIES} (zip-bomb guard); refusing."
                    )
                total_uncompressed = sum(i.file_size for i in infos)
                if total_uncompressed > _MAX_TOTAL_UNCOMPRESSED:
                    raise ValueError(
                        f"ingest_zip_archive: uncompressed size "
                        f"{total_uncompressed} B exceeds {_MAX_TOTAL_UNCOMPRESSED} B "
                        "(zip-bomb guard); refusing."
                    )
                for info in infos:
                    if info.is_dir():
                        continue
                    dest = (extract_dir / info.filename).resolve()
                    if dest != extract_root and not str(dest).startswith(
                        str(extract_root) + os.sep
                    ):
                        raise ValueError(
                            f"ingest_zip_archive: unsafe path {info.filename!r} "
                            "escapes extract dir (zip-slip guard); refusing."
                        )
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(dest, "wb") as out:
                        shutil.copyfileobj(src, out)

            all_files = [p for p in extract_dir.rglob("*") if p.is_file()]
            total = len(all_files)
            log.info("ingest_zip_archive: extracted %d files run_id=%s", total, input.run_id)
            if archive_run_id:
                await _archive_progress.mark_fanning_out(
                    archive_run_id=archive_run_id, file_count=total,
                )

            # ── 3. Open a single asyncpg connection for SQL ingesters ─────────
            # NOTE: inside the tempfile context — extracted files are still on
            # disk while ingesters read them. Pre-archive_lifecycle this lived
            # outside the tempfile context which was incidentally wrong (the
            # tempfile cleanup races with ingestion); the wrap fixed it.
            conn: asyncpg.Connection = await asyncpg.connect(
                _build_dsn(),
                statement_cache_size=0,
            )
            try:
                # Audit 2026-06-28: session-scoped GUCs. This is a dedicated,
                # DIRECT (POSTGRES_DIRECT_HOST, non-PgBouncer) connection used
                # across all per-file ingesters with per-file error recovery —
                # a single wrapping transaction is impossible (one bad file
                # would abort it). SET LOCAL (is_local=true) outside a txn is
                # discarded immediately, leaving RLS GUCs unset for the ingester
                # queries. Session scope persists across the autocommit
                # statements; the conn is closed in the finally below so there
                # is no cross-tenant leak.
                await bind_workspace_scope(
                    conn,
                    workspace_id=input.workspace_id,
                    site="hatchet.ingest_zip_archive",
                    is_local=False,
                )
                await conn.execute(
                    "SELECT set_config('app.project_id', $1, false)",
                    input.project_id,
                )

                # ── 4. Fan-out by extension ───────────────────────────────────
                counts: dict[str, int] = {
                    "las": 0, "log": 0, "csv": 0, "tif": 0, "xlsx": 0, "pdf": 0,
                    "skipped": 0, "errors": 0, "unknown": 0,
                }
                errors: list[dict[str, str]] = []

                for idx, file_path in enumerate(all_files, start=1):
                    ext = file_path.suffix.lower().lstrip(".")
                    try:
                        await _ingest_one(
                            file_path=file_path,
                            ext=ext,
                            conn=conn,
                            store=store,
                            input=input,
                            counts=counts,
                        )
                        # Per-file success bump on the parent. Skip for
                        # extensions we treat as "skipped" rather than
                        # "succeeded" — _ingest_one bumps counts["skipped"]
                        # internally for those (zero-handler branches).
                        if archive_run_id and ext not in ("skipped",):
                            await _archive_progress.increment_counts(
                                archive_run_id=archive_run_id, succeeded=1,
                            )
                    except Exception as exc:
                        counts["errors"] += 1
                        errors.append({"file": file_path.name, "ext": ext, "error": str(exc)})
                        log.warning(
                            "ingest_zip_archive: error on %s — %s (continuing)",
                            file_path.name,
                            exc,
                        )
                        if archive_run_id:
                            await _archive_progress.increment_counts(
                                archive_run_id=archive_run_id, failed=1,
                            )

                    if idx % 10 == 0:
                        log.info(
                            "ingest_zip_archive: progress %d/%d run_id=%s counts=%s",
                            idx,
                            total,
                            input.run_id,
                            counts,
                        )

            finally:
                await conn.close()

        # Terminal mark INSIDE the archive_lifecycle — 'partial' when any
        # per-file ingester failed, 'completed' otherwise. archive_lifecycle
        # would mark 'failed' if we raised; we don't (per-file errors are
        # caught + counted above so a single bad LAS doesn't kill the run).
        if archive_run_id:
            terminal_status = "partial" if counts["errors"] > 0 else "completed"
            terminal_error = (
                f"{counts['errors']} of {total} files failed; see ingest_progress"
                if counts["errors"] > 0
                else None
            )
            await _archive_progress.mark_terminal(
                archive_run_id=archive_run_id,
                status=terminal_status,
                error_text=terminal_error,
            )

    summary = {
        "run_id": input.run_id,
        "archive_run_id": archive_run_id,
        "minio_key": input.minio_key,
        "total_files": total,
        "counts": counts,
        "error_count": len(errors),
        "errors_sample": errors[:20],  # cap sample to keep payload small
        "completed_at": datetime.now(UTC).isoformat(),
    }
    log.info("ingest_zip_archive.complete run_id=%s summary=%s", input.run_id, counts)
    return summary


# ---------------------------------------------------------------------------
# Per-file dispatcher
# ---------------------------------------------------------------------------

async def _ingest_one(
    *,
    file_path: Path,
    ext: str,
    conn: asyncpg.Connection,
    store: ObjectStorage,
    input: IngestZipArchiveInput,
    counts: dict[str, int],
) -> None:
    """Route a single extracted file to its ingester.

    Ingesters are imported lazily inside each branch so that a missing
    optional dep (e.g. ``lasio`` not installed in the ingestion worker
    image) only fails that extension's branch, not the entire workflow.
    """

    if ext in ("las",):
        # LAS well-log files → silver.collars + silver.well_log_curves
        from app.services.ingest.las_ingester import ingest_las_file  # noqa: PLC0415

        async with conn.transaction():
            result = await ingest_las_file(
                conn,
                str(file_path),
                workspace_id=input.workspace_id,
                project_id_override=input.project_id,
            )
        if result.skipped:
            counts["skipped"] += 1
            log.debug("ingest_zip_archive: LAS skipped %s — %s", file_path.name, result.skipped_reason)
        else:
            counts["las"] += 1

    elif ext == "log":
        # Cameco binary log files → parse header + upsert collar
        from app.services.ingest.cameco_log_ingester import (  # noqa: PLC0415
            parse_cameco_log_header,
            upsert_collar_from_log,
        )

        parsed = parse_cameco_log_header(str(file_path))
        if parsed.skipped:
            counts["skipped"] += 1
            log.debug("ingest_zip_archive: LOG skipped %s — %s", file_path.name, parsed.skipped_reason)
        else:
            async with conn.transaction():
                await upsert_collar_from_log(
                    conn,
                    project_id=input.project_id,
                    parsed=parsed,
                    workspace_id=input.workspace_id,
                )
            counts["log"] += 1

    elif ext == "csv":
        # Plain CSV collar/assay files → silver.collars. Restored 2026-08
        # after the Dagster retirement (2026-07-28) left CSV uploads with
        # no live ingestion path (Laravel's UploadController hard-rejects
        # category=collar/assay with a 422) — this ZIP-archive path was
        # the one live end-to-end route left, so it gets a .csv branch
        # instead of reviving Dagster. See csv_collar_ingester.py for the
        # expected column shape.
        from app.services.ingest.csv_collar_ingester import ingest_csv_collar_file  # noqa: PLC0415

        async with conn.transaction():
            result = await ingest_csv_collar_file(
                conn,
                str(file_path),
                workspace_id=input.workspace_id,
                project_id=input.project_id,
            )
        if result.skipped:
            counts["skipped"] += 1
            log.debug(
                "ingest_zip_archive: CSV skipped %s — %s", file_path.name, result.skipped_reason,
            )
        else:
            counts["csv"] += 1
            if result.skipped_rows:
                log.info(
                    "ingest_zip_archive: CSV %s landed %d/%d rows (%d skipped)",
                    file_path.name, result.valid_rows, result.total_rows, result.skipped_rows,
                )

    elif ext in ("tif", "tiff"):
        # TIFF scans → upload to bronze tiff/ prefix + trigger tiff_normalize
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        safe_name = _safe_filename(file_path.name)
        tiff_key = f"tiff/{input.project_id}/{ts}_{safe_name}"
        file_bytes = file_path.read_bytes()
        await asyncio.to_thread(store.put_bytes, Bucket.BRONZE, tiff_key, file_bytes)
        await tiff_normalize.aio_run_no_wait(
            TiffNormalizeInput(
                workspace_id=input.workspace_id,  # type: ignore[arg-type]
                project_id=input.project_id,
                minio_key=tiff_key,
                file_size=len(file_bytes),
                correlation_token=f"zip-{input.run_id}-{file_path.name}",
            )
        )
        # F7 (2026-08-11) — throttle the fan-out. An unthrottled burst of
        # dispatches saturates the GROUP_ROUND_ROBIN concurrency queue and
        # Hatchet silently CANCELS the overflow — exactly the Cameco
        # 529-file incident ([[cameco-recovery-2026-06-02]]).
        await asyncio.sleep(0.25)
        counts["tif"] += 1

    elif ext in ("xlsx", "xls"):
        # XLSX spreadsheets → silver.document_passages
        from app.services.ingest.xlsx_ingester import ingest_xlsx_file  # noqa: PLC0415

        async with conn.transaction():
            result = await ingest_xlsx_file(
                conn,
                str(file_path),
                workspace_id=input.workspace_id,
                project_id=input.project_id,
            )
        if result.skipped:
            counts["skipped"] += 1
        else:
            counts["xlsx"] += 1

    elif ext == "pdf":
        # PDF reports → upload to bronze reports/ prefix + trigger ingest_pdf
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        safe_name = _safe_filename(file_path.name)
        pdf_key = f"reports/{input.project_id}/{ts}_{safe_name}"
        file_bytes = file_path.read_bytes()
        await asyncio.to_thread(store.put_bytes, Bucket.BRONZE, pdf_key, file_bytes)
        await ingest_pdf.aio_run_no_wait(
            IngestPdfInput(
                workspace_id=input.workspace_id,
                project_id=input.project_id,
                minio_key=pdf_key,
                file_size=len(file_bytes),
                correlation_token=f"zip-{input.run_id}-{file_path.name}",
            )
        )
        # F7 (2026-08-11) — throttle the fan-out; see the TIFF branch above
        # (Cameco 529-file GROUP_ROUND_ROBIN saturation).
        await asyncio.sleep(0.25)
        counts["pdf"] += 1

    else:
        counts["unknown"] += 1
        log.debug("ingest_zip_archive: unknown ext .%s for %s — skipping", ext, file_path.name)


def _safe_filename(name: str) -> str:
    """Collapse characters that are unsafe in S3 keys to underscores."""
    import re
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]


# ---------------------------------------------------------------------------
# Failure hook (Theme D — 2026-06-03 audit)
# ---------------------------------------------------------------------------
@ingest_zip_archive.on_failure_task(
    name="on_failure",
    execution_timeout="30s",
    schedule_timeout="30m",
    retries=2,
)
async def on_failure(input: IngestZipArchiveInput, ctx: Context) -> dict[str, Any]:
    """Workflow-level failure hook for ZIP archive ingests.

    Fires from every path that can leave the run in a non-terminal state:
      - The body raised an unhandled exception that escaped the per-file
        try/except (the ``archive_lifecycle`` context manager re-raises
        after marking the row failed — this hook is the second backstop).
      - Hatchet cancelled the workflow (queue-depth saturation, manual
        cancel via the Hatchet UI). The ``archive_lifecycle`` body never
        ran in that case so the parent row stays ``queued`` — we
        transition it here.
      - Worker SIGTERM / SIGKILL.

    Mirrors the ``ingest_pdf.on_failure`` shape and the pattern documented
    in [[cameco-recovery-2026-06-02]].
    """
    from app.hatchet_workflows import _archive_progress  # noqa: PLC0415

    archive_run_id = await _archive_progress.lookup_archive_run_id_by_run_id(input.run_id)
    if archive_run_id is None:
        log.warning(
            "ingest_zip_archive.on_failure: no archive_run found for run_id=%s — "
            "the body never reached start_run. Cancellation likely fired before "
            "workflow dispatch.",
            input.run_id,
        )
        return {"updated": False, "reason": "no_archive_run"}

    # 2026-08-16 — capture the real upstream exception via Hatchet's
    # task_run_errors (populated for on_failure hooks, engine >= v0.53.10;
    # we run v0.89.7) instead of a hardcoded placeholder. Same fix as
    # ingest_pdf.on_failure.
    try:
        task_errors = ctx.task_run_errors
    except Exception as exc:  # noqa: BLE001 — never let diagnostics block the hook
        log.warning("ingest_zip_archive.on_failure: could not read task_run_errors: %s", exc)
        task_errors = {}
    if task_errors:
        error_detail = "; ".join(f"{name}: {msg}" for name, msg in task_errors.items())
    else:
        error_detail = "no task_run_errors available (worker crash/cancellation with no captured exception)"

    transitioned = await _archive_progress.mark_terminal(
        archive_run_id=archive_run_id,
        status="failed",
        error_text=error_detail,
    )
    return {
        "updated": transitioned,
        "archive_run_id": archive_run_id,
        "run_id": input.run_id,
    }


__all__ = ["ingest_zip_archive", "IngestZipArchiveInput"]
