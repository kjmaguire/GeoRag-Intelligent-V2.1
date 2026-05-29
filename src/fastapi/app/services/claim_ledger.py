"""§7.4 Claim Ledger service.

Records the structured claims an LLM makes in an answer + their
verification status. Callers (the orchestrator's response_assembler +
post-hoc claim_validator) write to this table; the Trust Inspector
reads from it.

Usage from response_assembler::

    from app.services.claim_ledger import record_claim, ClaimType, Support

    await record_claim(
        pool,
        workspace_id=workspace_id,
        answer_run_id=run.answer_run_id,
        claim_text="Hole 36-1042 intersected 4.2g/t Au over 12.5m.",
        claim_type=ClaimType.NUMERIC,
        required_support=Support.CITATION,
        sequence_in_answer=3,
        source_passage_id=passage_id,
    )

The claim_validator agent later sweeps pending rows + flips
verification_status to verified / failed / insufficient.
"""
from __future__ import annotations

import json
import logging
from enum import Enum
from typing import Any
from uuid import UUID

import asyncpg

log = logging.getLogger("georag.claim_ledger")


class ClaimType(str, Enum):
    NUMERIC = "numeric"
    ENTITY = "entity"
    TEMPORAL = "temporal"
    SPATIAL = "spatial"
    RELATIONSHIP = "relationship"
    REFUSAL = "refusal"
    QUALITATIVE = "qualitative"


class Support(str, Enum):
    CITATION = "citation"
    STRUCTURED_ROW = "structured_row"
    COMPUTATION = "computation"
    NONE = "none"


class VerificationStatus(str, Enum):
    PENDING = "pending"
    VERIFIED = "verified"
    FAILED = "failed"
    SKIPPED = "skipped"
    INSUFFICIENT = "insufficient"


async def record_claim(
    pool: asyncpg.Pool,
    *,
    workspace_id: UUID | str,
    answer_run_id: UUID | str,
    claim_text: str,
    claim_type: ClaimType,
    required_support: Support,
    sequence_in_answer: int | None = None,
    source_passage_id: UUID | str | None = None,
) -> str:
    """Insert a single claim. Returns claim_id."""
    workspace_id_str = str(workspace_id)
    async with pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id_str,
        )
        row = await conn.fetchrow(
            """
            INSERT INTO silver.claim_ledger
                (workspace_id, answer_run_id, claim_text, claim_type,
                 required_support_type, sequence_in_answer, source_passage_id)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7)
            RETURNING claim_id::text
            """,
            workspace_id_str, str(answer_run_id), claim_text,
            claim_type.value, required_support.value,
            sequence_in_answer,
            str(source_passage_id) if source_passage_id else None,
        )
    return row["claim_id"]


async def record_claims_bulk(
    pool: asyncpg.Pool,
    *,
    workspace_id: UUID | str,
    answer_run_id: UUID | str,
    claims: list[dict[str, Any]],
) -> int:
    """Bulk insert claims for an answer. Each dict: {claim_text,
    claim_type, required_support, sequence_in_answer?, source_passage_id?}.
    Returns count inserted."""
    if not claims:
        return 0
    workspace_id_str = str(workspace_id)
    rows = [
        (
            workspace_id_str, str(answer_run_id), c["claim_text"],
            c["claim_type"] if isinstance(c["claim_type"], str) else c["claim_type"].value,
            c["required_support"] if isinstance(c["required_support"], str) else c["required_support"].value,
            c.get("sequence_in_answer"),
            str(c["source_passage_id"]) if c.get("source_passage_id") else None,
        )
        for c in claims
    ]
    async with pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id_str,
        )
        await conn.executemany(
            """
            INSERT INTO silver.claim_ledger
                (workspace_id, answer_run_id, claim_text, claim_type,
                 required_support_type, sequence_in_answer, source_passage_id)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7)
            """,
            rows,
        )
    return len(rows)


async def update_verification(
    pool: asyncpg.Pool,
    *,
    workspace_id: UUID | str,
    claim_id: UUID | str,
    status: VerificationStatus,
    verifier: str,
    evidence: dict[str, Any] | None = None,
    confidence_score: float | None = None,
) -> bool:
    """Flip a pending claim to verified/failed/skipped/insufficient."""
    workspace_id_str = str(workspace_id)
    async with pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id_str,
        )
        result = await conn.execute(
            """
            UPDATE silver.claim_ledger
               SET verification_status = $1,
                   verifier = $2,
                   verifier_evidence_json = $3::jsonb,
                   confidence_score = $4,
                   updated_at = now()
             WHERE claim_id = $5::uuid
            """,
            status.value, verifier,
            json.dumps(evidence or {}),
            confidence_score, str(claim_id),
        )
    return result.startswith("UPDATE 1")


async def list_claims_for_run(
    pool: asyncpg.Pool,
    *,
    workspace_id: UUID | str,
    answer_run_id: UUID | str,
) -> list[dict[str, Any]]:
    """Read all claims for one answer_run (Trust Inspector consumer)."""
    workspace_id_str = str(workspace_id)
    async with pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id_str,
        )
        rows = await conn.fetch(
            """
            SELECT claim_id::text, claim_text, claim_type,
                   required_support_type, verification_status,
                   verifier, confidence_score, sequence_in_answer,
                   created_at
              FROM silver.claim_ledger
             WHERE answer_run_id = $1::uuid
             ORDER BY sequence_in_answer NULLS LAST, created_at
            """,
            str(answer_run_id),
        )
    return [dict(r) for r in rows]


async def summary_for_run(
    pool: asyncpg.Pool,
    *,
    workspace_id: UUID | str,
    answer_run_id: UUID | str,
) -> dict[str, Any]:
    """Aggregate stats for the Trust Inspector summary block."""
    workspace_id_str = str(workspace_id)
    async with pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id_str,
        )
        row = await conn.fetchrow(
            """
            SELECT count(*)::int AS total,
                   sum(CASE WHEN verification_status = 'verified' THEN 1 ELSE 0 END)::int AS verified,
                   sum(CASE WHEN verification_status = 'failed' THEN 1 ELSE 0 END)::int AS failed,
                   sum(CASE WHEN verification_status = 'pending' THEN 1 ELSE 0 END)::int AS pending,
                   sum(CASE WHEN verification_status = 'insufficient' THEN 1 ELSE 0 END)::int AS insufficient
              FROM silver.claim_ledger
             WHERE answer_run_id = $1::uuid
            """,
            str(answer_run_id),
        )
    return dict(row) if row else {
        "total": 0, "verified": 0, "failed": 0, "pending": 0, "insufficient": 0,
    }


__all__ = [
    "ClaimType", "Support", "VerificationStatus",
    "record_claim", "record_claims_bulk",
    "update_verification", "list_claims_for_run", "summary_for_run",
]
