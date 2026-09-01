"""Neo4j DrillHole label migration tests.

Covers the allowlist regression: "DrillHole" passes, "Drillhole" is
rejected. `_ALLOWED_GRAPH_LABELS` and `_validate_cypher_label` are live
sanitisation code in app/agent/tools.py, so these still guard something
real even though the graph itself is gone.

The three @pytest.mark.integration tests that lived here (no legacy nodes
exist, nodes exist after migration, migration is idempotent) were removed
on 2026-08-28 along with the `neo4j` driver package. They needed a live
Neo4j and the conftest fixture that dialled it; with the driver no longer
installed they could never run, only skip.

Architecture reference: Section 04f (entity model), docs/kyle-decisions.md D2.
Migration script: ops/migrations/neo4j/2026-04-27-drillhole-rename.cypher.
"""

from __future__ import annotations

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
