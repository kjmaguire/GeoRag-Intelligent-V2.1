"""Evidence inspector endpoint — Module 6 Phase B Chunk 4a (spec B6).

GET /v1/evidence/{evidence_id}
------------------------------
Returns a type-branched evidence payload for the Module 7 inspector UI.
Each evidence_type (document_passage, structured_record, graph_edge,
map_feature) returns a distinct Pydantic model with fields appropriate
for that type.

Auth
----
Reuses the existing X-Service-Key + JWT pattern from queries.py:
  - X-Service-Key header validated via ``verify_service_key`` dependency.
  - Workspace scope resolved from JWT sub-claim (``extract_user_context``).
  - RBAC: evidence_id must belong to the caller's workspace_id. Cross-tenant
    access returns 404 (not 403) to prevent existence enumeration.

Module 9 will harden auth further — this endpoint has workspace-boundary
enforcement but no row-level permission check beyond workspace_id.

Performance target: ≤500ms p95 per spec B6.
  - No LLM calls.
  - At most 2 DB round-trips (evidence_items + one hydration query).
  - Neo4j queries are ≤2s each (asyncio.wait_for enforced).
  - All DB errors return 500 {detail: "evidence_fetch_failed"}, logged
    with evidence_id for post-hoc debugging.

Architecture references
-----------------------
  Module spec 06-citation-hallucination-guards.md §6 B6
  georag-architecture-addendum-v1.10.html §04j (evidence model shapes)
  Section 04i hallucination prevention (evidence inspector read path)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, Field

from app.services.auth import UserContext, extract_user_context, verify_service_key

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/evidence",
    tags=["evidence"],
    dependencies=[Depends(verify_service_key)],
)

# ---------------------------------------------------------------------------
# Neo4j timeout (per Section 06 spec: Neo4j ≤3s per query)
# ---------------------------------------------------------------------------

_NEO4J_TIMEOUT_S = 2.0  # tight — don't let a slow graph query hang the inspector


# ---------------------------------------------------------------------------
# Workspace resolver
# ---------------------------------------------------------------------------


# Module 9 Chunk 9.4 (A2-04) — _resolve_workspace_id replaced by the shared
# helper in app.services.workspace_resolution. The default-UUID fallback is
# gone; missing workspace context now returns HTTP 403.
import contextlib  # noqa: E402

from app.services.workspace_resolution import resolve_workspace_id  # noqa: E402, F401

# ---------------------------------------------------------------------------
# Payload models — one per evidence_type
# ---------------------------------------------------------------------------


class EvidencePassagePayload(BaseModel):
    """Payload for evidence_type='document_passage'.

    Hydrated from silver.document_passages + silver.document_revisions.
    context_before / context_after are adjacent passage texts (ordinal ±1).
    deep_link is a Laravel document-viewer URL (None when ambiguous).
    """

    evidence_type: Literal["document_passage"]
    evidence_id: UUID
    passage_text: str
    context_before: str = Field(
        "", description="Up to 2 sentences before the passage in the same revision"
    )
    context_after: str = Field(
        "", description="Up to 2 sentences after the passage in the same revision"
    )
    document_revision_id: UUID
    source_uri: str
    source_date: str | None = None
    page: int | None = None
    deep_link: str | None = Field(
        default=None,
        description=(
            "URL pattern for the source document viewer. "
            "Pattern: /api/v1/documents/view?bronze_uri=<uri>&page=<page>"
        ),
    )
    workspace_id: UUID


class EvidenceStructuredPayload(BaseModel):
    """Payload for evidence_type='structured_record'.

    structured_ref is the opaque JSONB from evidence_items (schema+table+PK).
    lineage is resolved from silver.structured_record_lineage if present.
    """

    evidence_type: Literal["structured_record"]
    evidence_id: UUID
    structured_ref: dict[str, Any] = Field(
        ..., description="Opaque schema+table+PK tuple from evidence_items.structured_ref"
    )
    lineage: dict[str, Any] | None = Field(
        default=None,
        description="Lineage row from silver.structured_record_lineage, if present",
    )
    bronze_uri: str | None = None
    parser_name: str | None = None
    parser_version: str | None = None
    ingestion_run_id: UUID | None = None
    workspace_id: UUID


class EvidenceGraphEdgePayload(BaseModel):
    """Payload for evidence_type='graph_edge'.

    graph_edge_ref is the raw JSONB. start_node_* / end_node_* are hydrated
    from Neo4j (best-effort — absent when Neo4j is unavailable).
    described_in lists DocumentRevision nodes connected to the edge nodes.
    """

    evidence_type: Literal["graph_edge"]
    evidence_id: UUID
    graph_edge_ref: dict[str, Any] = Field(
        ..., description="start_node_id + end_node_id + rel_type from evidence_items"
    )
    start_node_labels: list[str] | None = None
    start_node_preview: dict[str, Any] | None = Field(
        default=None,
        description='{"primary_property": "value"} from Neo4j node properties',
    )
    end_node_labels: list[str] | None = None
    end_node_preview: dict[str, Any] | None = None
    described_in: list[dict[str, Any]] | None = Field(
        default=None,
        description="DocumentRevision nodes connected to the edge endpoints (best-effort)",
    )
    workspace_id: UUID


class EvidenceMapFeaturePayload(BaseModel):
    """Payload for evidence_type='map_feature'.

    map_feature_ref is the raw JSONB. Tile rendering is Module 8's job;
    this endpoint only exposes the feature metadata.
    """

    evidence_type: Literal["map_feature"]
    evidence_id: UUID
    map_feature_ref: dict[str, Any] = Field(
        ..., description="tile_function + feature properties + bbox from evidence_items"
    )
    tile_function: str | None = None
    bbox: list[float] | None = Field(
        default=None, description="[minx, miny, maxx, maxy] in EPSG:4326"
    )
    feature_properties: dict[str, Any] | None = None
    workspace_id: UUID


# Annotated union for OpenAPI discriminated union serialisation.
EvidencePayload = (
    EvidencePassagePayload
    | EvidenceStructuredPayload
    | EvidenceGraphEdgePayload
    | EvidenceMapFeaturePayload
)


# ---------------------------------------------------------------------------
# DB helpers — PostgreSQL
# ---------------------------------------------------------------------------


async def _fetch_evidence_row(
    pg_pool: object, evidence_id: UUID, workspace_id: UUID
) -> dict[str, Any] | None:
    """Fetch one silver.evidence_items row scoped to workspace_id.

    Returns None on:
      - No row found (404)
      - Row belongs to a different workspace (404 — silent to prevent enumeration)
    """
    sql = """
        SELECT
            evidence_id,
            workspace_id,
            evidence_type,
            passage_id,
            structured_ref,
            graph_edge_ref,
            map_feature_ref,
            source_uri,
            source_date,
            linked_node_ids,
            created_at
        FROM silver.evidence_items
        WHERE evidence_id = $1 AND workspace_id = $2
    """
    try:
        async with pg_pool.acquire() as conn:  # type: ignore[union-attr]
            row = await conn.fetchrow(sql, evidence_id, workspace_id)
        if row is None:
            return None
        return dict(row)
    except Exception:
        logger.exception(
            "evidence: DB fetch failed evidence_id=%s", evidence_id
        )
        raise


async def _fetch_passage_with_context(
    pg_pool: object, passage_id: UUID
) -> dict[str, Any]:
    """Fetch passage text + adjacent passage context + document_revision metadata.

    Grabs ordinal ±1 rows from the same document revision for context.
    """
    # Fetch the target passage and its document_revision_id + ordinal.
    passage_sql = """
        SELECT
            dp.passage_id,
            dp.document_revision_id,
            dp.ordinal,
            dp.passage_text,
            dp.page_number,
            dr.source_uri,
            dr.source_date
        FROM silver.document_passages dp
        JOIN silver.document_revisions dr
            ON dr.document_revision_id = dp.document_revision_id
        WHERE dp.passage_id = $1
    """
    # Fetch context passages (ordinal ±1).
    context_sql = """
        SELECT ordinal, passage_text
        FROM silver.document_passages
        WHERE document_revision_id = $1
          AND ordinal IN ($2, $3)
        ORDER BY ordinal
    """
    try:
        async with pg_pool.acquire() as conn:  # type: ignore[union-attr]
            row = await conn.fetchrow(passage_sql, passage_id)
            if not row:
                return {}
            ordinal = row["ordinal"] or 0
            context_rows = await conn.fetch(
                context_sql,
                row["document_revision_id"],
                ordinal - 1,
                ordinal + 1,
            )
        ctx_map = {r["ordinal"]: r["passage_text"] or "" for r in context_rows}
        return {
            "passage_text": row["passage_text"] or "",
            "document_revision_id": row["document_revision_id"],
            "page": row["page_number"],
            "source_uri": row["source_uri"] or "",
            "source_date": (
                str(row["source_date"]) if row["source_date"] else None
            ),
            "context_before": ctx_map.get(ordinal - 1, ""),
            "context_after": ctx_map.get(ordinal + 1, ""),
        }
    except Exception:
        logger.exception(
            "evidence: passage fetch failed passage_id=%s", passage_id
        )
        raise


async def _fetch_structured_lineage(
    pg_pool: object, evidence_id: UUID
) -> dict[str, Any] | None:
    """Fetch silver.structured_record_lineage for this evidence_id."""
    sql = """
        SELECT
            lineage_id,
            evidence_id,
            bronze_uri,
            bronze_sha256,
            parser_name,
            parser_version,
            ingestion_run_id,
            native_locator
        FROM silver.structured_record_lineage
        WHERE evidence_id = $1
    """
    try:
        async with pg_pool.acquire() as conn:  # type: ignore[union-attr]
            row = await conn.fetchrow(sql, evidence_id)
        return dict(row) if row else None
    except Exception:
        logger.warning(
            "evidence: lineage fetch failed evidence_id=%s (non-fatal)",
            evidence_id,
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Neo4j helpers — graph_edge hydration
# ---------------------------------------------------------------------------


async def _fetch_neo4j_node(
    neo4j_driver: object, node_id: int
) -> tuple[list[str], dict[str, Any]]:
    """Fetch labels + properties for one Neo4j node by internal ID.

    Returns (labels, preview_props). On any error returns ([], {}).
    """
    cypher = (
        "MATCH (n) WHERE id(n) = $node_id "
        "RETURN labels(n) AS labels, properties(n) AS props LIMIT 1"
    )
    try:
        async with neo4j_driver.session() as session:  # type: ignore[union-attr]
            result = await asyncio.wait_for(
                session.run(cypher, node_id=int(node_id)),
                timeout=_NEO4J_TIMEOUT_S,
            )
            record = await asyncio.wait_for(result.single(), timeout=_NEO4J_TIMEOUT_S)
        if not record:
            return [], {}
        labels = list(record["labels"] or [])
        props = dict(record["props"] or {})
        # Trim to a compact preview: first 5 properties.
        preview = dict(list(props.items())[:5])
        return labels, preview
    except (TimeoutError, Exception):
        logger.warning(
            "evidence: Neo4j node fetch failed node_id=%s (non-fatal)",
            node_id,
            exc_info=True,
        )
        return [], {}


async def _fetch_neo4j_described_in(
    neo4j_driver: object, start_id: int, end_id: int, rel_type: str
) -> list[dict[str, Any]] | None:
    """Fetch DocumentRevision nodes connected to start/end nodes.

    Best-effort: returns None on Neo4j unavailability or empty match.
    """
    cypher = (
        "MATCH (start)-[r]->(end) "
        "WHERE id(start) = $sid AND id(end) = $eid AND type(r) = $rel "
        "MATCH (start)-[:DESCRIBED_IN]->(d:DocumentRevision) "
        "RETURN properties(d) AS props LIMIT 5"
    )
    try:
        async with neo4j_driver.session() as session:  # type: ignore[union-attr]
            result = await asyncio.wait_for(
                session.run(cypher, sid=int(start_id), eid=int(end_id), rel=rel_type),
                timeout=_NEO4J_TIMEOUT_S,
            )
            records = await asyncio.wait_for(result.data(), timeout=_NEO4J_TIMEOUT_S)
        if not records:
            return None
        return [dict(r.get("props") or {}) for r in records]
    except (TimeoutError, Exception):
        logger.warning(
            "evidence: Neo4j described_in fetch failed (non-fatal)",
            exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Branch assemblers
# ---------------------------------------------------------------------------


async def _assemble_passage(
    row: dict[str, Any], pg_pool: object, workspace_id: UUID
) -> EvidencePassagePayload:
    """Assemble EvidencePassagePayload from the evidence_items row."""
    passage_id = row.get("passage_id")
    if not passage_id:
        # Should not happen: has_target CHECK prevents this.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="evidence_fetch_failed",
        )

    passage_data = await _fetch_passage_with_context(pg_pool, UUID(str(passage_id)))
    if not passage_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Evidence not found",
        )

    source_uri = passage_data.get("source_uri") or str(row.get("source_uri") or "")
    page = passage_data.get("page")

    # Build deep_link when source_uri looks like a Bronze path.
    deep_link: str | None = None
    if source_uri:
        page_param = f"&page={page}" if page else ""
        deep_link = f"/api/v1/documents/view?bronze_uri={source_uri}{page_param}"

    return EvidencePassagePayload(
        evidence_type="document_passage",
        evidence_id=UUID(str(row["evidence_id"])),
        passage_text=passage_data.get("passage_text", ""),
        context_before=passage_data.get("context_before", ""),
        context_after=passage_data.get("context_after", ""),
        document_revision_id=UUID(str(passage_data["document_revision_id"])),
        source_uri=source_uri,
        source_date=passage_data.get("source_date"),
        page=page,
        deep_link=deep_link,
        workspace_id=workspace_id,
    )


async def _assemble_structured(
    row: dict[str, Any], pg_pool: object, workspace_id: UUID
) -> EvidenceStructuredPayload:
    """Assemble EvidenceStructuredPayload from the evidence_items row."""
    structured_ref = row.get("structured_ref") or {}
    if isinstance(structured_ref, str):
        import json  # noqa: PLC0415
        structured_ref = json.loads(structured_ref)

    lineage_row = await _fetch_structured_lineage(pg_pool, UUID(str(row["evidence_id"])))

    lineage: dict[str, Any] | None = None
    bronze_uri: str | None = None
    parser_name: str | None = None
    parser_version: str | None = None
    ingestion_run_id: UUID | None = None

    if lineage_row:
        lineage = {
            "lineage_id": str(lineage_row.get("lineage_id", "")),
            "bronze_sha256": lineage_row.get("bronze_sha256"),
            "native_locator": lineage_row.get("native_locator"),
        }
        bronze_uri = lineage_row.get("bronze_uri")
        parser_name = lineage_row.get("parser_name")
        parser_version = lineage_row.get("parser_version")
        run_id = lineage_row.get("ingestion_run_id")
        if run_id:
            with contextlib.suppress(ValueError):
                ingestion_run_id = UUID(str(run_id))

    return EvidenceStructuredPayload(
        evidence_type="structured_record",
        evidence_id=UUID(str(row["evidence_id"])),
        structured_ref=structured_ref,
        lineage=lineage,
        bronze_uri=bronze_uri,
        parser_name=parser_name,
        parser_version=parser_version,
        ingestion_run_id=ingestion_run_id,
        workspace_id=workspace_id,
    )


async def _assemble_graph_edge(
    row: dict[str, Any], neo4j_driver: object | None, workspace_id: UUID
) -> EvidenceGraphEdgePayload:
    """Assemble EvidenceGraphEdgePayload from the evidence_items row."""
    graph_edge_ref = row.get("graph_edge_ref") or {}
    if isinstance(graph_edge_ref, str):
        import json  # noqa: PLC0415
        graph_edge_ref = json.loads(graph_edge_ref)

    start_id = graph_edge_ref.get("start_node_id")
    end_id = graph_edge_ref.get("end_node_id")
    rel_type = graph_edge_ref.get("rel_type", "")

    start_labels: list[str] | None = None
    start_preview: dict[str, Any] | None = None
    end_labels: list[str] | None = None
    end_preview: dict[str, Any] | None = None
    described_in: list[dict[str, Any]] | None = None

    if neo4j_driver is not None and start_id is not None and end_id is not None:
        # Parallel fan-out for start node + end node + described_in.
        start_task = _fetch_neo4j_node(neo4j_driver, start_id)
        end_task = _fetch_neo4j_node(neo4j_driver, end_id)
        described_task = _fetch_neo4j_described_in(
            neo4j_driver, start_id, end_id, rel_type
        )
        raw_start, raw_end, described_in = await asyncio.gather(
            start_task, end_task, described_task, return_exceptions=True
        )

        if isinstance(raw_start, BaseException):
            raw_start = ([], {})
        if isinstance(raw_end, BaseException):
            raw_end = ([], {})
        if isinstance(described_in, BaseException):
            described_in = None

        start_labels, start_preview = raw_start  # type: ignore[misc]
        end_labels, end_preview = raw_end  # type: ignore[misc]
        start_labels = start_labels or None
        start_preview = start_preview or None
        end_labels = end_labels or None
        end_preview = end_preview or None

    return EvidenceGraphEdgePayload(
        evidence_type="graph_edge",
        evidence_id=UUID(str(row["evidence_id"])),
        graph_edge_ref=graph_edge_ref,
        start_node_labels=start_labels,
        start_node_preview=start_preview,
        end_node_labels=end_labels,
        end_node_preview=end_preview,
        described_in=described_in if isinstance(described_in, list) else None,
        workspace_id=workspace_id,
    )


def _assemble_map_feature(
    row: dict[str, Any], workspace_id: UUID
) -> EvidenceMapFeaturePayload:
    """Assemble EvidenceMapFeaturePayload from the evidence_items row."""
    map_feature_ref = row.get("map_feature_ref") or {}
    if isinstance(map_feature_ref, str):
        import json  # noqa: PLC0415
        map_feature_ref = json.loads(map_feature_ref)

    tile_function = map_feature_ref.get("tile_function")
    bbox_raw = map_feature_ref.get("bbox")
    bbox: list[float] | None = None
    if bbox_raw:
        try:
            bbox = [float(v) for v in bbox_raw]
        except (TypeError, ValueError):
            bbox = None

    feature_properties = map_feature_ref.get("properties") or map_feature_ref.get(
        "feature_properties"
    )

    return EvidenceMapFeaturePayload(
        evidence_type="map_feature",
        evidence_id=UUID(str(row["evidence_id"])),
        map_feature_ref=map_feature_ref,
        tile_function=tile_function,
        bbox=bbox,
        feature_properties=feature_properties,
        workspace_id=workspace_id,
    )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.get(
    "/{evidence_id}",
    response_model=Annotated[EvidencePayload, None],
    summary="Fetch evidence by ID with type-branched payload for the Module 7 inspector UI",
    description=(
        "Returns a type-branched evidence payload. "
        "Branch on `evidence_type` for document_passage, structured_record, "
        "graph_edge, or map_feature payloads. "
        "Auth: X-Service-Key required; workspace scope enforced via X-Workspace-Id header "
        "or default workspace fallback. Module 9 will harden with JWT workspace claim."
    ),
)
async def get_evidence(
    evidence_id: UUID = Path(..., description="UUID of the evidence item to fetch"),
    request: Request = None,  # type: ignore[assignment]
    user: UserContext = Depends(extract_user_context),
) -> EvidencePayload:
    """GET /v1/evidence/{evidence_id}.

    Steps:
      1. Resolve workspace_id from X-Workspace-Id header or JWT fallback.
      2. Fetch evidence_items row (workspace-scoped).
      3. Branch on evidence_type:
           document_passage → fetch passage + context + document_revision
           structured_record → fetch structured_ref + lineage
           graph_edge → hydrate Neo4j nodes + described_in
           map_feature → parse tile_function / bbox / properties
      4. Return typed payload.

    Returns 404 on missing row OR cross-tenant mismatch (silent enumeration guard).
    Returns 500 {detail: "evidence_fetch_failed"} on internal DB / Neo4j errors.
    """
    # Resolve DB pools from app.state.
    try:
        pg_pool = request.app.state.pg_pool
    except AttributeError:
        logger.error("evidence: pg_pool not found on app.state")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="evidence_fetch_failed",
        )

    # Resolve workspace_id (Module 9 Chunk 9.4 — JWT-derived, no default fallback).
    redis_client = getattr(request.app.state, "redis_client", None)
    workspace_id = await resolve_workspace_id(user, request, pg_pool, redis_client)

    try:
        neo4j_driver = request.app.state.neo4j_driver
    except AttributeError:
        neo4j_driver = None

    # 1. Fetch base evidence_items row.
    try:
        row = await _fetch_evidence_row(pg_pool, evidence_id, workspace_id)
    except Exception:
        logger.error(
            "evidence: base row fetch failed evidence_id=%s", evidence_id
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="evidence_fetch_failed",
        )

    if row is None:
        # 404 for both "not found" and "wrong workspace" — no enumeration.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Evidence not found",
        )

    evidence_type: str = str(row.get("evidence_type") or "")

    # 2. Branch on evidence_type.
    try:
        if evidence_type == "document_passage":
            return await _assemble_passage(row, pg_pool, workspace_id)

        if evidence_type == "structured_record":
            return await _assemble_structured(row, pg_pool, workspace_id)

        if evidence_type == "graph_edge":
            return await _assemble_graph_edge(row, neo4j_driver, workspace_id)

        if evidence_type == "map_feature":
            return _assemble_map_feature(row, workspace_id)

        # Unknown evidence_type — schema drift or future extension.
        logger.error(
            "evidence: unknown evidence_type=%s evidence_id=%s",
            evidence_type,
            evidence_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="evidence_fetch_failed",
        )

    except HTTPException:
        raise
    except Exception:
        logger.exception(
            "evidence: assembly failed evidence_id=%s evidence_type=%s",
            evidence_id,
            evidence_type,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="evidence_fetch_failed",
        )
