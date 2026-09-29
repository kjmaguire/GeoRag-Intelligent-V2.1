"""Ingest geophysics line and DC/IP data into silver (ING-19, 2026-09-29).

Two parsers had no caller until this workflow existed:

* ``georag_geoparsers.xyz_parser`` — Geosoft XYZ line data (airborne and
  ground magnetics, gravity, radiometrics, EM). A ``.xyz`` upload answered
  ``422 retired_pipeline`` and an ``.xyz`` inside a ZIP was ``unknown``.
* ``georag_geoparsers.dcip2d_survey`` — a UBC-GIF DCIP2D inversion export
  (observed ``.rdt*`` readings, ``dcinv2d.*`` / ``ipinv2d.*`` models and the
  ``.inp`` control file). A DCIP2D export is a DIRECTORY, so it arrives
  inside a ZIP: ``ingest_zip_archive`` bundles each export directory into its
  own small ZIP and dispatches it here. A single ``.rdt*`` file is accepted
  too and read as an export of one split.

Where it lands — ``app/services/ingest/geophysics_writer.py``:
``silver.geophysics_surveys`` (one row per file / export) with its lines
and channels (XYZ) or its readings and models (DCIP2D) beneath it.

Why its own workflow
--------------------
Not a branch of ``ingest_spatial``: an XYZ survey is not a vector layer —
it is channels sampled along lines, stored as arrays per line the way
``ingest_well_logs`` stores curves per hole, and a DCIP2D export has no
coordinates at all. Not a branch of ``ingest_tabular`` either: neither
format is a table with a header row the drill classifier could read.

Refusals and partial results
----------------------------
Row-level problems (a short XYZ row, a malformed reading, an unreadable
model) skip that row or file with a reason and never fail the upload. A
FILE-level ambiguity — no coordinate header in an XYZ, DC/IP splits that
disagree about which line they are — is a refusal: nothing is written, the
run completes as ``partial`` with the reason, and the previous upload of
the same file (if any) is left as it was. Only an infrastructure failure
(storage, database) fails the run, so Hatchet's retry is meaningful.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
import time as _t
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

import asyncpg
from georag_object_storage import Bucket, get_storage_client
from hatchet_sdk import Context
from pydantic import BaseModel, Field, field_validator

from app.db import bind_workspace_scope
from app.db.dsn import build_dsn
from app.hatchet_workflows import _progress, hatchet
from app.services.ingest.dcip_bundle import (
    DCIP_OBSERVED_PREFIX,
    extract_bundle,
    survey_name_for,
)
from app.services.ingest.geochronology_writer import logical_source_name

log = logging.getLogger("georag.hatchet.ingest_geophysics")

XYZ_EXTENSIONS = frozenset({".xyz"})
#: A ZIP holding ONE DCIP2D export directory (what ingest_zip_archive sends).
BUNDLE_EXTENSIONS = frozenset({".zip"})
#: The platform default for an XYZ with no declared or project CRS. The SAME
#: default ingest_tabular uses for collars, and warned about as loudly.
DEFAULT_SOURCE_EPSG = 32613

_build_dsn = build_dsn

#: Database errors that mean "this data", not "this database": a value a
#: CHECK or type refuses. They refuse the one survey; anything else
#: (connection loss, permissions) fails the run so Hatchet retries it.
_DATA_REFUSALS: tuple[type[Exception], ...] = (
    asyncpg.DataError,
    asyncpg.IntegrityConstraintViolationError,
)


def is_supported(filename: str) -> bool:
    suffix = Path(filename).suffix.lower()
    return (
        suffix in XYZ_EXTENSIONS
        or suffix in BUNDLE_EXTENSIONS
        or suffix.startswith(DCIP_OBSERVED_PREFIX)
    )


class IngestGeophysicsInput(BaseModel):
    workspace_id: str
    project_id: str
    minio_key: str
    run_id: str | None = None
    #: EPSG of an XYZ file's X/Y. Same name, type and range as
    #: IngestTabularInput.source_epsg. Ignored for DC/IP, which has no
    #: coordinates to place.
    source_epsg: int | None = Field(default=None, ge=1024, le=32767)
    #: The name the survey is filed under, when the bronze key's own name is
    #: not the one the user knows — ingest_zip_archive passes
    #: "<archive>/<path of the export directory>" for a DCIP2D bundle, so a
    #: re-upload of the same archive replaces the same surveys.
    source_name: str | None = None

    @field_validator("workspace_id", "project_id")
    @classmethod
    def _must_be_uuid(cls, v: str) -> str:
        import uuid  # noqa: PLC0415

        uuid.UUID(v)
        return v


class IngestGeophysicsOut(BaseModel):
    run_id: str | None
    source_format: str
    surveys: list[dict[str, Any]] = Field(default_factory=list)
    rows_written: int = 0
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    duration_ms: int = 0


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _refusal(label: str, exc: BaseException, what: str) -> dict[str, Any]:
    return {
        "code": "geophysics_file_refused",
        "message": f"{label}: not ingested — {what}",
        "detail": (
            f"{label} could not be read as {what}: {str(exc)[:400]}. Nothing was "
            f"written and any earlier upload of it was left as it was."
        ),
    }


ingest_geophysics = hatchet.workflow(
    name="ingest_geophysics",
    input_validator=IngestGeophysicsInput,
)


@ingest_geophysics.task(execution_timeout="2h", schedule_timeout="2h", retries=1)
async def run_ingest_geophysics(
    input: IngestGeophysicsInput, ctx: Context,
) -> IngestGeophysicsOut:
    """Download one XYZ / DCIP2D upload, parse it and replace its survey(s)."""
    from app.services.ingest.geophysics_writer import (  # noqa: PLC0415
        write_dcip_survey,
        write_xyz_survey,
    )

    t0 = _t.monotonic()
    store = get_storage_client()
    filename = input.minio_key.rsplit("/", 1)[-1]
    suffix = Path(filename).suffix.lower()
    if not is_supported(filename):
        raise ValueError(
            f"ingest_geophysics cannot handle {suffix!r} ({filename}); supported: "
            ".xyz, .rdt*, or a .zip bundle holding a DCIP2D export directory"
        )
    source_name = input.source_name or logical_source_name(filename)

    run_id = await _progress.start_run(
        workspace_id=input.workspace_id,
        project_id=input.project_id,
        minio_key=input.minio_key,
        triggered_by="upload",
        workflow_run_id=getattr(ctx, "workflow_run_id", None),
        run_id=input.run_id,
    )

    warnings: list[dict[str, Any]] = []
    surveys: list[dict[str, Any]] = []
    rows_written = 0
    source_format = "xyz" if suffix in XYZ_EXTENSIONS else "dcip2d"

    try:
        if run_id:
            await _progress.mark_stage_started(run_id=run_id, stage="preflight")

        with tempfile.TemporaryDirectory(prefix="georag_geophysics_") as tmpdir:
            local = Path(tmpdir) / filename
            await asyncio.to_thread(store.get_file, Bucket.BRONZE, input.minio_key, str(local))
            sha256 = await asyncio.to_thread(_sha256_file, str(local))

            if run_id:
                await _progress.mark_stage_started(run_id=run_id, stage="parse")

            conn = await asyncpg.connect(_build_dsn())
            try:
                await bind_workspace_scope(
                    conn,
                    workspace_id=input.workspace_id,
                    site="hatchet.ingest_geophysics",
                    is_local=False,
                )
                if run_id:
                    await _progress.mark_stage_started(run_id=run_id, stage="persist")

                if suffix in XYZ_EXTENSIONS:
                    project_epsg: int | None = None
                    if input.source_epsg is None:
                        try:
                            raw_epsg = await conn.fetchval(
                                "SELECT crs_epsg FROM silver.projects WHERE project_id = $1::uuid",
                                input.project_id,
                            )
                            project_epsg = int(raw_epsg) if raw_epsg is not None else None
                        except asyncpg.PostgresError as exc:
                            log.warning(
                                "ingest_geophysics: project CRS unreadable for %s — %s",
                                input.project_id, type(exc).__name__,
                            )
                    try:
                        result = await write_xyz_survey(
                            conn,
                            path=str(local),
                            workspace_id=input.workspace_id,
                            project_id=input.project_id,
                            survey_name=source_name,
                            source_file=filename,
                            source_file_sha256=sha256,
                            source_object_key=input.minio_key,
                            declared_epsg=input.source_epsg,
                            project_epsg=project_epsg,
                            default_epsg=DEFAULT_SOURCE_EPSG,
                        )
                    except ValueError as exc:
                        warnings.append(_refusal(filename, exc, "a Geosoft XYZ export"))
                    except _DATA_REFUSALS as exc:
                        warnings.append(
                            _refusal(filename, exc, "rows the geophysics tables accept"),
                        )
                    else:
                        warnings.extend(result.warnings)
                        rows_written += result.rows_written
                        surveys.append({
                            "survey_id": result.survey_id,
                            "survey_name": result.survey_name,
                            "survey_type": result.survey_type,
                            "replaced": result.replaced,
                            **result.counts,
                        })
                else:
                    written, found = await _ingest_dcip(
                        conn, local=local, tmpdir=Path(tmpdir), input=input,
                        filename=filename, source_name=source_name, sha256=sha256,
                        warnings=warnings, write=write_dcip_survey,
                    )
                    rows_written += written
                    surveys.extend(found)
            finally:
                await conn.close()

        if run_id:
            transitioned = await _progress.mark_completed_by_run(
                run_id=run_id, rows_written=rows_written, warnings=warnings,
            )
            if transitioned:
                await _progress.broadcast_terminal(
                    workspace_id=input.workspace_id,
                    project_id=input.project_id,
                    run_id=run_id,
                    stage="persist",
                    status=_progress.terminal_status(
                        rows_written=rows_written, warnings=warnings,
                    ),
                    message=_progress.terminal_message(
                        rows_written=rows_written, warnings=warnings,
                        noun="point" if source_format == "xyz" else "reading",
                    ),
                )
    except Exception as exc:
        if run_id:
            await _progress.mark_failed_by_run(run_id=run_id, error=str(exc)[:1000])
        log.exception("ingest_geophysics failed for %s", input.minio_key)
        raise

    out = IngestGeophysicsOut(
        run_id=run_id,
        source_format=source_format,
        surveys=surveys,
        rows_written=rows_written,
        warnings=warnings[:20],
        duration_ms=int((_t.monotonic() - t0) * 1000),
    )
    log.info("ingest_geophysics complete: %s", out.model_dump(exclude={"warnings"}))
    return out


async def _ingest_dcip(
    conn: asyncpg.Connection,
    *,
    local: Path,
    tmpdir: Path,
    input: IngestGeophysicsInput,
    filename: str,
    source_name: str,
    sha256: str,
    warnings: list[dict[str, Any]],
    write: Any,
) -> tuple[int, list[dict[str, Any]]]:
    """Read every DCIP2D export in the upload and write each as its own survey."""
    from georag_geoparsers.dcip2d_survey import read_dcip2d_survey  # noqa: PLC0415

    if local.suffix.lower() in BUNDLE_EXTENSIONS:
        bundle_root = tmpdir / "bundle"
        bundle_root.mkdir()
        try:
            export_dirs = await asyncio.to_thread(extract_bundle, local, bundle_root)
        except (zipfile.BadZipFile, ValueError) as exc:
            warnings.append(_refusal(filename, exc, "a DCIP2D export bundle"))
            return 0, []
    else:
        # A lone observed-data split: read it as an export of one file. The
        # directory is named for nothing, so the line comes from the title.
        bundle_root = tmpdir / "single" / "export"
        bundle_root.mkdir(parents=True)
        local.rename(bundle_root / local.name)
        export_dirs = [bundle_root]

    if not export_dirs:
        warnings.append({
            "code": "geophysics_no_dcip_export",
            "message": f"{filename}: no DCIP2D observed-data (.rdt*) file inside",
            "detail": f"{filename} holds no .rdt* file, so there was no survey to read.",
        })
        return 0, []

    written = 0
    surveys: list[dict[str, Any]] = []
    for export_dir in export_dirs:
        label = str(PurePosixPath(source_name) / export_dir.relative_to(bundle_root).as_posix())
        try:
            survey = await asyncio.to_thread(
                read_dcip2d_survey, export_dir, skip_bad_rows=True,
            )
        except (ValueError, FileNotFoundError) as exc:
            warnings.append(_refusal(label, exc, "a UBC-GIF DCIP2D export"))
            continue
        survey_name = survey_name_for(source_name, export_dir, bundle_root, survey.line_id)
        try:
            result = await write(
                conn,
                survey=survey,
                workspace_id=input.workspace_id,
                project_id=input.project_id,
                survey_name=survey_name,
                source_file=filename,
                source_file_sha256=sha256,
                source_object_key=input.minio_key,
            )
        except _DATA_REFUSALS as exc:
            # One export the table refuses (its own transaction rolled back)
            # must not cost the other exports in the same bundle.
            warnings.append(_refusal(label, exc, "rows the geophysics tables accept"))
            continue
        warnings.extend(result.warnings)
        written += result.rows_written
        surveys.append({
            "survey_id": result.survey_id,
            "survey_name": survey_name,
            "survey_type": result.survey_type,
            "replaced": result.replaced,
            **result.counts,
        })
    return written, surveys


@ingest_geophysics.on_failure_task(
    name="on_failure",
    execution_timeout="30s",
    schedule_timeout="30m",
    retries=2,
)
async def on_failure(input: IngestGeophysicsInput, ctx: Context) -> dict[str, Any]:
    """Close the ingest_progress row when the workflow dies (mirrors ingest_well_logs)."""
    return await _progress.close_run_after_workflow_failure(
        workflow_name="ingest_geophysics",
        workspace_id=str(input.workspace_id) if input.workspace_id else None,
        project_id=str(input.project_id) if input.project_id else None,
        minio_key=input.minio_key,
        run_id=input.run_id,
        ctx=ctx,
    )


__all__ = [
    "BUNDLE_EXTENSIONS",
    "DCIP_OBSERVED_PREFIX",
    "XYZ_EXTENSIONS",
    "IngestGeophysicsInput",
    "IngestGeophysicsOut",
    "ingest_geophysics",
    "is_supported",
]
