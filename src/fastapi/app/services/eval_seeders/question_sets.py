"""Golden-question seed slots (§10.2) — doc-phase 99 skeleton.

8 question sets per §24.1. Each slot holds a list of seed questions
ready for population — empty in the skeleton. Per the §10 scope
proposal, ~50% of these are mechanical (OCR triage, schema mapping)
and can be autonomously populated; ~50% need Kyle SME (core_chat,
target_recommendation).
"""
from __future__ import annotations

from typing import Any, Literal

QuestionSet = Literal[
    "core_chat",
    "public_private_boundary",
    "numeric_grounding",
    "refusal_correctness",
    "target_recommendation",
    "report_section",
    "schema_mapping",
    "ocr_triage",
]


# Per-set guidance for the SME / contractor population pass.
QUESTION_SET_NOTES: dict[QuestionSet, str] = {
    "core_chat": (
        "Standard geological Q&A — 'what commodities are present in this "
        "AOI', 'what deposit model best fits these signatures'. ~15-20 "
        "questions; SME-authored."
    ),
    "public_private_boundary": (
        "Tests §2.9 language template enforcement. 'Does this property "
        "have uranium?' should produce the public-records-within-25km "
        "phrasing, not 'yes/no'. ~10 questions; SME-authored."
    ),
    "numeric_grounding": (
        "Tests numeric-claim verification (§04i layer 3). 'What was the "
        "highest Au assay on this property?' must match silver.assay_results "
        "with units. ~15 questions; mechanical (auto-grading from fixture)."
    ),
    "refusal_correctness": (
        "Tests refusal correctness — questions the system MUST refuse "
        "vs. ones it MUST answer. ~10 questions; SME-authored."
    ),
    "target_recommendation": (
        "Tests target recommendation flow — sign-off ceremony language, "
        "explainability, 'never say drill here' enforcement. ~10 questions; "
        "SME-authored."
    ),
    "report_section": (
        "Tests report section generation — TDD Item 4, NI 43-101 Item 6, "
        "etc. produce structured output. ~10 questions; mechanical."
    ),
    "schema_mapping": (
        "Tests schema-mapping correctness — given a raw spreadsheet, "
        "does the system map to canonical silver.* schema? ~10 questions; "
        "mechanical."
    ),
    "ocr_triage": (
        "Tests §04p OCR quality routing — given a synthetic page with "
        "known low confidence, does it route to silver_review? "
        "~10 questions; mechanical."
    ),
}


# Empty seed slots — to be populated by §10.2 SME / autonomous pass.
QUESTION_SET_SLOTS: dict[QuestionSet, list[dict[str, Any]]] = {
    qs: [] for qs in QUESTION_SET_NOTES
}


def seed_question_sets() -> list[QuestionSet]:
    """Return the 8 canonical question set names."""
    return list(QUESTION_SET_NOTES.keys())


__all__ = [
    "QuestionSet",
    "QUESTION_SET_NOTES",
    "QUESTION_SET_SLOTS",
    "seed_question_sets",
]
