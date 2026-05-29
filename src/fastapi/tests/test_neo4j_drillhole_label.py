"""Neo4j DrillHole label migration tests.

Covers:
  1. Post-migration assertion: no :Drillhole (legacy lowercase-h) nodes exist.
  2. Allowlist regression: "DrillHole" passes, "Drillhole" is rejected.
  3. Idempotency: running the migration Cypher twice produces the same result.

Integration tests (marked @pytest.mark.integration) require a live Neo4j
instance and are skipped in the standard fast suite:

    pytest -m "not integration and not chaos and not live"

To run integration tests against a local Neo4j container:

    pytest -m integration tests/test_neo4j_drillhole_label.py -v

Architecture reference: Section 04f (entity model), docs/kyle-decisions.md D2.
Migration script: ops/migrations/neo4j/2026-04-27-drillhole-rename.cypher.
"""

from __future__ import annotations

import pytest

from app.agent.tools import _ALLOWED_GRAPH_LABELS, _validate_cypher_label


# ---------------------------------------------------------------------------
# Unit tests — run in the fast suite, no live Neo4j required
# ---------------------------------------------------------------------------


def test_drillhole_camel_case_is_on_allowlist():
    """The canonical PascalCase form must be on the allowlist so the agent
    can query DrillHole nodes after the 2026-04-27 migration."""
    assert "DrillHole" in _ALLOWED_GRAPH_LABELS


def test_drillhole_lowercase_h_not_on_allowlist():
    """The legacy lowercase-h form must NOT be on the allowlist — any
    query using :Drillhole would silently return zero rows post-migration."""
    assert "Drillhole" not in _ALLOWED_GRAPH_LABELS


def test_drillhole_camel_validates():
    """_validate_cypher_label must accept the canonical form."""
    assert _validate_cypher_label("DrillHole") == "DrillHole"


def test_drillhole_lowercase_h_rejected():
    """_validate_cypher_label must reject the legacy form after the migration
    canonicalised all nodes to :DrillHole. Returning None prevents the agent
    from constructing Cypher that would match zero nodes."""
    assert _validate_cypher_label("Drillhole") is None


def test_drillhole_all_other_casings_rejected():
    """All non-canonical casings must be rejected — Cypher labels are
    case-sensitive and the graph only carries one spelling post-migration."""
    for bad in ("drillhole", "DRILLHOLE", "Drill_Hole", "drill_hole"):
        assert _validate_cypher_label(bad) is None, (
            f"Expected {bad!r} to be rejected by _validate_cypher_label"
        )


# ---------------------------------------------------------------------------
# Integration tests — skipped without a live Neo4j instance
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_no_legacy_drillhole_nodes_exist(neo4j_driver) -> None:  # type: ignore[no-untyped-def]
    """Post-migration: confirm zero :Drillhole (lowercase-h) nodes remain.

    Requires the `neo4j_driver` fixture, which must be provided by the
    integration test conftest pointing at a live Neo4j instance that has
    already had the 2026-04-27 migration applied.

    Expected result: count == 0
    """
    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (n:Drillhole) RETURN count(n) AS legacy_count"
        )
        record = await result.single()

    assert record is not None, "Query returned no records"
    legacy_count = record["legacy_count"]
    assert legacy_count == 0, (
        f"Found {legacy_count} nodes still labelled :Drillhole (legacy lowercase-h). "
        "Run ops/migrations/neo4j/2026-04-27-drillhole-rename.cypher to migrate them."
    )


@pytest.mark.integration
async def test_drillhole_nodes_exist_after_migration(neo4j_driver) -> None:  # type: ignore[no-untyped-def]
    """Post-migration: confirm :DrillHole nodes are present in the graph.

    This test will only pass after both the migration AND at least one run of
    the index_neo4j or populate_neo4j asset (which creates the nodes with the
    new label). Pre-migration graphs will have zero :DrillHole nodes.
    """
    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (n:DrillHole) RETURN count(n) AS camel_count"
        )
        record = await result.single()

    assert record is not None, "Query returned no records"
    camel_count = record["camel_count"]
    assert camel_count > 0, (
        "No :DrillHole nodes found in the graph. "
        "Run the index_neo4j Dagster asset after applying the migration to populate nodes."
    )


@pytest.mark.integration
async def test_migration_idempotent(neo4j_driver) -> None:  # type: ignore[no-untyped-def]
    """Running the rename Cypher a second time is a no-op.

    SET n:DrillHole on a node that already has :DrillHole does nothing.
    REMOVE n:Drillhole on a node that has no :Drillhole does nothing.
    The count before and after a second migration pass must be identical.
    """
    async with neo4j_driver.session() as session:
        # Record the count before a second migration pass.
        before_result = await session.run(
            "MATCH (n:DrillHole) RETURN count(n) AS cnt"
        )
        before_record = await before_result.single()
        count_before = before_record["cnt"] if before_record else 0

        # Run the migration Cypher again (idempotency check).
        await session.run(
            "MATCH (n:Drillhole) SET n:DrillHole REMOVE n:Drillhole"
        )

        # Record the count after the second pass.
        after_result = await session.run(
            "MATCH (n:DrillHole) RETURN count(n) AS cnt"
        )
        after_record = await after_result.single()
        count_after = after_record["cnt"] if after_record else 0

    assert count_before == count_after, (
        f"Migration is not idempotent: count changed from {count_before} "
        f"to {count_after} on the second run."
    )
