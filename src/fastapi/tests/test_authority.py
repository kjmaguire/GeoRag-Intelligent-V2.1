"""Unit tests for plan §3b document authority ranking."""

from __future__ import annotations

import pytest

from app.agent.authority import (
    DEFAULT_AUTHORITY_RANK,
    annotate_evidence_packet_with_authority,
    infer_authority_rank,
    iter_top_authority,
    rank_evidence_by_authority,
)
from app.agent.evidence import (
    AssayEvidence,
    CollarEvidence,
    DocumentEvidence,
    EvidencePacket,
)


# ---------------------------------------------------------------------------
# infer_authority_rank — table-driven
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "document_type,expected_rank",
    [
        # Rank 1 — Very High
        ("NI 43-101", 1),
        ("NI43-101", 1),
        ("ni43101", 1),
        ("Technical Report", 1),
        ("technical-report", 1),
        ("Feasibility Study", 1),
        ("FS", 1),
        ("PFS", 1),
        ("PEA", 1),
        ("Resource Estimate", 1),
        ("Reserve Statement", 1),
        ("JORC", 1),
        ("CRIRSCO", 1),
        # Rank 2 — High
        ("Assessment Report", 2),
        ("Annual Report", 2),
        ("annual-filing", 2),
        ("Fact Sheet", 2),
        ("43-101F1", 2),
        ("Government Disclosure", 2),
        ("SEDAR", 2),
        # Rank 3 — Medium
        ("Press Release", 3),
        ("press-release", 3),
        ("Investor Presentation", 3),
        ("investor-deck", 3),
        ("Corporate Presentation", 3),
        ("News Release", 3),
        # Rank 4 — Medium-Low
        ("Historical Report", 4),
        ("Archived Report", 4),
        ("Archival Report", 4),
        ("Legacy Data", 4),
        # Rank 5 — Low
        ("Internal Notes", 5),
        ("Internal Memo", 5),
        ("Email", 5),
        ("Field Note", 5),
        ("Uncited", 5),
    ],
)
def test_infer_authority_rank_table(document_type, expected_rank):
    assert infer_authority_rank(document_type) == expected_rank


def test_unknown_document_type_falls_back_to_default():
    assert infer_authority_rank("foobarbaz") == DEFAULT_AUTHORITY_RANK


def test_none_input_returns_default():
    assert infer_authority_rank(None) == DEFAULT_AUTHORITY_RANK


def test_empty_string_returns_default():
    assert infer_authority_rank("") == DEFAULT_AUTHORITY_RANK


def test_rank_1_wins_over_later_patterns_in_same_string():
    """If a string would match multiple patterns, the EARLIEST (highest-
    authority) wins because the table is ordered by rank ascending."""
    # "NI 43-101 / Press Release announcing the resource update" — would
    # match both Rank 1 (NI 43-101) and Rank 3 (press release).
    assert infer_authority_rank(
        "NI 43-101 / Press Release announcing the resource update",
    ) == 1


# ---------------------------------------------------------------------------
# rank_evidence_by_authority — sort behaviour
# ---------------------------------------------------------------------------


def _doc(
    title: str = "x",
    document_type: str = "NI 43-101",
    authority_rank: int = 3,
    is_current: bool = True,
    confidence: float = 1.0,
) -> DocumentEvidence:
    return DocumentEvidence(
        document_id=title.lower(),
        document_title=title,
        document_type=document_type,
        authority_rank=authority_rank,
        is_current=is_current,
        confidence=confidence,
        page=1,
        chunk_id=f"chunk-{title.lower()}",
        text=f"text from {title}",
        char_start=0,
        char_end=10,
    )


def test_packet_sorts_high_authority_first():
    packet = EvidencePacket(
        query_id="q-1", query_text="x",
        evidence=[
            _doc("PR", document_type="Press Release", authority_rank=3),
            _doc("NI", document_type="NI 43-101", authority_rank=1),
            _doc("FS", document_type="Fact Sheet", authority_rank=2),
        ],
    )
    sorted_packet = rank_evidence_by_authority(packet)
    titles = [e.document_title for e in sorted_packet.evidence]
    assert titles == ["NI", "FS", "PR"]


def test_packet_sort_is_stable_for_ties():
    """Two rank-1 docs in the same packet keep their original order."""
    packet = EvidencePacket(
        query_id="q-1", query_text="x",
        evidence=[
            _doc("NI_A", authority_rank=1),
            _doc("NI_B", authority_rank=1),
            _doc("Other", authority_rank=3),
        ],
    )
    sorted_packet = rank_evidence_by_authority(packet)
    titles = [e.document_title for e in sorted_packet.evidence]
    assert titles == ["NI_A", "NI_B", "Other"]


def test_current_documents_outrank_superseded_at_same_rank():
    packet = EvidencePacket(
        query_id="q-1", query_text="x",
        evidence=[
            _doc("Old_NI", authority_rank=1, is_current=False),
            _doc("New_NI", authority_rank=1, is_current=True),
        ],
    )
    sorted_packet = rank_evidence_by_authority(packet)
    titles = [e.document_title for e in sorted_packet.evidence]
    assert titles == ["New_NI", "Old_NI"]


def test_higher_confidence_breaks_tie_at_same_rank_and_currency():
    packet = EvidencePacket(
        query_id="q-1", query_text="x",
        evidence=[
            _doc("LowConf", authority_rank=1, confidence=0.5),
            _doc("HighConf", authority_rank=1, confidence=0.95),
        ],
    )
    sorted_packet = rank_evidence_by_authority(packet)
    titles = [e.document_title for e in sorted_packet.evidence]
    assert titles == ["HighConf", "LowConf"]


def test_non_document_evidence_interleaves_at_middle_rank():
    """Non-Document evidence kinds sort to the default mid-rank, so they
    appear between rank-1/2 documents and rank-4/5 documents."""
    packet = EvidencePacket(
        query_id="q-1", query_text="x",
        evidence=[
            _doc("Internal", authority_rank=5),
            AssayEvidence(
                project_id="p", hole_id="X",
                depth_from_m=0.0, depth_to_m=10.0, interval_length_m=10.0,
                commodity="Au", value=1.0, unit="g/t",
            ),
            _doc("NI", authority_rank=1),
            CollarEvidence(
                hole_id="X", easting=0.0, northing=0.0, crs="EPSG:26913",
            ),
        ],
    )
    sorted_packet = rank_evidence_by_authority(packet)
    kinds_in_order = [e.kind for e in sorted_packet.evidence]
    # NI (rank 1) first; assay+collar (rank 3 default); Internal (rank 5) last.
    assert kinds_in_order[0] == "document"
    assert kinds_in_order[0:1] == ["document"]
    assert kinds_in_order[-1] == "document"  # Internal
    assert sorted_packet.evidence[0].document_title == "NI"
    assert sorted_packet.evidence[-1].document_title == "Internal"


def test_pure_function_does_not_mutate_input():
    original = EvidencePacket(
        query_id="q-1", query_text="x",
        evidence=[
            _doc("PR", authority_rank=3),
            _doc("NI", authority_rank=1),
        ],
    )
    original_titles_before = [e.document_title for e in original.evidence]
    _ = rank_evidence_by_authority(original)
    original_titles_after = [e.document_title for e in original.evidence]
    assert original_titles_before == original_titles_after


# ---------------------------------------------------------------------------
# annotate_evidence_packet_with_authority
# ---------------------------------------------------------------------------


def test_annotate_refreshes_authority_rank_from_document_type():
    """A packet built with default rank 3 gets re-ranked to 1 when the
    document_type identifies it as NI 43-101."""
    packet = EvidencePacket(
        query_id="q-1", query_text="x",
        evidence=[
            _doc("NI_A", document_type="NI 43-101", authority_rank=3),
            _doc("Memo", document_type="Internal Memo", authority_rank=3),
        ],
    )
    annotated = annotate_evidence_packet_with_authority(packet)
    by_title = {e.document_title: e for e in annotated.evidence}
    assert by_title["NI_A"].authority_rank == 1
    assert by_title["Memo"].authority_rank == 5


def test_annotate_is_idempotent():
    packet = EvidencePacket(
        query_id="q-1", query_text="x",
        evidence=[_doc("NI", document_type="NI 43-101", authority_rank=1)],
    )
    once = annotate_evidence_packet_with_authority(packet)
    twice = annotate_evidence_packet_with_authority(once)
    assert (
        [e.authority_rank for e in once.evidence]
        == [e.authority_rank for e in twice.evidence]
    )


def test_annotate_preserves_non_document_evidence_unchanged():
    assay = AssayEvidence(
        project_id="p", hole_id="X",
        depth_from_m=0.0, depth_to_m=10.0, interval_length_m=10.0,
        commodity="Au", value=1.0, unit="g/t",
    )
    packet = EvidencePacket(query_id="q-1", query_text="x", evidence=[assay])
    annotated = annotate_evidence_packet_with_authority(packet)
    assert annotated.evidence[0].kind == "assay"
    assert annotated.evidence[0].value == 1.0


# ---------------------------------------------------------------------------
# iter_top_authority
# ---------------------------------------------------------------------------


def test_iter_top_authority_yields_documents_only():
    packet = EvidencePacket(
        query_id="q-1", query_text="x",
        evidence=[
            _doc("NI", authority_rank=1),
            AssayEvidence(
                project_id="p", hole_id="X",
                depth_from_m=0.0, depth_to_m=10.0, interval_length_m=10.0,
                commodity="Au", value=1.0, unit="g/t",
            ),
            _doc("FS", authority_rank=2),
        ],
    )
    top = list(iter_top_authority(packet))
    assert [e.document_title for e in top] == ["NI", "FS"]


def test_iter_top_authority_respects_limit():
    packet = EvidencePacket(
        query_id="q-1", query_text="x",
        evidence=[
            _doc("A", authority_rank=1),
            _doc("B", authority_rank=1),
            _doc("C", authority_rank=2),
            _doc("D", authority_rank=3),
        ],
    )
    top_two = list(iter_top_authority(packet, limit=2))
    assert [e.document_title for e in top_two] == ["A", "B"]


def test_iter_top_authority_zero_limit_is_zero_docs():
    packet = EvidencePacket(
        query_id="q-1", query_text="x",
        evidence=[_doc("NI", authority_rank=1)],
    )
    assert list(iter_top_authority(packet, limit=0)) == []
