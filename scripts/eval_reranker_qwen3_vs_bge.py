"""Compare bge-reranker-base vs Qwen3-Reranker (causal-LM) on the golden set.

Audit 2026-06-29 (C). For each golden query: embed (live model) → retrieve
top-K from georag_chunks → rerank with each reranker → NDCG@10 against the
golden expected_substrings. Reports dense (no rerank), bge, and Qwen3 means.

DECISION RULE (Kyle 2026-06-29: "clear wins only"): flip the live reranker to
Qwen3 only if its mean NDCG@10 beats bge by >= RERANK_EVAL_WIN_DELTA (default
0.02). Otherwise keep bge.

Run in a GPU container with vllm-vl paused:
    docker run --rm --gpus all --network georag -e RERANK_EVAL_DEVICE=cuda \
      -v .../src/fastapi:/app -w /app ... georag/fastapi:latest \
      python scripts/eval_reranker_qwen3_vs_bge.py
"""
from __future__ import annotations

import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
)
log = logging.getLogger("rerank_eval")

sys.path.insert(0, "/app/scripts")
from bench_retrieval_ndcg import (  # noqa: E402
    GOLDEN_BENCH_QUERIES,
    QDRANT_COLLECTION,
    _grade,
    _ndcg,
)

TOP_K = int(os.environ.get("RERANK_EVAL_TOPK", "20"))
WIN_DELTA = float(os.environ.get("RERANK_EVAL_WIN_DELTA", "0.02"))
DEVICE = os.environ.get("RERANK_EVAL_DEVICE", "cuda")
MAX_CHARS = 2000


def _order(scores: list[float]) -> list[int]:
    return sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)


def main() -> None:
    from qdrant_client import QdrantClient
    from sentence_transformers import CrossEncoder, SentenceTransformer

    from app.config import settings
    from app.services.reranker import (
        RERANKER_MODEL_NAME,
        RERANKER_REVISION,
        _Qwen3CausalReranker,
    )

    log.info("Loading embedder %s on %s", settings.EMBEDDING_MODEL_NAME, DEVICE)
    embed = SentenceTransformer(settings.EMBEDDING_MODEL_NAME, device=DEVICE)
    qc = QdrantClient(
        host=os.environ.get("QDRANT_HOST", "qdrant"),
        port=int(os.environ.get("QDRANT_PORT", "6333")),
        timeout=60,
    )
    log.info("Loading bge reranker %s", RERANKER_MODEL_NAME)
    bge = CrossEncoder(RERANKER_MODEL_NAME, revision=RERANKER_REVISION, device=DEVICE)
    log.info("Loading Qwen3 causal reranker")
    qwen = _Qwen3CausalReranker(
        os.environ.get("QWEN3_RERANKER_MODEL", "Qwen/Qwen3-Reranker-0.6B"),
        device=DEVICE,
    )

    sums = {"dense": 0.0, "bge": 0.0, "qwen3": 0.0}
    n = 0
    for case in GOLDEN_BENCH_QUERIES:
        dense_vec = embed.encode(
            [case["query"]], normalize_embeddings=True, show_progress_bar=False
        ).tolist()[0]
        res = qc.query_points(
            collection_name=QDRANT_COLLECTION, query=dense_vec, using="",
            limit=TOP_K, with_payload=True,
        )
        hits = res.points
        if not hits:
            continue
        texts = [str(h.payload.get("text") or h.payload.get("passage_text") or "") for h in hits]
        rels = [_grade(t, case["expected_substrings"]) for t in texts]
        pairs = [(case["query"], t[:MAX_CHARS]) for t in texts]

        sums["dense"] += _ndcg(rels, 10)
        bge_order = _order([float(s) for s in bge.predict(pairs)])
        sums["bge"] += _ndcg([rels[i] for i in bge_order], 10)
        qwen_order = _order([float(s) for s in qwen.predict(pairs)])
        sums["qwen3"] += _ndcg([rels[i] for i in qwen_order], 10)
        n += 1
        log.info("  %s done", case["id"])

    log.info("=== RERANKER EVAL (%d queries, top_k=%d) ===", n, TOP_K)
    for k in ("dense", "bge", "qwen3"):
        log.info("  mean NDCG@10 [%-6s] = %.4f", k, sums[k] / max(n, 1))
    bge_m = sums["bge"] / max(n, 1)
    qwen_m = sums["qwen3"] / max(n, 1)
    delta = qwen_m - bge_m
    log.info("  Qwen3 - bge delta = %+.4f (%.1f%%)", delta, 100 * delta / max(bge_m, 0.001))
    decision = (
        "FLIP_TO_QWEN3" if delta >= WIN_DELTA else "KEEP_BGE"
    )
    log.info("  DECISION (win_delta=%.3f): %s", WIN_DELTA, decision)


if __name__ == "__main__":
    main()
