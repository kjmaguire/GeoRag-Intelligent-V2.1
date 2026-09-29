"""§12.10 continuous_learning_loop tests (Phase H4 graduation)."""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from app.hatchet_workflows.continuous_learning_loop import (
    ContinuousLearningLoopInput,
)
from app.hatchet_workflows.continuous_learning_loop import (
    execute as continuous_learning_loop_execute,
)


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _live_db_available() -> bool:
    return bool(os.environ.get("POSTGRES_PASSWORD"))


@pytest.mark.integration
@pytest.mark.asyncio
async def test_continuous_learning_loop_runs_against_real_workspaces() -> None:
    """Smoke: the orchestrator walks every workspace, records the
    threshold check, and emits an audit anchor."""
    if not _live_db_available():
        pytest.skip("POSTGRES_PASSWORD not set")

    inp = ContinuousLearningLoopInput(
        initiated_by="test",
        target_retraining_threshold=25,
        source_trust_retraining_threshold=500,
        loop_request_id=uuid4(),
    )
    out = await continuous_learning_loop_execute.aio_mock_run(inp)
    assert out.success is True
    # Scanned at least the workspaces that exist
    assert out.workspaces_scanned >= 1
    # `workspaces_evaluated` was asserted here to equal
    # `workspaces_scanned`. It always did -- the loop it counted has no
    # early exit -- which is why the field was removed on 2026-08-22
    # along with `eval_regressions_detected`. Neither ever measured an
    # evaluation; `evaluate_workspace` was deleted in 09d1d35.


@pytest.mark.integration
@pytest.mark.asyncio
async def test_continuous_learning_loop_emits_audit_anchor() -> None:
    """The loop emits a `continuous_learning_loop.completed` row to
    the audit ledger so the next run can compute deltas from it."""
    if not _live_db_available():
        pytest.skip("POSTGRES_PASSWORD not set")

    request_id = uuid4()
    inp = ContinuousLearningLoopInput(
        initiated_by="test",
        loop_request_id=request_id,
    )
    out = await continuous_learning_loop_execute.aio_mock_run(inp)
    assert out.success is True

    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        n = await conn.fetchval(
            """
            SELECT count(*) FROM audit.audit_ledger
             WHERE action_type = 'continuous_learning_loop.completed'
               AND target_id = $1
            """,
            str(request_id),
        )
    finally:
        await conn.close()
    assert n == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_continuous_learning_loop_threshold_flags_pending_workspaces() -> None:
    """With thresholds set to 0, every workspace with any outcomes
    activity should flag as pending training. (Defensive check — the
    orchestrator should never crash and always return a structured
    result.)"""
    if not _live_db_available():
        pytest.skip("POSTGRES_PASSWORD not set")

    inp = ContinuousLearningLoopInput(
        initiated_by="test",
        target_retraining_threshold=0,
        source_trust_retraining_threshold=0,
        loop_request_id=uuid4(),
    )
    out = await continuous_learning_loop_execute.aio_mock_run(inp)
    assert out.success is True
    # workspaces_pending_training >= 0 (no live outcome data yet in this DB)
    assert out.workspaces_pending_training >= 0


# ---------------------------------------------------------------------------
# HAT-13 (2026-09-29): the cron, and why scheduling it is safe. These run
# without a database. The shutdown-window tests check the 22:30 UTC slot
# and its 30 minute budget in PDT and PST.
# ---------------------------------------------------------------------------
_MODULE = (
    Path(__file__).resolve().parent.parent
    / "app" / "hatchet_workflows" / "continuous_learning_loop.py"
)


def test_the_loop_is_declared_as_a_daily_cron() -> None:
    """Its docstring said 'daily cron' for months while it had no on_crons."""
    text = _MODULE.read_text(encoding="utf-8")
    match = re.search(
        r'hatchet\.workflow\(\s*name="continuous_learning_loop"(.*?)\n\)', text, re.S,
    )
    assert match, "workflow declaration not found"
    assert re.search(r'on_crons\s*=\s*\["30 22 \* \* \*"\]', match.group(1))


def test_a_cron_tick_with_no_input_validates() -> None:
    """An engine cron sends NO input. Every field must default."""
    inp = ContinuousLearningLoopInput.model_validate({})
    assert inp.initiated_by == "cron"
    assert inp.target_retraining_threshold == 25


def test_the_loop_imports_nothing_that_can_spend() -> None:
    """Kyle's bar for an unattended cron: nothing that bills Cohere.

    No spend ceiling exists, so a scheduled workflow must not reach an LLM,
    embedder, reranker or Parse. This pins it statically: a future edit that
    auto-spawns a trainer or calls a model has to change this test first.
    """
    source = _MODULE.read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    spenders = re.compile(r"llm|cohere|bedrock|anthropic|embed|rerank|parse|\.agent\b|train_")
    assert not {m for m in imported if spenders.search(m)}, sorted(imported)
    assert "aio_run" not in source, "the loop must not dispatch other workflows unattended"


class _FakeConn:
    """Enough of asyncpg.Connection to run the loop body."""

    def __init__(self, workspace_ids: list[str]) -> None:
        self.workspace_ids = workspace_ids
        self.in_tx = False
        self.scope = ""
        self.counts: list[tuple[str, tuple, str, bool]] = []

    def is_in_transaction(self) -> bool:
        return self.in_tx

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self) -> None:
                conn.in_tx = True

            async def __aexit__(self, *exc: object) -> bool:
                conn.in_tx = False
                conn.scope = ""  # SET LOCAL ends with the transaction
                return False

        return _Tx()

    async def execute(self, sql: str, *args: object) -> str:
        if "set_config('app.workspace_id'" in sql:
            self.scope = str(args[0]) if args else ""
        return "SELECT 1"

    async def fetch(self, sql: str, *args: object) -> list[dict[str, str]]:
        assert "silver.workspaces" in sql
        return [{"workspace_id": w} for w in self.workspace_ids]

    async def fetchval(self, sql: str, *args: object):
        if "audit.audit_ledger" in sql:
            return None
        self.counts.append((sql, args, self.scope, self.in_tx))
        return 30 if "target_outcomes" in sql else 0

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_each_workspace_is_counted_under_its_own_scope(monkeypatch) -> None:
    """HAT-1 shape: the counts hit fail-closed tables, so each must run
    inside a transaction with that workspace bound, and no scope may be left
    on the connection for the NULL-workspace audit insert."""
    from app import audit as audit_mod
    from app.hatchet_workflows import continuous_learning_loop as mod
    from app.services import laravel_bridge

    workspaces = [
        "a0000000-0000-0000-0000-000000000001",
        "a0000000-0000-0000-0000-000000000002",
    ]
    conn = _FakeConn(workspaces)
    audits: list[dict] = []

    async def _connect(*_a, **_k):
        return conn

    async def _emit(c, **kwargs):
        audits.append({**kwargs, "scope_at_emit": c.scope, "in_tx": c.in_tx})

    async def _noop(**_k):
        return None

    monkeypatch.setattr(mod.asyncpg, "connect", _connect)
    monkeypatch.setattr(audit_mod, "emit_audit", _emit)
    monkeypatch.setattr(laravel_bridge, "post_admin_surface_updated", _noop)

    out = await continuous_learning_loop_execute.aio_mock_run(
        ContinuousLearningLoopInput.model_validate({}),
    )

    assert out.success is True, out.failure_reason
    assert out.workspaces_scanned == 2
    assert out.workspaces_pending_training == 2  # 30 outcomes >= 25
    assert len(conn.counts) == 4
    for sql, args, scope, in_tx in conn.counts:
        assert in_tx, f"count ran outside a transaction: {sql}"
        assert scope == args[0], "count ran under another workspace's scope"
    assert audits and audits[0]["scope_at_emit"] == ""
    assert audits[0].get("workspace_id") is None
