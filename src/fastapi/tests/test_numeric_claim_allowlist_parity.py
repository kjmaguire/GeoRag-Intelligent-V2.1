"""Audit PG-12 (2026-09-29): verify_numerical_claim's column allowlist must
name real NUMERIC columns.

It named four columns no migration creates (silver.samples.sample_length /
.recovery, silver.geochemistry.value / .detection_limit) and one text column
(silver.alteration.intensity). A claim routed to any of them errored at the
database, came back verified=False, counted toward NUMERIC_RETRY_THRESHOLD
and triggered a retry on a correct number.

This checks every allowlisted (table, column) against database/migrations:
the column must be declared with a numeric type in a migration that names
the table. It is a text scan, not a live catalog, so it cannot see a column
dropped later — but every drift found so far was a column that never
existed at all.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.agent.tools import NUMERIC_CLAIM_COLUMNS

_MIGRATIONS = Path(__file__).resolve().parents[3] / "database" / "migrations"

_BLUEPRINT_NUMERIC = (
    r"->(?:float|double|decimal|integer|smallInteger|bigInteger|tinyInteger|"
    r"unsignedInteger|unsignedBigInteger|unsignedSmallInteger|unsignedDecimal)"
    r"\(\s*'{col}'"
)
_SQL_NUMERIC = (
    r"\b{col}\s+(?:numeric|double\s+precision|real|float\d*|integer|int\d?|"
    r"smallint|bigint|decimal)\b"
)


def _migration_texts_for(table: str) -> list[str]:
    schema, name = table.split(".")
    pattern = re.compile(
        rf"\b{schema}\.{name}\b|['\"]{schema}\.{name}['\"]"
    )
    texts = []
    for path in sorted(_MIGRATIONS.glob("*.php")):
        text = path.read_text(errors="replace")
        if pattern.search(text):
            texts.append(text)
    return texts


@pytest.mark.skipif(not _MIGRATIONS.is_dir(), reason="migrations not in this checkout")
@pytest.mark.parametrize(
    ("table", "column"),
    sorted(
        (table, column)
        for table, (_pk, columns, _mode) in NUMERIC_CLAIM_COLUMNS.items()
        for column in columns
    ),
)
def test_allowlisted_column_is_a_real_numeric_column(table: str, column: str):
    texts = _migration_texts_for(table)
    assert texts, f"no migration mentions {table}"
    blueprint = re.compile(_BLUEPRINT_NUMERIC.format(col=re.escape(column)))
    sql = re.compile(_SQL_NUMERIC.format(col=re.escape(column)), re.IGNORECASE)
    assert any(blueprint.search(t) or sql.search(t) for t in texts), (
        f"{table}.{column} is allowlisted for numeric verification but no "
        f"migration declares it as a numeric column"
    )


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("silver.samples", "sample_length"),
        ("silver.samples", "recovery"),
        ("silver.geochemistry", "value"),
        ("silver.geochemistry", "detection_limit"),
        ("silver.alteration", "intensity"),
    ],
)
def test_the_pg12_phantom_columns_are_gone(table: str, column: str):
    assert column not in NUMERIC_CLAIM_COLUMNS[table][1]
