"""Numeric-grounding golden questions (15 mechanical cases).

Exercises §04i layer 3 — numerical claim verification. Every assertion
in chat output must trace to a `silver.*` row within the declared
tolerance, OR the system must refuse the question.

Each question's `expected_numeric_values` entry has the shape:

    {"path": "max_au_g_t", "value": 12.4, "unit": "g/t", "tolerance_pct": 0.5}

Verifier semantics:
    - Path is a domain-namespaced key the answer extractor populates
      from the chat output.
    - Value must match within tolerance_pct of `value`.
    - Hallucinated values (no source row) fail the question.

These questions are deliberately schema-shape only. The actual numeric
expectations land when the test workspace fixture is seeded with the
20-collar test corpus (already populated per the prior autonomous-run
memory entry for the test fixture).
"""
from __future__ import annotations

QUESTIONS: list[dict] = [
    {
        "question_set": "numeric_grounding",
        "question_text": "What is the maximum Au assay value reported for project Lazy Edward Bay?",
        "context_setup": {
            "workspace_slug": "test-workspace",
            "project_slug": "lazy-edward-bay",
            "fixture_anchor": "phase18_20_collar_corpus",
        },
        "expected_intent_class": "spatial_or_attribute_query",
        "expected_numeric_values": [
            {"path": "max_au_g_t", "unit": "g/t", "tolerance_pct": 0.5,
             "source_table": "silver.assays"}
        ],
        "expected_refusal": False,
        "difficulty": "easy",
    },
    {
        "question_set": "numeric_grounding",
        "question_text": "How many drillhole collars does the Lazy Edward Bay project have?",
        "context_setup": {"project_slug": "lazy-edward-bay"},
        "expected_intent_class": "count_query",
        "expected_numeric_values": [
            {"path": "collar_count", "unit": "count", "tolerance_pct": 0,
             "source_table": "silver.collars"}
        ],
        "expected_refusal": False,
        "difficulty": "easy",
    },
    {
        "question_set": "numeric_grounding",
        "question_text": "What is the mean Au grade across all reported assays in this project?",
        "context_setup": {"project_slug": "lazy-edward-bay"},
        "expected_intent_class": "aggregate_query",
        "expected_numeric_values": [
            {"path": "mean_au_g_t", "unit": "g/t", "tolerance_pct": 1.0,
             "source_table": "silver.assays",
             "aggregate": "mean"}
        ],
        "expected_refusal": False,
        "difficulty": "easy",
    },
    {
        "question_set": "numeric_grounding",
        "question_text": "How deep is the deepest drillhole on this property?",
        "context_setup": {"project_slug": "lazy-edward-bay"},
        "expected_intent_class": "aggregate_query",
        "expected_numeric_values": [
            {"path": "max_total_depth_m", "unit": "m", "tolerance_pct": 0.1,
             "source_table": "silver.collars"}
        ],
        "expected_refusal": False,
        "difficulty": "easy",
    },
    {
        "question_set": "numeric_grounding",
        "question_text": "What is the highest U3O8 grade reported in this project?",
        "context_setup": {"project_slug": "lazy-edward-bay"},
        "expected_intent_class": "spatial_or_attribute_query",
        "expected_numeric_values": [
            {"path": "max_u3o8_pct", "unit": "%", "tolerance_pct": 1.0,
             "source_table": "silver.assays",
             "filter": "commodity = 'U3O8'"}
        ],
        "expected_refusal": False,
        "difficulty": "medium",
        # NOTE: If the test fixture contains no U3O8 assays, the system
        # MUST refuse rather than fabricate. See refusal_correctness set
        # for the paired-refusal test.
    },
    {
        "question_set": "numeric_grounding",
        "question_text": "What is the average dip of all drillhole collars in this project?",
        "context_setup": {"project_slug": "lazy-edward-bay"},
        "expected_intent_class": "aggregate_query",
        "expected_numeric_values": [
            {"path": "mean_dip_deg", "unit": "deg", "tolerance_pct": 1.0,
             "source_table": "silver.collars",
             "aggregate": "mean"}
        ],
        "expected_refusal": False,
        "difficulty": "medium",
    },
    {
        "question_set": "numeric_grounding",
        "question_text": "How many surveys exist for collar 'LEB-001'?",
        "context_setup": {"project_slug": "lazy-edward-bay", "collar": "LEB-001"},
        "expected_intent_class": "count_query",
        "expected_numeric_values": [
            {"path": "survey_count_for_collar", "unit": "count",
             "tolerance_pct": 0, "source_table": "silver.surveys"}
        ],
        "expected_refusal": False,
        "difficulty": "easy",
    },
    {
        "question_set": "numeric_grounding",
        "question_text": "What is the total core length sampled across this project?",
        "context_setup": {"project_slug": "lazy-edward-bay"},
        "expected_intent_class": "aggregate_query",
        "expected_numeric_values": [
            {"path": "total_sampled_length_m", "unit": "m", "tolerance_pct": 1.0,
             "source_table": "silver.assays",
             "aggregate": "sum_of(depth_to - depth_from)"}
        ],
        "expected_refusal": False,
        "difficulty": "medium",
    },
    {
        "question_set": "numeric_grounding",
        "question_text": "What is the standard deviation of Au grades in this project?",
        "context_setup": {"project_slug": "lazy-edward-bay"},
        "expected_intent_class": "aggregate_query",
        "expected_numeric_values": [
            {"path": "stddev_au_g_t", "unit": "g/t", "tolerance_pct": 5.0,
             "source_table": "silver.assays",
             "aggregate": "stddev"}
        ],
        "expected_refusal": False,
        "difficulty": "medium",
    },
    {
        "question_set": "numeric_grounding",
        "question_text": "How many assays in this project exceed 1 g/t Au?",
        "context_setup": {"project_slug": "lazy-edward-bay"},
        "expected_intent_class": "count_query",
        "expected_numeric_values": [
            {"path": "count_au_above_1_g_t", "unit": "count",
             "tolerance_pct": 0, "source_table": "silver.assays",
             "filter": "au_ppm > 1.0"}
        ],
        "expected_refusal": False,
        "difficulty": "medium",
    },
    {
        "question_set": "numeric_grounding",
        "question_text": "What is the easternmost UTM easting of any collar in this project?",
        "context_setup": {"project_slug": "lazy-edward-bay"},
        "expected_intent_class": "spatial_or_attribute_query",
        "expected_numeric_values": [
            {"path": "max_easting_m", "unit": "m", "tolerance_pct": 0,
             "source_table": "silver.collars",
             "aggregate": "max"}
        ],
        "expected_refusal": False,
        "difficulty": "easy",
    },
    {
        "question_set": "numeric_grounding",
        "question_text": "What is the average dip-azimuth of drillholes in this project?",
        "context_setup": {"project_slug": "lazy-edward-bay"},
        "expected_intent_class": "aggregate_query",
        "expected_numeric_values": [
            {"path": "mean_azimuth_deg", "unit": "deg", "tolerance_pct": 1.0,
             "source_table": "silver.collars",
             "aggregate": "mean"}
        ],
        "expected_refusal": False,
        "difficulty": "medium",
    },
    {
        "question_set": "numeric_grounding",
        "question_text": "What is the maximum core sample length recorded?",
        "context_setup": {"project_slug": "lazy-edward-bay"},
        "expected_intent_class": "aggregate_query",
        "expected_numeric_values": [
            {"path": "max_sample_length_m", "unit": "m", "tolerance_pct": 0.1,
             "source_table": "silver.assays",
             "aggregate": "max(depth_to - depth_from)"}
        ],
        "expected_refusal": False,
        "difficulty": "medium",
    },
    {
        "question_set": "numeric_grounding",
        "question_text": "How many collars in this project have a dip steeper than -60 degrees?",
        "context_setup": {"project_slug": "lazy-edward-bay"},
        "expected_intent_class": "count_query",
        "expected_numeric_values": [
            {"path": "count_collars_steep", "unit": "count",
             "tolerance_pct": 0, "source_table": "silver.collars",
             "filter": "dip < -60"}
        ],
        "expected_refusal": False,
        "difficulty": "medium",
    },
    {
        "question_set": "numeric_grounding",
        "question_text": "What was the highest-grade Au intercept length-weighted-averaged across all collars?",
        "context_setup": {"project_slug": "lazy-edward-bay"},
        "expected_intent_class": "aggregate_query",
        "expected_numeric_values": [
            {"path": "max_lwa_au_g_t", "unit": "g/t", "tolerance_pct": 2.0,
             "source_table": "silver.assays",
             "aggregate": "length_weighted_average(au_ppm, depth_to - depth_from)"}
        ],
        "expected_refusal": False,
        "difficulty": "hard",
    },
]

assert len(QUESTIONS) == 15, f"Expected 15 numeric_grounding questions, got {len(QUESTIONS)}"
