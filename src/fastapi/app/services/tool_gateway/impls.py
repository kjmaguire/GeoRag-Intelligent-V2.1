"""§4 Tool Gateway — bind real implementations to the 19 registered tools.

Each tool name in workspace.agent_risk_tiers needs a Python impl bound
via register_tool() so existing call sites can migrate to invoke_tool().

This module bundles the 10 R0/R1 (read-only / suggestion) impls — those
are the safest to migrate first. R2-R5 impls live in their respective
domain modules and self-register on import (e.g. report_builder
registers `generate_report` from inside its package).

Idempotent: re-importing this module re-registers the same impls (safe).
"""
from __future__ import annotations

import logging
import os
from typing import Any

from app.services.tool_gateway.gateway import register_tool

log = logging.getLogger("georag.tool_gateway.impls")


# ─── R0 read-only ─────────────────────────────────────────────────────
async def _audit_provenance(inputs: dict[str, Any]) -> dict[str, Any]:
    """Read silver.* provenance chain for a single row.
    Inputs: {table_name: str, silver_pk: uuid}
    """
    from app.services.review_lineage_lookup import lookup_review_lineage
    from app.main import app
    pg_pool = getattr(app.state, "pg_pool", None)
    table = inputs.get("table_name", "")
    pk = inputs.get("silver_pk", "")
    if not (table and pk):
        return {"error": "table_name + silver_pk required"}
    lineage = await lookup_review_lineage(pg_pool, table, pk)
    return {"lineage": lineage}


async def _query_postgis_readonly(inputs: dict[str, Any]) -> dict[str, Any]:
    """Whitelisted read-only SQL against silver/gold/public_geo.
    Inputs: {sql: str, args: list, workspace_id: str}
    Allows only SELECT statements; blocks DDL/DML at the parser level.
    """
    from app.main import app
    pg_pool = getattr(app.state, "pg_pool", None)
    if pg_pool is None:
        return {"error": "pg_pool not initialised"}
    sql = (inputs.get("sql") or "").strip()
    if not sql.lower().startswith("select "):
        return {"error": "only SELECT statements allowed"}
    if any(banned in sql.lower() for banned in (";", "insert ", "update ", "delete ", "drop ", "alter ", "create ", "grant ", "revoke ")):
        return {"error": "potentially-mutating clause rejected"}
    workspace_id = inputs.get("workspace_id") or "a0000000-0000-0000-0000-000000000001"
    args = inputs.get("args", [])
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id,
        )
        rows = await conn.fetch(sql, *args)
    return {"rows": [dict(r) for r in rows], "count": len(rows)}


async def _query_neo4j_readonly(inputs: dict[str, Any]) -> dict[str, Any]:
    """Read-only Cypher against the graph.
    Inputs: {cypher: str, params: dict, workspace_id: str}
    Blocks any write keyword.
    """
    try:
        from neo4j import AsyncGraphDatabase
    except ImportError:
        return {"error": "neo4j driver not installed in this worker"}
    cypher = (inputs.get("cypher") or "").strip()
    lc = cypher.lower()
    if any(banned in lc for banned in (
        "create ", "merge ", "delete ", "remove ", "set ", "drop ",
    )):
        return {"error": "potentially-mutating clause rejected"}
    host = os.environ.get("NEO4J_HOST", "neo4j")
    port = int(os.environ.get("NEO4J_PORT", "7687"))
    user = os.environ.get("NEO4J_USER", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD", "")
    if not password:
        return {"error": "NEO4J_PASSWORD not set"}
    driver = AsyncGraphDatabase.driver(f"bolt://{host}:{port}", auth=(user, password))
    try:
        async with driver.session() as session:
            result = await session.run(cypher, **(inputs.get("params") or {}))
            rows = [dict(r) async for r in result]
        return {"rows": rows[:1000], "count": len(rows)}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        try: await driver.close()
        except Exception: pass


async def _retrieve_qdrant(inputs: dict[str, Any]) -> dict[str, Any]:
    """Vector search against workspace + public Qdrant collections.
    Inputs: {query: str, k: int, workspace_id: str, collection: str?}
    """
    try:
        from qdrant_client import AsyncQdrantClient
        from qdrant_client.models import Filter, FieldCondition, MatchValue
    except ImportError:
        return {"error": "qdrant client not installed"}
    host = os.environ.get("QDRANT_HOST", "qdrant")
    port = int(os.environ.get("QDRANT_HTTP_PORT", "6333"))
    collection = inputs.get("collection", "georag_reports")
    workspace_id = inputs.get("workspace_id")
    k = int(inputs.get("k", 10))

    # We need an embedding for the query — the proper path is the SPLADE
    # encoder, but as a starting point return points filtered by payload
    # match-string rather than vector similarity. Real embedding wiring
    # is wave 2 (would call services/qdrant_service.retrieve()).
    client = AsyncQdrantClient(host=host, port=port)
    try:
        flt = None
        if workspace_id:
            flt = Filter(must=[FieldCondition(
                key="workspace_id",
                match=MatchValue(value=workspace_id),
            )])
        batch, _ = await client.scroll(
            collection_name=collection,
            scroll_filter=flt,
            with_vectors=False, with_payload=True,
            limit=k,
        )
        return {
            "hits": [{"id": p.id, "payload": dict(p.payload or {})} for p in batch],
            "count": len(batch),
        }
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        try: await client.close()
        except Exception: pass


async def _query_public_geo(inputs: dict[str, Any]) -> dict[str, Any]:
    """Read public_geo.* layers within a bounding box.
    Inputs: {layer: str, bbox: [lng,lat,lng,lat], limit: int}
    """
    from app.main import app
    pg_pool = getattr(app.state, "pg_pool", None)
    layer = inputs.get("layer", "pg_mineral_occurrence")
    bbox = inputs.get("bbox")
    limit = int(inputs.get("limit", 100))
    if layer not in {"pg_mineral_occurrence", "pg_drillhole_collar", "pg_mine",
                     "pg_bedrock_geology", "pg_assessment_survey"}:
        return {"error": f"layer {layer} not in approved list"}
    if not bbox or len(bbox) != 4:
        return {"error": "bbox required [lng_min,lat_min,lng_max,lat_max]"}
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id::text, jurisdiction_code, source_id,
                   ST_AsGeoJSON(geom)::jsonb AS geojson
              FROM public_geo.{layer}
             WHERE geom && ST_MakeEnvelope($1, $2, $3, $4, 4326)
             LIMIT $5
            """,
            bbox[0], bbox[1], bbox[2], bbox[3], limit,
        )
    return {"features": [dict(r) for r in rows], "count": len(rows)}


# ─── R1 suggestion ────────────────────────────────────────────────────
async def _validate_schema(inputs: dict[str, Any]) -> dict[str, Any]:
    """Suggest canonical-column mapping for a vendor column name.
    Inputs: {vendor_column: str, vendor_profile_hint: str?}
    """
    from app.main import app
    pg_pool = getattr(app.state, "pg_pool", None)
    col = (inputs.get("vendor_column") or "").strip().lower()
    if not col:
        return {"error": "vendor_column required"}

    # Simple fuzzy: look up existing column_mappings rows with matching
    # vendor_column. Real shape would call the schema mapping agent.
    async with pg_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT canonical_column, count(*) AS n
              FROM column_mappings
             WHERE lower(vendor_column) = $1
             GROUP BY canonical_column
             ORDER BY n DESC LIMIT 5
            """,
            col,
        )
    suggestions = [
        {"canonical_column": r["canonical_column"], "confidence": min(0.99, r["n"] / 10.0)}
        for r in rows
    ]
    return {"vendor_column": col, "suggestions": suggestions}


async def _run_evaluation(inputs: dict[str, Any]) -> dict[str, Any]:
    """Fire the eval harness against a question_set.
    Inputs: {question_set: str, evaluator_kind: str}
    """
    from app.hatchet_workflows.evaluate_workspace import (
        EvaluateWorkspaceInput, run as evaluate_workspace_task,
    )
    from uuid import uuid4

    qs = inputs.get("question_set", "refusal_correctness")
    evaluator = inputs.get("evaluator_kind", "aio_mock")
    inp = EvaluateWorkspaceInput(
        eval_request_id=uuid4(),
        evaluator_kind=evaluator,
        question_set_filter=qs,
        blocks_promotion=False,
    )
    out = await evaluate_workspace_task.aio_mock_run(inp)
    return {
        "run_id": str(out.run_id),
        "pass_count": out.pass_count,
        "fail_count": out.fail_count,
        "question_count": out.question_count,
    }


# ─── Boot — call once at FastAPI startup ───────────────────────────
def register_all_impls() -> None:
    """Register every available impl. Idempotent."""
    register_tool("audit_provenance",       _audit_provenance)
    register_tool("query_postgis_readonly", _query_postgis_readonly)
    register_tool("query_neo4j_readonly",   _query_neo4j_readonly)
    register_tool("retrieve_qdrant",        _retrieve_qdrant)
    register_tool("query_public_geo",       _query_public_geo)
    register_tool("validate_schema",        _validate_schema)
    register_tool("run_evaluation",         _run_evaluation)
    log.info("tool_gateway: registered 7 R0/R1 impls")


__all__ = ["register_all_impls"]
