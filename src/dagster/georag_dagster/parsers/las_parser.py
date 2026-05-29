"""LAS 2.0 well log parser.

Reads a LAS file using lasio and returns a structured LasParseResult containing
well metadata from the ~W section and one LasCurve per non-depth channel.

Parse quality is defined as (curves successfully parsed) / (curves present).
Malformed or unreadable curves are logged individually and counted; they never
silently drop data.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

PARSER_NAME = "las_parser"
PARSER_VERSION = "1.0.0"


def _sha256_file(path: str) -> str:
    """Stream-hash the file at *path*, returning the hex digest."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class LasCurve:
    """Parsed representation of a single LAS log curve (one channel)."""

    name: str
    unit: str
    description: str
    depths: list[float]
    values: list[float]
    min_depth: float
    max_depth: float
    step: float | None
    null_value: float
    sample_count: int


@dataclass
class LasParseResult:
    """Top-level result returned by parse_las_file."""

    well_name: str | None
    company: str | None
    field_name: str | None
    location: str | None
    las_version: str
    curves: list[LasCurve]
    source_file: str
    depth_curve_name: str   # usually "DEPT" or "DEPTH"
    total_curves_in_file: int
    skipped_curves: int
    parse_quality_pct: float  # 0.0 – 1.0
    skipped_details: list[dict] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_las_file(path: str) -> LasParseResult:
    """Parse a LAS file using lasio and return curves + well metadata.

    Args:
        path: Absolute path to the .las file on the local filesystem.

    Returns:
        LasParseResult with all successfully parsed curves. Curves that fail
        validation are logged and counted in skipped_curves — they are never
        silently dropped.

    Raises:
        FileNotFoundError: if the file does not exist at *path*.
        Exception: re-raises fatal lasio errors (corrupt file, binary LAS, etc.)
    """
    import lasio  # deferred — avoids import cost in environments not running LAS

    logger.info("LAS parser: opening '%s'", path)
    sha256_hex = _sha256_file(path)
    las = lasio.read(path)

    # ------------------------------------------------------------------
    # ~W section (well metadata)
    # lasio exposes header items via las.well; use .get() with a sentinel
    # to avoid AttributeError on missing mnemonics.
    # ------------------------------------------------------------------
    _empty = lasio.HeaderItem()

    def _well_val(mnemonic: str) -> str | None:
        item = las.well.get(mnemonic, _empty)
        val = getattr(item, "value", None)
        if val is None or str(val).strip() == "":
            return None
        return str(val).strip()

    well_name = _well_val("WELL")
    company   = _well_val("COMP")
    field_name = _well_val("FLD")
    location  = _well_val("LOC")

    # NULL value — default -999.25 per LAS 2.0 spec
    null_raw = _well_val("NULL")
    try:
        null_value = float(null_raw) if null_raw is not None else -999.25
    except (ValueError, TypeError):
        null_value = -999.25

    # STEP value — depth increment; may be 0 for irregular spacing
    step_raw = _well_val("STEP")
    try:
        step_value = float(step_raw) if step_raw is not None else None
    except (ValueError, TypeError):
        step_value = None

    # LAS version
    try:
        las_version = str(las.version.VERS.value).strip() if hasattr(las.version, "VERS") else "2.0"
    except Exception:
        las_version = "2.0"

    # ------------------------------------------------------------------
    # Depth curve — always the first curve in the ~C section
    # ------------------------------------------------------------------
    if not las.curves:
        raise ValueError(f"LAS file '{path}' contains no curves in the ~C section.")

    depth_curve_item = las.curves[0]
    depth_curve_name = depth_curve_item.mnemonic

    try:
        depths = las[depth_curve_name].tolist()
    except Exception as exc:
        raise ValueError(
            f"LAS parser: failed to read depth curve '{depth_curve_name}': {exc}"
        ) from exc

    if not depths:
        raise ValueError(f"LAS file '{path}' has an empty depth curve.")

    depth_min = min(depths)
    depth_max = max(depths)

    logger.info(
        "LAS parser: well='%s', depth_curve='%s', depth range=%.2f–%.2f, "
        "total_curves=%d (excluding depth)",
        well_name or "<unknown>",
        depth_curve_name,
        depth_min,
        depth_max,
        len(las.curves) - 1,
    )

    # ------------------------------------------------------------------
    # Log curves (skip the depth curve itself)
    # ------------------------------------------------------------------
    total_curves_in_file = len(las.curves) - 1   # exclude depth curve
    curves: list[LasCurve] = []
    skipped_details: list[dict] = []

    for curve_item in las.curves[1:]:
        curve_name = curve_item.mnemonic
        curve_unit = curve_item.unit or ""
        curve_desc = curve_item.descr or ""

        try:
            raw_values = las[curve_name].tolist()
        except Exception as exc:
            reason = f"lasio read error: {exc}"
            logger.warning(
                "LAS parser: skipping curve '%s' in '%s' — %s",
                curve_name, path, reason,
            )
            skipped_details.append({"curve": curve_name, "reason": reason})
            continue

        if len(raw_values) != len(depths):
            reason = (
                f"length mismatch: curve has {len(raw_values)} samples, "
                f"depth has {len(depths)} samples"
            )
            logger.warning(
                "LAS parser: skipping curve '%s' in '%s' — %s",
                curve_name, path, reason,
            )
            skipped_details.append({"curve": curve_name, "reason": reason})
            continue

        curves.append(
            LasCurve(
                name=curve_name,
                unit=curve_unit,
                description=curve_desc,
                depths=depths,
                values=raw_values,
                min_depth=depth_min,
                max_depth=depth_max,
                step=step_value,
                null_value=null_value,
                sample_count=len(depths),
            )
        )

    skipped_curves = total_curves_in_file - len(curves)
    parse_quality_pct = (
        len(curves) / total_curves_in_file if total_curves_in_file > 0 else 1.0
    )

    if skipped_curves > 0:
        logger.warning(
            "LAS parser: %d of %d curves skipped in '%s' — parse quality %.1f%%",
            skipped_curves,
            total_curves_in_file,
            path,
            parse_quality_pct * 100,
        )
    else:
        logger.info(
            "LAS parser: all %d curves parsed successfully — quality 100%%",
            total_curves_in_file,
        )

    return LasParseResult(
        well_name=well_name,
        company=company,
        field_name=field_name,
        location=location,
        las_version=las_version,
        curves=curves,
        source_file=path.split("/")[-1],
        depth_curve_name=depth_curve_name,
        total_curves_in_file=total_curves_in_file,
        skipped_curves=skipped_curves,
        parse_quality_pct=parse_quality_pct,
        skipped_details=skipped_details,
        provenance={
            "source_file_sha256": sha256_hex,
            "parser_name": PARSER_NAME,
            "parser_version": PARSER_VERSION,
            "source_col_map": None,
        },
    )
