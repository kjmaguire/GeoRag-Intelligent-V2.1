"""Persist the reusable reranker training artifacts to MinIO.

After Plan B v2 HOLD (§39), the MLM-adapted backbone at /tmp/reranker-mlm
is the only durable asset from the overnight run. It lives on ephemeral
tmpfs inside georag-fastapi — a container restart kills it. This script
preserves it (plus the Phase 1 extended-tokenizer base + vocab list) to
the new `reranker-checkpoints` MinIO bucket so the next reranker cycle
can pick up where this one left off.

Skips the failed FT + LoRA candidates — both verdicts are HOLD; the
adapters are kept on-disk for forensic spot-checks only, not worth
durable storage.

Usage
-----

    docker exec georag-fastapi \\
        python /app/scripts/_persist_reranker_checkpoints_to_minio.py
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("persist_reranker_checkpoints")


def _make_s3():
    import boto3  # noqa: PLC0415
    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("S3_ENDPOINT", "http://minio:8333"),
        aws_access_key_id=os.environ.get("S3_ACCESS_KEY", "georag-admin"),
        aws_secret_access_key=os.environ["S3_SECRET_KEY"],
        region_name=os.environ.get("S3_REGION", "us-east-1"),
    )


def _ensure_bucket(s3, bucket: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
        logger.info("bucket exists: %s", bucket)
    except Exception:  # noqa: BLE001
        s3.create_bucket(Bucket=bucket)
        logger.info("bucket created: %s", bucket)


def _upload_dir(s3, bucket: str, local_dir: Path, key_prefix: str) -> int:
    n = 0
    total_bytes = 0
    for p in sorted(local_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(local_dir).as_posix()
        key = f"{key_prefix.rstrip('/')}/{rel}"
        size = p.stat().st_size
        s3.upload_file(str(p), bucket, key)
        n += 1
        total_bytes += size
        logger.info("  %s (%.1f MB) -> s3://%s/%s",
                    rel, size / (1024 * 1024), bucket, key)
    logger.info("uploaded %d files (%.1f MB) under %s/",
                n, total_bytes / (1024 * 1024), key_prefix)
    return n


def _upload_file(s3, bucket: str, local: Path, key: str) -> None:
    if not local.is_file():
        logger.warning("skip missing: %s", local)
        return
    size = local.stat().st_size
    s3.upload_file(str(local), bucket, key)
    logger.info("  %s (%.1f KB) -> s3://%s/%s",
                local.name, size / 1024, bucket, key)


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser()
    p.add_argument("--bucket", default="reranker-checkpoints")
    p.add_argument("--run-id", default="2026-05-29-mlm-extended")
    args = p.parse_args()

    s3 = _make_s3()
    _ensure_bucket(s3, args.bucket)

    base_prefix = f"v1/run_id={args.run_id}"
    logger.info("uploading under s3://%s/%s/", args.bucket, base_prefix)

    # 1. Phase 1 extended-tokenizer backbone (smaller — useful for any
    #    alternative MLM scheme experiment).
    extended = Path("/tmp/reranker-extended")
    if extended.is_dir():
        logger.info("uploading phase1_extended_tokenizer ...")
        _upload_dir(s3, args.bucket, extended, f"{base_prefix}/phase1_extended_tokenizer")

    # 2. Phase 2 MLM-adapted backbone (THE asset).
    mlm = Path("/tmp/reranker-mlm")
    if mlm.is_dir():
        logger.info("uploading phase2_mlm_adapted ...")
        _upload_dir(s3, args.bucket, mlm, f"{base_prefix}/phase2_mlm_adapted")

    # 3. Vocab candidates TSV — needed to reproduce Phase 1.
    vocab = Path("/tmp/vocab_candidates.tsv")
    _upload_file(s3, args.bucket, vocab, f"{base_prefix}/vocab_candidates.tsv")

    # 4. Bench manifests for both HOLD verdicts (forensic reference).
    for src, dest in [
        (Path("/tmp/reranker-bench.json"),
         f"{base_prefix}/bench_phase3_full_ft_HOLD.json"),
        (Path("/tmp/reranker-lora-real-bench.json"),
         f"{base_prefix}/bench_lora_real_5q_inDist.json"),
        (Path("/tmp/reranker-lora-real-bench-large.json"),
         f"{base_prefix}/bench_lora_real_5143q_OOD_HOLD.json"),
    ]:
        _upload_file(s3, args.bucket, src, dest)

    # 5. Top-level manifest describing the whole package.
    manifest = {
        "run_id":          args.run_id,
        "captured_at_utc": "2026-05-29T20:00:00+00:00",
        "verdict":         "HOLD (both full FT and LoRA on real)",
        "reusable_asset":  f"{base_prefix}/phase2_mlm_adapted/",
        "asset_notes":     (
            "XLMRobertaForSequenceClassification backbone (vocab=250242), "
            "MLM-continued-pretrained for 2 epochs on 156,610 chunks from "
            "silver.document_passages (the 158k-passage TIER 0b enriched "
            "corpus). Reusable as --base-model for any future LoRA or full "
            "FT cycle once real query traffic accumulates. See "
            "OVERNIGHT_LOG.md §38-§39 for the full story."
        ),
        "do_not_promote":  [
            f"{base_prefix}/bench_phase3_full_ft_HOLD.json",
            f"{base_prefix}/bench_lora_real_5143q_OOD_HOLD.json",
        ],
        "phase1_artifacts": f"{base_prefix}/phase1_extended_tokenizer/",
        "phase0_vocab":     f"{base_prefix}/vocab_candidates.tsv",
    }
    body = json.dumps(manifest, indent=2).encode("utf-8")
    s3.put_object(Bucket=args.bucket, Key=f"{base_prefix}/MANIFEST.json", Body=body)
    logger.info("wrote MANIFEST.json")
    logger.info("DONE — durable at s3://%s/%s/", args.bucket, base_prefix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
