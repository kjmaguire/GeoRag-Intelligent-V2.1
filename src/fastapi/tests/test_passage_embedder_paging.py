"""embed_pending_passages pages the backlog instead of loading all of it.

It used to run ``SELECT ... WHERE embedding_id IS NULL ORDER BY created_at``
with ``max_passages=None`` and hold every pending passage (text plus
contextualized_content) in memory at once. Now: keyset pages on
``(created_at, passage_id)``, PENDING_PAGE_SIZE rows per query.

The fake connection implements the keyset semantics from the SQL it is given,
so these tests exercise the real ``pending_page_query`` output, not a script.
"""

from __future__ import annotations

import re
import types
import uuid
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import numpy as np
import pytest

from app.services.ingest import passage_embedder as pe

WS = "a0000000-0000-0000-0000-000000000001"
T0 = datetime(2026, 10, 1, 12, 0, 0)


def _row(i: int, *, created_at: datetime | None = ..., modality: str = "text") -> dict:
    return {
        "passage_id": str(uuid.UUID(int=i + 1)),
        "document_id": str(uuid.UUID(int=10_000)),
        "contextualized_content": None,
        "text": f"passage {i} text about the Austin zone",
        "ordinal": i,
        "page_first": 1, "page_last": 1,
        "ocr_confidence": None, "ocr_method": None, "ocr_status": None,
        "chunk_kind": "narrative",
        "created_at": T0 + timedelta(seconds=i // 3) if created_at is ... else created_at,
        "modality": modality, "page_number": None, "image_object_key": None,
        "report_title": "Report", "project_id": str(uuid.UUID(int=20_000)),
    }


class FakeConn:
    """Answers the pending-passage SELECT by interpreting its keyset predicate."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.embedded: dict[str, str] = {}
        self.queries: list[tuple[str, tuple]] = []
        self.closed = False

    async def execute(self, *a: Any, **k: Any) -> str:
        return "OK"

    async def fetch(self, sql: str, *params: Any) -> list[dict]:
        self.queries.append((sql, params))
        pending = [r for r in self.rows if r["passage_id"] not in self.embedded]
        has_project = "COALESCE(r.project_id, dp.project_id) = $1::uuid" in sql
        n = 1 if has_project else 0
        if "dp.created_at IS NULL AND dp.passage_id >" in sql:
            cursor_id = params[n]
            pending = [r for r in pending if r["created_at"] is None and r["passage_id"] > cursor_id]
        elif "(dp.created_at, dp.passage_id) >" in sql:
            c_at, c_id = params[n], params[n + 1]
            pending = [
                r for r in pending
                if r["created_at"] is None
                or (r["created_at"], r["passage_id"]) > (c_at, c_id)
            ]
        pending.sort(key=lambda r: (r["created_at"] is None, r["created_at"] or T0, r["passage_id"]))
        limit = int(re.search(r"LIMIT (\d+)\s*$", sql).group(1))
        return pending[:limit]

    async def executemany(self, sql: str, args: list[tuple]) -> None:
        for point_id, passage_id in args:
            self.embedded[passage_id] = point_id

    async def close(self) -> None:
        self.closed = True


class FakeModel:
    model_name = "fake-embed"

    def __init__(self) -> None:
        self.calls: list[int] = []

    def encode(self, texts, normalize_embeddings=True, show_progress_bar=False):
        self.calls.append(len(texts))
        return np.ones((len(texts), 4), dtype=float)


class FakeQdrant:
    def __init__(self) -> None:
        self.upserts: list[tuple[int, bool]] = []
        self.points: list[Any] = []

    async def upsert(self, collection_name, points, wait):
        self.upserts.append((len(points), wait))
        self.points.extend(points)

    async def retrieve(self, collection_name, ids, with_payload, with_vectors):
        point = next(p for p in self.points if p.id == ids[0])
        return [types.SimpleNamespace(payload=point.payload)]

    async def close(self) -> None:
        pass


@pytest.fixture
def harness(monkeypatch):
    def _make(rows: list[dict], *, page_size: int = 4, sparse=None):
        conn = FakeConn(rows)
        monkeypatch.setattr(pe.asyncpg, "connect", AsyncMock(return_value=conn))
        monkeypatch.setattr(pe, "bind_workspace_scope", AsyncMock())
        monkeypatch.setattr(pe, "PENDING_PAGE_SIZE", page_size)
        monkeypatch.setattr(pe, "_SPARSE_FAILURE_LOGGED", set())
        monkeypatch.setattr(
            "app.services.sparse_encoder.encode_sparse",
            sparse or (lambda text: {1: 0.5, 7: 0.2}),
        )
        return conn, FakeModel(), FakeQdrant()

    return _make


async def _run(conn, model, qdrant, **kw):
    return await pe.embed_pending_passages(
        workspace_id=WS, embedding_model=model, qdrant_client=qdrant,
        batch_size=3, concurrency=2, **kw,
    )


@pytest.mark.asyncio
async def test_the_backlog_is_fetched_in_pages_not_all_at_once(harness) -> None:
    conn, model, qdrant = harness([_row(i) for i in range(10)], page_size=4)

    result = await _run(conn, model, qdrant)

    limits = [int(re.search(r"LIMIT (\d+)\s*$", sql).group(1)) for sql, _ in conn.queries]
    assert limits and all(n == 4 for n in limits)
    assert len(conn.queries) == 3  # 4 + 4 + 2 (a short page ends the walk)
    assert result.passages_seen == 10
    assert result.passages_embedded == 10
    assert len(conn.embedded) == 10 and conn.closed


@pytest.mark.asyncio
async def test_every_query_orders_on_the_index_keys_and_keysets_after_the_first(harness) -> None:
    conn, model, qdrant = harness([_row(i) for i in range(10)], page_size=4)
    await _run(conn, model, qdrant)

    first_sql = conn.queries[0][0]
    assert "ORDER BY dp.created_at ASC, dp.passage_id ASC" in first_sql
    assert "(dp.created_at, dp.passage_id) >" not in first_sql
    for sql, params in conn.queries[1:]:
        assert "(dp.created_at, dp.passage_id) > ($1::timestamp, $2::uuid)" in sql
        assert isinstance(params[0], datetime)
        assert "ORDER BY dp.created_at ASC, dp.passage_id ASC" in sql


@pytest.mark.asyncio
async def test_the_project_filter_keeps_its_placeholder_and_the_cursor_follows_it(harness) -> None:
    conn, model, qdrant = harness([_row(i) for i in range(9)], page_size=4)
    project = str(uuid.UUID(int=20_000))

    await pe.embed_pending_passages(
        workspace_id=WS, project_id=project, embedding_model=model,
        qdrant_client=qdrant, batch_size=3, concurrency=2,
    )

    assert all(params[0] == project for _sql, params in conn.queries)
    later_sql, later_params = conn.queries[1]
    assert "COALESCE(r.project_id, dp.project_id) = $1::uuid" in later_sql
    assert "($2::timestamp, $3::uuid)" in later_sql
    assert len(later_params) == 3


@pytest.mark.asyncio
async def test_max_passages_caps_the_whole_run_not_each_page(harness) -> None:
    conn, model, qdrant = harness([_row(i) for i in range(20)], page_size=4)

    result = await _run(conn, model, qdrant, max_passages=6)

    assert result.passages_seen == 6
    assert result.passages_embedded == 6
    limits = [int(re.search(r"LIMIT (\d+)\s*$", sql).group(1)) for sql, _ in conn.queries]
    assert limits == [4, 2]


@pytest.mark.asyncio
async def test_a_skipped_passage_is_not_refetched_and_does_not_loop_forever(harness) -> None:
    """Sparse failure leaves embedding_id NULL (RAG-11). The keyset cursor is
    what stops that row coming back on the next page of the SAME run."""
    rows = [_row(i) for i in range(8)]
    bad = rows[2]["text"]

    def sparse(text: str):
        if text == bad:
            raise RuntimeError("sparse sidecar rejected this passage")
        return {1: 0.5}

    conn, model, qdrant = harness(rows, page_size=4, sparse=sparse)

    result = await _run(conn, model, qdrant)

    assert result.passages_seen == 8  # each row seen exactly once
    assert result.passages_embedded == 7
    assert rows[2]["passage_id"] not in conn.embedded  # still pending, retried next sweep
    assert any(e.startswith("sparse_encode_failed") for e in result.errors)
    # 4 + 4, then one empty page proves the end. Without the cursor the skipped
    # row would be refetched by page 2 and the walk would never terminate.
    assert len(conn.queries) == 3


@pytest.mark.asyncio
async def test_a_sparse_failure_still_discards_the_dense_vector(harness) -> None:
    """Behaviour kept: never write a dense-only point (RAG-11)."""
    rows = [_row(0), _row(1)]
    conn, model, qdrant = harness(rows, sparse=lambda t: (_ for _ in ()).throw(RuntimeError("down")))

    result = await _run(conn, model, qdrant)

    assert qdrant.points == []
    assert conn.embedded == {}
    assert result.passages_embedded == 0 and result.passages_skipped == 2


@pytest.mark.asyncio
async def test_only_the_first_batch_of_the_run_waits_for_the_upsert(harness) -> None:
    conn, model, qdrant = harness([_row(i) for i in range(10)], page_size=4)
    await _run(conn, model, qdrant)

    waits = [w for _n, w in qdrant.upserts]
    assert waits.count(True) == 1 and waits[0] is True


@pytest.mark.asyncio
async def test_an_empty_backlog_returns_after_one_query(harness) -> None:
    conn, model, qdrant = harness([])
    result = await _run(conn, model, qdrant)
    assert result.passages_seen == 0 and len(conn.queries) == 1 and conn.closed


@pytest.mark.asyncio
async def test_rows_with_a_null_created_at_are_paged_after_the_dated_ones(harness) -> None:
    """created_at is nullable; a NULL in a row-value comparison would silently
    drop those rows from every page after the first."""
    rows = [_row(i) for i in range(5)] + [_row(i, created_at=None) for i in range(5, 11)]
    conn, model, qdrant = harness(rows, page_size=4)

    result = await _run(conn, model, qdrant)

    assert result.passages_seen == 11
    assert len(conn.embedded) == 11
    assert any("dp.created_at IS NULL AND dp.passage_id >" in sql for sql, _ in conn.queries)


def test_pending_page_query_shapes() -> None:
    sql, *params = pe.pending_page_query("SELECT 1 WHERE x ", [], cursor=None, limit=7)
    assert sql.endswith("ORDER BY dp.created_at ASC, dp.passage_id ASC LIMIT 7") and params == []

    cursor = (T0, str(uuid.UUID(int=3)))
    sql, *params = pe.pending_page_query("SELECT 1 WHERE x $1 ", ["proj"], cursor=cursor, limit=9)
    assert "($2::timestamp, $3::uuid)" in sql and params == ["proj", T0, cursor[1]]

    sql, *params = pe.pending_page_query("S ", [], cursor=(None, "id"), limit=5)
    assert "dp.created_at IS NULL AND dp.passage_id > $1::uuid" in sql and params == ["id"]


def test_the_default_page_size_is_2000() -> None:
    assert pe.PENDING_PAGE_SIZE == 2000


# ---------------------------------------------------------------------------
# B16 -- the sparse-failure retry loop is made visible
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_sparse_failure_is_logged_once_per_passage_per_process(harness, caplog) -> None:
    rows = [_row(0), _row(1)]
    bad_text = rows[0]["text"]

    def sparse(text: str):
        if text == bad_text:
            raise RuntimeError("poison")
        return {1: 0.1}

    conn, model, qdrant = harness(rows, sparse=sparse)

    with caplog.at_level("ERROR", logger="georag.ingest.passage_embedder"):
        for _ in range(3):  # three sweeps over the same poisoned passage
            await _run(conn, model, qdrant)

    loud = [r for r in caplog.records if "sparse_encode_retry_loop" in r.getMessage()]
    assert len(loud) == 1
    assert rows[0]["passage_id"] in loud[0].getMessage()
    assert "embed_attempts" in loud[0].getMessage()  # names the missing column


def test_the_failure_log_set_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(pe, "_SPARSE_FAILURE_LOGGED", set())
    monkeypatch.setattr(pe, "_SPARSE_FAILURE_LOG_CAP", 3)
    for i in range(10):
        pe._log_sparse_failure_once(f"p{i}")
    assert len(pe._SPARSE_FAILURE_LOGGED) <= 3
