"""
2026-08-17 — regression coverage for silver.reports.created_at/updated_at
never being populated on insert.

Background
----------
INSERT_REPORT_SQL never listed created_at/updated_at in its column list or
VALUES clause, and the column had no DB-level default either. Every report
inserted through the live ingest_pdf.py path ended up with NULL created_at
(confirmed live: 35/35 rows in one workspace). Foundry/SourcesController,
Foundry/IngestQualityController, and Foundry/IngestionRunsController all
`ORDER BY created_at DESC` when listing recent reports, so with every row
NULL the ordering was effectively undefined — duplicate/stale-looking
report rows could surface ahead of genuinely recent ones.

Fixed at both layers: a migration adds `DEFAULT NOW()` to the column (so
any writer, present or future, gets a correct value even if it forgets to
set one explicitly), and INSERT_REPORT_SQL now writes NOW() for both
columns explicitly on insert (created_at is deliberately NOT touched in
the ON CONFLICT UPDATE branch — a re-parse of an existing report must not
reset its original creation time).

Style note: pure source-inspection test, matching the existing convention
in test_on_failure_captures_real_error.py — see that file's docstring for
why (Hatchet Task objects aren't practically constructible in a unit test).
"""
from __future__ import annotations

import pathlib
import re

INGEST_PDF_PATH = (
    pathlib.Path(__file__).parents[1] / "app" / "hatchet_workflows" / "ingest_pdf.py"
)


def _insert_report_sql() -> str:
    source = INGEST_PDF_PATH.read_text(encoding="utf-8")
    start = source.index('INSERT_REPORT_SQL = """')
    rest = source[start:]
    end = rest.index('"""', len('INSERT_REPORT_SQL = """'))
    return rest[: end + 3]


def test_insert_report_sql_writes_created_at_and_updated_at():
    sql = _insert_report_sql()

    columns_clause = sql[sql.index("(") : sql.index(")")]
    assert "created_at" in columns_clause, (
        "INSERT_REPORT_SQL must list created_at in its INSERT column list — "
        "without a DB default AND an explicit value here, every inserted "
        "report gets NULL created_at, which breaks every 'ORDER BY "
        "created_at DESC' query the Foundry controllers rely on."
    )
    assert "updated_at" in columns_clause, (
        "INSERT_REPORT_SQL must list updated_at in its INSERT column list "
        "for the same reason as created_at."
    )

    values_clause = sql[sql.index("VALUES") : sql.index("ON CONFLICT")]
    assert re.search(r"NOW\(\)\s*,\s*NOW\(\)", values_clause), (
        "the VALUES clause must supply NOW() for both created_at and "
        "updated_at on insert."
    )


def test_on_conflict_update_does_not_reset_created_at():
    sql = _insert_report_sql()
    update_clause = sql[sql.index("DO UPDATE SET") :]

    assert "created_at" not in update_clause, (
        "the ON CONFLICT DO UPDATE branch must not touch created_at — "
        "a re-parse of an existing report should keep its original "
        "creation time, only updated_at should move forward."
    )
    assert "updated_at     = NOW()" in update_clause or "updated_at = NOW()" in update_clause, (
        "the ON CONFLICT DO UPDATE branch must still bump updated_at on "
        "every re-parse."
    )
