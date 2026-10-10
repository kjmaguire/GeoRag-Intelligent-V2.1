"""What a same-named curve from ANOTHER LAS file does to a stored one (audit finding 5).

``silver.well_log_curves`` is unique on ``(collar_id, curve_name)``, and both
LAS writers resolved a clash silently: ``las_ingester._insert_curve`` with
``ON CONFLICT DO UPDATE``, ``ingest_well_logs`` with a ``DELETE`` of the names
the file carries. That is right for a re-upload of the SAME file and wrong for
two different tool runs down one hole, which routinely both carry GAMMA: the
second run replaced the first with nothing saying so.

Policy (one decision per curve, ``decide``):

* nothing stored under that name, or the stored one came from the same logical
  file (the upload timestamp aside)  -> write; a re-ingest stays idempotent;
* stored by a DIFFERENT file and the two depth ranges overlap -> replace, and
  say so: ``curve_replaced_from_other_file`` names both files and both
  ranges, and states what depth coverage the replacement gave up;
* stored by a DIFFERENT file and the ranges do NOT overlap -> the two files are
  complementary runs of the same tool string. Replacing would throw one away
  and there is no merge convention in this codebase (one hole holds one curve
  per name), so the new curve is refused and the stored one kept:
  ``curve_replacement_refused``. Concatenating them would be a modelling
  decision (same tool, same units, same calibration?) that ingest does not
  make.

A stored row whose depth unit was never recorded (written before 2026-09-29,
``depth_unit`` NULL) cannot be compared on range, so it is treated as
overlapping and the warning says why.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("georag.ingest.las_curve_conflicts")

#: The upload-stamp rule shared by every ingest workflow (ingest_spatial ING-7).
_UPLOAD_STAMP_RE = re.compile(r"^[0-9]{8}_[0-9]{6}(?:_[0-9]{1,6})?_")

_FEET_TO_METRES = 0.3048

#: Curves named in full in one warning; the rest are counted.
_NAMES_SHOWN = 6


def _shown(name: str | None) -> str:
    """The file name as the user gave it (no upload timestamp)."""
    text = str(name or "").strip()
    return _UPLOAD_STAMP_RE.sub("", text, count=1) or text


def _logical(name: str | None) -> str:
    """The comparison key for "the same file": ``_shown``, case-insensitive."""
    return _shown(name).lower()


@dataclass(frozen=True)
class StoredCurve:
    """The part of a stored curve row a replacement is judged on, in METRES."""

    name: str
    source_file: str | None
    min_depth: float | None       # None: the stored unit was never recorded
    max_depth: float | None


@dataclass(frozen=True)
class CurveDecision:
    """What to do with one incoming curve."""

    action: str                   # "write" | "replace" | "refuse"
    stored: StoredCurve | None = None
    reason: str = ""


async def fetch_stored_curves(
    conn: Any, collar_id: str, curve_names: list[str],
) -> dict[str, StoredCurve]:
    """The curves already stored on *collar_id* under any of *curve_names*."""
    if not curve_names:
        return {}
    rows = await conn.fetch(
        "SELECT curve_name, source_file, min_depth, max_depth, depth_unit "
        "FROM silver.well_log_curves "
        "WHERE collar_id = $1::uuid AND curve_name = ANY($2::text[])",
        collar_id, curve_names,
    )
    out: dict[str, StoredCurve] = {}
    for r in rows:
        unit = r["depth_unit"]
        factor = 1.0 if unit == "m" else _FEET_TO_METRES if unit == "ft" else None
        lo, hi = r["min_depth"], r["max_depth"]
        min_depth: float | None = None
        max_depth: float | None = None
        if factor is not None and lo is not None and hi is not None:
            min_depth, max_depth = float(lo) * factor, float(hi) * factor
        out[r["curve_name"]] = StoredCurve(
            name=r["curve_name"],
            source_file=r["source_file"],
            min_depth=min_depth,
            max_depth=max_depth,
        )
    return out


def decide(
    name: str, stored: StoredCurve | None, *,
    new_file: str, new_min: float, new_max: float,
) -> CurveDecision:
    """Write, replace-and-warn, or refuse one incoming curve (module docstring)."""
    if stored is None:
        return CurveDecision("write")
    if stored.source_file and _logical(stored.source_file) == _logical(new_file):
        return CurveDecision("write", stored, "same file")
    if stored.min_depth is None or stored.max_depth is None:
        return CurveDecision("replace", stored, "the stored depth unit was never recorded")
    if new_min < stored.max_depth and stored.min_depth < new_max:
        return CurveDecision("replace", stored, "overlapping depth ranges")
    return CurveDecision("refuse", stored, "the depth ranges do not overlap")


def _range(lo: float | None, hi: float | None) -> str:
    if lo is None or hi is None:
        return "an unrecorded depth range"
    return f"{lo:g}-{hi:g} m"


def _file(name: str | None) -> str:
    return repr(_shown(name)) if name else "an unrecorded file"


def replacement_warnings(
    decisions: dict[str, CurveDecision],
    ranges: dict[str, tuple[float, float]],
    *, new_file: str, structured: bool = True,
) -> list[dict[str, Any]]:
    """The run's warnings for the curves that replaced or were refused.

    ``ranges`` holds each incoming curve's ``(min, max)`` in metres.
    ``structured=False`` drops the non-string ``curves`` list (the LAS
    ingester types its warnings ``dict[str, str]``).
    """
    out: list[dict[str, Any]] = []
    replaced = {n: d for n, d in decisions.items() if d.action == "replace"}
    refused = {n: d for n, d in decisions.items() if d.action == "refuse"}

    def _line(name: str, d: CurveDecision) -> str:
        stored = d.stored
        if stored is None:      # every replace / refuse decision carries the stored row
            return name
        return (
            f"{name} ({_file(stored.source_file)}, {_range(stored.min_depth, stored.max_depth)} "
            f"-> {_file(new_file)}, {_range(*ranges[name])})"
        )

    if replaced:
        lines = [_line(n, d) for n, d in list(replaced.items())[:_NAMES_SHOWN]]
        more = len(replaced) - len(lines)
        lost = [
            n for n, d in replaced.items()
            if d.stored is not None
            and d.stored.min_depth is not None and d.stored.max_depth is not None
            and (ranges[n][0] > d.stored.min_depth or ranges[n][1] < d.stored.max_depth)
        ]
        warning: dict[str, Any] = {
            "code": "curve_replaced_from_other_file",
            "message": (
                f"{len(replaced)} curve(s) stored from another LAS file were replaced "
                f"by {_file(new_file)}"
            ),
            "detail": (
                f"{len(replaced)} curve(s) of this hole were already stored from a "
                f"DIFFERENT LAS file and were replaced, because one hole holds one "
                f"curve per name: " + "; ".join(lines)
                + (f"; and {more} more" if more else "")
                + ". "
                + (
                    f"For {len(lost)} of them the new file covers less depth than the "
                    f"one it replaced, so part of the earlier coverage is no longer "
                    f"stored ({', '.join(lost[:_NAMES_SHOWN])}); re-upload the earlier "
                    f"file to restore it (that would replace these again). "
                    if lost else ""
                )
                + "If the two files are different tool runs, keep them apart by "
                "renaming the curve in one of them."
            )[:900],
        }
        if structured:
            warning["curves"] = sorted(replaced)
        out.append(warning)
    if refused:
        lines = [_line(n, d) for n, d in list(refused.items())[:_NAMES_SHOWN]]
        more = len(refused) - len(lines)
        warning = {
            "code": "curve_replacement_refused",
            "message": (
                f"{len(refused)} curve(s) of {_file(new_file)} were not stored: the hole "
                f"already holds a curve of that name from another file"
            ),
            "detail": (
                f"{len(refused)} curve(s) were NOT loaded: the hole already holds a "
                f"curve of the same name from a different LAS file, covering a "
                f"different depth range, so the two look like complementary runs: "
                + "; ".join(lines)
                + (f"; and {more} more" if more else "")
                + ". One hole holds one curve per name and merging runs is not done "
                "automatically, so the stored curves were kept. Rename the curve in "
                "one of the files (or remove the stored one) and upload again."
            )[:900],
        }
        if structured:
            warning["curves"] = sorted(refused)
        out.append(warning)
    return out


__all__ = [
    "CurveDecision",
    "StoredCurve",
    "decide",
    "fetch_stored_curves",
    "replacement_warnings",
]
