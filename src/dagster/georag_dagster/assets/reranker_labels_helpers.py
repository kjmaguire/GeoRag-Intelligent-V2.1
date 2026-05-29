"""Pure-function helpers extracted from ``reranker_labels``.

Lives in its own module so unit tests can import it without dragging in
the Dagster runtime, psycopg2, boto3, or the rest of the asset graph.
The ``reranker_labels`` asset module re-exports everything from here so
all production code paths stay at the original import sites.

Nothing in this file may import Dagster, asyncpg, psycopg2, boto3, the
openai SDK, polars, or any other heavy-weight dependency. Standard
library only.
"""

from __future__ import annotations

import hashlib
import math
import random
import uuid

# ---------------------------------------------------------------------------
# Constants — shared between the asset module and the unit tests.
# ---------------------------------------------------------------------------

# Stratified-sample target. ~50k chunks across 9 strata keeps the model
# small enough to train in <12h on a single A100 while staying large
# enough for the bge-reranker-base parameter count.
TARGET_SAMPLE_SIZE = 50_000

# 9-stratum cross.
DOC_CLASSES = ("ni43", "drill_log", "assay_table")
SOURCE_BUCKETS = ("text", "ocr", "table-extract")

# Prompt set is locked at v1.0; the per-row gen_prompt_hash captures
# byte-level changes when this is bumped.
PROMPT_VERSION = "v1.0"

# Leakage filter: reject when >40% of chunk unique tokens echo into the
# query verbatim.
LEAKAGE_THRESHOLD = 0.40

# Asset-check thresholds — Kyle-approved blocking gate on the final asset.
CHECK_MIN_TRIPLES = 150_000
CHECK_MAX_LEAKAGE_WARN_RATE = 0.05

# Relevance floor — drop self-critique <2 before training.
CRITIQUE_MIN_SCORE = 2

# §5e XL pre-flight 2026-05-29 — locked decisions from Kyle:
#
# MIN_CHUNK_CHARS — short-chunk pre-filter. The 2026-05-19 run had
# `fact_span="3"` rows (page-number-shaped content). Asking the LLM
# to generate meaningful questions about numeric-only content is
# guaranteed to produce placeholders. 200 chars ≈ 1 sentence — enough
# context to anchor a real query.
MIN_CHUNK_CHARS = 200

# MULTI_HOP_RATIO_TARGET — single-chunk vs multi-hop training-row
# proportion. 1.0 = equal mix. Each chunk emits 2 single-chunk rows
# (literal + paraphrase); each multi-hop pair emits 2 rows sharing a
# query_group_id. With ratio=1.0, pairs_per_report = chunks_per_report
# so the output is balanced. Increases vLLM call count by ~50% per
# report but the agentic-retrieval graph benefits from a strong
# multi-evidence training signal.
MULTI_HOP_RATIO_TARGET = 1.0


# ---------------------------------------------------------------------------
# Determinism helpers
# ---------------------------------------------------------------------------

def deterministic_chunk_id(
    report_id: str, table_name: str, page: int, region: int
) -> str:
    """Mirror of ``index_reports._deterministic_point_id`` for ingest_* rows.

    The reranker dataset needs a UUID that uniquely addresses the
    (report_id, table, page, region) tuple so dedupe and join keys stay
    stable across re-runs.
    """
    digest = hashlib.sha256(
        f"{report_id}::{table_name}::{page}::{region}".encode()
    ).hexdigest()
    return str(uuid.UUID(digest[:32]))


def prompt_sha256(system: str, user: str) -> str:
    """SHA-256 of the rendered system+user prompt pair (hex). Stamped per row."""
    return hashlib.sha256(f"{system}\n---\n{user}".encode()).hexdigest()


def seed_from_run_id(run_id: str) -> int:
    """Deterministic 64-bit seed derived from the Dagster run_id (UUID string)."""
    return int(hashlib.sha256(run_id.encode()).hexdigest()[:16], 16)


def strata_key(source_bucket: str, doc_class: str) -> str:
    return f"{source_bucket}|{doc_class}"


# ---------------------------------------------------------------------------
# Allocation + filter logic
# ---------------------------------------------------------------------------

def sqrt_proportional_allocation(
    strata_counts: dict[str, int],
    target_total: int,
) -> dict[str, int]:
    """Allocate ``target_total`` slots across strata by sqrt(count) weighting.

    Sqrt-proportional allocation prevents the largest stratum from
    drowning out small strata while still respecting the population
    distribution. Slots are capped at the stratum's available count.
    """
    weights = {k: math.sqrt(max(v, 0)) for k, v in strata_counts.items()}
    total_w = sum(weights.values())
    if total_w == 0:
        return {k: 0 for k in strata_counts}

    raw = {k: target_total * (w / total_w) for k, w in weights.items()}
    allocations: dict[str, int] = {}
    for k, cnt in strata_counts.items():
        allocations[k] = min(int(round(raw[k])), cnt)
    return allocations


def leakage_ratio(query: str, chunk_text: str) -> float:
    """Fraction of the chunk's unique non-stopword tokens echoed verbatim in the query.

    Cheap proxy for memorisation. A 1500-char chunk has ~150-250 unique
    tokens; even an aggressive paraphrase rarely exceeds 0.4 leakage,
    while pure regurgitation lands in 0.8+. Stopwords are filtered by
    a minimum token length (>3 chars) which is good enough for English
    geological prose and avoids importing nltk just for this check.
    """
    def _tokenise(text: str) -> set[str]:
        return {
            t.lower().strip(".,;:()[]\"'")
            for t in text.split()
            if len(t) > 3
        }

    chunk_tokens = _tokenise(chunk_text)
    if not chunk_tokens:
        return 0.0
    query_tokens = _tokenise(query)
    overlap = chunk_tokens & query_tokens
    return len(overlap) / len(chunk_tokens)


# ---------------------------------------------------------------------------
# Split assignment
# ---------------------------------------------------------------------------

def train_val_test_split_by_report(
    report_ids: list[str],
    seed: int,
    ratios: tuple[float, float, float] = (0.80, 0.10, 0.10),
) -> dict[str, str]:
    """Assign every report_id to train / val / test by seeded shuffle.

    Splitting on report_id (not triple count) is non-negotiable: chunks
    from the same report share author voice and boilerplate. Triple-
    level shuffling would leak that signal across splits.
    """
    rng = random.Random(seed)
    shuffled = list(report_ids)
    rng.shuffle(shuffled)
    n = len(shuffled)
    n_train = int(n * ratios[0])
    n_val = int(n * ratios[1])
    assignment: dict[str, str] = {}
    for i, rid in enumerate(shuffled):
        if i < n_train:
            assignment[rid] = "train"
        elif i < n_train + n_val:
            assignment[rid] = "val"
        else:
            assignment[rid] = "test"
    return assignment


# ---------------------------------------------------------------------------
# Doc-class heuristic
# ---------------------------------------------------------------------------

def compute_doc_class(
    title: str, has_drill_traces: bool, has_samples: bool
) -> str:
    """V1 heuristic. ``ni43`` is the default fallback.

    Detection precedence:
      1. drill_log — silver.drill_traces has rows for the report's collars
      2. assay_table — silver.samples has rows for the report's collars
      3. ni43 — title contains "technical report" (case-insensitive)
      4. ni43 — default
    """
    if has_drill_traces:
        return "drill_log"
    if has_samples:
        return "assay_table"
    if "technical report" in title.lower():
        return "ni43"
    return "ni43"
