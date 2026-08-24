"""Structural contract tests for INSERT_REPORT_SQL in ingest_pdf.

Motivated by two production bugs of the same shape, found 2026-08-18:

  * `is_scanned` was read off the parse result via
    ``getattr(result, "is_scanned", False)`` against a dataclass that had no
    such field, so every silver.reports row claimed "not scanned".
  * `extraction_confidence` was computed and logged in pdf_report.py but had
    no field on ReportParseResult and no column in this INSERT, so it was
    NULL on every production row and the OCR review-routing signal never had
    any input.

Both were silent: no exception, no failing test, just a column that stayed
empty forever. These tests pin the column/placeholder contract so a field can
not be dropped from the write path again without something going red.

The SQL is read out of the module source rather than imported, so the test
stays runnable without ingest_pdf's heavy transitive imports (hatchet,
georag_object_storage, the azure SDK).
"""

from __future__ import annotations

import re
from pathlib import Path

_INGEST_PDF = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "hatchet_workflows"
    / "ingest_pdf.py"
)


def _insert_report_sql() -> str:
    source = _INGEST_PDF.read_text(encoding="utf-8")
    match = re.search(
        r'INSERT_REPORT_SQL\s*=\s*"""(.*?)"""',
        source,
        re.DOTALL,
    )
    assert match, "INSERT_REPORT_SQL literal not found in ingest_pdf.py"
    return match.group(1)


def _column_list(sql: str) -> list[str]:
    """Column names from the `INSERT INTO silver.reports (...)` clause."""
    match = re.search(
        r"INSERT\s+INTO\s+silver\.reports\s*\((.*?)\)\s*VALUES",
        sql,
        re.DOTALL | re.IGNORECASE,
    )
    assert match, "could not locate the silver.reports column list"
    return [c.strip() for c in match.group(1).split(",") if c.strip()]


def _values_clause(sql: str) -> str:
    match = re.search(r"VALUES\s*\((.*?)\)\s*ON\s+CONFLICT", sql, re.DOTALL | re.IGNORECASE)
    assert match, "could not locate the VALUES clause"
    return match.group(1)


def test_column_count_matches_values_expression_count() -> None:
    """One VALUES expression per column.

    A mismatch here is what turns "I added a column" into a runtime
    asyncpg error, or worse, a silently shifted binding.
    """
    sql = _insert_report_sql()
    columns = _column_list(sql)
    values = _values_clause(sql)

    # Split on commas that are not inside a function call such as ARRAY[]::text[].
    expressions = [v.strip() for v in values.split(",") if v.strip()]

    assert len(columns) == len(expressions), (
        f"{len(columns)} columns but {len(expressions)} VALUES expressions:\n"
        f"columns={columns}\nvalues={expressions}"
    )


def test_placeholders_are_contiguous_from_one() -> None:
    """$1..$N with no gaps and no duplicates beyond intentional reuse.

    The binding order in the `conn.execute(INSERT_REPORT_SQL, ...)` call is
    positional, so a gap means every later argument lands in the wrong column.
    """
    values = _values_clause(_insert_report_sql())
    numbers = sorted({int(n) for n in re.findall(r"\$(\d+)", values)})

    assert numbers, "no $N placeholders found in the VALUES clause"
    assert numbers == list(range(1, len(numbers) + 1)), (
        f"placeholders are not contiguous from $1: {numbers}"
    )


def test_quality_signal_columns_are_written() -> None:
    """The columns that carry ingestion trust signals must be in the INSERT.

    These are the ones that silently stayed NULL in production. NULL here is
    not cosmetic: extraction_confidence feeds OCR review routing, and
    is_scanned drives the scanned-document badge in the UI.
    """
    columns = _column_list(_insert_report_sql())

    for required in ("extraction_confidence", "is_scanned", "parse_quality_pct", "page_count"):
        assert required in columns, (
            f"{required!r} is missing from the silver.reports INSERT — it will "
            f"read NULL on every ingested document"
        )


def test_upsert_refreshes_quality_signals() -> None:
    """Re-ingesting a document must refresh its quality signals, not keep stale ones.

    ON CONFLICT DO UPDATE is the path taken on every re-parse of the same
    report_id, so a column omitted there keeps whatever the first parse wrote.
    """
    sql = _insert_report_sql()
    match = re.search(r"ON\s+CONFLICT.*?DO\s+UPDATE\s+SET(.*)$", sql, re.DOTALL | re.IGNORECASE)
    assert match, "could not locate the ON CONFLICT DO UPDATE clause"
    update_clause = match.group(1)

    for required in ("extraction_confidence", "parse_quality_pct", "is_scanned", "page_count"):
        assert re.search(rf"\b{required}\s*=", update_clause), (
            f"{required!r} is not refreshed on re-ingest; a re-parse would keep "
            f"the value from the first parse"
        )


def test_text_page_coverage_reaches_the_insert() -> None:
    """The extraction number must be written, not just logged.

    parse_quality_pct is NI 43-101 section-heading coverage. It sat alone
    on silver.reports for months while the number people read it as --
    what fraction of pages produced any text -- was computed in
    pdf_report.py and thrown away after one log line. A report whose table
    of contents yielded 17 headings while 300 pages OCR'd to nothing
    therefore stored 1.0 and nothing to contradict it.

    Same failure shape as is_scanned and extraction_confidence above:
    computed, never persisted, silent.
    """
    sql = _insert_report_sql()

    assert "text_page_coverage_pct" in sql, (
        "text_page_coverage_pct is missing from INSERT_REPORT_SQL"
    )
    # And on the UPDATE side -- a re-parse that improves extraction has to
    # be able to correct the number, or the first parse wins forever.
    conflict = sql.split("DO UPDATE SET", 1)[1]
    assert "text_page_coverage_pct = EXCLUDED.text_page_coverage_pct" in conflict


def test_the_two_coverage_numbers_are_both_written() -> None:
    # Pinned together deliberately: either one alone is misleading, and the
    # whole point of storing the second was that the first was being read
    # as an answer to a question it does not answer.
    sql = _insert_report_sql()

    assert "parse_quality_pct" in sql
    assert "text_page_coverage_pct" in sql


def test_page_coverage_counts_pages_that_produced_text() -> None:
    from app.services.ingest.pdf_report import _text_page_coverage

    pages = [(1, "granodiorite"), (2, "   "), (3, ""), (4, "assay"), (5, None)]

    total, with_text, fraction = _text_page_coverage(pages)  # type: ignore[arg-type]

    assert (total, with_text) == (5, 2)
    assert fraction == 0.4


def test_page_coverage_of_nothing_is_zero_not_a_crash() -> None:
    from app.services.ingest.pdf_report import _text_page_coverage

    # The empty-text early return in parse_pdf_report calls this, and a
    # ZeroDivisionError there would turn "we extracted nothing" into a
    # failed workflow rather than a report that says so.
    assert _text_page_coverage([]) == (0, 0, 0.0)
    assert _text_page_coverage(None) == (0, 0, 0.0)
