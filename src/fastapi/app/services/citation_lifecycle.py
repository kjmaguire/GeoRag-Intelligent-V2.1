"""Citation lifecycle state machine helper.

Module 6 Phase B Chunk 1 — §09b lifecycle wiring.

This module owns the transition_lifecycle() function that advances an
answer_run row through the citation lifecycle state machine:

    draft → generated → validated → committed
                   ↘                    ↗
                   rejected  (from any non-terminal state on guard failure)

Terminal states: committed, rejected.

Usage
-----
The orchestrator calls transition_lifecycle() at each phase boundary:

    1. Before retrieval (query entry): 'draft'
    2. After LLM stream completes: 'generated'
    3. After all guards pass: 'validated'   (or 'rejected' if a blocking guard fails)
    4. After all persistence writes: 'committed'

All calls are fire-and-forget from the orchestrator's perspective: exceptions
are caught and logged, never raised. This preserves the invariant that
observability writes never fail a user query.

The transition is idempotent — writing the same state twice is safe and
produces a no-op UPDATE. The DB does not enforce transition ordering at the
row level (no CHECK constraint on prior state); the orchestrator is responsible
for calling transitions in the correct sequence.

Design note
-----------
No state transition table or FSM library is used. The orchestrator calls
transition_lifecycle() with the target state in explicit sequence. A future
chunk may add a DB-level guard (e.g. a PG trigger that rejects illegal
transitions), but for Chunk 1 the application layer is the sole enforcer.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal
from uuid import UUID

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

# Valid lifecycle states — mirrors the DB CHECK constraint on
# silver.answer_runs.citation_lifecycle_state.
CitationLifecycleStateLiteral = Literal[
    "draft",
    "generated",
    "validated",
    "committed",
    "rejected",
]

# Terminal states — once reached, no further transitions are valid.
_TERMINAL_STATES: frozenset[str] = frozenset({"committed", "rejected"})


async def transition_lifecycle(
    pool: asyncpg.Pool,
    answer_run_id: UUID,
    new_state: CitationLifecycleStateLiteral,
    rejection_reason: str | None = None,
) -> None:
    """Advance an answer_run row to new_state.

    Args:
        pool:             asyncpg Pool (app.state.pg_pool).
        answer_run_id:    UUID of the answer_run row to update.
        new_state:        Target lifecycle state.
        rejection_reason: Optional human-readable failure reason.  Only
                          meaningful when new_state='rejected'; ignored
                          for other transitions (no column to store it on
                          answer_runs yet — Chunk 3 may add one).

    Behaviour:
        - Idempotent: writing the same state twice is a no-op UPDATE.
        - Failures are logged at WARNING level and never raised.
        - Pool=None guard: skips the UPDATE silently (tests that don't
          wire a real pool still work).

    Rejection reason note:
        answer_runs.rejection_reason column added in Chunk 3
        (migration 2026_04_22_100000).  When new_state='rejected' and
        rejection_reason is provided, this function writes it to the column.
        answer_citation_items.rejection_reason is the per-citation-marker
        rejection field (populated by the span resolver on marker-level failures).
    """
    if pool is None:
        logger.debug(
            "transition_lifecycle: pg_pool is None — skipping UPDATE "
            "(answer_run_id=%s new_state=%s)",
            answer_run_id,
            new_state,
        )
        return

    try:
        async with pool.acquire() as conn:  # type: ignore[union-attr]
            if rejection_reason and new_state == "rejected":
                # Chunk 3: write rejection_reason to the new column
                # (migration 2026_04_22_100000 added this column).
                await conn.execute(
                    """
                    UPDATE silver.answer_runs
                       SET citation_lifecycle_state = $1,
                           rejection_reason         = $2,
                           updated_at               = NOW()
                     WHERE answer_run_id = $3
                    """,
                    new_state,
                    rejection_reason,
                    str(answer_run_id),
                )
                logger.warning(
                    "transition_lifecycle: answer_run_id=%s REJECTED — %s",
                    answer_run_id,
                    rejection_reason,
                )
            else:
                await conn.execute(
                    """
                    UPDATE silver.answer_runs
                       SET citation_lifecycle_state = $1,
                           updated_at               = NOW()
                     WHERE answer_run_id = $2
                    """,
                    new_state,
                    str(answer_run_id),
                )
                logger.debug(
                    "transition_lifecycle: answer_run_id=%s → %s",
                    answer_run_id,
                    new_state,
                )

    except Exception:
        logger.warning(
            "transition_lifecycle: UPDATE failed (non-fatal — "
            "answer_run_id=%s new_state=%s)",
            answer_run_id,
            new_state,
            exc_info=True,
        )


async def transition_to_draft(
    pool: asyncpg.Pool,
    answer_run_id: UUID,
) -> None:
    """Convenience wrapper: advance to 'draft' (query entry)."""
    await transition_lifecycle(pool, answer_run_id, "draft")


async def transition_to_generated(
    pool: asyncpg.Pool,
    answer_run_id: UUID,
) -> None:
    """Convenience wrapper: advance to 'generated' (LLM stream complete)."""
    await transition_lifecycle(pool, answer_run_id, "generated")


async def transition_to_validated(
    pool: asyncpg.Pool,
    answer_run_id: UUID,
) -> None:
    """Convenience wrapper: advance to 'validated' (guards passed)."""
    await transition_lifecycle(pool, answer_run_id, "validated")


async def transition_to_committed(
    pool: asyncpg.Pool,
    answer_run_id: UUID,
) -> None:
    """Convenience wrapper: advance to 'committed' (all persistence complete)."""
    await transition_lifecycle(pool, answer_run_id, "committed")


async def transition_to_rejected(
    pool: asyncpg.Pool,
    answer_run_id: UUID,
    reason: str | None = None,
) -> None:
    """Convenience wrapper: advance to 'rejected' (guard failure or LLM error)."""
    await transition_lifecycle(pool, answer_run_id, "rejected", rejection_reason=reason)
