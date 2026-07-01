"""Conflict Resolver Agent (§7.4 / §15.4).

Detects and disclosures conflicts in the claim ledger for one section
of a report. Two failure modes the agent surfaces:

1. **Value mismatch** — two claims in the same section cite different
   evidence chunks but make conflicting numerical or named-entity
   claims (e.g., "total depth 339 m" vs "total depth 510 m" on the
   same hole_id).

2. **Freshness drift** — a claim's cited evidence is older than the
   `silver.workspaces.data_version` cut-over for the project. The
   claim may be supported by stale data the operator hasn't refreshed.

Per §29.2 item 6, unresolved conflicts must be surfaced in a
"Conflicts Disclosed" section before export. The agent emits a
structured ``conflicts`` list that the Export Compliance Agent (§7.8)
reads to enforce G06.

The agent itself does NOT mutate state — it READS the claim ledger
and PROPOSES disclosures. The Report Builder's
``conflict_resolution`` node merges the proposals into
``state.conflicts_disclosed``.

Phase H4 — graduated from doc-phase 81 skeleton.

Output contract:
    {
        "section_id":      str,
        "conflicts": [
            {
                "conflict_id":      str,
                "claim_ids":        [str],
                "kind":             "value_mismatch" | "freshness" |
                                    "missing_provenance",
                "values":           list[str] | None,
                "evidence_ids":     [str],
                "resolution":       "auto_disclose" | "manual_review" |
                                    None,
                "disclosed_in_text": str | None,
            },
            ...
        ],
        "all_resolved":   bool,
        "summary":        str,
    }
"""
from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

from app.agents import AgentContext, georag_agent

logger = logging.getLogger(__name__)


# Numeric pattern used by the value-mismatch detector. Matches
# integers and decimals (with optional units following).
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")

# Tolerance for numeric agreement (relative): two values within 1%
# of each other are NOT a conflict.
_NUMERIC_TOLERANCE_REL = 0.01

# Tolerance floor (absolute): values within 0.1 of each other are NOT
# a conflict (covers rounding-induced apparent mismatches).
_NUMERIC_TOLERANCE_ABS = 0.1


def _extract_numbers(text: str) -> list[float]:
    """Pull all numeric tokens from a claim's text."""
    out: list[float] = []
    for m in _NUM_RE.finditer(text):
        try:
            out.append(float(m.group()))
        except ValueError:
            continue
    return out


def _numbers_agree(a: float, b: float) -> bool:
    """Two numeric values agree if within absolute OR relative tolerance."""
    if abs(a - b) <= _NUMERIC_TOLERANCE_ABS:
        return True
    if a != 0 and abs((a - b) / max(abs(a), abs(b))) <= _NUMERIC_TOLERANCE_REL:
        return True
    return False


def _detect_value_mismatches(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pairwise scan claims for numeric disagreement.

    A "mismatch" requires:
      - Both claims share at least one numeric value position (same
        ordinal in their text — e.g., both have "depth N m" as the
        first number).
      - The values disagree beyond _NUMERIC_TOLERANCE.

    Lightweight heuristic — false positives are tolerable because the
    standalone agent emits proposals, not mutations. The Report
    Builder's conflict_resolution node either auto-discloses or routes
    the proposal to the geologist sign-off ceremony for review.
    """
    conflicts: list[dict[str, Any]] = []
    seen_pairs: set[tuple[str, str]] = set()

    for i, claim_a in enumerate(claims):
        nums_a = _extract_numbers(claim_a.get("text") or "")
        if not nums_a:
            continue
        for j in range(i + 1, len(claims)):
            claim_b = claims[j]
            nums_b = _extract_numbers(claim_b.get("text") or "")
            if not nums_b:
                continue
            # Only compare claims with the same number of numeric
            # tokens (otherwise we'd compare apples-to-pomegranates).
            if len(nums_a) != len(nums_b):
                continue
            disagree = [
                (na, nb) for na, nb in zip(nums_a, nums_b, strict=False)
                if not _numbers_agree(na, nb)
            ]
            if not disagree:
                continue
            pair_key = tuple(sorted([
                claim_a["claim_id"], claim_b["claim_id"],
            ]))
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            conflicts.append({
                "conflict_id": f"vm-{claim_a['claim_id']}-{claim_b['claim_id']}",
                "claim_ids":   [claim_a["claim_id"], claim_b["claim_id"]],
                "kind":        "value_mismatch",
                "values":      [
                    f"{na} vs {nb}" for na, nb in disagree
                ],
                "evidence_ids": _collect_evidence_ids(claim_a, claim_b),
                "resolution":   "auto_disclose",
                "disclosed_in_text": (
                    f"Cited evidence disagrees on numeric value(s): "
                    f"{'; '.join(f'{na} vs {nb}' for na, nb in disagree)}."
                ),
            })
    return conflicts


def _detect_freshness_drift(
    claims: list[dict[str, Any]],
    workspace_data_version: int | None,
) -> list[dict[str, Any]]:
    """Flag claims whose cited evidence pre-dates the current workspace
    data_version cut-over."""
    if workspace_data_version is None:
        return []
    conflicts: list[dict[str, Any]] = []
    for claim in claims:
        stale_evidence_ids = [
            (e.get("source_chunk_id") or "")
            for e in (claim.get("evidence") or [])
            if e.get("is_stale")
        ]
        if not stale_evidence_ids:
            continue
        conflicts.append({
            "conflict_id":  f"fr-{claim['claim_id']}",
            "claim_ids":    [claim["claim_id"]],
            "kind":         "freshness",
            "values":       None,
            "evidence_ids": stale_evidence_ids,
            "resolution":   "auto_disclose",
            "disclosed_in_text": (
                f"Claim {claim['claim_id']} cites evidence marked stale "
                f"against workspace data_version={workspace_data_version}. "
                f"Operator should refresh the underlying source before "
                f"signing off."
            ),
        })
    return conflicts


def _detect_missing_provenance(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flag claims that are validated=True but have zero evidence rows
    attached. The §04i Layer 5 chunk-provenance gate normally catches
    this — but if a claim slipped through it's a conflict because the
    §29.2 G01 gate would block the export anyway."""
    conflicts: list[dict[str, Any]] = []
    for claim in claims:
        if not claim.get("validated"):
            continue
        if claim.get("evidence"):
            continue
        conflicts.append({
            "conflict_id":  f"mp-{claim['claim_id']}",
            "claim_ids":    [claim["claim_id"]],
            "kind":         "missing_provenance",
            "values":       None,
            "evidence_ids": [],
            "resolution":   "manual_review",
            "disclosed_in_text": (
                f"Claim {claim['claim_id']} is marked validated but "
                f"carries no evidence rows. §04i Layer 5 contract "
                f"requires every validated claim to cite at least one "
                f"chunk; this needs operator review before export."
            ),
        })
    return conflicts


def _collect_evidence_ids(*claims: dict[str, Any]) -> list[str]:
    """Flatten unique evidence chunk IDs across multiple claims."""
    seen: set[str] = set()
    out: list[str] = []
    for c in claims:
        for e in c.get("evidence") or []:
            cid = e.get("source_chunk_id") or ""
            if cid and cid not in seen:
                seen.add(cid)
                out.append(cid)
    return out


@georag_agent(
    name="Conflict Resolver Agent",
    risk_tier="R1",  # Read + disclose; doesn't mutate state
    version="1.0.0",  # graduated doc-phase 186
)
async def conflict_resolver(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    section_id: str,
    claims: list[dict[str, Any]],
    workspace_data_version: int | None = None,
) -> dict[str, Any]:
    """Detect + propose disclosures for conflicts in a section's claim ledger.

    Args:
        workspace_id: RLS scope (informational; agent reads no DB).
        section_id: section under conflict review.
        claims: list of claim dicts; each carries keys
            ``claim_id`` (str), ``text`` (str), ``validated`` (bool),
            ``evidence`` (list of dicts with at minimum
            ``source_chunk_id`` and ``is_stale``).
        workspace_data_version: optional cut-over version for the
            freshness-drift detector. None disables that check.

    Returns the agent's structured output (see module docstring schema).

    Behavior — graduated doc-phase 186. The agent is pure-function over
    its inputs (no DB / network calls), so the Report Builder graph's
    conflict_resolution node can call it deterministically and idempotently.
    """
    conflicts: list[dict[str, Any]] = []

    # 1. Pairwise value-mismatch detection.
    conflicts.extend(_detect_value_mismatches(claims))

    # 2. Freshness drift detection (cited evidence flagged is_stale).
    conflicts.extend(_detect_freshness_drift(claims, workspace_data_version))

    # 3. Missing provenance (validated claim with no evidence rows).
    conflicts.extend(_detect_missing_provenance(claims))

    unresolved = [c for c in conflicts if c["resolution"] is None
                  or c["resolution"] == "manual_review"]
    all_resolved = len(unresolved) == 0

    summary_lines = [f"section={section_id} claims={len(claims)}"]
    if conflicts:
        kinds = {}
        for c in conflicts:
            kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
        summary_lines.append(
            f"conflicts={len(conflicts)} "
            + " ".join(f"{k}={v}" for k, v in sorted(kinds.items()))
            + f" all_resolved={all_resolved}"
        )
    else:
        summary_lines.append("no conflicts detected")

    logger.info(
        "conflict_resolver: %s",
        " | ".join(summary_lines),
    )

    return {
        "section_id":   section_id,
        "conflicts":    conflicts,
        "all_resolved": all_resolved,
        "summary":      " | ".join(summary_lines),
    }
