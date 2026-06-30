"""Claim Validator Agent (§7.4 / §15.4).

Per-section claim ledger validation against the §04i hallucination
prevention layers:

    1. Retrieval quality gate         (≥1 cited chunk per claim)
    2. Typed output validation        (claim text + numerics non-empty)
    3. Numerical claim verification   (any numeric mentioned in text
                                       must match a cited evidence
                                       row's payload)
    4. Entity resolution              (named entities present in the
                                       claim text appear in at least
                                       one cited chunk)
    5. Chunk provenance               (every cited chunk has a real
                                       ``source_chunk_id``)
    6. Geological constraint rules    (e.g. dip ∈ [0, 90], assay
                                       grades non-negative)

For each claim, returns ``validated=true|false`` + layer-by-layer
results. Unvalidated claims block the graph at the
``verify_evidence_budget`` node.

Phase H4 graduation — deterministic six-layer check.

Output contract — see module docstring.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

from app.agents import AgentContext, georag_agent

logger = logging.getLogger(__name__)


_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
# Loose proper-noun detector: a Title-cased token of 3+ chars not at
# sentence start. Good enough for the entity-resolution layer; the
# real §04i Layer 4 uses spaCy + the geological ontology. Skip
# common-noun false positives via a small stop-list.
_STOPLIST = {
    "The", "This", "That", "These", "Those", "It", "They", "Some",
    "Each", "Every", "Section", "Figure", "Table", "Total", "Average",
    "Cited", "Drilled", "Reported", "Hole", "Depth", "Strike", "Dip",
}
_PROPER_NOUN_RE = re.compile(r"\b[A-Z][a-zA-Z][a-zA-Z]+(?:-\d+)?\b")


def _extract_numbers(text: str) -> list[float]:
    out = []
    for m in _NUM_RE.finditer(text or ""):
        try:
            out.append(float(m.group()))
        except ValueError:
            continue
    return out


def _extract_proper_nouns(text: str) -> list[str]:
    out = []
    seen = set()
    for m in _PROPER_NOUN_RE.finditer(text or ""):
        tok = m.group()
        if tok in _STOPLIST or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def _numbers_appear_in_evidence(
    claim_nums: list[float], evidence_rows: list[dict[str, Any]],
    tolerance: float = 0.1,
) -> bool:
    """For each numeric in the claim, at least one cited evidence row
    must mention an equal-or-close value in its payload/raw_text."""
    if not claim_nums:
        return True  # no numerics → trivially passes
    if not evidence_rows:
        return False
    combined_haystack: list[float] = []
    for row in evidence_rows:
        combined_haystack.extend(_extract_numbers(
            str(row.get("raw_text") or row.get("payload") or "")
        ))
    if not combined_haystack:
        return False
    for cn in claim_nums:
        if not any(
            abs(cn - hn) <= tolerance
            or (cn != 0 and abs((cn - hn) / max(abs(cn), abs(hn))) <= 0.01)
            for hn in combined_haystack
        ):
            return False
    return True


def _entities_resolve(
    claim_entities: list[str], evidence_rows: list[dict[str, Any]],
) -> bool:
    """Every named entity in the claim must appear in at least one
    evidence row's text."""
    if not claim_entities:
        return True
    if not evidence_rows:
        return False
    haystack = " ".join(
        str(row.get("raw_text") or row.get("payload") or "")
        for row in evidence_rows
    ).lower()
    for ent in claim_entities:
        if ent.lower() not in haystack:
            return False
    return True


def _geological_constraints_ok(claim: dict[str, Any]) -> tuple[bool, str | None]:
    """Sanity checks against impossible geology. Returns (ok, reason)."""
    text = (claim.get("text") or "").lower()
    nums = _extract_numbers(text)

    # Heuristic dip check — "dip 95 degrees" can't be real.
    if "dip" in text:
        for n in nums:
            if 0 <= n <= 90:
                continue
            if n > 90 or n < 0:
                return False, f"dip value {n} outside [0, 90]"

    # Assay grade negative check.
    if "grade" in text or "ppm" in text or "g/t" in text:
        for n in nums:
            if n < 0:
                return False, f"negative assay grade {n}"

    return True, None


@georag_agent(
    name="Claim Validator Agent",
    risk_tier="R1",  # Read-only check; emits notes only
    version="1.0.0",  # graduated Phase H4
)
async def claim_validator(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    section_id: str,
    claim_ids: list[str],
    claims: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate every claim in a section against §04i.

    Args:
        workspace_id: RLS scope (informational).
        section_id: section under validation.
        claim_ids: claims to check.
        claims: optional claim dicts (with ``text``, ``evidence``).
            Production wires this from the report-builder state; tests
            pass claims directly.

    Returns:
        Section-level validation result. ``section_validated`` is the
        AND of every claim's six-layer result.
    """
    claims = claims or []
    claim_by_id = {c.get("claim_id"): c for c in claims}

    validations: list[dict[str, Any]] = []
    for cid in claim_ids:
        c = claim_by_id.get(cid) or {}
        text = c.get("text") or ""
        evidence = c.get("evidence") or []

        # Layer 1 — retrieval quality
        retrieval_quality = len(evidence) >= 1

        # Layer 2 — typed output
        typed_output = bool(text.strip())

        # Layer 3 — numerical claim verification
        nums = _extract_numbers(text)
        numerical_claim = _numbers_appear_in_evidence(nums, evidence)

        # Layer 4 — entity resolution
        entities = _extract_proper_nouns(text)
        entity_resolution = _entities_resolve(entities, evidence)

        # Layer 5 — chunk provenance
        chunk_provenance = all(
            bool((row.get("source_chunk_id") or "").strip())
            for row in evidence
        ) if evidence else False

        # Layer 6 — geological constraints
        constraint_ok, constraint_reason = _geological_constraints_ok(c)

        layer_results = {
            "retrieval_quality":     retrieval_quality,
            "typed_output":          typed_output,
            "numerical_claim":       numerical_claim,
            "entity_resolution":     entity_resolution,
            "chunk_provenance":      chunk_provenance,
            "geological_constraints": constraint_ok,
        }
        validated = all(layer_results.values())

        failure_notes: list[str] = []
        if not retrieval_quality: failure_notes.append("no cited evidence")
        if not typed_output:      failure_notes.append("claim text empty")
        if not numerical_claim:   failure_notes.append("numerics not present in cited evidence")
        if not entity_resolution: failure_notes.append("named entities not present in cited evidence")
        if not chunk_provenance:  failure_notes.append("missing source_chunk_id on at least one evidence row")
        if not constraint_ok:     failure_notes.append(f"geological constraint: {constraint_reason}")

        validations.append({
            "claim_id":      cid,
            "validated":     validated,
            "layer_results": layer_results,
            "notes":         "; ".join(failure_notes) or "validated",
        })

    section_validated = all(v["validated"] for v in validations) and len(validations) > 0
    summary = (
        f"section={section_id} claims={len(claim_ids)} "
        f"validated={sum(1 for v in validations if v['validated'])} "
        f"section_validated={section_validated}"
    )
    logger.info("claim_validator: %s", summary)

    return {
        "section_id":        section_id,
        "validations":       validations,
        "section_validated": section_validated,
        "summary":           summary,
    }


__all__ = ["claim_validator"]
