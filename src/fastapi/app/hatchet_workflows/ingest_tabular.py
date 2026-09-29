"""Ingest drill data from CSV, XLSX, dBASE/DAT and Access into the silver drill tables.

Formats
-------
``.csv`` — one table per file, routed by the caller's category or by
classifying the header row.

``.xlsx`` / ``.xls`` — every sheet is enumerated and classified
independently. A single workbook routinely holds Collars, Survey, Lithology
and Assays as separate tabs, and treating only the first one as data is how
the multi-sheet silent-loss bug of 2026-05-23 happened.

``.dbf`` — a STANDALONE dBASE table, i.e. one with no same-stem ``.shp``
beside it. A ``.dbf`` that does have that sibling is a shapefile's
attribute sidecar and belongs to ``ingest_spatial``; the two cases are
indistinguishable after the file is opened (GDAL resolves the stem and
hands back the shapefile, geometry included), so the discrimination is a
sibling stat taken BEFORE the open — see ``_assert_standalone_dbf``.
Every dBASE table lands in ``silver.attribute_tables`` as JSONB, the
lossless record. Its headers are ALSO run through the same classifier the CSV
and workbook paths use (``_typed_verdict_for_table``): a table that classifies
as collar / survey / lithology / sample / structure is routed through the same
parsers and writers as the identical columns in a CSV, and a table that
classifies as nothing - legend tables, survey point registers, comment logs -
is unchanged. The same holds for each table of a ``.mdb`` and for a MapInfo
``.dat``; a Discover trace export and a surface-geochemistry table keep their
dedicated writers.

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
silver.collars stores easting/northing as given plus geom_4326. A projected
source CRS is NOT discoverable from a CSV — there is no header for it — so
it comes from the upload, then the project, defaulting to
``DEFAULT_SOURCE_EPSG``. When that default is used rather than supplied,
``georef_method`` records 'assumed' and the run carries one prominent
``collar_crs_assumed`` warning, because a UTM easting read in the wrong zone
lands hundreds of km away and the map has no way to know it is wrong.

A longitude/latitude table IS discoverable (headers, or degree-like values)
and is placed as EPSG:4326 whatever the project CRS says (GIS-1,
2026-09-29) — it used to be read as UTM metres and land on the equator.
Every placed table is then checked against the project's known extent and
its CRS's area of use; implausible positions warn (GIS-13). See
app/services/ingest/collar_crs.py.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as _dt
import json
import logging
import math
import re
import tempfile
import time as _t
import uuid
from pathlib import Path
from typing import Any

import asyncpg
from georag_object_storage import Bucket, get_storage_client
from hatchet_sdk import Context
from pydantic import BaseModel, Field, field_validator

from app.db import bind_workspace_scope
from app.db.dsn import build_dsn
from app.hatchet_workflows import _progress, hatchet
from app.services.ingest.silver_row_guard import (
    LITHOLOGY_PERCENT_RANGE,
    LITHOLOGY_TEXT_WIDTHS,
    MINERALIZATION_PCT_RANGE,
    SAMPLE_TEXT_WIDTHS,
    SURVEY_TEXT_WIDTHS,
    UNKNOWN,
    RowIssues,
    finite,
    fit_text,
    guard_collar,
    guard_interval,
    guard_percent,
    issue_warnings,
)

log = logging.getLogger("georag.hatchet.ingest_tabular")

CSV_EXTENSIONS = frozenset({".csv", ".txt", ".tsv"})
EXCEL_EXTENSIONS = frozenset({".xlsx", ".xls", ".xlsm"})
#: Standalone dBASE tables. Listed here rather than left out because the
#: extension gate below is a hard raise that fires BEFORE start_run — an
#: unlisted extension means no progress row and nothing but the on_failure
#: hook to close the run.
DBF_EXTENSIONS = frozenset({".dbf"})

#: MapInfo's attribute half. A dBASE file in every structural respect, but it
#: cannot go through the pyogrio path with the ``.dbf`` files:
#:
#:   * GDAL's shapefile driver is EXTENSION-GATED and refuses a ``.dat`` path
#:     outright ("not recognized as being in a supported file format").
#:   * Copied to ``.dbf`` it opens and returns 91.9% nulls on a real 854-row
#:     file — INCLUDING easting and northing in every row — because MapInfo
#:     writes numerics as raw little-endian doubles inside 'C'-typed fields
#:     and GDAL truncates a character value at its first NUL. Every round
#:     coordinate starts with one. Nothing raises; it looks like a
#:     mostly-empty table, which is the worst possible failure mode.
#:
#: ``georag_geoparsers.dbase_reader`` decodes the bytes directly. Kept
#: separate from DBF_EXTENSIONS rather than switching both: the ``.dbf`` path
#: is measured and tested on real ArcGIS files, and there is no evidence it
#: needs changing. If a ``.dbf`` ever shows the null-truncation symptom, the
#: same reader handles it — it is verified against MiscPoints_2005.dbf.
MAPINFO_DAT_EXTENSIONS = frozenset({".dat"})

DBASE_EXTENSIONS = DBF_EXTENSIONS | MAPINFO_DAT_EXTENSIONS

#: Microsoft Access. Unlike every other extension here it holds MANY tables in
#: one file — a Geosoft IP survey ships 19 — so it fans out to one
#: attribute_tables layer per Access table rather than landing as a single one.
#: Read through mdbtools, which the fastapi runtime image installs.
ACCESS_EXTENSIONS = frozenset({".mdb", ".accdb"})

SUPPORTED_EXTENSIONS = (
    CSV_EXTENSIONS | EXCEL_EXTENSIONS | DBASE_EXTENSIONS | ACCESS_EXTENSIONS
)

#: Formats whose tables are read into Python row dicts rather than parsed from
#: the file on disk. The CSV/Excel typed path re-opens the file per sheet; a
#: dBASE/DAT/Access table has no text form to re-open, so its rows are parsed
#: from memory (``_parse_rows``) by the same georag_geoparsers parsers.
TABLE_SOURCE_EXTENSIONS = DBASE_EXTENSIONS | ACCESS_EXTENSIONS

#: UTM zone 13N — the Athabasca Basin, where this platform's corpus is
#: centred. A default, not a detection: see the module docstring.
DEFAULT_SOURCE_EPSG = 32613

#: silver.collars carries ONE geometry, `geom_4326` (geometry(Point, 4326)),
#: transformed at insert straight from the source CRS above. The old
#: `geom` column — pinned to SRID 32613 for every collar on earth, which
#: made the whole CSV/Excel collar path fail outside zone 13N until it was
#: conformed — was retired 2026-09-29 (Kyle, §04e;
#: 2026_09_30_100000_drop_silver_collars_geom). The easting/northing COLUMNS
#: keep the untouched source values.

#: Order matters — see the module docstring. Anything not in this tuple is
#: reported as an unclassified sheet rather than guessed at.
#:
#: Collars first: every other type resolves hole_id -> collar_id against
#: silver.collars. ``structure`` (silver.structure, per-collar oriented
#: measurements) follows the collars it references, and so do ``alteration``
#: and ``mineralization`` (silver.alteration / silver.mineralization: one
#: alteration or mineral over one interval of a hole) - as standalone tables,
#: and as the companion columns of a lithology log (see _COMPANION_TYPES).
WRITE_ORDER: tuple[str, ...] = (
    "collar", "structure", "survey", "lithology", "alteration",
    "mineralization", "sample",
)

#: NOT NULL text columns the parsers do not guarantee. Defaulting these is
#: the difference between ingesting a real-world file and rejecting it:
#: plenty of collar exports carry no Status or HoleType column, and most
#: assay exports carry no sample-type column. ``hole_type`` / ``status``
#: default inside ``silver_row_guard.guard_collar``. ``total_depth`` is NOT
#: defaulted: it used to be 0.0, which chk_total_depth_positive refuses, and
#: since 2026-09-29 (§04e) the column is nullable, so an absent depth is
#: stored as NULL — see ``_write_collars``.
_SURVEY_METHOD_DEFAULT = UNKNOWN
_SAMPLE_TYPE_DEFAULT = UNKNOWN

_INSERT_BATCH = 500


# One DSN builder for the whole service — see app/db/dsn.py for why
# sixty copies of this existed and what the drift cost.
_build_dsn = build_dsn


def _survey_azimuth_reference(rec: dict[str, Any]) -> str | None:
    """The station's declared azimuth reference, canonical or None.

    csv_survey already canonicalises it; re-reading through the same function
    keeps a record from any other source inside silver.surveys' CHECK
    (true / magnetic / grid) instead of failing the whole batch on INSERT.
    """
    from georag_geoparsers._azimuth_reference import (  # noqa: PLC0415
        canonical_azimuth_reference,
    )

    return canonical_azimuth_reference(rec.get("azimuth_reference"))


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
    #: A column mapping the user confirmed, ``{sheet_type: {field: column}}``.
    #:
    #: Keyed by drill type, not by sheet name, so one workbook can map its
    #: collar sheet and its lithology sheet differently while a loose .csv
    #: and the same table inside an .xlsx are described identically.
    #:
    #: Applied AHEAD of the built-in spellings rather than instead of them:
    #: a user who names the one column we could not find should not have to
    #: re-state the six we did. Fields left unmapped keep alias matching.
    column_map: dict[str, dict[str, str]] | None = None

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
    #: Sheets sent to the text fallback: those that matched no drill type
    #: AND those that matched one and then wrote nothing. It is the set
    #: that got no typed rows, not only the set the classifier gave up on.
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
    drill_type, hole_status,
    created_at, updated_at, geom_4326
) VALUES (
    gen_random_uuid(), $1::uuid, $2::uuid, $3, $4,
    $5, $6, $7, $8, $9, $10,
    $11, $12, $13, $14,
    $16, $17,
    NOW(), NOW(),
    ST_Transform(ST_SetSRID(ST_MakePoint($5, $6), $15::int), 4326)
)
-- One collar per (project, canonical hole id) — §04e, SME-approved
-- 2026-09-29. The WHERE clause lets Postgres infer either arbiter: the
-- partial uq_collars_project_hole_canonical before
-- 2026_09_29_230300 builds the full index, the full
-- collars_project_id_hole_id_canonical_unique after. hole_id_canonical is
-- derived by trg_collars_hole_id_canonical BEFORE arbitration, so a
-- separator/case variant updates the existing collar instead of becoming a
-- ghost beside it; the stored hole_id spelling is kept.
ON CONFLICT (project_id, hole_id_canonical) WHERE hole_id_canonical IS NOT NULL
DO UPDATE SET
    hole_id_canonical = EXCLUDED.hole_id_canonical,
    easting     = EXCLUDED.easting,
    northing    = EXCLUDED.northing,
    elevation   = EXCLUDED.elevation,
    -- Optional (§04e): a file without a depth keeps the stored one.
    total_depth = COALESCE(EXCLUDED.total_depth, silver.collars.total_depth),
    azimuth     = EXCLUDED.azimuth,
    dip         = EXCLUDED.dip,
    hole_type   = EXCLUDED.hole_type,
    drill_date  = EXCLUDED.drill_date,
    status      = EXCLUDED.status,
    -- Only ever the overflow of a too-long hole_type / status (PG-10), so a
    -- row that did not overflow keeps whatever an earlier writer stored.
    drill_type  = COALESCE(EXCLUDED.drill_type, silver.collars.drill_type),
    hole_status = COALESCE(EXCLUDED.hole_status, silver.collars.hole_status),
    geom_4326   = EXCLUDED.geom_4326,
    updated_at  = NOW()
"""

#: $7 is the station's DECLARED azimuth reference ('true' | 'magnetic' |
#: 'grid', or NULL) from the file's azimuth-reference column, canonicalised
#: by csv_survey. Desurvey prefers it over the project's orientation_reference
#: (app/services/ingest/azimuth_reference.py; Kyle, 2026-09-29).
_SURVEY_SQL = """
INSERT INTO silver.surveys (
    survey_id, workspace_id, collar_id, depth, azimuth, dip,
    survey_method, azimuth_reference, created_at, updated_at
) VALUES (gen_random_uuid(), $1::uuid, $2::uuid, $3, $4, $5, $6, $7, NOW(), NOW())
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
    sample_type, lab_id, qaqc_type,
    commodity_assays, commodity_assay_flags,
    created_at, updated_at
) VALUES (
    gen_random_uuid(), $1::uuid, $2::uuid, $3, $4, $5, $6, $7,
    $8::jsonb, $9::jsonb, NOW(), NOW()
)
"""

#: silver.structure — the COLUMNS created by
#: 2026_05_20_060400_create_silver_geological_singulars, which is not the field
#: list in the architecture doc's §04e table (that names depth_m / alpha / beta
#: / dip_dir / dip / confidence; the table has depth, alpha_angle, beta_angle,
#: true_dip, true_dip_dir, roughness, infill, notes and no confidence).
#: The plural ``silver.structures`` was dropped by that migration - the demo
#: seeder that still targets it is stale.
#:
#: The angle parameters are cast through double precision: the columns are
#: ``numeric`` and asyncpg would otherwise send the exact binary expansion of a
#: Python float (0.1 -> 0.1000000000000000055...). float8 -> numeric rounds to
#: 15 significant digits, which is what the source file said.
_STRUCTURE_SQL = """
INSERT INTO silver.structure (
    id, workspace_id, collar_id, depth, structure_type,
    alpha_angle, beta_angle, true_dip, true_dip_dir,
    roughness, infill, notes, created_at
) VALUES (
    gen_random_uuid(), $1::uuid, $2::uuid, $3::double precision, $4,
    $5::double precision, $6::double precision,
    $7::double precision, $8::double precision,
    $9, $10, $11, NOW()
)
"""

#: silver.alteration / silver.mineralization - the COLUMNS created by
#: 2026_05_20_060400_create_silver_geological_singulars (the older plural
#: ``silver.alterations`` of 2026_04_09_180400 was dropped by it). Every
#: column of both tables is written; none is added. ``minerals`` is text[].
#: Depths and the percentage go through double precision so the numeric column
#: receives what the file said (see _STRUCTURE_SQL).
_ALTERATION_SQL = """
INSERT INTO silver.alteration (
    id, workspace_id, collar_id, from_depth, to_depth,
    alteration_type, intensity, minerals, notes, created_at
) VALUES (
    gen_random_uuid(), $1::uuid, $2::uuid,
    $3::double precision, $4::double precision,
    $5, $6, $7::text[], $8, NOW()
)
"""

_MINERALIZATION_SQL = """
INSERT INTO silver.mineralization (
    id, workspace_id, collar_id, from_depth, to_depth,
    mineral, abundance_pct, form, grain_size, notes, created_at
) VALUES (
    gen_random_uuid(), $1::uuid, $2::uuid,
    $3::double precision, $4::double precision,
    $5, $6::double precision, $7, $8, $9, NOW()
)
"""

#: Per-element assay intervals for silver.assays_v2 — the §04e-canonical
#: assay table (Kyle 2026-05-20, reaffirmed 2026-08-25). Every assay-side
#: reader (nl_summaries, the agent's assay tools, AssayResolver citations,
#: the DrillholeDetail assay panel) queries THIS table; until 2026-08-25
#: nothing wrote it after the Dagster retirement, and this writer dropped
#: the parser's per-element values on the floor besides.
#:
#: The id is supplied, not defaulted: it is a uuid5 over the row's natural
#: key, so re-uploading the same file writes the same ids and the
#: nl_summaries passages derived from them do not churn. ON CONFLICT
#: handles the same natural key appearing twice WITHIN one file (last
#: row wins, matching the interval tables' replace semantics).
_ASSAYS_V2_SQL = """
INSERT INTO silver.assays_v2 (
    id, workspace_id, collar_id, sample_id, from_depth, to_depth,
    element, value, unit, value_ppm, detection_limit,
    over_detection, under_detection, half_dl_substituted, lab_name
) VALUES (
    $1::uuid, $2::uuid, $3::uuid, $4, $5, $6,
    $7, $8, $9, $10, $11, $12, $13, $14, $15
)
ON CONFLICT (id) DO UPDATE SET
    value               = EXCLUDED.value,
    unit                = EXCLUDED.unit,
    value_ppm           = EXCLUDED.value_ppm,
    detection_limit     = EXCLUDED.detection_limit,
    over_detection      = EXCLUDED.over_detection,
    under_detection     = EXCLUDED.under_detection,
    half_dl_substituted = EXCLUDED.half_dl_substituted,
    lab_name            = EXCLUDED.lab_name
"""


#: A dBASE table has no geology schema to map onto, so its rows land
#: whole, as JSONB, keyed by where they came from.
#:
#: Idempotent by (project_id, source_file_sha256, source_layer,
#: row_index). That key is what lets a re-upload be a no-op instead of
#: forcing the replace-or-append choice the interval tables had to make:
#: the same bytes produce the same hash, so the same row updates in place
#: and a corrected export of the same table cannot double itself. A
#: genuinely different file has a different hash and lands beside the old
#: one rather than silently overwriting it.
#: Surface geochemistry — a sample with a location and no drill hole.
#:
#: collar_id, from_depth and to_depth are omitted entirely rather than passed
#: as NULL, so this statement documents on its face that a surface sample has
#: none of them. They became nullable in
#: 2026_08_25_010000_allow_surface_samples_in_silver_geochemistry; before that
#: migration this INSERT could not run at all.
#:
#: geom is built with ST_Transform from the source CRS, NOT stored in native
#: coordinates: the column is geometry(Point,4326) and every map layer reads
#: it as such. A UTM easting written straight in would place the sample at
#: longitude 394,240.
_GEOCHEM_SQL = """
INSERT INTO silver.geochemistry (
    geochem_id, workspace_id, project_id,
    sample_id, sample_type, geom,
    assay_element_codes, assay_values_ppm,
    created_at, updated_at
) VALUES (
    gen_random_uuid(), $1::uuid, $2::uuid,
    $3, $4,
    ST_Transform(ST_SetSRID(ST_MakePoint($5::double precision, $6::double precision), $7::int), 4326),
    $8::text[], $9::jsonb,
    NOW(), NOW()
)
-- The WHERE is REQUIRED, not decoration. uq_geochemistry_project_sample is a
-- PARTIAL index (WHERE sample_id IS NOT NULL), and Postgres will not infer a
-- partial index from the column list alone — without a matching predicate
-- every insert fails with 42P10 "there is no unique or exclusion constraint
-- matching the ON CONFLICT specification". Reproduced on a real server: the
-- writer wrote ZERO rows, every time, while looking correct on review.
ON CONFLICT (project_id, sample_id) WHERE sample_id IS NOT NULL
DO UPDATE SET
    sample_type         = EXCLUDED.sample_type,
    geom                = EXCLUDED.geom,
    assay_element_codes = EXCLUDED.assay_element_codes,
    assay_values_ppm    = EXCLUDED.assay_values_ppm,
    updated_at          = NOW()
"""

_ATTRIBUTE_TABLE_SQL = """
INSERT INTO silver.attribute_tables (
    attribute_row_id, workspace_id, project_id,
    source_file, source_file_sha256, source_layer, row_index,
    attributes, created_at, updated_at
) VALUES (
    gen_random_uuid(), $1::uuid, $2::uuid,
    $3, $4, $5, $6,
    $7::jsonb, NOW(), NOW()
)
ON CONFLICT (project_id, source_file_sha256, source_layer, row_index)
DO UPDATE SET
    source_file = EXCLUDED.source_file,
    attributes  = EXCLUDED.attributes,
    updated_at  = NOW()
"""


def _sha256_file(path: str) -> str:
    """Streaming SHA-256 of the source file.

    Streamed rather than ``read_bytes()`` for the same reason
    ingest_spatial streams: the worker has a fixed memory budget and the
    upload cap does not.
    """
    import hashlib  # noqa: PLC0415

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _assert_standalone_dbf(path: str) -> None:
    """Refuse a ``.dbf`` that is really a shapefile's attribute sidecar.

    Measured 2026-08-23: hand GDAL ``x.dbf`` while ``x.shp`` sits in the
    same directory and it returns the SHAPEFILE -- geometry and all --
    not the table. The two cases therefore cannot be told apart from the
    result, which is why this check runs before the open rather than
    after it.

    This workflow downloads exactly one object into a fresh
    TemporaryDirectory, so the sibling cannot normally be present. That
    is the invariant the branch depends on, stated out loud: a future
    caller that unpacks a whole delivery into one directory and points
    this workflow at a member fails here, loudly, instead of quietly
    landing geometry in an attribute table.

    Case-insensitive on purpose. GDAL on Linux resolves the stem
    case-sensitively, but ``veins.dbf`` beside ``Veins.shp`` is still one
    shapefile to the geologist who made it, and treating it as a table
    would split a dataset in half.
    """
    target = Path(path)
    wanted = target.stem.lower() + ".shp"
    for sibling in target.parent.iterdir():
        if sibling.name.lower() == wanted:
            raise ValueError(
                f"{target.name} is the attribute sidecar of {sibling.name}, "
                f"not a standalone table. Upload the shapefile (or its zip) "
                f"so ingest_spatial reads the geometry and attributes "
                f"together."
            )


def _jsonable(value: Any) -> Any:
    """One dBASE cell -> something ``json.dumps`` will accept.

    pyogrio's raw reader yields numpy scalars; ``.item()`` unwraps each to
    the nearest Python builtin. NaN becomes NULL rather than the string
    ``'nan'``, because a dBASE numeric with nothing in it is missing
    data, not the text "nan" -- and a JSONB document carrying "nan" is
    indistinguishable from one where somebody typed it.
    """
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if hasattr(value, "dtype") and callable(getattr(value, "item", None)):
        # numpy scalar. datetime64 unwraps to date/datetime (NaT -> None),
        # which the isoformat branch below then handles.
        value = value.item()
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    return str(value)


def _read_dbf_table(path: str) -> list[dict[str, Any]]:
    """Read a standalone dBASE table into plain, JSON-safe row dicts.

    pyogrio, not a new dependency: GDAL's ESRI Shapefile driver opens a
    bare ``.dbf`` as an attribute-only layer (measured 2026-08-23 -- 10
    rows, 9 columns, no geometry column). dbfread and simpledbf would
    each add a package that check_pyproject_covers_imports and
    check_fastapi_lock_export both gate on, to do what GDAL already does.

    The raw reader rather than ``read_arrow``: pyarrow is absent from
    every image and lockfile in this repo, so the arrow path raises
    RuntimeError. ``read_geometry=False`` because there is none.

    Encoding is left to GDAL. All five dBASE files in the RedStar
    delivery are LDID 0x57 with no ``.cpg`` and decode correctly on that
    basis, and pyogrio's ``encoding=`` kwarg measurably has no effect on
    this driver -- passing one would be decoration. A ``.cpg`` that lies
    raises UnicodeDecodeError, which fails the run loudly; that is the
    right outcome for a file whose declared encoding is wrong, and far
    better than the mojibake a guess would land.
    """
    from pyogrio.raw import read  # noqa: PLC0415

    meta, _fids, _geometry, field_data = read(path, read_geometry=False)
    fields = [str(name) for name in meta["fields"]]
    if not fields or not field_data:
        # A dBASE table always declares at least one field, so this is
        # "the driver gave us nothing", not "the table is empty".
        return []

    return [
        {
            name: _jsonable(column[index])
            for name, column in zip(fields, field_data, strict=True)
        }
        for index in range(len(field_data[0]))
    ]


def _read_mapinfo_dat_table(path: str) -> list[dict[str, Any]]:
    """Read a MapInfo ``.DAT`` into plain, JSON-safe row dicts.

    Uses ``georag_geoparsers.dbase_reader`` rather than the pyogrio path
    above, for the reasons on MAPINFO_DAT_EXTENSIONS: GDAL cannot open a
    ``.dat`` at all, and renaming it produces silent nulls rather than an
    error.

    The reader decides per FIELD whether a 'C'-typed column actually holds
    binary doubles, because a per-cell test cannot work in either direction —
    ``14.0`` encodes as eight printable-or-NUL bytes, and the real 8-character
    text ``'SOIL'`` has trailing NULs that land in a double's exponent. Rows
    flagged deleted are skipped, which is why a table can return fewer rows
    than its header declares.
    """
    from georag_geoparsers.dbase_reader import read_dbase  # noqa: PLC0415

    table = read_dbase(path)
    return [
        {name: _jsonable(value) for name, value in row.items()}
        for row in table.rows
    ]


async def _write_attribute_rows(
    conn: asyncpg.Connection, *, workspace_id: str, project_id: str,
    source_file: str, source_file_sha256: str, source_layer: str,
    rows: list[dict[str, Any]],
) -> dict[str, int]:
    """Land a standalone dBASE table in silver.attribute_tables.

    ``skipped`` and ``orphaned`` are reported as zero rather than omitted
    so the per-type accumulator in the workflow body sums the same keys
    for every branch.
    """
    params = [
        (
            workspace_id, project_id, source_file, source_file_sha256,
            source_layer, index, json.dumps(attributes, default=str),
        )
        for index, attributes in enumerate(rows)
    ]

    written = 0
    for start in range(0, len(params), _INSERT_BATCH):
        chunk = params[start:start + _INSERT_BATCH]
        await conn.executemany(_ATTRIBUTE_TABLE_SQL, chunk)
        written += len(chunk)
    return {"written": written, "skipped": 0, "orphaned": 0}


#: Element columns a surface geochemistry table is recognised by.
#:
#: Deliberately the ECONOMIC + PATHFINDER suite rather than every element an
#: ICP package reports. A table is only geochemistry if it carries assays
#: someone would map, and matching on, say, `si` alone would promote any
#: table with a two-letter column. Matched after normalize_header, so
#: `Au_ppm`, `au_ppm`, `AU (ppm)` and `au` all reach the same key.
_GEOCHEM_ELEMENTS: frozenset[str] = frozenset({
    "au", "ag", "cu", "pb", "zn", "as", "sb", "hg", "mo", "ni", "co",
    "bi", "cd", "sn", "te", "tl", "u", "w", "ba", "mn", "cr", "li", "re",
})

#: Unit suffixes an assay column carries. `au_ppm` and `au_ppb` are DIFFERENT
#: columns holding different numbers, so the unit is part of the identity and
#: is preserved in assay_values_ppm's keys rather than folded away.
_ASSAY_UNITS: frozenset[str] = frozenset({"ppm", "ppb", "pct", "per", "gpt", "oz"})

def _is_below_detection(value: float) -> bool:
    """Whether an assay value means "below the detection limit".

    A NEGATED DETECTION LIMIT, not a single sentinel. This started as an
    equality test against -9.0, which was wrong: measured across
    all_historical_soils_clean.DAT there are TWELVE distinct negative values
    over 5,062 cells — -0.04, -0.1, -0.2, -1, -2, -5, -9, -10, -20, -40, -100,
    -200. Each is the negation of that batch's detection limit, so -0.2 means
    "below 0.2 ppm", and the -9 that inspired the constant was simply the
    commonest limit rather than a magic number.

    Testing only for -9 left 1,326 cells of the other eleven values intact, so
    a gold column would carry real negative grades — a mean pulled below zero
    by samples that in fact contain no measurable gold, and a colour ramp with
    a floor nothing can reach.

    A concentration cannot be negative, so the sign alone is the signal and no
    list of limits has to be maintained. The value is recorded as ABSENT
    rather than substituted: half-detection-limit is a common convention but
    it invents a number nobody measured, and the verbatim original is already
    preserved in silver.attribute_tables.
    """
    return value < 0


def _assay_columns(columns: list[str]) -> dict[str, str]:
    """Assay columns in *columns*, as ``{normalised key: original column}``.

    An assay column is an element symbol optionally followed by a unit and
    optionally more qualifiers: ``au_ppm``, ``au_fa_grav``, ``ag_icp_aqr``.
    The element must come FIRST — ``sample_type`` starts with no element and
    ``grainsize`` merely contains one.
    """
    from georag_geoparsers._header_match import normalize_header  # noqa: PLC0415

    found: dict[str, str] = {}
    for original in columns:
        skeleton = normalize_header(original)
        if not skeleton:
            continue
        # Longest element first so 'as' does not shadow nothing and 'ag' does
        # not match the leading letters of a longer symbol.
        for element in sorted(_GEOCHEM_ELEMENTS, key=len, reverse=True):
            if not skeleton.startswith(element):
                continue
            rest = skeleton[len(element):]
            # Bare symbol, or symbol + a unit/qualifier that starts on a token
            # boundary. `australia` must not read as gold.
            if rest and not any(rest.startswith(u) for u in _ASSAY_UNITS):
                continue
            found.setdefault(skeleton, original)
            break
    return found


def _surface_geochem_columns(columns: list[str]) -> dict[str, Any] | None:
    """Map *columns* to a surface-geochemistry shape, or None if it is not one.

    Requires all three of: a sample identifier, BOTH coordinates, and at
    least three assay columns. Three rather than one because a collar table
    carrying a single `au_ppm` is a drill table, not a soil survey, and the
    cost of a false positive here is rows written into the wrong table.

    Returns None for anything else, which leaves the file as the attribute
    table it already lands as — this path only ever ADDS a destination.
    """
    from georag_geoparsers._header_match import build_column_map  # noqa: PLC0415

    located, _ = build_column_map(columns, {
        "sample_id": ["sample", "sample_no", "sample_number", "sampleid", "station"],
        # Longitude/latitude are coordinates too (GIS-1): the CRS decision
        # places a lon/lat survey as EPSG:4326, so it no longer has to be
        # renamed to x/y to be recognised at all.
        "easting": ["easting", "east", "utm_e", "x", "xcoord", "longitude", "long", "lon"],
        "northing": ["northing", "north", "utm_n", "y", "ycoord", "latitude", "lat"],
        "sample_type": ["sample_typ", "sample_type", "samptype", "type"],
    })

    if not {"sample_id", "easting", "northing"} <= set(located):
        return None

    # A DEPTH INTERVAL means down-hole, whatever else the columns look like.
    # A drill sample table carries sample/from_depth/to_depth plus assays, and
    # sometimes collar coordinates too — every test above passes for it. Those
    # rows belong to a collar and route through the `sample` sheet type; this
    # writer would strip their interval and file them as surface samples, which
    # is silent, plausible-looking data loss.
    downhole, _ = build_column_map(columns, {
        "from_depth": ["from_depth", "from", "depth_from", "from_m"],
        "to_depth": ["to_depth", "to", "depth_to", "to_m"],
    })
    if downhole:
        return None

    assays = _assay_columns(columns)
    if len(assays) < 3:
        return None

    return {"located": located, "assays": assays}


#: silver.geochemistry accepts these; anything else violates the CHECK.
_SAMPLE_TYPES: frozenset[str] = frozenset({
    "soil", "rock_chip", "grab", "channel", "stream_sediment",
    "till", "drillhole_pulp", "drillhole_reject", "other",
})

#: One-letter codes MapInfo/Discover exports use in a `sample_typ` column.
#: Measured: all_historical_soils_clean.DAT uses 'S' throughout.
_SAMPLE_TYPE_CODES: dict[str, str] = {
    "s": "soil", "soil": "soil",
    "r": "rock_chip", "rock": "rock_chip", "rc": "rock_chip",
    "g": "grab", "grab": "grab",
    "c": "channel", "chan": "channel",
    # §04e, SME-approved 2026-09-29: a trench sample is a channel sample
    # (the same mapping csv_sample.SAMPLE_TYPE_SYNONYMS makes for drill
    # samples). RAB/aircore are drill cuttings and do not occur here.
    "trench": "channel", "trench channel": "channel",
    "ss": "stream_sediment", "stream": "stream_sediment", "sed": "stream_sediment",
    "t": "till", "till": "till",
}


def _sample_type_of(raw: Any) -> str:
    """Normalise a sample-type code to the CHECK's vocabulary.

    Falls back to 'other' rather than NULL: the column is what a geologist
    filters the map on, and an unrecognised code is still a statement that
    the sample HAS a type. NULL would read as "not recorded".
    """
    text = str(raw or "").strip().lower()
    if not text:
        return "other"
    if text in _SAMPLE_TYPES:
        return text
    return _SAMPLE_TYPE_CODES.get(text, "other")


#: Unit → parts-per-million factor. The unit is what the COLUMN SUFFIX
#: declared; there is no guessing an undeclared unit from the value. The
#: parser stores every assay in one of these three (g/t is ppm, oz/t is
#: converted to ppm) under a canonical key such as ``Au_ppm`` — see
#: georag_geoparsers._assay_columns, which also reads the key back here.
_UNIT_TO_PPM = {"ppm": 1.0, "ppb": 0.001, "pct": 10000.0}


def derive_assay_v2_rows(
    rec: dict[str, Any],
    *,
    workspace_id: str,
    collar_id: str,
    element_ref: dict[str, str],
) -> tuple[list[tuple], int]:
    """Explode one sample record's commodity_assays into assays_v2 params.

    Returns ``(rows, skipped)``. A row is skipped — counted, never silently
    dropped — when the record has no lab sample number (the column is NOT
    NULL and inventing one would break the "same file, same ids" property),
    when the interval is missing or inverted, or when a value is negative
    (the table CHECK rejects it and one bad cell must not sink the batch).

    A below-detection cell with an unknown threshold ("BDL") still becomes a
    row: value NULL + under_detection TRUE is the difference between "below
    detection" and "never analysed", and the renderer downstream
    distinguishes exactly that.

    ``element_ref`` maps element symbol → default unit
    (silver.element_reference) for headers that named only the element.
    """
    sample_id = str(rec.get("sample_id") or "").strip()
    from_depth = _num(rec.get("from_depth"))
    to_depth = _num(rec.get("to_depth"))

    assays: dict[str, Any] = rec.get("commodity_assays") or {}
    flags: dict[str, Any] = rec.get("commodity_assay_flags") or {}
    # Union: BDL-unknown cells carry a flag but no value, and still count as
    # a measurement. Unparseable cells ("NS", "NR") are excluded — those are
    # "not reported", not "below detection".
    keys = set(assays) | {
        k for k, f in flags.items() if isinstance(f, dict) and f.get("dl_flag")
    }
    if not keys:
        return [], 0

    if (
        not sample_id
        or from_depth is None
        or to_depth is None
        or to_depth <= from_depth
    ):
        return [], len(keys)

    from georag_geoparsers._assay_columns import split_assay_key  # noqa: PLC0415

    rows: list[tuple] = []
    skipped = 0
    for key in sorted(keys):
        parsed = split_assay_key(key)
        if parsed is None:
            # Not an assay key the parser could have produced — counted,
            # never raised.
            skipped += 1
            continue
        element, suffix = parsed
        unit = suffix or element_ref.get(element) or "unspecified"

        value = assays.get(key)
        if value is not None and value < 0:
            skipped += 1
            continue
        factor = _UNIT_TO_PPM.get(unit)
        value_ppm = value * factor if value is not None and factor else None

        flag = flags.get(key) if isinstance(flags.get(key), dict) else {}
        under_detection = bool(flag.get("dl_flag"))
        # ">10": the value is the upper limit, and the row says so (ING-9) —
        # it used to be hard-coded False and the cell dropped by the parser.
        over_detection = bool(flag.get("od_flag"))
        detection_limit = flag.get("dl_threshold")
        if detection_limit is None and over_detection:
            detection_limit = flag.get("od_threshold")
        half_dl = flag.get("substitution") == "half_dl"

        # uuid5 over the natural key: re-uploading the same file rewrites
        # the same ids, so nl_summaries passages keyed on them do not churn.
        row_id = uuid.uuid5(
            uuid.NAMESPACE_OID,
            f"silver.assays_v2:{collar_id}:{sample_id}:"
            f"{from_depth}:{to_depth}:{element}:{unit}",
        )
        rows.append((
            str(row_id), workspace_id, collar_id, sample_id,
            from_depth, to_depth,
            element, value, unit, value_ppm, detection_limit,
            over_detection, under_detection, half_dl,
            rec.get("lab_id"),
        ))
    return rows, skipped


def _discover_trace_columns(columns: list[str]) -> dict[str, str] | None:
    """Map a Discover/MapInfo drillhole-TRACE export, or None if not one.

    This is not a collar table and must not be confused with one. A Discover
    trace export writes one row per SEGMENT of a desurveyed hole, so a
    straight hole arrives as TWO rows: the collar at depth 0, and the segment
    midpoint at SegmentLen = Depth/2. Both carry the same CollarID.

    Recognised by SegmentLen together with MidX/MidY — a plain collar table
    has easting/northing and no notion of a segment, so it cannot match.
    """
    from georag_geoparsers._header_match import build_column_map  # noqa: PLC0415

    mapped, _ = build_column_map(columns, {
        "hole_id": ["collarid_d", "collarid", "collar_id", "hole_id", "holeid"],
        "depth": ["depth_db", "depth"],
        "azimuth": ["azimuth_db", "azimuth", "bearing"],
        "dip": ["dip_db", "dip", "inclination"],
        "mid_x": ["midx_db", "midx", "mid_x"],
        "mid_y": ["midy_db", "midy", "mid_y"],
        "mid_z": ["midz_db", "midz", "mid_z"],
        "segment_len": ["segmentlen", "segment_len", "seglen"],
    })

    required = {"hole_id", "depth", "mid_x", "mid_y", "segment_len"}
    return mapped if required <= set(mapped) else None


def _collapse_discover_traces(
    rows: list[dict[str, Any]], mapped: dict[str, str],
) -> list[dict[str, Any]]:
    """Collapse trace segments into one collar per hole.

    THE BUG THIS EXISTS TO PREVENT. _COLLAR_SQL is ON CONFLICT DO UPDATE and
    the segments arrive in key order, so feeding this file row by row writes
    the collar, then OVERWRITES it with the midpoint. Every hole lands 10-40 m
    from where it was drilled, with no error and no warning — the map just
    quietly disagrees with the survey.

    The collar is the row at depth 0; the hole's length is the greatest depth
    on any of its segments. Verified against the real file: for all five
    trenches, start + (Depth/2) x (sin azimuth, cos azimuth) reproduces the
    midpoint row's MidX/MidY to 0.0000 m, which is what confirms the depth-0
    row is the collar rather than another segment.
    """
    by_hole: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        hole = str(row.get(mapped["hole_id"], "") or "").strip()
        if hole:
            by_hole.setdefault(hole, []).append(row)

    collars: list[dict[str, Any]] = []
    for hole, segments in by_hole.items():
        # A MISSING depth is not depth zero. `or 0.0` turned every unparseable
        # or blank depth into a perfect match for the depth-0 collar test
        # below, so a hole whose collar row was absent would take an arbitrary
        # midpoint as its collar — the exact failure this function exists to
        # prevent, arriving through the coercion instead of the data.
        depths = [
            (depth, s)
            for s, depth in ((s, _num(s.get(mapped["depth"]))) for s in segments)
            if depth is not None
        ]
        if not depths:
            log.warning(
                "ingest_tabular: trace for %r has no readable depth on any "
                "segment — cannot tell the collar from a midpoint", hole,
            )
            continue
        # Sorting rather than trusting file order: a re-export can interleave
        # holes, and "the first row I saw" would then be an arbitrary segment.
        depths.sort(key=lambda pair: pair[0])
        shallowest_depth, collar_row = depths[0]

        if shallowest_depth > 0:
            # No depth-0 segment: the collar position was never exported, and
            # the shallowest midpoint is NOT the collar. Guessing here is the
            # exact error this function exists to avoid, so skip the hole and
            # let the caller report it.
            log.warning(
                "ingest_tabular: trace for %r has no depth-0 segment "
                "(shallowest %.2f m) — cannot locate its collar",
                hole, shallowest_depth,
            )
            continue

        collars.append({
            "hole_id": hole,
            "easting": _num(collar_row.get(mapped["mid_x"])),
            "northing": _num(collar_row.get(mapped["mid_y"])),
            "elevation": _num(collar_row.get(mapped["mid_z"])) if "mid_z" in mapped else None,
            "total_depth": max(depth for depth, _ in depths),
            "azimuth": _num(collar_row.get(mapped["azimuth"])) if "azimuth" in mapped else None,
            "dip": _num(collar_row.get(mapped["dip"])) if "dip" in mapped else None,
        })
    return collars


def _trace_survey_stations(
    rows: list[dict[str, Any]], mapped: dict[str, str],
) -> list[dict[str, Any]]:
    """Downhole survey stations from a Discover trace export.

    The same rows that yield collars are also a SURVEY: each segment records
    the depth it ends at, plus the azimuth and dip the hole was running at.
    That is exactly a station, and without it the Workspace 3D view has hole
    positions and no trajectories — a stereosphere with nothing in it, which
    reads as broken rather than as absent data.

    Every segment becomes a station, including the depth-0 one: a survey that
    starts below the collar leaves the first stretch of the hole
    unconstrained, and for a 61.5 m trench that is the whole thing.

    Azimuth and dip are required — a station without them constrains nothing.
    Depth is required for the same reason and must be readable, not coerced:
    the collar collapse above learned that lesson, and a station silently
    placed at 0 m would bend the trajectory back to the collar.
    """
    stations: list[dict[str, Any]] = []
    for row in rows:
        hole = str(row.get(mapped["hole_id"], "") or "").strip()
        depth = _num(row.get(mapped["depth"]))
        azimuth = _num(row.get(mapped["azimuth"])) if "azimuth" in mapped else None
        dip = _num(row.get(mapped["dip"])) if "dip" in mapped else None

        if not hole or depth is None or azimuth is None:
            continue

        stations.append({
            "hole_id": hole,
            "depth": depth,
            "azimuth": azimuth,
            # A Discover trace writes 0 for a horizontal trench rather than
            # leaving it blank, so an absent dip really does mean 0 here —
            # but only when the column exists at all. `or 0.0` would hide a
            # missing column, so the distinction is kept.
            "dip": dip if dip is not None else (0.0 if "dip" in mapped else None),
            "survey_method": "desurveyed_trace",
        })
    return stations


def _normalize_trace_dips(
    rows: list[dict[str, Any]], mapped: dict[str, str],
    collars: list[dict[str, Any]], stations: list[dict[str, Any]],
    *, label: str,
) -> list[dict[str, Any]]:
    """Put a trace export's dips in the silver convention; return warnings.

    Up-holes are stored as measured since 2026-09-29 (§04e, SME-approved), so
    a positive dip is no longer blanked on the way in — which makes the sign
    convention of THIS file matter. The trace path never ran the per-file
    heuristic the collar and survey parsers use; it does now, the same
    ``resolve_dip_convention``: a file whose dips are mostly positive is a
    down-positive export and every value is flipped, and an individual
    positive dip in a down-negative export stays an up-hole. Mutates
    ``collars`` and ``stations`` in place.
    """
    from georag_geoparsers._dip_convention import (  # noqa: PLC0415
        normalize_dip,
        resolve_dip_convention,
    )

    if "dip" not in mapped:
        return []
    dips = [d for d in (_num(r.get(mapped["dip"])) for r in rows) if d is not None]
    resolution = resolve_dip_convention(dips, header=mapped["dip"], parser="discover_trace")
    if resolution.convention in ("down_positive", "from_vertical"):
        for rec in (*collars, *stations):
            if rec.get("dip") is not None:
                rec["dip"] = normalize_dip(float(rec["dip"]), resolution.convention)
    return [
        {"code": w["code"], "message": f"{label}: {w['message']}", **({"detail": w["detail"]} if w.get("detail") else {})}
        for w in resolution.warnings
    ]


async def _write_surface_geochem(
    conn: asyncpg.Connection, *, workspace_id: str, project_id: str,
    shape: dict[str, Any], rows: list[dict[str, Any]], source_epsg: int,
) -> dict[str, int]:
    """Land surface samples in silver.geochemistry.

    ADDITIVE. The same rows already went to silver.attribute_tables, which
    stays the verbatim record of the file; this gives the assays a typed home
    the map and the agent can read. A failure here must therefore not lose the
    attribute copy, which is why the caller treats it as best-effort.

    A row is skipped, not failed, when it has no sample id or no usable
    coordinate pair — a survey file routinely carries a trailing blank row or
    a legend line, and refusing the other 853 samples over it would be
    absurd. The count comes back so the caller can report it.
    """
    located = shape["located"]
    assays: dict[str, str] = shape["assays"]

    params = []
    skipped = 0
    for row in rows:
        sample_id = str(row.get(located["sample_id"], "") or "").strip()
        easting = _num(row.get(located["easting"]))
        northing = _num(row.get(located["northing"]))
        if not sample_id or easting is None or northing is None:
            skipped += 1
            continue

        # Below-detection is recorded as absent, not as a negative grade. A
        # -9 stored as a number drags every mean and every colour ramp with
        # it, and the sample genuinely has no measured value.
        values = {}
        for key, column in assays.items():
            value = _num(row.get(column))
            if value is None or _is_below_detection(value):
                continue
            values[key] = value

        sample_type = _sample_type_of(
            row.get(located["sample_type"]) if "sample_type" in located else None,
        )

        params.append((
            workspace_id, project_id, sample_id, sample_type,
            easting, northing, source_epsg,
            sorted(values), json.dumps(values),
        ))

    written = 0
    for start in range(0, len(params), _INSERT_BATCH):
        chunk = params[start:start + _INSERT_BATCH]
        await conn.executemany(_GEOCHEM_SQL, chunk)
        written += len(chunk)
    return {"written": written, "skipped": skipped, "orphaned": 0}


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


async def _existing_collars_by_canonical(
    conn: asyncpg.Connection, project_id: str,
) -> dict[str, tuple[str, float | None]]:
    """``canonical hole id -> (stored hole_id, stored total_depth)``.

    The upsert used to be keyed on ``(project_id, hole_id)``, so ``SRE09_6``
    from a LAS header on Monday and ``SRE09-6`` from Friday's collar table
    became two collars at two positions for one hole (ING-14). It is now
    keyed on the canonical hole id (§04e, 2026-09-29), which already lands
    the variant on the existing collar; reading the stored spelling here
    keeps that spelling in the row and lets the run REPORT the match
    (``hole_id_matched_existing_collar``).

    When the project still holds two ghosts of one hole (before
    ``php artisan collars:merge-duplicates`` has run), the one whose stored
    spelling sorts first wins, so the choice is deterministic.
    """
    from georag_geoparsers._hole_id import canonicalize  # noqa: PLC0415

    rows = await conn.fetch(
        "SELECT hole_id, hole_id_canonical, total_depth FROM silver.collars "
        "WHERE project_id = $1::uuid ORDER BY hole_id",
        project_id,
    )
    out: dict[str, tuple[str, float | None]] = {}
    for r in rows:
        stored = str(r["hole_id"] or "")
        canon = r["hole_id_canonical"] or canonicalize(stored)
        if canon and stored:
            out.setdefault(str(canon).upper(), (stored, r["total_depth"]))
    return out


async def _write_collars(
    conn: asyncpg.Connection, *, workspace_id: str, project_id: str,
    records: list[dict], epsg: int, georef_method: str,
    issues: RowIssues | None = None,
) -> dict[str, int]:
    """Upsert collars, one bad ROW never failing the batch (ING-1).

    Every row is checked against the table's CHECK constraints and column
    widths BEFORE it is sent (``silver_row_guard``): an out-of-range optional
    value is blanked and reported, a row missing a NOT NULL value is skipped
    and reported, and nothing is defaulted to a number nobody measured —
    ``total_depth`` used to default to 0.0, which ``chk_total_depth_positive``
    refuses, so a collar table with no EOH column failed outright; it is now
    NULL when absent (§04e, SME-approved 2026-09-29) and those collars land.

    All batches go in ONE transaction: a crash on batch 2 no longer leaves
    batch 1's 500 collars committed. Called inside the per-sheet transaction
    of ``run_ingest_tabular`` this becomes a savepoint.

    ``issues`` collects what was blanked, skipped and merged so the caller
    can turn it into the run's warnings.
    """
    from georag_geoparsers._hole_id import canonicalize  # noqa: PLC0415

    issues = issues if issues is not None else RowIssues()
    skipped_before = len(issues.skipped)
    existing = await _existing_collars_by_canonical(conn, project_id)
    existing_ids = {stored for stored, _td in existing.values()}

    rows = []
    for rec in records:
        hole_id = str(rec.get("hole_id") or "").strip()
        # Recomputed rather than trusted from the record: the rule is
        # _hole_id.canonicalize, and a record whose canonical disagrees with
        # its own hole_id must not merge two different holes.
        canon = canonicalize(hole_id)
        if hole_id and canon is None:
            # All separators ("--", "./"): no canonical key, so no collar the
            # canonical-key upsert could land on — reported, not sent.
            issues.skip(rec, f"hole id {hole_id!r} has no letters or digits")
            continue
        match = existing.get(str(canon).upper()) if canon else None
        existing_td: float | None = None
        if match is not None:
            stored_id, existing_td = match
            if hole_id and stored_id != hole_id and hole_id not in existing_ids:
                # Same hole, different separators/case: update the collar
                # that exists rather than create a ghost beside it.
                issues.merged.append((rec.get("_source_row"), hole_id, stored_id))
                rec = {**rec, "hole_id": stored_id}
        rec = {**rec, "hole_id_canonical": canon}
        values = guard_collar(rec, issues, existing_total_depth=existing_td)
        if values is None:
            continue
        if canon and match is None:
            # A second spelling of the same hole LATER IN THIS FILE lands on
            # the collar this row creates, not on a ghost of its own.
            existing[str(canon).upper()] = (values["hole_id"], values["total_depth"])
            existing_ids.add(values["hole_id"])
        rows.append((
            workspace_id, project_id,
            values["hole_id"], values["hole_id_canonical"],
            values["easting"], values["northing"], values["elevation"],
            values["total_depth"], values["azimuth"], values["dip"],
            values["hole_type"],
            rec.get("drill_date"),
            values["status"],
            georef_method,
            epsg,
            values["drill_type"], values["hole_status"],
        ))

    written = 0
    async with conn.transaction():
        for start in range(0, len(rows), _INSERT_BATCH):
            chunk = rows[start:start + _INSERT_BATCH]
            await conn.executemany(_COLLAR_SQL, chunk)
            written += len(chunk)
    return {
        "written": written,
        "skipped": len(issues.skipped) - skipped_before,
        "orphaned": 0,
    }


#: Interval tables have no natural unique key, so re-running an ingest would
#: APPEND a second copy of every row. Collars are protected by
#: ON CONFLICT (project_id, hole_id_canonical); these are not.
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
#: Within ONE ingest run the replace happens once per (table, collar): a
#: workbook with an Au sheet and a Cu/Zn sheet for the same holes, or an
#: Access database with several assay tables, used to have its second sheet
#: delete the first (ING-2). ``run_ingest_tabular`` now carries a
#: ``replaced_scope`` so later sheets of the same run append.
#:
#: The caveat, stated because it is a real workflow: if one hole's intervals
#: are split across two SEPARATE uploads (two files uploaded one after the
#: other, or two members of a ZIP — each member is its own run), loading the
#: second replaces the first. The replaced count is in the run's output; the
#: tables carry no source-file column, so which upload wrote the replaced
#: rows cannot be told apart and no warning claims to.
_INTERVAL_TABLES = {
    "survey": "silver.surveys",
    "lithology": "silver.lithology_logs",
    "sample": "silver.samples",
    # Not an interval, but the same replace-per-collar rule applies: a
    # corrected structure log replaces the holes it mentions, and re-running
    # the same file must not double every measurement on the stereonet.
    "structure": "silver.structure",
    # Same replace-per-collar rule: a corrected alteration / mineralization
    # log replaces the holes it mentions, and re-running the same file (or the
    # same lithology log, whose alteration columns feed the same tables) must
    # not stack a second copy of every interval.
    "alteration": "silver.alteration",
    "mineralization": "silver.mineralization",
}

#: Tables a geology log can ALSO feed. One source row of a log with columns
#: like ``Lith, Lith_Desc, Alteration, Alt_Intensity, Mineral1, Min1_%`` is a
#: lithology interval, an alteration interval and a mineralization interval,
#: so the file is read once per table it carries the columns of, rather than
#: asking the user to split it. The primary type keeps priority (the sheet
#: classifier decides it); a companion is written only when the headers name
#: its family explicitly.
_COMPANION_TYPES: dict[str, tuple[str, ...]] = {
    "lithology": ("alteration", "mineralization"),
    "alteration": ("mineralization",),
    "mineralization": ("alteration",),
}


async def _write_intervals(
    conn: asyncpg.Connection, *, workspace_id: str, sheet_type: str,
    records: list[dict], index: dict[str, str],
    issues: RowIssues | None = None,
    replaced_scope: set[tuple[str, str]] | None = None,
) -> dict[str, int]:
    """Write survey / lithology / sample (and structure, alteration, mineralization) rows against resolved collars.

    A sample sheet writes TWO tables in one transaction: the interval row
    into silver.samples (with its commodity_assays payload — dropped on the
    floor by this writer until 2026-08-25), and one row per element into
    silver.assays_v2, the §04e-canonical assay table every assay-side
    reader queries. Both replace, scoped to the collars the file mentions,
    for the reasons on _INTERVAL_TABLES.

    ``replaced_scope`` (ING-2) is the run's record of which
    ``(table, collar_id)`` pairs it has ALREADY replaced. A workbook with an
    ``Au_FA`` and an ``ICP_ME`` sheet, or an Access database with three assay
    tables, reaches this function once per sheet; replacing per sheet made
    the second sheet delete the first sheet's rows for the same holes. With
    a scope, a hole is cleared once per table per ingest run — the first
    sheet replaces what an EARLIER upload wrote, later sheets append.
    ``None`` keeps the per-call replace for callers outside a run.

    Rows the tables' constraints would refuse are handled per ROW, never per
    batch (ING-1): an interval with no readable or an inverted from/to is
    skipped, an out-of-range optional value (RQD / recovery / abundance
    outside 0..100) or an over-width text value is blanked, and both are
    recorded in ``issues``.
    """
    issues = issues if issues is not None else RowIssues()
    skipped_before = len(issues.skipped)

    element_ref: dict[str, str] = {}
    if sheet_type == "sample":
        element_ref = {
            r["symbol"]: r["default_unit"]
            for r in await conn.fetch(
                "SELECT symbol, default_unit FROM silver.element_reference",
            )
        }

    rows = []
    assay_rows: list[tuple] = []
    assay_skipped = 0
    orphaned = 0
    for rec in records:
        collar_id = _resolve_collar(index, rec.get("hole_id"))
        if collar_id is None:
            # Reported, never silently dropped — an assay interval for a hole
            # nobody uploaded is a completeness gap the geologist must know.
            orphaned += 1
            continue

        if sheet_type == "survey":
            depth = finite(rec.get("depth"))
            if depth is None:
                issues.skip(rec, "survey station has no readable depth")
                continue
            rows.append((
                workspace_id, collar_id, depth,
                finite(rec.get("azimuth")), finite(rec.get("dip")),
                fit_text(
                    rec, "survey_method", SURVEY_TEXT_WIDTHS["survey_method"],
                    issues, default=_SURVEY_METHOD_DEFAULT,
                ),
                _survey_azimuth_reference(rec),
            ))
        elif sheet_type == "structure":
            depth = finite(rec.get("depth"))
            if depth is None:
                # NOT NULL, and the parser already rejects such a row; kept
                # as a guard for records that did not come from it, and
                # counted rather than dropped in silence.
                issues.skip(rec, "structure measurement has no readable depth")
                continue
            rows.append((
                workspace_id, collar_id, depth,
                rec.get("structure_type") or "other",
                _num(rec.get("alpha_angle")), _num(rec.get("beta_angle")),
                _num(rec.get("true_dip")), _num(rec.get("true_dip_dir")),
                rec.get("roughness"), rec.get("infill"), rec.get("notes"),
            ))
        elif sheet_type == "alteration":
            if not rec.get("alteration_type"):
                issues.skip(rec, "alteration interval has no alteration type")
                continue
            bounds = guard_interval(rec, issues)
            if bounds is None:
                continue
            rows.append((
                workspace_id, collar_id, bounds[0], bounds[1],
                rec["alteration_type"], rec.get("intensity"),
                list(rec["minerals"]) if rec.get("minerals") else None,
                rec.get("notes"),
            ))
        elif sheet_type == "mineralization":
            if not rec.get("mineral"):
                issues.skip(rec, "mineralization interval has no mineral")
                continue
            bounds = guard_interval(rec, issues)
            if bounds is None:
                continue
            rows.append((
                workspace_id, collar_id, bounds[0], bounds[1],
                rec["mineral"],
                guard_percent(rec, "abundance_pct", MINERALIZATION_PCT_RANGE, issues),
                rec.get("form"), rec.get("grain_size"), rec.get("notes"),
            ))
        elif sheet_type == "lithology":
            bounds = guard_interval(rec, issues)
            if bounds is None:
                continue
            widths = LITHOLOGY_TEXT_WIDTHS
            code = fit_text(rec, "lithology_code", widths["lithology_code"], issues)
            description = rec.get("lithology_description")
            if code is None and not description and rec.get("lithology_code"):
                # A free-text "code" too long for varchar(20) is still the
                # logger's words: keep them in the unbounded description
                # column rather than lose them with the blanked code.
                description = str(rec["lithology_code"]).strip() or None
            rows.append((
                workspace_id, collar_id, bounds[0], bounds[1],
                code,
                description,
                fit_text(rec, "grain_size", widths["grain_size"], issues),
                fit_text(rec, "color", widths["color"], issues),
                fit_text(rec, "hardness", widths["hardness"], issues),
                guard_percent(rec, "rqd", LITHOLOGY_PERCENT_RANGE, issues),
                guard_percent(rec, "recovery", LITHOLOGY_PERCENT_RANGE, issues),
                fit_text(rec, "weathering", widths["weathering"], issues),
            ))
        else:  # sample
            bounds = guard_interval(rec, issues)
            if bounds is None:
                continue
            rows.append((
                workspace_id, collar_id, bounds[0], bounds[1],
                fit_text(
                    rec, "sample_type", SAMPLE_TEXT_WIDTHS["sample_type"],
                    issues, default=_SAMPLE_TYPE_DEFAULT,
                ),
                fit_text(rec, "lab_id", SAMPLE_TEXT_WIDTHS["lab_id"], issues),
                fit_text(rec, "qaqc_type", SAMPLE_TEXT_WIDTHS["qaqc_type"], issues),
                json.dumps(rec.get("commodity_assays") or {}),
                (
                    json.dumps(rec["commodity_assay_flags"])
                    if rec.get("commodity_assay_flags") else None
                ),
            ))
            exploded, exploded_skipped = derive_assay_v2_rows(
                rec,
                workspace_id=workspace_id,
                collar_id=collar_id,
                element_ref=element_ref,
            )
            assay_rows.extend(exploded)
            assay_skipped += exploded_skipped

    sql = {
        "survey": _SURVEY_SQL,
        "structure": _STRUCTURE_SQL,
        "lithology": _LITHOLOGY_SQL,
        "alteration": _ALTERATION_SQL,
        "mineralization": _MINERALIZATION_SQL,
        "sample": _SAMPLE_SQL,
    }[sheet_type]

    # Replace, don't append — see _INTERVAL_TABLES. Scoped to the collars this
    # file actually mentions, inside the same transaction as the insert so a
    # failure cannot leave the holes emptied — and, within one run, to the
    # collars no earlier sheet of this run has already replaced (ING-2).
    table = _INTERVAL_TABLES[sheet_type]
    mentioned = sorted({r[1] for r in rows})
    touched = [
        cid for cid in mentioned
        if replaced_scope is None or (table, cid) not in replaced_scope
    ]
    assay_touched = [
        cid for cid in mentioned
        if replaced_scope is None or ("silver.assays_v2", cid) not in replaced_scope
    ] if sheet_type == "sample" else []
    replaced = 0
    assay_replaced = 0
    written = 0

    async with conn.transaction():
        if touched:
            replaced = int(
                await conn.fetchval(
                    f"WITH d AS (DELETE FROM {table} "  # noqa: S608
                    "WHERE collar_id = ANY($1::uuid[]) RETURNING 1) "
                    "SELECT count(*) FROM d",
                    touched,
                ) or 0
            )
        if assay_touched:
            # Same replace semantics for the canonical assay table —
            # a corrected sample file must replace its holes' element
            # rows too, or the doubled-composite failure the interval
            # tables guard against comes back one table over.
            assay_replaced = int(
                await conn.fetchval(
                    "WITH d AS (DELETE FROM silver.assays_v2 "
                    "WHERE collar_id = ANY($1::uuid[]) RETURNING 1) "
                    "SELECT count(*) FROM d",
                    assay_touched,
                ) or 0
            )

        for start in range(0, len(rows), _INSERT_BATCH):
            chunk = rows[start:start + _INSERT_BATCH]
            await conn.executemany(sql, chunk)
            written += len(chunk)

        for start in range(0, len(assay_rows), _INSERT_BATCH):
            chunk = assay_rows[start:start + _INSERT_BATCH]
            await conn.executemany(_ASSAYS_V2_SQL, chunk)

    if replaced_scope is not None:
        # Recorded once this call's transaction (or savepoint) has
        # succeeded. The caller rolls the scope back with the sheet if the
        # enclosing per-sheet transaction fails afterwards.
        replaced_scope.update((table, cid) for cid in touched)
        replaced_scope.update(("silver.assays_v2", cid) for cid in assay_touched)

    stats = {
        "written": written,
        "skipped": len(issues.skipped) - skipped_before,
        "orphaned": orphaned,
        "replaced": replaced,
    }
    if sheet_type == "sample":
        stats["assay_rows"] = len(assay_rows)
        stats["assay_rows_skipped"] = assay_skipped
        stats["assay_replaced"] = assay_replaced
    return stats


def _result_headers(result: Any) -> list[str]:
    """Every column a parser saw: the ones it mapped plus the ones it did not."""
    return [
        *(getattr(result, "column_map", None) or {}).values(),
        *(getattr(result, "unmapped_columns", None) or []),
    ]


def _companion_types_present(
    result: Any, write_type: str,
    column_map: dict[str, dict[str, str]] | None,
) -> list[str]:
    """The other tables the file just parsed as *write_type* also feeds.

    Read off the headers the primary parse saw, by the same token rules the
    classifier uses (``has_family_evidence``): a companion needs a column that
    NAMES the family (``Alteration``, ``Mineral1``, ``Mineralization``). A
    table the user mapped an alteration / mineralization column for counts too.
    """
    from georag_geoparsers._geology_columns import has_family_evidence  # noqa: PLC0415

    headers = _result_headers(result)
    out: list[str] = []
    for companion in _COMPANION_TYPES.get(write_type, ()):
        if has_family_evidence(headers, companion) or (column_map or {}).get(companion):
            out.append(companion)
    return out


#: Columns listed in a not-ingested warning.
_NOT_INGESTED_MAX_COLUMNS = 12


def _columns_not_ingested_warning(
    *, label: str, write_type: str, columns: list[str],
) -> dict[str, Any] | None:
    """Say which columns of a geology log matched no field, so were not read.

    A lithology / alteration / mineralization parse maps the columns it knows
    and ignores the rest; the ignoring used to be a log line. Data the
    geologist typed - a second description column, a vein log, a logger's
    name - therefore vanished with no trace in the run. The columns stay in
    bronze (the uploaded file); nothing is invented for them.
    """
    if not columns:
        return None
    shown = columns[:_NOT_INGESTED_MAX_COLUMNS]
    more = len(columns) - len(shown)
    names = ", ".join(repr(c[:60]) for c in shown) + (f" and {more} more" if more else "")
    return {
        "code": "columns_not_ingested",
        "message": (
            f"{len(columns)} column(s) of {label} were not recognised and were "
            f"not read as {write_type} data"
        ),
        "detail": (
            f"{label} was read as {write_type} data. These columns matched no "
            f"field, so their values are not in the drillhole tables: {names}. "
            f"The file itself is kept in bronze. If one of them is a value you "
            f"expect to see (a second description, a mineral, an alteration), "
            f"rename it to a recognised header or map it explicitly and "
            f"re-upload."
        )[:900],
        "columns": shown,
    }


def _csv_preamble_warning(path: str, filename: str) -> dict[str, Any] | None:
    """Say that title/comment lines above a CSV's header were skipped (ING-13).

    ``_csv_io.open_csv_with_encoding`` drops them for every reader, so the
    parsers and ``_csv_headers`` see the real header; this is the note that
    tells the geologist which lines were not read as data.
    """
    from georag_geoparsers._csv_io import open_csv_with_encoding  # noqa: PLC0415

    stream, _encoding, _sha, _size = open_csv_with_encoding(path)
    skipped = int(getattr(stream, "preamble_lines", 0) or 0)
    if not skipped:
        return None
    return {
        "code": "header_row_detected",
        "message": (
            f"{filename}: {skipped} line(s) above the column headers were read "
            f"as a title or comments and skipped"
        ),
        "detail": (
            f"The first {skipped} line(s) of {filename} are not part of the "
            f"table (a title, notes or '#' comments), so the column headers "
            f"were taken from line {skipped + 1} and the lines above it were "
            f"not read as data."
        ),
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


#: How the CSV parsers report a FILE-level refusal: one
#: ``skipped_details`` entry with ``row`` unset, ``code`` ==
#: ``"missing_required"``, and a ``reason`` naming the column set no alias
#: matched (csv_collar.py:369, csv_survey.py:348, csv_lithology.py:426,
#: csv_sample.py:889). ``parse_xlsx_sheet`` serialises the sheet and hands
#: it to those same parsers, so a workbook sheet carries the identical
#: shape. The same code ALSO tags per-row skips ("row 12 is missing
#: hole_id"), which is why ``row`` must be checked as well: those are not
#: the file-level refusal and there can be thousands of them.
_FILE_LEVEL_REFUSAL_CODE = "missing_required"

#: ``frozenset({'hole_id'})`` is a repr, not English. The column names
#: inside it are the whole point of the message and are kept verbatim.
_FROZENSET_REPR = re.compile(r"frozenset\(\{(.*?)\}\)")


def _readable_reason(reason: str) -> str:
    """The parser's own words with the Python-only shapes taken out.

    The refusal arrives as ``file-level: missing required column
    mapping(s): frozenset({'hole_id'})``. ``file-level:`` is internal
    bookkeeping and the frozenset wrapper is noise; what a geologist
    needs is the column names.
    """
    cleaned = _FROZENSET_REPR.sub(r"\1", reason).strip()
    return cleaned.removeprefix("file-level:").strip()


def _refusal_reason(result: Any) -> str | None:
    """Why a writer got no records, in the parser's own words.

    Returns None when the parse reported no file-level refusal — the
    caller then says only that the writer required columns the sheet does
    not have, rather than inventing a specific reason it cannot source.
    """
    for detail in getattr(result, "skipped_details", None) or []:
        if not isinstance(detail, dict):
            continue
        if detail.get("row") is not None:
            continue    # a per-row skip, not the file-level refusal
        if detail.get("code") == _FILE_LEVEL_REFUSAL_CODE and detail.get("reason"):
            return _readable_reason(str(detail["reason"]))
    return None


def _rows_rejected_warning(
    *, label: str, write_type: str, result: Any, written: int,
) -> dict[str, Any] | None:
    """Say how many rows of a PARTLY-landed sheet the parser rejected, and why.

    The parsers validate row by row and drop the rows that fail — a
    lithology interval whose optional ``Texture`` or ``Weathering`` value is
    outside the fixed vocabulary, a survey station with a positive dip in a
    file too short to detect the convention, a from/to pair that is inverted,
    a collar with no coordinates. Each drop is recorded in
    ``skipped_details``, but this workflow only read that list when NOTHING
    was written (``_refusal_reason``). A 3,000-row lithology log that landed
    1,100 rows therefore closed with the same headline as one that landed all
    3,000, and the strip logs for the other holes were simply missing.

    Returns None when nothing was rejected, or when nothing was written — the
    all-rejected case already has the ``wrote_nothing`` warning and its
    reason.
    """
    if not written:
        return None
    details = [
        d for d in (getattr(result, "skipped_details", None) or [])
        if isinstance(d, dict) and d.get("row") is not None
    ]
    total_rows = int(getattr(result, "total_rows", 0) or 0)
    valid_rows = int(getattr(result, "valid_rows", 0) or 0)
    rejected = int(getattr(result, "skipped_rows", 0) or 0) or len(details)
    if rejected <= 0:
        return None

    by_code: dict[str, int] = {}
    for d in details:
        by_code[str(d.get("code") or "unknown")] = (
            by_code.get(str(d.get("code") or "unknown"), 0) + 1
        )
    breakdown = ", ".join(
        f"{code} x{n}" for code, n in sorted(by_code.items(), key=lambda kv: -kv[1])
    )
    first = next((str(d["reason"]) for d in details if d.get("reason")), "")
    of_total = f" of {total_rows}" if total_rows else ""
    return {
        "code": "rows_rejected",
        "message": (
            f"{rejected}{of_total} {write_type} row(s) in {label} were rejected "
            f"({valid_rows or written} kept)"
        ),
        "detail": (
            f"{label} was read as {write_type} data and {written} row(s) "
            f"landed, but {rejected} failed validation and were left out"
            + (f" ({breakdown})" if breakdown else "")
            + (f". First: {first}" if first else "")
            + ". The rejected rows are not in silver; fix the values (or map "
            "the columns) and re-upload the file to replace this hole's rows."
        )[:900],
    }


def _fold_writer_skips(result: Any, issues: RowIssues) -> None:
    """Record the writer's per-row skips on the parse result (ING-1).

    ``skipped_details`` is where a skipped row's reason lives for every other
    rejection, and ``_rows_rejected_warning`` summarises from it; a row the
    writer refused because the table cannot hold it is the same kind of loss
    and must be counted the same way. A result object that cannot take the
    update (a test double, a frozen dataclass) is left alone — the
    ``db_constraint_rows_skipped`` warning still reports the rows.
    """
    details = issues.skipped_details()
    if not details:
        return
    existing = getattr(result, "skipped_details", None)
    if isinstance(existing, list):
        existing.extend(details)
    for attr, delta in (("skipped_rows", len(details)), ("valid_rows", -len(details))):
        current = getattr(result, attr, None)
        if isinstance(current, int) and not isinstance(current, bool):
            try:
                setattr(result, attr, max(0, current + delta))
            except AttributeError:
                log.debug(
                    "ingest_tabular: parse result %s is read-only; the writer "
                    "skips are reported by their own warning",
                    type(result).__name__, exc_info=True,
                )
                return


#: Exceptions that mean the DATABASE or the worker is in trouble, not that
#: one sheet's data is bad. These still fail the run so Hatchet retries it;
#: anything else raised while writing one sheet costs that sheet only.
_INFRASTRUCTURE_ERRORS: tuple[type[BaseException], ...] = (
    asyncpg.InterfaceError,
    asyncpg.PostgresConnectionError,
    asyncpg.exceptions.OperatorInterventionError,
    asyncpg.exceptions.InsufficientResourcesError,
    asyncpg.exceptions.TransactionRollbackError,
    OSError,
    TimeoutError,
    MemoryError,
)


def _sheet_write_failed_warning(
    *, label: str, sheet_type: str, exc: BaseException, table_source: bool,
) -> dict[str, Any]:
    """Say that ONE sheet/table failed to write, and that nothing of it landed."""
    kept = (
        "every row is in the attribute table and in bronze"
        if table_source else
        "the file is in bronze and the sheet was kept as searchable text"
    )
    return {
        "code": "typed_table_write_failed",
        "message": (
            f"'{label}' looks like {sheet_type} data but could not be written "
            f"as {sheet_type} rows"
        ),
        "detail": (
            f"'{label}' classified as {sheet_type} and writing it as "
            f"{sheet_type} rows failed: {str(exc)[:300]}. None of its rows "
            f"were written (the sheet is all-or-nothing) and the other sheets "
            f"were unaffected. The data is not lost - {kept} - and "
            f"re-ingesting will retry."
        ),
    }


def _assumed_crs_warning(epsg: int, collars_written: int) -> dict[str, Any]:
    """Say that these collars were placed by guess, and name the guess.

    GIS-2 (Kyle, 2026-09-29): undeclared projected coordinates are still
    placed at the platform default rather than refused, so this warning is
    the only thing standing between the geologist and a hole drawn in the
    wrong zone. It names the assumption, what it costs when wrong, and the
    two places a CRS can be declared.
    """
    named = " (WGS 84 / UTM zone 13N, the platform default)" if epsg == 32613 else ""
    return {
        "code": "collar_crs_assumed",
        "message": (
            f"{collars_written} collar(s) placed using an ASSUMED coordinate "
            f"system (EPSG:{epsg}) — no CRS was declared"
        ),
        "detail": (
            f"Neither this upload nor its project declares a coordinate "
            f"system, so the easting/northing values were read as "
            f"EPSG:{epsg}{named}. Nothing in a CSV or spreadsheet declares a "
            f"projection, so this cannot be detected from the file. If the "
            f"holes were surveyed in any other zone or "
            f"datum they are now in the wrong place on the map: another UTM "
            f"zone is hundreds to thousands of kilometres off, NAD27 in the "
            f"same zone about 200 m. To fix it, re-upload with the correct EPSG "
            f"code: type it for this file in the Import wizard, or set the "
            f"project's coordinate system (Edit project -> CRS / EPSG) so every "
            f"future upload uses it. Re-uploading replaces these collars in "
            f"place."
        ),
    }


async def _resolve_table_crs(
    conn: asyncpg.Connection,
    *,
    project_id: str,
    label: str,
    records: list[dict[str, Any]],
    easting_column: str | None,
    northing_column: str | None,
    declared_epsg: int | None,
    project_epsg: int | None,
    id_key: str = "hole_id",
    x_key: str = "easting",
    y_key: str = "northing",
) -> tuple[Any, set[str]]:
    """Decide one table's source CRS and check where it puts the rows.

    GIS-1 / GIS-2 / GIS-13 — see app/services/ingest/collar_crs.py. Returns
    the ``CollarCrsDecision`` (its ``warnings`` already carry the
    plausibility findings) and the ids of the rows flagged implausible.
    """
    from app.services.ingest.collar_crs import (  # noqa: PLC0415
        decide_collar_crs,
        plausibility_warnings,
        project_reference,
    )

    xs = [_num(r.get(x_key)) for r in records]
    ys = [_num(r.get(y_key)) for r in records]
    decision = decide_collar_crs(
        eastings=xs, northings=ys,
        easting_column=easting_column, northing_column=northing_column,
        declared_epsg=declared_epsg, project_epsg=project_epsg,
        default_epsg=DEFAULT_SOURCE_EPSG, label=label,
    )
    if decision.refusal is not None:
        return decision, set()

    points = [
        (str(r.get(id_key)), x, y)
        for r, x, y in zip(records, xs, ys, strict=True)
        if r.get(id_key) and x is not None and y is not None
    ]
    if not points:
        return decision, set()
    reference = await project_reference(
        conn, project_id, exclude_hole_ids=[p[0] for p in points],
    )
    found, flagged = await asyncio.to_thread(
        plausibility_warnings,
        epsg=decision.epsg, points=points, reference=reference, label=label,
    )
    decision.warnings.extend(found)
    return decision, flagged


async def _stamp_crs_confidence(
    conn: asyncpg.Connection,
    *,
    project_id: str,
    hole_ids: list[str],
    confidence: float,
    flagged: set[str],
) -> None:
    """Record how far each written collar's CRS is to be believed.

    silver.collars.crs_confidence was never written by this path, so the
    MVT's crs_confidence was NULL for every tabular collar. Flagged
    (implausible) collars get 0.1. Best-effort in a savepoint: a failure
    here must not undo collars that are already written.
    """
    if not hole_ids:
        return
    try:
        async with conn.transaction():
            await conn.execute(
                "UPDATE silver.collars SET crs_confidence = CASE "
                "WHEN hole_id = ANY($3::text[]) THEN LEAST($4::real, 0.1::real) "
                "ELSE $4::real END "
                "WHERE project_id = $1::uuid AND hole_id = ANY($2::text[])",
                project_id, hole_ids, sorted(flagged), confidence,
            )
    except Exception as exc:  # noqa: BLE001 — best-effort; the collars are already written
        log.warning(
            "ingest_tabular: could not record crs_confidence for %s: %s",
            project_id, exc,
        )


def _remap_facts(result: Any, sheet_type: str) -> dict[str, Any] | None:
    """What the UI needs to offer a column mapping for a refused sheet.

    Read off the parse result rather than out of the refusal message: every
    parser's result dataclass carries ``column_map`` and
    ``unmapped_columns``, so one shape covers all four, and the UI is
    offering the columns the parser ACTUALLY saw rather than a list
    reconstructed from prose.

    Returns None when nothing is missing — there is then no mapping to
    offer, and a control that appears over a sheet with no gap is noise.

    ``columns`` deliberately includes the ones that DID map. A user
    correcting a mis-match ("that is not the easting, this is") needs the
    whole column list, not only the leftovers.
    """
    from georag_geoparsers._drill_schema import schemas  # noqa: PLC0415

    entry = schemas().get(sheet_type)
    if entry is None:
        return None
    _aliases, required = entry

    mapped: dict[str, str] = dict(getattr(result, "column_map", None) or {})
    unmapped: list[str] = list(getattr(result, "unmapped_columns", None) or [])
    missing = sorted(set(required) - set(mapped))
    if not missing:
        return None

    columns = sorted({*mapped.values(), *unmapped})
    if not columns:
        return None

    return {
        "sheet_type": sheet_type,
        "missing": missing,
        "mapped": dict(sorted(mapped.items())),
        "columns": columns,
    }


def _wrote_nothing_warning(
    *, label: str, classified_as: str, reason: str | None,
    from_category: bool = False,
    headers_matched: str | None = None,
    retry_reason: str | None = None,
    remap: dict[str, Any] | None = None,
    table_source: bool = False,
) -> dict[str, Any]:
    """Say which sheet was refused, what it was taken for, and why.

    ``table_source`` is a dBASE/DAT/Access table: it has no text form, so the
    sheet-as-searchable-text fallback does not apply to it, and the honest
    statement is that its rows are in the data-table copy (which is written
    for every such table regardless).

    ``message`` AND ``detail``: the Ingestion Runs page renders
    ``detail``, falling back to ``code`` — a warning with neither shows
    the geologist a bare token like ``classified_but_nothing_written``.

    ``from_category`` separates the two ways a sheet arrives at a writer,
    which the first version of this message conflated. A workbook sheet is
    CLASSIFIED by its headers; a single-table upload is TOLD what it is by
    the category it was dropped into, and the classifier never runs. Saying
    "matched the collar layout" about the second case is simply false --
    the customer's FA16099231_edit.csv is a 66-column assay certificate
    whose headers match no drill table at all, and it reached the collar
    writer only because it was uploaded under `collars`. Being told the
    file matched a layout it does not match sends the geologist off to
    rename columns that were never the problem.

    ``headers_matched`` and ``retry_reason`` carry the outcome of the
    header re-check that now runs after every category-forced refusal
    (see the retry block in run_ingest_tabular): by the time this warning
    is emitted for a forced sheet, the classifier HAS looked at the
    headers, and the advice can be specific instead of speculative. The
    first version of the forced message said "leave the category off and
    let the headers decide" — advice that cannot be followed:
    UploadController requires a category on every upload and the wizard
    fills one in from the extension, so there has never been a way to
    leave it off.
    """
    because = reason or (
        "the writer required columns this sheet does not have"
    )
    if from_category:
        how = (
            f"'{label}' was uploaded to the {classified_as} category, so it "
            f"was sent to the {classified_as} writer without its headers "
            f"being checked first"
        )
        if headers_matched is None:
            fix = (
                f"Its headers were then checked against every drill layout "
                f"and matched none, so this looks like a non-drill table "
                f"and the data-table copy is the intended landing for it. "
                f"If it really is {classified_as} data, rename its columns "
                f"to ones the {classified_as} parser recognises and "
                f"re-upload."
            )
        elif headers_matched == classified_as:
            # Only point "above" at column names when the refusal actually
            # named columns. A file whose headers all map but whose rows
            # were refused one by one has no file-level reason, and telling
            # the user to add columns they already have is the class of
            # advice this function exists to kill.
            fix = (
                f"Its headers do match the {classified_as} layout, so the "
                f"missing column(s) named above are the specific gap — add "
                f"them and re-upload."
            ) if reason is not None else (
                f"Its headers do match the {classified_as} layout — the "
                f"rows themselves were refused, and the parser notes "
                f"beside this one report the row-level problems."
            )
        else:
            second = retry_reason or (
                "the writer required columns this sheet does not have"
            )
            fix = (
                f"Its headers match the {headers_matched} layout instead, "
                f"but re-read as {headers_matched} it was refused again: "
                f"{second}."
            )
    else:
        how = (
            f"'{label}' matched the {classified_as} layout, so it was sent "
            f"to the {classified_as} writer"
        )
        fix = (
            f"If this is not {classified_as} data, re-upload it with the "
            f"right type or rename its columns to ones the "
            f"{classified_as} parser recognises."
        )
    kept = (
        "The table was kept whole as a data table (every column, every "
        "row); it is not in the drillhole tables."
        if table_source else
        "The sheet was kept as searchable text and, where its columns "
        "allow, as a data table; the warnings beside this one report what "
        "landed."
    )
    warning: dict[str, Any] = {
        "code": "classified_but_nothing_written",
        "message": (
            f"'{label}' was treated as a {classified_as} sheet, but no "
            f"{classified_as} rows could be written"
        ),
        "detail": (
            f"{how} — which accepted none of its rows: {because}. No "
            f"{classified_as} rows were written. {kept} {fix}"
        ),
    }
    if remap is not None:
        # Structured, so the Ingestion Runs page can offer the columns this
        # sheet actually has instead of repeating the prose above. `label`
        # rides along because a workbook's warnings all land in one array
        # and the control has to know which sheet it is correcting.
        warning["remap"] = {"label": label, **remap}
    return warning


def _category_corrected_warning(
    *, label: str, forced_as: str, matched: str,
    written: int, orphaned: int,
) -> dict[str, Any]:
    """Say the category was wrong, what the headers said, and what landed.

    Emitted INSTEAD of _wrote_nothing_warning when the post-refusal header
    re-check found a different drill layout and that writer accepted the
    rows. The category is a hint, not a verdict: a survey file dropped into
    `collars` — or left on the wizard's `.csv` default, which IS `collars` —
    used to land as prose and a generic table. Now it lands as survey rows,
    and this warning is how the geologist learns the category on the next
    upload of the same file.
    """
    landed = f"{written} {matched} row(s) were written"
    if orphaned:
        landed += (
            f" and {orphaned} row(s) are waiting for their collars to be "
            f"uploaded"
        )
    return {
        "code": "category_corrected",
        "message": (
            f"'{label}' was uploaded as {forced_as} but its headers match "
            f"the {matched} layout — written as {matched} instead"
        ),
        "detail": (
            f"'{label}' was uploaded to the {forced_as} category, but the "
            f"{forced_as} writer could accept none of its rows, and its "
            f"headers match the {matched} layout. It was read as {matched} "
            f"data instead: {landed}. If it really is {forced_as} data, "
            f"rename its columns to ones the {forced_as} parser recognises "
            f"and re-upload."
        ),
    }


def _read_delimited_rows(path: str) -> list[dict[str, Any]]:
    """A delimited file's data rows as dicts, keyed by its header row.

    Goes through the same ``_csv_io`` helpers ``_csv_headers`` uses, for the
    same reason: these files arrive Latin-1 from Windows survey software and
    semicolon-delimited from European labs, and a table split on the wrong
    delimiter lands as one column of garbage.
    """
    import csv  # noqa: PLC0415

    from georag_geoparsers._csv_io import (  # noqa: PLC0415
        detect_delimiter,
        open_csv_with_encoding,
    )

    stream, _encoding, _sha, _size = open_csv_with_encoding(path)
    content = stream.read()
    reader = csv.DictReader(
        content.splitlines(), delimiter=detect_delimiter(content),
    )
    return [dict(row) for row in reader]


async def _land_unclassified_as_rows(
    conn: Any,
    *,
    path: str,
    suffix: str,
    filename: str,
    unclassified: list[str],
    workspace_id: str,
    project_id: str,
) -> dict | None:
    """Keep a non-drill table's VALUES, not just its prose.

    The text fallback beside this one makes an unrecognised sheet
    answerable in chat, which is the floor. It is not the same as having
    the data: a geochemical certificate rendered to passages cannot be
    filtered by Au_ppm, and 100 samples of 66 elements read back as a wall
    of numbers. silver.attribute_tables already stores exactly this shape
    for a standalone .dbf -- one JSON object per row, keyed by the source
    file and layer -- so a sheet that matches no drill type lands there
    rather than nowhere.

    Never raises. The typed rows and the text passages have already landed
    by this point, and losing the structured copy must not turn a run that
    wrote them into a failure.
    """
    if suffix in DBASE_EXTENSIONS:
        # A standalone .dbf/.dat already lands in this exact table through the
        # preflight branch, and never reaches `unclassified`. Guarded anyway
        # because the alternative failure is silent: the delimited reader
        # below would happily read a binary dBASE file as text and write a
        # table of mojibake next to the real one.
        return None

    total = 0
    layers = 0
    try:
        sha = await asyncio.to_thread(_sha256_file, path)
        # A delimited file is one table however many labels the caller
        # collected for it, so it is read once. Looping would write the same
        # rows under each label -- the row_index upsert key would not catch
        # it, because `source_layer` is part of that key.
        labels = unclassified if suffix in EXCEL_EXTENSIONS else unclassified[:1]
        for label in labels:
            if suffix in EXCEL_EXTENSIONS:
                from georag_geoparsers.xlsx_parser import (  # noqa: PLC0415
                    read_sheet_rows,
                )

                rows = await asyncio.to_thread(read_sheet_rows, path, label)
            else:
                rows = await asyncio.to_thread(_read_delimited_rows, path)
            if not rows:
                continue
            stats = await _write_attribute_rows(
                conn,
                workspace_id=workspace_id, project_id=project_id,
                source_file=filename, source_file_sha256=sha,
                source_layer=label, rows=rows,
            )
            total += stats.get("written", 0)
            layers += 1
    except Exception as exc:  # noqa: BLE001 — typed rows and text already landed
        log.warning(
            "ingest_tabular: attribute-row fallback failed for %s (%s)",
            path, exc,
        )
        return None

    if not total:
        return None

    where = f"{layers} sheet(s)" if layers > 1 else "it"
    return {
        "code": "unclassified_kept_as_table",
        "rows": total,
        "message": f"{total} row(s) kept as a data table",
        "detail": (
            f"{total} row(s) from {where} were also kept as a data table, "
            f"with every column preserved, so the values stay queryable "
            f"even though they are not collar / survey / lithology / "
            f"sample rows and will not appear in the drillhole views."
        ),
    }


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

    The success warning carries ``passages`` beside its prose. The count
    is already in the sentence; it is repeated as an integer because the
    caller has to add it to ``rows_written``, and re-reading it out of
    the English is how that number goes wrong. Extra keys are inert on
    the Ingestion Runs page, which reads ``detail`` and falls back to
    ``code``.
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

    if not result.skipped and result.document_id and not result.passages_inserted:
        # Already indexed. land_sheets_as_text dedupes on the file sha within
        # the project, so a re-upload of the same workbook finds the existing
        # report and inserts nothing new. Saying "produced no searchable text"
        # there reads as a failure when the content is in fact already
        # answerable -- the same false-negative as the "no data written"
        # headline this change set exists to fix.
        return {
            "code": "unclassified_already_indexed",
            "detail": (
                f"{len(unclassified)} sheet(s) matched no drill type "
                f"({names}{more}). This file was already indexed, so no new "
                "passages were added; its contents are still answerable in chat."
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
        "passages": int(result.passages_inserted),
        "detail": (
            f"{len(unclassified)} sheet(s) matched no collar / survey / "
            f"lithology / sample layout ({names}{more}) and were indexed as "
            f"{result.passages_inserted} searchable passage(s) instead. They "
            f"are answerable in chat but will not appear in the drillhole, "
            f"map or cross-section views."
        ),
    }


def _vendor_aliases_for(
    column_map: dict[str, dict[str, str]] | None, sheet_type: str,
) -> dict[str, list[str]] | None:
    """Turn a user's confirmed mapping into the parsers' alias shape.

    ``column_map`` is ``{sheet_type: {canonical_field: source column}}`` —
    keyed by drill type rather than by sheet name so one workbook can carry
    a different mapping for its collar sheet and its lithology sheet, and so
    a loose ``.csv`` and the same table inside an ``.xlsx`` are described
    identically.

    Each entry becomes a single-element alias list, which
    ``merge_vendor_aliases`` puts AHEAD of the built-in spellings. That is
    the whole enforcement mechanism: the user's choice wins because it is
    matched first, not because anything special-cases it.
    """
    fields = (column_map or {}).get(sheet_type)
    if not fields:
        return None
    return {
        canonical: [source]
        for canonical, source in fields.items()
        if isinstance(source, str) and source.strip()
    }


def _table_columns(rows: list[dict[str, Any]]) -> list[str]:
    """Column names of a row-dict table, in first-seen order.

    A UNION over every row, not ``list(rows[0])``: mdb-json omits a key whose
    value is NULL in that row (see access_mdb.read_table), so the first row of
    an Access table can be missing a column that every later row carries.
    """
    seen: dict[str, None] = {}
    for row in rows:
        for key in row:
            seen.setdefault(key)
    return list(seen)


def _rows_as_csv_stream(rows: list[dict[str, Any]]) -> Any:
    """A dBASE/DAT/Access table as the in-memory CSV the CSV parsers read.

    The same trick ``parse_xlsx_sheet`` uses for a worksheet, for the same
    reason: hole-ID canonicalisation, range checks, dip-convention and
    unit-ambiguity handling live in the CSV parsers, and re-implementing them
    per source would fork the most heavily audited logic in the pipeline.
    Everything is written as text and the parsers cast for themselves.
    """
    import csv  # noqa: PLC0415
    import io  # noqa: PLC0415

    buffer = io.StringIO()
    writer = csv.DictWriter(
        buffer, fieldnames=_table_columns(rows), restval="",
        extrasaction="ignore",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if v is None else v) for k, v in row.items()})
    buffer.seek(0)
    return buffer


def _parse_rows(
    rows: list[dict[str, Any]],
    sheet_type: str,
    column_map: dict[str, dict[str, str]] | None = None,
    companion: bool = False,
) -> Any:
    """Run the CSV parser for *sheet_type* over an in-memory table.

    ``companion`` reads only the alteration / mineralization columns of a table
    that is primarily a lithology log (see _COMPANION_TYPES).
    """
    extra = {"companion": True} if companion else {}
    return _csv_parser_for(sheet_type)(
        _rows_as_csv_stream(rows),
        vendor_aliases=_vendor_aliases_for(column_map, sheet_type),
        **extra,
    )


def _typed_verdict_for_table(
    rows: list[dict[str, Any]],
    column_map: dict[str, dict[str, str]] | None = None,
    *,
    dbase_side_writes: bool = False,
) -> tuple[str, float]:
    """The drill type a dBASE/DAT/Access table's headers classify as.

    The SAME classifier the CSV and workbook paths use
    (``_sheet_classifier.classify_sheet_type``), so a table is a collar /
    survey / lithology / sample / structure table by exactly the rule that
    would apply to the identical columns in a CSV or a worksheet. Returns
    ``("unknown", 0.0)`` when nothing classifies - the table then behaves
    as it always did, landing only in silver.attribute_tables.

    ``dbase_side_writes`` gives the two shapes that already have their own
    dedicated writers precedence over the generic route, so the same rows
    are not written twice by two writers that disagree:

      * a Discover/MapInfo drillhole TRACE export has hole/depth/azimuth/dip,
        so it would classify as a survey - but its rows are segment
        midpoints, and ``_collapse_discover_traces`` exists precisely
        because treating them as stations bends every hole off its collar;
      * a surface-geochemistry table is a sample with a location and no hole.
    """
    columns = _table_columns(rows)
    if not columns:
        return "unknown", 0.0
    if dbase_side_writes and (
        _discover_trace_columns(columns) is not None
        or _surface_geochem_columns(columns) is not None
    ):
        return "unknown", 0.0

    from georag_geoparsers._sheet_classifier import (  # noqa: PLC0415
        classify_sheet_type,
    )

    sheet_type, confidence = classify_sheet_type(columns, column_map=column_map)
    return sheet_type, confidence


def _csv_parser_for(sheet_type: str) -> Any:
    """The georag_geoparsers CSV parser that reads *sheet_type*."""
    from georag_geoparsers import (  # noqa: PLC0415
        parse_csv_alteration,
        parse_csv_collars,
        parse_csv_lithology,
        parse_csv_mineralization,
        parse_csv_samples,
        parse_csv_structures,
        parse_csv_surveys,
    )

    return {
        "collar": parse_csv_collars,
        "survey": parse_csv_surveys,
        "structure": parse_csv_structures,
        "lithology": parse_csv_lithology,
        "alteration": parse_csv_alteration,
        "mineralization": parse_csv_mineralization,
        "sample": parse_csv_samples,
    }[sheet_type]


def _parse_one(
    path: str,
    sheet_type: str,
    sheet_name: str | None,
    column_map: dict[str, dict[str, str]] | None = None,
    companion: bool = False,
) -> Any:
    """Run the parser matching *sheet_type*."""
    parser = _csv_parser_for(sheet_type)

    vendor_aliases = _vendor_aliases_for(column_map, sheet_type)
    extra = {"companion": True} if companion else {}

    if sheet_name is None:
        return parser(path, vendor_aliases=vendor_aliases, **extra)

    # A workbook sheet is materialised to CSV first so the CSV parsers —
    # which carry the delimiter, encoding, decimal-comma, hole-ID and
    # unit-ambiguity handling — apply unchanged. Reimplementing that per
    # sheet would fork the most heavily audited logic in the pipeline.
    from georag_geoparsers.xlsx_parser import parse_xlsx_sheet  # noqa: PLC0415

    return parse_xlsx_sheet(
        path,
        sheet_name=sheet_name,
        sheet_type=sheet_type,
        vendor_aliases=vendor_aliases,
        **extra,
    )


#: A radiometric-age table (ING-19, 2026-09-29) -> silver.geochronology_samples.
#:
#: Deliberately NOT in WRITE_ORDER: its rows key on a sample, not a hole, so
#: they neither wait for collars nor appear in the drill classifier. Routed
#: by ``csv_geochronology.geochronology_signal`` instead: a sample id, an age
#: and an isotopic-system column ("strong") claim a table even when it also
#: carries hole/depth columns — a drill-core dating table is still a dating
#: table — and a sample id, an age and a method column ("weak") claim only a
#: table no drill layout wanted. The upload category ``geochronology`` sends
#: the hint.
GEOCHRONOLOGY_TYPE = "geochronology"


def _routes_to_geochronology(
    headers: list[str], drill_type: str | None, *, hinted: bool = False,
) -> bool:
    """Whether a table with these headers is written as radiometric ages."""
    from app.services.ingest.geochronology_writer import (  # noqa: PLC0415
        routes_to_geochronology,
    )

    return routes_to_geochronology(
        headers, drill_type, drill_types=WRITE_ORDER, hinted=hinted,
    )


def _parse_geochronology(
    path: str,
    *,
    label: str,
    sheet_name: str | None,
    rows: list[dict[str, Any]] | None,
) -> Any:
    """Parse one table as geochronology: CSV on disk, a worksheet, or rows."""
    from georag_geoparsers.csv_geochronology import (  # noqa: PLC0415
        parse_csv_geochronology,
        parse_geochronology_rows,
    )

    if rows is not None:
        return parse_geochronology_rows(rows, source_label=label)
    if sheet_name is not None:
        from georag_geoparsers.xlsx_parser import read_sheet_rows  # noqa: PLC0415

        return parse_geochronology_rows(
            read_sheet_rows(path, sheet_name), source_label=label,
        )
    return parse_csv_geochronology(path, source_label=label)


async def _land_geochronology(
    conn: asyncpg.Connection,
    *,
    input: IngestTabularInput,
    path: str,
    filename: str,
    work: list[tuple[str, str | None, list[dict[str, Any]] | None]],
    sha256: str | None,
    project_epsg: int | None,
    written: dict[str, dict[str, int]],
    sheets: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> tuple[list[str], int]:
    """Write every geochronology table of this upload.

    ``work`` is ``(label, sheet_name, rows)``: a CSV has neither sheet nor
    rows, a worksheet has its name, a dBASE/Access table its rows.

    Returns ``(labels that wrote nothing, rows landed from table sources)``:
    the first join the text/table fallback so the table is not lost, the
    second is the typed copy of rows that ALSO landed as attribute rows.
    """
    from app.services.ingest.geochronology_writer import (  # noqa: PLC0415
        write_geochronology,
    )

    nothing: list[str] = []
    shadowing = 0
    for label, sheet_name, rows in work:
        result = await asyncio.to_thread(
            _parse_geochronology, path, label=label, sheet_name=sheet_name, rows=rows,
        )
        warnings.extend(getattr(result, "warnings", None) or [])
        source_file = filename if sheet_name is None else f"{filename}:{sheet_name}"
        try:
            stats = await write_geochronology(
                conn,
                workspace_id=input.workspace_id,
                project_id=input.project_id,
                result=result,
                label=label,
                source_file=source_file,
                source_file_sha256=sha256,
                source_object_key=input.minio_key,
                declared_epsg=input.source_epsg,
                project_epsg=project_epsg,
                default_epsg=DEFAULT_SOURCE_EPSG,
            )
        except _INFRASTRUCTURE_ERRORS:
            raise
        except Exception as exc:
            # One table's failure costs that table only (ING-1); its own
            # transaction rolled back.
            log.warning(
                "ingest_tabular: geochronology write failed for %s: %s",
                label, exc, exc_info=True,
            )
            warnings.append(_sheet_write_failed_warning(
                label=label, sheet_type=GEOCHRONOLOGY_TYPE, exc=exc,
                table_source=rows is not None,
            ))
            if rows is None:
                nothing.append(sheet_name or label)
            continue
        warnings.extend(stats.warnings)
        prior = written.setdefault(
            GEOCHRONOLOGY_TYPE,
            {"written": 0, "skipped": 0, "orphaned": 0, "replaced": 0},
        )
        for key, value in stats.as_counts().items():
            prior[key] = prior.get(key, 0) + value
        sheets.append({"sheet": label, "type": GEOCHRONOLOGY_TYPE, "rows": stats.written})
        rejected_note = _rows_rejected_warning(
            label=label, write_type=GEOCHRONOLOGY_TYPE, result=result,
            written=stats.written,
        )
        if rejected_note is not None:
            warnings.append(rejected_note)
        if stats.written:
            if rows is not None:
                shadowing += stats.written
            continue
        reason = _refusal_reason(result)
        warnings.append({
            "code": "geochron_wrote_nothing",
            "message": f"{label} looks like a radiometric-age table but no age was written",
            "detail": (
                f"{label}'s headers read as geochronology (sample, age and "
                f"isotopic system or method), but none of its rows could be "
                f"written"
                + (f": {reason}" if reason else "")
                + ". The row reasons are listed beside this note. "
                + ("The table is kept as attribute rows."
                   if rows is not None else
                   "It was kept as searchable text and table rows instead.")
            ),
        })
        if rows is None:
            nothing.append(sheet_name or label)
    return nothing, shadowing


ingest_tabular = hatchet.workflow(
    name="ingest_tabular",
    input_validator=IngestTabularInput,
)


@ingest_tabular.task(execution_timeout="2h", schedule_timeout="2h", retries=1)
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
    #: Collars placed under the ASSUMED default, across every table of this
    #: run — the source of the one run-level collar_crs_assumed warning
    #: (GIS-2). Function scope so the warning block after the connection
    #: closes can always read it.
    crs_state: dict[str, int] = {"assumed_collars": 0}

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
    #: Sheets that DID classify and then wrote nothing — (label, type,
    #: reason, forced, headers_matched, retry_reason). Collected per sheet,
    #: not per type: one workbook can hold a collar tab that lands and a
    #: second that is refused, and the per-type accumulator cannot tell
    #: them apart. The last two carry the post-refusal header re-check for
    #: category-forced sheets, so the warning can advise from what the
    #: headers actually say rather than speculate.
    wrote_nothing: list[
        tuple[
            str, str, str | None, bool, str | None, str | None,
            dict[str, Any] | None,
        ]
    ] = []
    #: Searchable passages the text fallback landed. Part of what the run
    #: wrote — see the rows_written comment at the terminal write.
    text_passages = 0
    #: Structured rows the attribute-table fallback landed. Counted
    #: separately from `written` because that dict is keyed by drill sheet
    #: type and these rows are, by definition, none of those types.
    table_rows = 0

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
            #: Radiometric-age tables (ING-19): ``(label, sheet_name, rows)``
            #: — see _land_geochronology. Never in `work`: not a drill type.
            geochron_work: list[
                tuple[str, str | None, list[dict[str, Any]] | None]
            ] = []
            #: Standalone-.dbf branch state. Empty for every other format.
            attribute_rows: list[dict[str, Any]] = []
            attribute_layer = ""
            attribute_sha256 = ""
            #: Access branch state: one (table_name, rows) per Access table.
            access_layers: list[tuple[str, list[dict[str, Any]]]] = []
            #: dBASE/DAT/Access tables whose headers classified as a drill
            #: type: ``{work label: (display name, rows)}``. Their rows are
            #: parsed from memory by the same parsers the CSV/Excel paths
            #: use; the attribute_tables copy is written for them as well.
            table_sources: dict[str, tuple[str, list[dict[str, Any]]]] = {}
            #: Rows that landed BOTH typed and as attribute rows. They are
            #: the same rows, so the run's headline count subtracts them
            #: once instead of reporting 2N for an N-row collar table.
            typed_rows_shadowing_attribute = 0

            if suffix in ACCESS_EXTENSIONS:
                # An Access database is MANY tables in one file — measured: 19
                # in a Geosoft IP survey. Each becomes its own
                # attribute_tables layer, keyed by the Access table name, so a
                # user sees 19 named tables rather than one opaque blob.
                #
                # Only READ here; the write happens in the connection block
                # below alongside every other branch's, so one failure rolls
                # back with the rest rather than leaving a half-written file.
                #
                # A table that fails to read is skipped with a warning rather
                # than failing the file: a legacy .mdb routinely carries a
                # system or corrupt table beside 18 good ones, and losing all
                # of them to one bad one is the wrong trade.
                from georag_geoparsers.access_mdb import list_tables, read_table  # noqa: PLC0415

                attribute_sha256 = await asyncio.to_thread(_sha256_file, local)
                access_names = await asyncio.to_thread(list_tables, local)
                for table_name in access_names:
                    try:
                        rows = await asyncio.to_thread(read_table, local, table_name)
                    except Exception as exc:
                        log.warning(
                            "ingest_tabular: Access table %r in %s failed to read: %s",
                            table_name, filename, exc, exc_info=True,
                        )
                        warnings.append({
                            "code": "access_table_unreadable",
                            "message": f"table {table_name!r} could not be read",
                            "detail": (
                                f"{filename} holds {len(access_names)} tables and "
                                f"{table_name!r} could not be read: {exc}. The other "
                                f"tables were unaffected."
                            ),
                        })
                        continue
                    if rows:
                        access_layers.append((table_name, rows))
                        sheets.append({
                            "sheet": f"{filename}:{table_name}",
                            "type": "attribute_table",
                            "rows": len(rows),
                        })
                        # The same header classification the CSV/Excel path
                        # runs. A table that matches no drill type (the
                        # common case in a geophysics .mdb) is unaffected.
                        verdict, _conf = _typed_verdict_for_table(
                            rows, input.column_map,
                        )
                        if _routes_to_geochronology(_table_columns(rows), verdict):
                            # Additive, like the typed drill route: the
                            # attribute_tables copy below is still written.
                            geochron_work.append(
                                (f"{filename}:{table_name}", None, rows),
                            )
                        elif verdict in WRITE_ORDER:
                            work.append((verdict, table_name))
                            table_sources[table_name] = (
                                f"{filename}:{table_name}", rows,
                            )
                if not access_layers:
                    warnings.append({
                        "code": "access_no_tables",
                        "message": "the Access database held no readable tables",
                        "detail": (
                            f"{filename} opened but every table was empty or "
                            f"unreadable, so nothing was landed. The file is in "
                            f"bronze and can be re-ingested."
                        ),
                    })

            elif suffix in DBASE_EXTENSIONS:
                # None of the sheet machinery below applies: a dBASE table
                # has one layer, no geometry and no drill schema. The
                # sibling check runs before the read for the reason given
                # in _assert_standalone_dbf.
                _assert_standalone_dbf(local)
                attribute_layer = Path(local).stem
                attribute_sha256 = await asyncio.to_thread(_sha256_file, local)
                reader = (
                    _read_mapinfo_dat_table
                    if suffix in MAPINFO_DAT_EXTENSIONS
                    else _read_dbf_table
                )
                attribute_rows = await asyncio.to_thread(reader, local)
                sheets.append({
                    "sheet": filename,
                    "type": "attribute_table",
                    "rows": len(attribute_rows),
                })
                if attribute_rows:
                    # Typed routing by the same header classification the
                    # CSV/Excel path runs; a trace export or a surface
                    # geochemistry table keeps its dedicated writer below.
                    verdict, _conf = _typed_verdict_for_table(
                        attribute_rows, input.column_map,
                        dbase_side_writes=True,
                    )
                    if _routes_to_geochronology(
                        _table_columns(attribute_rows), verdict,
                    ):
                        geochron_work.append((filename, None, attribute_rows))
                    elif verdict in WRITE_ORDER:
                        work.append((verdict, attribute_layer))
                        table_sources[attribute_layer] = (
                            filename, attribute_rows,
                        )
                if not attribute_rows:
                    warnings.append({
                        "code": "dbf_no_rows",
                        "message": "the dBASE table declared no rows",
                        "detail": (
                            f"{filename} opened cleanly but holds no rows, so "
                            f"nothing was landed. The file is stored in bronze "
                            f"and can be re-ingested if this is unexpected."
                        ),
                    })
            elif suffix in EXCEL_EXTENSIONS:
                from georag_geoparsers.xlsx_parser import enumerate_sheets  # noqa: PLC0415

                for meta in enumerate_sheets(local, column_map=input.column_map):
                    # SheetMeta.name, not .sheet_name — the dataclass names it
                    # `name` while carrying `sheet_type` beside it, which is an
                    # easy pair to mistype.
                    is_geochron = bool(meta.row_count) and _routes_to_geochronology(
                        list(getattr(meta, "headers", None) or []), meta.sheet_type,
                    )
                    sheets.append({
                        "sheet": meta.name,
                        "type": GEOCHRONOLOGY_TYPE if is_geochron else meta.sheet_type,
                        "confidence": meta.classify_confidence,
                        "rows": meta.row_count,
                        "hidden": meta.hidden,
                    })
                    if is_geochron:
                        geochron_work.append((meta.name, meta.name, None))
                    elif meta.sheet_type in WRITE_ORDER and meta.row_count:
                        work.append((meta.sheet_type, meta.name))
                    elif meta.sheet_type not in WRITE_ORDER:
                        unclassified.append(meta.name)
            else:
                preamble_note = await asyncio.to_thread(
                    _csv_preamble_warning, local, filename,
                )
                if preamble_note is not None:
                    warnings.append(preamble_note)
                sheet_type = input.sheet_type
                headers = _csv_headers(local)
                if sheet_type not in WRITE_ORDER:
                    from georag_geoparsers._sheet_classifier import (  # noqa: PLC0415
                        classify_sheet_type,
                    )

                    sheet_type, confidence = classify_sheet_type(
                        headers, column_map=input.column_map,
                    )
                    sheets.append({
                        "sheet": filename, "type": sheet_type,
                        "confidence": confidence,
                    })
                if _routes_to_geochronology(
                    headers, sheet_type,
                    hinted=input.sheet_type == GEOCHRONOLOGY_TYPE,
                ):
                    geochron_work.append((filename, None, None))
                elif sheet_type in WRITE_ORDER:
                    work.append((sheet_type, None))
                else:
                    unclassified.append(filename)

            # A .dbf/.dat classifies to exactly one thing and never enters
            # `work`, so the drill-sheet advice below would be both wrong
            # and unactionable for it.
            if not work and not geochron_work and suffix not in DBASE_EXTENSIONS:
                warnings.append({
                    "code": "nothing_classified",
                    "detail": (
                        "No sheet matched the collar / survey / lithology / "
                        "sample layouts, so nothing landed in the drillhole "
                        "tables — the notes beside this one report how the "
                        "data was kept instead. If one of these sheets IS "
                        "drill data, its headers were not recognised: rename "
                        "the key columns to standard names (hole_id, plus "
                        "easting/northing for collars or from/to depths for "
                        "intervals) and re-upload. The New Project screen's "
                        "file list can also upload a single-table file under "
                        "its matching category to force the type; the import "
                        "wizard picks the category from the extension."
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

                # The PROJECT's coordinate system, when the upload did not
                # declare one.
                #
                # Nothing in a CSV or a spreadsheet says what projection its
                # easting/northing are in, so the only two sources are the
                # per-file override the wizard collects and the project the
                # file was uploaded into. Until now only the first existed,
                # and its absence fell straight through to
                # DEFAULT_SOURCE_EPSG — 32613, WGS 84 / UTM zone 13N, which
                # runs through Colorado. A project created as EPSG:26904
                # (Alaska, zone 4N) still had every collar CSV read as zone
                # 13 and written ~2,500 km east of the hole, while the
                # correct answer sat on silver.projects.crs_epsg, unread.
                #
                # Precedence, most-specific first:
                #   1. input.source_epsg  — this file, typed by the user
                #   2. projects.crs_epsg  — this project, set at creation
                #   3. DEFAULT_SOURCE_EPSG — a guess, and warned about as one
                #
                # Only (3) still counts as `assumed`: a project CRS is a
                # declaration, so georef_method reads 'declared' and the
                # scary warning stays off a file that is in fact placed
                # correctly. Wrapped because a missing column or an
                # unreadable row must not fail an ingest that would
                # otherwise succeed — it falls back to the old behaviour,
                # warning included.
                #
                # Since GIS-1 (2026-09-29) that precedence is applied PER
                # TABLE by _resolve_table_crs, which also recognises a
                # longitude/latitude table and places it as EPSG:4326
                # whatever the project says; `epsg` below is only the
                # fallback for the run-level output fields.
                project_epsg: int | None = None
                if input.source_epsg is None:
                    try:
                        project_epsg = await conn.fetchval(
                            "SELECT crs_epsg FROM silver.projects "
                            "WHERE project_id = $1::uuid",
                            input.project_id,
                        )
                    except Exception as crs_exc:  # noqa: BLE001
                        project_epsg = None
                        log.warning(
                            "ingest_tabular: could not read project CRS for %s — %s",
                            input.project_id, crs_exc,
                        )
                    if project_epsg is not None:
                        project_epsg = int(project_epsg)
                        epsg = project_epsg
                        epsg_assumed = False

                async def _placed_collars(
                    label: str, records: list[dict[str, Any]],
                    easting_column: str | None, northing_column: str | None,
                    *, trace_export: bool = False,
                    issues: RowIssues | None = None,
                ) -> dict[str, int]:
                    """Decide this table's CRS, write its collars, record confidence."""
                    decision, flagged = await _resolve_table_crs(
                        conn,
                        project_id=input.project_id,
                        label=label,
                        records=records,
                        easting_column=easting_column,
                        northing_column=northing_column,
                        declared_epsg=input.source_epsg,
                        project_epsg=project_epsg,
                    )
                    warnings.extend(decision.warnings)
                    if decision.refusal is not None:
                        warnings.append(decision.refusal)
                        return {"written": 0, "skipped": len(records), "orphaned": 0}
                    method = decision.georef_method
                    if trace_export and method == "declared":
                        # A trace export names no CRS itself; see the
                        # Discover branch below for why this is 'manual'.
                        method = "manual"
                    collar_stats = await _write_collars(
                        conn,
                        workspace_id=input.workspace_id,
                        project_id=input.project_id,
                        records=records, epsg=decision.epsg,
                        georef_method=method,
                        issues=issues,
                    )
                    await _stamp_crs_confidence(
                        conn,
                        project_id=input.project_id,
                        hole_ids=[str(r["hole_id"]) for r in records if r.get("hole_id")],
                        confidence=decision.crs_confidence,
                        flagged=flagged,
                    )
                    if decision.assumed:
                        crs_state["assumed_collars"] += collar_stats.get("written", 0)
                    return collar_stats

                #: Rows the companion tables of the sheet just written landed.
                #: A lithology sheet the lithology writer refused can still have
                #: fed alteration or mineralization, and that is not "wrote
                #: nothing" - so the refusal path reads this.
                companion_landed: dict[str, int] = {"rows": 0}

                #: (table, collar_id) pairs this run has already replaced
                #: (ING-2): the first sheet of a type clears what an EARLIER
                #: upload wrote for its holes, later sheets of the same run
                #: append instead of deleting the first one's rows.
                replaced_scope: set[tuple[str, str]] = set()

                async def _write_companions(
                    primary_result: Any, write_type: str,
                    table: tuple[str, list[dict[str, Any]]] | None,
                    target_sheet: str | None, index: dict[str, str],
                ) -> None:
                    """Feed the tables *write_type*'s columns also describe.

                    The same file is parsed again as each companion type
                    (``companion=True``: rows with no alteration / mineral are
                    not-applicable, not rejected, and generic Comments columns
                    stay with the lithology), and the records go through the
                    same collar resolution and replace-per-collar write. Then
                    the columns NOBODY read are reported.
                    """
                    label = (
                        (table[0] if table is not None else None)
                        or target_sheet or filename
                    )
                    claimed_elsewhere: set[str] = set()
                    for companion_type in _companion_types_present(
                        primary_result, write_type, input.column_map,
                    ):
                        if table is not None:
                            comp = await asyncio.to_thread(
                                _parse_rows, table[1], companion_type,
                                input.column_map, True,
                            )
                        else:
                            comp = await asyncio.to_thread(
                                _parse_one, local, companion_type,
                                target_sheet, input.column_map, True,
                            )
                        warnings.extend(getattr(comp, "warnings", None) or [])
                        claimed_elsewhere.update(
                            (getattr(comp, "column_map", None) or {}).values()
                        )
                        comp_records = getattr(comp, "records", None) or []
                        if not comp_records:
                            continue
                        comp_issues = RowIssues()
                        comp_stats = await _write_intervals(
                            conn,
                            workspace_id=input.workspace_id,
                            sheet_type=companion_type,
                            records=comp_records, index=index,
                            issues=comp_issues,
                            replaced_scope=replaced_scope,
                        )
                        warnings.extend(issue_warnings(
                            comp_issues, label=label, table=companion_type,
                        ))
                        # The same source rows already reported their unknown
                        # holes under the primary type; counting them again
                        # would double the orphan total.
                        comp_stats["orphaned"] = 0
                        prior_c = written.setdefault(
                            companion_type,
                            {"written": 0, "skipped": 0, "orphaned": 0, "replaced": 0},
                        )
                        for k, v in comp_stats.items():
                            prior_c[k] = prior_c.get(k, 0) + v
                        companion_landed["rows"] += comp_stats.get("written", 0)
                        sheets.append({
                            "sheet": label,
                            "type": companion_type,
                            "rows": comp_stats.get("written", 0),
                            "companion_of": write_type,
                        })
                    # Only for a table that landed rows. A refused one already has
                    # its own warning, and listing every column of it as
                    # "not read" would say the same thing a second, noisier way.
                    unread = [
                        c for c in (getattr(primary_result, "unmapped_columns", None) or [])
                        if c not in claimed_elsewhere
                    ] if (getattr(primary_result, "records", None) or companion_landed["rows"]) else []
                    note = _columns_not_ingested_warning(
                        label=label, write_type=write_type, columns=unread,
                    )
                    if note is not None:
                        warnings.append(note)

                async def _parse_and_write(
                    write_type: str, target_sheet: str | None,
                ) -> tuple[Any, dict[str, int]]:
                    """Parse `local` as `write_type` and persist the records.

                    Inner on purpose: it closes over the connection, the CRS
                    decision and the per-type accumulator, all of which live
                    only inside this block. The category-retry below is the
                    second caller — without this it would duplicate the
                    collar/interval split and the accumulator arithmetic,
                    and the two copies would drift.
                    """
                    companion_landed["rows"] = 0
                    table = (
                        table_sources.get(target_sheet)
                        if target_sheet is not None else None
                    )
                    if table is not None:
                        # dBASE/DAT/Access: parse the rows already in memory.
                        result = await asyncio.to_thread(
                            _parse_rows, table[1], write_type,
                            input.column_map,
                        )
                    else:
                        result = await asyncio.to_thread(
                            _parse_one, local, write_type, target_sheet,
                            input.column_map,
                        )
                    records = getattr(result, "records", None) or []
                    warnings.extend(getattr(result, "warnings", None) or [])
                    label = (
                        (table[0] if table is not None else None)
                        or target_sheet or filename
                    )

                    row_issues = RowIssues()
                    if write_type == "collar":
                        result_map = getattr(result, "column_map", None) or {}
                        stats = await _placed_collars(
                            (table[0] if table is not None else None)
                            or target_sheet or filename,
                            records,
                            result_map.get("easting"),
                            result_map.get("northing"),
                            issues=row_issues,
                        )
                    else:
                        # Rebuilt per type so collars written moments ago in
                        # THIS run are resolvable by the sheets that follow.
                        index = await _collar_index(conn, input.project_id)
                        stats = await _write_intervals(
                            conn,
                            workspace_id=input.workspace_id,
                            sheet_type=write_type,
                            records=records, index=index,
                            issues=row_issues,
                            replaced_scope=replaced_scope,
                        )
                        if write_type in _COMPANION_TYPES:
                            await _write_companions(
                                result, write_type, table, target_sheet, index,
                            )

                    # Rows the WRITER refused (a value the table's constraints
                    # cannot hold) are rejected rows too: fold them into the
                    # parse result so the rows_rejected summary below counts
                    # them, and say what was skipped / blanked in their own
                    # words (ING-1).
                    _fold_writer_skips(result, row_issues)
                    warnings.extend(issue_warnings(
                        row_issues, label=label, table=write_type,
                    ))

                    rejected_note = _rows_rejected_warning(
                        label=label,
                        write_type=write_type,
                        result=result,
                        written=stats.get("written", 0),
                    )
                    if rejected_note is not None:
                        warnings.append(rejected_note)

                    prior = written.setdefault(
                        write_type,
                        {"written": 0, "skipped": 0, "orphaned": 0, "replaced": 0},
                    )
                    for k, v in stats.items():
                        prior[k] = prior.get(k, 0) + v
                    return result, stats

                async def _parse_and_write_atomically(
                    write_type: str, target_sheet: str | None,
                ) -> tuple[Any, dict[str, int]]:
                    """``_parse_and_write`` as ONE transaction per sheet (ING-1).

                    The writers each commit on their own, so a sheet whose
                    lithology landed and whose companion alteration write then
                    failed used to leave half of itself behind; and batches of
                    500 committed one by one before that. Here the whole sheet
                    - primary table, companions, assays - commits together or
                    not at all (the writers' own transactions become
                    savepoints), and the run's accumulators are put back the
                    way they were so the headline does not count rows that
                    rolled back.
                    """
                    written_before = copy.deepcopy(written)
                    sheets_before = len(sheets)
                    scope_before = set(replaced_scope)
                    try:
                        async with conn.transaction():
                            return await _parse_and_write(write_type, target_sheet)
                    except BaseException:
                        written.clear()
                        written.update(written_before)
                        del sheets[sheets_before:]
                        replaced_scope.clear()
                        replaced_scope.update(scope_before)
                        raise

                for sheet_type, sheet_name in work:
                    # Where this attempt's parser warnings begin and end in
                    # the run's list. Both retry outcomes need the span:
                    # correction drops it (those notes describe a reading of
                    # the file the correction says was wrong), and a failed
                    # retry dedupes against it (both attempts re-detect the
                    # same encoding/delimiter facts and would report each
                    # twice).
                    forced_warn_start = len(warnings)
                    is_table_source = suffix in TABLE_SOURCE_EXTENSIONS
                    try:
                        result, stats = await _parse_and_write_atomically(
                            sheet_type, sheet_name,
                        )
                    except _INFRASTRUCTURE_ERRORS:
                        # The database or the worker, not this sheet: fail
                        # the run so Hatchet retries it whole.
                        raise
                    except Exception as exc:
                        # One sheet's failure costs that sheet only (ING-1).
                        # It rolled back whole, the sheets before it are
                        # committed, and the ones after it still run - a
                        # workbook used to lose every survey / lithology /
                        # assay sheet to one bad collar. A dBASE/Access table
                        # keeps its attribute copy (written below); a CSV or
                        # worksheet joins the text fallback so it stays
                        # searchable.
                        display = (
                            table_sources[sheet_name or ""][0]
                            if is_table_source else (sheet_name or filename)
                        )
                        log.warning(
                            "ingest_tabular: typed %s write failed for %s: %s",
                            sheet_type, display, exc, exc_info=True,
                        )
                        # Its notes described rows that are not in silver.
                        del warnings[forced_warn_start:]
                        warnings.append(_sheet_write_failed_warning(
                            label=display, sheet_type=sheet_type, exc=exc,
                            table_source=is_table_source,
                        ))
                        if not is_table_source and display not in unclassified:
                            unclassified.append(display)
                        continue
                    forced_warn_end = len(warnings)
                    if is_table_source and stats.get("written"):
                        display = table_sources[sheet_name or ""][0]
                        sheets.append({
                            "sheet": display,
                            "type": sheet_type,
                            "rows": stats["written"],
                        })
                        typed_rows_shadowing_attribute += stats["written"]
                    if (
                        stats.get("written") or stats.get("orphaned")
                        or companion_landed["rows"]
                    ):
                        # ORPHANED COUNTS AS LANDED DELIBERATELY. An
                        # interval sheet whose rows all orphaned parsed
                        # perfectly well -- its collars simply are not
                        # uploaded yet, and its own orphaned_intervals
                        # warning already says to upload them and re-run.
                        # Text-indexing it now would leave that copy behind
                        # when the typed rows land on the second run,
                        # competing with them in the recall set. That is
                        # the exact duplication the only_sheets scoping
                        # exists to prevent, one case over.
                        continue

                    # Classified (or category-forced), then refused.
                    # Recorded here, where the parse result is still in
                    # scope and can say why; acted on after the write pass,
                    # so WRITE_ORDER and the collars-first sort above are
                    # untouched.
                    #
                    # `forced` excludes workbooks: their sheets are always
                    # classified per sheet, so a stray sheet_type on the
                    # input that happens to equal a sheet's classified type
                    # must not read as "the category forced this".
                    forced = (
                        sheet_type == input.sheet_type
                        and suffix not in EXCEL_EXTENSIONS
                        # A dBASE/Access table is classified from its own
                        # headers whatever category the upload carried, and
                        # has no CSV header row for the re-check to read.
                        and not is_table_source
                    )
                    headers_matched: str | None = None
                    retry_reason: str | None = None
                    if forced:
                        # ── The category was wrong; ask the headers ──────
                        # The forced route skipped the classifier on the way
                        # in — that is what "forced" means — so run it now,
                        # after the refusal, when it costs nothing: the
                        # forced writer has already accepted zero rows, so a
                        # different verdict can only add data, never replace
                        # any. A survey file left on the wizard's `.csv`
                        # default (which is `collars`) lands as survey rows
                        # instead of as prose plus a generic table.
                        from georag_geoparsers._sheet_classifier import (  # noqa: PLC0415
                            classify_sheet_type,
                        )
                        try:
                            reclass, _reconf = classify_sheet_type(
                                _csv_headers(local), column_map=input.column_map,
                            )
                        except Exception as exc:  # noqa: BLE001 — the retry is best-effort; the text/table fallback must still land
                            log.warning(
                                "ingest_tabular: post-refusal header re-check "
                                "failed for %s: %s", filename, exc,
                            )
                            reclass = "unknown"
                        headers_matched = (
                            reclass if reclass in WRITE_ORDER else None
                        )
                        if (
                            headers_matched is not None
                            and headers_matched != sheet_type
                        ):
                            try:
                                retry_result, retry_stats = (
                                    await _parse_and_write_atomically(
                                        headers_matched, None,
                                    )
                                )
                            except _INFRASTRUCTURE_ERRORS:
                                raise
                            except Exception as retry_exc:
                                # The re-read as another type failed to write;
                                # the original refusal stands and the file
                                # still reaches the text/table fallback.
                                log.warning(
                                    "ingest_tabular: re-read of %s as %s failed: %s",
                                    filename, headers_matched, retry_exc,
                                    exc_info=True,
                                )
                                retry_result = None
                                retry_stats = {}
                            if retry_stats.get("written") or retry_stats.get(
                                "orphaned",
                            ):
                                # Drop the forced attempt's parser notes:
                                # they describe the file read as a type this
                                # correction says it never was, and the
                                # retry's own parse re-detected and re-said
                                # the file-level facts (encoding, delimiter)
                                # in the entries after the span.
                                del warnings[forced_warn_start:forced_warn_end]
                                warnings.append(_category_corrected_warning(
                                    label=sheet_name or filename,
                                    forced_as=sheet_type,
                                    matched=headers_matched,
                                    written=retry_stats.get("written", 0),
                                    orphaned=retry_stats.get("orphaned", 0),
                                ))
                                continue
                            # Both attempts parsed the same bytes, so the
                            # file-level notes arrive twice; keep the
                            # retry's only where it says something new.
                            already = warnings[:forced_warn_end]
                            warnings[forced_warn_end:] = [
                                w for w in warnings[forced_warn_end:]
                                if w not in already
                            ]
                            retry_reason = _refusal_reason(retry_result)

                    wrote_nothing.append((
                        sheet_name or filename,
                        sheet_type,
                        _refusal_reason(result),
                        forced,
                        headers_matched,
                        retry_reason,
                        # The columns this sheet actually has, so the page
                        # can offer a mapping instead of only explaining
                        # the refusal.
                        _remap_facts(result, sheet_type),
                    ))

                # ── Radiometric ages (ING-19) ────────────────────────────
                # After the drill tables, before the attribute copy: a table
                # that could not be written as ages still reaches the
                # text/table fallback below, never nothing.
                if geochron_work:
                    geo_sha = attribute_sha256 or await asyncio.to_thread(
                        _sha256_file, local,
                    )
                    geo_nothing, geo_shadowing = await _land_geochronology(
                        conn,
                        input=input,
                        path=local,
                        filename=filename,
                        work=geochron_work,
                        sha256=geo_sha,
                        project_epsg=project_epsg,
                        written=written,
                        sheets=sheets,
                        warnings=warnings,
                    )
                    typed_rows_shadowing_attribute += geo_shadowing
                    for geo_label in geo_nothing:
                        if (
                            suffix not in TABLE_SOURCE_EXTENSIONS
                            and geo_label not in unclassified
                        ):
                            unclassified.append(geo_label)

                if attribute_rows:
                    written["attribute_table"] = await _write_attribute_rows(
                        conn,
                        workspace_id=input.workspace_id,
                        project_id=input.project_id,
                        source_file=filename,
                        source_file_sha256=attribute_sha256,
                        source_layer=attribute_layer,
                        rows=attribute_rows,
                    )

                # One Access table -> one attribute_tables layer. source_layer
                # is the ACCESS TABLE NAME, not the file stem: that is what
                # makes 19 tables distinguishable on the Tables page instead
                # of 19 identically-named blobs.
                for access_name, access_rows in access_layers:
                    stats = await _write_attribute_rows(
                        conn,
                        workspace_id=input.workspace_id,
                        project_id=input.project_id,
                        source_file=filename,
                        source_file_sha256=attribute_sha256,
                        source_layer=access_name,
                        rows=access_rows,
                    )
                    prior = written.get("attribute_table", {})
                    written["attribute_table"] = {
                        key: prior.get(key, 0) + value for key, value in stats.items()
                    }

                # A dBASE table that is a SURFACE GEOCHEMISTRY survey also
                # lands as typed samples. Additive: the attribute copy
                # above stays the verbatim record of the file, and this
                # gives the assays a home the map and the agent can read.
                #
                # Best-effort on purpose. The attribute rows are already
                # committed by the time this runs, and losing the typed
                # copy is a smaller failure than turning a successful
                # ingest into a failed one — the file is in bronze and can
                # be re-ingested. The warning says what happened.
                dbase_columns = list(attribute_rows[0]) if attribute_rows else []

                # A Discover/MapInfo drillhole TRACE export also lands as
                # collars. Same additive, best-effort contract as the
                # geochem branch below: the attribute copy is already
                # committed and must not be lost to a typed-write failure.
                trace_shape = _discover_trace_columns(dbase_columns)
                if trace_shape is not None:
                    trace_issues = RowIssues()
                    try:
                        collar_rows = _collapse_discover_traces(
                            attribute_rows, trace_shape,
                        )
                        # Computed up front so the collar dips and the
                        # station dips are normalised by ONE file-level
                        # convention decision (§04e up-holes, 2026-09-29).
                        stations = _trace_survey_stations(
                            attribute_rows, trace_shape,
                        )
                        warnings.extend(_normalize_trace_dips(
                            attribute_rows, trace_shape, collar_rows, stations,
                            label=filename,
                        ))
                        if collar_rows:
                            # The coordinates came from the export, not
                            # from a human typing an EPSG — 'declared'
                            # would overstate it, since the file itself
                            # names no CRS, so a declared/project CRS is
                            # recorded as 'manual' (trace_export=True).
                            written["collar"] = await _placed_collars(
                                filename, collar_rows,
                                trace_shape.get("mid_x"), trace_shape.get("mid_y"),
                                trace_export=True,
                                issues=trace_issues,
                            )
                            sheets.append({
                                "sheet": filename,
                                "type": "collar",
                                "rows": written["collar"]["written"],
                            })

                            # The same rows are also a downhole survey.
                            # Written AFTER the collars because a station
                            # resolves hole_id -> collar_id against them,
                            # and the index has to be rebuilt here rather
                            # than reused: these collars did not exist
                            # when any earlier index was taken.
                            if stations:
                                survey_index = await _collar_index(
                                    conn, input.project_id,
                                )
                                written["survey"] = await _write_intervals(
                                    conn,
                                    workspace_id=input.workspace_id,
                                    sheet_type="survey",
                                    records=stations,
                                    index=survey_index,
                                    issues=trace_issues,
                                )
                                sheets.append({
                                    "sheet": filename,
                                    "type": "survey",
                                    "rows": written["survey"]["written"],
                                })
                            warnings.extend(issue_warnings(
                                trace_issues, label=filename, table="collar",
                            ))
                            # An assumed CRS is reported once for the run,
                            # from crs_state, below — this site used to add
                            # a second copy of the same warning.
                        skipped_holes = len({
                            str(r.get(trace_shape["hole_id"], "") or "").strip()
                            for r in attribute_rows
                            if str(r.get(trace_shape["hole_id"], "") or "").strip()
                        }) - len(collar_rows)
                        if skipped_holes > 0:
                            warnings.append({
                                "code": "trace_collar_unlocatable",
                                "message": (
                                    f"{skipped_holes} hole(s) in the trace export "
                                    f"have no depth-0 segment"
                                ),
                                "detail": (
                                    f"{filename} is a drillhole trace export, which "
                                    f"records one row per segment. The collar is the "
                                    f"segment at depth 0, and {skipped_holes} hole(s) "
                                    f"do not have one — their shallowest row is a "
                                    f"MIDPOINT, tens of metres from the collar. Those "
                                    f"holes were left out rather than placed wrongly. "
                                    f"Re-export including the collar segment."
                                ),
                            })
                    except Exception as exc:
                        log.warning(
                            "ingest_tabular: trace collar write failed for %s: %s",
                            filename, exc, exc_info=True,
                        )
                        warnings.append({
                            "code": "trace_collar_write_failed",
                            "message": "the trace landed as a table but not as collars",
                            "detail": (
                                f"{filename} looks like a drillhole trace export and "
                                f"its rows were stored, but writing them as collars "
                                f"failed: {exc}. The data is not lost — it is in the "
                                f"attribute table and in bronze."
                            ),
                        })

                geochem_shape = _surface_geochem_columns(dbase_columns)
                if geochem_shape is not None:
                    try:
                        # Same per-table CRS rule as collars (GIS-1): a soil
                        # survey in lon/lat is placed as EPSG:4326, not read
                        # as UTM metres at the equator.
                        located = geochem_shape["located"]
                        geo_decision, _ = await _resolve_table_crs(
                            conn,
                            project_id=input.project_id,
                            label=filename,
                            records=attribute_rows,
                            easting_column=located["easting"],
                            northing_column=located["northing"],
                            declared_epsg=input.source_epsg,
                            project_epsg=project_epsg,
                            id_key=located["sample_id"],
                            x_key=located["easting"],
                            y_key=located["northing"],
                        )
                        warnings.extend(geo_decision.warnings)
                        if geo_decision.refusal is not None:
                            warnings.append(geo_decision.refusal)
                            written["geochemistry"] = {
                                "written": 0, "skipped": len(attribute_rows),
                                "orphaned": 0,
                            }
                        else:
                            written["geochemistry"] = await _write_surface_geochem(
                                conn,
                                workspace_id=input.workspace_id,
                                project_id=input.project_id,
                                shape=geochem_shape,
                                rows=attribute_rows,
                                source_epsg=geo_decision.epsg,
                            )
                        sheets.append({
                            "sheet": filename,
                            "type": "geochemistry",
                            "rows": written["geochemistry"]["written"],
                        })
                        if geo_decision.assumed:
                            warnings.append(_assumed_crs_warning(
                                geo_decision.epsg, written["geochemistry"]["written"],
                            ))
                    except Exception as exc:
                        log.warning(
                            "ingest_tabular: surface geochem write failed for %s: %s",
                            filename, exc, exc_info=True,
                        )
                        warnings.append({
                            "code": "geochem_write_failed",
                            "message": "the samples landed as a table but not as geochemistry",
                            "detail": (
                                f"{filename} looks like a surface geochemistry "
                                f"survey and its rows were stored, but writing "
                                f"them as typed samples failed: {exc}. The data "
                                f"is not lost — it is in the attribute table and "
                                f"in bronze, and re-ingesting will retry."
                            ),
                        })

                # ── Classified, and then wrote nothing ──────────────────
                # The fallback below used to run only for sheets that
                # matched NO drill type, so a sheet that classified and
                # was then refused by its writer got neither typed rows
                # nor searchable text — and the run said so with an
                # unrelated warning, if any. Measured on the customer's
                # export_UTM.xls: 24 rows of IP station coordinates
                # (Grids_Name, LineNumber, X, Y, Z) classified as
                # 'collar' at 0.75 confidence because X/Y/Z matched
                # easting/northing/elevation, the collar writer refused
                # every row for having no hole_id, and the only thing the
                # UI showed was 'xls_legacy_format_detected'.
                #
                # Joining the fallback set is the floor: the sheet is at
                # least answerable in chat. The warning is the rest of
                # it — the refusal has a reason and the geologist should
                # not have to read a worker log to find it.
                for (
                    label, classified_as, reason, forced, matched, second,
                    remap,
                ) in wrote_nothing:
                    # The text/table fallbacks re-read the file as delimited
                    # text or a worksheet; a dBASE/Access table is neither,
                    # and is already whole in silver.attribute_tables.
                    if (
                        suffix not in TABLE_SOURCE_EXTENSIONS
                        and label not in unclassified
                    ):
                        unclassified.append(label)
                    warnings.append(_wrote_nothing_warning(
                        label=label,
                        classified_as=classified_as,
                        reason=reason,
                        from_category=forced,
                        headers_matched=matched,
                        retry_reason=second,
                        remap=remap,
                        table_source=suffix in TABLE_SOURCE_EXTENSIONS,
                    ))

                # ── Whatever did not classify ───────────────────────────
                # A sheet that matches no drill type is not necessarily
                # junk: a sample dispatch log, a QA/QC summary, a
                # historical production table. The answer used to be one
                # `nothing_classified` warning and nothing else, so the
                # file was not in the system in ANY form — and for a
                # workbook arriving inside a ZIP that is a regression on
                # the old archive branch, which at least landed it as text.
                #
                # The advice in that warning (rename the headers, or force
                # the type via the upload category) is header-side on
                # purpose: a file inside an archive has no user-chosen
                # category — the archive branch deliberately passes no
                # hint — so category-side advice cannot be followed there.
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
                        text_passages += int(text_landed.get("passages") or 0)

                    # Beside the text fallback, not instead of it: the two
                    # answer different questions ("what does this say?" vs
                    # "what are its values?") and land in different places,
                    # so neither competes with the other in the recall set.
                    rows_landed = await _land_unclassified_as_rows(
                        conn,
                        path=local,
                        suffix=suffix,
                        filename=filename,
                        unclassified=unclassified,
                        workspace_id=input.workspace_id,
                        project_id=input.project_id,
                    )
                    if rows_landed:
                        warnings.append(rows_landed)
                        table_rows += int(rows_landed.get("rows") or 0)

                # Collars were just written: LAS files kept in bronze because
                # their hole had no collar attach now (never raises).
                if written.get("collar", {}).get("written"):
                    from app.services.ingest.las_pending import (  # noqa: PLC0415
                        attach_pending_las,
                    )

                    try:  # a hook defect costs a warning, never these collars
                        await attach_pending_las(
                            conn, store=store, workspace_id=input.workspace_id,
                            project_id=input.project_id,
                        )
                    except Exception as attach_exc:  # noqa: BLE001
                        log.warning(
                            "ingest_tabular: pending LAS attach failed for %s: %s",
                            filename, attach_exc,
                        )
                        warnings.append({
                            "code": "las_pending_attach_failed",
                            "message": (
                                "LAS files waiting for these collars were not "
                                "attached this run; they stay kept and attach "
                                f"on the next collar upload ({attach_exc})"
                            )[:500],
                        })
            finally:
                await conn.close()

        # Orphan accounting BEFORE the terminal write. This block used
        # to sit after it, so `warnings` was already serialised into the
        # progress row by the time the orphan entry was appended and the
        # entry reached only the workflow output object. That object is not
        # what the Ingestion Runs page reads — which is precisely the
        # failure mark_completed_by_run's docstring cites as its reason for
        # existing, quoting THIS warning's text as the example.
        # Coordinates written under a GUESSED coordinate system.
        #
        # DEFAULT_SOURCE_EPSG is 32613 -- WGS 84 / UTM zone 13N, which runs
        # through Colorado. Nothing has ever sent ingest_tabular a
        # source_epsg (the wizard's CRS donation works by injecting a .prj
        # into a zipped bundle, and a .csv or .xls cannot carry one), so
        # every collar the platform has ingested without a typed override
        # was placed in zone 13 whatever zone it was surveyed in. For the
        # Alaska Peninsula -- zone 4N -- that is about 2,500 km east, in
        # open country a thousand miles from the hole.
        #
        # `georef_method` already records 'assumed' on the row, but nothing
        # renders it, so the geologist had no way to know. This is the same
        # failure the spatial parser's CRS refusal exists to stop, one
        # workflow over; the difference is that a collar file is refused by
        # this workflow only if it has no coordinates at all, so warning
        # loudly is the honest move rather than dropping the rows.
        #
        # Fires only when collars were actually written: an interval-only
        # upload has no coordinates for the assumption to damage.
        #
        # Counted per table since GIS-1/GIS-2 (2026-09-29): a lon/lat table
        # is placed as EPSG:4326 and is not "assumed" even when the run
        # declares nothing, so the count comes from the decisions made, not
        # from the run-level fallback.
        if crs_state["assumed_collars"]:
            warnings.append(_assumed_crs_warning(
                DEFAULT_SOURCE_EPSG, crs_state["assumed_collars"],
            ))

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
            # sheets wrote between them — PLUS the passages the text
            # fallback landed. Counting only typed silver rows made a
            # successful text-only ingest report zero, which the
            # Ingestion Runs page renders as "Finished — no data
            # written" (IngestionRuns.tsx:79) directly beside this run's
            # own warning saying it indexed N searchable passages. Both
            # cannot be true; the passages are on disk.
            #
            # The status is unchanged by this: terminal_status() returns
            # 'partial' when rows_written == 0 OR warnings exist, and a
            # text-only run always has warnings. What changes is the
            # headline, which stops contradicting the warning under it.
            rows_written = sum(
                stats.get("written", 0) for stats in written.values()
            ) + text_passages + table_rows - typed_rows_shadowing_attribute
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

                # Silver is not what the Workspace draws. SECTION, 3D, LOGS
                # and COMPARE read gold.drillhole_intervals_visual and
                # silver.drill_traces, and between the Dagster retirement
                # (2026-07-28) and 2026-08-25 nothing wrote either — so a
                # collar file could ingest cleanly, appear on the map, and
                # leave every downhole view blank. Promoting here is what
                # makes an upload visible in the views built to show it.
                #
                # Fire-and-forget, and deliberately outside the run's own
                # success: the file HAS landed in silver by this point, and
                # a promotion that fails must not relabel a good ingest as
                # failed. dispatch_promotion owns the bound and the swallow
                # — it never raises, so there is nothing to catch here.
                from app.hatchet_workflows.promote_silver_to_gold import (  # noqa: PLC0415
                    dispatch_promotion,
                )
                await dispatch_promotion(
                    workspace_id=input.workspace_id,
                    project_id=input.project_id,
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
    "DBASE_EXTENSIONS",
    "DBF_EXTENSIONS",
    "DEFAULT_SOURCE_EPSG",
    "EXCEL_EXTENSIONS",
    "MAPINFO_DAT_EXTENSIONS",
    "SUPPORTED_EXTENSIONS",
    "IngestTabularInput",
    "IngestTabularOut",
    "ingest_tabular",
]
