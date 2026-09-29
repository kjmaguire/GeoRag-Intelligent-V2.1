"""UBC-GIF DCIP2D exports as ONE ingest member (ING-19, 2026-09-29).

A DCIP2D inversion export is a DIRECTORY — observed ``.rdt*`` splits,
``dcinv2d.NNN`` / ``ipinv2d.NNN|chg`` models and an ``.inp`` control file,
plus the mesh and topography files the ``.inp`` names when they were
delivered. No one file is readable on its own (``dcip2d_survey`` explains
why each piece needs the others), so:

* ``ingest_zip_archive`` finds each export directory in an archive
  (:func:`find_dcip_exports`), claims exactly its files, re-zips them keeping
  their paths, and dispatches the bundle to ``ingest_geophysics`` — the move
  the ``.shp`` branch already makes for a shapefile's sidecars;
* ``ingest_geophysics`` unpacks the bundle (:func:`extract_bundle`) and
  reads every export directory in it.

Pure file handling, no Hatchet or database import, so the rules are testable
anywhere.
"""

from __future__ import annotations

import logging
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("georag.ingest.dcip_bundle")

#: Observed-data splits: ``.rdt`` and its ``.rdtmd`` / ``.rdtmm`` / ``.rdtmp``
#: siblings — the same prefix test ``dcip2d_survey`` applies.
DCIP_OBSERVED_PREFIX = ".rdt"

#: Model and model-input files. ``.msh`` / ``.con`` are INPUTS the survey
#: reader deliberately does not read as results; they travel with the bundle
#: so its "mesh delivered?" check can see them.
DCIP_MODEL_NAME = re.compile(r"^(dcinv2d|ipinv2d)\.(\d{3}|chg|msh|con)$", re.IGNORECASE)

#: Bound on one bundle's extracted size. The bundle is built by this
#: platform's own archive workflow; this only stops a hand-made bomb.
MAX_BUNDLE_BYTES = 2 * 1024 ** 3


@dataclass(frozen=True)
class DcipExport:
    """One DCIP2D export directory and the files it owns."""

    directory: Path
    members: tuple[Path, ...]


def is_observed_file(path: Path) -> bool:
    return path.suffix.lower().startswith(DCIP_OBSERVED_PREFIX)


def inp_named_files(inp: Path) -> set[str]:
    """Lower-cased basenames of the mesh / topography files an ``.inp`` names.

    An unreadable ``.inp`` names nothing here; it still travels with the
    bundle, where ``dcip2d_survey`` reads it again and reports the problem.
    """
    from georag_geoparsers.dcip2d_parser import INP_UNSET, read_inp  # noqa: PLC0415

    try:
        manifest = read_inp(inp)
    except (OSError, ValueError) as exc:
        log.warning("dcip_bundle: could not read %s: %s", inp.name, exc)
        return set()
    names: set[str] = set()
    for label in ("mesh", "topography"):
        value = (manifest.get(label) or "").strip()
        if value and value != INP_UNSET:
            names.add(value.replace("\\", "/").rsplit("/", 1)[-1].lower())
    return names


def find_dcip_exports(files: list[Path]) -> tuple[list[DcipExport], list[Path]]:
    """Split ``files`` into DCIP2D export directories and everything else.

    A directory is an export when it holds an observed-data ``.rdt*`` file.
    It claims its ``.rdt*`` splits, its ``dcinv2d.*`` / ``ipinv2d.*`` files,
    its ``.inp`` control file(s) and the mesh / topography files an ``.inp``
    names — nothing else, so a PDF report or a station spreadsheet in the
    same folder still takes its own route. Order is preserved for the rest.
    """
    export_dirs = sorted({f.parent for f in files if f.is_file() and is_observed_file(f)})
    if not export_dirs:
        return [], list(files)

    exports: list[DcipExport] = []
    claimed: set[Path] = set()
    for directory in export_dirs:
        siblings = [f for f in files if f.is_file() and f.parent == directory]
        named: set[str] = set()
        for inp in (f for f in siblings if f.suffix.lower() == ".inp"):
            named |= inp_named_files(inp)
        owned = tuple(
            f for f in siblings
            if is_observed_file(f)
            or f.suffix.lower() == ".inp"
            or DCIP_MODEL_NAME.match(f.name)
            or f.name.lower() in named
        )
        claimed.update(owned)
        exports.append(DcipExport(directory=directory, members=owned))
    return exports, [f for f in files if f not in claimed]


def relative_dir(export: DcipExport, root: Path) -> str:
    """The export directory's path inside the archive, POSIX-style ('.' at root)."""
    if export.directory.is_relative_to(root):
        return export.directory.relative_to(root).as_posix()
    return export.directory.name


def survey_name_for(source_name: str, export_dir: Path, bundle_root: Path, line_id: str) -> str:
    """``"<line> DCIP2D — <archive>/<path>"``: the survey's upsert name.

    Stable across re-uploads of the same archive (so they replace), distinct
    for two exports of the same line in different folders or archives (so
    they do not overwrite each other). Never derived from the line alone —
    ``to_geophysics_survey_payload`` explains why that collides.
    """
    rel = (
        export_dir.relative_to(bundle_root).as_posix()
        if export_dir.is_relative_to(bundle_root)
        else export_dir.name
    )
    where = source_name if rel in ("", ".") else f"{source_name}/{rel}"
    return f"{line_id} DCIP2D — {where}"


def write_bundle(export: DcipExport, root: Path, bundle: Path) -> None:
    """Zip one export, keeping each member's path relative to ``root``.

    The directory names matter: ``dcip2d_survey`` takes the line from a
    directory named ``L3750N`` (or its parent) when no title names it.
    """
    rel = relative_dir(export, root)
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as zf:
        for member in export.members:
            zf.write(member, arcname=member.name if rel == "." else f"{rel}/{member.name}")


def extract_bundle(zip_path: Path, dest: Path) -> list[Path]:
    """Unpack a bundle; return every directory holding an ``.rdt*`` file.

    Zip-slip and size guarded. Directories come back sorted so the survey
    names derived from them are stable across runs.
    """
    total = 0
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            total += info.file_size
            if total > MAX_BUNDLE_BYTES:
                raise ValueError("geophysics bundle exceeds the extraction size bound")
            target = (dest / info.filename).resolve()
            if root not in target.parents:
                raise ValueError(f"geophysics bundle member escapes the bundle: {info.filename!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                while chunk := src.read(1 << 20):
                    out.write(chunk)
    return sorted({p.parent for p in dest.rglob("*") if p.is_file() and is_observed_file(p)})


__all__ = [
    "DCIP_MODEL_NAME",
    "DCIP_OBSERVED_PREFIX",
    "DcipExport",
    "extract_bundle",
    "find_dcip_exports",
    "inp_named_files",
    "is_observed_file",
    "relative_dir",
    "survey_name_for",
    "write_bundle",
]
