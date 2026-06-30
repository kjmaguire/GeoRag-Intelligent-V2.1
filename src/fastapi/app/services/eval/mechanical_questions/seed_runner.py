"""Mechanical golden-questions seeder — doc-phase 124.

Idempotently lands the 45 mechanical questions into
`eval.golden_questions`. Stable per-question UUID derived from a hash
of (question_set, question_text) — re-seeding produces identical
question_ids so any reference (e.g. `eval.run_results.question_id`)
stays stable across re-seeds.

Mechanical questions land with `status='active'` immediately (no
human-review queue — they exercise deterministic system behaviors,
not interpretive output).
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

import asyncpg


@dataclass(frozen=True, slots=True)
class SeedReport:
    """Result of a mechanical-questions seed run."""

    inserted: int
    updated: int
    unchanged: int
    total_processed: int
    question_ids: list[UUID]


def stable_question_id(question_set: str, question_text: str) -> UUID:
    """SHA-256-derived UUID — same (set, text) always maps to same id."""
    digest = hashlib.sha256(
        f"{question_set}|{question_text}".encode()
    ).digest()
    # Set UUID v4 variant + version bits.
    digest = bytearray(digest[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40  # version 4
    digest[8] = (digest[8] & 0x3F) | 0x80  # variant RFC 4122
    return UUID(bytes=bytes(digest))


async def seed_mechanical_questions(
    conn: asyncpg.Connection,
    *,
    authored_by_user_id: int,
    questions: Sequence[dict],
) -> SeedReport:
    """Idempotently upsert each question into `eval.golden_questions`.

    Args:
        conn: asyncpg Connection. Function manages its own transaction.
        authored_by_user_id: public.users.id recorded as author/reviewer
            on each row (mechanical questions are self-reviewing).
        questions: list of dicts matching the eval.golden_questions
            column shape.

    Returns:
        SeedReport.
    """
    if not questions:
        return SeedReport(0, 0, 0, 0, [])

    inserted = 0
    updated = 0
    unchanged = 0
    ids: list[UUID] = []

    async with conn.transaction():
        for q in questions:
            qset = q["question_set"]
            qtext = q["question_text"]
            qid = stable_question_id(qset, qtext)
            ids.append(qid)

            existing = await conn.fetchrow(
                """
                SELECT question_text, context_setup::text, expected_intent_class,
                       expected_citations::text, expected_entities::text,
                       expected_numeric_values::text, expected_refusal,
                       expected_refusal_reason, expected_language_compliance::text,
                       difficulty, status
                FROM eval.golden_questions
                WHERE question_id = $1::uuid
                """,
                str(qid),
            )

            ctx_json = json.dumps(q.get("context_setup") or {}, default=str)
            cits_json = json.dumps(q.get("expected_citations") or [], default=str)
            ents_json = json.dumps(q.get("expected_entities") or [], default=str)
            nums_json = json.dumps(q.get("expected_numeric_values") or [], default=str)
            lang_json = json.dumps(q.get("expected_language_compliance") or [], default=str)
            diff = q.get("difficulty", "medium")
            intent = q.get("expected_intent_class")
            refusal = bool(q.get("expected_refusal", False))
            refusal_reason = q.get("expected_refusal_reason")

            if existing is None:
                await conn.execute(
                    """
                    INSERT INTO eval.golden_questions (
                        question_id, question_set, question_text, context_setup,
                        expected_intent_class, expected_citations, expected_entities,
                        expected_numeric_values, expected_refusal,
                        expected_refusal_reason, expected_language_compliance,
                        difficulty, authored_by_user_id, authored_at,
                        reviewed_by_user_id, reviewed_at, status
                    )
                    VALUES (
                        $1::uuid, $2, $3, $4::jsonb,
                        $5, $6::jsonb, $7::jsonb, $8::jsonb,
                        $9, $10, $11::jsonb,
                        $12, $13, NOW(), $13, NOW(), 'active'
                    )
                    """,
                    str(qid), qset, qtext, ctx_json,
                    intent, cits_json, ents_json, nums_json,
                    refusal, refusal_reason, lang_json,
                    diff, authored_by_user_id,
                )
                inserted += 1
                continue

            # Compare JSONB fields by parsed structural equality, not text —
            # Postgres reorders JSONB keys vs Python's json.dumps order, so
            # text comparison produces false-positive "updated" outcomes
            # on idempotent re-seeds.
            def _eq_json(stored_text: str | None, new_obj: object) -> bool:
                if stored_text is None or stored_text == "":
                    return new_obj in ({}, [], None)
                try:
                    return json.loads(stored_text) == new_obj
                except (json.JSONDecodeError, TypeError):
                    return False

            new_ctx = q.get("context_setup") or {}
            new_cits = q.get("expected_citations") or []
            new_ents = q.get("expected_entities") or []
            new_nums = q.get("expected_numeric_values") or []
            new_lang = q.get("expected_language_compliance") or []

            same = (
                existing["question_text"] == qtext
                and _eq_json(existing["context_setup"], new_ctx)
                and existing["expected_intent_class"] == intent
                and _eq_json(existing["expected_citations"], new_cits)
                and _eq_json(existing["expected_entities"], new_ents)
                and _eq_json(existing["expected_numeric_values"], new_nums)
                and existing["expected_refusal"] == refusal
                and existing["expected_refusal_reason"] == refusal_reason
                and _eq_json(existing["expected_language_compliance"], new_lang)
                and existing["difficulty"] == diff
                and existing["status"] == "active"
            )
            if same:
                unchanged += 1
                continue

            await conn.execute(
                """
                UPDATE eval.golden_questions SET
                    question_text = $2,
                    context_setup = $3::jsonb,
                    expected_intent_class = $4,
                    expected_citations = $5::jsonb,
                    expected_entities = $6::jsonb,
                    expected_numeric_values = $7::jsonb,
                    expected_refusal = $8,
                    expected_refusal_reason = $9,
                    expected_language_compliance = $10::jsonb,
                    difficulty = $11,
                    reviewed_by_user_id = $12,
                    reviewed_at = NOW(),
                    status = 'active'
                WHERE question_id = $1::uuid
                """,
                str(qid), qtext, ctx_json,
                intent, cits_json, ents_json, nums_json,
                refusal, refusal_reason, lang_json,
                diff, authored_by_user_id,
            )
            updated += 1

    return SeedReport(
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        total_processed=len(questions),
        question_ids=ids,
    )


__all__ = ["SeedReport", "seed_mechanical_questions", "stable_question_id"]
