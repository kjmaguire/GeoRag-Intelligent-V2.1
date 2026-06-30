"""Z-roadmap Z.9 — Graph Tenant Auditor tests.

Three coverage layers:

  1. Source-shape regression — every invariant the agent claims to
     check is actually expressed in the Cypher source.
  2. Pure-helper test — `_persist_run_summary_sql_args` builds the
     correct INSERT shape without needing a live driver or pool.
  3. End-to-end mocked run — `AsyncGraphDatabase.driver` is patched
     with a stub Neo4j session that returns:
       - one cross-workspace edge (the canonical fence breach)
       - one node missing workspace_id
       - one orphan project node not present in silver.projects
     and the assertion is that the auditor counts all three and
     persists matching finding rows.

The mocked-Neo4j approach mirrors `test_phase0_smoke.py`: stub the
runtime, stub the wrapper-side hooks, monkey-patch
``neo4j.AsyncGraphDatabase`` at the import site so the agent picks up
the stub via its lazy import.
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


def test_graph_auditor_runs_three_invariants() -> None:
    """The three Cypher invariants must be expressed in source.

    Regression net for somebody silently deleting a check while
    refactoring — the source-text grep is the cheapest way to pin the
    behavioural contract.
    """
    from app.agents.phase0 import graph_tenant_auditor as m

    src = inspect.getsource(m)

    # Check 1 — node workspace_id coverage
    assert "n.workspace_id IS NULL" in src
    # Check 2 — cross-workspace edge fence
    assert "a.workspace_id <> b.workspace_id" in src
    # Check 3 — orphan / missing project cross-store
    assert "MATCH (p:Project {project_id: pid, " in src
    assert "MATCH (p:Project {workspace_id: $ws})" in src


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
# Layer 3 — end-to-end mocked Neo4j run
# ---------------------------------------------------------------------------


class _FakeRecord:
    """Mimics neo4j.Record's dict-style `__getitem__`."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._p = payload

    def __getitem__(self, key: str) -> Any:
        return self._p[key]


class _FakeResult:
    """Mimics the AsyncResult interface used by the agent.

    Supports `async for record in res` AND `await res.single()`.
    """

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = [_FakeRecord(r) for r in records]

    def __aiter__(self) -> _FakeResult:
        self._iter = iter(self._records)
        return self

    async def __anext__(self) -> _FakeRecord:
        try:
            return next(self._iter)
        except StopIteration as exc:  # noqa: BLE001
            raise StopAsyncIteration from exc

    async def single(self) -> _FakeRecord | None:
        return self._records[0] if self._records else None


class _FakeSession:
    """Stub Neo4j session that returns canned results per Cypher query.

    The agent runs five queries in sequence; we key the canned results
    on a substring unique to each query so a re-order in the agent
    doesn't silently break the test (StopIteration would fire instead).
    """

    def __init__(self) -> None:
        # ws_a / ws_b are the two halves of the canonical fence breach.
        self.ws_a = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        self.ws_b = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        # One :Project node exists in graph that's NOT in silver
        # (orphan_in_graph). silver returns one project that the graph
        # doesn't carry (missing_in_graph).
        self.graph_project_ids = ["graph-only-proj-1"]

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    async def run(self, query: str, **params: Any) -> _FakeResult:  # noqa: ARG002
        if "n.workspace_id IS NULL" in query:
            # Check 1 — one violating label group with 1 node
            return _FakeResult(
                [
                    {
                        "labels": ["Document"],
                        "missing_count": 1,
                        "sample_ids": ["elt-doc-1"],
                    }
                ]
            )
        if "MATCH (n) RETURN count(n)" in query:
            return _FakeResult([{"c": 42}])
        if "a.workspace_id <> b.workspace_id" in query:
            # Check 2 — one cross-workspace edge
            return _FakeResult(
                [
                    {
                        "rel_type": "HAS_HOLE",
                        "ws_a": self.ws_a,
                        "ws_b": self.ws_b,
                        "violations": 1,
                        "sample_ids": ["elt-edge-1"],
                    }
                ]
            )
        if "MATCH ()-[e]->() RETURN count(e)" in query:
            return _FakeResult([{"c": 17}])
        if "OPTIONAL MATCH (p:Project {project_id: pid" in query:
            # Check 3a — silver has one project that the graph doesn't.
            # `pg_rows` in the agent provides the id list; here we
            # return whichever ids weren't in graph_project_ids.
            ids = params.get("ids", [])
            missing = [i for i in ids if i not in self.graph_project_ids]
            return _FakeResult([{"missing": missing}])
        if "MATCH (p:Project {workspace_id: $ws})" in query:
            # Check 3b — graph has one project node for the workspace
            return _FakeResult([{"ids": list(self.graph_project_ids)}])
        # Fallback — empty result.
        return _FakeResult([])


class _FakeDriver:
    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    def session(self) -> _FakeSession:
        return _FakeSession()

    async def close(self) -> None:
        return None


class _FakeAsyncGraphDatabase:
    """Stand-in for `neo4j.AsyncGraphDatabase` — the agent calls
    `AsyncGraphDatabase.driver(uri, auth=...)` as a class-level factory."""

    @staticmethod
    def driver(*_: Any, **__: Any) -> _FakeDriver:
        return _FakeDriver()


class _FakePool:
    """Async stub for asyncpg.Pool that records writes for assertion."""

    def __init__(self) -> None:
        self.finding_writes: list[tuple[str, str, str]] = []  # (drift_type, store, target)
        self.audit_writes: list[dict[str, Any]] = []
        # silver.projects returns one row whose project_id is NOT in
        # the graph (missing_in_graph) so the orphan check fires.
        self.silver_projects = [
            {
                "project_id": "silver-only-proj-1",
                "project_name": "Silver only",
            }
        ]

    async def fetch(self, query: str, *_: Any) -> list[dict[str, Any]]:
        if "FROM silver.projects" in query and "ANY($2::text[])" in query:
            # Check 3b cross-check — graph asked for ids; silver returns
            # only those that exist. Our graph carries `graph-only-proj-1`
            # which is NOT in silver, so return empty.
            return []
        if "FROM silver.projects" in query:
            return self.silver_projects
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
async def test_graph_auditor_detects_cross_workspace_edge_and_persists_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical end-to-end test.

    Mock Neo4j carries:
      - one Document node missing workspace_id
      - one HAS_HOLE edge spanning two workspaces
      - one :Project node not present in silver.projects (orphan)
      - silver.projects has one project not present in graph (missing)

    Assertions:
      - summary.graph_violations == 4
      - summary.cross_workspace_edges == 1
      - 4 store_reconciliation_findings rows written
      - 1 tenant_isolation_audit row written with the same total
    """
    # Patch neo4j.AsyncGraphDatabase BEFORE the agent runs — the agent
    # imports it lazily at call time via `from neo4j import ...`, so the
    # monkeypatch on the module attribute is what the agent sees.
    import neo4j

    monkeypatch.setattr(neo4j, "AsyncGraphDatabase", _FakeAsyncGraphDatabase)

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
    assert summary["neo4j_reachable"] is True
    # 1 missing workspace_id + 1 cross-workspace edge + 1 missing project
    # in graph + 1 orphan project in graph
    assert summary["missing_workspace_id"] == 1
    assert summary["cross_workspace_edges"] == 1
    assert summary["orphan_nodes"] == 2
    assert summary["graph_violations"] == 4

    # 4 per-row findings written
    assert len(pool.finding_writes) == 4
    drift_types = sorted(w[0] for w in pool.finding_writes)
    # missing workspace_id => missing_in_b; cross-edge => orphan_in_b;
    # missing_in_graph => missing_in_b; orphan_in_graph => orphan_in_b
    assert drift_types == ["missing_in_b", "missing_in_b", "orphan_in_b", "orphan_in_b"]
    assert all(w[1] == "neo4j" for w in pool.finding_writes)

    # 1 run-summary row in tenant_isolation_audit
    assert len(pool.audit_writes) == 1
    assert pool.audit_writes[0]["graph_violations"] == 4
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
    assert summary["skipped_reason"] == "neo4j driver not installed"
    assert summary["graph_violations"] == 0
    # No findings should be written when the auditor short-circuits.
    assert pool.finding_writes == []
