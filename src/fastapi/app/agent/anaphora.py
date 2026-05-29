"""Anaphora resolution for the agentic retrieval orchestrator.

Track A.2 Phase 2.B — pure function, no I/O, no async, no DB.

Purpose
-------
Detects anaphoric references in the user's query (pronouns, partial entity
names, temporal references, spatial references) and rewrites the query with
explicit context tokens drawn from ConversationState.  The rewritten query is
intentionally verbose — it prepends disambiguated context so the downstream
classifier and retrieval stores see fully resolved text.

Example transformations
-----------------------
  "what about it?"
    + entity_focus=["MS-117"]
  -> "MS-117: what about it?"   (Pattern 1 — bare pronoun)

  "what about 117?"
    + entity_focus=["MS-117"]
  -> "MS-117: what about 117?"  (Pattern 2 — suffix-match partial entity ref)

  "year before that"
    + temporal_focus=(date(2024,1,1), date(2025,1,1))
  -> "(in 2024-01-01..2025-01-01) year before that"  (Pattern 3)

  "near here"
    + spatial_focus={'lat': 56.5, 'lon': -108.5, 'radius_m': 500}
  -> "(within 500m of 56.5,-108.5) near here"  (Pattern 4)

Pattern check order: relative (2) > pronoun (1) > temporal (3) > spatial (4).
Most-specific first ensures "what about it near here?" gets both the entity
prefix AND the spatial prefix applied in a single pass.

Multiple patterns can fire sequentially on the already-rewritten query — each
works on the output of the previous step.

False-positive guard
--------------------
Pattern 1 (pronoun) only triggers when entity_focus is non-empty.
Pattern 2 (relative) requires an explicit trigger phrase prefix
  ("what about", "and", "tell me about", "how about") — bare noun phrases
  like "the resource at hole 117" do NOT match.
Pattern 3 (temporal) only triggers when temporal_focus is populated.
Pattern 4 (spatial) only triggers when spatial_focus is populated.

This avoids false positives on geological queries that contain words like
"that", "those", "near" in non-anaphoric usage:
  "is the resource at hole 117 high?" — none of the 4 patterns fire.
  "what formations are similar to those at hole MS-117?" — Pattern 1 fires
    but only when entity_focus is non-empty AND "those" is a standalone
    pronoun (word-boundary anchored).

No new dependencies — stdlib re only.
"""

from __future__ import annotations

import re

from app.models.conversation_state import ConversationState

# ---------------------------------------------------------------------------
# Compiled module-level regexes (no per-call compilation)
# ---------------------------------------------------------------------------

# Pattern 1 — bare pronouns standing alone as the anaphoric reference.
# Word-boundary anchored. Case-insensitive.
_PRONOUN_PATTERN: re.Pattern[str] = re.compile(
    r"\b(it|that|this|those|these|them)\b",
    re.IGNORECASE,
)

# Pattern 2 — "what about X", "and X", "tell me about X", "how about X"
# Captures X as group 2 (the partial entity reference).
# Non-greedy so it stops at ? or end-of-string.
_RELATIVE_PATTERN: re.Pattern[str] = re.compile(
    r"\b(what about|and|tell me about|how about)\s+([\w][\w\s\-]*?)(?:\s*\?|$)",
    re.IGNORECASE,
)

# Pattern 3 — temporal anaphora
_TEMPORAL_PATTERN: re.Pattern[str] = re.compile(
    r"\b(year before(?: that)?|earlier than(?: that)?|after that(?: one)?|before that(?: one)?)\b",
    re.IGNORECASE,
)

# Pattern 4 — spatial anaphora
_SPATIAL_PATTERN: re.Pattern[str] = re.compile(
    r"\b(near here|in this area|around there|nearby)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_anaphora(
    query: str,
    state: ConversationState | None,
) -> tuple[str, list[str], bool]:
    """Rewrite an anaphoric query using prior conversation state.

    Pure function — no I/O, no async, no side effects.

    Detects 4 anaphora patterns and uses ConversationState to disambiguate:

      Pattern 1 — bare pronouns: "what about it?", "and that?", "those too?"
                  -> look up state.entity_focus[-1] (most recent) and prepend
                     "{entity_name}: " to the query.

      Pattern 2 — "what about X" / "and X" / "tell me about X" / "how about X"
                  where X is a partial entity reference matching one of
                  state.entity_focus by suffix or prefix.  Substitute the
                  full canonical entity name if needed.

      Pattern 3 — temporal anaphora: "the year before that", "earlier than
                  that", "after that one" -> look up state.temporal_focus
                  and prepend the date range as "(in start..end) " context.

      Pattern 4 — spatial anaphora: "near here", "in this area", "around
                  there" -> look up state.spatial_focus + prepend the
                  centroid as "(within {radius_m}m of {lat},{lon}) " context.

    Pattern check order: relative (2) > pronoun (1) > temporal (3) > spatial (4).
    Multiple patterns fire in sequence on the already-rewritten query.

    Args:
        query: The raw user query string.
        state: ConversationState from the current conversation, or None.

    Returns:
        (rewritten_query, resolved_entity_ids, was_rewritten)

        rewritten_query   — query with explicit context prepended/substituted.
        resolved_entity_ids — entity_focus values that participated in the
                              rewrite (empty if no entity rewrite occurred).
        was_rewritten     — True if any transformation was applied.

    When state is None OR no anaphora pattern matches, returns the original
    query unchanged with empty resolved list and was_rewritten=False.
    """
    if state is None:
        return query, [], False

    rewritten = query
    resolved_entity_ids: list[str] = []
    was_rewritten = False

    # -----------------------------------------------------------------------
    # Pattern 2 — relative ("what about X") — most specific, checked first
    # -----------------------------------------------------------------------
    rewritten, resolved_entity_ids, was_rewritten = _apply_relative_pattern(
        rewritten, state, resolved_entity_ids, was_rewritten
    )

    # -----------------------------------------------------------------------
    # Pattern 1 — bare pronouns (only when entity_focus is non-empty)
    # -----------------------------------------------------------------------
    rewritten, resolved_entity_ids, was_rewritten = _apply_pronoun_pattern(
        rewritten, state, resolved_entity_ids, was_rewritten
    )

    # -----------------------------------------------------------------------
    # Pattern 3 — temporal anaphora (only when temporal_focus is populated)
    # -----------------------------------------------------------------------
    rewritten, was_rewritten = _apply_temporal_pattern(rewritten, state, was_rewritten)

    # -----------------------------------------------------------------------
    # Pattern 4 — spatial anaphora (only when spatial_focus is populated)
    # -----------------------------------------------------------------------
    rewritten, was_rewritten = _apply_spatial_pattern(rewritten, state, was_rewritten)

    return rewritten, resolved_entity_ids, was_rewritten


# ---------------------------------------------------------------------------
# Private helpers — one per pattern
# ---------------------------------------------------------------------------


def _apply_relative_pattern(
    query: str,
    state: ConversationState,
    resolved_ids: list[str],
    was_rewritten: bool,
) -> tuple[str, list[str], bool]:
    """Apply Pattern 2 — resolve partial entity references following trigger phrases.

    Matches "what about X", "and X", "tell me about X", "how about X" where
    X is a partial entity reference that matches one of state.entity_focus by
    case-insensitive suffix OR prefix.

    If X already fully matches an entity in entity_focus (exact match after
    case normalisation), no substitution is needed — we do not flag
    was_rewritten in that case (user typed the canonical name).

    If X is a partial match, we prepend "{canonical_entity}: " to the query
    so the downstream retrieval sees the full canonical name.
    """
    if not state.entity_focus:
        return query, resolved_ids, was_rewritten

    match = _RELATIVE_PATTERN.search(query)
    if not match:
        return query, resolved_ids, was_rewritten

    x_capture = match.group(2).strip()
    x_lower = x_capture.lower()

    # Check for exact match first — no substitution needed
    for entity in state.entity_focus:
        if entity.lower() == x_lower:
            # Already canonical; no rewrite needed
            return query, resolved_ids, was_rewritten

    # Check for suffix or prefix match (partial entity reference)
    for entity in state.entity_focus:
        entity_lower = entity.lower()
        if entity_lower.endswith(x_lower) or entity_lower.startswith(x_lower):
            rewritten = f"{entity}: {query}"
            new_ids = list(resolved_ids)
            if entity not in new_ids:
                new_ids.append(entity)
            return rewritten, new_ids, True

    return query, resolved_ids, was_rewritten


def _apply_pronoun_pattern(
    query: str,
    state: ConversationState,
    resolved_ids: list[str],
    was_rewritten: bool,
) -> tuple[str, list[str], bool]:
    """Apply Pattern 1 — bare pronoun anaphora resolution.

    Only triggers when entity_focus is non-empty AND the query contains a
    bare pronoun (word-boundary anchored).  Uses entity_focus[-1] (most
    recently focused entity).

    We only apply this when Pattern 2 has NOT already fired (i.e. we don't
    double-prepend entity context).  If was_rewritten is already True from
    Pattern 2, we skip Pattern 1 to avoid duplicate entity prefix.
    """
    if not state.entity_focus:
        return query, resolved_ids, was_rewritten

    if not _PRONOUN_PATTERN.search(query):
        return query, resolved_ids, was_rewritten

    # If Pattern 2 already resolved an entity, avoid double-prefix
    if was_rewritten and resolved_ids:
        return query, resolved_ids, was_rewritten

    entity = state.entity_focus[-1]
    rewritten = f"{entity}: {query}"
    new_ids = list(resolved_ids)
    if entity not in new_ids:
        new_ids.append(entity)
    return rewritten, new_ids, True


def _apply_temporal_pattern(
    query: str,
    state: ConversationState,
    was_rewritten: bool,
) -> tuple[str, bool]:
    """Apply Pattern 3 — temporal anaphora resolution.

    Only triggers when temporal_focus is non-None AND the query contains a
    temporal anaphora phrase.  Prepends "(in start..end) " to the query.
    """
    if state.temporal_focus is None:
        return query, was_rewritten

    if not _TEMPORAL_PATTERN.search(query):
        return query, was_rewritten

    start_date, end_date = state.temporal_focus
    date_range = f"{start_date}..{end_date}"
    rewritten = f"(in {date_range}) {query}"
    return rewritten, True


def _apply_spatial_pattern(
    query: str,
    state: ConversationState,
    was_rewritten: bool,
) -> tuple[str, bool]:
    """Apply Pattern 4 — spatial anaphora resolution.

    Only triggers when spatial_focus is non-None AND the query contains a
    spatial anaphora phrase.  Extracts lat/lon/radius_m from spatial_focus
    (centroid shape: {'lat', 'lon', 'radius_m'}) and prepends
    "(within {radius_m}m of {lat},{lon}) " to the query.

    If spatial_focus uses the bbox shape ({'minx','miny','maxx','maxy'})
    rather than the centroid shape, we compute the centroid inline.
    radius_m defaults to 0 for bbox-shaped focus (caller should set it
    explicitly when constructing spatial_focus).
    """
    if state.spatial_focus is None:
        return query, was_rewritten

    if not _SPATIAL_PATTERN.search(query):
        return query, was_rewritten

    focus = state.spatial_focus

    # Centroid shape
    if "lat" in focus and "lon" in focus:
        lat = focus["lat"]
        lon = focus["lon"]
        radius_m = focus.get("radius_m", 0)
    # Bbox shape — compute centroid
    elif "minx" in focus and "miny" in focus and "maxx" in focus and "maxy" in focus:
        lat = (focus["miny"] + focus["maxy"]) / 2.0
        lon = (focus["minx"] + focus["maxx"]) / 2.0
        radius_m = focus.get("radius_m", 0)
    else:
        # Unrecognised shape — skip to avoid garbling the query
        return query, was_rewritten

    rewritten = f"(within {radius_m}m of {lat},{lon}) {query}"
    return rewritten, True


__all__ = ["resolve_anaphora"]
