"""Text the chat shows a geologist carries no internal references.

Validator warnings are written for operators: they name hallucination
layers, CLAUDE.md rule numbers and design docs. They belong in
``validation_warnings`` and the logs. Two places used to copy them into
what the user sees — the caveat banner prepended to a flagged answer, and
the refusal panel's message for a terminal repair strategy — so a chat
answer could read "... (CLAUDE.md rule 4)". These tests pin both shut.
"""

from __future__ import annotations

import re

import pytest

from app.agent.agentic_retrieval.nodes import (
    _BANNER_REASON_DEFAULT,
    _banner_reason,
    _build_terminal_refusal_payload,
    _floor_confidence_with_warning_banner,
)
from app.agent.guards import GuardErrorCode
from app.agent.repair_strategy import TERMINAL_STRATEGIES

#: Anything that marks text as written for the team rather than the user.
_INTERNAL = re.compile(
    r"CLAUDE\.md|\bLayer\s*\d|\brule\s*#?\d|§|ADR-\d|\bL\d\b|strategy triggered|"
    r"[A-Z]{3,}_[A-Z_]{3,}",
)

# Real warnings, copied from the validators that emit them.
_WARNINGS = [
    (
        "Layer 1: weak retrieval — 2 chunk(s) considered, best reranker score 0.21",
        "weak match",
    ),
    (
        "Layer 2: 3 uncited claim sentence(s) removed — every claim must carry "
        "a citation to a retrieved source (CLAUDE.md rule 4)",
        "no supporting source",
    ),
    ("Layer 3: Ungrounded number 4.2 in response", "number"),
    ("Layer 3 tuple: value 5.2 reported as 'g/t' but source says '%'", "number"),
    ("Layer 4: Drill-hole ID 'DDH-999' not found in project records", "records"),
    ("Layer 4/6: fabrication or constraint signal detected", "records"),
    ("Layer 5: citation c1 names a chunk that was not retrieved", "citation"),
    ("Layer 6: grade 140% exceeds the physical maximum", "geological"),
]


@pytest.mark.parametrize(("warning", "expected"), _WARNINGS)
def test_banner_reason_is_plain_language(warning: str, expected: str) -> None:
    reason = _banner_reason(warning)
    assert expected in reason
    assert not _INTERNAL.search(reason), reason


@pytest.mark.parametrize("warning", [None, "", "something unexpected", "Layer 9: new guard"])
def test_unrecognised_warning_falls_back_to_generic_reason(warning: str | None) -> None:
    assert _banner_reason(warning) == _BANNER_REASON_DEFAULT


@pytest.mark.parametrize(("warning", "_expected"), _WARNINGS)
def test_banner_in_answer_text_has_no_internal_references(warning: str, _expected: str) -> None:
    class _Response:
        confidence = 0.9
        text = "Hole RS-01 returned 4.2 g/t Au over 3 m [1]."

        def model_copy(self, update: dict) -> _Response:
            copy = _Response()
            copy.confidence = update["confidence"]
            copy.text = update["text"]
            return copy

    flagged = _floor_confidence_with_warning_banner(_Response(), _banner_reason(warning))
    banner = flagged.text.removesuffix(_Response.text)
    assert "automated fact-checking flagged" in banner
    assert not _INTERNAL.search(banner), banner


@pytest.mark.parametrize("strategy", sorted(TERMINAL_STRATEGIES, key=str))
def test_terminal_refusal_message_has_no_internal_references(strategy) -> None:
    payload = _build_terminal_refusal_payload(
        None, strategy, [GuardErrorCode.CONFLICTING_SOURCES],
    )
    assert payload is not None
    message = payload["message"]
    assert message
    assert not _INTERNAL.search(message), message
    # Routing and audit still get the machine values, in their own fields.
    assert payload["strategy"] == strategy.value
    assert payload["guard_codes"] == [GuardErrorCode.CONFLICTING_SOURCES.value]


# ---------------------------------------------------------------------------
# Audit 2026-10-04 (items 2, 7, 8): withheld answers read as refusals
# ---------------------------------------------------------------------------


def test_system_refusal_texts_and_payload_messages_have_no_internal_references() -> None:
    from app.agent.hallucination.layer2_typed_output import CITATION_REFUSAL_TEXT
    from app.agent.hallucination.refusals import (
        MODEL_NO_OUTPUT_MESSAGE,
        MODEL_NO_OUTPUT_TEXT,
        PROVENANCE_REFUSAL_TEXT,
        UNSUPPORTED_BY_SOURCES_MESSAGE,
        make_refusal_payload,
    )

    for text in (
        CITATION_REFUSAL_TEXT, PROVENANCE_REFUSAL_TEXT, MODEL_NO_OUTPUT_TEXT,
        MODEL_NO_OUTPUT_MESSAGE, UNSUPPORTED_BY_SOURCES_MESSAGE,
    ):
        assert not _INTERNAL.search(text), text
    payload = make_refusal_payload("unsupported_by_sources", UNSUPPORTED_BY_SOURCES_MESSAGE)
    assert payload["type"] == "refusal"
    assert payload["reason_code"] == "unsupported_by_sources"


def test_operator_budget_text_is_never_the_user_text() -> None:
    from app.agent.hallucination.refusals import MODEL_NO_OUTPUT_TEXT
    from app.agent.llm_common import BUDGET_EXHAUSTED_FALLBACK

    assert "token budget" in BUDGET_EXHAUSTED_FALLBACK
    assert "budget" not in MODEL_NO_OUTPUT_TEXT.lower()
    assert "raise the configured" not in MODEL_NO_OUTPUT_TEXT


def test_banner_for_a_system_refusal_is_skipped_but_the_floor_applies() -> None:
    from app.agent.hallucination.refusals import PROVENANCE_REFUSAL_TEXT
    from app.agent.response_assembler import assemble_response

    response = assemble_response(PROVENANCE_REFUSAL_TEXT, [])
    out = _floor_confidence_with_warning_banner(response, _banner_reason("Layer 5: x"))
    assert out.text == PROVENANCE_REFUSAL_TEXT
    assert out.confidence <= 0.1
