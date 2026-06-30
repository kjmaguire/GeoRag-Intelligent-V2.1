"""Report-section golden questions (10 mechanical cases).

Exercises §15.1 / §15.2 — given a `report_type`, the system's
`sections_plan` must include exactly the §15.2 template sections.
Mechanical because the template manifest in
`app.services.report_builder.templates` is the canonical source — if
a report renders without one of its template sections, the eval
catches the regression.

Each question's `expected_entities` entry lists the required
section_ids. The verifier asserts the generated `sections_plan`
contains EVERY listed id (order-independent) without spurious
extras.
"""
from __future__ import annotations

QUESTIONS: list[dict] = [
    {
        "question_set": "report_section",
        "question_text": "Generate sections_plan for the weekly_project_digest report type.",
        "context_setup": {"report_type": "weekly_project_digest"},
        "expected_entities": [
            {"required_section_ids": [
                "summary", "recent_findings", "open_questions", "next_week",
            ]}
        ],
        "difficulty": "easy",
    },
    {
        "question_set": "report_section",
        "question_text": "Generate sections_plan for the ingestion_quality report type.",
        "context_setup": {"report_type": "ingestion_quality"},
        "expected_entities": [
            {"required_section_ids": [
                "overview", "parser_distribution", "low_confidence_queue",
                "remediation_recommendations",
            ]}
        ],
        "difficulty": "easy",
    },
    {
        "question_set": "report_section",
        "question_text": "Generate sections_plan for the technical_due_diligence report type.",
        "context_setup": {"report_type": "technical_due_diligence"},
        "expected_entities": [
            {"required_section_ids": [
                "executive_summary", "property_description", "regional_geology",
                "property_geology", "exploration_history", "drilling_summary",
                "data_quality", "conclusions",
            ],
             "expected_count": 8}
        ],
        "difficulty": "medium",
    },
    {
        "question_set": "report_section",
        "question_text": "Generate sections_plan for the ni43101_section_pack report type.",
        "context_setup": {"report_type": "ni43101_section_pack"},
        "expected_entities": [
            {"required_section_ids": [
                "item_3_reliance", "item_4_property_description",
                "item_6_history", "item_7_geological_setting",
                "item_10_drilling", "item_11_sample_preparation",
                "item_12_data_verification", "item_25_qp_signoff",
            ],
             "expected_count": 8}
        ],
        "difficulty": "medium",
    },
    {
        "question_set": "report_section",
        "question_text": "Generate sections_plan for the target_recommendation report type.",
        "context_setup": {"report_type": "target_recommendation"},
        "expected_entities": [
            {"required_section_ids": [
                "methodology", "ranked_targets", "per_target_briefs",
                "qp_signoff",
            ]}
        ],
        "difficulty": "easy",
    },
    {
        "question_set": "report_section",
        "question_text": "Generate sections_plan for the public_geo_overlay report type.",
        "context_setup": {"report_type": "public_geo_overlay"},
        "expected_entities": [
            {"required_section_ids": [
                "aoi_setup", "public_occurrences", "private_vs_public",
                "licenses",
            ]}
        ],
        "difficulty": "easy",
    },
    {
        "question_set": "report_section",
        "question_text": "Generate sections_plan for the data_room_package report type.",
        "context_setup": {"report_type": "data_room_package"},
        "expected_entities": [
            {"required_section_ids": [
                "package_manifest", "all_reports", "raw_data_dumps",
                "provenance_chain",
            ]}
        ],
        "difficulty": "easy",
    },
    {
        "question_set": "report_section",
        "question_text": "Generate sections_plan for the what_changed report type.",
        "context_setup": {"report_type": "what_changed"},
        "expected_entities": [
            {"required_section_ids": [
                "period", "data_changes", "claim_changes", "target_changes",
            ]}
        ],
        "difficulty": "easy",
    },
    {
        "question_set": "report_section",
        "question_text": "Generate sections_plan for the csa11348_disclosure_pack report type.",
        "context_setup": {"report_type": "csa11348_disclosure_pack"},
        "expected_entities": [
            {"required_section_ids": [
                "material_information", "forward_looking_statements",
                "risk_factors", "qualified_person_statement",
            ]}
        ],
        "difficulty": "medium",
    },
    {
        "question_set": "report_section",
        "question_text": "Generate sections_plan for the executive_project_intelligence report type.",
        "context_setup": {"report_type": "executive_project_intelligence"},
        "expected_entities": [
            {"required_section_ids": [
                "thesis", "key_metrics", "risk_register", "next_milestones",
            ]}
        ],
        "difficulty": "easy",
    },
]

assert len(QUESTIONS) == 10, f"Expected 10 report_section questions, got {len(QUESTIONS)}"
