"""Z-roadmap Z.9 — Graph Tenant Auditor tests.

B1 (2026-07-28) removed Neo4j from the stack; this agent is now a
permanent no-op (see the module docstring in
``app.agents.phase0.graph_tenant_auditor``). Three coverage layers,
updated to match:

  1. Source-shape regression — inverted from the original intent.
     Instead of pinning the (now-removed) Cypher invariants in place,
     guards against someone silently reintroducing live Cypher here
     without updating the module docstring and this test.
  2. Pure-helper test — `_persist_run_summary_sql_args` builds the
     correct INSERT shape without needing a live driver or pool.
     Unaffected by B1 — still exercised on every run, including the
     no-op path.
  3. End-to-end run against a stubbed pg pool — no Neo4j mocking left
     to do (the agent never touches `neo4j.AsyncGraphDatabase`
     anymore). Asserts the actual current contract: zero violations,
     zero per-row findings, but one summary row still written to
     `tenant_isolation_audit` every run.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

# ---------------------------------------------------------------------------
# Layer 1 — source-shape regression
# ---------------------------------------------------------------------------


def test_graph_auditor_module_imports() -> None:
    from app.agents.phase0 import graph_tenant_auditor as m

    assert m is not None
    assert callable(getattr(m, "graph_tenant_audit", None))


def test_graph_auditor_is_a_documented_neo4j_removal_stub() -> None:
    """B1 (2026-07-28) removed Neo4j from the stack; the three Cypher
    invariants this agent used to run no longer exist in source. This is
    the inverse of the original regression net (which pinned the Cypher
    in place): it now guards against someone silently reintroducing live
    Cypher here without updating both the module docstring and this test
    — Neo4j is gone platform-wide, not just from this one agent.
    """
    from app.agents.phase0 import graph_tenant_auditor as m

    src = inspect.getsource(m)
    assert "neo4j was removed from the stack (B1, 2026-07-28)" in src
    assert "MATCH (" not in src, (
        "Found live Cypher in a module that's supposed to be a permanent "
        "no-op post-B1 — either Neo4j is back (update this test) or this "
        "is dead code that should be removed too."
    )


def test_graph_auditor_persists_to_tenant_isolation_audit() -> None:
    from app.agents.phase0 import graph_tenant_auditor as m

    src = inspect.getsource(m)
    assert "silver.tenant_isolation_audit" in src
    assert "silver.store_reconciliation_findings" in src
    assert "Graph Tenant Auditor" in src


def test_graph_auditor_uses_lowercase_node_variables() -> None:
    """CLAUDE.md Cypher style — `n`, `e`, `p`, `a`, `b` only."""
    from app.agents.phase0 import graph_tenant_auditor as m

    src = inspect.getsource(m)
    # Negative check — uppercase or PascalCase variable would be a style
    # regression. Match the bound-variable form `(VAR:Label`, with the
    # variable starting uppercase. False-positive guard: skip the well-
    # known {Label} property-only match like `{:Internal}` by requiring
    # an alpha char before the colon.
    import re

    bad = re.findall(r"MATCH \(([A-Z]\w*):\w+", src)
    assert not bad, (
        f"Found uppercase Cypher node variable(s) {bad} — "
        "CLAUDE.md hard rule mandates lowercase (p, n, e, a, b)."
    )


def test_graph_auditor_does_not_use_enterprise_features() -> None:
    """CLAUDE.md hard rule #9 — Community Edition only."""
    from app.agents.phase0 import graph_tenant_auditor as m

    src = inspect.getsource(m)
    enterprise_markers = [
        "SHOW DATABASES",
        "CREATE DATABASE",
        "GRANT ROLE",
        "CALL apoc.warmup",
        # apoc.* is technically a plugin, but warmup is the canonical
        # enterprise warmup pattern Phase 0 explicitly rejects.
    ]
    for marker in enterprise_markers:
        assert marker not in src, (
            f"Found Enterprise-only Cypher feature {marker!r} — "
            "CLAUDE.md hard rule #9 forbids it."
        )


# ---------------------------------------------------------------------------
# Layer 2 — pure helper
# ---------------------------------------------------------------------------


def test_persist_run_summary_sql_args_shape() -> None:
    from app.agents.phase0.graph_tenant_auditor import (
        _persist_run_summary_sql_args,
    )

    ws = UUID("11111111-1111-1111-1111-111111111111")
    summary: dict[str, Any] = {
        "graph_violations": 3,
        "labels_probed": 2,
        "nodes_probed": 100,
        "edges_probed": 250,
        "missing_workspace_id_details": [{"labels": ["Document"], "missing_count": 1}],
        "cross_workspace_edge_details": [
            {"rel_type": "HAS_HOLE", "ws_a": "a", "ws_b": "b", "violations": 1}
        ],
        "orphan_node_details": [{"kind": "missing_in_graph", "count": 1}],
    }
    sql, args = _persist_run_summary_sql_args(summary, ws)
    assert "INSERT INTO silver.tenant_isolation_audit" in sql
    assert "'neo4j_graph'" in sql
    # arg order matches the placeholder order in the INSERT
    assert args[0] == ws
    assert args[1] == 3
    assert args[2] == 2
    assert args[3] == 250
    assert args[4] == 100
    # JSONB payload is a string the wrapper passes through ::jsonb
    assert isinstance(args[5], str)
    assert "missing_workspace_id" in args[5]
    assert "cross_workspace_edges" in args[5]
    assert "orphan_nodes" in args[5]


# ---------------------------------------------------------------------------
# Layer 3 — no-op run against a stubbed pg pool
#
# B1 (2026-07-28) removed Neo4j from the stack. The mocked-Neo4j-session
# fixtures this layer used to need (_FakeRecord/_FakeResult/_FakeSession/
# _FakeDriver/_FakeAsyncGraphDatabase) are gone with it — the agent never
# touches neo4j.AsyncGraphDatabase anymore, so there's nothing left to
# mock on that side. Only the pg_pool stub survives, since the agent
# still writes one summary row to tenant_isolation_audit every run.
# ---------------------------------------------------------------------------


class _FakePool:
    """Async stub for asyncpg.Pool that records writes for assertion.

    B1 (2026-07-28): the agent no longer calls `.fetch(...)` at all (the
    orphan/missing-project cross-store check it used to feed was part of
    the removed Cypher invariants), so this stub only needs to answer
    `.execute(...)` for the two INSERT statements that still run.
    """

    def __init__(self) -> None:
        self.finding_writes: list[tuple[str, str, str]] = []  # (drift_type, store, target)
        self.audit_writes: list[dict[str, Any]] = []

    async def fetch(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        return []

    async def fetchval(self, *_: Any, **__: Any) -> Any:
        return None

    async def execute(self, query: str, *args: Any) -> None:
        if "INSERT INTO silver.store_reconciliation_findings" in query:
            # args order: workspace_id, drift_type, target_store, source_id, details_json
            self.finding_writes.append((args[1], args[2], args[3]))
        elif "INSERT INTO silver.tenant_isolation_audit" in query:
            self.audit_writes.append({
                "workspace_id": args[0],
                "graph_violations": args[1],
            })

    def acquire(self) -> _FakePool:
        return self

    async def __aenter__(self) -> _FakePool:
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False


def _install_wrapper_stubs(monkeypatch: pytest.MonkeyPatch, pool: _FakePool) -> None:
    """Reusing the pattern from test_phase0_smoke.py.

    Stubs the timeout policy + every wrapper-side DB hook so the
    decorated function executes end-to-end without a live runtime.
    """
    from app.agents.runtime import register_runtime

    register_runtime(pg_pool=pool, redis=None)

    monkeypatch.setattr(
        "app.agents.wrapper._load_timeout_policy",
        AsyncMock(return_value={
            "agent_name": "Graph Tenant Auditor",
            "risk_tier": "R0",
            "soft_timeout_ms": 30_000,
            "hard_timeout_ms": 60_000,
            "retry_count": 0,
            "circuit_breaker_scope": "none",
            "failure_threshold": 0,
            "cool_down_seconds": 0,
        }),
    )
    for hook in (
        "_circuit_check",
        "_circuit_record",
        "_write_usage_event",
        "_idempotency_lookup",
        "emit_audit",
    ):
        monkeypatch.setattr(
            f"app.agents.wrapper.{hook}",
            AsyncMock(return_value=None),
        )


@pytest.mark.asyncio
async def test_graph_auditor_is_permanent_noop_and_persists_one_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B1 (2026-07-28) — Neo4j was removed; this agent is now a permanent
    no-op regardless of driver/mock state. Was "the canonical end-to-end
    test" against a mocked Neo4j session; that mock is now unreachable
    dead weight since the agent never touches `neo4j.AsyncGraphDatabase`
    at all anymore. Rewritten to assert the actual current contract:
    zero violations detected (there's nothing left to detect), zero
    per-row findings persisted, but exactly one summary row still
    written to tenant_isolation_audit so the ops dashboard's history
    doesn't show a gap.
    """
    pool = _FakePool()
    _install_wrapper_stubs(monkeypatch, pool)

    from app.agents.context import AgentContext
    from app.agents.phase0.graph_tenant_auditor import graph_tenant_audit

    ws = UUID("11111111-1111-1111-1111-111111111111")
    ctx = AgentContext(workspace_id=ws, actor_kind="test", agent_name="Graph Tenant Auditor")
    result = await graph_tenant_audit(ctx=ctx, sample_limit_per_check=5)

    summary = result.value
    assert summary is not None, (
        f"agent returned no value — outcome={result.outcome!r}, "
        f"error={result.error!r}"
    )
    assert summary["neo4j_reachable"] is False
    assert summary["skipped_reason"] == "neo4j was removed from the stack (B1, 2026-07-28)"
    assert summary["missing_workspace_id"] == 0
    assert summary["cross_workspace_edges"] == 0
    assert summary["orphan_nodes"] == 0
    assert summary["graph_violations"] == 0

    # Nothing to detect => nothing to persist per-row.
    assert pool.finding_writes == []

    # The run-summary row is still written every run so the dashboard's
    # isolation-health history stays continuous.
    assert len(pool.audit_writes) == 1
    assert pool.audit_writes[0]["graph_violations"] == 0
    assert pool.audit_writes[0]["workspace_id"] == ws


@pytest.mark.asyncio
async def test_graph_auditor_no_op_when_neo4j_driver_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defensive — environments without neo4j installed must not crash."""
    pool = _FakePool()
    _install_wrapper_stubs(monkeypatch, pool)

    # Simulate ImportError by removing the neo4j module from sys.modules
    # and blocking re-import. Cleaner than rewriting the import statement.
    import builtins
    import sys

    real_import = builtins.__import__

    def _blocked_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "neo4j":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    sys.modules.pop("neo4j", None)
    monkeypatch.setattr(builtins, "__import__", _blocked_import)

    from app.agents.context import AgentContext
    from app.agents.phase0.graph_tenant_auditor import graph_tenant_audit

    result = await graph_tenant_audit(ctx=AgentContext(agent_name="Graph Tenant Auditor"))
    summary = result.value
    assert summary is not None
    assert summary["neo4j_reachable"] is False
    # B1 (2026-07-28): Neo4j was removed from the stack entirely, so the
    # agent no longer even attempts the lazy `from neo4j import ...` this
    # test used to defend against — the skip reason is now the fixed B1
    # message regardless of whether the driver is importable.
    assert summary["skipped_reason"] == "neo4j was removed from the stack (B1, 2026-07-28)"
    assert summary["graph_violations"] == 0
    # No findings should be written when the auditor short-circuits.
    assert pool.finding_writes == []
