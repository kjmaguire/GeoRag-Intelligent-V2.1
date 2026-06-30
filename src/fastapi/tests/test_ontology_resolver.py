"""Live tests for the §9.2 ontology resolver (doc-phase 114).

Requires the database to have the doc-phase 112 mechanical seed
applied (47 commodities + 29 geological ages + 7 resource classes).
Runs against the postgres test connection.

If you see test failures here on a fresh database, run:

    php artisan db:seed --class=GeologicalOntologyMechanicalSeeder --force
"""
from __future__ import annotations

import os

import asyncpg
import pytest

from app.services.geological_ontology import (
    find_synonyms,
    resolve_term,
)


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@pytest.fixture
async def conn():
    """asyncpg connection for the test."""
    c = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        yield c
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_synonym_match(conn):
    """U3O8 should resolve to Uranium via the synonym table."""
    r = await resolve_term(conn, raw_term="U3O8")
    assert r is not None
    assert r.canonical_term == "Uranium"
    assert r.ontology_class == "commodity"
    assert r.matched_via == "synonym"
    assert r.payload.get("element_symbol") == "U"


@pytest.mark.asyncio
async def test_canonical_term_match(conn):
    """Direct canonical lookup falls back when no synonym matches."""
    r = await resolve_term(conn, raw_term="Gold")
    assert r is not None
    assert r.canonical_term == "Gold"
    assert r.ontology_class == "commodity"
    assert r.matched_via == "canonical_term"


@pytest.mark.asyncio
async def test_case_insensitive_synonym(conn):
    """Lookup is case-insensitive."""
    r = await resolve_term(conn, raw_term="YELLOWCAKE")
    assert r is not None
    assert r.canonical_term == "Uranium"

    r = await resolve_term(conn, raw_term="yellowcake")
    assert r is not None
    assert r.canonical_term == "Uranium"


@pytest.mark.asyncio
async def test_class_restriction_blocks_wrong_class(conn):
    """U3O8 is a commodity synonym; restrict_to_class=geological_age
    must NOT return it."""
    r = await resolve_term(
        conn,
        raw_term="U3O8",
        restrict_to_class="geological_age",
    )
    assert r is None


@pytest.mark.asyncio
async def test_no_match_returns_none(conn):
    """Garbage input returns None rather than raising."""
    r = await resolve_term(conn, raw_term="xyzznonexistent")
    assert r is None


@pytest.mark.asyncio
async def test_empty_string_returns_none(conn):
    """Empty / whitespace input returns None."""
    assert await resolve_term(conn, raw_term="") is None
    assert await resolve_term(conn, raw_term="   ") is None


@pytest.mark.asyncio
async def test_geological_age_resolves(conn):
    """Cretaceous resolves into the geological_age class."""
    r = await resolve_term(conn, raw_term="Cretaceous")
    assert r is not None
    assert r.canonical_term == "Cretaceous Period"
    assert r.ontology_class == "geological_age"


@pytest.mark.asyncio
async def test_resource_class_synonym(conn):
    """'inferred' resolves to the CIM Inferred Mineral Resource."""
    r = await resolve_term(conn, raw_term="inferred")
    assert r is not None
    assert r.canonical_term == "Inferred Mineral Resource"
    assert r.ontology_class == "resource_class"


@pytest.mark.asyncio
async def test_find_synonyms_returns_all(conn):
    """find_synonyms returns every alias for a canonical term."""
    syns = await find_synonyms(conn, canonical_term="Uranium")
    assert "U" in syns
    assert "U3O8" in syns
    assert "yellowcake" in syns


@pytest.mark.asyncio
async def test_find_synonyms_canonical_unknown(conn):
    """Unknown canonical term returns empty list (not None)."""
    syns = await find_synonyms(conn, canonical_term="ThisIsNotASeededTerm")
    assert syns == []
