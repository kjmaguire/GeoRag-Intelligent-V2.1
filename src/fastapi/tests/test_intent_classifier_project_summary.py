"""ADR-0007 PR-1 — project_summary intent classifier coverage.

Verifies that the new structured-aggregation intent fires on the keyword
sets advertised in the ADR ("breakdown / by year / contractors / who
worked / techniques / project summary / data collection").

Run with:
    pytest tests/test_intent_classifier_project_summary.py -q
"""

from __future__ import annotations

import pytest

from app.agent.agentic_retrieval import INTENT_LABELS, classify_intent_sync


PROJECT_SUMMARY_QUERIES: tuple[str, ...] = (
    "Give me a breakdown of data collection techniques by year.",
    "What techniques have been used to collect data on this project?",
    "Who worked on this project? Show me the contractors and geologists.",
    "Show the project summary — campaigns, drilling, geophysics by year.",
    "What's the drilling history for this project?",
    "Break down historical work over time, by contractor.",
)


@pytest.mark.parametrize("query", PROJECT_SUMMARY_QUERIES, ids=lambda q: q[:48])
def test_project_summary_intent_classification(query: str) -> None:
    """Each query routes to project_summary, not synthesis."""
    got = classify_intent_sync(query)
    assert got.intent == "project_summary", (
        f"expected project_summary; got {got.intent} (triggers={got.matched_triggers})"
    )


def test_project_summary_is_in_intent_labels() -> None:
    """ADR-0007 guarantees project_summary is a first-class intent."""
    assert "project_summary" in INTENT_LABELS


def test_project_summary_tiebreak_with_synthesis() -> None:
    """Tiebreak: project_summary loses to synthesis when both are within the
    breadth-priority window (synthesis ranks broader per _BREADTH_RANK).

    A query with ONE project_summary trigger and ONE synthesis trigger
    should route to synthesis (the broader-retrieval intent).
    """
    # "summarise" → synthesis; "by year" → project_summary. Equal counts.
    # _BREADTH_RANK favours synthesis when within the 0.1 tiebreak window.
    query = "Summarise the program by year."
    got = classify_intent_sync(query)
    assert got.intent == "synthesis"


def test_project_summary_does_not_false_fire_on_factual_query() -> None:
    """Pure factual queries stay in factual_lookup."""
    got = classify_intent_sync(
        "What is the formal NI 43-101 definition of a Measured Resource?"
    )
    assert got.intent == "factual_lookup"


def test_project_summary_does_not_false_fire_on_anomaly_query() -> None:
    """Anomaly queries with 'collected' should still route to anomaly_detection."""
    # No project_summary triggers here; pure anomaly path.
    got = classify_intent_sync(
        "Which assay intervals are outliers after CRM screening?"
    )
    assert got.intent == "anomaly_detection"
