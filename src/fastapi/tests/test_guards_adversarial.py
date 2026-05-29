"""Adversarial fuzz tests for ``classify_guards`` warning patterns.

The §4b code classifier maps free-form warning strings (emitted by the
hallucination layer validators) onto typed :class:`GuardErrorCode`
values via a regex table in ``app/agent/guards.py``. Today's
``test_guards.py`` covers the happy paths; THIS file covers:

  1. Case-sensitivity invariants (warnings come from many code paths
     with varying casing conventions)
  2. Whitespace + punctuation tolerance
  3. Near-match patterns that MUST NOT fire (false-positive bait)
  4. Substring-match-from-inside-longer-string behaviour
  5. Multi-pattern collisions (one warning matching two patterns —
     order resolution)
  6. Empty / None / pathological inputs

The shadow-mode wire (Plan §4b Stage 1) now classifies guards on every
query and writes the codes to ``silver.query_traces.repair_strategies_used``.
A drift in the classifier directly skews the telemetry the next rollout
stage relies on — hence the adversarial coverage.
"""

from __future__ import annotations

import pytest

from app.agent.guards import GuardErrorCode, classify_guards


def _codes(warnings=None, *, demotion_reasons=None, **extra) -> list[GuardErrorCode]:
    """Convenience — call classify_guards with only the warnings/demotion
    paths populated and return the codes list."""
    return classify_guards(
        validation_warnings=list(warnings or []),
        demotion_reasons=list(demotion_reasons or []),
        tool_results=extra.get("tool_results"),
        response_citations=extra.get("response_citations"),
        citation_lifecycle_state=extra.get("citation_lifecycle_state"),
        conflicting_evidence_present=extra.get("conflicting_evidence_present", False),
    )


# ---------------------------------------------------------------------------
# Case sensitivity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("warning", [
    "Layer 3: ungrounded number 5.0",       # initial caps
    "LAYER 3: ungrounded number 5.0",       # all caps
    "layer 3: ungrounded number 5.0",       # all lower
    "Layer 3: UNGROUNDED NUMBER 5.0",       # mixed
])
def test_layer3_prefix_matches_regardless_of_case(warning):
    """Warning strings come from validators authored at different times
    with different casing conventions. The classifier MUST be
    case-insensitive."""
    codes = _codes([warning])
    assert GuardErrorCode.NUMERIC_GROUNDING_FAILED in codes


@pytest.mark.parametrize("warning", [
    "drill-hole id ABC not found",
    "DRILL-HOLE ID abc not found",
    "Drill-Hole ID ABC Not Found",
])
def test_drill_hole_id_pattern_matches_any_case(warning):
    codes = _codes([warning])
    assert GuardErrorCode.ENTITY_NOT_FOUND in codes


# ---------------------------------------------------------------------------
# Whitespace + punctuation tolerance
# ---------------------------------------------------------------------------


def test_pattern_match_tolerates_leading_whitespace():
    warning = "   layer 3: ungrounded number 5.0"
    codes = _codes([warning])
    assert GuardErrorCode.NUMERIC_GROUNDING_FAILED in codes


def test_pattern_match_tolerates_trailing_punctuation():
    """Warning strings often end with trailing periods, semicolons, or
    bracketed metadata. Match must survive."""
    for trailing in (".", ";", " [chunk-id=abc]", " (Layer 3)"):
        warning = f"ungrounded number 5.0{trailing}"
        codes = _codes([warning])
        assert GuardErrorCode.NUMERIC_GROUNDING_FAILED in codes, (
            f"failed on trailing {trailing!r}"
        )


def test_pattern_match_inside_longer_warning_string():
    """A real-world warning often embeds the trigger phrase inside a
    longer diagnostic — the classifier must still pick it up."""
    warning = (
        "Layer 3 validator: at chunk-id=abc-123 in document Crackingstone "
        "Annual Report 2024 — ungrounded number 1.23 g/t Au (no source span)."
    )
    codes = _codes([warning])
    assert GuardErrorCode.NUMERIC_GROUNDING_FAILED in codes


# ---------------------------------------------------------------------------
# Near-match patterns that MUST NOT fire
# ---------------------------------------------------------------------------


def test_unrelated_warning_returns_empty_when_no_other_signal():
    """A pristine warning string with no recognised substring must NOT
    cause ANY code to fire."""
    codes = _codes(
        ["Validator initialised successfully"],
        # No empty tool_results path (None) so NO_EVIDENCE_FOUND can't
        # implicitly fire.
        tool_results=None,
    )
    assert codes == []


def test_warning_containing_number_alone_does_not_fire_numeric():
    """The phrase 'number' alone shouldn't trigger NUMERIC_GROUNDING_FAILED
    — only the qualified 'ungrounded number' / 'layer 3' patterns should."""
    codes = _codes(["the number of holes is 47", "result count: 12"])
    # Defensive: this verifies the regex requires the QUALIFIED form.
    assert GuardErrorCode.NUMERIC_GROUNDING_FAILED not in codes


def test_warning_containing_layer_alone_does_not_fire():
    """'Layer' as a noun (geological layer) must NOT be confused for the
    validator-layer prefix."""
    codes = _codes(["mineralisation in the upper layer is high-grade"])
    assert GuardErrorCode.NUMERIC_GROUNDING_FAILED not in codes
    assert GuardErrorCode.SOURCE_SCOPE_VIOLATION not in codes


def test_workspace_outside_substring_requires_full_phrase():
    """'outside your workspace' should fire SOURCE_SCOPE_VIOLATION;
    'workspace outside' (different word order) should NOT."""
    fires = _codes(["referenced chunk is outside your workspace"])
    assert GuardErrorCode.SOURCE_SCOPE_VIOLATION in fires
    misses = _codes(["workspace outside the corridor was empty"])
    assert GuardErrorCode.SOURCE_SCOPE_VIOLATION not in misses


# ---------------------------------------------------------------------------
# Multi-pattern collisions
# ---------------------------------------------------------------------------


def test_warning_matching_multiple_patterns_picks_first_in_table():
    """The pattern table is ordered — 'layer 3:' wins over a tail
    'ungrounded number' phrase if both could match. Verify the first
    matched pattern is the one that fires (this is what
    ``_classify_warning`` documents)."""
    # Construct a warning that triggers BOTH 'layer 3:' AND
    # 'ungrounded number'. Both happen to map to NUMERIC_GROUNDING_FAILED
    # so the result is the same code; the test exists to lock the
    # one-code-per-warning contract.
    warning = "layer 3: ungrounded number found at depth 105m"
    codes = _codes([warning])
    # Exactly ONE code (no dedup confusion).
    assert codes.count(GuardErrorCode.NUMERIC_GROUNDING_FAILED) == 1


def test_warning_in_multiple_categories_each_fire_once():
    """Two warnings, two different categories — both codes fire, each
    once. Tests that the deduplication is per-code, not per-warning."""
    codes = _codes([
        "layer 3: ungrounded number 5.0",                  # NUMERIC_GROUNDING_FAILED
        "drill-hole id ABC not found in collars",          # ENTITY_NOT_FOUND
    ])
    assert GuardErrorCode.NUMERIC_GROUNDING_FAILED in codes
    assert GuardErrorCode.ENTITY_NOT_FOUND in codes
    assert len(codes) == 2


def test_duplicate_warnings_dedupe_to_one_code():
    """Same warning twice → the code appears once in the output."""
    codes = _codes([
        "layer 3: ungrounded number 5.0",
        "layer 3: ungrounded number 6.0",
    ])
    assert codes.count(GuardErrorCode.NUMERIC_GROUNDING_FAILED) == 1


# ---------------------------------------------------------------------------
# Demotion-reason path
# ---------------------------------------------------------------------------


def test_demotion_reason_uses_same_pattern_table():
    """Demotion reasons go through the same _classify_warning helper —
    the same patterns must fire."""
    codes = _codes(
        warnings=[],
        demotion_reasons=["over-filtered query — relaxing filter set"],
    )
    assert GuardErrorCode.OVER_FILTERED_QUERY in codes


def test_demotion_and_validation_dedupe_to_one_code():
    """Same pattern in both validation_warnings AND demotion_reasons →
    the code fires once."""
    codes = _codes(
        warnings=["layer 3: ungrounded number"],
        demotion_reasons=["layer 3: ungrounded number"],
    )
    assert codes.count(GuardErrorCode.NUMERIC_GROUNDING_FAILED) == 1


# ---------------------------------------------------------------------------
# Empty / None / pathological
# ---------------------------------------------------------------------------


def test_empty_warning_string_is_ignored():
    codes = _codes(["", " ", "\t"])
    # Some of these might match nothing, but the function must not crash.
    # Specifically, "" returns None from _classify_warning early.
    assert all(c == GuardErrorCode.NO_EVIDENCE_FOUND for c in codes if c) or codes == []


def test_none_warnings_input_returns_empty():
    codes = classify_guards(
        validation_warnings=None,
        demotion_reasons=None,
        tool_results=None,
        response_citations=None,
    )
    assert codes == []


def test_unicode_warning_does_not_crash():
    """Some validator messages contain Unicode (deg symbols, NBSP, etc.)
    — the classifier must handle them without raising."""
    warning = "Layer 3: ungrounded number 5.0° at depth 105 m (NBSP)"
    codes = _codes([warning])
    assert GuardErrorCode.NUMERIC_GROUNDING_FAILED in codes


def test_very_long_warning_string_classifies_correctly():
    """A 10K-char warning shouldn't time out or fail to match."""
    prefix = "x" * 10_000
    warning = f"{prefix} layer 3: ungrounded number 5.0 {prefix}"
    codes = _codes([warning])
    assert GuardErrorCode.NUMERIC_GROUNDING_FAILED in codes


# ---------------------------------------------------------------------------
# Composite-signal integration with classifier
# ---------------------------------------------------------------------------


def test_no_evidence_signal_fires_only_when_all_tools_empty():
    """``NO_EVIDENCE_FOUND`` only fires when EVERY tool result is empty."""
    # All empty → fires.
    codes = _codes(
        tool_results=[
            ("search_documents", []),
            ("query_assay_data", []),
        ],
    )
    assert GuardErrorCode.NO_EVIDENCE_FOUND in codes

    # One non-empty → does NOT fire.
    codes = _codes(
        tool_results=[
            ("search_documents", []),
            ("query_assay_data", [{"hole_id": "X"}]),
        ],
    )
    assert GuardErrorCode.NO_EVIDENCE_FOUND not in codes


def test_no_evidence_does_not_fire_when_tool_results_is_none():
    """``tool_results=None`` means "caller didn't supply that signal" —
    we don't infer from absence."""
    codes = _codes(tool_results=None)
    assert GuardErrorCode.NO_EVIDENCE_FOUND not in codes


def test_citation_incomplete_fires_on_empty_citations_list():
    codes = _codes(response_citations=[])
    assert GuardErrorCode.CITATION_INCOMPLETE in codes


def test_citation_incomplete_fires_on_rejected_lifecycle_state():
    codes = _codes(citation_lifecycle_state="rejected")
    assert GuardErrorCode.CITATION_INCOMPLETE in codes


def test_citation_incomplete_does_not_fire_when_citations_present():
    codes = _codes(
        response_citations=[{"chunk_id": "abc"}],
        citation_lifecycle_state="committed",
    )
    assert GuardErrorCode.CITATION_INCOMPLETE not in codes


def test_conflicting_sources_fires_only_with_explicit_flag():
    """``conflicting_evidence_present=True`` is the trigger — falsey
    forms (None, [], False) all skip."""
    codes_with = _codes(conflicting_evidence_present=True)
    codes_without = _codes(conflicting_evidence_present=False)
    assert GuardErrorCode.CONFLICTING_SOURCES in codes_with
    assert GuardErrorCode.CONFLICTING_SOURCES not in codes_without


# ---------------------------------------------------------------------------
# Output stability — insertion-order is preserved
# ---------------------------------------------------------------------------


def test_classify_guards_preserves_insertion_order():
    """The function uses a dict for dedup; Python guarantees ordered
    iteration. Locking this so downstream consumers (the dispatcher,
    the trace) can rely on stable ordering."""
    codes = _codes(
        warnings=[
            "layer 3: ungrounded number 5.0",     # → NUMERIC_GROUNDING_FAILED
            "drill-hole id missing",              # → ENTITY_NOT_FOUND
            "missing units in assay row",         # → MISSING_ASSAY_UNITS
        ],
    )
    assert codes == [
        GuardErrorCode.NUMERIC_GROUNDING_FAILED,
        GuardErrorCode.ENTITY_NOT_FOUND,
        GuardErrorCode.MISSING_ASSAY_UNITS,
    ]


def test_repeated_pattern_does_not_break_order():
    codes = _codes(
        warnings=[
            "layer 3: first issue",     # NUMERIC_GROUNDING_FAILED
            "drill-hole id X missing",  # ENTITY_NOT_FOUND
            "layer 3: second issue",    # NUMERIC_GROUNDING_FAILED (already added — skipped)
            "no spatial matches",       # SPATIAL_QUERY_EMPTY
        ],
    )
    assert codes == [
        GuardErrorCode.NUMERIC_GROUNDING_FAILED,
        GuardErrorCode.ENTITY_NOT_FOUND,
        GuardErrorCode.SPATIAL_QUERY_EMPTY,
    ]
