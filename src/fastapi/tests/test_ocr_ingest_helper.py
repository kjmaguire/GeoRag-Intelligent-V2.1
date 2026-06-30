"""§04p ingest-helper behaviour tests (master-plan §3 Step 7c, doc-phase 57).

Tests the bridge between the Hatchet `ingest_pdf.persist` step and
the orchestrator + persistence chain. Operates against the real
PostgreSQL (creates throwaway silver.reports row, asserts §04p
rows land, deletes via CASCADE for teardown).

These tests do NOT invoke the Hatchet engine — they call the helper
function directly with PDF bytes. The Hatchet step contract change is
covered by the fact that the helper is the only new code path; the
existing v1.49 writes in ingest_pdf.persist are unchanged and have
their own coverage in the broader test suite.
"""
from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ocr"
PLS_2024 = FIXTURE_DIR / "PLS-2024-Technical-Report.pdf"

TEST_WORKSPACE_ID = "a0000000-0000-0000-0000-000000000001"


@pytest.fixture(scope="module")
def native_pdf_bytes() -> bytes:
    if not PLS_2024.exists():
        pytest.skip(f"fixture not found: {PLS_2024}")
    return PLS_2024.read_bytes()


@pytest.fixture(scope="module")
def db_available() -> bool:
    return bool(os.environ.get("POSTGRES_USER") and os.environ.get("POSTGRES_PASSWORD"))


async def _make_pool():
    import asyncpg

    from app.ocr._persist import _dsn

    return await asyncpg.create_pool(
        _dsn(), min_size=1, max_size=2, statement_cache_size=0
    )


async def _make_test_report(pool, title: str) -> str:
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
    async with pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", TEST_WORKSPACE_ID,
        )
        await conn.execute(
            "DELETE FROM silver.reports WHERE report_id = $1::uuid",
            report_id,
        )


# ----- happy path -----

def test_run_p04p_for_ingest_happy_path(
    native_pdf_bytes: bytes, db_available: bool
) -> None:
    if not db_available:
        pytest.skip("POSTGRES_USER/POSTGRES_PASSWORD not set in this env")

    from app.ocr._ingest_helper import run_p04p_for_ingest

    async def run() -> dict:
        pool = await _make_pool()
        try:
            report_id = await _make_test_report(pool, "phase57-helper-happy")
            try:
                telemetry = await run_p04p_for_ingest(
                    workspace_id=TEST_WORKSPACE_ID,
                    report_id=report_id,
                    pdf_body=native_pdf_bytes,
                )

                # Verify silver rows landed.
                async with pool.acquire() as conn:
                    extractions = await conn.fetchval(
                        "SELECT COUNT(*) FROM silver.ingest_extractions WHERE report_id = $1::uuid",
                        report_id,
                    )
                    doc_action = await conn.fetchval(
                        "SELECT recommended_action FROM silver.document_ingestion_quality WHERE report_id = $1::uuid",
                        report_id,
                    )
                    return {
                        "telemetry": telemetry,
                        "db_extractions": extractions,
                        "db_doc_action": doc_action,
                    }
            finally:
                await _delete_test_report(pool, report_id)
        finally:
            await pool.close()

    out = asyncio.run(run())

    assert out["telemetry"]["ok"] is True
    assert out["telemetry"]["error"] is None
    assert out["telemetry"]["document_profile"] == "native"
    assert out["telemetry"]["recommended_action"] in {"accept", "accept_with_review"}
    assert out["telemetry"]["counts"]["ingest_extractions"] >= 10
    assert out["telemetry"]["counts"]["ocr_page_quality"] >= 1
    assert out["telemetry"]["counts"]["parser_run_artifacts"] >= 2

    assert out["db_extractions"] >= 10
    assert out["db_doc_action"] in {"accept", "accept_with_review"}


# ----- defense in depth: helper catches its own exceptions -----

def test_run_p04p_for_ingest_handles_invalid_pdf(
    db_available: bool
) -> None:
    """Garbage bytes should produce telemetry.ok=True with reject action
    (preflight catches the magic-mismatch and the orchestrator routes
    to a synthetic reject decision; persistence writes the doc-level
    reject row but no ingest_* rows).
    """
    if not db_available:
        pytest.skip("POSTGRES_USER/POSTGRES_PASSWORD not set in this env")

    from app.ocr._ingest_helper import run_p04p_for_ingest

    async def run() -> dict:
        pool = await _make_pool()
        try:
            report_id = await _make_test_report(pool, "phase57-helper-bad-pdf")
            try:
                telemetry = await run_p04p_for_ingest(
                    workspace_id=TEST_WORKSPACE_ID,
                    report_id=report_id,
                    pdf_body=b"This is not a PDF.",
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
                    return {
                        "telemetry": telemetry,
                        "db_doc_action": doc_action,
                        "db_extractions": extractions,
                    }
            finally:
                await _delete_test_report(pool, report_id)
        finally:
            await pool.close()

    out = asyncio.run(run())

    # The helper itself succeeds (no exception raised); orchestrator's
    # preflight gate produces a reject doc summary.
    assert out["telemetry"]["ok"] is True
    assert out["telemetry"]["recommended_action"] == "reject"
    assert out["db_doc_action"] == "reject"
    assert out["db_extractions"] == 0


def test_run_p04p_for_ingest_handles_missing_workspace(
    native_pdf_bytes: bytes, db_available: bool
) -> None:
    """If the workspace_id doesn't exist (or report_id is bogus), the
    helper logs the error and returns telemetry.ok=False — does NOT
    raise. This is the explicit safety contract for dual-write.
    """
    if not db_available:
        pytest.skip("POSTGRES_USER/POSTGRES_PASSWORD not set in this env")

    from app.ocr._ingest_helper import run_p04p_for_ingest

    async def run() -> dict:
        # Bogus IDs — FK violations expected on the silver writes.
        bogus_workspace = "deadbeef-dead-beef-dead-beefdeadbeef"
        bogus_report = str(uuid.uuid4())  # no actual silver.reports row

        return await run_p04p_for_ingest(
            workspace_id=bogus_workspace,
            report_id=bogus_report,
            pdf_body=native_pdf_bytes,
        )

    telemetry = asyncio.run(run())

    assert telemetry["ok"] is False
    assert telemetry["error"] is not None
