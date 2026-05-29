"""Resolve a free-text project reference to a silver.projects.project_id.

ADR-0007 PR-1 — the new ``project_summary`` / ``coverage_gap`` intents need
to bind a query like "give me a breakdown for Triple R" or "what's missing
for project ATDD" to a concrete project_id BEFORE the SQL aggregate fires.

Resolution strategy (workspace-scoped):

  1. Exact-match (case-insensitive) on project_name, slug, or project_code
  2. Prefix match on slug or project_name
  3. Substring match (``ILIKE %term%``) on project_name

The resolver returns the top match plus the runner-up candidates so the
answer can clarify when the query is ambiguous. It is **async**, uses
asyncpg via the caller's pool, and ALWAYS filters by workspace_id so a
member of workspace A cannot resolve a project belonging to workspace B.

The module is intentionally small + dependency-free. No caching here —
callers already short-circuit on a project_id supplied via the JWT /
context envelope; resolution only fires when the user types a name.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import asyncpg

logger = logging.getLogger(__name__)


# Pull a candidate project token out of a query. The heuristic is
# deliberately conservative — quoted strings (``"Triple R"``), proper-noun
# bigrams ("Star Diamond"), and the explicit "project X" / "for X"
# phrasings. Tokens shorter than 3 chars are dropped so "or" / "to" don't
# pollute the candidate set.
_PROJECT_HINT_REGEX = re.compile(
    r"""
    (?:"([^"]{3,})")              # quoted phrase
    | (?:project[\s:]+([A-Za-z0-9_\- ]{3,40}))   # "project X"
    | (?:for\s+(?:the\s+)?([A-Z][A-Za-z0-9_\-]{2,30}(?:\s+[A-Z][A-Za-z0-9_\-]{2,30})?))  # "for Foo Bar"
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class ProjectCandidate:
    """One row of the resolver's ranked candidate list."""

    project_id: str
    project_name: str
    slug: str | None
    project_code: str | None
    match_kind: str  # 'exact' | 'prefix' | 'substring'


@dataclass(frozen=True)
class ProjectResolution:
    """Result of :func:`resolve_project_name`.

    Attributes:
        project_id: Top candidate's project_id, or None when nothing matched.
        top: Top candidate row (None when no match).
        ambiguous: True when more than one candidate matched at the
            same precedence tier — the answer should clarify which one
            the user meant.
        candidates: Up to 5 ranked candidates (top first). Useful for
            building a "did you mean…" clarification block.
        search_term: The token the resolver searched on.
    """

    project_id: str | None
    top: ProjectCandidate | None
    ambiguous: bool
    candidates: tuple[ProjectCandidate, ...]
    search_term: str | None


def extract_project_hint(query: str) -> str | None:
    """Pick the most plausible project-name token from a free-text query.

    Returns None when no hint is identifiable. The hint is sent to the
    resolver as the search term. The function is pure / side-effect-free.
    """
    if not query:
        return None
    match = _PROJECT_HINT_REGEX.search(query)
    if not match:
        return None
    # First non-None capture group is the candidate phrase.
    for group in match.groups():
        if group:
            cleaned = group.strip().strip(".,;:")
            return cleaned or None
    return None


async def resolve_project_name(
    pg_pool: asyncpg.Pool,
    *,
    workspace_id: str,
    search_term: str,
    limit: int = 5,
) -> ProjectResolution:
    """Look up *search_term* in ``silver.projects`` and return the best match.

    Args:
        pg_pool: Asyncpg pool. Required — synchronous psycopg is banned per
            CLAUDE.md async-only rule.
        workspace_id: Caller's workspace UUID from the JWT. Always
            included in the WHERE clause — never optional, never skipped.
        search_term: The text token to look up. Trimmed; empty strings
            short-circuit to a no-match resolution.
        limit: Maximum candidates to return (default 5). Capped at 25.

    Returns:
        :class:`ProjectResolution` — top match + up to ``limit`` candidates.
    """
    term = (search_term or "").strip()
    if not term:
        return ProjectResolution(
            project_id=None,
            top=None,
            ambiguous=False,
            candidates=(),
            search_term=None,
        )

    limit = max(1, min(int(limit), 25))

    # Three-tier search expressed as a single CTE so we get one round-trip.
    # ``match_kind`` carries the precedence so the caller can detect ties.
    sql = """
        WITH exact_hits AS (
            SELECT
                project_id::text AS project_id,
                project_name,
                slug,
                project_code,
                'exact'::text AS match_kind
            FROM silver.projects
            WHERE workspace_id = $1::uuid
              AND (
                lower(project_name) = lower($2)
                OR lower(slug) = lower($2)
                OR lower(project_code) = lower($2)
              )
        ),
        prefix_hits AS (
            SELECT
                project_id::text AS project_id,
                project_name,
                slug,
                project_code,
                'prefix'::text AS match_kind
            FROM silver.projects
            WHERE workspace_id = $1::uuid
              AND (
                lower(slug) LIKE lower($2) || '%'
                OR lower(project_name) LIKE lower($2) || '%'
              )
              AND project_id NOT IN (SELECT project_id::uuid FROM exact_hits)
        ),
        substring_hits AS (
            SELECT
                project_id::text AS project_id,
                project_name,
                slug,
                project_code,
                'substring'::text AS match_kind
            FROM silver.projects
            WHERE workspace_id = $1::uuid
              AND project_name ILIKE '%' || $2 || '%'
              AND project_id NOT IN (SELECT project_id::uuid FROM exact_hits)
              AND project_id NOT IN (SELECT project_id::uuid FROM prefix_hits)
        )
        SELECT project_id, project_name, slug, project_code, match_kind
        FROM exact_hits
        UNION ALL
        SELECT project_id, project_name, slug, project_code, match_kind
        FROM prefix_hits
        UNION ALL
        SELECT project_id, project_name, slug, project_code, match_kind
        FROM substring_hits
        LIMIT $3
    """

    try:
        async with pg_pool.acquire() as conn:
            rows = await conn.fetch(sql, workspace_id, term, limit)
    except Exception:
        logger.exception(
            "project_name_resolver: lookup failed workspace=%s term=%r",
            workspace_id,
            term[:60],
        )
        return ProjectResolution(
            project_id=None,
            top=None,
            ambiguous=False,
            candidates=(),
            search_term=term,
        )

    candidates = tuple(
        ProjectCandidate(
            project_id=row["project_id"],
            project_name=row["project_name"] or "",
            slug=row["slug"],
            project_code=row["project_code"],
            match_kind=row["match_kind"],
        )
        for row in rows
    )

    if not candidates:
        return ProjectResolution(
            project_id=None,
            top=None,
            ambiguous=False,
            candidates=(),
            search_term=term,
        )

    top = candidates[0]
    # Ambiguous when the second candidate is on the same precedence tier.
    ambiguous = len(candidates) > 1 and candidates[1].match_kind == top.match_kind

    return ProjectResolution(
        project_id=top.project_id,
        top=top,
        ambiguous=ambiguous,
        candidates=candidates,
        search_term=term,
    )


__all__ = [
    "ProjectCandidate",
    "ProjectResolution",
    "extract_project_hint",
    "resolve_project_name",
]
