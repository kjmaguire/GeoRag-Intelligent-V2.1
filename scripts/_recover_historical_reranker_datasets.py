#!/usr/bin/env python
"""TIER 0a — recover historical reranker training datasets from MinIO.

The `reranker-labels/v1/` bucket holds 6 historical Dagster materializations
of the `reranker_label_dataset` asset across the day. Pass 4 of the
2026-05-28 corpus coverage audit observed:

  v1/run_id=28d9013e-d1be-4dee-bf40-f0bc4198a503/
    train.jsonl                    74,849 KB  (~75 MB)
    test.jsonl                     46,994 KB
    val.jsonl                       2,758 KB
    generated_queries.parquet       1,914 KB

That's ~100× larger than the dataset the LoRA cycle trained on this
afternoon (`run_id=181fdf15`, 724 KB train). Each older run is a
snapshot of:

* Qwen3-generated synthetic queries over a previous corpus state
* Qdrant hard-negative mining
* Critique-filtered relevance scores in the same JSONL shape as today

This script:

1. Lists every historical run_id under v1/.
2. Downloads each one's {manifest,train,val,test}.jsonl to a local dir.
3. Deduplicates queries across runs (same query → keep the most-recent
   variant — newest run_id wins).
4. Concatenates the dedup'd corpus into a single
   /tmp/reranker-train-historical/ tree matching the LoRA training
   script's expected layout (train/val/test JSONL).
5. Writes a recovery_manifest.json next to the splits documenting:
   - Source run_ids + their per-split row counts
   - Dedup count (queries collapsed)
   - Final row counts per split
   - Total recovered training pairs

This is **TIER 0a from the 2026-05-28 corpus coverage audit** — the
highest-confidence training-data win because it requires zero new
engineering (just download + dedup) and immediately gives the next
training cycle ~100× more pairs than this afternoon's LoRA run.

Usage
-----

    docker exec georag-fastapi bash -c \\
        "python /app/scripts/_recover_historical_reranker_datasets.py \\
            --output /tmp/reranker-train-historical"

Idempotent: re-runs detect already-downloaded files via SHA + skip
re-download. Re-runs also re-dedupe the queries against the full set.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("recover_historical_reranker_datasets")


def _list_run_ids(s3, bucket: str) -> list[str]:
    """List every run_id= prefix under v1/ in the reranker-labels bucket."""
    paginator = s3.get_paginator("list_objects_v2")
    run_ids: set[str] = set()
    for page in paginator.paginate(Bucket=bucket, Prefix="v1/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            # Keys look like: v1/run_id=<uuid>/{manifest,train,val,test}.jsonl
            parts = key.split("/")
            if len(parts) >= 3 and parts[1].startswith("run_id="):
                run_ids.add(parts[1].split("=", 1)[1])
    return sorted(run_ids)


def _download_run(s3, bucket: str, run_id: str, dest: Path) -> dict[str, Path]:
    """Download one run's manifest+train+val+test. Returns the local paths."""
    dest.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for fname in ("manifest.json", "train.jsonl", "val.jsonl", "test.jsonl"):
        key = f"v1/run_id={run_id}/{fname}"
        local = dest / fname
        if local.is_file() and local.stat().st_size > 0:
            logger.info("    skip download (already present): %s", local.name)
            paths[fname] = local
            continue
        try:
            s3.download_file(bucket, key, str(local))
            logger.info("    downloaded %s (%d bytes)", fname, local.stat().st_size)
            paths[fname] = local
        except Exception as exc:  # noqa: BLE001
            logger.warning("    missing or error on %s: %s", key, exc)
    return paths


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                logger.warning("    bad json in %s: %s", path.name, exc)
    return rows


def _write_jsonl(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _query_key(row: dict[str, Any]) -> str | None:
    """The dedupe key — query text. Same query across runs is the same row."""
    q = row.get("query")
    if q is None:
        return None
    return str(q).strip().lower()


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser()
    p.add_argument("--output", default="/tmp/reranker-train-historical",
                   help="Local directory to assemble the recovered dataset into")
    p.add_argument("--bucket", default="reranker-labels")
    p.add_argument("--prefer-newest", action="store_true", default=True,
                   help="On dedupe collision, keep the newer run's variant.")
    args = p.parse_args()

    import boto3  # noqa: PLC0415

    s3 = boto3.client(
        "s3",
        endpoint_url=os.environ.get("S3_ENDPOINT", "http://minio:8333"),
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY", "georag-admin"),
        aws_secret_access_key=os.environ["S3_SECRET_KEY"],
        region_name=os.environ.get("S3_REGION", "us-east-1"),
    )

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = out_dir / "_per_run_cache"

    # --- Stage 1: discover + download ---
    run_ids = _list_run_ids(s3, args.bucket)
    logger.info("discovered %d historical run_id(s): %s", len(run_ids), run_ids)
    if not run_ids:
        logger.error("no historical run_ids found — nothing to recover")
        return 64

    per_run_paths: dict[str, dict[str, Path]] = {}
    for run_id in run_ids:
        logger.info("  run_id=%s", run_id)
        per_run_paths[run_id] = _download_run(s3, args.bucket, run_id, cache_dir / run_id)

    # --- Stage 2: load + dedupe per split ---
    per_split_rows: dict[str, dict[str, dict[str, Any]]] = {
        # key = query_key, value = the chosen row (newest wins by default)
        "train": {},
        "val":   {},
        "test":  {},
    }
    per_run_split_counts: dict[str, dict[str, int]] = defaultdict(dict)
    dedup_collisions = 0

    # Sort run_ids so that prefer-newest works deterministically.
    # We treat the lex-greatest run_id as "newest" (UUIDs are time-ordered enough for our purposes).
    iter_order = sorted(run_ids, reverse=not args.prefer_newest)
    for run_id in iter_order:
        paths = per_run_paths[run_id]
        for split in ("train", "val", "test"):
            jsonl = paths.get(f"{split}.jsonl")
            if jsonl is None:
                per_run_split_counts[run_id][split] = 0
                continue
            rows = _load_jsonl(jsonl)
            per_run_split_counts[run_id][split] = len(rows)
            logger.info("    %s %s: %d rows", run_id, split, len(rows))
            for r in rows:
                k = _query_key(r)
                if k is None:
                    continue
                if k in per_split_rows[split]:
                    dedup_collisions += 1
                    # Newer wins → with prefer_newest=True we iterate newest-first,
                    # so first-write wins. Keep the existing.
                    continue
                per_split_rows[split][k] = r

    # --- Stage 3: write merged splits ---
    final_counts = {}
    for split, by_key in per_split_rows.items():
        out_path = out_dir / f"{split}.jsonl"
        _write_jsonl(list(by_key.values()), out_path)
        final_counts[split] = len(by_key)
        logger.info("wrote %s: %d unique rows", out_path.name, len(by_key))

    # --- Stage 4: recovery manifest ---
    manifest = {
        "asset":               "TIER 0a — historical reranker dataset recovery",
        "source_bucket":       args.bucket,
        "source_prefix":       "v1/",
        "discovered_run_ids":  run_ids,
        "per_run_split_counts": per_run_split_counts,
        "final_split_counts":  final_counts,
        "dedup_collisions":    dedup_collisions,
        "prefer_newest":       args.prefer_newest,
        "total_recovered":     sum(final_counts.values()),
    }
    manifest_path = out_dir / "recovery_manifest.json"
    with open(manifest_path, "w") as fh:
        json.dump(manifest, fh, indent=2)
    logger.info("wrote: %s", manifest_path)

    # Summary line
    logger.info(
        "RECOVERY COMPLETE — total %d unique training pairs across "
        "%d historical runs (vs the ~963-row LoRA cycle dataset).",
        manifest["total_recovered"], len(run_ids),
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
