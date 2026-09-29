"""Dip sign-convention detection and normalisation.

The silver.collars table enforces dip >= -90 AND dip <= 0 (down-negative).
Some CSV exports use down-positive convention (positive values for downward dip).
This module detects which convention a batch of dip values uses and normalises
them to down-negative before insertion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

#: ``from_vertical`` is an INCLINATION measured from the vertical (0 = a
#: vertical hole, 90 = horizontal), which only an explicitly named column
#: (``Inclination_From_Vertical``) is ever read as — see
#: :func:`resolve_dip_convention`.
DipConvention = Literal["down_negative", "down_positive", "ambiguous", "from_vertical"]

_MINIMUM_SAMPLES = 5
_MAJORITY_THRESHOLD = 0.80


def detect_dip_convention(dips: list[float]) -> DipConvention:
    """Classify the sign convention used in *dips*.

    Heuristic:
    - With at least ``_MINIMUM_SAMPLES`` values, a majority vote:
      > 80 % of values in [-90, 0] -> ``"down_negative"``,
      > 80 % in [0, 90] -> ``"down_positive"``, otherwise ``"ambiguous"``.
    - Below that, only a UNANIMOUS file is classified: every value in
      [-90, 0] -> ``"down_negative"``, every value in [0, 90] ->
      ``"down_positive"``; otherwise ``"ambiguous"``. Either answer is
      low-confidence (:func:`is_low_confidence`) and callers say so.

    Parameters
    ----------
    dips:
        Raw float dip values from the CSV (NaN/None already filtered out
        by the caller before passing).

    Returns
    -------
    DipConvention
    """
    valid = [d for d in dips if d is not None]
    if not valid:
        return "down_negative"
    if len(valid) < _MINIMUM_SAMPLES:
        # Too few values for a majority vote, but not too few to be
        # UNANIMOUS. This branch used to return "down_negative" for any
        # small file, so a 3-hole collar table with dips of +55..+70 was
        # stored positive and hit chk_dip_range for the whole batch, and a
        # 4-station survey at +60 was rejected row by row with no
        # convention warning at all (GIS-14 / ING-12). A small file whose
        # every value is below horizontal in one convention is read in
        # that convention; anything mixed is "ambiguous", so the caller
        # warns instead of silently picking the database's convention.
        if all(-90.0 <= d <= 0.0 for d in valid):
            return "down_negative"
        if all(0.0 <= d <= 90.0 for d in valid):
            return "down_positive"
        return "ambiguous"

    neg = sum(1 for d in valid if -90.0 <= d <= 0.0)
    pos = sum(1 for d in valid if 0.0 <= d <= 90.0)
    total = len(valid)

    if neg / total >= _MAJORITY_THRESHOLD:
        return "down_negative"
    if pos / total >= _MAJORITY_THRESHOLD:
        return "down_positive"
    return "ambiguous"


def normalize_dip(value: float, source_convention: DipConvention) -> float:
    """Return *value* normalised to down-negative convention.

    ``"down_positive"`` flips the sign; ``"from_vertical"`` converts an
    inclination from vertical to a dip from horizontal (``inc - 90``).
    For ``"down_negative"`` or ``"ambiguous"`` the value is returned as-is.

    Parameters
    ----------
    value:
        Raw dip value from the CSV.
    source_convention:
        Convention detected by :func:`detect_dip_convention` or
        :func:`resolve_dip_convention`.
    """
    if source_convention == "down_positive":
        return -value
    if source_convention == "from_vertical":
        # 0 (vertical) -> -90, 30 -> -60, 90 (horizontal) -> 0.
        return value - 90.0
    return value


def is_low_confidence(sample_count: int) -> bool:
    """Whether a convention was decided from fewer values than a vote needs."""
    return 0 < sample_count < _MINIMUM_SAMPLES


# ---------------------------------------------------------------------------
# Header-aware resolution (GIS-14)
# ---------------------------------------------------------------------------

#: Header skeletons (``_header_match.normalize_header``) that say, in so
#: many words, that the angle is measured from the VERTICAL. Only these are
#: converted as inclination: the conversion is unambiguous because the
#: header is.
FROM_VERTICAL_SKELETONS: frozenset[str] = frozenset({
    "inclinationfromvertical", "incfromvertical", "inclfromvertical",
    "anglefromvertical", "inclinationfromvert", "incfromvert",
})

#: Header skeletons that say "inclination" and nothing more. Vendors use the
#: word both for dip-from-horizontal (-60) and for the drilling-industry
#: inclination-from-vertical (30 for the same hole). Negative values settle
#: it; positive values do not, and are warned about rather than converted.
GENERIC_INCLINATION_SKELETONS: frozenset[str] = frozenset({
    "inclination", "inc", "incl",
})

CODE_CONVENTION = "dip_convention_normalized"
CODE_AMBIGUOUS = "dip_convention_ambiguous"
CODE_FROM_VERTICAL = "dip_inclination_from_vertical"
CODE_INCLINATION_AMBIGUOUS = "dip_inclination_ambiguous"


@dataclass
class DipResolution:
    """How a file's dip column is to be read, and what to tell the user."""

    convention: DipConvention
    warnings: list[dict[str, Any]] = field(default_factory=list)


def resolve_dip_convention(
    dips: list[float], *, header: str | None, parser: str,
) -> DipResolution:
    """Decide how *dips* (one file's dip column) map to down-negative dip.

    ``header`` is the column's header as written in the file. It matters
    because the dip aliases include ``Inclination``/``INC``, and an
    inclination measured from vertical read as dip turns a vertical hole
    into a horizontal one.

    * ``Inclination_From_Vertical`` (and the other FROM_VERTICAL_SKELETONS)
      with every value in [0, 90] -> converted, ``dip = inc - 90``.
    * Plain ``Inclination`` / ``INC`` with positive values -> read as dip
      below horizontal, as before, with a ``dip_inclination_ambiguous``
      warning. Converting would be a guess; so would not converting, and
      the warning says which guess was made and how to override it.
    * Everything else -> :func:`detect_dip_convention`, with the result
      reported (and marked ``low_confidence`` for a small file).

    Up-holes (dip above horizontal) cannot be represented: the database
    CHECK is ``dip BETWEEN -90 AND 0``. A fan of up-holes recorded
    down-negative therefore looks down-positive and is flipped; that is a
    schema decision, not something this function can detect.
    """
    from georag_geoparsers._header_match import normalize_header  # noqa: PLC0415

    valid = [d for d in dips if d is not None]
    skeleton = normalize_header(header) if header else ""
    low = is_low_confidence(len(valid))

    if skeleton in FROM_VERTICAL_SKELETONS:
        if valid and all(0.0 <= d <= 90.0 for d in valid):
            return DipResolution("from_vertical", [{
                "row": None,
                "code": CODE_FROM_VERTICAL,
                "message": (
                    f"'{header}' is an inclination from vertical — converted to "
                    f"dip below horizontal (dip = inclination - 90)"
                ),
                "context": {"column": header, "sample_count": len(valid)},
            }])
        return DipResolution("ambiguous", [{
            "row": None,
            "code": CODE_AMBIGUOUS,
            "message": (
                f"'{header}' says it is measured from vertical, but its values "
                f"are not all within 0..90 — not converted"
            ),
            "context": {"column": header, "sample_count": len(valid)},
        }])

    convention = detect_dip_convention(valid)
    warnings: list[dict[str, Any]] = []
    if convention == "down_positive":
        warnings.append({
            "row": None,
            "code": CODE_CONVENTION,
            "message": (
                "detected down_positive dip convention — flipping sign to "
                "down_negative (DB convention)"
                + (f"; decided from only {len(valid)} value(s)" if low else "")
            ),
            "context": {
                "source_convention": convention,
                "sample_count": len(valid),
                "low_confidence": low,
            },
        })
    elif convention == "ambiguous":
        warnings.append({
            "row": None,
            "code": CODE_AMBIGUOUS,
            "message": (
                "dip convention is ambiguous (mix of positive and negative "
                "values) — no sign flip applied; a dip above horizontal "
                "fails the range check"
            ),
            "context": {
                "source_convention": convention,
                "sample_count": len(valid),
                "low_confidence": low,
            },
        })

    if skeleton in GENERIC_INCLINATION_SKELETONS and convention == "down_positive":
        warnings.append({
            "row": None,
            "code": CODE_INCLINATION_AMBIGUOUS,
            "message": (
                f"'{header}' holds positive angles and was read as dip BELOW "
                f"HORIZONTAL (60 = 60 degrees down)"
            ),
            "detail": (
                f"{parser}: the column '{header}' could mean either dip below "
                f"horizontal or inclination from VERTICAL (0 = a vertical "
                f"hole). The values were read as dip below horizontal. If "
                f"they are measured from vertical, every hole is drawn at the "
                f"complement of its true angle — a vertical hole as "
                f"horizontal. Rename the header to "
                f"'Inclination_From_Vertical' and re-upload to have them "
                f"converted."
            ),
            "context": {"column": header, "sample_count": len(valid)},
        })

    return DipResolution(convention, warnings)
