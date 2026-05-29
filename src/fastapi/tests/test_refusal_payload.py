"""Unit tests for Module 6 Phase B Chunk 4a — refusal payload + evidence inspector.

Tests
-----
  Refusal builder:
    1. build_guard_refusal_payload — numeric guard failure
    2. build_guard_refusal_payload — entity guard failure
    3. build_guard_refusal_payload — completeness guard failure
    4. build_guard_refusal_payload — multiple guard failures (reason_code priority)
    5. build_guard_refusal_payload — pg_pool=None (DB fallback path)
    6. build_llm_unavailable_payload — shape + reason_code
    7. build_budget_exhausted_payload — shape + reason_code
    8. build_insufficient_evidence_payload — synchronous path + shape
    9. RefusalReasonCode — all six values present in model
   10. fallback stub in layer_completeness.build_refusal_payload — shape stable

  Evidence inspector (unit, no DB):
   11. EvidencePassagePayload — Pydantic round-trip
   12. EvidenceStructuredPayload — Pydantic round-trip
   13. EvidenceGraphEdgePayload — Pydantic round-trip (no Neo4j)
   14. EvidenceMapFeaturePayload — bbox parsing
   15. _assemble_map_feature — tile_function / bbox / properties extraction
   16. 404 on cross-tenant workspace mismatch (mocked DB returns None)
   17. 500 on DB fetch exception
   18. reason_code stability — enum values unchanged (Module 7 contract)

All tests are pure unit tests — no live DB, no Docker required.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

# ---------------------------------------------------------------------------
# Stubs for GuardResult / GuardBundle
# ---------------------------------------------------------------------------


@dataclass
class _GuardResult:
    guard_name: str
    passed: bool
    failed_tokens: list[str] = field(default_factory=list)
    failed_entities: list[str] = field(default_factory=list)
    uncited_sentences: list[str] = field(default_factory=list)
    derivation_log: list[str] = field(default_factory=list)


@dataclass
class _GuardBundle:
    all_passed: bool
    numeric: _GuardResult
    entity: _GuardResult
    completeness: _GuardResult
    failed_guards: list[_GuardResult] = field(default_factory=list)


def _make_numeric_fail() -> _GuardBundle:
    n = _GuardResult("numeric", False, failed_tokens=["12.5", "99.9"])
    e = _GuardResult("entity", True)
    c = _GuardResult("completeness", True)
    return _GuardBundle(False, n, e, c, failed_guards=[n])


def _make_entity_fail() -> _GuardBundle:
    n = _GuardResult("numeric", True)
    e = _GuardResult("entity", False, failed_entities=["XYZ-99-NONEXIST"])
    c = _GuardResult("completeness", True)
    return _GuardBundle(False, n, e, c, failed_guards=[e])


def _make_completeness_fail() -> _GuardBundle:
    n = _GuardResult("numeric", True)
    e = _GuardResult("entity", True)
    c = _GuardResult("completeness", False, uncited_sentences=["Gold grades were high."])
    return _GuardBundle(False, n, e, c, failed_guards=[c])


def _make_multi_fail() -> _GuardBundle:
    n = _GuardResult("numeric", False, failed_tokens=["42"])
    e = _GuardResult("entity", False, failed_entities=["FAKE-HOLE"])
    c = _GuardResult("completeness", True)
    return _GuardBundle(False, n, e, c, failed_guards=[n, e])


# ---------------------------------------------------------------------------
# Refusal builder tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guard_refusal_numeric_reason_code():
    """build_guard_refusal_payload returns guard_numeric_fail for numeric guard failure."""
    from app.services.refusal_builder import build_guard_refusal_payload

    bundle = _make_numeric_fail()
    payload = await build_guard_refusal_payload(guard_bundle=bundle, pg_pool=None)

    assert payload["type"] == "refusal"
    assert payload["reason_code"] == "guard_numeric_fail"
    assert "searched" in payload
    assert "missing" in payload
    assert "message" in payload
    assert "12.5" in payload["missing"]["what_was_needed"] or "numeric" in payload["missing"]["what_was_needed"].lower()


@pytest.mark.asyncio
async def test_guard_refusal_entity_reason_code():
    """build_guard_refusal_payload returns guard_entity_fail for entity guard failure."""
    from app.services.refusal_builder import build_guard_refusal_payload

    bundle = _make_entity_fail()
    payload = await build_guard_refusal_payload(
        guard_bundle=bundle, pg_pool=None, query_context="What is the depth of XYZ-99-NONEXIST?"
    )

    assert payload["reason_code"] == "guard_entity_fail"
    assert "XYZ-99-NONEXIST" in payload["missing"]["what_was_needed"]


@pytest.mark.asyncio
async def test_guard_refusal_completeness_reason_code():
    """build_guard_refusal_payload returns guard_completeness_fail for completeness failure."""
    from app.services.refusal_builder import build_guard_refusal_payload

    bundle = _make_completeness_fail()
    payload = await build_guard_refusal_payload(guard_bundle=bundle, pg_pool=None)

    assert payload["reason_code"] == "guard_completeness_fail"
    assert "Gold grades were high." in payload["missing"]["what_was_needed"]


@pytest.mark.asyncio
async def test_guard_refusal_multi_fail_priority():
    """Multiple guard failures — numeric takes priority over entity in reason_code."""
    from app.services.refusal_builder import build_guard_refusal_payload

    bundle = _make_multi_fail()
    payload = await build_guard_refusal_payload(guard_bundle=bundle, pg_pool=None)

    # numeric > entity in priority
    assert payload["reason_code"] == "guard_numeric_fail"
    # Both guard names should appear in failed_guards list
    assert "numeric" in payload["failed_guards"]
    assert "entity" in payload["failed_guards"]


@pytest.mark.asyncio
async def test_guard_refusal_no_pool_fallback_searched():
    """build_guard_refusal_payload returns a valid searched block even without pg_pool."""
    from app.services.refusal_builder import build_guard_refusal_payload

    bundle = _make_completeness_fail()
    payload = await build_guard_refusal_payload(guard_bundle=bundle, pg_pool=None)

    searched = payload["searched"]
    assert isinstance(searched["stores_queried"], list)
    assert len(searched["stores_queried"]) > 0
    assert isinstance(searched["candidates_considered"], int)
    assert isinstance(searched["query_class"], str)
    missing = payload["missing"]
    assert isinstance(missing["nearest_candidates"], list)


@pytest.mark.asyncio
async def test_llm_unavailable_payload_shape():
    """build_llm_unavailable_payload returns correct reason_code and shape."""
    from app.services.refusal_builder import build_llm_unavailable_payload

    payload = await build_llm_unavailable_payload(
        backend_chain=["ollama:failed:timeout", "anthropic:failed:connection_error"]
    )

    assert payload["type"] == "refusal"
    assert payload["reason_code"] == "llm_unavailable"
    assert "ollama" in payload["message"] or "unavailable" in payload["message"]
    assert "searched" in payload
    assert payload["searched"]["candidates_considered"] == 0
    assert payload["missing"]["nearest_candidates"] == []


@pytest.mark.asyncio
async def test_budget_exhausted_payload_shape():
    """build_budget_exhausted_payload returns correct reason_code and shape."""
    from app.services.refusal_builder import build_budget_exhausted_payload

    payload = await build_budget_exhausted_payload()

    assert payload["type"] == "refusal"
    assert payload["reason_code"] == "budget_exhausted"
    assert "timed out" in payload["message"].lower() or "budget" in payload["message"].lower()


def test_insufficient_evidence_payload_sync():
    """build_insufficient_evidence_payload is synchronous and returns full B4 shape."""
    from app.services.refusal_builder import build_insufficient_evidence_payload

    payload = build_insufficient_evidence_payload(
        query_context="What is the gold grade at drill hole ABC-01?",
        stores_queried=["qdrant", "postgis"],
        candidates_considered=12,
    )

    assert payload["type"] == "refusal"
    assert payload["reason_code"] == "insufficient_evidence"
    assert payload["searched"]["candidates_considered"] == 12
    assert payload["searched"]["stores_queried"] == ["qdrant", "postgis"]
    assert "ABC-01" in payload["missing"]["what_was_needed"]


def test_refusal_reason_code_enum_values():
    """RefusalReasonCode Literal contains all six stable values for Module 7 branching."""
    from app.models.answer_run import RefusalReasonCode
    import typing

    # Unwrap the Literal args — works for both Python 3.8+ forms.
    args = typing.get_args(RefusalReasonCode)
    expected = {
        "insufficient_evidence",
        "guard_numeric_fail",
        "guard_entity_fail",
        "guard_completeness_fail",
        "llm_unavailable",
        "budget_exhausted",
    }
    assert set(args) == expected, f"RefusalReasonCode values changed: {args}"


def test_layer_completeness_fallback_stub_shape():
    """Fallback stub in layer_completeness.build_refusal_payload has stable B4 shape."""
    from app.agent.hallucination.layer_completeness import build_refusal_payload

    bundle = _make_numeric_fail()
    payload = build_refusal_payload(bundle)

    assert payload["type"] == "refusal"
    assert payload["reason_code"] == "guard_numeric_fail"
    assert "searched" in payload
    assert "missing" in payload
    assert "failed_guards" in payload
    assert isinstance(payload["failed_guards"], list)


# ---------------------------------------------------------------------------
# Evidence inspector — Pydantic model round-trips
# ---------------------------------------------------------------------------


def test_evidence_passage_payload_roundtrip():
    """EvidencePassagePayload validates and serialises correctly."""
    from app.routers.evidence import EvidencePassagePayload

    payload = EvidencePassagePayload(
        evidence_type="document_passage",
        evidence_id=UUID("00000000-0000-0000-0000-000000000001"),
        passage_text="Gold mineralisation was observed at 45.3m depth.",
        context_before="The drill hole penetrated granodiorite to 80m.",
        context_after="Assay results are presented in Table 3.",
        document_revision_id=UUID("00000000-0000-0000-0000-000000000002"),
        source_uri="s3://bronze/reports/ni43101_2023.pdf",
        source_date="2023-06-15",
        page=22,
        deep_link="/api/v1/documents/view?bronze_uri=s3://bronze/reports/ni43101_2023.pdf&page=22",
        workspace_id=UUID("a0000000-0000-0000-0000-000000000001"),
    )

    dumped = payload.model_dump()
    assert dumped["evidence_type"] == "document_passage"
    assert dumped["page"] == 22
    assert "bronze_uri" in dumped["deep_link"]
    assert dumped["context_before"].startswith("The drill hole")


def test_evidence_structured_payload_roundtrip():
    """EvidenceStructuredPayload validates and serialises correctly."""
    from app.routers.evidence import EvidenceStructuredPayload

    payload = EvidenceStructuredPayload(
        evidence_type="structured_record",
        evidence_id=UUID("00000000-0000-0000-0000-000000000003"),
        structured_ref={
            "schema": "silver",
            "table": "collars",
            "pk": {"collar_id": "abc-123"},
        },
        lineage={
            "lineage_id": "00000000-0000-0000-0000-000000000010",
            "bronze_sha256": "a" * 64,
        },
        bronze_uri="s3://bronze/collars/collars_2023.csv",
        parser_name="collar_csv_parser",
        parser_version="1.2.0",
        ingestion_run_id=UUID("00000000-0000-0000-0000-000000000099"),
        workspace_id=UUID("a0000000-0000-0000-0000-000000000001"),
    )

    dumped = payload.model_dump()
    assert dumped["evidence_type"] == "structured_record"
    assert dumped["structured_ref"]["table"] == "collars"
    assert dumped["bronze_uri"] == "s3://bronze/collars/collars_2023.csv"


def test_evidence_graph_edge_payload_roundtrip():
    """EvidenceGraphEdgePayload validates with optional Neo4j fields as None."""
    from app.routers.evidence import EvidenceGraphEdgePayload

    payload = EvidenceGraphEdgePayload(
        evidence_type="graph_edge",
        evidence_id=UUID("00000000-0000-0000-0000-000000000004"),
        graph_edge_ref={
            "start_node_id": 100,
            "end_node_id": 200,
            "rel_type": "HAS_SAMPLE",
        },
        start_node_labels=["DrillHole"],
        start_node_preview={"name": "ABC-01-99"},
        end_node_labels=["Sample"],
        end_node_preview={"sample_id": "S-9999"},
        described_in=None,
        workspace_id=UUID("a0000000-0000-0000-0000-000000000001"),
    )

    dumped = payload.model_dump()
    assert dumped["evidence_type"] == "graph_edge"
    assert dumped["start_node_labels"] == ["DrillHole"]
    assert dumped["described_in"] is None


def test_evidence_map_feature_payload_bbox_parsing():
    """_assemble_map_feature extracts bbox and tile_function from map_feature_ref."""
    from app.routers.evidence import _assemble_map_feature

    row = {
        "evidence_id": UUID("00000000-0000-0000-0000-000000000005"),
        "map_feature_ref": {
            "tile_function": "collars_mvt",
            "bbox": [-110.5, 52.1, -110.0, 52.5],
            "properties": {"hole_id": "ABC-01-99", "depth": 120.5},
        },
    }
    workspace_id = UUID("a0000000-0000-0000-0000-000000000001")

    result = _assemble_map_feature(row, workspace_id)

    assert result.evidence_type == "map_feature"
    assert result.tile_function == "collars_mvt"
    assert result.bbox == [-110.5, 52.1, -110.0, 52.5]
    assert result.feature_properties["hole_id"] == "ABC-01-99"


@pytest.mark.asyncio
async def test_evidence_endpoint_404_on_missing_row():
    """get_evidence raises 404 when _fetch_evidence_row returns None."""
    from fastapi import HTTPException
    from app.routers.evidence import get_evidence
    from app.services.auth import UserContext

    mock_request = MagicMock()
    # Module 9 Chunk 9.4 — workspace must come from JWT (workspace_id claim)
    # not the X-Workspace-Id header in multi-tenant mode (default). Setting
    # user.workspace_id mirrors a fully populated JWT.
    mock_request.headers = {}
    mock_pool = AsyncMock()

    mock_app = MagicMock()
    mock_app.state.pg_pool = mock_pool
    mock_app.state.neo4j_driver = None
    mock_app.state.redis_client = None
    mock_request.app = mock_app

    user = UserContext(workspace_id="a0000000-0000-0000-0000-000000000001")

    with patch(
        "app.routers.evidence._fetch_evidence_row",
        new_callable=AsyncMock,
        return_value=None,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_evidence(
                evidence_id=UUID("00000000-0000-0000-0000-000000000099"),
                request=mock_request,
                user=user,
            )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_evidence_endpoint_500_on_db_exception():
    """get_evidence raises 500 when DB raises an unexpected exception."""
    from fastapi import HTTPException
    from app.routers.evidence import get_evidence
    from app.services.auth import UserContext

    mock_request = MagicMock()
    mock_request.headers = {}
    mock_pool = AsyncMock()
    mock_app = MagicMock()
    mock_app.state.pg_pool = mock_pool
    mock_app.state.neo4j_driver = None
    mock_app.state.redis_client = None
    mock_request.app = mock_app

    # Module 9 Chunk 9.4 — workspace from JWT, not header.
    user = UserContext(workspace_id="a0000000-0000-0000-0000-000000000001")

    with patch(
        "app.routers.evidence._fetch_evidence_row",
        new_callable=AsyncMock,
        side_effect=RuntimeError("DB connection lost"),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_evidence(
                evidence_id=UUID("00000000-0000-0000-0000-000000000099"),
                request=mock_request,
                user=user,
            )
    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "evidence_fetch_failed"
