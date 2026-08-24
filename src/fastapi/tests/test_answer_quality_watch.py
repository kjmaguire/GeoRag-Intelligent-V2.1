"""The comparison that decides whether answers got worse.

WHY THE TESTS CLUSTER ON THE SMALL-SAMPLE GUARD
    This deployment serves almost no traffic. The failure mode for a
    quality watch here is not missing a regression -- it is firing on
    n=3, being muted, and then missing every regression forever. So the
    guard gets more coverage than the thresholds do.

WHAT `compare` DELIBERATELY DOES NOT DO, EACH PINNED BELOW
    * It does not alert on an improvement. A refusal rate halving is an
      equally large change and is not an incident.
    * It does not alert on latency. Latency has its own signal and its own
      runbook; folding it in would make a QUALITY alert fire for an
      infrastructure problem, and whoever is paged would go to the wrong
      runbook.
    * It does not compound metrics into a score. "Three things moved" and
      "one thing moved three times" need different responses, and a single
      number cannot tell them apart.
"""
from __future__ import annotations

import pytest

from app.hatchet_workflows.answer_quality_watch import (
    ALERT_MARKER,
    CONFIDENCE_DROP,
    MIN_SAMPLE,
    REFUSAL_RISE_PP,
    AnswerQualityWatchInput,
    QualityWindow,
    compare,
    parse_windows,
)


def window(total: int = 100, **overrides) -> QualityWindow:
    return QualityWindow(total_runs=total, **overrides)


class TestTheSmallSampleGuard:
    def test_a_quiet_current_window_makes_no_claim(self) -> None:
        """Three queries and one refusal is a 33% refusal rate and means
        nothing. Reporting it as a regression is how a watch gets muted."""
        regressions, insufficient = compare(
            window(3, refusals=1), window(200, refusals=2),
        )

        assert insufficient is True
        assert regressions == []

    def test_a_quiet_baseline_also_makes_no_claim(self) -> None:
        """Both sides matter: comparing a busy day against a baseline of
        four runs is the same arithmetic in the other direction."""
        regressions, insufficient = compare(
            window(200, refusals=120), window(4, refusals=0),
        )

        assert insufficient is True
        assert regressions == []

    def test_exactly_at_the_minimum_is_enough(self) -> None:
        regressions, insufficient = compare(
            window(MIN_SAMPLE, refusals=MIN_SAMPLE),
            window(MIN_SAMPLE, refusals=0),
        )

        assert insufficient is False
        assert regressions, "a 0% -> 100% refusal rate is a regression"

    def test_one_below_the_minimum_is_not(self) -> None:
        _regressions, insufficient = compare(
            window(MIN_SAMPLE - 1, refusals=MIN_SAMPLE - 1),
            window(MIN_SAMPLE, refusals=0),
        )
        assert insufficient is True

    def test_two_empty_windows_are_insufficient_not_perfect(self) -> None:
        """A deployment nobody queried must not report "steady"."""
        regressions, insufficient = compare(QualityWindow(), QualityWindow())

        assert insufficient is True
        assert regressions == []

    def test_the_threshold_is_configurable_for_a_busier_deployment(
        self,
    ) -> None:
        regressions, insufficient = compare(
            window(5, refusals=5), window(5, refusals=0), min_sample=5,
        )
        assert insufficient is False
        assert regressions


class TestWhatCountsAsARegression:
    def test_a_refusal_spike_is_reported_with_both_numbers(self) -> None:
        regressions, _ = compare(
            window(100, refusals=40), window(100, refusals=10),
        )

        assert len(regressions) == 1
        assert "refusal_rate" in regressions[0]
        assert "10.0%" in regressions[0] and "40.0%" in regressions[0]
        assert "+30.0pp" in regressions[0]

    def test_a_rise_below_the_threshold_is_not_reported(self) -> None:
        """The thresholds are absolute percentage points, not relative.

        2% -> 6% is a 200% relative jump and almost certainly noise at
        this traffic level; 20% -> 35% is real users getting nothing.
        """
        regressions, _ = compare(
            window(100, refusals=6), window(100, refusals=2),
        )
        assert regressions == []

    def test_exactly_at_the_threshold_counts(self) -> None:
        regressions, _ = compare(
            window(100, refusals=int(REFUSAL_RISE_PP)), window(100, refusals=0),
        )
        assert regressions

    def test_guard_fires_are_watched_separately_from_refusals(self) -> None:
        """They are different failures. Guards firing means the model
        produced something the chain rejected; refusing means it produced
        nothing to reject."""
        regressions, _ = compare(
            window(100, guard_fires=40), window(100, guard_fires=10),
        )

        assert len(regressions) == 1
        assert "guard_fire_rate" in regressions[0]

    def test_zero_evidence_runs_are_watched(self) -> None:
        """The 2026-06-01 shape: retrieval returns nothing, so there is
        nothing to answer from. Catching it here is the difference between
        noticing on Tuesday and noticing from a support email."""
        regressions, _ = compare(
            window(100, zero_evidence=50), window(100, zero_evidence=5),
        )

        assert len(regressions) == 1
        assert "zero_evidence_rate" in regressions[0]

    def test_a_confidence_collapse_is_reported(self) -> None:
        regressions, _ = compare(
            window(100, mean_confidence=0.40),
            window(100, mean_confidence=0.80),
        )

        assert len(regressions) == 1
        assert "mean_confidence" in regressions[0]
        assert "0.800" in regressions[0] and "0.400" in regressions[0]

    def test_a_small_confidence_wobble_is_not(self) -> None:
        regressions, _ = compare(
            window(100, mean_confidence=0.80 - CONFIDENCE_DROP / 2),
            window(100, mean_confidence=0.80),
        )
        assert regressions == []

    def test_several_metrics_moving_are_reported_separately(self) -> None:
        """"Three things moved" and "one thing moved three times" need
        different responses; a combined score cannot tell them apart."""
        regressions, _ = compare(
            window(100, refusals=40, guard_fires=40, zero_evidence=40,
                   mean_confidence=0.3),
            window(100, refusals=1, guard_fires=1, zero_evidence=1,
                   mean_confidence=0.9),
        )

        assert len(regressions) == 4


class TestWhatIsDeliberatelyIgnored:
    def test_an_improvement_is_not_an_incident(self) -> None:
        regressions, insufficient = compare(
            window(100, refusals=2, guard_fires=1, zero_evidence=0,
                   mean_confidence=0.95),
            window(100, refusals=60, guard_fires=50, zero_evidence=40,
                   mean_confidence=0.40),
        )

        assert insufficient is False
        assert regressions == []

    def test_latency_alone_never_triggers(self) -> None:
        """Latency has its own signal and its own runbook. A quality alert
        firing for an infrastructure problem sends the responder to the
        wrong page."""
        regressions, _ = compare(
            window(100, p95_latency_ms=45_000.0),
            window(100, p95_latency_ms=900.0),
        )
        assert regressions == []

    def test_a_window_with_no_confidence_at_all_is_skipped(self) -> None:
        """NULL means the model reported none, which is not the same as
        reporting zero. Reading it as 0.0 would be a fabricated collapse."""
        regressions, _ = compare(
            window(100, mean_confidence=None),
            window(100, mean_confidence=0.9),
        )
        assert regressions == []

    def test_a_baseline_with_no_confidence_is_also_skipped(self) -> None:
        regressions, _ = compare(
            window(100, mean_confidence=0.2),
            window(100, mean_confidence=None),
        )
        assert regressions == []


class TestRates:
    def test_rates_of_an_empty_window_are_zero_not_a_zero_division(
        self,
    ) -> None:
        empty = QualityWindow()
        assert empty.refusal_rate == 0.0
        assert empty.guard_fire_rate == 0.0
        assert empty.zero_evidence_rate == 0.0


class TestParseWindows:
    def test_it_splits_the_grouped_result(self) -> None:
        current, baseline = parse_windows([
            {"window": "current", "total_runs": 10, "refusals": 1,
             "guard_fires": 2, "zero_evidence": 3, "mean_confidence": 0.8,
             "p95_latency_ms": 1200.0},
            {"window": "baseline", "total_runs": 90, "refusals": 4,
             "guard_fires": 5, "zero_evidence": 6, "mean_confidence": 0.85,
             "p95_latency_ms": 1100.0},
        ])

        assert current.total_runs == 10 and current.refusals == 1
        assert baseline.total_runs == 90 and baseline.zero_evidence == 6

    def test_a_window_with_no_rows_is_absent_from_the_result(self) -> None:
        """GROUP BY emits nothing for an empty window, so this cannot
        index — a day with no queries at all is the normal quiet case."""
        current, baseline = parse_windows([
            {"window": "baseline", "total_runs": 90, "refusals": 4,
             "guard_fires": 5, "zero_evidence": 6, "mean_confidence": 0.85,
             "p95_latency_ms": 1100.0},
        ])

        assert current.total_runs == 0
        assert current.mean_confidence is None
        assert baseline.total_runs == 90

    def test_nulls_survive_as_none_not_zero(self) -> None:
        current, _ = parse_windows([
            {"window": "current", "total_runs": 5, "refusals": 0,
             "guard_fires": 0, "zero_evidence": 0, "mean_confidence": None,
             "p95_latency_ms": None},
        ])

        assert current.mean_confidence is None
        assert current.p95_latency_ms is None

    def test_an_empty_result_gives_two_empty_windows(self) -> None:
        current, baseline = parse_windows([])
        assert current.total_runs == 0 and baseline.total_runs == 0


class TestTheCronContract:
    def test_a_cron_payload_needs_no_fields(self) -> None:
        """A declarative on_crons trigger sends NO input — hatchet_sdk
        hardcodes cron_input=None — so `{}` is all the validator ever
        sees. A required field here means every tick dies on
        ValidationError before the task starts."""
        assert AnswerQualityWatchInput().baseline_days == 8

    def test_the_default_window_compares_a_day_against_a_week(self) -> None:
        assert AnswerQualityWatchInput().baseline_days == 8

    def test_the_alert_marker_is_distinctive_enough_to_grep(self) -> None:
        """Log Analytics has no metric to threshold on here, so the log
        line IS the signal and a scheduled query rule matches this
        string."""
        assert ALERT_MARKER.isupper()
        assert "_" in ALERT_MARKER
        assert len(ALERT_MARKER) > 12

    def test_the_worker_registers_it(self) -> None:
        from pathlib import Path

        worker = (
            Path(__file__).resolve().parent.parent
            / "app" / "hatchet_workflows" / "worker.py"
        ).read_text(encoding="utf-8")

        assert "answer_quality_watch" in worker


@pytest.mark.parametrize(
    "threshold", [REFUSAL_RISE_PP, CONFIDENCE_DROP],
)
def test_thresholds_are_not_accidentally_zero(threshold: float) -> None:
    """A threshold of 0 turns the watch into an alert on every wobble,
    which is the same outcome as switching it off."""
    assert threshold > 0
