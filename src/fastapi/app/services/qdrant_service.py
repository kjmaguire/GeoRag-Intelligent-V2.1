"""Qdrant hybrid retrieval service -- Query API with RRF fusion.

Module 4 Chunk 2 -- replaces dense-only search() calls with the Qdrant 1.17
Query API (query_points with Prefetch + FusionQuery).

Design
------
hybrid_query() issues a single query_points call with two Prefetch branches:
  1. Dense ANN: cosine similarity against the "" (default) named vector.
  2. Sparse BM25-style: inner product against the "text" SPLADE++ vector.

Qdrant's built-in RRF fuses the two branches server-side, returning a
merged ranked list. The caller then applies the cross-encoder reranker on
top (Layer 1 precision gate).

Workspace filter (GI-9)
------------------------
workspace_id is a mandatory payload filter on every Prefetch branch. Queries
without workspace_id cannot proceed -- this enforces multi-tenant isolation
at the retrieval level before any LLM sees the results.

Dense field name
----------------
The dense vector uses the unnamed default slot "" (empty string). All
georag_reports and pg_* collections were provisioned with this convention
in Module 2. The "" name is Qdrant's convention for the default vector in
a multi-vector collection.

Sparse field name
-----------------
The sparse vector slot is named "text" per the Module 2 / Module 4 Chunk 2
convention. All collections have this slot.

Failure policy (GI-11)
-----------------------
If the sparse encoder raises an exception, the query FAILS. There is no
dense-only fallback. Hybrid retrieval is core V1 (Global Invariant 11).
The caller (orchestrator) handles the exception and surfaces it to the user
rather than silently degrading to dense-only.
"""

from __future__ import annotations

import logging
from uuid import UUID

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    Fusion,
    FusionQuery,
    MatchValue,
    Prefetch,
    ScoredPoint,
    SparseVector,
)

logger = logging.getLogger(__name__)

# Number of candidates fetched per branch before RRF fusion.
# Qdrant fuses the two sets via RRF and returns `limit` final results.
# A wider prefetch improves recall at the cost of more points evaluated.
PREFETCH_LIMIT = 100


async def hybrid_query(
    client: AsyncQdrantClient,
    collection: str,
    query_dense: list[float],
    query_sparse: dict[int, float],
    workspace_id: str | UUID,
    limit: int = 50,
    additional_filter: Filter | None = None,
    sparse_boost_factor: float = 1.0,
) -> list[ScoredPoint]:
    """Run a hybrid dense+sparse query against a Qdrant collection using RRF.

    Issues two Prefetch branches (dense ANN + sparse BM25-style) and asks
    Qdrant's Query API to fuse them with RRF server-side.

    Args:
        client:           Async Qdrant client (from app.state.qdrant_client).
        collection:       Collection name, e.g. "georag_reports".
        query_dense:      Dense query vector (float list, must match collection dim).
        query_sparse:     Sparse query vector as {token_id: weight} dict from
                          SPLADE++ encode_sparse(). Must be non-empty.
        workspace_id:     Workspace UUID for mandatory multi-tenant isolation filter.
                          Applied to BOTH prefetch branches (GI-9).
        limit:            Final number of results to return after RRF fusion.
        additional_filter: Optional extra filter to AND with the workspace_id filter
                          (e.g., project_id scope for project-scoped queries).
        sparse_boost_factor: Multiplier for the sparse prefetch limit (default 1.0).
                          Pass SPARSE_BOOST_FACTOR (1.5) from identifier_boost when
                          an identifier is detected in the query -- widens the sparse
                          candidate pool so exact-token hits from SPLADE++ rank higher
                          in the cross-store RRF pool.  Dense prefetch is unchanged.

    Returns:
        List of ScoredPoint sorted by RRF-fused score descending.
        May be empty if no documents match the filters.

    Raises:
        Exception: Propagates Qdrant client exceptions upward. Caller wraps
                   with asyncio.wait_for for timeout enforcement.
    """
    ws_str = str(workspace_id)

    # Mandatory workspace_id filter -- applied to both prefetch branches.
    ws_filter = Filter(
        must=[
            FieldCondition(
                key="workspace_id",
                match=MatchValue(value=ws_str),
            )
        ]
    )

    # Compose the branch filter: workspace_id AND (optional extra conditions).
    if additional_filter is not None:
        branch_filter = Filter(
            must=[
                ws_filter,
                additional_filter,
            ]
        )
    else:
        branch_filter = ws_filter

    # Sort sparse indices to ensure deterministic serialization.
    sorted_indices = sorted(query_sparse.keys())
    sorted_values = [query_sparse[i] for i in sorted_indices]

    # Identifier-boost: widen the sparse prefetch pool when a geological
    # identifier was detected in the query (B3).  Dense branch is unchanged.
    sparse_prefetch_limit = max(1, int(PREFETCH_LIMIT * sparse_boost_factor))

    result = await client.query_points(
        collection_name=collection,
        prefetch=[
            # Branch 1: dense ANN using the "" (default) named vector.
            Prefetch(
                query=query_dense,
                using="",
                limit=PREFETCH_LIMIT,
                filter=branch_filter,
            ),
            # Branch 2: sparse BM25-style using the "text" SPLADE++ slot.
            # Limit is optionally widened via sparse_boost_factor (B3).
            Prefetch(
                query=SparseVector(
                    indices=sorted_indices,
                    values=sorted_values,
                ),
                using="text",
                limit=sparse_prefetch_limit,
                filter=branch_filter,
            ),
        ],
        query=FusionQuery(fusion=Fusion.RRF),
        limit=limit,
        with_payload=True,
    )

    logger.debug(
        "hybrid_query: collection=%s workspace=%s returned=%d points",
        collection,
        ws_str[:8],
        len(result.points),
    )

    return result.points


async def hybrid_query_no_workspace(
    client: AsyncQdrantClient,
    collection: str,
    query_dense: list[float],
    query_sparse: dict[int, float],
    limit: int = 50,
    additional_filter: Filter | None = None,
    sparse_boost_factor: float = 1.0,
) -> list[ScoredPoint]:
    """Hybrid query WITHOUT workspace_id filter.

    Use ONLY for public/open-access collections (pg_* public geoscience)
    where all data is workspace-agnostic. For tenant-scoped collections
    (georag_reports), always use hybrid_query() with workspace_id.

    This is NOT a fallback -- it is for explicitly public data.

    Args:
        sparse_boost_factor: Multiplier for sparse prefetch limit (B3 identifier boost).
                             Default 1.0 (no boost). Pass 1.5 when an identifier is
                             detected in the query.
    """
    sorted_indices = sorted(query_sparse.keys())
    sorted_values = [query_sparse[i] for i in sorted_indices]

    sparse_prefetch_limit = max(1, int(PREFETCH_LIMIT * sparse_boost_factor))

    result = await client.query_points(
        collection_name=collection,
        prefetch=[
            Prefetch(
                query=query_dense,
                using="",
                limit=PREFETCH_LIMIT,
                filter=additional_filter,
            ),
            Prefetch(
                query=SparseVector(
                    indices=sorted_indices,
                    values=sorted_values,
                ),
                using="text",
                limit=sparse_prefetch_limit,
                filter=additional_filter,
            ),
        ],
        query=FusionQuery(fusion=Fusion.RRF),
        limit=limit,
        with_payload=True,
    )

    return result.points
