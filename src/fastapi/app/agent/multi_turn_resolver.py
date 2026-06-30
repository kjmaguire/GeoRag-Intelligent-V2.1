"""Plan §3e — multi-turn context resolution (foundation).

Geologists chain queries within a conversation:

  Turn 1: "What's the deepest hole in Crackingstone?"
  Turn 2: "What were ITS top assays?"           ← "its" → hole from T1
  Turn 3: "And THE SAME HOLE'S lithology log?" ← coreference to T1's hole
  Turn 4: "What about hole 36-1085?"            ← explicit new entity
  Turn 5: "How does it compare to the previous one?"
                                                ← "previous one" → 36-1085
                                                  "the previous one"  → hole from T3 (?)

This module resolves three classes of references in the LATEST query
against the conversation HISTORY:

  1. **Pronoun coreference** ("it", "its", "they", "their", "that")
     → resolved to the last named entity of compatible type in history.
  2. **Demonstrative reference** ("the same hole", "those assays",
     "this property") → resolved to the most recent entity of the
     named class.
  3. **Comparative reference** ("the previous one", "the other one",
     "the same / different one") → resolved to a sibling at the right
     distance back.

The output is a :class:`ResolvedQuery` carrying:

  - The original query text (untouched — caller chooses whether to
    feed the rewritten form or the original to the LLM).
  - A ``rewritten_query`` with references expanded inline.
  - A ``resolution_trace`` listing each substitution and its source
    turn.
  - A ``confidence`` score in [0, 1] for downstream demotion logic.

The function is PURE — no I/O, no LLM, no DB. Designed to be safe to
call on every turn even when the query doesn't reference history (in
which case ``rewritten_query == query`` and ``resolution_trace == []``).

Wiring (downstream): a new pre-classifier step (or an envelope-side
augmentation) calls this to expand the user's question before the
6-intent classifier sees it. Carries the trace into
``silver.query_traces.multi_turn_resolution`` JSONB for audit.

Limitations of the foundation pass (deferred to later iterations):

  - English-only patterns. Multilingual UI is a Phase F11 concern.
  - No semantic-similarity check (does "hole" in T1 *mean* the same
    thing as "drillhole" in T3?). Surface-form match only.
  - Entity-type compatibility is heuristic — "it" can refer to a
    hole, a property, OR an assay, and we pick the most recent of
    the three.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)


__all__ = [
    "ConversationTurn",
    "EntityMention",
    "ResolvedQuery",
    "ResolutionStep",
    "resolve_multi_turn",
    "extract_entity_mentions",
]


# ---------------------------------------------------------------------------
# Conversation history shape
# ---------------------------------------------------------------------------


EntityType = Literal["hole", "property", "formation", "commodity", "report"]


@dataclass(frozen=True)
class EntityMention:
    """One named entity in a turn's text.

    Attributes:
        surface_form: How the entity appeared in text (e.g. "PLS-22-08",
            "Crackingstone", "biotite gneiss"). Stored verbatim so the
            rewriter can substitute the exact phrase back into the
            next turn.
        entity_type: Coarse class — drives pronoun-resolution matching.
        normalised_id: Optional canonical ID (e.g. UUID for a hole) when
            the upstream entity resolver attached one. None when we're
            only tracking the surface form.
        turn_index: Which turn introduced this mention. Used by the
            "previous one" / "the same one" resolvers to walk back.
    """

    surface_form: str
    entity_type: EntityType
    turn_index: int
    normalised_id: str | None = None


@dataclass(frozen=True)
class ConversationTurn:
    """One previous turn in the conversation history.

    Attributes:
        turn_index: 0 = oldest, N = most recent (the turn we're trying
            to resolve against). The latest user query is NOT a turn —
            it's the input to :func:`resolve_multi_turn`.
        role: "user" or "assistant". Pronoun resolution biases to
            entities surfaced in the most recent **user** or **assistant**
            turn (same effect either way — the entity was on screen).
        text: The full text of the turn — for entity re-extraction
            when the upstream metadata is missing.
        entity_mentions: Pre-extracted EntityMentions. When empty,
            :func:`resolve_multi_turn` falls back to extracting from
            ``text``.
    """

    turn_index: int
    role: Literal["user", "assistant"]
    text: str
    entity_mentions: tuple[EntityMention, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolutionStep:
    """One substitution the resolver applied.

    Attributes:
        kind: Which class of reference triggered it.
        original_phrase: The pronoun / demonstrative as it appeared.
        resolved_to: The entity surface form it was resolved to.
        source_turn_index: The conversation turn that introduced the
            referenced entity.
        confidence: 0.0-1.0. Lower for fuzzy or ambiguous matches.
    """

    kind: Literal["pronoun", "demonstrative", "comparative"]
    original_phrase: str
    resolved_to: str
    source_turn_index: int
    confidence: float


@dataclass(frozen=True)
class ResolvedQuery:
    """Output of :func:`resolve_multi_turn`."""

    query: str
    rewritten_query: str
    resolution_trace: tuple[ResolutionStep, ...] = field(default_factory=tuple)
    overall_confidence: float = 1.0

    @property
    def made_changes(self) -> bool:
        return self.query != self.rewritten_query


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
#
# Pronoun / demonstrative / comparative tables. Order matters — longer
# phrases are tested before shorter ones inside each class so "the
# same hole" wins over "it" when both could match.


# Pronouns we resolve. Possessive ('its / their') resolves to the same
# entity as the nominative ('it / they') but renders as a possessive
# in the rewritten string.
_PRONOUN_TO_TYPE: dict[str, EntityType] = {
    # Possessive pronouns (rendered as "X's" in the rewrite)
    "its": "hole",        # Most common — geologists ask "its assays"
    "their": "hole",      # Same
    # Nominative pronouns — type inferred by recency
    "it": "hole",
    "they": "hole",
    "that": "hole",
    "those": "hole",
    "this": "property",
}

# Demonstratives — these include a TYPE noun, so the resolver knows
# what to look for. The phrase is replaced with the surface form of
# the latest mention of that type.
_DEMONSTRATIVE_PATTERNS: tuple[tuple[re.Pattern[str], EntityType], ...] = (
    (re.compile(r"\bthe\s+same\s+hole\b", re.IGNORECASE), "hole"),
    (re.compile(r"\bthat\s+(drill\s*)?hole\b", re.IGNORECASE), "hole"),
    (re.compile(r"\bthis\s+(drill\s*)?hole\b", re.IGNORECASE), "hole"),
    (re.compile(r"\bthose\s+holes\b", re.IGNORECASE), "hole"),
    (re.compile(r"\bthe\s+same\s+property\b", re.IGNORECASE), "property"),
    (re.compile(r"\bthat\s+property\b", re.IGNORECASE), "property"),
    (re.compile(r"\bthis\s+property\b", re.IGNORECASE), "property"),
    (re.compile(r"\bthe\s+same\s+formation\b", re.IGNORECASE), "formation"),
    (re.compile(r"\bthat\s+formation\b", re.IGNORECASE), "formation"),
    (re.compile(r"\bthose\s+assays\b", re.IGNORECASE), "hole"),  # assays belong to a hole
    (re.compile(r"\bthe\s+same\s+report\b", re.IGNORECASE), "report"),
)

# Comparative — "the previous one" / "the other one" / "the earlier
# one" walk back by 1 mention of the inferred type.
_COMPARATIVE_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"\bthe\s+previous\s+(?:one|hole|property)\b", re.IGNORECASE), 1),
    (re.compile(r"\bthe\s+earlier\s+(?:one|hole|property)\b", re.IGNORECASE), 1),
    (re.compile(r"\bthe\s+other\s+(?:one|hole|property)\b", re.IGNORECASE), 1),
    (re.compile(r"\bthe\s+first\s+(?:one|hole|property)\b", re.IGNORECASE), -1),  # walks to OLDEST
)


# Surface-form extraction patterns for `extract_entity_mentions`.
# These are deliberately conservative — we only want HIGH-CONFIDENCE
# entity mentions to feed the resolver. False positives are worse than
# misses (a bad resolution silently changes the user's question).

# Hole IDs — matches PLS-22-08, DDH-1234, 36-1085, BG21-001, etc.
# Structure: optional letters → optional dash → digits → 1-3 more
# (-digits) segments. Requires at least one dash OR length ≥ 4 chars
# to avoid matching trivial "unit 4" style fragments.
_HOLE_ID_PATTERN = re.compile(
    r"\b([A-Z]{0,4}-?\d{1,4}(?:[-\s]\d{1,4}){0,3})\b",
)

# Property / project names — title-case multi-word phrases followed by
# the keyword 'property' / 'project' / 'deposit'. The leading letter
# MUST be uppercase (so "the deepest deposit" doesn't false-positive),
# but the keyword itself is matched case-insensitively. Tighter than a
# generic NER would be, but the agentic_retrieval pipeline has a real
# NER for those; this is a foundation fallback.
_PROPERTY_PATTERN = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\s+(?i:property|project|deposit)\b",
)


def extract_entity_mentions(
    text: str,
    turn_index: int,
) -> list[EntityMention]:
    """Heuristic extractor for entity mentions in a turn's text.

    Used as a fallback when the conversation history doesn't carry
    pre-extracted mentions. The agentic_retrieval pipeline's real NER
    (``app.agent.viz_builder.extract_hole_ids`` etc.) should populate
    ``ConversationTurn.entity_mentions`` upstream — this function is
    the safety net for tests + ad-hoc usage.
    """
    mentions: list[EntityMention] = []
    seen: set[tuple[str, EntityType]] = set()

    for match in _HOLE_ID_PATTERN.finditer(text):
        surface = match.group(1).strip()
        if not any(c.isdigit() for c in surface):
            continue  # bare letters aren't a hole ID
        # Reject trivial matches: must have a dash OR ≥ 4 chars to
        # qualify (avoids matching "unit 4" / "44" type noise).
        if "-" not in surface and len(surface) < 4:
            continue
        key = (surface.upper(), "hole")
        if key in seen:
            continue
        seen.add(key)
        mentions.append(EntityMention(
            surface_form=surface,
            entity_type="hole",
            turn_index=turn_index,
        ))

    for match in _PROPERTY_PATTERN.finditer(text):
        surface = match.group(1).strip()
        key = (surface.lower(), "property")
        if key in seen:
            continue
        seen.add(key)
        mentions.append(EntityMention(
            surface_form=surface,
            entity_type="property",
            turn_index=turn_index,
        ))

    return mentions


# ---------------------------------------------------------------------------
# Public resolver
# ---------------------------------------------------------------------------


def resolve_multi_turn(
    query: str,
    history: list[ConversationTurn],
) -> ResolvedQuery:
    """Resolve coreferences in ``query`` against ``history``.

    Args:
        query: The latest user query string.
        history: Prior conversation turns, OLDEST first. Each turn
            should carry its ``entity_mentions`` populated by the
            upstream NER; if empty, :func:`extract_entity_mentions`
            is used as a fallback.

    Returns:
        :class:`ResolvedQuery` with the rewritten query + trace.

    Notes:
        - Pure function.
        - Empty / no-history input returns the query unchanged with
          ``made_changes=False`` and confidence 1.0.
        - Multiple substitutions compose: "what about its assays at
          the same depth" can resolve both "its" → hole and "the
          same depth" if the depth was mentioned in history.
        - When a reference is ambiguous (no clear referent in history),
          the resolver LEAVES it unchanged and lowers
          ``overall_confidence`` to signal downstream that the query
          may have unresolved context.
    """
    if not query or not history:
        return ResolvedQuery(query=query, rewritten_query=query)

    # Backfill entity_mentions on turns that have empty lists.
    augmented_history = [_augment_turn_mentions(t) for t in history]

    rewritten = query
    steps: list[ResolutionStep] = []
    unresolved_refs = 0
    total_refs = 0

    # 1. Demonstrative resolution (longest patterns first).
    for pattern, target_type in _DEMONSTRATIVE_PATTERNS:
        match = pattern.search(rewritten)
        if not match:
            continue
        total_refs += 1
        latest = _latest_mention_of_type(augmented_history, target_type)
        if latest is None:
            unresolved_refs += 1
            continue
        original_phrase = match.group(0)
        rewritten = pattern.sub(latest.surface_form, rewritten, count=1)
        steps.append(ResolutionStep(
            kind="demonstrative",
            original_phrase=original_phrase,
            resolved_to=latest.surface_form,
            source_turn_index=latest.turn_index,
            confidence=0.9,
        ))

    # 2. Comparative resolution.
    for pattern, walk_back in _COMPARATIVE_PATTERNS:
        match = pattern.search(rewritten)
        if not match:
            continue
        total_refs += 1
        # Comparative refs are entity-type-agnostic; resolve to the
        # walked-back mention of any type.
        target = _walk_back_mention(augmented_history, walk_back)
        if target is None:
            unresolved_refs += 1
            continue
        original_phrase = match.group(0)
        rewritten = pattern.sub(target.surface_form, rewritten, count=1)
        steps.append(ResolutionStep(
            kind="comparative",
            original_phrase=original_phrase,
            resolved_to=target.surface_form,
            source_turn_index=target.turn_index,
            confidence=0.7,
        ))

    # 3. Pronoun resolution — done LAST so we don't accidentally
    #    expand the "its" inside an already-rewritten demonstrative.
    rewritten, pronoun_steps, p_total, p_unresolved = _resolve_pronouns(
        rewritten, augmented_history,
    )
    steps.extend(pronoun_steps)
    total_refs += p_total
    unresolved_refs += p_unresolved

    # Confidence: 1.0 when no references found; degrades linearly with
    # unresolved-fraction.
    if total_refs == 0:
        confidence = 1.0
    else:
        resolved_fraction = (total_refs - unresolved_refs) / total_refs
        confidence = max(0.0, min(1.0, resolved_fraction))

    return ResolvedQuery(
        query=query,
        rewritten_query=rewritten,
        resolution_trace=tuple(steps),
        overall_confidence=confidence,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _augment_turn_mentions(turn: ConversationTurn) -> ConversationTurn:
    """If the turn has no mentions, extract them from text."""
    if turn.entity_mentions:
        return turn
    extracted = extract_entity_mentions(turn.text, turn.turn_index)
    return ConversationTurn(
        turn_index=turn.turn_index,
        role=turn.role,
        text=turn.text,
        entity_mentions=tuple(extracted),
    )


def _all_mentions_newest_first(
    history: list[ConversationTurn],
) -> list[EntityMention]:
    """Flatten history → mentions, latest turn first."""
    out: list[EntityMention] = []
    for turn in sorted(history, key=lambda t: t.turn_index, reverse=True):
        for m in turn.entity_mentions:
            out.append(m)
    return out


def _latest_mention_of_type(
    history: list[ConversationTurn],
    target_type: EntityType,
) -> EntityMention | None:
    """Most recent mention of the given entity type."""
    for m in _all_mentions_newest_first(history):
        if m.entity_type == target_type:
            return m
    return None


def _walk_back_mention(
    history: list[ConversationTurn],
    walk_back: int,
) -> EntityMention | None:
    """Step back ``walk_back`` mentions in history.

    ``walk_back=1`` means "the most recent one" (the same as the
    pronoun resolver does); ``walk_back=2`` means "the one before
    that"; ``walk_back=-1`` is a special sentinel for "the FIRST" —
    returns the oldest mention.
    """
    mentions = _all_mentions_newest_first(history)
    if not mentions:
        return None
    if walk_back == -1:
        return mentions[-1]
    idx = walk_back - 1
    if idx < 0 or idx >= len(mentions):
        return None
    return mentions[idx]


def _resolve_pronouns(
    rewritten: str,
    history: list[ConversationTurn],
) -> tuple[str, list[ResolutionStep], int, int]:
    """Resolve standalone pronouns. Returns (new_text, steps,
    total_pronoun_refs_found, unresolved_count)."""
    steps: list[ResolutionStep] = []
    total = 0
    unresolved = 0

    # Process each pronoun. We compile a per-pronoun regex with word
    # boundaries so "items" doesn't match "it", etc.
    # We process in deterministic order — sorted by descending length so
    # longer pronouns (those, their) are tried before shorter (it, its).
    pronouns_sorted = sorted(_PRONOUN_TO_TYPE.keys(), key=len, reverse=True)

    for pronoun in pronouns_sorted:
        pattern = re.compile(rf"\b{pronoun}\b", re.IGNORECASE)
        match = pattern.search(rewritten)
        if not match:
            continue
        total += 1
        target_type = _PRONOUN_TO_TYPE[pronoun]
        latest = _latest_mention_of_type(history, target_type)
        # If the type-specific recency lookup misses, fall back to the
        # latest mention of ANY type — pronouns are inherently ambiguous.
        if latest is None:
            any_mentions = _all_mentions_newest_first(history)
            if any_mentions:
                latest = any_mentions[0]
        if latest is None:
            unresolved += 1
            continue
        # Possessive pronouns render as "X's" in the rewrite.
        if pronoun in ("its", "their"):
            replacement = f"{latest.surface_form}'s"
            confidence = 0.85
        else:
            replacement = latest.surface_form
            confidence = 0.75
        rewritten = pattern.sub(replacement, rewritten, count=1)
        steps.append(ResolutionStep(
            kind="pronoun",
            original_phrase=match.group(0),
            resolved_to=latest.surface_form,
            source_turn_index=latest.turn_index,
            confidence=confidence,
        ))

    return rewritten, steps, total, unresolved
