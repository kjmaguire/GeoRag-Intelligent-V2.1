"""Regression tests — `factual_lookup` sub-queries against silver.structure /
silver.alteration must actually resolve through the agentic query planner.

Context (2026-08-15 audit): `_SilverTable` in app/models/decomposition.py
listed "structures"/"alterations" (plural) as valid `FactualLookupInput.table`
values. `_dispatch_factual_lookup` builds `table=f"silver.{inp.table}"` with
no pluralization mapping, so those Literal values produced "silver.structures"
/ "silver.alterations" — table names that were DROPPED by
database/migrations/2026_05_20_060400_create_silver_geological_singulars.php
in favour of the singular "silver.structure" / "silver.alteration".

Before the same-day tenancy-scoping fix to verify_numerical_claim's allowlist
(app/agent/tools.py), this hit a DB "relation does not exist" error. After
that fix (which correctly keys the allowlist off the real singular names),
the same plural input instead hits "BLOCKED — table not in allowlist"
immediately — a different failure mode, same dead end. Neither was ever
exercised by a test at the plan_executor / decomposition-contract level;
tests/test_agent_tools.py only covers verify_numerical_claim directly with
literal SQL-side table strings, not the FactualLookupInput -> _dispatch_
factual_lookup path an LLM-planned sub-query actually takes.

This file locks in the fix: `_SilverTable` now uses the singular
"structure"/"alteration" so `_dispatch_factual_lookup` produces the real
table name and the lookup resolves.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.deps import AgentDeps, ToolContext
from app.agent.plan_executor import _dispatch_factual_lookup
from app.models.decomposition import FactualLookupInput, SubQueryFactualLookup


class _TxnCM:
    """Async context manager stand-in for asyncpg's ``conn.transaction()``."""

    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _make_ctx(fetchrow_return: dict) -> ToolContext:
    mock_conn = AsyncMock()
    mock_conn.fetchrow = AsyncMock(return_value=fetchrow_return)
    # verify_numerical_claim acquires through AgentDeps.acquire_scoped()
    # (2026-08-22), which does `async with conn.transaction():`. A bare
    # AsyncMock's .transaction() returns a coroutine rather than an async
    # context manager, and the tool's blanket handler swallows the
    # resulting TypeError into db_value=None.
    mock_conn.transaction = MagicMock(return_value=_TxnCM())
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    deps = AgentDeps(
        pg_pool=mock_pool,
        qdrant_client=None,
        neo4j_driver=None,
        project_id="00000000-0000-0000-0000-0000000000aa",
        embedding_model=None,
        reranker=None,
    )
    return ToolContext(deps)


@pytest.mark.asyncio
async def test_factual_lookup_structure_table_resolves() -> None:
    """table="structure" (singular, matching the real schema) must reach
    silver.structure and return the looked-up value rather than BLOCKED."""
    ctx = _make_ctx({"true_dip": 45.0})
    sq = SubQueryFactualLookup(
        id="sq-1",
        sub_query_class="factual_lookup",
        input=FactualLookupInput(
            table="structure",
            entity_id="structure-uuid-001",
            fields=["true_dip"],
        ),
        latency_budget_s=5.0,
    )

    result = await _dispatch_factual_lookup(sq, ctx)

    assert result["value"] == 45.0
    assert "silver:structure:" in result["source_chunk_id"]


@pytest.mark.asyncio
async def test_factual_lookup_alteration_table_resolves() -> None:
    """table="alteration" (singular, matching the real schema) must reach
    silver.alteration and return the looked-up value rather than BLOCKED."""
    ctx = _make_ctx({"intensity": 3.0})
    sq = SubQueryFactualLookup(
        id="sq-1",
        sub_query_class="factual_lookup",
        input=FactualLookupInput(
            table="alteration",
            entity_id="alteration-uuid-001",
            fields=["intensity"],
        ),
        latency_budget_s=5.0,
    )

    result = await _dispatch_factual_lookup(sq, ctx)

    assert result["value"] == 3.0
    assert "silver:alteration:" in result["source_chunk_id"]


def test_silver_table_literal_uses_singular_names() -> None:
    """Guard against re-introducing the plural "structures"/"alterations"
    values, which don't correspond to any real table (see module docstring)."""
    from app.models.decomposition import _SilverTable

    allowed = set(_SilverTable.__args__)
    assert "structure" in allowed
    assert "alteration" in allowed
    assert "structures" not in allowed
    assert "alterations" not in allowed
