"""QGIS project files (.qgs / .qgz) — layer manifest plus bundled data.

A QGIS project is not a data file. It is a description of a *map*: which
layers to draw, where each layer's data lives, what CRS to render in, and how
to style it. That distinction drives the whole design here.

What a geologist actually hands you
-----------------------------------
Two shapes, and they need different handling:

  ``.qgs``  A bare XML project. Its ``<datasource>`` entries point at files
            on the geologist's own disk (``../data/collars.shp``,
            ``C:/Projects/Eagle/geology.gpkg``). Those paths do not exist on
            our side, so **no features can be read** — but the manifest is
            still worth having. It tells us the project's CRS, the layer
            names, their geometry types and their declared providers, which
            is exactly the "what is in this project" question a reader asks.

  ``.qgz``  A ZIP containing the ``.qgs`` plus, very often, the data itself
            (QGIS's "package layers" / GeoPackage-backed projects put a
            ``.gpkg`` right beside the project). When the data is in the
            archive, we resolve the datasources against it and parse the
            layers for real.

So this module returns a manifest ALWAYS, and features WHEN THE DATA IS
THERE, and says clearly which happened. Reporting "0 features" for a .qgs
whose data was never uploaded would look like a parse failure; it is not.

Datasource strings are not paths
--------------------------------
QGIS encodes provider-specific connection strings, not filenames:

    ./geology.gpkg|layername=lithology          GPKG, one layer of several
    ./collars.shp                                simple file path
    dbname='georag' host=... table="silver"."collars" (geom)   PostGIS

Only file-backed ones can resolve locally. A PostGIS datasource is recorded
in the manifest and skipped for reading — chasing someone else's database
credentials out of a project file would be both impossible and wrong.

Security
--------
The archive is expanded with an explicit path guard. A ZIP entry named
``../../etc/passwd`` is a real attack against naive extraction (Zip Slip),
and geology data arrives from third parties by definition.
"""

from __future__ import annotations

import logging
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Extensions this module handles.
QGIS_EXTENSIONS = frozenset({".qgs", ".qgz"})

#: Datasource providers whose data lives in a file we might have.
_FILE_PROVIDERS = frozenset({"ogr", "gdal", "delimitedtext", "spatialite"})

#: Hard cap on expanded archive size. A QGZ is normally well under 100 MB;
#: this exists so a zip bomb cannot fill the worker's disk.
_MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB


@dataclass
class QgisLayer:
    """One ``<maplayer>`` entry from the project."""

    name: str
    provider: str | None
    datasource: str
    #: The file portion of the datasource, if it is file-backed.
    datasource_path: str | None
    #: Layer name inside a multi-layer container (``|layername=`` fragment).
    sublayer: str | None
    crs: str | None
    geometry_type: str | None
    #: True once the datasource resolved to a file present in the upload.
    resolved: bool = False
    #: Absolute path it resolved to, for the caller to parse.
    resolved_path: str | None = None

    #: Keepalive for the archive's TemporaryDirectory.
    #:
    #: The natural way to use this API is
    #:
    #:     layers = parse_qgis_project(p).layers
    #:
    #: which drops the result immediately. Without a reference here the
    #: TemporaryDirectory is collected the moment that expression ends, the
    #: extraction tree is deleted, and every ``resolved_path`` points at a
    #: file that existed a microsecond ago. The failure is silent and reads
    #: like a disappearing-file bug rather than a lifetime one, so each layer
    #: keeps the directory alive on its own.
    _tmpdir: Any = field(default=None, repr=False, compare=False)


@dataclass
class QgisProjectResult:
    """Everything we could learn about the project."""

    source_format: str            # "qgs" or "qgz"
    project_title: str | None
    project_crs: str | None
    qgis_version: str | None
    layers: list[QgisLayer] = field(default_factory=list)
    #: Data files found inside a .qgz, whether or not a layer references them.
    bundled_files: list[str] = field(default_factory=list)
    #: Directory the archive was expanded into; None for a bare .qgs. The
    #: caller owns cleanup — it is a TemporaryDirectory kept alive by
    #: ``_tmpdir`` below so resolved_path stays valid for the caller's reads.
    extract_dir: str | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)

    #: Held so the TemporaryDirectory is not garbage-collected (and deleted)
    #: while the caller is still reading resolved_path. Not part of the data.
    _tmpdir: Any = field(default=None, repr=False, compare=False)

    @property
    def resolved_layer_count(self) -> int:
        return sum(1 for lyr in self.layers if lyr.resolved)

    @property
    def is_manifest_only(self) -> bool:
        """True when no layer's data came with the project.

        The caller should say "project catalogued, data not included" rather
        than "0 features parsed" — they are very different outcomes.
        """
        return bool(self.layers) and self.resolved_layer_count == 0


def _safe_extract(archive: Path, dest: Path) -> list[str]:
    """Expand a ZIP, refusing entries that escape *dest* (Zip Slip).

    Returns the extracted member paths, relative to dest.
    """
    written: list[str] = []
    total = 0
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue

            target = (dest / info.filename).resolve()
            if not str(target).startswith(str(dest.resolve())):
                # Not a corrupt-archive edge case — this is the documented
                # Zip Slip attack, and geology data arrives from third
                # parties by definition.
                logger.warning(
                    "qgis_parser: refusing archive member escaping the "
                    "extraction root: %r", info.filename,
                )
                continue

            total += info.file_size
            if total > _MAX_EXPANDED_BYTES:
                logger.warning(
                    "qgis_parser: archive exceeds %d bytes expanded; stopping",
                    _MAX_EXPANDED_BYTES,
                )
                break

            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                out.write(src.read())
            written.append(info.filename)
    return written


def _split_datasource(raw: str) -> tuple[str | None, str | None]:
    """Pull (file path, sublayer) out of a QGIS datasource string.

    Handles the three shapes that actually occur:

        ./geology.gpkg|layername=lithology  -> ("./geology.gpkg", "lithology")
        ./collars.shp                       -> ("./collars.shp", None)
        dbname='x' host=y table="a"."b"     -> (None, None)   # PostGIS
    """
    if not raw:
        return None, None

    text = raw.strip()

    # A database connection string, not a path. Identified by the key=value
    # pairs QGIS uses for the postgres/mssql/oracle providers.
    if re.search(r"\b(dbname|host|service)\s*=", text):
        return None, None

    path_part, _, rest = text.partition("|")
    sublayer = None
    if rest:
        for fragment in rest.split("|"):
            key, _, value = fragment.partition("=")
            if key.strip() in {"layername", "layerName"}:
                sublayer = value.strip()
                break

    path_part = path_part.strip()
    return (path_part or None), sublayer


def _resolve(
    datasource_path: str, roots: list[Path], *, search_tree: bool
) -> Path | None:
    """Find a datasource on disk, tolerating how QGIS wrote the path.

    Exact relative resolution first. The project may then reference
    ``./data/collars.shp`` while the archive stored it as ``collars.shp`` at
    the root, so a basename search is the fallback — but ONLY inside an
    extracted archive (``search_tree=True``), which is self-contained by
    construction.

    For a bare ``.qgs`` the root is whatever directory the file happens to sit
    in, which for an upload is a shared scratch directory. Walking it would be
    slow, would leak across concurrent ingests, and could attach some other
    upload's ``collars.shp`` to this project's layer. Exact-match only there.

    An ambiguous basename is refused rather than guessed: silently binding the
    wrong file to a layer name is worse than reporting the layer unresolved.
    """
    candidate = Path(datasource_path)
    for root in roots:
        exact = (root / candidate).resolve()
        if exact.exists():
            return exact

    if not search_tree:
        return None

    basename = candidate.name
    if not basename:
        return None
    for root in roots:
        matches = [p for p in root.rglob(basename) if p.is_file()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            logger.warning(
                "qgis_parser: %r matches %d files in the archive; not guessing",
                basename, len(matches),
            )
            return None
    return None


def _parse_qgs_xml(
    xml_path: Path, roots: list[Path], *, search_tree: bool
) -> QgisProjectResult:
    """Read the project XML into a manifest, resolving what it can."""
    result = QgisProjectResult(
        source_format="qgs",
        project_title=None,
        project_crs=None,
        qgis_version=None,
    )

    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as exc:
        result.warnings.append({
            "code": "project_xml_unparseable",
            "detail": str(exc)[:300],
        })
        return result

    root = tree.getroot()
    result.qgis_version = root.get("version")
    result.project_title = root.get("projectname") or None

    title_el = root.find("title")
    if title_el is not None and (title_el.text or "").strip():
        result.project_title = title_el.text.strip()

    # Project CRS lives under projectCrs/spatialrefsys; the authid
    # ("EPSG:26913") is the only part worth keeping.
    authid = root.find(".//projectCrs/spatialrefsys/authid")
    if authid is not None and authid.text:
        result.project_crs = authid.text.strip()

    for maplayer in root.iter("maplayer"):
        name_el = maplayer.find("layername")
        datasource_el = maplayer.find("datasource")
        provider_el = maplayer.find("provider")
        layer_authid = maplayer.find(".//srs/spatialrefsys/authid")

        datasource = (datasource_el.text or "").strip() if datasource_el is not None else ""
        path_part, sublayer = _split_datasource(datasource)

        layer = QgisLayer(
            name=(name_el.text or "").strip() if name_el is not None else "(unnamed)",
            provider=(provider_el.text or "").strip() if provider_el is not None else None,
            datasource=datasource,
            datasource_path=path_part,
            sublayer=sublayer,
            crs=(layer_authid.text or "").strip() if layer_authid is not None else None,
            geometry_type=maplayer.get("geometry"),
        )

        if path_part and (layer.provider or "ogr") in _FILE_PROVIDERS:
            found = _resolve(path_part, roots, search_tree=search_tree)
            if found is not None:
                layer.resolved = True
                layer.resolved_path = str(found)

        result.layers.append(layer)

    if result.is_manifest_only:
        result.warnings.append({
            "code": "data_not_bundled",
            "detail": (
                "The project references "
                f"{len(result.layers)} layer(s) but none of their data files "
                "were included. Catalogued the layer manifest; upload the "
                "data (or export the project as a .qgz with layers packaged) "
                "to ingest features."
            ),
        })

    return result


def parse_qgis_project(path: str) -> QgisProjectResult:
    """Parse a ``.qgs`` or ``.qgz`` into a layer manifest.

    For a ``.qgz`` the archive is expanded to a temporary directory and any
    bundled data files are resolved, so ``layer.resolved_path`` can be handed
    straight to ``spatial_parser.parse_spatial_file``. The temporary directory
    stays alive for as long as the returned result is referenced.

    Raises:
        FileNotFoundError: if *path* does not exist.
        ValueError: if the extension is not .qgs/.qgz, or a .qgz contains no
            project XML at all.
    """
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(path)

    suffix = src.suffix.lower()
    if suffix not in QGIS_EXTENSIONS:
        raise ValueError(f"not a QGIS project file: {path}")

    if suffix == ".qgs":
        # search_tree=False: a bare .qgs sits in a shared scratch dir; see
        # _resolve for why walking it would be wrong.
        result = _parse_qgs_xml(src, roots=[src.parent], search_tree=False)
        result.source_format = "qgs"
        return result

    # .qgz — a ZIP holding the .qgs and (usually) its data.
    tmpdir = tempfile.TemporaryDirectory(prefix="qgz_")
    dest = Path(tmpdir.name)
    try:
        members = _safe_extract(src, dest)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"unreadable .qgz archive: {exc}") from exc

    qgs_files = sorted(dest.rglob("*.qgs"))
    if not qgs_files:
        raise ValueError(
            ".qgz contains no .qgs project file "
            f"(members: {', '.join(members[:10]) or 'none'})"
        )
    if len(qgs_files) > 1:
        logger.info(
            "qgis_parser: %d .qgs files in archive; using %s",
            len(qgs_files), qgs_files[0].name,
        )

    result = _parse_qgs_xml(
        qgs_files[0], roots=[qgs_files[0].parent, dest], search_tree=True
    )
    result.source_format = "qgz"
    result.extract_dir = str(dest)
    # Keep the extraction tree alive for as long as EITHER the result or any
    # single layer taken off it is still referenced. See QgisLayer._tmpdir.
    result._tmpdir = tmpdir
    for layer in result.layers:
        layer._tmpdir = tmpdir
    result.bundled_files = [
        m for m in members if not m.lower().endswith((".qgs", ".qgd"))
    ]
    if len(qgs_files) > 1:
        result.warnings.append({
            "code": "multiple_projects_in_archive",
            "detail": f"{len(qgs_files)} .qgs files found; read {qgs_files[0].name}",
        })
    return result


__all__ = [
    "QGIS_EXTENSIONS",
    "QgisLayer",
    "QgisProjectResult",
    "parse_qgis_project",
]
