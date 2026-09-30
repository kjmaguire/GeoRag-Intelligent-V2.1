"""Write parsed radiometric ages into ``silver.geochronology_samples`` (ING-19).

Called by ``ingest_tabular`` for a CSV, a worksheet or a dBASE/Access table
whose headers read as a geochronology table
(``csv_geochronology.geochronology_signal``).

Replace, per source file
------------------------
Every row an EARLIER upload of the same file (same project, same name with
the upload timestamp stripped — the ``ingest_spatial`` rule, ING-7) wrote is
deleted first, in the same transaction as the insert. A re-upload of a
corrected table therefore replaces, and a Hatchet retry cannot duplicate.

A row whose (project, sample, isotopic system, mineral) already exists from
a DIFFERENT file is not overwritten — silently moving an age between source
files would break its citation — and is skipped with a reason naming the
file it already came from. A duplicate inside the same file is skipped the
same way. Neither fails the file.

Location
--------
Longitude/latitude columns place the sample as EPSG:4326 directly. An
easting/northing pair goes through ``collar_crs.decide_collar_crs`` — the
upload's EPSG, else the project's, else the platform default, marked
``assumed`` with a loud warning — exactly as a collar table would. The
coordinates as delivered stay in ``x_native`` / ``y_native`` /
``source_epsg``. A row with no usable location keeps its age with a NULL
geom (the column is nullable because academic and government records often
carry none).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import asyncpg

log = logging.getLogger("georag.ingest.geochronology_writer")

#: Mirrors ingest_spatial's upload-stamp rule (ING-7).
_UPLOAD_STAMP_RE = re.compile(r"^[0-9]{8}_[0-9]{6}(?:_[0-9]{1,6})?_")
_UPLOAD_STAMP_SQL = "^[0-9]{8}_[0-9]{6}(_[0-9]{1,6})?_"


def logical_source_name(filename: str) -> str:
    """The file name as the user gave it, without the bronze upload stamp."""
    return _UPLOAD_STAMP_RE.sub("", filename, count=1) or filename


def routes_to_geochronology(
    headers: list[Any],
    drill_type: str | None,
    *,
    drill_types: tuple[str, ...] | frozenset[str],
    hinted: bool = False,
) -> bool:
    """Whether ``ingest_tabular`` writes a table with these headers as ages.

    ``hinted``: the upload category said so. Otherwise the header signal
    decides (``csv_geochronology.geochronology_signal``): "strong" (sample,
    age AND isotopic-system columns — a combination no drill table has)
    wins even over a drill classification, "weak" (sample, age and a method
    column) only when no drill layout (``drill_types``) claimed the table.
    """
    if hinted:
        return True
    from georag_geoparsers.csv_geochronology import geochronology_signal  # noqa: PLC0415

    signal = geochronology_signal([str(h) for h in headers if h is not None])
    return signal == "strong" or (signal == "weak" and drill_type not in drill_types)


INSERT_SQL = """
INSERT INTO silver.geochronology_samples (
    workspace_id, project_id, sample_id, rock_type, isotopic_system,
    mineral_dated, age_ma, age_uncertainty_ma, uncertainty_kind,
    analytical_method, laboratory, publication_ref, geom, crs_confidence,
    georef_method, x_native, y_native, source_epsg, source_file,
    source_file_sha256, source_object_key, source_row, parser_name,
    parser_version, created_at, updated_at
) VALUES (
    $1::uuid, $2::uuid, $3, $4, $5,
    $6, $7::float8::numeric, $8::float8::numeric, $9,
    $10, $11, $12,
    CASE
        WHEN $13::float8 IS NULL OR $14::float8 IS NULL OR $17::integer IS NULL THEN NULL
        ELSE ST_Transform(ST_SetSRID(ST_MakePoint($13::float8, $14::float8), $17::integer), 4326)
    END,
    $15::real, $16, $13::float8, $14::float8, $17::integer, $18,
    $19, $20, $21, $22,
    $23, now(), now()
)
ON CONFLICT (workspace_id, project_id, sample_id, isotopic_system, mineral_dated)
DO NOTHING
RETURNING sample_pk
"""

_REPLACE_SQL = (
    "WITH gone AS ("
    "  DELETE FROM silver.geochronology_samples"
    "   WHERE project_id = $1::uuid"
    "     AND (source_file = $2 OR regexp_replace(source_file, $4, '') = $3)"
    "  RETURNING 1"
    ") SELECT count(*) FROM gone"
)

_EXISTING_SOURCE_SQL = """
SELECT source_file FROM silver.geochronology_samples
 WHERE workspace_id = $1::uuid AND project_id = $2::uuid AND sample_id = $3
   AND isotopic_system = $4 AND mineral_dated IS NOT DISTINCT FROM $5
"""


@dataclass
class GeochronWriteStats:
    written: int = 0
    skipped: int = 0
    replaced: int = 0
    located: int = 0
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def as_counts(self) -> dict[str, int]:
        return {
            "written": self.written, "skipped": self.skipped,
            "orphaned": 0, "replaced": self.replaced,
        }


async def write_geochronology(
    conn: asyncpg.Connection,
    *,
    workspace_id: str,
    project_id: str,
    result: Any,
    label: str,
    source_file: str,
    source_file_sha256: str | None,
    source_object_key: str | None,
    declared_epsg: int | None,
    project_epsg: int | None,
    default_epsg: int,
) -> GeochronWriteStats:
    """Replace this file's ages. ``result`` is a ``GeochronParseResult``."""
    from georag_geoparsers.csv_geochronology import PARSER_NAME, PARSER_VERSION  # noqa: PLC0415

    from app.services.ingest.collar_crs import (  # noqa: PLC0415
        _is_geographic,
        decide_collar_crs,
        plausibility_warnings,
        project_reference,
    )

    stats = GeochronWriteStats()
    records: list[dict[str, Any]] = list(getattr(result, "records", None) or [])
    column_map: dict[str, str] = dict(getattr(result, "column_map", None) or {})

    # ── CRS. Lon/lat rows: WGS 84, or the geographic EPSG the upload
    # declared. Easting/northing rows: the collar rule (declared / project /
    # assumed). Both are then checked against the project's known extent.
    geographic = [r for r in records if r.get("geom_wkt") is not None]
    projected = [
        r for r in records
        if r.get("geom_wkt") is None
        and r.get("easting") is not None and r.get("northing") is not None
    ]
    geo_epsg, geo_method, geo_conf = 4326, "detected", 0.8
    if declared_epsg is not None and _is_geographic(declared_epsg):
        geo_epsg, geo_method, geo_conf = int(declared_epsg), "declared", 1.0

    decision = None
    if projected:
        decision = decide_collar_crs(
            eastings=[r["easting"] for r in projected],
            northings=[r["northing"] for r in projected],
            easting_column=column_map.get("easting"),
            northing_column=column_map.get("northing"),
            declared_epsg=declared_epsg,
            project_epsg=project_epsg,
            default_epsg=default_epsg,
            label=label,
        )
        stats.warnings.extend(_as_sample_warning(w) for w in decision.warnings)
        if decision.refusal is not None:
            stats.warnings.append(_as_sample_warning(decision.refusal))
            projected = []
            decision = None
        elif decision.assumed:
            stats.warnings.append({
                "code": "geochron_crs_assumed",
                "message": (
                    f"{label}: {len(projected)} sample location(s) placed using an "
                    f"ASSUMED coordinate system (EPSG:{decision.epsg})"
                ),
                "detail": (
                    f"Neither this upload nor its project declares a coordinate "
                    f"system, so the easting/northing values were read as "
                    f"EPSG:{decision.epsg}, the platform default. If the samples "
                    f"were located in another zone or datum they are misplaced on "
                    f"the map (the ages themselves are unaffected). Re-upload with "
                    f"the EPSG typed in the Import wizard — re-uploading replaces "
                    f"these rows. A project's coordinate system can only be set "
                    f"when the project is created."
                ),
            })

    # Where each row goes: (x, y, epsg, georef_method, crs_confidence).
    placement: dict[int, tuple[float, float, int, str, float]] = {}
    for r in geographic:
        placement[int(r["_source_row"])] = (
            float(r["longitude"]), float(r["latitude"]), geo_epsg, geo_method, geo_conf,
        )
    if decision is not None:
        for r in projected:
            placement[int(r["_source_row"])] = (
                float(r["easting"]), float(r["northing"]), decision.epsg,
                decision.georef_method, decision.crs_confidence,
            )
    if placement:
        reference = await project_reference(conn, project_id)
        by_epsg: dict[int, list[tuple[str, float, float]]] = {}
        for row_no, (x, y, epsg, _method, _conf) in placement.items():
            by_epsg.setdefault(epsg, []).append((str(row_no), x, y))
        for epsg, points in by_epsg.items():
            found, flagged = await asyncio.to_thread(
                plausibility_warnings,
                epsg=epsg, points=points, reference=reference, label=label,
            )
            stats.warnings.extend(_as_sample_warning(w) for w in found)
            for flagged_row in flagged:
                x, y, e, m, c = placement[int(flagged_row)]
                placement[int(flagged_row)] = (x, y, e, m, min(c, 0.1))

    duplicates: list[tuple[int, str]] = []
    refused: list[tuple[int, str]] = []
    async with conn.transaction():
        stats.replaced = int(await conn.fetchval(
            _REPLACE_SQL, project_id, source_file,
            logical_source_name(source_file), _UPLOAD_STAMP_SQL,
        ) or 0)

        for r in records:
            row_no = int(r["_source_row"])
            placed = placement.get(row_no)
            px: float | None = placed[0] if placed else None
            py: float | None = placed[1] if placed else None
            epsg_used = placed[2] if placed else None
            method = placed[3] if placed else None
            conf = placed[4] if placed else None
            try:
                async with conn.transaction():
                    pk = await conn.fetchval(
                        INSERT_SQL,
                        workspace_id, project_id, r["sample_id"], r.get("rock_type"),
                        r["isotopic_system"], r.get("mineral_dated"), r.get("age_ma"),
                        r.get("age_uncertainty_ma"), r.get("uncertainty_kind"),
                        r.get("analytical_method"), r.get("laboratory"),
                        r.get("publication_ref"), px, py, conf, method, epsg_used,
                        source_file, source_file_sha256, source_object_key,
                        row_no, PARSER_NAME, PARSER_VERSION,
                    )
            except asyncpg.PostgresError as exc:
                # A value the table refuses fails THIS row, never the file
                # (ING-1). The class name, not the message: messages quote values.
                log.warning(
                    "geochronology_writer: row %s refused (%s)", row_no, type(exc).__name__,
                )
                stats.skipped += 1
                refused.append((
                    row_no, f"row {row_no}: refused by the database ({type(exc).__name__})",
                ))
                continue
            if pk is None:
                prior = await conn.fetchval(
                    _EXISTING_SOURCE_SQL, workspace_id, project_id, r["sample_id"],
                    r["isotopic_system"], r.get("mineral_dated"),
                )
                where = (
                    "earlier in this file" if prior == source_file
                    else f"from {logical_source_name(str(prior))!r}"
                )
                stats.skipped += 1
                duplicates.append((
                    row_no,
                    f"row {row_no}: sample {r['sample_id']!r} already has a "
                    f"{r['isotopic_system']} age for "
                    f"{r.get('mineral_dated') or 'an unnamed mineral'} {where}",
                ))
                continue
            stats.written += 1
            if px is not None:
                stats.located += 1

    for code, found_rows, tail in (
        ("geochron_rows_not_written", duplicates,
         "The table holds one age per sample, isotopic system and mineral in a "
         "project; the existing value was kept."),
        ("geochron_rows_refused", refused, "Each was skipped on its own."),
    ):
        if not found_rows:
            continue
        rows = ", ".join(str(n) for n, _ in found_rows[:5])
        more = f" and {len(found_rows) - 5} more" if len(found_rows) > 5 else ""
        stats.warnings.append({
            "code": code,
            "message": f"{label}: {len(found_rows)} age row(s) were not written",
            "detail": (
                f"{found_rows[0][1]}. Rows: {rows}{more}. {tail} "
                f"The rest of the file landed."
            ),
        })
    issues = list(getattr(result, "location_issues", None) or [])
    if issues:
        stats.warnings.append({
            "code": "geochron_location_dropped",
            "message": (
                f"{label}: {len(issues)} sample(s) kept without a location — "
                f"latitude/longitude out of range"
            ),
            "detail": (
                f"{issues[0]['reason']}. Rows: "
                + ", ".join(str(i["row"]) for i in issues[:5])
                + ". Correct the coordinates and re-upload to place them."
            ),
        })
    return stats


def _as_sample_warning(warning: dict[str, Any]) -> dict[str, Any]:
    """Reword a collar_crs warning for dated samples rather than drill holes."""
    out = dict(warning)
    code = str(out.get("code") or "")
    if code.startswith("collar_"):
        out["code"] = "geochron_" + code[len("collar_"):]
    for key in ("message", "detail"):
        if isinstance(out.get(key), str):
            out[key] = (
                out[key]
                .replace("collar(s)", "sample location(s)")
                .replace("collars", "sample locations")
                .replace("every hole", "every sample")
                .replace("the holes", "the samples")
            )
    return out


__all__ = [
    "INSERT_SQL",
    "GeochronWriteStats",
    "logical_source_name",
    "routes_to_geochronology",
    "write_geochronology",
]
