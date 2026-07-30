"""ADR-0015 Phase 3 — Qwen3-VL shadow-eval gate.

The Qwen2.5-VL-7B → Qwen3-VL-8B cutover (ADR-0015) is *gated on a shadow-mode
evaluation pass*. During the shadow window both model versions run on the same
rendered figure pages (V2 = current `Qwen2.5-VL-7B-Instruct`, V3 = candidate
`Qwen3-VL-8B-Instruct-AWQ`); their outputs are already cached independently in
`silver.pdf_vl_summaries` (keyed on `model_id`), so a paired comparison is a
natural read.

This module is the *gate*: given the per-section shadow observations it
computes the three ADR-0015 step-3 metrics and decides whether V3 may be
promoted (flip `PDF_VL_MODEL_VERSION` 2 → 3):

  1. **Schema-valid output rate** — fraction of V3 sections that produced a
     valid `VlSummaryShape`. Must be ≥ ``SCHEMA_VALID_RATE_MIN`` (the same 95%
     bar the typed-output validators already enforce).
  2. **Figure→caption link rate vs the V2 baseline** — fraction of sections
     that yielded at least one grounded claim. V3 must not regress more than
     ``LINK_RATE_REGRESSION_TOLERANCE_PP`` points below V2.
  3. **Per-page latency p95** — reported for both versions (ADR says *track*,
     not hard-gate; surfaced so the operator sees the cost of the swap).

Sits alongside ``promotion_gate`` (golden-question pass-rate deltas); this gate
is VL-specific. It is pure/synchronous analytical logic — the dual-write data
collection that produces ``VlShadowObservation`` rows is the runtime piece and
needs the V3 serving endpoint stood up (ADR-0015 step 2), which this module
deliberately does not require.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# ── Locked gate thresholds (ADR-0015 step 3) ─────────────────────────
SCHEMA_VALID_RATE_MIN: float = 0.95
"""V3 must validate as VlSummaryShape on ≥ 95% of shadow sections — the same
bar the existing §04i typed-output validators hold the VL response to."""

LINK_RATE_REGRESSION_TOLERANCE_PP: float = 2.0
"""Points the V3 figure→caption link rate may fall below the V2 baseline before
it blocks. A small tolerance absorbs sampling noise; a real regression beyond
it means V3 grounds fewer sections than 2.5-VL and must not promote."""

MIN_OBSERVATIONS_FOR_GATE: int = 20
"""Below this the shadow sample is too small to gate on — matches ADR-0015's
"golden 20-PDF corpus". Fewer observations blocks promotion (can't decide yet),
it does not silently pass."""


def grounded_claim_count(summary: Any) -> int:
    """Count grounded claims on a VlSummaryShape-like object, dict, or None.

    Duck-typed so this module needs no import dependency on the pdf_vl service.
    A claim only lands in ``VlSummaryShape.claims`` once it has passed the
    (page, bbox) provenance validator, so a positive count == grounded output.
    """
    if summary is None:
        return 0
    claims = getattr(summary, "claims", None)
    if claims is None and isinstance(summary, dict):
        claims = summary.get("claims")
    return len(claims) if claims else 0


@dataclass(frozen=True, slots=True)
class VlShadowObservation:
    """One section's paired V2/V3 shadow outcome.

    ``*_schema_valid`` is True when that version returned a valid
    ``VlSummaryShape``. Latencies are wall-clock per the whole section request
    (divided by ``page_count`` for the per-page metric); ``None`` when that
    version did not run / errored before timing.
    """

    pdf_id: str
    section_ref_hash: str
    page_count: int
    v2_schema_valid: bool
    v3_schema_valid: bool
    v2_grounded_claims: int = 0
    v3_grounded_claims: int = 0
    v2_latency_ms: float | None = None
    v3_latency_ms: float | None = None

    @property
    def v2_has_grounded_output(self) -> bool:
        return self.v2_schema_valid and self.v2_grounded_claims >= 1

    @property
    def v3_has_grounded_output(self) -> bool:
        return self.v3_schema_valid and self.v3_grounded_claims >= 1

    @classmethod
    def from_summaries(
        cls,
        *,
        pdf_id: str,
        section_ref_hash: str,
        page_count: int,
        v2_summary: Any,
        v3_summary: Any,
        v2_latency_ms: float | None = None,
        v3_latency_ms: float | None = None,
    ) -> VlShadowObservation:
        """Build an observation from two VL outputs (validated shape or None).

        ``None`` for a version means it failed schema validation (or errored) —
        i.e. that version is counted as schema-invalid for this section.
        """
        return cls(
            pdf_id=pdf_id,
            section_ref_hash=section_ref_hash,
            page_count=page_count,
            v2_schema_valid=v2_summary is not None,
            v3_schema_valid=v3_summary is not None,
            v2_grounded_claims=grounded_claim_count(v2_summary),
            v3_grounded_claims=grounded_claim_count(v3_summary),
            v2_latency_ms=v2_latency_ms,
            v3_latency_ms=v3_latency_ms,
        )


@dataclass(frozen=True, slots=True)
class VlShadowAssessment:
    """Outcome of ``assess_vl_shadow`` — the promote/block decision + metrics."""

    allow: bool
    n: int
    v3_schema_valid_rate: float
    v2_link_rate: float
    v3_link_rate: float
    link_rate_delta_pp: float  # (v3 - v2) * 100; negative = regression
    v2_latency_p95_per_page_ms: float | None
    v3_latency_p95_per_page_ms: float | None
    blocking_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allow": self.allow,
            "n": self.n,
            "thresholds": {
                "schema_valid_rate_min": SCHEMA_VALID_RATE_MIN,
                "link_rate_regression_tolerance_pp": LINK_RATE_REGRESSION_TOLERANCE_PP,
                "min_observations_for_gate": MIN_OBSERVATIONS_FOR_GATE,
            },
            "v3_schema_valid_rate": round(self.v3_schema_valid_rate, 4),
            "v2_link_rate": round(self.v2_link_rate, 4),
            "v3_link_rate": round(self.v3_link_rate, 4),
            "link_rate_delta_pp": round(self.link_rate_delta_pp, 2),
            "v2_latency_p95_per_page_ms": (
                round(self.v2_latency_p95_per_page_ms, 1)
                if self.v2_latency_p95_per_page_ms is not None
                else None
            ),
            "v3_latency_p95_per_page_ms": (
                round(self.v3_latency_p95_per_page_ms, 1)
                if self.v3_latency_p95_per_page_ms is not None
                else None
            ),
            "blocking_reasons": list(self.blocking_reasons),
        }


def assess_vl_shadow(
    observations: Sequence[VlShadowObservation],
) -> VlShadowAssessment:
    """Assess a Qwen3-VL shadow run against the ADR-0015 step-3 gate.

    Returns ``allow=True`` only when ALL hold:
      - enough observations (``n >= MIN_OBSERVATIONS_FOR_GATE``),
      - V3 schema-valid rate ``>= SCHEMA_VALID_RATE_MIN``,
      - V3 figure→caption link rate not more than
        ``LINK_RATE_REGRESSION_TOLERANCE_PP`` points below the V2 baseline.

    Latency p95 is reported but not gated (ADR-0015: *track*, no hard bar).
    """
    n = len(observations)

    if n == 0:
        return VlShadowAssessment(
            allow=False,
            n=0,
            v3_schema_valid_rate=0.0,
            v2_link_rate=0.0,
            v3_link_rate=0.0,
            link_rate_delta_pp=0.0,
            v2_latency_p95_per_page_ms=None,
            v3_latency_p95_per_page_ms=None,
            blocking_reasons=["insufficient_observations"],
        )

    v3_schema_valid_rate = sum(o.v3_schema_valid for o in observations) / n
    v2_link_rate = sum(o.v2_has_grounded_output for o in observations) / n
    v3_link_rate = sum(o.v3_has_grounded_output for o in observations) / n
    link_rate_delta_pp = (v3_link_rate - v2_link_rate) * 100.0

    v2_latency_p95 = _per_page_latency_p95(observations, version=2)
    v3_latency_p95 = _per_page_latency_p95(observations, version=3)

    blocking: list[str] = []
    if n < MIN_OBSERVATIONS_FOR_GATE:
        blocking.append("insufficient_observations")
    if v3_schema_valid_rate < SCHEMA_VALID_RATE_MIN:
        blocking.append("schema_valid_rate_below_min")
    if link_rate_delta_pp < -LINK_RATE_REGRESSION_TOLERANCE_PP:
        blocking.append("figure_link_rate_regression")

    return VlShadowAssessment(
        allow=not blocking,
        n=n,
        v3_schema_valid_rate=v3_schema_valid_rate,
        v2_link_rate=v2_link_rate,
        v3_link_rate=v3_link_rate,
        link_rate_delta_pp=link_rate_delta_pp,
        v2_latency_p95_per_page_ms=v2_latency_p95,
        v3_latency_p95_per_page_ms=v3_latency_p95,
        blocking_reasons=blocking,
    )


def _per_page_latency_p95(
    observations: Sequence[VlShadowObservation],
    *,
    version: int,
) -> float | None:
    """p95 of per-page latency (latency_ms / page_count) for one version.

    Skips observations with no recorded latency. Returns None when no version
    timing is available.
    """
    per_page: list[float] = []
    for o in observations:
        latency = o.v3_latency_ms if version == 3 else o.v2_latency_ms
        if latency is None:
            continue
        pages = max(o.page_count, 1)
        per_page.append(latency / pages)
    return _percentile(per_page, 0.95)


def _percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in [0, 1]); None for an empty input.

    Stdlib-only — the eval gate stays import-light (no numpy at call time).
    """
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = q * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac
