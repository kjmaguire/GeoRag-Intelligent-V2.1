"""ADR-0025: dense embedding on Cohere's own API (Embed 5), ``EMBEDDING_BACKEND=cohere``.

``embedding._CohereEmbedding`` is a sibling of ``_BedrockEmbedding`` and its wire shape is
**UNVERIFIED** -- every field is ``Status.ASSUMED`` in ``cohere_wire.EMBED``.
These tests prove what can be proved without a key: that the adapter builds the
request the contract declares, honours ``Retry-After``, refuses a reply of the
wrong width, and -- the one the ADR singles out -- that the QUERY path really
sends ``input_type: "search_query"`` through the real ``tools.py`` call site.

That last point is why this file drives ``search_documents`` and not only the
class. ``tools.py`` checks ``hasattr(model, "embed_query")``; a missing or
renamed method makes every question fall back to ``.encode()`` and embed as
``search_document``, with no error anywhere. A test that calls ``embed_query``
directly passes in exactly that failure.

No network: every request goes through ``httpx.MockTransport``.

The classes are always reached as ``embedding.<name>``, never imported by name:
``test_backend_selection`` reloads the module to read its import-time default,
and a name bound before that reload is a different class from the one
``get_embedding_model`` builds afterwards.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.config import settings
from app.services import cohere_wire, embedding

KEY = "test-only-not-a-real-cohere-key"
DIM = 1024


@pytest.fixture(autouse=True)
def _cohere_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key for the adapter, no real sleeping, no leftover Foundry env."""
    monkeypatch.setattr(settings, "COHERE_API_KEY", KEY)
    monkeypatch.setattr(settings, "COHERE_BASE_URL", "https://api.cohere.com")
    monkeypatch.setattr(embedding, "_sleep", lambda _s: None)
    monkeypatch.setattr(embedding._CohereEmbedding, "_IMAGE_WIRE_SHAPE", None)
    for name in (
        "AZURE_FOUNDRY_ENDPOINT",
        "AZURE_FOUNDRY_API_KEY",
        "AZURE_FOUNDRY_DEPLOYMENT",
        "AZURE_FOUNDRY_EMBED_DEPLOYMENT",
        "AZURE_FOUNDRY_RERANK_DEPLOYMENT",
        "AZURE_FOUNDRY_PARSE_DEPLOYMENT",
    ):
        monkeypatch.delenv(name, raising=False)


@dataclass
class _Server:
    """A recording fake of ``POST /v2/embed``."""

    requests: list[httpx.Request]
    bodies: list[dict[str, Any]]
    script: list[httpx.Response]  # consumed first-to-last; empty -> healthy reply
    dimension: int = DIM

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append(request)
        self.bodies.append(body)
        if self.script:
            return self.script.pop(0)
        n = len(body.get("texts") or []) or 1
        # The first coordinate of row i is the text's own number when the text
        # is "t<number>", so a test can see that order survived chunking.
        rows = []
        for text in (body.get("texts") or ["x"]):
            head = float(text[1:]) if text[:1] == "t" and text[1:].isdigit() else 0.0
            rows.append([head] + [0.0] * (self.dimension - 1))
        assert len(rows) == n
        return httpx.Response(200, json={"id": "fake", "embeddings": {"float": rows}})


def _model(
    server: _Server | None = None,
    *,
    query_model: str | None = None,
    dimension: int = DIM,
    timeout_s: float = 30.0,
) -> tuple[embedding._CohereEmbedding, _Server]:
    server = server or _Server([], [], [])
    client = httpx.Client(transport=httpx.MockTransport(server.handler))
    model = embedding._CohereEmbedding(
        "embed-v5.0-pro",
        query_model=query_model,
        dimension=dimension,
        timeout_s=timeout_s,
        client=client,
    )
    return model, server


# ---------------------------------------------------------------------------
# Request shape: document, query, image
# ---------------------------------------------------------------------------


class TestRequestShape:
    def test_a_document_body_is_exactly_the_declared_contract(self) -> None:
        model, server = _model()
        out = model.encode(["quartz vein", "chalcopyrite"], normalize_embeddings=True, batch_size=8)

        assert out.shape == (2, DIM)
        assert len(server.requests) == 1
        request = server.requests[0]
        assert str(request.url) == "https://api.cohere.com/v2/embed"
        assert request.headers["authorization"].lower() == f"bearer {KEY}".lower()
        assert server.bodies[0] == {
            "model": "embed-v5.0-pro",
            "texts": ["quartz vein", "chalcopyrite"],
            "input_type": "search_document",
            "embedding_types": ["float"],
            "output_dimension": DIM,
        }

    def test_the_body_matches_the_wire_contract_field_for_field(self) -> None:
        """cohere_wire.EMBED is a claim about this adapter; this holds the two
        together so neither can move alone."""
        model, server = _model()
        model.encode(["a"])
        model.embed_query("b")
        required = {
            f.path.split(".")[0].removesuffix("[]")
            for f in cohere_wire.EMBED.request
            if f.required
        }
        for body in server.bodies:
            assert set(body) == required, "the adapter sends a field EMBED does not declare, or omits one"

    def test_a_single_string_returns_a_vector_not_a_matrix(self) -> None:
        model, _ = _model()
        assert model.encode("one").shape == (DIM,)

    def test_a_query_body_uses_search_query_and_the_query_model(self) -> None:
        model, server = _model(query_model="embed-v5.0-fast")
        model.encode(["doc"])
        vector = model.embed_query("what grade?")

        assert vector.shape == (DIM,)
        doc_body, query_body = server.bodies
        assert doc_body["input_type"] == "search_document" and doc_body["model"] == "embed-v5.0-pro"
        assert query_body["input_type"] == "search_query"
        assert query_body["model"] == "embed-v5.0-fast"
        assert query_body["texts"] == ["what grade?"]

    def test_the_query_model_defaults_to_the_document_model(self) -> None:
        model, server = _model()
        model.embed_query("q")
        assert server.bodies[0]["model"] == "embed-v5.0-pro"
        assert model.model_name == model.query_model_name == "embed-v5.0-pro"

    def test_an_image_body_is_the_primary_shape_with_input_type_image(self) -> None:
        model, server = _model()
        vector = model.embed_image(b"\x89PNG fake", mime="image/png")

        assert vector.shape == (DIM,)
        assert len(server.bodies) == 1
        body = server.bodies[0]
        assert body["input_type"] == "image"
        assert body["model"] == "embed-v5.0-pro"
        assert body["embedding_types"] == ["float"] and body["output_dimension"] == DIM
        assert body["images"] == ["data:image/png;base64,iVBORyBmYWtl"]
        assert "texts" not in body, "text and image inputs cannot share a request"

    def test_a_schema_rejection_falls_back_once_to_the_inputs_shape(self) -> None:
        refusal = httpx.Response(400, json={"message": "unknown field images"})
        model, server = _model(_Server([], [], [refusal]))
        model.embed_image(b"png")

        assert [("images" in b, "inputs" in b) for b in server.bodies] == [(True, False), (False, True)]
        part = server.bodies[1]["inputs"][0]["content"][0]
        assert part["type"] == "image_url" and part["image_url"]["url"].startswith("data:image/png;base64,")
        # The winner is remembered, so the next page leads with it.
        assert embedding._CohereEmbedding._IMAGE_WIRE_SHAPE == "inputs"
        model.embed_image(b"png2")
        assert "inputs" in server.bodies[2]
        assert len(server.bodies) == 3

    def test_422_is_a_schema_rejection_too(self) -> None:
        model, server = _model(_Server([], [], [httpx.Response(422, json={"message": "bad"})]))
        model.embed_image(b"png")
        assert len(server.bodies) == 2

    def test_an_auth_failure_is_not_reshaped(self) -> None:
        """A 401 means the request was understood; different JSON just burns a call."""
        model, server = _model(_Server([], [], [httpx.Response(401, json={"message": "invalid api token"})]))
        with pytest.raises(embedding.CohereEmbeddingHttpError) as caught:
            model.embed_image(b"png")
        assert caught.value.status_code == 401
        assert len(server.bodies) == 1

    def test_both_image_shapes_refused_is_a_loud_error(self) -> None:
        refusals = [httpx.Response(400, json={"message": "no"}), httpx.Response(400, json={"message": "no"})]
        model, _ = _model(_Server([], [], refusals))
        with pytest.raises(RuntimeError, match="both documented image wire shapes"):
            model.embed_image(b"png")

    def test_the_key_never_appears_in_an_error(self) -> None:
        model, _ = _model(_Server([], [], [httpx.Response(401, json={"message": "invalid api token"})]))
        with pytest.raises(embedding.CohereEmbeddingHttpError) as caught:
            model.encode(["x"])
        assert KEY not in str(caught.value)


# ---------------------------------------------------------------------------
# Chunking, dimension, and reply validation
# ---------------------------------------------------------------------------


class TestBatchingAndValidation:
    def test_texts_are_chunked_at_96_and_order_survives(self) -> None:
        model, server = _model()
        texts = [f"t{i}" for i in range(200)]
        out = model.encode(texts)

        assert [len(b["texts"]) for b in server.bodies] == [96, 96, 8]
        assert embedding.COHERE_EMBED_MAX_TEXTS_PER_CALL == 96
        assert out.shape == (200, DIM)
        assert out[:, 0].tolist() == [float(i) for i in range(200)]

    def test_exactly_96_is_one_request_and_97_is_two(self) -> None:
        model, server = _model()
        model.encode([f"t{i}" for i in range(96)])
        assert len(server.bodies) == 1
        model.encode([f"t{i}" for i in range(97)])
        assert [len(b["texts"]) for b in server.bodies[1:]] == [96, 1]

    def test_no_texts_is_no_request(self) -> None:
        model, server = _model()
        assert model.encode([]).shape == (0, DIM)
        assert server.requests == []

    def test_a_reply_of_the_wrong_width_is_refused(self) -> None:
        """A silently ignored output_dimension would otherwise write wrong-width
        vectors into a 1024-dim collection."""
        model, _ = _model(_Server([], [], [], dimension=1536))
        with pytest.raises(RuntimeError, match="COHERE_EMBED_DIMENSION=1024"):
            model.encode(["x"])

    def test_a_short_reply_is_refused_not_concatenated(self) -> None:
        short = httpx.Response(200, json={"embeddings": {"float": [[0.0] * DIM]}})
        model, _ = _model(_Server([], [], [short]))
        with pytest.raises(RuntimeError, match="shape"):
            model.encode(["a", "b"])

    def test_a_reply_without_embeddings_names_what_it_did_send(self) -> None:
        model, _ = _model(_Server([], [], [httpx.Response(200, json={"id": "x", "message": "hi"})]))
        with pytest.raises(RuntimeError, match=r"embeddings\.float"):
            model.encode(["a"])

    def test_the_dimension_follows_the_constructor(self) -> None:
        model, server = _model(_Server([], [], [], dimension=768), dimension=768)
        assert model.get_sentence_embedding_dimension() == 768
        model.encode(["a"])
        assert server.bodies[0]["output_dimension"] == 768

    def test_the_startup_check_helper_agrees_with_the_collection(self) -> None:
        model, _ = _model()
        assert embedding.embedding_dimension_mismatch(model, settings.EMBEDDING_DIMENSION) is None
        assert embedding.embedding_dimension_mismatch(model, 768) is not None


# ---------------------------------------------------------------------------
# Retry-After and transient failures
# ---------------------------------------------------------------------------


class TestRetries:
    def test_a_429_honours_retry_after_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        waits: list[float] = []
        monkeypatch.setattr(embedding, "_sleep", waits.append)
        throttled = httpx.Response(429, headers={"Retry-After": "7"}, json={"message": "rate limited"})
        model, server = _model(_Server([], [], [throttled]))

        out = model.encode(["t5"])

        assert out[0, 0] == 5.0
        assert len(server.bodies) == 2, "the same request is re-sent"
        assert server.bodies[0] == server.bodies[1]
        assert len(waits) == 1 and waits[0] >= 7.0, "waited less than the host asked for"

    def test_without_retry_after_the_backoff_ladder_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        waits: list[float] = []
        monkeypatch.setattr(embedding, "_sleep", waits.append)
        script = [httpx.Response(503, json={}), httpx.Response(503, json={})]
        model, server = _model(_Server([], [], script))
        model.encode(["x"])
        assert len(server.bodies) == 3
        assert 2.0 <= waits[0] < 4.0 and 4.0 <= waits[1] < 8.0

    def test_ingest_gives_up_after_its_attempts_and_raises_the_status(self) -> None:
        script = [httpx.Response(429, json={}) for _ in range(10)]
        model, server = _model(_Server([], [], script))
        with pytest.raises(embedding.CohereEmbeddingHttpError) as caught:
            model.encode(["x"])
        assert caught.value.status_code == 429
        assert len(server.bodies) == embedding._COHERE_INGEST_MAX_ATTEMPTS

    def test_a_non_retryable_status_is_not_retried(self) -> None:
        model, server = _model(_Server([], [], [httpx.Response(404, json={"message": "model not found"})]))
        with pytest.raises(embedding.CohereEmbeddingHttpError) as caught:
            model.encode(["x"])
        assert caught.value.status_code == 404
        assert len(server.bodies) == 1

    def test_a_transport_fault_is_retried(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def flaky(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ConnectError("boom", request=request)
            return httpx.Response(200, json={"embeddings": {"float": [[0.0] * DIM]}})

        model = embedding._CohereEmbedding("embed-v5.0-pro", client=httpx.Client(transport=httpx.MockTransport(flaky)))
        assert model.encode(["x"]).shape == (1, DIM)
        assert calls["n"] == 2

    def test_the_query_path_does_not_wait_past_its_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A question is inside the chat latency budget. A Retry-After of 15 s
        cannot fit a 10 s total, so the 429 surfaces instead of stalling."""
        waits: list[float] = []
        monkeypatch.setattr(embedding, "_sleep", waits.append)
        script = [httpx.Response(429, headers={"Retry-After": "15"}, json={})]
        model, server = _model(_Server([], [], script), timeout_s=10.0)
        with pytest.raises(embedding.CohereEmbeddingHttpError) as caught:
            model.embed_query("q")
        assert caught.value.status_code == 429
        assert waits == [] and len(server.bodies) == 1

    def test_the_query_path_retries_a_short_429(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(embedding, "_sleep", lambda _s: None)
        script = [httpx.Response(429, headers={"Retry-After": "1"}, json={})]
        model, server = _model(_Server([], [], script), timeout_s=30.0)
        assert model.embed_query("q").shape == (DIM,)
        assert len(server.bodies) == 2
        assert all(b["input_type"] == "search_query" for b in server.bodies)


# ---------------------------------------------------------------------------
# The real tools.py call site
# ---------------------------------------------------------------------------


class _Ctx:
    def __init__(self, deps: Any) -> None:
        self.deps = deps


@pytest.mark.asyncio
async def test_search_documents_embeds_the_question_as_search_query() -> None:
    """THE test the ADR demands. Not ``embed_query`` called by hand: the real
    ``search_documents`` -> ``_encode_query`` -> ``hasattr(_model, "embed_query")``
    path, over the real adapter, with the HTTP hop recorded."""
    from app.agent.deps import AgentDeps
    from app.agent.tools import DocumentSearchResult, search_documents

    model, server = _model()
    deps = AgentDeps(
        pg_pool=None,  # type: ignore[arg-type]
        qdrant_client=MagicMock(),  # type: ignore[arg-type]
        neo4j_driver=None,  # type: ignore[arg-type]
        project_id="00000000-0000-0000-0000-0000000000aa",
        embedding_model=model,
        reranker=None,
        workspace_id="a0000000-0000-0000-0000-000000000001",
    )

    with patch("app.agent.tools.settings") as mock_settings, \
         patch("app.services.sparse_encoder.encode_sparse", return_value={1: 0.5}), \
         patch("app.services.qdrant_service.hybrid_query", new=AsyncMock(return_value=[])) as hybrid:
        mock_settings.TIMEOUT_QDRANT_S = 5.0
        mock_settings.TIMEOUT_RERANKER_S = 8.0
        mock_settings.RETRIEVAL_TOP_N = 20
        mock_settings.RETRIEVAL_QUALITY_THRESHOLD = 0.3
        mock_settings.RERANKER_TOP_K = 5
        result = await search_documents(
            _Ctx(deps),  # type: ignore[arg-type]
            query_text="What is the indicated copper resource?",
            project_id="proj-test-uuid",
        )

    assert isinstance(result, DocumentSearchResult)
    assert result.retrieval_failure is None, result.data_source
    hybrid.assert_awaited_once()
    assert len(server.bodies) == 1, "exactly one embed call for one question"
    assert server.bodies[0]["input_type"] == "search_query", (
        "the question was embedded as a document: tools.py fell back to .encode()"
    )
    assert "indicated copper resource" in server.bodies[0]["texts"][0]
    assert len(hybrid.await_args.kwargs["query_dense"]) == DIM


def test_both_adapters_keep_the_duck_typed_surface_tools_py_relies_on() -> None:
    """The names are a contract enforced only by duck typing (ADR-0025)."""
    for cls in (embedding._CohereEmbedding, embedding._BedrockEmbedding):
        for name in ("encode", "embed_query", "embed_image", "get_sentence_embedding_dimension"):
            assert callable(getattr(cls, name, None)), f"{cls.__name__}.{name} is missing"
        assert isinstance(cls.model_name, property), f"{cls.__name__}.model_name"


# ---------------------------------------------------------------------------
# Selection, default and credentials
# ---------------------------------------------------------------------------


class TestSelection:
    def test_the_code_default_is_cohere(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib

        monkeypatch.delenv("EMBEDDING_BACKEND", raising=False)
        try:
            reloaded = importlib.reload(embedding)
            assert reloaded.EMBEDDING_BACKEND == "cohere"
            assert reloaded.COHERE_EMBED_MODEL == "embed-v5.0-pro"
            assert reloaded.COHERE_EMBED_QUERY_MODEL == "embed-v5.0-pro"
            assert reloaded.COHERE_EMBED_DIMENSION == 1024
            assert reloaded.COHERE_EMBED_TIMEOUT_S == 30.0
        finally:
            monkeypatch.undo()
            importlib.reload(embedding)

    def test_the_query_model_can_diverge_by_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import importlib

        monkeypatch.setenv("COHERE_EMBED_QUERY_MODEL", "embed-v5.0-fast")
        try:
            reloaded = importlib.reload(embedding)
            assert reloaded.COHERE_EMBED_MODEL == "embed-v5.0-pro"
            assert reloaded.COHERE_EMBED_QUERY_MODEL == "embed-v5.0-fast"
        finally:
            monkeypatch.undo()
            importlib.reload(embedding)

    def test_get_embedding_model_builds_the_cohere_adapter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(embedding, "EMBEDDING_BACKEND", "cohere")
        monkeypatch.setattr(embedding, "COHERE_EMBED_QUERY_MODEL", "embed-v5.0-fast")
        model = embedding.get_embedding_model("Qwen/Qwen3-Embedding-0.6B")
        assert isinstance(model, embedding._CohereEmbedding)
        assert model.model_name == "embed-v5.0-pro"
        assert model.query_model_name == "embed-v5.0-fast"
        assert model.get_sentence_embedding_dimension() == embedding.COHERE_EMBED_DIMENSION

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_an_empty_key_fails_loudly_on_the_query_path(self, monkeypatch: pytest.MonkeyPatch, blank: str) -> None:
        monkeypatch.setattr(embedding, "EMBEDDING_BACKEND", "cohere")
        monkeypatch.setattr(settings, "COHERE_API_KEY", blank)
        with pytest.raises(RuntimeError, match="COHERE_API_KEY"):
            embedding.get_embedding_model("Qwen/Qwen3-Embedding-0.6B")

    def test_an_empty_key_fails_loudly_on_the_ingest_path_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.services.ingest.passage_embedder import load_embedding_model

        monkeypatch.setattr(embedding, "EMBEDDING_BACKEND", "cohere")
        monkeypatch.setattr(settings, "COHERE_API_KEY", "")
        with pytest.raises(RuntimeError, match="COHERE_API_KEY"):
            load_embedding_model()

    def test_a_blank_key_also_fails_at_call_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Belt and braces: an adapter built before the key was blanked."""
        model, server = _model()
        monkeypatch.setattr(settings, "COHERE_API_KEY", "")
        with pytest.raises(RuntimeError, match="COHERE_API_KEY"):
            model.encode(["x"])
        assert server.requests == []

    def test_ingest_and_query_build_the_same_adapter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ADR-0025 gotcha 1: they must agree on model, dimension and host."""
        from app.services.ingest.passage_embedder import load_embedding_model

        monkeypatch.setattr(embedding, "EMBEDDING_BACKEND", "cohere")
        query = embedding.get_embedding_model("Qwen/Qwen3-Embedding-0.6B")
        ingest = load_embedding_model()
        assert type(query) is type(ingest) is embedding._CohereEmbedding
        assert (query.model_name, query.query_model_name, query.get_sentence_embedding_dimension()) == (
            ingest.model_name, ingest.query_model_name, ingest.get_sentence_embedding_dimension(),
        )

    def test_bedrock_stays_selectable_as_the_rollback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(embedding, "EMBEDDING_BACKEND", "bedrock")
        monkeypatch.setattr(embedding, "BEDROCK_EMBED_MODEL_ID", "cohere.embed-v4:0")
        model = embedding.get_embedding_model("Qwen/Qwen3-Embedding-0.6B")
        assert isinstance(model, embedding._BedrockEmbedding)
        assert model.model_name == "cohere.embed-v4:0"

    def test_the_ingest_loader_still_reaches_bedrock_when_asked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.services.ingest.passage_embedder import load_embedding_model

        monkeypatch.setattr(embedding, "EMBEDDING_BACKEND", "bedrock")
        monkeypatch.setattr(embedding, "BEDROCK_EMBED_MODEL_ID", "cohere.embed-v4:0")
        assert isinstance(load_embedding_model(), embedding._BedrockEmbedding)


# ---------------------------------------------------------------------------
# What the ingest path does with a rejection
# ---------------------------------------------------------------------------


def test_a_cohere_400_is_a_request_rejection_so_a_poison_text_is_isolated() -> None:
    """VEN-14 reads ``exc.response``, which this adapter's own exception does not
    carry. Without the explicit case one over-long text would fail its whole
    batch on every sweep."""
    from app.services.ingest.passage_embedder import _is_request_rejection, encode_isolating_rejections

    assert _is_request_rejection(embedding.CohereEmbeddingHttpError(400, "text too long"))
    assert _is_request_rejection(embedding.CohereEmbeddingHttpError(422, "bad"))
    assert not _is_request_rejection(embedding.CohereEmbeddingHttpError(429, "slow"))
    assert not _is_request_rejection(embedding.CohereEmbeddingHttpError(401, "key"))

    def encode(texts: list[str]) -> list[list[float]]:
        if "poison" in texts:
            raise embedding.CohereEmbeddingHttpError(400, "text too long")
        return [[1.0] for _ in texts]

    assert encode_isolating_rejections(encode, ["a", "poison", "b"]) == [[1.0], None, [1.0]]


# ---------------------------------------------------------------------------
# answer_runs.embedding_model (the query-side record)
# ---------------------------------------------------------------------------


class TestAnswerRunEmbeddingModel:
    def _state(self, tool_results: list[tuple[str, Any]], model: Any) -> Any:
        return MagicMock(tool_results=tool_results, deps=MagicMock(embedding_model=model))

    def _result(self, *, failure: str | None = None) -> Any:
        from app.agent.tools import DocumentSearchResult

        return DocumentSearchResult(chunks=[], count=0, data_source="Qdrant", retrieval_failure=failure)

    def test_the_query_model_is_recorded(self) -> None:
        from app.agent.agentic_retrieval.nodes import _embedding_model_for_run

        model, _ = _model(query_model="embed-v5.0-fast")
        state = self._state([("search_documents", self._result())], model)
        assert _embedding_model_for_run(state) == "embed-v5.0-fast"

    def test_bedrock_records_its_model_id(self) -> None:
        from app.agent.agentic_retrieval.nodes import _embedding_model_for_run

        state = self._state(
            [("search_documents", self._result())], embedding._BedrockEmbedding("cohere.embed-v4:0")
        )
        assert _embedding_model_for_run(state) == "cohere.embed-v4:0"

    def test_the_local_model_records_the_configured_name(self) -> None:
        from app.agent.agentic_retrieval.nodes import _embedding_model_for_run

        class _Local:  # no model_name, like SentenceTransformer and the sidecar proxy
            pass

        state = self._state([("search_documents", self._result())], _Local())
        assert _embedding_model_for_run(state) == settings.EMBEDDING_MODEL_NAME

    def test_a_run_with_no_document_search_records_null(self) -> None:
        from app.agent.agentic_retrieval.nodes import _embedding_model_for_run

        model, _ = _model()
        assert _embedding_model_for_run(self._state([], model)) is None
        assert _embedding_model_for_run(self._state([("query_spatial_collars", object())], model)) is None

    def test_a_search_that_never_embedded_records_null(self) -> None:
        from app.agent.agentic_retrieval.nodes import _embedding_model_for_run

        model, _ = _model()
        for failure in ("model_not_loaded", "timeout", "error"):
            state = self._state([("search_documents", self._result(failure=failure))], model)
            assert _embedding_model_for_run(state) is None, failure

    def test_the_value_fits_the_column(self) -> None:
        from app.agent.agentic_retrieval.nodes import _embedding_model_for_run

        long_name = "arn:aws:bedrock:us-east-1:123456789012:endpoint/" + "x" * 300
        state = self._state(
            [("search_documents", self._result())], embedding._BedrockEmbedding(long_name)
        )
        assert len(_embedding_model_for_run(state) or "") <= 128

    def test_the_insert_writes_it_as_the_last_positional_argument(self) -> None:
        from app.agent.agentic_retrieval.nodes import _ANSWER_RUN_INSERT_SQL

        assert "embedding_model," in _ANSWER_RUN_INSERT_SQL
        assert "$20" in _ANSWER_RUN_INSERT_SQL and "$21" not in _ANSWER_RUN_INSERT_SQL
