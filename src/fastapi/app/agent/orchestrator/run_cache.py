"""Phase F.14 — Retrieval cache + data-version helpers extracted from
``orchestrator/__init__.py``.

This module owns:

* :func:`fetch_data_versions` — single PG round-trip that returns
  ``(workspace_data_version, project_data_version)``. Used by the
  cache key (so a Dagster ingestion bump produces an automatic
  cache miss) and by ``answer_runs`` freshness fields.
* :func:`cache_key` — deterministic Redis key builder. v6 prefix
  includes ``_SYSTEM_PROMPT_VERSION`` so any prompt edit busts the
  cache automatically.
* :func:`build_cached_candidates` — convert the orchestrator's
  ``_fused_candidates`` (a list of ``ScoredCandidate``) into the
  list-of-``CachedRetrievalCandidate`` shape Redis stores.
* :func:`build_cached_context` — assemble a full
  :class:`CachedRetrievalContext` from current-run state.
* :func:`rehydrate_tool_results` — **Phase H new path**. Reconstruct
  ``tool_results`` (the ``list[tuple[tool_name, dataclass]]`` shape
  ``_build_context`` consumes) from a cache hit's
  ``candidates_reranked``. Closes the design-incomplete state that
  forced ``RETRIEVAL_CACHE_ENABLED=False`` overnight.

Re-export contract
------------------
``orchestrator/__init__.py`` imports + re-exports ``_cache_key`` and
``_fetch_data_versions`` here so the 19 production callers and the
test suite keep working without touching their import lines.
"""
from __future__ import annotations

import dataclasses as _dc
import hashlib
import json as _json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from app.agent.tools import (
        CollarRecord,
        DocumentChunk,
    )
    from app.models.retrieval_cache import (
        CachedRetrievalCandidate,
        CachedRetrievalContext,
    )

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Per-query data versions
# ----------------------------------------------------------------------------


async def fetch_data_versions(
    pg_pool: Any,
    workspace_id: str | None,
    project_id: str | None,
) -> tuple[int, int | None]:
    """Fetch workspace + project data_version from PostGIS in one round-trip.

    Returns ``(workspace_data_version, project_data_version)``. Degrades
    gracefully on DB unavailability: returns ``(0, None)`` so the cache
    key includes a version field — one that will simply always miss,
    which is the correct safe-default.

    A hot-cache optimisation (Redis-backed version lookup) is
    deliberately deferred until telemetry shows this is a latency
    concern. At current scale (single PgBouncer, < 100 concurrent
    users) the round-trip is sub-millisecond.
    """
    if pg_pool is None:
        return 0, None

    try:
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT
                    w.data_version  AS ws_version,
                    p.data_version  AS proj_version
                FROM silver.workspaces w
                LEFT JOIN silver.projects p
                    ON p.project_id = $2::uuid
                    AND p.workspace_id = w.workspace_id
                WHERE w.workspace_id = $1::uuid
                """,
                workspace_id,
                project_id,
            )
        if row is None:
            logger.warning(
                "fetch_data_versions: workspace_id=%s not found; "
                "degrading to version=0",
                workspace_id,
            )
            return 0, None
        ws_version: int = int(row["ws_version"] or 0)
        proj_version: int | None = (
            int(row["proj_version"])
            if row["proj_version"] is not None
            else None
        )
        return ws_version, proj_version
    except Exception as exc:
        logger.warning(
            "fetch_data_versions: DB lookup failed (workspace=%s "
            "project=%s): %s — degrading to version=0 (cache will miss, "
            "which is safe)",
            workspace_id,
            project_id,
            exc,
        )
        return 0, None


# ----------------------------------------------------------------------------
# Cache key construction
# ----------------------------------------------------------------------------


def cache_key(
    query: str,
    project_id: str,
    *,
    system_prompt_version: int,
    categories: dict[str, Any] | None = None,
    workspace_data_version: int = 0,
    project_data_version: int | None = None,
    workspace_id: str | None = None,
) -> str:
    """Build a Redis cache key for a query+project+versions tuple.

    Key construction per addendum §05d:

        sha256(query_hash | workspace_id | workspace_data_version |
               project_data_version | retrieval_strategy_version |
               system_prompt_version | filters_hash | rbac_scope_hash | cats)

    ``workspace_data_version`` / ``project_data_version`` MUST be live
    values from ``silver.workspaces`` / ``silver.projects`` immediately
    before this call — NOT a config constant. A Dagster ingestion run
    bumps data_version and automatically produces a cache miss
    (new version → new key), which is the correct freshness behaviour
    (Global Invariant 12).

    ``system_prompt_version`` is the orchestrator's
    ``_SYSTEM_PROMPT_VERSION`` constant. Passed in by the caller rather
    than imported here to keep the cache-key surface independent of
    the orchestrator's prompt-version cadence (avoids an import cycle).
    """
    # Late import to avoid forcing query_classifier load at module init.
    from app.services.query_classifier import RETRIEVAL_STRATEGY_VERSION  # noqa: PLC0415

    # Sentinels for not-yet-implemented Module 9 scope fields.
    filters_hash = ""
    rbac_scope_hash = ""

    normalised = query.strip().lower()
    if categories:
        cache_inputs = {
            "q":    normalised,
            "wid":  workspace_id or "",
            "pid":  project_id,
            "wdv":  workspace_data_version,
            "pdv":  project_data_version if project_data_version is not None else "",
            "rsv":  RETRIEVAL_STRATEGY_VERSION,
            "spv":  system_prompt_version,
            "fh":   filters_hash,
            "rh":   rbac_scope_hash,
            "cats": {
                k: (sorted(v) if isinstance(v, list) else v)
                for k, v in sorted(categories.items())
                if k != "downhole_hole_ids"
            },
        }
        raw = _json.dumps(cache_inputs, sort_keys=True, ensure_ascii=False)
        h = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return f"georag:rag_cache:v6:{h}"

    # Back-compat path (no categories).
    raw_back = (
        f"{workspace_id or ''}:{project_id}:{normalised}:"
        f"{workspace_data_version}:{project_data_version or ''}:"
        f"{system_prompt_version}"
    )
    h = hashlib.sha256(raw_back.encode()).hexdigest()[:16]
    return f"georag:rag_cache:v6:{h}"


# ----------------------------------------------------------------------------
# CachedRetrievalContext write helpers
# ----------------------------------------------------------------------------


def build_cached_candidates(
    fused_candidates: list[Any],
) -> list[CachedRetrievalCandidate]:
    """Convert the orchestrator's _fused_candidates list to the cache shape.

    ``fused_candidates`` is a list of ``ScoredCandidate`` (from
    ``app.services.fusion``). Each wraps a ``Candidate`` whose
    ``.payload`` carries the original tool-result dataclass instance.

    Phase H — for postgis / neo4j candidates we now ALSO serialise the
    full payload dict via ``dataclasses.asdict``, not just a
    ``{store, canonical_id}`` ref. This is what unlocks the
    :func:`rehydrate_tool_results` path: without the full payload the
    rehydration would have to re-query the DB, which defeats the
    cache's purpose.
    """
    from app.models.retrieval_cache import CachedRetrievalCandidate  # noqa: PLC0415

    out: list[CachedRetrievalCandidate] = []
    for sc in fused_candidates:
        cand = sc.candidate
        text = ""
        passage_id: UUID | None = None
        cand_ref: dict | None = None
        payload_for_cache: dict | None = None

        if cand.store == "qdrant":
            payload_obj = cand.payload
            text = (
                getattr(payload_obj, "text", "")
                or getattr(payload_obj, "content", "")
                or ""
            )
            chunk_id_str = getattr(payload_obj, "chunk_id", None)
            if chunk_id_str:
                try:
                    passage_id = UUID(str(chunk_id_str))
                except (ValueError, AttributeError):
                    cand_ref = {"chunk_id": str(chunk_id_str)}
            try:
                payload_for_cache = (
                    _dc.asdict(payload_obj)
                    if _dc.is_dataclass(payload_obj)
                    else None
                )
            except Exception:
                pass
        elif cand.store in ("neo4j", "postgis"):
            payload_obj = cand.payload
            text = str(
                getattr(payload_obj, "name", None)
                or getattr(payload_obj, "hole_id", None)
                or cand.canonical_id
            )
            cand_ref = {
                "store":        cand.store,
                "canonical_id": cand.canonical_id,
            }
            # Phase H — capture the full dataclass payload so the
            # rehydration path can reconstruct the original tool result
            # without re-querying. Falls back to None when payload isn't
            # a dataclass (graph entity wrappers etc.).
            try:
                payload_for_cache = (
                    _dc.asdict(payload_obj)
                    if _dc.is_dataclass(payload_obj)
                    else None
                )
            except Exception:
                pass

        out.append(
            CachedRetrievalCandidate(
                source_store=cand.store,
                passage_id=passage_id,
                candidate_ref=cand_ref,
                text=text or "[no text]",
                retriever_score=cand.score,
                reranker_score=None,
                rrf_rank=sc.rrf_rank,
                rrf_score=sc.rrf_score,
                payload=payload_for_cache,
            )
        )
    return out


# Tool names whose results are NOT in the RRF candidate pool but DO
# contribute to synthesis. These get serialised into the
# `auxiliary_tool_results` slot of CachedRetrievalContext.
#
# `search_public_geoscience` IS partly in the RRF pool (the candidate
# records carry partial info) but the full `PublicGeoscienceSearchResult`
# wrapper (with jurisdictions_queried, canonical_types_queried,
# data_source telemetry) is lost on the candidate-only roundtrip.
# Phase H3 added it here so the full result dataclass roundtrips
# cleanly alongside the candidate-level entries.
_AUXILIARY_TOOL_NAMES: tuple[str, ...] = (
    "query_project_overview",
    "query_downhole_logs",
    "query_assay_data",
    "drill_targeting",
    "search_public_geoscience",
)


def build_auxiliary_tool_results(
    tool_results: list[tuple[str, Any]],
) -> dict[str, Any]:
    """Phase H — serialise auxiliary tool results for the cache.

    The orchestrator's ``tool_results`` list contains items of the
    form ``(tool_name, result_dataclass)``. The RRF candidate pool only
    covers ``query_spatial_collars`` + ``search_documents`` +
    ``search_public_geoscience`` + graph traversal. Tools that produce
    answers WITHOUT going through RRF (project_overview, downhole,
    assay, targeting) need their own cache slot.

    Returns a dict keyed by tool name, valued by the
    ``dataclasses.asdict`` representation of the result. Skipped
    silently when the result isn't a dataclass (e.g. a plain list).
    """
    out: dict[str, Any] = {}
    for tool_name, result in tool_results:
        if tool_name not in _AUXILIARY_TOOL_NAMES:
            continue
        if _dc.is_dataclass(result):
            try:
                out[tool_name] = _dc.asdict(result)
            except Exception:
                logger.debug(
                    "build_auxiliary_tool_results: asdict failed for %s",
                    tool_name,
                    exc_info=True,
                )
        elif isinstance(result, list):
            # drill_targeting returns list[TargetRecommendation].
            try:
                out[tool_name] = [
                    _dc.asdict(item) if _dc.is_dataclass(item) else item
                    for item in result
                ]
            except Exception:
                logger.debug(
                    "build_auxiliary_tool_results: list asdict failed "
                    "for %s",
                    tool_name,
                    exc_info=True,
                )
    return out


def build_cached_context(
    *,
    workspace_id: str,
    project_id: str | None,
    workspace_data_version: int,
    project_data_version: int | None,
    query_class: str,
    sparse_boost_applied: bool,
    embedding_model_version: str,
    sparse_model_version: str,
    reranker_version: str | None,
    partial_failures: list[tuple[str, str]] | None,
    fused_candidates: list[Any],
    tool_results: list[tuple[str, Any]] | None = None,
) -> CachedRetrievalContext:
    """Assemble a full CachedRetrievalContext ready for SETEX.

    Phase H — added ``tool_results`` parameter so the
    auxiliary-tool-result slot gets populated alongside
    ``candidates_reranked``. Callers that don't have non-RRF tool
    results to cache (none of project_overview / downhole / assay /
    targeting fired) can omit it.
    """
    from app.models.retrieval_cache import CachedRetrievalContext  # noqa: PLC0415
    from app.services.query_classifier import (  # noqa: PLC0415
        RETRIEVAL_STRATEGY_VERSION,
    )

    pf_dict: dict | None = None
    if partial_failures:
        pf_dict = {tn: ec for tn, ec in partial_failures}

    auxiliary = (
        build_auxiliary_tool_results(tool_results)
        if tool_results else {}
    )

    return CachedRetrievalContext(
        cached_at=datetime.now(UTC),
        workspace_id=UUID(workspace_id),
        project_id=UUID(project_id) if project_id else None,
        workspace_data_version_at_cache=workspace_data_version,
        project_data_version_at_cache=project_data_version,
        query_class=query_class,
        sparse_boost_applied=sparse_boost_applied,
        fusion_method="rrf",
        retrieval_strategy_version=RETRIEVAL_STRATEGY_VERSION,
        embedding_model_version=embedding_model_version,
        sparse_model_version=sparse_model_version,
        reranker_version=reranker_version,
        partial_failure_details=pf_dict,
        candidates_reranked=build_cached_candidates(fused_candidates),
        auxiliary_tool_results=auxiliary,
    )


# ----------------------------------------------------------------------------
# Phase H — Rehydration: rebuild tool_results from a cache hit
# ----------------------------------------------------------------------------


def _coerce_collar(payload: dict) -> CollarRecord | None:
    """Reconstruct a CollarRecord from its dataclass-asdict payload."""
    from app.agent.tools import CollarRecord  # noqa: PLC0415

    if not isinstance(payload, dict):
        return None
    try:
        # CollarRecord is a dataclass — let Python complain if the
        # fields don't line up rather than us silently dropping data.
        return CollarRecord(**{
            f.name: payload.get(f.name)
            for f in _dc.fields(CollarRecord)
            if f.name in payload
        })
    except (TypeError, ValueError) as exc:
        logger.warning(
            "rehydrate_tool_results: CollarRecord reconstruction failed: %s",
            exc,
        )
        return None


def _coerce_document_chunk(payload: dict) -> DocumentChunk | None:
    """Reconstruct a DocumentChunk from its dataclass-asdict payload."""
    from app.agent.tools import DocumentChunk  # noqa: PLC0415

    if not isinstance(payload, dict):
        return None
    try:
        return DocumentChunk(**{
            f.name: payload.get(f.name)
            for f in _dc.fields(DocumentChunk)
            if f.name in payload
        })
    except (TypeError, ValueError) as exc:
        logger.warning(
            "rehydrate_tool_results: DocumentChunk reconstruction failed: %s",
            exc,
        )
        return None


def _coerce_dataclass(payload: dict | None, cls: type) -> Any | None:
    """Rebuild any dataclass instance from its ``dataclasses.asdict`` form.

    Skips fields not present in the payload (graceful degradation when
    the cache was written under an older schema). Returns None on
    construction failure.
    """
    if payload is None or not isinstance(payload, dict):
        return None
    try:
        return cls(**{
            f.name: payload.get(f.name)
            for f in _dc.fields(cls)
            if f.name in payload
        })
    except (TypeError, ValueError) as exc:
        logger.warning(
            "_coerce_dataclass: %s reconstruction failed: %s",
            cls.__name__, exc,
        )
        return None


def _rehydrate_auxiliary(
    auxiliary: dict[str, Any] | None,
) -> list[tuple[str, Any]]:
    """Rebuild non-RRF tool results from CachedRetrievalContext.auxiliary_tool_results.

    Skips any auxiliary entry whose payload doesn't round-trip cleanly
    (e.g. cache written before the field existed, or schema drift).
    Returns the same ``(tool_name, result_dataclass)`` shape as the
    main rehydration path.
    """
    from app.agent.tools import (  # noqa: PLC0415
        AssayDataResult,
        AssaySample,
        DownholeLogsResult,
        LithologyInterval,
        ProjectOverviewResult,
    )

    if not auxiliary:
        return []

    out: list[tuple[str, Any]] = []

    # query_project_overview — flat dataclass, easy roundtrip.
    if "query_project_overview" in auxiliary:
        po = _coerce_dataclass(
            auxiliary["query_project_overview"], ProjectOverviewResult,
        )
        if po is not None:
            out.append(("query_project_overview", po))

    # query_downhole_logs — nested: CollarRecord + list[LithologyInterval].
    if "query_downhole_logs" in auxiliary:
        from app.agent.tools import CollarRecord  # noqa: PLC0415
        payload = auxiliary["query_downhole_logs"]
        if isinstance(payload, dict):
            collar_dict = payload.get("collar")
            collar = (
                _coerce_dataclass(collar_dict, CollarRecord)
                if collar_dict else None
            )
            interval_dicts = payload.get("intervals") or []
            intervals: list[LithologyInterval] = []
            for itv in interval_dicts:
                rebuilt = _coerce_dataclass(itv, LithologyInterval)
                if rebuilt is not None:
                    intervals.append(rebuilt)
            try:
                out.append(("query_downhole_logs", DownholeLogsResult(
                    collar=collar,
                    intervals=intervals,
                    count=int(payload.get("count", len(intervals))),
                    data_source=str(
                        payload.get("data_source",
                                    "PostGIS silver.lithology_logs (cache-rehydrated)")
                    ),
                )))
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "_rehydrate_auxiliary: DownholeLogsResult build failed: %s",
                    exc,
                )

    # query_assay_data — nested: list[AssaySample] + aggregates.
    if "query_assay_data" in auxiliary:
        payload = auxiliary["query_assay_data"]
        if isinstance(payload, dict):
            sample_dicts = payload.get("samples") or []
            samples: list[AssaySample] = []
            for sd in sample_dicts:
                rebuilt = _coerce_dataclass(sd, AssaySample)
                if rebuilt is not None:
                    samples.append(rebuilt)
            try:
                out.append(("query_assay_data", AssayDataResult(
                    samples=samples,
                    count=int(payload.get("count", len(samples))),
                    element=str(payload.get("element", "")),
                    available_elements=list(payload.get("available_elements") or []),
                    min_value=payload.get("min_value"),
                    max_value=payload.get("max_value"),
                    mean_value=payload.get("mean_value"),
                    median_value=payload.get("median_value"),
                    data_source=str(
                        payload.get("data_source",
                                    "PostGIS silver.samples (cache-rehydrated)")
                    ),
                )))
            except (TypeError, ValueError) as exc:
                logger.warning(
                    "_rehydrate_auxiliary: AssayDataResult build failed: %s",
                    exc,
                )

    # search_public_geoscience — nested: list[PublicGeoscienceRecord]
    # + jurisdictions/canonical_types/commodities query metadata. The
    # records all carry simple primitive / list / dict fields so they
    # round-trip cleanly via _coerce_dataclass.
    if "search_public_geoscience" in auxiliary:
        try:
            from app.agent.public_geoscience_tool import (  # noqa: PLC0415
                PublicGeoscienceRecord,
                PublicGeoscienceSearchResult,
            )
        except ImportError:
            logger.debug(
                "_rehydrate_auxiliary: public_geo module unavailable"
            )
        else:
            payload = auxiliary["search_public_geoscience"]
            if isinstance(payload, dict):
                record_dicts = payload.get("records") or []
                records: list[PublicGeoscienceRecord] = []
                for rd in record_dicts:
                    rebuilt = _coerce_dataclass(rd, PublicGeoscienceRecord)
                    if rebuilt is not None:
                        records.append(rebuilt)
                try:
                    out.append(("search_public_geoscience",
                        PublicGeoscienceSearchResult(
                            records=records,
                            count=int(payload.get("count", len(records))),
                            jurisdictions_queried=list(
                                payload.get("jurisdictions_queried") or []
                            ),
                            canonical_types_queried=list(
                                payload.get("canonical_types_queried") or []
                            ),
                            data_source=str(
                                payload.get(
                                    "data_source",
                                    "Qdrant public_geo.* "
                                    "(cache-rehydrated)",
                                )
                            ),
                        )))
                except (TypeError, ValueError) as exc:
                    logger.warning(
                        "_rehydrate_auxiliary: "
                        "PublicGeoscienceSearchResult build failed: %s",
                        exc,
                    )

    # drill_targeting — list[TargetRecommendation]. The dataclass lives
    # under app.agent.drill_targeting; import lazily so rehydration
    # doesn't pay the load cost when targeting wasn't used.
    if "drill_targeting" in auxiliary:
        try:
            from app.agent.drill_targeting import TargetRecommendation  # noqa: PLC0415
            payload = auxiliary["drill_targeting"]
            if isinstance(payload, list):
                rec_list: list[TargetRecommendation] = []
                for rec in payload:
                    rebuilt = _coerce_dataclass(rec, TargetRecommendation)
                    if rebuilt is not None:
                        rec_list.append(rebuilt)
                if rec_list:
                    out.append(("drill_targeting", rec_list))
        except ImportError:
            logger.debug(
                "_rehydrate_auxiliary: drill_targeting module unavailable"
            )

    if out:
        logger.info(
            "_rehydrate_auxiliary: rebuilt %d auxiliary tool result(s): %s",
            len(out), [n for (n, _) in out],
        )
    return out


def rehydrate_tool_results(
    cached_ctx: CachedRetrievalContext,
) -> list[tuple[str, Any]]:
    """Rebuild ``tool_results`` from a cache hit's ``candidates_reranked``
    AND ``auxiliary_tool_results``.

    Returns the same ``list[tuple[tool_name, tool_result_dataclass]]``
    shape that ``run_deterministic_rag`` accumulates on a cache miss
    via its ``parallel_branches`` block. Callers (the orchestrator's
    cache-hit path) can feed the output directly to
    ``_build_context`` / ``assemble_response``.

    Grouping rules:
      ``source_store == "qdrant"``   →  one ``DocumentSearchResult``
        named ``search_documents``. The chunks are reconstructed from
        each candidate's ``payload`` dict via the
        ``DocumentChunk`` dataclass.
      ``source_store == "postgis"``  →  one ``SpatialQueryResult``
        named ``query_spatial_collars``. Collars are reconstructed
        from each candidate's ``payload`` dict via ``CollarRecord``.
      ``source_store == "neo4j"``    →  *skipped* (no clean
        round-trip — graph entities are pydantic-AI wrappers, not
        dataclasses). The orchestrator's classifier will re-fire the
        graph branch on the next run; rehydration is best-effort.

    Plus auxiliary rehydration via :func:`_rehydrate_auxiliary` for
    project_overview / downhole / assay / targeting tool results
    (Phase H continued).

    Returns an empty list when the cached context has NO candidates
    AND NO auxiliary results — callers should fall back to treating
    the hit as a miss and running fresh retrieval.
    """
    from app.agent.tools import (  # noqa: PLC0415
        DocumentSearchResult,
        SpatialQueryResult,
    )

    # Auxiliary rehydration runs regardless of candidates_reranked
    # state — a query may have hit project_overview alone (no RRF).
    auxiliary_out = _rehydrate_auxiliary(
        getattr(cached_ctx, "auxiliary_tool_results", None)
    )

    if not cached_ctx.candidates_reranked:
        return auxiliary_out

    qdrant_chunks: list[DocumentChunk] = []
    postgis_collars: list[CollarRecord] = []
    skipped_neo4j = 0

    for cand in cached_ctx.candidates_reranked:
        store = cand.source_store
        payload = cand.payload
        if store == "qdrant":
            ch = _coerce_document_chunk(payload) if payload else None
            if ch is not None:
                qdrant_chunks.append(ch)
        elif store == "postgis":
            cl = _coerce_collar(payload) if payload else None
            if cl is not None:
                postgis_collars.append(cl)
        elif store == "neo4j":
            skipped_neo4j += 1
        # Unknown stores: silently skip — the cache may have entries
        # written by a newer codepath we don't recognise yet.

    out: list[tuple[str, Any]] = []
    if qdrant_chunks:
        out.append((
            "search_documents",
            DocumentSearchResult(
                chunks=qdrant_chunks,
                count=len(qdrant_chunks),
                data_source="Qdrant (cache-rehydrated)",
            ),
        ))
    if postgis_collars:
        out.append((
            "query_spatial_collars",
            SpatialQueryResult(
                collars=postgis_collars,
                count=len(postgis_collars),
                data_source="PostGIS silver.collars (cache-rehydrated)",
            ),
        ))

    if skipped_neo4j:
        logger.info(
            "rehydrate_tool_results: skipped %d neo4j candidate(s) "
            "(graph entities don't round-trip via payload dict — the "
            "orchestrator's graph branch must re-run on this query)",
            skipped_neo4j,
        )

    # Phase H continued — append auxiliary tool results AFTER the
    # RRF-store-derived entries. The orchestrator's downstream
    # consumers (citation_id assignment, _build_context) walk
    # tool_results in order to build the EVIDENCE block; placing
    # auxiliary entries after the canonical retrieval order preserves
    # the existing context shape.
    out.extend(auxiliary_out)

    logger.info(
        "rehydrate_tool_results: rebuilt %d tool result(s) from cache "
        "(qdrant_chunks=%d, postgis_collars=%d, auxiliary=%d)",
        len(out), len(qdrant_chunks), len(postgis_collars),
        len(auxiliary_out),
    )
    return out


__all__ = [
    "fetch_data_versions",
    "cache_key",
    "build_cached_candidates",
    "build_auxiliary_tool_results",
    "build_cached_context",
    "rehydrate_tool_results",
]
