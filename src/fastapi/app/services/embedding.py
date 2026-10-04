"""Embedding model access — Cohere Embed 5 (default), Bedrock Embed v4, or local.

``EMBEDDING_BACKEND=cohere`` (the default since ADR-0025, 2026-10-04) calls
Cohere's own API (:class:`_CohereEmbedding`); ``bedrock`` is the rollback
(:class:`_BedrockEmbedding`); ``local`` is the sidecar / SentenceTransformer
path described below, selected explicitly by ``.env.example`` and by the
Helm chart.

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
import threading
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
# Backend selection — EMBEDDING_BACKEND=cohere | bedrock | local
# ---------------------------------------------------------------------------
# Both hosted values take precedence over EMBEDDING_SERVICE_URL below; neither
# loads a local model or needs a sidecar.
#
# Default was "foundry" from 2026-09-06 until the AWS move on 2026-09-08, then
# "bedrock" until ADR-0025 (2026-10-04) moved dense embedding to Cohere's own
# API for Embed 5, which Bedrock does not serve. It is "cohere" now for the
# same reason it was "foundry" and "bedrock" before: production has no GPU
# host, so an UNSET variable used to select a model host that does not exist
# there and the query path silently ran with no embedding model (ADR-0021
# gotcha 1). Unset means Cohere in code, in docker-compose.yml and in
# Terraform alike; .env.example sets EMBEDDING_BACKEND=local explicitly to use
# the self-hosted sidecar, and charts/georag/ does the same for on-prem.
#
# The query path (here) and the ingest path (passage_embedder.
# load_embedding_model) MUST agree on this value. A mismatch writes one vector
# space and queries another, and fails as poor retrieval, not as an error.
EMBEDDING_BACKEND = (os.environ.get("EMBEDDING_BACKEND") or "cohere").strip().lower()

# ---------------------------------------------------------------------------
# Cohere's own API (Embed 5) — EMBEDDING_BACKEND=cohere (ADR-0025)
# ---------------------------------------------------------------------------
# POST {COHERE_BASE_URL}/v2/embed with COHERE_API_KEY, the same host and key
# as chat (llm_cohere.py) and Parse (cohere_parse_client.py). Embed 5 is not
# on Bedrock; the SageMaker listing is a Marketplace endpoint that bills while
# idle, which is why this is the host (ADR-0025 options B and C).
#
# [UNVERIFIED] on every point below: model names, output dimensions, the
# image request shape and the per-request input limit come from Cohere's
# 2026-09-30 publications, not from a call made with our key. Every field is
# Status.ASSUMED in cohere_wire.EMBED; ops/validation/cohere_probe.py
# probe_embed() is how the first credentialed run turns that into a diff.
#
# Ingest (documents and page images) uses COHERE_EMBED_MODEL. Queries use
# COHERE_EMBED_QUERY_MODEL, which defaults to the SAME model: Cohere says
# embed-v5.0-pro and embed-v5.0-fast share an embedding space, but that is a
# vendor claim about the one property that fails silently when false, so
# moving queries to Fast waits for the probe's cross-model measurement.
COHERE_EMBED_MODEL = (os.environ.get("COHERE_EMBED_MODEL") or "embed-v5.0-pro").strip()
COHERE_EMBED_QUERY_MODEL = (
    os.environ.get("COHERE_EMBED_QUERY_MODEL") or COHERE_EMBED_MODEL
).strip()
# Embed 5 Pro offers 2048/1536/1024/768/512/256. 1024 matches the existing
# georag_chunks collection, so no Qdrant migration. MUST match
# settings.EMBEDDING_DIMENSION: config.py fails startup when they differ and
# main.py's dimension check re-asserts it against get_sentence_embedding_dimension().
COHERE_EMBED_DIMENSION = int(os.environ.get("COHERE_EMBED_DIMENSION", "1024"))
# Total budget for the QUERY path (config.py validates it under
# TIMEOUT_QDRANT_S, like BEDROCK_EMBED_TIMEOUT_S). On the ingest path this is
# only the per-request read timeout: a sweep has no wall clock and must not
# inherit a query's budget, nor the query its sweep's patience.
COHERE_EMBED_TIMEOUT_S = float(os.environ.get("COHERE_EMBED_TIMEOUT_S", "30"))

# ---------------------------------------------------------------------------
# Amazon Bedrock (Cohere Embed v4) backend — EMBEDDING_BACKEND=bedrock
# ---------------------------------------------------------------------------
# The rollback for ADR-0025 and the route back if Bedrock lists Embed 5. Reaches
# Bedrock with the ECS task role's SigV4 credentials (app.services._bedrock)
# rather than an endpoint plus API key. Vectors are Embed v4's: switching to or
# from it needs a full re-embed (scripts/reset_embeddings_for_reencode.py --all)
# or a Qdrant snapshot.
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
#: The same figure for Embed 5 on Cohere's own API. [ASSUMED] -- carried over
#: from v4, not read from any Embed 5 response; the probe's input-count ladder
#: (cohere_probe.probe_embed) is what measures it. Kept a separate name so the
#: two can diverge without one silently moving the other.
COHERE_EMBED_MAX_TEXTS_PER_CALL = 96

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

    @property
    def model_name(self) -> str:
        """The model id, recorded as ``embed_model`` on every point it writes.

        Not the host: ``cohere.embed-v4:0`` and ``embed-v5.0-pro`` are what a
        reader needs to tell two vector spaces apart (ADR-0025).
        """
        return self._model_id

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


class CohereEmbeddingHttpError(RuntimeError):
    """A non-2xx from ``POST /v2/embed`` that was not (or is no longer) retried.

    Carries the status because ``embed_image`` branches on it: 400/422 means
    the body's SHAPE was refused and the alternate image shape is worth one
    try; anything else means the request was understood.
    """

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"HTTP {status_code} from Cohere embed: {message}")
        self.status_code = status_code
        self.message = message


#: Statuses worth another try, the same set llm_cohere.py and
#: cohere_parse_client.py retry. 4xx other than 429 are a rejected request.
_COHERE_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
#: Ingest has no wall clock, so it can afford the full ladder (2, 4, 8 s plus
#: any Retry-After). The query path is capped by _QUERY_MAX_ATTEMPTS and, more
#: tightly, by its time budget.
_COHERE_INGEST_MAX_ATTEMPTS = 4
#: Ceiling on an honoured Retry-After during ingest. A sweep that is told to
#: wait longer than this has hit a quota, not a blip; it records the error and
#: the next sweep retries, rather than parking a pool thread for minutes.
_COHERE_INGEST_MAX_RETRY_AFTER_S = 60.0
_COHERE_CONNECT_TIMEOUT_S = 10.0
#: Indirection so tests can run the pacing without real sleeps.
_sleep = time.sleep


class _CohereEmbedding:
    """Cohere Embed 5 on Cohere's own API, behind the SentenceTransformer surface.

    A sibling of :class:`_BedrockEmbedding` with the same duck-typed surface
    (``encode`` / ``embed_query`` / ``embed_image`` /
    ``get_sentence_embedding_dimension``). **The names are a contract enforced
    only by duck typing**: ``tools.py`` checks ``hasattr(model, "embed_query")``
    and, if it is missing, silently embeds every question as
    ``search_document``. test_embedding_cohere.py drives the real call site.

    Wire shape (ADR-0025)::

        POST {COHERE_BASE_URL}/v2/embed
        Authorization: bearer $COHERE_API_KEY
        {"model": "embed-v5.0-pro", "texts": [str, ...],
         "input_type": "search_document"|"search_query",
         "embedding_types": ["float"], "output_dimension": 1024}
        -> {"embeddings": {"float": [[...], ...]}}

    **[UNVERIFIED]** — nothing here has been observed from a call made with our
    key; every field is ``Status.ASSUMED`` in ``cohere_wire.EMBED``. Run
    ``ops/validation/cohere_probe.py`` (``probe_embed``) and commit its report.

    Synchronous httpx on purpose, like :class:`_RemoteEmbedding`: every caller
    already runs these in an executor thread, so the event loop is never
    blocked. ``model_name`` is the DOCUMENT model (what ingest tags points
    with); ``query_model_name`` is what questions are embedded with and what
    ``answer_runs.embedding_model`` records.
    """

    def __init__(
        self,
        model: str = COHERE_EMBED_MODEL,
        *,
        query_model: str | None = None,
        dimension: int = COHERE_EMBED_DIMENSION,
        timeout_s: float = COHERE_EMBED_TIMEOUT_S,
        client: Any = None,
    ) -> None:
        self._model = model
        self._query_model = query_model or model
        self._dimension = dimension
        self._timeout_s = timeout_s
        self._client_obj = client
        self._client_lock = threading.Lock()

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def query_model_name(self) -> str:
        return self._query_model

    # -- transport --------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        from app.config import settings  # noqa: PLC0415

        key = (settings.COHERE_API_KEY or "").strip()
        if not key:
            raise RuntimeError(
                "COHERE_API_KEY is empty but EMBEDDING_BACKEND=cohere. It is the "
                "same key as chat and Parse, written to Secrets Manager out of "
                "band (deploy/aws/README.md Step 3); ECS will not start a task "
                "referencing a key that does not exist, so reaching this line "
                "means the value is present but blank."
            )
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _url(self) -> str:
        from app.config import settings  # noqa: PLC0415

        return f"{(settings.COHERE_BASE_URL or 'https://api.cohere.com').rstrip('/')}/v2/embed"

    def _http(self) -> Any:
        """One pooled ``httpx.Client`` per instance, built on first use."""
        import httpx  # noqa: PLC0415

        with self._client_lock:
            if self._client_obj is None:
                self._client_obj = httpx.Client(
                    timeout=httpx.Timeout(self._timeout_s, connect=_COHERE_CONNECT_TIMEOUT_S)
                )
            return self._client_obj

    def _retry_wait_s(
        self,
        attempt: int,
        retry_after_s: float | None,
        *,
        deadline: float | None,
    ) -> float | None:
        """Seconds to wait before retry ``attempt``, or None if it must not happen.

        The ladder and the ``Retry-After`` rule are the shared ones
        (llm_common.pre_stream_backoff_s). What differs is the budget: a query
        gives up when the wait would not fit its deadline or the host asks for
        more than PRE_STREAM_RETRY_AFTER_CAP_S; ingest clamps Retry-After to
        _COHERE_INGEST_MAX_RETRY_AFTER_S and keeps going.
        """
        from app.agent.llm_common import (  # noqa: PLC0415
            PRE_STREAM_RETRY_AFTER_CAP_S,
            pre_stream_backoff_s,
        )

        if deadline is None:
            if retry_after_s is not None:
                retry_after_s = min(retry_after_s, _COHERE_INGEST_MAX_RETRY_AFTER_S)
            return pre_stream_backoff_s(attempt, retry_after_s=retry_after_s)
        if retry_after_s is not None and retry_after_s > PRE_STREAM_RETRY_AFTER_CAP_S:
            return None
        delay = pre_stream_backoff_s(attempt, retry_after_s=retry_after_s)
        # Leave at least a second for the attempt the wait is buying.
        return delay if time.monotonic() + delay + 1.0 < deadline else None

    def _request(self, body: dict[str, Any], *, query_path: bool = False) -> dict[str, Any]:
        """POST one embed body; retry 429/5xx/transport faults; return the JSON.

        Retries are explicit because httpx has none (botocore did this under
        Bedrock, which is why this host would otherwise regress under load).
        On the query path the whole call, waits included, stays inside
        COHERE_EMBED_TIMEOUT_S (config.py validates it under TIMEOUT_QDRANT_S);
        on the ingest path it does not.
        """
        import httpx  # noqa: PLC0415

        headers = self._headers()
        url = self._url()
        started = time.monotonic()
        deadline = started + self._timeout_s if query_path else None
        max_attempts = _QUERY_MAX_ATTEMPTS if query_path else _COHERE_INGEST_MAX_ATTEMPTS

        for attempt in range(1, max_attempts + 1):
            read_timeout = self._timeout_s
            if deadline is not None:
                read_timeout = max(1.0, min(_QUERY_READ_TIMEOUT_S, deadline - time.monotonic()))
            retry_after_s: float | None = None
            try:
                resp = self._http().post(
                    url,
                    headers=headers,
                    json=body,
                    timeout=httpx.Timeout(read_timeout, connect=_COHERE_CONNECT_TIMEOUT_S),
                )
            except (httpx.TransportError, httpx.StreamError) as exc:
                # Retried below; the final failure is raised with its cause.
                logger.debug("cohere embed: transport error, will retry if budget allows", exc_info=True)
                failure: Exception = exc
            else:
                if resp.status_code < 300:
                    try:
                        payload = resp.json()
                    except ValueError as exc:
                        raise RuntimeError(
                            f"Cohere embed answered HTTP {resp.status_code} with a body that is not JSON"
                        ) from exc
                    if not isinstance(payload, dict):
                        raise RuntimeError("Cohere embed answered with a JSON body that is not an object")
                    return payload
                failure = CohereEmbeddingHttpError(resp.status_code, resp.text[:200])
                if resp.status_code not in _COHERE_RETRYABLE_STATUS:
                    raise failure
                from app.agent.llm_common import parse_retry_after  # noqa: PLC0415

                retry_after_s = parse_retry_after(resp.headers.get("retry-after"))

            wait = (
                self._retry_wait_s(attempt, retry_after_s, deadline=deadline)
                if attempt < max_attempts
                else None
            )
            if wait is None:
                raise failure
            logger.warning(
                "cohere embed: %s (attempt %d/%d, %s path) -- retrying in %.1fs",
                type(failure).__name__ if not isinstance(failure, CohereEmbeddingHttpError)
                else f"HTTP {failure.status_code}",
                attempt,
                max_attempts,
                "query" if query_path else "ingest",
                wait,
            )
            _sleep(wait)
        raise RuntimeError("cohere embed: retry loop exited without a result")  # pragma: no cover

    def _vectors(self, payload: dict[str, Any], expected_rows: int, model: str) -> np.ndarray:
        try:
            raw = payload["embeddings"]["float"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError(
                f"Cohere embed answered without embeddings.float (model {model!r}; "
                f"top-level keys {sorted(payload)})"
            ) from exc
        vectors = np.asarray(raw, dtype=np.float32)
        if vectors.ndim != 2 or vectors.shape[0] != expected_rows:
            # Concatenating a short answer would silently attach every later
            # vector to the wrong text.
            raise RuntimeError(
                f"Cohere embed returned shape {tuple(vectors.shape)} for {expected_rows} input(s) "
                f"(model {model!r})"
            )
        if vectors.shape[1] != self._dimension:
            raise RuntimeError(
                f"Cohere embed returned {vectors.shape[1]}-dim vectors but "
                f"COHERE_EMBED_DIMENSION={self._dimension} (model {model!r}); "
                "writing them would corrupt georag_chunks"
            )
        return vectors

    # -- text -------------------------------------------------------------

    def _post(self, texts: list[str], input_type: str, *, query_path: bool = False) -> np.ndarray:
        """Embed ``texts``, in requests of at most COHERE_EMBED_MAX_TEXTS_PER_CALL.

        Chunked here, not at each caller, for the reason ``_BedrockEmbedding._post``
        gives: ``encode()`` absorbs ``batch_size``, so a caller's batch size was
        never a bound on the request.
        """
        if not texts:
            return np.zeros((0, self._dimension), dtype=np.float32)
        model = self._query_model if input_type == "search_query" else self._model
        parts: list[np.ndarray] = []
        for start in range(0, len(texts), COHERE_EMBED_MAX_TEXTS_PER_CALL):
            chunk = texts[start : start + COHERE_EMBED_MAX_TEXTS_PER_CALL]
            body = {
                "model": model,
                "texts": chunk,
                "input_type": input_type,
                "embedding_types": ["float"],
                "output_dimension": self._dimension,
            }
            payload = self._request(body, query_path=query_path)
            parts.append(self._vectors(payload, len(chunk), model))
        return parts[0] if len(parts) == 1 else np.concatenate(parts, axis=0)

    def encode(
        self,
        sentences: str | list[str],
        normalize_embeddings: bool = False,  # noqa: ARG002 — vectors are assumed pre-normalized
        input_type: str = "search_document",
        **_kwargs: Any,  # absorbs show_progress_bar, batch_size, prompt_name, etc.
    ) -> np.ndarray:
        single = isinstance(sentences, str)
        texts = [sentences] if single else list(sentences)
        arr = self._post(texts, input_type)
        return arr[0] if single else arr

    def embed_query(self, text: str) -> np.ndarray:
        """Query-time embedding with ``input_type="search_query"``.

        On the budgeted query path (COHERE_EMBED_TIMEOUT_S), with the query
        model, not the ingest one.
        """
        return self._post([text], "search_query", query_path=True)[0]

    # -- multimodal (page-image) embedding --------------------------------
    #
    # Same constraints and the same reason as _BedrockEmbedding: image vectors
    # share the text space, so a page image lands in the existing dense slot;
    # text and image inputs cannot be mixed in one call; callers downscale
    # first (page_image.render_page_png). The pixel cap for Embed 5 is
    # UNOBSERVED (v4's was 2M px) -- cohere_probe.probe_embed measures it.
    #
    # Which image request shape Embed 5 accepts is also unobserved (ADR-0025
    # gotcha 4). The try-primary, fall-back-once strategy carries over, but a
    # schema rejection is an HTTP 400/422 here, not a botocore
    # ValidationException. Collapse to the winner once the probe or the first
    # ingest log names it.
    _IMAGE_WIRE_SHAPE: str | None = None  # None = undetermined; set on first success

    def embed_image(self, png_bytes: bytes, *, mime: str = "image/png") -> np.ndarray:
        """Embed ONE page image, returning a ``COHERE_EMBED_DIMENSION`` vector.

        Single-image on purpose, for the memory reason in
        ``_BedrockEmbedding.embed_image`` (the 2026-08-07 SPLADE OOM).
        """
        import base64  # noqa: PLC0415

        data_uri = f"data:{mime};base64,{base64.b64encode(png_bytes).decode('ascii')}"

        def _body_images() -> dict[str, Any]:
            return {
                "model": self._model,
                "images": [data_uri],
                "input_type": "image",
                "embedding_types": ["float"],
                "output_dimension": self._dimension,
            }

        def _body_inputs() -> dict[str, Any]:
            return {
                "model": self._model,
                "inputs": [{"content": [{"type": "image_url", "image_url": {"url": data_uri}}]}],
                "input_type": "image",
                "embedding_types": ["float"],
                "output_dimension": self._dimension,
            }

        shapes: list[tuple[str, Any]] = (
            [("images", _body_images), ("inputs", _body_inputs)]
            if _CohereEmbedding._IMAGE_WIRE_SHAPE in (None, "images")
            else [("inputs", _body_inputs), ("images", _body_images)]
        )

        last_exc: Exception | None = None
        for name, build_body in shapes:
            try:
                payload = self._request(build_body())
            except CohereEmbeddingHttpError as exc:
                # Only a schema rejection is worth re-shaping for. Anything
                # else (auth, throttling that outlasted its retries, 5xx)
                # means the request was understood and different JSON just
                # burns another call.
                if exc.status_code in (400, 422):
                    last_exc = exc
                    logger.debug(
                        "cohere image embed: wire shape %r rejected (HTTP %d) — trying alternate",
                        name, exc.status_code,
                    )
                    continue
                raise

            if name != _CohereEmbedding._IMAGE_WIRE_SHAPE:
                _CohereEmbedding._IMAGE_WIRE_SHAPE = name
                logger.info("cohere image embed: using wire shape %r", name)
            return self._vectors(payload, 1, self._model)[0]

        raise RuntimeError(
            "Cohere Embed rejected both documented image wire shapes "
            f"(images[], inputs[]) on model {self._model!r}"
        ) from last_exc

    def get_sentence_embedding_dimension(self) -> int:
        return self._dimension


def build_cohere_embedding() -> _CohereEmbedding:
    """The one construction site for ``EMBEDDING_BACKEND=cohere``.

    Called by BOTH ``get_embedding_model`` (query path) and
    ``passage_embedder.load_embedding_model`` (ingest path), so the two cannot
    drift apart on model, dimension or credentials -- a mismatch writes one
    vector space and queries another (ADR-0021 migration step 2, ADR-0025
    gotcha 1). Fails loudly on a blank key rather than at the first call.
    """
    from app.config import settings  # noqa: PLC0415
    from app.services._bedrock import assert_no_retired_foundry_env  # noqa: PLC0415

    assert_no_retired_foundry_env(context="EMBEDDING_BACKEND=cohere")
    if not (settings.COHERE_API_KEY or "").strip():
        raise RuntimeError(
            "EMBEDDING_BACKEND=cohere but COHERE_API_KEY is empty. It is the same "
            "key as chat and Parse, so a key that only covers chat will fail "
            "here too. Set it, or set EMBEDDING_BACKEND=bedrock / local."
        )
    if not COHERE_EMBED_MODEL:
        raise RuntimeError("EMBEDDING_BACKEND=cohere but COHERE_EMBED_MODEL is empty")
    logger.info(
        "Embedding model via Cohere API: documents=%s queries=%s dim=%d",
        COHERE_EMBED_MODEL, COHERE_EMBED_QUERY_MODEL, COHERE_EMBED_DIMENSION,
    )
    return _CohereEmbedding(
        COHERE_EMBED_MODEL,
        query_model=COHERE_EMBED_QUERY_MODEL,
        dimension=COHERE_EMBED_DIMENSION,
        timeout_s=COHERE_EMBED_TIMEOUT_S,
    )


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

    Precedence: EMBEDDING_BACKEND=cohere (Cohere Embed 5 on Cohere's own API,
    the default; no local model at all) > bedrock (Cohere Embed v4, the
    rollback) > a shared-sidecar HTTP proxy when EMBEDDING_SERVICE_URL is set >
    locally-loaded SentenceTransformer on CPU.
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

    if EMBEDDING_BACKEND == "cohere":
        return build_cohere_embedding()
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
