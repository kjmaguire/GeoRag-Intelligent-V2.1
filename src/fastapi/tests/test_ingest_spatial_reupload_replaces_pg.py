"""The replace-on-re-upload prefix regex, evaluated by PostgreSQL itself.

test_ingest_spatial_reupload_replaces.py proves the regex in Python and
guards against Python-only syntax by inspection. This module hands the same
pattern to ``regexp_replace`` on a live server, which is the only real proof
that ``_REPLACE_SQL`` finds a row written under the old timestamped shape.

No table is touched: the predicate's left-hand side is a pure expression,
so it is evaluated over literal values.
"""

from __future__ import annotations

import os

import asyncpg
import pytest

from app.hatchet_workflows import ingest_spatial as module

pytestmark = pytest.mark.integration


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@pytest.fixture
async def pg_conn():
    conn = await asyncpg.connect(_dsn())
    try:
        yield conn
    finally:
        await conn.close()


@pytest.mark.parametrize(
    ("stored", "stable"),
    [
        ("20260902_143012_geology_poly.zip", "geology_poly.zip"),
        ("20260902_143012_123456_geology_poly.zip", "geology_poly.zip"),
        ("20260902_143012_deadbeef_geology_poly.zip", "geology_poly.zip"),
        ("geology_poly.zip", "geology_poly.zip"),
        # The 8-digit date reads as the 8-hex digest variant on both sides;
        # see the sibling unit test for why that is fine.
        (
            "20260902_143012_20240101_120000_survey.zip",
            "120000_survey.zip",
        ),
    ],
)
async def test_postgres_strips_the_same_prefix_python_does(
    pg_conn: asyncpg.Connection,
    stored: str,
    stable: str,
) -> None:
    got = await pg_conn.fetchval(
        "SELECT regexp_replace($1::text, $2::text, '')",
        stored,
        module._LEGACY_SOURCE_FILE_PREFIX,
    )
    assert got == stable


async def test_the_predicate_matches_old_and_new_rows_and_nothing_else(
    pg_conn: asyncpg.Connection,
) -> None:
    """The exact WHERE clause from _REPLACE_SQL, over a literal row set."""
    hits = await pg_conn.fetch(
        """
        SELECT v.source_file
          FROM (VALUES
                  ('20260902_143012_geology_poly.zip'),
                  ('20260902_143012_123456_geology_poly.zip'),
                  ('geology_poly.zip'),
                  ('20260902_143012_geology_line.zip'),
                  (NULL::text)
               ) AS v(source_file)
         WHERE regexp_replace(v.source_file, $2, '') = $1
         ORDER BY 1
        """,
        "geology_poly.zip",
        module._LEGACY_SOURCE_FILE_PREFIX,
    )
    assert [r["source_file"] for r in hits] == [
        "20260902_143012_123456_geology_poly.zip",
        "20260902_143012_geology_poly.zip",
        "geology_poly.zip",
    ]
