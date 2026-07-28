"""Tests for Phase 5 agents retained by the visualizations router."""
from __future__ import annotations

import asyncio

from app.agents.phase5.drillhole_visual_qa import drillhole_visual_qa
from app.agents.phase5.visual_readiness import visual_readiness
from app.agents.phase9.spatial_relationship import spatial_relationship


def _run(agent, **kwargs):
    inner = getattr(agent, "__wrapped__", agent)
    return asyncio.run(inner(ctx=None, **kwargs))


def test_drillhole_visual_qa_clean_collar_is_ready() -> None:
    result = _run(
        drillhole_visual_qa,
        collar_id="c1",
        inventory={
            "has_collar": True,
            "has_total_depth": True,
            "has_azimuth_dip": True,
            "interval_count": 10,
            "trace_point_count": 5,
            "has_lithology_codes": True,
        },
    )
    assert result["visualization_ready"] is True
    assert "strip_log" in result["supported_visualizations"]
    assert result["issues"] == []


def test_drillhole_visual_qa_missing_collar_is_critical() -> None:
    result = _run(
        drillhole_visual_qa,
        collar_id="missing",
        inventory={"has_collar": False},
    )
    assert result["visualization_ready"] is False
    assert any(issue["severity"] == "critical" for issue in result["issues"])


def test_visual_readiness_strip_log_with_enough_data() -> None:
    result = _run(
        visual_readiness,
        viz_kind="strip_log",
        collar_id="c1",
        inventory={"interval_count": 10, "has_total_depth": 1},
    )
    assert result["ready"] is True
    assert "strip_log" in result["supported"]


def test_visual_readiness_cross_section_needs_project_id() -> None:
    result = _run(visual_readiness, viz_kind="cross_section", inventory={})
    assert result["ready"] is False
    assert "project_id" in result["missing"]


def test_spatial_relationship_filters_predicates() -> None:
    result = _run(
        spatial_relationship,
        workspace_id="ws",
        project_id="p",
        subject_entity_id="hole-1",
        predicate_filter=["crosscuts", "hosts"],
        relationships=[
            {
                "predicate": "crosscuts",
                "object_id": "fault-1",
                "evidence_chunk_ids": ["e1"],
            },
            {
                "predicate": "near",
                "object_id": "intr-1",
                "evidence_chunk_ids": ["e2"],
            },
            {
                "predicate": "hosts",
                "object_id": "lith-1",
                "evidence_chunk_ids": ["e3"],
            },
        ],
    )
    assert {item["predicate"] for item in result["relationships"]} == {
        "crosscuts",
        "hosts",
    }
