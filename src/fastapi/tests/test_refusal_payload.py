"""Unit tests for Module 6 Phase B Chunk 4a — evidence inspector + reason codes.

Tests
-----
  Refusal reason codes:
    1. RefusalReasonCode — all six values present in model

  Evidence inspector (unit, no DB):
    2. EvidencePassagePayload — Pydantic round-trip
    3. EvidenceStructuredPayload — Pydantic round-trip
    4. EvidenceGraphEdgePayload — Pydantic round-trip (no Neo4j)
    5. EvidenceMapFeaturePayload — bbox parsing
    6. _assemble_map_feature — tile_function / bbox / properties extraction
    7. 404 on cross-tenant workspace mismatch (mocked DB returns None)
    8. 500 on DB fetch exception

History (2026-08-21): this file also covered app/services/refusal_builder.py
and layer_completeness.build_refusal_payload — ten tests over two modules
that had no production caller between them. refusal_builder was reachable
only from these tests, and the GuardBundle it consumed was constructed only
by layer_completeness.evaluate_guards, which nothing called either. Both
modules were deleted and their tests removed with them. The reason-code
Literal in app/models/answer_run.py survives and is still pinned below,
because it is the Module 7 branching contract.

All tests are pure unit tests — no live DB, no Docker required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest


def test_refusal_reason_code_enum_values():
    """RefusalReasonCode Literal contains all six stable values for Module 7 branching."""
    import typing

    from app.models.answer_run import RefusalReasonCode

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
