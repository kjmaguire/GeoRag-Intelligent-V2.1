"""Sheet-type classifier for multi-sheet Excel workbooks.

Given a sheet's header row, decides whether the sheet looks like
``collar`` / ``survey`` / ``lithology`` / ``sample`` / ``structure`` /
``alteration`` / ``mineralization`` data — or ``unknown`` if no schema matches
confidently.

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


#: Canonical fields that carry an ORIENTATION. A structural table must have
#: at least one; a bare "Structure" column beside from/to depths is a
#: lithology log's texture descriptor as often as it is a structure log.
_STRUCTURE_ORIENTATION_FIELDS: frozenset[str] = frozenset({
    "true_dip", "true_dip_dir", "alpha_angle", "beta_angle",
})


#: Structural evidence no other drill table ever carries: alpha/beta are
#: measured against the core axis, and a dip DIRECTION is not a hole bearing.
_STRUCTURE_STRONG_FIELDS: frozenset[str] = frozenset({
    "alpha_angle", "beta_angle", "true_dip_dir",
})


def _structure_evidence(
    headers: set[str],
    matched: set[str],
    user_fields: dict,
) -> str:
    """How strongly the headers say "structure": ``""``, ``"explicit"`` or ``"strong"``.

    Both halves are needed, because survey and structure share hole, depth
    and dip: (1) an orientation column, and (2) a column that only a
    structural log has - an explicit structure-type name (``explicit``), or
    alpha / beta / a dip-DIRECTION spelling (``strong``). The weak spellings
    (``Type``, ``Azimuth``) do not count on their own; a column the user
    named for a structure field does, since that is a confirmed statement
    rather than a guess.

    ``strong`` may take a tie from any other type (no lithology or sample
    table has an alpha angle); ``explicit`` takes a tie only from survey (a
    lithology log with a ``Structure`` column and a dip is still lithology).
    """
    from georag_geoparsers._drill_schema import STRUCTURE_SIGNAL_ALIASES

    if not matched & _STRUCTURE_ORIENTATION_FIELDS:
        return ""
    verdict = ""
    for canonical, explicit in STRUCTURE_SIGNAL_ALIASES.items():
        named = user_fields.get(canonical)
        extra = [named] if isinstance(named, str) and named.strip() else []
        if headers & _alias_skeletons(canonical, [*extra, *explicit]):
            if canonical in _STRUCTURE_STRONG_FIELDS:
                return "strong"
            verdict = "explicit"
    return verdict


#: The two geology-log families read by ``_geology_columns``. Neither is ever
#: chosen on coverage alone (see ``classify_sheet_type``).
_KEY_FIELDS: frozenset[str] = frozenset({"hole_id", "from_depth", "to_depth"})

_GEOLOGY_FAMILY_TYPES: dict[str, tuple[str, str]] = {
    "alteration": ("alteration", "alteration_type"),
    "mineralization": ("mineralization", "mineral"),
}


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
        ``sheet_type`` is one of ``collar`` / ``survey`` / ``lithology`` / ``structure``
        / ``sample`` / ``alteration`` / ``mineralization`` / ``unknown``. ``confidence`` is the fraction of
        the winning type's REQUIRED_FIELDS that were matched — 0.0 when
        the result is ``unknown``.

    ``alteration`` and ``mineralization`` need EXPLICIT evidence - a column
    that names the family (``Alteration``, ``Alt_Type``, ``Mineral1``,
    ``Mineralization``, ``Sulphide%``); an intensity, a percentage or a
    ``Comments`` column is what many tables have and proves nothing. They also
    never displace another type: a table with a lithology code AND alteration
    columns is a lithology log (the parsers read the alteration columns of the
    same rows as companions), and only a table with no other reading becomes an
    alteration or mineralization table of its own.
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
    best_matched: set[str] = set()

    for sheet_type, (aliases, required) in schemas.items():
        user_fields = (column_map or {}).get(sheet_type) or {}
        # Track which canonical fields matched any alias in the headers.
        matched_canonicals: set[str] = set()
        family_entry = _GEOLOGY_FAMILY_TYPES.get(sheet_type)
        for canonical, alias_list in aliases.items():
            if family_entry is not None and canonical not in _KEY_FIELDS:
                # Alteration / mineralization columns are read by TOKEN
                # (_geology_columns), not by alias skeleton: the skeleton of
                # "Min1_%" is "min1", the same as the mineral-name column
                # "Min1", so alias matching would call a percentage column a
                # mineral column and classify a table on it.
                continue
            named = user_fields.get(canonical)
            extra = [named] if isinstance(named, str) and named.strip() else []
            if headers_lower & _alias_skeletons(canonical, [*extra, *alias_list]):
                matched_canonicals.add(canonical)

        if family_entry is not None:
            from georag_geoparsers._geology_columns import has_family_evidence

            family, type_field = family_entry
            named_type = user_fields.get(type_field)
            user_named = (
                isinstance(named_type, str) and named_type.strip()
                and _normalize_header(named_type) in headers_lower
            )
            if user_named or has_family_evidence(list(headers), family):
                matched_canonicals.add(type_field)

        required_matched = matched_canonicals & set(required)
        coverage = len(required_matched) / max(1, len(required))
        total = len(matched_canonicals)

        # Checked before the discriminator branch below on purpose: a
        # sheet carrying `sample_type` but no hole is still not a sample
        # sheet, so the lock must not be able to override this.
        if _IDENTITY_FIELD in required and _IDENTITY_FIELD not in matched_canonicals:
            continue

        # Alteration / mineralization: explicit evidence only, and never a
        # tie-break win. Anything already classified keeps its type at equal
        # coverage (a lithology log with an Alteration column stays lithology).
        if family_entry is not None:
            if family_entry[1] not in matched_canonicals:
                continue
            if best_type != "unknown" and coverage <= best_coverage:
                continue

        # Structure shares hole + depth + dip (+ azimuth) with survey, so
        # coverage alone can never tell them apart. It is a candidate only
        # when the headers carry EXPLICIT structural evidence; otherwise the
        # table is left to survey (or whatever else claims it).
        if sheet_type == "structure":
            evidence = _structure_evidence(
                headers_lower, matched_canonicals, user_fields,
            )
            if not evidence:
                continue
            if best_type == "survey" and (
                # A survey_method column is a survey's own fingerprint, so a
                # sheet carrying one stays a survey whatever else it has.
                "survey_method" in best_matched
                # Otherwise the explicit evidence above lets structure take
                # an exact tie with survey (both are 100% covered) - but not
                # a win it has not earned.
                or coverage < best_coverage
            ):
                continue
            if best_type in ("collar", "lithology", "sample") and (
                coverage < best_coverage
                or (coverage == best_coverage and evidence != "strong")
            ):
                # These win a tie unless the evidence is alpha/beta/dip
                # direction: a lithology log with a "Structure" column and a
                # dip is still a lithology log.
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
                best_matched = matched_canonicals
            continue

        if coverage < min_required_coverage:
            continue

        # Structure was screened above; on an exact coverage tie it has
        # already been cleared to take over, so `total` must not be allowed
        # to hand the win back on match count.
        structure_takes_survey_tie = (
            sheet_type == "structure"
            and best_type != "unknown"
            and coverage == best_coverage
        )
        if (
            structure_takes_survey_tie
            or coverage > best_coverage
            or (coverage == best_coverage and total > best_total_matches)
        ):
            best_type = sheet_type
            best_coverage = coverage
            best_total_matches = total
            best_matched = matched_canonicals

    if best_type == "unknown":
        return ("unknown", 0.0)

    return (best_type, best_coverage)


#: How far down a sheet a header row is looked for (ING-13).
HEADER_SCAN_ROWS: int = 15


def detect_header_row(
    rows: list,
    *,
    column_map=None,
    max_scan: int = HEADER_SCAN_ROWS,
) -> int:
    """Index of the row that holds a sheet's column headers (ING-13).

    Row 0 unless row 0 classifies as nothing AND a later row, within the
    first ``max_scan``, classifies as a drill layout - a branded export with
    "Acme Gold Corp - Drill Collar Table" in A1 and the header in row 3 used
    to classify ``unknown`` and reach only the text fallback. The best-scoring
    row wins; on a tie, the earlier one.

    Row 0 is only ever left behind when it matches nothing, so a sheet that
    classifies today classifies identically.
    """
    def cells(row) -> list[str]:
        return [
            str(c).strip() for c in (row or [])
            if c is not None and str(c).strip()
        ]

    if not rows:
        return 0
    first = cells(rows[0])
    if len(first) >= 2 and classify_sheet_type(first, column_map=column_map)[0] != "unknown":
        return 0

    best_index, best_confidence = 0, 0.0
    for index, row in enumerate(rows[1:max_scan], start=1):
        found = cells(row)
        if len(found) < 2:
            continue
        sheet_type, confidence = classify_sheet_type(found, column_map=column_map)
        if sheet_type != "unknown" and confidence > best_confidence:
            best_index, best_confidence = index, confidence
    return best_index


__all__ = [
    "HEADER_SCAN_ROWS",
    "MIN_REQUIRED_COVERAGE",
    "classify_sheet_type",
    "detect_header_row",
]
