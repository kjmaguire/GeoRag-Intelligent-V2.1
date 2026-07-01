"""Cross-store Reciprocal Rank Fusion (RRF) for GeoRAG hybrid retrieval.

Module 4 Chunk 2 -- implements B5 from the module spec: fusing result lists
from multiple retrieval stores (Qdrant hybrid, Neo4j traversal, PostGIS filter)
into a single ranked list using the standard RRF formula.

RRF overview
------------
Reciprocal Rank Fusion (Cormack et al., 2009) scores each document as:

    score(d) = sum_{list L} 1 / (k + rank_L(d))

where k=60 is the standard smoothing constant that prevents a single
top-ranked document from dominating. Documents appearing in multiple lists
accumulate higher scores; documents absent from a list contribute 0 for that
list.

Cross-store canonical ID policy
---------------------------------
Every retrieved item must have a unique `canonical_id` for deduplication.
Convention:

  - Qdrant passages:    passage UUID string, e.g. "3f8a1c2d-..."
  - Neo4j nodes:        "neo4j:<node_element_id>" or "neo4j:<node_id>"
  - PostGIS collars:    "postgis:collars:<collar_id_uuid>"
  - PostGIS samples:    "postgis:samples:<sample_id>"
  - PostGIS lithology:  "postgis:lithology:<interval_id>"
  - PostGIS geochemistry: "postgis:geochemistry:<geom_id>"

All callers must populate `canonical_id` before passing candidates to
rrf_fuse(). Callers that do not have a natural unique key should derive one
deterministically (e.g., SHA-256 of table+pk).

Tiebreak policy
---------------
When two candidates have identical RRF scores, they are ordered by
canonical_id (ascending lexicographic). This is a stable, deterministic
ordering that makes test assertions predictable.

fusion_method constant
----------------------
The string "rrf" is available as FUSION_METHOD for callers that need to
record the fusion method in answer_runs (Chunk 3 scope).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

FUSION_METHOD = "rrf"
RRF_K = 60  # standard smoothing constant per Cormack et al. 2009


@dataclass
class Candidate:
    """A single retrieved item from any store.

    Attributes:
        canonical_id: Unique ID per the cross-store convention (see module docstring).
        store: Which store produced this candidate. One of "qdrant", "neo4j",
               "postgis", "hybrid".
        score: Raw relevance score from the producing store (cosine, BM25, etc.).
               Not used in RRF arithmetic but preserved for diagnostic logging.
        payload: Original result payload (DocumentChunk, collar dict, etc.).
                 Opaque to the fusion layer -- callers interpret their own payloads.
    """
    canonical_id: str
    store: str
    score: float = 0.0
    payload: Any = field(default=None, repr=False)


@dataclass
class ScoredCandidate:
    """A candidate enriched with its RRF score and final rank.

    Attributes:
        candidate: The original Candidate object.
        rrf_score: Accumulated RRF score across all input lists.
        rrf_rank: 1-based rank in the final merged ordering (1 = best).
    """
    candidate: Candidate
    rrf_score: float
    rrf_rank: int


def rrf_fuse(
    result_lists: list[list[Candidate]],
    k: int = RRF_K,
) -> list[ScoredCandidate]:
    """Fuse multiple ranked result lists using Reciprocal Rank Fusion.

    Standard RRF: score(d) = sum over lists of 1/(k + rank(d))
    where rank is 1-based and documents absent from a list contribute 0.

    Returns merged list sorted by fused score descending. Stable tiebreak
    on canonical_id ascending (deterministic for tests).

    Args:
        result_lists: List of ranked candidate lists. Each inner list is
                      ordered best-first (rank 1 = index 0). Empty lists
                      are ignored. Lists may have overlapping candidates
                      (identified by canonical_id) -- these accumulate score.
        k: RRF smoothing constant. Default 60 per the literature.

    Returns:
        List of ScoredCandidate sorted by rrf_score descending.
        Empty list if all input lists are empty.

    Examples:
        Single list -- trivially preserves original order:
            rrf_fuse([[c1, c2, c3]]) -> [c1, c2, c3] (same order)

        Two disjoint lists -- score = 1/(k+1) for each rank-1 item:
            rrf_fuse([[c1], [c2]]) -> [c1, c2] (tiebreak by canonical_id)

        Overlap -- c1 in both lists at rank 1 scores 2/(k+1):
            rrf_fuse([[c1, c2], [c1, c3]])
            -> c1 (score ~0.032), c2 (score ~0.016), c3 (score ~0.016)
    """
    scores: dict[str, float] = {}
    best_candidate: dict[str, Candidate] = {}

    for ranked_list in result_lists:
        if not ranked_list:
            continue
        for rank_zero_based, cand in enumerate(ranked_list):
            key = cand.canonical_id
            # rank is 1-based in the formula
            contribution = 1.0 / (k + rank_zero_based + 1)
            scores[key] = scores.get(key, 0.0) + contribution
            if key not in best_candidate:
                best_candidate[key] = cand

    # Sort: descending score, then ascending canonical_id for stable tiebreak
    merged = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))

    return [
        ScoredCandidate(
            candidate=best_candidate[key],
            rrf_score=score,
            rrf_rank=i + 1,
        )
        for i, (key, score) in enumerate(merged)
    ]


# ---------------------------------------------------------------------------
# Freshness boost (Eval 01 L6 follow-up, 2026-05-20).
#
# Per the §04i guard spec: stale public_geo records should be demoted in
# retrieval ranking when workspace data is newer. The `what_changed_*`
# Hatchet workflow output feeds report_builder but did not previously
# influence the retrieval ranking. This helper closes that gap.
#
# Behaviour
# ---------
# For each ScoredCandidate, if the candidate is from a `public_geo*` store
# AND the candidate's `payload.ingested_at` (when present) is older than
# the supplied `workspace_data_version_ts`, apply a multiplicative demotion
# factor to the rrf_score. Other candidates are unchanged. Re-sorts by the
# new score.
#
# Weight semantics
# ----------------
# `weight=0.0`  → no-op. The function returns the input list unchanged.
#                  This is the conservative default until an operator opts in.
# `weight=0.2`  → demote stale public_geo by 20 % (multiply rrf_score by 0.8).
# `weight=1.0`  → full demotion (multiply by 0.0 — stale entries effectively dropped).
#
# The operator dials in via settings.FRESHNESS_RANKING_WEIGHT.
#
# Safety
# ------
# Missing `ingested_at` on a candidate → treated as fresh (no demotion).
# Missing `workspace_data_version_ts` (e.g. ingest just ran, no version yet)
# → no-op (returns input unchanged). The function never raises.
# ---------------------------------------------------------------------------


def apply_freshness_boost(
    scored: list[ScoredCandidate],
    *,
    workspace_data_version_ts: float | None,
    weight: float = 0.0,
) -> list[ScoredCandidate]:
    """Demote stale public_geo candidates relative to newer workspace data.

    Args:
        scored: Output of `rrf_fuse` — sorted-best-first.
        workspace_data_version_ts: Unix timestamp of the most recent
            workspace data write. If None, the helper is a no-op.
        weight: Demotion strength, 0.0 (no-op) to 1.0 (full removal).

    Returns:
        Re-sorted ScoredCandidate list. Stable when weight=0 OR no stale
        public_geo candidates were present.
    """
    if weight <= 0.0 or workspace_data_version_ts is None or not scored:
        return scored

    demotion = max(0.0, 1.0 - weight)
    boosted: list[ScoredCandidate] = []
    for sc in scored:
        store = (sc.candidate.store or "").lower()
        is_public_geo = store.startswith("public_geo") or "pgeo" in store

        if not is_public_geo:
            boosted.append(sc)
            continue

        payload = sc.candidate.payload
        ingested_at = None
        ingested_at = payload.get("ingested_at") if isinstance(payload, dict) else getattr(payload, "ingested_at", None)

        # Treat missing ingested_at as fresh — we can't prove stale-ness.
        if ingested_at is None:
            boosted.append(sc)
            continue

        # Compare timestamps. Accept either Unix float or ISO 8601 str.
        ts_value: float | None = None
        if isinstance(ingested_at, (int, float)):
            ts_value = float(ingested_at)
        elif isinstance(ingested_at, str):
            try:
                from datetime import datetime  # noqa: PLC0415
                ts_value = datetime.fromisoformat(ingested_at).timestamp()
            except (ValueError, TypeError):
                ts_value = None

        if ts_value is None or ts_value >= workspace_data_version_ts:
            # Either unparseable timestamp (treat as fresh) or actually newer
            # than the workspace data — no demotion.
            boosted.append(sc)
            continue

        # Stale public_geo. Apply demotion factor.
        boosted.append(
            ScoredCandidate(
                candidate=sc.candidate,
                rrf_score=sc.rrf_score * demotion,
                rrf_rank=sc.rrf_rank,  # will be re-sorted below
            )
        )

    # Re-sort by demoted scores. Same stable tiebreak as rrf_fuse.
    boosted.sort(key=lambda x: (-x.rrf_score, x.candidate.canonical_id))
    return [
        ScoredCandidate(
            candidate=b.candidate, rrf_score=b.rrf_score, rrf_rank=i + 1
        )
        for i, b in enumerate(boosted)
    ]
