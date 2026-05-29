"""Master-plan §8 target agents (doc-phase 87 skeletons).

Eleven agents per §18.4. R1 for read/advise; R2 for SeaweedFS writers;
R5 for the Sign-Off agent (highest risk tier in the system).

The §8 graph (Target Recommendation Graph, §18.2) calls these as
graph nodes — see `app/services/target_recommendation/`.
"""
from app.agents.phase8.backtesting import backtesting
from app.agents.phase8.candidate_generation import candidate_generation
from app.agents.phase8.constraint import constraint
from app.agents.phase8.deposit_model import deposit_model
from app.agents.phase8.evidence_layer import evidence_layer
from app.agents.phase8.field_outcome import field_outcome
from app.agents.phase8.geologist_signoff import geologist_signoff
from app.agents.phase8.recommendation_explainer import recommendation_explainer
from app.agents.phase8.scenario_planning import scenario_planning
from app.agents.phase8.target_scoring import target_scoring
from app.agents.phase8.uncertainty import uncertainty

__all__ = [
    "backtesting",
    "candidate_generation",
    "constraint",
    "deposit_model",
    "evidence_layer",
    "field_outcome",
    "geologist_signoff",
    "recommendation_explainer",
    "scenario_planning",
    "target_scoring",
    "uncertainty",
]
