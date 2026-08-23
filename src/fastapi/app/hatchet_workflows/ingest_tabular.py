"""Ingest drill data from CSV and XLSX into the silver drill tables.

Formats
-------
``.csv`` — one table per file, routed by the caller's category or by
classifying the header row.

``.xlsx`` / ``.xls`` — every sheet is enumerated and classified
independently. A single workbook routinely holds Collars, Survey, Lithology
and Assays as separate tabs, and treating only the first one as data is how
the multi-sheet silent-loss bug of 2026-05-23 happened.

Why this workflow exists
------------------------
The `collars` / `surveys` / `lithology` / `samples` / `excel` upload
categories have answered ``422 retired_pipeline`` since 2026-07-28. Azure
holds 14 reports and 9,190 document passages — the PDF path works — and
**zero rows in silver.collars**. A geology platform that cannot accept a
collar file is missing its primary quantitative input.

Write order is not incidental
-----------------------------
silver.surveys, silver.lithology_logs and silver.samples all carry
``collar_id`` referencing silver.collars. Depth intervals are meaningless
without the hole they were logged in, and the FK enforces it. So collars are
written first and everything else resolves ``hole_id -> collar_id`` against
what is now in the table — including collars that were already there from an
earlier upload, which is what makes "collars Monday, assays Friday" work.

Rows whose hole is unknown are counted and reported as ``orphaned``, never
silently dropped: an assay interval for a hole nobody uploaded is a
data-completeness problem the geologist needs told about.

Coordinates and CRS
-------------------
silver.collars stores easting/northing as given plus a geom. The source CRS
is NOT discoverable from a CSV — there is no header for it — so it comes from
the caller, defaulting to ``DEFAULT_SOURCE_EPSG``. When that default is used
rather than supplied, ``georef_method`` records 'assumed', because a UTM
easting read as WGS84 lands in the Gulf of Guinea and the map has no way to
know it is wrong.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time as _t
from pathlib import Path
from typing import Any

import asyncpg
from georag_object_storage import Bucket, get_storage_client
from hatchet_sdk import Context
from pydantic import BaseModel, Field, field_validator

from app.db import bind_workspace_scope
from app.db.dsn import build_dsn
from app.hatchet_workflows import _progress, hatchet

log = logging.getLogger("georag.hatchet.ingest_tabular")

CSV_EXTENSIONS = frozenset({".csv", ".txt", ".tsv"})
EXCEL_EXTENSIONS = frozenset({".xlsx", ".xls", ".xlsm"})
SUPPORTED_EXTENSIONS = CSV_EXTENSIONS | EXCEL_EXTENSIONS

#: UTM zone 13N — the Athabasca Basin, where this platform's corpus is
#: centred. A default, not a detection: see the module docstring.
DEFAULT_SOURCE_EPSG = 32613

#: Order matters — see the module docstring. Anything not in this tuple is
#: reported as an unclassified sheet rather than guessed at.
WRITE_ORDER: tuple[str, ...] = ("collar", "survey", "lithology", "sample")

#: NOT NULL columns the parsers do not guarantee. Defaulting these is the
#: difference between ingesting a real-world file and rejecting it: plenty of
#: collar exports carry no Status or HoleType column at all.
_COLLAR_DEFAULTS = {
    "hole_type": "unknown",
    "status": "unknown",
    "total_depth": 0.0,
}
_SURVEY_METHOD_DEFAULT = "unknown"
_SAMPLE_TYPE_DEFAULT = "unknown"

_INSERT_BATCH = 500


# One DSN builder for the whole service — see app/db/dsn.py for why
# sixty copies of this existed and what the drift cost.
_build_dsn = build_dsn


class IngestTabularInput(BaseModel):
    workspace_id: str
    project_id: str
    minio_key: str
    run_id: str | None = None
    #: For a CSV, which table it holds. None means classify from the header.
    #: Ignored for workbooks — every sheet is classified on its own.
    sheet_type: str | None = None
    #: EPSG of easting/northing. See DEFAULT_SOURCE_EPSG.
    source_epsg: int | None = None

    @field_validator("workspace_id", "project_id")
    @classmethod
    def _must_be_uuid(cls, v: str) -> str:
        import uuid  # noqa: PLC0415

        uuid.UUID(v)
        return v


class IngestTabularOut(BaseModel):
    run_id: str | None
    source_format: str
    #: Per-type counts, e.g. {"collar": {"written": 42, "orphaned": 0}}.
    written: dict[str, dict[str, int]] = Field(default_factory=dict)
    sheets: list[dict[str, Any]] = Field(default_factory=list)
    unclassified: list[str] = Field(default_factory=list)
    source_epsg: int = DEFAULT_SOURCE_EPSG
    epsg_assumed: bool = True
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    duration_ms: int = 0


_COLLAR_SQL = """
INSERT INTO silver.collars (
    collar_id, workspace_id, project_id, hole_id, hole_id_canonical,
    easting, northing, elevation, total_depth, azimuth, dip,
    hole_type, drill_date, status, georef_method,
    created_at, updated_at, geom, geom_4326
) VALUES (
    gen_random_uuid(), $1::uuid, $2::uuid, $3, $4,
    $5, $6, $7, $8, $9, $10,
    $11, $12, $13, $14,
    NOW(), NOW(),
    ST_SetSRID(ST_MakePoint($5, $6), $15::int),
    ST_Transform(ST_SetSRID(ST_MakePoint($5, $6), $15::int), 4326)
)
ON CONFLICT (project_id, hole_id) DO UPDATE SET
    hole_id_canonical = EXCLUDED.hole_id_canonical,
    easting     = EXCLUDED.easting,
    northing    = EXCLUDED.northing,
    elevation   = EXCLUDED.elevation,
    total_depth = EXCLUDED.total_depth,
    azimuth     = EXCLUDED.azimuth,
    dip         = EXCLUDED.dip,
    hole_type   = EXCLUDED.hole_type,
    drill_date  = EXCLUDED.drill_date,
    status      = EXCLUDED.status,
    geom        = EXCLUDED.geom,
    geom_4326   = EXCLUDED.geom_4326,
    updated_at  = NOW()
"""

_SURVEY_SQL = """
INSERT INTO silver.surveys (
    survey_id, workspace_id, collar_id, depth, azimuth, dip,
    survey_method, created_at, updated_at
) VALUES (gen_random_uuid(), $1::uuid, $2::uuid, $3, $4, $5, $6, NOW(), NOW())
"""

_LITHOLOGY_SQL = """
INSERT INTO silver.lithology_logs (
    log_id, workspace_id, collar_id, from_depth, to_depth,
    lithology_code, lithology_description, grain_size, color,
    hardness, rqd, recovery, weathering, created_at, updated_at
) VALUES (
    gen_random_uuid(), $1::uuid, $2::uuid, $3, $4,
    $5, $6, $7, $8, $9, $10, $11, $12, NOW(), NOW()
)
"""

_SAMPLE_SQL = """
INSERT INTO silver.samples (
    sample_id, workspace_id, collar_id, from_depth, to_depth,
    sample_type, lab_id, qaqc_type, created_at, updated_at
) VALUES (
    gen_random_uuid(), $1::uuid, $2::uuid, $3, $4, $5, $6, $7, NOW(), NOW()
)
"""


def _num(value: Any) -> float | None:
    if value in (None, "", " "):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def _collar_index(
    conn: asyncpg.Connection, project_id: str,
) -> dict[str, str]:
    """``hole_id`` and its canonical form -> collar_id, for this project.

    Both spellings are indexed because a survey file may write ``EL-001``
    where the collar file wrote ``EL001``; the canonical form is exactly what
    _hole_id.canonicalize() exists to reconcile.
    """
    rows = await conn.fetch(
        "SELECT collar_id, hole_id, hole_id_canonical FROM silver.collars "
        "WHERE project_id = $1::uuid",
        project_id,
    )
    index: dict[str, str] = {}
    for r in rows:
        cid = str(r["collar_id"])
        if r["hole_id"]:
            index[str(r["hole_id"]).strip().upper()] = cid
        if r["hole_id_canonical"]:
            index.setdefault(str(r["hole_id_canonical"]).strip().upper(), cid)
    return index


def _resolve_collar(index: dict[str, str], hole_id: Any) -> str | None:
    if not hole_id:
        return None
    from georag_geoparsers._hole_id import canonicalize  # noqa: PLC0415

    key = str(hole_id).strip().upper()
    if key in index:
        return index[key]
    canon = canonicalize(str(hole_id))
    return index.get(str(canon).strip().upper()) if canon else None


async def _write_collars(
    conn: asyncpg.Connection, *, workspace_id: str, project_id: str,
    records: list[dict], epsg: int, georef_method: str,
) -> dict[str, int]:
    rows = []
    skipped = 0
    for rec in records:
        easting, northing = _num(rec.get("easting")), _num(rec.get("northing"))
        if not rec.get("hole_id") or easting is None or northing is None:
            # NOT NULL on both, and a collar without coordinates cannot be
            # placed on a map or projected into a section — it is not a
            # collar, so it is reported rather than written as zeroes.
            skipped += 1
            continue
        rows.append((
            workspace_id, project_id,
            str(rec["hole_id"]), rec.get("hole_id_canonical"),
            easting, northing, _num(rec.get("elevation")),
            _num(rec.get("total_depth")) if rec.get("total_depth") is not None
            else _COLLAR_DEFAULTS["total_depth"],
            _num(rec.get("azimuth")), _num(rec.get("dip")),
            rec.get("hole_type") or _COLLAR_DEFAULTS["hole_type"],
            rec.get("drill_date"),
            rec.get("status") or _COLLAR_DEFAULTS["status"],
            georef_method,
            epsg,
        ))

    written = 0
    for start in range(0, len(rows), _INSERT_BATCH):
        chunk = rows[start:start + _INSERT_BATCH]
        await conn.executemany(_COLLAR_SQL, chunk)
        written += len(chunk)
    return {"written": written, "skipped": skipped, "orphaned": 0}


#: Interval tables have no natural unique key, so re-running an ingest would
#: APPEND a second copy of every row. Collars are protected by
#: ON CONFLICT (project_id, hole_id); these are not.
#:
#: Duplicated intervals are the worse failure by a wide margin. They are
#: silent, and they corrupt exactly the numbers people act on — a doubled
#: assay interval skews a composite grade, a doubled lithology log
#: double-counts thickness. Missing data, by contrast, is visible immediately
#: and fixed by re-uploading.
#:
#: So an interval upload REPLACES what is already recorded for the holes it
#: mentions, scoped to those collar_ids and nothing else. Re-uploading a
#: corrected lithology log for EL-001 replaces EL-001 and leaves every other
#: hole untouched, which is what a geologist means by "here is the corrected
#: file".
#:
#: The caveat, stated because it is a real workflow: if one hole's intervals
#: are split across two files, loading the second replaces the first. The
#: replaced count is reported for exactly that reason — it is never silent.
_INTERVAL_TABLES = {
    "survey": "silver.surveys",
    "lithology": "silver.lithology_logs",
    "sample": "silver.samples",
}


async def _write_intervals(
    conn: asyncpg.Connection, *, workspace_id: str, sheet_type: str,
    records: list[dict], index: dict[str, str],
) -> dict[str, int]:
    """Write survey / lithology / sample rows against resolved collars."""
    rows = []
    orphaned = 0
    for rec in records:
        collar_id = _resolve_collar(index, rec.get("hole_id"))
        if collar_id is None:
            # Reported, never silently dropped — an assay interval for a hole
            # nobody uploaded is a completeness gap the geologist must know.
            orphaned += 1
            continue

        if sheet_type == "survey":
            rows.append((
                workspace_id, collar_id, _num(rec.get("depth")),
                _num(rec.get("azimuth")), _num(rec.get("dip")),
                rec.get("survey_method") or _SURVEY_METHOD_DEFAULT,
            ))
        elif sheet_type == "lithology":
            rows.append((
                workspace_id, collar_id,
                _num(rec.get("from_depth")), _num(rec.get("to_depth")),
                rec.get("lithology_code"), rec.get("lithology_description"),
                rec.get("grain_size"), rec.get("color"), rec.get("hardness"),
                _num(rec.get("rqd")), _num(rec.get("recovery")),
                rec.get("weathering"),
            ))
        else:  # sample
            rows.append((
                workspace_id, collar_id,
                _num(rec.get("from_depth")), _num(rec.get("to_depth")),
                rec.get("sample_type") or _SAMPLE_TYPE_DEFAULT,
                rec.get("lab_id"), rec.get("qaqc_type"),
            ))

    sql = {
        "survey": _SURVEY_SQL,
        "lithology": _LITHOLOGY_SQL,
        "sample": _SAMPLE_SQL,
    }[sheet_type]

    # Replace, don't append — see _INTERVAL_TABLES. Scoped to the collars this
    # file actually mentions, inside the same transaction as the insert so a
    # failure cannot leave the holes emptied.
    touched = sorted({r[1] for r in rows})
    replaced = 0
    written = 0

    async with conn.transaction():
        if touched:
            replaced = int(
                await conn.fetchval(
                    f"WITH d AS (DELETE FROM {_INTERVAL_TABLES[sheet_type]} "  # noqa: S608
                    "WHERE collar_id = ANY($1::uuid[]) RETURNING 1) "
                    "SELECT count(*) FROM d",
                    touched,
                ) or 0
            )

        for start in range(0, len(rows), _INSERT_BATCH):
            chunk = rows[start:start + _INSERT_BATCH]
            await conn.executemany(sql, chunk)
            written += len(chunk)

    return {
        "written": written,
        "skipped": 0,
        "orphaned": orphaned,
        "replaced": replaced,
    }


def _csv_headers(path: str) -> list[str]:
    """Read a CSV's header row, honouring its real encoding and delimiter.

    Goes through the same ``_csv_io`` helpers the parsers use rather than a
    naive ``open(path).readline().split(",")``: these files arrive as
    Latin-1 from Windows survey software and semicolon-delimited from
    European labs, and a header row split on the wrong delimiter classifies
    as 'unknown' and silently routes the whole file to nothing.
    """
    import csv  # noqa: PLC0415

    from georag_geoparsers._csv_io import (  # noqa: PLC0415
        detect_delimiter,
        open_csv_with_encoding,
    )

    stream, _encoding, _sha, _size = open_csv_with_encoding(path)
    content = stream.read()
    delimiter = detect_delimiter(content)
    for row in csv.reader(content.splitlines(), delimiter=delimiter):
        if any((cell or "").strip() for cell in row):
            return [(cell or "").strip() for cell in row]
    return []


async def _land_unclassified_as_text(
    conn: Any,
    *,
    path: str,
    suffix: str,
    unclassified: list[str],
    workspace_id: str,
    project_id: str,
) -> dict | None:
    """Make the sheets that matched no drill type searchable anyway.

    Returns the warning to attach, or None when nothing landed. Never
    raises: a text fallback failing must not turn a run that DID write
    typed drill rows into a failure.
    """
    from app.services.ingest.xlsx_ingester import (  # noqa: PLC0415
        ingest_delimited_as_text,
        ingest_xlsx_file,
    )

    names = ", ".join(unclassified[:5])
    more = "" if len(unclassified) <= 5 else f" (+{len(unclassified) - 5} more)"

    try:
        if suffix in EXCEL_EXTENSIONS:
            result = await ingest_xlsx_file(
                conn, path,
                workspace_id=workspace_id,
                project_id=project_id,
                only_sheets=frozenset(unclassified),
            )
        else:
            result = await ingest_delimited_as_text(
                conn, path,
                workspace_id=workspace_id,
                project_id=project_id,
            )
    except Exception as exc:  # noqa: BLE001 — the typed rows already landed
        log.warning(
            "ingest_tabular: text fallback failed for %s (%s)", path, exc,
        )
        return {
            "code": "unclassified_not_indexed",
            "detail": (
                f"{len(unclassified)} sheet(s) matched no drill type and "
                f"could not be indexed as text either ({names}{more}): "
                f"{str(exc)[:200]}"
            ),
        }

    if result.skipped or not result.passages_inserted:
        return {
            "code": "unclassified_not_indexed",
            "detail": (
                f"{len(unclassified)} sheet(s) matched no drill type and "
                f"produced no searchable text ({names}{more}): "
                f"{result.skipped_reason or 'no passages'}."
            ),
        }

    return {
        "code": "unclassified_indexed_as_text",
        "detail": (
            f"{len(unclassified)} sheet(s) matched no collar / survey / "
            f"lithology / sample layout ({names}{more}) and were indexed as "
            f"{result.passages_inserted} searchable passage(s) instead. They "
            f"are answerable in chat but will not appear in the drillhole, "
            f"map or cross-section views."
        ),
    }


def _parse_one(path: str, sheet_type: str, sheet_name: str | None) -> Any:
    """Run the parser matching *sheet_type*."""
    from georag_geoparsers import (  # noqa: PLC0415
        parse_csv_collars,
        parse_csv_lithology,
        parse_csv_samples,
        parse_csv_surveys,
    )

    parser = {
        "collar": parse_csv_collars,
        "survey": parse_csv_surveys,
        "lithology": parse_csv_lithology,
        "sample": parse_csv_samples,
    }[sheet_type]

    if sheet_name is None:
        return parser(path)

    # A workbook sheet is materialised to CSV first so the CSV parsers —
    # which carry the delimiter, encoding, decimal-comma, hole-ID and
    # unit-ambiguity handling — apply unchanged. Reimplementing that per
    # sheet would fork the most heavily audited logic in the pipeline.
    from georag_geoparsers.xlsx_parser import parse_xlsx_sheet  # noqa: PLC0415

    return parse_xlsx_sheet(path, sheet_name=sheet_name, sheet_type=sheet_type)


ingest_tabular = hatchet.workflow(
    name="ingest_tabular",
    input_validator=IngestTabularInput,
)


@ingest_tabular.task(execution_timeout="2h", retries=1)
async def run_ingest_tabular(
    input: IngestTabularInput, ctx: Context,
) -> IngestTabularOut:
    """Download, classify, parse and persist one CSV or workbook."""
    t0 = _t.monotonic()
    store = get_storage_client()
    filename = input.minio_key.rsplit("/", 1)[-1]
    suffix = Path(filename).suffix.lower()

    if suffix not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"ingest_tabular cannot handle {suffix!r} ({filename}); "
            f"supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    epsg = input.source_epsg or DEFAULT_SOURCE_EPSG
    epsg_assumed = input.source_epsg is None
    georef_method = "assumed" if epsg_assumed else "declared"

    # Always create the row, under the run_id the caller minted. Laravel
    # stamps a UUID on every upload, and this used to read
    # `input.run_id or start_run(...)` — so the INSERT never fired, no row
    # existed, and every stage/completion/failure UPDATE below silently
    # matched zero rows. The upload was invisible in the Ingestion Runs UI,
    # successes and failures alike. start_run() is an upsert now, so the
    # trigger endpoint and this preflight may both call it.
    run_id = await _progress.start_run(
        workspace_id=input.workspace_id,
        project_id=input.project_id,
        minio_key=input.minio_key,
        triggered_by="upload",
        workflow_run_id=getattr(ctx, "workflow_run_id", None),
        run_id=input.run_id,
    )

    written: dict[str, dict[str, int]] = {}
    sheets: list[dict[str, Any]] = []
    unclassified: list[str] = []
    warnings: list[dict[str, Any]] = []

    try:
        if run_id:
            await _progress.mark_stage_started(run_id=run_id, stage="preflight")

        with tempfile.TemporaryDirectory(prefix="georag_tabular_") as tmpdir:
            local = str(Path(tmpdir) / filename)
            await asyncio.to_thread(
                store.get_file, Bucket.BRONZE, input.minio_key, local,
            )

            if run_id:
                await _progress.mark_stage_started(run_id=run_id, stage="parse")

            # ── Work out what tables this file holds ────────────────────
            work: list[tuple[str, str | None]] = []   # (sheet_type, sheet_name)
            if suffix in EXCEL_EXTENSIONS:
                from georag_geoparsers.xlsx_parser import enumerate_sheets  # noqa: PLC0415

                for meta in enumerate_sheets(local):
                    # SheetMeta.name, not .sheet_name — the dataclass names it
                    # `name` while carrying `sheet_type` beside it, which is an
                    # easy pair to mistype.
                    sheets.append({
                        "sheet": meta.name,
                        "type": meta.sheet_type,
                        "confidence": meta.classify_confidence,
                        "rows": meta.row_count,
                        "hidden": meta.hidden,
                    })
                    if meta.sheet_type in WRITE_ORDER and meta.row_count:
                        work.append((meta.sheet_type, meta.name))
                    elif meta.sheet_type not in WRITE_ORDER:
                        unclassified.append(meta.name)
            else:
                sheet_type = input.sheet_type
                if sheet_type not in WRITE_ORDER:
                    from georag_geoparsers._sheet_classifier import (  # noqa: PLC0415
                        classify_sheet_type,
                    )

                    headers = _csv_headers(local)
                    sheet_type, confidence = classify_sheet_type(headers)
                    sheets.append({
                        "sheet": filename, "type": sheet_type,
                        "confidence": confidence,
                    })
                if sheet_type in WRITE_ORDER:
                    work.append((sheet_type, None))
                else:
                    unclassified.append(filename)

            if not work:
                warnings.append({
                    "code": "nothing_classified",
                    "detail": (
                        "No sheet matched collar / survey / lithology / sample. "
                        "Pass sheet_type explicitly if the headers are unusual."
                    ),
                })

            # Collars first — everything else FKs to them.
            work.sort(key=lambda pair: WRITE_ORDER.index(pair[0]))

            if run_id:
                await _progress.mark_stage_started(run_id=run_id, stage="persist")

            conn = await asyncpg.connect(_build_dsn())
            try:
                # bind_workspace_scope, NOT a bespoke set_config — see
                # test_scoped_connection's allowlist. It validates the UUID
                # shape and owns the is_local semantics. is_local=False
                # because the writes below are autocommit, not wrapped in one
                # outer transaction.
                await bind_workspace_scope(
                    conn,
                    workspace_id=input.workspace_id,
                    site="hatchet.ingest_tabular",
                    is_local=False,
                )

                for sheet_type, sheet_name in work:
                    result = await asyncio.to_thread(
                        _parse_one, local, sheet_type, sheet_name,
                    )
                    records = getattr(result, "records", None) or []
                    warnings.extend(getattr(result, "warnings", None) or [])

                    if sheet_type == "collar":
                        stats = await _write_collars(
                            conn,
                            workspace_id=input.workspace_id,
                            project_id=input.project_id,
                            records=records, epsg=epsg,
                            georef_method=georef_method,
                        )
                    else:
                        # Rebuilt per type so collars written moments ago in
                        # THIS run are resolvable by the sheets that follow.
                        index = await _collar_index(conn, input.project_id)
                        stats = await _write_intervals(
                            conn,
                            workspace_id=input.workspace_id,
                            sheet_type=sheet_type,
                            records=records, index=index,
                        )

                    prior = written.setdefault(
                        sheet_type,
                        {"written": 0, "skipped": 0, "orphaned": 0, "replaced": 0},
                    )
                    for k, v in stats.items():
                        prior[k] = prior.get(k, 0) + v

                # ── Whatever did not classify ───────────────────────────
                # A sheet that matches no drill type is not necessarily
                # junk: a sample dispatch log, a QA/QC summary, a
                # historical production table. The answer used to be one
                # `nothing_classified` warning and nothing else, so the
                # file was not in the system in ANY form — and for a
                # workbook arriving inside a ZIP that is a regression on
                # the old archive branch, which at least landed it as text.
                #
                # The advice in that warning ("pass sheet_type explicitly")
                # is also unactionable for a zipped file: the archive
                # branch deliberately passes no hint, because inside an
                # archive there is no user-chosen category to pass.
                #
                # Scoped to the unclassified sheets only. Sending the whole
                # workbook would duplicate every drill row as a second,
                # text-shaped copy competing with the typed one.
                if unclassified:
                    text_landed = await _land_unclassified_as_text(
                        conn,
                        path=local,
                        suffix=suffix,
                        unclassified=unclassified,
                        workspace_id=input.workspace_id,
                        project_id=input.project_id,
                    )
                    if text_landed:
                        warnings.append(text_landed)
            finally:
                await conn.close()

        # Orphan accounting BEFORE the terminal write. This block used
        # to sit after it, so `warnings` was already serialised into the
        # progress row by the time the orphan entry was appended and the
        # entry reached only the workflow output object. That object is not
        # what the Ingestion Runs page reads — which is precisely the
        # failure mark_completed_by_run's docstring cites as its reason for
        # existing, quoting THIS warning's text as the example.
        orphans = sum(v.get("orphaned", 0) for v in written.values())
        if orphans:
            warnings.append({
                "code": "orphaned_intervals",
                "detail": (
                    f"{orphans} row(s) reference a hole_id with no collar in "
                    "this project. Upload the collar file, then re-run this one."
                ),
            })

        # Report what actually landed, not just that the workflow ran to
        # the end. mark_completed_by_run downgrades to 'partial' when the
        # row count is zero or warnings are attached, and persists the
        # warnings so their text reaches the Ingestion Runs page instead of
        # dying inside the Hatchet run object.
        if run_id:
            # written is per-sheet-type; the run wrote what all the
            # sheets wrote between them.
            rows_written = sum(
                stats.get("written", 0) for stats in written.values()
            )
            transitioned = await _progress.mark_completed_by_run(
                run_id=run_id,
                rows_written=rows_written,
                warnings=warnings,
            )
            if transitioned:
                # Terminal in the database is not terminal in the product.
                # Nothing else notifies Laravel for the tabular path, so
                # without this the collars land and every surface stays as
                # it was: no toast, no partial reload on Overview or the
                # drillhole page, no data_version bump, and a map still
                # serving the tiles it built before the upload.
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
                    ),
                )

    except Exception as exc:
        if run_id:
            # The kwarg is `error`, not `error_text`. Passing the wrong
            # name raised TypeError *inside* the handler, so the real
            # failure was replaced by the TypeError and the progress row
            # never reached a terminal state.
            await _progress.mark_failed_by_run(
                run_id=run_id, error=str(exc)[:1000],
            )
        log.exception("ingest_tabular failed for %s", input.minio_key)
        raise

    out = IngestTabularOut(
        run_id=run_id,
        source_format=suffix.lstrip("."),
        written=written,
        sheets=sheets,
        unclassified=unclassified,
        source_epsg=epsg,
        epsg_assumed=epsg_assumed,
        warnings=warnings[:20],
        duration_ms=int((_t.monotonic() - t0) * 1000),
    )
    log.info("ingest_tabular complete: %s", out.model_dump(exclude={"sheets"}))
    return out




# ---------------------------------------------------------------------------
# Failure hook (2026-08-21). Mirrors ingest_zip_archive.on_failure.
# ---------------------------------------------------------------------------
@ingest_tabular.on_failure_task(
    name="on_failure",
    execution_timeout="30s",
    schedule_timeout="30m",
    retries=2,
)
async def on_failure(input: IngestTabularInput, ctx: Context) -> dict[str, Any]:
    """Close the ingest_progress row when the workflow dies.

    Without this hook a Hatchet cancellation — concurrency-queue
    expiry, a manual cancel, a worker SIGTERM — left the row created
    by start_run sitting at 'queued' with nothing to close it, because
    the body that would have closed it never ran. The 15-minute stale
    sweep was the only backstop.
    """
    return await _progress.close_run_after_workflow_failure(
        workflow_name="ingest_tabular",
        workspace_id=str(input.workspace_id) if input.workspace_id else None,
        project_id=str(input.project_id) if input.project_id else None,
        minio_key=input.minio_key,
        run_id=input.run_id,
        ctx=ctx,
    )


__all__ = [
    "CSV_EXTENSIONS",
    "DEFAULT_SOURCE_EPSG",
    "EXCEL_EXTENSIONS",
    "SUPPORTED_EXTENSIONS",
    "IngestTabularInput",
    "IngestTabularOut",
    "ingest_tabular",
]
