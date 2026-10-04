"""System-written refusal texts and the machine-readable refusal payload.

One home for the canned texts that are NOT model output, so the three
consumers that must agree about them -- ``layer2_typed_output._is_system_text``
(exempts them from guards), ``response_assembler._is_refusal`` (floors their
confidence) and ``validate_node`` (no "fact-checking flagged" banner in front
of a refusal) -- cannot drift apart.

Nothing here may name a guard, a layer or an internal enum in a user-visible
string: ``message`` renders verbatim in the chat's RefusalPanel.
"""

from __future__ import annotations

from typing import Any

#: What the reader sees when the model returned no usable content
#: (``llm_common.BUDGET_EXHAUSTED_FALLBACK`` is operator wording about token
#: budgets and must never reach the chat). Opens with a phrase
#: ``response_assembler._is_refusal`` recognises.
MODEL_NO_OUTPUT_TEXT = (
    "The model did not produce an answer this time. Please ask again."
)

#: The answer when every citation failed the chunk-provenance gate, or the
#: only sentences left after removing the rejected ones were empty. This used
#: to reuse Layer 1's text ("Document search found no passages ..."), which
#: is false here: passages were found, the answer just cited ones that were
#: not among them. Opens with "I don't have" for ``_is_refusal``.
PROVENANCE_REFUSAL_TEXT = (
    "I don't have an answer I can support with the documents found for this "
    "question. The draft answer pointed to sources that were not among the "
    "ones retrieved, so it was withheld rather than shown. Try rephrasing "
    "the question, or narrowing it to a specific hole, report or area."
)

#: Plain ``message`` for a withheld-for-unsupported-claims refusal payload.
UNSUPPORTED_BY_SOURCES_MESSAGE = (
    "The draft answer could not be tied to the documents and data found for "
    "this question, so it was withheld."
)

#: Plain ``message`` for a model that produced nothing.
MODEL_NO_OUTPUT_MESSAGE = (
    "The model did not produce an answer for this question. Asking again "
    "usually works."
)


def make_refusal_payload(reason_code: str, message: str) -> dict[str, Any]:
    """``GeoRAGResponse.refusal_payload`` for a refusal that ran no repair.

    Same shape as ``layer1_retrieval.build_refusal_payload`` and the one
    ``_build_terminal_refusal_payload`` produces, so the RefusalPanel and
    persist's ``rejection_reason`` read every refusal the same way.
    """
    return {
        "type": "refusal",
        "reason_code": reason_code,
        "strategy": None,
        "message": message,
        "candidates": [],
        "guard_codes": [],
    }


def is_budget_exhausted_text(text: str | None) -> bool:
    """True when ``text`` is ``llm_common.BUDGET_EXHAUSTED_FALLBACK``.

    The adapters return that operator-facing string instead of raising when
    the model produced no content. It must be replaced before it is assembled
    into a response: it reads as a clean, cited, high-confidence answer to
    every guard that exempts system text.
    """
    from app.agent.llm_common import BUDGET_EXHAUSTED_FALLBACK  # noqa: PLC0415

    return (text or "").strip() == BUDGET_EXHAUSTED_FALLBACK.strip()
