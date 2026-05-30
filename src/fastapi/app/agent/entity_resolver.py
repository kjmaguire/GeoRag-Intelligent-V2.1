"""Plan §2c — entity resolver (foundation).

Looks up entity surface forms against ``silver.entity_aliases`` and
returns either the canonical name + URI (hit) or logs the miss to
``silver.alias_gaps`` for SME review (miss).

The resolver is the bridge between:

  - the user's free-text query (e.g. "Cracking-Stone Property")
  - the canonical entity name as stored on `silver.projects` /
    `silver.collars` / etc. (e.g. "Crackingstone Property")
  - the CGI vocab concept URI when one exists (e.g. earth science
    minerals SKOS)

Three resolve flavours:

  1. **exact_canonical** — alias_normalised = lower(input). Highest
     confidence; returns the canonical_name + canonical_uri from
     the matched row.

  2. **fuzzy_pgtrgm** — uses pg_trgm similarity when exact misses.
     The similarity threshold is configurable (default 0.6); ties
     resolved by descending confidence column then alphabetical
     canonical_name.

  3. **gap_log** — when neither exact nor fuzzy hits an above-
     threshold candidate, the entity_text is INSERTed into
     ``silver.alias_gaps`` with the calling tool name as
     ``detector``. Pure side-effect; no return value change.

The module is async: it expects an asyncpg pool. All queries set the
``georag.workspace_id`` GUC inside the transaction so RLS works.

Pure-function unit-testable shell:
  - :func:`resolve_entity` takes the pool, workspace_id, entity_type,
    entity_text. Returns :class:`EntityResolution`.
  - Tests mock the asyncpg connection via a small protocol.

The wire (orchestrator → resolver → context_envelope.resolved_entities)
is downstream — this is the foundation.
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal


logger = logging.getLogger(__name__)


__all__ = [
    "EntityResolution",
    "EntityKind",
    "normalise_entity_text",
    "resolve_entity",
    "log_alias_gap",
]


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


EntityKind = Literal[
    "property", "project", "company", "commodity",
    "hole_id", "formation", "document_type",
    "technical_term", "mineral", "method",
]


_VALID_ENTITY_TYPES: frozenset[str] = frozenset({
    "property", "project", "company", "commodity",
    "hole_id", "formation", "document_type",
    "technical_term", "mineral", "method",
})


@dataclass(frozen=True)
class EntityResolution:
    """One resolution outcome.

    Attributes:
        entity_text: The original surface form the caller passed in
            (UNCHANGED — case + punctuation preserved for citation
            rendering).
        canonical_name: When a hit landed, the canonical_name from
            entity_aliases. None when the resolver logged a gap.
        canonical_uri: CGI / SKOS / wikidata URI when present.
        match_kind: How the resolution happened.
            ``"exact_canonical"`` — alias_normalised hit
            ``"fuzzy_pgtrgm"``    — pg_trgm similarity hit
            ``"gap_logged"``      — no match; gap row written
        confidence: Resolver confidence in the match. Exact = 1.0;
            fuzzy = the similarity score (0.0-1.0); gap = 0.0.
        alias_id: The matched silver.entity_aliases row's PK when
            a real hit; None for gaps.
    """

    entity_text: str
    canonical_name: str | None
    canonical_uri: str | None
    match_kind: Literal["exact_canonical", "fuzzy_pgtrgm", "gap_logged"]
    confidence: float
    alias_id: str | None = None


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


# Strip punctuation + collapse whitespace; lowercase. Mirrors the
# alias_normalised column population convention.
_PUNCT_RE = re.compile(r"[^\w\s-]+")
_WS_RE = re.compile(r"\s+")


def normalise_entity_text(text: str) -> str:
    """Mirror the SQL convention for alias_normalised. Tests lock the
    invariant that ``normalise_entity_text(text) ==
    alias_normalised`` on the matching row."""
    if not text:
        return ""
    lowered = text.lower().strip()
    no_punct = _PUNCT_RE.sub(" ", lowered)
    collapsed = _WS_RE.sub(" ", no_punct).strip()
    return collapsed


# ---------------------------------------------------------------------------
# Public resolver
# ---------------------------------------------------------------------------


async def resolve_entity(
    pool: Any,
    *,
    workspace_id: str,
    entity_type: str,
    entity_text: str,
    fuzzy_threshold: float = 0.6,
    log_gap_on_miss: bool = True,
    gap_detector: str = "entity_resolver",
    gap_query_id: str | None = None,
    gap_user_id: int | None = None,
) -> EntityResolution:
    """Resolve a free-text entity to a canonical row or log a gap.

    Args:
        pool: asyncpg.Pool-like with async ``acquire()`` context manager.
        workspace_id: Tenant scope — set as ``georag.workspace_id``
            GUC inside the transaction so RLS applies.
        entity_type: One of the 10 known entity kinds. Unknown types
            raise ValueError to avoid silent typos.
        entity_text: The free-text surface form the user typed.
        fuzzy_threshold: pg_trgm similarity floor. Default 0.6.
            Below this, the resolver falls through to the gap-log
            branch even if pg_trgm returned candidates.
        log_gap_on_miss: When True (default), missing entities are
            inserted into ``silver.alias_gaps``. Set False for
            speculative lookups that shouldn't pollute the gap log.
        gap_detector: ``silver.alias_gaps.detector`` value when a gap
            is logged. Defaults to ``entity_resolver``.
        gap_query_id: ``silver.alias_gaps.query_id`` for tracing the
            originating query.
        gap_user_id: ``silver.alias_gaps.user_id`` for ownership.

    Returns:
        :class:`EntityResolution`. Always returns; never raises on
        miss (gap-log path).

    Raises:
        ValueError: ``entity_type`` not in the valid set, or
            ``workspace_id`` empty.
    """
    if not workspace_id:
        raise ValueError("workspace_id is required (sets georag.workspace_id)")
    if entity_type not in _VALID_ENTITY_TYPES:
        raise ValueError(
            f"unknown entity_type {entity_type!r}; valid: "
            f"{sorted(_VALID_ENTITY_TYPES)}"
        )

    normalised = normalise_entity_text(entity_text)
    if not normalised:
        # Empty / whitespace-only input — return a no-op gap without
        # writing.
        return EntityResolution(
            entity_text=entity_text,
            canonical_name=None,
            canonical_uri=None,
            match_kind="gap_logged",
            confidence=0.0,
            alias_id=None,
        )

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, true)",
                workspace_id,
            )

            # Exact lookup first.
            row = await conn.fetchrow(
                """
                SELECT alias_id, canonical_name, canonical_uri, confidence
                FROM silver.entity_aliases
                WHERE entity_type = $1
                  AND alias_normalised = $2
                ORDER BY confidence DESC, canonical_name ASC
                LIMIT 1
                """,
                entity_type,
                normalised,
            )
            if row is not None:
                return EntityResolution(
                    entity_text=entity_text,
                    canonical_name=row["canonical_name"],
                    canonical_uri=row["canonical_uri"],
                    match_kind="exact_canonical",
                    confidence=float(row["confidence"]),
                    alias_id=str(row["alias_id"]),
                )

            # Fuzzy lookup. Uses pg_trgm similarity — the migration
            # already created a trigram GIN index on
            # alias_normalised when pg_trgm is installed.
            row = await conn.fetchrow(
                """
                SELECT
                    alias_id,
                    canonical_name,
                    canonical_uri,
                    confidence,
                    similarity(alias_normalised, $2) AS sim
                FROM silver.entity_aliases
                WHERE entity_type = $1
                  AND similarity(alias_normalised, $2) >= $3
                ORDER BY sim DESC, confidence DESC, canonical_name ASC
                LIMIT 1
                """,
                entity_type,
                normalised,
                float(fuzzy_threshold),
            )
            if row is not None:
                return EntityResolution(
                    entity_text=entity_text,
                    canonical_name=row["canonical_name"],
                    canonical_uri=row["canonical_uri"],
                    match_kind="fuzzy_pgtrgm",
                    confidence=float(row["sim"]),
                    alias_id=str(row["alias_id"]),
                )

            # Miss → log gap (or skip per log_gap_on_miss).
            if log_gap_on_miss:
                await _insert_gap(
                    conn,
                    entity_text=entity_text,
                    entity_text_normalised=normalised,
                    entity_type_guess=entity_type,
                    detector=gap_detector,
                    query_id=gap_query_id,
                    user_id=gap_user_id,
                )

    return EntityResolution(
        entity_text=entity_text,
        canonical_name=None,
        canonical_uri=None,
        match_kind="gap_logged",
        confidence=0.0,
        alias_id=None,
    )


# ---------------------------------------------------------------------------
# Gap logger — public so callers can log gaps without a resolve call
# ---------------------------------------------------------------------------


async def log_alias_gap(
    pool: Any,
    *,
    workspace_id: str,
    entity_text: str,
    entity_type_guess: str | None = None,
    detector: str = "entity_resolver",
    query_id: str | None = None,
    user_id: int | None = None,
) -> None:
    """Insert one alias_gaps row directly. Idempotency is handled by
    the upstream review process — this function always inserts.

    Use case: the hole-id extractor finds an ID that doesn't match
    any silver.collars row — log it as a gap with detector=
    ``hole_id_extractor`` so the SME review queue picks it up.
    """
    if not workspace_id:
        raise ValueError("workspace_id is required")
    normalised = normalise_entity_text(entity_text)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, true)",
                workspace_id,
            )
            await _insert_gap(
                conn,
                entity_text=entity_text,
                entity_text_normalised=normalised,
                entity_type_guess=entity_type_guess,
                detector=detector,
                query_id=query_id,
                user_id=user_id,
            )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _insert_gap(
    conn: Any,
    *,
    entity_text: str,
    entity_text_normalised: str,
    entity_type_guess: str | None,
    detector: str,
    query_id: str | None,
    user_id: int | None,
) -> None:
    """INSERT into silver.alias_gaps within an active workspace-scoped
    transaction. Caller owns the transaction + GUC set_config."""
    try:
        await conn.execute(
            """
            INSERT INTO silver.alias_gaps (
                entity_text,
                entity_text_normalised,
                entity_type_guess,
                detector,
                query_id,
                user_id,
                workspace_id
            ) VALUES (
                $1, $2, $3, $4, $5::uuid, $6,
                current_setting('app.workspace_id', true)::uuid
            )
            """,
            entity_text,
            entity_text_normalised,
            entity_type_guess,
            detector,
            query_id,
            user_id,
        )
    except Exception:  # pragma: no cover — defensive
        # The gap log is best-effort observability; a constraint
        # violation (e.g. duplicate gap on the same query in a tight
        # window) shouldn't break the answer path.
        logger.warning(
            "entity_resolver: gap log INSERT failed for %r (non-fatal)",
            entity_text,
            exc_info=True,
        )
