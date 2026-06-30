"""§04p persistence integration tests (master-plan §3 Step 7b, doc-phase 56).

End-to-end test: orchestrator on PLS-2024 → persist → verify silver rows.

These tests touch the running PostgreSQL. They run inside the
georag-fastapi container which connects to postgres via the
POSTGRES_DIRECT_* env vars. Tests are reversible — each test creates
its own throwaway report row and deletes it at teardown (CASCADE
cleans up the silver rows we wrote).
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ocr"
PLS_2024 = FIXTURE_DIR / "PLS-2024-Technical-Report.pdf"

# Pre-existing workspace seeded in the dev DB.
TEST_WORKSPACE_ID = "a0000000-0000-0000-0000-000000000001"


@pytest.fixture(scope="module")
def native_pdf_path() -> Path:
    if not PLS_2024.exists():
        pytest.skip(f"fixture not found: {PLS_2024}")
    return PLS_2024


@pytest.fixture(scope="module")
def db_available() -> bool:
    """Skip persistence tests when direct postgres env isn't available
    (e.g. running these tests outside the container).
    """
    return bool(os.environ.get("POSTGRES_USER") and os.environ.get("POSTGRES_PASSWORD"))


async def _make_pool():
    import asyncpg

    from app.ocr._persist import _dsn

    return await asyncpg.create_pool(
        _dsn(), min_size=1, max_size=2, statement_cache_size=0
    )


async def _make_test_report(pool, title: str = "phase56-test-report") -> str:
    """Insert a minimal silver.reports row and return its report_id."""
    report_id = str(uuid.uuid4())
    async with pool.acquire() as conn:
        # Block-1 RLS (2026-05-15): silver.reports requires workspace_id.
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", TEST_WORKSPACE_ID,
        )
        await conn.execute(
            "INSERT INTO silver.reports (report_id, title, workspace_id) "
            "VALUES ($1::uuid, $2, $3::uuid)",
            report_id, title, TEST_WORKSPACE_ID,
        )
    return report_id


async def _delete_test_report(pool, report_id: str) -> None:
    """Delete the report — CASCADE cleans up all silver rows we wrote."""
    async with pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", TEST_WORKSPACE_ID,
        )
        await conn.execute(
            "DELETE FROM silver.reports WHERE report_id = $1::uuid",
            report_id,
        )


# ----- end-to-end orchestrator + persist -----

def test_persist_orchestrator_native_pdf_e2e(
    native_pdf_path: Path, db_available: bool
) -> None:
    if not db_available:
        pytest.skip("POSTGRES_USER/POSTGRES_PASSWORD not set in this env")

    from app.ocr._orchestrator import orchestrate
    from app.ocr._persist import (
        persist_orchestrator_result,
        transactional_workspace_session,
    )

    async def run() -> dict:
        result = await orchestrate(native_pdf_path)

        pool = await _make_pool()
        try:
            report_id = await _make_test_report(pool, "phase56-native-e2e")
            try:
                async with transactional_workspace_session(
                    pool, TEST_WORKSPACE_ID
                ) as conn:
                    counts = await persist_orchestrator_result(
                        conn, TEST_WORKSPACE_ID, report_id, result
                    )

                # Verify rows landed.
                async with pool.acquire() as conn:
                    parser_artifacts = await conn.fetchval(
                        "SELECT COUNT(*) FROM silver.parser_run_artifacts WHERE report_id = $1::uuid",
                        report_id,
                    )
                    page_quality = await conn.fetchval(
                        "SELECT COUNT(*) FROM silver.ocr_page_quality WHERE report_id = $1::uuid",
                        report_id,
                    )
                    doc_quality = await conn.fetchval(
                        "SELECT recommended_action FROM silver.document_ingestion_quality WHERE report_id = $1::uuid",
                        report_id,
                    )
                    extractions = await conn.fetchval(
                        "SELECT COUNT(*) FROM silver.ingest_extractions WHERE report_id = $1::uuid",
                        report_id,
                    )
                    return {
                        "report_id": report_id,
                        "counts": counts,
                        "db_parser_artifacts": parser_artifacts,
                        "db_page_quality": page_quality,
                        "db_doc_quality_action": doc_quality,
                        "db_extractions": extractions,
                        "preflight_page_count": result["preflight"]["page_count"],
                    }
            finally:
                await _delete_test_report(pool, report_id)
        finally:
            await pool.close()

    out = asyncio.run(run())

    # parser_run_artifacts: preflight + profiler + native = 3 rows
    assert out["counts"]["parser_run_artifacts"] >= 2
    assert out["db_parser_artifacts"] == out["counts"]["parser_run_artifacts"]

    # One ocr_page_quality row per page
    assert out["db_page_quality"] == out["preflight_page_count"]

    # document_ingestion_quality recommends accept (PLS-2024 is clean native)
    assert out["db_doc_quality_action"] in {"accept", "accept_with_review"}

    # ingest_extractions: PLS-2024 has ~30-50 passages across 7 pages
    assert out["db_extractions"] >= 10


def test_persist_orchestrator_invalid_pdf_writes_reject(
    tmp_path: Path, db_available: bool
) -> None:
    """Invalid preflight → recommendation == reject, no ingest_* rows."""
    if not db_available:
        pytest.skip("POSTGRES_USER/POSTGRES_PASSWORD not set in this env")

    from app.ocr._orchestrator import orchestrate
    from app.ocr._persist import (
        persist_orchestrator_result,
        transactional_workspace_session,
    )

    bad_pdf = tmp_path / "not-a-pdf.pdf"
    bad_pdf.write_bytes(b"Plain text masquerading as PDF.")

    async def run() -> dict:
        result = await orchestrate(bad_pdf)

        pool = await _make_pool()
        try:
            report_id = await _make_test_report(pool, "phase56-reject-e2e")
            try:
                async with transactional_workspace_session(
                    pool, TEST_WORKSPACE_ID
                ) as conn:
                    counts = await persist_orchestrator_result(
                        conn, TEST_WORKSPACE_ID, report_id, result
                    )

                async with pool.acquire() as conn:
                    doc_action = await conn.fetchval(
                        "SELECT recommended_action FROM silver.document_ingestion_quality WHERE report_id = $1::uuid",
                        report_id,
                    )
                    extractions = await conn.fetchval(
                        "SELECT COUNT(*) FROM silver.ingest_extractions WHERE report_id = $1::uuid",
                        report_id,
                    )
                    parser_artifacts = await conn.fetchval(
                        "SELECT COUNT(*) FROM silver.parser_run_artifacts WHERE report_id = $1::uuid",
                        report_id,
                    )
                    return {
                        "counts": counts,
                        "db_doc_action": doc_action,
                        "db_extractions": extractions,
                        "db_parser_artifacts": parser_artifacts,
                    }
            finally:
                await _delete_test_report(pool, report_id)
        finally:
            await pool.close()

    out = asyncio.run(run())

    # Document recommends reject, no ingest_extractions rows (no parsing happened)
    assert out["db_doc_action"] == "reject"
    assert out["db_extractions"] == 0
    # But we DO get a parser_run_artifacts row for preflight (with error logged)
    assert out["db_parser_artifacts"] >= 1


# ----- transactional_workspace_session -----

def test_transactional_workspace_session_sets_guc(db_available: bool) -> None:
    if not db_available:
        pytest.skip("POSTGRES_USER/POSTGRES_PASSWORD not set in this env")

    from app.ocr._persist import transactional_workspace_session

    async def run() -> str:
        pool = await _make_pool()
        try:
            async with transactional_workspace_session(
                pool, TEST_WORKSPACE_ID
            ) as conn:
                guc_value = await conn.fetchval(
                    "SELECT current_setting('app.workspace_id', true)"
                )
                return guc_value
        finally:
            await pool.close()

    out = asyncio.run(run())
    assert out == TEST_WORKSPACE_ID


# ----- _to_db_numeric / _compute_doc_quality_score smoke -----

def test_to_db_numeric_handles_none_and_floats() -> None:
    from app.ocr._persist import _to_db_numeric

    assert _to_db_numeric(None) is None
    assert _to_db_numeric(0.5) == 0.5
    assert _to_db_numeric("0.7") == 0.7
    assert _to_db_numeric("garbage") is None


def test_compute_doc_quality_score_basics() -> None:
    from app.ocr._persist import _compute_doc_quality_score

    assert _compute_doc_quality_score([]) is None
    decisions = [
        {"route": "accept"},
        {"route": "accept"},
        {"route": "silver_review"},
        {"route": "accept"},
    ]
    assert _compute_doc_quality_score(decisions) == 0.75
