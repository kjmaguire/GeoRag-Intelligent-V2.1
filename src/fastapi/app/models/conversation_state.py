"""Track A.2 D6 — typed conversation state for the agentic retrieval orchestrator.

Per `docs/plans/track-a2-agentic-retrieval.md` D6 (locked 2026-04-29):

  Per-conversation typed state held in `chat_conversations.state_json`
  (public schema, NOT silver.* — verified against
  2026_04_16_130000_create_chat_conversations_table.php).

Updated after each turn. Anaphora resolution ("what about hole 117?"
referencing previous focus) reads from `entity_focus` to disambiguate.

NULL `state_json` means the conversation hasn't exercised the agentic
retrieval path yet. Backward compatible with historical rows.

V2 expansion (per consolidated-plan Bucket 1.4): persistent state across
sessions, branching, sharing.

Phase 2 build wiring is operator-data-gated; this model lands ahead of
the build so Phase 1+ orchestrator code can import it without a separate
schema-locking step.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from pydantic import BaseModel, Field


class ConversationState(BaseModel):
    """Typed conversation state persisted to chat_conversations.state_json.

    Shape locked in A.2 D6. Every field nullable / default-empty so the
    orchestrator can incrementally populate state across turns without
    pre-seeding everything.
    """

    schema_version: str = Field(
        default="1",
        description="Forward-compat marker; bump when adding required fields.",
    )

    last_query_class: str | None = Field(
        default=None,
        description=(
            "Last query's classifier verdict (e.g. 'document', 'spatial', "
            "'computation'). Drives anaphora resolution when the next query "
            "is ambiguous about which class continues from the previous turn."
        ),
    )

    entity_focus: list[str] = Field(
        default_factory=list,
        description=(
            "Entity IDs in current focus — populated from the last query's "
            "resolved entities. 'what about hole 117?' resolves to "
            "entity_focus[-1] + the new entity name."
        ),
    )

    spatial_focus: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Spatial scope of the previous answer. Either a bbox "
            "{'minx','miny','maxx','maxy','crs'} or a polygon centroid "
            "{'lat','lon','radius_m'}. Used by 'show me holes near here' "
            "follow-ups."
        ),
    )

    temporal_focus: tuple[date, date] | None = Field(
        default=None,
        description=(
            "Temporal scope from the previous answer (e.g. ('2024-01-01', "
            "'2025-01-01')). Used by 'and the year before that?' follow-ups."
        ),
    )

    pending_followup: str | None = Field(
        default=None,
        description=(
            "Hint emitted by the agent when it suspects the next user turn "
            "will be a follow-up — e.g. 'awaiting hole_id specification'. "
            "The classifier reads this to bias query interpretation."
        ),
    )

    last_plan_json: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Compact copy of the previous turn's silver.answer_runs.plan_json "
            "for 'what did you just look up?' replay queries. The orchestrator "
            "may truncate large plans before storing here to keep state_json "
            "row size bounded."
        ),
    )

    model_config = {
        # Strict — reject unknown fields so schema-version drift surfaces as
        # a validation error rather than silently swallowing data.
        "extra": "forbid",
    }


__all__ = ["ConversationState"]
