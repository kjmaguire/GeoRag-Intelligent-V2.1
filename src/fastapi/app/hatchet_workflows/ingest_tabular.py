"""Ingest drill data from CSV and XLSX into the silver drill tables.

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
A dBASE table matches no geology schema at all, so its rows land in
``silver.attribute_tables`` as JSONB rather than being guessed into a
collar or a sample. They arrive from GIS deliveries as legend tables,
survey point registers and comment logs: real data with no typed home.

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
import datetime as _dt
import json
import logging
import math
import re
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
SUPPORTED_EXTENSIONS = CSV_EXTENSIONS | EXCEL_EXTENSIONS | DBASE_EXTENSIONS

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
ON CONFLICT (project_id, sample_id)
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

#: Below-detection sentinel. Measured in all_historical_soils_clean.DAT:
#: au_ppm ranges -9..1.589, and -9 is the legacy "not detected" marker, not a
#: negative concentration. Storing it as a number would make every summary
#: statistic wrong — a mean grade pulled below zero by absent samples.
_BELOW_DETECTION = -9.0


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
        "easting": ["easting", "east", "utm_e", "x", "xcoord"],
        "northing": ["northing", "north", "utm_n", "y", "ycoord"],
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
        depths = [(_num(s.get(mapped["depth"])) or 0.0, s) for s in segments]
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
            if value is None or value == _BELOW_DETECTION:
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


#: How the four CSV parsers report a FILE-level refusal: one
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


def _assumed_crs_warning(epsg: int, collars_written: int) -> dict[str, Any]:
    """Say that these collars were placed by guess, and name the guess."""
    return {
        "code": "collar_crs_assumed",
        "message": (
            f"{collars_written} collar(s) placed using an assumed "
            f"coordinate system (EPSG:{epsg})"
        ),
        "detail": (
            f"No coordinate system was supplied with this upload, so its "
            f"easting/northing were read as EPSG:{epsg}. If the holes were "
            f"surveyed in a different projection they are now in the wrong "
            f"place on the map — re-upload with the correct EPSG code to fix "
            f"it. Nothing in a CSV or spreadsheet declares a projection, so "
            f"this cannot be detected from the file."
        ),
    }


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
) -> dict[str, Any]:
    """Say which sheet was refused, what it was taken for, and why.

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
    warning: dict[str, Any] = {
        "code": "classified_but_nothing_written",
        "message": (
            f"'{label}' was treated as a {classified_as} sheet, but no "
            f"{classified_as} rows could be written"
        ),
        "detail": (
            f"{how} — which accepted none of its rows: {because}. No "
            f"{classified_as} rows were written. The sheet was kept as "
            f"searchable text and, where its columns allow, as a data "
            f"table; the warnings beside this one report what landed. {fix}"
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


def _parse_one(
    path: str,
    sheet_type: str,
    sheet_name: str | None,
    column_map: dict[str, dict[str, str]] | None = None,
) -> Any:
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

    vendor_aliases = _vendor_aliases_for(column_map, sheet_type)

    if sheet_name is None:
        return parser(path, vendor_aliases=vendor_aliases)

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
    )


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
            #: Standalone-.dbf branch state. Empty for every other format.
            attribute_rows: list[dict[str, Any]] = []
            attribute_layer = ""
            attribute_sha256 = ""

            if suffix in DBASE_EXTENSIONS:
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
                    sheet_type, confidence = classify_sheet_type(
                        headers, column_map=input.column_map,
                    )
                    sheets.append({
                        "sheet": filename, "type": sheet_type,
                        "confidence": confidence,
                    })
                if sheet_type in WRITE_ORDER:
                    work.append((sheet_type, None))
                else:
                    unclassified.append(filename)

            # A .dbf/.dat classifies to exactly one thing and never enters
            # `work`, so the drill-sheet advice below would be both wrong
            # and unactionable for it.
            if not work and suffix not in DBASE_EXTENSIONS:
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
                    result = await asyncio.to_thread(
                        _parse_one, local, write_type, target_sheet,
                        input.column_map,
                    )
                    records = getattr(result, "records", None) or []
                    warnings.extend(getattr(result, "warnings", None) or [])

                    if write_type == "collar":
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
                            sheet_type=write_type,
                            records=records, index=index,
                        )

                    prior = written.setdefault(
                        write_type,
                        {"written": 0, "skipped": 0, "orphaned": 0, "replaced": 0},
                    )
                    for k, v in stats.items():
                        prior[k] = prior.get(k, 0) + v
                    return result, stats

                for sheet_type, sheet_name in work:
                    # Where this attempt's parser warnings begin and end in
                    # the run's list. Both retry outcomes need the span:
                    # correction drops it (those notes describe a reading of
                    # the file the correction says was wrong), and a failed
                    # retry dedupes against it (both attempts re-detect the
                    # same encoding/delimiter facts and would report each
                    # twice).
                    forced_warn_start = len(warnings)
                    result, stats = await _parse_and_write(
                        sheet_type, sheet_name,
                    )
                    forced_warn_end = len(warnings)
                    if stats.get("written") or stats.get("orphaned"):
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
                            retry_result, retry_stats = await _parse_and_write(
                                headers_matched, None,
                            )
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
                        try:
                            collar_rows = _collapse_discover_traces(
                                attribute_rows, trace_shape,
                            )
                            if collar_rows:
                                written["collar"] = await _write_collars(
                                    conn,
                                    workspace_id=input.workspace_id,
                                    project_id=input.project_id,
                                    records=collar_rows,
                                    epsg=epsg,
                                    # The coordinates came from the export, not
                                    # from a human typing an EPSG — 'declared'
                                    # would overstate it, since the file itself
                                    # names no CRS. 'assumed' matches what the
                                    # tabular path already records elsewhere.
                                    georef_method="assumed" if epsg_assumed else "manual",
                                )
                                sheets.append({
                                    "sheet": filename,
                                    "type": "collar",
                                    "rows": written["collar"]["written"],
                                })
                                if epsg_assumed:
                                    warnings.append(_assumed_crs_warning(
                                        epsg, written["collar"]["written"],
                                    ))
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
                            written["geochemistry"] = await _write_surface_geochem(
                                conn,
                                workspace_id=input.workspace_id,
                                project_id=input.project_id,
                                shape=geochem_shape,
                                rows=attribute_rows,
                                source_epsg=epsg,
                            )
                            sheets.append({
                                "sheet": filename,
                                "type": "geochemistry",
                                "rows": written["geochemistry"]["written"],
                            })
                            if epsg_assumed:
                                warnings.append(_assumed_crs_warning(
                                    epsg, written["geochemistry"]["written"],
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
                    if label not in unclassified:
                        unclassified.append(label)
                    warnings.append(_wrote_nothing_warning(
                        label=label,
                        classified_as=classified_as,
                        reason=reason,
                        from_category=forced,
                        headers_matched=matched,
                        retry_reason=second,
                        remap=remap,
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
        collars_written = written.get("collar", {}).get("written", 0)
        if epsg_assumed and collars_written:
            warnings.append(_assumed_crs_warning(epsg, collars_written))

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
            ) + text_passages + table_rows
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
