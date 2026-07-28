"""Target sign-off agent used by the registered cockpit router."""

from app.agents.phase8.deposit_model import deposit_model
from app.agents.phase8.evidence_layer import evidence_layer
from app.agents.phase8.geologist_signoff import geologist_signoff

__all__ = [
    "deposit_model",
    "evidence_layer",
    "geologist_signoff",
]
