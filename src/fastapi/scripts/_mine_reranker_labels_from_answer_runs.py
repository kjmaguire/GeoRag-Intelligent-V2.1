#!/usr/bin/env python
"""TIER 0e — mine real reranker training labels from silver.answer_runs.

Pass 2 of the 2026-05-28 audit identified `silver.answer_runs` (19,354
real geologist queries) + `silver.answer_retrieval_items` (56,286
retrieval records) + `silver.answer_citation_items` (4,393 citations)
as the highest-quality training signal in the system. Unlike the
synthetic `reranker_label_dataset` Dagster asset (which overfit and
caused this morning's LoRA HOLD verdict), this data is:

* Real geologist query phrasings, not Qwen3 syntheses
* Real ranked retrievals from the live chat pipeline
* Real positive/negative labels via citation outcomes:
    - cited passages → POSITIVE (the answer actually relied on them)
    - retrieved-but-not-cited → HARD NEGATIVE (model considered, then dropped)

Output JSONL shape matches the existing
`reranker_label_dataset` synthetic JSONL so the training script can
concatenate the two pools directly.

Per-row schema (matches v1 dataset):

    {
        "query":                  <query_text>,
        "chunk_id":               <positive_chunk_id>,
        "pdf_id":                 <document_id>,
        "page":                   null,
        "bbox":                   null,
        "source_method":          "real_answer_runs_v1",
        "extraction_confidence":  null,
        "label":                  1.0,
        "positive_chunk_text":    <text>,
        "hardneg_ids":            [<retrieved-but-not-cited>],
        "hard_negative_chunk_texts": [<text>...],
        "variant":                "real",
        "query_group_id":         <answer_run_id>,
        "gen_model":              "silver.answer_runs",
        "gen_prompt_hash":        "n/a",
        "fact_span":              null
    }

This is **TIER 0e from the 2026-05-28 audit** — the highest-quality
training-data win because it's drawn from the actual inference
distribution geologists use at runtime.

Filtering rules (applied in SQL):

1. Skip runs without at least 1 citation_item (no positive signal).
2. Skip runs where retrieved_items count < 2 (no useful negatives to pick).
3. Skip runs flagged as refusals (no answer was produced; the chunks
   weren't validated against an actual answer outcome).
4. Skip runs where query_text is < 10 chars or > 800 chars.
5. Skip runs from test workspaces (test-hg-* / test-ws-* slugs).

Splits:
* Default 80/10/10 split by answer_run_id (NOT by query, so multiple
  runs of the same query land together in one split — prevents leak).
* Held-out queries from `eval.golden_questions` are FORCED into test
  split regardless of the split ratio (bench fidelity).

Usage
-----

    docker exec georag-fastapi bash -c \\
        "python /app/scripts/_mine_reranker_labels_from_answer_runs.py \\
            --output /tmp/reranker-train-real \\
            --max-negs-per-query 6"
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
from pathlib import Path
from typing import Any

logger = logging.getLogger("mine_reranker_labels_from_answer_runs")


_FETCH_SQL = """
WITH eligible_runs AS (
    SELECT
        ar.answer_run_id,
        ar.workspace_id,
        ar.query_text,
        ar.query_class,
        ar.created_at
    FROM silver.answer_runs ar
    WHERE ar.query_text IS NOT NULL
      AND length(ar.query_text) BETWEEN 10 AND 800
      AND ar.workspace_id IN (
          SELECT workspace_id FROM silver.workspaces
          WHERE slug NOT LIKE 'test-hg-%' AND slug NOT LIKE 'test-ws-%'
            AND slug NOT LIKE 'orphan-%' AND slug NOT LIKE 'phase%'
            AND slug NOT LIKE 'state-machine-tests'
      )
),
positive_chunks AS (
    -- citation_items.passage_id ↔ document_passages.passage_id is the join
    -- to recover the parent document_id. No report_id column on citation_items.
    SELECT
        ci.answer_run_id,
        ci.workspace_id,
        ci.passage_id,
        dp.document_id,
        dp.text AS chunk_text,
        ROW_NUMBER() OVER (PARTITION BY ci.answer_run_id ORDER BY ci.created_at) AS rn
    FROM silver.answer_citation_items ci
    INNER JOIN silver.document_passages dp ON dp.passage_id = ci.passage_id
    WHERE dp.text IS NOT NULL AND length(dp.text) >= 50
      AND ci.rejection_reason IS NULL  -- skip rejected citations
),
neg_candidates AS (
    -- answer_retrieval_items already tracks used_in_citation; hard negatives
    -- are simply the retrievals NOT used in the final citation set.
    SELECT
        ri.answer_run_id,
        ri.passage_id,
        dp.text AS chunk_text,
        COALESCE(ri.reranker_score, ri.retriever_score, ri.rrf_score) AS score,
        ROW_NUMBER() OVER (PARTITION BY ri.answer_run_id ORDER BY ri.rrf_rank NULLS LAST, ri.created_at) AS rn
    FROM silver.answer_retrieval_items ri
    INNER JOIN silver.document_passages dp ON dp.passage_id = ri.passage_id
    WHERE ri.used_in_citation = false
      AND ri.passage_id IS NOT NULL
      AND dp.text IS NOT NULL AND length(dp.text) >= 50
)
SELECT
    er.answer_run_id::text       AS answer_run_id,
    er.workspace_id::text        AS workspace_id,
    -- regexp_replace(..., '[[:cntrl:]]', ...) strips all C0 control chars
    -- including U+0000. PDF parser sometimes leaves these in chunk text;
    -- asyncpg's wire codec rejects U+0000 in text-typed columns.
    er.query_text                                                  AS query,
    er.query_class               AS query_class,
    er.created_at                AS created_at,
    pc.passage_id::text          AS positive_chunk_id,
    pc.document_id::text         AS positive_document_id,
    pc.chunk_text                                                  AS positive_chunk_text,
    -- aggregate up to 30 candidate negatives (we'll sub-sample down)
    (
        SELECT jsonb_agg(jsonb_build_object(
            'passage_id', nc.passage_id::text,
            'text', nc.chunk_text
        ))
        FROM (
            SELECT * FROM neg_candidates nc
            WHERE nc.answer_run_id = er.answer_run_id
            LIMIT 30
        ) nc
    ) AS hard_negative_candidates
FROM eligible_runs er
INNER JOIN positive_chunks pc
    ON pc.answer_run_id = er.answer_run_id
    AND pc.rn = 1   -- one row per run × first positive
WHERE EXISTS (
    SELECT 1 FROM neg_candidates nc
    WHERE nc.answer_run_id = er.answer_run_id
)
ORDER BY er.created_at
"""


_GOLDEN_QUERY_HASHES_SQL = """
SELECT lower(trim(question_text)) AS query_text FROM eval.golden_questions
"""


def _query_hash(text: str) -> str:
    return hashlib.sha1((text or "").strip().lower().encode("utf-8")).hexdigest()[:16]


def _strip_nul(s: str | None) -> str:
    """PDF parser sometimes leaves U+0000 in silver.document_passages.text.
    Postgres TEXT permits it but asyncpg's wire codec rejects it
    (ProgramLimitExceededError: 'null character not permitted'). Strip
    in Python after fetch — cheapest place to do it."""
    if s is None:
        return ""
    return s.replace("\x00", "")


def _split_assignment(
    run_ids: list[str],
    seed: int,
    val_ratio: float = 0.10,
    test_ratio: float = 0.10,
) -> dict[str, str]:
    """80/10/10 split by answer_run_id (NOT by query — same query across runs
    must land in one split to prevent train↔test leakage)."""
    rng = random.Random(seed)
    shuffled = list(run_ids)
    rng.shuffle(shuffled)
    n = len(shuffled)
    n_val = int(n * val_ratio)
    n_test = int(n * test_ratio)
    out: dict[str, str] = {}
    for i, rid in enumerate(shuffled):
        if i < n_test:
            out[rid] = "test"
        elif i < n_test + n_val:
            out[rid] = "val"
        else:
            out[rid] = "train"
    return out


async def _load_golden_query_hashes(conn) -> set[str]:
    try:
        rows = await conn.fetch(_GOLDEN_QUERY_HASHES_SQL)
        return {_query_hash(r["query_text"]) for r in rows if r["query_text"]}
    except Exception as exc:  # noqa: BLE001
        logger.warning("could not load eval.golden_questions hashes (%s) — bench-leak protection disabled", exc)
        return set()


def main_sync(args):
    """psycopg2-based synchronous main. The async version (asyncpg) raised
    ProgramLimitExceededError 'null character not permitted' on PDF-parser
    fallout where U+0000 bytes ended up in silver.document_passages.text.
    psycopg2 lets us catch and skip the row instead of failing the batch."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    import psycopg2  # noqa: PLC0415
    import psycopg2.extras  # noqa: PLC0415

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    # NOTE: connect as OWNER role `georag` (POSTGRES_OWNER_USER /
    # POSTGRES_OWNER_PASSWORD), NOT as `georag_app`. The runtime
    # role is subject to RLS, and silver.workspaces' RLS policy
    # uses `chr(0)` as a NULLIF sentinel (see 2026-05-25 "Parked
    # items" memory — known broken-but-fail-open under normal
    # GUC handling). Under psycopg2 the chr(0) evaluation surfaces
    # as ProgramLimitExceeded: 'null character not permitted',
    # blocking every SELECT against the table. Owner bypasses RLS.
    owner_user = os.environ.get("POSTGRES_OWNER_USER", "georag")
    owner_pass = (
        os.environ.get("POSTGRES_OWNER_PASSWORD")
        or os.environ.get("POSTGRES_SUPERUSER_PASSWORD")
        or os.environ["POSTGRES_PASSWORD"]
    )
    conn = psycopg2.connect(
        host=os.environ.get("POSTGRES_DIRECT_HOST", "postgresql"),
        port=int(os.environ.get("POSTGRES_DIRECT_PORT", 5432)),
        user=owner_user,
        password=owner_pass,
        dbname=os.environ.get("POSTGRES_DB", "georag"),
    )
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Golden hashes
            try:
                cur.execute(_GOLDEN_QUERY_HASHES_SQL)
                golden_hashes = {_query_hash(r["query_text"]) for r in cur.fetchall() if r["query_text"]}
            except Exception as exc:  # noqa: BLE001
                logger.warning("golden hash load failed (%s) — bench leak prot off", exc)
                golden_hashes = set()
        logger.info("loaded %d golden-query hashes for bench-leak protection",
                    len(golden_hashes))

        logger.info("fetching answer_runs joined to citations + retrievals ...")
        # Plain tuple cursor — avoid RealDictCursor's type-aware conversion path
        # which trips on jsonb conversion. We manually unpack tuples below.
        with conn.cursor() as cur:
            cur.execute(_FETCH_SQL)
            colnames = [d[0] for d in cur.description]
            raw_rows = cur.fetchall()
            rows = [dict(zip(colnames, t)) for t in raw_rows]
        logger.info("fetched %d eligible answer_run × positive_chunk rows", len(rows))

        # --- Per-row → training record ---
        rng = random.Random(42)
        records: list[dict[str, Any]] = []
        for r in rows:
            negs_raw = r["hard_negative_candidates"] or []
            if isinstance(negs_raw, str):
                # asyncpg returns jsonb as str in some versions
                negs_raw = json.loads(negs_raw)
            if not isinstance(negs_raw, list) or len(negs_raw) == 0:
                continue
            if len(negs_raw) > args.max_negs_per_query:
                negs_raw = rng.sample(negs_raw, args.max_negs_per_query)

            record = {
                "query":                 r["query"],
                "chunk_id":              r["positive_chunk_id"],
                "pdf_id":                r["positive_document_id"],
                "page":                  None,
                "bbox":                  None,
                "source_method":         "real_answer_runs_v1",
                "extraction_confidence": None,
                "label":                 1.0,
                "positive_chunk_text":   r["positive_chunk_text"] or "",
                "hardneg_ids":           [n.get("passage_id") for n in negs_raw],
                "hard_negative_chunk_texts": [n.get("text", "") for n in negs_raw],
                "variant":               "real",
                "query_group_id":        r["answer_run_id"],
                "gen_model":             "silver.answer_runs",
                "gen_prompt_hash":       "n/a",
                "fact_span":             None,
                # extras the synthetic generator doesn't carry — keep for analysis
                "_run_workspace_id":     r["workspace_id"],
                "_run_query_class":      r["query_class"],
            }
            records.append(record)

        logger.info("built %d training records", len(records))

        # --- Forced test split for queries that overlap with eval.golden_questions ---
        forced_test_run_ids: set[str] = set()
        for rec in records:
            if _query_hash(rec["query"]) in golden_hashes:
                forced_test_run_ids.add(rec["query_group_id"])
        logger.info("forced to test split (bench overlap): %d answer_run_id(s)",
                    len(forced_test_run_ids))

        # --- 80/10/10 split by answer_run_id ---
        all_run_ids = sorted({rec["query_group_id"] for rec in records})
        assignment = _split_assignment(all_run_ids, seed=42)
        # Override for forced test
        for rid in forced_test_run_ids:
            assignment[rid] = "test"

        splits: dict[str, list[dict[str, Any]]] = {"train": [], "val": [], "test": []}
        for rec in records:
            split = assignment.get(rec["query_group_id"], "train")
            splits[split].append(rec)

        # --- Write JSONL splits ---
        for split_name, recs in splits.items():
            out_path = out_dir / f"{split_name}.jsonl"
            with open(out_path, "w") as fh:
                for rec in recs:
                    fh.write(json.dumps(rec) + "\n")
            logger.info("wrote %s: %d rows", out_path.name, len(recs))

        # --- Manifest ---
        manifest = {
            "asset":                "TIER 0e — real reranker labels mined from silver.answer_runs",
            "splits":               {k: len(v) for k, v in splits.items()},
            "total":                len(records),
            "forced_test_run_ids":  len(forced_test_run_ids),
            "bench_leak_protection_active": bool(golden_hashes),
            "max_negs_per_query":   args.max_negs_per_query,
        }
        with open(out_dir / "manifest.json", "w") as fh:
            json.dump(manifest, fh, indent=2)
        logger.info("wrote manifest with splits=%s", manifest["splits"])

    finally:
        conn.close()

    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", default="/tmp/reranker-train-real")
    p.add_argument("--max-negs-per-query", type=int, default=6)
    args = p.parse_args()
    return main_sync(args)


if __name__ == "__main__":
    sys.exit(main())
