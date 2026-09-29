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

  1. **Pronoun coreference** ("it", "its", "they", "their", "them")
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

from app.agent.hallucination.citation_markers import ALL_MARKER_RE
from app.agent.hole_id_patterns import (
    HOLE_CONTEXT_RE,
    HOLE_ID_RE,
    NUMERIC_HOLE_ID_RE,
)

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
    # Nominative / object pronouns
    "it": "hole",
    "they": "hole",
    "them": "hole",
}
# "that", "this" and "those" USED to be in this table as bare pronouns
# (audit AGT-1, 2026-09-29). In a geologist's question they are almost
# always a determiner ("what does this mean", "those assays") or a
# relativizer / complementizer ("assays THAT exceed 2 g/t", "possible THAT
# the mineralization continues"), and replacing them turned
#   "Which holes have assays that exceed 2 g/t U3O8?"
# into "Which holes have assays PLS-22-08 exceed 2 g/t U3O8?", which the
# intent classifier, the hole-ID pre-pass and the LLM then all saw. They
# now resolve ONLY inside the typed demonstrative patterns below ("that
# hole", "this deposit", "those assays"), where the noun makes the
# reference unambiguous.

# Secondary entity types a pronoun may resolve to when NO mention of its
# preferred type exists anywhere in history. Deliberately narrow: the
# resolver used to fall back to the latest mention of ANY type, which is
# how a pronoun with no plausible referent still got replaced.
_PRONOUN_SECONDARY_TYPES: dict[str, tuple[EntityType, ...]] = {
    "its": ("property",),
    "it": ("property",),
}

# Expletive ("dummy") it — "it is possible that", "is it likely", "it
# seems", "how long does it take". These have no referent; rewriting them
# was the second AGT-1 reproduction ("Is it possible that the
# mineralization continues" -> "Is PLS-22-08 possible PLS-22-08 the
# mineralization continues").
_EXPLETIVE_ADJECTIVES = (
    r"possible|impossible|likely|unlikely|probable|improbable|plausible|"
    r"true|false|clear|unclear|known|unknown|necessary|important|"
    r"reasonable|feasible|worth|worthwhile|fair|safe|common|typical|"
    r"normal|usual|unusual|reported|thought|believed|assumed|expected|"
    r"estimated|interpreted|inferred|suggested|recommended|required|"
    r"difficult|easy|hard|better|best|advisable|appropriate|correct|"
    r"accurate|valid|realistic|sensible|economic|economical|"
    r"uneconomic|viable|enough|sufficient|the\s+case"
)
# Matched against the text starting AT the pronoun ("it is possible ...").
_EXPLETIVE_IT_FORWARD_RES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"it(?:'s|\s+(?:is|was|isn't|wasn't|would|could|will|might|may|"
        r"should|must|can))(?:\s+(?:not|be|been|have\s+been))*"
        r"(?:\s+\w+ly)?\s+(?:" + _EXPLETIVE_ADJECTIVES + r")\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"it\s+(?:seems?|seemed|appears?|appeared|looks?\s+like|looked\s+like|"
        r"turns?\s+out|turned\s+out|follows|depends|matters|remains\s+to|"
        r"takes?|took|costs?)\b",
        re.IGNORECASE,
    ),
)
# Inverted order — "is it possible", "would it be worth", "does it matter".
# The head is matched against the text BEFORE the pronoun, the tail
# against the text starting at it.
_EXPLETIVE_IT_INVERTED_HEAD_RE = re.compile(
    r"\b(?:is|was|isn't|wasn't|would|could|will|might|may|should|does|did|"
    r"doesn't|didn't)\s+$",
    re.IGNORECASE,
)
_EXPLETIVE_IT_INVERTED_TAIL_RE = re.compile(
    r"it(?:\s+(?:not|be|been|have\s+been))*(?:\s+\w+ly)?\s+(?:"
    + _EXPLETIVE_ADJECTIVES
    + r"|matter|make\s+sense|take|cost|seem|appear)\b",
    re.IGNORECASE,
)

# resolve_node does not substitute the rewrite for the user's question
# when the overall confidence falls below this (spec §10.2 already uses
# 0.6 as the "ask the user to confirm the interpretation" line).
REWRITE_MIN_CONFIDENCE = 0.6

# Per-step confidence when a pronoun had to fall back to a secondary type.
_SECONDARY_TYPE_CONFIDENCE = 0.6

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
    (re.compile(r"\bthe\s+same\s+(?:deposit|project)\b", re.IGNORECASE), "property"),
    (re.compile(r"\bth(?:at|is)\s+(?:deposit|project)\b", re.IGNORECASE), "property"),
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

# Hole IDs. This used to carry its own pattern,
# `[A-Z]{0,4}-?\d{1,4}(?:[-\s]\d{1,4}){0,3}`, and the `{0,4}` on the letter
# prefix greedily ate the DATA / NI43 out of a citation marker while the
# marker's own dash satisfied the "must contain a dash" filter. Nothing
# stripped markers first, and this extractor runs over EVERY history turn
# including the assistant's own answers — the Laravel bridge never writes
# entity_mentions into chat_messages.metadata, so the heuristic fallback
# always fires.
#
# The result: "Average grade was 1.85 g/t Au over 12.5 m [NI43-2]." yielded
# the mention `NI43-2`, and a follow-up of "what is its depth?" was rewritten
# into "what is NI43-2's depth?". That rewritten string is what the intent
# classifier, the retrieval profile and every downstream hole-ID extractor
# then saw. The retrieval was garbage and the only surface was a small
# "Interpreted as:" chip.
#
# Now shares app.agent.hole_id_patterns with viz_builder and Layer 4, which
# requires a two-letter minimum prefix (so "Figure A-1" is not a hole) and
# gates bare numeric IDs on a hole-context word.
_HOLE_ID_PATTERN = HOLE_ID_RE

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

    # Strip citation markers before looking for anything, exactly as every
    # §04i layer already does. Without this the markers in the assistant's
    # own previous answer are the richest source of "hole IDs" in the
    # history.
    text = ALL_MARKER_RE.sub(" ", text)

    lettered_spans: list[tuple[int, int]] = []

    for match in _HOLE_ID_PATTERN.finditer(text):
        surface = match.group(1).strip()
        if not any(c.isdigit() for c in surface):
            continue  # bare letters aren't a hole ID
        lettered_spans.append(match.span(1))
        key = (surface.upper(), "hole")
        if key in seen:
            continue
        seen.add(key)
        mentions.append(EntityMention(
            surface_form=surface,
            entity_type="hole",
            turn_index=turn_index,
        ))

    # Numeric-only IDs (36-1085, the Cameco shape) only when the turn is
    # actually talking about holes — otherwise "pages 11-14" and "the
    # 20-30 m interval" become drill holes.
    if HOLE_CONTEXT_RE.search(text):
        for match in NUMERIC_HOLE_ID_RE.finditer(text):
            start, end = match.span(1)
            # "PLS-22-08" also contains "22-08". Taking both would give the
            # resolver a second, wrong candidate for the same hole — and
            # _resolve_pronouns picks the most recent mention, so which one
            # wins would come down to iteration order.
            if any(s <= start and end <= e for s, e in lettered_spans):
                continue
            surface = match.group(1).strip()
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

    # Confidence: 1.0 when no references found. Otherwise the resolved
    # fraction, capped by the weakest step actually applied — a rewrite
    # built on a 0.75 nominative-pronoun guess used to report 1.0 because
    # only UNRESOLVED references lowered the number (audit AGT-1).
    if total_refs == 0:
        confidence = 1.0
    else:
        resolved_fraction = (total_refs - unresolved_refs) / total_refs
        confidence = resolved_fraction
        if steps:
            confidence *= min(s.confidence for s in steps)
        confidence = max(0.0, min(1.0, confidence))

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


def _is_expletive_it(text: str, start: int) -> bool:
    """True when the "it" at ``text[start:]`` is a dummy subject.

    "it is possible that…", "it seems…", "is it likely…", "how long does
    it take…" — no referent, so nothing to resolve.
    """
    tail = text[start:]
    if any(p.match(tail) for p in _EXPLETIVE_IT_FORWARD_RES):
        return True
    return bool(
        _EXPLETIVE_IT_INVERTED_HEAD_RE.search(text[:start])
        and _EXPLETIVE_IT_INVERTED_TAIL_RE.match(tail)
    )


def _candidate_for_type(
    history: list[ConversationTurn],
    target_type: EntityType,
) -> tuple[EntityMention | None, bool]:
    """Latest mention of ``target_type`` and whether it is ambiguous.

    Ambiguous means the most recent turn that mentions the type at all
    mentions MORE than one distinct entity of it ("PLS-22-08 and
    PLS-22-11 both…") — a pronoun cannot say which one it means.
    """
    for turn in sorted(history, key=lambda t: t.turn_index, reverse=True):
        typed = [m for m in turn.entity_mentions if m.entity_type == target_type]
        if not typed:
            continue
        distinct = {m.surface_form.upper() for m in typed}
        return typed[0], len(distinct) > 1
    return None, False


def _resolve_pronouns(
    rewritten: str,
    history: list[ConversationTurn],
) -> tuple[str, list[ResolutionStep], int, int]:
    """Resolve standalone pronouns. Returns (new_text, steps,
    total_pronoun_refs_found, unresolved_count).

    A pronoun is left untouched (and counted as unresolved, which lowers
    the overall confidence) when:

    * it is an expletive "it" ("it is possible that…");
    * no mention of its preferred or secondary type exists in history —
      there is no any-type fallback any more;
    * the most recent turn naming that type names more than one entity.
    """
    steps: list[ResolutionStep] = []
    total = 0
    unresolved = 0

    # Process each pronoun. We compile a per-pronoun regex with word
    # boundaries so "items" doesn't match "it", etc.
    # We process in deterministic order — sorted by descending length so
    # longer pronouns (their) are tried before shorter (it, its).
    pronouns_sorted = sorted(_PRONOUN_TO_TYPE.keys(), key=len, reverse=True)

    for pronoun in pronouns_sorted:
        pattern = re.compile(rf"\b{pronoun}\b", re.IGNORECASE)
        match = None
        for candidate_match in pattern.finditer(rewritten):
            if pronoun == "it" and _is_expletive_it(
                rewritten, candidate_match.start(),
            ):
                continue
            match = candidate_match
            break
        if match is None:
            continue
        total += 1

        confidence_cap = 1.0
        latest, ambiguous = _candidate_for_type(history, _PRONOUN_TO_TYPE[pronoun])
        if latest is None:
            for secondary in _PRONOUN_SECONDARY_TYPES.get(pronoun, ()):
                latest, ambiguous = _candidate_for_type(history, secondary)
                if latest is not None:
                    confidence_cap = _SECONDARY_TYPE_CONFIDENCE
                    break
        if latest is None or ambiguous:
            unresolved += 1
            continue
        # Possessive pronouns render as "X's" in the rewrite.
        if pronoun in ("its", "their"):
            replacement = f"{latest.surface_form}'s"
            confidence = min(0.85, confidence_cap)
        else:
            replacement = latest.surface_form
            confidence = min(0.75, confidence_cap)
        rewritten = (
            rewritten[: match.start()] + replacement + rewritten[match.end():]
        )
        steps.append(ResolutionStep(
            kind="pronoun",
            original_phrase=match.group(0),
            resolved_to=latest.surface_form,
            source_turn_index=latest.turn_index,
            confidence=confidence,
        ))

    return rewritten, steps, total, unresolved
