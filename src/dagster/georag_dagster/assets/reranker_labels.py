"""Reranker label-dataset Dagster asset graph.

Generates synthetic (query, positive_chunk, hard_negatives) labelled
triples for in-place fine-tuning of ``BAAI/bge-reranker-base``. The
graph is five assets in the ``reranker_labels`` group:

  1. ``reranker_chunk_population``  — candidate chunks + class labels
  2. ``reranker_chunk_sample``      — stratified ~50k sample
  3. ``reranker_generated_queries`` — vLLM-generated literal + paraphrase
  4. ``reranker_mined_negatives``   — Qdrant top-50 hard-negative mining
  5. ``reranker_label_dataset``     — self-critique filter + JSONL splits

Output lives under ``s3://reranker-labels/v1/run_id=<dagster_run_id>/``
in the SeaweedFS deployment. The bucket is created lazily on first
materialisation via ``S3Resource.ensure_bucket``. The dataset is split
by ``report_id`` 80/10/10 to prevent train→val/test leakage — Kyle
explicitly approved this split key over per-triple shuffling.

Multi-hop query generation (2026-05-23): in addition to literal +
paraphrase variants per chunk, the asset now emits one multi-hop
query per pair of chunks from the same ``report_id`` (capped at one
pair per report to keep the ratio comparable to literal/paraphrase).
Multi-hop rows share a ``query_group_id`` UUID so downstream training
can group both bridge chunks as positives for the same query.

NOTE: Do NOT add ``from __future__ import annotations`` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and
that import breaks runtime annotation evaluation.
"""

import io
import json
import random
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import polars as pl
import psycopg2.extras
from dagster import (
    AssetCheckExecutionContext,
    AssetCheckResult,
    AssetCheckSeverity,
    AssetExecutionContext,
    AssetIn,
    MaterializeResult,
    MetadataValue,
    Output,
    asset,
    asset_check,
)

from georag_dagster.assets.index_document_passages import index_document_passages
from georag_dagster.assets.reranker_labels_helpers import (
    CHECK_MAX_LEAKAGE_WARN_RATE,
    CHECK_MIN_TRIPLES,
    CRITIQUE_MIN_SCORE,
    DOC_CLASSES,
    LEAKAGE_THRESHOLD,
    MIN_CHUNK_CHARS,
    MULTI_HOP_RATIO_TARGET,
    PROMPT_VERSION,
    SOURCE_BUCKETS,
    TARGET_SAMPLE_SIZE,
    compute_doc_class as _compute_doc_class,
    deterministic_chunk_id as _deterministic_chunk_id,
    leakage_ratio as _leakage_ratio,
    prompt_sha256 as _prompt_sha256,
    seed_from_run_id as _seed_from_run_id,
    sqrt_proportional_allocation as _sqrt_proportional_allocation,
    strata_key as _strata_key,
    train_val_test_split_by_report as _train_val_test_split_by_report,
)
from georag_dagster.assets.silver_reports import silver_reports
from georag_dagster.clients.vllm_openai import (
    VllmJsonRequest,
    VllmOpenAIClient,
)
from georag_dagster.resources import (
    PostgresResource,
    QdrantResource,
    S3Resource,
    VllmResource,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ASSET_GROUP = "reranker_labels"

S3_BUCKET = "reranker-labels"
S3_PREFIX_VERSION = "v1"

# ADR-0010 Session B — hard-negative mining now queries georag_chunks
# (the canonical retrieval index per ADR-0010) instead of the legacy
# georag_reports collection. The chunk_id we emit per row IS the Qdrant
# point_id (passage_id) so re-runs land in the same upserted point.
QDRANT_COLLECTION = "georag_chunks"

# Source-method buckets — mirrors the silver.ingest_* enum values
# established in database/migrations/2026_05_12_180005..7. Each ingest
# table contributes one row per (report_id, page, region), so the bucket
# label is derived from the row's table_name + source_method.
SOURCE_METHOD_BUCKETS: dict[str, str] = {
    # silver.ingest_extractions (text-bucket)
    "pdfminer_six":                                "text",
    "pdfplumber_text":                             "text",
    "docling_text_region":                         "text",
    # silver.ingest_extractions (table-extract bucket)
    "pdfplumber_table_cell":                       "table-extract",
    "docling_table_cell":                          "table-extract",
    # silver.ingest_ocr_results — every row is OCR by definition
    "paddleocr_pp_ocrv5":                          "ocr",
    "paddleocr_pp_ocrv5_retry_binarized":          "ocr",
    "paddleocr_pp_ocrv5_retry_lang_hint":          "ocr",
    "paddleocr_pp_structure_v3_table_cell":        "ocr",
}

# Pinned at the byte level. Do NOT reformat — newlines and whitespace
# are part of the contract because gen_prompt_hash is computed over the
# rendered system+user pair.
LITERAL_PROMPT_SYSTEM = (
    "You are a geological information retrieval expert. "
    "Generate a single short, literal question that the given chunk of "
    "geological text answers directly. The question must be answerable "
    "purely from the chunk without external knowledge. Reply with a "
    "JSON object: {\"query\": \"...\", \"fact_span\": \"...\"} where "
    "fact_span is a verbatim substring of the chunk that answers the "
    "question."
)

PARAPHRASE_PROMPT_SYSTEM = (
    "You are a geological information retrieval expert. "
    "Generate a single natural-language question that paraphrases what "
    "a geologist would actually ask. The question must still be "
    "answerable from the chunk. Use synonyms, geological jargon, or a "
    "different sentence structure than the chunk itself. Reply with a "
    "JSON object: {\"query\": \"...\", \"fact_span\": \"...\"} where "
    "fact_span is a verbatim substring of the chunk."
)

# 2026-05-23 multi-hop addition. Both chunks come from the same report
# so the bridging entity (deposit, formation, hole_id, age, etc.) is
# plausibly present in both. The generated question must REQUIRE both
# chunks to answer — a single-chunk answer is a failure.
MULTI_HOP_PROMPT_SYSTEM = (
    "You are a geological information retrieval expert. Given TWO chunks "
    "from the same geological report, generate a single question that "
    "requires BOTH chunks to answer fully. Neither chunk alone may be "
    "sufficient. Common bridge patterns: connecting an assay grade in "
    "one chunk to a structural / lithological context in another; "
    "connecting a drill-hole ID to a resource estimate; connecting a "
    "formation name to its stratigraphic age. Reply with a JSON object: "
    "{\"query\": \"...\", \"bridge\": \"<entity / concept that links the chunks>\"}."
)

CRITIQUE_PROMPT_SYSTEM = (
    "You are a strict geological retrieval relevance grader. Score how "
    "well the chunk answers the question on a 0-3 scale: "
    "0=irrelevant, 1=tangentially related, 2=partially answers, "
    "3=fully answers. Reply with a JSON object: "
    "{\"score\": <int 0-3>, \"reason\": \"...\"}."
)

USER_PROMPT_QUERY_TEMPLATE = "Chunk:\n---\n{chunk_text}\n---"
USER_PROMPT_MULTIHOP_TEMPLATE = (
    "Chunk A:\n---\n{chunk_a_text}\n---\n\n"
    "Chunk B:\n---\n{chunk_b_text}\n---"
)
USER_PROMPT_CRITIQUE_TEMPLATE = (
    "Question: {query}\n\nChunk:\n---\n{chunk_text}\n---"
)

# Hard-negative mining bands — sampled from Qdrant top-50.
NEG_RANK_BANDS = [(2, 5), (10, 20), (40, 50)]
NEG_PER_QUERY_MIN = 4
NEG_PER_QUERY_MAX = 6


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_output_prefix(run_id: str) -> str:
    return f"{S3_PREFIX_VERSION}/run_id={run_id}"


# ---------------------------------------------------------------------------
# Doc-class heuristic
# ---------------------------------------------------------------------------

_FETCH_DOC_CLASS_INPUT_SQL = """
SELECT
    r.report_id::text AS report_id,
    COALESCE(r.title, '')              AS title,
    EXISTS (
        SELECT 1
        FROM silver.collars c
        JOIN silver.drill_traces dt ON dt.collar_id = c.collar_id
        WHERE c.report_id = r.report_id
    ) AS has_drill_traces,
    EXISTS (
        SELECT 1
        FROM silver.collars c
        JOIN silver.samples s ON s.collar_id = c.collar_id
        WHERE c.report_id = r.report_id
    ) AS has_samples
FROM silver.reports r
WHERE r.report_id = ANY(%(report_ids)s::uuid[])
;
"""

# NOTE: ``silver.collars.report_id`` may not exist on every deployment —
# the column was added late. The heuristic falls back to title-only
# classification when the join fails, which is the realistic v1 state.
_FETCH_DOC_CLASS_TITLE_ONLY_SQL = """
SELECT
    r.report_id::text AS report_id,
    COALESCE(r.title, '') AS title
FROM silver.reports r
WHERE r.report_id = ANY(%(report_ids)s::uuid[])
;
"""


def _fetch_doc_class_map(
    postgres: PostgresResource,
    report_ids: list[str],
) -> dict[str, str]:
    """Return a {report_id: doc_class} mapping. Falls back on title-only
    when silver.collars lacks a report_id column."""
    if not report_ids:
        return {}

    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                cur.execute(
                    _FETCH_DOC_CLASS_INPUT_SQL,
                    {"report_ids": report_ids},
                )
                rows = cur.fetchall()
                return {
                    r["report_id"]: _compute_doc_class(
                        r["title"],
                        bool(r.get("has_drill_traces", False)),
                        bool(r.get("has_samples", False)),
                    )
                    for r in rows
                }
            except psycopg2.errors.UndefinedColumn:
                # Fallback for deployments where silver.collars.report_id
                # has not yet been added. We rollback and retry on a fresh
                # connection because the failed query aborts the txn.
                conn.rollback()

    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                _FETCH_DOC_CLASS_TITLE_ONLY_SQL,
                {"report_ids": report_ids},
            )
            rows = cur.fetchall()
            return {
                r["report_id"]: _compute_doc_class(r["title"], False, False)
                for r in rows
            }


# ---------------------------------------------------------------------------
# Asset 1 — chunk population
# ---------------------------------------------------------------------------

# ADR-0010 Session B: chunk-population now reads silver.document_passages
# (the canonical chunked-content corpus) instead of silver.ingest_extractions +
# silver.ingest_ocr_results (raw extraction layer, retired as a chunk source).
#
# Field mapping (document_passages → reranker chain expectations):
#   passage_id       → chunk_id        (UUID, used directly — no derivation)
#   document_id      → report_id
#   page_first       → page            (passages spanning multiple pages use
#                                       the first page; bbox stays anchored
#                                       to the first-page region)
#   ordinal          → region          (semantic shift but same downstream role)
#   text             → chunk_text
#   chunk_kind       → source_method_bucket via chunk_kind mapping
#   parser_confidence→ extraction_confidence
#   bbox_x0/y0/x1/y1 → bbox (synthesised array [x0,y0,x1,y1] for downstream
#                                       parity with the old ingest_extractions
#                                       bbox column)
#
# The MIN_CHUNK_CHARS filter still applies — short chunks (page numbers etc.)
# are excluded at SQL time to avoid wasting vLLM calls on placeholder content.
_FETCH_DOCUMENT_PASSAGES_SQL = f"""
SELECT
    p.passage_id::text                          AS chunk_id,
    p.document_id::text                         AS report_id,
    COALESCE(p.page_first, 0)::int              AS page,
    COALESCE(p.ordinal, 0)::int                 AS region,
    p.chunk_kind                                AS chunk_kind,
    COALESCE(p.parser_confidence, 0)::FLOAT8    AS extraction_confidence,
    ARRAY[
        COALESCE(p.bbox_x0, 0)::FLOAT8,
        COALESCE(p.bbox_y0, 0)::FLOAT8,
        COALESCE(p.bbox_x1, 0)::FLOAT8,
        COALESCE(p.bbox_y1, 0)::FLOAT8
    ]                                           AS bbox,
    p.ocr_method                                AS ocr_method,
    COALESCE(p.text, '')                        AS chunk_text
FROM silver.document_passages p
-- §5e pre-flight: short-chunk filter. Page-numbers / numeric-only content
-- can't anchor a meaningful LLM-generated query, so they're excluded at
-- population time rather than wasting vLLM calls + getting filtered
-- downstream by the critique step.
WHERE COALESCE(LENGTH(p.text), 0) >= {MIN_CHUNK_CHARS}
  -- Skip OCR rows still in the reocr cycle — their text is from a
  -- low-confidence first pass and will be replaced shortly. Indexing
  -- them now would just churn the dataset on the next reocr_complete
  -- materialisation.
  AND (p.ocr_status IS NULL OR p.ocr_status != 'pending_reocr')
;
"""


def _chunk_kind_to_source_bucket(chunk_kind: str | None, ocr_method: str | None) -> str:
    """Map document_passages.chunk_kind + ocr_method → source_method_bucket.

    Mirrors the SOURCE_METHOD_BUCKETS map used in the ingest_extractions era
    so downstream stratified sampling continues to work without changes.

    Precedence:
      1. ocr_method IS NOT NULL → "ocr"  (OCR provenance overrides chunk_kind)
      2. chunk_kind = 'table'   → "table-extract"
      3. anything else          → "text"
    """
    if ocr_method:
        return "ocr"
    if chunk_kind == "table":
        return "table-extract"
    # narrative / section / paragraph / caption_figure / character_window / NULL
    return "text"


@asset(
    group_name=ASSET_GROUP,
    deps=[silver_reports],
    description=(
        "ADR-0010 — Polars dataframe of candidate reranker-label chunks "
        "pulled from silver.document_passages (the canonical chunked-content "
        "corpus per ADR-0010). Adds source_method_bucket + doc_class labels "
        "for stratified sampling downstream."
    ),
)
def reranker_chunk_population(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> Output[pl.DataFrame]:
    """Pull silver.document_passages into a single Polars df + class labels."""

    context.log.info(
        "reranker_chunk_population: fetching from silver.document_passages "
        "(canonical chunked-content corpus per ADR-0010)"
    )

    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_FETCH_DOCUMENT_PASSAGES_SQL)
            rows = [dict(r) for r in cur.fetchall()]

    context.log.info(
        "reranker_chunk_population: fetched %d rows from silver.document_passages",
        len(rows),
    )

    if not rows:
        context.log.warning(
            "reranker_chunk_population: no rows in silver.document_passages — "
            "empty population"
        )
        return Output(
            pl.DataFrame(),
            metadata={"row_count": MetadataValue.int(0)},
        )

    df = pl.DataFrame(rows, infer_schema_length=1000)
    if "extraction_confidence" in df.columns:
        df = df.with_columns(
            pl.col("extraction_confidence").cast(pl.Float64, strict=False)
        )

    # Derive source_method_bucket from chunk_kind + ocr_method. The
    # downstream stratified-sampling code keys on this column name so we
    # preserve the contract from the ingest_extractions era.
    source_buckets = [
        _chunk_kind_to_source_bucket(r.get("chunk_kind"), r.get("ocr_method"))
        for r in df.iter_rows(named=True)
    ]
    df = df.with_columns(pl.Series("source_method_bucket", source_buckets))

    # chunk_id is already the passage_id from SQL — no derivation needed.
    # Keep the table_name column for downstream parity with the legacy
    # shape (some readers reference it for provenance display).
    df = df.with_columns(pl.lit("document_passages").alias("table_name"))

    # Doc-class lookup — batch by report_id to amortise the PG round-trip.
    unique_reports = df.get_column("report_id").unique().to_list()
    doc_class_map = _fetch_doc_class_map(postgres, unique_reports)
    df = df.with_columns(
        pl.col("report_id").map_elements(
            lambda r: doc_class_map.get(r, "ni43"),
            return_dtype=pl.Utf8,
        ).alias("doc_class")
    )

    bucket_counts = (
        df.group_by(["source_method_bucket", "doc_class"])
          .agg(pl.len().alias("n"))
          .sort(["source_method_bucket", "doc_class"])
    )
    context.log.info("reranker_chunk_population: strata distribution:\n%s", bucket_counts)

    return Output(
        df,
        metadata={
            "row_count":       MetadataValue.int(df.height),
            "unique_reports":  MetadataValue.int(len(unique_reports)),
            "strata_summary":  MetadataValue.md(bucket_counts.to_pandas().to_markdown(index=False)),
            "source_table":    MetadataValue.text("silver.document_passages"),
        },
    )


# ---------------------------------------------------------------------------
# Asset 2 — stratified sample
# ---------------------------------------------------------------------------

@asset(
    group_name=ASSET_GROUP,
    ins={"population": AssetIn("reranker_chunk_population")},
    description=(
        f"Sqrt-proportional stratified sample of ~{TARGET_SAMPLE_SIZE} chunks "
        "across 9 strata = source_method_bucket × doc_class. Deterministic seed "
        "derived from dagster run_id."
    ),
)
def reranker_chunk_sample(
    context: AssetExecutionContext,
    population: pl.DataFrame,
    minio: S3Resource,
) -> Output[pl.DataFrame]:
    """Draw a reproducible stratified sample. Persists weights+draws to manifest.json."""

    if population.is_empty():
        context.log.warning("reranker_chunk_sample: empty population — emitting empty sample")
        return Output(pl.DataFrame(), metadata={"sampled_count": MetadataValue.int(0)})

    # Count per stratum.
    strata_counts: dict[str, int] = {}
    for source_bucket in SOURCE_BUCKETS:
        for doc_class in DOC_CLASSES:
            key = _strata_key(source_bucket, doc_class)
            strata_counts[key] = (
                population
                .filter(
                    (pl.col("source_method_bucket") == source_bucket)
                    & (pl.col("doc_class") == doc_class)
                )
                .height
            )

    allocations = _sqrt_proportional_allocation(strata_counts, TARGET_SAMPLE_SIZE)
    context.log.info("reranker_chunk_sample: allocations = %s", allocations)

    seed = _seed_from_run_id(context.run_id)
    rng = random.Random(seed)
    sampled_frames: list[pl.DataFrame] = []
    for stratum_key, target_n in allocations.items():
        if target_n <= 0:
            continue
        source_bucket, doc_class = stratum_key.split("|", 1)
        stratum = population.filter(
            (pl.col("source_method_bucket") == source_bucket)
            & (pl.col("doc_class") == doc_class)
        )
        if stratum.is_empty():
            continue
        # Deterministic shuffle via index list.
        indices = list(range(stratum.height))
        rng.shuffle(indices)
        keep = indices[:target_n]
        sampled_frames.append(stratum[keep])

    sample = (
        pl.concat(sampled_frames, how="vertical_relaxed")
        if sampled_frames
        else pl.DataFrame()
    )

    manifest = {
        "asset":             "reranker_chunk_sample",
        "run_id":            context.run_id,
        "seed":              seed,
        "target_total":      TARGET_SAMPLE_SIZE,
        "strata_counts":     strata_counts,
        "allocations":       allocations,
        "drawn":             sample.height,
        "captured_at":       datetime.now(timezone.utc).isoformat(),
    }

    minio.ensure_bucket(S3_BUCKET)
    manifest_key = f"{_run_output_prefix(context.run_id)}/manifest.sample.json"
    minio.upload_bytes(
        S3_BUCKET, manifest_key,
        json.dumps(manifest, indent=2).encode("utf-8"),
        content_type="application/json",
    )
    context.log.info("reranker_chunk_sample: manifest persisted s3://%s/%s", S3_BUCKET, manifest_key)

    return Output(
        sample,
        metadata={
            "sampled_count":   MetadataValue.int(sample.height),
            "manifest_s3_uri": MetadataValue.text(f"s3://{S3_BUCKET}/{manifest_key}"),
            "seed":            MetadataValue.text(str(seed)),
        },
    )


# ---------------------------------------------------------------------------
# Asset 3 — query generation (vLLM)
# ---------------------------------------------------------------------------

@asset(
    group_name=ASSET_GROUP,
    ins={"sample": AssetIn("reranker_chunk_sample")},
    description=(
        "Generate synthetic queries per sampled chunk: 2 per-chunk variants "
        "(literal + paraphrase) and 1 multi-hop pair per report (emitted as "
        "2 rows sharing a query_group_id). "
        f"vLLM Qwen3-14B-AWQ at temperature=0.2. Prompt version {PROMPT_VERSION}."
    ),
)
def reranker_generated_queries(
    context: AssetExecutionContext,
    sample: pl.DataFrame,
    vllm: VllmResource,
    minio: S3Resource,
) -> Output[pl.DataFrame]:
    """Call vLLM per chunk; produce one row per (chunk_id, variant) pair."""

    if sample.is_empty():
        context.log.warning("reranker_generated_queries: empty sample input")
        return Output(pl.DataFrame(), metadata={"generated_count": MetadataValue.int(0)})

    raw_client = vllm.get_client()
    client = VllmOpenAIClient(client=raw_client, model=vllm.model, max_workers=32)

    requests: list[VllmJsonRequest] = []
    request_meta: dict[str, dict[str, Any]] = {}
    # Group sampled chunks by report_id so we can build multi-hop pairs
    # (need two chunks from the same report to bridge sensibly).
    chunks_by_report: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sample.iter_rows(named=True):
        chunk_id = row["chunk_id"]
        chunk_text = row["chunk_text"][:6000]  # keep prompt under context budget

        user_msg = USER_PROMPT_QUERY_TEMPLATE.format(chunk_text=chunk_text)

        for variant, system_msg in (
            ("literal",    LITERAL_PROMPT_SYSTEM),
            ("paraphrase", PARAPHRASE_PROMPT_SYSTEM),
        ):
            req_id = f"{chunk_id}::{variant}"
            requests.append(
                VllmJsonRequest(
                    custom_id=req_id,
                    system=system_msg,
                    user=user_msg,
                    max_tokens=200,
                    temperature=0.2,
                )
            )
            request_meta[req_id] = {
                "chunk_id": chunk_id,
                "variant":  variant,
                "gen_prompt_hash": _prompt_sha256(system_msg, user_msg),
            }

        # Stash for multi-hop pair selection.
        report_id = row.get("report_id")
        if report_id:
            chunks_by_report[str(report_id)].append({
                "chunk_id":   chunk_id,
                "chunk_text": chunk_text,
            })

    # Multi-hop pair selection. Per the §5e pre-flight 2026-05-29 decision,
    # MULTI_HOP_RATIO_TARGET = 1.0 means single-chunk and multi-hop rows
    # should be in roughly equal proportion in the final dataset.
    #
    # Per-report row math:
    #   single_rows = C × 2  (literal + paraphrase per chunk)
    #   multihop_rows = P × 2  (each pair emits 2 rows sharing a group_id)
    # For ratio R = single / multi-hop: P = C / R
    # At R = 1.0 → P = C → emit C pairs per report.
    #
    # Pair selection is deterministic: sort chunks by chunk_id, then take
    # consecutive-window pairs with wrap-around so chunk_0 also pairs with
    # chunk_C-1. This gives exactly C pairs per report at C ≥ 2 (only 1
    # pair at C = 2 since the wrap-around is the same pair).
    import hashlib as _hashlib  # noqa: PLC0415 — keep import-locality
    import uuid as _uuid  # noqa: PLC0415

    multihop_pairs: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for report_id, chunks in chunks_by_report.items():
        if len(chunks) < 2:
            continue
        sorted_chunks = sorted(chunks, key=lambda c: c["chunk_id"])
        C = len(sorted_chunks)
        target_pairs = max(1, int(round(C * MULTI_HOP_RATIO_TARGET)))
        # Combinatorial cap: at C=2 there's only 1 unique pair (chunks 0,1).
        # Sliding-window with wrap would just repeat it.
        if C == 2:
            target_pairs = 1
        for i in range(target_pairs):
            a = sorted_chunks[i % C]
            b = sorted_chunks[(i + 1) % C]
            _digest = _hashlib.sha256(
                f"multihop::{report_id}::{a['chunk_id']}::{b['chunk_id']}".encode()
            ).hexdigest()[:32]
            group_id = str(_uuid.UUID(_digest))
            multihop_pairs.append((a, b, group_id))

    context.log.info(
        "reranker_generated_queries: built %d multi-hop pairs across %d reports "
        "(ratio target=%.2f → ~equal single/multi-hop row mix)",
        len(multihop_pairs), len(chunks_by_report), MULTI_HOP_RATIO_TARGET,
    )

    for a, b, group_id in multihop_pairs:
        user_msg_mh = USER_PROMPT_MULTIHOP_TEMPLATE.format(
            chunk_a_text=a["chunk_text"], chunk_b_text=b["chunk_text"],
        )
        req_id = f"multihop::{group_id}"
        requests.append(
            VllmJsonRequest(
                custom_id=req_id,
                system=MULTI_HOP_PROMPT_SYSTEM,
                user=user_msg_mh,
                max_tokens=200,
                temperature=0.2,
            )
        )
        request_meta[req_id] = {
            "chunk_id": a["chunk_id"],  # primary chunk for the row; b's row is emitted below
            "chunk_id_b": b["chunk_id"],
            "variant":  "multi_hop",
            "query_group_id": group_id,
            "gen_prompt_hash": _prompt_sha256(MULTI_HOP_PROMPT_SYSTEM, user_msg_mh),
        }

    context.log.info("reranker_generated_queries: dispatching %d requests to vLLM", len(requests))

    def _progress(done: int, total: int) -> None:
        if done % max(total // 10, 1) == 0:
            context.log.info("reranker_generated_queries: %d / %d completed", done, total)

    results = client.run_many(requests, progress=_progress)

    # Coerce results into rows. Multi-hop responses emit TWO rows (one per
    # bridge chunk) with a shared query_group_id so the training stage can
    # treat both as positives for the same query.
    out_rows: list[dict[str, Any]] = []
    parse_failures = 0
    for r in results:
        meta = request_meta[r.custom_id]
        if r.parsed is None:
            parse_failures += 1
            continue
        query = r.parsed.get("query")
        if not query or not isinstance(query, str):
            parse_failures += 1
            continue

        if meta["variant"] == "multi_hop":
            bridge = r.parsed.get("bridge")
            for chunk_id_role in ("chunk_id", "chunk_id_b"):
                out_rows.append({
                    "chunk_id":        meta[chunk_id_role],
                    "variant":         "multi_hop",
                    "query":           query.strip(),
                    "fact_span":       "",  # multi-hop has no single-chunk span
                    "bridge":          (bridge or "").strip() if isinstance(bridge, str) else "",
                    "query_group_id": meta["query_group_id"],
                    "gen_model":       vllm.model,
                    "gen_prompt_hash": meta["gen_prompt_hash"],
                    "prompt_version":  PROMPT_VERSION,
                })
            continue

        fact_span = r.parsed.get("fact_span")
        out_rows.append({
            "chunk_id":        meta["chunk_id"],
            "variant":         meta["variant"],
            "query":           query.strip(),
            "fact_span":       (fact_span or "").strip() if isinstance(fact_span, str) else "",
            "bridge":          "",
            "query_group_id":  f"single::{meta['chunk_id']}::{meta['variant']}",
            "gen_model":       vllm.model,
            "gen_prompt_hash": meta["gen_prompt_hash"],
            "prompt_version":  PROMPT_VERSION,
        })

    context.log.info(
        "reranker_generated_queries: generated %d queries (%d parse/empty failures)",
        len(out_rows), parse_failures,
    )

    queries_df = pl.DataFrame(out_rows) if out_rows else pl.DataFrame()

    # Stash to S3 for downstream debugging.
    if not queries_df.is_empty():
        buf = io.BytesIO()
        queries_df.write_parquet(buf)
        minio.ensure_bucket(S3_BUCKET)
        key = f"{_run_output_prefix(context.run_id)}/generated_queries.parquet"
        minio.upload_bytes(S3_BUCKET, key, buf.getvalue(), content_type="application/x-parquet")
        context.log.info("reranker_generated_queries: wrote s3://%s/%s", S3_BUCKET, key)

    return Output(
        queries_df,
        metadata={
            "generated_count":  MetadataValue.int(queries_df.height),
            "parse_failures":   MetadataValue.int(parse_failures),
            "prompt_version":   MetadataValue.text(PROMPT_VERSION),
        },
    )


# ---------------------------------------------------------------------------
# Asset 4 — hard-negative mining
# ---------------------------------------------------------------------------

# ADR-0010 Session B note: the legacy _QDRANT_TO_PAGE_SQL round-trip was
# removed — the new georag_chunks payload carries `page` directly (aliased
# from page_first in index_document_passages._build_payload), so the
# hard-negative mining loop reads page off the payload without a PG hop.


@asset(
    group_name=ASSET_GROUP,
    deps=[index_document_passages],
    ins={
        "sample":    AssetIn("reranker_chunk_sample"),
        "queries":   AssetIn("reranker_generated_queries"),
    },
    description=(
        "Mine 4-6 hard negatives per (query, c+) pair from the Qdrant "
        "georag_chunks collection top-50 (canonical per ADR-0010). "
        "Drops c+ + same-document neighbours."
    ),
)
def reranker_mined_negatives(
    context: AssetExecutionContext,
    sample: pl.DataFrame,
    queries: pl.DataFrame,
    qdrant: QdrantResource,
    postgres: PostgresResource,
) -> Output[pl.DataFrame]:
    """Build candidate hard-negative lists keyed by (chunk_id, variant)."""

    if queries.is_empty() or sample.is_empty():
        context.log.warning("reranker_mined_negatives: nothing to mine")
        return Output(pl.DataFrame(), metadata={"mined_rows": MetadataValue.int(0)})

    # We need to embed the query string with the same dense encoder used
    # for the index, then call Qdrant `search`. Reuse the cached model
    # from index_reports for vector-space parity.
    from georag_dagster.assets.index_document_passages import _embed_in_batches  # noqa: PLC0415

    sample_indexed = sample.select(["chunk_id", "report_id", "page", "chunk_text"]).unique(
        subset=["chunk_id"]
    )
    chunk_lookup: dict[str, dict[str, Any]] = {
        r["chunk_id"]: r for r in sample_indexed.iter_rows(named=True)
    }

    query_rows = list(queries.iter_rows(named=True))
    query_texts = [r["query"] for r in query_rows]
    context.log.info("reranker_mined_negatives: embedding %d query strings", len(query_texts))
    embeddings = _embed_in_batches(query_texts, context)

    client = qdrant.get_client()
    seed = _seed_from_run_id(context.run_id)
    rng = random.Random(seed)

    mined_rows: list[dict[str, Any]] = []
    no_neighbour_rows = 0
    rank_band_counts: dict[str, int] = defaultdict(int)

    for q_row, vec in zip(query_rows, embeddings):
        chunk_id = q_row["chunk_id"]
        positive = chunk_lookup.get(chunk_id)
        if positive is None:
            continue
        try:
            hits = client.query_points(
                collection_name=QDRANT_COLLECTION,
                query=vec.tolist(),
                using="",
                limit=50,
                with_payload=True,
            ).points
        except Exception as exc:  # noqa: BLE001
            context.log.warning("reranker_mined_negatives: qdrant search failed for %s: %s", chunk_id, exc)
            continue

        # Cross-deposit note: we lean on `commodity` as the lithology
        # substitute in v1. This is a deliberate weakening — Qdrant
        # payloads don't yet carry lithology fields, and cross-commodity
        # negatives are still cheap and useful for the reranker.
        positive_report = str(positive["report_id"])
        candidates: list[tuple[int, dict[str, Any]]] = []
        for rank, hit in enumerate(hits, start=1):
            payload = hit.payload or {}
            hit_report = str(payload.get("report_id", ""))
            # Drop the positive itself + same-page neighbours.
            # Qdrant doesn't carry `page` today — v1 workaround: drop
            # entire same-report hits. Follow-up adds page-aware filter.
            if hit_report == positive_report:
                continue
            # 2026-05-29 — capture chunk_text from the Qdrant payload so
            # the downstream training script can read denormalised text
            # directly. Previously the asset persisted only point IDs,
            # leaving the training step unable to load samples without
            # an extra silver.document_passages join (which broke when
            # chunks were re-ingested between materialisation + training).
            candidates.append((rank, {
                "qdrant_point_id": str(hit.id),
                "score":           float(hit.score),
                "rank":            rank,
                "report_id":       hit_report,
                "commodity":       payload.get("commodity"),
                "section_title":   payload.get("section_title"),
                "chunk_text":      payload.get("text", ""),
            }))

        if not candidates:
            no_neighbour_rows += 1
            continue

        # Bucket by rank band → sample 4-6.
        banded: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for rank, c in candidates:
            for lo, hi in NEG_RANK_BANDS:
                if lo <= rank <= hi:
                    banded[f"{lo}-{hi}"].append(c)
                    break

        target = rng.randint(NEG_PER_QUERY_MIN, NEG_PER_QUERY_MAX)
        chosen: list[dict[str, Any]] = []
        # Round-robin across bands so we always have spread.
        bands_order = list(banded.keys())
        rng.shuffle(bands_order)
        while len(chosen) < target and bands_order:
            for band in list(bands_order):
                bucket = banded[band]
                if not bucket:
                    bands_order.remove(band)
                    continue
                pick = rng.choice(bucket)
                bucket.remove(pick)
                chosen.append(pick)
                rank_band_counts[band] += 1
                if len(chosen) >= target:
                    break

        if not chosen:
            no_neighbour_rows += 1
            continue

        mined_rows.append({
            "chunk_id":         chunk_id,
            "variant":          q_row["variant"],
            "query":            q_row["query"],
            "fact_span":        q_row["fact_span"],
            "gen_model":        q_row["gen_model"],
            "gen_prompt_hash":  q_row["gen_prompt_hash"],
            "positive":         dict(positive),
            "hardneg":          chosen,
        })

    context.log.info(
        "reranker_mined_negatives: %d rows mined, %d skipped (no neighbours), band counts=%s",
        len(mined_rows), no_neighbour_rows, dict(rank_band_counts),
    )

    df = pl.DataFrame(mined_rows) if mined_rows else pl.DataFrame()
    return Output(
        df,
        metadata={
            "mined_rows":             MetadataValue.int(df.height),
            "skipped_no_neighbours":  MetadataValue.int(no_neighbour_rows),
            "rank_band_counts":       MetadataValue.json(dict(rank_band_counts)),
        },
    )


# ---------------------------------------------------------------------------
# Asset 5 — label dataset with self-critique
# ---------------------------------------------------------------------------

@asset(
    group_name=ASSET_GROUP,
    ins={"mined": AssetIn("reranker_mined_negatives")},
    description=(
        "Self-critique relevance scoring + leakage filter. Emits JSONL train/val/test "
        "splits to s3://reranker-labels/v1/run_id=<run_id>/. Splits computed by report_id "
        "80/10/10 — Kyle-approved leakage-prevention split key."
    ),
)
def reranker_label_dataset(
    context: AssetExecutionContext,
    mined: pl.DataFrame,
    vllm: VllmResource,
    minio: S3Resource,
) -> MaterializeResult:
    """Filter mined rows by self-critique relevance + leakage; emit JSONL splits."""

    if mined.is_empty():
        context.log.warning("reranker_label_dataset: no input rows — emitting empty dataset")
        return MaterializeResult(metadata={"final_rows": MetadataValue.int(0)})

    raw_client = vllm.get_client()
    client = VllmOpenAIClient(client=raw_client, model=vllm.model, max_workers=32)

    rows = list(mined.iter_rows(named=True))
    requests: list[VllmJsonRequest] = []
    request_meta: dict[str, int] = {}
    for idx, r in enumerate(rows):
        chunk_text = r["positive"]["chunk_text"][:6000]
        user_msg = USER_PROMPT_CRITIQUE_TEMPLATE.format(
            query=r["query"], chunk_text=chunk_text,
        )
        req_id = f"critique::{idx}"
        requests.append(VllmJsonRequest(
            custom_id=req_id,
            system=CRITIQUE_PROMPT_SYSTEM,
            user=user_msg,
            max_tokens=120,
            temperature=0.2,
        ))
        request_meta[req_id] = idx

    context.log.info("reranker_label_dataset: dispatching %d critique requests", len(requests))

    def _progress(done: int, total: int) -> None:
        if done % max(total // 10, 1) == 0:
            context.log.info("reranker_label_dataset: critique %d / %d", done, total)

    critique_results = client.run_many(requests, progress=_progress)

    scores: dict[int, int] = {}
    critique_failures = 0
    for cr in critique_results:
        idx = request_meta.get(cr.custom_id)
        if idx is None or cr.parsed is None:
            critique_failures += 1
            continue
        score = cr.parsed.get("score")
        try:
            scores[idx] = int(score)
        except (TypeError, ValueError):
            critique_failures += 1

    # Filter — relevance >= 2 AND leakage <= threshold.
    keep_rows: list[dict[str, Any]] = []
    rejected_lowscore = 0
    rejected_leakage = 0
    leakage_flagged = 0  # for asset-check warning rate
    for idx, r in enumerate(rows):
        score = scores.get(idx)
        if score is None or score < CRITIQUE_MIN_SCORE:
            rejected_lowscore += 1
            continue
        chunk_text = r["positive"]["chunk_text"]
        leakage = _leakage_ratio(r["query"], chunk_text)
        if leakage > LEAKAGE_THRESHOLD:
            rejected_leakage += 1
            leakage_flagged += 1
            continue
        keep_rows.append({**r, "label": int(score), "leakage_ratio": leakage})

    context.log.info(
        "reranker_label_dataset: kept=%d rejected_lowscore=%d rejected_leakage=%d critique_failures=%d",
        len(keep_rows), rejected_lowscore, rejected_leakage, critique_failures,
    )

    if not keep_rows:
        context.log.error("reranker_label_dataset: all rows filtered — dataset is empty")
        return MaterializeResult(metadata={"final_rows": MetadataValue.int(0)})

    # Split by report_id 80/10/10.
    report_ids = sorted({r["positive"]["report_id"] for r in keep_rows})
    split_assignment = _train_val_test_split_by_report(
        report_ids,
        seed=_seed_from_run_id(context.run_id),
    )

    splits: dict[str, list[dict[str, Any]]] = {"train": [], "val": [], "test": []}
    for r in keep_rows:
        positive = r["positive"]
        # 2026-05-29 — emit denormalised chunk text so the training
        # script can load directly from the JSONL without joining
        # against silver.document_passages (the join is unreliable when
        # chunks are re-ingested between materialisation + training).
        # Also surfaces `variant` + `query_group_id` so the training
        # script's multi-hop grouping logic can run; both were already
        # tracked through the upstream stages but dropped at persist.
        record = {
            "query":                 r["query"],
            "chunk_id":              r["chunk_id"],
            "pdf_id":                positive["report_id"],
            "page":                  positive.get("page"),
            "bbox":                  positive.get("bbox"),
            "source_method":         positive.get("source_method"),
            "extraction_confidence": positive.get("extraction_confidence"),
            "label":                 r["label"],
            "positive_chunk_text":   positive.get("chunk_text", ""),
            "hardneg_ids":           [hn["qdrant_point_id"] for hn in r["hardneg"]],
            "hard_negative_chunk_texts": [
                hn.get("chunk_text", "") for hn in r["hardneg"]
            ],
            "variant":               r.get("variant", "literal"),
            "query_group_id":        r.get("query_group_id"),
            "gen_model":             r["gen_model"],
            "gen_prompt_hash":       r["gen_prompt_hash"],
            "fact_span":             r["fact_span"],
        }
        split = split_assignment.get(positive["report_id"], "train")
        splits[split].append(record)

    # Persist JSONL + manifest.
    minio.ensure_bucket(S3_BUCKET)
    out_prefix = _run_output_prefix(context.run_id)
    written: dict[str, str] = {}
    for split_name, records in splits.items():
        buf = io.BytesIO()
        for rec in records:
            buf.write(json.dumps(rec).encode("utf-8") + b"\n")
        key = f"{out_prefix}/{split_name}.jsonl"
        minio.upload_bytes(S3_BUCKET, key, buf.getvalue(), content_type="application/jsonl")
        written[split_name] = key
        context.log.info("reranker_label_dataset: wrote %d %s rows to s3://%s/%s",
                         len(records), split_name, S3_BUCKET, key)

    # Also dump the full population + generated_queries parquet snapshots if
    # not already there (re-emitted for self-contained run output).
    leakage_rate = leakage_flagged / max(len(rows), 1)
    manifest = {
        "asset":             "reranker_label_dataset",
        "run_id":            context.run_id,
        "prompt_version":    PROMPT_VERSION,
        "gen_model":         vllm.model,
        "input_rows":        len(rows),
        "rejected_lowscore": rejected_lowscore,
        "rejected_leakage":  rejected_leakage,
        "critique_failures": critique_failures,
        "final_rows":        len(keep_rows),
        "splits": {k: len(v) for k, v in splits.items()},
        "leakage_warn_rate": leakage_rate,
        "files":             written,
        "captured_at":       datetime.now(timezone.utc).isoformat(),
    }
    manifest_key = f"{out_prefix}/manifest.json"
    minio.upload_bytes(
        S3_BUCKET, manifest_key,
        json.dumps(manifest, indent=2).encode("utf-8"),
        content_type="application/json",
    )

    return MaterializeResult(
        metadata={
            "final_rows":         MetadataValue.int(len(keep_rows)),
            "train_rows":         MetadataValue.int(len(splits["train"])),
            "val_rows":           MetadataValue.int(len(splits["val"])),
            "test_rows":          MetadataValue.int(len(splits["test"])),
            "rejected_lowscore":  MetadataValue.int(rejected_lowscore),
            "rejected_leakage":   MetadataValue.int(rejected_leakage),
            "leakage_warn_rate":  MetadataValue.float(leakage_rate),
            "manifest_s3_uri":    MetadataValue.text(f"s3://{S3_BUCKET}/{manifest_key}"),
            "gen_model":          MetadataValue.text(vllm.model),
            "prompt_version":     MetadataValue.text(PROMPT_VERSION),
        }
    )


# ---------------------------------------------------------------------------
# Asset check — blocking gate on final dataset
# ---------------------------------------------------------------------------

@asset_check(
    asset=reranker_label_dataset,
    description=(
        f"Blocks downstream training when filtered triple count < {CHECK_MIN_TRIPLES} "
        f"or leakage-flag rate exceeds {CHECK_MAX_LEAKAGE_WARN_RATE:.0%}."
    ),
)
def reranker_label_dataset_minimum_size_check(
    context: AssetCheckExecutionContext,
    minio: S3Resource,
) -> AssetCheckResult:
    """Read the manifest for the most-recent run_id and check triple count + leakage rate.

    The check pulls the manifest written by the asset itself rather than
    re-running the filter. This keeps the gate cheap to re-evaluate after
    a partial-failure replay.
    """
    manifest_key = f"{_run_output_prefix(context.run.run_id)}/manifest.json"
    try:
        raw = minio.download_bytes(S3_BUCKET, manifest_key)
    except Exception as exc:  # noqa: BLE001
        return AssetCheckResult(
            passed=False,
            severity=AssetCheckSeverity.ERROR,
            description=f"Could not load manifest s3://{S3_BUCKET}/{manifest_key}: {exc}",
        )

    manifest = json.loads(raw.decode("utf-8"))
    final_rows = int(manifest.get("final_rows", 0))
    leakage_rate = float(manifest.get("leakage_warn_rate", 0.0))

    failures: list[str] = []
    if final_rows < CHECK_MIN_TRIPLES:
        failures.append(
            f"final_rows={final_rows} < {CHECK_MIN_TRIPLES}"
        )
    if leakage_rate > CHECK_MAX_LEAKAGE_WARN_RATE:
        failures.append(
            f"leakage_warn_rate={leakage_rate:.3f} > {CHECK_MAX_LEAKAGE_WARN_RATE:.3f}"
        )

    return AssetCheckResult(
        passed=not failures,
        severity=AssetCheckSeverity.ERROR if failures else AssetCheckSeverity.WARN,
        description=(
            "; ".join(failures) if failures
            else f"final_rows={final_rows} leakage_warn_rate={leakage_rate:.3f}"
        ),
        metadata={
            "final_rows":        MetadataValue.int(final_rows),
            "leakage_warn_rate": MetadataValue.float(leakage_rate),
        },
    )
