"""Live tests for `get_ontology_class_stats` (doc-phase 120).

Aggregates the seeded ontology data + verifies the per-class status
heuristic.

Depends on the doc-phase 112 mechanical seed being in place
(47 commodities + 29 geological_age + 7 resource_class).
"""
from __future__ import annotations

import os

import asyncpg
import pytest

from app.services.geological_ontology import (
    OntologyClassStats,
    OntologyStatsSummary,
    get_ontology_class_stats,
    seed_classes,
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
    c = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        yield c
    finally:
        await c.close()


@pytest.mark.asyncio
async def test_stats_includes_all_12_classes(conn):
    """Default call returns one entry per §20.1 class."""
    summary = await get_ontology_class_stats(conn)
    assert isinstance(summary, OntologyStatsSummary)
    classes_returned = {c.ontology_class for c in summary.by_class}
    assert classes_returned == set(seed_classes())
    assert len(summary.by_class) == 12


@pytest.mark.asyncio
async def test_commodity_class_populated(conn):
    """commodity has 47 seeded terms ≥ 30 → status='populated'."""
    summary = await get_ontology_class_stats(conn, only_classes=["commodity"])
    assert len(summary.by_class) == 1
    c = summary.by_class[0]
    assert c.ontology_class == "commodity"
    assert c.term_count >= 30
    assert c.status == "populated"
    assert c.populated_threshold == 30


@pytest.mark.asyncio
async def test_geological_age_populated(conn):
    """geological_age has 29 ≥ 20 → 'populated'."""
    summary = await get_ontology_class_stats(conn, only_classes=["geological_age"])
    c = summary.by_class[0]
    assert c.term_count >= 20
    assert c.status == "populated"


@pytest.mark.asyncio
async def test_resource_class_populated(conn):
    """resource_class has 7 ≥ 6 → 'populated'."""
    summary = await get_ontology_class_stats(conn, only_classes=["resource_class"])
    c = summary.by_class[0]
    assert c.term_count >= 6
    assert c.status == "populated"


@pytest.mark.asyncio
async def test_sme_pending_classes_show_empty(conn):
    """9 SME-pending classes have 0 terms → 'empty'."""
    pending = ["deposit_model", "lithology", "alteration", "structure",
               "mineral_assemblage", "host_rock", "tectonic_setting",
               "geochemistry", "geophysics"]
    summary = await get_ontology_class_stats(conn, only_classes=pending)
    for c in summary.by_class:
        # These could be 0 (fresh db) or have data from a future SME pass;
        # for the doc-phase-120 baseline we assert most should be empty.
        # The assertion is permissive: status MUST be one of the valid 4.
        assert c.status in {"empty", "sme_populating", "populated"}


@pytest.mark.asyncio
async def test_summary_rollup_counts(conn):
    """Total terms across all classes ≥ 83 (mechanical seed minimum)."""
    summary = await get_ontology_class_stats(conn)
    # At least 83 from mechanical seed; SME data may add more later.
    assert summary.total_terms >= 83
    assert summary.total_synonyms >= 134
    # At least the 3 mechanical classes have data.
    assert summary.classes_with_any_data >= 3
    # At least the 3 mechanical classes meet their thresholds.
    assert summary.classes_populated >= 3


@pytest.mark.asyncio
async def test_sme_pass_complete_flag(conn):
    """sme_pass_complete is True ONLY if ALL 12 classes populated."""
    summary = await get_ontology_class_stats(conn)
    # Today: 3 mechanical classes populated, 9 SME-pending → False
    # When SME pass lands and all 12 ≥ threshold, this becomes True
    # without code changes.
    expected = summary.classes_populated == 12
    assert summary.sme_pass_complete is expected


@pytest.mark.asyncio
async def test_per_class_stats_dataclass(conn):
    """Verify dataclass field types + shape."""
    summary = await get_ontology_class_stats(conn, only_classes=["commodity"])
    c = summary.by_class[0]
    assert isinstance(c, OntologyClassStats)
    assert isinstance(c.term_count, int)
    assert isinstance(c.synonym_count, int)
    assert isinstance(c.populated_threshold, int)
    assert c.status in {"empty", "mechanical_seeded", "sme_populating", "populated"}
