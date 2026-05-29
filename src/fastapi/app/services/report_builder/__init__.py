"""Report Builder Graph (§7.1 / §15.1) — doc-phase 80 skeleton.

The LangGraph pipeline that converts a report-type request into a
signed, compliance-checked report bundle. Twelve nodes per §15.1:

    select_report_type → plan_sections → gather_evidence →
    verify_evidence_budget → generate_section_drafts →
    validate_claims → attach_citations → generate_maps_charts →
    build_appendix → compliance_check → geologist_approval →
    export_package → activepieces_delivery

This module currently exposes:
- `ReportBuilderState` — Pydantic model carrying graph state between
  nodes. Lock the contract; node bodies graduate in later ticks.
- Node function STUBS (one per §15.1 step). Each is a no-op that
  raises NotImplementedError; the §15.1 LangGraph wiring lands when
  the `langgraph` pyproject extra ships in the runtime image.

The graph is invoked from the Hatchet `generate_report` workflow
(§7.10), not from a FastAPI request — reports are long-running async
work routed through the workflow layer.
"""
from app.services.report_builder.state import ReportBuilderState
from app.services.report_builder.templates import (
    REPORT_RISK_TIERS,
    REPORT_TEMPLATES,
    get_risk_tier,
    get_template,
)
from app.services.report_builder.nodes import (
    select_report_type,
    plan_sections,
    gather_evidence,
    verify_evidence_budget,
    generate_section_drafts,
    validate_claims,
    attach_citations,
    generate_maps_charts,
    build_appendix,
    compliance_check,
    geologist_approval,
    export_package,
    activepieces_delivery,
)
from app.services.report_builder.graph import build_report_builder_graph

__all__ = [
    "ReportBuilderState",
    "REPORT_RISK_TIERS",
    "REPORT_TEMPLATES",
    "get_risk_tier",
    "get_template",
    "select_report_type",
    "plan_sections",
    "gather_evidence",
    "verify_evidence_budget",
    "generate_section_drafts",
    "validate_claims",
    "attach_citations",
    "generate_maps_charts",
    "build_appendix",
    "compliance_check",
    "geologist_approval",
    "export_package",
    "activepieces_delivery",
    "build_report_builder_graph",
]
