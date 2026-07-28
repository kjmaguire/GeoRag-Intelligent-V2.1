"""Tests for reporting agents retained by registered admin routers."""
from __future__ import annotations

import asyncio

import pytest

from app.agents.phase7.evidence_curator import evidence_curator
from app.agents.phase7.report_planner import report_planner


def _inner(agent):
    return getattr(agent, "__wrapped__", agent)


def test_report_planner_all_11_types_produce_sections() -> None:
    inner = _inner(report_planner)
    report_types = [
        "weekly_project_digest",
        "ingestion_quality",
        "technical_due_diligence",
        "executive_project_intelligence",
        "gis_arcgis_sync",
        "target_recommendation",
        "public_geo_overlay",
        "data_room_package",
        "what_changed",
        "ni43101_section_pack",
        "csa11348_disclosure_pack",
    ]
    for report_type in report_types:
        result = asyncio.run(
            inner(
                ctx=None,
                workspace_id="ws-1",
                project_id="p-1",
                report_type=report_type,
            )
        )
        assert result["sections"]
        assert result["report_type"] == report_type


def test_report_planner_ni43101_has_qp_sections() -> None:
    result = asyncio.run(
        _inner(report_planner)(
            ctx=None,
            workspace_id="ws",
            project_id="p",
            report_type="ni43101_section_pack",
        )
    )
    section_ids = [section["section_id"] for section in result["sections"]]
    assert "title_page" in section_ids
    assert "interpretation" in section_ids


def test_report_planner_unknown_type_raises() -> None:
    with pytest.raises(ValueError):
        asyncio.run(
            _inner(report_planner)(
                ctx=None,
                workspace_id="ws",
                project_id="p",
                report_type="bogus_type",
            )
        )


def test_evidence_curator_orders_by_score() -> None:
    result = asyncio.run(
        _inner(evidence_curator)(
            ctx=None,
            workspace_id="ws",
            project_id="p",
            section_id="s-1",
            required_evidence_kinds=["assay_results"],
            claim_ids=["claim-1"],
            candidate_evidence={
                "claim-1": [
                    {
                        "source_chunk_id": "low",
                        "evidence_kind": "new_passages",
                        "relevance_score": 0.3,
                        "data_visibility": "public",
                        "is_stale": False,
                    },
                    {
                        "source_chunk_id": "high",
                        "evidence_kind": "assay_results",
                        "relevance_score": 0.7,
                        "data_visibility": "workspace",
                        "is_stale": False,
                    },
                ]
            },
        )
    )
    scores = [
        item["relevance_score"]
        for item in result["evidence_per_claim"]["claim-1"]
    ]
    assert scores == sorted(scores, reverse=True)
