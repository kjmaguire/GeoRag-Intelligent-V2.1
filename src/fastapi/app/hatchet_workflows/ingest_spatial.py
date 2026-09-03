"""Ingest geology vector data into silver.spatial_features.

Formats
-------
Everything ``georag_geoparsers.spatial_parser`` reads — ESRI Shapefile,
GeoJSON, GeoPackage, GML, GPX, DXF, File Geodatabase, FlatGeobuf — plus QGIS
projects (``.qgs`` / ``.qgz``) via ``georag_geoparsers.qgis_parser``.

Why this workflow exists
------------------------
The `spatial` upload category was answered with ``422 retired_pipeline`` from
2026-07-28, when the Dagster services were removed. The parsers were never the
problem — they are the most heavily audited code in the ingestion path (CRS
confidence scoring, QField detection, multi-layer GeoPackage handling) and
they kept working. What went away was the thing that called them. Uploading a
shapefile has been impossible since, which is a strange gap in a geology
platform.

A QGIS project is not a data file
---------------------------------
``.qgs`` / ``.qgz`` describe a *map*: which layers to draw and where their
data lives. Two outcomes, and they are reported differently on purpose:

  * The project bundles its data (the usual ``.qgz`` case) — each resolvable
    layer is parsed and written, tagged with its own ``source_layer``.
  * The project points at the geologist's own disk — nothing can be read.
    That is NOT a parse failure, and reporting "0 features" would read like
    one. The run completes with ``manifest_only`` set and the layer inventory
    in its stats, so the answer to "what is in this project" survives even
    when the data did not come with it.

Tenancy
-------
Every row carries workspace_id and project_id, and the connection binds the
workspace GUC before writing, so silver.spatial_features' RLS policy applies
to the insert rather than being bypassed.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time as _t
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
from georag_object_storage import Bucket, get_storage_client
from hatchet_sdk import Context
from pydantic import BaseModel, Field, field_validator

from app.db import bind_workspace_scope
from app.db.dsn import build_dsn
from app.hatchet_workflows import _progress, hatchet

log = logging.getLogger("georag.hatchet.ingest_spatial")

#: Extensions this workflow claims. Anything else should never be routed here
#: — the upload controller decides, and a mismatch is a routing bug worth
#: failing loudly on rather than silently producing zero features.
VECTOR_EXTENSIONS = frozenset({
    ".shp", ".geojson", ".json", ".gpkg", ".gml", ".gpx",
    ".dxf", ".dgn", ".gdb", ".fgb",
    # MapInfo, 2026-08-23. The parser learned "MapInfo File" in the same
    # change that added .tab/.mif to Laravel's CATEGORIES and the frontend
    # bundler -- but NOT to this set, which is what _extract_archive uses to
    # decide which extracted members are worth opening. The result was that a
    # zipped .tab unpacked correctly, matched nothing here, and was reported
    # as "archive contains no readable vector file ... must include the .shp",
    # which is both wrong and unactionable. Five real MapInfo tables were
    # refused that way before this line existed.
    #
    # ONLY the two masters. .dat/.map/.id/.ind/.mid are sidecars read THROUGH
    # the master; .mid in particular opens directly, so listing it would
    # ingest a MIF/MID pair twice -- the same trap the shapefile sidecars are
    # kept out of this set to avoid.
    ".tab", ".mif",
    # Surpac string file, 2026-08-25. No OGR driver exists; spatial_parser
    # returns early to a hand-written reader. Listed here so a .str inside a
    # ZIP is recognised as a readable member rather than reported as "archive
    # contains no readable vector file".
    ".str",
})
QGIS_PROJECT_EXTENSIONS = frozenset({".qgs", ".qgz"})

#: A shapefile is never one file. ".shp" is meaningless without the ".shx"
#: index, the ".dbf" attribute table and — critically — the ".prj" that says
#: what coordinate system it is in. They travel as a ZIP, which is how
#: shapefiles are actually delivered, so refusing ZIP would refuse the normal
#: case while accepting the rare one.
ARCHIVE_EXTENSIONS = frozenset({".zip"})

SUPPORTED_EXTENSIONS = (
    VECTOR_EXTENSIONS | QGIS_PROJECT_EXTENSIONS | ARCHIVE_EXTENSIONS
)

#: Files inside an archive that are worth opening. A zipped shapefile also
#: contains .shx/.dbf/.prj/.cpg — those are read by pyogrio through the .shp
#: and must NOT be opened directly.
_ARCHIVE_MEMBER_EXTENSIONS = VECTOR_EXTENSIONS | QGIS_PROJECT_EXTENSIONS

#: The files a ".shp" needs beside it. Never opened directly -- pyogrio reads
#: them through the ".shp" -- but WHICH of them arrived decides what the
#: delivery is worth, and they are not equal: GDAL rebuilds a missing ".shx"
#: from the ".shp" itself (SHAPE_RESTORE_SHX, set once at spatial_parser
#: import), the ".dbf" carries the attributes, the ".cpg" only names an
#: encoding -- and the ".prj" is the one nothing can reconstruct.
_SHAPEFILE_SIDECAR_EXTENSIONS = frozenset({".shx", ".dbf", ".prj", ".cpg"})

#: Same ceiling as the QGIS extractor. A zip bomb must not fill the worker.
#:
#: Checked against the sum of the central directory BEFORE anything is
#: written, not accumulated mid-loop. The old form tripped after up to
#: 2 GiB had already landed on the worker's disk — the one case the cap
#: exists for was the one case it could not help.
_MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024

#: Parity with ingest_zip_archive's extractor, which has had this since
#: the 2026-06-28 audit. A 200,000-entry archive of one-byte members
#: passes the size cap comfortably and still spends the run's whole
#: 2 h budget on inode churn.
_MAX_ARCHIVE_ENTRIES = 50_000

#: Rows per executemany batch. Large enough to amortise round trips, small
#: enough that one oversized layer cannot build a single multi-hundred-MB
#: statement in memory.
_INSERT_BATCH = 500


# One DSN builder for the whole service — see app/db/dsn.py for why
# sixty copies of this existed and what the drift cost.
_build_dsn = build_dsn


class IngestSpatialInput(BaseModel):
    """Payload handed over by Laravel's UploadController."""

    workspace_id: str
    project_id: str
    minio_key: str
    run_id: str | None = None
    #: Overrides the parser's per-feature heuristic. The geologist knows what
    #: they uploaded ("these are all faults") better than a name-sniffing rule.
    feature_type: str | None = Field(default=None)
    #: EPSG of the file's own coordinates, supplied by the uploader for a
    #: delivery that declares none -- a ".shp" that arrived without its
    #: ".prj" being the case this exists for.
    #:
    #: A FALLBACK, never an override of a stated fact: a CRS the file itself
    #: declares always wins. Silently replacing a declared CRS with a
    #: half-remembered one is the same corruption in the other direction.
    #:
    #: An integer, never a CRS string. Same name, type and range as
    #: IngestTabularInput.source_epsg, as StoreQueryRequest's validation rule
    #: and as the CHECK on silver.spatial_features.crs_epsg_native -- one
    #: concept, one spelling, because three definitions of "a valid CRS" in
    #: one codebase is how they come to disagree.
    #:
    #: Defaulted, and it has to stay defaulted: ingest_zip_archive's spatial
    #: fan-out and stale_run_detector's recovery both construct this model
    #: with three fields, and a required field would break them at
    #: validation time.
    source_epsg: int | None = Field(default=None)
    #: The `.prj` text the wizard's CRS donation found in the same drop, for
    #: a recipient that cannot carry a `.prj` member of its own -- a `.dxf`
    #: or `.dgn` travels as a single file, not a ZIP, so the donation cannot
    #: be zipped in beside it the way it is for a shapefile bundle.
    #:
    #: Raw WKT, resolved to an EPSG integer HERE (see _epsg_from_wkt) and
    #: then fed through the same source_epsg path as every other override.
    #: The browser deliberately does no WKT->EPSG resolution -- see
    #: shapefileBundle.ts's crsLabel(), which calls a second, weaker
    #: resolver "a new way to be confidently wrong about a coordinate
    #: system" -- so the string must arrive whole and pyproj must be the one
    #: to read it. Ignored whenever source_epsg is supplied: a code the user
    #: typed outranks a copy the wizard found.
    source_crs_wkt: str | None = Field(default=None, max_length=65536)

    @field_validator("source_epsg")
    @classmethod
    def _epsg_in_range(cls, v: int | None) -> int | None:
        """Reject at the boundary what the database would reject at persist.

        crs_epsg_native is CHECK-constrained to 1024..32767. Letting a bad
        code through to the INSERT fails the whole file rather than one row
        -- the shape of the feature_type bug of 2026-08-20 -- and by then
        the uploader is long gone.
        """
        if v is None:
            return v
        if not (1024 <= v <= 32767):
            raise ValueError("EPSG codes must be in the range 1024-32767.")
        return v

    @field_validator("workspace_id", "project_id")
    @classmethod
    def _must_be_uuid(cls, v: str) -> str:
        import uuid  # noqa: PLC0415

        uuid.UUID(v)  # raises on malformed input, at the trigger boundary
        return v


def _epsg_from_wkt(wkt: str) -> tuple[int | None, str | None]:
    """Resolve donated `.prj` text to (EPSG int, CRS name), or (None, why).

    The pyproj work lives in georag_geoparsers.spatial_parser
    (epsg_from_wkt_text) — pyproj is that package's declared dependency,
    it is what reads every other `.prj` in this pipeline, and the
    default-confidence-only rule (min_confidence=25 measurably mis-matched
    a custom grid to EPSG:26929) is documented there.

    This wrapper owns the platform halves: unreadable uploader-supplied
    text is an answer, not an error; and the result is bounds-checked
    against the same 1024..32767 window as source_epsg itself --
    silver.spatial_features.crs_epsg_native carries a CHECK, and a
    resolution outside it must read as "unresolved", not as an INSERT
    failure an hour later.
    """
    try:
        from georag_geoparsers.spatial_parser import (  # noqa: PLC0415
            epsg_from_wkt_text,
        )

        epsg, name = epsg_from_wkt_text(wkt)
    except Exception as exc:  # noqa: BLE001 — donated text is uploader-supplied; unreadable is an answer, not an error
        log.warning("ingest_spatial: donated WKT unreadable: %s", exc)
        return None, None
    if epsg is None or not (1024 <= epsg <= 32767):
        return None, name
    return int(epsg), name


class IngestSpatialOut(BaseModel):
    run_id: str | None
    source_format: str
    features_written: int
    layers: list[str]
    #: True when a QGIS project was catalogued but none of its data shipped.
    manifest_only: bool = False
    #: Layer inventory for a QGIS project, whether or not it resolved.
    project_layers: list[dict[str, Any]] = Field(default_factory=list)
    empty_geom_skipped: int = 0
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    duration_ms: int = 0


#: $15 is appended rather than slotted in numerically so the existing
#: fourteen placeholders keep their numbers -- a renumber here is a silent
#: column-shuffle bug waiting to happen, and the tuple built in
#: _write_features just gains one element on the end.
#: Rows written before 2026-09-03 carry the storage basename in
#: ``source_file`` — upload timestamp included — so a re-upload of the same
#: file has to reach them through the prefix rule, not by equality. Same
#: regex as the Ingestion Runs display name. PostgreSQL's ARE dialect reads
#: ``\d``, ``{n}`` and ``(?:...)`` the way Python does, which is what lets one
#: pattern serve both sides (test_ingest_spatial_reupload_replaces.py pins it).
_LEGACY_SOURCE_FILE_PREFIX = _progress._GENERATED_PREFIX.pattern

#: $1 project_id, $2 the prefix-stripped source_file, $3 the prefix regex.
#: regexp_replace without the 'g' flag strips one leading prefix, exactly
#: as _filename_from_key does; a name already in the new shape is unchanged.
_REPLACE_SQL = """
WITH gone AS (
    DELETE FROM silver.spatial_features
     WHERE project_id = $1::uuid
       AND regexp_replace(source_file, $3, '') = $2
    RETURNING 1
) SELECT count(*) FROM gone
"""

_INSERT_SQL = """
INSERT INTO silver.spatial_features (
    feature_id, workspace_id, project_id,
    feature_type, feature_name, source, source_file, source_file_sha256,
    source_crs, source_layer, source_feature_id, properties,
    crs_epsg_native, crs_confidence, georef_method,
    created_at, updated_at, geom
) VALUES (
    gen_random_uuid(), $1::uuid, $2::uuid,
    $3, $4, $5, $6, $15,
    $7, $8, $9, $10::jsonb,
    $11, $12, $13,
    NOW(), NOW(),
    ST_Force2D(ST_SetSRID(ST_GeomFromText($14::text), 4326))
)
"""


def _sha256_file(path: Path) -> str:
    """Streaming SHA-256 of the source file.

    Streamed, not ``read_bytes()``: a spatial delivery is capped at 2 GiB
    and this runs on an 8 Gi worker. Same reason ``_extract_archive`` uses
    ``copyfileobj``.
    """
    import hashlib  # noqa: PLC0415

    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class _ArchiveExtraction:
    """What came out of a spatial archive, and what was refused.

    ``_extract_archive`` used to return a bare list, so a member it
    declined to write had no way to reach the run. The caller appends
    ``warnings`` to the run's own list, which makes
    ``mark_completed_by_run`` downgrade the terminal status to
    'partial' — the difference between a delivery that ingested
    cleanly and one that ingested in part.
    """

    members: list[Path]
    warnings: list[dict[str, Any]] = field(default_factory=list)
    #: For every ".shp" member, the shapefile sidecars that came with it,
    #: as lower-cased suffixes, keyed by ``str(member_path)``.
    #:
    #: ``members`` deliberately does not change to carry this -- a sidecar is
    #: not a thing to open, and returning one would parse the same layer
    #: twice or, for the ".prj", fail outright. But once GDAL can read a
    #: lone ".shp" the question stops being "can this be opened" and becomes
    #: "what came with it", and only the extractor is in a position to
    #: answer: by the time the parser has run, SHAPE_RESTORE_SHX may have
    #: written a ".shx" that was never delivered.
    companions: dict[str, list[str]] = field(default_factory=dict)


def _extract_archive(archive: Path, dest: Path) -> _ArchiveExtraction:
    """Expand a ZIP and return the vector/project files worth parsing.

    Guarded against Zip Slip — a member named ``../../etc/passwd`` is a real
    attack against naive extraction, and geology data arrives from third
    parties by definition.

    Sidecars are extracted but not returned: a zipped shapefile carries
    ``.shx``, ``.dbf``, ``.prj`` and often ``.cpg`` beside the ``.shp``, and
    pyogrio reads them through the ``.shp``. Returning them would parse the
    same layer four times and, for the ``.prj``, fail outright.

    Anything else in the archive (READMEs, metadata XML, stray PDFs) is left
    alone rather than guessed at.

    Refuses the whole archive, before writing a byte, when it is over the
    size or entry cap. Refusing beats truncating here because a spatial
    delivery is an INTERDEPENDENT file set: a shapefile is a .shp plus a
    .dbf plus a .shx plus usually a .prj, in no guaranteed order in the
    central directory. Stopping mid-archive does not yield 'fewer
    layers', it yields a layer whose attributes or CRS fell past the
    cut-off — geometry with no attribute table, or coordinates
    interpreted in the wrong CRS. That is corrupt, not incomplete, and it
    used to be reported as a clean ingest.
    """
    import zipfile  # noqa: PLC0415

    warnings: list[dict[str, Any]] = []
    root = dest.resolve()

    with zipfile.ZipFile(archive) as zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]

        if len(infos) > _MAX_ARCHIVE_ENTRIES:
            raise ValueError(
                f"ingest_spatial: archive holds {len(infos)} entries, over "
                f"the {_MAX_ARCHIVE_ENTRIES} cap (zip-bomb guard); refusing."
            )

        declared = sum(i.file_size for i in infos)
        if declared > _MAX_EXPANDED_BYTES:
            raise ValueError(
                f"ingest_spatial: archive expands to {declared} B, over the "
                f"{_MAX_EXPANDED_BYTES} B cap; refusing. Extracting the "
                f"part that fits would split shapefile sidecars from their "
                f"own .shp and report the result as a complete ingest. "
                f"Split the delivery into smaller archives."
            )

        for info in infos:
            target = (dest / info.filename).resolve()
            # Anchored on the separator, not a bare string prefix.
            # `startswith(str(root))` also accepts a SIBLING whose name
            # starts with the root's, so a member named
            # `../_unzipped_evil/x.geojson` resolved outside the
            # extraction root and was written — verified against the
            # live function before this line was changed. It landed in
            # the enclosing mkdtemp rather than anywhere dangerous, but
            # that containment is an accident of where the caller happens
            # to put `dest`, not something this guard established. The
            # sibling extractor in ingest_zip_archive has always had the
            # separator.
            if target != root and not str(target).startswith(str(root) + os.sep):
                log.warning(
                    "ingest_spatial: refusing archive member escaping the "
                    "extraction root: %r", info.filename,
                )
                warnings.append({
                    "code": "archive_member_refused",
                    "member": info.filename,
                    "detail": (
                        "This member would have been written outside the "
                        "extraction directory and was skipped. The archive "
                        "is malformed or was tampered with; ask for a "
                        "fresh copy."
                    ),
                })
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            # copyfileobj, not read() — `src.read()` made one member
            # wholly resident, so a single 1.5 GiB raster or FileGDB
            # table inside an otherwise-legal archive peaked at its full
            # size on an 8 Gi worker. Same reason the sibling extractor
            # uses it.
            with zf.open(info) as src, open(target, mode='wb') as out:
                shutil.copyfileobj(src, out)

    # __MACOSX/ and dot-underscore files are AppleDouble resource forks that
    # macOS adds when zipping. They mirror the real names, so including them
    # would double every layer.
    def _is_member(p) -> bool:
        if "__MACOSX" in p.parts or p.name.startswith("._"):
            return False
        suffix = p.suffix.lower()
        if p.is_file():
            return suffix in _ARCHIVE_MEMBER_EXTENSIONS
        # An Esri File Geodatabase is a DIRECTORY, and pyogrio's OpenFileGDB
        # driver takes that directory path. The is_file() filter here excluded
        # it, and its contents (a00000001.gdbtable, .gdbtablx, timestamps)
        # match no known extension — so a zipped .gdb yielded ZERO members,
        # produced one `archive_has_no_vector_data` warning, wrote nothing and
        # reported the run completed. FileGDB is the standard Esri delivery
        # format and by far the most common thing a consultant hands over; the
        # parser supported it the whole time and was simply never called.
        return p.is_dir() and suffix == ".gdb"

    # A .gdb's own contents must not also be returned as members.
    members = [p for p in dest.rglob("*") if _is_member(p)]
    gdb_roots = [p for p in members if p.is_dir()]
    kept = sorted(
        p for p in members
        if not any(gdb in p.parents for gdb in gdb_roots)
    )

    # Stem match is case-INSENSITIVE on purpose. GDAL on Linux is not, so a
    # delivery holding `drobeck_shumagin_veins.shp` beside
    # `Drobeck_Shumagin_Veins.prj` -- a real one does -- reads as having no
    # CRS at all. That is a resolution problem for whoever opens the file;
    # what this inventory must not do is report the .prj as absent when it
    # is sitting right there, because a missing CRS is now a refusal.
    companions: dict[str, list[str]] = {}
    for member in kept:
        if not member.is_file() or member.suffix.lower() != ".shp":
            continue
        stem = member.stem.lower()
        companions[str(member)] = sorted(
            sibling.suffix.lower()
            for sibling in member.parent.iterdir()
            if sibling.is_file()
            and sibling.stem.lower() == stem
            and sibling.suffix.lower() in _SHAPEFILE_SIDECAR_EXTENSIONS
        )

    return _ArchiveExtraction(
        members=kept,
        warnings=warnings,
        companions=companions,
    )


def _crs_epsg(source_crs: str | None) -> int | None:
    """Pull the numeric code out of an ``EPSG:26913``-style string."""
    if not source_crs:
        return None
    text = str(source_crs).strip().upper()
    if text.startswith("EPSG:"):
        try:
            return int(text.split(":", 1)[1])
        except (ValueError, IndexError):
            return None
    return None


def _renderable(parser_warnings: Any) -> list[dict[str, Any]]:
    """Give every parser warning the key the UI actually reads.

    The parsers speak ``{code, message, context}``. IngestionRuns.tsx reads
    ``detail`` and falls back to ``code``, so a warning carrying only
    ``message`` renders as the bare word "dbf_missing" -- the one warning
    whose whole point is to tell a geologist that the attribute table did
    not arrive, delivered as a token they have to look up.

    Done here rather than in each parser because every warning this
    workflow forwards has the same shape and the same problem, and because
    the parsers are shared with callers that never reach this page.
    """
    out: list[dict[str, Any]] = []
    for warning in parser_warnings or []:
        if not isinstance(warning, dict):
            out.append(warning)
            continue
        if warning.get("detail") or not warning.get("message"):
            out.append(warning)
            continue
        out.append({**warning, "detail": warning["message"]})
    return out


def _override_was_applied(result: Any, requested_epsg: int | None) -> bool:
    """Did the uploader's EPSG actually decide this result's CRS?

    Not "was one supplied". A CRS the file declares wins over one a person
    typed, so an EPSG handed to a shapefile that did carry its .prj is
    inert, and those rows must still say 'declared' -- claiming 'manual'
    for a CRS the human did not in fact choose is a fabricated provenance,
    which is the same class of lie as an invented confidence score.

    Two signals, in order. An explicit flag from the parser is
    authoritative, because the parser is the only thing that knows which
    arm of its CRS decision ran. Absent one, the observable fact is that
    the code that came back is the code that went in -- which can only
    happen if the parser applied it, or if the file happened to declare
    the very same CRS, in which case 'manual' and 'declared' describe
    identical coordinates and the distinction costs nothing.
    """
    if requested_epsg is None:
        return False
    flagged = getattr(result, "crs_override_applied", None)
    if flagged is not None:
        return bool(flagged)
    return _crs_epsg(getattr(result, "source_crs", None)) == requested_epsg


def _crs_refusal(
    parsed: list[tuple[str | None, Any]],
    *,
    filename: str,
    sidecars_by_layer: dict[str, list[str]] | None = None,
) -> str | None:
    """The reason to write nothing, or None to go ahead.

    A parse result carrying ``crs_missing`` -- or, equivalently, no
    ``source_crs`` at all -- has been past both the file's own declaration
    and any ``source_epsg`` the uploader supplied, and past the parser's
    allowlist of formats that legitimately carry no CRS (DXF, DGN, GeoJSON's
    RFC 7946 default: all of those return an explicit code). What is left is
    a file whose numbers have no frame of reference.

    Both signals are read, and neither is redundant. ``crs_missing`` is the
    parser's own verdict and says WHY; the falsy ``source_crs`` is the state
    the row would be written in, and it also covers a duck-typed result --
    every parse result in this workflow's tests is one -- that predates the
    flag.

    Storing them anyway is what this change set exists to stop, and it is
    measured, not hypothetical: a 4-point shapefile in EPSG:26904 stripped
    of its .prj came back through this pipeline as POINT (400797.89
    6117305.85) -- longitude four hundred thousand degrees -- written as
    SRID 4326 with crs_confidence 0.0, and the run reported success.

    Which layer, and which file is missing, both go in the message: it is
    rendered on the Ingestion Runs page and it is the only thing the
    uploader gets.
    """
    sidecars_by_layer = sidecars_by_layer or {}

    for layer_name, result in parsed:
        if not getattr(result, "crs_missing", False) and getattr(
            result, "source_crs", None,
        ):
            continue

        named = layer_name or getattr(result, "source_file", None) or filename
        present = sidecars_by_layer.get(layer_name or "", None)
        if present is not None and ".prj" not in present:
            missing = f"The delivery contains {named}.shp but no {named}.prj. "
        else:
            missing = ""

        # Kept short on purpose: the broadcast that carries this to the
        # page is capped at 500 characters by the Laravel endpoint, and a
        # 422 there is a dropped notification. The instruction must survive
        # a long delivery name, so it goes last but the budget is spent
        # sparingly before it.
        return (
            f"'{filename}' cannot be ingested: layer '{named}' declares no "
            f"coordinate reference system. {missing}"
            "Nothing was written -- read as WGS84 these coordinates land "
            "hundreds of thousands of degrees off the planet. Re-upload with "
            "the .prj included, or supply the EPSG code of the data "
            "(source_epsg, an integer between 1024 and 32767)."
        )

    return None


def _reported_layers(parse_result: Any, layer_override: str | None) -> list[str]:
    """The layer names a parse result will land under in the database.

    The aggregate form of the per-feature rule in ``_write_features``
    (``parsed_layer or layer_override``), and it has to stay the
    aggregate form of it: the run report and the rows it describes are
    read side by side on the Ingestion Runs page, and they disagreed.
    A zipped multi-layer ``eagle.gpkg`` wrote five real source_layer
    values and reported one layer called 'eagle'.

    ``layer_names`` is populated only for the multi-layer drivers
    (GPKG / OpenFileGDB / GML / GPX); the single-layer formats leave it
    empty, so the override still names a lone ``.shp`` after its own
    member file.
    """
    return list(parse_result.layer_names or (
        [layer_override] if layer_override else []
    ))


#: WKT for a 3D geometry names the dimension before the coordinate list --
#: "POINT Z (...)", "LINESTRING ZM (...)". Cheap to spot, and we only need to
#: know whether the layer has any.
_WKT_HAS_Z_RE = re.compile(r"^\s*[A-Z]+\s+Z", re.IGNORECASE)


def _layer_drops_z(parse_result: Any) -> bool:
    """True when this layer carries Z that the 2D geom column cannot hold.

    silver.spatial_features.geom is geometry(Geometry, 4326) with
    coord_dimension 2, so a 3D WKT makes PostGIS reject the INSERT outright --
    "Geometry has Z dimension but column does not" -- and takes every feature
    in the file down with it, not just the 3D ones. A real DXF failed exactly
    that way on 2026-08-23.

    The INSERT now wraps the geometry in ST_Force2D, which fixes the failure.
    This exists so the fix is not silent: a PointZ collar file's Z IS its
    elevation, and quietly flattening it is the same class of loss as quietly
    assuming a CRS. Checked over the features rather than a declared type
    because the parser reports geometry_type per feature.
    """
    for feat in getattr(parse_result, "features", None) or ():
        wkt = getattr(feat, "geometry_wkt", None)
        if wkt and _WKT_HAS_Z_RE.match(wkt):
            return True
    return False


async def _write_features(
    conn: asyncpg.Connection,
    *,
    workspace_id: str,
    project_id: str,
    parse_result: Any,
    source_file: str,
    source_file_sha256: str | None,
    source_label: str,
    layer_override: str | None,
    georef_method: str,
    crs_confidence: float | None,
) -> int:
    """Insert one parse result's features. Returns the row count written.

    ``source_file_sha256`` is the hash of the file named by
    ``source_file`` -- the archive itself for a zipped delivery, not the
    member. Optional so a caller that genuinely cannot hash its source
    (none today) writes NULL rather than a wrong hash.
    """
    import json  # noqa: PLC0415

    epsg = _crs_epsg(parse_result.source_crs)
    rows = []
    for feat in parse_result.features:
        props = dict(feat.properties or {})
        # _layer_name is the parser's bookkeeping column, not upstream data.
        # It becomes source_layer, so leaving it in properties duplicates it
        # into every row's jsonb for no benefit.
        #
        # Popped UNCONDITIONALLY. Writing this as
        # `layer_override or props.pop(...)` short-circuits whenever an
        # override is supplied — which is exactly the QGIS-project path — so
        # the column stayed in the jsonb for every row that had a named layer.
        parsed_layer = props.pop("_layer_name", None)
        # The parser's own per-feature layer wins when it has one. The
        # override used to win unconditionally, and the archive branch passes
        # `member.stem` as the override — so a QField `eagle.gpkg` inside a
        # ZIP, holding collars / outcrops / structures / samples / traverses,
        # had all five layers read and written correctly and then every row
        # stamped `source_layer = 'eagle'`. Layer identity survived a direct
        # upload of the same file (override None) and was lost only on the
        # zipped path, so "show me only structural measurements" could not
        # tell the five apart and a corrected single layer could not be
        # re-uploaded without replacing all of them.
        #
        # The override still applies where it is the only name available:
        # a lone .shp or .geojson carries no per-feature layer.
        layer_name = parsed_layer or layer_override
        rows.append((
            workspace_id,
            project_id,
            feat.feature_type,
            feat.name,
            source_label,
            source_file,
            parse_result.source_crs,
            layer_name,
            # `x and str(x) or None` turns feature id 0 into NULL, and 0 is
            # a perfectly ordinary first fid in a shapefile. Only a genuinely
            # absent fid should be NULL.
            (str(props["fid"]) if props.get("fid") is not None else None),
            json.dumps(props, default=str),
            epsg,
            crs_confidence,
            georef_method,
            feat.geometry_wkt,
            source_file_sha256,
        ))

    written = 0
    for start in range(0, len(rows), _INSERT_BATCH):
        chunk = rows[start:start + _INSERT_BATCH]
        await conn.executemany(_INSERT_SQL, chunk)
        written += len(chunk)
    return written


ingest_spatial = hatchet.workflow(
    name="ingest_spatial",
    input_validator=IngestSpatialInput,
)


@ingest_spatial.task(execution_timeout="2h", retries=1)
async def run_ingest_spatial(
    input: IngestSpatialInput, ctx: Context,
) -> IngestSpatialOut:
    """Download, parse and persist one vector file or QGIS project."""
    from georag_geoparsers.qgis_parser import parse_qgis_project  # noqa: PLC0415
    from georag_geoparsers.spatial_parser import parse_spatial_file  # noqa: PLC0415

    t0 = _t.monotonic()
    store = get_storage_client()
    filename = input.minio_key.rsplit("/", 1)[-1]
    suffix = Path(filename).suffix.lower()
    # The identity the replace-on-re-upload below keys on. `filename` is the
    # storage basename and carries the timestamp the upload controllers
    # prepend (`20260902_143012_geology_poly.zip`; the ZIP fan-out adds a
    # microsecond component), so two uploads of the same shapefile never
    # shared it — the delete matched nothing and the map drew every polygon
    # twice (RedStar batch, 2026-09-02). _filename_from_key strips exactly
    # those prefixes and nothing else: the name the geologist typed.
    source_file = _progress._filename_from_key(input.minio_key)

    if suffix not in SUPPORTED_EXTENSIONS:
        # A routing bug, not user error: the upload controller decides which
        # workflow a file goes to. Failing loudly beats writing zero features
        # and reporting success.
        raise ValueError(
            f"ingest_spatial cannot handle {suffix!r} ({filename}); "
            f"supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

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

    warnings: list[dict[str, Any]] = []
    project_layers: list[dict[str, Any]] = []
    manifest_only = False
    layers_written: list[str] = []
    features_written = 0
    empty_skipped = 0
    source_format = suffix.lstrip(".")
    #: Where the run got to, for the failure path. mark_failed_by_run
    #: COALESCEs a None onto whatever mark_stage_started last wrote, but the
    #: broadcast needs the name in hand, and "failed at persist" is a
    #: different conversation from "failed at parse".
    current_stage = "preflight"
    #: Which shapefile sidecars each archive member arrived with. Empty on
    #: every non-archive path, which is why _crs_refusal treats "no entry"
    #: and "an entry without a .prj" as different things.
    sidecars_by_layer: dict[str, list[str]] = {}

    # ── The CRS override this run will parse under ──────────────────────
    # One resolution, up front, so every parse site below and the
    # _crs_quality call at persist all see the same answer. A typed
    # source_epsg outranks the donated WKT: the code the user typed is a
    # decision, the copy the wizard found is a guess with provenance. The
    # parser's own precedence still applies on top -- a CRS the file itself
    # declares beats both.
    #
    # A SUCCESSFUL resolution is deliberately not a warning. The same
    # donation carried as a `.prj` member into a shapefile bundle produces
    # no workflow warning at all -- the provenance lands in
    # georef_method/crs_confidence on the rows -- and terminal_status turns
    # any warning into 'partial', so warning here would paint every clean
    # WKT-donated ingest amber while the identical member-carriage ingest
    # completes green. It also must not claim anything about the file
    # ("declares no CRS", "was applied"): nothing has been parsed yet, and
    # the parser may yet ignore the override for a file that declares its
    # own. A FAILED resolution is a warning -- the file will land as
    # 'assumed' and only the uploader can supply the missing code.
    effective_epsg = input.source_epsg
    epsg_via_wkt = False
    if effective_epsg is None and input.source_crs_wkt:
        import asyncio  # noqa: PLC0415

        # to_thread, hard rule 2: pyproj's from_wkt/to_epsg do synchronous
        # sqlite reads of proj.db, measured at ~75-200 ms.
        resolved, crs_name = await asyncio.to_thread(
            _epsg_from_wkt, input.source_crs_wkt,
        )
        if resolved is not None:
            effective_epsg = resolved
            epsg_via_wkt = True
            log.info(
                "ingest_spatial: donated WKT for %s resolved to EPSG:%s (%s)",
                filename, resolved, crs_name,
            )
        else:
            warnings.append({
                "code": "donated_wkt_unresolved",
                "detail": (
                    f"A coordinate system was found beside {filename} in "
                    f"the upload"
                    + (f" ({crs_name})" if crs_name else "")
                    + ", but it could not be resolved to an EPSG code, so "
                    "it was not applied. Type the EPSG code on this file "
                    "and re-upload to place its features properly."
                ),
            })

    # A lone ".shp" used to be refused right here, before the download. The
    # reason was real: pyogrio opened it, went looking for the ".shx" index
    # beside it, and failed with "Unable to open <name>.shx ... Set
    # SHAPE_RESTORE_SHX" -- a message about a GDAL config option, handed to
    # a geologist.
    #
    # That option is now set once at spatial_parser import, and it was
    # MEASURED to settle the case completely: a bare ".shp" with no sidecars
    # at all opens and yields every feature, because GDAL regenerates the
    # index from the ".shp" itself. Refusing the file now refuses data this
    # pipeline can read -- and refusing it HERE never helped the case that
    # actually occurs, since both upload wizards zip a bare ".shp" before
    # sending it and ingest_zip_archive re-zips one itself, so the real
    # shape of this problem is a lone ".shp" INSIDE an archive, which this
    # branch never saw.
    #
    # What no config option can recover is the ".prj". Absent it GDAL
    # reports crs=None and nothing can reconstruct it. So the refusal moved
    # rather than disappeared: from "this file is unreadable", which is no
    # longer true, to "this file has no coordinate reference system", which
    # is the actual corruption. That check sits at the persist gate below,
    # covers every path including the in-archive one, and is answered by
    # supplying source_epsg.

    try:
        if run_id:
            await _progress.mark_stage_started(run_id=run_id, stage="preflight")

        with tempfile.TemporaryDirectory(prefix="georag_spatial_") as tmpdir:
            local = Path(tmpdir) / filename
            import asyncio  # noqa: PLC0415

            await asyncio.to_thread(
                store.get_file, Bucket.BRONZE, input.minio_key, str(local),
            )

            current_stage = "parse"
            if run_id:
                await _progress.mark_stage_started(run_id=run_id, stage="parse")

            # ── QGIS project ────────────────────────────────────────────
            if suffix in QGIS_PROJECT_EXTENSIONS:
                # Hard rule 2 — sync GDAL work off the event loop.
                project = await asyncio.to_thread(parse_qgis_project, str(local))
                source_format = project.source_format
                warnings.extend(project.warnings)
                manifest_only = project.is_manifest_only
                project_layers = [
                    {
                        "name": lyr.name,
                        "provider": lyr.provider,
                        "crs": lyr.crs,
                        "geometry_type": lyr.geometry_type,
                        "resolved": lyr.resolved,
                        "sublayer": lyr.sublayer,
                    }
                    for lyr in project.layers
                ]

                parsed: list[tuple[str, Any]] = []
                for lyr in project.layers:
                    if not lyr.resolved or not lyr.resolved_path:
                        continue
                    try:
                        parsed.append((
                            lyr.name,
                            await asyncio.to_thread(
                                parse_spatial_file,
                                lyr.resolved_path,
                                feature_type=input.feature_type,
                                layer=lyr.sublayer,
                                source_epsg=effective_epsg,
                            ),
                        ))
                    except Exception as exc:  # noqa: BLE001 — one layer must not sink the project
                        warnings.append({
                            "code": "layer_parse_failed",
                            "layer": lyr.name,
                            "detail": str(exc)[:300],
                        })
            elif suffix in ARCHIVE_EXTENSIONS:
                # A zipped shapefile — the normal delivery shape. Every
                # readable member is parsed and tagged with its own file name
                # as source_layer, so a bundle holding faults.shp and
                # claims.shp stays two distinguishable layers.
                extraction = await asyncio.to_thread(
                    _extract_archive, local, Path(tmpdir) / "_unzipped",
                )
                members = extraction.members
                # Anything the extractor declined to write is the run's
                # business, not just the log's.
                warnings.extend(extraction.warnings)
                # Keyed by the name each member is written under
                # (member.stem is the layer_override a few lines down), so
                # the CRS gate can name the file that is missing rather
                # than the concept that is missing.
                sidecars_by_layer = {
                    Path(member_path).stem: present
                    for member_path, present in extraction.companions.items()
                }
                if not members:
                    warnings.append({
                        "code": "archive_has_no_vector_data",
                        "detail": (
                            "The archive contains no readable vector or QGIS "
                            "file. A zipped shapefile must include the .shp "
                            "itself, not only its .dbf/.shx sidecars; a zipped "
                            "MapInfo table must include the .tab or .mif, not "
                            "only its .dat/.map/.id."
                        ),
                    })
                parsed = []
                for index, member in enumerate(members):
                    # The only heartbeat between 'parse' and 'persist'.
                    # A multi-layer delivery spends minutes in this loop --
                    # longer now that GDAL may be rebuilding a .shx for a
                    # member that arrived without one -- and the stale sweep
                    # times a run out after fifteen silent minutes, which
                    # relabels a healthy run as dead and hands the geologist
                    # a failure for a file that is still ingesting.
                    if run_id:
                        await _progress.mark_stage_progress(
                            run_id=run_id,
                            stage_pct=index / len(members),
                            stage_detail=(
                                f"parsing {member.name} "
                                f"({index + 1}/{len(members)})"
                            ),
                        )
                    try:
                        if member.suffix.lower() in QGIS_PROJECT_EXTENSIONS:
                            # A project inside a zip: catalogue it, and read
                            # whatever layers the same archive resolved.
                            proj = await asyncio.to_thread(
                                parse_qgis_project, str(member),
                            )
                            project_layers.extend(
                                {
                                    "name": lyr.name, "provider": lyr.provider,
                                    "crs": lyr.crs, "resolved": lyr.resolved,
                                    "sublayer": lyr.sublayer,
                                }
                                for lyr in proj.layers
                            )
                            warnings.extend(proj.warnings)
                            for lyr in proj.layers:
                                if lyr.resolved and lyr.resolved_path:
                                    parsed.append((
                                        lyr.name,
                                        await asyncio.to_thread(
                                            parse_spatial_file,
                                            lyr.resolved_path,
                                            feature_type=input.feature_type,
                                            layer=lyr.sublayer,
                                            source_epsg=effective_epsg,
                                        ),
                                    ))
                            continue

                        parsed.append((
                            member.stem,
                            await asyncio.to_thread(
                                parse_spatial_file,
                                str(member),
                                feature_type=input.feature_type,
                                source_epsg=effective_epsg,
                            ),
                        ))
                    except Exception as exc:  # noqa: BLE001 — one bad member must not sink the archive
                        warnings.append({
                            "code": "archive_member_failed",
                            "member": member.name,
                            "detail": str(exc)[:300],
                        })
            else:
                parsed = [(None, await asyncio.to_thread(
                    parse_spatial_file,
                    str(local),
                    feature_type=input.feature_type,
                    source_epsg=effective_epsg,
                ))]

            # One hash per delivery, computed after every parse and before
            # the writes, so a parse failure costs nothing. Reused across
            # every layer in the file -- hashing inside the per-layer loop
            # would re-read a 2 GiB archive once per layer.
            source_sha256 = await asyncio.to_thread(_sha256_file, local)

            current_stage = "persist"
            if run_id:
                await _progress.mark_stage_started(run_id=run_id, stage="persist")

            conn = await asyncpg.connect(_build_dsn())
            try:
                # Bind the workspace GUC so the table's RLS policy applies to
                # these inserts rather than being bypassed by the owner role.
                #
                # bind_workspace_scope, NOT a bespoke set_config: the helper
                # validates the UUID shape (a malformed value would otherwise
                # bind as an opaque GUC and silently match nothing) and is the
                # single place the is_local semantics are decided. A bespoke
                # call here is exactly what test_scoped_connection's
                # monotonically-shrinking allowlist exists to prevent.
                #
                # is_local=False because the GUC has to outlive the explicit
                # transaction opened below as well as any autocommit
                # statement outside it.
                await bind_workspace_scope(
                    conn,
                    workspace_id=input.workspace_id,
                    site="hatchet.ingest_spatial",
                    is_local=False,
                )

                # Replace, don't accumulate. silver.spatial_features has no
                # unique constraint and _INSERT_SQL inserts a
                # gen_random_uuid() with no ON CONFLICT, so every re-run
                # added a second copy of every feature. Two ways in: this
                # task carries retries=1, so a connection drop after batch 60
                # of 80 left 30,000 rows committed and then re-inserted all
                # 40,000; and a geologist re-uploading a corrected shapefile
                # — which ingest_tabular's own comment block calls out as
                # normal behaviour — did the same thing deliberately. The map
                # then drew every polygon twice and every count-by-layer was
                # wrong, with nothing in the schema able to tell the copies
                # apart.
                #
                # Scoped to (project_id, source_file) and wrapped with the
                # inserts in one transaction, so the delete only lands if the
                # re-insert does. source_file is the prefix-stripped name;
                # rows written before 2026-09-03 stored the timestamped
                # storage basename instead, so the predicate strips that
                # prefix on the way through rather than comparing raw
                # strings — one re-upload also collapses the old copies.
                #
                # This used to say it mirrored ingest_well_logs, which until
                # 2026-08-22 deleted EVERY curve on the hole regardless of
                # which file wrote it — so the one workflow doing it wrong
                # read as the convention. Each of the three scopes its
                # replace by whatever identifies "the rows this file owns":
                # source_file here, the collars mentioned in ingest_tabular,
                # the curve names in ingest_well_logs.
                replaced = 0
                async with conn.transaction():
                    # The CRS gate. Inside the transaction on purpose: the
                    # delete-then-reinsert below is what makes a re-upload
                    # idempotent, and a refusal must not become the thing
                    # that destroys the previous, good ingest of the same
                    # file.
                    #
                    # 'failed', not 'partial'. Laravel's DATA_LANDED_STATUSES
                    # is exactly ['completed','partial'], so a partial with
                    # zero rows still bumps data_version and fires the MV
                    # refresh -- the status alone cannot keep bad rows off
                    # the map. The only thing that can is not writing them.
                    refusal = _crs_refusal(
                        parsed,
                        filename=source_file,
                        sidecars_by_layer=sidecars_by_layer,
                    )
                    if refusal:
                        raise ValueError(refusal)

                    replaced = int(await conn.fetchval(
                        _REPLACE_SQL,
                        input.project_id, source_file, _LEGACY_SOURCE_FILE_PREFIX,
                    ) or 0)
                    if replaced:
                        log.info(
                            "ingest_spatial: replacing %d existing feature(s) for "
                            "%s in project=%s",
                            replaced, source_file, input.project_id,
                        )
                        warnings.append({
                            "code": "features_replaced",
                            "detail": (
                                f"{replaced} feature(s) from a previous ingest of "
                                f"{source_file} were replaced."
                            ),
                        })

                    for layer_name, result in parsed:
                        empty_skipped += result.empty_geom_skipped
                        # _renderable, not the raw list: a parser warning
                        # carries `message`, and the Ingestion Runs page
                        # reads `detail`. This is what carries 'dbf_missing'
                        # -- the attribute table did not arrive with the
                        # geometry -- to a geologist as a sentence rather
                        # than as a token they have to look up.
                        warnings.extend(_renderable(result.warnings))
                        if _layer_drops_z(result):
                            warnings.append({
                                "code": "z_dropped",
                                "detail": (
                                    f"'{layer_name or source_file}' carries 3D "
                                    "coordinates. The map stores 2D geometry, "
                                    "so the Z values were dropped. If the "
                                    "elevations matter, export them from the "
                                    "source software as an attribute column, "
                                    "or as a point layer with an elevation "
                                    "field — vertex elevations on lines and "
                                    "polygons cannot be kept here."
                                ),
                            })
                        crs_conf, georef = _crs_quality(
                            result, requested_epsg=effective_epsg,
                            # A code the user typed is a human assertion
                            # ('manual'); one resolved from a donated .prj
                            # the wizard found is the platform working it
                            # out ('detected'). Stamping the latter 'manual'
                            # would fabricate a human claim -- see
                            # _crs_quality's docstring.
                            override_method=(
                                "detected" if epsg_via_wkt else "manual"
                            ),
                        )
                        n = await _write_features(
                            conn,
                            workspace_id=input.workspace_id,
                            project_id=input.project_id,
                            parse_result=result,
                            source_file=source_file,
                            source_file_sha256=source_sha256,
                            source_label=source_format,
                            layer_override=layer_name,
                            georef_method=georef,
                            crs_confidence=crs_conf,
                        )
                        features_written += n
                        layers_written.extend(
                            _reported_layers(result, layer_name)
                        )
            finally:
                await conn.close()

        # Report what actually landed, not just that the workflow ran to
        # the end. mark_completed_by_run downgrades to 'partial' when the
        # row count is zero or warnings are attached, and persists the
        # warnings so their text reaches the Ingestion Runs page instead of
        # dying inside the Hatchet run object.
        if run_id:
            transitioned = await _progress.mark_completed_by_run(
                run_id=run_id,
                rows_written=features_written,
                warnings=warnings,
            )
            if transitioned:
                # See _progress.broadcast_terminal. The spatial path is the
                # one where silence is most obvious: the features go
                # straight into the Silver MVT layers the map reads, and
                # without the data_version bump this triggers, MapLibre
                # keeps serving the cached tiles from before the upload.
                await _progress.broadcast_terminal(
                    workspace_id=input.workspace_id,
                    project_id=input.project_id,
                    run_id=run_id,
                    stage="persist",
                    status=_progress.terminal_status(
                        rows_written=features_written, warnings=warnings,
                    ),
                    message=_progress.terminal_message(
                        rows_written=features_written,
                        warnings=warnings,
                        noun="feature",
                    ),
                )

    except Exception as exc:
        if run_id:
            # kwarg is `error`, not `error_text` -- passing the wrong name
            # raised TypeError *inside* the handler, so the real failure
            # was replaced by the TypeError and the progress row never
            # reached a terminal state.
            transitioned = await _progress.mark_failed_by_run(
                run_id=run_id, stage=current_stage, error=str(exc)[:1000],
            )
            if transitioned:
                # No ingest workflow broadcast on its failure path before
                # this one. The row went to 'failed' in Postgres and the
                # page showing it was never told, so a refused upload sat
                # on screen as "running" until somebody reloaded -- and a
                # refusal nobody sees is not much better than the silent
                # corruption it replaced.
                #
                # 'failed' is accepted by the Laravel validator and is
                # correctly OUTSIDE DATA_LANDED_STATUSES, so this notifies
                # without bumping data_version or refreshing the
                # materialised views. That is exactly right for a run that
                # deliberately wrote nothing.
                await _progress.broadcast_terminal(
                    workspace_id=input.workspace_id,
                    project_id=input.project_id,
                    run_id=run_id,
                    stage=current_stage,
                    status="failed",
                    message=str(exc)[:500],
                )
        log.exception("ingest_spatial failed for %s", input.minio_key)
        raise

    out = IngestSpatialOut(
        run_id=run_id,
        source_format=source_format,
        features_written=features_written,
        layers=sorted({lyr for lyr in layers_written if lyr}),
        manifest_only=manifest_only,
        project_layers=project_layers,
        empty_geom_skipped=empty_skipped,
        warnings=warnings[:20],
        duration_ms=int((_t.monotonic() - t0) * 1000),
    )
    log.info("ingest_spatial complete: %s", out.model_dump(exclude={"project_layers"}))
    return out


def _crs_quality(
    result: Any, *, requested_epsg: int | None = None,
    override_method: str = "manual",
) -> tuple[float | None, str]:
    """Map the parser's CRS finding onto the CC-01 georef columns.

    ``georef_method`` is CHECK-constrained to
    declared / detected / assumed / manual / survey, and the distinction is
    not cosmetic: it drives the map's positional-uncertainty ring. A CRS
    assumed to be WGS84 that is really UTM puts features a continent away,
    and 'assumed' is the only honest way to say the location may be wrong.

    QField captures come from a GNSS receiver in someone's hand, which is a
    genuine survey fix — the parser detects those separately and they get
    the fixed 0.9 confidence the pipeline has always assigned them.

    ``requested_epsg`` is the code the uploader supplied, and it is a
    parameter rather than something read back off the result for one
    reason: this function must never invent 'manual'. 'manual' means a
    person asserted the CRS, so a heuristic producing it out of the
    parser's own findings would be fabricating a human claim. Taking the
    request as an argument is what keeps that branch unreachable unless a
    human really made one.

    ``override_method`` exists for the same reason, from the other side:
    an override RESOLVED from a donated `.prj` the wizard found in the
    drop is not a human assertion either, and stamping it 'manual' would
    fabricate one. The caller passes 'detected' for that case — the
    constraint's word for a CRS the platform worked out rather than one a
    person typed or the file declared.
    """
    if _override_was_applied(result, requested_epsg):
        # The CRS was supplied for a file that stated none. 'manual' when
        # a person typed the code; 'detected' when it was resolved from a
        # sidecar found beside the file.
        #
        # The confidence is the parser's MEASURED score of the geometry
        # against the CRS that was claimed -- not a flat 1.0. The claim is
        # checkable (do these coordinates fall inside that CRS's extent?)
        # and checking beats trusting: someone picking the wrong UTM zone
        # out of a dropdown is making the same mistake this gate exists to
        # catch, by hand.
        confidence = getattr(result, "crs_confidence", None)
        return (
            (float(confidence) if confidence is not None else None),
            override_method,
        )

    if getattr(result, "is_qfield", False):
        return 0.9, "survey"

    confidence = getattr(result, "crs_confidence", None)
    if confidence is None:
        return None, "assumed"

    confidence = float(confidence)
    if confidence >= 0.85:
        # The file carried an explicit, self-consistent CRS.
        return confidence, "declared"
    if confidence >= 0.5:
        # Inferred from coordinate ranges rather than stated outright.
        return confidence, "detected"
    return confidence, "assumed"




# ---------------------------------------------------------------------------
# Failure hook (2026-08-21). Mirrors ingest_zip_archive.on_failure.
# ---------------------------------------------------------------------------
@ingest_spatial.on_failure_task(
    name="on_failure",
    execution_timeout="30s",
    schedule_timeout="30m",
    retries=2,
)
async def on_failure(input: IngestSpatialInput, ctx: Context) -> dict[str, Any]:
    """Close the ingest_progress row when the workflow dies.

    Without this hook a Hatchet cancellation — concurrency-queue
    expiry, a manual cancel, a worker SIGTERM — left the row created
    by start_run sitting at 'queued' with nothing to close it, because
    the body that would have closed it never ran. The 15-minute stale
    sweep was the only backstop.
    """
    return await _progress.close_run_after_workflow_failure(
        workflow_name="ingest_spatial",
        workspace_id=str(input.workspace_id) if input.workspace_id else None,
        project_id=str(input.project_id) if input.project_id else None,
        minio_key=input.minio_key,
        run_id=input.run_id,
        ctx=ctx,
    )


__all__ = [
    "QGIS_PROJECT_EXTENSIONS",
    "SUPPORTED_EXTENSIONS",
    "VECTOR_EXTENSIONS",
    "IngestSpatialInput",
    "IngestSpatialOut",
    "ingest_spatial",
]
