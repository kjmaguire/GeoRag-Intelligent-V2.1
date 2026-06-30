"""Support Cockpit service utilities (§10.12 / §25.3) — doc-phase 99.

Currently exposes:
- `emit_support_access_audit()` — emits an `audit_ledger.action_type
  = 'support_access'` entry every time an ops user reads or replays
  cross-workspace data. Workspace owners see these on their own
  audit ledger.

Live behavior lands when the cockpit Laravel admin module (§10.11)
wires in.
"""
from app.services.support_cockpit.access_audit import (
    AccessKind,
    emit_support_access_audit,
)
from app.services.support_cockpit.customer_response_drafting import (
    DraftOutcome,
    draft_customer_response,
)
from app.services.support_cockpit.escalation_routing import (
    EscalationOutcome,
    RoutingDecision,
    route_escalation,
)
from app.services.support_cockpit.langfuse_link import (
    build_langfuse_trace_url,
    open_trace_with_audit,
)
from app.services.support_cockpit.root_cause_investigation import (
    InvestigationResult,
    investigate_ticket,
)
from app.services.support_cockpit.support_packet import (
    SupportPacket,
    build_support_packet,
)
from app.services.support_cockpit.ticket_triage import (
    TriageOutcome,
    triage_ticket,
    triage_unclassified_tickets,
)

__all__ = [
    "AccessKind",
    "emit_support_access_audit",
    "build_langfuse_trace_url",
    "open_trace_with_audit",
    "TriageOutcome",
    "triage_ticket",
    "triage_unclassified_tickets",
    "InvestigationResult",
    "investigate_ticket",
    "SupportPacket",
    "build_support_packet",
    "DraftOutcome",
    "draft_customer_response",
    "EscalationOutcome",
    "RoutingDecision",
    "route_escalation",
]
