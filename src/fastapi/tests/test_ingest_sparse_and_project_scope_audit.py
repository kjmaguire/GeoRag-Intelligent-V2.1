"""Audit 2026-09-29, batch B: ingest/query parity for the sparse leg and
project scoping of synthesized passages.

RAG-10  SPLADE encodes long text in overlapping windows (it truncated at 512
        tokens, so ~2/3 of every 5,000-char chunk never reached the sparse
        leg).
RAG-11  a text passage whose sparse encode FAILED is left unembedded for
        retry, not written dense-only and marked embedded.
RAG-9   synthesized passages carry project_id, and project_or_public
        retrieval no longer admits project-less synthesized summaries.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# RAG-10 — windowed SPLADE
# ---------------------------------------------------------------------------


class _Tok:
    """Word-level tokenizer: token id = 1000 + word position (unique per
    position, so which positions reached the model is observable)."""

    cls_token_id = 1
    sep_token_id = 2
    pad_token_id = 0

    def __call__(self, text, add_special_tokens=True, truncation=False, **_):
        ids = [1000 + i for i, _w in enumerate(text.split())]
        return {"input_ids": ids}


class _Model:
    """Logit 1.0 at vocab index == input id, zero elsewhere."""

    vocab = 4000

    def __call__(self, input_ids, attention_mask):
        import torch

        batch, width = input_ids.shape
        logits = torch.zeros((batch, width, self.vocab))
        for b in range(batch):
            for p in range(width):
                logits[b, p, int(input_ids[b, p])] = 1.0

        class _Out:
            pass

        out = _Out()
        out.logits = logits
        return out


def test_short_text_is_not_windowed():
    from app.services.sparse_encoder import _window_token_ids

    assert _window_token_ids(_Tok(), " ".join(["w"] * 510)) is None


def test_long_text_windows_cover_every_token_with_overlap():
    from app.services.sparse_encoder import (
        _WINDOW_CONTENT_TOKENS,
        _window_token_ids,
    )

    windows = _window_token_ids(_Tok(), " ".join(["w"] * 1300))
    assert windows is not None and len(windows) >= 3
    assert all(len(w) <= _WINDOW_CONTENT_TOKENS for w in windows)
    covered = {t for w in windows for t in w}
    assert covered == {1000 + i for i in range(1300)}
    assert set(windows[0]) & set(windows[1])  # overlap


def test_windowed_encode_keeps_tokens_past_512():
    from app.services.sparse_encoder import _encode_windows, _window_token_ids

    tok = _Tok()
    windows = _window_token_ids(tok, " ".join(["w"] * 1300))
    vec = _encode_windows(tok, _Model(), windows)
    # Position 1299 is far beyond the old 512-token cut.
    assert 1000 + 1299 in vec
    assert 1000 + 700 in vec
    assert vec[1000 + 1299] == pytest.approx(0.6931, abs=1e-3)  # log1p(1)


def test_in_place_splade_weights_match_the_out_of_place_formula():
    """2026-09-30: the aggregation was rewritten in place to stop the sparse
    sidecar allocating four vocab-sized tensors per forward pass (it was
    OOM-killed on the first production ingest). The numbers must not move,
    or every stored sparse vector stops matching new query vectors."""
    import torch

    from app.services.sparse_encoder import _splade_weights

    torch.manual_seed(0)
    logits = torch.randn(3, 7, 50)
    mask = torch.tensor([[1] * 7, [1] * 4 + [0] * 3, [1] * 2 + [0] * 5])
    expected = (torch.log1p(torch.relu(logits)) * mask.unsqueeze(-1)).amax(dim=1)

    got = _splade_weights(logits.clone(), mask).amax(dim=1)

    assert torch.allclose(got, expected)


class _RecordingModel(_Model):
    """Records each forward's batch size and whether the lock was held."""

    def __init__(self):
        self.batches: list[int] = []
        self.locked: list[bool] = []

    def __call__(self, input_ids, attention_mask):
        from app.services.sparse_encoder import _FORWARD_LOCK

        self.batches.append(int(input_ids.shape[0]))
        self.locked.append(_FORWARD_LOCK.locked())
        return super().__call__(input_ids, attention_mask)


def test_long_text_forwards_are_bounded_and_serialised():
    from app.services.sparse_encoder import (
        _WINDOWS_PER_FORWARD,
        _encode_windows,
        _window_token_ids,
    )

    tok = _Tok()
    windows = _window_token_ids(tok, " ".join(["w"] * 3000))
    model = _RecordingModel()
    vec = _encode_windows(tok, model, windows)

    assert len(windows) > _WINDOWS_PER_FORWARD  # the input really was split
    assert max(model.batches) <= _WINDOWS_PER_FORWARD <= 4
    assert all(model.locked), "every forward pass must hold _FORWARD_LOCK"
    assert 1000 + 2999 in vec  # still covers the whole text


def test_batch_mixes_short_and_long_in_order(monkeypatch):
    import app.services.sparse_encoder as se

    monkeypatch.setattr(se, "SPARSE_SERVICE_URL", "")
    monkeypatch.setattr(se, "_get_sparse_model", lambda: (_Tok(), _Model()))
    calls: list[list[str]] = []

    def fake_short(tokenizer, model, batch):
        calls.append(batch)
        return [{7: 1.0} for _ in batch]

    monkeypatch.setattr(se, "_encode_short_batch", fake_short)
    long_text = " ".join(["w"] * 1300)
    out = se.encode_sparse_batch(["a b", long_text, "c"])
    assert calls == [["a b", "c"]]
    assert out[0] == {7: 1.0} and out[2] == {7: 1.0}
    assert 1000 + 1299 in out[1]


# ---------------------------------------------------------------------------
# RAG-11 / RAG-9 — passage_embedder
# ---------------------------------------------------------------------------


def _passage(text: str, **extra: Any) -> dict[str, Any]:
    row = {
        "passage_id": str(uuid.uuid4()), "document_id": None,
        "contextualized_content": None, "text": text, "ordinal": 0,
        "page_first": None, "page_last": None, "ocr_confidence": None,
        "ocr_method": None, "ocr_status": None,
        "chunk_kind": "structured_summary", "modality": "text",
        "page_number": None, "image_object_key": None,
        "report_title": "structured_summary",
        "project_id": "11111111-1111-1111-1111-111111111111",
    }
    row.update(extra)
    return row


class _PgConn:
    def __init__(self, rows):
        self.rows = rows
        self.queries: list[str] = []
        self.writebacks: list[tuple] = []

    async def fetch(self, sql, *args):
        self.queries.append(sql)
        return self.rows

    async def execute(self, sql, *args):
        return "OK"

    async def executemany(self, sql, args):
        self.writebacks.extend(args)

    async def close(self):
        return None


class _Qdrant:
    def __init__(self):
        self.points = []

    async def upsert(self, collection_name, points, wait):
        self.points.extend(points)

    async def retrieve(self, collection_name, ids, with_payload, with_vectors):
        by_id = {p.id: p for p in self.points}

        class _R:
            def __init__(self, payload):
                self.payload = payload

        return [_R(by_id[i].payload) for i in ids if i in by_id]

    async def close(self):
        return None


class _Dense:
    def encode(self, texts, normalize_embeddings=True, show_progress_bar=False):
        import numpy as np

        return np.ones((len(texts), 4))


@pytest.mark.asyncio
async def test_failed_sparse_encode_leaves_the_passage_for_retry(monkeypatch):
    import app.services.ingest.passage_embedder as pe
    import app.services.sparse_encoder as se

    ok, bad = _passage("collar PLS-22-08"), _passage("SPARSE-DOWN")
    conn = _PgConn([ok, bad])

    async def _connect(*a, **k):
        return conn

    async def _bind(*a, **k):
        return None

    def fake_sparse(text):
        if text == "SPARSE-DOWN":
            raise ConnectionError("sparse sidecar unreachable")
        return {5: 1.0}

    monkeypatch.setattr(pe.asyncpg, "connect", _connect)
    monkeypatch.setattr(pe, "bind_workspace_scope", _bind)
    monkeypatch.setattr(pe, "_dsn", lambda: "postgresql://x")
    monkeypatch.setattr(se, "encode_sparse", fake_sparse)

    qdrant = _Qdrant()
    result = await pe.embed_pending_passages(
        workspace_id="ws", embedding_model=_Dense(), qdrant_client=qdrant,
        concurrency=1,
    )

    written = [p.payload["text"] for p in qdrant.points]
    assert written == ["collar PLS-22-08"]
    assert "text" in qdrant.points[0].vector  # the sparse slot is present
    assert [pid for _eid, pid in conn.writebacks] == [ok["passage_id"]]
    assert result.passages_skipped == 1
    assert any(e.startswith("sparse_encode_failed") for e in result.errors)
    # RAG-9: the synthesized passage's project reaches the payload.
    assert qdrant.points[0].payload["project_id"] == ok["project_id"]
    assert "COALESCE(r.project_id, dp.project_id)" in conn.queries[0]


# ---------------------------------------------------------------------------
# RAG-9 — the retrieval filter, evaluated by a real (in-memory) Qdrant
# ---------------------------------------------------------------------------


def test_project_or_public_excludes_projectless_synthesized_summaries(monkeypatch):
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, PointStruct, VectorParams

    from app.agent.tools import _build_document_scope_filter
    from app.config import settings

    monkeypatch.setattr(settings, "QDRANT_DOCUMENT_PROJECT_SCOPE", "project_or_public")
    client = QdrantClient(":memory:")
    client.create_collection("c", vectors_config=VectorParams(size=2, distance=Distance.DOT))
    points = {
        1: {"project_id": "A", "chunk_kind": "structured_summary"},
        2: {"chunk_kind": "structured_summary"},          # pre-RAG-9 orphan
        3: {"chunk_kind": "public_geo_synthesis"},        # public, no project
        4: {"project_id": "B", "chunk_kind": "narrative"},
        5: {"project_id": "public", "chunk_kind": "narrative"},
        6: {"chunk_kind": "narrative"},                   # legacy public report
    }
    client.upsert("c", [
        PointStruct(id=i, vector=[1.0, 0.0], payload=p) for i, p in points.items()
    ])
    hits, _ = client.scroll("c", scroll_filter=_build_document_scope_filter("A"), limit=10)
    assert sorted(h.id for h in hits) == [1, 3, 5, 6]
