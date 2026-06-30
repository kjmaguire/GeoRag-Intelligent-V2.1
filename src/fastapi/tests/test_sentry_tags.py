"""Unit tests for app.agent.sentry_tags.

Strategy: monkey-patch the lazy `_sentry_sdk` helper to return a stub
that records every set_tag call. Verifies:

  • tag NAMES match the spec doc exactly
  • tag VALUES are normalised per the rules (lowercase bools,
    bucketed enums, ≤200-char CSV)
  • each setter is no-op + non-raising when the SDK isn't installed
  • each setter swallows its own internal errors
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.agent import sentry_tags

# ---------------------------------------------------------------------------
# Fake sentry_sdk stub
# ---------------------------------------------------------------------------


class _FakeSentry:
    def __init__(self) -> None:
        self.tags: dict[str, str] = {}

    def set_tag(self, key: str, value: Any) -> None:
        self.tags[key] = value


@pytest.fixture
def fake_sdk(monkeypatch: pytest.MonkeyPatch) -> _FakeSentry:
    stub = _FakeSentry()
    monkeypatch.setattr(sentry_tags, "_sentry_sdk", lambda: stub)
    return stub


@pytest.fixture
def no_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the lazy import to return None — i.e. SDK not installed."""
    monkeypatch.setattr(sentry_tags, "_sentry_sdk", lambda: None)


# ---------------------------------------------------------------------------
# Small fake state + plan shapes — the tag-setter only uses attribute
# access, so duck-typed objects are fine.
# ---------------------------------------------------------------------------


@dataclass
class _FakePlan:
    terminal: bool = False
    strategy: Any = None


class _StrategyEnum:
    """Stand-in for RepairStrategy enum member — has `.value`."""
    def __init__(self, value: str) -> None:
        self.value = value


@dataclass
class _FakeState:
    repair_codes_observed: list[str]
    repair_attempts: list[Any]
    repair_terminal_reason: str | None = None
    effective_intent: Any = None
    intent: Any = None
    resolution_trace: list[dict[str, Any]] | None = None
    resolution_confidence: float | None = None
    history: list[Any] | None = None


# ---------------------------------------------------------------------------
# stamp_workspace_tag
# ---------------------------------------------------------------------------


def test_workspace_tag_sets_string(fake_sdk: _FakeSentry) -> None:
    sentry_tags.stamp_workspace_tag("ws-uuid-123")
    assert fake_sdk.tags["workspace.id"] == "ws-uuid-123"


def test_workspace_tag_skips_when_workspace_is_none(fake_sdk: _FakeSentry) -> None:
    sentry_tags.stamp_workspace_tag(None)
    assert "workspace.id" not in fake_sdk.tags


def test_workspace_tag_noop_when_sdk_missing(no_sdk: None) -> None:
    # Must not raise.
    sentry_tags.stamp_workspace_tag("ws")


# ---------------------------------------------------------------------------
# stamp_repair_tags
# ---------------------------------------------------------------------------


def test_repair_tags_no_plan_no_attempts(fake_sdk: _FakeSentry) -> None:
    state = _FakeState(repair_codes_observed=[], repair_attempts=[])
    sentry_tags.stamp_repair_tags(state, plan=None)
    assert fake_sdk.tags["repair.shadow_mode"] in ("true", "false")
    assert fake_sdk.tags["repair.codes_count"] == "0"
    assert fake_sdk.tags["repair.terminal"] == "false"
    assert fake_sdk.tags["repair.terminal_strategy"] == ""
    assert fake_sdk.tags["repair.attempts"] == "0"
    assert fake_sdk.tags["repair.death_loop"] == "false"


def test_repair_tags_terminal_plan_uses_strategy_value(
    fake_sdk: _FakeSentry,
) -> None:
    state = _FakeState(
        repair_codes_observed=["CONFLICTING_SOURCES", "NUMERIC_GROUNDING_FAILED"],
        repair_attempts=[{}, {}],
    )
    plan = _FakePlan(terminal=True, strategy=_StrategyEnum("SURFACE_CONFLICT"))
    sentry_tags.stamp_repair_tags(state, plan)
    assert fake_sdk.tags["repair.codes_count"] == "2"
    assert fake_sdk.tags["repair.terminal"] == "true"
    assert fake_sdk.tags["repair.terminal_strategy"] == "SURFACE_CONFLICT"
    assert fake_sdk.tags["repair.attempts"] == "2"


def test_repair_tags_death_loop_detected_from_reason(fake_sdk: _FakeSentry) -> None:
    state = _FakeState(
        repair_codes_observed=[],
        repair_attempts=[],
        repair_terminal_reason="death_loop: identical no-progress retries",
    )
    sentry_tags.stamp_repair_tags(state, plan=None)
    assert fake_sdk.tags["repair.death_loop"] == "true"


def test_repair_tags_plain_str_strategy_passes_through(fake_sdk: _FakeSentry) -> None:
    """If a non-enum string slips in as strategy, it should still set cleanly."""
    state = _FakeState(repair_codes_observed=[], repair_attempts=[])
    plan = _FakePlan(terminal=True, strategy="ASK_FOR_DISAMBIGUATION")
    sentry_tags.stamp_repair_tags(state, plan)
    assert fake_sdk.tags["repair.terminal_strategy"] == "ASK_FOR_DISAMBIGUATION"


def test_repair_tags_noop_when_sdk_missing(no_sdk: None) -> None:
    state = _FakeState(repair_codes_observed=[], repair_attempts=[])
    # Must not raise.
    sentry_tags.stamp_repair_tags(state, plan=None)


# ---------------------------------------------------------------------------
# stamp_context_prep_tags
# ---------------------------------------------------------------------------


@dataclass
class _FakePacket:
    total_tokens: int
    remaining_budget: int


@dataclass
class _FakePrepared:
    reached_budget: bool
    dropped_evidence_ids: list[str]
    packet: Any


def test_context_prep_tags_no_prepared_uses_defaults(fake_sdk: _FakeSentry) -> None:
    state = _FakeState(repair_codes_observed=[], repair_attempts=[])
    sentry_tags.stamp_context_prep_tags(state, prepared=None)
    assert fake_sdk.tags["context_prep.budget_reached"] == "false"
    assert fake_sdk.tags["context_prep.drops_count"] == "0"
    assert fake_sdk.tags["context_prep.budget_pressure"] == "unknown"


def test_context_prep_tags_comfortable_budget(fake_sdk: _FakeSentry) -> None:
    state = _FakeState(repair_codes_observed=[], repair_attempts=[])
    prep = _FakePrepared(
        reached_budget=False,
        dropped_evidence_ids=[],
        packet=_FakePacket(total_tokens=1000, remaining_budget=600),
    )
    sentry_tags.stamp_context_prep_tags(state, prep)
    assert fake_sdk.tags["context_prep.budget_pressure"] == "comfortable"
    assert fake_sdk.tags["context_prep.budget_reached"] == "false"


def test_context_prep_tags_tight_budget(fake_sdk: _FakeSentry) -> None:
    state = _FakeState(repair_codes_observed=[], repair_attempts=[])
    # 5% remaining → tight.
    prep = _FakePrepared(
        reached_budget=True,
        dropped_evidence_ids=["a", "b", "c"],
        packet=_FakePacket(total_tokens=1000, remaining_budget=50),
    )
    sentry_tags.stamp_context_prep_tags(state, prep)
    assert fake_sdk.tags["context_prep.budget_pressure"] == "tight"
    assert fake_sdk.tags["context_prep.budget_reached"] == "true"
    assert fake_sdk.tags["context_prep.drops_count"] == "3"


def test_context_prep_tags_over_budget(fake_sdk: _FakeSentry) -> None:
    state = _FakeState(repair_codes_observed=[], repair_attempts=[])
    prep = _FakePrepared(
        reached_budget=True,
        dropped_evidence_ids=["a"],
        packet=_FakePacket(total_tokens=1000, remaining_budget=-50),
    )
    sentry_tags.stamp_context_prep_tags(state, prep)
    assert fake_sdk.tags["context_prep.budget_pressure"] == "over"


def test_context_prep_tags_intent_from_effective_intent(
    fake_sdk: _FakeSentry,
) -> None:
    intent = _StrategyEnum("synthesis")
    state = _FakeState(
        repair_codes_observed=[], repair_attempts=[], effective_intent=intent,
    )
    sentry_tags.stamp_context_prep_tags(state, prepared=None)
    assert fake_sdk.tags["context_prep.intent"] == "synthesis"


# ---------------------------------------------------------------------------
# stamp_multi_turn_tags
# ---------------------------------------------------------------------------


def test_multi_turn_tags_no_changes(fake_sdk: _FakeSentry) -> None:
    state = _FakeState(
        repair_codes_observed=[], repair_attempts=[],
        resolution_trace=[], resolution_confidence=None, history=[],
    )
    sentry_tags.stamp_multi_turn_tags(state)
    assert fake_sdk.tags["multi_turn.made_changes"] == "false"
    assert fake_sdk.tags["multi_turn.steps_count"] == "0"
    assert fake_sdk.tags["multi_turn.confidence_bucket"] == "unknown"
    assert fake_sdk.tags["multi_turn.history_depth"] == "0"


def test_multi_turn_tags_high_confidence(fake_sdk: _FakeSentry) -> None:
    state = _FakeState(
        repair_codes_observed=[], repair_attempts=[],
        resolution_trace=[{"kind": "pronoun"}, {"kind": "demonstrative"}],
        resolution_confidence=0.92,
        history=[1, 2, 3, 4],
    )
    sentry_tags.stamp_multi_turn_tags(state)
    assert fake_sdk.tags["multi_turn.made_changes"] == "true"
    assert fake_sdk.tags["multi_turn.steps_count"] == "2"
    assert fake_sdk.tags["multi_turn.confidence_bucket"] == "high"
    assert fake_sdk.tags["multi_turn.history_depth"] == "4"


@pytest.mark.parametrize(
    ("confidence", "bucket"),
    [
        (0.99, "high"),
        (0.85, "high"),
        (0.84, "medium"),
        (0.60, "medium"),
        (0.59, "low"),
        (0.0, "low"),
        (None, "unknown"),
    ],
)
def test_confidence_bucket_thresholds(
    fake_sdk: _FakeSentry, confidence: float | None, bucket: str,
) -> None:
    state = _FakeState(
        repair_codes_observed=[], repair_attempts=[],
        resolution_trace=[], resolution_confidence=confidence, history=[],
    )
    sentry_tags.stamp_multi_turn_tags(state)
    assert fake_sdk.tags["multi_turn.confidence_bucket"] == bucket


# ---------------------------------------------------------------------------
# stamp_evidence_tags
# ---------------------------------------------------------------------------


@dataclass
class _FakeEvidence:
    kind: str


@dataclass
class _FakeEvidencePacket:
    evidence: list[Any]


def test_evidence_tags_none_packet(fake_sdk: _FakeSentry) -> None:
    sentry_tags.stamp_evidence_tags(None)
    assert fake_sdk.tags["evidence.kinds_count"] == "0"
    assert fake_sdk.tags["evidence.has_spatial"] == "false"
    assert fake_sdk.tags["evidence.has_graph"] == "false"
    assert fake_sdk.tags["evidence.first_kind"] == ""


def test_evidence_tags_mixed_packet(fake_sdk: _FakeSentry) -> None:
    packet = _FakeEvidencePacket(evidence=[
        _FakeEvidence(kind="document"),
        _FakeEvidence(kind="document"),
        _FakeEvidence(kind="spatial"),
        _FakeEvidence(kind="graph"),
    ])
    sentry_tags.stamp_evidence_tags(packet)
    # Three distinct kinds.
    assert fake_sdk.tags["evidence.kinds_count"] == "3"
    assert fake_sdk.tags["evidence.has_spatial"] == "true"
    assert fake_sdk.tags["evidence.has_graph"] == "true"
    assert fake_sdk.tags["evidence.first_kind"] == "document"


def test_evidence_tags_falls_back_to_class_name(fake_sdk: _FakeSentry) -> None:
    class SpatialEvidence:
        pass

    packet = _FakeEvidencePacket(evidence=[SpatialEvidence()])
    sentry_tags.stamp_evidence_tags(packet)
    assert fake_sdk.tags["evidence.has_spatial"] == "true"
    assert fake_sdk.tags["evidence.first_kind"] == "SpatialEvidence"


# ---------------------------------------------------------------------------
# stamp_guards_tags
# ---------------------------------------------------------------------------


def test_guards_tags_no_fires(fake_sdk: _FakeSentry) -> None:
    sentry_tags.stamp_guards_tags([])
    assert fake_sdk.tags["guards.fired_any"] == "false"
    assert fake_sdk.tags["guards.fired_terminal"] == "false"
    assert fake_sdk.tags["guards.codes_csv"] == ""


def test_guards_tags_terminal_detection(fake_sdk: _FakeSentry) -> None:
    sentry_tags.stamp_guards_tags(
        ["NUMERIC_GROUNDING_FAILED", "CONFLICTING_SOURCES"],
    )
    assert fake_sdk.tags["guards.fired_any"] == "true"
    assert fake_sdk.tags["guards.fired_terminal"] == "true"
    assert "CONFLICTING_SOURCES" in fake_sdk.tags["guards.codes_csv"]


def test_guards_tags_non_terminal_only(fake_sdk: _FakeSentry) -> None:
    sentry_tags.stamp_guards_tags(["NUMERIC_GROUNDING_FAILED"])
    assert fake_sdk.tags["guards.fired_terminal"] == "false"


def test_guards_tags_truncates_csv_at_200_chars(fake_sdk: _FakeSentry) -> None:
    many_codes = [f"GUARD_CODE_NUMBER_{i:03d}" for i in range(30)]
    sentry_tags.stamp_guards_tags(many_codes)
    csv = fake_sdk.tags["guards.codes_csv"]
    # Sentry's hard cap.
    assert len(csv) <= 200
    # Ellipsis sentinel signals truncation occurred.
    assert csv.endswith("...")
    # First code must be present (truncation happens from the tail).
    assert csv.startswith("GUARD_CODE_NUMBER_000")


def test_guards_tags_none_treated_as_empty(fake_sdk: _FakeSentry) -> None:
    sentry_tags.stamp_guards_tags(None)
    assert fake_sdk.tags["guards.fired_any"] == "false"
    assert fake_sdk.tags["guards.codes_csv"] == ""


def test_guards_tags_noop_when_sdk_missing(no_sdk: None) -> None:
    # Must not raise.
    sentry_tags.stamp_guards_tags(["CONFLICTING_SOURCES"])


# ---------------------------------------------------------------------------
# Internal helpers — direct unit tests
# ---------------------------------------------------------------------------


def test_bool_str_normalises():
    assert sentry_tags._bool_str(True) == "true"
    assert sentry_tags._bool_str(False) == "false"
    assert sentry_tags._bool_str(1) == "true"
    assert sentry_tags._bool_str(0) == "false"
    assert sentry_tags._bool_str(None) == "false"
    assert sentry_tags._bool_str("") == "false"
    assert sentry_tags._bool_str("yes") == "true"


def test_budget_pressure_unknown_when_packet_lacks_totals():
    class _OpaquePacket:
        pass
    assert sentry_tags._budget_pressure_bucket(_OpaquePacket()) == "unknown"
    assert sentry_tags._budget_pressure_bucket(None) == "unknown"


# ---------------------------------------------------------------------------
# stamp_card_type_tag — §6b P6
# ---------------------------------------------------------------------------


class _FakeVizPayload:
    """Minimal stand-in for app.models.rag.VizPayload — duck-typed for the
    stamper's `getattr(viz_payload, "chart_type", None)` access."""
    def __init__(self, chart_type: str) -> None:
        self.chart_type = chart_type


def test_card_type_none_when_viz_payload_is_none(fake_sdk: _FakeSentry):
    """No viz on the response (text-only answer) → tags still set so the
    dashboard's filter never sees a missing key."""
    sentry_tags.stamp_card_type_tag(None)
    assert fake_sdk.tags["card.rendered"] == "false"
    assert fake_sdk.tags["card.type"] == "none"


@pytest.mark.parametrize("chart_type", [
    "drill_trace_3d", "downhole_strip", "stereonet", "technique_timeline",
    "coverage_table", "assay_histogram", "cross_section", "graph_viz",
])
def test_card_type_known_chart_types_pass_through(
    fake_sdk: _FakeSentry, chart_type: str,
):
    """Every chart_type the §6b dispatcher can emit must be in
    _KNOWN_CARD_TYPES — otherwise it'd land as 'unknown' and a follow-up
    spec-doc update is needed. Parametrise across all 8."""
    sentry_tags.stamp_card_type_tag(_FakeVizPayload(chart_type=chart_type))
    assert fake_sdk.tags["card.type"] == chart_type
    assert fake_sdk.tags["card.rendered"] == "true"


def test_card_type_unknown_when_chart_type_drifts(fake_sdk: _FakeSentry):
    """A new chart_type not in _KNOWN_CARD_TYPES lands as 'unknown' so the
    tag stays low-cardinality. The unknown bucket is the signal to update
    the frozenset + the docs."""
    sentry_tags.stamp_card_type_tag(_FakeVizPayload(chart_type="future_card_type"))
    assert fake_sdk.tags["card.type"] == "unknown"
    assert fake_sdk.tags["card.rendered"] == "true"


def test_card_type_empty_chart_type_treated_as_none(fake_sdk: _FakeSentry):
    """Defensive: a VizPayload with chart_type='' shouldn't claim a
    rendered card — treat as none."""
    sentry_tags.stamp_card_type_tag(_FakeVizPayload(chart_type=""))
    assert fake_sdk.tags["card.type"] == "none"
    assert fake_sdk.tags["card.rendered"] == "false"


def test_card_type_noop_when_sdk_missing(no_sdk: None):
    """SDK absent → no raise, no side effects. Mirrors the other stampers."""
    sentry_tags.stamp_card_type_tag(_FakeVizPayload(chart_type="stereonet"))


def test_known_card_types_set_matches_dispatcher_contract():
    """Sanity check: the frozenset in sentry_tags should match the
    chart_type values the dispatcher emits. If a new chart_type lands
    in `_build_chat_card_payloads` without being added here, the
    Sentry tag would slip to 'unknown' silently. This test pins both
    sides in sync."""
    expected = {
        "drill_trace_3d",     # DrillTrace3DResult branch
        "downhole_strip",     # CollarDetailsResult branch
        "stereonet",          # StereonetResult branch
        "technique_timeline", # ProjectSummaryResult branch
        "coverage_table",     # CoverageGapResult branch
        # The three below ride on the legacy ViznPayload shape but the
        # frontend dispatcher (InlineViz) handles them; keep them tagged.
        "assay_histogram",
        "cross_section",
        "graph_viz",
    }
    assert frozenset(expected) == sentry_tags._KNOWN_CARD_TYPES
