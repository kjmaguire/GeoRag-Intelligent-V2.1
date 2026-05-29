"""§7 Report Builder agent graduations (Phase H4)."""
from __future__ import annotations

import asyncio

import pytest

from app.agents.phase7.appendix_builder import appendix_builder
from app.agents.phase7.claim_validator import claim_validator
from app.agents.phase7.evidence_curator import evidence_curator
from app.agents.phase7.map_chart_planner import map_chart_planner
from app.agents.phase7.presentation_coach import presentation_coach
from app.agents.phase7.report_planner import report_planner


def _inner(agent):
    return getattr(agent, "__wrapped__", agent)


# ─────────────────────────── report_planner ──────────────────────────


def test_report_planner_all_11_types_produce_sections() -> None:
    """Every §15.2 report_type yields at least one section."""
    inner = _inner(report_planner)
    types = [
        "weekly_project_digest", "ingestion_quality", "technical_due_diligence",
        "executive_project_intelligence", "gis_arcgis_sync", "target_recommendation",
        "public_geo_overlay", "data_room_package", "what_changed",
        "ni43101_section_pack", "csa11348_disclosure_pack",
    ]
    for t in types:
        result = asyncio.run(inner(
            ctx=None, workspace_id="ws-1", project_id="p-1", report_type=t,
        ))
        assert len(result["sections"]) >= 1
        assert result["report_type"] == t


def test_report_planner_ni43101_has_qp_sections() -> None:
    inner = _inner(report_planner)
    result = asyncio.run(inner(
        ctx=None, workspace_id="ws", project_id="p", report_type="ni43101_section_pack",
    ))
    section_ids = [s["section_id"] for s in result["sections"]]
    assert "title_page" in section_ids
    assert "interpretation" in section_ids


def test_report_planner_unknown_type_raises() -> None:
    inner = _inner(report_planner)
    with pytest.raises(ValueError):
        asyncio.run(inner(
            ctx=None, workspace_id="ws", project_id="p", report_type="bogus_type",
        ))


# ─────────────────────────── evidence_curator ────────────────────────


def test_evidence_curator_orders_by_score() -> None:
    inner = _inner(evidence_curator)
    candidates = {
        "claim-1": [
            {"source_chunk_id": "low",  "evidence_kind": "new_passages",
             "relevance_score": 0.3, "data_visibility": "public", "is_stale": False},
            {"source_chunk_id": "high", "evidence_kind": "assay_results",
             "relevance_score": 0.7, "data_visibility": "workspace", "is_stale": False},
        ],
    }
    result = asyncio.run(inner(
        ctx=None, workspace_id="ws", project_id="p", section_id="s-1",
        required_evidence_kinds=["assay_results"], claim_ids=["claim-1"],
        candidate_evidence=candidates,
    ))
    scores = [r["relevance_score"] for r in result["evidence_per_claim"]["claim-1"]]
    assert scores == sorted(scores, reverse=True)


def test_evidence_curator_flags_missing_kinds() -> None:
    inner = _inner(evidence_curator)
    candidates = {"claim-1": [
        {"source_chunk_id": "x", "evidence_kind": "collars",
         "relevance_score": 0.5, "data_visibility": "workspace"},
    ]}
    result = asyncio.run(inner(
        ctx=None, workspace_id="ws", project_id="p", section_id="s-1",
        required_evidence_kinds=["collars", "assay_results"],
        claim_ids=["claim-1"], candidate_evidence=candidates,
    ))
    assert result["sufficiency"]["section_supported"] is False
    assert "assay_results" in result["sufficiency"]["missing_kinds"]


def test_evidence_curator_stale_penalty() -> None:
    inner = _inner(evidence_curator)
    candidates = {"c": [
        {"source_chunk_id": "fresh", "evidence_kind": "alterations",
         "relevance_score": 0.5, "data_visibility": "workspace", "is_stale": False},
        {"source_chunk_id": "stale", "evidence_kind": "alterations",
         "relevance_score": 0.5, "data_visibility": "workspace", "is_stale": True},
    ]}
    result = asyncio.run(inner(
        ctx=None, workspace_id="ws", project_id="p", section_id="s-1",
        required_evidence_kinds=["alterations"], claim_ids=["c"],
        candidate_evidence=candidates,
    ))
    items = result["evidence_per_claim"]["c"]
    assert items[0]["source_chunk_id"] == "fresh"


# ─────────────────────────── claim_validator ─────────────────────────


def test_claim_validator_passes_well_formed_claim() -> None:
    inner = _inner(claim_validator)
    claims = [{
        "claim_id": "c1",
        "text": "Total depth of hole PLS-22-08 is 339 metres.",
        "evidence": [{
            "source_chunk_id": "chunk-1",
            "raw_text": "Hole PLS-22-08 reached total depth 339 m.",
        }],
    }]
    result = asyncio.run(inner(
        ctx=None, workspace_id="ws", section_id="s-1",
        claim_ids=["c1"], claims=claims,
    ))
    assert result["section_validated"] is True
    assert result["validations"][0]["validated"] is True


def test_claim_validator_flags_missing_evidence() -> None:
    inner = _inner(claim_validator)
    claims = [{"claim_id": "c1", "text": "some claim", "evidence": []}]
    result = asyncio.run(inner(
        ctx=None, workspace_id="ws", section_id="s-1",
        claim_ids=["c1"], claims=claims,
    ))
    v = result["validations"][0]
    assert v["validated"] is False
    assert v["layer_results"]["retrieval_quality"] is False


def test_claim_validator_flags_numeric_mismatch() -> None:
    inner = _inner(claim_validator)
    claims = [{
        "claim_id": "c1",
        "text": "Total depth is 339 metres.",
        "evidence": [{"source_chunk_id": "x", "raw_text": "Total depth is 510 metres."}],
    }]
    result = asyncio.run(inner(
        ctx=None, workspace_id="ws", section_id="s-1",
        claim_ids=["c1"], claims=claims,
    ))
    assert result["validations"][0]["layer_results"]["numerical_claim"] is False


def test_claim_validator_geological_constraint_dip_out_of_range() -> None:
    inner = _inner(claim_validator)
    claims = [{
        "claim_id": "c1",
        "text": "Bedding dip 95 degrees.",
        "evidence": [{"source_chunk_id": "x", "raw_text": "Bedding dip 95 degrees."}],
    }]
    result = asyncio.run(inner(
        ctx=None, workspace_id="ws", section_id="s-1",
        claim_ids=["c1"], claims=claims,
    ))
    assert result["validations"][0]["layer_results"]["geological_constraints"] is False


# ─────────────────────────── map_chart_planner ───────────────────────


def test_map_chart_planner_emits_known_kinds() -> None:
    inner = _inner(map_chart_planner)
    result = asyncio.run(inner(
        ctx=None, workspace_id="ws", project_id="p", section_id="s-1",
        pending_map_kinds=["collar_map", "target_heatmap"],
        pending_chart_kinds=["strip_log", "stereonet"],
    ))
    assert len(result["maps"]) == 2
    assert len(result["charts"]) == 2
    for m in result["maps"] + result["charts"]:
        assert m["exhibit_id"]
        # §17.4 contract: 6 metadata fields
        for field in (
            "source_data", "method", "filters", "crs",
            "citations", "confidence_warnings",
        ):
            assert field in m["metadata"]


def test_map_chart_planner_skips_unknown_kinds() -> None:
    inner = _inner(map_chart_planner)
    result = asyncio.run(inner(
        ctx=None, workspace_id="ws", project_id="p", section_id="s-1",
        pending_map_kinds=["collar_map", "bogus_kind"],
        pending_chart_kinds=[],
    ))
    assert len(result["maps"]) == 1
    assert result["maps"][0]["kind"] == "collar_map"


# ─────────────────────────── presentation_coach ──────────────────────


def test_presentation_coach_preserves_claims() -> None:
    inner = _inner(presentation_coach)
    body = "The project [claim-1] has 63 drillholes, totalling [claim-2] 12,000 metres."
    result = asyncio.run(inner(
        ctx=None, workspace_id="ws", section_id="s-1",
        body_markdown=body, tone="technical",
        claim_ids=["claim-1", "claim-2"],
    ))
    assert result["tone_applied"] == "technical"
    assert "claim-1" in result["rewritten_markdown"]
    assert "claim-2" in result["rewritten_markdown"]


def test_presentation_coach_executive_tone_marker() -> None:
    inner = _inner(presentation_coach)
    body = "Project status [c1] is on track."
    result = asyncio.run(inner(
        ctx=None, workspace_id="ws", section_id="s-1",
        body_markdown=body, tone="executive", claim_ids=["c1"],
    ))
    assert "Executive" in result["rewritten_markdown"]


def test_presentation_coach_missing_claim_raises() -> None:
    inner = _inner(presentation_coach)
    body = "Project has 63 drillholes."  # no claim markers!
    with pytest.raises(ValueError):
        asyncio.run(inner(
            ctx=None, workspace_id="ws", section_id="s-1",
            body_markdown=body, tone="technical",
            claim_ids=["claim-1"],
        ))


# ─────────────────────────── appendix_builder ────────────────────────


def test_appendix_builder_no_store_returns_inline_lengths() -> None:
    inner = _inner(appendix_builder)
    citation_payload = {
        "by_section": {
            "s-1": {
                "claim-1": [{
                    "source_chunk_id": "chunk-1",
                    "source_uri": "s3://r/report.pdf",
                    "page": 12, "sha256": "abc",
                    "source_title": "NI 43-101 example",
                }],
            }
        }
    }
    evidence_ledger = {"section_id": "s-1", "evidence_per_claim": {"claim-1": []}}
    result = asyncio.run(inner(
        ctx=None, workspace_id="ws-1", report_id="rpt-1",
        citation_payload=citation_payload, evidence_ledger=evidence_ledger,
    ))
    assert result["citation_manifest_uri"] == ""
    assert result["hash_chain_proof"]["citation_manifest_sha256"]
    assert result["inline_payloads"]["citation_manifest_bytes_len"] > 0


def test_appendix_builder_with_store_writes_artifacts() -> None:
    inner = _inner(appendix_builder)

    class FakeStore:
        def __init__(self):
            self.puts: dict[str, bytes] = {}

        async def put(self, key: str, content: bytes) -> str:
            self.puts[key] = content
            return f"s3://fake/{key}"

    store = FakeStore()
    citation_payload = {"by_section": {
        "s-1": {"c1": [{"source_chunk_id": "x", "source_uri": "u"}]}
    }}
    evidence_ledger = {"foo": "bar"}
    result = asyncio.run(inner(
        ctx=None, workspace_id="ws", report_id="rpt-1",
        citation_payload=citation_payload, evidence_ledger=evidence_ledger,
        store=store,
    ))
    assert "s3://fake/" in result["citation_manifest_uri"]
    assert len(store.puts) == 3
    assert any(k.endswith("citation_manifest.csv") for k in store.puts)
    assert any(k.endswith("source_manifest.json") for k in store.puts)
    assert any(k.endswith("evidence.json") for k in store.puts)
