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

    @field_validator("workspace_id", "project_id")
    @classmethod
    def _must_be_uuid(cls, v: str) -> str:
        import uuid  # noqa: PLC0415

        uuid.UUID(v)  # raises on malformed input, at the trigger boundary
        return v


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
    ST_SetSRID(ST_GeomFromText($14::text), 4326)
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
    return _ArchiveExtraction(
        members=sorted(
            p for p in members
            if not any(gdb in p.parents for gdb in gdb_roots)
        ),
        warnings=warnings,
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

    # A lone ".shp" cannot be read: pyogrio opens it and then goes looking for
    # the ".shx" index and ".dbf" table beside it, which a single-object upload
    # never has. GDAL's own message for this ("Unable to open <name>.shx ... Set
    # SHAPE_RESTORE_SHX") tells a user nothing they can act on, and until this
    # check it was the only thing they got. Refuse before the download with the
    # instruction that actually resolves it.
    if suffix == ".shp":
        detail = (
            f"'{filename}' was uploaded on its own. A shapefile is not one "
            "file - it needs its .shx, .dbf and .prj siblings, which cannot be "
            "uploaded separately. Zip the .shp together with them and upload "
            "the .zip."
        )
        if run_id:
            await _progress.mark_failed_by_run(
                run_id=run_id, stage="preflight", error=detail,
            )
        raise ValueError(detail)

    try:
        if run_id:
            await _progress.mark_stage_started(run_id=run_id, stage="preflight")

        with tempfile.TemporaryDirectory(prefix="georag_spatial_") as tmpdir:
            local = Path(tmpdir) / filename
            import asyncio  # noqa: PLC0415

            await asyncio.to_thread(
                store.get_file, Bucket.BRONZE, input.minio_key, str(local),
            )

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
                if not members:
                    warnings.append({
                        "code": "archive_has_no_vector_data",
                        "detail": (
                            "The archive contains no readable vector or QGIS "
                            "file. A zipped shapefile must include the .shp "
                            "itself, not only its .dbf/.shx sidecars."
                        ),
                    })
                parsed = []
                for member in members:
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
                                        ),
                                    ))
                            continue

                        parsed.append((
                            member.stem,
                            await asyncio.to_thread(
                                parse_spatial_file,
                                str(member), feature_type=input.feature_type,
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
                    str(local), feature_type=input.feature_type,
                ))]

            # One hash per delivery, computed after every parse and before
            # the writes, so a parse failure costs nothing. Reused across
            # every layer in the file -- hashing inside the per-layer loop
            # would re-read a 2 GiB archive once per layer.
            source_sha256 = await asyncio.to_thread(_sha256_file, local)

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
                # re-insert does.
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
                    replaced = int(await conn.fetchval(
                        "WITH gone AS ("
                        "  DELETE FROM silver.spatial_features"
                        "   WHERE project_id = $1::uuid AND source_file = $2"
                        "  RETURNING 1"
                        ") SELECT count(*) FROM gone",
                        input.project_id, filename,
                    ) or 0)
                    if replaced:
                        log.info(
                            "ingest_spatial: replacing %d existing feature(s) for "
                            "%s in project=%s",
                            replaced, filename, input.project_id,
                        )
                        warnings.append({
                            "code": "features_replaced",
                            "detail": (
                                f"{replaced} feature(s) from a previous ingest of "
                                f"{filename} were replaced."
                            ),
                        })

                    for layer_name, result in parsed:
                        empty_skipped += result.empty_geom_skipped
                        warnings.extend(result.warnings or [])
                        crs_conf, georef = _crs_quality(result)
                        n = await _write_features(
                            conn,
                            workspace_id=input.workspace_id,
                            project_id=input.project_id,
                            parse_result=result,
                            source_file=filename,
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
            # kwarg is `error`, not `error_text` � passing the wrong name
            # raised TypeError *inside* the handler, so the real failure
            # was replaced by the TypeError and the progress row never
            # reached a terminal state.
            await _progress.mark_failed_by_run(
                run_id=run_id, error=str(exc)[:1000],
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


def _crs_quality(result: Any) -> tuple[float | None, str]:
    """Map the parser's CRS finding onto the CC-01 georef columns.

    ``georef_method`` is CHECK-constrained to
    declared / detected / assumed / manual / survey, and the distinction is
    not cosmetic: it drives the map's positional-uncertainty ring. A CRS
    assumed to be WGS84 that is really UTM puts features a continent away,
    and 'assumed' is the only honest way to say the location may be wrong.

    QField captures come from a GNSS receiver in someone's hand, which is a
    genuine survey fix — the parser detects those separately and they get
    the fixed 0.9 confidence the pipeline has always assigned them.
    """
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
