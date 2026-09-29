"""Audit RAG-17 (2026-09-29): the collar lookup's fallback no longer swaps
holes.

Branch 3 of query_collar_details was a substring match, first
alphabetically: "BH-1" (stored "BH-01") resolved to BH-10 and "36-108" to
36-1085. It is now an equality on normalize_hole_id(), answered only when
exactly one collar in the project has that form.
"""

from __future__ import annotations

import re

import pytest

from app.agent.tools import _hole_norm_sql, normalize_hole_id, query_collar_details


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BH-01", "BH-1"),
        ("bh 1", "BH-1"),
        ("BH1", "BH-1"),
        ("BH12", "BH-12"),
        ("BH-12", "BH-12"),
        ("PLS22-08", "PLS-22-8"),
        ("PLS-22-08", "PLS-22-8"),
        ("PLS-2-28", "PLS-2-28"),
        ("36-1085", "36-1085"),
        ("36-108", "36-108"),
        ("DDH-000", "DDH-0"),
        ("  --A_7-- ", "A-7"),
    ],
)
def test_normalize_hole_id(raw, expected):
    assert normalize_hole_id(raw) == expected


@pytest.mark.parametrize(
    ("a", "b"),
    [("BH-1", "BH-10"), ("36-108", "36-1085"), ("PLS-22-08", "PLS-2-28")],
)
def test_substrings_and_prefixes_do_not_collide(a, b):
    assert normalize_hole_id(a) != normalize_hole_id(b)


def _pg_regexp_replace(text: str, pattern: str, repl: str) -> str:
    # PostgreSQL writes back-references as \1; Python's re does too.
    return re.sub(pattern, repl, text)


def test_sql_twin_uses_the_same_steps_in_the_same_order():
    """The SQL normaliser must be the Python one, step for step. Parse the
    four regexp_replace calls back out of the SQL and replay them."""
    sql = _hole_norm_sql("hole_id")
    steps = re.findall(r"'([^']*)', '([^']*)', 'g'", sql)
    assert len(steps) == 4
    for raw in ("BH-01", "PLS22-08", "36-1085", "bh 1", "DDH-000"):
        value = raw.upper()
        for pattern, repl in steps:
            value = _pg_regexp_replace(value, pattern, repl)
        assert value.strip("-") == normalize_hole_id(raw), raw


class _Conn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return None

    async def fetch(self, sql, *args):
        return []


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Ctx()


class _Deps:
    def __init__(self, conn):
        self.pg_pool = _Pool(conn)


@pytest.mark.asyncio
async def test_lookup_binds_the_normalised_id_and_has_no_substring_branch():
    conn = _Conn()
    await query_collar_details(
        _Deps(conn), "a0000000-0000-0000-0000-000000000001",
        "762b147e-af53-4593-b569-04ee46f31d97", "bh 01",
    )
    sql, args = conn.calls[0]
    assert "ILIKE" not in sql
    assert args[2:] == ("bh 01", "BH-1")
    # Only answers when the normalised form is unique in the project.
    assert ") = 1" in sql
