"""Live tests for §10.2 mechanical golden questions (doc-phase 124).

Pure-shape tests (no DB) plus end-to-end seeder tests against the
live `eval.golden_questions` table.
"""
from __future__ import annotations

import os

import asyncpg
import pytest

from app.services.eval.mechanical_questions import (
    ALL_MECHANICAL_QUESTIONS,
    NUMERIC_GROUNDING_QUESTIONS,
    OCR_TRIAGE_QUESTIONS,
    REFUSAL_CORRECTNESS_QUESTIONS,
    REPORT_SECTION_QUESTIONS,
    SCHEMA_MAPPING_QUESTIONS,
    seed_mechanical_questions,
)
from app.services.eval.mechanical_questions.seed_runner import stable_question_id


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@pytest.fixture
async def conn():
    c = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        yield c
    finally:
        await c.close()


@pytest.fixture
async def synthetic_user(conn):
    from uuid import uuid4
    email = f"mech-q-test-{uuid4()}@example.com"
    user_id = await conn.fetchval(
        "INSERT INTO public.users (name, email, password) VALUES ($1,$2,$3) RETURNING id",
        "Mechanical Q Test", email, "test-hash",
    )
    try:
        yield user_id
    finally:
        await conn.execute("DELETE FROM public.users WHERE id = $1", user_id)


# ---------------------------------------------------------------------------
# Pure-shape tests (no DB needed)
# ---------------------------------------------------------------------------

def test_question_counts():
    """Each set has its expected count."""
    assert len(NUMERIC_GROUNDING_QUESTIONS) == 15
    assert len(SCHEMA_MAPPING_QUESTIONS) == 10
    assert len(OCR_TRIAGE_QUESTIONS) == 10
    assert len(REPORT_SECTION_QUESTIONS) == 10
    assert len(REFUSAL_CORRECTNESS_QUESTIONS) == 8
    # 53 mechanical + 22 Wyoming core_chat = 75 (follow-up that pulled the
    # previously-orphaned core_chat_wyoming_uranium set into the seeder so
    # autonomous re-seeds keep all 22 Wyoming golden questions in sync).
    assert len(ALL_MECHANICAL_QUESTIONS) == 75


def test_question_sets_match_module():
    """Every question's question_set matches its module name."""
    for q in NUMERIC_GROUNDING_QUESTIONS:
        assert q["question_set"] == "numeric_grounding"
    for q in SCHEMA_MAPPING_QUESTIONS:
        assert q["question_set"] == "schema_mapping"
    for q in OCR_TRIAGE_QUESTIONS:
        assert q["question_set"] == "ocr_triage"
    for q in REPORT_SECTION_QUESTIONS:
        assert q["question_set"] == "report_section"
    for q in REFUSAL_CORRECTNESS_QUESTIONS:
        assert q["question_set"] == "refusal_correctness"
        assert q["expected_refusal"] is True
        assert q.get("expected_refusal_reason"), (
            "refusal_correctness questions must carry expected_refusal_reason"
        )


def test_required_fields_present():
    """Every question has question_set + question_text + difficulty."""
    for q in ALL_MECHANICAL_QUESTIONS:
        assert "question_set" in q and q["question_set"]
        assert "question_text" in q and q["question_text"]
        assert "difficulty" in q and q["difficulty"] in ("easy", "medium", "hard")


def test_question_texts_unique_per_set():
    """No duplicate (question_set, question_text) — would map to same UUID."""
    seen: set[tuple[str, str]] = set()
    for q in ALL_MECHANICAL_QUESTIONS:
        key = (q["question_set"], q["question_text"])
        assert key not in seen, f"Duplicate question: {key}"
        seen.add(key)


def test_stable_question_id_is_deterministic():
    """Same (set, text) → same UUID; different → different."""
    a = stable_question_id("numeric_grounding", "What is the maximum Au value?")
    b = stable_question_id("numeric_grounding", "What is the maximum Au value?")
    c = stable_question_id("numeric_grounding", "What is the minimum Au value?")
    d = stable_question_id("schema_mapping", "What is the maximum Au value?")
    assert a == b
    assert a != c
    assert a != d
    # All are valid UUIDs version 4
    assert a.version == 4


def test_refusal_questions_have_reason():
    """When expected_refusal=True, expected_refusal_reason must be set."""
    for q in ALL_MECHANICAL_QUESTIONS:
        if q.get("expected_refusal"):
            assert q.get("expected_refusal_reason"), (
                f"Refusal question without reason: {q['question_text']}"
            )


def test_numeric_grounding_expected_values_well_formed():
    """numeric_grounding questions have expected_numeric_values shape."""
    for q in NUMERIC_GROUNDING_QUESTIONS:
        evns = q.get("expected_numeric_values", [])
        assert evns, f"Missing expected_numeric_values: {q['question_text']}"
        for ev in evns:
            assert "path" in ev
            assert "unit" in ev
            assert "tolerance_pct" in ev
            assert "source_table" in ev


def test_schema_mapping_expected_entities_well_formed():
    """schema_mapping questions have raw_column → canonical_table.canonical_column."""
    for q in SCHEMA_MAPPING_QUESTIONS:
        ents = q.get("expected_entities", [])
        assert len(ents) == 1
        e = ents[0]
        assert "raw_column" in e
        assert "canonical_table" in e and e["canonical_table"].startswith("silver.")
        assert "canonical_column" in e


def test_ocr_triage_expected_routes_valid():
    """ocr_triage expected_route is one of the 4 §9.7 routes."""
    valid_routes = {"accept", "re_ocr", "silver_review", "reject"}
    for q in OCR_TRIAGE_QUESTIONS:
        ents = q.get("expected_entities", [])
        assert len(ents) == 1
        assert ents[0]["expected_route"] in valid_routes


def test_report_section_required_ids_match_template_slugs():
    """Every report_section question's required_section_ids match a real template."""
    from app.services.report_builder.templates import REPORT_TEMPLATES

    for q in REPORT_SECTION_QUESTIONS:
        report_type = q["context_setup"]["report_type"]
        assert report_type in REPORT_TEMPLATES, (
            f"Question references unknown report_type: {report_type}"
        )
        template_ids = {s.section_id for s in REPORT_TEMPLATES[report_type]}
        required = set(q["expected_entities"][0]["required_section_ids"])
        # Every required id must exist in the template
        assert required.issubset(template_ids), (
            f"required_section_ids {required - template_ids} "
            f"not in template for {report_type}"
        )


# ---------------------------------------------------------------------------
# Live seeder tests
#
# CRITICAL: these tests use SYNTHETIC question lists with prefixed text to
# avoid colliding with stable_question_ids that the production seeder writes.
# Earlier versions of these tests used ALL_MECHANICAL_QUESTIONS directly,
# which caused the test cleanup `DELETE` to nuke real production rows every
# time the verifier ran (doc-phase 128 incident). Synthetic questions =
# distinct stable IDs = test cleanup is fully isolated.
# ---------------------------------------------------------------------------

def _synthetic_questions(prefix: str) -> list[dict]:
    """Build a list of 6 synthetic-but-schema-valid questions for testing.

    Question_set values must satisfy the CHECK constraint, so we use real
    set names — but each question_text is prefixed so the SHA-derived
    stable_question_id never collides with production seeds.
    """
    base = [
        {"question_set": "numeric_grounding",
         "question_text": f"[{prefix}] max Au value test",
         "expected_numeric_values": [{"path": "x", "unit": "g/t",
                                       "tolerance_pct": 0.5,
                                       "source_table": "silver.assays"}],
         "difficulty": "easy"},
        {"question_set": "numeric_grounding",
         "question_text": f"[{prefix}] min Au value test",
         "expected_numeric_values": [{"path": "y", "unit": "g/t",
                                       "tolerance_pct": 0.5,
                                       "source_table": "silver.assays"}],
         "difficulty": "easy"},
        {"question_set": "schema_mapping",
         "question_text": f"[{prefix}] map test column A",
         "expected_entities": [{"raw_column": "A",
                                "canonical_table": "silver.assays",
                                "canonical_column": "a"}],
         "difficulty": "easy"},
        {"question_set": "schema_mapping",
         "question_text": f"[{prefix}] map test column B",
         "expected_entities": [{"raw_column": "B",
                                "canonical_table": "silver.assays",
                                "canonical_column": "b"}],
         "difficulty": "easy"},
        {"question_set": "ocr_triage",
         "question_text": f"[{prefix}] route synthetic page",
         "expected_entities": [{"expected_route": "accept",
                                "expected_reason": None}],
         "difficulty": "easy"},
        {"question_set": "report_section",
         "question_text": f"[{prefix}] generate sections for digest",
         "context_setup": {"report_type": "weekly_project_digest"},
         "expected_entities": [{"required_section_ids": ["summary"]}],
         "difficulty": "easy"},
    ]
    return base


@pytest.mark.asyncio
async def test_first_seed_inserts_all_synthetic(conn, synthetic_user):
    """First seed against a clean slate inserts every synthetic question."""
    from uuid import uuid4
    prefix = uuid4().hex[:8]
    questions = _synthetic_questions(prefix)
    expected = len(questions)

    stable_ids = [str(stable_question_id(q["question_set"], q["question_text"]))
                  for q in questions]

    try:
        report = await seed_mechanical_questions(
            conn,
            authored_by_user_id=synthetic_user,
            questions=questions,
        )

        assert report.total_processed == expected
        assert report.inserted == expected
        assert report.updated == 0
        assert report.unchanged == 0

        # Confirm they exist in DB with status='active'
        count = await conn.fetchval(
            "SELECT count(*) FROM eval.golden_questions "
            "WHERE question_id = ANY($1::uuid[]) AND status='active'",
            stable_ids,
        )
        assert count == expected
    finally:
        # Cleanup — synthetic-prefixed IDs don't collide with prod data
        await conn.execute(
            "DELETE FROM eval.golden_questions WHERE question_id = ANY($1::uuid[])",
            stable_ids,
        )


@pytest.mark.asyncio
async def test_reseed_is_idempotent(conn, synthetic_user):
    """Re-seeding with identical synthetic questions reports them unchanged."""
    from uuid import uuid4
    prefix = uuid4().hex[:8]
    questions = _synthetic_questions(prefix)
    expected = len(questions)

    stable_ids = [str(stable_question_id(q["question_set"], q["question_text"]))
                  for q in questions]

    try:
        r1 = await seed_mechanical_questions(
            conn, authored_by_user_id=synthetic_user, questions=questions,
        )
        r2 = await seed_mechanical_questions(
            conn, authored_by_user_id=synthetic_user, questions=questions,
        )

        assert r1.inserted == expected
        assert r2.inserted == 0
        assert r2.updated == 0
        assert r2.unchanged == expected
    finally:
        await conn.execute(
            "DELETE FROM eval.golden_questions WHERE question_id = ANY($1::uuid[])",
            stable_ids,
        )


@pytest.mark.asyncio
async def test_changed_question_triggers_update(conn, synthetic_user):
    """Modifying a question's difficulty triggers update, not insert."""
    from uuid import uuid4
    prefix = uuid4().hex[:8]
    questions = _synthetic_questions(prefix)
    expected = len(questions)

    stable_ids = [str(stable_question_id(q["question_set"], q["question_text"]))
                  for q in questions]

    try:
        # First seed
        await seed_mechanical_questions(
            conn, authored_by_user_id=synthetic_user, questions=questions,
        )

        # Mutate one question's difficulty in-memory + re-seed
        mutated = [dict(q) for q in questions]
        mutated[0] = {**mutated[0], "difficulty": "hard"}

        r = await seed_mechanical_questions(
            conn, authored_by_user_id=synthetic_user, questions=mutated,
        )
        assert r.inserted == 0
        assert r.updated == 1
        assert r.unchanged == expected - 1

        # Confirm the difficulty changed in DB
        row = await conn.fetchrow(
            "SELECT difficulty FROM eval.golden_questions WHERE question_id = $1::uuid",
            str(stable_question_id(mutated[0]["question_set"], mutated[0]["question_text"])),
        )
        assert row["difficulty"] == "hard"
    finally:
        await conn.execute(
            "DELETE FROM eval.golden_questions WHERE question_id = ANY($1::uuid[])",
            stable_ids,
        )


@pytest.mark.asyncio
async def test_seed_empty_list_is_noop(conn, synthetic_user):
    """Passing an empty list returns a zero SeedReport."""
    r = await seed_mechanical_questions(
        conn, authored_by_user_id=synthetic_user, questions=[],
    )
    assert r.total_processed == 0
    assert r.inserted == 0
