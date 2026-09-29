"""Honour a length unit written in a drill-table header.

## Why this exists (GIS-3 / ING-5, 2026-09-29)

``_header_match.normalize_header`` drops a trailing unit token so that
``From_ft``, ``From (m)`` and ``From`` all match ``from_depth``. That is
the right rule for MATCHING and was the wrong one for STORING: nothing
downstream ever looked at the token it threw away, so a US-style log with
``From_ft`` / ``To_ft`` / ``EOH_ft`` landed in metre columns unchanged.
Every interval sat 3.28x too deep, every desurveyed trace ran 3.28x too
long, and nothing warned.

Every silver depth/length column is metres (§04e). So:

* a depth or length column whose header says feet is converted at parse
  time (x 0.3048, the international foot) and the conversion is reported
  as a ``depth_unit_converted`` warning naming the columns;
* a COORDINATE column whose header says feet is NOT converted. A foot
  coordinate belongs to a foot-based CRS (State Plane in US survey feet,
  e.g. EPSG:3736), and the CRS — not this module — says which foot and
  what origin. Converting it here would turn a correct ftUS easting into
  a wrong metre one. It is reported as ``coordinate_unit_feet`` so the
  operator declares the matching EPSG.

A header with no unit token is left alone: the file declares nothing and
metres is the schema's unit. That is the pre-existing behaviour and it is
not changed here.
"""

from __future__ import annotations

from typing import Any

from georag_geoparsers._header_match import header_unit

#: The international foot. The US survey foot (1200/3937 m) differs by two
#: parts per million — 2 mm on a 1,000 m hole — which is immaterial for a
#: depth; for COORDINATES it is not, which is one reason coordinates are
#: never converted here.
FEET_TO_METRES = 0.3048

CODE_DEPTH_UNIT_CONVERTED = "depth_unit_converted"
CODE_COORDINATE_UNIT_FEET = "coordinate_unit_feet"


def feet_fields(
    headers: dict[str, str], fields: tuple[str, ...] | frozenset[str],
) -> dict[str, str]:
    """``{canonical: original_header}`` for the *fields* whose header says feet."""
    return {
        f: headers[f]
        for f in fields
        if f in headers and header_unit(headers[f]) == "ft"
    }


def convert_feet_columns(
    df: Any,
    *,
    columns: dict[str, str],
    headers: dict[str, str],
    fields: tuple[str, ...] | frozenset[str],
    parser: str,
) -> tuple[Any, dict[str, Any] | None]:
    """Multiply every feet-labelled length column of *df* by 0.3048.

    Parameters
    ----------
    df:
        A polars DataFrame read with ``infer_schema=False`` (every cell a
        string), which is how every drill parser reads.
    columns:
        ``{canonical: name of that column IN df}``. The renamed-frame
        parsers pass ``{f: f}``; ``_geology_interval`` passes the original
        header because it never renames.
    headers:
        ``{canonical: header as written in the file}`` — where the unit is
        read from.
    fields:
        The canonical fields that are lengths along the hole.
    parser:
        Named in the warning.

    A cell that does not parse as a number is left exactly as it was, so the
    parser's own numeric-cast rejection still fires on it with the original
    text in the report.

    Returns ``(df, warning_or_None)``.
    """
    import polars as pl  # noqa: PLC0415 — the parsers already hold it

    in_feet = feet_fields(headers, fields)
    in_feet = {f: h for f, h in in_feet.items() if columns.get(f) in df.columns}
    if not in_feet:
        return df, None

    exprs = []
    for field in in_feet:
        name = columns[field]
        col = pl.col(name)
        num = col.cast(pl.String).str.strip_chars().cast(pl.Float64, strict=False)
        exprs.append(
            pl.when(num.is_not_null())
            .then((num * FEET_TO_METRES).cast(pl.String))
            .otherwise(col.cast(pl.String))
            .alias(name)
        )
    df = df.with_columns(exprs)

    described = ", ".join(f"'{h}' -> {f}" for f, h in sorted(in_feet.items()))
    warning = {
        "row": None,
        "code": CODE_DEPTH_UNIT_CONVERTED,
        "message": (
            f"{len(in_feet)} column(s) are labelled in feet and were converted "
            f"to metres (x {FEET_TO_METRES})"
        ),
        "detail": (
            f"{parser}: {described}. The header names feet, and every depth "
            f"in the database is metres, so the values were multiplied by "
            f"{FEET_TO_METRES} (international foot) before they were stored. "
            f"If these columns are in fact metres, rename the header without "
            f"the feet suffix and re-upload."
        ),
        "context": {
            "columns": {f: h for f, h in sorted(in_feet.items())},
            "factor": FEET_TO_METRES,
            "source_unit": "ft",
            "stored_unit": "m",
        },
    }
    return df, warning


def feet_coordinate_warning(
    headers: dict[str, str], *, parser: str,
) -> dict[str, Any] | None:
    """A warning when easting/northing headers say feet; never a conversion."""
    in_feet = feet_fields(headers, ("easting", "northing"))
    if not in_feet:
        return None
    described = ", ".join(f"'{h}'" for _, h in sorted(in_feet.items()))
    return {
        "row": None,
        "code": CODE_COORDINATE_UNIT_FEET,
        "message": "coordinate columns are labelled in feet and were NOT converted",
        "detail": (
            f"{parser}: {described} name feet. Coordinates are stored as the "
            f"file gives them and placed using the coordinate system declared "
            f"for the upload or the project, so that system must itself be in "
            f"feet (for example EPSG:3736, NAD83 / Wyoming East in US survey "
            f"feet). If the declared system is metric the holes will be "
            f"misplaced by a factor of 3.28 — declare the feet-based EPSG code "
            f"with the upload and re-upload."
        ),
        "context": {"columns": dict(sorted(in_feet.items())), "source_unit": "ft"},
    }
