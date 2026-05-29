"""P2 #28 — Cypher identifier allowlist tests.

Mirrors the pattern of test_verify_numerical_claim_whitelist.py for the
SQL allowlist. Pins:

  * Valid labels and relationship types pass through unchanged.
  * Cypher metacharacters (`]`, `;`, ` `, `(`, comments) are rejected
    by the regex BEFORE the allowlist check ever runs.
  * Unknown-but-syntactically-valid identifiers are rejected by the
    allowlist check (regex passes, allowlist fails).
  * The full tool wrappers (`traverse_knowledge_graph` /
    `query_graph_by_label`) honour the rejection: traverse falls back
    to the unfiltered traversal; query_graph_by_label bails out empty.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.tools import (
    _ALLOWED_GRAPH_LABELS,
    _ALLOWED_GRAPH_RELATIONSHIPS,
    _validate_cypher_label,
    _validate_cypher_relationship,
)


# ---------------------------------------------------------------------------
# Validator unit tests
# ---------------------------------------------------------------------------


def test_known_labels_pass_through():
    # Canonical drill-hole label is `DrillHole` (PascalCase, §04f Global
    # Invariant 4). The 2026-04-27 migration renamed all live nodes from
    # the legacy `:Drillhole` (lowercase h) form. Only `DrillHole` is on
    # the allowlist; Cypher labels are case-sensitive.
    for label in ("Project", "DrillHole", "Formation", "Deposit", "QualifiedPerson"):
        assert _validate_cypher_label(label) == label


def test_drillhole_lowercase_h_rejected():
    """Defence against regression — only the PascalCase form is canonical.
    The old lowercase-h form must be rejected so queries don't silently
    return zero rows if any code still references the legacy label."""
    assert _validate_cypher_label("Drillhole") is None


def test_known_relationships_pass_through():
    for rt in ("HAS_HOLE", "HOSTS", "AUTHORED_BY", "TARGETS"):
        assert _validate_cypher_relationship(rt) == rt


def test_label_with_cypher_injection_payload_rejected():
    """The classic Cypher injection: close the bracket, run a destructive
    query, comment out the rest. Must be rejected by the regex."""
    payload = "DrillHole) DETACH DELETE n; //"
    assert _validate_cypher_label(payload) is None


def test_relationship_with_cypher_injection_payload_rejected():
    payload = "HOSTS_IN] WITH count(*) AS x MATCH (n) DETACH DELETE n //"
    assert _validate_cypher_relationship(payload) is None


def test_unknown_label_rejected_by_allowlist():
    """Syntactically valid identifier that isn't on the allowlist."""
    # Passes the regex (valid identifier shape) but fails the allowlist.
    assert _validate_cypher_label("EvilLabel") is None
    # Case-sensitive — only `DrillHole` (capital H) is canonical.
    # All other casings must be rejected.
    assert _validate_cypher_label("Drillhole") is None  # legacy lowercase-h form
    assert _validate_cypher_label("drillhole") is None
    assert _validate_cypher_label("DRILLHOLE") is None


def test_unknown_relationship_rejected_by_allowlist():
    assert _validate_cypher_relationship("DROP_TABLE") is None
    assert _validate_cypher_relationship("hosts") is None  # case-sensitive!


def test_empty_or_none_returns_none():
    assert _validate_cypher_label(None) is None
    assert _validate_cypher_label("") is None
    assert _validate_cypher_label("   ") is None
    assert _validate_cypher_relationship(None) is None
    assert _validate_cypher_relationship("") is None


def test_label_with_whitespace_trimmed():
    """Surrounding whitespace is allowed; gets trimmed, then validated."""
    assert _validate_cypher_label("  Formation  ") == "Formation"


def test_label_too_long_rejected():
    """64-char identifier ceiling enforced by the regex."""
    long_id = "A" * 65
    assert _validate_cypher_label(long_id) is None


def test_allowlists_are_non_empty():
    """Sanity — the lists shouldn't accidentally ship empty."""
    assert len(_ALLOWED_GRAPH_LABELS) >= 7  # 7 entity types per spec
    assert len(_ALLOWED_GRAPH_RELATIONSHIPS) >= 10


# ---------------------------------------------------------------------------
# Tool-wrapper integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_graph_by_label_returns_empty_for_unsafe_label():
    """The wrapper must NOT issue any Cypher when the label is unsafe."""
    from app.agent.tools import query_graph_by_label

    # Build a context whose neo4j session would AssertionError if touched —
    # proves the validator short-circuited before any session.run() call.
    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def run(self, *_a, **_kw):
            raise AssertionError(
                "Cypher MUST NOT execute when the label is rejected"
            )

    deps = SimpleNamespace(
        neo4j_driver=SimpleNamespace(session=lambda: _Session()),
    )
    ctx = SimpleNamespace(deps=deps)

    result = await query_graph_by_label(
        ctx, label="EvilLabel) MATCH (n) DETACH DELETE n //",
        project_id="3a2c6f5e-9d11-4f8a-9b3e-1c2d4e5f6a7b",
    )

    assert result.count == 0
    assert result.entities == []
    assert "rejected" in result.data_source


@pytest.mark.asyncio
async def test_traverse_knowledge_graph_falls_back_when_rel_type_rejected():
    """When the relationship_type is rejected, the wrapper must run the
    Cypher with the UNFILTERED `[r]` form — preserves the user's intent
    (find any related entity) while dropping the unsafe filter."""
    from app.agent import tools as tools_module

    # Capture the cypher string actually sent to neo4j.
    captured: dict = {}

    class _Result:
        async def data(self):
            return []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def run(self, cypher, **_kw):
            captured["cypher"] = cypher
            return _Result()

    deps = SimpleNamespace(
        neo4j_driver=SimpleNamespace(session=lambda: _Session()),
    )
    ctx = SimpleNamespace(deps=deps)

    await tools_module.traverse_knowledge_graph(
        ctx,
        entity_name="Triple R",
        project_id="3a2c6f5e-9d11-4f8a-9b3e-1c2d4e5f6a7b",
        relationship_type="HOSTS_IN] DETACH DELETE n //",
    )

    assert "cypher" in captured
    cypher = captured["cypher"]
    # The unfiltered form is `[r]` (or `(start)-[r]-(related)`) — the
    # injection payload must NOT appear.
    assert "DETACH DELETE" not in cypher
    assert "DROP" not in cypher.upper()
    # The fallback form is `(start)-[r]-(related)` — no `:RELATION_TYPE`.
    assert "[r:" not in cypher


@pytest.mark.asyncio
async def test_traverse_knowledge_graph_uses_safe_rel_type_when_allowed():
    """Sanity — a known-good relationship_type makes it into the cypher
    with the `[r:HOSTS]` form intact."""
    from app.agent import tools as tools_module

    captured: dict = {}

    class _Result:
        async def data(self):
            return []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def run(self, cypher, **_kw):
            captured["cypher"] = cypher
            return _Result()

    deps = SimpleNamespace(
        neo4j_driver=SimpleNamespace(session=lambda: _Session()),
    )
    ctx = SimpleNamespace(deps=deps)

    await tools_module.traverse_knowledge_graph(
        ctx,
        entity_name="Triple R",
        project_id="3a2c6f5e-9d11-4f8a-9b3e-1c2d4e5f6a7b",
        relationship_type="HOSTS",
    )

    assert "[r:HOSTS]" in captured["cypher"]
