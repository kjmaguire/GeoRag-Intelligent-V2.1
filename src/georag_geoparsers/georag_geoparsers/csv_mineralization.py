"""CSV Mineralization Parser — Bronze → Silver ingestion for mineralization logs.

Targets the ``silver.mineralization`` COLUMNS (created by
2026_05_20_060400_create_silver_geological_singulars):

    from_depth, to_depth   NOT NULL, to_depth > from_depth
    mineral                NOT NULL, free text
    abundance_pct          numeric, CHECK 0-100 or NULL
    form                   free text (style / habit)
    grain_size             free text
    notes

The same parser reads two kinds of table (see ``_geology_interval``): a
standalone mineralization log, and the mineral columns of a lithology or
alteration log (``companion=True``), so one geology log feeds
``silver.lithology_logs`` and ``silver.mineralization`` from the same rows.

Columns: ``Mineral1`` / ``Min1`` / ``Mineral`` (a named mineral; ``Mineral2``
... for more than one per interval) with ``Min1_%`` / ``Mineral1_Pct`` (its
percentage), ``Min_Style`` / ``Mineral_Style`` (form), ``Min1_Grain_Size``,
``Sulphide%`` (a percentage of a mineral GROUP), and a free-text
``Mineralization`` column (stored verbatim as the mineral, and reported).

What is refused rather than guessed - percentages that are not plain numbers
(``trace``, ``<1``, ``3-5``), intensity (no column: kept in notes), a
percentage that cannot be tied to one of several minerals - is described in
``_geology_interval``.
"""

from __future__ import annotations

from pathlib import Path
from typing import IO

from georag_geoparsers._drill_schema import (
    MINERALIZATION_ALIASES,
    MINERALIZATION_REQUIRED,
)
from georag_geoparsers._geology_columns import FAMILY_MINERALIZATION
from georag_geoparsers._geology_interval import FamilyParseResult, parse_family

COLUMN_ALIASES: dict = MINERALIZATION_ALIASES
REQUIRED_FIELDS: frozenset = MINERALIZATION_REQUIRED

PARSER_VERSION = "1.0.0"


class MineralizationParseResult(FamilyParseResult):
    """A completed mineralization parse."""


def parse_csv_mineralization(
    source: str | Path | IO,
    *,
    null_values: list | None = None,
    vendor_aliases: dict[str, list[str]] | None = None,
    companion: bool = False,
) -> MineralizationParseResult:
    """Parse a mineralization table into a :class:`MineralizationParseResult`.

    ``companion=True`` reads the mineral columns of a table that is primarily
    something else: rows with no mineralization are not-applicable rather than
    rejected, and generic ``Comments`` / ``Description`` columns are left to
    the primary table.
    """
    result = parse_family(
        source,
        family=FAMILY_MINERALIZATION,
        base_aliases=COLUMN_ALIASES,
        parser_name="csv_mineralization",
        parser_version=PARSER_VERSION,
        null_values=null_values,
        vendor_aliases=vendor_aliases,
        companion=companion,
    )
    return MineralizationParseResult(**result.__dict__)
