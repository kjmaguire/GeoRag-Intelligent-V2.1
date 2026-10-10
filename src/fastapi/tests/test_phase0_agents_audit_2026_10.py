"""Regression tests for the 2026-10 audit of the phase-0 ops agents.

Findings pinned here:

* 1  both R2 agents (Storage Tiering, Support Packet) were rejected by the
     wrapper's idempotency-key check before they ran, and every failure,
     timeout, refusal and open breaker came back as a green Hatchet run
     carrying "clean-looking" defaults. A dead-lettered propagation with no
     workspace also crashed Store Reconciliation on a NOT NULL column.
* 9  model_cost_summary's soft warning never re-armed after the first month.
* 10 the nightly store reconciliation compared zero to zero.
* 11 the circuit breaker was re-armed by the calls it rejected.

The agents run through the REAL ``@georag_agent`` wrapper and the REAL
``_ctx_from`` context the Hatchet task builds. Only the wrapper's database
and Redis hooks are stubbed, because the idempotency-key check (the thing
that rejected the R2 agents) is pure code and must not be stubbed away.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from hatchet_sdk import NonRetryableException

from app.agents import (
    AgentCircuitOpenError,
    AgentContext,
    AgentResult,
    georag_agent,
    register_runtime,
)
from app.agents import wrapper as agent_wrapper
from app.hatchet_workflows import phase0_agents as p0

WORKSPACE = "a0000000-0000-0000-0000-000000000001"
OTHER_WORKSPACE = "b0000000-0000-0000-0000-000000000002"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
class _Tx:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _Conn:
        self.conn.in_tx = True
        return self.conn

    async def __aexit__(self, *exc: object) -> bool:
        self.conn.in_tx = False
        return False


class _Conn:
    def __init__(self, pool: _Pool) -> None:
        self.pool = pool
        self.in_tx = False

    async def execute(self, sql: str, *args: Any) -> str:
        return await self.pool.execute(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return await self.pool.fetch(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return await self.pool.fetchval(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return await self.pool.fetchrow(sql, *args)

    def is_in_transaction(self) -> bool:
        return self.in_tx

    def transaction(self) -> _Tx:
        return _Tx(self)


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    async def __aenter__(self) -> _Conn:
        return self.conn

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Pool:
    """asyncpg pool stand-in: rows are routed by SQL substring, writes recorded."""

    def __init__(
        self,
        *,
        fetch: list[tuple[str, list[dict[str, Any]]]] | None = None,
        fetchval: list[tuple[str, Any]] | None = None,
        fetchrow: list[tuple[str, Any]] | None = None,
    ) -> None:
        self._fetch = fetch or []
        self._fetchval = fetchval or []
        self._fetchrow = fetchrow or []
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        for needle, rows in self._fetch:
            if needle in sql:
                return rows
        return []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        for needle, value in self._fetchval:
            if needle in sql:
                return value
        return 0

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        for needle, row in self._fetchrow:
            if needle in sql:
                return row
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "OK"

    def acquire(self) -> _Acquire:
        return _Acquire(_Conn(self))

    def inserts_into(self, table: str) -> list[tuple[Any, ...]]:
        return [args for sql, args in self.executed if "INSERT INTO" in sql and table in sql]


@pytest.fixture
def wrapper_hooks(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Stub the wrapper's DB/Redis hooks. ``_compute_idempotency_key`` stays real."""
    mocks = SimpleNamespace(
        circuit_check=AsyncMock(return_value=None),
        circuit_record=AsyncMock(return_value=None),
        idempotency_lookup=AsyncMock(return_value=None),
        idempotency_store=AsyncMock(return_value=None),
        usage=AsyncMock(return_value=None),
        audit=AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        agent_wrapper,
        "_load_timeout_policy",
        AsyncMock(
            return_value={
                "agent_name": "Test",
                "risk_tier": "R0",
                "soft_timeout_ms": 30_000,
                "hard_timeout_ms": 60_000,
                "retry_count": 0,
                "circuit_breaker_scope": "workspace",
                "failure_threshold": 5,
                "cool_down_seconds": 300,
            }
        ),
    )
    monkeypatch.setattr(agent_wrapper, "_circuit_check", mocks.circuit_check)
    monkeypatch.setattr(agent_wrapper, "_circuit_record", mocks.circuit_record)
    monkeypatch.setattr(agent_wrapper, "_idempotency_lookup", mocks.idempotency_lookup)
    monkeypatch.setattr(agent_wrapper, "_idempotency_store", mocks.idempotency_store)
    monkeypatch.setattr(agent_wrapper, "_write_usage_event", mocks.usage)
    monkeypatch.setattr(agent_wrapper, "emit_audit", mocks.audit)
    return mocks


def _use_pool(monkeypatch: pytest.MonkeyPatch, pool: _Pool) -> None:
    """Make the Hatchet task bodies run on ``pool`` instead of opening real ones."""
    register_runtime(pg_pool=pool, redis=None)  # type: ignore[arg-type]

    @asynccontextmanager
    async def _runtime():  # type: ignore[no-untyped-def]
        yield

    monkeypatch.setattr(p0, "_agent_runtime", _runtime)


# ---------------------------------------------------------------------------
# Finding 1 — the R2 agents run from the context the workflow builds
# ---------------------------------------------------------------------------
async def test_storage_tiering_cron_run_is_not_rejected_by_the_r2_key_check(
    monkeypatch: pytest.MonkeyPatch, wrapper_hooks: SimpleNamespace
) -> None:
    """The nightly cron has no workspace and no document. Before the fix the
    wrapper raised "R2 idempotency requires ctx.workspace_id and ctx.document_id"
    and the task still returned green with defaults."""
    _use_pool(monkeypatch, _Pool())

    out = await p0._run_storage_tiering.aio_mock_run(p0.AgentRunInput())

    # Either branch of the agent body proves it ran (aioboto3 present or not).
    assert {"note", "fatal"} & set(out.model_extra or {})
    wrapper_hooks.idempotency_lookup.assert_not_awaited()


async def test_support_packet_runs_and_dedupes_on_the_incident(
    monkeypatch: pytest.MonkeyPatch, wrapper_hooks: SimpleNamespace
) -> None:
    from georag_object_storage import StorageConfig

    from app.agents.phase0 import support_packet as sp

    def _no_credentials() -> None:
        raise ValueError("no credentials in this test")

    pool = _Pool()
    _use_pool(monkeypatch, pool)
    monkeypatch.setattr(StorageConfig, "from_env", staticmethod(_no_credentials))
    monkeypatch.setattr(sp, "emit_audit", AsyncMock(return_value=None))

    async def _run(incident: str) -> p0.SupportPacketAssembleOutput:
        return await p0._run_support_packet.aio_mock_run(
            p0.AgentRunInput(workspace_id=WORKSPACE, kwargs={"incident_id": incident})
        )

    first = await _run("INC-1")
    again = await _run("INC-1")
    other = await _run("INC-2")

    assert first.incident_id == "INC-1"
    assert first.packet_id  # the agent body ran and wrote its row
    assert len(pool.inserts_into("silver.support_packets")) == 3
    keys = [call.args[0] for call in wrapper_hooks.idempotency_lookup.await_args_list]
    assert keys[0] == keys[1], "same incident must map to the same idempotency key"
    assert keys[0] != keys[2], "a different incident must not share a key"
    assert again.incident_id == "INC-1" and other.incident_id == "INC-2"


async def test_support_packet_without_an_incident_fails_loudly(
    monkeypatch: pytest.MonkeyPatch, wrapper_hooks: SimpleNamespace
) -> None:
    pool = _Pool()
    _use_pool(monkeypatch, pool)

    with pytest.raises(p0.AgentRunFailedError, match="outcome=failure"):
        await p0._run_support_packet.aio_mock_run(p0.AgentRunInput(workspace_id=WORKSPACE))

    assert pool.executed == []


# ---------------------------------------------------------------------------
# Finding 1 — a non-success outcome is not a green run
# ---------------------------------------------------------------------------
TASKS = [
    ("_run_tenant_isolation", "_tenant_isolation_agent", p0.TenantIsolationAuditOutput),
    ("_run_lineage_walk", "_lineage_walk_agent", p0.LineageWalkOutput),
    ("_run_storage_tiering", "_storage_tiering_agent", p0.StorageTieringRunOutput),
    ("_run_index_health", "_index_health_check_agent", p0.IndexHealthCheckOutput),
    ("_run_store_recon", "_store_recon_agent", p0.StoreReconciliationRunOutput),
    ("_run_model_upgrade_watch", "_model_upgrade_watch_agent", p0.ModelUpgradeWatchRunOutput),
    ("_run_model_cost_summary", "_model_cost_summary_agent", p0.ModelCostSummaryRunOutput),
    ("_run_llm_incident", "_llm_incident_agent", p0.LlmIncidentDiagnosisRunOutput),
    ("_run_support_packet", "_support_packet_agent", p0.SupportPacketAssembleOutput),
]


def _agent_returning(outcome: str, value: dict[str, Any] | None, error: str | None = None) -> Any:
    async def _agent(*, ctx: AgentContext, **_kw: Any) -> AgentResult[Any]:
        return AgentResult(value=value, outcome=outcome, ctx=ctx, duration_ms=1, error=error)  # type: ignore[arg-type]

    return _agent


@pytest.mark.parametrize("outcome", ["failure", "timeout", "circuit_open"])
@pytest.mark.parametrize("task_attr,agent_attr,_model", TASKS, ids=[t[0] for t in TASKS])
async def test_a_failed_agent_makes_the_hatchet_run_fail(
    monkeypatch: pytest.MonkeyPatch, task_attr: str, agent_attr: str, _model: type, outcome: str
) -> None:
    _use_pool(monkeypatch, _Pool())
    monkeypatch.setattr(p0, agent_attr, _agent_returning(outcome, None, "RuntimeError: boom"))

    with pytest.raises(p0.AgentRunFailedError, match=f"outcome={outcome}.*boom"):
        await getattr(p0, task_attr).aio_mock_run(
            p0.AgentRunInput(workspace_id=WORKSPACE, kwargs={})
        )


@pytest.mark.parametrize("task_attr,agent_attr,_model", TASKS, ids=[t[0] for t in TASKS])
async def test_a_refusal_fails_the_run_without_a_retry(
    monkeypatch: pytest.MonkeyPatch, task_attr: str, agent_attr: str, _model: type
) -> None:
    """A refusal is a deliberate "no answer", so retrying cannot change it, but
    the workflow produced nothing and must not look like it succeeded."""
    _use_pool(monkeypatch, _Pool())
    monkeypatch.setattr(
        p0, agent_attr, _agent_returning("refusal", None, "insufficient context for diagnosis")
    )

    with pytest.raises(NonRetryableException, match="insufficient context"):
        await getattr(p0, task_attr).aio_mock_run(
            p0.AgentRunInput(workspace_id=WORKSPACE, kwargs={})
        )


@pytest.mark.parametrize("outcome", ["success", "deduped"])
@pytest.mark.parametrize("task_attr,agent_attr,model", TASKS, ids=[t[0] for t in TASKS])
async def test_success_and_dedupe_still_return_the_typed_output(
    monkeypatch: pytest.MonkeyPatch, task_attr: str, agent_attr: str, model: type, outcome: str
) -> None:
    _use_pool(monkeypatch, _Pool())
    monkeypatch.setattr(p0, agent_attr, _agent_returning(outcome, {"replayed": True}))

    out = await getattr(p0, task_attr).aio_mock_run(
        p0.AgentRunInput(workspace_id=WORKSPACE, kwargs={})
    )

    assert isinstance(out, model)
    assert (out.model_extra or {}).get("replayed") is True


def test_unreported_verdicts_default_to_unknown_not_clean() -> None:
    assert p0.TenantIsolationAuditOutput().violations is None
    assert p0.TenantIsolationAuditOutput().violation_details is None
    assert p0.TenantIsolationAuditOutput().set_local_violations is None
    assert p0.LineageWalkOutput().is_intact is None
    assert p0.LineageWalkOutput().broken_at is None
    assert p0.StorageTieringRunOutput().errors is None
    assert p0.IndexHealthCheckOutput().bloat_findings is None
    assert p0.IndexHealthCheckOutput().findings_unpersisted is None
    assert p0.StoreReconciliationRunOutput().dead_lettered is None
    assert p0.StoreReconciliationRunOutput().cross_store_drift is None
    assert p0.ModelUpgradeWatchRunOutput().errors is None
    assert p0.ModelCostSummaryRunOutput().errors is None
    assert p0.LlmIncidentDiagnosisRunOutput().diagnosis is None
    assert p0.SupportPacketAssembleOutput().packet_id is None


# ---------------------------------------------------------------------------
# Wrapper: deduped replay returns a dict; the breaker is not re-armed by itself
# ---------------------------------------------------------------------------
async def test_idempotency_lookup_decodes_the_stored_json(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = {"packet_id": "p-1", "incident_id": "INC-1"}
    pool = _Pool(
        fetchrow=[("idempotency_keys", {"id": 1, "result_summary": json.dumps(stored), "outcome": "success"})]
    )
    register_runtime(pg_pool=pool, redis=None)  # type: ignore[arg-type]

    found = await agent_wrapper._idempotency_lookup(b"k")

    assert found is not None
    assert found["result_summary"] == stored


async def test_idempotency_lookup_misses_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    register_runtime(pg_pool=_Pool(), redis=None)  # type: ignore[arg-type]
    assert await agent_wrapper._idempotency_lookup(b"k") is None


async def test_a_call_the_breaker_rejects_does_not_rearm_it(
    wrapper_hooks: SimpleNamespace,
) -> None:
    @georag_agent(name="Breaker Test", risk_tier="R0", version="0")
    async def _agent(ctx: AgentContext) -> dict[str, Any]:
        raise AssertionError("must not run while the breaker is open")

    wrapper_hooks.circuit_check.side_effect = AgentCircuitOpenError("open")

    result = await _agent(ctx=AgentContext(workspace_id=None))

    assert result.outcome == "circuit_open"
    wrapper_hooks.circuit_record.assert_not_awaited()


async def test_a_genuine_failure_still_counts_against_the_breaker(
    wrapper_hooks: SimpleNamespace,
) -> None:
    @georag_agent(name="Breaker Test", risk_tier="R0", version="0")
    async def _agent(ctx: AgentContext) -> dict[str, Any]:
        raise RuntimeError("boom")

    result = await _agent(ctx=AgentContext(workspace_id=None))

    assert result.outcome == "failure"
    wrapper_hooks.circuit_record.assert_awaited_once()
    assert wrapper_hooks.circuit_record.await_args.kwargs["success"] is False


# ---------------------------------------------------------------------------
# Store reconciliation: NULL-workspace rows (finding 1) and zero-vs-zero (10)
# ---------------------------------------------------------------------------
def _dead_letter(workspace_id: str | None, source_id: str) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "workspace_id": workspace_id,
        "source_schema": "silver",
        "source_table": "collars",
        "source_id": source_id,
        "target_store": "qdrant",
        "target_collection": "georag_chunks",
        "dead_lettered_at": datetime(2026, 10, 9, tzinfo=UTC),
    }


class _FakeQdrant:
    """Records the workspace each count() was filtered on and answers from a table."""

    counts: dict[str, int] = {}
    asked: list[str] = []
    closed = 0

    def __init__(self, **_kw: Any) -> None:
        pass

    async def count(self, *, collection_name: str, count_filter: Any, exact: bool) -> Any:
        workspace = count_filter.must[0].match.value
        type(self).asked.append(workspace)
        return SimpleNamespace(count=type(self).counts[workspace])

    async def close(self) -> None:
        type(self).closed += 1


@pytest.fixture
def recon(monkeypatch: pytest.MonkeyPatch, wrapper_hooks: SimpleNamespace) -> SimpleNamespace:
    from app.agents.phase0 import store_reconciliation as sr

    _FakeQdrant.counts = {}
    _FakeQdrant.asked = []
    _FakeQdrant.closed = 0
    monkeypatch.setattr("qdrant_client.AsyncQdrantClient", _FakeQdrant)

    pg_counts: dict[str, int] = {}
    scoped_to: list[str] = []

    @asynccontextmanager
    async def _scoped(pool: Any, *, workspace_id: str, site: str = "unknown"):  # type: ignore[no-untyped-def]
        scoped_to.append(workspace_id)

        class _C:
            async def fetchval(self, sql: str, *_a: Any) -> int:
                return pg_counts.get(workspace_id, 0) if "document_passages" in sql else 1

        yield _C()

    monkeypatch.setattr(sr, "scoped_connection", _scoped)
    return SimpleNamespace(
        module=sr, pg_counts=pg_counts, scoped_to=scoped_to, qdrant=_FakeQdrant, monkeypatch=monkeypatch
    )


async def test_a_dead_letter_with_no_workspace_is_counted_not_inserted(
    recon: SimpleNamespace,
) -> None:
    pool = _Pool(
        fetch=[
            (
                "status = 'dead_lettered'",
                [_dead_letter(None, "orphan"), _dead_letter(WORKSPACE, "scoped")],
            )
        ]
    )
    register_runtime(pg_pool=pool, redis=None)  # type: ignore[arg-type]
    recon.pg_counts[WORKSPACE] = 5
    recon.qdrant.counts[WORKSPACE] = 5

    result = await recon.module.store_reconciliation_run(ctx=AgentContext(workspace_id=WORKSPACE))

    assert result.outcome == "success", result.error
    findings = pool.inserts_into("silver.store_reconciliation_findings")
    assert [args[0] for args in findings] == [WORKSPACE]  # the NOT NULL column never sees None
    assert result.value["dead_lettered"] == 1
    assert result.value["unscoped_skipped"] == 1


@pytest.mark.parametrize("kind", ["stuck", "missing"])
async def test_stuck_and_missing_rows_without_a_workspace_are_skipped_too(
    recon: SimpleNamespace, kind: str
) -> None:
    needle = "status = 'in_flight'" if kind == "stuck" else "NOT EXISTS"
    row = {
        **_dead_letter(None, "orphan"),
        "last_attempted_at": None,
        "enqueued_at": datetime(2026, 10, 9, tzinfo=UTC),
    }
    pool = _Pool(fetch=[(needle, [row])])
    register_runtime(pg_pool=pool, redis=None)  # type: ignore[arg-type]
    recon.pg_counts[WORKSPACE] = 0
    recon.qdrant.counts[WORKSPACE] = 0

    result = await recon.module.store_reconciliation_run(ctx=AgentContext(workspace_id=WORKSPACE))

    assert result.outcome == "success", result.error
    assert pool.inserts_into("silver.store_reconciliation_findings") == []
    assert result.value["unscoped_skipped"] == 1


async def test_the_nightly_run_compares_every_workspace_not_none(
    recon: SimpleNamespace,
) -> None:
    """With no workspace in scope the old code counted ``workspace_id = NULL`` in
    Postgres and filtered Qdrant on the string "None": 0 against 0, every night."""
    pool = _Pool()
    register_runtime(pg_pool=pool, redis=None)  # type: ignore[arg-type]
    listed = AsyncMock(return_value=[WORKSPACE, OTHER_WORKSPACE])
    recon.monkeypatch.setattr(recon.module, "list_workspace_ids", listed)
    recon.pg_counts.update({WORKSPACE: 100, OTHER_WORKSPACE: 50})
    recon.qdrant.counts.update({WORKSPACE: 100, OTHER_WORKSPACE: 10})  # the second drifts

    result = await recon.module.store_reconciliation_run(ctx=AgentContext(workspace_id=None))

    assert result.outcome == "success", result.error
    listed.assert_awaited_once()
    assert recon.scoped_to == [WORKSPACE, OTHER_WORKSPACE]  # counted with each scope bound
    assert recon.qdrant.asked == [WORKSPACE, OTHER_WORKSPACE]
    assert "None" not in recon.qdrant.asked
    assert recon.qdrant.closed == 1  # one client for the whole sweep, closed

    drift = result.value["cross_store_drift"]
    assert drift[WORKSPACE]["qdrant_georag_chunks"]["is_drift"] is False
    assert drift[OTHER_WORKSPACE]["qdrant_georag_chunks"] == {
        "pg": 50,
        "store": 10,
        "abs_diff": 40,
        "rel_drift": 0.8,
        "is_drift": True,
    }
    assert "cross_store_skipped" not in result.value

    written = pool.inserts_into("silver.store_reconciliation_findings")
    assert [(args[0], args[1]) for args in written] == [(OTHER_WORKSPACE, "qdrant_georag_chunks")]


async def test_the_nightly_run_says_so_when_there_is_nothing_to_compare(
    recon: SimpleNamespace,
) -> None:
    register_runtime(pg_pool=_Pool(), redis=None)  # type: ignore[arg-type]
    recon.monkeypatch.setattr(recon.module, "list_workspace_ids", AsyncMock(return_value=[]))

    result = await recon.module.store_reconciliation_run(ctx=AgentContext(workspace_id=None))

    assert result.outcome == "success", result.error
    assert result.value["cross_store_drift"] == {}
    assert result.value["cross_store_skipped"] == "no workspaces to compare"


async def test_a_failed_workspace_listing_is_reported_not_swallowed(
    recon: SimpleNamespace,
) -> None:
    register_runtime(pg_pool=_Pool(), redis=None)  # type: ignore[arg-type]
    recon.monkeypatch.setattr(
        recon.module, "list_workspace_ids", AsyncMock(side_effect=RuntimeError("pg down"))
    )

    result = await recon.module.store_reconciliation_run(ctx=AgentContext(workspace_id=None))

    assert result.outcome == "success", result.error
    assert result.value["cross_store_skipped"] == "workspace enumeration failed: RuntimeError"


# ---------------------------------------------------------------------------
# Finding 9: the soft warning re-arms each month
# ---------------------------------------------------------------------------
def _ceiling(last_sent: datetime | None, last_pct: int | None) -> dict[str, Any]:
    return {
        "workspace_id": WORKSPACE,
        "monthly_ceiling_usd": 100,
        "soft_warn_threshold_pct": 80,
        "hard_stop_threshold_pct": 100,
        "last_warn_sent_at": last_sent,
        "last_warn_pct": last_pct,
    }


async def _run_cost_summary(
    monkeypatch: pytest.MonkeyPatch, ceiling: dict[str, Any], mtd_usd: float
) -> tuple[Any, _Pool, AsyncMock]:
    from app.agents.phase0 import model_cost_summary as mcs

    pool = _Pool(
        fetch=[("workspace_cost_ceilings", [ceiling])],
        fetchval=[("usage_aggregates_daily", mtd_usd)],
    )
    register_runtime(pg_pool=pool, redis=None)  # type: ignore[arg-type]
    audit = AsyncMock(return_value=None)
    monkeypatch.setattr(mcs, "emit_audit", audit)
    monkeypatch.delenv("SLACK_NOTIFICATION_WEBHOOK_URL", raising=False)
    result = await mcs.model_cost_summary_run(
        ctx=AgentContext(workspace_id=None), rollup_date=date(2026, 10, 10)
    )
    return result, pool, audit


async def test_last_months_peak_does_not_silence_this_months_warning(
    monkeypatch: pytest.MonkeyPatch, wrapper_hooks: SimpleNamespace
) -> None:
    # September peaked at 95%; October is at 85% (>= the 80% threshold).
    result, pool, audit = await _run_cost_summary(
        monkeypatch, _ceiling(datetime(2026, 9, 28, tzinfo=UTC), 95), mtd_usd=85
    )

    assert result.outcome == "success", result.error
    assert result.value["warnings_emitted"] == 1
    audit.assert_awaited_once()
    updates = [args for sql, args in pool.executed if "UPDATE usage.workspace_cost_ceilings" in sql]
    assert updates == [(WORKSPACE, 85)]


async def test_a_higher_mark_already_warned_this_month_still_suppresses(
    monkeypatch: pytest.MonkeyPatch, wrapper_hooks: SimpleNamespace
) -> None:
    result, _pool, audit = await _run_cost_summary(
        monkeypatch, _ceiling(datetime(2026, 10, 5, tzinfo=UTC), 90), mtd_usd=85
    )

    assert result.value["warnings_emitted"] == 0
    audit.assert_not_awaited()


async def test_a_workspace_never_warned_before_is_warned(
    monkeypatch: pytest.MonkeyPatch, wrapper_hooks: SimpleNamespace
) -> None:
    result, _pool, audit = await _run_cost_summary(monkeypatch, _ceiling(None, None), mtd_usd=85)

    assert result.value["warnings_emitted"] == 1
    audit.assert_awaited_once()
