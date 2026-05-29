"""§10.2 mechanical golden-question content (doc-phase 124).

Per the §10 scope proposal, ~50% of the 100 §24.1 golden questions are
mechanical and autonomous-safe (no SME judgment needed — they test
fixed system behaviors):

    numeric_grounding (15)  — §04i layer 3 numeric-claim verification
    report_section    (10)  — §15.1/§15.2 report structure integrity
    schema_mapping    (10)  — §04 schema-mapping decision correctness
    ocr_triage        (10)  — §04p / §9.7 quality_graph routing

The other 50 SME-authored questions (core_chat, public_private_boundary,
refusal_correctness, target_recommendation) follow the same template
once Kyle authors them.

Pattern:
    1. Each question_set has its own module with a `QUESTIONS` list of
       dicts. Dict shape matches `eval.golden_questions` columns
       verbatim — `question_text`, `context_setup`, `expected_*`,
       `difficulty`.
    2. The seeder takes a stable UUID per question by hashing
       (question_set, question_text). Re-seeding produces identical
       IDs — true idempotence.
    3. Mechanical questions land with `status='active'` immediately
       (no human review queue — they test deterministic system
       behaviors, not interpretive output).
"""
from app.services.eval.mechanical_questions.core_chat_wyoming_uranium import (
    QUESTIONS as CORE_CHAT_WYOMING_QUESTIONS,
)
from app.services.eval.mechanical_questions.numeric_grounding import (
    QUESTIONS as NUMERIC_GROUNDING_QUESTIONS,
)
from app.services.eval.mechanical_questions.ocr_triage import (
    QUESTIONS as OCR_TRIAGE_QUESTIONS,
)
from app.services.eval.mechanical_questions.refusal_correctness import (
    QUESTIONS as REFUSAL_CORRECTNESS_QUESTIONS,
)
from app.services.eval.mechanical_questions.report_section import (
    QUESTIONS as REPORT_SECTION_QUESTIONS,
)
from app.services.eval.mechanical_questions.schema_mapping import (
    QUESTIONS as SCHEMA_MAPPING_QUESTIONS,
)
from app.services.eval.mechanical_questions.seed_runner import (
    SeedReport,
    seed_mechanical_questions,
)


ALL_MECHANICAL_QUESTIONS: list[dict] = (
    NUMERIC_GROUNDING_QUESTIONS
    + REPORT_SECTION_QUESTIONS
    + SCHEMA_MAPPING_QUESTIONS
    + OCR_TRIAGE_QUESTIONS
    + REFUSAL_CORRECTNESS_QUESTIONS
    + CORE_CHAT_WYOMING_QUESTIONS
)


__all__ = [
    "ALL_MECHANICAL_QUESTIONS",
    "CORE_CHAT_WYOMING_QUESTIONS",
    "NUMERIC_GROUNDING_QUESTIONS",
    "OCR_TRIAGE_QUESTIONS",
    "REFUSAL_CORRECTNESS_QUESTIONS",
    "REPORT_SECTION_QUESTIONS",
    "SCHEMA_MAPPING_QUESTIONS",
    "SeedReport",
    "seed_mechanical_questions",
]
