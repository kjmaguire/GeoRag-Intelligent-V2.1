"""§9.10 competing-hypothesis emitter — doc-phase 134.

Live orchestration for `silver.hypotheses` + `silver.hypothesis_evidence_links`
writes. The Hypothesis Workspace admin surface (doc-phase 131) reads
from these tables.

What's live in this graduation:

  - `generate_hypotheses_for_question()` — async function that:
      1. Sets the `app.workspace_id` GUC so RLS WITH CHECK accepts
         the INSERTs
      2. Calls `_synthetic_hypothesis_set()` (the stub generator)
         to produce 3 deterministic competing hypotheses (labels
         A, B, C) with template descriptions tagged by the parent
         question hash
      3. Inserts each hypothesis row into `silver.hypotheses` with
         `review_status='ai_suggested'`
      4. Distributes the candidate_evidence_chunk_ids across the
         hypotheses with synthetic roles
         (supporting/contradicting/missing/recommended_test)
      5. Emits a `hypothesis.generated` audit ledger anchor
      6. Returns a `HypothesisGenerationResult` summary

  - `_synthetic_hypothesis_set()` — deterministic stub generator.
    Real LLM-driven reasoning lands in a future graduation; the
    orchestration above won't change.

The synthetic stub is honest: every hypothesis row gets a
`description` clearly tagged with "[synthetic_stub doc-phase 134]"
so the Hypothesis Workspace can mark them clearly until the real
reasoning agent ships.

Idempotency: the function does NOT dedupe on parent_question. The
caller (Hatchet workflow / chat handler) keys on its own request id.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any, NamedTuple
from uuid import UUID

import asyncpg

from app.audit import emit_audit

log = logging.getLogger("georag.geological_reasoning.hypothesis_generator")


class EvidenceLinkDraft(NamedTuple):
    """One evidence link the stub generator wants written."""

    source_chunk_id: str | None
    role: str  # 'supporting' | 'contradicting' | 'missing' | 'recommended_test'
    weight: float | None
    payload: dict[str, Any]


class HypothesisDraft(NamedTuple):
    """One hypothesis the stub generator wants written."""

    label: str
    description: str
    confidence: float | None
    confidence_method: str | None
    rationale: str | None
    evidence_links: list[EvidenceLinkDraft]


class HypothesisGenerationResult(NamedTuple):
    """Aggregate returned by `generate_hypotheses_for_question`."""

    workspace_id: str
    parent_question: str
    hypothesis_ids: list[UUID]
    labels: list[str]
    evidence_link_count: int


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _synthetic_hypothesis_set(
    parent_question: str,
    candidate_evidence_chunk_ids: list[str],
) -> list[HypothesisDraft]:
    """Deterministic stub: produces 3 hypotheses (A, B, C) for any input.

    Labels A and B carry "competing" descriptions; label C is the
    "null hypothesis" / "insufficient evidence" option. Evidence
    chunks are distributed as:
      - First third → A as 'supporting'
      - Second third → B as 'supporting'
      - Last third → A as 'contradicting'

    Plus each hypothesis gets one 'missing' and one 'recommended_test'
    link with `source_chunk_id=None` so the role distribution
    matches what real reasoning would produce.

    Confidence values are deterministic: A=0.55, B=0.30, C=0.15.
    confidence_method='synthetic_stub'.
    """
    # Bucket chunks into thirds for A-supporting / B-supporting / A-contradicting.
    n = len(candidate_evidence_chunk_ids)
    third = max(1, n // 3) if n else 0
    a_support = candidate_evidence_chunk_ids[0:third] if n else []
    b_support = candidate_evidence_chunk_ids[third : 2 * third] if n else []
    a_contradict = candidate_evidence_chunk_ids[2 * third : n] if n else []

    q_hash = hashlib.sha256(parent_question.encode("utf-8")).hexdigest()[:8]
    tag = f"[synthetic_stub doc-phase 134 qhash={q_hash}]"

    def _supporting_links(chunks: list[str]) -> list[EvidenceLinkDraft]:
        return [
            EvidenceLinkDraft(
                source_chunk_id=c, role="supporting", weight=0.75,
                payload={"evaluator": "synthetic_stub"},
            )
            for c in chunks
        ]

    def _contradicting_links(chunks: list[str]) -> list[EvidenceLinkDraft]:
        return [
            EvidenceLinkDraft(
                source_chunk_id=c, role="contradicting", weight=0.65,
                payload={"evaluator": "synthetic_stub"},
            )
            for c in chunks
        ]

    missing_link = EvidenceLinkDraft(
        source_chunk_id=None, role="missing", weight=None,
        payload={"reason": "additional drill core data needed", "evaluator": "synthetic_stub"},
    )
    recommended_test_link = EvidenceLinkDraft(
        source_chunk_id=None, role="recommended_test", weight=None,
        payload={"test": "directional drilling at predicted intersect", "evaluator": "synthetic_stub"},
    )

    return [
        HypothesisDraft(
            label="A",
            description=f"Primary working hypothesis {tag}: the observed signal "
                        "is consistent with the dominant deposit model.",
            confidence=0.55,
            confidence_method="synthetic_stub",
            rationale="Highest weight evidence aligns with model A.",
            evidence_links=[
                *_supporting_links(a_support),
                *_contradicting_links(a_contradict),
                missing_link,
                recommended_test_link,
            ],
        ),
        HypothesisDraft(
            label="B",
            description=f"Competing alternative {tag}: a structural overprint "
                        "explains the signal without the dominant-model "
                        "assumption.",
            confidence=0.30,
            confidence_method="synthetic_stub",
            rationale="Partial structural evidence supports an alternative read.",
            evidence_links=[
                *_supporting_links(b_support),
                missing_link,
                recommended_test_link,
            ],
        ),
        HypothesisDraft(
            label="C",
            description=f"Null hypothesis {tag}: existing evidence is "
                        "insufficient to decide between A and B; additional "
                        "data acquisition is required.",
            confidence=0.15,
            confidence_method="synthetic_stub",
            rationale="High residual uncertainty.",
            evidence_links=[
                missing_link,
                recommended_test_link,
            ],
        ),
    ]


async def generate_hypotheses_for_question(
    *,
    workspace_id: UUID | str,
    parent_question: str,
    candidate_evidence_chunk_ids: list[str] | None = None,
    pool: asyncpg.Pool | None = None,
) -> HypothesisGenerationResult:
    """Generate + persist a competing-hypothesis set for `parent_question`.

    Args:
        workspace_id: Workspace RLS scope (UUID).
        parent_question: The user's question / interpretation target.
        candidate_evidence_chunk_ids: Optional list of chunk ids the
            retrieval layer surfaced. The stub generator distributes
            these across the hypotheses with synthetic roles.
        pool: Optional asyncpg pool to reuse (tests pass one in).

    Returns:
        HypothesisGenerationResult with the new hypothesis_ids +
        labels + evidence link total.
    """
    candidate_evidence_chunk_ids = candidate_evidence_chunk_ids or []
    workspace_str = (
        str(workspace_id) if isinstance(workspace_id, UUID) else workspace_id
    )

    owns_pool = pool is None
    if owns_pool:
        pool = await asyncpg.create_pool(
            _dsn(), min_size=1, max_size=2, statement_cache_size=0
        )

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # 0. Set the GUC so RLS WITH CHECK on silver.hypotheses
                #    accepts the INSERT.
                await conn.execute(
                    "SELECT set_config('app.workspace_id', $1, true)",
                    workspace_str,
                )

                drafts = _synthetic_hypothesis_set(
                    parent_question, candidate_evidence_chunk_ids
                )
                hypothesis_ids: list[UUID] = []
                evidence_count = 0

                for draft in drafts:
                    # 1. INSERT hypothesis row.
                    hid = await conn.fetchval(
                        """
                        INSERT INTO silver.hypotheses (
                            workspace_id, parent_question, label, description,
                            confidence, confidence_method, review_status,
                            rationale
                        )
                        VALUES (
                            $1::uuid, $2, $3, $4,
                            $5, $6, 'ai_suggested',
                            $7
                        )
                        RETURNING hypothesis_id
                        """,
                        workspace_str,
                        parent_question,
                        draft.label,
                        draft.description,
                        draft.confidence,
                        draft.confidence_method,
                        draft.rationale,
                    )
                    hypothesis_ids.append(hid)

                    # 2. INSERT evidence links for this hypothesis.
                    for link in draft.evidence_links:
                        await conn.execute(
                            """
                            INSERT INTO silver.hypothesis_evidence_links (
                                hypothesis_id, source_chunk_id, role,
                                weight, payload, workspace_id
                            )
                            VALUES ($1::uuid, $2, $3, $4, $5::jsonb, $6::uuid)
                            """,
                            str(hid),
                            link.source_chunk_id,
                            link.role,
                            link.weight,
                            json.dumps(link.payload, default=str, sort_keys=True),
                            str(workspace_id),
                        )
                        evidence_count += 1

                # 3. Audit anchor.
                await emit_audit(
                    conn,
                    action_type="hypothesis.generated",
                    workspace_id=workspace_str,
                    actor_kind="system",
                    target_schema="silver",
                    target_table="hypotheses",
                    payload={
                        "evaluator": "synthetic_stub",
                        "doc_phase": 134,
                        "parent_question_hash": hashlib.sha256(
                            parent_question.encode("utf-8")
                        ).hexdigest()[:16],
                        "hypothesis_count": len(hypothesis_ids),
                        "evidence_link_count": evidence_count,
                    },
                )

                log.info(
                    "hypothesis_generator.completed workspace=%s parent_question=%r "
                    "hypotheses=%d evidence_links=%d",
                    workspace_str, parent_question[:60],
                    len(hypothesis_ids), evidence_count,
                )

                return HypothesisGenerationResult(
                    workspace_id=workspace_str,
                    parent_question=parent_question,
                    hypothesis_ids=hypothesis_ids,
                    labels=[d.label for d in drafts],
                    evidence_link_count=evidence_count,
                )
    finally:
        if owns_pool and pool is not None:
            await pool.close()


__all__ = [
    "EvidenceLinkDraft",
    "HypothesisDraft",
    "HypothesisGenerationResult",
    "generate_hypotheses_for_question",
]
