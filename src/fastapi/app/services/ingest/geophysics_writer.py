"""Write parsed geophysics into silver (ING-19, 2026-09-29).

Two formats, one parent row each in ``silver.geophysics_surveys``:

* **Geosoft XYZ** (``georag_geoparsers.xyz_parser``) — line data. Each line
  (or 100k-point segment) is one ``silver.geophysics_lines`` row with its
  path in EPSG:4326 and the delivered coordinates kept verbatim; each
  channel of each line is one ``silver.geophysics_line_channels`` row, the
  values as an array (the ``well_log_curves`` shape — an airborne survey is
  millions of points, and a row per point is the wrong shape for both the
  table and every reader).
* **UBC-GIF DCIP2D** (``georag_geoparsers.dcip2d_survey``) — observed
  readings into ``silver.geophysics_dcip_observations`` (electrode
  CHAINAGES, not coordinates) and inversion models into
  ``silver.geophysics_dcip_models``. No geometry is written for either: the
  export does not say where the chainage axis is on the ground, and the
  parser's docstring is the record of why guessing it is not an option.

Idempotency
-----------
The survey is upserted on ``(workspace_id, project_id, survey_name)`` and
every child row of that survey is deleted before the new ones are written,
all in ONE transaction: a Hatchet retry or a corrected re-upload of the same
file replaces, never duplicates, and a failure part-way leaves the previous
upload intact.

CRS (XYZ only)
--------------
An XYZ file declares no coordinate system. The rule is the collar rule
(``collar_crs.decide_collar_crs``): the upload's declared EPSG, else the
project's, else the platform default — marked ``assumed`` and warned about
loudly — and a table whose values or headers are longitude/latitude is
placed as EPSG:4326 whatever the project says. Every placed line is then
checked against the project's known extent (``plausibility_warnings``); an
implausible position warns and never refuses.

Lineage lives on the rows (``source_file`` / ``source_file_sha256`` /
``source_object_key`` / ``parser_*`` on the survey, ``source_rows`` on each
line, ``source_file`` + ``source_row`` on each DC/IP reading), the same
choice ``ingest_spatial`` made — see tests/test_provenance_coverage.py.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

import asyncpg

log = logging.getLogger("georag.ingest.geophysics_writer")

#: ``chk_geophysics_surveys_type``.
SURVEY_TYPES: frozenset[str] = frozenset({
    "seismic", "magnetic", "gravity", "radiometric", "IP", "EM", "other",
})

#: Channel-name rules for the survey_type LABEL. A label, not a decision
#: anything depends on: a mixed survey (mag + radiometrics is the common
#: airborne package) is 'other', and every channel name is kept in
#: processing_notes either way.
_FAMILY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("magnetic", re.compile(r"MAG|TMI|RMI|IGRF|DIURN|(^|_)NT($|_)")),
    ("gravity", re.compile(r"GRAV|BOUG|FREE_?AIR|(^|_)(FAA|CBA|SBA|GZ)($|_)")),
    ("radiometric", re.compile(
        r"^(K|U|TH|TC|EU|ETH)($|_|PCT|PPM|CPS|CORR)|TOTAL_?COUNT|DOSE|RADIO|SPECTR"
    )),
    ("EM", re.compile(
        r"^EM|HEM|TEM|DBDT|DB_DT|INPHASE|QUAD|(^|_)C[PX][IQ]\d*|OFF_?TIME|AEM|B_?FIELD"
    )),
    ("IP", re.compile(r"CHARG|(^|_)IP($|_)|RESIS|APP_?RES|(^|_)(RHO|MX|VP)($|_)")),
)

#: Channels that describe the platform, not the ground — never a type signal.
_NON_SIGNAL = re.compile(
    r"^(FID|FIDUCIAL|DATE|TIME|UTC|GPS.*|ALT.*|RADALT.*|BARO.*|HEIGHT|ELEV.*|DEM|Z|"
    r"LAT.*|LON.*|X.*|Y.*|EAST.*|NORTH.*|FLIGHT|LINE.*|HEADING|SPEED|PITCH|ROLL|YAW)$"
)

_INSERT_BATCH = 500


@dataclass
class GeophysicsWriteResult:
    """What one survey write landed, for the run summary."""

    survey_id: str | None = None
    survey_name: str = ""
    survey_type: str = "other"
    #: True when a survey of the same name already existed in the project
    #: and was replaced (its previous lines/readings/models deleted first).
    replaced: bool = False
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)
    georef_method: str | None = None
    source_epsg: int | None = None

    @property
    def rows_written(self) -> int:
        """Measurement rows landed: XYZ points, or DC/IP readings + models."""
        return (
            self.counts.get("points", 0)
            + self.counts.get("observations", 0)
            + self.counts.get("models", 0)
        )


# ---------------------------------------------------------------------------
# Survey type
# ---------------------------------------------------------------------------

def classify_channels(names: list[str]) -> tuple[str, dict[str, list[str]]]:
    """``(survey_type, {family: [channels]})`` from channel names."""
    families: dict[str, list[str]] = {}
    for name in names:
        key = re.sub(r"[^A-Z0-9]+", "_", str(name).upper()).strip("_")
        if not key or _NON_SIGNAL.match(key):
            continue
        for family, pattern in _FAMILY_RULES:
            if pattern.search(key):
                families.setdefault(family, []).append(str(name))
                break
    if len(families) == 1:
        return next(iter(families)), families
    return "other", families


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_SURVEY_UPSERT_SQL = """
INSERT INTO silver.geophysics_surveys (
    workspace_id, project_id, survey_type, survey_name, line_ids, crs_epsg,
    processing_notes, anomaly_summary, source_file, source_file_sha256,
    source_object_key, parser_name, parser_version, georef_method,
    crs_confidence, aoi_geom, created_at, updated_at
) VALUES (
    $1::uuid, $2::uuid, $3, $4, $5::text[], $6::integer,
    $7, $8, $9, $10,
    $11, $12, $13, $14,
    $15::real, NULL, now(), now()
)
ON CONFLICT (workspace_id, project_id, survey_name) DO UPDATE SET
    survey_type        = EXCLUDED.survey_type,
    line_ids           = EXCLUDED.line_ids,
    crs_epsg           = EXCLUDED.crs_epsg,
    processing_notes   = EXCLUDED.processing_notes,
    anomaly_summary    = EXCLUDED.anomaly_summary,
    source_file        = EXCLUDED.source_file,
    source_file_sha256 = EXCLUDED.source_file_sha256,
    source_object_key  = EXCLUDED.source_object_key,
    parser_name        = EXCLUDED.parser_name,
    parser_version     = EXCLUDED.parser_version,
    georef_method      = EXCLUDED.georef_method,
    crs_confidence     = EXCLUDED.crs_confidence,
    aoi_geom           = NULL,
    updated_at         = now()
RETURNING survey_id::text AS survey_id, (xmax::text <> '0') AS replaced
"""
# contractor / acquisition_date / interpretation_pdf_id are deliberately NOT
# in the column list: nothing in either format states them (dcip2d_survey's
# payload docstring explains why each is not derived), and leaving them out
# of the UPDATE keeps a value a person entered later across a re-upload.

_CLEAR_CHILDREN_SQL = (
    "WITH l AS (DELETE FROM silver.geophysics_lines WHERE survey_id = $1::uuid RETURNING 1),"
    " o AS (DELETE FROM silver.geophysics_dcip_observations"
    "       WHERE survey_id = $1::uuid RETURNING 1),"
    " m AS (DELETE FROM silver.geophysics_dcip_models WHERE survey_id = $1::uuid RETURNING 1)"
    " SELECT (SELECT count(*) FROM l) + (SELECT count(*) FROM o) + (SELECT count(*) FROM m)"
)

#: The ordered point array, built once and shared by both geometry shapes.
_POINTS = (
    "ARRAY(SELECT ST_MakePoint(p.x, p.y)"
    " FROM unnest($7::float8[], $8::float8[]) WITH ORDINALITY AS p(x, y, i)"
    " ORDER BY p.i)"
)

LINE_SQL = f"""
INSERT INTO silver.geophysics_lines (
    workspace_id, project_id, survey_id, line_id, line_type, segment,
    point_count, x_native, y_native, source_rows, source_epsg, geom
)
SELECT $1::uuid, $2::uuid, $3::uuid, $4::text, $5::varchar, $6::integer,
       cardinality($7::float8[]), $7::float8[], $8::float8[], $9::integer[], $10::integer,
       CASE
           WHEN $11::boolean THEN NULL
           WHEN cardinality($7::float8[]) = 1 THEN ST_Transform(
               ST_SetSRID(ST_MakePoint(($7::float8[])[1], ($8::float8[])[1]), $10::integer),
               4326)
           WHEN $5::varchar = 'points' THEN ST_Transform(
               ST_SetSRID(ST_Collect({_POINTS}), $10::integer), 4326)
           ELSE ST_Transform(ST_SetSRID(ST_MakeLine({_POINTS}), $10::integer), 4326)
       END
RETURNING line_pk::text
"""

CHANNEL_SQL = """
INSERT INTO silver.geophysics_line_channels (
    workspace_id, project_id, survey_id, line_pk, channel_name,
    channel_values, null_count, min_value, max_value
) VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5, $6::float8[], $7, $8, $9)
"""

_AOI_SQL = """
UPDATE silver.geophysics_surveys s
   SET aoi_geom = (
           SELECT CASE WHEN GeometryType(h.hull) = 'POLYGON' THEN h.hull END
             FROM (SELECT ST_ConvexHull(ST_Collect(l.geom)) AS hull
                     FROM silver.geophysics_lines l
                    WHERE l.survey_id = s.survey_id AND l.geom IS NOT NULL) h
       ),
       updated_at = now()
 WHERE s.survey_id = $1::uuid
"""

OBSERVATION_SQL = """
INSERT INTO silver.geophysics_dcip_observations (
    workspace_id, project_id, survey_id, line_id, source_file, source_row,
    array_type, quantity, c1_chainage_m, c2_chainage_m, p1_chainage_m,
    p2_chainage_m, value
) VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
"""

MODEL_SQL = """
INSERT INTO silver.geophysics_dcip_models (
    workspace_id, project_id, survey_id, source_file, family, stage, iteration,
    is_final, unit, nx, nz, cell_values, air_mask, earth_min, earth_max,
    earth_median
) VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7, $8, $9, $10, $11,
          $12::float8[], $13::boolean[], $14, $15, $16)
"""


async def upsert_survey(
    conn: asyncpg.Connection,
    *,
    workspace_id: str,
    project_id: str,
    survey_type: str,
    survey_name: str,
    line_ids: list[str],
    crs_epsg: int | None,
    processing_notes: str,
    anomaly_summary: str | None,
    source_file: str,
    source_file_sha256: str | None,
    source_object_key: str | None,
    parser_name: str,
    parser_version: str,
    georef_method: str | None,
    crs_confidence: float | None,
) -> tuple[str, bool, int]:
    """Upsert the parent row and clear its children. ``(survey_id, replaced, cleared)``.

    Must run inside the caller's transaction, so the delete only lands with
    the re-insert.
    """
    if survey_type not in SURVEY_TYPES:
        raise ValueError(f"survey_type {survey_type!r} is not one of {sorted(SURVEY_TYPES)}")
    row = await conn.fetchrow(
        _SURVEY_UPSERT_SQL,
        workspace_id, project_id, survey_type, survey_name, line_ids, crs_epsg,
        processing_notes, anomaly_summary, source_file, source_file_sha256,
        source_object_key, parser_name, parser_version, georef_method,
        crs_confidence,
    )
    survey_id = str(row["survey_id"])
    cleared = int(await conn.fetchval(_CLEAR_CHILDREN_SQL, survey_id) or 0)
    return survey_id, bool(row["replaced"]), cleared


def _channel_row(values: list[float | None]) -> tuple[int, float | None, float | None]:
    clean = [v for v in values if v is not None and math.isfinite(v)]
    return (
        len(values) - len(clean),
        min(clean) if clean else None,
        max(clean) if clean else None,
    )


async def write_line(
    conn: asyncpg.Connection,
    *,
    workspace_id: str,
    project_id: str,
    survey_id: str,
    line: Any,
    source_epsg: int,
) -> tuple[str, bool]:
    """Insert one XYZ line and its channels. ``(line_pk, placed)``.

    The geometry is built in a savepoint; if PostGIS cannot transform it
    (coordinates outside what ``source_epsg`` can describe) the line is
    written again WITHOUT geometry and ``placed`` is False, so one bad line
    costs its map position, never the survey.
    """
    args = [
        workspace_id, project_id, survey_id, line.line_id, line.line_type,
        int(line.segment), [float(v) for v in line.x], [float(v) for v in line.y],
        [int(r) for r in line.source_rows], int(source_epsg),
    ]
    placed = True
    try:
        async with conn.transaction():
            line_pk = await conn.fetchval(LINE_SQL, *args, False)
    except asyncpg.PostgresError as exc:
        log.warning(
            "geophysics_writer: line %s segment %s did not transform from EPSG:%s (%s)",
            line.line_id, line.segment, source_epsg, type(exc).__name__,
        )
        placed = False
        line_pk = await conn.fetchval(LINE_SQL, *args, True)

    rows = []
    for name, values in line.channels.items():
        nulls, lo, hi = _channel_row(values)
        rows.append((
            workspace_id, project_id, survey_id, line_pk, str(name),
            [None if v is None or not math.isfinite(v) else float(v) for v in values],
            nulls, lo, hi,
        ))
    for start in range(0, len(rows), _INSERT_BATCH):
        await conn.executemany(CHANNEL_SQL, rows[start:start + _INSERT_BATCH])
    return str(line_pk), placed


async def finalize_aoi(conn: asyncpg.Connection, survey_id: str) -> None:
    """Set the survey's AOI to the convex hull of its placed lines (NULL if none)."""
    await conn.execute(_AOI_SQL, survey_id)


# ---------------------------------------------------------------------------
# Geosoft XYZ
# ---------------------------------------------------------------------------

@dataclass
class XyzSurveySummary:
    """Pass 1 over an XYZ file: enough to decide the CRS before writing."""

    header: Any
    line_ids: list[str]
    point_count: int
    line_count: int
    #: ``(label, x, y)`` — first, middle and last point of every line.
    sample: list[tuple[str, float, float]]
    issues: list[Any]


def summarize_xyz(path: str) -> XyzSurveySummary:
    """Read the file once (streamed) for its line ids, size and a coordinate sample."""
    from georag_geoparsers.xyz_parser import (  # noqa: PLC0415
        iter_xyz_lines,
        scan_xyz_header,
    )

    header = scan_xyz_header(path)
    issues: list[Any] = []
    line_ids: list[str] = []
    sample: list[tuple[str, float, float]] = []
    points = lines = 0
    for block in iter_xyz_lines(path, header, issues=issues):
        lines += 1
        points += block.point_count
        label = block.line_id if block.line_id is not None else "points"
        if block.line_id is not None and block.line_id not in line_ids:
            line_ids.append(block.line_id)
        n = block.point_count
        for i in sorted({0, n // 2, n - 1}):
            sample.append((f"{label}#{block.segment}:{i + 1}", block.x[i], block.y[i]))
    return XyzSurveySummary(header, line_ids, points, lines, sample, issues)


def _as_geophysics_warning(warning: dict[str, Any]) -> dict[str, Any]:
    """Reword a collar_crs warning for survey lines rather than drill holes."""
    out = dict(warning)
    code = str(out.get("code") or "")
    if code.startswith("collar_"):
        out["code"] = "geophysics_" + code[len("collar_"):]
    for key in ("message", "detail"):
        if isinstance(out.get(key), str):
            out[key] = (
                out[key]
                .replace("collar(s)", "survey point(s)")
                .replace("collars", "survey points")
                .replace("every hole", "every point")
                .replace("the holes", "the survey")
            )
    return out


def assumed_crs_warning(label: str, epsg: int, lines: int) -> dict[str, Any]:
    """The loud one — same stance as ingest_tabular's collar_crs_assumed."""
    return {
        "code": "geophysics_crs_assumed",
        "message": (
            f"{label}: {lines} survey line(s) placed using an ASSUMED coordinate "
            f"system (EPSG:{epsg}) — no CRS was declared"
        ),
        "detail": (
            f"An XYZ file does not state its projection, and neither this upload "
            f"nor its project declares one, so the X/Y values were read as "
            f"EPSG:{epsg}, the platform default. If the survey was flown or "
            f"walked in another zone or datum it is in the wrong place on the "
            f"map. Re-upload with the EPSG typed in the Import wizard, or set the "
            f"project's CRS — re-uploading replaces this survey in place."
        ),
    }


async def write_xyz_survey(
    conn: asyncpg.Connection,
    *,
    path: str,
    workspace_id: str,
    project_id: str,
    survey_name: str,
    source_file: str,
    source_file_sha256: str | None,
    source_object_key: str | None,
    declared_epsg: int | None,
    project_epsg: int | None,
    default_epsg: int,
) -> GeophysicsWriteResult:
    """Parse *path* as Geosoft XYZ and replace its survey in silver.

    Raises ValueError (from the parser) when the file has no usable header —
    a file-level refusal the caller reports. Row-level problems are skipped
    and summarised in ``warnings``.
    """
    from georag_geoparsers.xyz_parser import (  # noqa: PLC0415
        PARSER_NAME,
        PARSER_VERSION,
        iter_xyz_lines,
    )

    from app.services.ingest.collar_crs import (  # noqa: PLC0415
        decide_collar_crs,
        plausibility_warnings,
        project_reference,
    )

    summary = await asyncio.to_thread(summarize_xyz, path)
    header = summary.header
    result = GeophysicsWriteResult(survey_name=survey_name)

    if summary.point_count == 0:
        result.warnings.append({
            "code": "geophysics_no_points",
            "message": f"{source_file}: no data rows could be read",
            "detail": (
                f"{source_file} has a recognisable header "
                f"({header.easting_column}/{header.northing_column}) but no row "
                f"with numeric coordinates, so nothing was written."
            ),
        })
        result.warnings.extend(_row_issue_warnings(source_file, summary.issues))
        return result

    decision = decide_collar_crs(
        eastings=[p[1] for p in summary.sample],
        northings=[p[2] for p in summary.sample],
        easting_column=header.easting_column,
        northing_column=header.northing_column,
        declared_epsg=declared_epsg,
        project_epsg=project_epsg,
        default_epsg=default_epsg,
        label=source_file,
    )
    result.warnings.extend(_as_geophysics_warning(w) for w in decision.warnings)
    if decision.refusal is not None:
        result.warnings.append(_as_geophysics_warning(decision.refusal))
        result.counts = {"points": 0, "lines": 0}
        return result
    reference = await project_reference(conn, project_id)
    found, flagged = await asyncio.to_thread(
        plausibility_warnings,
        epsg=decision.epsg, points=summary.sample, reference=reference,
        label=source_file,
    )
    result.warnings.extend(_as_geophysics_warning(w) for w in found)
    confidence = decision.crs_confidence
    if flagged:
        confidence = min(confidence, 0.1)
    if decision.assumed:
        result.warnings.append(assumed_crs_warning(source_file, decision.epsg, summary.line_count))

    channels = header.channel_columns
    survey_type, families = classify_channels(channels)
    notes = [
        f"Geosoft XYZ export, {summary.point_count:,} point(s) on "
        f"{summary.line_count} line segment(s) "
        f"({len(summary.line_ids)} named line(s)).",
        f"Coordinates: {header.easting_column}/{header.northing_column} read as "
        f"EPSG:{decision.epsg} ({decision.georef_method}).",
        f"Channels ({len(channels)}): {', '.join(channels) or 'none'}.",
    ]
    if header.other_axis_columns:
        notes.append(
            "Also present and kept as channels: "
            + ", ".join(header.other_axis_columns) + "."
        )
    if len(families) > 1:
        notes.append(
            "Channel families: "
            + "; ".join(f"{f}: {', '.join(c)}" for f, c in sorted(families.items()))
            + " — survey_type recorded as 'other'."
        )
    if summary.issues:
        notes.append(f"{len(summary.issues)} row(s) skipped; see the ingest run warnings.")

    unplaced = 0
    points = lines = channel_rows = 0
    async with conn.transaction():
        survey_id, replaced, _cleared = await upsert_survey(
            conn,
            workspace_id=workspace_id, project_id=project_id,
            survey_type=survey_type, survey_name=survey_name,
            line_ids=summary.line_ids, crs_epsg=decision.epsg,
            processing_notes="\n".join(notes), anomaly_summary=None,
            source_file=source_file, source_file_sha256=source_file_sha256,
            source_object_key=source_object_key, parser_name=PARSER_NAME,
            parser_version=PARSER_VERSION, georef_method=decision.georef_method,
            crs_confidence=confidence,
        )
        blocks = iter_xyz_lines(path, header)
        while True:
            block = await asyncio.to_thread(next, blocks, None)
            if block is None:
                break
            _pk, placed = await write_line(
                conn, workspace_id=workspace_id, project_id=project_id,
                survey_id=survey_id, line=block, source_epsg=decision.epsg,
            )
            lines += 1
            points += block.point_count
            channel_rows += len(block.channels)
            unplaced += 0 if placed else 1
        await finalize_aoi(conn, survey_id)

    if unplaced:
        result.warnings.append({
            "code": "geophysics_lines_unplaced",
            "message": (
                f"{source_file}: {unplaced} line segment(s) stored without a map "
                f"position"
            ),
            "detail": (
                f"PostGIS could not transform {unplaced} line segment(s) from "
                f"EPSG:{decision.epsg} to longitude/latitude, so their values were "
                f"stored but they are not drawn on the map. Check the declared EPSG."
            ),
        })
    result.warnings.extend(_row_issue_warnings(source_file, summary.issues))
    result.survey_id = survey_id
    result.survey_type = survey_type
    result.replaced = replaced
    result.georef_method = decision.georef_method
    result.source_epsg = decision.epsg
    result.counts = {
        "points": points, "lines": lines, "channels": channel_rows,
        "skipped_rows": len(summary.issues), "unplaced_lines": unplaced,
    }
    return result


def _row_issue_warnings(label: str, issues: list[Any]) -> list[dict[str, Any]]:
    """One warning per issue code, naming up to five rows."""
    by_code: dict[str, list[Any]] = {}
    for issue in issues:
        by_code.setdefault(issue.code, []).append(issue)
    out: list[dict[str, Any]] = []
    for code, found in by_code.items():
        rows = ", ".join(str(i.row) for i in found[:5])
        more = f" and {len(found) - 5} more" if len(found) > 5 else ""
        out.append({
            "code": f"xyz_rows_skipped_{code}",
            "message": f"{label}: {len(found)} row(s) skipped ({code.replace('_', ' ')})",
            "detail": f"{found[0].reason}. Rows: {rows}{more}. The rest of the file landed.",
        })
    return out


# ---------------------------------------------------------------------------
# UBC-GIF DCIP2D
# ---------------------------------------------------------------------------

_MODEL_UNIT = {"dcinv2d": "S/m", "ipinv2d": "mV/V"}


async def write_dcip_survey(
    conn: asyncpg.Connection,
    *,
    survey: Any,
    workspace_id: str,
    project_id: str,
    survey_name: str,
    source_file: str,
    source_file_sha256: str | None,
    source_object_key: str | None,
) -> GeophysicsWriteResult:
    """Replace one DCIP2D survey — readings and models — in silver."""
    import numpy as np  # noqa: PLC0415
    from georag_geoparsers.dcip2d_survey import PARSER_NAME, PARSER_VERSION  # noqa: PLC0415

    payload = survey.to_geophysics_survey_payload(survey_name)
    result = GeophysicsWriteResult(survey_name=survey_name, survey_type=str(payload["survey_type"]))

    final_ids = {
        id(m) for m in (survey.final_conductivity, survey.final_chargeability) if m is not None
    }
    observations = 0
    models = 0
    async with conn.transaction():
        survey_id, replaced, _cleared = await upsert_survey(
            conn,
            workspace_id=workspace_id, project_id=project_id,
            survey_type=str(payload["survey_type"]), survey_name=survey_name,
            line_ids=list(payload["line_ids"] or []),  # type: ignore[call-overload]
            crs_epsg=payload["crs_epsg"],  # type: ignore[arg-type]
            processing_notes=str(payload["processing_notes"]),
            anomaly_summary=str(payload["anomaly_summary"] or "") or None,
            source_file=source_file, source_file_sha256=source_file_sha256,
            source_object_key=source_object_key, parser_name=PARSER_NAME,
            parser_version=PARSER_VERSION,
            # A DC/IP export places nothing on the ground (dcip2d_survey), so
            # there is no georeference method to record — NULL, not 'assumed'.
            georef_method=None, crs_confidence=None,
        )
        rows = []
        for split in survey.observed:
            source_rows = split.source_rows or tuple(range(3, 3 + len(split.records)))
            for (c1, c2, p1, p2, value), row_no in zip(split.records, source_rows, strict=True):
                rows.append((
                    workspace_id, project_id, survey_id, survey.line_id, split.filename,
                    int(row_no), survey.array_type, split.quantity,
                    float(c1), float(c2), float(p1), float(p2), float(value),
                ))
        for start in range(0, len(rows), _INSERT_BATCH):
            await conn.executemany(OBSERVATION_SQL, rows[start:start + _INSERT_BATCH])
        observations = len(rows)

        for model_file in survey.models:
            model = model_file.model
            earth = model.values[~model.air_mask]
            await conn.execute(
                MODEL_SQL,
                workspace_id, project_id, survey_id, model_file.filename,
                model_file.family, model_file.stage, model_file.iteration,
                id(model_file) in final_ids, _MODEL_UNIT[model_file.family],
                int(model.nx), int(model.nz),
                [float(v) for v in model.values.ravel(order="C")],
                [bool(v) for v in model.air_mask.ravel(order="C")],
                float(earth.min()) if earth.size else None,
                float(earth.max()) if earth.size else None,
                float(np.median(earth)) if earth.size else None,
            )
            models += 1

    if not survey.is_georeferenced:
        reasons = list(survey.join.unresolved_reasons) + list(survey.mesh.unresolved_reasons)
        result.warnings.append({
            "code": "dcip_not_georeferenced",
            "message": (
                f"{survey_name}: line {survey.line_id} was stored in chainage / mesh "
                f"coordinates — it cannot be placed on the map"
            ),
            "detail": (
                "The DC/IP readings and inversion sections were stored, but nothing "
                "in the export says where the line is on the ground: "
                + "; ".join(reasons[:4])
                + ". Delivering the station coordinates for this line (with a zone "
                "and datum) and the mesh file would resolve it."
            ),
        })
    skipped = [
        (split.filename, row, reason)
        for split in survey.observed for row, reason in split.skipped_rows
    ]
    if skipped:
        named = ", ".join(f"{f} {r}" for f, r, _ in skipped[:5])
        result.warnings.append({
            "code": "dcip_rows_skipped",
            "message": f"{survey_name}: {len(skipped)} malformed reading row(s) skipped",
            "detail": (
                f"{skipped[0][2]}. Rows: {named}"
                + (f" and {len(skipped) - 5} more" if len(skipped) > 5 else "")
                + ". A reading missing a field cannot be repaired by guessing "
                "which electrode it lost; the rest of the file landed."
            ),
        })
    for filename, reason in survey.rejected_files:
        result.warnings.append({
            "code": "dcip_model_rejected",
            "message": f"{survey_name}: model {filename} could not be read and was left out",
            "detail": f"{reason} The other models and the readings landed.",
        })

    result.survey_id = survey_id
    result.replaced = replaced
    result.counts = {
        "observations": observations, "models": models,
        "skipped_rows": len(skipped), "rejected_files": len(survey.rejected_files),
    }
    return result


__all__ = [
    "CHANNEL_SQL",
    "LINE_SQL",
    "MODEL_SQL",
    "OBSERVATION_SQL",
    "SURVEY_TYPES",
    "GeophysicsWriteResult",
    "XyzSurveySummary",
    "assumed_crs_warning",
    "classify_channels",
    "finalize_aoi",
    "summarize_xyz",
    "upsert_survey",
    "write_dcip_survey",
    "write_line",
    "write_xyz_survey",
]
