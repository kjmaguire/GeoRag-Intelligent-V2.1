"""Unit-ambiguity detector — CC-01 Item 1 Slice 2.

Flags assay rows where the unit is missing or ambiguous enough that a
geologist needs to confirm what was actually measured. Output is shaped
for direct insertion into ``silver.review_queue.outlier_flags`` as
``{"unit_ambiguity": ["Au column", "Ag column"]}``.

Three classes of ambiguity caught:

1. **Bare-element columns** (wide format): a column named ``Au``, ``Ag``,
   etc. with no unit suffix. The same header convention covers both
   ``%`` (sulphide concentrates) and ``ppm`` (typical assays); the
   difference is four orders of magnitude.

2. **Missing unit in long format**: a row in long format whose ``unit``
   column is NULL/empty. The parser currently defaults to ``ppm`` with
   a warning; this detector escalates it to a review-queue flag because
   the wrong default on a noble metal materially changes the geology.

3. **Unit cross-mixing**: within the same file, the same element
   appears with inconsistent units (``g/t`` AND ``oz/t`` for Au, or
   ``ppm`` AND ``g/t`` for any noble metal). The MINORITY rows are
   flagged so the reviewer can confirm whether the file is correctly
   mixed-unit or whether a unit-column typo slipped in.

The detector is pure and does not touch the DB. The Dagster asset is
responsible for writing the flagged rows to ``silver.review_queue``
via :mod:`georag_dagster.services.review_queue_writer`.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

# Noble + precious metals — values differ by orders of magnitude across
# unit choices, so ambiguity here is high risk.
NOBLE_METALS: frozenset[str] = frozenset({"Au", "Ag", "Pt", "Pd", "Rh"})

# Units that are ambiguous when used for a noble metal column.
NOBLE_METAL_AMBIGUOUS_UNITS: frozenset[str] = frozenset({
    "ppm",   # could mean g/t at the bench but ppm in the header is fine — flag for confirm
    "g/t",
    "gpt",
    "oz/t",
})

# Base/industrial metals where bare columns (no unit) are usually pct.
BARE_BASE_METAL_DEFAULT_UNIT: dict[str, str] = {
    "Cu": "pct",
    "Pb": "pct",
    "Zn": "pct",
    "Ni": "pct",
    "Fe": "pct",
    "Ti": "pct",
    "Li": "pct",
    "U3O8": "pct",
}

_BARE_ELEMENT_RE = re.compile(r"^([A-Z][a-z]?[A-Z0-9]*?)$")
_ELEMENT_WITH_UNIT_RE = re.compile(
    r"^([A-Z][a-z]?[A-Z0-9]*?)_?(ppm|pct|ppb|gpt|oz_t|g_t)$",
    re.IGNORECASE,
)


def _split_column(col: str) -> tuple[str, str | None]:
    """Split a wide-format assay column into (element, unit_or_None).

    Examples:
        Au_ppm  → ('Au', 'ppm')
        Au      → ('Au', None)
        U3O8    → ('U3O8', None)
    """
    if (match := _ELEMENT_WITH_UNIT_RE.match(col)):
        element = match.group(1)
        unit = match.group(2).lower().replace("_", "")
        # Normalise oz_t / g_t → ozt / gt for downstream comparison.
        unit = {"ozt": "oz/t", "gt": "g/t"}.get(unit, unit)
        return element, unit

    if _BARE_ELEMENT_RE.match(col):
        return col, None

    return col, None


def detect_wide_format(
    assay_columns: list[str],
    records: list[dict[str, Any]],
) -> list[list[str]]:
    """Detect per-record unit ambiguity in wide-format parsed records.

    Returns a list aligned 1:1 with ``records`` — element ``i`` is the
    list of ambiguity flags for record ``i``, or ``[]`` when clean.

    Wide format means each commodity is its own column (``Au_ppm``,
    ``Cu_pct``, ...). The ambiguity rules in this case are:

    * Bare noble-metal column (``Au`` with no ``_ppm`` suffix) — always flag
      because the convention is split across vendors.
    * Bare base-metal column whose value range looks wrong for the
      conventional default (e.g. ``Cu`` value > 100 suggests ppm, not pct).
    """
    if not assay_columns or not records:
        return [[] for _ in records]

    # Pre-classify each column once so the per-row loop stays cheap.
    column_flag_reason: dict[str, str | None] = {}
    for col in assay_columns:
        element, unit = _split_column(col)
        if element in NOBLE_METALS and unit is None:
            column_flag_reason[col] = f"{col}: bare noble-metal column has no unit suffix (ppm vs g/t ambiguous)"
            continue
        column_flag_reason[col] = None

    flags_per_record: list[list[str]] = []
    for rec in records:
        row_flags: list[str] = []
        assays = rec.get("commodity_assays") or {}

        for col, reason in column_flag_reason.items():
            if reason is not None and col in assays:
                row_flags.append(reason)

        # Value-range sanity for bare BASE-metal columns (Cu, Pb, ...) —
        # values > 100 in a column with no unit usually indicate ppm in
        # a column the vendor assumed was pct. Cheap heuristic; reviewers
        # will catch false positives.
        for col, value in assays.items():
            element, unit = _split_column(col)
            if unit is None and element in BARE_BASE_METAL_DEFAULT_UNIT:
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    continue
                if numeric > 100:
                    row_flags.append(
                        f"{col}: bare base-metal column with value {numeric:g} — "
                        f"defaults to {BARE_BASE_METAL_DEFAULT_UNIT[element]} but "
                        f"magnitude suggests ppm"
                    )

        flags_per_record.append(row_flags)

    return flags_per_record


def detect_long_format_units(
    rows_long: list[dict[str, Any]],
    element_col: str,
    unit_col: str | None,
) -> list[list[str]]:
    """Detect ambiguity in long-format raw rows BEFORE pivoting.

    Returns a list aligned with ``rows_long`` — same indexing as the
    parser sees them. Two checks:

    * Missing unit cell on a noble-metal row.
    * Unit cross-mixing: same element appears in the file with
      ``oz/t`` AND ``g/t`` (or ``ppm`` AND ``g/t`` for noble metals).
      The MINORITY-unit rows are flagged.
    """
    if not rows_long:
        return []

    flags_per_row: list[list[str]] = [[] for _ in rows_long]

    # Pass 1 — collect per-element unit distribution.
    element_units: dict[str, Counter] = {}
    for row in rows_long:
        elem = str(row.get(element_col, "") or "").strip()
        if not elem:
            continue
        unit_raw = row.get(unit_col) if unit_col else None
        unit = (str(unit_raw).strip().lower() if unit_raw is not None else "") or ""
        element_units.setdefault(elem, Counter())[unit] += 1

    # Identify cross-mixed elements: > 1 distinct non-empty unit.
    cross_mixed: dict[str, set[str]] = {}
    for elem, counter in element_units.items():
        non_empty = {u for u in counter if u != ""}
        if len(non_empty) > 1:
            cross_mixed[elem] = non_empty

    # For each cross-mixed element pick the MAJORITY unit so we can flag
    # the minority rows specifically (not every row in the column).
    majority_unit: dict[str, str] = {}
    for elem, units in cross_mixed.items():
        counter = element_units[elem]
        # Counter.most_common returns sorted by count desc. Skip the empty
        # key when picking majority.
        for unit, _count in counter.most_common():
            if unit != "":
                majority_unit[elem] = unit
                break

    # Pass 2 — flag rows.
    for idx, row in enumerate(rows_long):
        elem = str(row.get(element_col, "") or "").strip()
        if not elem:
            continue

        unit_raw = row.get(unit_col) if unit_col else None
        unit = (str(unit_raw).strip().lower() if unit_raw is not None else "") or ""

        if unit == "" and elem in NOBLE_METALS:
            flags_per_row[idx].append(
                f"{elem} column: row has no unit — noble metal default is "
                f"ambiguous (ppm vs g/t)"
            )

        if elem in cross_mixed:
            maj = majority_unit.get(elem)
            if maj is not None and unit != "" and unit != maj:
                flags_per_row[idx].append(
                    f"{elem} column: unit '{unit}' differs from file majority "
                    f"'{maj}' — possible unit cross-mixing"
                )

    return flags_per_row


def merge_flags(
    base: list[str],
    extra: list[str],
) -> list[str]:
    """Merge two lists of ambiguity strings, preserving order and dedup."""
    out = list(base)
    seen = set(out)
    for s in extra:
        if s not in seen:
            out.append(s)
            seen.add(s)
    return out


__all__ = [
    "NOBLE_METALS",
    "NOBLE_METAL_AMBIGUOUS_UNITS",
    "BARE_BASE_METAL_DEFAULT_UNIT",
    "detect_wide_format",
    "detect_long_format_units",
    "merge_flags",
]
