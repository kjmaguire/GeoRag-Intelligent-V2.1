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
# least this fraction of the type's REQUIRED_FIELDS appear in the headers.
#
# 0.66 admits 2 of 3 and 3 of 4. It was 0.75 while every schema had four
# required fields; collar dropped to three when `elevation` stopped being
# required (see _drill_schema.COLLAR_REQUIRED), and leaving the threshold
# at 0.75 would have QUIETLY TIGHTENED collar detection from 3-of-4 to
# 3-of-3 — a stricter classifier shipped inside a change whose whole
# purpose was to accept more real files.
#
# The floor under this is _IDENTITY_FIELD below: hole_id must match
# whatever the coverage, so 2-of-3 on collar means hole_id plus one
# coordinate, never two coordinates and no hole.
MIN_REQUIRED_COVERAGE: float = 0.66

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

# Every drill table is keyed by the hole it was logged in. ``hole_id`` is a
# REQUIRED field of all four schemas, and the three interval types resolve
# to a collar through it, so a sheet without it cannot be written as any of
# them however many of the OTHER required fields it happens to share.
#
# Measured on the customer's export_UTM.xls: 24 rows of IP station
# coordinates (Grids_Name, LineNumber, X, Y, Z) scored 3/4 on collar --
# X/Y/Z alias to easting/northing/elevation -- cleared the 0.75 threshold,
# and the collar writer then refused every row for having no hole_id.
# Coverage alone cannot separate those cases, because the one field the
# sheet is missing is the identity. Geophysics station lists, soil grids and
# assay certificates all carry coordinates and no hole.
_IDENTITY_FIELD: str = "hole_id"


def _load_schemas() -> dict[str, tuple[dict[str, list[str]], frozenset[str]]]:
    """The four drill layouts' alias + required sets.

    Read from ``_drill_schema``, which is pure stdlib. This used to import
    all four CSV parsers purely to reach their module-level dicts, which
    dragged polars, geopandas and rasterio into every process that wanted
    to guess a sheet type — and meant one parser failing to import took
    classification down with it.

    Still called rather than inlined so the lazy-import contract the
    callers rely on is unchanged.
    """
    from georag_geoparsers._drill_schema import schemas

    return schemas()


def _normalize_header(h: str) -> str:
    """Normalise a header for alias matching.

    Delegates to ``_header_match.normalize_header`` — the SAME function the
    parsers use. It previously only stripped and lower-cased, on the
    reasoning that "the alias lists are exhaustive enough" and that
    'hole id' vs 'hole_id' was a real distinction. Measured on a customer
    delivery on 2026-08-24, neither held: the alias lists carried no
    spaced spellings at all, and the parser's own matching was stricter
    still, so a sheet the classifier accepted could have every row
    rejected by the writer it was dispatched to.
    """
    from georag_geoparsers._header_match import normalize_header

    return normalize_header(h)


def _alias_skeletons(canonical: str, alias_list: list[str]) -> set[str]:
    """Normalised spellings of one field — the same set the parsers map on."""
    from georag_geoparsers._header_match import alias_skeletons

    return alias_skeletons(canonical, alias_list)


def classify_sheet_type(
    headers: list[str],
    *,
    min_required_coverage: float = MIN_REQUIRED_COVERAGE,
    column_map=None,
) -> tuple[str, float]:
    """Classify an Excel sheet's header row as one of the known types.

    Parameters
    ----------
    headers : list[str]
        The sheet's first-row column names.
    min_required_coverage : float, optional
        Fraction of REQUIRED_FIELDS the winning type must match.
        Defaults to ``MIN_REQUIRED_COVERAGE`` (0.66).
    column_map : dict, optional
        A mapping the user confirmed, ``{sheet_type: {field: column}}``.

        Classification has to see it, or the mapping could never take
        effect on the sheets that most need one: a sheet whose headers we
        do not recognise is classified ``unknown`` and sent to the text
        fallback, so it never reaches the parser the mapping was written
        for. Naming the columns is what makes the sheet classifiable, and
        the same map then resolves them — the classifier and the parser
        agreeing about what a header means is the invariant this whole
        module depends on.

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
        user_fields = (column_map or {}).get(sheet_type) or {}
        # Track which canonical fields matched any alias in the headers.
        matched_canonicals: set[str] = set()
        for canonical, alias_list in aliases.items():
            named = user_fields.get(canonical)
            extra = [named] if isinstance(named, str) and named.strip() else []
            if headers_lower & _alias_skeletons(canonical, [*extra, *alias_list]):
                matched_canonicals.add(canonical)

        required_matched = matched_canonicals & set(required)
        coverage = len(required_matched) / max(1, len(required))
        total = len(matched_canonicals)

        # Checked before the discriminator branch below on purpose: a
        # sheet carrying `sample_type` but no hole is still not a sample
        # sheet, so the lock must not be able to override this.
        if _IDENTITY_FIELD in required and _IDENTITY_FIELD not in matched_canonicals:
            continue

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
    "MIN_REQUIRED_COVERAGE",
    "classify_sheet_type",
]
