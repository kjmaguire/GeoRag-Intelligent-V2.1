"""Master-plan §7 reporting agents (doc-phase 78 + 81 skeletons).

Eight in-graph reporting agents per §15.4:

- Export Compliance Agent (§7.8) — universal §29.2 gate (R3)
- Report Planner Agent (§7.3) — section structure
- Evidence Curator Agent (§7.3) — per-section retrieval
- Claim Validator Agent (§7.4) — §04i layer validation
- Map/Chart Planner Agent (§7.5) — invokes Map / Chart subgraphs
- Appendix Builder Agent (§7.6) — citation/source/evidence manifests
- Presentation Coach Agent (§7.6) — workspace-tone rewrite
- Conflict Resolver Agent (§7.4) — §29.2 conflict disclosure
"""
from app.agents.phase7.appendix_builder import appendix_builder
from app.agents.phase7.claim_validator import claim_validator
from app.agents.phase7.conflict_resolver import conflict_resolver
from app.agents.phase7.evidence_curator import evidence_curator
from app.agents.phase7.export_compliance import export_compliance
from app.agents.phase7.map_chart_planner import map_chart_planner
from app.agents.phase7.presentation_coach import presentation_coach
from app.agents.phase7.report_planner import report_planner

__all__ = [
    "appendix_builder",
    "claim_validator",
    "conflict_resolver",
    "evidence_curator",
    "export_compliance",
    "map_chart_planner",
    "presentation_coach",
    "report_planner",
]
