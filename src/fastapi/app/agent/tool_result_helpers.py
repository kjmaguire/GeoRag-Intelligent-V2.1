"""Pure-function helpers that operate on tool-result objects.

Extracted from ``app.agent.orchestrator`` in Phase F.7 (see
``docs/master_plan_orchestrator_refactor.md``). These helpers used
to live in ``orchestrator.py`` and are re-exported from there for
backward compatibility.

Module contents (all synchronous, no I/O):

* ``_build_collar_aggregates`` — pre-computes deepest/shallowest/avg/etc.
  over a ``SpatialQueryResult.collars`` list so the LLM doesn't have to
  do arithmetic on small models.
* ``_mmr_select_chunks`` — Max-Marginal-Relevance dedupe over
  ``DocumentChunk`` payloads.
* ``_is_empty_tool_result`` — Phase F.4 helper, returns True when a
  tool result has no rows worth citing.
"""

from __future__ import annotations

from typing import Any

from app.agent.public_geoscience_tool import PublicGeoscienceSearchResult
from app.agent.tools import (
    AssayDataResult,
    CollarDetailsResult,
    CoverageGapResult,
    DocumentSearchResult,
    DownholeLogsResult,
    ProjectOverviewResult,
    ProjectSummaryResult,
    SpatialQueryResult,
)
from app.config import settings


def _build_collar_aggregates(collars: list) -> list[str]:
    """Pre-compute aggregates so the LLM doesn't have to do arithmetic.

    Small local models (qwen2.5:14b) reliably parrot numbers from context
    but cannot compute averages, sort by min/max, or filter-count across
    rows. We compute everything here in Python and inject it as a summary
    block the LLM can quote verbatim.
    """
    if not collars:
        return []

    # Filter out None values per-field — historical projects (e.g. Wyoming
    # uranium historical) have collars with NULL coordinates and/or NULL
    # total_depth. Unfiltered max()/min() over None raises TypeError on
    # Python 3.13 and crashes the agent with INTERNAL_ERROR before the
    # answer can be assembled.
    depth_collars = [c for c in collars if c.total_depth is not None]
    east_collars = [c for c in collars if c.easting is not None]
    north_collars = [c for c in collars if c.northing is not None]

    avg_depth = sum(c.total_depth for c in depth_collars) / len(depth_collars) if depth_collars else None
    deepest = max(depth_collars, key=lambda c: c.total_depth) if depth_collars else None
    shallowest = min(depth_collars, key=lambda c: c.total_depth) if depth_collars else None
    # Ranked by GROUND position (longitude/latitude off geom_4326) whenever
    # every candidate has one (GIS-6, 2026-09-29). easting/northing hold what
    # each source gave — metres in one zone, degrees, feet — so max(easting)
    # across a project that mixes sources compares different grids. The
    # quoted value is still the source easting/northing, which is what the
    # geologist's own tables say.
    def _east_key(c: Any) -> float:
        return float(c.longitude) if lon_ranked else float(c.easting)

    def _north_key(c: Any) -> float:
        return float(c.latitude) if lat_ranked else float(c.northing)

    lon_ranked = bool(east_collars) and all(
        getattr(c, "longitude", None) is not None for c in east_collars
    )
    lat_ranked = bool(north_collars) and all(
        getattr(c, "latitude", None) is not None for c in north_collars
    )
    easternmost = max(east_collars, key=_east_key) if east_collars else None
    westernmost = min(east_collars, key=_east_key) if east_collars else None
    northernmost = max(north_collars, key=_north_key) if north_collars else None
    southernmost = min(north_collars, key=_north_key) if north_collars else None

    # Group by hole_type
    by_type: dict[str, int] = {}
    for c in collars:
        by_type[c.hole_type] = by_type.get(c.hole_type, 0) + 1
    type_breakdown = ", ".join(f"{k}={v}" for k, v in sorted(by_type.items()))

    # Group by status
    by_status: dict[str, int] = {}
    for c in collars:
        by_status[c.status] = by_status.get(c.status, 0) + 1
    status_breakdown = ", ".join(f"{k}={v}" for k, v in sorted(by_status.items()))

    # Group by drill year (from drill_date string)
    by_year: dict[str, int] = {}
    for c in collars:
        if c.drill_date and len(c.drill_date) >= 4:
            year = c.drill_date[:4]
            by_year[year] = by_year.get(year, 0) + 1
    year_breakdown = ", ".join(f"{k}={v}" for k, v in sorted(by_year.items())) or "unknown"

    lines = [
        "",
        "=== PRE-COMPUTED SUMMARY (use these exact values in your answer) ===",
        f"Total drill holes: {len(collars)}",
    ]
    if avg_depth is not None:
        lines.append(f"Average total_depth: {avg_depth:.1f} metres")
    else:
        lines.append("Average total_depth: unknown (no depth values recorded)")
    if deepest is not None:
        lines.append(f"Deepest hole: {deepest.hole_id} at {deepest.total_depth} m")
    if shallowest is not None:
        lines.append(f"Shallowest hole: {shallowest.hole_id} at {shallowest.total_depth} m")
    if easternmost is not None:
        lines.append(f"Easternmost hole: {easternmost.hole_id} at easting={easternmost.easting}")
    if westernmost is not None:
        lines.append(f"Westernmost hole: {westernmost.hole_id} at easting={westernmost.easting}")
    if northernmost is not None:
        lines.append(f"Northernmost hole: {northernmost.hole_id} at northing={northernmost.northing}")
    if southernmost is not None:
        lines.append(f"Southernmost hole: {southernmost.hole_id} at northing={southernmost.northing}")
    if not east_collars and not north_collars:
        lines.append("(No spatial coordinates recorded for any collar in this project.)")
    lines.extend([
        f"Breakdown by hole_type: {type_breakdown}",
        f"Breakdown by status: {status_breakdown}",
        f"Breakdown by drill year: {year_breakdown}",
        "=== END SUMMARY ===",
        "",
    ])
    return lines


def _mmr_select_chunks(chunks: list[Any], lambda_weight: float = 0.7, k: int | None = None) -> list[Any]:
    """Max-Marginal-Relevance selection over document chunks.

    B4: de-duplicates near-identical passages that the cross-encoder reranker
    happily keeps (e.g., a Section 13 resource paragraph that appears in both
    the 2023 and 2024 amendments of the same NI 43-101). Without MMR the top
    5 slots can all be minor variations of the same paragraph.

    Similarity: Jaccard on the lowercased word set of the first 200 chars
    of chunk.text. Cheap, order-insensitive, no second inference hop.
    Score: lambda * relevance - (1 - lambda) * max_similarity_to_selected.

    P1 #17 — the old `len(chunks) < 3` early-return was a "length filter"
    that masked real benefit when the retrieval pool was small (e.g. on a
    fresh project with only 4-5 documents indexed). It's removed: MMR with
    k=1 just returns the most-relevant chunk, k=2 picks the most-relevant
    plus the most-diverse second pick — both are correct behaviours.
    Disabled-by-config still short-circuits.
    """
    if not chunks:
        return []
    if not getattr(settings, "MMR_ENABLED", True):
        return list(chunks)

    k = k if k is not None else len(chunks)

    def _token_set(chunk: Any) -> frozenset[str]:
        txt = (getattr(chunk, "text", "") or "")[:200].lower()
        # Fast word tokeniser — whitespace split is enough for dedupe-grade
        # similarity; punctuation noise is small at 200 chars.
        return frozenset(w for w in txt.split() if len(w) > 2)

    remaining = list(chunks)
    selected: list[Any] = []
    selected_tokens: list[frozenset[str]] = []

    # Seed with the highest-relevance chunk.
    remaining.sort(key=lambda c: -float(getattr(c, "relevance_score", 0.0) or 0.0))
    seed = remaining.pop(0)
    selected.append(seed)
    selected_tokens.append(_token_set(seed))

    while remaining and len(selected) < k:
        best_idx = 0
        best_score = -float("inf")
        for i, cand in enumerate(remaining):
            rel = float(getattr(cand, "relevance_score", 0.0) or 0.0)
            cand_tokens = _token_set(cand)
            # Jaccard similarity to each already-selected chunk; take the max.
            max_sim = 0.0
            for sel_toks in selected_tokens:
                if not cand_tokens or not sel_toks:
                    continue
                inter = len(cand_tokens & sel_toks)
                union = len(cand_tokens | sel_toks)
                sim = inter / union if union else 0.0
                if sim > max_sim:
                    max_sim = sim
            score = lambda_weight * rel - (1.0 - lambda_weight) * max_sim
            if score > best_score:
                best_score = score
                best_idx = i
        chosen = remaining.pop(best_idx)
        selected.append(chosen)
        selected_tokens.append(_token_set(chosen))

    return selected


def _is_empty_tool_result(result: Any) -> bool:
    """Return True when a tool result carries no rows worth citing.

    Phase F.4 — empty tool results must be dropped *before* citation
    assignment so they don't produce zero-relevance citations that trip the
    Layer 1 retrieval_quality gate, and so the LLM's Evidence Set block
    doesn't include a marker for a tool that returned nothing.

    Criteria match the per-type relevance heuristics in
    ``response_assembler._extract_relevance`` — anything that would have
    produced ``0.0`` there is considered empty here:

      * ``AssayDataResult.count == 0``
      * ``DownholeLogsResult.count == 0``
      * ``SpatialQueryResult.count == 0``
      * ``DocumentSearchResult.chunks == []``
      * ``PublicGeoscienceSearchResult.records == []``
      * ``ProjectSummaryResult.count == 0``
      * ``CoverageGapResult.count == 0``

    Unknown / synthetic result objects (e.g. the ``drill_targeting`` text
    blob) are treated as non-empty so we never silently drop a tool the
    assembler doesn't yet special-case.
    """
    if isinstance(result, AssayDataResult):
        return (result.count or 0) == 0
    if isinstance(result, DownholeLogsResult):
        # An empty result is one with NEITHER intervals NOR a collar — the
        # collar alone (hole_type, total_depth, status, drill_date, coords)
        # is enough for the LLM to describe the hole even when no lithology
        # is on file. Wyoming-historical-style collars commonly carry only
        # collar metadata; dropping them entirely makes "tell me about hole
        # X" refuse despite the system having usable data on X.
        return (result.count or 0) == 0 and result.collar is None
    if isinstance(result, SpatialQueryResult):
        return (result.count or 0) == 0
    if isinstance(result, DocumentSearchResult):
        return not result.chunks
    if isinstance(result, PublicGeoscienceSearchResult):
        return not result.records
    if isinstance(result, ProjectOverviewResult):
        # Empty only when the project genuinely has no metadata AND no
        # log curves. The `count` field is the sum of those two halves
        # (see tools.ProjectOverviewResult).
        return (result.count or 0) == 0
    if isinstance(result, CollarDetailsResult):
        # Empty when the hole lookup missed (count=0 → collar_id is None).
        return (result.count or 0) == 0
    if isinstance(result, (ProjectSummaryResult, CoverageGapResult)):
        # `count` is the number of breakdown / coverage rows surfaced. Zero is
        # either a project with nothing to summarise or a failed query; both
        # are empty. Before this branch they fell through to `False`, so an
        # outage was kept as a citable "result" and an answer was built on it.
        # execute_node reads `retrieval_failure` BEFORE the drop, so a failed
        # query still reaches degraded_sources.
        return (result.count or 0) == 0
    return False


__all__ = [
    "_build_collar_aggregates",
    "_mmr_select_chunks",
    "_is_empty_tool_result",
]
