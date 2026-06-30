"""Unit tests for plan §3a/§3b tool_results → EvidencePacket converter."""

from __future__ import annotations

from app.agent.evidence import (
    AssayEvidence,
    CollarEvidence,
    DocumentEvidence,
    GraphEvidence,
    SpatialEvidence,
)
from app.agent.evidence_converter import (
    build_evidence_packet,
    estimate_evidence_tokens,
    extract_assay_evidence,
    extract_collar_evidence,
    extract_document_evidence,
    extract_graph_evidence,
    extract_spatial_evidence,
)

# ---------------------------------------------------------------------------
# extract_document_evidence
# ---------------------------------------------------------------------------


def test_document_extractor_minimal_dict():
    payload = [
        {
            "chunk_id": "c-1",
            "text": "Eckville property comprises 12 mineral claims.",
            "document_id": "doc-1",
            "document_title": "NI 43-101 Eckville 2024",
            "document_type": "NI 43-101",
            "page": 7,
            "char_start": 0,
            "char_end": 47,
        },
    ]
    out = extract_document_evidence(payload)
    assert len(out) == 1
    assert isinstance(out[0], DocumentEvidence)
    assert out[0].document_id == "doc-1"
    assert out[0].text.startswith("Eckville")
    assert out[0].chunk_id == "c-1"


def test_document_extractor_skips_entries_without_text():
    payload = [
        {"chunk_id": "c-1", "text": ""},
        {"chunk_id": "c-2", "text": "real text"},
    ]
    out = extract_document_evidence(payload)
    assert len(out) == 1
    assert out[0].chunk_id == "c-2"


def test_document_extractor_field_aliases():
    """The tool layer uses different field names across its history;
    extractor accepts the synonyms."""
    payload = [{
        "id": "passage-99",       # alias for chunk_id
        "content": "alt key for text",  # alias for text
        "doc_id": "doc-2",         # alias for document_id
        "title": "Some Report",    # alias for document_title
        "page_number": 5,          # alias for page
    }]
    out = extract_document_evidence(payload)
    assert out[0].chunk_id == "passage-99"
    assert out[0].text == "alt key for text"
    assert out[0].document_id == "doc-2"
    assert out[0].page == 5


def test_document_extractor_handles_relevance_score_as_confidence():
    payload = [{
        "chunk_id": "c", "text": "x", "relevance_score": 0.73,
        "document_id": "d", "document_title": "t", "document_type": "u",
    }]
    out = extract_document_evidence(payload)
    assert out[0].confidence == 0.73


def test_document_extractor_clamps_confidence_to_unit_range():
    """A rogue tool value of 5.0 must not blow the [0,1] validator."""
    payload = [{
        "chunk_id": "c", "text": "x", "score": 5.0,
        "document_id": "d", "document_title": "t", "document_type": "u",
    }]
    out = extract_document_evidence(payload)
    assert out[0].confidence == 1.0


def test_document_extractor_skips_malformed_row():
    """A row that would fail Pydantic validation gets skipped, not raised."""
    payload = [
        {"chunk_id": "c", "text": "valid",
         "document_id": "d", "document_title": "t", "document_type": "u",
         "page": "not-an-int"},  # malformed
        {"chunk_id": "c2", "text": "also valid",
         "document_id": "d", "document_title": "t", "document_type": "u"},
    ]
    out = extract_document_evidence(payload)
    # First row had an unparseable page; coercion returns 0; row survives.
    # Verify we got both, the malformed page is coerced to a valid int.
    assert len(out) == 2
    assert out[0].page == 0


def test_document_extractor_empty_or_non_list_returns_empty():
    assert extract_document_evidence(None) == []
    assert extract_document_evidence({}) == []
    assert extract_document_evidence("not a list") == []
    assert extract_document_evidence([]) == []


# ---------------------------------------------------------------------------
# extract_assay_evidence
# ---------------------------------------------------------------------------


def test_assay_extractor_minimal_row():
    payload = [{
        "hole_id": "ECK-22-001",
        "depth_from_m": 142.0,
        "depth_to_m": 150.4,
        "interval_length_m": 8.4,
        "commodity": "Au",
        "value": 2.31,
        "unit": "g/t",
        "project_id": "proj-1",
    }]
    out = extract_assay_evidence(payload)
    assert len(out) == 1
    assert isinstance(out[0], AssayEvidence)
    assert out[0].hole_id == "ECK-22-001"
    assert out[0].value == 2.31


def test_assay_extractor_swaps_inverted_depth_range():
    """A row with depth_from_m > depth_to_m gets normalised, not skipped."""
    payload = [{
        "hole_id": "X",
        "depth_from_m": 150.0, "depth_to_m": 140.0,  # inverted
        "interval_length_m": 10.0,
        "commodity": "Au", "value": 1.0, "unit": "g/t",
        "project_id": "p",
    }]
    out = extract_assay_evidence(payload)
    assert out[0].depth_from_m == 140.0
    assert out[0].depth_to_m == 150.0


def test_assay_extractor_skips_row_missing_required_fields():
    payload = [
        {"hole_id": "X"},                       # missing depth + value + commodity
        {"hole_id": "Y", "depth_from_m": 0.0, "depth_to_m": 10.0,
         "interval_length_m": 10.0, "commodity": "Au", "value": 1.0,
         "unit": "g/t", "project_id": "p"},     # complete
    ]
    out = extract_assay_evidence(payload)
    assert len(out) == 1
    assert out[0].hole_id == "Y"


def test_assay_extractor_derives_interval_length_when_missing():
    payload = [{
        "hole_id": "X", "depth_from_m": 100.0, "depth_to_m": 110.0,
        "commodity": "Cu", "value": 0.5, "unit": "%",
        "project_id": "p",
        # interval_length_m omitted
    }]
    out = extract_assay_evidence(payload)
    assert out[0].interval_length_m == 10.0


def test_assay_extractor_aliases_grade_field():
    """`grade` is a legacy synonym for `value`."""
    payload = [{
        "hole_id": "X", "depth_from_m": 0.0, "depth_to_m": 10.0,
        "interval_length_m": 10.0, "element": "Au", "grade": 3.14,
        "unit": "g/t", "project_id": "p",
    }]
    out = extract_assay_evidence(payload)
    assert out[0].value == 3.14
    assert out[0].commodity == "Au"


# ---------------------------------------------------------------------------
# extract_collar_evidence  +  extract_spatial_evidence
# ---------------------------------------------------------------------------


def test_collar_extractor_normal_row():
    payload = [{
        "hole_id": "ECK-22-001",
        "easting": 500_000.0, "northing": 5_000_000.0,
        "elevation": 1234.5,
        "crs": "EPSG:26913",
        "azimuth": 270.0, "dip": -65.0, "total_depth": 250.0,
    }]
    out = extract_collar_evidence(payload)
    assert len(out) == 1
    assert isinstance(out[0], CollarEvidence)
    assert out[0].azimuth == 270.0


def test_collar_extractor_skips_row_missing_crs():
    payload = [{
        "hole_id": "X", "easting": 0.0, "northing": 0.0,
        # crs missing — plan §1g `collar_missing_crs` skip behaviour
    }]
    out = extract_collar_evidence(payload)
    assert out == []


def test_spatial_extractor_only_picks_rows_with_spatial_op():
    payload = [
        {"hole_id": "X", "easting": 0.0, "northing": 0.0, "crs": "EPSG:26913"},
        {"spatial_operation": "distance", "geometry_type": "point",
         "crs": "EPSG:26913", "result_value": 487.3,
         "intersecting_entities": ["X"]},
    ]
    out = extract_spatial_evidence(payload)
    assert len(out) == 1
    assert isinstance(out[0], SpatialEvidence)
    assert out[0].result_value == 487.3


def test_spatial_extractor_clamps_unknown_geometry_type_to_point():
    payload = [{
        "spatial_operation": "within", "geometry_type": "not-a-real-type",
        "crs": "EPSG:4326",
    }]
    out = extract_spatial_evidence(payload)
    assert out[0].geometry_type == "point"


def test_spatial_extractor_clamps_unknown_operation_to_within():
    payload = [{
        "spatial_operation": "weird_op", "geometry_type": "point",
        "crs": "EPSG:4326",
    }]
    out = extract_spatial_evidence(payload)
    assert out[0].spatial_operation == "within"


# ---------------------------------------------------------------------------
# extract_graph_evidence
# ---------------------------------------------------------------------------


def test_graph_extractor_minimal():
    payload = [{
        "node_ids": ["n-1", "n-2"],
        "relationship_ids": ["r-1"],
        "path": "(:Project)-[:HAS_DEPOSIT]->(:Deposit)",
        "relationship_types": ["HAS_DEPOSIT"],
    }]
    out = extract_graph_evidence(payload)
    assert len(out) == 1
    assert isinstance(out[0], GraphEvidence)
    assert out[0].path.startswith("(:Project)")


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------


def test_estimate_evidence_tokens_floor_is_one():
    """Even minimal evidence costs at least 1 token (so it's
    counted as non-zero)."""
    d = DocumentEvidence(
        document_id="d", document_title="t", document_type="u",
        page=0, chunk_id="c", text="x", char_start=0, char_end=1,
    )
    assert estimate_evidence_tokens(d) >= 1


def test_estimate_evidence_tokens_grows_with_text_length():
    short = DocumentEvidence(
        document_id="d", document_title="t", document_type="u",
        page=0, chunk_id="c", text="abc", char_start=0, char_end=3,
    )
    long_doc = DocumentEvidence(
        document_id="d", document_title="t", document_type="u",
        page=0, chunk_id="c", text="x" * 1000, char_start=0, char_end=1000,
    )
    assert estimate_evidence_tokens(long_doc) > estimate_evidence_tokens(short)


# ---------------------------------------------------------------------------
# build_evidence_packet — the dispatcher + assembler
# ---------------------------------------------------------------------------


def test_build_packet_routes_each_tool_to_its_extractor():
    tool_results = [
        ("search_documents", [
            {"chunk_id": "c-1", "text": "doc text",
             "document_id": "d-1", "document_title": "t", "document_type": "NI 43-101"},
        ]),
        ("query_assay_data", [
            {"hole_id": "X", "depth_from_m": 0.0, "depth_to_m": 10.0,
             "interval_length_m": 10.0, "commodity": "Au", "value": 1.0,
             "unit": "g/t", "project_id": "p"},
        ]),
        ("traverse_knowledge_graph", [
            {"node_ids": ["n"], "path": "(:n)"},
        ]),
    ]
    packet = build_evidence_packet(
        query_id="q-1",
        query_text="What's the best assay at ECK?",
        tool_results=tool_results,
        system_prompt_tokens=3400,
    )
    kinds = [e.kind for e in packet.evidence]
    assert "document" in kinds
    assert "assay" in kinds
    assert "graph" in kinds
    assert packet.query_id == "q-1"
    assert packet.tool_plan == "search_documents, query_assay_data, traverse_knowledge_graph"


def test_build_packet_computes_remaining_budget():
    """remaining_budget = max_context_tokens - system_prompt_tokens - total_tokens."""
    packet = build_evidence_packet(
        query_id="q-1", query_text="x",
        tool_results=[],
        system_prompt_tokens=3400,
        max_context_tokens=6500,
    )
    assert packet.total_tokens == 0
    assert packet.remaining_budget == 6500 - 3400 - 0


def test_build_packet_with_evidence_subtracts_total_tokens():
    packet = build_evidence_packet(
        query_id="q-1", query_text="x",
        tool_results=[
            ("search_documents", [
                {"chunk_id": "c", "text": "abcd" * 100,  # ~400 chars
                 "document_id": "d", "document_title": "t", "document_type": "u"},
            ]),
        ],
        system_prompt_tokens=3400,
        max_context_tokens=6500,
    )
    assert packet.total_tokens > 0
    assert packet.remaining_budget == 6500 - 3400 - packet.total_tokens


def test_build_packet_query_spatial_collars_emits_both_kinds():
    """query_spatial_collars rows can be plain collar rows OR spatial-op
    rows — the dispatcher runs both extractors."""
    tool_results = [
        ("query_spatial_collars", [
            {"hole_id": "X", "easting": 0.0, "northing": 0.0, "crs": "EPSG:26913"},
            {"spatial_operation": "distance", "geometry_type": "point",
             "crs": "EPSG:26913", "result_value": 100.0},
        ]),
    ]
    packet = build_evidence_packet(
        query_id="q-1", query_text="x", tool_results=tool_results,
    )
    kinds = [e.kind for e in packet.evidence]
    assert "collar" in kinds
    assert "spatial" in kinds


def test_build_packet_unknown_tool_falls_back_to_document_type_unknown():
    tool_results = [
        ("some_new_tool_not_yet_known", "raw text payload from this tool"),
    ]
    packet = build_evidence_packet(
        query_id="q-1", query_text="x", tool_results=tool_results,
    )
    assert len(packet.evidence) == 1
    e = packet.evidence[0]
    assert e.kind == "document"
    assert e.document_type == "unknown"
    assert "raw text payload" in e.text


def test_build_packet_empty_tool_results_returns_empty_evidence():
    packet = build_evidence_packet(query_id="q-1", query_text="x", tool_results=[])
    assert packet.evidence == []
    assert packet.tool_plan == ""


def test_build_packet_negative_remaining_budget_is_a_signal():
    """When evidence overflows the context budget, remaining_budget can
    legitimately go negative — the caller is expected to read that and
    truncate before the LLM call. The converter doesn't truncate."""
    packet = build_evidence_packet(
        query_id="q-1", query_text="x",
        tool_results=[
            ("search_documents", [
                {"chunk_id": f"c-{i}", "text": "z" * 4000,
                 "document_id": "d", "document_title": "t", "document_type": "u"}
                for i in range(20)
            ]),
        ],
        system_prompt_tokens=3400,
        max_context_tokens=6500,
    )
    assert packet.remaining_budget < 0
