"""Dedicated SPLADE++ sparse-encoder sidecar (2026-06-24).

Hosts ONE SPLADE++ model so the main FastAPI service's uvicorn workers don't
each load their own. SPLADE was the LAST GPU user inside those workers (the
dense embedding and the reranker are already sidecar'd / CPU), so routing it
here lets the workers run CPU-only and drop their per-process CUDA contexts.
The workers proxy ``encode_sparse`` / ``encode_sparse_batch`` to this service
over a localhost hop (gated by SPARSE_SERVICE_URL in
``app.services.sparse_encoder``). Same pattern as the reranker + embedding
sidecars.

This service runs SPLADE on CPU (fp32) — it is given no GPU device in compose,
so ``torch.cuda.is_available()`` is False and the model loads on CPU. A single
query encode is ~15-60 ms (well within the query budget) and it frees the
model's VRAM on the contended A4500 entirely. The Dagster bulk-index pipeline
keeps its own model; only the FastAPI query path is proxied.

Run with one worker:
    uvicorn app.sparse_service:app --host 0.0.0.0 --port 8000 --workers 1

Imports only ``app.services.sparse_encoder`` (which pulls torch/transformers
lazily, no app.config Settings, no DB) so the sidecar stays lean. It must NOT
have SPARSE_SERVICE_URL set — it is the model host; setting it would make the
sidecar proxy to itself (the encode functions would recurse over HTTP).

Endpoints:
    POST /sparse  {"texts": [...]} -> {"sparse": [{token_id: weight}, ...]}
                  (token_id keys are JSON strings; the proxy restores them to int)
    GET  /health  200 once the model is warm, 503 otherwise.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Fail fast on the self-proxy misconfiguration: if SPARSE_SERVICE_URL were set
# here, encode_sparse would POST to this very service and recurse over HTTP.
if (os.environ.get("SPARSE_SERVICE_URL") or "").strip():
    raise RuntimeError(
        "SPARSE_SERVICE_URL must NOT be set on the sparse sidecar — it is the "
        "model host, not a proxy (would recurse over HTTP)."
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pre-warm the single shared model at startup so the first /sparse doesn't
    # pay the cold load. Non-fatal — /health reports 503 until it succeeds.
    try:
        from app.services.sparse_encoder import encode_sparse  # noqa: PLC0415

        n = await asyncio.to_thread(lambda: len(encode_sparse("warm-up drillhole uranium grade")))
        logger.info("sparse sidecar ready: SPLADE++ warm (%d non-zero terms)", n)
    except Exception:
        logger.exception("sparse sidecar: model warm-up failed at startup")
    yield


app = FastAPI(lifespan=lifespan, title="georag-sparse-sidecar")


class SparseRequest(BaseModel):
    texts: list[str]


@app.post("/sparse")
async def sparse(req: SparseRequest) -> dict:
    if not req.texts:
        return {"sparse": []}
    from app.services.sparse_encoder import encode_sparse_batch  # noqa: PLC0415

    vecs = await asyncio.to_thread(encode_sparse_batch, req.texts)
    # JSON object keys must be strings; the proxy restores int token-ids.
    return {"sparse": [{str(k): v for k, v in d.items()} for d in vecs]}


@app.get("/health")
async def health() -> dict:
    from app.services.sparse_encoder import _get_sparse_model  # noqa: PLC0415

    try:
        # Cached after the lifespan pre-warm — cheap. Triggers a (re)load if the
        # startup warm-up failed transiently.
        await asyncio.to_thread(_get_sparse_model)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"sparse model not ready: {exc}") from exc
    return {"status": "ok"}
