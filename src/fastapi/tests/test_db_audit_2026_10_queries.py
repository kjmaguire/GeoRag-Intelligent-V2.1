"""Query-shape tests for the 2026-10 database audit changes (item 6).

Each query below used to wrap a column in a function inside the WHERE, which
cannot use an index. They now use the indexable form; the matching indexes are
created by database/migrations/2026_10_04_200200 and verified against a real
catalogue in tests/Feature/Tenancy/DatabaseAuditHardeningMigrationsTest.php.
These tests hold the Python side: the operator is in the SQL, the thresholds
reach the GUC the operator reads, tenant scope is still explicit, and a
bounding box can never exclude a row the exact geography test would keep.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.agent.entity_resolver import resolve_entity
from app.services import qdrant_fallback

WS = "11111111-1111-4111-8111-111111111111"


class _Conn:
    def __init__(self, rows: list[dict[str, Any]] | None = None, fetchval: Any = True) -> None:
        self.rows = rows or []
        self.fetchval_result = fetchval
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.in_tx = False

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append(("execute", sql, args))
        return "OK"

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append(("fetch", sql, args))
        return self.rows

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append(("fetchrow", sql, args))
        return None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append(("fetchval", sql, args))
        return self.fetchval_result

    def is_in_transaction(self) -> bool:
        return self.in_tx

    def transaction(self):  # noqa: ANN201
        conn = self

        @asynccontextmanager
        async def _tx():  # noqa: ANN202
            conn.in_tx = True
            try:
                yield conn
            finally:
                conn.in_tx = False

        return _tx()


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def acquire(self):  # noqa: ANN201
        conn = self.conn

        @asynccontextmanager
        async def _ctx():  # noqa: ANN202
            yield conn

        return _ctx()


# ---------------------------------------------------------------------------
# qdrant_fallback
# ---------------------------------------------------------------------------


async def test_fallback_uses_the_indexable_operator_and_sets_its_threshold_first() -> None:
    conn = _Conn(rows=[{"id": "p1", "score": 0.7, "payload": '{"passage_text": "x", "ordinal": 1}'}])
    out = await qdrant_fallback._pg_trgm_search(
        pg_pool=_Pool(conn), query_text="unconformity uranium", workspace_id=WS, limit=5,
    )

    verbs = [c[0] for c in conn.calls if "set_config('pg_trgm" in c[1] or c[0] == "fetch"]
    assert verbs == ["execute", "fetch"], "the threshold GUC must be set before the query reads it"
    (_, set_sql, set_args) = next(c for c in conn.calls if "pg_trgm.strict_word_similarity_threshold" in c[1])
    assert set_args == ("0.3",)
    assert ", true)" in set_sql, "must be transaction-local (SET LOCAL semantics)"

    (_, query, args) = next(c for c in conn.calls if c[0] == "fetch")
    assert "$1 <<% text" in query
    assert "strict_word_similarity($1, text) > 0.3" not in query
    assert "workspace_id = $2::uuid" in query
    assert args == ("unconformity uranium", WS, 5)

    # jsonb arrives as TEXT (no codec is registered): it must still parse.
    assert out == [{"id": "p1", "score": 0.7, "payload": {"passage_text": "x", "ordinal": 1}}]


@pytest.mark.parametrize("raw,expected", [
    (None, {}), ("", {}), ('{"a": 1}', {"a": 1}), (b'{"a": 2}', {"a": 2}), ({"a": 3}, {"a": 3}),
])
def test_payload_dict_accepts_text_bytes_and_mappings(raw: Any, expected: dict[str, Any]) -> None:
    assert qdrant_fallback._payload_dict(raw) == expected


# ---------------------------------------------------------------------------
# entity_resolver
# ---------------------------------------------------------------------------


async def test_fuzzy_lookup_is_workspace_scoped_and_index_shaped() -> None:
    conn = _Conn()
    await resolve_entity(
        _Pool(conn), workspace_id=WS, entity_type="property",
        entity_text="Crackingstone Properties", fuzzy_threshold=0.6, log_gap_on_miss=False,
    )
    fetchrows = [c for c in conn.calls if c[0] == "fetchrow"]
    exact, fuzzy = fetchrows

    assert "workspace_id = $3::uuid" in exact[1] and exact[2][2] == WS

    assert "alias_normalised % $2" in fuzzy[1]
    assert "workspace_id = $4::uuid" in fuzzy[1] and fuzzy[2][3] == WS
    assert "similarity(alias_normalised, $2) >= $3" in fuzzy[1], "exact recheck kept"

    set_call = next(c for c in conn.calls if "pg_trgm.similarity_threshold" in c[1])
    assert set_call[2] == ("0.6",)
    order = [c[0] for c in conn.calls if c is set_call or c is fuzzy]
    assert order == ["execute", "fetchrow"], "threshold set before the query"


@pytest.mark.parametrize("given,expected", [(0.0, "0.0"), (1.0, "1.0"), (1.5, "1.0"), (-0.2, "0.0")])
async def test_fuzzy_threshold_is_clamped_into_the_range_the_guc_accepts(given: float, expected: str) -> None:
    conn = _Conn()
    await resolve_entity(
        _Pool(conn), workspace_id=WS, entity_type="property",
        entity_text="x", fuzzy_threshold=given, log_gap_on_miss=False,
    )
    set_call = next(c for c in conn.calls if "pg_trgm.similarity_threshold" in c[1])
    assert set_call[2] == (expected,)
