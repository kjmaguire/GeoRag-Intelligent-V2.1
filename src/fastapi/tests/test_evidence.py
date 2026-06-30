"""Unit tests for plan §3a typed evidence objects.

Pure-data tests — no I/O, no fixtures beyond plain construction.
"""

from __future__ import annotations

import json

import pytest

from app.agent.evidence import (
    AssayEvidence,
    CollarEvidence,
    DocumentEvidence,
    EvidencePacket,
    GraphEvidence,
    SpatialEvidence,
    TableEvidence,
)

# ---------------------------------------------------------------------------
# Construction defaults
# ---------------------------------------------------------------------------


def _minimal_doc(**overrides) -> DocumentEvidence:
    defaults = {
        "document_id": "doc-1",
        "document_title": "NI 43-101 Eckville 2024",
        "document_type": "NI 43-101",
        "page": 7,
        "chunk_id": "chunk-1",
        "text": "Eckville property comprises 12 mineral claims.",
        "char_start": 0,
        "char_end": 47,
    }
    return DocumentEvidence(**{**defaults, **overrides})


def test_document_evidence_has_fresh_uuid_per_instance():
    a = _minimal_doc()
    b = _minimal_doc()
    assert a.evidence_id != b.evidence_id
    assert len(a.evidence_id) == 36  # UUID4 canonical length


def test_document_evidence_kind_discriminator_is_document():
    assert _minimal_doc().kind == "document"


def test_document_evidence_defaults():
    d = _minimal_doc()
    assert d.confidence == 1.0
    assert d.is_current is True
    assert d.authority_rank == 3
    assert d.parent_chunk_id is None
    assert d.taxonomy_term_id is None
    assert d.vocab_tags == []
    assert d.extracted_entities == []
    assert d.section == ""


# ---------------------------------------------------------------------------
# Field validators
# ---------------------------------------------------------------------------


def test_document_evidence_char_end_must_not_precede_char_start():
    with pytest.raises(ValueError, match="char_end"):
        DocumentEvidence(
            document_id="d", document_title="t", document_type="NI 43-101",
            page=1, chunk_id="c", text="x",
            char_start=100, char_end=50,
        )


def test_document_evidence_confidence_bounds():
    with pytest.raises(ValueError):
        _minimal_doc(confidence=1.5)
    with pytest.raises(ValueError):
        _minimal_doc(confidence=-0.1)


def test_document_evidence_authority_rank_bounds():
    _minimal_doc(authority_rank=1)  # ok
    _minimal_doc(authority_rank=5)  # ok
    with pytest.raises(ValueError):
        _minimal_doc(authority_rank=0)
    with pytest.raises(ValueError):
        _minimal_doc(authority_rank=6)


def test_assay_evidence_depth_to_must_not_precede_depth_from():
    with pytest.raises(ValueError, match="depth_to_m"):
        AssayEvidence(
            project_id="p", hole_id="ECK-22-001",
            depth_from_m=150.0, depth_to_m=140.0,
            interval_length_m=10.0,
            commodity="Au", value=2.31, unit="g/t",
        )


def test_collar_evidence_crs_required():
    """Plan §1g rule `collar_missing_crs` — CRS string can't be empty."""
    with pytest.raises(ValueError):
        CollarEvidence(
            hole_id="ECK-22-001",
            easting=500_000.0, northing=5_000_000.0,
            crs="",
        )


def test_collar_evidence_azimuth_dip_bounds():
    CollarEvidence(
        hole_id="X", easting=0.0, northing=0.0, crs="EPSG:26913",
        azimuth=180.0, dip=-60.0,
    )  # ok
    with pytest.raises(ValueError):
        CollarEvidence(
            hole_id="X", easting=0.0, northing=0.0, crs="EPSG:26913",
            azimuth=400.0,
        )
    with pytest.raises(ValueError):
        CollarEvidence(
            hole_id="X", easting=0.0, northing=0.0, crs="EPSG:26913",
            dip=-100.0,
        )


# ---------------------------------------------------------------------------
# Strict `extra=forbid` behaviour
# ---------------------------------------------------------------------------


def test_extra_fields_are_rejected():
    """`model_config = extra='forbid'` catches typos at construction time."""
    with pytest.raises(ValueError, match="(extra|unknown)"):
        _minimal_doc(authorty_rank=2)  # typo on purpose


# ---------------------------------------------------------------------------
# Other evidence types — smoke construction
# ---------------------------------------------------------------------------


def test_table_evidence_minimal():
    t = TableEvidence(
        document_id="d", page=42, table_id="tbl-1",
        column_names=["From (m)", "To (m)", "Au (g/t)"],
        cell_values=[
            {"From (m)": 142.0, "To (m)": 150.4, "Au (g/t)": 2.31},
            {"From (m)": 150.4, "To (m)": 158.0, "Au (g/t)": 0.8},
        ],
        units={"From (m)": "m", "To (m)": "m", "Au (g/t)": "g/t"},
    )
    assert t.kind == "table"
    assert len(t.cell_values) == 2
    assert t.units["Au (g/t)"] == "g/t"


def test_assay_evidence_minimal():
    a = AssayEvidence(
        project_id="proj-1", hole_id="ECK-22-001",
        depth_from_m=142.0, depth_to_m=150.4, interval_length_m=8.4,
        commodity="Au", value=2.31, unit="g/t",
    )
    assert a.kind == "assay"
    assert a.is_composite is False
    assert a.qaqc_flags == []


def test_spatial_evidence_minimal():
    s = SpatialEvidence(
        geometry_type="point", crs="EPSG:26913",
        spatial_operation="distance", result_value=487.3,
        intersecting_entities=["ECK-22-001"],
    )
    assert s.kind == "spatial"
    assert s.result_value == 487.3


def test_graph_evidence_minimal():
    g = GraphEvidence(
        node_ids=["n-1", "n-2"], relationship_ids=["r-1"],
        path="(:Project)-[:HAS_DEPOSIT]->(:Deposit)",
        relationship_types=["HAS_DEPOSIT"],
    )
    assert g.kind == "graph"


# ---------------------------------------------------------------------------
# EvidencePacket — the bundle
# ---------------------------------------------------------------------------


def test_evidence_packet_holds_mixed_kinds():
    packet = EvidencePacket(
        query_id="q-1", query_text="What Au grade did ECK-22-001 return?",
        tool_plan="search_documents, query_assay_data",
        evidence=[
            _minimal_doc(),
            AssayEvidence(
                project_id="p", hole_id="ECK-22-001",
                depth_from_m=142.0, depth_to_m=150.4,
                interval_length_m=8.4,
                commodity="Au", value=2.31, unit="g/t",
            ),
        ],
        total_tokens=400, system_prompt_tokens=3400,
        remaining_budget=2700,
    )
    assert len(packet.evidence) == 2
    assert packet.evidence[0].kind == "document"
    assert packet.evidence[1].kind == "assay"


def test_by_kind_filter():
    doc = _minimal_doc()
    assay = AssayEvidence(
        project_id="p", hole_id="X",
        depth_from_m=0.0, depth_to_m=10.0, interval_length_m=10.0,
        commodity="Au", value=1.0, unit="g/t",
    )
    packet = EvidencePacket(
        query_id="q-1", query_text="x",
        evidence=[doc, assay, _minimal_doc(document_id="doc-2", chunk_id="chunk-2")],
    )
    assert len(packet.by_kind("document")) == 2
    assert len(packet.by_kind("assay")) == 1
    assert packet.by_kind("collar") == []


def test_evidence_ids_returns_in_order():
    a = _minimal_doc()
    b = _minimal_doc(document_id="doc-2", chunk_id="chunk-2")
    packet = EvidencePacket(query_id="q-1", query_text="x", evidence=[a, b])
    assert packet.evidence_ids() == [a.evidence_id, b.evidence_id]


# ---------------------------------------------------------------------------
# Discriminated-union round-trip via JSON
# ---------------------------------------------------------------------------


def test_packet_round_trips_through_json_with_discriminator():
    """Pydantic v2 should re-route each member to the right concrete class
    via the `kind` discriminator when deserializing from a JSON dict."""
    original = EvidencePacket(
        query_id="q-1", query_text="x",
        evidence=[
            _minimal_doc(),
            CollarEvidence(
                hole_id="ECK-22-001",
                easting=500_000.0, northing=5_000_000.0,
                crs="EPSG:26913",
            ),
            SpatialEvidence(
                geometry_type="point", crs="EPSG:26913",
                spatial_operation="within",
            ),
        ],
    )

    dumped = original.model_dump_json()
    rebuilt = EvidencePacket.model_validate_json(dumped)

    assert len(rebuilt.evidence) == 3
    assert isinstance(rebuilt.evidence[0], DocumentEvidence)
    assert isinstance(rebuilt.evidence[1], CollarEvidence)
    assert isinstance(rebuilt.evidence[2], SpatialEvidence)
    assert rebuilt.evidence[1].hole_id == "ECK-22-001"


def test_unknown_kind_in_json_rejects():
    """Discriminator should refuse to deserialize an unknown kind."""
    payload = json.dumps({
        "query_id": "q-1",
        "query_text": "x",
        "evidence": [{"kind": "definitely_not_a_real_kind", "evidence_id": "e-1"}],
    })
    with pytest.raises(ValueError):
        EvidencePacket.model_validate_json(payload)
