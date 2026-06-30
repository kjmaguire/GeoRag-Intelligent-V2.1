"""Dedicated cross-encoder reranker sidecar (2026-06-24).

Hosts ONE `bge-reranker-base` CrossEncoder so the main FastAPI service's
uvicorn workers don't each load their own copy. Previously every one of the 6
workers lazily loaded a ~1-1.5 GiB CrossEncoder on first query, which drove the
container past its memory limit and OOM-killed it mid-stream. The workers now
share this single model over a localhost HTTP hop (see
`app.services.reranker._RemoteReranker`, gated by RERANKER_SERVICE_URL).

Run as its own service with a single worker:

    uvicorn app.reranker_service:app --host 0.0.0.0 --port 8000 --workers 1

This module imports ONLY `app.services.reranker` (no §04p PDF stack, no
embedding/SPLADE, no DB) so the sidecar stays lean. It must NOT have
RERANKER_SERVICE_URL set — it is the model host, not a proxy.

Endpoints:
    POST /rerank   {"pairs": [[query, passage], ...]} -> {"scores": [...], "version": str}
    GET  /health   200 once the model is loaded, 503 otherwise.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.services.reranker import RERANKER_VERSION, _get_reranker
from app.sidecar_auth import enforce_batch_limits, require_service_key

logger = logging.getLogger(__name__)

# Audit 2026-06-27: bound the rerank batch. The query path reranks a few dozen
# candidates; the cap is generous but stops an unbounded-body memory DoS.
_MAX_PAIRS = int(os.environ.get("RERANK_MAX_PAIRS", "1024"))
_MAX_TOTAL_CHARS = int(os.environ.get("RERANK_MAX_TOTAL_CHARS", "4000000"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pre-warm the single shared model copy at startup so the first /rerank
    # call doesn't pay the cold-load. A failure here is logged but not fatal —
    # /health then reports 503 and callers degrade to RRF order.
    try:
        await asyncio.to_thread(_get_reranker)
        logger.info("reranker sidecar: model pre-warmed (%s)", RERANKER_VERSION)
    except Exception:
        logger.exception("reranker sidecar: model preload failed at startup")
    yield


app = FastAPI(title="GeoRAG reranker sidecar", lifespan=lifespan)


class RerankRequest(BaseModel):
    pairs: list[tuple[str, str]] = Field(
        ..., description="(query, passage) pairs to score; mirrors CrossEncoder.predict input"
    )


class RerankResponse(BaseModel):
    scores: list[float]
    version: str


@app.post(
    "/rerank",
    response_model=RerankResponse,
    dependencies=[Depends(require_service_key)],
)
async def rerank(req: RerankRequest) -> RerankResponse:
    """Score (query, passage) pairs with the shared cross-encoder."""
    if not req.pairs:
        return RerankResponse(scores=[], version=RERANKER_VERSION)
    # Flatten (query, passage) tuples for the total-chars guard.
    enforce_batch_limits(
        [s for pair in req.pairs for s in pair],
        max_items=_MAX_PAIRS * 2,
        max_total_chars=_MAX_TOTAL_CHARS, label="rerank",
    )
    try:
        model = _get_reranker()
        # CrossEncoder.predict is sync + CPU-bound — run it off the event loop.
        scores = await asyncio.to_thread(model.predict, req.pairs)
    except Exception as exc:  # noqa: BLE001
        logger.exception("reranker sidecar: predict failed")
        raise HTTPException(status_code=503, detail="reranker_unavailable") from exc
    return RerankResponse(scores=[float(s) for s in scores], version=RERANKER_VERSION)


@app.get("/health")
async def health() -> dict[str, str]:
    """200 once the model is loadable, else 503 (so the orchestrator degrades)."""
    try:
        await asyncio.to_thread(_get_reranker)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail="model_not_loaded") from exc
    return {"status": "ok", "version": RERANKER_VERSION}
