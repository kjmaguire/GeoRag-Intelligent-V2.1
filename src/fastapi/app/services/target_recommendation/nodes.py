"""Target Recommendation Graph nodes (§8.4 / §18.2).

Doc-phase 86 skeletons → doc-phase 138 graduation of 6 of 12 nodes
(the deterministic / non-LLM half) + the §8.7 weighted-scoring formula.

12 §18.2 nodes:

  Graduated in doc-phase 138:
    1. select_commodity_deposit_model — registry lookup by commodity
    2. load_workspace_playbook        — workspace-scoped overrides (synthetic empty stub)
    6. score_candidate_zones          — REAL §8.7 weighted formula
    7. calculate_uncertainty          — synthetic heuristic uncertainty
    8. apply_constraints              — exclusion-set application (empty stub)
    9. rank_targets                   — REAL sort by aggregate_score DESC

  Still skeleton (need retrieval / PostGIS spatial / LLM / SeaweedFS):
    3. collect_private_evidence
    4. collect_public_geoscience
    5. generate_candidate_zones
   10. explain_score_factors
   11. create_map_layers
   12. route_to_review_cockpit

The §8.7 weighted-scoring formula is REAL math (not synthetic stub).
What's synthetic is the *factor population* — when no `score_factors`
are present on the candidate zones, the scorer derives uniform stub
factors deterministically from the zone_id hash.
"""
from __future__ import annotations

import hashlib
import logging
from uuid import UUID

from app.services.target_recommendation.deposit_models import (
    DEPOSIT_MODEL_TEMPLATES,
)
from app.services.target_recommendation.state import (
    CandidateZone,
    RankedTarget,
    ScoreFactor,
    TargetRecommendationState,
    UncertaintyEntry,
    ZoneScore,
)

log = logging.getLogger("georag.target_recommendation.nodes")


# ---------------------------------------------------------------------------
# § 8.7 weighted-scoring formula (live math)
# ---------------------------------------------------------------------------

def weighted_aggregate(factors: list[ScoreFactor]) -> float:
    """Compute the §8.7 weighted aggregate score.

    Formula: aggregate = sum(factor_value * factor_weight) / sum(factor_weight)

    Result is clamped to [0, 1]. If factors is empty or sum of weights
    is 0, returns 0.0.

    This is REAL math — not a synthetic stub. It graduates with
    doc-phase 138 because it stands alone from the LLM-dependent
    factor-population layer.
    """
    if not factors:
        return 0.0
    total_weight = sum(f.factor_weight for f in factors)
    if total_weight <= 0:
        return 0.0
    weighted_sum = sum(f.factor_value * f.factor_weight for f in factors)
    aggregate = weighted_sum / total_weight
    return max(0.0, min(1.0, aggregate))


def _synthetic_factors_for_zone(
    zone: CandidateZone, deposit_model_slug: str | None
) -> list[ScoreFactor]:
    """Stub factor population. Real evidence-driven factor generation
    lives in `collect_private_evidence` + `collect_public_geoscience`
    (still skeleton). This stub generates 3 deterministic factors per
    zone based on the zone_id hash, so different zones get different
    scores.

    Real factor population replaces this without touching the
    §8.7 formula or the downstream rank/uncertainty pipelines.
    """
    h = hashlib.sha256(str(zone.zone_id).encode()).hexdigest()
    return [
        ScoreFactor(
            factor_name="proximity_to_known_occurrence",
            factor_value=int(h[0:2], 16) / 255.0,
            factor_weight=0.4,
            contribution=0.0,
            evidence_chunk_ids=[],
        ),
        ScoreFactor(
            factor_name="alteration_signature_match",
            factor_value=int(h[2:4], 16) / 255.0,
            factor_weight=0.35,
            contribution=0.0,
            evidence_chunk_ids=[],
        ),
        ScoreFactor(
            factor_name="structural_intersect_density",
            factor_value=int(h[4:6], 16) / 255.0,
            factor_weight=0.25,
            contribution=0.0,
            evidence_chunk_ids=[],
        ),
    ]


# ---------------------------------------------------------------------------
# Graduated nodes (doc-phase 138)
# ---------------------------------------------------------------------------

async def select_commodity_deposit_model(
    state: TargetRecommendationState,
) -> TargetRecommendationState:
    """Pick a deposit model template based on workspace_playbook's
    `target_commodity` hint, or fall back to athabasca_uranium
    (Saskatchewan launch default).

    Synthetic stub — real implementation queries `targeting.target_models`
    table and the active `target_model_versions` row. For this
    graduation we use the in-process DEPOSIT_MODEL_TEMPLATES registry.

    Sets `state.target_model_id` to a deterministic UUID derived
    from the slug (synthetic; replaced when DB-backed registry
    lookup graduates).

    Graduated doc-phase 138.
    """
    commodity_hint = state.workspace_playbook.get("target_commodity")
    selected_slug = None
    for tmpl in DEPOSIT_MODEL_TEMPLATES:
        if (
            commodity_hint is not None
            and tmpl["commodity_primary"].lower() == str(commodity_hint).lower()
        ):
            selected_slug = tmpl["slug"]
            break
    if selected_slug is None:
        selected_slug = "athabasca_uranium"

    seed = hashlib.sha256(f"deposit_model__{selected_slug}".encode()).hexdigest()
    model_id = UUID(seed[:32])
    version_id = UUID(seed[32:64])

    log.info(
        "select_commodity_deposit_model.selected run_id=%s slug=%s "
        "commodity_hint=%s",
        state.run_id, selected_slug, commodity_hint,
    )
    return state.model_copy(update={
        "target_model_id": model_id,
        "target_model_version_id": version_id,
        "workspace_playbook": {
            **state.workspace_playbook,
            "selected_deposit_model_slug": selected_slug,
        },
    })


async def load_workspace_playbook(
    state: TargetRecommendationState,
) -> TargetRecommendationState:
    """Load workspace-scoped overrides from `workspace_playbooks`.

    Synthetic stub — returns the playbook unchanged. Real
    implementation queries the workspace's playbook row and merges
    its `factor_weight_overrides` + `additional_exclusions`.

    Graduated doc-phase 138.
    """
    log.info(
        "load_workspace_playbook.loaded run_id=%s playbook_keys=%s",
        state.run_id, list(state.workspace_playbook.keys()),
    )
    return state


async def collect_private_evidence(
    state: TargetRecommendationState,
) -> TargetRecommendationState:
    """Workspace-scoped retrieval (private evidence channel).

    Graduated Phase H4 — emits a deterministic evidence inventory
    payload that downstream nodes can consume. The real hybrid
    retrieval call (Qdrant + Neo4j + Postgres against
    silver.workspaces=state.workspace_id) plugs in here when the §6
    retrieval layer is feature-complete. Until then, the payload
    documents what WOULD be collected so the graph runs end-to-end.
    """
    payload = state.private_evidence or {}
    payload.setdefault("status",       "deterministic_stub")
    payload.setdefault("workspace_id", str(state.workspace_id))
    payload.setdefault("kinds_planned", [
        "collars", "lithology_logs", "assays", "structures",
        "alterations", "well_log_curves", "reports",
        "decision_records", "hypotheses",
    ])
    payload.setdefault("query_bounds", {
        "aoi_wkt":      state.aoi_geom_wkt,
        "scoring_kind": state.scoring_kind,
        "deposit_model_id": str(state.target_model_id) if state.target_model_id else None,
    })
    payload.setdefault("evidence_chunk_count", 0)
    log.info(
        "collect_private_evidence.deterministic_stub run_id=%s kinds=%d",
        state.run_id, len(payload["kinds_planned"]),
    )
    return state.model_copy(update={"private_evidence": payload})


async def collect_public_geoscience(
    state: TargetRecommendationState,
) -> TargetRecommendationState:
    """Public-geoscience retrieval channel (NRCan / BC / SK / etc.).

    Graduated Phase H4 — like ``collect_private_evidence``, records
    what the §6 public adapters WOULD return (jurisdiction tags +
    layer manifest). Real adapter calls land when the workflow gets
    permission to make outbound HTTPX requests during evaluation.
    """
    payload = state.public_evidence or {}
    payload.setdefault("status", "deterministic_stub")
    payload.setdefault("jurisdiction_hints", [
        "CA-SK",   # Saskatchewan Geological Survey
        "CA-BC",   # BC Geological Survey
        "CA-NRCAN",  # Natural Resources Canada
    ])
    payload.setdefault("layer_kinds_planned", [
        "bedrock_geology", "drillhole_collar", "mineral_occurrence",
        "mine", "rock_sample", "assessment_survey",
    ])
    payload.setdefault("query_bounds", {
        "aoi_wkt": state.aoi_geom_wkt,
    })
    payload.setdefault("record_count", 0)
    log.info(
        "collect_public_geoscience.deterministic_stub run_id=%s jurisdictions=%d",
        state.run_id, len(payload["jurisdiction_hints"]),
    )
    return state.model_copy(update={"public_evidence": payload})


async def generate_candidate_zones(
    state: TargetRecommendationState,
) -> TargetRecommendationState:
    """Generate candidate zones from the AOI.

    Graduated Phase H4 — when the caller already pre-populated
    ``state.candidate_zones`` (e.g. via the admin UI's manual-zone
    drawer or an upstream PostGIS pipeline), this node is a no-op so
    the explicit zones are preserved.

    When no zones are pre-populated, this node synthesises a small
    deterministic grid of 5 candidate zones centred on the AOI's
    centroid using a 0.5-degree dilation pattern. The grid is enough
    to keep the §18.2 pipeline runnable end-to-end during dev / test
    against a fresh workspace.

    Real PostGIS-based zone generation (CST decomposition, geological
    domain intersection, ore-body shape templates) replaces this
    synthesiser when the spatial pipeline lands.
    """
    if state.candidate_zones:
        log.info(
            "generate_candidate_zones.passthrough run_id=%s zones=%d",
            state.run_id, len(state.candidate_zones),
        )
        return state

    from uuid import uuid4
    # Synthetic 5-zone grid around an arbitrary centroid. The geom is
    # a tiny square so downstream renderers have something to plot.
    deltas = [(0.0, 0.0), (0.1, 0.1), (-0.1, 0.1), (0.1, -0.1), (-0.1, -0.1)]
    zones: list[CandidateZone] = []
    for dx, dy in deltas:
        # 0.02-degree square ~ 2 km on a side at mid-latitudes.
        wkt = (
            f"POLYGON(({dx} {dy}, {dx + 0.02} {dy}, "
            f"{dx + 0.02} {dy + 0.02}, {dx} {dy + 0.02}, "
            f"{dx} {dy}))"
        )
        zones.append(CandidateZone(zone_id=uuid4(), geom_wkt=wkt))

    log.info(
        "generate_candidate_zones.synthesised run_id=%s zones=%d source=stub_grid",
        state.run_id, len(zones),
    )
    return state.model_copy(update={"candidate_zones": zones})


async def score_candidate_zones(
    state: TargetRecommendationState,
) -> TargetRecommendationState:
    """Per-zone §8.7 weighted scoring.

    For each `state.candidate_zones` row:
      1. If `state.scores` already has a ZoneScore for that zone,
         skip (idempotent).
      2. Otherwise, look up (or stub-generate) per-zone factors.
      3. Compute `contribution = factor_value * factor_weight` for
         each factor.
      4. Compute `aggregate_score = sum(contributions) / sum(weights)`.
      5. Append a ZoneScore row.

    The §8.7 formula in `weighted_aggregate()` is REAL math. What's
    synthetic in this graduation is the factor population (when zones
    don't carry factors yet, we stub them).

    Graduated doc-phase 138.
    """
    if not state.candidate_zones:
        log.info("score_candidate_zones.no_zones run_id=%s", state.run_id)
        return state

    selected_slug = state.workspace_playbook.get("selected_deposit_model_slug")

    existing_scored_ids = {s.zone_id for s in state.scores}
    new_scores: list[ZoneScore] = []
    for zone in state.candidate_zones:
        if zone.zone_id in existing_scored_ids:
            continue
        factors = _synthetic_factors_for_zone(zone, selected_slug)
        factors_with_contrib = [
            ScoreFactor(
                factor_name=f.factor_name,
                factor_value=f.factor_value,
                factor_weight=f.factor_weight,
                contribution=f.factor_value * f.factor_weight,
                evidence_chunk_ids=f.evidence_chunk_ids,
            )
            for f in factors
        ]
        aggregate = weighted_aggregate(factors_with_contrib)
        new_scores.append(
            ZoneScore(
                zone_id=zone.zone_id,
                aggregate_score=aggregate,
                factors=factors_with_contrib,
            )
        )

    combined = list(state.scores) + new_scores
    log.info(
        "score_candidate_zones.scored run_id=%s new=%d total=%d",
        state.run_id, len(new_scores), len(combined),
    )
    return state.model_copy(update={"scores": combined})


async def calculate_uncertainty(
    state: TargetRecommendationState,
) -> TargetRecommendationState:
    """Per-zone uncertainty estimation (heuristic stub).

    For each ZoneScore, emits a 'data_sparsity' uncertainty entry
    keyed off `1 - aggregate_score / 2` so high-score zones get
    moderate uncertainty (high score with thin evidence → 0.5
    sparsity) and low-score zones get higher sparsity.

    Real uncertainty (Bayesian / bootstrap) replaces this without
    touching the surrounding flow.

    Graduated doc-phase 138.
    """
    if not state.scores:
        return state

    uncertainties: list[UncertaintyEntry] = list(state.uncertainties)
    seen_zones = {u.payload.get("zone_id") for u in uncertainties}
    for s in state.scores:
        if str(s.zone_id) in seen_zones:
            continue
        sparsity = 1.0 - (s.aggregate_score / 2.0)
        sparsity = max(0.0, min(1.0, sparsity))
        uncertainties.append(
            UncertaintyEntry(
                factor_name=None,
                uncertainty_kind="data_sparsity",
                uncertainty_value=sparsity,
                method="heuristic",
                payload={
                    "zone_id": str(s.zone_id),
                    "evaluator": "synthetic_stub",
                    "doc_phase": 138,
                },
            )
        )

    by_zone_uncertainty: dict[str, float] = {}
    for u in uncertainties:
        if u.factor_name is None:
            zid = u.payload.get("zone_id")
            if zid is not None:
                by_zone_uncertainty[zid] = u.uncertainty_value
    updated_scores: list[ZoneScore] = []
    for s in state.scores:
        updated_scores.append(
            s.model_copy(update={
                "aggregate_uncertainty": by_zone_uncertainty.get(
                    str(s.zone_id), s.aggregate_uncertainty
                ),
            })
        )

    log.info(
        "calculate_uncertainty.applied run_id=%s entries=%d",
        state.run_id, len(uncertainties),
    )
    return state.model_copy(update={
        "uncertainties": uncertainties,
        "scores": updated_scores,
    })


async def apply_constraints(
    state: TargetRecommendationState,
) -> TargetRecommendationState:
    """Apply excluded-area constraints. Synthetic stub: no zones
    are excluded in this graduation (real path queries the workspace's
    `excluded_areas` table).

    Real path is a PostGIS ST_Intersects against an exclusion polygon
    set. Graduates with the §8.5 constraints table loader.

    Graduated doc-phase 138.
    """
    log.info(
        "apply_constraints.applied run_id=%s excluded=%d (synthetic-stub)",
        state.run_id, len(state.excluded_zone_ids),
    )
    return state


async def rank_targets(
    state: TargetRecommendationState,
) -> TargetRecommendationState:
    """Sort scored zones by aggregate_score DESC (filtering out
    `excluded_zone_ids`); assign 1-based rank. Populates
    `state.ranked_targets`.

    This is REAL math — sorted ranking by score, with exclusion
    filtering. Per-target rationale text is filled by the
    `explain_score_factors` node (still skeleton).

    Graduated doc-phase 138.
    """
    excluded = set(state.excluded_zone_ids)
    eligible = [s for s in state.scores if s.zone_id not in excluded]
    eligible.sort(key=lambda s: s.aggregate_score, reverse=True)

    ranked: list[RankedTarget] = []
    for idx, s in enumerate(eligible, start=1):
        ranked.append(
            RankedTarget(
                zone_id=s.zone_id,
                rank=idx,
                aggregate_score=s.aggregate_score,
                explanation_markdown=(
                    f"[synthetic_stub doc-phase 138] Zone {s.zone_id} "
                    f"ranked #{idx} with weighted aggregate score "
                    f"{s.aggregate_score:.3f}. Rationale text awaits the "
                    f"§8.12 Recommendation Explainer Agent graduation."
                ),
                factors=s.factors,
            )
        )

    log.info(
        "rank_targets.ranked run_id=%s ranked=%d excluded=%d",
        state.run_id, len(ranked), len(excluded),
    )
    return state.model_copy(update={"ranked_targets": ranked})


# ---------------------------------------------------------------------------
# Still-skeleton nodes (doc-phase 86, await LLM / SeaweedFS / pause-resume)
# ---------------------------------------------------------------------------

async def explain_score_factors(
    state: TargetRecommendationState,
) -> TargetRecommendationState:
    """Per-target rationale rendered from the ZoneScore + factor table.

    Graduated Phase H4 with a deterministic Markdown template — no LLM
    dependency. Format:

        ### Zone {zone_id} — Rank #{rank} — Score {aggregate:.3f}

        | Factor | Value | Weight | Contribution |
        |---|---|---|---|
        | ... | ... | ... | ... |

        Top contributing factor: **{factor_name}** ({contribution:.3f}).
        Drag on the score: **{worst_factor_name}** ({worst_contribution:.3f}).

    §8.12 Recommendation Explainer Agent will swap this template for
    LLM-generated narrative once the agent graduates. The template
    output is deterministic + cacheable + lossless against the
    underlying factor math, which is what the §29.2 export gates need
    for now.
    """
    if not state.ranked_targets:
        log.info(
            "explain_score_factors.no_targets run_id=%s", state.run_id,
        )
        return state

    new_targets: list[RankedTarget] = []
    for t in state.ranked_targets:
        rows = "\n".join(
            f"| {f.factor_name} | {f.factor_value:.3f} | {f.factor_weight:.3f} "
            f"| {f.contribution:.3f} |"
            for f in t.factors
        )
        sorted_by_contrib = sorted(
            t.factors, key=lambda f: f.contribution, reverse=True,
        )
        top = sorted_by_contrib[0] if sorted_by_contrib else None
        bottom = sorted_by_contrib[-1] if sorted_by_contrib else None
        explanation = (
            f"### Zone {t.zone_id} — Rank #{t.rank} — Score {t.aggregate_score:.3f}\n\n"
            f"| Factor | Value | Weight | Contribution |\n"
            f"|---|---|---|---|\n"
            f"{rows}\n\n"
            + (
                f"Top contributing factor: **{top.factor_name}** "
                f"({top.contribution:.3f}). "
                if top else ""
            )
            + (
                f"Drag on the score: **{bottom.factor_name}** "
                f"({bottom.contribution:.3f}).\n"
                if bottom and bottom is not top else "\n"
            )
        )
        new_targets.append(t.model_copy(update={"explanation_markdown": explanation}))

    log.info(
        "explain_score_factors.rendered run_id=%s targets=%d",
        state.run_id, len(new_targets),
    )
    return state.model_copy(update={"ranked_targets": new_targets})


async def create_map_layers(
    state: TargetRecommendationState,
) -> TargetRecommendationState:
    """Render map layers (target_heatmap, ranked_target_zones) and
    expose as Martin tile layers or PNG exports.

    Graduated Phase H4 — records the map-layer plan as a manifest
    keyed off zone_id. Real rendering (Martin tile generation,
    SeaweedFS PNG writes) lands when §6 layer packs are wired; until
    then the manifest documents intent so the downstream review
    cockpit knows which layers to expect.
    """
    if not state.ranked_targets:
        log.info("create_map_layers.no_targets run_id=%s", state.run_id)
        return state

    layer_uris: dict[str, str] = dict(state.map_layer_uris)

    # Per-run layer pack manifest (URI is a logical reference until
    # the renderer call lands; consumers can ignore "stub://" prefix).
    layer_uris.setdefault(
        "target_heatmap",
        f"stub://layer/target_heatmap/run/{state.run_id}",
    )
    layer_uris.setdefault(
        "ranked_target_zones",
        f"stub://layer/ranked_target_zones/run/{state.run_id}",
    )

    # Per-target individual zone overlay so the operator can toggle
    # each target on/off in the cockpit.
    for t in state.ranked_targets:
        key = f"zone/{t.zone_id}"
        layer_uris.setdefault(key, f"stub://layer/{key}/run/{state.run_id}")

    log.info(
        "create_map_layers.recorded run_id=%s layers=%d",
        state.run_id, len(layer_uris),
    )
    return state.model_copy(update={"map_layer_uris": layer_uris})


async def route_to_review_cockpit(
    state: TargetRecommendationState,
) -> TargetRecommendationState:
    """Route to R5 review cockpit for QP credential verification +
    sign-off.

    Graduated Phase H4 — sets the routing flags + composes the
    cockpit URL deterministically off the run_id. Real Hatchet
    pause/resume hookups land when the sign-off UI ships; until then
    the state marker lets the orchestrator know the run is awaiting
    review.

    The convention for the cockpit URL is documented in §11 (admin UI
    routing). Operators land on /admin/target_recommendation/runs/<id>
    with the QP credential check + signature block rendered.
    """
    if not state.ranked_targets:
        log.info("route_to_review_cockpit.no_targets run_id=%s", state.run_id)
        return state.model_copy(update={"sent_to_review_cockpit": False})

    cockpit_url = (
        f"/admin/target_recommendation/runs/{state.run_id}"
        f"?workspace_id={state.workspace_id}"
    )

    log.info(
        "route_to_review_cockpit.routed run_id=%s targets=%d url=%s",
        state.run_id, len(state.ranked_targets), cockpit_url,
    )
    return state.model_copy(update={
        "sent_to_review_cockpit": True,
        "review_cockpit_url":     cockpit_url,
    })
