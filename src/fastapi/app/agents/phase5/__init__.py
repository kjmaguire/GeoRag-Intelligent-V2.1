"""Master-plan §5 visual agents (doc-phase 73 skeletons).

Two agents per §17.5:
- Drillhole Visual QA Agent (§5.10) — validates drillhole data is
  visualization-ready
- Visual Readiness Agent (§5.11) — explains why a visualization is
  or isn't possible

Status: Step 5.10/5.11 skeletons. Each `@georag_agent`-decorated
function has the signature locked + returns a `NotImplementedError`-
shaped placeholder until visualization endpoints exist to drive them.
"""
from app.agents.phase5.drillhole_visual_qa import drillhole_visual_qa
from app.agents.phase5.visual_readiness import visual_readiness

__all__ = ["drillhole_visual_qa", "visual_readiness"]
