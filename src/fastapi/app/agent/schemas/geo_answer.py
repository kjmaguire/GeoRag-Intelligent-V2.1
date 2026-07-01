"""OIUR answer contract — Phase 1 / Step 1.2.

Four-tier structured answer schema for geological query responses:

    Observations    → what the retrieved evidence directly shows (cited only)
    Interpretations → what those observations suggest (linked to supporting obs)
    Uncertainty     → evidence-weighted confidence + drivers + what would reduce
    Recommended actions → ranked next steps tied to evidence

The schema **wraps** the existing ``Citation`` provenance contract in
``app.models.rag``. Each section carries its own list of citation_ids
(strings like ``[DATA-7]``, ``[NI43-2]`` that index into
``GeoRAGResponse.citations``) — citations themselves are not duplicated.

Refusal-path contract: an answer with an empty ``observations`` section
MUST be routed to the existing Refusal path guard, not returned as a
``GeoAnswer``. The schema enforces this at validation time — constructing
a ``GeoAnswer`` with empty observations raises ``ValueError``. Callers must
short-circuit to ``build_refusal_payload()`` before building the model.

Partial-evidence contract: when the retrieved corpus supports some sections
but not others (e.g. a factual lookup with no decision context), the unused
sections MUST be populated with a :class:`SectionEmpty` carrying an explicit
reason — never omitted, never left as an empty list. Empty list on
``interpretations`` / ``uncertainty`` / ``recommended_actions`` is a
validation failure; callers must use ``SectionEmpty(reason=...)`` instead.

Confidence ``level`` computation is deferred to Step 1.3. This module accepts
any ``ConfidenceLevel`` value with a non-empty ``reason``; Step 1.3 will add
the rule-based computation gate that prevents High when L3 numeric grounding
would flag the answer.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, StringConstraints, model_validator

GEO_ANSWER_SCHEMA_VERSION = "1.0"

ConfidenceLevel = Literal["High", "Medium", "Low"]

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# Matches the citation marker formats emitted by the orchestrator —
# colon variant (production default since Module 6 Phase B Chunk 3) and the
# dash variant kept for legacy paths. See
# ``orchestrator_shared_preamble_colon.py`` for the runtime contract.
_CITATION_ID_RE = re.compile(r"^\[(?:NI43|PUB|DATA|PGEO)[:\-]\d+\]$")


def _validate_citation_ids(values: list[str]) -> list[str]:
    """Each citation_id must match the orchestrator's marker format."""
    bad = [v for v in values if not _CITATION_ID_RE.match(v)]
    if bad:
        raise ValueError(
            f"citation_id values must match [NI43|PUB|DATA|PGEO][:-]<int> — got {bad!r}"
        )
    return values


CitationIdList = Annotated[list[str], Field(default_factory=list)]


# ---------------------------------------------------------------------------
# SectionEmpty — explicit "section not applicable to this query"
# ---------------------------------------------------------------------------


class SectionEmpty(BaseModel):
    """Marker for a section the retrieved corpus does not support.

    Used in place of an empty list when, e.g., a factual-lookup query has
    no decision context (recommended_actions = ``SectionEmpty(reason=...)``).
    The plan requires partial-evidence cases to be **explicitly empty with a
    stated reason**, never silently omitted.
    """

    kind: Literal["empty"] = "empty"
    reason: NonEmptyStr = Field(
        ...,
        description=(
            "Why this section is empty — e.g. 'No decision context was supplied "
            "in the query; recommended actions cannot be ranked.'"
        ),
    )


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


class Observation(BaseModel):
    """A single fact the retrieved evidence directly shows.

    Observations are the **only** section that may cite retrieval chunks
    directly. Interpretations, uncertainty drivers, and recommendations
    reference observations by ``observation_id``; they never cite chunks
    directly without going through an observation.
    """

    observation_id: Annotated[
        str, StringConstraints(pattern=r"^O\d+$")
    ] = Field(
        ...,
        description="Stable id within this answer — e.g. 'O1', 'O2'. Referenced by interpretations and recommendations.",
    )
    text: NonEmptyStr = Field(
        ...,
        description="What the evidence shows. Plain factual statement, no interpretation.",
    )
    citation_ids: list[str] = Field(
        ...,
        min_length=1,
        description="Citation markers (e.g. ['[DATA-3]', '[NI43-1]']) that support this observation. Must be non-empty.",
    )

    @model_validator(mode="after")
    def _validate_markers(self) -> Observation:
        _validate_citation_ids(self.citation_ids)
        return self


# ---------------------------------------------------------------------------
# Interpretation
# ---------------------------------------------------------------------------


class Interpretation(BaseModel):
    """What the observations suggest.

    Each interpretation is linked to ≥1 supporting observation. When the
    evidence supports more than one reading, interpretations may be flagged
    via ``competing_with`` so the UI / downstream can render them side by side.
    """

    interpretation_id: Annotated[
        str, StringConstraints(pattern=r"^I\d+$")
    ] = Field(
        ...,
        description="Stable id within this answer — e.g. 'I1', 'I2'.",
    )
    text: NonEmptyStr = Field(
        ...,
        description="The interpretation itself. Must be derivable from cited observations.",
    )
    supporting_observation_ids: list[
        Annotated[str, StringConstraints(pattern=r"^O\d+$")]
    ] = Field(
        ...,
        min_length=1,
        description="Observation ids this interpretation rests on. Must be non-empty.",
    )
    competing_with: list[
        Annotated[str, StringConstraints(pattern=r"^I\d+$")]
    ] = Field(
        default_factory=list,
        description="Other interpretation_ids that explain the same observations differently.",
    )


# ---------------------------------------------------------------------------
# Uncertainty + Confidence
# ---------------------------------------------------------------------------


class ConfidenceBlock(BaseModel):
    """Evidence-weighted confidence — populated per Step 1.3 rules.

    ``level`` is computed deterministically in Step 1.3 from retrieval
    signals (independent-source count, L3 numeric-conflict flag, chunk
    agreement). The LLM emits only ``reason`` and ``drivers``.
    """

    level: ConfidenceLevel = Field(
        ...,
        description="High / Medium / Low. Computed rule-based from retrieval signals (Step 1.3).",
    )
    reason: NonEmptyStr = Field(
        ...,
        description=(
            "One or two sentences stating what constrains confidence. "
            "Example: 'Two drill holes at 250 m spacing constrain the eastern contact. "
            "The western limit is inferred from a single surface sample.'"
        ),
    )
    drivers: list[NonEmptyStr] = Field(
        default_factory=list,
        max_length=4,
        description="Bullet list (max 4) of what most limits the interpretation.",
    )
    data_to_reduce_uncertainty: NonEmptyStr = Field(
        ...,
        description=(
            "One specific actionable item — never generic. "
            "Example: 'One infill hole between DDH-07 and DDH-12 would resolve the grade continuity question.' "
            "Strings like 'more data' fail the Step 1.3 generic-driver check."
        ),
    )

    @model_validator(mode="after")
    def _reject_generic_reduction_target(self) -> ConfidenceBlock:
        """Reject obvious generic stand-ins for the reduce-uncertainty target.

        Step 1.3 acceptance criterion: '"more data" is not acceptable — it
        must name a specific data type or location.'
        """
        generic = {
            "more data",
            "more information",
            "additional data",
            "additional information",
            "further data",
            "further information",
            "more research",
            "n/a",
            "tbd",
            "unknown",
        }
        if self.data_to_reduce_uncertainty.strip().lower() in generic:
            raise ValueError(
                f"data_to_reduce_uncertainty is too generic: {self.data_to_reduce_uncertainty!r}. "
                "Must name a specific data type or location."
            )
        return self


class UncertaintyBlock(BaseModel):
    """Uncertainty section — confidence + what's missing/conflicting in the corpus."""

    confidence: ConfidenceBlock
    missing_or_conflicting: list[NonEmptyStr] = Field(
        default_factory=list,
        description=(
            "Specific items the retrieved corpus is missing or contains "
            "conflicts about. Each item should be concrete (e.g. 'No CRM "
            "results in batch B-2024-17')."
        ),
    )
    citation_ids: list[str] = Field(
        default_factory=list,
        description="Citation markers backing the missing/conflicting items (the chunks that contain the conflicts).",
    )

    @model_validator(mode="after")
    def _validate_markers(self) -> UncertaintyBlock:
        _validate_citation_ids(self.citation_ids)
        return self


# ---------------------------------------------------------------------------
# RecommendedAction
# ---------------------------------------------------------------------------


class RecommendedAction(BaseModel):
    """One ranked next step.

    The plan requires at least one action to be tied to a cited evidence
    item (no free-floating recommendations). The ``GeoAnswer`` root validator
    enforces this across the list.
    """

    rank: int = Field(
        ...,
        ge=1,
        description="1-based rank (1 = highest priority).",
    )
    action: NonEmptyStr = Field(
        ...,
        description="The action itself, e.g. 'Drill one infill hole between DDH-07 and DDH-12'.",
    )
    rationale: NonEmptyStr = Field(
        ...,
        description="Why this action, tied to cited evidence.",
    )
    citation_ids: list[str] = Field(
        default_factory=list,
        description="Citation markers backing this recommendation. ≥1 action in the list must have non-empty citation_ids.",
    )
    expected_information_gain: NonEmptyStr | None = Field(
        default=None,
        description="What this action would resolve. Required for decision-support queries (Step 1.4).",
    )
    risk: NonEmptyStr | None = Field(
        default=None,
        description="Key risks of this action. Required for decision-support queries (Step 1.4).",
    )
    supporting_observation_ids: list[
        Annotated[str, StringConstraints(pattern=r"^O\d+$")]
    ] = Field(
        default_factory=list,
        description="Observations this action rests on. Cross-checked by GeoAnswer root validator.",
    )

    @model_validator(mode="after")
    def _validate_markers(self) -> RecommendedAction:
        _validate_citation_ids(self.citation_ids)
        return self


# ---------------------------------------------------------------------------
# DecisionSupport — Phase 1 / Step 1.4 extras
# ---------------------------------------------------------------------------


class DecisionSupport(BaseModel):
    """Decision-support extras attached to a :class:`GeoAnswer`.

    Populated when the orchestrator classifies the query as decision support
    (see :func:`app.agent.decision_support_classifier.is_decision_support_query`).
    The ranked options themselves live in ``GeoAnswer.recommended_actions`` —
    this model carries only the surrounding context the plan's decision-support
    template requires:

      * **Unresolved prerequisites** — items that must be known BEFORE the
        decision can be made confidently. If this list is non-empty, the
        ranking is provisional.
      * **Regulatory constraints** — NI 43-101 / CRIRSCO / applicable code
        implications. Required to be non-empty when the query touches
        resource classification, drilling, or sampling (the plan's
        "at least one NI 43-101 implication is surfaced" criterion); enforcement
        of that requirement happens at the orchestrator/prompt level, not in
        the schema (the schema is structural).
      * **Ranking deferred reason** — if the corpus does not support a
        defensible ranking, the LLM emits this field instead of a fabricated
        order. When present, ``GeoAnswer.recommended_actions`` should be a
        :class:`SectionEmpty` referencing the same reason.
    """

    unresolved_prerequisites: list[NonEmptyStr] = Field(
        default_factory=list,
        description=(
            "Items that must be resolved before the recommended action can be "
            "made with confidence. Empty list = ranking has no outstanding "
            "blockers."
        ),
    )
    regulatory_constraints: list[NonEmptyStr] = Field(
        default_factory=list,
        description=(
            "NI 43-101 / CRIRSCO / provincial-code implications for the "
            "recommended action. The plan requires ≥1 entry when the query "
            "touches resource classification, drilling, or sampling."
        ),
    )
    ranking_deferred_reason: NonEmptyStr | None = Field(
        default=None,
        description=(
            "When the corpus cannot defensibly differentiate options, the LLM "
            "emits this reason instead of a ranked list. Mutually exclusive "
            "with non-empty recommended_actions."
        ),
    )


# ---------------------------------------------------------------------------
# GeoAnswer — the OIUR root model
# ---------------------------------------------------------------------------


ObservationsSection = Union[list[Observation], SectionEmpty]  # noqa: UP007
InterpretationsSection = Union[list[Interpretation], SectionEmpty]  # noqa: UP007
UncertaintySection = Union[UncertaintyBlock, SectionEmpty]  # noqa: UP007
RecommendedActionsSection = Union[list[RecommendedAction], SectionEmpty]  # noqa: UP007


class GeoAnswer(BaseModel):
    """Four-section structured geological answer.

    Refusal contract: an empty ``observations`` list is **rejected**. Callers
    must short-circuit to the existing refusal payload before constructing
    a ``GeoAnswer``. A ``SectionEmpty`` on ``observations`` is also rejected
    — refusal is not an OIUR shape, it is a separate response type.

    Partial-evidence contract: empty lists on the three downstream sections
    are also rejected; use ``SectionEmpty(reason=...)`` to mark sections the
    retrieved corpus genuinely does not support.
    """

    schema_version: Literal["1.0"] = Field(
        default=GEO_ANSWER_SCHEMA_VERSION,
        description="OIUR schema version. Bumped when the contract changes.",
    )
    observations: ObservationsSection = Field(
        ...,
        description="What the retrieved evidence directly shows. Must be a non-empty list — refusal is handled outside this schema.",
    )
    interpretations: InterpretationsSection = Field(
        ...,
        description="What the observations suggest. Use SectionEmpty(reason=...) if the corpus supports only observations.",
    )
    uncertainty: UncertaintySection = Field(
        ...,
        description="Evidence-weighted confidence + drivers. Use SectionEmpty only when no interpretations exist.",
    )
    recommended_actions: RecommendedActionsSection = Field(
        ...,
        description="Ranked next steps. Use SectionEmpty(reason=...) for factual-lookup queries with no decision context.",
    )
    decision_support: DecisionSupport | None = Field(
        default=None,
        description=(
            "Decision-support extras (unresolved prerequisites, regulatory "
            "constraints, ranking-deferred reason). Populated when the "
            "orchestrator classifies the query as decision-support. None on "
            "factual / synthesis / lookup queries."
        ),
    )

    @model_validator(mode="after")
    def _enforce_oiur_invariants(self) -> GeoAnswer:
        # ── Observations: cannot be empty (refusal is a separate shape) ──
        if isinstance(self.observations, SectionEmpty):
            raise ValueError(
                "observations cannot be SectionEmpty — empty observations must "
                "route to the Refusal path, not a GeoAnswer."
            )
        if isinstance(self.observations, list) and len(self.observations) == 0:
            raise ValueError(
                "observations cannot be an empty list — empty observations "
                "must route to the Refusal path, not a GeoAnswer."
            )

        # ── Observation ids must be unique within the answer ──
        obs_ids = {o.observation_id for o in self.observations}
        if len(obs_ids) != len(self.observations):
            raise ValueError("observation_ids must be unique within a GeoAnswer.")

        # ── Empty lists on downstream sections are rejected — use SectionEmpty ──
        for name, section in (
            ("interpretations", self.interpretations),
            ("recommended_actions", self.recommended_actions),
        ):
            if isinstance(section, list) and len(section) == 0:
                raise ValueError(
                    f"{name} cannot be an empty list — use SectionEmpty(reason=...) "
                    f"to mark sections the retrieved corpus does not support."
                )

        # ── Interpretation refs must point at real observations ──
        if isinstance(self.interpretations, list):
            interp_ids: set[str] = set()
            for interp in self.interpretations:
                missing = [
                    oid for oid in interp.supporting_observation_ids if oid not in obs_ids
                ]
                if missing:
                    raise ValueError(
                        f"Interpretation {interp.interpretation_id!r} cites "
                        f"unknown observation ids: {missing!r}"
                    )
                if interp.interpretation_id in interp_ids:
                    raise ValueError(
                        f"Duplicate interpretation_id: {interp.interpretation_id!r}"
                    )
                interp_ids.add(interp.interpretation_id)
            # competing_with must reference other interpretations in this answer
            for interp in self.interpretations:
                bad = [c for c in interp.competing_with if c not in interp_ids]
                if bad:
                    raise ValueError(
                        f"Interpretation {interp.interpretation_id!r} competes with "
                        f"unknown interpretation ids: {bad!r}"
                    )

        # ── Recommended actions: ≥1 must be tied to cited evidence ──
        if isinstance(self.recommended_actions, list):
            if not any(a.citation_ids for a in self.recommended_actions):
                raise ValueError(
                    "recommended_actions must contain at least one action with "
                    "non-empty citation_ids (no free-floating recommendations)."
                )
            # action observation refs must point at real observations
            for action in self.recommended_actions:
                missing = [
                    oid
                    for oid in action.supporting_observation_ids
                    if oid not in obs_ids
                ]
                if missing:
                    raise ValueError(
                        f"RecommendedAction rank {action.rank} cites unknown "
                        f"observation ids: {missing!r}"
                    )
            # ranks must be a contiguous 1..N sequence (no gaps, no duplicates)
            ranks = sorted(a.rank for a in self.recommended_actions)
            if ranks != list(range(1, len(ranks) + 1)):
                raise ValueError(
                    f"recommended_actions ranks must be contiguous 1..N — got {ranks!r}"
                )

        # ── Uncertainty: only SectionEmpty when interpretations is also empty ──
        if isinstance(self.uncertainty, SectionEmpty) and isinstance(
            self.interpretations, list
        ):
            raise ValueError(
                "uncertainty cannot be SectionEmpty when interpretations are present "
                "— interpretations always carry confidence + drivers."
            )

        # ── Decision-support: ranking_deferred_reason and a ranked options ──
        #     list are mutually exclusive (Step 1.4 contract).
        if self.decision_support is not None:
            deferred = self.decision_support.ranking_deferred_reason
            actions_is_list = isinstance(self.recommended_actions, list)
            if deferred and actions_is_list:
                raise ValueError(
                    "decision_support.ranking_deferred_reason and a non-empty "
                    "recommended_actions list are mutually exclusive — if the "
                    "corpus cannot rank options, recommended_actions must be "
                    "SectionEmpty referencing the same reason."
                )

        return self

    def cited_marker_ids(self) -> set[str]:
        """All citation marker ids referenced anywhere in this answer.

        Used by the lineage layer (Step 1.5) to record which markers the
        answer actually used vs. which were merely retrieved.
        """
        used: set[str] = set()
        if isinstance(self.observations, list):
            for o in self.observations:
                used.update(o.citation_ids)
        if isinstance(self.uncertainty, UncertaintyBlock):
            used.update(self.uncertainty.citation_ids)
        if isinstance(self.recommended_actions, list):
            for a in self.recommended_actions:
                used.update(a.citation_ids)
        return used
