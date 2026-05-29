"""Parse OIUR markdown LLM output into a ``GeoAnswer`` — Phase 1 / Step 1.2.

Companion to :mod:`app.agent.prompts.oiur_section`. The grammar lives in that
module's docstring; this parser implements it. Change both together.

Design tenets:
  - **Best-effort, never raise to the caller.** Returns ``(geo_answer, warnings)``.
    A None ``geo_answer`` means parsing failed badly enough that the assembler
    should fall back to the flat-text path.
  - **Refusal contract.** If the Observations section is missing entirely or
    has zero parseable observations, return ``(None, [...])`` with a marker
    warning so the caller can route to the refusal payload.
  - **Tolerant of minor LLM drift.** Extra whitespace, smart quotes, missing
    trailing periods, and "Not applicable" on a single line are all accepted.
"""

from __future__ import annotations

import logging
import re
from typing import Final

from app.agent.schemas import (
    ConfidenceBlock,
    ConfidenceLevel,
    DecisionSupport,
    GeoAnswer,
    Interpretation,
    Observation,
    RecommendedAction,
    SectionEmpty,
    UncertaintyBlock,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Section splitting
# ---------------------------------------------------------------------------

# H2 headers we recognise. Case-insensitive at parse time. Order is the
# canonical OIUR order — interpretations BEFORE uncertainty, etc.
_SECTION_KEYS: Final[tuple[str, ...]] = (
    "observations",
    "interpretations",
    "uncertainty",
    "recommended actions",
)

_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_OBS_LINE_RE = re.compile(r"^\(O(\d+)\)\s+(.+)$")
_INTERP_LINE_RE = re.compile(r"^\(I(\d+)\)\s+(.+)$")
_SUPPORTS_RE = re.compile(r"^supports:\s*([^.]+)\.\s*(.*)$", re.IGNORECASE)
_COMPETES_RE = re.compile(r"^competes-with:\s*([^.]+)\.\s*(.*)$", re.IGNORECASE)
_CITATION_MARKER_RE = re.compile(r"\[(?:NI43|DATA|PUB|PGEO)[:\-]\d+\]")
_CONFIDENCE_RE = re.compile(
    r"\*\*\s*Confidence\s*:\s*(High|Medium|Low)\s*\*\*", re.IGNORECASE
)
_REASON_RE = re.compile(r"^Reason\s*:\s*(.+)$", re.IGNORECASE)
_DATA_TO_REDUCE_RE = re.compile(
    r"^Data to reduce uncertainty\s*:\s*(.+)$", re.IGNORECASE
)
_NOT_APPLICABLE_RE = re.compile(r"^_+\s*Not applicable\s*:\s*(.+?)\s*_+$", re.IGNORECASE)
_NUMBERED_ITEM_RE = re.compile(r"^(\d+)\.\s+(.+)$")
_H3_RE = re.compile(r"^###\s+(.+?)\s*$")
_RANKING_DEFERRED_RE = re.compile(
    r"^_+\s*Ranking deferred\s*:\s*(.+?)\s*_+$", re.IGNORECASE
)
_NONE_BULLET_RE = re.compile(
    r"^-\s*none\b(?:\s*[.—-]\s*.*)?$", re.IGNORECASE
)
_RATIONALE_RE = re.compile(r"Rationale\s*:\s*(.+?)(?=(?:\s*Expected gain\s*:|\s*Risk\s*:|$))", re.IGNORECASE)
_EXPECTED_GAIN_RE = re.compile(r"Expected gain\s*:\s*(.+?)(?=(?:\s*Risk\s*:|$))", re.IGNORECASE)
_RISK_RE = re.compile(r"Risk\s*:\s*(.+?)$", re.IGNORECASE)


def _split_sections(text: str) -> dict[str, str]:
    """Return {lowercased_header: body_text} for each H2 section in *text*."""
    sections: dict[str, str] = {}
    matches = list(_H2_RE.finditer(text))
    if not matches:
        return sections
    for i, m in enumerate(matches):
        header = m.group(1).strip().lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[header] = text[start:end].strip()
    return sections


def _detect_not_applicable(body: str) -> str | None:
    """If *body* is a single ``_Not applicable: <reason>._`` line, return the reason."""
    stripped = body.strip()
    if not stripped:
        return None
    # Allow either a single line or multiple whitespace-only surrounding lines.
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if len(lines) != 1:
        return None
    m = _NOT_APPLICABLE_RE.match(lines[0].strip())
    if m:
        return m.group(1).strip()
    return None


def _extract_citations(line: str) -> list[str]:
    return _CITATION_MARKER_RE.findall(line)


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------


def _parse_observations(body: str, warnings: list[str]) -> list[Observation]:
    obs: list[Observation] = []
    seen_ids: set[str] = set()
    for raw_line in body.splitlines():
        line = raw_line.strip().lstrip("-").lstrip("*").strip()
        if not line:
            continue
        m = _OBS_LINE_RE.match(line)
        if not m:
            # Unknown line in observations — log but don't fail outright.
            warnings.append(f"observations: ignored unrecognised line: {line!r}")
            continue
        obs_id = f"O{m.group(1)}"
        if obs_id in seen_ids:
            warnings.append(f"observations: duplicate {obs_id} — keeping first")
            continue
        text = m.group(2).strip()
        cits = _extract_citations(text)
        if not cits:
            warnings.append(f"observations: {obs_id} has no citation marker")
            continue
        try:
            obs.append(
                Observation(observation_id=obs_id, text=text, citation_ids=cits)
            )
            seen_ids.add(obs_id)
        except Exception as exc:
            warnings.append(f"observations: {obs_id} rejected by schema: {exc}")
    return obs


def _parse_interpretations(
    body: str, observation_ids: set[str], warnings: list[str]
) -> list[Interpretation] | SectionEmpty:
    na = _detect_not_applicable(body)
    if na is not None:
        return SectionEmpty(reason=na)

    interps: list[Interpretation] = []
    seen: set[str] = set()
    for raw_line in body.splitlines():
        line = raw_line.strip().lstrip("-").lstrip("*").strip()
        if not line:
            continue
        m = _INTERP_LINE_RE.match(line)
        if not m:
            warnings.append(f"interpretations: ignored unrecognised line: {line!r}")
            continue
        interp_id = f"I{m.group(1)}"
        if interp_id in seen:
            warnings.append(f"interpretations: duplicate {interp_id} — keeping first")
            continue
        rest = m.group(2).strip()

        supports_match = _SUPPORTS_RE.match(rest)
        if not supports_match:
            warnings.append(
                f"interpretations: {interp_id} missing 'supports:' clause — skipped"
            )
            continue
        support_ids = [
            s.strip().upper() for s in supports_match.group(1).split(",") if s.strip()
        ]
        # Normalise stray formatting like "o1" → "O1".
        support_ids = [
            sid if sid.startswith("O") else f"O{sid.lstrip('Oo')}" for sid in support_ids
        ]
        unknown = [sid for sid in support_ids if sid not in observation_ids]
        if unknown:
            warnings.append(
                f"interpretations: {interp_id} references unknown observation ids {unknown!r} — skipped"
            )
            continue

        remainder = supports_match.group(2).strip()
        competing_with: list[str] = []
        competes_match = _COMPETES_RE.match(remainder)
        if competes_match:
            competing_with = [
                c.strip().upper()
                for c in competes_match.group(1).split(",")
                if c.strip()
            ]
            competing_with = [
                c if c.startswith("I") else f"I{c.lstrip('Ii')}" for c in competing_with
            ]
            remainder = competes_match.group(2).strip()

        if not remainder:
            warnings.append(f"interpretations: {interp_id} has empty text — skipped")
            continue

        try:
            interps.append(
                Interpretation(
                    interpretation_id=interp_id,
                    text=remainder,
                    supporting_observation_ids=support_ids,
                    competing_with=competing_with,
                )
            )
            seen.add(interp_id)
        except Exception as exc:
            warnings.append(f"interpretations: {interp_id} rejected by schema: {exc}")

    if not interps:
        return SectionEmpty(
            reason="No interpretations could be parsed from the LLM output."
        )
    return interps


def _parse_uncertainty(
    body: str, warnings: list[str]
) -> UncertaintyBlock | SectionEmpty:
    na = _detect_not_applicable(body)
    if na is not None:
        return SectionEmpty(reason=na)

    level_match = _CONFIDENCE_RE.search(body)
    if not level_match:
        warnings.append("uncertainty: missing **Confidence: <Level>** line")
        return SectionEmpty(
            reason="No confidence level was parseable from the LLM output."
        )
    raw_level = level_match.group(1).strip().capitalize()
    level: ConfidenceLevel = raw_level  # type: ignore[assignment]

    reason: str | None = None
    drivers: list[str] = []
    data_to_reduce: str | None = None
    missing: list[str] = []
    in_drivers = False

    citation_ids = _extract_citations(body)

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            in_drivers = False
            continue
        if _CONFIDENCE_RE.search(line):
            continue

        m_reason = _REASON_RE.match(line)
        if m_reason:
            reason = m_reason.group(1).strip()
            in_drivers = False
            continue

        m_data = _DATA_TO_REDUCE_RE.match(line)
        if m_data:
            data_to_reduce = m_data.group(1).strip()
            in_drivers = False
            continue

        if line.lower().startswith("drivers:"):
            in_drivers = True
            tail = line.split(":", 1)[1].strip()
            if tail:
                drivers.append(tail)
            continue

        if in_drivers and (line.startswith("-") or line.startswith("*")):
            drivers.append(line.lstrip("-*").strip())
            continue

        if line.lower().startswith("missing") or line.lower().startswith("conflicting"):
            # Optional free-form "Missing: ..." / "Conflicting: ..." line.
            missing.append(line.split(":", 1)[-1].strip())
            in_drivers = False

    if not reason:
        warnings.append("uncertainty: missing 'Reason:' line")
        return SectionEmpty(reason="No confidence reason was parseable.")
    if not data_to_reduce:
        warnings.append("uncertainty: missing 'Data to reduce uncertainty:' line")
        return SectionEmpty(reason="No reduce-uncertainty target was parseable.")

    try:
        conf = ConfidenceBlock(
            level=level,
            reason=reason,
            drivers=drivers[:4],  # schema caps at 4
            data_to_reduce_uncertainty=data_to_reduce,
        )
    except Exception as exc:
        warnings.append(f"uncertainty: ConfidenceBlock rejected: {exc}")
        return SectionEmpty(reason=f"Confidence block invalid: {exc}")

    try:
        return UncertaintyBlock(
            confidence=conf,
            missing_or_conflicting=missing,
            citation_ids=citation_ids,
        )
    except Exception as exc:
        warnings.append(f"uncertainty: UncertaintyBlock rejected: {exc}")
        return SectionEmpty(reason=f"Uncertainty block invalid: {exc}")


def _split_actions_subsections(body: str) -> tuple[str, dict[str, str]]:
    """Split a Recommended-actions body into (preamble, {h3_name: h3_body}).

    The preamble holds the numbered list (or "_Ranking deferred:_" line);
    each H3 subsection holds its own body. Subsection names are lowercased.
    """
    preamble_lines: list[str] = []
    current_h3: str | None = None
    h3_buf: dict[str, list[str]] = {}
    for raw_line in body.splitlines():
        m = _H3_RE.match(raw_line.strip())
        if m:
            current_h3 = m.group(1).strip().lower()
            h3_buf.setdefault(current_h3, [])
            continue
        if current_h3 is None:
            preamble_lines.append(raw_line)
        else:
            h3_buf[current_h3].append(raw_line)
    return (
        "\n".join(preamble_lines).strip(),
        {k: "\n".join(v).strip() for k, v in h3_buf.items()},
    )


def _bullets(body: str) -> list[str]:
    """Extract '- ' bullet items from *body*, stripping bullet markers.

    Lines starting with '-' or '*' are treated as bullets. "- None …" is
    recognised as the explicit-empty marker and yields no entries.
    """
    items: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not (line.startswith("-") or line.startswith("*")):
            continue
        if _NONE_BULLET_RE.match(line):
            return []  # explicit "no entries" — return empty list
        items.append(line.lstrip("-*").strip())
    return items


def _parse_actions(
    body: str, observation_ids: set[str], warnings: list[str]
) -> tuple[list[RecommendedAction] | SectionEmpty, DecisionSupport | None]:
    """Return (recommended_actions, decision_support_or_None).

    The decision-support extras (unresolved_prerequisites + regulatory_constraints
    + ranking_deferred_reason) are emitted when the section body carries the
    H3 subsections defined in
    :mod:`app.agent.prompts.decision_support_section`. When neither subsection
    is present, the second tuple element is None and the answer is treated as
    a non-decision-support query.
    """
    na = _detect_not_applicable(body)
    if na is not None:
        return SectionEmpty(reason=na), None

    preamble, subsections = _split_actions_subsections(body)

    # Detect "_Ranking deferred: <reason>._" sentinel inside the preamble.
    ranking_deferred_reason: str | None = None
    deferred_lines = [ln.strip() for ln in preamble.splitlines() if ln.strip()]
    if len(deferred_lines) == 1:
        m_def = _RANKING_DEFERRED_RE.match(deferred_lines[0])
        if m_def:
            ranking_deferred_reason = m_def.group(1).strip()

    actions: list[RecommendedAction] = []
    current_rank: int | None = None
    current_buf: list[str] = []

    def flush() -> None:
        nonlocal current_rank, current_buf
        if current_rank is None or not current_buf:
            current_rank = None
            current_buf = []
            return
        text = " ".join(current_buf).strip()
        # Split out rationale / expected gain / risk.
        m_rat = _RATIONALE_RE.search(text)
        rationale = m_rat.group(1).strip().rstrip(".") if m_rat else None
        m_gain = _EXPECTED_GAIN_RE.search(text)
        expected_gain = m_gain.group(1).strip().rstrip(".") if m_gain else None
        m_risk = _RISK_RE.search(text)
        risk = m_risk.group(1).strip().rstrip(".") if m_risk else None
        # Action text is everything before "Rationale:".
        action_text = text
        if m_rat:
            action_text = text[: m_rat.start()].strip().rstrip(".")
        cits = _extract_citations(text)
        if not rationale:
            warnings.append(f"recommended_actions: item {current_rank} missing Rationale — skipped")
        elif not action_text:
            warnings.append(f"recommended_actions: item {current_rank} missing action text — skipped")
        else:
            try:
                actions.append(
                    RecommendedAction(
                        rank=current_rank,
                        action=action_text,
                        rationale=rationale,
                        citation_ids=cits,
                        expected_information_gain=expected_gain,
                        risk=risk,
                    )
                )
            except Exception as exc:
                warnings.append(
                    f"recommended_actions: item {current_rank} rejected by schema: {exc}"
                )
        current_rank = None
        current_buf = []

    # Iterate over the preamble (not the whole body) so H3 subsections
    # don't pollute the action items.
    for raw_line in preamble.splitlines():
        line = raw_line.rstrip()
        if ranking_deferred_reason and line.strip().startswith("_"):
            continue  # already captured the deferral sentence
        m = _NUMBERED_ITEM_RE.match(line.strip())
        if m:
            flush()
            current_rank = int(m.group(1))
            current_buf = [m.group(2).strip()]
        elif line.strip():
            if current_rank is not None:
                current_buf.append(line.strip())
            elif not ranking_deferred_reason:
                warnings.append(
                    f"recommended_actions: ignored stray line before first item: {line!r}"
                )
    flush()

    # Decision-support extras (Step 1.4): pick up the two H3 subsections
    # whenever they appear, regardless of whether the orchestrator flagged
    # the query as decision-support. The presence of the subsections is the
    # signal — the LLM may emit them for any query when the prompt invites it.
    decision_support: DecisionSupport | None = None
    unresolved_prereqs = _bullets(subsections.get("unresolved prerequisites", ""))
    reg_constraints = _bullets(
        subsections.get("reporting / regulatory constraints", "")
        or subsections.get("regulatory constraints", "")
        or subsections.get("reporting constraints", "")
    )
    has_subsections = (
        "unresolved prerequisites" in subsections
        or any(
            k in subsections
            for k in (
                "reporting / regulatory constraints",
                "regulatory constraints",
                "reporting constraints",
            )
        )
    )
    if has_subsections or ranking_deferred_reason:
        try:
            decision_support = DecisionSupport(
                unresolved_prerequisites=unresolved_prereqs,
                regulatory_constraints=reg_constraints,
                ranking_deferred_reason=ranking_deferred_reason,
            )
        except Exception as exc:
            warnings.append(f"decision_support: rejected by schema: {exc}")
            decision_support = None

    # If the LLM deferred ranking, recommended_actions becomes SectionEmpty
    # pointing at the same reason (the GeoAnswer validator enforces this
    # mutual-exclusivity).
    if ranking_deferred_reason:
        return SectionEmpty(reason=ranking_deferred_reason), decision_support

    if not actions:
        return (
            SectionEmpty(
                reason="No recommended actions could be parsed from the LLM output."
            ),
            decision_support,
        )

    # Sort by rank and renumber to contiguous 1..N — the LLM occasionally
    # writes "1., 2., 4." with a gap; we close the gap rather than reject.
    actions.sort(key=lambda a: a.rank)
    renumbered: list[RecommendedAction] = []
    for new_rank, a in enumerate(actions, start=1):
        if a.rank != new_rank:
            warnings.append(
                f"recommended_actions: renumbered rank {a.rank} → {new_rank} to close gap"
            )
        renumbered.append(a.model_copy(update={"rank": new_rank}))
    return renumbered, decision_support


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


def parse_oiur_markdown(text: str) -> tuple[GeoAnswer | None, list[str]]:
    """Parse OIUR-shaped markdown into a :class:`GeoAnswer`.

    Returns ``(geo_answer, warnings)``. On any condition that prevents the
    refusal contract from being checked or the schema from validating,
    returns ``(None, warnings)`` — the caller falls back to the legacy
    flat-text path and routes empty-observations cases to the refusal payload.
    """
    warnings: list[str] = []
    sections = _split_sections(text)

    missing_sections = [
        k for k in _SECTION_KEYS if k not in {h.lower() for h in sections}
    ]
    if missing_sections:
        warnings.append(f"missing H2 sections: {missing_sections!r}")
        return None, warnings

    # Observations first — refusal contract gate.
    obs_body = sections.get("observations", "")
    observations = _parse_observations(obs_body, warnings)
    if not observations:
        warnings.append(
            "no parseable observations — caller should route to refusal path"
        )
        return None, warnings

    obs_ids = {o.observation_id for o in observations}
    interpretations = _parse_interpretations(
        sections.get("interpretations", ""), obs_ids, warnings
    )
    uncertainty = _parse_uncertainty(sections.get("uncertainty", ""), warnings)
    recommended_actions, decision_support = _parse_actions(
        sections.get("recommended actions", ""), obs_ids, warnings
    )

    # uncertainty SectionEmpty is only valid when interpretations is also SectionEmpty.
    if isinstance(uncertainty, SectionEmpty) and isinstance(interpretations, list):
        warnings.append(
            "uncertainty parsed as SectionEmpty but interpretations present — falling back"
        )
        return None, warnings

    try:
        answer = GeoAnswer(
            observations=observations,
            interpretations=interpretations,
            uncertainty=uncertainty,
            recommended_actions=recommended_actions,
            decision_support=decision_support,
        )
    except Exception as exc:
        warnings.append(f"GeoAnswer schema rejected: {exc}")
        return None, warnings

    return answer, warnings


__all__ = ["parse_oiur_markdown"]
