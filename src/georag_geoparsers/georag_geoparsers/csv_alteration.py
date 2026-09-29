"""CSV Alteration Parser — Bronze → Silver ingestion for alteration logs.

Targets the ``silver.alteration`` COLUMNS (created by
2026_05_20_060400_create_silver_geological_singulars):

    from_depth, to_depth   NOT NULL, to_depth > from_depth
    alteration_type        NOT NULL, free text
    intensity              free text
    minerals               text[]
    notes

The older ``silver.alterations`` (2026_04_09_180400) was dropped by that
migration; it is not a target.

The same parser reads two kinds of table (see ``_geology_interval``): a
standalone alteration log, and the alteration columns of a lithology log
(``companion=True``), so one geology log feeds ``silver.lithology_logs`` and
``silver.alteration`` from the same rows.

Columns: ``Alteration`` / ``Alt`` / ``Alt_Type`` (the type; ``Alt1``, ``Alt2``
for more than one per interval), ``Alt_Intensity`` / ``Alt1_Int``,
``Alt_Minerals``, ``Alt_Comments``. Values are kept as typed; nothing is
mapped onto a vocabulary.
"""

from __future__ import annotations

from pathlib import Path
from typing import IO

from georag_geoparsers._drill_schema import ALTERATION_ALIASES, ALTERATION_REQUIRED
from georag_geoparsers._geology_columns import FAMILY_ALTERATION
from georag_geoparsers._geology_interval import FamilyParseResult, parse_family

COLUMN_ALIASES: dict = ALTERATION_ALIASES
REQUIRED_FIELDS: frozenset = ALTERATION_REQUIRED

PARSER_VERSION = "1.0.0"


class AlterationParseResult(FamilyParseResult):
    """A completed alteration parse."""


def parse_csv_alteration(
    source: str | Path | IO,
    *,
    null_values: list | None = None,
    vendor_aliases: dict[str, list[str]] | None = None,
    companion: bool = False,
) -> AlterationParseResult:
    """Parse an alteration table into an :class:`AlterationParseResult`.

    ``companion=True`` reads the alteration columns of a table that is
    primarily something else (a lithology log): rows with no alteration are
    not-applicable rather than rejected, and generic ``Comments`` /
    ``Description`` columns are left to the primary table.
    """
    result = parse_family(
        source,
        family=FAMILY_ALTERATION,
        base_aliases=COLUMN_ALIASES,
        parser_name="csv_alteration",
        parser_version=PARSER_VERSION,
        null_values=null_values,
        vendor_aliases=vendor_aliases,
        companion=companion,
    )
    return AlterationParseResult(**result.__dict__)
