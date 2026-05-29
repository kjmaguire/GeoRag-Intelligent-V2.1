"""§5 visual QA + §6 public/private boundary + §9 spatial relationship
agent graduations (Phase H4)."""
from __future__ import annotations

import asyncio

import pytest

from app.agents.phase5.drillhole_visual_qa import drillhole_visual_qa
from app.agents.phase5.visual_readiness import visual_readiness
from app.agents.phase6.public_private_boundary import public_private_boundary
from app.agents.phase9.spatial_relationship import spatial_relationship


def _inner(agent):
    return getattr(agent, "__wrapped__", agent)


def _run(agent, **kwargs):
    return asyncio.run(_inner(agent)(ctx=None, **kwargs))


# ──────────────────────── drillhole_visual_qa ─────────────────────


def test_drillhole_visual_qa_clean_collar_is_ready() -> None:
    result = _run(
        drillhole_visual_qa, collar_id="c1",
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


def test_drillhole_visual_qa_missing_collar_critical() -> None:
    result = _run(
        drillhole_visual_qa, collar_id="missing",
        inventory={"has_collar": False},
    )
    assert result["visualization_ready"] is False
    assert any(i["severity"] == "critical" for i in result["issues"])
    assert result["supported_visualizations"] == []


def test_drillhole_visual_qa_sparse_intervals_warning() -> None:
    result = _run(
        drillhole_visual_qa, collar_id="c1",
        inventory={
            "has_collar": True, "has_total_depth": True,
            "has_azimuth_dip": True, "interval_count": 1,
            "trace_point_count": 5, "has_lithology_codes": True,
        },
    )
    # Sparse intervals → warning, not critical
    assert result["visualization_ready"] is True
    assert any(i["severity"] == "warning" and i["field"] == "lithology"
               for i in result["issues"])


def test_drillhole_visual_qa_no_inventory_returns_critical() -> None:
    result = _run(drillhole_visual_qa, collar_id="c1")
    assert result["visualization_ready"] is False
    assert result["supported_visualizations"] == []


# ──────────────────────── visual_readiness ────────────────────────


def test_visual_readiness_strip_log_with_enough_data() -> None:
    result = _run(
        visual_readiness, viz_kind="strip_log", collar_id="c1",
        inventory={"interval_count": 10, "has_total_depth": 1},
    )
    assert result["ready"] is True
    assert "strip_log" in result["supported"]


def test_visual_readiness_strip_log_missing_depth() -> None:
    result = _run(
        visual_readiness, viz_kind="strip_log", collar_id="c1",
        inventory={"interval_count": 5, "has_total_depth": 0},
    )
    assert result["ready"] is False
    assert any("total_depth" in m for m in result["missing"])


def test_visual_readiness_cross_section_needs_project_id() -> None:
    result = _run(
        visual_readiness, viz_kind="cross_section",
        inventory={},
    )
    assert result["ready"] is False
    assert "project_id" in result["missing"]


def test_visual_readiness_stereonet_sparse_warns() -> None:
    result = _run(
        visual_readiness, viz_kind="stereonet", collar_id="c1",
        inventory={"structure_count": 5},  # ready but maybe sparse
    )
    assert result["ready"] is True


def test_visual_readiness_unknown_viz_kind() -> None:
    result = _run(
        visual_readiness, viz_kind="unknown_kind",  # type: ignore[arg-type]
        collar_id="c1", inventory={},
    )
    assert result["ready"] is False


# ──────────────────────── public_private_boundary ─────────────────


def test_public_private_boundary_flags_forbidden_with_only_public() -> None:
    """§2.9 violation: 'this project has uranium' when no workspace
    evidence is present."""
    result = _run(
        public_private_boundary, workspace_id="ws-1",
        retrieved_chunks=[
            {"chunk_id": "pg-1", "source_metadata": {"data_visibility": "public"}},
            {"chunk_id": "pg-2", "source_metadata": {"data_visibility": "public"}},
        ],
        candidate_response_text="This project has uranium based on regional records.",
    )
    assert result["approve_for_emission"] is False
    assert len(result["language_violations"]) == 1
    assert "public records show" in result["language_violations"][0]["suggested_revision"].lower()


def test_public_private_boundary_allows_when_workspace_evidence_present() -> None:
    """Workspace evidence backs the assertion — not a violation."""
    result = _run(
        public_private_boundary, workspace_id="ws-1",
        retrieved_chunks=[
            {"chunk_id": "ws-1", "source_metadata": {"data_visibility": "workspace"}},
        ],
        candidate_response_text="This project has uranium based on our drilling.",
    )
    assert result["approve_for_emission"] is True
    assert result["language_violations"] == []


def test_public_private_boundary_tags_chunks_by_workspace_match() -> None:
    """When chunks lack explicit data_visibility, the agent tags by
    workspace_id match."""
    result = _run(
        public_private_boundary, workspace_id="ws-1",
        retrieved_chunks=[
            {"chunk_id": "a", "workspace_id": "ws-1"},
            {"chunk_id": "b", "workspace_id": "ws-2"},
            {"chunk_id": "c"},
        ],
        candidate_response_text="Routine summary.",
    )
    tags = {t["chunk_id"]: t["data_visibility"] for t in result["tagged_chunks"]}
    assert tags["a"] == "workspace"
    assert tags["b"] == "public"   # different workspace → public
    assert tags["c"] == "public"   # no workspace match


def test_public_private_boundary_clean_response_emits() -> None:
    result = _run(
        public_private_boundary, workspace_id="ws-1",
        retrieved_chunks=[],
        candidate_response_text=(
            "Public records show uranium-related occurrences within 25 km "
            "of this project area."
        ),
    )
    assert result["approve_for_emission"] is True


# ──────────────────────── spatial_relationship ────────────────────


def test_spatial_relationship_filters_predicates() -> None:
    result = _run(
        spatial_relationship, workspace_id="ws", project_id="p",
        subject_entity_id="hole-1",
        predicate_filter=["crosscuts", "hosts"],
        relationships=[
            {"predicate": "crosscuts", "object_id": "fault-1", "evidence_chunk_ids": ["e1"]},
            {"predicate": "near",      "object_id": "intr-1", "evidence_chunk_ids": ["e2"]},
            {"predicate": "hosts",     "object_id": "lith-1", "evidence_chunk_ids": ["e3"]},
        ],
    )
    preds = [r["predicate"] for r in result["relationships"]]
    assert "near" not in preds
    assert set(preds) == {"crosscuts", "hosts"}


def test_spatial_relationship_sorted_by_confidence() -> None:
    result = _run(
        spatial_relationship, workspace_id="ws", project_id="p",
        subject_entity_id="x",
        relationships=[
            {"predicate": "near", "object_id": "a", "evidence_chunk_ids": ["e1"]},
            {"predicate": "near", "object_id": "b",
             "evidence_chunk_ids": ["e1", "e2", "e3", "e4"]},
        ],
    )
    scores = [r["confidence"] for r in result["relationships"]]
    assert scores == sorted(scores, reverse=True)


def test_spatial_relationship_explicit_confidence_wins() -> None:
    result = _run(
        spatial_relationship, workspace_id="ws", project_id="p",
        subject_entity_id="x",
        relationships=[
            {"predicate": "hosts", "object_id": "y", "confidence": 0.42},
        ],
    )
    assert result["relationships"][0]["confidence"] == 0.42


def test_spatial_relationship_empty_input_safe() -> None:
    result = _run(
        spatial_relationship, workspace_id="ws", project_id="p",
        subject_entity_id="x",
    )
    assert result["relationships"] == []
