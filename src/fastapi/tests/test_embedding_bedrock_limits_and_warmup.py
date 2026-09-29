"""Cohere Embed v4 on Bedrock: request limits, query budget, warm-up (2026-09-29).

VEN-3  — ``_BedrockEmbedding`` sent every text in ONE InvokeModel call and
         swallowed ``batch_size``; Embed v4 takes at most 96 texts ([ASSUMED],
         vendor documentation, not probed), so reembed_qdrant.py's 100-point
         pages were rejected on every page.
VEN-6  — ``embed_query`` (query path, under TIMEOUT_QDRANT_S) used the ingest
         client: 4 attempts x 30 s.
VEN-1  — one failed warm-up at boot disabled the embedder for the life of the
         process and /ready stayed green.

No AWS call is made: the Bedrock client is always a fake.
"""

from __future__ import annotations

import asyncio
from typing import Any

import numpy as np
import pytest

from app.services import _bedrock
from app.services import embedding as emb


def _vectors(n: int, dim: int = 1024, offset: int = 0) -> dict[str, Any]:
    return {"embeddings": {"float": [[float(offset + i)] * dim for i in range(n)]}}


class TestBatchLimit:
    def _install(self, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
        sent: list[dict[str, Any]] = []

        def fake_invoke(self, body, **_kw):  # noqa: ANN001
            sent.append(body)
            return _vectors(len(body["texts"]), offset=sum(len(b["texts"]) for b in sent[:-1]))

        monkeypatch.setattr(emb._BedrockEmbedding, "_invoke", fake_invoke)
        return sent

    def test_a_100_text_call_is_split_at_96_and_reassembled_in_order(self, monkeypatch) -> None:
        sent = self._install(monkeypatch)
        texts = [f"t{i}" for i in range(100)]

        out = emb._BedrockEmbedding().encode(texts, batch_size=8)

        assert [len(b["texts"]) for b in sent] == [96, 4]
        assert sent[0]["texts"][0] == "t0" and sent[1]["texts"][-1] == "t99"
        assert out.shape == (100, 1024)
        # Row i carries the value the fake assigned to text i -- order kept.
        assert [float(v) for v in out[:, 0]] == [float(i) for i in range(100)]

    def test_no_request_ever_exceeds_the_limit(self, monkeypatch) -> None:
        sent = self._install(monkeypatch)
        emb._BedrockEmbedding().encode([f"t{i}" for i in range(500)])
        assert max(len(b["texts"]) for b in sent) <= emb.EMBED_V4_MAX_TEXTS_PER_CALL == 96

    def test_a_single_string_still_returns_one_vector(self, monkeypatch) -> None:
        self._install(monkeypatch)
        assert emb._BedrockEmbedding().encode("one").shape == (1024,)

    def test_an_empty_list_makes_no_call(self, monkeypatch) -> None:
        sent = self._install(monkeypatch)
        assert emb._BedrockEmbedding().encode([]).shape == (0, 1024)
        assert sent == []

    def test_a_short_answer_raises_rather_than_misaligning(self, monkeypatch) -> None:
        def fake_invoke(self, body, **_kw):  # noqa: ANN001
            return _vectors(len(body["texts"]) - 1)

        monkeypatch.setattr(emb._BedrockEmbedding, "_invoke", fake_invoke)
        with pytest.raises(RuntimeError, match="returned 2 vectors for 3 texts"):
            emb._BedrockEmbedding().encode(["a", "b", "c"])


class TestQueryPathBudget:
    @pytest.fixture(autouse=True)
    def _clients(self, monkeypatch: pytest.MonkeyPatch):
        seen: list[tuple[int, float]] = []

        class _Body:
            def __init__(self, payload: bytes) -> None:
                self._payload = payload

            def read(self) -> bytes:
                return self._payload

        def fake_get_client(service, *, max_attempts=4, read_timeout_s=30.0):
            seen.append((max_attempts, read_timeout_s))

            class _Client:
                @staticmethod
                def invoke_model(**kwargs):
                    import json

                    n = len(json.loads(kwargs["body"])["texts"])
                    return {"body": _Body(json.dumps(_vectors(n)).encode())}

            return _Client()

        monkeypatch.setattr(_bedrock, "get_client", fake_get_client)
        self.seen = seen

    def test_embed_query_uses_a_client_that_fits_its_budget(self) -> None:
        model = emb._BedrockEmbedding(timeout_s=30.0)
        model.embed_query("gold grades at Shirley Basin")
        attempts, read_timeout = self.seen[-1]
        worst = attempts * read_timeout + _bedrock._botocore_worst_backoff_s(attempts)
        assert worst <= 30.0, (attempts, read_timeout)
        assert attempts >= 2, "still room for one retry"
        assert read_timeout <= emb._QUERY_READ_TIMEOUT_S

    def test_ingest_encode_keeps_the_patient_profile(self) -> None:
        emb._BedrockEmbedding(timeout_s=30.0).encode(["chunk"])
        assert self.seen[-1] == (4, 30.0)


# ---------------------------------------------------------------------------
# VEN-1 — warm-up and readiness
# ---------------------------------------------------------------------------


class _FlakyModel:
    def __init__(self, failures: int, dim: int | None = 1024) -> None:
        self.failures_left = failures
        self.dim = dim
        self.calls = 0

    def encode(self, *_a: Any, **_k: Any) -> np.ndarray:
        self.calls += 1
        if self.failures_left > 0:
            self.failures_left -= 1
            raise RuntimeError("Could not connect to the endpoint URL: arn:aws:iam::123456789012:role/secret-ish")
        return np.zeros(1024, dtype=np.float32)

    def get_sentence_embedding_dimension(self) -> int | None:
        return self.dim


def test_a_failed_warm_up_marks_warming_and_keeps_the_error_out_of_the_probe() -> None:
    readiness = emb.EmbeddingReadiness()
    assert emb.warm_up_once(_FlakyModel(1), readiness) is False
    assert readiness.state == emb.EMBEDDING_WARMING
    assert not readiness.ready
    described = readiness.describe()
    assert "RuntimeError" in described
    assert "123456789012" not in described, "/ready is unauthenticated"


@pytest.mark.asyncio
async def test_the_re_warm_retries_with_backoff_until_it_succeeds() -> None:
    model = _FlakyModel(3)
    readiness = emb.EmbeddingReadiness()
    assert emb.warm_up_once(model, readiness) is False  # the boot attempt
    slept: list[float] = []
    disabled: list[str] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    await emb.rewarm_until_ready(
        model, readiness, expected_dim=1024, on_disable=disabled.append, sleep=fake_sleep
    )

    assert readiness.ready
    assert slept == [5.0, 10.0, 20.0]
    assert model.calls == 4
    assert disabled == []


@pytest.mark.asyncio
async def test_the_backoff_is_capped() -> None:
    model = _FlakyModel(12)
    readiness = emb.EmbeddingReadiness()
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    await emb.rewarm_until_ready(
        model, readiness, expected_dim=1024, on_disable=lambda _r: None, sleep=fake_sleep, max_backoff_s=60.0
    )
    assert max(slept) == 60.0
    assert readiness.ready


@pytest.mark.asyncio
async def test_a_dimension_mismatch_found_after_warm_up_disables() -> None:
    model = _FlakyModel(1, dim=384)
    readiness = emb.EmbeddingReadiness()
    disabled: list[str] = []

    async def fake_sleep(_delay: float) -> None:
        return None

    await emb.rewarm_until_ready(
        model, readiness, expected_dim=1024, on_disable=disabled.append, sleep=fake_sleep
    )
    assert readiness.state == emb.EMBEDDING_DISABLED
    assert disabled and "384" in disabled[0]


def test_an_unknown_dimension_is_not_a_mismatch() -> None:
    assert emb.embedding_dimension_mismatch(_FlakyModel(0, dim=None), 1024) is None
    assert emb.embedding_dimension_mismatch(_FlakyModel(0, dim=1024), 1024) is None
    assert emb.embedding_dimension_mismatch(_FlakyModel(0, dim=384), 1024) is not None


class TestReadyEndpoint:
    """/ready must stop saying "ready" while the embedder cannot embed."""

    @staticmethod
    def _state(monkeypatch: pytest.MonkeyPatch, readiness: emb.EmbeddingReadiness | None):
        from app import main

        class _Conn:
            async def fetchval(self, _q: str) -> int:
                return 1

        class _Acquire:
            async def __aenter__(self) -> _Conn:
                return _Conn()

            async def __aexit__(self, *_e: object) -> None:
                return None

        class _Pool:
            def acquire(self) -> _Acquire:
                return _Acquire()

        class _Qdrant:
            async def get_collections(self) -> list:
                return []

        class _Redis:
            async def ping(self) -> bool:
                return True

        monkeypatch.setattr(main.app.state, "pg_pool", _Pool(), raising=False)
        monkeypatch.setattr(main.app.state, "qdrant_client", _Qdrant(), raising=False)
        monkeypatch.setattr(main.app.state, "redis_client", _Redis(), raising=False)
        monkeypatch.setattr(main.app.state, "embedding_readiness", readiness, raising=False)
        return main

    def test_warm_embedder_is_ready(self, monkeypatch) -> None:
        main = self._state(monkeypatch, emb.EmbeddingReadiness(state=emb.EMBEDDING_OK))
        assert asyncio.run(main.ready()) == {"status": "ready"}

    @pytest.mark.parametrize("state", [emb.EMBEDDING_WARMING, emb.EMBEDDING_DISABLED])
    def test_a_cold_or_disabled_embedder_is_not_ready(self, monkeypatch, state) -> None:
        from fastapi import HTTPException

        main = self._state(monkeypatch, emb.EmbeddingReadiness(state=state, detail="RuntimeError"))
        with pytest.raises(HTTPException) as info:
            asyncio.run(main.ready())
        assert info.value.status_code == 503
        assert info.value.detail["checks"]["embedding"].startswith(state)
