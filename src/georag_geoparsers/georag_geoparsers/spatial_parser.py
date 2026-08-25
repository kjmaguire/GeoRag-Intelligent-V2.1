"""Spatial vector feature parser — Shapefile, GeoJSON, GPKG, DXF, GDB, and more.

Reads vector formats supported by pyogrio/GeoPandas (ESRI Shapefile, GeoJSON,
GeoPackage, GML, DXF, DGN, OpenFileGDB, GPX, FlatGeobuf) and returns
a structured SpatialParseResult with one SpatialFeature per non-empty geometry row.

KML/KMZ support is deferred (V1-roadmap, not implemented) per spec §04d.
Kyle-approved removal 2026-04-20 (Module 3 Phase B Decision B).

Format-specific behaviour:
  - Shapefile: repairs mis-cased sidecars, then checks for the .prj and .dbf
    sidecars; emits prj_missing / dbf_missing if absent.  A missing .shx is
    rebuilt by GDAL (SHAPE_RESTORE_SHX, set once at import).
  - GeoJSON/GeoJSONSeq: EPSG:4326 by default per RFC 7946.
  - MapInfo: .tab and .mif are the only entry points; .dat/.map/.id/.ind and
    .mid are sidecars.  A .mif delivered without its .mid reads with every
    attribute empty, so it is flagged (mid_missing).
  - GeoPackage (GPKG): multi-layer; all layers are read, features tagged with
    _layer_name attribute.
  - DXF: no CRS; emits dxf_no_crs; appends "dxf_blocks" to deferred_capabilities.
  - FileGDB (.gdb directory): read-only via pyogrio's OpenFileGDB driver.  Emits
    filegdb_metadata_deferred warning; appends domain/subtype/relationship-class
    extraction to deferred_capabilities (requires GDAL Python bindings, Sprint 4b).
  - FlatGeobuf (.fgb), GPX, GML, DGN: read via pyogrio driver inference.

CRS handling:
  - Source CRS is captured before any transformation.
  - A CRS the FILE declares always wins.  The ``source_epsg`` argument is a
    human's claim about a file that says nothing; it is applied ONLY when the
    file declares no CRS, and the confidence recorded against it is the
    measured fit of the geometry to that CRS, not a constant.
  - A file that declares no CRS, was given no override, and is not one of the
    formats that legitimately carry none (see ``_NO_CRS_EXTENSIONS``) is
    REFUSED: ``crs_missing`` is set and the caller must not persist the
    features.  Assuming EPSG:4326 for a projected shapefile is what put
    RedStar's Unga Island veins at longitude 400,797 degrees.
  - If the source is not WGS84 (EPSG:4326), the GeoDataFrame is reprojected
    to EPSG:4326 before WKT is extracted.  This matches Section 04b step 4
    (transform to project CRS) — for spatial features the target is geographic
    4326 so they slot into silver.spatial_features.geom GEOMETRY(Geometry,4326).

Null/empty geometries are logged and skipped; they are never silently dropped
from the count.

FileGDB provenance: since a .gdb is a directory, the SHA-256 is computed over the
concatenation of '{filename}:{size_bytes}' for every file inside the directory
(sorted for determinism), rather than over the raw bytes of a single file.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.
"""

import hashlib
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PARSER_VERSION = "2.4.0"

# QField (mobile companion to QGIS) writes GeoPackages with a recognisable
# attribute schema. Detection is per-layer: a layer is considered a QField
# layer when it carries (a) a GPS accuracy column AND (b) at least one
# corroborating field-collection column (timestamp / device / photo).
# Names are lower-cased before comparison.
_QFIELD_ACCURACY_COLS = frozenset({
    "accuracy", "horizontal_accuracy", "gnss_accuracy", "gps_accuracy",
})
_QFIELD_CORROBORATING_COLS = frozenset({
    "timestamp", "captured_at", "device_id", "device", "photo",
    "picture", "image",
})
# QField/QGIS-authored GeoPackages carry one or more of these SQLite tables
# in addition to the user data layers. Presence elevates QField confidence.
_QFIELD_GPKG_METADATA_TABLES = frozenset({
    "qgis_relations",
    "qgis_layer_styles",
    "qgis_layer_metadata",
    "gpkg_data_columns",
    "gpkg_metadata",
    "gpkg_metadata_reference",
})


# ---------------------------------------------------------------------------
# Format detection constants
# ---------------------------------------------------------------------------

_VECTOR_EXTENSIONS: dict[str, str] = {
    ".shp":     "ESRI Shapefile",
    ".geojson": "GeoJSON",
    ".json":    "GeoJSON",
    ".gpkg":    "GPKG",
    # .kml / .kmz removed — KML/KMZ is V1-roadmap deferred per spec §04d.
    # Kyle-approved 2026-04-20 (Module 3 Phase B Decision B).
    ".gml":     "GML",
    ".gpx":     "GPX",
    ".dxf":     "DXF",
    ".dgn":     "DGN",
    ".gdb":     "OpenFileGDB",   # directory — read-only via pyogrio
    ".fgb":     "FlatGeobuf",
    # MapInfo. .tab (a NATIVE table) and .mif (the interchange format) are
    # the only ENTRY POINTS. .dat/.map/.id/.ind belong to a .tab and .mid
    # belongs to a .mif — and opening a .mid directly SUCCEEDS, so listing
    # any of them here would ingest a MIF/MID pair twice. The "MapInfo File"
    # driver is present in the deployed image (verified 2026-08-23).
    ".tab":     "MapInfo File",
    ".mif":     "MapInfo File",
}

# Formats that support (or may contain) multiple OGR layers.
# KML removed — V1-roadmap deferred per spec §04d (Module 3 Phase B Decision B).
_MULTI_LAYER_DRIVERS = frozenset({"GPKG", "OpenFileGDB", "GML", "GPX"})

#: Extensions that legitimately carry no CRS of their own. For these — and
#: only these — an absent CRS is not a defect, and EPSG:4326 is the honest
#: reading rather than a guess:
#:
#:   .dxf / .dgn       AutoCAD and MicroStation have no CRS concept at all.
#:                     The drawing is in model units and the caller must
#:                     georeference it; both get a *_no_crs warning and a
#:                     confidence of 0.0, which _crs_quality reads as
#:                     'assumed' so the map draws the uncertainty ring.
#:   .geojson / .json  RFC 7946 §4 fixes GeoJSON to WGS84 lon/lat. GDAL's
#:                     GeoJSON driver already reports EPSG:4326 for a file
#:                     with no `crs` member (measured), so this arm is a
#:                     backstop — but it MUST exist, because there is no
#:                     other GeoJSON-specific CRS code in this module and
#:                     without it every RFC 7946 file would be refused.
#:
#: Anything else arriving without a CRS and without a source_epsg override
#: is refused outright (see _resolve_crs).
#:
#: This replaces _NO_CRS_DRIVERS, which named the DXF *driver*, was
#: referenced by nothing, and therefore exempted nothing — every DXF
#: exemption in this file has always been the literal `ext == ".dxf"`.
_NO_CRS_EXTENSIONS: frozenset[str] = frozenset({".dxf", ".dgn", ".geojson", ".json"})

#: The CRS assigned to a format on _NO_CRS_EXTENSIONS.
_NO_CRS_DEFAULT = "EPSG:4326"

#: silver.spatial_features.crs_epsg_native is CHECK-constrained to this range
#: (chk_spatial_features_crs_native) and the HTTP edge applies the identical
#: rule. An override outside it would fail the INSERT for every feature in
#: the file, so it is refused here, before anything is read.
_MIN_EPSG = 1024
_MAX_EPSG = 32767

# Deferred capabilities signalled at parse time for Sprint 4b work.
_DEFERRED_DXF = ["dxf_blocks"]
_DEFERRED_FILEGDB = [
    "filegdb_domains",
    "filegdb_subtypes",
    "filegdb_relationship_classes",
]

#: Sidecar files belonging to a multi-file format, keyed by the extension of
#: the file the parser is handed. Used for case repair and completeness
#: reporting only — never as entry points.
_SIDECAR_EXTENSIONS: dict[str, tuple[str, ...]] = {
    ".shp": (".shx", ".dbf", ".prj", ".cpg"),
    ".tab": (".dat", ".map", ".id", ".ind"),
    ".mif": (".mid",),
}


# ---------------------------------------------------------------------------
# Process-global GDAL configuration
# ---------------------------------------------------------------------------

def _configure_gdal_once() -> None:
    """Apply the process-global GDAL options this parser depends on.

    SHAPE_RESTORE_SHX=YES makes GDAL rebuild a missing .shx from the .shp
    itself. Measured on the deployed image: without it, a shapefile delivered
    without its index raises ``DataSourceError: Unable to open …shx``; with
    it, the same file yields every one of its features. RedStar's
    drobeck_shumagin_veins.shp is exactly that file.

    It is set ONCE, at module import, and deliberately NOT scoped with a
    try/finally around each read:

      * ``pyogrio.set_gdal_config_options`` is process-global AND
        thread-leaking. Measured: a value set on a worker thread is visible
        on the main thread, and threads started afterwards observe it too.
        pyogrio exposes no thread-local setter and no per-read equivalent —
        ``read_dataframe(**kwargs)`` forwards driver OPEN options only.
      * ``parse_spatial_file`` runs under ``asyncio.to_thread`` at four sites
        in ingest_spatial. A try/finally that restored the previous value
        would let one parse clear the option in the middle of another
        parse's read, turning a readable shapefile into a hard failure at
        random. Narrowing the scope would create the race, not remove it.
      * The option is unconditionally desirable and idempotent, so there is
        nothing to scope. Set-once removes the race entirely.

    Caveat: GDAL WRITES the regenerated .shx beside the .shp. Every caller in
    this repo reads from a TemporaryDirectory; moving the read to a read-only
    mount would make GDAL log "Error opening file …shx for writing" and fail.
    """
    try:
        from pyogrio import set_gdal_config_options
    except ImportError as exc:
        # The module is importable without the geospatial stack — it is
        # parse_spatial_file that needs it, and it imports geopandas itself.
        logger.debug(
            "spatial_parser: pyogrio unavailable; GDAL options not set (%s)", exc
        )
        return
    set_gdal_config_options({"SHAPE_RESTORE_SHX": "YES"})


_configure_gdal_once()


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class SpatialFeature:
    """Parsed representation of a single vector feature."""

    name: str | None
    feature_type: str
    geometry_wkt: str      # WKT string ready for ST_GeomFromText(..., 4326)
    geometry_type: str     # Shapely geom_type: "Polygon", "MultiPolygon", etc.
    properties: dict       # All non-geometry attributes from the source file


@dataclass
class SpatialParseResult:
    """Top-level result returned by parse_spatial_file."""

    source_format: str           # "shapefile", "geojson", "gpkg", "dxf", etc.
    source_crs: str              # e.g. "EPSG:4326" or "EPSG:32613"
    feature_count: int           # number of successfully parsed features
    empty_geom_skipped: int      # features dropped due to null/empty geometry
    features: list[SpatialFeature]
    source_file: str
    # Sprint 4 additions
    driver: str | None = None           # OGR driver used (pyogrio/geopandas)
    layer_count: int = 1                # number of OGR layers found
    layer_names: list[str] = field(default_factory=list)   # populated for multi-layer formats
    deferred_capabilities: list[str] = field(default_factory=list)  # signals for Sprint 4b
    dxf_blocks: list[dict] = field(default_factory=list)            # populated for DXF files (Sprint 4b)
    skipped_details: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    # QField (mobile companion to QGIS) detection — populated for .gpkg only.
    # When True, silver_spatial treats features as field observations:
    # sets georef_method='survey', crs_confidence=0.9, maps QField 'accuracy'
    # → spatial_uncertainty_m, uploads BLOB photos to MinIO.
    is_qfield: bool = False
    qfield_layers: list[str] = field(default_factory=list)
    qfield_metadata_tables: list[str] = field(default_factory=list)
    #: CRS confidence (0..1) from _score_crs_confidence, and the heuristic's
    #: reason. Computed since Section 04b but never surfaced — it only ever
    #: reached a log line when it fell below 0.5, so silver.spatial_features'
    #: crs_confidence / georef_method columns (CC-01 Item 2, which drive the
    #: map's positional-uncertainty ring) had no value to store and every
    #: feature landed as 'assumed'.
    crs_confidence: float | None = None
    crs_confidence_reason: str | None = None
    #: The file declared no CRS, no ``source_epsg`` override was supplied,
    #: and the format is not one that legitimately carries none. ``source_crs``
    #: is the empty string and the features, though parsed, are in unknown
    #: units — THE CALLER MUST NOT PERSIST THEM. Writing them as SRID 4326 is
    #: what put a correctly-georeferenced Alaskan shapefile at longitude
    #: 400,797 degrees, and a run that stores them cannot be told apart from
    #: a good one afterwards.
    crs_missing: bool = False
    #: ``source_epsg`` was applied because the file declared no CRS of its
    #: own. ingest_spatial reads this as georef_method='manual' — a human
    #: asserted the coordinate system — while ``crs_confidence`` stays the
    #: MEASURED fit of the geometry to that CRS, so the claim is checked
    #: against the data rather than trusted.
    crs_override_applied: bool = False
    #: The EPSG code actually applied, when ``crs_override_applied``.
    crs_override_epsg: int | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _detect_format(path: str) -> str | None:
    """Map file extension (case-insensitive) to an OGR driver name.

    Returns None if the extension is not in the known map.
    """
    ext = os.path.splitext(path)[1].lower()
    return _VECTOR_EXTENSIONS.get(ext)


def _materialise_case_variant_sidecars(path: str, ext: str) -> list[str]:
    """Give GDAL the exactly-cased sidecar names it insists on.

    GDAL on Linux resolves sidecars case-SENSITIVELY. Measured against the
    deployed image: ``drobeck_shumagin_veins.shp`` beside
    ``Drobeck_Shumagin_Veins.prj`` reads with ``crs=None``; rename the .prj
    to match the .shp's case and the same file reads as EPSG:26904. An
    ALL-UPPER .prj does not work either — only an exact-case match does.

    Real deliveries do not honour that. The RedStar hand-off ships exactly
    the pair above, and MapInfo tables there carry upper-case .DAT/.MAP
    siblings beside mixed-case .TAB entry points. Without this repair the
    missing-CRS refusal in ``_resolve_crs`` would hard-reject a file whose
    coordinate system is sitting right there on disk, and the geologist
    would be told to supply an EPSG code they already supplied.

    The directory is listed ONCE and any sidecar whose lower-cased name
    matches but whose case differs is COPIED (never renamed — the original
    may be referenced by something else, and a rename that half-succeeded
    would destroy the delivery) to the name GDAL will look for.

    Returns the list of file names created, for the provenance record.
    """
    sidecar_exts = _SIDECAR_EXTENSIONS.get(ext)
    if not sidecar_exts:
        return []

    directory = os.path.dirname(os.path.abspath(path))
    stem = os.path.splitext(os.path.basename(path))[0]

    try:
        entries = os.listdir(directory)
    except OSError as exc:
        logger.warning(
            "spatial_parser: cannot list '%s' for sidecar case repair: %s",
            directory, exc,
        )
        return []

    by_lower: dict[str, str] = {}
    for name in entries:
        by_lower.setdefault(name.lower(), name)

    created: list[str] = []
    for sidecar_ext in sidecar_exts:
        target = f"{stem}{sidecar_ext}"
        target_path = os.path.join(directory, target)
        # os.path.exists rather than a membership test against `entries`:
        # on a case-INSENSITIVE filesystem (Windows, macOS) the mis-cased
        # file already answers to the name GDAL wants, and copying it onto
        # itself would raise.
        if os.path.exists(target_path):
            continue
        actual = by_lower.get(target.lower())
        if actual is None:
            continue
        try:
            shutil.copyfile(os.path.join(directory, actual), target_path)
        except OSError as exc:
            logger.warning(
                "spatial_parser: could not copy sidecar '%s' to '%s': %s",
                actual, target, exc,
            )
            continue
        created.append(target)
        logger.info(
            "spatial_parser: sidecar '%s' copied to '%s' so GDAL can find it",
            actual, target,
        )

    return created


#: A MapInfo .tab whose first line declares a raster. Such a file is the
#: georeferencing header for a .tif, not a vector table.
_MAPINFO_RASTER_RE = re.compile(r'^\s*type\s+"?raster"?', re.IGNORECASE | re.MULTILINE)

#: A Discover georeferencing (ground-control-point) table. Its Definition
#: Table is a fixed set of warp columns -- pixel positions, their map
#: coordinates, and the residuals of the fit -- and it is `Type NATIVE`, so
#: the RASTER check above never sees it. It holds the control points used to
#: rectify a scanned map, never geology, and no combination of sidecars
#: turns it into a vector layer. Three arrived in the RedStar delivery
#: (`*_gcp.TAB`), each reported to the geologist as a missing-sidecar error
#: telling them to re-upload files that do not exist.
_MAPINFO_GCP_FIELDS = ("image_x", "image_y", "map_x", "map_y")

#: Discover's cross-section module writes its section definitions as a
#: NATIVE table too (`Sitka_trA.tab`: ID / NumVal / StrVal, with the
#: section's project, name, collar table and depth units in metadata).
#: A literal, not a regex: the metadata key is backslash-delimited
#: and \x is not a legal escape inside a pattern.
_MAPINFO_XSECT_KEY = "\\discover\\xsects"

#: What CoordSys the header declares, if it declares one. A NATIVE .tab
#: keeps its projection in the .map, but Discover writes the warp projection
#: into the .tab's own metadata -- so these headers are readable coordinate
#: systems even when the table they describe is unreadable. The RedStar GCP
#: tables all carry "UTM Zone 4 (NAD 83)", the very CRS the .prj-less
#: shapefiles beside them needed.
_MAPINFO_PROJ_NAME_RE = re.compile(r'ProjectionName"?\s*=\s*"([^"]+)"', re.IGNORECASE)
_MAPINFO_COORDSYS_RE = re.compile(r'(CoordSys\s+Earth[^"\r\n]*)', re.IGNORECASE)


def _mapinfo_declared_crs(header: str) -> str | None:
    """The coordinate system a MapInfo header names, in its own words.

    Returned for the message only. It is deliberately NOT converted to an
    EPSG code here: the caller is refusing the file either way, and a wrong
    conversion asserted confidently is worse than naming what the file says.
    """
    name = _MAPINFO_PROJ_NAME_RE.search(header)
    if name:
        return name.group(1).strip()
    clause = _MAPINFO_COORDSYS_RE.search(header)
    return clause.group(1).strip() if clause else None


def _mapinfo_is_gcp_table(header: str) -> bool:
    """Whether the Definition Table is a warp control-point schema."""
    lowered = header.lower()
    return all(field in lowered for field in _MAPINFO_GCP_FIELDS)


def _inspect_mapinfo_tab(path: str) -> None:
    """Refuse a .tab this parser cannot read, with a message that says why.

    Two failure modes, both present in the RedStar delivery:

      * ``Type "RASTER"`` — the .tab is a georeferencing header for a raster
        image, not a vector table. Handed one, the MapInfo driver fails deep
        inside pyogrio with no hint that the file was never vector data.
      * a NATIVE table whose .map / .dat siblings were not delivered. GDAL
        raises ``DataSourceError``, which reaches the geologist as an
        unexplained stack trace. It is worth naming precisely, because a
        NATIVE .tab header carries no CoordSys of its own — the CRS lives in
        the .map — so a .tab without its .map is a CRS loss as well as a
        data loss, and no EPSG override can be checked against coordinates
        that were never delivered.

    Raises:
        NotImplementedError: the .tab describes a raster.
        FileNotFoundError: a NATIVE .tab is missing .map and/or .dat.
    """
    header = ""
    try:
        with open(path, encoding="latin-1") as fh:
            header = fh.read(4096)
    except OSError as exc:
        # Unreadable header is not itself fatal — let GDAL have its go and
        # report whatever it finds.
        logger.warning(
            "spatial_parser: could not read MapInfo header of '%s': %s", path, exc
        )
        return

    if _MAPINFO_RASTER_RE.search(header):
        raise NotImplementedError(
            f"'{os.path.basename(path)}' is a MapInfo RASTER "
            "table — a georeferencing header for an image, not vector data. "
            "Upload the image it references (.tif) through the raster path "
            "instead; this .tab carries only its corner points and CoordSys."
        )

    name = os.path.basename(path)
    declared = _mapinfo_declared_crs(header)
    crs_note = (
        f" It does declare a coordinate system ({declared}) — the one to use "
        f"for the files beside it that declare none."
        if declared else ""
    )

    # Both of these are checked BEFORE the sidecar test below, and the order
    # is the whole point: they are missing their .map and .dat as well, so
    # the sidecar message fires first and sends the geologist looking for
    # files that were never part of the table. Neither becomes vector data
    # once those files are found, so that advice cannot succeed.
    if _mapinfo_is_gcp_table(header):
        raise NotImplementedError(
            f"'{name}' is a georeferencing control-point table, not a map "
            f"layer. Its columns are pixel positions and their map "
            f"coordinates (Image_X / Image_Y / Map_X / Map_Y) plus the "
            f"residuals of the fit — the numbers MapInfo used to rectify a "
            f"scanned image. There is no geology in it to import, with or "
            f"without its sidecars. Upload the scanned map it rectifies (the "
            f".tif) through the raster path instead.{crs_note}"
        )

    if _MAPINFO_XSECT_KEY in header.lower():
        raise NotImplementedError(
            f"'{name}' is a Discover cross-section definition, not a map "
            f"layer — it records how a section was drawn (its project, the "
            f"collar table it was built from, its depth units), not features "
            f"with positions on the ground. Upload the collar and interval "
            f"tables it was built from and the section can be drawn from "
            f"those.{crs_note}"
        )

    stem = os.path.splitext(path)[0]
    missing = [e for e in (".map", ".dat") if not os.path.isfile(stem + e)]
    if missing:
        raise FileNotFoundError(
            f"MapInfo table '{os.path.basename(path)}' is "
            f"missing {', '.join(missing)}. A NATIVE .tab is only a header: "
            "the geometry lives in the .map and the attributes in the .dat. "
            "Re-upload the table with every sidecar (.tab, .dat, .map, .id "
            "and .ind if present)." + (
                f" Its coordinate system is readable from this header "
                f"({declared}), but the geometry it describes is not."
                if declared else
                " The coordinate system is in the .map too, so this is a CRS "
                "loss as well as a data loss."
            )
        )


def _sha256_path(path: str) -> str:
    """SHA-256 hex digest of a file path.

    For directories (.gdb), hashes the concatenation of '{name}:{size}' for
    every file inside the directory (sorted for determinism).
    """
    p = Path(path)
    if p.is_dir():
        h = hashlib.sha256()
        for child in sorted(p.rglob("*")):
            if child.is_file():
                entry = f"{child.name}:{child.stat().st_size}"
                h.update(entry.encode())
        return h.hexdigest()
    with open(path, "rb") as fh:
        raw = fh.read()
    return hashlib.sha256(raw).hexdigest()


#: The ONLY values silver.spatial_features.feature_type accepts, per
#: chk_spatial_features_type. This is a schema contract, not a suggestion —
#: anything outside it fails the INSERT for the whole batch.
FEATURE_TYPES: tuple[str, ...] = (
    "fault", "contact", "mineralization_zone", "shear_zone", "dyke",
    "alteration_halo", "lineament", "occurrence", "sample_point",
    "outcrop", "boundary", "other",
)

#: Ordered (substring, canonical type) rules. First match wins, so the more
#: specific patterns come first — "shear zone" must not be caught by a bare
#: "zone" rule, and "fault contact" should read as a contact.
_TYPE_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("shear", "mylonit"), "shear_zone"),
    (("dyke", "dike", "sill"), "dyke"),
    (("alteration", "anomal", "halo", "gossan"), "alteration_halo"),
    (("lineament", "magnetic_trend", "structural_trend"), "lineament"),
    (("contact", "unconformit"), "contact"),
    (("fault", "thrust", "shearzone"), "fault"),
    (("mineraliz", "orebody", "ore_zone", "ore zone", "vein", "lode"), "mineralization_zone"),
    (("occurrence", "showing", "prospect", "deposit", "minfile", "smdi"), "occurrence"),
    (("sample", "assay", "geochem", "station"), "sample_point"),
    (("outcrop", "exposure", "bedrock_expos"), "outcrop"),
    (("boundary", "claim", "tenure", "disposition", "lease", "permit"), "boundary"),
)


def _infer_feature_type(props: dict, path: str) -> str:
    """Best-effort feature type classification from file name and attributes.

    Purely heuristic — the caller can pass an explicit ``feature_type``
    override, and should when the geologist told them what the file is.

    Every return value is a member of FEATURE_TYPES. That was not true until
    2026-08-20: this returned "alteration" (the column wants
    "alteration_halo"), "target" and "feature", none of which are legal. Since
    "feature" was the DEFAULT, any file whose features matched no rule failed
    the whole insert with

        new row for relation "spatial_features" violates check constraint
        "chk_spatial_features_type"

    and the Dagster asset that used to call this passed the value straight
    through, so it had the same fault.

    "target" is deliberately NOT mapped to mineralization_zone. An exploration
    target is a hypothesis about where mineralization might be; recording it
    as a mineralization zone would assert a geological fact nobody has
    established. It falls through to "other", which keeps the feature and its
    name without inventing a claim.
    """
    lower_path = os.path.basename(path).lower()
    prop_values = " ".join(str(v).lower() for v in props.values() if v is not None)
    combined = f"{lower_path} {prop_values}"

    for needles, canonical in _TYPE_RULES:
        if any(needle in combined for needle in needles):
            return canonical
    return "other"


def _safe_str(val) -> str | None:
    """Convert a value to string, returning None for Pandas NA / None / empty."""
    if val is None:
        return None
    try:
        import pandas as pd
        if pd.isna(val):
            return None
    except (TypeError, ImportError):
        pass
    s = str(val).strip()
    return s if s else None


def _is_null_geometry(geom) -> bool:
    """True when a row carries no usable geometry.

    ``geom is None or geom.is_empty`` was not enough, and the gap lost whole
    layers. An ESRI Null shape (record type 0) is perfectly legal — ArcGIS
    writes them for deleted-but-not-packed records, so they are the normal
    state of a working dataset, not an exotic case. GeoPandas materialises
    one as ``None`` in the GeometryArray, but ``DataFrame.iterrows()``
    rebuilds each row as a Series, and that coercion turns the ``None`` into
    ``NaN`` — a float. ``geom.is_empty`` then raises AttributeError, the
    exception escapes the per-row loop, and EVERY feature in the file is
    lost because one was null.

    Measured on RedStar's drobeck_shumagin_veins.shp: 56 records, 54
    PolyLine and 2 Null (records 14 and 26), all 56 lost.

    Anything without an ``is_empty`` attribute is treated as null: that
    covers the NaN and leaves no second way for a non-geometry to reach
    ``.wkt`` below.
    """
    if geom is None:
        return True
    if not hasattr(geom, "is_empty"):
        return True
    return bool(geom.is_empty)


def _sanitise_properties(row_dict: dict) -> dict:
    """Strip geometry key and convert non-JSON-serialisable values to strings.

    psycopg2.extras.Json will serialise the result dict; we must ensure values
    are JSON-compatible basic types (str, int, float, bool, None).
    """
    result = {}
    for k, v in row_dict.items():
        if k == "geometry":
            continue
        if v is None:
            continue
        # Convert pandas NA, numpy scalars, etc. to Python natives
        try:
            import pandas as pd
            if pd.isna(v):
                continue
        except (TypeError, ImportError):
            pass
        if isinstance(v, (str, int, float, bool)):
            result[k] = v
        else:
            result[k] = str(v)
    return result


# ---------------------------------------------------------------------------
# CRS confidence helper
# ---------------------------------------------------------------------------

def _score_crs_confidence(gdf) -> tuple[float, str]:
    """Score how likely the GeoDataFrame's declared CRS matches its coordinate data.

    Compares the geometry bounding box against the CRS's declared area of use
    (PyProj's CRS.area_of_use.bounds).

    Returns
    -------
    (score, reason) where score is 0.0–1.0:
      1.0  — bounds fully inside CRS area of use
      0.5  — partial overlap
      0.0  — bounds outside CRS area, or CRS is None
    """
    if gdf.crs is None:
        return 0.0, "no CRS declared"

    try:
        from pyproj import CRS
        crs_obj = CRS.from_user_input(gdf.crs)
        area = crs_obj.area_of_use
        if area is None:
            return 0.5, "CRS has no area_of_use defined"

        # total_bounds returns (minx, miny, maxx, maxy) in projected units.
        # For geographic CRS these are degrees; for projected CRS they are metres.
        # We compare against area_of_use.bounds which is always in degrees.
        # We use the geographic CRS for the comparison.
        bounds = gdf.to_crs("EPSG:4326").geometry.total_bounds  # (minx, miny, maxx, maxy)
        data_west, data_south, data_east, data_north = bounds

        aou_west = area.west
        aou_south = area.south
        aou_east = area.east
        aou_north = area.north

        # Fully inside
        if (
            data_west >= aou_west
            and data_east <= aou_east
            and data_south >= aou_south
            and data_north <= aou_north
        ):
            return 1.0, "bounds match CRS extent"

        # Fully outside — no overlap
        if (
            data_east < aou_west
            or data_west > aou_east
            or data_north < aou_south
            or data_south > aou_north
        ):
            return 0.0, "coordinates outside declared CRS extent"

        # Partial overlap
        return 0.5, "partial CRS extent overlap"

    except Exception as exc:
        logger.debug("CRS confidence scoring failed: %s", exc)
        return 0.5, f"scoring error: {exc}"


def _validate_source_epsg(source_epsg) -> int | None:
    """Check a caller-supplied EPSG code before anything is read.

    The range is not this module's invention: silver.spatial_features
    constrains ``crs_epsg_native`` to 1024..32767 and the HTTP edge applies
    the identical rule. An out-of-range code would survive the parse and
    then fail the INSERT for every feature in the file — the failure mode
    that lost a whole delivery on 2026-08-20 when an unvalidated
    ``feature_type`` reached the same CHECK constraint. Refusing it here,
    before the read, turns a whole-file INSERT failure into one clear
    message.

    A CRS *string* is deliberately not accepted. There is exactly one wire
    representation for a coordinate system in this codebase and it is an
    integer EPSG code.
    """
    if source_epsg is None:
        return None
    # bool is an int subclass; True would otherwise read as EPSG 1.
    if isinstance(source_epsg, bool) or not isinstance(source_epsg, int):
        raise ValueError(
            "source_epsg must be an integer EPSG code, got "
            f"{source_epsg!r} ({type(source_epsg).__name__}). Coordinate "
            "reference systems are passed as codes, never as strings."
        )
    if not _MIN_EPSG <= source_epsg <= _MAX_EPSG:
        raise ValueError(
            f"source_epsg {source_epsg} is outside the "
            f"{_MIN_EPSG}-{_MAX_EPSG} range that "
            "silver.spatial_features.crs_epsg_native accepts."
        )
    return source_epsg


@dataclass
class _CrsDecision:
    """What the parser concluded about a frame's coordinate reference system."""

    source_crs: str
    confidence: float | None = None
    reason: str | None = None
    missing: bool = False
    override_applied: bool = False
    override_epsg: int | None = None


def _score_and_warn(gdf, path: str, warnings_out: list[dict]) -> tuple[float | None, str | None]:
    """Score the frame's CRS against its own coordinates, warning when poor."""
    if gdf.empty:
        return None, "no geometry to score"
    try:
        crs_score, crs_reason = _score_crs_confidence(gdf)
    except Exception as exc:
        logger.debug("spatial_parser: CRS confidence scoring skipped: %s", exc)
        return None, None

    if crs_score < 0.5:
        warnings_out.append({
            "code": "crs_low_confidence",
            "message": f"CRS confidence score {crs_score:.1f}: {crs_reason}",
            "detail": (
                f"The coordinates in {os.path.basename(path)} do not sit where "
                f"its coordinate system says they should ({crs_reason}). The "
                "features were kept, but their positions may be wrong."
            ),
            "context": {"score": crs_score, "reason": crs_reason},
        })
        logger.warning(
            "spatial_parser: low CRS confidence (%.1f) for '%s' — %s",
            crs_score, path, crs_reason,
        )
    return crs_score, crs_reason


def epsg_from_wkt_text(wkt: str) -> tuple[int | None, str | None]:
    """Resolve `.prj` text to (EPSG code, CRS name) with pyproj.

    Default identify confidence ONLY. At ``min_confidence=25`` pyproj
    matched a custom grid (the RedStar donor WKT with its central meridian
    moved to -158.123) to EPSG:26929 — a confident answer whose parameters
    differ from the file's. A custom mine grid must come back unresolved
    (``(None, its name)``) and be typed by a human, not rounded to the
    nearest UTM zone. The real donor shape — ESRI-style WKT with no
    AUTHORITY clause, ``"NAD_1983_UTM_Zone_4N"`` — resolves at default
    confidence through proj.db's alias tables; measured: 26904.

    Raises whatever pyproj raises on text it cannot read at all — the
    caller decides what a refusal means (ingest_spatial logs it and
    carries on unplaced).
    """
    from pyproj import CRS  # noqa: PLC0415

    crs = CRS.from_wkt(wkt)
    return crs.to_epsg(), crs.name


def _resolve_crs(gdf, ext: str, path: str, source_epsg: int | None,
                 warnings_out: list[dict]):
    """Decide a frame's CRS, before any reprojection. Returns (gdf, decision).

    This is the ONLY place source_crs is decided. It used to be decided in
    four: the generic else-arm, the DXF arm, and the empty-frame early
    return — and each of the three that anyone remembered to look at fell
    back to EPSG:4326 whatever the file actually was. That fallback is the
    bug. A projected shapefile whose .prj was lost read as 4326, was
    therefore never reprojected, and its metre eastings were inserted as
    degrees: RedStar's Unga Island veins landed at longitude 400,797.

    Precedence, in order, and not negotiable:

      1. A CRS the FILE declares always wins. ``source_epsg`` is a claim
         about a file that says nothing; it never overrules one that speaks.
         When both exist and disagree, the file is used and the caller is
         told their override was ignored.
      2. Otherwise ``source_epsg``, if supplied. The confidence stored is
         the MEASURED fit of the geometry to that CRS — we check the human's
         claim against the data instead of inventing a number for it.
      3. Otherwise EPSG:4326, but only for _NO_CRS_EXTENSIONS.
      4. Otherwise ``missing``: source_crs is empty, and the caller must
         refuse the file rather than write features it cannot place.

    A gdf is returned as well as a decision because assigning a CRS produces
    a new frame rather than mutating one.
    """
    basename = os.path.basename(path)

    if ext == ".dxf":
        # pyogrio may populate a synthetic CRS for DXF; clear it explicitly.
        # Cleared and then allowed to FALL THROUGH: this arm used to return
        # here unconditionally, which made DXF the one format whose
        # source_epsg override was silently ignored — the wizard rendered an
        # EPSG field on DXF rows, the API accepted the code, and nothing
        # read it. The declares-nothing logic below applies the override
        # with a measured fit, exactly as it does for a .prj-less shapefile.
        gdf = gdf.set_crs(None, allow_override=True)
        if source_epsg is None:
            warnings_out.append({
                "code": "dxf_no_crs",
                "message": "DXF files have no CRS; caller must georeference.",
                "detail": (
                    f"{basename} is a CAD drawing in model units — the format "
                    "has no coordinate system to read. Its features are "
                    "stored as 'assumed' so the map shows their position as "
                    "uncertain. Supply an EPSG code at upload time to place "
                    "them properly; dropping the file loose on the upload "
                    "screen beside a .prj also carries the coordinate system "
                    "over, but a .prj zipped in next to a CAD file is not "
                    "read."
                ),
                "context": {"path": path},
            })
            return gdf, _CrsDecision(
                source_crs=_NO_CRS_DEFAULT,
                confidence=0.0,
                reason="DXF carries no CRS; the caller must georeference",
            )

    declared = gdf.crs
    if declared is not None:
        source_crs = declared.to_string()
        declared_epsg = declared.to_epsg()
        if source_epsg is not None and declared_epsg != source_epsg:
            warnings_out.append({
                "code": "crs_override_ignored",
                "message": (
                    f"{basename} declares {source_crs}; the supplied "
                    f"EPSG:{source_epsg} was not applied."
                ),
                "detail": (
                    f"You supplied EPSG:{source_epsg}, but {basename} carries "
                    f"its own coordinate system ({source_crs}). The file's own "
                    "was used — overwriting a declared CRS silently moves data "
                    "and cannot be undone. If the file is wrong, fix it at "
                    "source and re-upload."
                ),
                "context": {
                    "declared": source_crs,
                    "declared_epsg": declared_epsg,
                    "requested_epsg": source_epsg,
                },
            })
            logger.warning(
                "spatial_parser: '%s' declares %s; supplied EPSG:%s ignored",
                path, source_crs, source_epsg,
            )
        confidence, reason = _score_and_warn(gdf, path, warnings_out)
        return gdf, _CrsDecision(
            source_crs=source_crs, confidence=confidence, reason=reason
        )

    # --- the file declares nothing ---

    if source_epsg is not None:
        gdf = gdf.set_crs(f"EPSG:{source_epsg}", allow_override=True)
        confidence, reason = _score_and_warn(gdf, path, warnings_out)
        logger.info(
            "spatial_parser: '%s' declares no CRS; applying supplied "
            "EPSG:%s (measured confidence %s)", path, source_epsg, confidence,
        )
        return gdf, _CrsDecision(
            source_crs=f"EPSG:{source_epsg}",
            confidence=confidence,
            reason=reason,
            override_applied=True,
            override_epsg=source_epsg,
        )

    if ext in _NO_CRS_EXTENSIONS:
        if ext == ".dgn":
            warnings_out.append({
                "code": "dgn_no_crs",
                "message": "DGN files have no CRS; caller must georeference.",
                "detail": (
                    f"{basename} is a MicroStation design file — the format "
                    "has no coordinate system to read, so its features are "
                    "stored as 'assumed' and the map shows their position as "
                    "uncertain. Supply an EPSG code at upload time to place "
                    "them properly."
                ),
                "context": {"path": path},
            })
            logger.warning(
                "spatial_parser: '%s' is DGN — no CRS concept in the format", path
            )
            return gdf, _CrsDecision(
                source_crs=_NO_CRS_DEFAULT,
                confidence=0.0,
                reason="DGN carries no CRS; the caller must georeference",
            )
        # GeoJSON: RFC 7946 §4 — WGS84 lon/lat is the specified default, not
        # an assumption, so this is not warned about. Scoring still runs: a
        # .geojson holding UTM eastings is a real and common mistake and the
        # low-confidence warning is how it gets surfaced.
        gdf = gdf.set_crs(_NO_CRS_DEFAULT, allow_override=True)
        confidence, reason = _score_and_warn(gdf, path, warnings_out)
        return gdf, _CrsDecision(
            source_crs=_NO_CRS_DEFAULT,
            confidence=confidence,
            reason=reason or "GeoJSON defaults to WGS84 per RFC 7946",
        )

    warnings_out.append({
        "code": "crs_required",
        "message": (
            f"{basename} declares no coordinate system and none was supplied."
        ),
        "detail": (
            f"{basename} carries no coordinate system, and {ext or 'this format'} "
            "is not one that legitimately omits it. Its coordinates cannot be "
            "placed on the map, so nothing was imported. Re-upload the file "
            "with its .prj sidecar, or supply the EPSG code at upload time."
        ),
        "context": {"path": path, "extension": ext},
    })
    logger.error(
        "spatial_parser: '%s' has no CRS and no source_epsg override — "
        "features cannot be georeferenced", path,
    )
    return gdf, _CrsDecision(
        source_crs="",
        confidence=0.0,
        reason="no CRS declared and no override supplied",
        missing=True,
    )


# ---------------------------------------------------------------------------
# QField detection helpers (GeoPackage only)
# ---------------------------------------------------------------------------

def _list_gpkg_sqlite_tables(path: str) -> list[str]:
    """Return the SQLite table names inside a GeoPackage file.

    GPKG is a SQLite database with the GeoPackage schema extension on top,
    so we can open it with the stdlib ``sqlite3`` driver to inspect QField /
    QGIS metadata tables that pyogrio does not expose.

    Returns an empty list on any IO/sqlite error — detection is best-effort
    and never blocks the main parse path.
    """
    import sqlite3

    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            return [row[0] for row in cur.fetchall()]
    except Exception as exc:
        logger.debug("spatial_parser: sqlite probe failed for '%s': %s", path, exc)
        return []


def _detect_qfield_layer(layer_columns: list[str]) -> tuple[bool, str | None]:
    """Return (is_qfield_layer, accuracy_column_name).

    A layer is treated as a QField field-observation layer when it carries
    both a GPS accuracy column and at least one corroborating column
    (timestamp / device / photo). Names are compared case-insensitively.
    """
    lower_cols = {c.lower(): c for c in layer_columns}
    accuracy_col_lower = next(
        (c for c in _QFIELD_ACCURACY_COLS if c in lower_cols), None
    )
    if accuracy_col_lower is None:
        return False, None
    corroborating = any(c in lower_cols for c in _QFIELD_CORROBORATING_COLS)
    if not corroborating:
        return False, None
    return True, lower_cols[accuracy_col_lower]


def _hoist_qfield_properties(
    row_dict: dict,
    accuracy_col: str | None,
) -> dict:
    """Lift QField waypoint attributes into synthetic ``_qfield_*`` keys.

    silver_spatial reads these keys to populate spatial_uncertainty_m,
    crs_confidence, georef_method, and to drive the photo upload + the
    qfield_photos MinIO write.

    Mutates ``row_dict`` in place (removes the lifted attributes) and
    returns a dict of the synthetic markers so the caller can stash them
    on SpatialFeature.properties.
    """
    out: dict = {"_qfield": True}

    # Accuracy → uncertainty radius in metres.
    if accuracy_col and accuracy_col in row_dict:
        try:
            val = row_dict.get(accuracy_col)
            if val is not None:
                out["_qfield_accuracy_m"] = float(val)
        except (TypeError, ValueError):
            pass

    # Timestamp lift — coerce to ISO string for JSON storage.
    for ts_col in ("timestamp", "captured_at"):
        actual = next((c for c in row_dict if c.lower() == ts_col), None)
        if actual and row_dict.get(actual) is not None:
            try:
                out["_qfield_timestamp"] = str(row_dict[actual])
            except Exception:
                pass
            break

    # Device id.
    for dev_col in ("device_id", "device"):
        actual = next((c for c in row_dict if c.lower() == dev_col), None)
        if actual and row_dict.get(actual) is not None:
            out["_qfield_device"] = str(row_dict[actual])
            break

    # Photo — BLOB (bytes) is uploaded by silver_spatial; a filename
    # reference is logged but cannot be resolved without the original
    # device directory tree. BLOB columns are POPPED so the bytes don't
    # round-trip through _sanitise_properties (which would stringify them
    # to a useless b'...' repr).
    for ph_col in ("photo", "picture", "image"):
        actual = next((c for c in row_dict if c.lower() == ph_col), None)
        if actual is None:
            continue
        val = row_dict.get(actual)
        if isinstance(val, (bytes, bytearray)) and len(val) > 0:
            out["_qfield_photo_bytes"] = bytes(val)
            row_dict.pop(actual, None)
            break
        if isinstance(val, str) and val.strip():
            out["_qfield_photo_ref"] = val.strip()
            break

    return out


# ---------------------------------------------------------------------------
# Multi-layer reader
# ---------------------------------------------------------------------------

def _read_all_layers(
    path: str, driver: str | None, layer: str | None = None
) -> tuple[Any, list[str], dict[str, list[str]]]:
    """Read layers from a multi-layer format using pyogrio.

    Returns ``(combined_gdf, layer_names_list, per_layer_columns)``.
    Each feature gets a '_layer_name' column indicating its source layer.
    ``per_layer_columns`` maps layer-name → original attribute column list
    (excluding 'geometry') so QField detection can inspect per-layer schema
    after the concat squashes columns together.

    When *layer* is given, ONLY that layer is read. A QGIS project references
    one layer of a GeoPackage as ``./eagle.gpkg|layername=collars``; without
    this, every layer of a multi-layer container returned the container's
    entire contents, so two different project layers pointing at the same
    .gpkg produced byte-identical feature sets and layer identity was lost.

    Falls back to geopandas.read_file for single-layer paths.
    """
    import geopandas as gpd
    import pyogrio

    raw_layers = pyogrio.list_layers(path)
    # list_layers returns an ndarray of shape (N, 2): [[name, geom_type], ...]
    # geom_type is None for non-spatial sidecar tables (e.g. QGIS / QField
    # metadata tables in a .gpkg). Those would concat into the combined
    # GeoDataFrame as rows with NaN geometry — skip them.
    layer_names = [
        str(row[0]) for row in raw_layers
        if row[1] is not None and str(row[1]).lower() != "none"
    ]
    if layer is not None:
        if layer not in layer_names:
            raise ValueError(
                f"layer {layer!r} not found in {path} "
                f"(available: {', '.join(layer_names) or 'none'})"
            )
        layer_names = [layer]

    per_layer_cols: dict[str, list[str]] = {}

    if len(layer_names) <= 1:
        gdf = gpd.read_file(path, layer=layer) if layer is not None else gpd.read_file(path)
        if len(layer_names) == 1:
            per_layer_cols[layer_names[0]] = [
                c for c in gdf.columns if c != "geometry"
            ]
            gdf["_layer_name"] = layer_names[0]
        return gdf, layer_names, per_layer_cols

    # Multiple layers — read each and concatenate
    frames = []
    for lname in layer_names:
        try:
            ldf = gpd.read_file(path, layer=lname)
            per_layer_cols[lname] = [c for c in ldf.columns if c != "geometry"]
            ldf["_layer_name"] = lname
            frames.append(ldf)
        except Exception as exc:
            logger.warning(
                "spatial_parser: failed to read layer '%s' from '%s': %s",
                lname, path, exc,
            )

    if not frames:
        return gpd.GeoDataFrame(), layer_names, per_layer_cols

    import pandas as pd
    combined = pd.concat(frames, ignore_index=True)
    return (
        gpd.GeoDataFrame(combined, crs=frames[0].crs if frames else None),
        layer_names,
        per_layer_cols,
    )


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------

def _extract_features(
    gdf,
    path: str,
    feature_type: str | None,
    qfield_layer_accuracy: dict[str, str | None] | None = None,
) -> tuple[list[SpatialFeature], int, list[dict]]:
    """Walk the GeoDataFrame rows and build SpatialFeature objects.

    Returns (features, empty_geom_skipped, skipped_details).

    ``qfield_layer_accuracy`` maps QField-detected layer name → its accuracy
    column name (or None). When a row's ``_layer_name`` is present in that
    map, QField synthetic properties (``_qfield_*``) are hoisted onto the
    feature properties so silver_spatial can populate
    spatial_uncertainty_m / crs_confidence / georef_method and upload
    photo BLOBs.
    """
    features: list[SpatialFeature] = []
    empty_geom_skipped = 0
    skipped_details: list[dict] = []
    qfield_map = qfield_layer_accuracy or {}

    for idx, row in gdf.iterrows():
        geom = row.get("geometry")

        if _is_null_geometry(geom):
            reason = "null or empty geometry"
            logger.warning(
                "spatial_parser: skipping feature %s in '%s' — %s", idx, path, reason
            )
            skipped_details.append({"feature_index": idx, "reason": reason})
            empty_geom_skipped += 1
            continue

        row_dict = row.to_dict()

        # QField hoist runs BEFORE _sanitise_properties so it can see the
        # raw bytes/datetime values that the sanitiser would otherwise
        # stringify or drop.
        qfield_extra: dict = {}
        layer_name = row_dict.get("_layer_name")
        if layer_name is not None and layer_name in qfield_map:
            qfield_extra = _hoist_qfield_properties(row_dict, qfield_map[layer_name])

        props = _sanitise_properties(row_dict)
        # _qfield_photo_bytes must survive the JSON sanitise: it is consumed
        # by silver_spatial (uploaded to MinIO + replaced with the object key)
        # before psycopg2.extras.Json ever serialises the dict.
        if qfield_extra:
            props.update({k: v for k, v in qfield_extra.items() if k != "_qfield_photo_bytes"})
            if "_qfield_photo_bytes" in qfield_extra:
                props["_qfield_photo_bytes"] = qfield_extra["_qfield_photo_bytes"]

        # Feature type: explicit override > heuristic inference
        ftype = feature_type if feature_type else _infer_feature_type(props, path)

        # Name extraction — try common attribute name patterns
        name_raw = (
            row_dict.get("name")
            or row_dict.get("NAME")
            or row_dict.get("feature_name")
            or row_dict.get("FEATURE_NAME")
            or row_dict.get("label")
            or row_dict.get("LABEL")
            or row_dict.get("id")
            or row_dict.get("ID")
        )
        name = _safe_str(name_raw) or f"feature_{idx}"

        features.append(
            SpatialFeature(
                name=name,
                feature_type=ftype,
                geometry_wkt=geom.wkt,
                geometry_type=geom.geom_type,
                properties=props,
            )
        )

    return features, empty_geom_skipped, skipped_details


# ---------------------------------------------------------------------------
# DXF block extraction (Sprint 4b) — requires ezdxf
# ---------------------------------------------------------------------------

def _extract_dxf_blocks(path: str | Path) -> list[dict]:
    """Return a list of block-definition dicts for a DXF file.

    Blocks in DXF are reusable collections of entities (points, lines, text, etc.).
    Each block has a name, an optional description, a base point, and a count
    of entities it contains. Block INSERT entities (insertions) reference a
    block by name with a transform (location, rotation, scale).

    Skips the implicit *MODEL_SPACE and *PAPER_SPACE blocks (names starting with '*').

    Raises:
        Any ezdxf exception on malformed files — caller wraps in try/except.
    """
    from collections import Counter

    import ezdxf

    doc = ezdxf.readfile(str(path))

    # Collect all INSERT entities from modelspace and paperspace for cross-ref.
    all_inserts: list = []
    for layout in (doc.modelspace(), doc.paperspace()):
        for ent in layout:
            if ent.dxftype() == "INSERT":
                all_inserts.append(ent)

    blocks_out: list[dict] = []
    for block in doc.blocks:
        # Skip implicit space blocks
        if block.name.startswith("*"):
            continue

        base_pt = block.base_point
        base_point_list: list[float] | None = [
            float(base_pt[0]), float(base_pt[1]), float(base_pt[2])
        ] if base_pt is not None else None

        # Layer comes from the BLOCK entity inside the BlockLayout, not from BLOCK_RECORD
        try:
            layer = block.block.dxf.layer
        except Exception:
            layer = "0"

        entities = list(block)
        entity_count = len(entities)
        entity_types = dict(Counter(e.dxftype() for e in entities))

        # Find insertions of this block in modelspace/paperspace
        insertions: list[dict] = []
        for ins in all_inserts:
            if ins.dxf.name != block.name:
                continue
            loc = ins.dxf.insert
            insertions.append({
                "location": [float(loc[0]), float(loc[1]), float(loc[2])],
                "rotation": float(ins.dxf.get("rotation", 0.0)),
                "xscale":   float(ins.dxf.get("xscale", 1.0)),
                "yscale":   float(ins.dxf.get("yscale", 1.0)),
                "layer":    ins.dxf.layer,
            })

        # Attribute tag names declared on the block
        attributes: list[str] = [a.dxf.tag for a in block.attdefs()]

        blocks_out.append({
            "name":          block.name,
            "base_point":    base_point_list,
            "layer":         layer,
            "entity_count":  entity_count,
            "entity_types":  entity_types,
            "insertions":    insertions,
            "attributes":    attributes,
        })

    return blocks_out


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _parse_surpac_strings(path: str, *, source_epsg: int | None) -> SpatialParseResult:
    """Parse a Surpac string file into spatial features.

    Separate from the GeoPandas path because there is no OGR driver for the
    format — ``gpd.read_file`` cannot open a ``.str`` at all — so the features
    are built directly rather than read out of a GeoDataFrame.

    Two decisions the caller cannot make later, so they are made here:

    * **The level elevation goes into properties, not the geometry.**
      ``silver.spatial_features.geom`` is 2D and every insert is wrapped in
      ``ST_Force2D``. Measured on a real file, the strings sit at 73 distinct
      elevations from -235 m to +125 m in exact 5 m steps — the level IS the
      dataset, and flattening it collapses 73 level plans into one plane of
      unreadable spaghetti.
    * **A closed string is only a ring if it encloses area.** ``closed`` from
      the reader means "the vertex list repeats its first point", which a
      two-vertex string A,A also satisfies. Those become LineStrings, and the
      genuinely open strings stay open — closing them would invent vein
      outline nobody digitised (measured gaps of 0.73 m and 0.40 m).

    The format declares no CRS, so an EPSG must be supplied or the caller is
    told not to persist — the same contract as a .prj-less shapefile, and for
    the same reason: assuming 4326 for projected coordinates is what put a
    previous delivery at longitude 400,797.
    """
    from georag_geoparsers.surpac_parser import read_surpac_strings  # noqa: PLC0415

    parsed = read_surpac_strings(path)
    basename = os.path.basename(path)

    features: list[SpatialFeature] = []
    for s in parsed.strings:
        # Distinct vertices, because a ring needs three of them to have area.
        distinct = {(round(x, 6), round(y, 6)) for x, y, _ in s.points}
        as_ring = s.closed and len(distinct) >= 3

        coords = ", ".join(f"{x} {y}" for x, y, _ in s.points)
        if as_ring:
            wkt, geom_type = f"POLYGON(({coords}))", "Polygon"
        else:
            wkt, geom_type = f"LINESTRING({coords})", "LineString"

        features.append(SpatialFeature(
            name=f"string {s.string_number}",
            feature_type="vein_outline",
            geometry_wkt=wkt,
            geometry_type=geom_type,
            properties={
                "surpac_string_number": s.string_number,
                "level_z": s.level_z,
                "point_count": len(s.points),
                "closed": s.closed,
            },
        ))

    warnings_out: list[dict] = []
    if source_epsg is None:
        warnings_out.append({
            "code": "surpac_no_crs",
            "message": "Surpac string files declare no coordinate system.",
            "detail": (
                f"{basename} is a Surpac string file, a format that stores "
                "coordinates in a mine grid with nothing to say which one. "
                "Supply an EPSG code at upload time to place it on the map; "
                "without one the features cannot be positioned and are not "
                "written."
            ),
        })

    return SpatialParseResult(
        source_format="surpac",
        source_crs=f"EPSG:{source_epsg}" if source_epsg else "",
        feature_count=len(features),
        empty_geom_skipped=0,
        features=features,
        source_file=path,
        driver=None,
        layer_count=1,
        layer_names=[Path(path).stem],
        warnings=warnings_out,
        provenance={
            "source_file": path,
            "source_file_sha256": _sha256_path(path),
            "parser_name": "surpac_parser",
            "parser_version": PARSER_VERSION,
            "source_col_map": {},
        },
        crs_missing=source_epsg is None,
        crs_override_applied=source_epsg is not None,
        # The coordinates are what the operator asserted, unverified against
        # any declaration in the file — there is none to check against.
        crs_confidence=0.5 if source_epsg else None,
        crs_confidence_reason=(
            "Surpac declares no CRS; the EPSG was supplied by the operator"
            if source_epsg else None
        ),
    )


def parse_spatial_file(
    path: str,
    feature_type: str | None = None,
    layer: str | None = None,
    source_epsg: int | None = None,
) -> SpatialParseResult:
    """Parse a vector spatial file into SpatialParseResult.

    Supported formats (via pyogrio/GeoPandas):
      .shp  — ESRI Shapefile
      .geojson / .json  — GeoJSON
      .gpkg  — GeoPackage (multi-layer)
      .gml  — GML
      .gpx  — GPX
      .dxf  — DXF (no CRS; georeferencing is the caller's responsibility)
      .dgn  — DGN (MicroStation)
      .gdb  — FileGDB directory (read-only via pyogrio OpenFileGDB driver)
      .fgb  — FlatGeobuf
      .tab / .mif  — MapInfo (entry points only; .dat/.map/.id/.ind/.mid are
            sidecars and must not be handed to this function directly)

    KML/KMZ is NOT supported — deferred to V1-roadmap per spec §04d.
    Kyle-approved 2026-04-20 (Module 3 Phase B Decision B).

    Args:
        path: Absolute path to the file (or directory for .gdb).
        feature_type: If supplied, every feature is tagged with this type
            instead of heuristic inference.  Passed in by the Silver asset
            config so the geologist can override at ingest time.
        layer: Read ONLY this layer from a multi-layer container. A QGIS
            project addresses one layer of a GeoPackage as
            ``./eagle.gpkg|layername=collars``; without this the whole
            container is read for every reference, so two project layers
            backed by the same file returned identical features.
        source_epsg: EPSG code (1024..32767) to use WHEN — and only when —
            the file declares no CRS of its own. A CRS the file declares
            always wins; if the two disagree, the file's is used and a
            crs_override_ignored warning is emitted. When the override is
            applied, ``crs_override_applied`` is set and ``crs_confidence``
            holds the MEASURED fit of the geometry to the supplied CRS, so
            the human's claim is checked against the data rather than
            trusted. A CRS string is not accepted.

    Returns:
        SpatialParseResult.  Empty/null geometries are counted in
        empty_geom_skipped and never silently ignored.

        CHECK ``crs_missing`` BEFORE PERSISTING ANYTHING. When it is True
        the file declared no coordinate system, none was supplied, and the
        format is not one that legitimately omits it: the features were
        parsed but their coordinates are in unknown units, ``source_crs`` is
        empty, and writing them is the corruption this contract exists to
        prevent.

    Raises:
        FileNotFoundError: if *path* does not exist (file or directory), or
            a MapInfo .tab was delivered without its .map / .dat.
        ValueError: if *source_epsg* is not an integer in 1024..32767.
        NotImplementedError: for a Geosoft .gdb or a MapInfo RASTER .tab.
        Exception: re-raises fatal GeoPandas/pyogrio read errors.
    """
    import geopandas as gpd  # deferred — avoids import cost in non-GIS envs

    source_epsg = _validate_source_epsg(source_epsg)

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Path not found at '{path}'")

    # Surpac strings return before GeoPandas is used at all: there is no OGR
    # driver for the format, so gpd.read_file below cannot open one. Placed
    # after the EPSG validation and the existence check so a bad override or a
    # missing file still fails the same way it does for every other format.
    if p.suffix.lower() == ".str":
        return _parse_surpac_strings(path, source_epsg=source_epsg)

    # --- Provenance ---
    sha256_hex = _sha256_path(path)
    _provenance: dict[str, Any] = {
        "source_file": path,
        "source_file_sha256": sha256_hex,
        "parser_name": "spatial_parser",
        "parser_version": PARSER_VERSION,
        "source_col_map": {},  # populated after GDF columns are known
    }

    warnings_out: list[dict] = []
    deferred_capabilities: list[str] = []

    # Detect OGR driver from extension
    detected_driver = _detect_format(path)
    ext = os.path.splitext(path)[1].lower()

    # Repair mis-cased sidecars BEFORE anything stats or opens the file:
    # a .prj GDAL cannot see is a .prj the checks below would report as
    # absent, and the CRS refusal would then reject a file whose coordinate
    # system was delivered.
    repaired_sidecars = _materialise_case_variant_sidecars(path, ext)
    if repaired_sidecars:
        _provenance["sidecars_case_repaired"] = repaired_sidecars

    # Determine source_format label (human-readable)
    if ext == ".shp":
        source_format = "shapefile"
        shp_stem = os.path.splitext(path)[0]
        prj_path = shp_stem + ".prj"
        if not os.path.isfile(prj_path):
            warnings_out.append({
                "code": "prj_missing",
                "message": f"no .prj sidecar for {os.path.basename(path)}; CRS unknown",
                "detail": (
                    f"{os.path.basename(path)} arrived without its .prj file, "
                    "so it declares no coordinate system. Re-upload the "
                    "shapefile with all of its sidecars, or supply the EPSG "
                    "code at upload time."
                ),
                "context": {"shapefile": path, "expected_prj": prj_path},
            })
            logger.warning(
                "spatial_parser: no .prj sidecar found for '%s' — CRS will be None", path
            )
        # A missing .dbf is a DEGRADED ingest, not a clean one: the geometry
        # survives and every attribute is gone. GDAL does not complain —
        # measured, a .shp with no .dbf reads as columns ['geometry'] — and
        # no in-band signal can tell "no .dbf" from "a .dbf with one useless
        # column", because a dBASE table always has at least one field. So
        # the discriminator is the stat, exactly as it is for the .prj above.
        dbf_path = shp_stem + ".dbf"
        if not os.path.isfile(dbf_path):
            warnings_out.append({
                "code": "dbf_missing",
                "message": (
                    f"no .dbf sidecar for {os.path.basename(path)}; "
                    "attributes not imported"
                ),
                "detail": (
                    f"{os.path.basename(path)} arrived without its .dbf file. "
                    "The shapes were imported but every attribute — names, "
                    "codes, descriptions — is missing. If the delivery "
                    "includes the .dbf, re-upload the shapefile with all of "
                    "its sidecars to get them; some archives genuinely ship "
                    "geometry-only, and then the shapes are all there is."
                ),
                "context": {"shapefile": path, "expected_dbf": dbf_path},
            })
            logger.warning(
                "spatial_parser: no .dbf sidecar for '%s' — attributes lost", path
            )
    elif ext in (".geojson", ".json"):
        source_format = "geojson"
    elif ext == ".gpkg":
        source_format = "gpkg"
    elif ext == ".gml":
        source_format = "gml"
    elif ext == ".gpx":
        source_format = "gpx"
    elif ext == ".dxf":
        source_format = "dxf"
    elif ext == ".dgn":
        source_format = "dgn"
    elif ext == ".gdb":
        # Shared-extension disambiguation: Esri FileGDB is a DIRECTORY,
        # Geosoft GDB is a single binary FILE. The OpenFileGDB driver
        # only handles Esri FileGDB; pointed at a Geosoft GDB it crashes
        # with a cryptic "Cannot open" deep in pyogrio.
        if p.is_dir():
            source_format = "filegdb"
        else:
            # Geosoft GDB: binary Oasis montaj format. Not openly
            # parseable — Geosoft publishes data via their proprietary
            # API or via the open XYZ export (handled by bronze_xyz +
            # silver_xyz). Refuse with a clear-message error pointing
            # the user at the canonical workaround.
            raise NotImplementedError(
                f"'{p.name}' looks like a Geosoft GDB "
                "(binary file with .gdb extension). Geosoft GDB is not "
                "openly parseable; export to XYZ from Oasis montaj and "
                "upload via the geophysics/ MinIO prefix (silver_xyz) "
                "instead. See georag-architecture.html §11b roadmap."
            )
    elif ext == ".fgb":
        source_format = "flatgeobuf"
    elif ext == ".tab":
        source_format = "mapinfo_tab"
        _inspect_mapinfo_tab(path)
    elif ext == ".mif":
        source_format = "mapinfo_mif"
        # A .mif without its .mid does NOT fail: measured, it reads with the
        # geometry intact and every attribute None. Same bug class as a .shp
        # without its .dbf, and just as invisible, so it is said out loud.
        mid_path = os.path.splitext(path)[0] + ".mid"
        if not os.path.isfile(mid_path):
            warnings_out.append({
                "code": "mid_missing",
                "message": (
                    f"no .mid sidecar for {os.path.basename(path)}; "
                    "attributes not imported"
                ),
                "detail": (
                    f"{os.path.basename(path)} arrived without its .mid file. "
                    "MapInfo keeps the geometry in the .mif and the attributes "
                    "in the .mid, so the shapes were imported and every "
                    "attribute is empty. Re-upload both files together."
                ),
                "context": {"mapinfo_mif": path, "expected_mid": mid_path},
            })
            logger.warning(
                "spatial_parser: no .mid sidecar for '%s' — attributes lost", path
            )
    else:
        source_format = ext.lstrip(".") or "unknown"
        logger.warning(
            "spatial_parser: unrecognised extension '%s' for '%s' — attempting read",
            ext, path,
        )

    # --- Format-specific pre-read behaviour ---

    # DXF: will have no CRS; flag deferred capabilities
    if ext == ".dxf":
        deferred_capabilities.extend(_DEFERRED_DXF)

    # FileGDB: flag deferred capabilities
    if ext == ".gdb":
        deferred_capabilities.extend(_DEFERRED_FILEGDB)

    # --- Read the file ---
    logger.info("spatial_parser: reading %s from '%s'", source_format, path)

    is_multi_layer_driver = detected_driver in _MULTI_LAYER_DRIVERS

    per_layer_columns: dict[str, list[str]] = {}

    if is_multi_layer_driver and ext != ".shp":
        # Use pyogrio to enumerate layers; read all of them
        try:
            gdf, layer_names, per_layer_columns = _read_all_layers(
                path, detected_driver, layer=layer
            )
        except Exception as exc:
            logger.error(
                "spatial_parser: pyogrio layer read failed for '%s': %s", path, exc
            )
            raise

        layer_count = len(layer_names)

        if layer_count > 1:
            total_features_in_layers = len(gdf)
            warnings_out.append({
                "code": "multi_layer_format_detected",
                "message": (
                    f"Format '{source_format}' has {layer_count} layer(s); "
                    f"all features combined with '_layer_name' attribute."
                ),
                "context": {
                    "layers": layer_names,
                    "total_features": total_features_in_layers,
                },
            })
            logger.info(
                "spatial_parser: '%s' has %d layers with %d total features",
                path, layer_count, total_features_in_layers,
            )
    else:
        # Single-layer or Shapefile path — use geopandas directly
        try:
            gdf = gpd.read_file(path)
        except Exception as exc:
            logger.error("spatial_parser: read failed for '%s': %s", path, exc)
            raise
        layer_names = []
        layer_count = 1

    # FileGDB deferred warning (after layer count is known)
    if ext == ".gdb":
        warnings_out.append({
            "code": "filegdb_metadata_deferred",
            "message": (
                "Domain / subtype / relationship-class extraction requires "
                "GDAL Python bindings (Sprint 4b)."
            ),
            "context": {
                "layer_count": layer_count,
                "deferred": _DEFERRED_FILEGDB,
            },
        })

    if gdf.empty:
        logger.warning("spatial_parser: file '%s' contains no features.", path)
        # The CRS is resolved even here. This early return used to hard-code
        # EPSG:4326 and drop crs_confidence entirely, which made an empty
        # .prj-less shapefile indistinguishable from a WGS84 one — a fourth
        # exit quietly disagreeing with the other three.
        gdf, crs_decision = _resolve_crs(gdf, ext, path, source_epsg, warnings_out)
        return SpatialParseResult(
            source_format=source_format,
            source_crs=crs_decision.source_crs,
            feature_count=0,
            empty_geom_skipped=0,
            features=[],
            source_file=os.path.basename(path),
            driver=detected_driver,
            layer_count=layer_count,
            layer_names=layer_names,
            deferred_capabilities=deferred_capabilities,
            dxf_blocks=[],
            warnings=warnings_out,
            crs_confidence=crs_decision.confidence,
            crs_confidence_reason=crs_decision.reason,
            provenance=_provenance,
            is_qfield=False,
            qfield_layers=[],
            qfield_metadata_tables=[],
            crs_missing=crs_decision.missing,
            crs_override_applied=crs_decision.override_applied,
            crs_override_epsg=crs_decision.override_epsg,
        )

    # --- CRS resolution (Section 04b step 3) ---
    #
    # One decision point, not four. See _resolve_crs for the precedence and
    # for why the blanket EPSG:4326 fallback that used to live here had to
    # go.
    gdf, crs_decision = _resolve_crs(gdf, ext, path, source_epsg, warnings_out)
    source_crs = crs_decision.source_crs
    crs_score = crs_decision.confidence
    crs_reason = crs_decision.reason

    # Reproject to WGS84 if necessary (Section 04b step 4).
    #
    # When the CRS is unknown gdf.crs is None and nothing is reprojected, so
    # the WKT below stays in the file's own units. That is deliberate: it is
    # honest about what was read, and crs_missing tells the caller not to
    # store it. Reprojecting from a CRS we do not know is the corruption.
    if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
        logger.info(
            "spatial_parser: reprojecting from %s to EPSG:4326", source_crs
        )
        gdf = gdf.to_crs("EPSG:4326")

    # --- DXF block extraction via ezdxf (Sprint 4b) ---
    dxf_blocks_out: list[dict] = []
    if ext == ".dxf":
        try:
            import ezdxf as _ezdxf_probe  # noqa: F401 — probe import only
            dxf_blocks_out = _extract_dxf_blocks(path)
            # Block extraction succeeded — remove from deferred list
            if "dxf_blocks" in deferred_capabilities:
                deferred_capabilities.remove("dxf_blocks")
            logger.info(
                "spatial_parser: ezdxf extracted %d block(s) from '%s'",
                len(dxf_blocks_out),
                os.path.basename(path),
            )
        except ImportError:
            warnings_out.append({
                "code": "ezdxf_unavailable",
                "message": "DXF blocks not extracted; install ezdxf",
            })
            logger.warning(
                "spatial_parser: ezdxf not available — DXF blocks deferred for '%s'",
                os.path.basename(path),
            )
        except Exception as dxf_exc:
            warnings_out.append({
                "code": "dxf_block_extraction_failed",
                "message": str(dxf_exc),
            })
            logger.warning(
                "spatial_parser: DXF block extraction failed for '%s': %s",
                os.path.basename(path),
                dxf_exc,
            )

    # --- QField detection (GeoPackage only) ---
    qfield_layer_accuracy: dict[str, str | None] = {}
    qfield_metadata_tables: list[str] = []
    is_qfield = False

    if ext == ".gpkg":
        # SQLite metadata probe — populates evidence that this GPKG was
        # authored by QGIS / QField even if no user layer matches the
        # QField attribute schema.
        all_tables = _list_gpkg_sqlite_tables(path)
        qfield_metadata_tables = sorted(
            t for t in all_tables if t in _QFIELD_GPKG_METADATA_TABLES
        )

        # Per-layer schema probe — the strong signal.
        for lname, cols in per_layer_columns.items():
            is_qf_layer, accuracy_col = _detect_qfield_layer(cols)
            if is_qf_layer:
                qfield_layer_accuracy[lname] = accuracy_col

        # A GPKG is "QField" when at least one user layer has the
        # QField shape. QGIS-style metadata tables alone are not enough
        # (lots of QGIS-authored GPKGs aren't field-collection deliverables).
        is_qfield = bool(qfield_layer_accuracy)

        if is_qfield:
            warnings_out.append({
                "code": "qfield_detected",
                "message": (
                    f"QField field-observation layers detected: "
                    f"{sorted(qfield_layer_accuracy)}"
                ),
                "context": {
                    "qfield_layers": sorted(qfield_layer_accuracy),
                    "metadata_tables": qfield_metadata_tables,
                },
            })
            logger.info(
                "spatial_parser: QField layers in '%s' — %s",
                os.path.basename(path),
                sorted(qfield_layer_accuracy),
            )

    total_rows = len(gdf)
    features, empty_geom_skipped, skipped_details = _extract_features(
        gdf, path, feature_type, qfield_layer_accuracy=qfield_layer_accuracy
    )
    feature_count = len(features)

    logger.info(
        "spatial_parser: '%s' — total_rows=%d, parsed=%d, empty_skipped=%d, "
        "source_crs=%s, layers=%d",
        os.path.basename(path),
        total_rows,
        feature_count,
        empty_geom_skipped,
        source_crs,
        layer_count,
    )

    # Populate source_col_map with non-geometry column names
    _provenance["source_col_map"] = {
        col: col for col in gdf.columns if col != "geometry"
    }

    return SpatialParseResult(
        source_format=source_format,
        source_crs=source_crs,
        feature_count=feature_count,
        empty_geom_skipped=empty_geom_skipped,
        features=features,
        source_file=os.path.basename(path),
        driver=detected_driver,
        layer_count=layer_count,
        layer_names=layer_names,
        deferred_capabilities=deferred_capabilities,
        dxf_blocks=dxf_blocks_out,
        skipped_details=skipped_details,
        warnings=warnings_out,
        crs_confidence=crs_score,
        crs_confidence_reason=crs_reason,
        provenance=_provenance,
        is_qfield=is_qfield,
        qfield_layers=sorted(qfield_layer_accuracy),
        qfield_metadata_tables=qfield_metadata_tables,
        crs_missing=crs_decision.missing,
        crs_override_applied=crs_decision.override_applied,
        crs_override_epsg=crs_decision.override_epsg,
    )
