"""Unit tests for plan §2c entity resolver foundation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.agent.entity_resolver import (
    EntityResolution,
    log_alias_gap,
    normalise_entity_text,
    resolve_entity,
)


# ---------------------------------------------------------------------------
# Mock asyncpg connection / pool
# ---------------------------------------------------------------------------


class _MockConn:
    """Tiny asyncpg.Connection stand-in.

    Constructor accepts:
      - ``exact_row``  — return value for the exact-match query
      - ``fuzzy_row``  — return value for the fuzzy-match query
      - ``raise_on_insert`` — boolean; True causes _insert_gap to raise
    """

    def __init__(
        self,
        *,
        exact_row: dict[str, Any] | None = None,
        fuzzy_row: dict[str, Any] | None = None,
        raise_on_insert: bool = False,
    ) -> None:
        self.exact_row = exact_row
        self.fuzzy_row = fuzzy_row
        self.raise_on_insert = raise_on_insert
        self.execute_calls: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[Any, ...]]] = []
        self._fetchrow_pos = 0  # 0 = exact, 1 = fuzzy

    async def execute(self, sql: str, *args):
        self.execute_calls.append((sql, args))
        if "INSERT INTO silver.alias_gaps" in sql and self.raise_on_insert:
            raise RuntimeError("simulated gap insert failure")
        return "OK"

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        # First fetchrow is exact; second is fuzzy. (resolve_entity
        # calls them in that order.)
        if self._fetchrow_pos == 0:
            self._fetchrow_pos += 1
            return self.exact_row
        self._fetchrow_pos += 1
        return self.fuzzy_row

    def transaction(self):
        @asynccontextmanager
        async def _tx():
            yield None
        return _tx()


class _MockPool:
    def __init__(self, conn: _MockConn) -> None:
        self.conn = conn

    def acquire(self):
        @asynccontextmanager
        async def _ctx():
            yield self.conn
        return _ctx()


# ---------------------------------------------------------------------------
# normalise_entity_text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("Crackingstone Property", "crackingstone property"),
    ("  Crackingstone  Property  ", "crackingstone property"),
    ("Cracking-stone Property", "cracking-stone property"),
    ("CRACKINGSTONE!  PROPERTY,", "crackingstone property"),
    ("", ""),
    ("    ", ""),
])
def test_normalise_entity_text_collapses_to_expected_form(raw, expected):
    assert normalise_entity_text(raw) == expected


def test_normalise_keeps_hyphens_inside_words():
    # PLS-22-08 and "Cracking-stone" should keep their hyphens — the
    # alias index uses them as part of the key.
    assert normalise_entity_text("PLS-22-08") == "pls-22-08"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_entity_raises_on_empty_workspace():
    pool = _MockPool(_MockConn())
    with pytest.raises(ValueError, match="workspace_id is required"):
        await resolve_entity(
            pool, workspace_id="", entity_type="property", entity_text="x",
        )


@pytest.mark.asyncio
async def test_resolve_entity_raises_on_unknown_entity_type():
    pool = _MockPool(_MockConn())
    with pytest.raises(ValueError, match="unknown entity_type"):
        await resolve_entity(
            pool,
            workspace_id="ws-1",
            entity_type="not_a_real_kind",
            entity_text="x",
        )


# ---------------------------------------------------------------------------
# Exact-match path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exact_match_returns_canonical_with_confidence_one():
    exact_row = {
        "alias_id": "abc-123",
        "canonical_name": "Crackingstone Property",
        "canonical_uri": "https://example.org/cgi/property/crackingstone",
        "confidence": 1.0,
    }
    pool = _MockPool(_MockConn(exact_row=exact_row))
    result = await resolve_entity(
        pool,
        workspace_id="ws-1",
        entity_type="property",
        entity_text="cracking-stone property",  # alias
    )
    assert isinstance(result, EntityResolution)
    assert result.match_kind == "exact_canonical"
    assert result.canonical_name == "Crackingstone Property"
    assert result.canonical_uri == "https://example.org/cgi/property/crackingstone"
    assert result.confidence == 1.0
    assert result.alias_id == "abc-123"


@pytest.mark.asyncio
async def test_exact_match_does_not_log_gap():
    exact_row = {
        "alias_id": "abc-123",
        "canonical_name": "X",
        "canonical_uri": None,
        "confidence": 1.0,
    }
    conn = _MockConn(exact_row=exact_row)
    pool = _MockPool(conn)
    await resolve_entity(
        pool, workspace_id="ws-1", entity_type="property", entity_text="x",
    )
    # No INSERT INTO alias_gaps in execute_calls (only set_config).
    inserts = [
        c for c in conn.execute_calls
        if "INSERT INTO silver.alias_gaps" in c[0]
    ]
    assert inserts == []


# ---------------------------------------------------------------------------
# Fuzzy-match path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fuzzy_match_returns_similarity_as_confidence():
    fuzzy_row = {
        "alias_id": "def-456",
        "canonical_name": "Crackingstone Property",
        "canonical_uri": None,
        "confidence": 0.95,
        "sim": 0.78,
    }
    pool = _MockPool(_MockConn(exact_row=None, fuzzy_row=fuzzy_row))
    result = await resolve_entity(
        pool,
        workspace_id="ws-1",
        entity_type="property",
        entity_text="cracking stone",
    )
    assert result.match_kind == "fuzzy_pgtrgm"
    assert result.canonical_name == "Crackingstone Property"
    # confidence is the similarity score, NOT the row's stored confidence.
    assert result.confidence == 0.78
    assert result.alias_id == "def-456"


@pytest.mark.asyncio
async def test_fuzzy_match_respects_threshold():
    """When the fetchrow returns None (the SQL filter on similarity >=
    threshold did its job), the resolver falls through to gap-log."""
    pool = _MockPool(_MockConn(exact_row=None, fuzzy_row=None))
    result = await resolve_entity(
        pool,
        workspace_id="ws-1",
        entity_type="property",
        entity_text="totally unrelated string",
        fuzzy_threshold=0.95,
    )
    assert result.match_kind == "gap_logged"


# ---------------------------------------------------------------------------
# Gap-log path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_miss_logs_gap_with_detector():
    conn = _MockConn(exact_row=None, fuzzy_row=None)
    pool = _MockPool(conn)
    result = await resolve_entity(
        pool,
        workspace_id="ws-1",
        entity_type="property",
        entity_text="zzzzz",
        gap_detector="custom_detector",
    )
    assert result.match_kind == "gap_logged"
    assert result.canonical_name is None
    assert result.confidence == 0.0

    # Find the INSERT call and verify the detector was forwarded.
    insert_calls = [
        c for c in conn.execute_calls
        if "INSERT INTO silver.alias_gaps" in c[0]
    ]
    assert len(insert_calls) == 1
    _sql, args = insert_calls[0]
    # args = (entity_text, entity_text_normalised, entity_type_guess,
    #         detector, query_id, user_id)
    assert args[3] == "custom_detector"


@pytest.mark.asyncio
async def test_log_gap_on_miss_false_skips_gap_insert():
    conn = _MockConn(exact_row=None, fuzzy_row=None)
    pool = _MockPool(conn)
    await resolve_entity(
        pool,
        workspace_id="ws-1",
        entity_type="property",
        entity_text="zzzzz",
        log_gap_on_miss=False,
    )
    insert_calls = [
        c for c in conn.execute_calls
        if "INSERT INTO silver.alias_gaps" in c[0]
    ]
    assert insert_calls == []


@pytest.mark.asyncio
async def test_empty_entity_text_returns_gap_without_db_write():
    """Whitespace-only / empty input → no-op gap result, no SQL writes."""
    conn = _MockConn()
    pool = _MockPool(conn)
    result = await resolve_entity(
        pool, workspace_id="ws-1", entity_type="property", entity_text="   ",
    )
    assert result.match_kind == "gap_logged"
    # Pool.acquire() was never entered → no execute_calls.
    assert conn.execute_calls == []


@pytest.mark.asyncio
async def test_gap_log_swallows_db_failure():
    """A duplicate-gap or constraint violation must NOT propagate —
    the gap log is best-effort observability."""
    conn = _MockConn(
        exact_row=None, fuzzy_row=None, raise_on_insert=True,
    )
    pool = _MockPool(conn)
    # Resolver returns the gap result; doesn't raise.
    result = await resolve_entity(
        pool,
        workspace_id="ws-1",
        entity_type="property",
        entity_text="zzzzz",
    )
    assert result.match_kind == "gap_logged"


# ---------------------------------------------------------------------------
# Workspace GUC pinning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_sets_workspace_id_GUC_inside_transaction():
    conn = _MockConn(exact_row=None, fuzzy_row=None)
    pool = _MockPool(conn)
    await resolve_entity(
        pool,
        workspace_id="ws-tenant-7",
        entity_type="property",
        entity_text="x",
    )
    # First execute call is set_config; arg matches workspace_id.
    assert any(
        "set_config('georag.workspace_id'" in sql
        and args == ("ws-tenant-7",)
        for sql, args in conn.execute_calls
    )


# ---------------------------------------------------------------------------
# log_alias_gap public function
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_alias_gap_inserts_with_provided_detector():
    conn = _MockConn()
    pool = _MockPool(conn)
    await log_alias_gap(
        pool,
        workspace_id="ws-1",
        entity_text="UnknownHole-99",
        entity_type_guess="hole_id",
        detector="hole_id_extractor",
        query_id="00000000-0000-0000-0000-000000000abc",
        user_id=42,
    )
    insert_calls = [
        c for c in conn.execute_calls
        if "INSERT INTO silver.alias_gaps" in c[0]
    ]
    assert len(insert_calls) == 1
    _sql, args = insert_calls[0]
    # entity_text, normalised, entity_type_guess, detector, query_id, user_id
    assert args[0] == "UnknownHole-99"
    assert args[1] == "unknownhole-99"
    assert args[2] == "hole_id"
    assert args[3] == "hole_id_extractor"
    assert args[4] == "00000000-0000-0000-0000-000000000abc"
    assert args[5] == 42


@pytest.mark.asyncio
async def test_log_alias_gap_raises_without_workspace_id():
    pool = _MockPool(_MockConn())
    with pytest.raises(ValueError, match="workspace_id is required"):
        await log_alias_gap(pool, workspace_id="", entity_text="x")
