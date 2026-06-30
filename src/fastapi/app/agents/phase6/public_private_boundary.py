"""Public/Private Boundary Agent (§6.4 / master plan §2.9 enforcement).

The single most important regulatory anchor in the system. Per §2.9,
this agent:

1. Runs at every retrieval (post-RAG, pre-answer-graph)
2. Tags every retrieved chunk with ``data_visibility: public | workspace``
3. Validates response language against the §2.9 template BEFORE the
   Answer Graph emits

Required language pattern (§2.9):
> Public records show uranium-related occurrences within 25 km of this
> project area. The private project corpus does not yet include assays
> confirming uranium mineralization on this property.

Forbidden language (§2.9):
> This project has uranium.

Phase H4 graduation — deterministic tagger + regex-driven language
validator. The validator catches the §2.9-forbidden pattern
("this project has <commodity>") whenever the supporting evidence
is all-public. LLM-driven semantic checks (catching paraphrased
violations) plug in via §6.4-ll and the §11 Answer Graph; the deterministic
floor remains as defense-in-depth.

Output contract — see module docstring.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

from app.agents import AgentContext, georag_agent

logger = logging.getLogger(__name__)


# Commodities that the §2.9 forbidden pattern guards. Extend per
# operator request; the rule shape is the same for each.
_COMMODITIES = (
    "uranium", "gold", "copper", "nickel", "lithium",
    "zinc", "silver", "cobalt", "rare earth", "rare earths",
    "platinum", "palladium", "molybdenum", "tin", "lead",
    "iron ore", "potash",
)


def _build_forbidden_pattern() -> re.Pattern[str]:
    """Match "this <project|property|claim> has <commodity>" or
    "this <project> hosts <commodity>". Case-insensitive."""
    targets = r"(?:project|property|claim|prospect|deposit)"
    commodities = "|".join(re.escape(c) for c in _COMMODITIES)
    verbs = r"(?:has|hosts|contains|carries|holds|features)"
    return re.compile(
        rf"\bthis\s+{targets}\s+{verbs}\s+(?:[a-z\- ]*?)?({commodities})\b",
        re.IGNORECASE,
    )


_FORBIDDEN_RE = _build_forbidden_pattern()


def _tag_chunk(chunk: dict[str, Any], workspace_id: str) -> dict[str, Any]:
    """Determine a chunk's data_visibility.

    Order of precedence:
      1. Caller-provided ``source_metadata.data_visibility``
      2. Caller-provided top-level ``data_visibility``
      3. ``workspace_id`` match → "workspace"
      4. Default → "public"
    """
    meta = chunk.get("source_metadata") or {}
    if "data_visibility" in meta:
        return {
            "chunk_id":        chunk.get("chunk_id"),
            "data_visibility": meta["data_visibility"],
        }
    if "data_visibility" in chunk:
        return {
            "chunk_id":        chunk.get("chunk_id"),
            "data_visibility": chunk["data_visibility"],
        }
    chunk_ws = (
        chunk.get("workspace_id")
        or (meta or {}).get("workspace_id")
    )
    if chunk_ws and str(chunk_ws) == str(workspace_id):
        return {
            "chunk_id":        chunk.get("chunk_id"),
            "data_visibility": "workspace",
        }
    return {
        "chunk_id":        chunk.get("chunk_id"),
        "data_visibility": "public",
    }


def _suggest_revision(passage: str, commodity: str) -> str:
    """Replace the §2.9-forbidden form with the §2.9-prescribed
    public-records hedge."""
    return re.sub(
        _FORBIDDEN_RE,
        f"public records show {commodity}-related occurrences within "
        f"the project area; the private project corpus does not yet "
        f"confirm {commodity} on the property",
        passage,
        count=1,
    )


@georag_agent(
    name="Public/Private Boundary Agent",
    risk_tier="R2",  # Validation rejects answer emission; one tier above R1
    version="1.0.0",  # graduated Phase H4
)
async def public_private_boundary(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    retrieved_chunks: list[dict[str, Any]],
    candidate_response_text: str,
) -> dict[str, Any]:
    """Validate retrieved chunks + candidate response against §2.9.

    Args:
        workspace_id: workspace context — used to decide which chunks
            are 'workspace' vs 'public'.
        retrieved_chunks: list of chunk dicts. At minimum carry
            ``chunk_id``; ``data_visibility`` optional (auto-tagged
            from workspace_id match if absent).
        candidate_response_text: the LLM's draft response to validate.

    Returns:
        Tagged chunks + any §2.9 language violations + emission gate.
    """
    ws = str(workspace_id)
    tagged = [_tag_chunk(c, ws) for c in retrieved_chunks]
    has_workspace_evidence = any(
        t["data_visibility"] == "workspace" for t in tagged
    )

    violations: list[dict[str, Any]] = []
    # Scan each sentence; only flag when all supporting evidence is
    # public (no workspace chunks back the claim). The agent doesn't
    # know which chunks back which sentence — that's the §11 graph's
    # responsibility — so we use the bundle-wide heuristic: if no
    # workspace evidence is present at all, any "this project has X"
    # is forbidden.
    for match in _FORBIDDEN_RE.finditer(candidate_response_text):
        if has_workspace_evidence:
            # Workspace evidence backs the assertion; not a violation
            # (the bundle includes private corroboration).
            continue
        commodity = match.group(1).lower()
        passage = match.group(0)
        violations.append({
            "passage":            passage,
            "rule":               "implies_private_mineralization_from_public_data",
            "suggested_revision": _suggest_revision(passage, commodity),
        })

    approved = len(violations) == 0
    logger.info(
        "public_private_boundary: chunks=%d workspace_evidence=%s "
        "violations=%d approved=%s",
        len(tagged), has_workspace_evidence, len(violations), approved,
    )
    return {
        "tagged_chunks":         tagged,
        "language_violations":   violations,
        "approve_for_emission":  approved,
    }


__all__ = ["public_private_boundary"]
