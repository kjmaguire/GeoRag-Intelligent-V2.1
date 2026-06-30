"""Report-type template manifests (§7.2 / §15.2).

Locks the section structure + risk tier + required evidence kinds for
each of the 11 report types in §15.2. The Report Planner Agent (§7.3)
reads these as the seed for `state.sections_plan`.

Per master-plan §15.2, eleven report types ship in v1:
 1. Weekly Project Digest               — R3, automated
 2. Ingestion Quality Report            — R3, automated/on-demand
 3. Technical Due Diligence Report      — R4, geologist sign-off
 4. Executive Project Intelligence      — R4, executive tone
 5. GIS/ArcGIS Sync Report              — R3, automated
 6. Target Recommendation Report        — R5, QP sign-off
 7. PublicGeo Overlay Report            — R3, public/private template
 8. Data Room Package                   — R5, full export bundle
 9. What Changed Report                 — R3, delta detection
10. NI 43-101-style Section Pack        — R5, QP credential mandatory
11. CSA 11-348 Disclosure Pack          — R5, regulatory disclosure

Doc-phase 82 — section structure locked; downstream nodes consume.

Template philosophy: each section is a stable identifier the
Evidence Curator + Claim Validator can target. Section content is
LLM-drafted; section structure is template-driven. This avoids
LLM-driven structural drift between runs.
"""
from __future__ import annotations

from app.services.report_builder.state import (
    ReportRiskTier,
    ReportType,
    SectionPlan,
)

# ---------------------------------------------------------------------------
# Per-report-type manifests
# ---------------------------------------------------------------------------

WEEKLY_PROJECT_DIGEST: list[SectionPlan] = [
    SectionPlan(
        section_id="summary",
        title="Week in Review",
        template_slug="digest.summary",
        required_evidence_kinds=["workspace_activity", "ingestion_events"],
    ),
    SectionPlan(
        section_id="recent_findings",
        title="New Findings",
        template_slug="digest.recent_findings",
        required_evidence_kinds=["claim_ledger_delta"],
    ),
    SectionPlan(
        section_id="open_questions",
        title="Open Questions",
        template_slug="digest.open_questions",
        required_evidence_kinds=["unresolved_conflicts", "low_confidence_reviews"],
    ),
    SectionPlan(
        section_id="next_week",
        title="Recommended Focus for Next Week",
        template_slug="digest.next_week",
        required_evidence_kinds=["target_zone_rankings"],
    ),
]


INGESTION_QUALITY_REPORT: list[SectionPlan] = [
    SectionPlan(
        section_id="overview",
        title="Ingestion Health Overview",
        template_slug="ingestion.overview",
        required_evidence_kinds=["workflow_runs", "ocr_quality_metrics"],
        chart_kinds=["ingestion_volume_trend", "ocr_confidence_distribution"],
    ),
    SectionPlan(
        section_id="parser_distribution",
        title="Parser Distribution + Failure Rates",
        template_slug="ingestion.parser_distribution",
        required_evidence_kinds=["parser_run_artifacts"],
        chart_kinds=["parser_run_breakdown"],
    ),
    SectionPlan(
        section_id="low_confidence_queue",
        title="Low-Confidence Page Review Queue",
        template_slug="ingestion.low_confidence_queue",
        required_evidence_kinds=["low_confidence_page_reviews"],
    ),
    SectionPlan(
        section_id="remediation_recommendations",
        title="Recommended Remediation Actions",
        template_slug="ingestion.remediation_recommendations",
        required_evidence_kinds=["retry_history"],
    ),
]


TECHNICAL_DUE_DILIGENCE_REPORT: list[SectionPlan] = [
    SectionPlan(
        section_id="executive_summary",
        title="Executive Summary",
        template_slug="tdd.executive_summary",
        required_evidence_kinds=["project_overview"],
    ),
    SectionPlan(
        section_id="property_description",
        title="Property Description + Location",
        template_slug="tdd.property_description",
        required_evidence_kinds=["project_aoi", "claim_dispositions"],
        map_kinds=["overview", "claim_disposition_overlay"],
    ),
    SectionPlan(
        section_id="regional_geology",
        title="Regional Geology",
        template_slug="tdd.regional_geology",
        required_evidence_kinds=["public_geo.bedrock_geology"],
        map_kinds=["bedrock_geology"],
    ),
    SectionPlan(
        section_id="property_geology",
        title="Property Geology + Mineralization",
        template_slug="tdd.property_geology",
        required_evidence_kinds=["silver.assay_results", "silver.lithology_logs"],
        chart_kinds=["grade_tonnage", "cross_section_01"],
    ),
    SectionPlan(
        section_id="exploration_history",
        title="Exploration History",
        template_slug="tdd.exploration_history",
        required_evidence_kinds=["historical_assessment_files"],
    ),
    SectionPlan(
        section_id="drilling_summary",
        title="Drilling Summary",
        template_slug="tdd.drilling_summary",
        required_evidence_kinds=["silver.collars", "silver.surveys"],
        map_kinds=["drillhole_collars"],
        chart_kinds=["strip_log_grid"],
    ),
    SectionPlan(
        section_id="data_quality",
        title="Data Quality + Provenance",
        template_slug="tdd.data_quality",
        required_evidence_kinds=["validation_report_summary"],
    ),
    SectionPlan(
        section_id="conclusions",
        title="Conclusions + Recommendations",
        template_slug="tdd.conclusions",
        required_evidence_kinds=["target_zone_rankings"],
    ),
]


EXECUTIVE_PROJECT_INTELLIGENCE: list[SectionPlan] = [
    SectionPlan(
        section_id="thesis",
        title="Investment Thesis",
        template_slug="epi.thesis",
        required_evidence_kinds=["project_overview"],
    ),
    SectionPlan(
        section_id="key_metrics",
        title="Key Metrics + KPIs",
        template_slug="epi.key_metrics",
        required_evidence_kinds=["aggregate_assay_stats"],
        chart_kinds=["headline_metrics"],
    ),
    SectionPlan(
        section_id="risk_register",
        title="Risk Register",
        template_slug="epi.risk_register",
        required_evidence_kinds=["unresolved_conflicts", "data_freshness"],
    ),
    SectionPlan(
        section_id="next_milestones",
        title="Next Milestones",
        template_slug="epi.next_milestones",
        required_evidence_kinds=["target_zone_rankings"],
    ),
]


GIS_ARCGIS_SYNC_REPORT: list[SectionPlan] = [
    SectionPlan(
        section_id="sync_summary",
        title="Sync Summary",
        template_slug="gis_sync.summary",
        required_evidence_kinds=["arcgis_sync_events"],
    ),
    SectionPlan(
        section_id="layer_inventory",
        title="Layer Inventory + Diffs",
        template_slug="gis_sync.layer_inventory",
        required_evidence_kinds=["arcgis_layer_state"],
    ),
    SectionPlan(
        section_id="errors_and_warnings",
        title="Errors + Warnings",
        template_slug="gis_sync.errors",
        required_evidence_kinds=["arcgis_sync_failures"],
    ),
]


TARGET_RECOMMENDATION_REPORT: list[SectionPlan] = [
    SectionPlan(
        section_id="methodology",
        title="Target Scoring Methodology",
        template_slug="target.methodology",
        required_evidence_kinds=["scoring_factor_weights"],
    ),
    SectionPlan(
        section_id="ranked_targets",
        title="Ranked Target Zones",
        template_slug="target.ranked_targets",
        required_evidence_kinds=["target_zone_scores", "shap_breakdowns"],
        map_kinds=["target_heatmap"],
        chart_kinds=["shap_per_target"],
    ),
    SectionPlan(
        section_id="per_target_briefs",
        title="Per-Target Briefs",
        template_slug="target.per_target_briefs",
        required_evidence_kinds=["target_zone_evidence"],
    ),
    SectionPlan(
        section_id="qp_signoff",
        title="QP Sign-Off + Credential Verification",
        template_slug="target.qp_signoff",
        required_evidence_kinds=["qp_credential_record"],
    ),
]


PUBLIC_GEO_OVERLAY_REPORT: list[SectionPlan] = [
    SectionPlan(
        section_id="aoi_setup",
        title="Project AOI + Buffer",
        template_slug="public_geo.aoi_setup",
        required_evidence_kinds=["project_aoi"],
        map_kinds=["aoi_overview"],
    ),
    SectionPlan(
        section_id="public_occurrences",
        title="Public Mineral Occurrences in AOI",
        template_slug="public_geo.public_occurrences",
        required_evidence_kinds=["public_geo.mineral_disposition", "public_geo.mine"],
        map_kinds=["public_occurrences_overlay"],
    ),
    SectionPlan(
        section_id="private_vs_public",
        title="Private vs Public Posture (§2.9)",
        template_slug="public_geo.private_vs_public",
        required_evidence_kinds=["data_visibility_tags"],
    ),
    SectionPlan(
        section_id="licenses",
        title="Public Data Licenses + Attribution",
        template_slug="public_geo.licenses",
        required_evidence_kinds=["public_geo.sources"],
    ),
]


DATA_ROOM_PACKAGE: list[SectionPlan] = [
    SectionPlan(
        section_id="package_manifest",
        title="Package Manifest",
        template_slug="data_room.manifest",
        required_evidence_kinds=["workspace_inventory"],
    ),
    SectionPlan(
        section_id="all_reports",
        title="All Workspace Reports",
        template_slug="data_room.all_reports",
        required_evidence_kinds=["silver.reports_inventory"],
    ),
    SectionPlan(
        section_id="raw_data_dumps",
        title="Raw Data Dumps",
        template_slug="data_room.raw_data_dumps",
        required_evidence_kinds=["silver.assay_results", "silver.lithology_logs", "silver.surveys"],
    ),
    SectionPlan(
        section_id="provenance_chain",
        title="Full Provenance + Hash Chain",
        template_slug="data_room.provenance_chain",
        required_evidence_kinds=["audit_ledger_chain"],
    ),
]


WHAT_CHANGED_REPORT: list[SectionPlan] = [
    SectionPlan(
        section_id="period",
        title="Reporting Period",
        template_slug="what_changed.period",
        required_evidence_kinds=["report_window_metadata"],
    ),
    SectionPlan(
        section_id="data_changes",
        title="Data Changes",
        template_slug="what_changed.data_changes",
        required_evidence_kinds=["silver_delta_summary"],
    ),
    SectionPlan(
        section_id="claim_changes",
        title="Claim Ledger Changes",
        template_slug="what_changed.claim_changes",
        required_evidence_kinds=["claim_ledger_delta"],
    ),
    SectionPlan(
        section_id="target_changes",
        title="Target Recommendation Changes",
        template_slug="what_changed.target_changes",
        required_evidence_kinds=["target_zone_delta"],
    ),
]


NI43101_SECTION_PACK: list[SectionPlan] = [
    SectionPlan(
        section_id="item_3_reliance",
        title="Item 3 — Reliance on Other Experts",
        template_slug="ni43101.item_3_reliance",
        required_evidence_kinds=["external_expert_disclosures"],
    ),
    SectionPlan(
        section_id="item_4_property_description",
        title="Item 4 — Property Description + Location",
        template_slug="ni43101.item_4_property_description",
        required_evidence_kinds=["project_aoi", "claim_dispositions"],
        map_kinds=["claim_disposition_overlay"],
    ),
    SectionPlan(
        section_id="item_6_history",
        title="Item 6 — History",
        template_slug="ni43101.item_6_history",
        required_evidence_kinds=["historical_assessment_files"],
    ),
    SectionPlan(
        section_id="item_7_geological_setting",
        title="Item 7 — Geological Setting + Mineralization",
        template_slug="ni43101.item_7_geological_setting",
        required_evidence_kinds=["public_geo.bedrock_geology", "silver.lithology_logs"],
        map_kinds=["bedrock_geology"],
    ),
    SectionPlan(
        section_id="item_10_drilling",
        title="Item 10 — Drilling",
        template_slug="ni43101.item_10_drilling",
        required_evidence_kinds=["silver.collars", "silver.surveys", "silver.assay_results"],
        chart_kinds=["strip_log_grid", "cross_section_grid"],
    ),
    SectionPlan(
        section_id="item_11_sample_preparation",
        title="Item 11 — Sample Preparation, Analyses, Security",
        template_slug="ni43101.item_11_sample_preparation",
        required_evidence_kinds=["sample_chain_of_custody"],
    ),
    SectionPlan(
        section_id="item_12_data_verification",
        title="Item 12 — Data Verification",
        template_slug="ni43101.item_12_data_verification",
        required_evidence_kinds=["validation_report_summary"],
    ),
    SectionPlan(
        section_id="item_25_qp_signoff",
        title="Item 25 — QP Sign-Off + Credential",
        template_slug="ni43101.item_25_qp_signoff",
        required_evidence_kinds=["qp_credential_record"],
    ),
]


CSA11348_DISCLOSURE_PACK: list[SectionPlan] = [
    SectionPlan(
        section_id="material_information",
        title="Material Information Disclosure",
        template_slug="csa11348.material_information",
        required_evidence_kinds=["material_information_register"],
    ),
    SectionPlan(
        section_id="forward_looking_statements",
        title="Forward-Looking Statements",
        template_slug="csa11348.forward_looking_statements",
        required_evidence_kinds=["target_zone_rankings"],
    ),
    SectionPlan(
        section_id="risk_factors",
        title="Risk Factors",
        template_slug="csa11348.risk_factors",
        required_evidence_kinds=["risk_register"],
    ),
    SectionPlan(
        section_id="qualified_person_statement",
        title="Qualified Person Statement",
        template_slug="csa11348.qualified_person_statement",
        required_evidence_kinds=["qp_credential_record"],
    ),
]


# ---------------------------------------------------------------------------
# Manifest registry — Report Planner Agent reads from here.
# ---------------------------------------------------------------------------

REPORT_TEMPLATES: dict[ReportType, list[SectionPlan]] = {
    "weekly_project_digest": WEEKLY_PROJECT_DIGEST,
    "ingestion_quality": INGESTION_QUALITY_REPORT,
    "technical_due_diligence": TECHNICAL_DUE_DILIGENCE_REPORT,
    "executive_project_intelligence": EXECUTIVE_PROJECT_INTELLIGENCE,
    "gis_arcgis_sync": GIS_ARCGIS_SYNC_REPORT,
    "target_recommendation": TARGET_RECOMMENDATION_REPORT,
    "public_geo_overlay": PUBLIC_GEO_OVERLAY_REPORT,
    "data_room_package": DATA_ROOM_PACKAGE,
    "what_changed": WHAT_CHANGED_REPORT,
    "ni43101_section_pack": NI43101_SECTION_PACK,
    "csa11348_disclosure_pack": CSA11348_DISCLOSURE_PACK,
}


REPORT_RISK_TIERS: dict[ReportType, ReportRiskTier] = {
    "weekly_project_digest": "R3",
    "ingestion_quality": "R3",
    "technical_due_diligence": "R4",
    "executive_project_intelligence": "R4",
    "gis_arcgis_sync": "R3",
    "target_recommendation": "R5",
    "public_geo_overlay": "R3",
    "data_room_package": "R5",
    "what_changed": "R3",
    "ni43101_section_pack": "R5",
    "csa11348_disclosure_pack": "R5",
}


def get_template(report_type: ReportType) -> list[SectionPlan]:
    """Return the seed sections_plan for a report type.

    The list is a fresh shallow copy — callers may mutate or extend
    it without affecting the registry.
    """
    return list(REPORT_TEMPLATES[report_type])


def get_risk_tier(report_type: ReportType) -> ReportRiskTier:
    """Return the sign-off risk tier for a report type."""
    return REPORT_RISK_TIERS[report_type]


__all__ = [
    "REPORT_TEMPLATES",
    "REPORT_RISK_TIERS",
    "get_template",
    "get_risk_tier",
]
