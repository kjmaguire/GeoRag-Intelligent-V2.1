"""Query-prefix A/B for the Qwen3 embedding swap (audit 2026-06-29, B).

The live retrieval path DROPPED the bge-era query instruction prefix because
the Qwen3 corpus side has no prefix → prefix-free queries should match better.
This confirms it: NDCG@10 with a plain query vs with the old bge prefix, over
the golden bench queries against live georag_chunks.

DECISION (Kyle 2026-06-29: "clear wins only"): keep the prefix DROPPED (current
live) unless the prefixed variant beats plain by >= PREFIX_AB_WIN_DELTA (0.02
NDCG), in which case re-add it.
"""
from __future__ import annotations

import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
)
log = logging.getLogger("prefix_ab")

sys.path.insert(0, "/app/scripts")
from bench_retrieval_ndcg import (  # noqa: E402
    GOLDEN_BENCH_QUERIES,
    QDRANT_COLLECTION,
    _grade,
    _ndcg,
)

_BGE_PREFIX = "Represent this geological query for searching relevant passages: "
TOP_K = int(os.environ.get("PREFIX_AB_TOPK", "10"))
WIN_DELTA = float(os.environ.get("PREFIX_AB_WIN_DELTA", "0.02"))
DEVICE = os.environ.get("PREFIX_AB_DEVICE", "cpu")


def main() -> None:
    from qdrant_client import QdrantClient
    from sentence_transformers import SentenceTransformer

    from app.config import settings

    embed = SentenceTransformer(settings.EMBEDDING_MODEL_NAME, device=DEVICE)
    qc = QdrantClient(
        host=os.environ.get("QDRANT_HOST", "qdrant"),
        port=int(os.environ.get("QDRANT_PORT", "6333")),
        timeout=60,
    )

    plain_sum = pref_sum = 0.0
    n = 0
    for case in GOLDEN_BENCH_QUERIES:
        subs = case["expected_substrings"]
        for label, q in (("plain", case["query"]), ("pref", _BGE_PREFIX + case["query"])):
            vec = embed.encode(
                [q], normalize_embeddings=True, show_progress_bar=False
            ).tolist()[0]
            res = qc.query_points(
                collection_name=QDRANT_COLLECTION, query=vec, using="",
                limit=TOP_K, with_payload=True,
            )
            texts = [str(h.payload.get("text") or h.payload.get("passage_text") or "") for h in res.points]
            ndcg = _ndcg([_grade(t, subs) for t in texts], TOP_K)
            if label == "plain":
                plain_sum += ndcg
            else:
                pref_sum += ndcg
        n += 1

    plain_m = plain_sum / max(n, 1)
    pref_m = pref_sum / max(n, 1)
    delta = pref_m - plain_m
    log.info("=== QUERY-PREFIX A/B (%d queries, top_k=%d) ===", n, TOP_K)
    log.info("  plain    mean NDCG@10 = %.4f", plain_m)
    log.info("  prefixed mean NDCG@10 = %.4f", pref_m)
    log.info("  prefixed - plain = %+.4f", delta)
    log.info(
        "  DECISION (win_delta=%.3f): %s",
        WIN_DELTA,
        "RE_ADD_PREFIX" if delta >= WIN_DELTA else "KEEP_PREFIX_DROPPED",
    )


if __name__ == "__main__":
    main()
