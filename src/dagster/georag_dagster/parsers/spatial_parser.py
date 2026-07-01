"""Spatial vector feature parser — Shapefile, GeoJSON, GPKG, DXF, GDB, and more.

Reads vector formats supported by pyogrio/GeoPandas (ESRI Shapefile, GeoJSON,
GeoPackage, GML, DXF, DGN, OpenFileGDB, GPX, FlatGeobuf) and returns
a structured SpatialParseResult with one SpatialFeature per non-empty geometry row.

KML/KMZ support is deferred (V1-roadmap, not implemented) per spec §04d.
Kyle-approved removal 2026-04-20 (Module 3 Phase B Decision B).

Format-specific behaviour:
  - Shapefile: checks for .prj sidecar; emits prj_missing if absent.
  - GeoJSON/GeoJSONSeq: EPSG:4326 by default per RFC 7946.
  - GeoPackage (GPKG): multi-layer; all layers are read, features tagged with
    _layer_name attribute.
  - DXF: no CRS; emits dxf_no_crs; appends "dxf_blocks" to deferred_capabilities.
  - FileGDB (.gdb directory): read-only via pyogrio's OpenFileGDB driver.  Emits
    filegdb_metadata_deferred warning; appends domain/subtype/relationship-class
    extraction to deferred_capabilities (requires GDAL Python bindings, Sprint 4b).
  - FlatGeobuf (.fgb), GPX, GML, DGN: read via pyogrio driver inference.

CRS handling:
  - Source CRS is captured before any transformation.
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PARSER_VERSION = "2.3.0"

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
}

# Formats that support (or may contain) multiple OGR layers.
# KML removed — V1-roadmap deferred per spec §04d (Module 3 Phase B Decision B).
_MULTI_LAYER_DRIVERS = frozenset({"GPKG", "OpenFileGDB", "GML", "GPX"})

# Formats known to have no embedded CRS.
_NO_CRS_DRIVERS = frozenset({"DXF"})

# Deferred capabilities signalled at parse time for Sprint 4b work.
_DEFERRED_DXF = ["dxf_blocks"]
_DEFERRED_FILEGDB = [
    "filegdb_domains",
    "filegdb_subtypes",
    "filegdb_relationship_classes",
]


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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _detect_format(path: str) -> str | None:
    """Map file extension (case-insensitive) to an OGR driver name.

    Returns None if the extension is not in the known map.
    """
    ext = os.path.splitext(path)[1].lower()
    return _VECTOR_EXTENSIONS.get(ext)


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


def _infer_feature_type(props: dict, path: str) -> str:
    """Best-effort feature type classification from file path and property values.

    The classification is purely heuristic — the Silver asset allows the caller
    to supply an explicit feature_type override that takes precedence.
    """
    lower_path = os.path.basename(path).lower()
    prop_values = " ".join(str(v).lower() for v in props.values() if v is not None)
    combined = f"{lower_path} {prop_values}"

    if "boundary" in combined or "claim" in combined or "tenure" in combined:
        return "boundary"
    if "alteration" in combined or "anomal" in combined:
        return "alteration"
    if "fault" in combined:
        return "fault"
    if "contact" in combined:
        return "contact"
    if "target" in combined:
        return "target"
    return "feature"


def _safe_str(val) -> str | None:
    """Convert a value to string, returning None for Pandas NA / None / empty."""
    if val is None:
        return None
    try:
        import pandas as pd  # noqa: PLC0415 — deferred, only used here
        if pd.isna(val):
            return None
    except (TypeError, ImportError):
        pass
    s = str(val).strip()
    return s if s else None


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
            import pandas as pd  # noqa: PLC0415
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
        from pyproj import CRS  # noqa: PLC0415
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
    import sqlite3  # noqa: PLC0415

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
            try:  # noqa: SIM105
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
    path: str, driver: str | None
) -> tuple[Any, list[str], dict[str, list[str]]]:
    """Read all layers from a multi-layer format using pyogrio.

    Returns ``(combined_gdf, layer_names_list, per_layer_columns)``.
    Each feature gets a '_layer_name' column indicating its source layer.
    ``per_layer_columns`` maps layer-name → original attribute column list
    (excluding 'geometry') so QField detection can inspect per-layer schema
    after the concat squashes columns together.

    Falls back to geopandas.read_file for single-layer paths.
    """
    import geopandas as gpd  # noqa: PLC0415
    import pyogrio  # noqa: PLC0415

    raw_layers = pyogrio.list_layers(path)
    # list_layers returns an ndarray of shape (N, 2): [[name, geom_type], ...]
    # geom_type is None for non-spatial sidecar tables (e.g. QGIS / QField
    # metadata tables in a .gpkg). Those would concat into the combined
    # GeoDataFrame as rows with NaN geometry — skip them.
    layer_names = [
        str(row[0]) for row in raw_layers
        if row[1] is not None and str(row[1]).lower() != "none"
    ]
    per_layer_cols: dict[str, list[str]] = {}

    if len(layer_names) <= 1:
        gdf = gpd.read_file(path)
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

    import pandas as pd  # noqa: PLC0415
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

        if geom is None or geom.is_empty:
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
    import ezdxf  # noqa: PLC0415 — lazy import; non-DXF callers pay no import cost
    from collections import Counter  # noqa: PLC0415

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

def parse_spatial_file(path: str, feature_type: str | None = None) -> SpatialParseResult:
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

    KML/KMZ is NOT supported — deferred to V1-roadmap per spec §04d.
    Kyle-approved 2026-04-20 (Module 3 Phase B Decision B).

    Args:
        path: Absolute path to the file (or directory for .gdb).
        feature_type: If supplied, every feature is tagged with this type
            instead of heuristic inference.  Passed in by the Silver asset
            config so the geologist can override at ingest time.

    Returns:
        SpatialParseResult.  Empty/null geometries are counted in
        empty_geom_skipped and never silently ignored.

    Raises:
        FileNotFoundError: if *path* does not exist (file or directory).
        Exception: re-raises fatal GeoPandas/pyogrio read errors.
    """
    import geopandas as gpd  # deferred — avoids import cost in non-GIS envs

    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"spatial_parser: path not found at '{path}'")

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

    # Determine source_format label (human-readable)
    if ext == ".shp":
        source_format = "shapefile"
        prj_path = os.path.splitext(path)[0] + ".prj"
        if not os.path.isfile(prj_path):
            warnings_out.append({
                "code": "prj_missing",
                "message": f"no .prj sidecar for {os.path.basename(path)}; CRS unknown",
                "context": {"shapefile": path, "expected_prj": prj_path},
            })
            logger.warning(
                "spatial_parser: no .prj sidecar found for '%s' — CRS will be None", path
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
                f"spatial_parser: '{p.name}' looks like a Geosoft GDB "
                "(binary file with .gdb extension). Geosoft GDB is not "
                "openly parseable; export to XYZ from Oasis montaj and "
                "upload via the geophysics/ MinIO prefix (silver_xyz) "
                "instead. See georag-architecture.html §11b roadmap."
            )
    elif ext == ".fgb":
        source_format = "flatgeobuf"
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
            gdf, layer_names, per_layer_columns = _read_all_layers(path, detected_driver)
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
        return SpatialParseResult(
            source_format=source_format,
            source_crs="EPSG:4326",
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
            provenance=_provenance,
            is_qfield=False,
            qfield_layers=[],
            qfield_metadata_tables=[],
        )

    # --- DXF-specific CRS handling ---
    if ext == ".dxf":
        # pyogrio may populate a synthetic CRS for DXF; clear it explicitly
        gdf = gdf.set_crs(None, allow_override=True)
        source_crs = "EPSG:4326"   # placeholder; caller must georeference
        warnings_out.append({
            "code": "dxf_no_crs",
            "message": "DXF files have no CRS; caller must georeference.",
            "context": {"path": path},
        })
        crs_score = 0.0
    else:
        # Capture source CRS before any transformation
        if gdf.crs is not None:
            source_crs = gdf.crs.to_string()
        else:
            source_crs = "EPSG:4326"
            warnings_out.append({
                "code": "crs_unknown",
                "message": (
                    f"'{os.path.basename(path)}' has no CRS defined — assuming EPSG:4326"
                ),
                "context": {"path": path},
            })
            logger.warning(
                "spatial_parser: '%s' has no CRS defined — assuming EPSG:4326", path
            )

        # CRS confidence scoring (Section 04b step 3 heuristic)
        crs_score = 0.0
        try:
            crs_score, crs_reason = _score_crs_confidence(gdf)
            if crs_score < 0.5:
                warnings_out.append({
                    "code": "crs_low_confidence",
                    "message": f"CRS confidence score {crs_score:.1f}: {crs_reason}",
                    "context": {"score": crs_score, "reason": crs_reason},
                })
                logger.warning(
                    "spatial_parser: low CRS confidence (%.1f) for '%s' — %s",
                    crs_score, path, crs_reason,
                )
        except Exception as exc:
            logger.debug("spatial_parser: CRS confidence scoring skipped: %s", exc)

        # Reproject to WGS84 if necessary (Section 04b step 4)
        if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
            logger.info(
                "spatial_parser: reprojecting from %s to EPSG:4326", source_crs
            )
            gdf = gdf.to_crs("EPSG:4326")

    # --- DXF block extraction via ezdxf (Sprint 4b) ---
    dxf_blocks_out: list[dict] = []
    if ext == ".dxf":
        try:
            import ezdxf as _ezdxf_probe  # noqa: PLC0415, F401 — probe import only
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
        provenance=_provenance,
        is_qfield=is_qfield,
        qfield_layers=sorted(qfield_layer_accuracy),
        qfield_metadata_tables=qfield_metadata_tables,
    )
