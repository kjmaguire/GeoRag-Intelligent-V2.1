"""Master-plan §10 support + eval agents (doc-phase 98 skeletons).

Five support agents per §25.4 — two LangGraph (cognition) + three
Kestra (operational):

- Ticket Triage Agent (LangGraph) — categorize + severity + dupes
- Root Cause Investigation Agent (LangGraph) — drafts hypothesis
- Support Packet Agent (Kestra) — diagnostic bundle assembler
- Customer Response Drafting Agent (Kestra) — drafts response
- Escalation Routing Agent (Kestra) — high-severity routing
"""
from app.agents.phase10.customer_response_drafting import customer_response_drafting
from app.agents.phase10.escalation_routing import escalation_routing
from app.agents.phase10.root_cause_investigation import root_cause_investigation
from app.agents.phase10.support_packet import support_packet
from app.agents.phase10.ticket_triage import ticket_triage

__all__ = [
    "customer_response_drafting",
    "escalation_routing",
    "root_cause_investigation",
    "support_packet",
    "ticket_triage",
]
