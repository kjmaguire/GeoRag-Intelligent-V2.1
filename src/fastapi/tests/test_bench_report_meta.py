"""A pass rate is only interpretable next to the settings that produced it.

WHAT HAPPENED
    Two consecutive bench artefacts, eight minutes apart on 2026-06-02,
    record 20% and 75%. Nothing retrieved better in eight minutes: the
    Layer 1 recalibration moved the relevance gate from 0.5 to 0.3 and
    changed the rule from "all citations must clear the gate" to "at least
    one scored citation must".

    The report `meta` block recorded timestamp, git_sha, label,
    question_count, the filters and the timeouts. Nothing about the
    validators. So the two runs are indistinguishable from a genuine
    before/after, and anyone charting pass rate over time from
    bench_results/ reads a 55-point product improvement off a threshold
    edit.

WHY THESE FIELDS
    `_model_stack_fingerprint` already existed for this reason, and its
    docstring makes the argument: two reports are comparable only if the
    same stack produced them. A validator threshold is part of that stack
    and is the part most likely to be tuned between two runs someone then
    compares.

    REFUSAL_PATTERNS is hashed rather than inlined. It is the single
    largest determinant of the pass rate, and what a reader needs from the
    artefact is whether it changed between two runs.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def fingerprint() -> dict:
    from run_golden_benchmark import _validator_config_fingerprint

    return _validator_config_fingerprint()


def test_the_threshold_that_caused_the_jump_is_recorded(
    fingerprint: dict,
) -> None:
    """0.5 → 0.3 is the specific change the two artefacts straddle."""
    assert "min_relevance_score" in fingerprint
    assert isinstance(fingerprint["min_relevance_score"], (int, float))


def test_the_recorded_threshold_is_the_one_the_validator_uses(
    fingerprint: dict,
) -> None:
    """A fingerprint that reports a DIFFERENT number than the code applies
    is worse than none — it makes the artefact look attributable."""
    import inspect

    from app.services.eval import validators

    live = inspect.signature(
        validators.validate_retrieval_quality
    ).parameters["min_relevance_score"].default

    assert fingerprint["min_relevance_score"] == live


def test_the_refusal_patterns_are_fingerprinted(fingerprint: dict) -> None:
    assert len(fingerprint["refusal_patterns_sha256_12"]) == 12
    assert fingerprint["refusal_patterns_count"] > 0


def test_the_hash_changes_when_the_list_changes(monkeypatch) -> None:
    """The whole point. A hash that does not move when the list moves
    records nothing."""
    from run_golden_benchmark import _validator_config_fingerprint

    from app.services.eval import validators

    before = _validator_config_fingerprint()

    monkeypatch.setattr(
        validators,
        "REFUSAL_PATTERNS",
        [*validators.REFUSAL_PATTERNS, "i am unable to determine"],
    )
    after = _validator_config_fingerprint()

    assert before["refusal_patterns_sha256_12"] != after["refusal_patterns_sha256_12"]
    assert after["refusal_patterns_count"] == before["refusal_patterns_count"] + 1


def test_the_hash_is_stable_for_an_unchanged_list() -> None:
    """Otherwise every report differs from every other and the field is
    noise rather than signal."""
    from run_golden_benchmark import _validator_config_fingerprint

    assert (
        _validator_config_fingerprint()["refusal_patterns_sha256_12"]
        == _validator_config_fingerprint()["refusal_patterns_sha256_12"]
    )


def test_retrieval_scope_and_top_k_travel_with_it(fingerprint: dict) -> None:
    """The document-scope policy decides which corpus was searched at all,
    and it was flipped on 2026-08-21 — the same class of change."""
    assert fingerprint["qdrant_document_project_scope"] in (
        "cross_project", "project_or_public", "strict",
    )
    assert fingerprint["retrieval_top_n"] is not None
    assert fingerprint["reranker_top_k"] is not None


def test_the_report_meta_carries_it() -> None:
    """Wired, not merely defined. A fingerprint function nothing calls is
    the same as no fingerprint."""
    import inspect

    from run_golden_benchmark import _assemble_report

    # The FUNCTION's source, not the file's: the module docstring contains
    # an example report whose `"meta": {` block would match first and pass
    # this for the wrong reason.
    source = inspect.getsource(_assemble_report)

    assert '"validator_config": _validator_config_fingerprint()' in source

    meta_start = source.index('"meta": {')
    meta_end = source.index('"summary": {', meta_start)
    assert "validator_config" in source[meta_start:meta_end], (
        "the fingerprint is computed but not inside the meta block, so it "
        "will not travel with the artefact"
    )
