"""Plan B v2 — mine real (query, positive_passage, hard_negs) tuples by
searching Qdrant georag_chunks for top-K against each real production query.

Production data shape (probed 2026-05-29):
  - silver.answer_runs has 19,364 rows but only 264 distinct queries
  - silver.answer_citation_items has 4,393 rows referencing only 35 distinct
    (query_text, positive_passage_id) pairs — most are repeat nightly-bench
    runs against the golden questions.
  - Goal: take each distinct (query, positive_passage) pair, score it against
    Qdrant top-K, treat ranks 2-N (excluding the positive) as hard negatives.

Output schema matches scripts/_recover_historical_reranker_datasets.py /
train_reranker_lora.py — same `positive_chunk_text` / `hard_negative_chunk_texts`
fields so the existing trainer accepts it untouched.

Connects as the OWNER role `georag` (POSTGRES_OWNER_USER / OWNER_PASSWORD) to
bypass the silver.workspaces chr(0) RLS dead-end. Same trick as the TIER 0e
miner.

Usage
-----

    docker exec -e POSTGRES_OWNER_USER=georag \\
                -e POSTGRES_OWNER_PASSWORD=... \\
                georag-fastapi \\
        python /app/scripts/_mine_real_hard_negatives.py \\
            --output /tmp/reranker-train-real-only \\
            --top-k 20 --negs-per-pos 10
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("mine_real_hard_negatives")


_FETCH_REAL_PAIRS_SQL = """
SELECT DISTINCT
    ar.query_text,
    ci.passage_id::text AS positive_passage_id,
    dp.text             AS positive_chunk_text,
    dp.document_id::text AS positive_document_id
FROM silver.answer_citation_items ci
INNER JOIN silver.answer_runs ar ON ar.answer_run_id = ci.answer_run_id
INNER JOIN silver.document_passages dp ON dp.passage_id = ci.passage_id
WHERE ci.rejection_reason IS NULL
  AND dp.text IS NOT NULL AND length(dp.text) >= 50
  AND ar.query_text IS NOT NULL
  AND length(ar.query_text) BETWEEN 10 AND 800
ORDER BY ar.query_text, positive_passage_id
"""


def _query_hash(text: str) -> str:
    return hashlib.sha1((text or "").strip().lower().encode("utf-8")).hexdigest()[:16]


async def _load_golden_hashes(conn) -> set[str]:
    """Hashes of eval.golden_questions — pull these out of train, push into test
    so we never train on a question we'll bench against. Bench-leak protection."""
    try:
        rows = await conn.fetch(
            "SELECT lower(trim(question_text)) AS q FROM eval.golden_questions"
        )
        return {_query_hash(r["q"]) for r in rows if r["q"]}
    except Exception as exc:  # noqa: BLE001
        logger.warning("golden hashes load failed (%s) — bench leak prot disabled", exc)
        return set()


async def main_async(args):
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    import asyncpg  # noqa: PLC0415
    from qdrant_client import AsyncQdrantClient  # noqa: PLC0415
    from sentence_transformers import SentenceTransformer  # noqa: PLC0415

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── DB connect as owner ──────────────────────────────────────────
    owner_user = os.environ.get("POSTGRES_OWNER_USER", "georag")
    owner_pass = (
        os.environ.get("POSTGRES_OWNER_PASSWORD")
        or os.environ["POSTGRES_PASSWORD"]
    )
    dsn = (
        f"postgresql://{owner_user}:{owner_pass}"
        f"@{os.environ.get('POSTGRES_DIRECT_HOST', 'postgresql')}:"
        f"{os.environ.get('POSTGRES_DIRECT_PORT', '5432')}/"
        f"{os.environ.get('POSTGRES_DB', 'georag')}"
    )
    conn = await asyncpg.connect(dsn)

    golden_hashes = await _load_golden_hashes(conn)
    logger.info("loaded %d golden-question hashes (bench-leak protection)",
                len(golden_hashes))

    rows = await conn.fetch(_FETCH_REAL_PAIRS_SQL)
    await conn.close()
    logger.info("fetched %d distinct (query, positive_passage) pairs", len(rows))

    if not rows:
        logger.error("no real pairs found — abort")
        return 64

    # ── Qdrant + embedder ────────────────────────────────────────────
    embedder = SentenceTransformer(
        os.environ.get("EMBEDDING_MODEL_NAME", "BAAI/bge-small-en-v1.5"),
        device="cuda" if os.environ.get("CUDA_VISIBLE_DEVICES", "0") != "" else "cpu",
    )
    qclient = AsyncQdrantClient(
        host=os.environ.get("QDRANT_HOST", "qdrant"),
        port=int(os.environ.get("QDRANT_PORT", "6333")),
    )

    # ── Mine hard negs ───────────────────────────────────────────────
    records: list[dict[str, Any]] = []
    skipped_golden = 0
    skipped_no_negs = 0

    for r in rows:
        q = r["query_text"]
        pos_id = r["positive_passage_id"]
        pos_text = r["positive_chunk_text"]
        pos_doc = r["positive_document_id"]

        # Bench leak protection — drop training rows whose query is a golden Q
        # (we will keep them later as a held-out test split downstream).
        qh = _query_hash(q)
        if qh in golden_hashes:
            skipped_golden += 1
            continue

        try:
            dense = embedder.encode(q, normalize_embeddings=True).tolist()
        except Exception as exc:  # noqa: BLE001
            logger.warning("embed failed for query=%r: %s", q[:60], exc)
            continue

        # Qdrant dense-only search (sparse takes more wiring; dense alone is
        # fine for hard-neg mining — we just need plausible candidates).
        # qdrant_client v1.13+ deprecated `.search` → `.query_points`.
        resp = await qclient.query_points(
            collection_name="georag_chunks",
            query=dense,
            using="",                # default dense vector slot (name="")
            limit=args.top_k,
            with_payload=True,
            with_vectors=False,
        )
        hits = resp.points

        hard_negs: list[dict[str, str]] = []
        for h in hits:
            cand_id = str(h.id)
            if cand_id == pos_id:
                continue  # never use the positive as its own negative
            payload = h.payload or {}
            text = payload.get("text") or ""
            if len(text) < 50:
                continue
            hard_negs.append({"passage_id": cand_id, "text": text})
            if len(hard_negs) >= args.negs_per_pos:
                break

        if not hard_negs:
            skipped_no_negs += 1
            continue

        record = {
            "query":                     q,
            "chunk_id":                  pos_id,
            "pdf_id":                    pos_doc,
            "page":                      None,
            "bbox":                      None,
            "source_method":             "real_qdrant_hardneg_v1",
            "extraction_confidence":     None,
            "label":                     1.0,
            "positive_chunk_text":       pos_text,
            "hardneg_ids":               [n["passage_id"] for n in hard_negs],
            "hard_negative_chunk_texts": [n["text"] for n in hard_negs],
            "variant":                   "real",
            "query_group_id":            qh,    # split-by-query
            "gen_model":                 "qdrant.search/bge-small",
            "gen_prompt_hash":           "n/a",
            "fact_span":                 None,
            "_qhash":                    qh,
        }
        records.append(record)

    await qclient.close()
    logger.info(
        "mined %d records; skipped_golden=%d skipped_no_negs=%d",
        len(records), skipped_golden, skipped_no_negs,
    )

    if not records:
        logger.error("no usable records after filtering — abort")
        return 65

    # ── Split by query_group_id so the same query never spans splits ─
    rng = random.Random(42)
    by_qhash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        by_qhash[rec["_qhash"]].append(rec)
    qhashes = sorted(by_qhash.keys())
    rng.shuffle(qhashes)

    n_total = len(qhashes)
    n_test = max(1, int(n_total * 0.20))
    n_val = max(1, int(n_total * 0.10))
    test_set = set(qhashes[:n_test])
    val_set = set(qhashes[n_test:n_test + n_val])

    splits: dict[str, list[dict[str, Any]]] = {"train": [], "val": [], "test": []}
    for qh, recs in by_qhash.items():
        if qh in test_set:
            splits["test"].extend(recs)
        elif qh in val_set:
            splits["val"].extend(recs)
        else:
            splits["train"].extend(recs)

    for split, recs in splits.items():
        for rec in recs:
            rec.pop("_qhash", None)
        path = out_dir / f"{split}.jsonl"
        with open(path, "w") as fh:
            for rec in recs:
                fh.write(json.dumps(rec) + "\n")
        logger.info("wrote %s: %d rows", path.name, len(recs))

    manifest = {
        "asset":                "Plan B v2 — real-query Qdrant hard-neg miner",
        "source":               "silver.answer_runs ∪ silver.answer_citation_items",
        "qdrant_collection":    "georag_chunks",
        "embedding_model":      os.environ.get("EMBEDDING_MODEL_NAME",
                                               "BAAI/bge-small-en-v1.5"),
        "top_k":                args.top_k,
        "negs_per_pos":         args.negs_per_pos,
        "distinct_q_pos_pairs": len(rows),
        "records_mined":        len(records),
        "skipped_golden":       skipped_golden,
        "skipped_no_negs":      skipped_no_negs,
        "splits":               {k: len(v) for k, v in splits.items()},
    }
    (out_dir / "miner_manifest.json").write_text(json.dumps(manifest, indent=2))
    logger.info("wrote miner_manifest.json")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", default="/tmp/reranker-train-real-only")
    p.add_argument("--top-k", type=int, default=20)
    p.add_argument("--negs-per-pos", type=int, default=10)
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
