"""Tests for the orchestrator's prompt-selection chain — Phase 1 / Steps 1.2 + 1.4.

Confirms:
  - flag OFF: no OIUR rules, no decision-support rules appended
  - flag ON + non-DS query: OIUR rules appended, decision-support NOT appended
  - flag ON + DS query: OIUR + decision-support rules appended
  - flag ON + DS query + regulatory touch: regulatory-required block appended
"""

from __future__ import annotations

import pytest

from app.agent.orchestrator import _select_system_prompt
from app.agent.prompts.decision_support_section import (
    DECISION_SUPPORT_OUTPUT_RULES,
    DECISION_SUPPORT_REGULATORY_REQUIRED,
)
from app.agent.prompts.oiur_section import OIUR_OUTPUT_RULES
from app.config import settings


def test_flag_off_no_oiur_no_decision_support(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GEO_ANSWER_OIUR_ENABLED", False)
    prompt = _select_system_prompt(
        categories=None, query="Should we drill DDH-13?"
    )
    assert OIUR_OUTPUT_RULES not in prompt
    assert DECISION_SUPPORT_OUTPUT_RULES not in prompt


def test_flag_on_non_decision_query_no_decision_support_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "GEO_ANSWER_OIUR_ENABLED", True)
    prompt = _select_system_prompt(
        categories=None, query="How deep is DDH-07?"
    )
    assert OIUR_OUTPUT_RULES in prompt
    assert DECISION_SUPPORT_OUTPUT_RULES not in prompt
    assert DECISION_SUPPORT_REGULATORY_REQUIRED not in prompt


def test_flag_on_decision_query_appends_decision_support_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "GEO_ANSWER_OIUR_ENABLED", True)
    prompt = _select_system_prompt(
        categories=None,
        query="Rank the four candidate targets by structural complexity.",
    )
    assert OIUR_OUTPUT_RULES in prompt
    assert DECISION_SUPPORT_OUTPUT_RULES in prompt
    # No regulatory touch on a pure targeting question.
    assert DECISION_SUPPORT_REGULATORY_REQUIRED not in prompt


def test_flag_on_regulatory_decision_query_appends_full_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "GEO_ANSWER_OIUR_ENABLED", True)
    prompt = _select_system_prompt(
        categories=None,
        query="Should we apply for a Measured Resource classification?",
    )
    assert OIUR_OUTPUT_RULES in prompt
    assert DECISION_SUPPORT_OUTPUT_RULES in prompt
    assert DECISION_SUPPORT_REGULATORY_REQUIRED in prompt


def test_no_query_passed_skips_decision_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "GEO_ANSWER_OIUR_ENABLED", True)
    prompt = _select_system_prompt(categories=None, query=None)
    assert OIUR_OUTPUT_RULES in prompt
    # Without a query, classifier cannot run — decision-support rules
    # must NOT be appended (default-safe behaviour).
    assert DECISION_SUPPORT_OUTPUT_RULES not in prompt
