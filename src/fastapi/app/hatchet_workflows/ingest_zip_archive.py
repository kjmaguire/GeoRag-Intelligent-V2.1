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
       .mdb / .accdb / standalone .dbf / .dat
                    →  ingest_tabular (one attribute_tables layer per table)
       .pdf         →  re-uploads to bronze reports/ prefix + triggers ingest_pdf
       .shp + kin   →  re-zipped with its sidecars, uploaded to bronze
                       spatial/ prefix + triggers ingest_spatial
       .geojson / .gpkg / .gml / .gpx / .dxf / .fgb / .qgs / .qgz
                    →  uploaded to bronze spatial/ prefix + ingest_spatial
       .xyz         →  bronze xyz/ prefix + ingest_geophysics (Geosoft XYZ
                       line data; ING-19)
       a directory holding .rdt* files (a UBC-GIF DCIP2D export)
                    →  its .rdt* / dcinv2d.* / ipinv2d.* / .inp (+ the mesh
                       and topography files the .inp names) re-zipped as ONE
                       member, bronze xyz/ prefix + ingest_geophysics
  4. Logs progress every 10 files.
  5. Returns a summary dict with per-extension counts and error tally.

Individual file errors are caught, logged, and skipped — a corrupt LAS
file should not abort the 600 other files in the same ZIP.

Two-phase dispatch
------------------
Members are NOT all fired in ``rglob`` order. Interval tables (survey /
lithology / sample) and LAS curves attach to collars, and every collar-bearing
member is an asynchronous child run: an interval file that happened to run
first found no holes, ingest_tabular counted its rows ``orphaned`` (and warned
``orphaned_intervals``: "upload the collar file, then re-run this one"), and
nothing ever re-ran it. So:

  phase 1  everything that does not need a collar to exist -- collar tables,
           workbooks holding a collar sheet, spatial / raster / PDF members,
           Cameco ``.log`` headers, unclassifiable tables.
  wait     for the phase-1 ingest_tabular runs to reach a terminal state
           (``hatchet.runs.aio_get_status``, polled), bounded by
           ``GEORAG_ZIP_PHASE_WAIT_TIMEOUT_S`` (default 1500 s). On timeout
           phase 2 is dispatched anyway, with an ``archive_dependency_wait_
           timeout`` warning.
  phase 2  interval tables and LAS files.

Which phase a table belongs to is decided by the SAME header classifier
ingest_tabular uses (``classify_sheet_type`` / ``enumerate_sheets``), not by
filename guessing: a member is deferred only when it classifies to interval
types exclusively. A sniff that fails, or finds anything else, leaves it in
phase 1, i.e. today's behaviour. The alternative -- dispatch everything, then
re-dispatch members whose runs reported ``orphaned_intervals`` -- was
rejected: it needs each child's result payload, re-runs interval files a
second time (and a hole split across two files would have file 2's rows
replaced by file 1's re-run), and does nothing for LAS, which runs in-process
and needs its collars before it starts, not after.

Re-ingesting a member replaces rather than duplicates: ingest_tabular upserts
collars on (project_id, hole_id) and deletes-then-inserts interval rows scoped
to the collars a file mentions, in one transaction (``_INTERVAL_TABLES``).

The archive task's own ``execution_timeout`` stays 4 h: the two waits add at
most 2 x 25 min. While it waits it holds a worker slot (HATCHET_WORKER_SLOTS,
default 20); twenty archives waiting at once on children that need a slot
would stall until the timeout above, then carry on.
"""
from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import logging
import os
import re
import shutil
import tempfile
import uuid
import zipfile
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import asyncpg
from georag_object_storage import Bucket, ObjectStorage, get_storage_client
from hatchet_sdk import Context
from pydantic import BaseModel, Field, field_validator

from app.db import bind_workspace_scope
from app.db.dsn import build_dsn
from app.hatchet_workflows import _progress as ingest_progress
from app.hatchet_workflows import hatchet
from app.hatchet_workflows.ingest_geophysics import (
    IngestGeophysicsInput,
    ingest_geophysics,
)
from app.hatchet_workflows.ingest_pdf import IngestPdfInput, ingest_pdf
from app.hatchet_workflows.ingest_spatial import (
    QGIS_PROJECT_EXTENSIONS,
    VECTOR_EXTENSIONS,
    IngestSpatialInput,
    ingest_spatial,
)
from app.hatchet_workflows.ingest_tabular import IngestTabularInput, ingest_tabular
from app.hatchet_workflows.tiff_normalize import TiffNormalizeInput, tiff_normalize
from app.services.ingest.dcip_bundle import (
    DcipExport,
    find_dcip_exports,
    relative_dir,
    write_bundle,
)

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

#: Raster members, routed to the bronze `tiff/` prefix and tiff_normalize.
#:
#: THE FOURTH COPY of "is this a raster". UploadController collapsed its three
#: into RASTER_REPORT_EXTS when `.rrd` was added; this one was missed, so an
#: `.rrd` inside a ZIP fell through to `unknown` — and `unknown` never
#: contributes to the terminal status, so the archive was marked `completed`
#: having silently skipped it. Measured by dispatching the real RedStar
#: filenames through _ingest_one: both .rrd files returned {'unknown': 1}.
#:
#: It was missed AGAIN on 2026-08-25 when `.jpg`/`.jpeg` were added, with the
#: identical outcome: a scanned legend uploaded on its own ingests, and the
#: same file inside a ZIP is silently counted `unknown` on a run that reports
#: `completed`. Twice is a pattern, so the two lists are now pinned by
#: tests/test_zip_raster_exts_match_php.py, which parses
#: UploadController::RASTER_REPORT_EXTS and fails when they disagree — the
#: same trick resources/js/lib/__tests__/uploadCategories.test.ts uses to hold
#: the TypeScript side against the PHP.
#:
#: Kept as a local frozenset rather than imported because there is no shared
#: source between the two languages — but a comment is not a mechanism, and
#: the previous version of this note pointed at a PHP docblock that did not
#: in fact name this file.
#:
#: 2026-10-04: ``png``/``bmp``/``gif``/``webp`` added (standalone scanned
#: images, normalised by tiff_to_pdf). UploadController::RASTER_REPORT_EXTS
#: must carry the same set or test_zip_raster_exts_match_php.py fails.
_RASTER_EXTS = frozenset({
    "tif", "tiff", "rrd", "jpg", "jpeg", "png", "bmp", "gif", "webp",
})

#: Standalone dBASE tables. NOT shapefile sidecars when no same-stem .shp is
#: present in the archive — a bare .dbf or MapInfo .dat is a whole attribute
#: table that ingest_tabular reads directly. Being in the sidecar bucket meant
#: they were counted as handled and then nothing opened them.
_DBASE_EXTS = frozenset({"dbf", "dat"})

#: Microsoft Access databases. ingest_tabular reads them through mdbtools and
#: lands one layer per Access table; before this they fell to `unknown` inside a
#: ZIP although the same file uploaded on its own ingests.
_ACCESS_EXTS = frozenset({"mdb", "accdb"})

#: The ``counts`` buckets that mean "this member was handed to an ingester".
#: Their sum is what the archive's own silver.ingest_progress row reports as
#: ``rows_written`` — for an archive the unit of work is a member file, and
#: the rows those members produce are reported by the child runs.
_DISPATCHED_COUNT_KEYS: tuple[str, ...] = (
    "las", "las_pending", "log", "csv", "xlsx", "tif", "pdf", "spatial", "tabular",
    "geophysics",
)

#: Geosoft XYZ line data -> ingest_geophysics (ING-19, 2026-09-29). Fell to
#: `unknown` before, like every format without a branch here.
_GEOPHYSICS_XYZ_EXTS = frozenset({"xyz"})

#: A UBC-GIF DCIP2D export is a DIRECTORY whose files are unreadable alone;
#: it is bundled and dispatched as one member (services/ingest/dcip_bundle) —
#: the same move the .shp branch makes for a shapefile's sidecars.

#: Every bucket ``_ingest_one`` and the fan-out loop increment. The loop's
#: ``counts`` dict is built from this so a branch cannot bump a key the dict
#: never had: ``counts["tabular"] += 1`` on a dict without ``"tabular"`` is a
#: KeyError, and the per-file try/except turned that into ``errors += 1`` —
#: so every standalone .dbf/.dat in a ZIP was dispatched correctly and then
#: reported as a failed member, closing the archive as ``partial`` with
#: "1 of N files failed" for a file that had in fact landed.
_COUNT_KEYS: tuple[str, ...] = (
    *_DISPATCHED_COUNT_KEYS, "sidecar", "skipped", "errors", "unknown",
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

    #: The CRS the operator declared for the archive's contents, forwarded to
    #: every member this workflow fans out.
    #:
    #: Without it a zipped delivery had NO WAY to declare its coordinate
    #: system. ingest_tabular resolves `epsg = input.source_epsg or
    #: DEFAULT_SOURCE_EPSG` and never consults the project, so every collar
    #: and surface sample inside a ZIP was written as EPSG:32613 — the
    #: Athabasca default. For RedStar's Sitka collars (EPSG:26904, easting
    #: 400807, northing 6117291) that puts them at POINT(-106.5582 55.1922),
    #: northern Saskatchewan, 3,430 km from Unga Island. The run's only
    #: signal was the generic `collar_crs_assumed` warning, whose advice —
    #: "re-upload with the correct EPSG code" — could not be followed,
    #: because the wizard rendered no CRS control for the `archive` category.
    #:
    #: Same name, type and 1024..32767 range as IngestTabularInput and
    #: IngestSpatialInput, deliberately: one concept, one spelling.
    #:
    #: Defaulted, and it must stay defaulted — Laravel omits the key entirely
    #: when the operator typed nothing, and stale_run_detector reconstructs
    #: this model from a stored run without it.
    source_epsg: int | None = Field(default=None)

    @field_validator("source_epsg")
    @classmethod
    def _epsg_in_range(cls, v: int | None) -> int | None:
        """Reject at the boundary what the database would reject at persist."""
        if v is None:
            return v
        if not (1024 <= v <= 32767):
            raise ValueError("EPSG codes must be in the range 1024-32767.")
        return v

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
# Two-phase dispatch: collar producers first, collar dependents after
# ---------------------------------------------------------------------------

#: Tables that FK to a collar. A member that classifies to these ONLY has
#: nothing to write until its collars exist. ``structure`` is one: its rows
#: resolve to a collar by hole id exactly like an interval table's. So are
#: ``alteration`` and ``mineralization`` (silver.alteration /
#: silver.mineralization): a member that is a standalone alteration or
#: mineralization log waits for its collars like a lithology log does. (A
#: lithology log that merely CARRIES those columns is already a lithology
#: member, so it needs no rule of its own.)
_INTERVAL_SHEET_TYPES = frozenset({
    "survey", "lithology", "sample", "structure", "alteration", "mineralization",
})
_WRITE_SHEET_TYPES = frozenset({"collar"}) | _INTERVAL_SHEET_TYPES

_PHASE_PRODUCERS = 1
_PHASE_DEPENDENTS = 2

#: Extensions ingest_tabular classifies by header, and so can be sniffed.
_SNIFFED_CSV_EXTS = frozenset({"csv", "tsv", "txt"})
_SNIFFED_WORKBOOK_EXTS = frozenset({"xlsx", "xls", "xlsm"})
#: Standalone dBASE / MapInfo DAT tables and Access databases. ingest_tabular
#: now routes their tables through the same classifier (typed collar /
#: survey / lithology / sample / structure writes), so an Access database of
#: nothing but lithology has to wait for its collars like a CSV would.
_SNIFFED_DBASE_EXTS = frozenset({"dbf", "dat"})
_SNIFFED_ACCESS_EXTS = frozenset({"mdb", "accdb"})

_TERMINAL_RUN_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})

_DEFAULT_WAIT_TIMEOUT_S = 25 * 60.0
_DEFAULT_WAIT_POLL_S = 10.0
#: Concurrent status calls per poll round; each is a blocking REST call in a
#: thread (hatchet.runs.aio_get_status), so a 500-member archive must not
#: open 500 at once.
_STATUS_CONCURRENCY = 8


def _wait_timeout_s() -> float:
    """Bound on each dependency wait; GEORAG_ZIP_PHASE_WAIT_TIMEOUT_S overrides."""
    raw = os.environ.get("GEORAG_ZIP_PHASE_WAIT_TIMEOUT_S")
    if raw:
        try:
            value = float(raw)
        except ValueError:
            log.warning(
                "ingest_zip_archive: GEORAG_ZIP_PHASE_WAIT_TIMEOUT_S=%r is not "
                "a number; using %ss", raw, _DEFAULT_WAIT_TIMEOUT_S,
            )
        else:
            if value >= 0:
                return value
    return _DEFAULT_WAIT_TIMEOUT_S


@dataclass(frozen=True)
class _MemberRun:
    """A child run this archive started.

    ``run_id`` is the Hatchet workflow run id (what ``_await_runs`` polls).
    ``progress_run_id`` is the member's own silver.ingest_progress row,
    written before dispatch by ``_dispatch_member``.
    """

    name: str
    run_id: str
    progress_run_id: str | None = None


@dataclass
class _WaitOutcome:
    finished: list[_MemberRun]
    #: Reached a terminal state that is not COMPLETED, with that state.
    not_completed: list[tuple[_MemberRun, str]]
    #: Still queued / running (or unreadable) when the deadline passed.
    pending: list[_MemberRun]
    waited_s: float


async def _run_status(run_id: str) -> str | None:
    """Hatchet's status for one run, upper-cased, or None when unreadable.

    Same call stale_run_detector makes. None (unreachable engine, a run past
    retention) is treated as "not finished yet" by the caller, so an API
    outage degrades to the timeout path rather than to a wrong "done".
    """
    try:
        status = await hatchet.runs.aio_get_status(run_id)
    except Exception as exc:  # noqa: BLE001 — any API failure means "unknown"
        log.warning("ingest_zip_archive: could not read status of run %s: %s", run_id, exc)
        return None
    raw = getattr(status, "value", status)
    return str(raw).upper() if raw is not None else None


def _track_run(
    dispatched: list[_MemberRun] | None,
    name: str,
    ref: Any,
    progress_run_id: str | None = None,
) -> None:
    """Remember a child run so the archive can wait for it."""
    if dispatched is None:
        return
    run_id = getattr(ref, "workflow_run_id", None)
    if run_id:
        dispatched.append(
            _MemberRun(name=name, run_id=str(run_id), progress_run_id=progress_run_id),
        )


async def _dispatch_member(
    workflow: Any,
    payload: Any,
    *,
    archive: IngestZipArchiveInput,
    member_name: str,
    children: list[_MemberRun] | None,
) -> tuple[Any, str]:
    """Dispatch one extracted member with its own progress row (HAT-4).

    Every branch used to call ``aio_run_no_wait`` bare: no progress row, no
    run_id. A PDF, TIFF or spatial member that Hatchet cancelled before its
    body ran (schedule_timeout, a worker kill) therefore left no
    ingest_progress row at all. The archive still counted it as dispatched,
    and nothing could retry it, because the stale sweep and nightly Tier 1
    both work from progress rows. That is the Cameco failure mode
    ``shadow_trigger`` has closed for direct uploads since 2026-06-02.

    Tabular members had the other half of the problem. With no run_id,
    ingest_tabular minted a fresh id per ATTEMPT, so a killed attempt 1 left
    a ``started`` row the stale sweep later timed out and re-dispatched, a
    third ingest of a file whose retry had already succeeded.

    So, per member: mint a run_id, write the queued row under it, pass it
    in the input where the workflow takes one (tabular / spatial /
    well_logs upsert the same row on every attempt; ingest_pdf and
    tiff_normalize adopt it through ``lookup_active_run_id``), dispatch,
    then stamp the Hatchet id so the stale sweep can ask the engine whether
    the run is alive.

    Returns ``(ref, progress_run_id)``.
    """
    progress_run_id = str(uuid.uuid4())
    if "run_id" in type(payload).model_fields:
        payload = payload.model_copy(update={"run_id": progress_run_id})
    recorded = await ingest_progress.start_run(
        workspace_id=archive.workspace_id,
        project_id=archive.project_id,
        minio_key=payload.minio_key,
        triggered_by="upload",
        run_id=progress_run_id,
    )
    try:
        ref = await workflow.aio_run_no_wait(payload)
    except BaseException:
        if recorded:
            await ingest_progress.release_undispatched(run_id=progress_run_id)
        raise
    await ingest_progress.stamp_workflow_run_id(
        run_id=progress_run_id,
        workflow_run_id=getattr(ref, "workflow_run_id", None),
    )
    _track_run(children, member_name, ref, progress_run_id)
    return ref, progress_run_id


async def _await_runs(
    runs: list[_MemberRun],
    *,
    timeout_s: float,
    poll_s: float = _DEFAULT_WAIT_POLL_S,
    status_fn: Callable[[str], Awaitable[str | None]] | None = None,
    on_tick: Callable[[int, int], Awaitable[None]] | None = None,
) -> _WaitOutcome:
    """Poll ``runs`` until each is terminal or ``timeout_s`` elapses.

    Never raises for a run's own outcome: FAILED / CANCELLED come back in
    ``not_completed`` and unfinished ones in ``pending``, and the caller
    decides what to tell the operator. ``status_fn`` is looked up at call
    time so tests can replace ``_run_status``.
    """
    read_status = status_fn or _run_status
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + timeout_s
    gate = asyncio.Semaphore(_STATUS_CONCURRENCY)

    async def _one(run: _MemberRun) -> tuple[_MemberRun, str | None]:
        async with gate:
            return run, await read_status(run.run_id)

    pending = list(runs)
    finished: list[_MemberRun] = []
    not_completed: list[tuple[_MemberRun, str]] = []
    while pending:
        still: list[_MemberRun] = []
        for run, status in await asyncio.gather(*(_one(r) for r in pending)):
            if status is None or status not in _TERMINAL_RUN_STATUSES:
                still.append(run)
            elif status == "COMPLETED":
                finished.append(run)
            else:
                not_completed.append((run, status))
        pending = still
        if not pending:
            break
        if on_tick is not None:
            await on_tick(len(runs) - len(pending), len(runs))
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(poll_s, remaining))
    return _WaitOutcome(
        finished=finished,
        not_completed=not_completed,
        pending=pending,
        waited_s=loop.time() - started,
    )


def _sniff_sheet_types(path: Path, ext: str) -> set[str] | None:
    """Which drill tables (collar/survey/lithology/sample/structure/alteration/mineralization) a member holds.

    Uses the classifiers ingest_tabular itself uses, so the phase a member is
    put in and the table it is later written to cannot disagree. Returns None
    when the file cannot be sniffed; the caller treats that as "unknown".
    Blocking (file I/O) -- call through ``asyncio.to_thread``.
    """
    try:
        if ext in _SNIFFED_CSV_EXTS:
            from georag_geoparsers._sheet_classifier import (  # noqa: PLC0415
                classify_sheet_type,
            )

            from app.hatchet_workflows.ingest_tabular import _csv_headers  # noqa: PLC0415

            sheet_type, _conf = classify_sheet_type(_csv_headers(str(path)))
            return {sheet_type} & _WRITE_SHEET_TYPES
        if ext in _SNIFFED_WORKBOOK_EXTS:
            from georag_geoparsers.xlsx_parser import enumerate_sheets  # noqa: PLC0415

            return {
                meta.sheet_type for meta in enumerate_sheets(str(path))
                if meta.row_count
            } & _WRITE_SHEET_TYPES
        if ext in _SNIFFED_DBASE_EXTS or ext in _SNIFFED_ACCESS_EXTS:
            from app.hatchet_workflows.ingest_tabular import (  # noqa: PLC0415
                MAPINFO_DAT_EXTENSIONS,
                _read_dbf_table,
                _read_mapinfo_dat_table,
                _typed_verdict_for_table,
            )

            if ext in _SNIFFED_ACCESS_EXTS:
                from georag_geoparsers.access_mdb import (  # noqa: PLC0415
                    list_tables,
                    read_table,
                )

                tables = [read_table(str(path), name) for name in list_tables(str(path))]
            else:
                reader = (
                    _read_mapinfo_dat_table
                    if f".{ext}" in MAPINFO_DAT_EXTENSIONS
                    else _read_dbf_table
                )
                tables = [reader(str(path))]
            # dbase_side_writes as ingest_tabular applies it: a Discover trace
            # export or surface-geochem table goes to its own writer, which
            # needs no existing collar, so it must not be deferred.
            return {
                _typed_verdict_for_table(rows, dbase_side_writes=True)[0]
                for rows in tables if rows
            } & _WRITE_SHEET_TYPES
    except Exception as exc:  # noqa: BLE001 — a failed sniff must never fail the member
        log.warning(
            "ingest_zip_archive: could not sniff %s (%s); dispatching it in "
            "phase 1", path.name, exc,
        )
    return None


async def _member_phase(file_path: Path, ext: str) -> int:
    """Phase a member is dispatched in (see the module docstring).

    Deferred to phase 2: LAS (attaches curves to existing collars), and a
    csv/tsv/workbook, standalone dBASE/DAT table or Access database whose
    sniffed tables are ALL collar-dependent types. Everything else --
    including anything unsniffable -- is phase 1.
    """
    if ext == "las":
        return _PHASE_DEPENDENTS
    sniffable = ext in _SNIFFED_CSV_EXTS | _SNIFFED_WORKBOOK_EXTS | _SNIFFED_ACCESS_EXTS or (
        # A .dbf beside a same-stem .shp is that shapefile's sidecar and
        # travels with it; only a standalone table is its own member.
        ext in _SNIFFED_DBASE_EXTS and not _has_sibling(file_path, ".shp")
    )
    if sniffable:
        types = await asyncio.to_thread(_sniff_sheet_types, file_path, ext)
        if types and "collar" not in types and types <= _INTERVAL_SHEET_TYPES:
            return _PHASE_DEPENDENTS
    return _PHASE_PRODUCERS


async def _split_into_phases(files: list[Path]) -> tuple[list[Path], list[Path]]:
    """(phase-1 files, phase-2 files), each in the original order."""
    phase1: list[Path] = []
    phase2: list[Path] = []
    for path in files:
        ext = path.suffix.lower().lstrip(".")
        if await _member_phase(path, ext) == _PHASE_DEPENDENTS:
            phase2.append(path)
        else:
            phase1.append(path)
    return phase1, phase2


def _wait_warnings(
    outcome: _WaitOutcome, *, waiting_for: str, consequence: str,
) -> list[dict[str, str]]:
    """Archive warnings for a dependency wait that did not end cleanly."""
    out: list[dict[str, str]] = []
    if outcome.pending:
        out.append({
            "code": "archive_dependency_wait_timeout",
            "detail": (
                f"{len(outcome.pending)} of "
                f"{len(outcome.finished) + len(outcome.not_completed) + len(outcome.pending)} "
                f"{waiting_for} had not finished after {int(outcome.waited_s // 60)} min "
                f"({_names([r.name for r in outcome.pending])}), so {consequence} "
                "ran without waiting for them."
            ),
        })
    if outcome.not_completed:
        out.append({
            "code": "archive_member_run_not_completed",
            "detail": (
                f"{len(outcome.not_completed)} {waiting_for} ended "
                f"FAILED or CANCELLED ({_names([r.name for r, _ in outcome.not_completed])}); "
                f"{consequence} may find holes missing. See each run's own row."
            ),
        })
    return out


#: Archive-level wording for the per-member LAS warnings; ``{n}`` is the file
#: count and ``{names}`` the capped file list. One warning per code, never one
#: per file (a 400-file archive would otherwise bury the row).
_MEMBER_WARNING_TEXT: dict[str, str] = {
    "las_collar_unlocated": (
        "{n} LAS file(s) have no collar in this project and no usable coordinates "
        "in the header ({names}). Their curves are KEPT and will attach "
        "automatically when each hole's collar is uploaded."
    ),
    "archive_member_failed": (
        "{n} archive member(s) could not be extracted ({names}); they were "
        "skipped and the rest of the archive was processed. The original "
        "archive stays in bronze; re-export the damaged files and upload them "
        "on their own."
    ),
    "archive_member_encrypted": (
        "{n} archive member(s) are password-protected ({names}); they were "
        "skipped. Remove the password and upload them again."
    ),
    "archive_nested_zip_not_expanded": (
        "{n} zip file(s) inside the archive could not be unpacked ({names}); "
        "they were left as they are. Each stays in the original archive, which "
        "is kept in bronze; upload it on its own to retry."
    ),
    "las_pending_not_kept": (
        "{n} LAS file(s) had no collar and could NOT be kept for later ({names}); "
        "their curves were not loaded. Upload the collar table, then upload these "
        "LAS files again."
    ),
    "log_collar_crs_undeclared": (
        "{n} binary .log file(s) were NOT loaded: their E=/N= coordinates state "
        "no CRS (the format uses NAD83 / Wyoming East, EPSG:32155) and none was "
        "declared for this upload ({names}). Upload again with that CRS declared "
        "if it is correct, or load the collars from a collar table."
    ),
    "las_collar_crs_assumed": (
        "{n} LAS well(s) had header coordinates with no stated CRS; the "
        "project's CRS or WGS84 was assumed ({names})."
    ),
    "las_invalid_stop_depth": (
        "{n} LAS file(s) have no positive STOP (bottom depth) in the ~WELL "
        "section ({names}); their curves were loaded without a total depth. "
        "Correct STOP and upload them again to record one."
    ),
}


#: Member-warning codes meaning a member never reached the disk.
_MEMBER_EXTRACT_CODES = frozenset({"archive_member_failed", "archive_member_encrypted"})


def _member_warning_summaries(
    member_warnings: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Collapse per-file member warnings to one archive warning per code."""
    by_code: dict[str, list[dict[str, str]]] = {}
    for w in member_warnings:
        by_code.setdefault(str(w.get("code") or "member_warning"), []).append(w)
    out: list[dict[str, str]] = []
    for code, items in by_code.items():
        names = [
            f"{i.get('file') or '?'} [{i['reason']}]" if i.get("reason")
            else str(i.get("file") or "?")
            for i in items
        ]
        template = _MEMBER_WARNING_TEXT.get(code)
        if template is None:
            detail = str(items[0].get("detail") or code)
        else:
            detail = template.format(n=len(items), names=_names(names))
        out.append({"code": code, "detail": detail})
    return out


def _derive_warnings(summary: dict[str, Any] | None) -> list[dict[str, str]]:
    """ONE archive warning describing what derive_intervals did not do."""
    if not summary:
        return []
    if summary.get("error"):
        return [{
            "code": "derive_intervals_failed",
            "detail": (
                "Deriving lithology strips from the LAS curves failed "
                f"({summary['error']}). The LAS curves themselves loaded."
            ),
        }]
    if summary.get("skipped_reason") == "commodity_not_uranium":
        commodity = summary.get("commodity")
        stated = f"is {commodity!r}" if commodity else "is not set"
        return [{
            "code": "derive_intervals_skipped",
            "detail": (
                f"No lithology was derived from the LAS gamma curves: the project's "
                f"commodity {stated}, and the derivation rules are Wyoming roll-front "
                "uranium thresholds. If this is a uranium project, set its commodity "
                "to uranium and upload the archive again."
            ),
        }]
    logged = int(summary.get("collars_skipped_logged_lithology") or 0)
    if logged:
        return [{
            "code": "derive_intervals_skipped",
            "detail": (
                f"{logged} hole(s) already have logged lithology, so none was derived "
                "from their gamma curves; any strip derived earlier for them was removed."
            ),
        }]
    return []


# ---------------------------------------------------------------------------
# Extraction: safe, nested archives, geodatabase folders
# ---------------------------------------------------------------------------

_MAX_ENTRIES = 50_000
_MAX_TOTAL_UNCOMPRESSED = 5 * 1024 ** 3  # 5 GiB
#: How many zips deep a delivery may nest before the inner one is left alone.
_MAX_NESTED_DEPTH = 3


class _ArchiveBudgetExceeded(ValueError):
    """The archive (outer plus nested) outgrew a zip-bomb cap.

    A ValueError so the nested-archive handler, which already catches it,
    leaves the inner zip in place; the OUTER extraction lets it propagate and
    the run fails with this message rather than filling the worker's disk.
    """


class _ExtractBudget:
    """Entry and byte totals shared by the outer zip and every nested one.

    ``bytes`` counts bytes ACTUALLY written, not the sizes the central
    directory declares: those are attacker-controlled and a forged header
    would otherwise defeat the cap.
    """

    def __init__(self) -> None:
        self.entries = 0
        self.bytes = 0


#: Copy granularity. Small enough that a bomb is caught within one chunk of
#: crossing the cap, large enough that a 5 GiB extract is not syscall-bound.
_COPY_CHUNK = 1024 * 1024

_ZIP_ENCRYPTED_FLAG = 0x1

#: Longest member name echoed into a warning (names come from the archive).
_MEMBER_NAME_MAX = 200


def _member_warning(code: str, name: str, exc: BaseException) -> dict[str, str]:
    """A per-member extraction warning: name plus exception CLASS, no free text.

    The exception message is deliberately not carried: zipfile messages embed
    archive-controlled strings and ``detail`` ends up in a toast.
    """
    shown = name[:_MEMBER_NAME_MAX]
    reason = type(exc).__name__
    if code == "archive_member_encrypted":
        detail = f"{shown}: encrypted, password required"
    else:
        detail = f"{shown}: could not be extracted ({reason})"
    return {"code": code, "file": shown, "reason": reason, "detail": detail}


def _copy_member(
    zf: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path, budget: _ExtractBudget,
) -> None:
    """Copy one member to ``dest``, counting every byte against the budget.

    Raises ``_ArchiveBudgetExceeded`` the moment the running total passes
    ``_MAX_TOTAL_UNCOMPRESSED`` whatever the header declared.
    """
    with zf.open(info) as src, open(dest, "wb") as out:
        while True:
            chunk = src.read(_COPY_CHUNK)
            if not chunk:
                return
            budget.bytes += len(chunk)
            if budget.bytes > _MAX_TOTAL_UNCOMPRESSED:
                raise _ArchiveBudgetExceeded(
                    f"ingest_zip_archive: extracted more than "
                    f"{_MAX_TOTAL_UNCOMPRESSED} B while copying "
                    f"{info.filename[:_MEMBER_NAME_MAX]!r} (zip-bomb guard); "
                    "refusing. The central directory under-declared the size."
                )
            out.write(chunk)


def _extract_zip_into(
    zip_path: Path, dest_dir: Path, budget: _ExtractBudget,
) -> list[dict[str, str]]:
    """Extract ``zip_path`` into ``dest_dir`` under the shared ``budget``.

    Audit 2026-06-28: safe extraction. A bare zf.extractall() is vulnerable to
    (a) zip-bombs (unbounded decompressed size / entry count exhausts disk) and
    (b) zip-slip path traversal (an entry named '../../etc/x' escapes the
    destination). Guard both: cap entry count + total uncompressed size, and
    verify every resolved destination stays inside ``dest_dir`` before
    writing. Raises ValueError on a breach.

    The size cap is checked twice: against the declared central-directory
    sizes (cheap early refusal) and against the bytes actually copied, since
    the declared sizes are forgeable.

    One bad MEMBER does not abort the archive: a corrupt, CRC-failing,
    unsupported-compression or encrypted member is skipped and returned as an
    ``archive_member_failed`` / ``archive_member_encrypted`` warning, and the
    rest are extracted. A 400-file delivery must not die on file 12.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    root = dest_dir.resolve()
    warnings: list[dict[str, str]] = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        infos = zf.infolist()
        if budget.entries + len(infos) > _MAX_ENTRIES:
            raise ValueError(
                f"ingest_zip_archive: {budget.entries + len(infos)} entries exceeds "
                f"{_MAX_ENTRIES} (zip-bomb guard); refusing."
            )
        declared = sum(i.file_size for i in infos)
        if budget.bytes + declared > _MAX_TOTAL_UNCOMPRESSED:
            raise _ArchiveBudgetExceeded(
                f"ingest_zip_archive: uncompressed size "
                f"{budget.bytes + declared} B exceeds "
                f"{_MAX_TOTAL_UNCOMPRESSED} B (zip-bomb guard); refusing."
            )
        # Validate every path BEFORE writing any of them, so a slip aborts a
        # nested archive cleanly instead of leaving half of it on disk.
        targets: list[tuple[zipfile.ZipInfo, Path]] = []
        for info in infos:
            if info.is_dir():
                continue
            dest = (dest_dir / info.filename).resolve()
            if dest != root and not str(dest).startswith(str(root) + os.sep):
                raise ValueError(
                    f"ingest_zip_archive: unsafe path {info.filename!r} "
                    "escapes extract dir (zip-slip guard); refusing."
                )
            targets.append((info, dest))
        budget.entries += len(infos)
        for info, dest in targets:
            try:
                if info.flag_bits & _ZIP_ENCRYPTED_FLAG:
                    raise RuntimeError("encrypted")
                dest.parent.mkdir(parents=True, exist_ok=True)
                _copy_member(zf, info, dest, budget)
            except _ArchiveBudgetExceeded:
                dest.unlink(missing_ok=True)
                raise
            except (
                zipfile.BadZipFile, RuntimeError, EOFError, zlib.error,
                NotImplementedError, OSError,
            ) as exc:
                if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                    raise  # disk full is the worker's problem, not the member's
                dest.unlink(missing_ok=True)
                encrypted = isinstance(exc, RuntimeError) and (
                    info.flag_bits & _ZIP_ENCRYPTED_FLAG
                )
                warnings.append(_member_warning(
                    "archive_member_encrypted" if encrypted
                    else "archive_member_failed",
                    info.filename, exc,
                ))
                log.warning(
                    "ingest_zip_archive: member %r not extracted (%s); continuing",
                    info.filename[:_MEMBER_NAME_MAX], type(exc).__name__,
                )
    return warnings


def _is_junk(path: Path, root: Path) -> bool:
    """macOS AppleDouble forks, which mirror the real file names."""
    rel = path.relative_to(root).parts
    return "__MACOSX" in rel or path.name.startswith("._")


def _expand_nested_archives(root: Path, budget: _ExtractBudget) -> list[dict[str, str]]:
    """Unpack every ``.zip`` inside ``root`` in place, up to ``_MAX_NESTED_DEPTH``.

    A nested archive is expanded into a sibling directory named after it and
    the ``.zip`` itself is removed, so its members are ingested like any other
    and the zip is not reported as an unhandled member. One that cannot be
    expanded (corrupt, over the shared budget, too deep) is LEFT IN PLACE and
    named in a warning: it stays visible as an unhandled member rather than
    vanishing. Never raises for a single nested archive.
    """
    warnings: list[dict[str, str]] = []
    failed: set[Path] = set()  # left in place; must not be retried every pass
    depth = 0
    while depth < _MAX_NESTED_DEPTH:
        nested = sorted(
            p for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() == ".zip"
            and not _is_junk(p, root) and p not in failed
        )
        if not nested:
            return warnings
        depth += 1
        for zpath in nested:
            target = zpath.parent / f"{zpath.stem}__unzipped"
            try:
                warnings.extend(_extract_zip_into(zpath, target, budget))
            except (zipfile.BadZipFile, ValueError, OSError, RuntimeError) as exc:
                failed.add(zpath)
                # Whatever a refused nested archive wrote before the breach is
                # not a member: drop it so the .zip is the one thing left.
                shutil.rmtree(target, ignore_errors=True)
                warnings.append({
                    "code": "archive_nested_zip_not_expanded",
                    "file": zpath.name,
                    "detail": (
                        f"{zpath.name}: a zip inside the archive could not be unpacked "
                        f"({exc}); it was left as it is."
                    ),
                })
                log.warning("ingest_zip_archive: nested zip %s not expanded: %s", zpath.name, exc)
                continue
            zpath.unlink()
    leftovers = [
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() == ".zip"
        and not _is_junk(p, root) and p not in failed
    ]
    for zpath in leftovers:
        warnings.append({
            "code": "archive_nested_zip_not_expanded",
            "file": zpath.name,
            "detail": (
                f"{zpath.name}: nested more than {_MAX_NESTED_DEPTH} zips deep; "
                "it was left as it is."
            ),
        })
    return warnings


def _collect_members(root: Path) -> list[Path]:
    """Every ingestible member under ``root``, in filesystem order.

    An Esri File Geodatabase is a DIRECTORY (``name.gdb``) whose contents
    (a00000001.gdbtable, ...) match no extension and are not members in their
    own right: the folder is the member, returned once as a directory, and its
    files are left out. AppleDouble junk is dropped.
    """
    gdb_dirs = [
        p for p in root.rglob("*")
        if p.is_dir() and p.suffix.lower() == ".gdb" and not _is_junk(p, root)
    ]
    members: list[Path] = []
    for p in root.rglob("*"):
        if _is_junk(p, root):
            continue
        if p.is_file() and not any(g in p.parents for g in gdb_dirs):
            members.append(p)
    return [*members, *gdb_dirs]


# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------

ingest_zip_archive = hatchet.workflow(
    name="ingest_zip_archive",
    input_validator=IngestZipArchiveInput,
)


# HAT-3 (2026-09-29): schedule_timeout matches ingest_pdf. Hatchet's
# 5-minute default cancelled a queued archive silently behind long tasks.
@ingest_zip_archive.task(execution_timeout="4h", schedule_timeout="2h", retries=0)
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

    # The archive's OWN silver.ingest_progress row. The trigger endpoint
    # inserts it at dispatch time (status 'queued', step 0 of 5) under the
    # run_id Laravel minted, so a ZIP that Hatchet cancels before this body
    # runs is still visible on the Ingestion Runs page. Until 2026-09-02
    # nothing here ever touched that row again: the workflow tracked itself
    # in silver.archive_ingest_runs only, so every ZIP upload left a queued
    # ingest_progress row behind, and 15 minutes later the stale sweep
    # closed it as ``timed_out`` / ``stale_heartbeat`` — a red "Failed
    # (step 0 of 5)" beside a child run that had completed. start_run is an
    # upsert, so this is also the creation path for a dispatch that skipped
    # the trigger endpoint.
    progress_run_id = await ingest_progress.start_run(
        workspace_id=input.workspace_id,
        project_id=input.project_id,
        minio_key=input.minio_key,
        triggered_by="upload",
        workflow_run_id=getattr(ctx, "workflow_run_id", None),
        run_id=input.run_id,
    )

    async with (
        _archive_progress.archive_lifecycle(
            workspace_id=input.workspace_id,
            project_id=input.project_id,
            minio_key=input.minio_key,
            run_id=input.run_id,
            triggered_by="upload",
            workflow_run_id=getattr(ctx, "workflow_run_id", None),
        ) as archive_run_id,
        _progress_row_lifecycle(progress_run_id),
        # A multi-GB archive downloads and extracts for longer than the
        # sweep's 15-minute window with no stage transition in between;
        # the ticker is what keeps the row's heartbeat fresh meanwhile.
        ingest_progress.heartbeat_loop(run_id=progress_run_id),
    ):
        # ── 1. Download ZIP to a temp directory ──────────────────────────────
        with tempfile.TemporaryDirectory(prefix="georag_zip_") as tmpdir:
            zip_path = Path(tmpdir) / "archive.zip"

            log.info("ingest_zip_archive: downloading %s", input.minio_key)
            if archive_run_id:
                await _archive_progress.mark_extracting(archive_run_id=archive_run_id)
            if progress_run_id:
                await ingest_progress.mark_stage_started(
                    run_id=progress_run_id, stage="preflight",
                )
            # Hard rule 2 — boto3 is sync; keep it off the asyncio event loop.
            await asyncio.to_thread(store.get_file, Bucket.BRONZE, input.minio_key, str(zip_path))

            # ── 2. Extract all entries ────────────────────────────────────────
            extract_dir = Path(tmpdir) / "extracted"
            extract_dir.mkdir()
            if progress_run_id:
                await ingest_progress.mark_stage_started(
                    run_id=progress_run_id, stage="parse",
                )

            # Hard rule 2. Extraction is CPU-bound zlib plus disk I/O with no
            # await point anywhere in it — on a 5 GiB archive that is minutes
            # of blocking on the worker's event loop, during which Hatchet's
            # heartbeat cannot fire. The engine then marks the worker dead
            # and cancels every OTHER in-flight task on it: a concurrent PDF
            # parse, an embed sweep. The download two lines up was already
            # wrapped for exactly this reason; the extraction next to it was
            # not. Same failure the subprocess pool in ingest_pdf.py exists
            # to avoid, reintroduced in the sibling workflow.
            #
            # _extract_zip_into carries the 2026-06-28 audit guards (entry cap,
            # total-size cap, zip-slip) and shares ONE budget with every nested
            # archive, so a zip-of-zips cannot multiply past them.
            budget = _ExtractBudget()
            extract_warnings = await asyncio.to_thread(
                _extract_zip_into, zip_path, extract_dir, budget,
            )
            nested_warnings = [
                *extract_warnings,
                *await asyncio.to_thread(
                    _expand_nested_archives, extract_dir, budget,
                ),
            ]
            #: Members that never reached the disk. They are not in
            #: ``total`` or ``counts`` — the archive can only be `partial`.
            extract_failed = sum(
                1 for w in nested_warnings
                if w.get("code") in _MEMBER_EXTRACT_CODES
            )

            all_files = _collect_members(extract_dir)
            # DCIP2D export directories travel as ONE member each (ING-19):
            # their files mean nothing alone, so they are claimed here and
            # never reach the per-extension routing below.
            dcip_exports, all_files = await asyncio.to_thread(
                find_dcip_exports, all_files,
            )
            for export in dcip_exports:
                log.info(
                    "ingest_zip_archive: DCIP2D export %s claims %d file(s)",
                    relative_dir(export, extract_dir), len(export.members),
                )
            total = len(all_files) + len(dcip_exports)
            log.info("ingest_zip_archive: extracted %d files run_id=%s", total, input.run_id)
            if archive_run_id:
                await _archive_progress.mark_fanning_out(
                    archive_run_id=archive_run_id, file_count=total,
                )
            if progress_run_id:
                await ingest_progress.mark_stage_started(
                    run_id=progress_run_id, stage="persist",
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
                counts: dict[str, int] = dict.fromkeys(_COUNT_KEYS, 0)
                errors: list[dict[str, str]] = []
                unhandled: list[str] = []
                #: Per-file LAS warnings, collapsed to one per code at the end.
                member_warnings: list[dict[str, str]] = list(nested_warnings)
                #: Dependency-wait warnings (timeouts, failed collar runs).
                wait_warnings: list[dict[str, str]] = []
                #: Every ingest_tabular child run started, in dispatch order.
                dispatched_runs: list[_MemberRun] = []
                #: Every child run of ANY workflow, for the summary (HAT-4).
                member_runs: list[_MemberRun] = []

                # Collar producers first, dependents (interval tables, LAS)
                # after the producers' runs have finished — module docstring.
                producers, dependents = await _split_into_phases(all_files)
                ordered_files = [*producers, *dependents]
                first_dependent_idx = len(producers) + 1
                #: How many of ``dispatched_runs`` were phase 1; the rest are
                #: the interval-table runs the derive step waits for. Stays
                #: at the full length when there is no phase 2.
                phase1_run_count = 10**9
                log.info(
                    "ingest_zip_archive: dispatch plan run_id=%s phase1=%d "
                    "phase2=%d (%s)",
                    input.run_id, len(producers), len(dependents),
                    _names([p.name for p in dependents]),
                )

                async def _tick(done: int, of: int) -> None:
                    # Doubles as a heartbeat while the archive sits in the wait.
                    if progress_run_id:
                        await ingest_progress.mark_stage_progress(
                            run_id=progress_run_id,
                            stage_pct=(first_dependent_idx - 1) / total,
                            stage_detail=(
                                f"waiting for {of - done} of {of} "
                                "collar-bearing member run(s) before "
                                "loading interval tables and LAS files"
                            ),
                        )

                # DCIP2D exports first: they need no collar, and each one
                # failing costs that export only, like any other member.
                for export in dcip_exports:
                    try:
                        await _dispatch_dcip_export(
                            export, root=extract_dir, store=store, input=input,
                            children=member_runs,
                        )
                        counts["geophysics"] += 1
                        if archive_run_id:
                            await _archive_progress.increment_counts(
                                archive_run_id=archive_run_id, succeeded=1,
                            )
                    except Exception as exc:
                        counts["errors"] += 1
                        errors.append({
                            "file": export.directory.name, "ext": "dcip2d",
                            "error": str(exc),
                        })
                        log.warning(
                            "ingest_zip_archive: DCIP2D export %s failed — %s (continuing)",
                            export.directory.name, exc,
                        )
                        if archive_run_id:
                            await _archive_progress.increment_counts(
                                archive_run_id=archive_run_id, failed=1,
                            )

                for idx, file_path in enumerate(ordered_files, start=1):
                    ext = file_path.suffix.lower().lstrip(".")
                    if idx == first_dependent_idx:
                        phase1_run_count = len(dispatched_runs)
                    if idx == first_dependent_idx and dispatched_runs:
                        # Phase 1 is out. Its tabular runs are what create
                        # the collars phase 2 attaches to, so wait for them.
                        wait1 = await _await_runs(
                            list(dispatched_runs), timeout_s=_wait_timeout_s(),
                            on_tick=_tick,
                        )
                        wait_warnings.extend(_wait_warnings(
                            wait1,
                            waiting_for="collar-bearing member run(s)",
                            consequence="the interval tables and LAS files",
                        ))
                        log.info(
                            "ingest_zip_archive: phase-1 wait done run_id=%s "
                            "finished=%d not_completed=%d pending=%d waited=%.0fs",
                            input.run_id, len(wait1.finished),
                            len(wait1.not_completed), len(wait1.pending),
                            wait1.waited_s,
                        )
                    if idx == first_dependent_idx:
                        # Collars now exist that earlier uploads' LAS files
                        # were waiting for (this archive's own .log headers and
                        # phase-1 tables). Attach those before phase 2.
                        await _attach_waiting_las(conn, store, input)
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
                            dispatched=dispatched_runs,
                            member_warnings=member_warnings,
                            children=member_runs,
                            archive_root=extract_dir,
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

                        if counts["unknown"] != before_unknown:
                            unhandled.append(file_path.name)

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

                    if idx % 10 == 0 or idx == total:
                        log.info(
                            "ingest_zip_archive: progress %d/%d run_id=%s counts=%s",
                            idx,
                            total,
                            input.run_id,
                            counts,
                        )
                        if progress_run_id:
                            # Doubles as a heartbeat; the bar on the
                            # Ingestion Runs page moves with the fan-out.
                            await ingest_progress.mark_stage_progress(
                                run_id=progress_run_id,
                                stage_pct=idx / total,
                                stage_detail=(
                                    f"{idx}/{total} member files handed to "
                                    "their ingesters"
                                ),
                            )

                # The last members may have been collar sources (.log headers).
                await _attach_waiting_las(conn, store, input)

            finally:
                await conn.close()

        # ── 5. Derive lithology / interval strip logs from the LAS curves ──
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
        # derive_project is itself gated (derive_intervals module docstring): it
        # only derives for a URANIUM project, and only for holes with no LOGGED
        # lithology. Those checks read silver.lithology_logs, which the phase-2
        # interval tables write from child runs -- so those runs are waited for
        # first (bounded, same timeout as the phase-1 wait), or the "already
        # logged" test would race the very upload that makes it true.
        #
        # What it skips is reported ONCE, as a single archive warning, never per
        # hole -- see _derive_warnings.
        #
        # Runs once per archive rather than per file — derive_project sweeps every
        # collar in the project, and its writes are idempotent: each collar's
        # DERIVED-% lithology rows, derived_composite samples and 'lithology'
        # interval rows are deleted and re-emitted. A re-run, or an archive that
        # only adds some of a project's holes, simply recomputes from whatever
        # curves are present.
        #
        # A failure here must not fail the archive. Every file is already ingested
        # by this point, so the error is recorded in the summary and left for the
        # next run to correct — same "one bad step doesn't kill the run" posture
        # as the per-file loop. It runs BEFORE the terminal marks below so its
        # warning reaches the archive's row and the completion toast.
        derive_intervals_summary: dict[str, Any] | None = None
        if counts["las"] > 0:
            from app.services.ingest.derive_intervals import derive_project  # noqa: PLC0415

            phase2_runs = dispatched_runs[phase1_run_count:]
            if phase2_runs:
                wait2 = await _await_runs(phase2_runs, timeout_s=_wait_timeout_s())
                wait_warnings.extend(_wait_warnings(
                    wait2,
                    waiting_for="interval-table member run(s)",
                    consequence="the lithology derivation",
                ))
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

        # Terminal mark INSIDE the archive_lifecycle — 'partial' when any
        # per-file ingester failed, 'completed' otherwise. archive_lifecycle
        # would mark 'failed' if we raised; we don't (per-file errors are
        # caught + counted above so a single bad LAS doesn't kill the run).
        if archive_run_id:
            terminal_status = (
                "partial" if (counts["errors"] > 0 or extract_failed > 0)
                else "completed"
            )
            terminal_error = None
            if counts["errors"] > 0:
                terminal_error = (
                    f"{counts['errors']} of {total} files failed; see ingest_progress"
                )
            if extract_failed > 0:
                terminal_error = (
                    f"{extract_failed} archive member(s) could not be extracted"
                    + (f"; {terminal_error}" if terminal_error else "; see ingest_progress")
                )
            await _archive_progress.mark_terminal(
                archive_run_id=archive_run_id,
                status=terminal_status,
                error_text=terminal_error,
            )

        # The archive's ingest_progress row closes with the same accounting,
        # and Laravel is told — the same duty every other workflow discharges
        # (tests/test_ingest_completion_reaches_the_ui.py). The members the
        # archive handed off are their own runs and announce their own rows;
        # this broadcast is what flips the archive's line on the Ingestion
        # Runs page and carries its warnings to the toast.
        if progress_run_id:
            dispatched = sum(counts[k] for k in _DISPATCHED_COUNT_KEYS)
            archive_warnings = _archive_warnings(
                total=total, counts=counts, errors=errors, unhandled=unhandled,
                archive_key=input.minio_key,
                extra=[
                    *_member_warning_summaries(member_warnings),
                    *wait_warnings,
                    *_derive_warnings(derive_intervals_summary),
                ],
            )
            transitioned = await ingest_progress.mark_completed_by_run(
                run_id=progress_run_id,
                rows_written=dispatched,
                warnings=archive_warnings,
            )
            if transitioned:
                await ingest_progress.broadcast_terminal(
                    workspace_id=input.workspace_id,
                    project_id=input.project_id,
                    run_id=progress_run_id,
                    stage="persist",
                    status=ingest_progress.terminal_status(
                        rows_written=dispatched, warnings=archive_warnings,
                    ),
                    message=_archive_message(
                        dispatched=dispatched, total=total, warnings=archive_warnings,
                    ),
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
        "dispatch_plan": {"phase1": len(producers), "phase2": len(dependents)},
        # Every child run, whatever its workflow, with its own progress row.
        "member_runs": [
            {
                "name": m.name,
                "workflow_run_id": m.run_id,
                "progress_run_id": m.progress_run_id,
            }
            for m in member_runs
        ],
        "warnings": [
            *_member_warning_summaries(member_warnings),
            *wait_warnings,
            *_derive_warnings(derive_intervals_summary),
        ],
        "completed_at": datetime.now(UTC).isoformat(),
    }
    log.info("ingest_zip_archive.complete run_id=%s summary=%s", input.run_id, counts)
    return summary


@contextlib.asynccontextmanager
async def _progress_row_lifecycle(progress_run_id: str | None):
    """Close the archive's ingest_progress row when the body raises.

    ``archive_lifecycle`` does the same for silver.archive_ingest_runs. The
    error text is the real exception; ``stage`` is left None so the
    conditional update keeps whatever stage ``mark_stage_started`` last
    wrote, which is where the failure happened. The on_failure hook is the
    backstop for the paths that never reach this body at all.
    """
    try:
        yield
    except Exception as exc:  # noqa: BLE001 — explicitly broad, re-raised
        if progress_run_id:
            await ingest_progress.mark_failed_by_run(
                run_id=progress_run_id,
                error=f"{type(exc).__name__}: {exc}"[:1000],
            )
        raise


def _names(items: list[str], limit: int = 5) -> str:
    shown = ", ".join(items[:limit])
    return f"{shown} (+{len(items) - limit} more)" if len(items) > limit else shown


def _archive_message(
    *, dispatched: int, total: int, warnings: list[dict[str, str]],
) -> str:
    """The completion toast for an archive, in the archive's own unit.

    ``_progress.terminal_message`` says "N rows written"; an archive writes
    no rows of its own — it hands member files to their ingesters, and each
    of those reports its rows on its own run. Capped at 500 characters, the
    Laravel endpoint's validation limit on ``message``.
    """
    if total == 0:
        head = "The archive held no files"
    else:
        head = (
            f"{dispatched} of {total} member file(s) handed to their "
            "ingesters; each appears as its own run"
        )
    if not warnings:
        return head[:500]
    first = str(warnings[0].get("detail") or warnings[0].get("code") or "").strip()
    more = f" (+{len(warnings) - 1} more)" if len(warnings) > 1 else ""
    return f"{head} — {first}{more}"[:500]


def _archive_warnings(
    *,
    total: int,
    counts: dict[str, int],
    errors: list[dict[str, str]],
    unhandled: list[str],
    extra: list[dict[str, str]] | None = None,
    archive_key: str | None = None,
) -> list[dict[str, str]]:
    """The archive row's warnings — what did NOT reach an ingester, and why.

    ``extra`` carries the warnings the members and the dependent steps
    produced (LAS location, dependency waits, the derive step); they follow
    the accounting warnings below.

    A member with no handler or one whose ingester declined it used to leave
    no trace outside the worker log: ``unknown`` and ``skipped`` never
    affected the archive's terminal status, so a ZIP of nothing but notes
    closed ``completed`` having ingested nothing. Each is a warning here, so
    the row reads "Finished with warnings" and says which files it means.
    """
    warnings: list[dict[str, str]] = []
    if errors:
        warnings.append({
            "code": "archive_member_failed",
            "detail": (
                f"{len(errors)} of {total} member file(s) could not be handed "
                f"to an ingester: {_names([e['file'] for e in errors])}. The "
                "rest of the archive was processed; each member that was "
                "dispatched appears as its own run."
            ),
        })
    if unhandled:
        warnings.append({
            "code": "archive_member_unhandled",
            "detail": (
                f"{len(unhandled)} member file(s) had no ingester and were "
                f"left out: {_names(unhandled)}. They were NOT loaded, but they "
                "are not lost: the original archive stays in bronze"
                + (f" ({archive_key})" if archive_key else "")
                + ". Upload them on their own under the matching category "
                "if they hold data."
            ),
        })
    if counts.get("skipped"):
        warnings.append({
            "code": "archive_member_skipped",
            "detail": (
                f"{counts['skipped']} member file(s) were opened by their "
                "ingester and declined — an unreadable LAS, a workbook with "
                "no data sheet, a .log without a collar header. The worker "
                "log names each one."
            ),
        })
    warnings.extend(extra or [])
    return warnings


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
    dispatched: list[_MemberRun] | None = None,
    member_warnings: list[dict[str, str]] | None = None,
    children: list[_MemberRun] | None = None,
    archive_root: Path | None = None,
) -> None:
    """Route a single extracted file to its ingester.

    Ingesters are imported lazily inside each branch so that a missing
    optional dep (e.g. ``lasio`` not installed in the ingestion worker
    image) only fails that extension's branch, not the entire workflow.

    ``dispatched`` collects every ingest_tabular child run started, so the
    caller can wait for them; ``member_warnings`` collects per-file warnings
    (LAS location, invalid STOP) for the caller to summarise. Both are
    optional so a bare call still works.

    ``children`` collects EVERY child run started (all workflows), each
    with its own progress row; see ``_dispatch_member``.
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
                source_epsg=input.source_epsg,
            )
        if member_warnings is not None:
            member_warnings.extend(
                {**w, "file": file_path.name} for w in result.warnings
            )
        if result.skipped and result.skipped_reason == "collar_unlocated":
            # No collar and no usable header coordinates: the curves cannot be
            # placed honestly yet. Keep the file in bronze and record it, so it
            # attaches when the hole's collar is written (las_pending.py).
            if await _keep_las_for_later(
                file_path=file_path, hole_id=result.hole_id, conn=conn, store=store,
                input=input,
            ):
                counts["las_pending"] += 1
            else:
                counts["skipped"] += 1
                if member_warnings is not None:
                    member_warnings.append({
                        "code": "las_pending_not_kept",
                        "detail": f"{file_path.name}: could not be kept for later",
                        "file": file_path.name,
                    })
        elif result.skipped:
            counts["skipped"] += 1
            # WARNING, not debug: this is a member that loaded nothing.
            log.warning(
                "ingest_zip_archive: LAS skipped %s — %s",
                file_path.name, result.skipped_reason,
            )
        else:
            counts["las"] += 1

    elif ext == "log":
        # Binary gamma-log files → parse header + upsert collar. The format
        # writes its E=/N= pair in one fixed CRS (NAD83 / Wyoming East, ft) and
        # states none in the file, so it is used ONLY when the operator declared
        # that CRS for the upload (source_epsg); otherwise the member is
        # refused rather than placed in Wyoming whatever project it landed in.
        from app.services.ingest.cameco_log_ingester import (  # noqa: PLC0415
            LOG_COORD_EPSG,
            log_crs_declared,
            parse_cameco_log_header,
            upsert_collar_from_log,
        )

        parsed = parse_cameco_log_header(str(file_path))
        if parsed.skipped:
            counts["skipped"] += 1
            log.debug("ingest_zip_archive: LOG skipped %s — %s", file_path.name, parsed.skipped_reason)
        elif not log_crs_declared(input.source_epsg):
            counts["skipped"] += 1
            log.warning(
                "ingest_zip_archive: LOG %s not loaded — its coordinates carry no CRS "
                "and EPSG:%s (or 3736, the ftUS code) was not declared for this upload",
                file_path.name, LOG_COORD_EPSG,
            )
            if member_warnings is not None:
                member_warnings.append({
                    "code": "log_collar_crs_undeclared",
                    "detail": f"{file_path.name}: coordinates carry no CRS",
                    "file": file_path.name,
                })
        else:
            async with conn.transaction():
                log_collar_id = await upsert_collar_from_log(
                    conn,
                    project_id=input.project_id,
                    parsed=parsed,
                    workspace_id=input.workspace_id,
                    source_epsg=input.source_epsg,
                )
            if log_collar_id is None:
                counts["skipped"] += 1
            else:
                counts["log"] += 1

    # ".txt" goes to ingest_tabular like any delimited table: it classifies the
    # header (a delimited drill table lands typed) and text that matches no
    # drill layout is indexed as searchable passages by its text fallback. A
    # readme therefore lands as text rather than being dropped, which is what
    # the old "no .txt" exclusion did to it and to every drill table shipped as
    # .txt.
    elif ext in ("csv", "tsv", "txt", "xlsx", "xls", "xlsm"):
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
        # ingest_tabular replaces earlier rows by LOGICAL file name
        # (source_file, upload stamp stripped), per hole. Au/assays.csv and
        # Cu/assays.csv share the name "assays.csv", so without a directory tag
        # they shared one source_file and each upload deleted the other's rows,
        # whichever ran last winning - the very thing the per-file scoping was
        # added to stop. The same short, deterministic directory hash the
        # spatial branch applies keeps them distinct (members at the archive
        # root keep their plain name, so flat deliveries are unchanged and a
        # re-upload of the same archive still replaces itself). The tag sits
        # before the extension: ingest_tabular classifies by extension and the
        # logical-name stripper only removes the leading upload stamp.
        tag = _dir_tag(file_path, archive_root)
        safe_name = _safe_filename(f"{file_path.stem}{tag}{file_path.suffix}")
        tabular_key = f"tabular/{input.project_id}/{ts}_{safe_name}"
        await _put_member(store, tabular_key, file_path)
        tabular_ref, tabular_run_id = await _dispatch_member(
            ingest_tabular,
            IngestTabularInput(
                workspace_id=input.workspace_id,
                project_id=input.project_id,
                minio_key=tabular_key,
                # Forwarded, not defaulted: without this the member is
                # written as EPSG:32613 wherever it actually came from.
                source_epsg=input.source_epsg,
            ),
            archive=input, member_name=file_path.name, children=children,
        )
        _track_run(dispatched, file_path.name, tabular_ref, tabular_run_id)
        # F7 (2026-08-11) — throttle the fan-out; see the TIFF branch below
        # (Cameco 529-file GROUP_ROUND_ROBIN saturation).
        await asyncio.sleep(0.25)
        counts["csv" if ext in ("csv", "tsv", "txt") else "xlsx"] += 1

    elif ext in _RASTER_EXTS:
        # TIFF scans → upload to bronze tiff/ prefix + trigger tiff_normalize
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        safe_name = _safe_filename(file_path.name)
        tiff_key = f"tiff/{input.project_id}/{ts}_{safe_name}"
        tiff_size = await _put_member(store, tiff_key, file_path)
        await _dispatch_member(
            tiff_normalize,
            TiffNormalizeInput(
                workspace_id=input.workspace_id,  # type: ignore[arg-type]
                project_id=input.project_id,
                minio_key=tiff_key,
                file_size=tiff_size,
                correlation_token=f"zip-{input.run_id}-{file_path.name}",
            ),
            archive=input, member_name=file_path.name, children=children,
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
        pdf_size = await _put_member(store, pdf_key, file_path)
        await _dispatch_member(
            ingest_pdf,
            IngestPdfInput(
                workspace_id=input.workspace_id,
                project_id=input.project_id,
                minio_key=pdf_key,
                file_size=pdf_size,
                correlation_token=f"zip-{input.run_id}-{file_path.name}",
            ),
            archive=input, member_name=file_path.name, children=children,
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
        # ingest_spatial replaces earlier features by LOGICAL file name, so
        # 2019/faults.shp and 2021/faults.shp must not share one: whichever
        # ran last would delete the other's features, in non-deterministic
        # order. A short hash of the member's directory keeps them distinct
        # (members at the archive root keep their plain name).
        tag = _dir_tag(file_path, archive_root)
        if ext == "shp":
            members = _shapefile_members(file_path)
            # Sidecars take the same tag as the .shp (GDAL finds them by the
            # .shp's exact stem), so all four stay one shapefile.
            members = [
                (f"{file_path.stem}{tag}{Path(arc).suffix}", m) for arc, m in members
            ]
            bundle_path = file_path.parent / f"__bundle_{file_path.stem}.zip"
            def _write_bundle() -> None:
                with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for arcname, m in members:
                        zf.write(m, arcname=arcname)
            await asyncio.to_thread(_write_bundle)
            payload_path = bundle_path
            safe_name = _safe_filename(f"{file_path.stem}{tag}.zip")
        elif file_path.is_dir():
            # An Esri File Geodatabase is a folder. Zip it with its own name as
            # the top-level entry (ingest_spatial's archive path looks for a
            # `<name>.gdb` directory) and hand that off, the same way a
            # shapefile's sidecars are bundled.
            gdb_files = sorted(f for f in file_path.rglob("*") if f.is_file())
            gdb_bundle = file_path.parent / f"__bundle_{file_path.name}.zip"

            def _write_gdb_bundle() -> None:
                with zipfile.ZipFile(gdb_bundle, "w", zipfile.ZIP_DEFLATED) as zf:
                    for f in gdb_files:
                        zf.write(f, arcname=str(Path(file_path.name) / f.relative_to(file_path)))

            await asyncio.to_thread(_write_gdb_bundle)
            payload_path = gdb_bundle
            safe_name = _safe_filename(f"{file_path.stem}{tag}.zip")
        else:
            payload_path = file_path
            safe_name = _safe_filename(f"{file_path.stem}{tag}{file_path.suffix}")

        spatial_key = f"spatial/{input.project_id}/{ts}_{safe_name}"
        try:
            await _put_member(store, spatial_key, payload_path)
        finally:
            if payload_path != file_path:
                payload_path.unlink(missing_ok=True)
        await _dispatch_member(
            ingest_spatial,
            IngestSpatialInput(
                workspace_id=input.workspace_id,
                project_id=input.project_id,
                minio_key=spatial_key,
                source_epsg=input.source_epsg,
            ),
            archive=input, member_name=file_path.name, children=children,
        )
        # F7 (2026-08-11) — throttle the fan-out; see the TIFF branch above
        # (Cameco 529-file GROUP_ROUND_ROBIN saturation).
        await asyncio.sleep(0.25)
        counts["spatial"] += 1

    elif (ext in _DBASE_EXTS and not _has_sibling(file_path, ".shp")) or ext in _ACCESS_EXTS:
        # A dBASE table with NO same-stem .shp beside it is not a sidecar — it
        # is a standalone attribute table, and ingest_tabular reads one
        # directly. It reached the sidecar branch below and was counted as
        # handled, so a ZIP of attribute tables completed having written
        # nothing. Same distinction the import wizard's bundler already makes
        # for a loose .dbf/.dat.
        #
        # safe_name and file_bytes are bound HERE, not reused. Every other
        # branch assigns its own because the branches are mutually exclusive
        # `elif`s — reaching this one proves none of the earlier assignments
        # ran, so referencing theirs raised UnboundLocalError. The per-file
        # try/except swallowed it into counts['errors'], so a delivery ZIP
        # holding a standalone Sitka_trD.DAT produced no bronze object, no
        # ingest_tabular dispatch and no ingest_progress row: the file simply
        # was not there, on the exact format this branch was added for.
        ts = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S_%f")
        safe_name = _safe_filename(file_path.name)
        table_key = f"tables/{input.project_id}/{ts}_{safe_name}"
        await _put_member(store, table_key, file_path)
        table_ref, table_run_id = await _dispatch_member(
            ingest_tabular,
            IngestTabularInput(
                workspace_id=input.workspace_id,
                project_id=input.project_id,
                minio_key=table_key,
                source_epsg=input.source_epsg,
            ),
            archive=input, member_name=file_path.name, children=children,
        )
        _track_run(dispatched, file_path.name, table_ref, table_run_id)
        await asyncio.sleep(0.25)
        counts["tabular"] += 1

    elif ext in _GEOPHYSICS_XYZ_EXTS:
        # Geosoft XYZ line data -> ingest_geophysics (ING-19). Filed under
        # "<archive>/<path in archive>" so a re-upload of the same archive
        # replaces the same survey, and two archives that both hold a
        # `mag.xyz` do not overwrite each other.
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        safe_name = _safe_filename(file_path.name)
        geo_key = f"xyz/{input.project_id}/{ts}_{safe_name}"
        await _put_member(store, geo_key, file_path)
        await _dispatch_member(
            ingest_geophysics,
            IngestGeophysicsInput(
                workspace_id=input.workspace_id,
                project_id=input.project_id,
                minio_key=geo_key,
                source_epsg=input.source_epsg,
                source_name=_member_source_name(input, file_path, archive_root),
            ),
            archive=input, member_name=file_path.name, children=children,
        )
        await asyncio.sleep(0.25)
        counts["geophysics"] += 1

    elif ext in _SHAPEFILE_SIDECAR_EXTS:
        # Absorbed by the .shp branch above. Counted, not "unknown".
        counts["sidecar"] += 1

    else:
        counts["unknown"] += 1
        log.debug("ingest_zip_archive: unknown ext .%s for %s — skipping", ext, file_path.name)


async def _attach_waiting_las(
    conn: asyncpg.Connection,
    store: ObjectStorage,
    input: IngestZipArchiveInput,
) -> None:
    """Ingest LAS files kept from earlier uploads whose collar now exists."""
    from app.services.ingest.las_pending import attach_pending_las  # noqa: PLC0415

    summary = await attach_pending_las(
        conn, store=store, workspace_id=input.workspace_id, project_id=input.project_id,
    )
    if summary.attached:
        log.info(
            "ingest_zip_archive: attached %d waiting LAS file(s) run_id=%s",
            len(summary.attached), input.run_id,
        )


async def _keep_las_for_later(
    *,
    file_path: Path,
    hole_id: str,
    conn: asyncpg.Connection,
    store: ObjectStorage,
    input: IngestZipArchiveInput,
) -> bool:
    """Store an unplaceable LAS in bronze and record it as waiting for its collar.

    Returns False (after logging) if it could not be kept, so the caller can say
    so instead of implying it was.
    """
    from app.services.ingest.las_pending import record_pending  # noqa: PLC0415

    try:
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        key = f"las/{input.project_id}/{ts}_{_safe_filename(file_path.name)}"
        data = await asyncio.to_thread(file_path.read_bytes)
        await asyncio.to_thread(store.put_bytes, Bucket.BRONZE, key, data)
        await record_pending(
            conn,
            workspace_id=input.workspace_id,
            project_id=input.project_id,
            hole_id=hole_id,
            bronze_key=key,
            source_name=file_path.name,
        )
    except Exception as exc:  # noqa: BLE001 — reported by the caller as las_pending_not_kept
        log.warning(
            "ingest_zip_archive: could not keep LAS %s for later: %s", file_path.name, exc,
        )
        return False
    return True


async def _put_member(store: ObjectStorage, key: str, path: Path) -> int:
    """Upload one extracted member to bronze without holding it in memory.

    ING-17: members used to be ``read_bytes()`` then ``put_bytes`` - a whole
    multi-GB GeoTIFF or geodatabase bundle in RAM on a worker that runs many
    slots, while the archive allows 5 GiB uncompressed. ``put_file`` streams
    from disk (boto3 ``upload_file``). Returns the size in bytes.
    """
    size = (await asyncio.to_thread(path.stat)).st_size
    await asyncio.to_thread(store.put_file, Bucket.BRONZE, key, str(path))
    return size


def _shapefile_members(shp: Path) -> list[tuple[str, Path]]:
    """``(name in the bundle, file)`` for a shapefile and its same-stem sidecars.

    ING-16: matched case-INSENSITIVELY, like ``_has_sibling`` below, and
    renamed to the .shp's own stem inside the bundle. ``Veins.SHP`` beside
    ``veins.dbf`` / ``veins.shx`` / ``veins.prj`` used to bundle only the
    .shp - the .dbf was still counted as a handled sidecar - so ingest_spatial
    either failed or refused for want of a CRS, and the attributes were lost.
    GDAL looks the sidecars up by the .shp's exact stem (either extension
    case), which the rename provides. When two files differ only in stem
    case, the exact spelling wins.
    """
    stem = shp.stem.lower()
    chosen: dict[str, Path] = {}
    for sib in sorted(shp.parent.iterdir()):
        if not sib.is_file() or sib.stem.lower() != stem:
            continue
        suffix = sib.suffix.lower()
        if suffix not in chosen or sib.stem == shp.stem:
            chosen[suffix] = sib
    return sorted(
        ((f"{shp.stem}{member.suffix}", member) for member in chosen.values()),
        key=lambda pair: pair[0],
    )


def _dir_tag(path: Path, root: Path | None) -> str:
    """``"__<6 hex>"`` from the member's directory inside the archive, or ``""``.

    Deterministic (a re-upload of the same archive still replaces itself) and
    empty for a member at the archive root, so flat deliveries keep the names
    they were ingested under before. POSIX-normalised, so the tag does not
    depend on the OS the worker runs on.
    """
    if root is None:
        return ""
    try:
        rel = path.parent.relative_to(root).as_posix()
    except ValueError:
        # Outside the archive root (a sidecar resolved elsewhere): no tag.
        log.debug("zip: %s is not under %s; no directory tag", path, root, exc_info=True)
        return ""
    if rel in ("", "."):
        return ""
    return "__" + hashlib.blake2s(rel.encode("utf-8"), digest_size=3).hexdigest()


def _has_sibling(path: Path, suffix: str) -> bool:
    """Whether a same-stem file with *suffix* sits beside *path*.

    Case-insensitive on BOTH halves. A delivery routinely mixes cases —
    ``Veins.SHP`` beside ``veins.dbf`` — and this decides whether a dBASE file
    is a shapefile's attribute half or a standalone table, so getting it wrong
    either drops a real table or opens a sidecar that pyogrio should have read
    through its .shp.
    """
    stem = path.stem.lower()
    want = suffix.lower()
    return any(
        sib.is_file() and sib.stem.lower() == stem and sib.suffix.lower() == want
        for sib in path.parent.iterdir()
    )


_SAFE_NAME_MAX = 120
_DIR_TAG_TAIL = re.compile(r"__[0-9a-f]{6}$")


def _safe_filename(name: str) -> str:
    """Collapse characters that are unsafe in S3 keys to underscores, max 120.

    A name over the limit is shortened in the MIDDLE of its stem: the
    extension survives (ingest_tabular / ingest_spatial classify by it) and so
    does the ``__<6 hex>`` directory tag from `_dir_tag` (it is what keeps
    ``Au/<long name>.csv`` and ``Cu/<long name>.csv`` from sharing one logical
    source name). Cutting the last characters off, as this used to, removed
    both for any member with a long name.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", name)
    if len(cleaned) <= _SAFE_NAME_MAX:
        return cleaned
    stem, dot, ext = cleaned.rpartition(".")
    if not dot or len(ext) > 12:
        stem, ext = cleaned, ""
    else:
        ext = "." + ext
    tag = ""
    tag_match = _DIR_TAG_TAIL.search(stem)
    if tag_match is not None:
        tag = tag_match.group(0)
        stem = stem[: tag_match.start()]
    room = _SAFE_NAME_MAX - len(tag) - len(ext)
    return f"{stem[: max(room, 1)]}{tag}{ext}"[:_SAFE_NAME_MAX]


def _archive_display_name(input: IngestZipArchiveInput) -> str:
    """The archive's name as the user gave it (upload stamp stripped)."""
    from app.services.ingest.geochronology_writer import (  # noqa: PLC0415
        logical_source_name,
    )

    return logical_source_name(input.minio_key.rsplit("/", 1)[-1])


def _member_source_name(
    input: IngestZipArchiveInput, path: Path, root: Path | None,
) -> str:
    """``"<archive>/<path inside the archive>"`` — a member's stable identity."""
    rel = (
        path.relative_to(root).as_posix()
        if root is not None and path.is_relative_to(root)
        else path.name
    )
    return f"{_archive_display_name(input)}/{rel}"


async def _dispatch_dcip_export(
    export: DcipExport,
    *,
    root: Path,
    store: ObjectStorage,
    input: IngestZipArchiveInput,
    children: list[_MemberRun] | None,
) -> None:
    """Bundle one DCIP2D export directory and hand it to ingest_geophysics.

    Members keep their path relative to the archive root inside the bundle
    (``services/ingest/dcip_bundle.write_bundle``), so the directory names
    dcip2d_survey reads the line from (``L3750N/export``) survive the trip.
    """
    rel_dir = relative_dir(export, root)
    bundle = root.parent / f"__dcip_{uuid.uuid4().hex}.zip"
    await asyncio.to_thread(write_bundle, export, root, bundle)
    try:
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        stem = _safe_filename(rel_dir.replace("/", "_").strip("._") or export.directory.name)
        key = f"xyz/{input.project_id}/{ts}_{stem}_dcip2d.zip"
        await _put_member(store, key, bundle)
    finally:
        bundle.unlink(missing_ok=True)
    await _dispatch_member(
        ingest_geophysics,
        IngestGeophysicsInput(
            workspace_id=input.workspace_id,
            project_id=input.project_id,
            minio_key=key,
            source_name=_archive_display_name(input),
        ),
        archive=input, member_name=f"{rel_dir} (DCIP2D export)", children=children,
    )


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

    # The archive's ingest_progress row first — it exists from dispatch time
    # (the trigger endpoint inserts it), so a cancellation that fired before
    # the body ran still has a row to close. Conditional update: a no-op
    # when the body's own lifecycle already marked it.
    progress_close = await ingest_progress.close_run_after_workflow_failure(
        workflow_name="ingest_zip_archive",
        workspace_id=input.workspace_id,
        project_id=input.project_id,
        minio_key=input.minio_key,
        run_id=input.run_id,
        ctx=ctx,
    )

    archive_run_id = await _archive_progress.lookup_archive_run_id_by_run_id(input.run_id)
    if archive_run_id is None:
        log.warning(
            "ingest_zip_archive.on_failure: no archive_run found for run_id=%s — "
            "the body never reached start_run. Cancellation likely fired before "
            "workflow dispatch.",
            input.run_id,
        )
        return {
            "updated": False,
            "reason": "no_archive_run",
            "progress_row": progress_close,
        }

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
        "progress_row": progress_close,
    }


__all__ = ["ingest_zip_archive", "IngestZipArchiveInput"]
