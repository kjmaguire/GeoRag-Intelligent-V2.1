"""Embedding model access — local SentenceTransformer or shared sidecar proxy.

By default each uvicorn worker loads its OWN SentenceTransformer
(``settings.EMBEDDING_MODEL_NAME``) on CPU. For Qwen3-Embedding-0.6B that is
~2.4 GiB of host RAM *per worker* — measured 2026-06-24 as the dominant term in
the FastAPI container footprint (PSS ≈ private ≈ 3.8 GiB/worker, i.e. no
cross-worker page sharing, so the model is genuinely duplicated N times).

When ``EMBEDDING_SERVICE_URL`` is set, :func:`get_embedding_model` instead
returns a thin synchronous HTTP proxy (:class:`_RemoteEmbedding`) to the single
shared copy hosted by ``app.embedding_service``, so all workers share one model
over a localhost hop. Same pattern as the reranker sidecar
(``app.services.reranker._RemoteReranker``). The proxy only needs to mimic the
*subset* of the SentenceTransformer API used in-process on the query path:
``.encode(str|list, normalize_embeddings=...)`` and
``.get_sentence_embedding_dimension()``.

Only the FastAPI query path (``main.py`` → ``app.state.embedding_model``) routes
through here. The Hatchet ingest embedder (``passage_embedder``) and the eval
harness load their own local models and are intentionally unaffected.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Set on the FastAPI workers (NOT on the sidecar itself — the sidecar is the
# model host). When empty, get_embedding_model() loads a local model as before.
EMBEDDING_SERVICE_URL = (os.environ.get("EMBEDDING_SERVICE_URL") or "").strip()
EMBEDDING_MODEL_REVISION = (
    os.environ.get("EMBEDDING_MODEL_REVISION")
    or "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
).strip()

# Query-path encodes are single short strings; 30s is generous headroom for a
# cold sidecar still warming the model. Callers already run encode() in a
# thread-pool executor, so this blocking call never touches the event loop.
_HTTP_TIMEOUT_S = float(os.environ.get("EMBEDDING_SERVICE_TIMEOUT_S", "30") or "30")

# ---------------------------------------------------------------------------
# Amazon Bedrock (Cohere Embed v4) backend — EMBEDDING_BACKEND=bedrock
# ---------------------------------------------------------------------------
# Takes precedence over EMBEDDING_SERVICE_URL below. No local model, no
# sidecar, at all. Reaches Bedrock with the ECS task role's SigV4 credentials
# (app.services._bedrock) rather than an endpoint plus API key.
#
# Default was "foundry" from 2026-09-06 until the AWS move on 2026-09-08; it
# is "bedrock" now for the same reason it was "foundry" then. Production has
# no GPU host, so an UNSET variable used to select a model host that does not
# exist there and the query path silently ran with no embedding model
# (ADR-0021 gotcha 1). Unset means Bedrock in code and in docker-compose.yml
# alike; .env.example sets EMBEDDING_BACKEND=local explicitly to use the
# self-hosted sidecar.
EMBEDDING_BACKEND = (os.environ.get("EMBEDDING_BACKEND") or "bedrock").strip().lower()
# Bedrock model id for Cohere Embed v4, or the ARN of a Bedrock Marketplace
# endpoint serving it. [UNVERIFIED] that this exact id is offered in the
# target region — ADR-0022 step 0.
BEDROCK_EMBED_MODEL_ID = (
    os.environ.get("BEDROCK_EMBED_MODEL_ID") or "cohere.embed-v4:0"
).strip()
# Cohere Embed v4 supports Matryoshka-truncated output at 256/512/1024/1536
# dims. Request 1024 to match the existing georag_chunks collection schema
# exactly — no Qdrant migration needed. MUST match settings.EMBEDDING_DIMENSION.
BEDROCK_EMBED_DIMENSION = int(os.environ.get("BEDROCK_EMBED_DIMENSION", "1024"))
BEDROCK_EMBED_TIMEOUT_S = float(os.environ.get("BEDROCK_EMBED_TIMEOUT_S", "30"))

#: Cohere Embed v4 accepts at most 96 texts per request. [ASSUMED] -- the
#: vendor documentation figure; the Bedrock probe has only ever sent one text
#: (bedrock_wire EMBED_TEXT ``body.texts[]``). ``_post`` chunks to this so no
#: caller can exceed it: reembed_qdrant.py pages 100 points at a time, and a
#: single 100-text call would be rejected on every page (VEN-3, 2026-09-29).
EMBED_V4_MAX_TEXTS_PER_CALL = 96

#: Query-path read timeout per attempt. One short query string, so this is
#: the cold-connection ceiling rather than a batch budget. The total query
#: budget is BEDROCK_EMBED_TIMEOUT_S (30 s, validated in config.py to sit
#: under TIMEOUT_QDRANT_S); retry_profile_within_budget turns the pair into
#: 2 attempts x 10 s rather than the ingest profile's 4 x 30 s (VEN-6).
_QUERY_READ_TIMEOUT_S = 10.0
_QUERY_MAX_ATTEMPTS = 3


class _BedrockEmbedding:
    """Cohere Embed v4 on Amazon Bedrock, behind the SentenceTransformer surface.

    Mirrors ``SentenceTransformer.encode(str|list, normalize_embeddings=...)
    -> np.ndarray`` and ``.get_sentence_embedding_dimension()`` — same
    contract as ``_RemoteEmbedding`` below, so it's a drop-in wherever the
    query-path or ingestion code holds an embedding-model reference.

    Wire shape (ADR-0022)::

        bedrock-runtime.invoke_model(
            modelId=<BEDROCK_EMBED_MODEL_ID>,
            body={"texts": [str, ...],
                  "input_type": "search_document"|"search_query",
                  "embedding_types": ["float"], "output_dimension": 1024})
        -> body {"embeddings": {"float": [[...], ...]}}

    The request body is **Cohere's own v2 schema**, unchanged from the
    Foundry path this replaced — Bedrock's InvokeModel passes the provider
    body through and takes the model out of it into ``modelId``. That is why
    this adapter is a transport swap and not a rewrite.

    **[UNVERIFIED]** — the Foundry contract was confirmed empirically against
    a live deployment on 2026-07-30. This one has not been; run
    ``ops/validation/bedrock_probe.py`` and commit its report before trusting
    it in production (ADR-0022 "Verification").

    Cohere recommends asymmetric embedding: ``input_type="search_document"``
    for indexed corpus chunks, ``"search_query"`` for retrieval-time
    queries — a real quality lever the plain SentenceTransformer interface
    doesn't have a slot for. ``encode()`` defaults to "search_document"
    (correct for every ingestion call site, which never overrides it);
    query-time callers should use :meth:`embed_query` instead, which sets
    "search_query". Call sites that don't know about this distinction (or
    run against a different backend without it) safely fall back to
    ``encode()`` via a ``hasattr(model, "embed_query")`` check.
    """

    def __init__(
        self,
        model_id: str = BEDROCK_EMBED_MODEL_ID,
        *,
        dimension: int = BEDROCK_EMBED_DIMENSION,
        timeout_s: float = BEDROCK_EMBED_TIMEOUT_S,
    ) -> None:
        self._model_id = model_id
        self._dimension = dimension
        self._timeout_s = timeout_s

    def _client(self, *, query_path: bool = False):
        from app.services._bedrock import (  # noqa: PLC0415
            get_client,
            retry_profile_within_budget,
        )

        if query_path:
            # The query path runs under TIMEOUT_QDRANT_S, so it gets a
            # budgeted client like the reranker's: attempts x read timeout +
            # botocore backoff fit inside BEDROCK_EMBED_TIMEOUT_S. On the
            # ingest profile a stalled call could run 4 x 30 s, outliving the
            # branch deadline and holding a pool thread and quota (VEN-6).
            attempts, read_timeout_s = retry_profile_within_budget(
                self._timeout_s,
                read_timeout_s=min(_QUERY_READ_TIMEOUT_S, self._timeout_s),
                ceiling=_QUERY_MAX_ATTEMPTS,
            )
            return get_client("bedrock-runtime", max_attempts=attempts, read_timeout_s=read_timeout_s)
        # Ingestion has no wall clock, so this client keeps the full adaptive
        # retry ceiling.
        return get_client("bedrock-runtime", read_timeout_s=self._timeout_s)

    def _invoke(self, body: dict[str, Any], *, query_path: bool = False) -> dict[str, Any]:
        import json  # noqa: PLC0415

        resp = self._client(query_path=query_path).invoke_model(
            modelId=self._model_id,
            body=json.dumps(body),
            accept="application/json",
            contentType="application/json",
        )
        return json.loads(resp["body"].read())

    def _post(self, texts: list[str], input_type: str, *, query_path: bool = False) -> np.ndarray:
        """Embed ``texts``, in requests of at most EMBED_V4_MAX_TEXTS_PER_CALL.

        Chunking here rather than at each caller is what makes every caller
        safe: ``encode()`` absorbs ``batch_size`` (it is a SentenceTransformer
        keyword this backend has no use for), so a caller's batch size was
        never a bound on the request.
        """
        if not texts:
            return np.zeros((0, self._dimension), dtype=np.float32)
        parts: list[np.ndarray] = []
        for start in range(0, len(texts), EMBED_V4_MAX_TEXTS_PER_CALL):
            chunk = texts[start : start + EMBED_V4_MAX_TEXTS_PER_CALL]
            body = {
                "texts": chunk,
                "input_type": input_type,
                "embedding_types": ["float"],
                "output_dimension": self._dimension,
            }
            # The keyword only when it changes something, so the ingest call
            # keeps the one-argument `_invoke(body)` shape tests fake.
            payload = self._invoke(body, query_path=True) if query_path else self._invoke(body)
            vectors = np.asarray(payload["embeddings"]["float"], dtype=np.float32)
            if vectors.shape[0] != len(chunk):
                # Concatenating a short answer would silently attach every
                # later vector to the wrong text.
                raise RuntimeError(
                    f"Cohere Embed v4 returned {vectors.shape[0]} vectors for {len(chunk)} texts "
                    f"(model {self._model_id!r})"
                )
            parts.append(vectors)
        return parts[0] if len(parts) == 1 else np.concatenate(parts, axis=0)

    def encode(
        self,
        sentences: str | list[str],
        normalize_embeddings: bool = False,  # noqa: ARG002 — Cohere vectors are pre-normalized
        input_type: str = "search_document",
        **_kwargs: Any,  # absorbs show_progress_bar, batch_size, prompt_name, etc.
    ) -> np.ndarray:
        single = isinstance(sentences, str)
        texts = [sentences] if single else list(sentences)
        arr = self._post(texts, input_type)
        return arr[0] if single else arr

    def embed_query(self, text: str) -> np.ndarray:
        """Query-time embedding using Cohere's recommended input_type="search_query".

        On the budgeted query-path client (VEN-6), not the ingest one.
        """
        return self._post([text], "search_query", query_path=True)[0]

    # -- multimodal (page-image) embedding -------------------------------
    #
    # Embed v4 is multimodal and places image vectors in the SAME output
    # space as text vectors, so an image point lands in the existing
    # `georag_chunks` dense slot ('' / 1024-dim / Cosine) and a plain text
    # query matches it with no retrieval changes at all. That shared space
    # is the entire reason this feature is cheap to add — do not "fix" it
    # by giving images their own collection.
    #
    # Two hard constraints from the model card, unchanged by the host:
    #   1. Images cap at 2M pixels. Callers MUST downscale first — see
    #      app.services.ingest.page_image.render_page_png, which is the
    #      only supported producer. A 250-DPI letter page is ~5.8M px and
    #      is rejected outright.
    #   2. Text and image inputs CANNOT be combined in one call
    #      ("cannot have both text and image inputs"). So this is a
    #      separate request from _post(), never a merged one.
    #
    # WIRE SHAPE: Cohere shipped two accepted shapes for v4 — the older
    # `images: [data-uri]` and the newer interleaved `inputs: [...]` — and
    # which one a given host accepts is not documented. That ambiguity
    # predates the AWS move and survives it, so the same strategy carries
    # over: try the primary, fall back ONCE on a schema rejection, and log
    # whichever won so the first real run tells us definitively. Collapse
    # this to the winner (and delete the fallback) once observed in
    # production logs.
    _IMAGE_WIRE_SHAPE: str | None = None  # None = undetermined; set on first success

    def embed_image(self, png_bytes: bytes, *, mime: str = "image/png") -> np.ndarray:
        """Embed ONE page image, returning a 1024-dim vector.

        Deliberately single-image: Embed v4 accepts a batch, but a page
        render is ~1-3 MB and batching them multiplies an already-large
        request body by the batch size for no latency win worth the
        memory. The 2026-08-07 SPLADE batching regression (OOM-killed the
        worker, exit 137) is the cautionary precedent.
        """
        import base64  # noqa: PLC0415

        data_uri = f"data:{mime};base64,{base64.b64encode(png_bytes).decode('ascii')}"

        def _body_images() -> dict[str, Any]:
            return {
                "images": [data_uri],
                "input_type": "image",
                "embedding_types": ["float"],
                "output_dimension": self._dimension,
            }

        def _body_inputs() -> dict[str, Any]:
            return {
                "inputs": [{"content": [{"type": "image_url", "image_url": {"url": data_uri}}]}],
                "input_type": "image",
                "embedding_types": ["float"],
                "output_dimension": self._dimension,
            }

        shapes: list[tuple[str, Any]] = (
            [("images", _body_images), ("inputs", _body_inputs)]
            if _BedrockEmbedding._IMAGE_WIRE_SHAPE in (None, "images")
            else [("inputs", _body_inputs), ("images", _body_images)]
        )

        last_exc: Exception | None = None
        for name, build_body in shapes:
            try:
                payload = self._invoke(build_body())
            except Exception as exc:  # noqa: BLE001 — narrowed immediately below
                # Only a schema rejection is worth re-shaping for. Anything
                # else (auth, throttling, 5xx) means the request was
                # understood and retrying with different JSON just burns
                # another call. botocore raises ValidationException for a
                # body the model rejects; every other ClientError code, and
                # anything that is not a ClientError at all, propagates.
                code = getattr(exc, "response", {}).get("Error", {}).get("Code")
                if code in ("ValidationException", "ModelErrorException"):
                    last_exc = exc
                    logger.debug(
                        "bedrock image embed: wire shape %r rejected (%s) — trying alternate",
                        name, code,
                    )
                    continue
                raise

            if name != _BedrockEmbedding._IMAGE_WIRE_SHAPE:
                _BedrockEmbedding._IMAGE_WIRE_SHAPE = name
                logger.info("bedrock image embed: using wire shape %r", name)
            return np.asarray(payload["embeddings"]["float"], dtype=np.float32)[0]

        raise RuntimeError(
            "Cohere Embed v4 rejected both documented image wire shapes "
            f"(images[], inputs[]) on model {self._model_id!r}"
        ) from last_exc

    def get_sentence_embedding_dimension(self) -> int:
        return self._dimension


class _RemoteEmbedding:
    """HTTP proxy to the shared embedding sidecar.

    Mimics the SentenceTransformer surface the in-process query path relies on.
    ``encode`` returns a numpy array so existing ``.tolist()`` call sites are
    unchanged: a single ``str`` in → 1-D array (like SentenceTransformer); a
    list in → 2-D array.
    """

    def __init__(self, url: str, *, timeout_s: float = _HTTP_TIMEOUT_S, dim: int | None = None):
        self._url = url.rstrip("/")
        self._timeout_s = timeout_s
        self._dim = dim

    def encode(self, sentences: str | list[str], normalize_embeddings: bool = False, **_kwargs: Any) -> np.ndarray:
        import httpx  # noqa: PLC0415

        from app.sidecar_auth import SERVICE_KEY_HEADERS  # noqa: PLC0415

        single = isinstance(sentences, str)
        payload = {
            "sentences": [sentences] if single else list(sentences),
            "normalize": bool(normalize_embeddings),
        }
        resp = httpx.post(
            f"{self._url}/embed", json=payload, timeout=self._timeout_s,
            headers=SERVICE_KEY_HEADERS,
        )
        resp.raise_for_status()
        vectors = resp.json()["vectors"]
        arr = np.asarray(vectors, dtype=np.float32)
        # Cache the dimension off the real vectors — the startup dim-parity
        # guard in main.py runs right after the warm-up encode, so this keeps
        # the guard effective on the sidecar path even when /health is flaky.
        if self._dim is None and arr.size:
            self._dim = int(arr.shape[-1])
        return arr[0] if single else arr

    def get_sentence_embedding_dimension(self) -> int | None:
        if self._dim is None:
            import httpx  # noqa: PLC0415

            try:
                resp = httpx.get(f"{self._url}/health", timeout=self._timeout_s)
                resp.raise_for_status()
                self._dim = int(resp.json()["dimension"])
            except Exception:  # noqa: BLE001 — encode() also back-fills _dim
                logger.warning("remote embedding: could not fetch dimension from %s", self._url)
        return self._dim


# ---------------------------------------------------------------------------
# Warm-up and readiness (VEN-1, 2026-09-29)
# ---------------------------------------------------------------------------
# main.py used to wrap the warm-up encode in one try/except that set
# app.state.embedding_model = None on ANY failure, for the life of the
# process. On the Bedrock backend the model object is stateless, so one
# transient failure at boot -- throttling, a NAT route not up yet, a
# credential-provider hiccup, all plausible on the nightly cold start --
# disabled search on that task until someone restarted it, with /ready still
# green. Now only a CONFIRMED dimension mismatch disables the model; a failed
# warm-up keeps it, marks the embedder "warming", retries in the background
# with backoff, and /ready reports it.

#: Readiness states. Only "ok" is ready.
EMBEDDING_OK = "ok"
EMBEDDING_WARMING = "warming"
EMBEDDING_DISABLED = "disabled"


@dataclass
class EmbeddingReadiness:
    """What /ready reports about the query-path embedder."""

    state: str = EMBEDDING_WARMING
    detail: str | None = None
    failures: int = 0
    last_attempt_monotonic: float | None = None

    @property
    def ready(self) -> bool:
        return self.state == EMBEDDING_OK

    def describe(self) -> str:
        if self.state == EMBEDDING_OK:
            return "ok"
        suffix = f" after {self.failures} failed warm-up(s)" if self.failures else ""
        return f"{self.state}{suffix}: {self.detail or '-'}"


def embedding_dimension_mismatch(model: Any, expected_dim: int) -> str | None:
    """A message if ``model`` reports a dimension other than ``expected_dim``.

    None when they agree OR when the model cannot say (the sidecar proxy
    returns None while unreachable) -- an unknown dimension is not evidence of
    a mismatch, and disabling on it would recreate the VEN-1 failure.
    """
    loaded = model.get_sentence_embedding_dimension()
    if loaded is not None and int(loaded) != int(expected_dim):
        return f"model reports dim={loaded} but EMBEDDING_DIMENSION={expected_dim}"
    return None


def _describe_error(exc: BaseException) -> str:
    # The exception TYPE only: /ready is unauthenticated, and a botocore
    # message can carry the account id and role ARN (AccessDenied does).
    # The full message goes to the log, not the probe.
    response = getattr(exc, "response", None)
    error = response.get("Error") if isinstance(response, dict) else None
    code = error.get("Code") if isinstance(error, dict) else None
    return f"{type(exc).__name__}({code})" if isinstance(code, str) else type(exc).__name__


def warm_up_once(model: Any, readiness: EmbeddingReadiness) -> bool:
    """One synchronous warm-up encode. Updates ``readiness``; never raises."""
    readiness.last_attempt_monotonic = time.monotonic()
    try:
        model.encode("warm-up", normalize_embeddings=True)
    except Exception as exc:  # noqa: BLE001 -- recorded, retried, reported by /ready
        readiness.failures += 1
        readiness.state = EMBEDDING_WARMING
        readiness.detail = _describe_error(exc)
        logger.warning(
            "embedding warm-up failed (attempt %d): %s: %s -- keeping the model; "
            "queries still try it and a background re-warm is scheduled",
            readiness.failures,
            type(exc).__name__,
            exc,
        )
        return False
    readiness.state = EMBEDDING_OK
    readiness.detail = None
    return True


async def rewarm_until_ready(
    model: Any,
    readiness: EmbeddingReadiness,
    *,
    expected_dim: int,
    on_disable: Callable[[str], None],
    initial_backoff_s: float = 5.0,
    max_backoff_s: float = 300.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Retry the warm-up with exponential backoff until it succeeds.

    Runs the blocking encode in a worker thread. On success it re-checks the
    dimension (the sidecar proxy can only report it once reachable) and calls
    ``on_disable`` on a confirmed mismatch -- the one failure that SHOULD take
    the model out of service. Returns once ready or disabled; cancelled at
    shutdown.
    """
    delay = initial_backoff_s
    while True:
        await sleep(delay)
        ok = await asyncio.to_thread(warm_up_once, model, readiness)
        if ok:
            mismatch = embedding_dimension_mismatch(model, expected_dim)
            if mismatch is not None:
                readiness.state = EMBEDDING_DISABLED
                readiness.detail = f"dimension mismatch: {mismatch}"
                on_disable(readiness.detail)
            else:
                logger.info(
                    "embedding warm-up succeeded after %d failed attempt(s)",
                    readiness.failures,
                )
            return
        delay = min(delay * 2.0, max_backoff_s)


def get_embedding_model(
    model_name: str,
    revision: str = EMBEDDING_MODEL_REVISION,
) -> Any:
    """Return the embedding model for the FastAPI query path.

    Precedence: EMBEDDING_BACKEND=bedrock (Cohere Embed v4, no local model at
    all) > a shared-sidecar HTTP proxy when EMBEDDING_SERVICE_URL is set >
    locally-loaded SentenceTransformer on CPU (the prior default).
    """
    from app.services._bedrock import (  # noqa: PLC0415
        assert_no_retired_foundry_env,
        bedrock_region,
        reject_retired_backend,
    )

    # `foundry` used to be this setting's default and is still in every
    # pre-2026-09-08 deployment's environment. It fails here, loudly, rather
    # than falling through to the sidecar branch below — which on an AWS task
    # would resolve to no embedding model at all and a query path that
    # retrieves nothing while reporting success (ADR-0021 gotcha 1).
    reject_retired_backend(EMBEDDING_BACKEND, setting="EMBEDDING_BACKEND")

    if EMBEDDING_BACKEND == "bedrock":
        assert_no_retired_foundry_env(context="EMBEDDING_BACKEND=bedrock")
        if not BEDROCK_EMBED_MODEL_ID:
            raise RuntimeError(
                "EMBEDDING_BACKEND=bedrock but BEDROCK_EMBED_MODEL_ID is empty"
            )
        logger.info(
            "Embedding model via Amazon Bedrock: model=%s region=%s dim=%d",
            BEDROCK_EMBED_MODEL_ID, bedrock_region(), BEDROCK_EMBED_DIMENSION,
        )
        return _BedrockEmbedding(BEDROCK_EMBED_MODEL_ID)
    if EMBEDDING_SERVICE_URL:
        logger.info("Embedding model via shared sidecar: %s", EMBEDDING_SERVICE_URL)
        return _RemoteEmbedding(EMBEDDING_SERVICE_URL)

    from sentence_transformers import SentenceTransformer  # noqa: PLC0415

    return SentenceTransformer(
        model_name,
        revision=revision,
        trust_remote_code=False,
        device="cpu",
    )
