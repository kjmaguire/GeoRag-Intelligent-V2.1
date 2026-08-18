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

import logging
import os
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
# Azure AI Foundry (Cohere Embed v4) backend — EMBEDDING_BACKEND=foundry
# ---------------------------------------------------------------------------
# Takes precedence over EMBEDDING_SERVICE_URL below. No local model, no
# sidecar, at all. Reuses the same Azure AI Services resource as the LLM/
# reranker (AZURE_FOUNDRY_ENDPOINT/API_KEY) with its own deployment name.
EMBEDDING_BACKEND = (os.environ.get("EMBEDDING_BACKEND") or "local").strip().lower()
AZURE_FOUNDRY_EMBED_DEPLOYMENT = (os.environ.get("AZURE_FOUNDRY_EMBED_DEPLOYMENT") or "").strip()
# Cohere Embed v4 supports Matryoshka-truncated output at 256/512/1024/1536
# dims. Request 1024 to match the existing georag_chunks collection schema
# exactly — no Qdrant migration needed. MUST match settings.EMBEDDING_DIMENSION.
AZURE_FOUNDRY_EMBED_DIMENSION = int(os.environ.get("AZURE_FOUNDRY_EMBED_DIMENSION", "1024"))
AZURE_FOUNDRY_EMBED_TIMEOUT_S = float(os.environ.get("AZURE_FOUNDRY_EMBED_TIMEOUT_S", "30"))


class _FoundryEmbedding:
    """Cohere Embed v4 (Azure AI Foundry) behind the SentenceTransformer surface.

    Mirrors ``SentenceTransformer.encode(str|list, normalize_embeddings=...)
    -> np.ndarray`` and ``.get_sentence_embedding_dimension()`` — same
    contract as ``_RemoteEmbedding`` above, so it's a drop-in wherever the
    query-path or ingestion code holds an embedding-model reference.

    Wire shape empirically verified 2026-07-30 against a live deployment:
        POST {endpoint}/providers/cohere/v2/embed
        api-key: <key>
        body: {"model": "<deployment>", "texts": [str, ...],
               "input_type": "search_document"|"search_query",
               "embedding_types": ["float"], "output_dimension": 1024}
        -> {"embeddings": {"float": [[...], ...]}}

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
        endpoint: str,
        api_key: str,
        deployment: str,
        *,
        dimension: int = AZURE_FOUNDRY_EMBED_DIMENSION,
        timeout_s: float = AZURE_FOUNDRY_EMBED_TIMEOUT_S,
    ) -> None:
        self._url = endpoint.rstrip("/") + "/providers/cohere/v2/embed"
        self._api_key = api_key
        self._deployment = deployment
        self._dimension = dimension
        self._timeout_s = timeout_s

    def _post(self, texts: list[str], input_type: str) -> np.ndarray:
        import httpx  # noqa: PLC0415

        from app.services._foundry_retry import with_foundry_retry  # noqa: PLC0415

        def _do() -> httpx.Response:
            return httpx.post(
                self._url,
                headers={"api-key": self._api_key},
                timeout=self._timeout_s,
                json={
                    "model": self._deployment,
                    "texts": texts,
                    "input_type": input_type,
                    "embedding_types": ["float"],
                    "output_dimension": self._dimension,
                },
            )

        resp = with_foundry_retry(_do, label="foundry_embed")
        vectors = resp.json()["embeddings"]["float"]
        return np.asarray(vectors, dtype=np.float32)

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
        """Query-time embedding using Cohere's recommended input_type="search_query"."""
        return self._post([text], "search_query")[0]

    # -- multimodal (page-image) embedding -------------------------------
    #
    # Embed v4 is multimodal and places image vectors in the SAME output
    # space as text vectors, so an image point lands in the existing
    # `georag_chunks` dense slot ('' / 1024-dim / Cosine) and a plain text
    # query matches it with no retrieval changes at all. That shared space
    # is the entire reason this feature is cheap to add — do not "fix" it
    # by giving images their own collection.
    #
    # Two hard constraints from the model card (verified against
    # learn.microsoft.com 2026-08-18, `embed-v-4-0` on Foundry):
    #   1. Images cap at 2M pixels. Callers MUST downscale first — see
    #      app.services.ingest.page_image.render_page_png, which is the
    #      only supported producer. A 250-DPI letter page is ~5.8M px and
    #      is rejected outright.
    #   2. Text and image inputs CANNOT be combined in one call
    #      ("cannot have both text and image inputs"). So this is a
    #      separate request from _post(), never a merged one.
    #
    # WIRE SHAPE: unlike the text path above (empirically verified against
    # a live deployment 2026-07-30) the image path is written to Cohere's
    # documented v2 contract but has NOT yet been confirmed against this
    # deployment. Cohere shipped two accepted shapes for v4 — the older
    # `images: [data-uri]` and the newer interleaved `inputs: [...]` — and
    # which one a given Foundry build accepts is not documented. Rather
    # than guess, the primary shape is tried first and a 400/422 falls
    # back to the alternate ONCE, logging whichever succeeded so the first
    # real run tells us definitively. Collapse this to the winner (and
    # delete the fallback) once observed in production logs.
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

        import httpx  # noqa: PLC0415

        from app.services._foundry_retry import with_foundry_retry  # noqa: PLC0415

        data_uri = f"data:{mime};base64,{base64.b64encode(png_bytes).decode('ascii')}"

        def _body_images() -> dict[str, Any]:
            return {
                "model": self._deployment,
                "images": [data_uri],
                "input_type": "image",
                "embedding_types": ["float"],
                "output_dimension": self._dimension,
            }

        def _body_inputs() -> dict[str, Any]:
            return {
                "model": self._deployment,
                "inputs": [{"content": [{"type": "image_url", "image_url": {"url": data_uri}}]}],
                "input_type": "image",
                "embedding_types": ["float"],
                "output_dimension": self._dimension,
            }

        shapes: list[tuple[str, Any]] = (
            [("images", _body_images), ("inputs", _body_inputs)]
            if _FoundryEmbedding._IMAGE_WIRE_SHAPE in (None, "images")
            else [("inputs", _body_inputs), ("images", _body_images)]
        )

        last_exc: Exception | None = None
        for name, build_body in shapes:
            def _do(_b=build_body) -> httpx.Response:
                return httpx.post(
                    self._url,
                    headers={"api-key": self._api_key},
                    timeout=self._timeout_s,
                    json=_b(),
                )

            try:
                resp = with_foundry_retry(_do, label=f"foundry_embed_image[{name}]")
            except httpx.HTTPStatusError as exc:
                # Only a schema rejection is worth re-shaping for. Anything
                # else (401, 429, 5xx) means the request was understood and
                # retrying with different JSON just burns another call.
                if exc.response is not None and exc.response.status_code in (400, 422):
                    last_exc = exc
                    logger.debug(
                        "foundry image embed: wire shape %r rejected (%s) — trying alternate",
                        name, exc.response.status_code,
                    )
                    continue
                raise

            if name != _FoundryEmbedding._IMAGE_WIRE_SHAPE:
                _FoundryEmbedding._IMAGE_WIRE_SHAPE = name
                logger.info("foundry image embed: using wire shape %r", name)
            vectors = resp.json()["embeddings"]["float"]
            return np.asarray(vectors, dtype=np.float32)[0]

        raise RuntimeError(
            "Cohere Embed v4 rejected both documented image wire shapes "
            f"(images[], inputs[]) on deployment {self._deployment!r}"
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


def get_embedding_model(
    model_name: str,
    revision: str = EMBEDDING_MODEL_REVISION,
) -> Any:
    """Return the embedding model for the FastAPI query path.

    Precedence: EMBEDDING_BACKEND=foundry (Cohere Embed v4, no local model at
    all) > a shared-sidecar HTTP proxy when EMBEDDING_SERVICE_URL is set >
    locally-loaded SentenceTransformer on CPU (the prior default).
    """
    if EMBEDDING_BACKEND == "foundry":
        endpoint = (os.environ.get("AZURE_FOUNDRY_ENDPOINT") or "").strip()
        api_key = (os.environ.get("AZURE_FOUNDRY_API_KEY") or "").strip()
        if not (endpoint and api_key and AZURE_FOUNDRY_EMBED_DEPLOYMENT):
            raise RuntimeError(
                "EMBEDDING_BACKEND=foundry but AZURE_FOUNDRY_ENDPOINT/API_KEY/"
                "AZURE_FOUNDRY_EMBED_DEPLOYMENT not fully set"
            )
        logger.info(
            "Embedding model via Azure AI Foundry: deployment=%s dim=%d",
            AZURE_FOUNDRY_EMBED_DEPLOYMENT, AZURE_FOUNDRY_EMBED_DIMENSION,
        )
        return _FoundryEmbedding(endpoint, api_key, AZURE_FOUNDRY_EMBED_DEPLOYMENT)
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
