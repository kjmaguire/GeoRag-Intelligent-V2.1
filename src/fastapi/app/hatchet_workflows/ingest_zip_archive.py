"""Hatchet workflow: extract a ZIP archive and fan out to per-file ingesters.

Handles the common field-data ZIP use-case: a geologist drops a 5 GB ZIP
containing hundreds of small files (TIF, LAS, LOG, XLSX, PDF ≤10 MB each)
into the upload UI. This workflow:

  1. Downloads the ZIP from SeaweedFS / MinIO to a temp directory.
  2. Extracts every entry with Python's ``zipfile`` module.
  3. Routes each extracted file by extension:
       .las / .LAS  →  las_ingester.ingest_las_file
       .log         →  cameco_log_ingester (parse header + upsert collar)
       .csv / .tsv  →  re-uploads to bronze tabular/ prefix + triggers
                       ingest_tabular, which classifies the header
       .tif / .tiff →  re-uploads to bronze tiff/ prefix + triggers tiff_normalize
       .xlsx / .xls →  ingest_tabular (every sheet classified separately)
       .pdf         →  re-uploads to bronze reports/ prefix + triggers ingest_pdf
       .shp + kin   →  re-zipped with its sidecars, uploaded to bronze
                       spatial/ prefix + triggers ingest_spatial
       .geojson / .gpkg / .gml / .gpx / .dxf / .fgb / .qgs / .qgz
                    →  uploaded to bronze spatial/ prefix + ingest_spatial
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
from app.db.dsn import build_dsn
from app.hatchet_workflows import hatchet
from app.hatchet_workflows.ingest_pdf import IngestPdfInput, ingest_pdf
from app.hatchet_workflows.ingest_spatial import (
    QGIS_PROJECT_EXTENSIONS,
    VECTOR_EXTENSIONS,
    IngestSpatialInput,
    ingest_spatial,
)
from app.hatchet_workflows.ingest_tabular import IngestTabularInput, ingest_tabular
from app.hatchet_workflows.tiff_normalize import TiffNormalizeInput, tiff_normalize

#: Vector + QGIS members this workflow hands off to ingest_spatial, without
#: the leading dot (ingest_spatial stores them Path.suffix-style).
#:
#: Before this existed, every .shp/.shx/.dbf/.prj in an archive fell through
#: to the `unknown` bucket and was logged at DEBUG only — and `unknown` never
#: contributes to the terminal status, so a ZIP of nothing but shapefiles was
#: marked `completed` with zero features written. The import wizard produced
#: exactly that ZIP, because it names a shapefile bundle `<stem>.zip` and
#: `.zip` resolves to the `archive` category.
_SPATIAL_EXTS = frozenset(
    e.lstrip(".") for e in (VECTOR_EXTENSIONS | QGIS_PROJECT_EXTENSIONS)
)

#: Shapefile companions. pyogrio reads these THROUGH the .shp, so opening one
#: directly is wrong — but they are not "unknown" either: the .shp branch
#: below re-zips them alongside their .shp. Counting them separately keeps
#: `unknown` meaning "we genuinely do not handle this".
_SHAPEFILE_SIDECAR_EXTS = frozenset({
    "shx", "dbf", "prj", "cpg", "qpj", "sbn", "sbx", "qix", "idx",
    "ain", "aih", "atx", "fbn", "fbx", "mxs", "shp_xml",
})

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



# One DSN builder for the whole service — see app/db/dsn.py for why
# sixty copies of this existed and what the drift cost.
_build_dsn = build_dsn


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

            # Hard rule 2. Extraction is CPU-bound zlib plus disk I/O with no
            # await point anywhere in it — on a 5 GiB archive that is minutes
            # of blocking on the worker's event loop, during which Hatchet's
            # heartbeat cannot fire. The engine then marks the worker dead
            # and cancels every OTHER in-flight task on it: a concurrent PDF
            # parse, an embed sweep. The download two lines up was already
            # wrapped for exactly this reason; the extraction next to it was
            # not. Same failure the subprocess pool in ingest_pdf.py exists
            # to avoid, reintroduced in the sibling workflow.
            def _extract_all() -> None:
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
                            f"{total_uncompressed} B exceeds "
                            f"{_MAX_TOTAL_UNCOMPRESSED} B (zip-bomb guard); refusing."
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

            await asyncio.to_thread(_extract_all)

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
                    "spatial": 0, "sidecar": 0,
                    "skipped": 0, "errors": 0, "unknown": 0,
                }
                errors: list[dict[str, str]] = []

                for idx, file_path in enumerate(all_files, start=1):
                    ext = file_path.suffix.lower().lstrip(".")
                    try:
                        # Snapshot the buckets _ingest_one may bump, so
                        # "did this file actually land" is answered by what
                        # changed rather than by whether an exception was
                        # raised. Several ingesters report failure by
                        # RETURNING skipped=True — lasio on an unreadable
                        # LAS, an empty workbook — and never raise at all.
                        before_skipped = counts["skipped"]
                        before_unknown = counts["unknown"]
                        before_sidecar = counts["sidecar"]

                        await _ingest_one(
                            file_path=file_path,
                            ext=ext,
                            conn=conn,
                            store=store,
                            input=input,
                            counts=counts,
                        )

                        # This used to read `if ext not in ("skipped",)`.
                        # `ext` is a file extension — 'las', 'docx', '' — and
                        # can never equal the literal string "skipped", so
                        # the condition was always true and every file
                        # counted as a success. A 600-file ZIP of .docx notes
                        # and shapefile bundles, none of which had a handler,
                        # reported "600 files, 600 succeeded, 0 failed,
                        # completed" having ingested nothing at all.
                        handled = (
                            counts["skipped"] == before_skipped
                            and counts["unknown"] == before_unknown
                        )
                        # A shapefile sidecar is neither a success nor a
                        # failure: the .shp branch already carried it.
                        was_sidecar = counts["sidecar"] != before_sidecar

                        if archive_run_id and handled and not was_sidecar:
                            await _archive_progress.increment_counts(
                                archive_run_id=archive_run_id, succeeded=1,
                            )
                        elif archive_run_id and not handled:
                            await _archive_progress.increment_counts(
                                archive_run_id=archive_run_id, skipped=1,
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

    # ── 5. Derive lithology / interval strip logs from the LAS curves ──────
    # gold.drillhole_intervals_visual — the lithology strip logs, ore-band
    # counts and mean grades behind Workspace / Compare / DrillholeDetail —
    # had no automated writer. Its Dagster asset was deleted in #124 (it read
    # a table that never existed) and the only correct writer,
    # services/ingest/derive_intervals.derive_project, was reachable only from
    # the manual script scripts/ingest_one_cluster.py. So every archive
    # ingested through this workflow produced well-log curves that never
    # became a strip log unless someone ran that script by hand.
    #
    # Gated on counts["las"]: derive_project reads silver.well_log_curves, and
    # LAS is the only extension in this workflow that writes them (.log files
    # upsert a collar header only).
    #
    # Runs once per archive rather than per file — derive_project sweeps every
    # collar in the project, and its writes are idempotent: each collar's
    # DERIVED-% lithology rows, derived_composite samples and 'lithology'
    # interval rows are deleted and re-emitted. A re-run, or an archive that
    # only adds some of a project's holes, simply recomputes from whatever
    # curves are present.
    #
    # A failure here must not fail the archive. Every file is already ingested
    # by this point and the terminal status has already been marked, so the
    # error is recorded in the summary and left for the next run to correct —
    # same "one bad step doesn't kill the run" posture as the per-file loop.
    derive_intervals_summary: dict[str, Any] | None = None
    if counts["las"] > 0:
        from app.services.ingest.derive_intervals import derive_project  # noqa: PLC0415

        try:
            derive_intervals_summary = await derive_project(input.project_id)
            log.info(
                "ingest_zip_archive.derive_intervals run_id=%s %s",
                input.run_id,
                derive_intervals_summary,
            )
        except Exception as exc:
            derive_intervals_summary = {"error": str(exc)[:200]}
            log.warning(
                "ingest_zip_archive: derive_intervals failed run_id=%s — %s (continuing)",
                input.run_id,
                exc,
            )

    summary = {
        "run_id": input.run_id,
        "archive_run_id": archive_run_id,
        "minio_key": input.minio_key,
        "total_files": total,
        "counts": counts,
        "error_count": len(errors),
        "errors_sample": errors[:20],  # cap sample to keep payload small
        "derive_intervals": derive_intervals_summary,
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

    # No ".txt": inside an archive that is almost always a readme, and
    # routing one into ingest_tabular spawns a workflow whose only output is
    # a `nothing_classified` warning. Drill data arriving as .txt comes in
    # under an explicit upload category, where the user has said what it is.
    elif ext in ("csv", "tsv", "xlsx", "xls", "xlsm"):
        # Tabular data — re-upload to bronze and hand off to ingest_tabular,
        # the same pattern .pdf, .tif and the vector branch use.
        #
        # This branch used to call ingest_csv_collar_file unconditionally for
        # .csv, and that ingester requires hole_id/easting/northing. Zip a
        # hole's full dataset — collars.csv, survey.csv, lithology.csv,
        # assays.csv — and only collars.csv landed. The other three returned
        # skipped_reason="missing_required_columns", which increments
        # counts["skipped"] rather than counts["errors"], so the archive was
        # still marked completed and the summary reported four files
        # succeeded. The user got collars with no surveys, no lithology and
        # no assays, and nothing told them.
        #
        # ingest_tabular classifies the header and routes to the right silver
        # table, and for a workbook it classifies EVERY sheet rather than
        # assuming the first one is the data. Deliberately no sheet_type
        # hint: inside an archive there is no user-chosen category to pass,
        # and an explicit hint makes ingest_tabular skip classification
        # entirely.
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        safe_name = _safe_filename(file_path.name)
        tabular_key = f"tabular/{input.project_id}/{ts}_{safe_name}"
        file_bytes = await asyncio.to_thread(file_path.read_bytes)
        await asyncio.to_thread(store.put_bytes, Bucket.BRONZE, tabular_key, file_bytes)
        await ingest_tabular.aio_run_no_wait(
            IngestTabularInput(
                workspace_id=input.workspace_id,
                project_id=input.project_id,
                minio_key=tabular_key,
            )
        )
        # F7 (2026-08-11) — throttle the fan-out; see the TIFF branch below
        # (Cameco 529-file GROUP_ROUND_ROBIN saturation).
        await asyncio.sleep(0.25)
        counts["csv" if ext in ("csv", "tsv") else "xlsx"] += 1

    elif ext in ("tif", "tiff"):
        # TIFF scans → upload to bronze tiff/ prefix + trigger tiff_normalize
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        safe_name = _safe_filename(file_path.name)
        tiff_key = f"tiff/{input.project_id}/{ts}_{safe_name}"
        file_bytes = await asyncio.to_thread(file_path.read_bytes)
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

    elif ext == "pdf":
        # PDF reports → upload to bronze reports/ prefix + trigger ingest_pdf
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        safe_name = _safe_filename(file_path.name)
        pdf_key = f"reports/{input.project_id}/{ts}_{safe_name}"
        file_bytes = await asyncio.to_thread(file_path.read_bytes)
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

    elif ext in _SPATIAL_EXTS:
        # Vector / QGIS data — re-upload to the bronze spatial/ prefix and hand
        # off to ingest_spatial, the same pattern the .pdf and .tif branches
        # use. A shapefile is never one file, so a .shp is re-zipped with its
        # same-stem companions first; ingest_spatial's archive path unpacks
        # that and pyogrio reads the .prj it needs to know the CRS.
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        if ext == "shp":
            members = sorted(
                sib for sib in file_path.parent.iterdir()
                if sib.is_file() and sib.stem == file_path.stem
            )
            bundle_path = file_path.parent / f"__bundle_{file_path.stem}.zip"
            def _write_bundle() -> None:
                with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for m in members:
                        zf.write(m, arcname=m.name)
            await asyncio.to_thread(_write_bundle)
            payload_bytes = await asyncio.to_thread(bundle_path.read_bytes)
            bundle_path.unlink(missing_ok=True)
            safe_name = _safe_filename(f"{file_path.stem}.zip")
        else:
            payload_bytes = await asyncio.to_thread(file_path.read_bytes)
            safe_name = _safe_filename(file_path.name)

        spatial_key = f"spatial/{input.project_id}/{ts}_{safe_name}"
        await asyncio.to_thread(store.put_bytes, Bucket.BRONZE, spatial_key, payload_bytes)
        await ingest_spatial.aio_run_no_wait(
            IngestSpatialInput(
                workspace_id=input.workspace_id,
                project_id=input.project_id,
                minio_key=spatial_key,
            )
        )
        # F7 (2026-08-11) — throttle the fan-out; see the TIFF branch above
        # (Cameco 529-file GROUP_ROUND_ROBIN saturation).
        await asyncio.sleep(0.25)
        counts["spatial"] += 1

    elif ext in _SHAPEFILE_SIDECAR_EXTS:
        # Absorbed by the .shp branch above. Counted, not "unknown".
        counts["sidecar"] += 1

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
