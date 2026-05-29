"""Asyncpg read/write helpers for chat_conversations.state_json.

Track A.2 Phase 2.A — typed conversation state persistence.

Design
------
Mirrors the answer_run_store pattern:
- Defensive try/except on every I/O call.
- Never raises to callers — failures log at WARNING and return None/False.
- None semantics on update: leaves column unchanged (does not write NULL).
  Rationale: same as update_answer_run_plan_json — avoids accidentally
  clearing agentic state when a non-agentic code path runs.

SQL
---
Both helpers target the PUBLIC schema (chat_conversations is not under
silver.*).  This was verified against
2026_04_16_130000_create_chat_conversations_table.php and confirmed in the
Phase 2 migration doc/track.

PgBouncer compatibility
-----------------------
asyncpg statement_cache_size=0 is set in the pool config (see main.py), so
prepared statements are not cached across PgBouncer transaction-mode
connections.  The ::jsonb / ::uuid cast syntax is used consistently with the
Phase 1.D pattern.
"""

from __future__ import annotations

import json
import logging
from uuid import UUID

from app.models.conversation_state import ConversationState

logger = logging.getLogger(__name__)


async def read_conversation_state(
    pool: object,
    conversation_id: UUID | str | None,
) -> ConversationState | None:
    """Read chat_conversations.state_json for the given conversation_id.

    Returns:
      - ConversationState instance on successful read of non-NULL state_json.
      - None on:
          - pool is None
          - conversation_id is None
          - row not found
          - state_json IS NULL (fresh conversation, hasn't exercised agentic
            retrieval path yet — backward compatible with historical rows)
          - DB error (logged at WARNING; returns None)
          - state_json JSONB fails ConversationState validation (logged at
            WARNING; returns None — schema-version drift surfaces here as a
            clean recovery path, not a crash)
    """
    if pool is None:
        logger.debug("read_conversation_state: pool is None — returning None")
        return None

    if conversation_id is None:
        logger.debug("read_conversation_state: conversation_id is None — returning None")
        return None

    sql = """
        SELECT state_json
        FROM chat_conversations
        WHERE conversation_id = $1::uuid
    """

    try:
        async with pool.acquire() as conn:  # type: ignore[union-attr]
            row = await conn.fetchrow(sql, str(conversation_id))

        if row is None:
            logger.debug(
                "read_conversation_state: no row for conversation_id=%s",
                conversation_id,
            )
            return None

        raw = row["state_json"]
        if raw is None:
            logger.debug(
                "read_conversation_state: state_json IS NULL for conversation_id=%s"
                " (fresh conversation)",
                conversation_id,
            )
            return None

        # asyncpg returns JSONB columns as dicts directly; accept both dict and str
        if isinstance(raw, str):
            raw = json.loads(raw)

        state = ConversationState.model_validate(raw)
        logger.debug(
            "read_conversation_state: loaded state conversation_id=%s "
            "schema_version=%s last_query_class=%s entity_focus_count=%d",
            conversation_id,
            state.schema_version,
            state.last_query_class,
            len(state.entity_focus),
        )
        return state

    except Exception:
        logger.warning(
            "read_conversation_state: failed for conversation_id=%s (non-fatal)",
            conversation_id,
            exc_info=True,
        )
        return None


async def update_conversation_state(
    pool: object,
    conversation_id: UUID | str | None,
    state: ConversationState | None,
) -> bool:
    """Update chat_conversations.state_json for the given conversation_id.

    Args:
        pool: asyncpg Pool (app.state.pg_pool).
        conversation_id: UUID of the chat_conversations row to update.
        state: ConversationState instance OR None.
              None → NO-OP; returns False without touching the DB.
              Rationale: leaves column unchanged so agentic state from prior
              turns is not accidentally cleared by a non-agentic code path
              (same semantics as Phase 1.D update_answer_run_plan_json).

    Returns:
        True on successful UPDATE affecting ≥1 row.
        False on:
          - pool is None
          - conversation_id is None
          - state is None (no-op)
          - DB error (logged at WARNING)
          - "UPDATE 0" — no row matched (logged at WARNING)

    SQL: UPDATE chat_conversations SET state_json = $1::jsonb
         WHERE conversation_id = $2::uuid
    JSONB binding: model_dump(mode='json') → json.dumps → bind as $1::jsonb
    (matches Phase 1.D pattern; works under PgBouncer transaction mode).
    """
    if pool is None or conversation_id is None or state is None:
        return False

    sql = """
        UPDATE chat_conversations
        SET state_json = $1::jsonb
        WHERE conversation_id = $2::uuid
    """

    try:
        state_dict = state.model_dump(mode="json")
        state_json_str = json.dumps(state_dict)

        async with pool.acquire() as conn:  # type: ignore[union-attr]
            result = await conn.execute(sql, state_json_str, str(conversation_id))

        # asyncpg returns "UPDATE N" on success; parse the count
        updated = isinstance(result, str) and result.startswith("UPDATE ") and result != "UPDATE 0"
        if not updated:
            logger.warning(
                "update_conversation_state: no row matched conversation_id=%s"
                " (result=%r)",
                conversation_id,
                result,
            )
            return False

        logger.debug(
            "update_conversation_state: persisted state conversation_id=%s "
            "schema_version=%s last_query_class=%s entity_focus_count=%d",
            conversation_id,
            state.schema_version,
            state.last_query_class,
            len(state.entity_focus),
        )
        return True

    except Exception:
        logger.warning(
            "update_conversation_state: UPDATE failed for conversation_id=%s (non-fatal)",
            conversation_id,
            exc_info=True,
        )
        return False


__all__ = ["read_conversation_state", "update_conversation_state"]
