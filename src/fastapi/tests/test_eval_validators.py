"""Unit tests for the doc-phase 163 §04i validators module.

Covers `validate_refusal_correctness`, `validate_citation_presence`,
and `chain_validators`. Pure functions — no DB or external services.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from app.services.eval.validators import (
    REFUSAL_PATTERNS,
    ValidatorOutcome,
    chain_validators,
    detect_refusal,
    validate_chunk_provenance,
    validate_citation_presence,
    validate_entity_resolution,
    validate_numeric_claims,
    validate_refusal_correctness,
    validate_retrieval_quality,
)
from app.services.eval.workspace_evaluator import QuestionRecord


def _make_question(
    *,
    expected_refusal: bool = False,
    expected_citations: list | None = None,
    expected_entities: list | None = None,
    expected_numeric_values: list | None = None,
) -> QuestionRecord:
    return QuestionRecord(
        question_id=uuid4(),
        question_set="core_chat",
        question_text="test",
        context_setup={},
        expected_intent_class=None,
        expected_citations=expected_citations or [],
        expected_entities=expected_entities or [],
        expected_numeric_values=expected_numeric_values or [],
        expected_refusal=expected_refusal,
        expected_refusal_reason=None,
        expected_language_compliance=[],
        difficulty="easy",
    )


# ----------------------------------------------------------------------
# detect_refusal
# ----------------------------------------------------------------------
def test_detect_refusal_canonical_phrases():
    assert detect_refusal("I cannot disclose that.") is True
    assert detect_refusal("I can't help with that.") is True
    assert detect_refusal("Insufficient evidence to answer.") is True


def test_detect_refusal_orchestrator_phrases():
    """Doc-phase 162 patterns observed in real RAG runs."""
    assert detect_refusal("I can only answer geological questions...") is True
    assert detect_refusal("No, that's not possible. The provided evidence...") is True
    assert detect_refusal("Arrow is not referenced in the provided context") is True


def test_detect_refusal_no_match_on_normal_answer():
    assert detect_refusal(
        "The Athabasca Basin hosts unconformity-related uranium deposits."
    ) is False


def test_detect_refusal_empty():
    assert detect_refusal("") is False
    assert detect_refusal(None) is False  # type: ignore[arg-type]


def test_refusal_patterns_module_export():
    """REFUSAL_PATTERNS is exported and non-trivial."""
    assert isinstance(REFUSAL_PATTERNS, list)
    assert len(REFUSAL_PATTERNS) >= 15


# ----------------------------------------------------------------------
# validate_refusal_correctness
# ----------------------------------------------------------------------
def test_refusal_validator_pass_when_refused_as_expected():
    q = _make_question(expected_refusal=True)
    out = validate_refusal_correctness(
        response_text="I cannot answer that.", question=q,
    )
    assert out.layer == "6_refusal"
    assert out.passed is True
    assert out.failure_message is None
    assert out.detail["detected_refusal"] is True
    assert out.detail["expected_refusal"] is True


def test_refusal_validator_pass_when_answered_as_expected():
    q = _make_question(expected_refusal=False)
    out = validate_refusal_correctness(
        response_text="The deposit type is unconformity-related uranium.",
        question=q,
    )
    assert out.passed is True


def test_refusal_validator_fail_when_refused_unexpectedly():
    q = _make_question(expected_refusal=False)
    out = validate_refusal_correctness(
        response_text="I cannot answer that.", question=q,
    )
    assert out.passed is False
    assert "expected_refusal=False" in out.failure_message
    assert "detected_refusal=True" in out.failure_message


def test_refusal_validator_fail_when_answered_unexpectedly():
    q = _make_question(expected_refusal=True)
    out = validate_refusal_correctness(
        response_text="The answer is 42.", question=q,
    )
    assert out.passed is False


# ----------------------------------------------------------------------
# validate_citation_presence
# ----------------------------------------------------------------------
def test_citation_validator_vacuous_pass_on_refusal():
    """When the question expects refusal, citations are optional."""
    q = _make_question(expected_refusal=True)
    out = validate_citation_presence(citations=[], question=q)
    assert out.layer == "2_citation_presence"
    assert out.passed is True
    assert out.detail["vacuous_pass_refusal_path"] is True


def test_citation_validator_fails_when_non_refusal_has_no_citations():
    """Layer 2 hard rule: non-refusal response MUST have ≥1 citation."""
    q = _make_question(expected_refusal=False)
    out = validate_citation_presence(citations=[], question=q)
    assert out.passed is False
    assert "Layer 2 violation" in out.failure_message
    assert "zero citations" in out.failure_message


def test_citation_validator_passes_with_minimum_one_citation():
    q = _make_question(expected_refusal=False)
    out = validate_citation_presence(citations=["chunk_a"], question=q)
    assert out.passed is True
    assert out.detail["citation_count"] == 1


def test_citation_validator_passes_with_many_citations():
    q = _make_question(expected_refusal=False)
    out = validate_citation_presence(
        citations=["c1", "c2", "c3", "c4"], question=q,
    )
    assert out.passed is True
    assert out.detail["citation_count"] == 4


def test_citation_validator_enforces_expected_count_when_specified():
    """When expected_citations has N entries, response must have ≥N."""
    q = _make_question(
        expected_refusal=False,
        expected_citations=[{"a": 1}, {"a": 2}, {"a": 3}],
    )
    # Only 2 actual citations — short of expected 3.
    out = validate_citation_presence(
        citations=["c1", "c2"], question=q,
    )
    assert out.passed is False
    assert "2 citations" in out.failure_message
    assert "at least 3" in out.failure_message


def test_citation_validator_passes_when_meeting_expected_count():
    q = _make_question(
        expected_refusal=False,
        expected_citations=[{"a": 1}, {"a": 2}],
    )
    out = validate_citation_presence(
        citations=["c1", "c2", "c3"], question=q,
    )
    assert out.passed is True


# ----------------------------------------------------------------------
# chain_validators
# ----------------------------------------------------------------------
def test_chain_all_pass():
    outcomes = [
        ValidatorOutcome("6_refusal", True, {}, None),
        ValidatorOutcome("2_citation_presence", True, {}, None),
    ]
    all_passed, layer, msg = chain_validators(outcomes)
    assert all_passed is True
    assert layer is None
    assert msg is None


def test_chain_first_failure_short_circuits():
    """First failing outcome's layer + message returned."""
    outcomes = [
        ValidatorOutcome("6_refusal", False, {}, "refusal mismatch"),
        ValidatorOutcome("2_citation_presence", False, {}, "no citations"),
    ]
    all_passed, layer, msg = chain_validators(outcomes)
    assert all_passed is False
    assert layer == "6_refusal"
    assert msg == "refusal mismatch"


def test_chain_late_failure_caught():
    """When an earlier validator passes but a later one fails."""
    outcomes = [
        ValidatorOutcome("6_refusal", True, {}, None),
        ValidatorOutcome("2_citation_presence", False, {}, "no citations"),
    ]
    all_passed, layer, msg = chain_validators(outcomes)
    assert all_passed is False
    assert layer == "2_citation_presence"
    assert msg == "no citations"


def test_chain_empty_list_passes():
    """No validators → vacuously passes."""
    all_passed, layer, msg = chain_validators([])
    assert all_passed is True
    assert layer is None
    assert msg is None


# ----------------------------------------------------------------------
# validate_chunk_provenance — Layer 5 (doc-phase 165)
# ----------------------------------------------------------------------
class _FakeCitation:
    """Stand-in for Pydantic Citation with the fields the validator reads."""
    def __init__(self, source_chunk_id, corpus=None, citation_id=None):
        self.source_chunk_id = source_chunk_id
        self.corpus = corpus
        self.citation_id = citation_id or source_chunk_id


class _FakeQdrant:
    """Minimal AsyncQdrantClient stand-in.

    Configure `resolved_ids` to control which chunk IDs the validator
    sees as 'present'. Any other ID gets back an empty list.
    """
    def __init__(self, resolved_ids: set[str] | None = None, raise_for: set[str] | None = None):
        self.resolved_ids = resolved_ids or set()
        self.raise_for = raise_for or set()
        self.calls: list[str] = []

    async def retrieve(self, *, collection_name, ids, with_payload, with_vectors):
        self.calls.append(str(ids))
        for cid in ids:
            if cid in self.raise_for:
                raise RuntimeError(f"injected failure for {cid}")
        return [
            {"id": cid, "payload": {}}  # only truthiness matters to the validator
            for cid in ids
            if cid in self.resolved_ids
        ]


@pytest.mark.asyncio
async def test_chunk_provenance_vacuous_pass_on_refusal_with_no_citations():
    q = _make_question(expected_refusal=True)
    qd = _FakeQdrant()
    out = await validate_chunk_provenance(
        citations=[], qdrant_client=qd, question=q,
    )
    assert out.layer == "5_chunk_provenance"
    assert out.passed is True
    assert out.detail["vacuous_pass_refusal_path"] is True
    assert len(qd.calls) == 0


@pytest.mark.asyncio
async def test_chunk_provenance_passes_when_all_ids_resolve():
    q = _make_question(expected_refusal=False)
    qd = _FakeQdrant(resolved_ids={"chunk_a", "chunk_b"})
    citations = [
        _FakeCitation("chunk_a", corpus="internal_archive"),
        _FakeCitation("chunk_b", corpus="internal_archive"),
    ]
    out = await validate_chunk_provenance(
        citations=citations, qdrant_client=qd, question=q,
    )
    assert out.passed is True
    assert out.detail["qdrant_resolved"] == 2
    assert out.detail["pgeo_skipped"] == 0


@pytest.mark.asyncio
async def test_chunk_provenance_fails_on_unresolved_id():
    q = _make_question(expected_refusal=False)
    # Only chunk_a is in Qdrant; chunk_b is hallucinated.
    qd = _FakeQdrant(resolved_ids={"chunk_a"})
    citations = [
        _FakeCitation("chunk_a", corpus="internal_archive"),
        _FakeCitation("chunk_b", corpus="internal_archive"),
    ]
    out = await validate_chunk_provenance(
        citations=citations, qdrant_client=qd, question=q,
    )
    assert out.passed is False
    assert "Layer 5 violation" in out.failure_message
    assert "chunk_b" in out.failure_message
    assert out.detail["unresolved_count"] == 1


@pytest.mark.asyncio
async def test_chunk_provenance_skips_public_geoscience_citations():
    """PGEO citations have structured IDs that aren't Qdrant points;
    they're skipped (counted in pgeo_skipped) and pass."""
    q = _make_question(expected_refusal=False)
    qd = _FakeQdrant()  # nothing resolves
    citations = [
        _FakeCitation("pgeo:bc_minfile:001", corpus="public_geoscience"),
        _FakeCitation("pgeo:nrcan_mines:001", corpus="public_geoscience"),
    ]
    out = await validate_chunk_provenance(
        citations=citations, qdrant_client=qd, question=q,
    )
    assert out.passed is True
    assert out.detail["pgeo_skipped"] == 2
    assert out.detail["qdrant_lookups"] == 0


@pytest.mark.asyncio
async def test_chunk_provenance_handles_qdrant_errors_as_unresolved():
    """A Qdrant exception per chunk counts as unresolved (defensive)."""
    q = _make_question(expected_refusal=False)
    qd = _FakeQdrant(raise_for={"chunk_x"})
    citations = [_FakeCitation("chunk_x", corpus="internal_archive")]
    out = await validate_chunk_provenance(
        citations=citations, qdrant_client=qd, question=q,
    )
    assert out.passed is False
    assert "qdrant_error" in out.detail["unresolved"][0]["reason"]


@pytest.mark.asyncio
async def test_chunk_provenance_works_with_dict_citations():
    """Validator accepts dict-shaped citations (Pydantic v3 round-trip safe)."""
    q = _make_question(expected_refusal=False)
    qd = _FakeQdrant(resolved_ids={"chunk_a"})
    citations = [
        {"source_chunk_id": "chunk_a", "corpus": "internal_archive",
         "citation_id": "[NI43-1]"},
    ]
    out = await validate_chunk_provenance(
        citations=citations, qdrant_client=qd, question=q,
    )
    assert out.passed is True


@pytest.mark.asyncio
async def test_chunk_provenance_skips_data_type_citations():
    """DATA citation_type points at silver.* SQL tables, not Qdrant.
    These are skipped — Layer 5 doesn't look them up. SQL-provenance
    validator (doc-phase 167+) handles those separately."""
    q = _make_question(expected_refusal=False)
    qd = _FakeQdrant(resolved_ids={"chunk_a"})
    citations = [
        _FakeCitation("chunk_a", corpus="internal_archive"),
        _FakeCitation("silver.collars:count=20", corpus="internal_archive"),
    ]
    # Add citation_type to second citation.
    citations[0].citation_type = "NI43"
    citations[1].citation_type = "DATA"

    out = await validate_chunk_provenance(
        citations=citations, qdrant_client=qd, question=q,
    )
    assert out.passed is True
    assert out.detail["qdrant_lookups"] == 1
    assert out.detail["qdrant_resolved"] == 1
    assert out.detail["sql_skipped"] == 1


@pytest.mark.asyncio
async def test_chunk_provenance_refusal_path_passes_even_with_synthetic_citations():
    """expected_refusal=True → Layer 5 vacuously passes regardless of
    citation count or whether the chunk_ids are sentinels."""
    q = _make_question(expected_refusal=True)
    qd = _FakeQdrant()  # nothing resolves
    citations = [
        _FakeCitation("georag_reports:empty", corpus="internal_archive"),
        _FakeCitation("fake_chunk", corpus="internal_archive"),
    ]
    out = await validate_chunk_provenance(
        citations=citations, qdrant_client=qd, question=q,
    )
    assert out.passed is True
    assert out.detail["vacuous_pass_refusal_path"] is True
    # No Qdrant calls because the early-return fired.
    assert len(qd.calls) == 0


# ----------------------------------------------------------------------
# validate_entity_resolution — Layer 4 (doc-phase 166)
# ----------------------------------------------------------------------
def test_entity_resolution_vacuous_pass_on_refusal():
    q = _make_question(expected_refusal=True)
    out = validate_entity_resolution(
        response_text="anything",
        question=q,
    )
    assert out.passed is True
    assert out.detail["vacuous_pass_refusal_path"] is True


def test_entity_resolution_vacuous_pass_on_structural_only_specs():
    """Mechanical questions with `{"expected_route": "accept"}` have no
    extractable `name` field — validator vacuously passes."""
    q = _make_question(
        expected_refusal=False,
        expected_entities=[
            {"expected_route": "accept", "expected_reason": None},
            {"required_section_ids": ["a", "b", "c"]},
        ],
    )
    out = validate_entity_resolution(response_text="…", question=q)
    assert out.passed is True
    assert out.detail["vacuous_pass_no_extractable_names"] is True


def test_entity_resolution_passes_when_entities_mentioned():
    q = _make_question(
        expected_refusal=False,
        expected_entities=[
            {"entity_kind": "rock", "name": "Athabasca Sandstone"},
            {"entity_kind": "deposit", "name": "McArthur River"},
        ],
    )
    out = validate_entity_resolution(
        response_text=(
            "The McArthur River deposit is hosted in the Athabasca "
            "Sandstone above the basement unconformity."
        ),
        question=q,
    )
    assert out.passed is True
    assert out.detail["all_entities_found"] is True


def test_entity_resolution_case_insensitive_match():
    q = _make_question(
        expected_refusal=False,
        expected_entities=[{"name": "Athabasca"}],
    )
    out = validate_entity_resolution(
        response_text="the athabasca basin hosts uranium",
        question=q,
    )
    assert out.passed is True


def test_entity_resolution_fails_with_missing_entities():
    q = _make_question(
        expected_refusal=False,
        expected_entities=[
            {"name": "McArthur River"},
            {"name": "Cigar Lake"},
            {"name": "Phoenix"},
        ],
    )
    out = validate_entity_resolution(
        response_text="The McArthur River deposit is in Saskatchewan.",
        question=q,
    )
    assert out.passed is False
    assert "Layer 4 violation" in out.failure_message
    assert "Cigar Lake" in out.failure_message
    assert "Phoenix" in out.failure_message
    assert set(out.detail["missing_entities"]) == {"Cigar Lake", "Phoenix"}


def test_entity_resolution_supports_entity_name_alias():
    """`entity_name` is also a valid key (alongside `name`)."""
    q = _make_question(
        expected_refusal=False,
        expected_entities=[{"entity_name": "Detour Lake"}],
    )
    out = validate_entity_resolution(
        response_text="Detour Lake is an Agnico Eagle gold mine in Ontario.",
        question=q,
    )
    assert out.passed is True


# ----------------------------------------------------------------------
# validate_numeric_claims — Layer 3 (doc-phase 167)
# ----------------------------------------------------------------------
def test_numeric_claims_vacuous_pass_on_refusal():
    q = _make_question(expected_refusal=True)
    out = validate_numeric_claims(response_text="any", question=q)
    assert out.passed is True
    assert out.detail["vacuous_pass_refusal_path"] is True


def test_numeric_claims_vacuous_pass_when_no_expectations():
    q = _make_question(expected_refusal=False)
    out = validate_numeric_claims(response_text="answer", question=q)
    assert out.passed is True
    assert out.detail["vacuous_pass_no_expectations"] is True


def test_numeric_claims_vacuous_pass_on_structural_only_specs():
    """Mechanical questions with `path`/`source_table` but no
    `expected_value` need silver data to validate. Layer 3 vacuously
    passes today; full ground truth lands when silver data is wired."""
    q = _make_question(
        expected_refusal=False,
        expected_numeric_values=[
            {"path": "max_au_g_t", "source_table": "silver.assays",
             "tolerance_pct": 0.5},
        ],
    )
    out = validate_numeric_claims(
        response_text="The maximum is 12.5 g/t.", question=q,
    )
    assert out.passed is True
    assert out.detail["vacuous_pass_needs_silver_data"] is True


def test_numeric_claims_pass_when_response_contains_expected_value():
    q = _make_question(
        expected_refusal=False,
        expected_numeric_values=[
            {"expected_value": 685.5, "unit": "m", "tolerance_pct": 0},
        ],
    )
    out = validate_numeric_claims(
        response_text="The drillhole reached a total depth of 685.5 m.",
        question=q,
    )
    assert out.passed is True


def test_numeric_claims_pass_within_tolerance():
    """tolerance_pct=1.0 → 685.5 ± 6.855."""
    q = _make_question(
        expected_refusal=False,
        expected_numeric_values=[
            {"expected_value": 685.5, "tolerance_pct": 1.0},
        ],
    )
    # Reported 690.0 is within 1% of 685.5 (delta=4.5, tolerance=6.855).
    out = validate_numeric_claims(
        response_text="depth reported as 690.0", question=q,
    )
    assert out.passed is True


def test_numeric_claims_fail_outside_tolerance():
    q = _make_question(
        expected_refusal=False,
        expected_numeric_values=[
            {"expected_value": 100.0, "tolerance_pct": 1.0},
        ],
    )
    # Reported 250 is way outside 1% of 100.
    out = validate_numeric_claims(
        response_text="result is 250", question=q,
    )
    assert out.passed is False
    assert "Layer 3 violation" in out.failure_message


def test_numeric_claims_fail_when_value_missing():
    q = _make_question(
        expected_refusal=False,
        expected_numeric_values=[
            {"expected_value": 42, "tolerance_pct": 0},
        ],
    )
    out = validate_numeric_claims(
        response_text="no numbers here", question=q,
    )
    assert out.passed is False


# ----------------------------------------------------------------------
# validate_retrieval_quality — Layer 1 (doc-phase 168)
# ----------------------------------------------------------------------
class _ScoredCitation:
    def __init__(self, citation_id, relevance_score):
        self.citation_id = citation_id
        self.relevance_score = relevance_score


def test_retrieval_quality_vacuous_pass_on_refusal():
    q = _make_question(expected_refusal=True)
    citations = [_ScoredCitation("[NI43-1]", 0.1)]  # below gate
    out = validate_retrieval_quality(citations=citations, question=q)
    assert out.passed is True
    assert out.detail["vacuous_pass_refusal_path"] is True


def test_retrieval_quality_pass_all_above_gate():
    q = _make_question(expected_refusal=False)
    citations = [
        _ScoredCitation("[NI43-1]", 0.85),
        _ScoredCitation("[NI43-2]", 0.72),
    ]
    out = validate_retrieval_quality(citations=citations, question=q)
    assert out.passed is True
    assert out.detail["scored_count"] == 2


def test_retrieval_quality_fail_below_gate():
    q = _make_question(expected_refusal=False)
    citations = [
        _ScoredCitation("[NI43-1]", 0.85),
        _ScoredCitation("[NI43-2]", 0.3),  # below 0.5 default
    ]
    out = validate_retrieval_quality(citations=citations, question=q)
    assert out.passed is False
    assert "Layer 1 violation" in out.failure_message
    assert "[NI43-2]" in out.failure_message


def test_retrieval_quality_custom_threshold():
    q = _make_question(expected_refusal=False)
    citations = [_ScoredCitation("[NI43-1]", 0.6)]
    # Default 0.5 passes; custom 0.7 fails.
    assert validate_retrieval_quality(
        citations=citations, question=q,
    ).passed is True
    assert validate_retrieval_quality(
        citations=citations, question=q, min_relevance_score=0.7,
    ).passed is False


def test_retrieval_quality_unscored_citations_skipped():
    """Citations without a relevance_score don't fail Layer 1 —
    they're just counted in unscored_count."""
    q = _make_question(expected_refusal=False)
    citations = [
        {"citation_id": "[NI43-1]"},  # no score field
        _ScoredCitation("[NI43-2]", 0.8),
    ]
    out = validate_retrieval_quality(citations=citations, question=q)
    assert out.passed is True
    assert out.detail["scored_count"] == 1
    assert out.detail["unscored_count"] == 1
