"""Multi-project query decomposition (#7) — 2026-06-02.

The cross-project gap questions fail at 60% refusal rate (50-question
bench 2026-06-02 04:21Z) because retrieval is structurally asymmetric:
a single retrieval pass against *"How do recommendations differ between
Shakespeare Property and Dixie Gold Red Lake Gold Project?"* tends to
fetch chunks heavily weighted toward ONE of the two projects, leaving
the LLM with no material to synthesize the comparison.

Failure-pattern analysis from the 30 cross-project refusals:
  - Q: "...between Shakespeare and Dixie..." → retrieved only Dixie
  - Q: "...between Ikkari and Bateman..." → retrieved only Ikkari
  - Q: "...between Dixie and Bateman..." → retrieved only Bateman

Multi-query expansion (the synonym/HyDE/entity-focused fan-out) does
not solve this — its expansions are alternative *phrasings* of the
same intent, not orthogonal *scopes*. What's needed is to recognise
that the query references N≥2 distinct entities and run retrieval
**once per entity**, then merge.

This module:

  1. Detects named projects in the query via a fuzzy lookup against
     ``silver.projects.project_name``. Avoids over-firing on incidental
     capital-noun phrases by requiring the matched string to actually
     correspond to a known project in the workspace.
  2. When N≥2 matches, splits the query into N sub-queries by replacing
     the "compare A and B" framing with per-project framing:
        original:  "How do X and Y differ on permitting?"
        sub-q A:   "Permitting information for X"
        sub-q B:   "Permitting information for Y"
  3. Returns the sub-queries — the caller (`search_documents`) is
     responsible for fanning out retrieval and unioning results.

Falls back to a 1-element list `[original_query]` whenever decomposition
isn't applicable (single project mentioned / no projects matched /
parser error). Never blocks retrieval.

Status: standalone module, NOT yet wired into ``search_documents``.
Wiring is one of the morning's first decisions. Tests can call
``decompose_query`` directly with a list of project names instead of
hitting Postgres.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DecompositionResult:
    """Outcome of a multi-project decomposition attempt."""

    original_query: str
    detected_projects: tuple[str, ...]
    sub_queries: tuple[str, ...]
    applied: bool  # True iff we actually split the query

    def all_queries(self) -> list[str]:
        """[original, sub_q_A, sub_q_B, ...] — preserves the original so
        the caller can still match generic phrasings."""
        return [self.original_query, *self.sub_queries]


# Fuzzy normalisation for project-name matching:
#   - lowercase
#   - drop common suffixes/prefixes ("project", "property", "mine", "ltd")
#   - collapse whitespace
_GENERIC_SUFFIXES = {
    "project",
    "property",
    "properties",
    "mine",
    "mines",
    "ltd",
    "ltd.",
    "limited",
    "corp",
    "corp.",
    "corporation",
    "inc",
    "inc.",
    "deposit",
    "deposits",
    "claim",
    "claims",
}


def _normalise_name(name: str) -> str:
    """Lowercased, suffix-stripped, whitespace-collapsed key for fuzzy match."""
    cleaned = re.sub(r"[^\w\s-]", " ", name.lower())
    tokens = [
        t for t in cleaned.split() if t and t not in _GENERIC_SUFFIXES
    ]
    return " ".join(tokens)


def detect_projects_in_query(
    query: str,
    project_names: list[str],
) -> list[str]:
    """Return the subset of ``project_names`` that appear in ``query``.

    Matching is case-insensitive on normalised keys (suffixes stripped).
    Requires a multi-token match so single-token nicknames like "Mine"
    don't false-positive on generic mineral-property language. Skips
    matches that are pure subsets of another (longer) match to avoid
    counting "Red Lake" separately when "West Red Lake Gold Mines" is
    also matched.
    """
    if not query or not project_names:
        return []

    query_norm = " " + _normalise_name(query) + " "

    matched: list[tuple[str, str]] = []  # (original, normalised)
    for raw in project_names:
        norm = _normalise_name(raw)
        if not norm:
            continue
        # Skip overly-generic single tokens that match anything geological.
        if " " not in norm and norm in {
            "mine", "mines", "project", "property", "deposit", "ltd",
            "corp", "inc", "limited",
        }:
            continue
        # Token-boundary match (handles single-token project names like
        # "Shakespeare", "Ikkari", "Madsen" while avoiding substring hits).
        if f" {norm} " in query_norm:
            matched.append((raw, norm))

    # Drop entries that are pure prefix/suffix of a longer match.
    deduped: list[str] = []
    norms_sorted = sorted({m[1] for m in matched}, key=len, reverse=True)
    consumed: set[str] = set()
    for n in norms_sorted:
        if any(n in c and n != c for c in consumed):
            continue
        consumed.add(n)
        # Emit the first original that maps to this norm.
        for raw, raw_norm in matched:
            if raw_norm == n and raw not in deduped:
                deduped.append(raw)
                break

    return deduped


_FRAMING_PATTERNS = [
    r"^how\s+do(?:es)?\s+(?:the\s+)?",
    r"^which\s+(?:of|report|project)\s+(?:between\s+)?",
    r"^compare\s+",
    r"^between\s+",
    r"\s+differ(?:s|ence)?\s+",
    r"\s+vs\.?\s+",
    r"\s+versus\s+",
]

_PROJECT_SUFFIXES = r"(?:\s+(?:project|property|mine|mines|deposit))?"


def _drop_other_projects(
    query: str, focus_project: str, all_detected: list[str]
) -> str:
    """Remove every detected project EXCEPT the focus one, plus generic suffixes.

    Result: query becomes "<topic> ... <focus_project> ... <topic>" with the
    other project names redacted. Comparison verbs cleaned up after.
    """
    out = query
    for p in all_detected:
        if p == focus_project:
            continue
        # Match "Shakespeare Property" / "Shakespeare" / "Shakespeare's"
        pattern = (
            r"\b" + re.escape(p) + r"(?:'s)?"
            + _PROJECT_SUFFIXES + r"\b"
        )
        out = re.sub(pattern, " ", out, flags=re.IGNORECASE)
    return out


def _strip_compare_framing(query: str) -> str:
    """Strip A-vs-B framing tokens. Conservative — returns original on no-match."""
    stripped = query
    for pat in _FRAMING_PATTERNS:
        stripped = re.sub(pat, " ", stripped, flags=re.IGNORECASE)
    # Also drop dangling "and X" / "or X" residue from the redaction step.
    stripped = re.sub(r"\s+(?:and|or)\s+(?:\s|$)", " ", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"\s+", " ", stripped).strip()
    return stripped or query


def build_per_project_sub_query(
    original_query: str,
    focus_project: str,
    all_detected: list[str] | None = None,
) -> str:
    """Render a single-project sub-query focused on the named project.

    Strategy:
      1. Redact every OTHER detected project from the query.
      2. Strip comparison-framing verbs ("differ", "vs", "between", "compare").
      3. If focus_project isn't already mentioned, append "at <focus_project>".
    """
    all_detected = all_detected or [focus_project]
    redacted = _drop_other_projects(original_query, focus_project, all_detected)
    core = _strip_compare_framing(redacted).rstrip("?").strip()
    if not core:
        return f"information about {focus_project}"
    # Always ensure the focus project name appears in the sub-query.
    if focus_project.lower() not in core.lower():
        return f"{core} at {focus_project}"
    return core


# Comparative framing markers. Decomposition only fires when the query
# has at least one of these *plus* 2+ detected projects. Prevents
# regression on single-intent queries that incidentally mention
# multiple property names (e.g. "Pure Gold operated the mine from
# 2014 to 2023 before WRLG for Madsen Mine" — about Madsen timeline,
# not a comparison; decomposing would split the intent).
_COMPARATIVE_MARKERS = (
    " compare ", " comparison ", " comparable ",
    " differ ", " differs ", " differing ", " difference ", " differences ",
    " versus ", " vs ", " vs. ",
    " which of ",
    " between ",
    " more advanced ", " more detailed ", " more information ",
    " more dependent ", " more conservative ", " more credible ",
    " how do they ", " how does it ",
    " benchmark ",
    " contrast ",
    " stronger ", " weaker ",
)


def _looks_comparative(query: str) -> bool:
    """Heuristic: does the query frame two entities for comparison?

    Adds whitespace boundaries to avoid substring false-positives
    (e.g. "difference" inside "indifference").
    """
    lower = " " + query.lower().strip() + " "
    return any(marker in lower for marker in _COMPARATIVE_MARKERS)


def decompose_query(
    query: str,
    project_names: list[str],
    *,
    min_projects: int = 2,
    require_comparative_framing: bool = True,
) -> DecompositionResult:
    """Detect named projects in the query and split into per-project sub-queries.

    Args:
        query: The user's raw natural-language query.
        project_names: All ``silver.projects.project_name`` values for the
            current workspace. Caller (typically ``search_documents``) is
            expected to cache this list per-workspace.
        min_projects: Threshold below which decomposition is a no-op.
            Defaults to 2 (the comparison case). Set higher to require
            triple-project queries before decomposing.
        require_comparative_framing: When True (default), additionally
            require a comparative marker ("compare", "differ", "vs",
            "which of", etc.) in the query before decomposing. Prevents
            regression on single-intent queries that mention multiple
            property names incidentally. Set False on test fixtures
            that don't include framing language.

    Returns:
        DecompositionResult. When ``applied=False`` the caller should
        retrieve normally; when ``applied=True`` the caller should fan
        out retrieval across ``sub_queries`` and union the results.
    """
    matched = detect_projects_in_query(query, project_names)
    if len(matched) < min_projects:
        return DecompositionResult(
            original_query=query,
            detected_projects=tuple(matched),
            sub_queries=(),
            applied=False,
        )

    if require_comparative_framing and not _looks_comparative(query):
        return DecompositionResult(
            original_query=query,
            detected_projects=tuple(matched),
            sub_queries=(),
            applied=False,
        )

    sub_queries = tuple(
        build_per_project_sub_query(query, name, matched) for name in matched
    )

    logger.info(
        "multi_project_decomposition: split query into %d sub-queries "
        "(projects=%s)",
        len(sub_queries),
        matched,
    )

    return DecompositionResult(
        original_query=query,
        detected_projects=tuple(matched),
        sub_queries=sub_queries,
        applied=True,
    )


# 2026-06-02 — Workspace-specific property nicknames. Some workspace
# "projects" in silver.projects are parent-company aggregates that own
# multiple PROPERTIES (e.g. WRLG owns PureGold, Rowan, Madsen, Dixie).
# The user's questions refer to the properties by name even though they
# share a single project_id. Decomposition needs to know these aliases
# so that "compare Dixie vs PureGold" splits into per-property sub-
# queries even though both nicknames resolve to the same DB project.
#
# Long-term fix: a silver.project_aliases table populated during
# ingestion (one row per property nickname → project_id). For now, this
# is hardcoded for Default Workspace. Maintenance burden is low because
# new properties only get added when a new NI 43-101 is ingested.
_KNOWN_PROPERTY_NICKNAMES: list[str] = [
    # WRLG (West Red Lake Gold Mines) properties
    "Dixie",
    "Dixie Gold",
    "Red Lake Gold",
    "PureGold",
    "Pure Gold",
    "Rowan",
    # Battle North properties / synonyms for Bateman
    "Bateman",
    "F2 Gold",
    # Shakespeare aliases (BTU Capital subsidiary)
    "Shakespeare Gold",
]


async def load_workspace_project_names(
    pg_pool: Any,
    workspace_id: str | None,
) -> list[str]:
    """Read project names + known property nicknames for the workspace.

    Combines two sources:
      1. ``silver.projects.project_name`` for the workspace
      2. The ``_KNOWN_PROPERTY_NICKNAMES`` hardcoded list for parent-
         company properties that share a project_id (WRLG → PureGold /
         Rowan / Dixie / etc., Battle North → Bateman / F2 Gold).

    Falls back to nicknames-only on any DB error — never raises. The
    caller (search_documents) treats empty as "skip decomposition."
    """
    db_names: list[str] = []
    if pg_pool is not None:
        try:
            async with pg_pool.acquire() as conn:
                if workspace_id:
                    rows = await conn.fetch(
                        "SELECT project_name FROM silver.projects "
                        "WHERE workspace_id = $1::uuid",
                        workspace_id,
                    )
                else:
                    rows = await conn.fetch(
                        "SELECT project_name FROM silver.projects"
                    )
            db_names = [
                r["project_name"] for r in rows if r.get("project_name")
            ]
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "multi_project_decomposition: project list lookup failed: %s",
                exc,
            )

    # Merge with hardcoded nicknames, preserving order + uniqueness.
    seen: set[str] = set()
    merged: list[str] = []
    for n in db_names + _KNOWN_PROPERTY_NICKNAMES:
        if n not in seen:
            seen.add(n)
            merged.append(n)
    return merged
