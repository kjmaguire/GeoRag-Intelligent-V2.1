"""Report Planner Agent (§7.3 / §15.4).

Decides section structure + per-section retrieval plan given a
requested report_type. Each of the 11 report types (§15.2) has a
canonical template manifest baked in here as a deterministic table.
LLM-driven planning replaces the table when §7.2 prompt locks land;
the output contract is preserved across both implementations.

Graduated Phase H4 — deterministic template manifest table per
report_type.

Output contract:
    {
        "sections": [
            {
                "section_id": str,
                "title": str,
                "template_slug": str,
                "required_evidence_kinds": [str],
                "map_kinds": [str],
                "chart_kinds": [str]
            }
        ],
        "summary": str,
        "report_type": str,
    }
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.agents import AgentContext, georag_agent
from app.services.report_builder.state import ReportType

logger = logging.getLogger(__name__)


# SME-curated canonical section structure per report type.
#
# Each section is (section_id, title, template_slug, evidence_kinds,
# map_kinds, chart_kinds). The Evidence Curator reads evidence_kinds
# to drive retrieval; the Map+Chart Planner reads the other two.
_TEMPLATES: dict[str, list[tuple[str, str, str, list[str], list[str], list[str]]]] = {
    "weekly_project_digest": [
        ("summary",       "Executive Summary",            "summary_paragraph", ["recent_decisions", "recent_claims"], [], []),
        ("changes",       "What Changed This Week",       "change_log",        ["new_passages", "new_outcomes"], ["activity_heatmap"], []),
        ("targets",       "Top Ranked Targets",           "target_list",       ["ranked_targets"], ["target_overview"], ["score_ranking"]),
        ("next_actions",  "Recommended Next Actions",     "action_list",       ["nbd_recommendations"], [], []),
    ],
    "ingestion_quality": [
        ("summary",         "Ingestion Health Summary",   "summary_paragraph", ["ingestion_stats"], [], ["throughput_timeline"]),
        ("parser_health",   "Parser Health by Format",    "table",             ["parser_run_artifacts"], [], ["parse_quality_bars"]),
        ("ocr_health",      "OCR Health by Page",         "table",             ["ocr_page_quality"], [], []),
        ("layer5_pass",     "§04i Layer 5 Pass Rate",     "table",             ["chunk_provenance_stats"], [], ["pass_rate_trend"]),
    ],
    "technical_due_diligence": [
        ("project_summary", "Project Summary",            "summary_paragraph", ["project_metadata"], ["project_aoi"], []),
        ("geology",         "Regional + Property Geology","geology_section",   ["bedrock_passages", "structure_measurements"], ["bedrock_overlay"], ["stereonet"]),
        ("drilling",        "Drilling History",           "drilling_summary",  ["collars", "lithology_logs", "well_log_curves"], ["collar_map"], ["strip_log"]),
        ("mineralization",  "Mineralization + Alteration","minz_section",     ["alterations", "structures", "assays"], [], ["cross_section"]),
        ("targets",         "Drilled + Untested Targets", "target_list",       ["ranked_targets", "field_outcomes"], ["target_heatmap"], ["score_ranking"]),
        ("risks",           "Material Risks",             "bullet_list",       ["risk_findings"], [], []),
        ("recommendations", "Operator Recommendations",   "action_list",       ["nbd_recommendations"], [], []),
    ],
    "executive_project_intelligence": [
        ("snapshot",      "30-Second Snapshot",           "summary_paragraph", ["recent_decisions", "ranked_targets"], ["project_aoi"], []),
        ("targets",       "Top 5 Ranked Targets",         "target_list",       ["ranked_targets"], ["target_overview"], ["score_ranking"]),
        ("changes",       "What Changed",                 "change_log",        ["new_passages", "new_outcomes"], [], []),
        ("data_health",   "Data Freshness Indicators",    "kpi_grid",          ["workspace_data_version"], [], []),
        ("next_actions",  "Recommended Next Actions",     "action_list",       ["nbd_recommendations"], [], []),
    ],
    "gis_arcgis_sync": [
        ("manifest",      "Layer Manifest",               "table",             ["layer_packs"], [], []),
        ("payload",       "GeoJSON Payload Reference",    "code_block",        ["layer_pack_uris"], [], []),
    ],
    "target_recommendation": [
        ("methodology",   "Targeting Methodology",        "summary_paragraph", ["selected_deposit_model"], [], []),
        ("targets",       "Ranked Target List",           "target_list",       ["ranked_targets"], ["target_heatmap"], ["score_ranking"]),
        ("uncertainty",   "Uncertainty Disclosures",      "table",             ["uncertainties"], [], ["uncertainty_bars"]),
        ("constraints",   "Operator Constraints Applied", "bullet_list",       ["applied_constraints"], [], []),
        ("next_data",     "Recommended Next Data",        "action_list",       ["nbd_recommendations"], [], []),
    ],
    "public_geo_overlay": [
        ("summary",       "Public Geoscience Overlay",    "summary_paragraph", ["pg_metadata"], [], []),
        ("layers",        "Available Layers",             "table",             ["pg_sources"], ["pg_overlay"], []),
        ("freshness",     "Data Freshness by Source",     "table",             ["pg_refresh_log"], [], []),
    ],
    "data_room_package": [
        ("manifest",      "Document Manifest",            "table",             ["report_list"], [], []),
        ("collars",       "Drillhole Manifest",           "table",             ["collars", "well_log_curves"], ["collar_map"], []),
        ("assays",        "Assay Summary",                "table",             ["assay_results"], [], ["grade_histogram"]),
        ("provenance",    "Source Provenance",            "table",             ["report_provenance"], [], []),
        ("licenses",      "License + Citation Statement", "license_block",     ["pg_sources"], [], []),
    ],
    "what_changed": [
        ("summary",       "Change Window Summary",        "summary_paragraph", ["window_stats"], [], []),
        ("ingestion",     "New Ingestions",               "table",             ["new_passages"], [], ["new_doc_timeline"]),
        ("decisions",     "New Decisions",                "table",             ["new_decisions"], [], []),
        ("targets",       "Target Score Shifts",          "table",             ["target_score_shifts"], [], ["score_delta"]),
        ("public_geo",    "Public Geoscience Updates",    "table",             ["new_public_records"], [], []),
    ],
    "ni43101_section_pack": [
        ("title_page",    "Title Page + Certificate",     "ni43101_title",     ["qp_credentials"], [], []),
        ("summary",       "Section 1 — Summary",          "ni43101_section",   ["project_summary"], [], []),
        ("geology",       "Section 7 — Geology",          "ni43101_section",   ["bedrock_passages", "structures"], ["bedrock_overlay"], ["stereonet"]),
        ("drilling",      "Section 10 — Drilling",        "ni43101_section",   ["collars", "lithology_logs"], ["collar_map"], ["strip_log"]),
        ("samples",       "Section 11 — Samples + Assay", "ni43101_section",   ["assay_results", "qaqc"], [], ["grade_histogram"]),
        ("interpretation","Section 14 — Mineral Resource","ni43101_section",   ["resource_estimates"], [], []),
        ("recommendations","Section 26 — Recommendations","ni43101_section",   ["nbd_recommendations"], [], []),
    ],
    "csa11348_disclosure_pack": [
        ("forward_looking","Forward-Looking Statements",  "csa_disclaimer",    ["disclosure_text"], [], []),
        ("material_facts","Material Facts",                "csa_section",       ["material_decisions"], [], []),
        ("uncertainties", "Uncertainty Disclosures",      "table",             ["uncertainties"], [], []),
        ("conflicts",     "Conflicts Disclosed",          "table",             ["conflicts_disclosed"], [], []),
        ("qp_sign_off",   "QP Sign-Off Block",            "csa_section",       ["qp_credentials"], [], []),
    ],
}


@georag_agent(
    name="Report Planner Agent",
    risk_tier="R1",  # Read-only template + project state
    version="1.0.0",  # graduated Phase H4
)
async def report_planner(
    ctx: AgentContext,
    *,
    workspace_id: UUID | str,
    project_id: UUID | str,
    report_type: ReportType,
) -> dict[str, Any]:
    """Plan section structure for the requested report type.

    Args:
        workspace_id: workspace context (RLS scope; informational here).
        project_id: project the report covers (informational here).
        report_type: one of the 11 §15.2 types.

    Returns:
        Sections plan + summary string.

    Raises:
        ValueError if report_type isn't one of the 11 §15.2 types.
    """
    if report_type not in _TEMPLATES:
        raise ValueError(
            f"unknown report_type={report_type!r}; expected one of "
            f"{sorted(_TEMPLATES.keys())!r}"
        )

    rows = _TEMPLATES[report_type]
    sections = [
        {
            "section_id":             section_id,
            "title":                  title,
            "template_slug":          template_slug,
            "required_evidence_kinds": list(evidence_kinds),
            "map_kinds":              list(map_kinds),
            "chart_kinds":            list(chart_kinds),
        }
        for (section_id, title, template_slug, evidence_kinds, map_kinds, chart_kinds)
        in rows
    ]
    summary = (
        f"report_type={report_type} sections={len(sections)} "
        f"section_ids={','.join(s['section_id'] for s in sections)}"
    )
    logger.info("report_planner: %s", summary)
    return {
        "sections":    sections,
        "summary":     summary,
        "report_type": report_type,
    }


__all__ = ["report_planner"]
