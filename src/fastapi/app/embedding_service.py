"""Dedicated embedding sidecar (2026-06-24).

Hosts ONE SentenceTransformer (``_MODEL_NAME``, CPU) so the
main FastAPI service's uvicorn workers don't each load their own copy. For
Qwen3-Embedding-0.6B that copy is ~2.4 GiB of host RAM, and (measured) it is NOT
shared across workers — N workers meant N full copies. The workers now share
this single model over a localhost HTTP hop (see
``app.services.embedding._RemoteEmbedding``, gated by ``EMBEDDING_SERVICE_URL``).

Run as its own service with a single worker:

    uvicorn app.embedding_service:app --host 0.0.0.0 --port 8000 --workers 1

This module imports ONLY sentence_transformers (no app.config Settings, no §04p
PDF stack, no reranker, no DB) so the sidecar stays lean and needs no DB/service
secrets. It must NOT have EMBEDDING_SERVICE_URL set — it is the model host, not a
proxy. The model name comes from the EMBEDDING_MODEL_NAME env var.

Endpoints:
    POST /embed   {"sentences": [...], "normalize": bool}
                  -> {"vectors": [[...]], "dimension": int}
    GET  /health  200 {"dimension": int} once the model is loaded, 503 otherwise.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Read the model name straight from the environment rather than app.config —
# importing the full Settings would demand FASTAPI_SERVICE_KEY / POSTGRES_PASSWORD
# etc. that this lean model-host has no business needing (the reranker sidecar
# avoids the same trap). The compose env passes EMBEDDING_MODEL_NAME to match the
# fastapi service, so the shared model has the dimension the query path expects.
_MODEL_NAME = os.environ.get("EMBEDDING_MODEL_NAME", "BAAI/bge-small-en-v1.5")

_model = None  # the single shared SentenceTransformer (loaded at startup)


def _load_model():
    """Load + warm the model. Called once at startup (and defensively on first
    request if startup load failed)."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415

        model = SentenceTransformer(_MODEL_NAME, device="cpu")
        # Warm-up so the first real /embed doesn't pay the JIT/model-init cost.
        model.encode("warm-up", normalize_embeddings=True)
        _model = model
    return _model


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pre-warm the single shared copy at startup. A failure here is logged but
    # not fatal — /health then reports 503 and callers can fall back to a local
    # load (or fail the query) rather than the sidecar wedging.
    try:
        await asyncio.to_thread(_load_model)
        logger.info(
            "embedding sidecar ready: %s (dim=%s)",
            _MODEL_NAME,
            _model.get_sentence_embedding_dimension() if _model else "?",
        )
    except Exception:
        logger.exception("embedding sidecar: model load failed at startup")
    yield


app = FastAPI(lifespan=lifespan, title="georag-embedding-sidecar")


class EmbedRequest(BaseModel):
    sentences: list[str]
    normalize: bool = False


@app.post("/embed")
async def embed(req: EmbedRequest) -> dict:
    if _model is None:
        # One lazy retry in case startup load failed transiently.
        try:
            await asyncio.to_thread(_load_model)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"embedding model unavailable: {exc}") from exc
    if not req.sentences:
        return {"vectors": [], "dimension": 0}
    vectors = await asyncio.to_thread(
        lambda: _model.encode(
            req.sentences, normalize_embeddings=req.normalize, show_progress_bar=False
        ).tolist()
    )
    return {"vectors": vectors, "dimension": len(vectors[0]) if vectors else 0}


@app.get("/health")
async def health() -> dict:
    if _model is None:
        raise HTTPException(status_code=503, detail="embedding model not loaded")
    return {"status": "ok", "dimension": _model.get_sentence_embedding_dimension()}
