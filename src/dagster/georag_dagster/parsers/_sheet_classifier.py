"""Sheet-type classifier for multi-sheet Excel workbooks.

Given a sheet's header row, decides whether the sheet looks like
``collar`` / ``survey`` / ``lithology`` / ``sample`` data — or
``unknown`` if no schema matches confidently.

Used by ``silver_xlsx`` to auto-dispatch each sheet of a multi-sheet
workbook to the right CSV parser, fixing the silent-data-loss bug where
the asset previously processed only the first sheet.

The classifier reuses the same ``COLUMN_ALIASES`` + ``REQUIRED_FIELDS``
maps the four CSV parsers already maintain — no duplicate alias lists.

Scoring strategy (per sheet_type):

1. For each canonical field in the type's schema, check whether any of
   its aliases appears in the headers (case-insensitive).
2. Count matches against the type's REQUIRED_FIELDS specifically — this
   is the primary signal because REQUIRED is what makes the type the
   type. A sheet that has 4/4 collar required fields IS a collar sheet.
3. Tie-break on total alias matches (collar with all 4 required + 3
   optional matched beats lithology with 4/4 required + 0 optional).
4. Apply a coverage threshold (``MIN_REQUIRED_COVERAGE``) below which
   the sheet is classified ``unknown``. Default 0.75 — 3/4 required for
   the typical 4-field sheets.

Returns ``(sheet_type, confidence)`` where confidence ∈ [0.0, 1.0] is
the fraction of REQUIRED_FIELDS that matched in the winning type.

NOTE: Do NOT add ``from __future__ import annotations`` — Dagster
Config classes downstream rely on runtime annotations.
"""

import logging

logger = logging.getLogger(__name__)

# Coverage threshold — a sheet classifies as a known type only when at
# least this fraction of the type's REQUIRED_FIELDS appear in the
# headers. Default 0.75 means 3/4 of required fields must match.
MIN_REQUIRED_COVERAGE: float = 0.75

# Discriminator fields that, when present, lock the classification
# regardless of overall coverage. Lets us correctly classify a sheet
# with most-required-fields-but-renamed-headers (e.g. an older template
# where 'Easting' was 'X_coord' which IS in our aliases under "X").
_HARD_DISCRIMINATORS: dict[str, set[str]] = {
    # 'lithology_code' / 'sample_type' / 'survey_method' are unique to
    # their respective schemas. If any of these match an alias, we lock.
    "lithology": {"lithology_code"},
    "sample":    {"sample_type"},
    "survey":    {"survey_method"},
    # 'collar' has no truly unique field (hole_id is shared, easting/
    # northing/elevation also appear in some asset templates). Rely on
    # required-coverage scoring for collar.
}


def _load_schemas() -> dict[str, tuple[dict[str, list[str]], frozenset[str]]]:
    """Lazy-import the four CSV parsers to grab their alias + required sets.

    Done lazily inside the function so any single parser's import failure
    doesn't crash the classifier at module-load time.
    """
    from georag_dagster.parsers.csv_collar import (  # noqa: PLC0415
        COLUMN_ALIASES as COLLAR_ALIASES,
        REQUIRED_FIELDS as COLLAR_REQUIRED,
    )
    from georag_dagster.parsers.csv_survey import (  # noqa: PLC0415
        COLUMN_ALIASES as SURVEY_ALIASES,
        REQUIRED_FIELDS as SURVEY_REQUIRED,
    )
    from georag_dagster.parsers.csv_lithology import (  # noqa: PLC0415
        COLUMN_ALIASES as LITH_ALIASES,
        REQUIRED_FIELDS as LITH_REQUIRED,
    )
    from georag_dagster.parsers.csv_sample import (  # noqa: PLC0415
        COLUMN_ALIASES as SAMPLE_ALIASES,
        REQUIRED_FIELDS as SAMPLE_REQUIRED,
    )

    return {
        "collar":    (COLLAR_ALIASES, COLLAR_REQUIRED),
        "survey":    (SURVEY_ALIASES, SURVEY_REQUIRED),
        "lithology": (LITH_ALIASES, LITH_REQUIRED),
        "sample":    (SAMPLE_ALIASES, SAMPLE_REQUIRED),
    }


def _normalize_header(h: str) -> str:
    """Normalise a header for case-insensitive alias matching.

    Strips whitespace and lowercases. We intentionally do NOT strip
    underscores or non-alpha chars — the alias lists are exhaustive
    enough that exact-lowercase match is sufficient, and over-stripping
    creates false positives ('hole id' vs 'hole_id' is a real distinction
    the parser handles via the alias list).
    """
    return (h or "").strip().lower()


def classify_sheet_type(
    headers: list[str],
    *,
    min_required_coverage: float = MIN_REQUIRED_COVERAGE,
) -> tuple[str, float]:
    """Classify an Excel sheet's header row as one of the known types.

    Parameters
    ----------
    headers : list[str]
        The sheet's first-row column names.
    min_required_coverage : float, optional
        Fraction of REQUIRED_FIELDS the winning type must match.
        Defaults to ``MIN_REQUIRED_COVERAGE`` (0.75).

    Returns
    -------
    (sheet_type, confidence) : tuple[str, float]
        ``sheet_type`` is one of ``collar`` / ``survey`` / ``lithology``
        / ``sample`` / ``unknown``. ``confidence`` is the fraction of
        the winning type's REQUIRED_FIELDS that were matched — 0.0 when
        the result is ``unknown``.
    """
    if not headers:
        return ("unknown", 0.0)

    headers_lower: set[str] = {_normalize_header(h) for h in headers if h}
    if not headers_lower:
        return ("unknown", 0.0)

    try:
        schemas = _load_schemas()
    except Exception as exc:
        logger.warning(
            "_sheet_classifier: failed to load CSV parser schemas — "
            "returning unknown. Error: %s", exc,
        )
        return ("unknown", 0.0)

    best_type: str = "unknown"
    best_coverage: float = 0.0
    best_total_matches: int = 0

    for sheet_type, (aliases, required) in schemas.items():
        # Track which canonical fields matched any alias in the headers.
        matched_canonicals: set[str] = set()
        for canonical, alias_list in aliases.items():
            alias_set_lower = {_normalize_header(a) for a in alias_list}
            if headers_lower & alias_set_lower:
                matched_canonicals.add(canonical)

        required_matched = matched_canonicals & set(required)
        coverage = len(required_matched) / max(1, len(required))
        total = len(matched_canonicals)

        # Hard discriminator override — if a unique-to-this-type field
        # matched, lock the classification regardless of coverage. This
        # rescues sheets where some required fields use exotic header
        # names not in our alias list but the type-distinctive field is
        # present.
        discriminators = _HARD_DISCRIMINATORS.get(sheet_type, set())
        if discriminators & matched_canonicals and coverage >= 0.5:
            # Treat as full confidence on the discriminator side, but
            # report actual required coverage so callers can see it.
            if coverage > best_coverage or (
                coverage == best_coverage and total > best_total_matches
            ):
                best_type = sheet_type
                best_coverage = coverage
                best_total_matches = total
            continue

        if coverage < min_required_coverage:
            continue

        if coverage > best_coverage or (
            coverage == best_coverage and total > best_total_matches
        ):
            best_type = sheet_type
            best_coverage = coverage
            best_total_matches = total

    if best_type == "unknown":
        return ("unknown", 0.0)

    return (best_type, best_coverage)


__all__ = [
    "classify_sheet_type",
    "MIN_REQUIRED_COVERAGE",
]
