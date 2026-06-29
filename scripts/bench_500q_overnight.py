"""
Overnight 500-question NDCG benchmark.

Waits for all 7 NI-43-101 projects to finish ingesting + embedding,
then runs every question in the CSV against Qdrant, scores using
expected_keywords, and saves a full report to /app/bench_results/.

Run:  python3 /tmp/bench_500q_overnight.py
Logs: /tmp/bench_500q.log
"""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/app")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/tmp/bench_500q.log"),
    ],
)
log = logging.getLogger("bench_500q")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CSV_PATH      = "/tmp/georag_questions_500.csv"
COLLECTION    = "georag_chunks"
TOP_K         = 10
POLL_INTERVAL = 900   # 15 min between ingestion checks

# Projects that must have passages before we start.
# These are the DB project_names that match the 7 CSV projects.
REQUIRED_PROJECTS = {
    "Shakespeare",
    "Battle North",
    "Ikkari",
    "Madsen",
    "WEST RED LAKE GOLD MINES  LTD",
}

# ---------------------------------------------------------------------------
# Wait for ingestion to complete
# ---------------------------------------------------------------------------

async def _all_ready(conn) -> bool:
    rows = await conn.fetch(
        """
        SELECT p.project_name,
               COUNT(dp.passage_id)   AS passages,
               COUNT(dp.embedding_id) AS embedded
        FROM   silver.projects p
        LEFT JOIN silver.reports r ON r.project_id = p.project_id
        LEFT JOIN silver.document_passages dp ON dp.document_id = r.report_id
        WHERE  p.project_name = ANY($1::text[])
        GROUP BY p.project_name
        """,
        list(REQUIRED_PROJECTS),
    )

    status = {r["project_name"]: (r["passages"], r["embedded"]) for r in rows}
    pending = await conn.fetchval(
        "SELECT COUNT(*) FROM silver.document_passages WHERE embedding_id IS NULL"
    )

    log.info("Ingestion status — pending_embed=%d", pending)
    for proj in sorted(REQUIRED_PROJECTS):
        p, e = status.get(proj, (0, 0))
        flag = "✓" if p > 0 and e == p else "…"
        log.info("  %s  %s  passages=%d  embedded=%d", flag, proj, p, e)

    all_have = all(status.get(p, (0, 0))[0] > 0 for p in REQUIRED_PROJECTS)
    all_embedded = pending == 0
    return all_have and all_embedded


async def wait_for_ingestion() -> None:
    import asyncpg
    dsn = (
        f"postgres://{os.environ['POSTGRES_USER']}:"
        f"{os.environ['POSTGRES_PASSWORD']}@postgresql:5432/georag"
    )
    while True:
        conn = await asyncpg.connect(dsn, statement_cache_size=0)
        try:
            ready = await _all_ready(conn)
        finally:
            await conn.close()

        if ready:
            log.info("All projects indexed and embedded — starting benchmark.")
            return

        log.info("Not ready yet. Sleeping %d minutes…", POLL_INTERVAL // 60)
        await asyncio.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Load passage texts from DB (for keyword scoring)
# ---------------------------------------------------------------------------

async def load_passage_texts() -> dict[str, str]:
    """Return {passage_id: text} for every embedded passage."""
    import asyncpg
    dsn = (
        f"postgres://{os.environ['POSTGRES_USER']}:"
        f"{os.environ['POSTGRES_PASSWORD']}@postgresql:5432/georag"
    )
    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        rows = await conn.fetch(
            """
            SELECT passage_id::text,
                   COALESCE(contextualized_content, text) AS content
            FROM   silver.document_passages
            WHERE  embedding_id IS NOT NULL
            """
        )
        log.info("Loaded %d passage texts from DB.", len(rows))
        return {r["passage_id"]: r["content"] for r in rows}
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# NDCG scorer
# ---------------------------------------------------------------------------

def _score_passage(text: str, keywords: list[str]) -> int:
    """Grade 2 = all keywords present, 1 = any keyword, 0 = none."""
    tl = text.lower()
    hits = sum(1 for kw in keywords if kw in tl)
    if hits == len(keywords):
        return 2
    if hits > 0:
        return 1
    return 0


def _ndcg(scores: list[int], k: int = 10) -> float:
    scores = scores[:k]
    dcg  = sum(s / math.log2(i + 2) for i, s in enumerate(scores))
    ideal = sorted(scores, reverse=True)
    idcg = sum(s / math.log2(i + 2) for i, s in enumerate(ideal))
    return dcg / idcg if idcg > 0 else 0.0


# ---------------------------------------------------------------------------
# Main benchmark
# ---------------------------------------------------------------------------

async def run_benchmark(passage_texts: dict[str, str]) -> dict:
    from sentence_transformers import SentenceTransformer
    from qdrant_client import AsyncQdrantClient

    log.info("Loading bge-small model…")
    model = SentenceTransformer("/app/models/bge-small-domain-ft", device="cuda")
    log.info("Model loaded.")

    qdrant = AsyncQdrantClient(host="qdrant", port=6333)
    info = await qdrant.get_collection(COLLECTION)
    log.info("Qdrant: %d points in %s", info.points_count, COLLECTION)

    with open(CSV_PATH, encoding="utf-8-sig") as f:
        questions = list(csv.DictReader(f))
    log.info("Questions: %d", len(questions))

    per_query   = []
    per_project: dict[str, list[float]] = {}

    for i, q in enumerate(questions):
        question = q["question"]
        project  = q["project"]
        keywords = [k.strip().lower() for k in q["expected_keywords"].split(";") if k.strip()]

        t0  = time.time()
        vec = model.encode(question, normalize_embeddings=True)
        result = await qdrant.query_points(
            collection_name=COLLECTION,
            query=vec.tolist(),
            limit=TOP_K,
            with_payload=True,
        )
        latency_ms = int((time.time() - t0) * 1000)

        scores = []
        for hit in result.points:
            pid  = hit.payload.get("passage_id", "") if hit.payload else ""
            text = passage_texts.get(pid, "")
            scores.append(_score_passage(text, keywords))

        ndcg = _ndcg(scores)
        per_query.append({
            "question":    question,
            "project":     project,
            "ndcg_at_10":  round(ndcg, 4),
            "any_hit":     any(s > 0 for s in scores),
            "latency_ms":  latency_ms,
            "keywords":    keywords,
        })
        per_project.setdefault(project, []).append(ndcg)

        if (i + 1) % 50 == 0:
            running_mean = sum(r["ndcg_at_10"] for r in per_query) / len(per_query)
            log.info("  %d/%d  running_mean=%.4f", i + 1, len(questions), running_mean)

    await qdrant.close()

    mean_ndcg    = sum(r["ndcg_at_10"] for r in per_query) / len(per_query)
    zero_hit     = sum(1 for r in per_query if not r["any_hit"])
    project_ndcg = {p: round(sum(v) / len(v), 4) for p, v in per_project.items()}

    return {
        "meta": {
            "label":        "500q-overnight",
            "timestamp":    datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            "questions_run": len(per_query),
            "top_k":        TOP_K,
            "collection_points": info.points_count,
        },
        "summary": {
            "mean_ndcg_at_10":   round(mean_ndcg, 4),
            "zero_hit_questions": zero_hit,
            "zero_hit_pct":      round(zero_hit / len(per_query) * 100, 1),
        },
        "per_project": project_ndcg,
        "per_query":   per_query,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    log.info("=== bench_500q_overnight starting ===")
    log.info("Waiting for all projects to finish ingesting…")

    await wait_for_ingestion()

    log.info("Loading passage texts for keyword scoring…")
    passage_texts = await load_passage_texts()

    log.info("Running benchmark…")
    report = await run_benchmark(passage_texts)

    ts    = report["meta"]["timestamp"]
    out   = Path(f"/app/bench_results/{ts}_500q-overnight.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))

    log.info("=== RESULTS ===")
    log.info("Mean NDCG@10 : %.4f", report["summary"]["mean_ndcg_at_10"])
    log.info("Zero-hit     : %d / %d  (%.1f%%)",
             report["summary"]["zero_hit_questions"],
             report["meta"]["questions_run"],
             report["summary"]["zero_hit_pct"])
    log.info("Per-project NDCG:")
    for proj, score in sorted(report["per_project"].items(), key=lambda x: -x[1]):
        log.info("  %-40s  %.4f", proj, score)
    log.info("Report saved: %s", out)
    log.info("=== bench_500q_overnight DONE ===")


if __name__ == "__main__":
    asyncio.run(main())
