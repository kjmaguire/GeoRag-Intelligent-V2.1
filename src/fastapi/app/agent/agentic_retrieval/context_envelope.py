"""Context envelope — Phase 2 / Step 2.4.

The 12 context fields a geologist *can* attach to a query so the retrieval
layer doesn't silently make assumptions. Phase 2 lands the backend model
only; the query-builder UI is Phase 3.

The plan's principle (Cross-cutting rules): **never silently assume**. Any
field left unspecified must be surfaced — either in the OIUR
``uncertainty.missing_or_conflicting`` list (so the geologist sees it
inline) or in the lineage artifact (so the audit trail records what was
left ambiguous).

The five fields that trigger explicit routing behaviour (per plan Step 2.4
table at lines 257-263) are wired here:

  - Area of interest unspecified         → retrieve project-wide
  - CRS / datum unspecified              → no spatial filtering
  - Reporting code unspecified           → default NI 43-101, flag assumed
  - QA/QC constraints unspecified        → apply Silver Review defaults
  - "Decision to support" unspecified    → demote decision_support → synthesis

Other unspecified fields (depth ref, scale, stratigraphic frame, specific
objects, data sources, units/DLs, desired output structure) are recorded
but do not by themselves change routing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from app.agent.agentic_retrieval.intent_classifier import Intent

# ---------------------------------------------------------------------------
# Literal types
# ---------------------------------------------------------------------------


DepthReference = Literal["bgl", "asl", "rl", "tvd", "md"]

DataSource = Literal[
    "drill_logs",
    "assays",
    "technical_reports",
    "maps",
    "geophysics",
    "public_geoscience",
]

ReportingCode = Literal[
    "NI 43-101",
    "CIM",
    "CRIRSCO",
    "JORC",
    "SAMREC",
    "PERC",
]


# Default applied when ``reporting_code`` is unspecified. Plan Step 2.4:
# "Default to NI 43-101 (Canadian jurisdiction default); flag as assumed
# in answer."
DEFAULT_REPORTING_CODE: ReportingCode = "NI 43-101"


# Step 3.3 — Field-mode-vs-Office-mode selector. Field mode caps retrieval
# to the project corpus and concise output (max 300 words); office mode is
# the default full-corpus behaviour.
QueryMode = Literal["field", "office"]
DEFAULT_QUERY_MODE: QueryMode = "office"


# ---------------------------------------------------------------------------
# ContextEnvelope
# ---------------------------------------------------------------------------


class ContextEnvelope(BaseModel):
    """Bundle of the 12 context fields a query may carry.

    All fields default to ``None`` / empty (i.e. unspecified). The Phase 3
    query-builder UI populates them; for Phase 2 the envelope is supplied
    programmatically (or fully unspecified, which is the most common case
    today).
    """

    # 1. Area of interest — free-text or future GeoJSON.
    area_of_interest: str | None = Field(default=None)

    # 2. CRS / datum — EPSG code (e.g. 26913 for UTM Zone 13N NAD83).
    crs_epsg: int | None = Field(default=None)

    # 3. Depth / elevation reference.
    depth_reference: DepthReference | None = Field(default=None)

    # 4. Scale / resolution (e.g. "1:50000", "1:10000 and finer only").
    scale_resolution: str | None = Field(default=None)

    # 5. Stratigraphic / time frame.
    stratigraphic_frame: str | None = Field(default=None)

    # 6. Specific objects (hole ids, sample ids, formation names).
    specific_objects: list[str] = Field(default_factory=list)

    # 7. Data sources to search.
    data_sources: list[DataSource] = Field(default_factory=list)

    # 8. QA/QC constraints (free-text or future structured exclusion list).
    qaqc_constraints: str | None = Field(default=None)

    # 9. Units and detection limits.
    units_and_detection_limits: str | None = Field(default=None)

    # 10. Reporting / regulatory frame.
    reporting_code: ReportingCode | None = Field(default=None)

    # 11. Decision to support (free-text describing the choice the geologist
    #     is trying to make). Triggers decision_support routing only when
    #     populated.
    decision_to_support: str | None = Field(default=None)

    # 12. Desired output structure (e.g. "interval table + confidence + citations").
    desired_output_structure: str | None = Field(default=None)

    # Phase 3 / Step 3.3 — Field-vs-Office mode toggle. Not one of the 12
    # context fields per se — sits alongside them in the envelope because
    # the UI submits it on the same form. Defaults to ``office`` (full
    # behaviour); ``field`` caps retrieval + output for in-the-field use.
    mode: QueryMode = Field(default=DEFAULT_QUERY_MODE)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    # ``mode`` is a setting, not one of the 12 context fields the plan
    # tracks for the "unspecified" UI surfacing. Excluded from the
    # populated / unspecified set so the field count stays at 12.
    _NON_CONTEXT_FIELDS = frozenset({"mode"})

    def populated_fields(self) -> set[str]:
        """Return the names of context fields that carry a non-default value."""
        out: set[str] = set()
        for name in type(self).model_fields:
            if name in self._NON_CONTEXT_FIELDS:
                continue
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, (list, str)) and not value:
                continue
            out.add(name)
        return out

    def unspecified_fields(self) -> set[str]:
        context_fields = set(type(self).model_fields) - self._NON_CONTEXT_FIELDS
        return context_fields - self.populated_fields()

    def effective_reporting_code(self) -> tuple[ReportingCode, bool]:
        """Return ``(code, was_defaulted)``.

        ``was_defaulted=True`` when ``reporting_code`` was unspecified and
        the Canadian default was applied — the answer must flag this as
        an assumption.
        """
        if self.reporting_code is not None:
            return self.reporting_code, False
        return DEFAULT_REPORTING_CODE, True

    def supports_decision_query(self) -> bool:
        """True when the envelope carries a 'Decision to support' string.

        Per plan Step 2.4: a decision-support classification is demoted to
        synthesis when this is unspecified — the geologist hasn't said
        what decision they're trying to make.
        """
        return bool(self.decision_to_support and self.decision_to_support.strip())


EMPTY_ENVELOPE = ContextEnvelope()


# ---------------------------------------------------------------------------
# Envelope-driven routing override
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnvelopeRoutingDecision:
    """Result of applying envelope rules to a classifier-emitted intent."""

    effective_intent: Intent
    override_reason: str | None  # None when no override applied
    notes: tuple[str, ...]  # human-readable lines for the OIUR uncertainty section


def apply_envelope_overrides(
    intent: Intent,
    envelope: ContextEnvelope | None,
) -> EnvelopeRoutingDecision:
    """Apply the plan's Step-2.4 routing-override table.

    Currently the only intent demotion the plan mandates is
    ``decision_support → synthesis`` when no decision context was supplied.
    Other unspecified fields are surfaced as notes but do not change the
    intent.
    """
    env = envelope or EMPTY_ENVELOPE
    notes: list[str] = []
    override_reason: str | None = None
    effective: Intent = intent

    # Decision-support demotion.
    if intent == "decision_support" and not env.supports_decision_query():
        effective = "synthesis"
        override_reason = (
            "Decision-support classification demoted to synthesis: the query "
            "matched decision-support triggers but no 'Decision to support' "
            "context was provided. Per plan Step 2.4 the system routes broader "
            "rather than fabricating a decision frame."
        )
        notes.append(
            "Decision context unspecified — answered as synthesis instead of "
            "ranked options. Provide a 'Decision to support' value to re-enable "
            "the decision-support template."
        )

    # AOI unspecified — broader retrieval, surface as a note.
    if not env.area_of_interest:
        notes.append(
            "Area of interest unspecified — retrieved project-wide. Narrow the "
            "AOI to constrain results."
        )

    # CRS unspecified — no spatial filtering.
    if env.crs_epsg is None:
        notes.append(
            "CRS / datum unspecified — no spatial filtering applied. Spatial "
            "filters require an EPSG code (e.g. 26913 for UTM Zone 13N NAD83)."
        )

    # Reporting code unspecified — default + flag.
    code, was_defaulted = env.effective_reporting_code()
    if was_defaulted:
        notes.append(
            f"Reporting code unspecified — defaulted to {code} (Canadian "
            "jurisdiction). Specify a code to apply a different framework."
        )

    # QA/QC constraints unspecified — Silver Review defaults apply.
    if not env.qaqc_constraints:
        notes.append(
            "QA/QC constraints unspecified — Silver Review queue defaults "
            "applied. Specify exclusions (e.g. 'Exclude batches failing "
            "CRM tolerance') to override."
        )

    return EnvelopeRoutingDecision(
        effective_intent=effective,
        override_reason=override_reason,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# Human-readable descriptions for unspecified fields
# ---------------------------------------------------------------------------


_FIELD_DESCRIPTIONS: dict[str, str] = {
    "area_of_interest": "Area of interest unspecified.",
    "crs_epsg": "CRS / datum unspecified — no spatial filtering applied.",
    "depth_reference": "Depth / elevation reference unspecified.",
    "scale_resolution": "Scale / resolution unspecified.",
    "stratigraphic_frame": "Stratigraphic / time frame unspecified.",
    "specific_objects": "Specific objects (hole ids, samples) unspecified.",
    "data_sources": "Data sources to search unspecified — all surfaces consulted.",
    "qaqc_constraints": "QA/QC constraints unspecified — Silver Review defaults applied.",
    "units_and_detection_limits": "Units / detection-limit handling unspecified.",
    "reporting_code": f"Reporting code unspecified — defaulted to {DEFAULT_REPORTING_CODE}.",
    "decision_to_support": "Decision-to-support context unspecified.",
    "desired_output_structure": "Desired output structure unspecified.",
}


def unspecified_field_descriptions(envelope: ContextEnvelope | None) -> list[str]:
    """Human-readable descriptions of every unspecified field.

    Used by the assemble node to populate
    ``GeoAnswer.uncertainty.missing_or_conflicting`` so the geologist sees
    inline what the system did not know about their query.
    """
    env = envelope or EMPTY_ENVELOPE
    return [_FIELD_DESCRIPTIONS[name] for name in sorted(env.unspecified_fields())]


__all__ = [
    "ContextEnvelope",
    "DEFAULT_REPORTING_CODE",
    "DataSource",
    "DepthReference",
    "EMPTY_ENVELOPE",
    "EnvelopeRoutingDecision",
    "ReportingCode",
    "apply_envelope_overrides",
    "unspecified_field_descriptions",
]
