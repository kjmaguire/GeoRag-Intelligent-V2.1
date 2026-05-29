"""Schema-mapping golden questions (10 mechanical cases).

Exercises §04 schema-mapping decisions — given a raw column name +
sample values, the system must map to the canonical `silver.*`
target column with correct unit conversion when needed.

Each question's `expected_entities` entry has the shape:

    {"raw_column": "AuGramTonne", "canonical_table": "silver.assays",
     "canonical_column": "au_ppm", "unit_conversion": "g/t → ppm = ×1"}

Mechanical because the mapping is deterministic — there's only one
right answer per raw column for the canonical schema.
"""
from __future__ import annotations


QUESTIONS: list[dict] = [
    {
        "question_set": "schema_mapping",
        "question_text": "Map column 'AuGramTonne' to its canonical silver schema target.",
        "context_setup": {
            "raw_column": "AuGramTonne",
            "sample_values": [0.12, 1.45, 3.8, 7.2, 12.4],
        },
        "expected_entities": [
            {"raw_column": "AuGramTonne",
             "canonical_table": "silver.assays",
             "canonical_column": "au_ppm",
             "unit_conversion": "g/t → ppm = ×1 (identical units)"}
        ],
        "difficulty": "easy",
    },
    {
        "question_set": "schema_mapping",
        "question_text": "Map column 'Ag_oz_ton' to its canonical silver schema target.",
        "context_setup": {
            "raw_column": "Ag_oz_ton",
            "sample_values": [0.5, 1.2, 4.6, 8.1, 15.2],
        },
        "expected_entities": [
            {"raw_column": "Ag_oz_ton",
             "canonical_table": "silver.assays",
             "canonical_column": "ag_ppm",
             "unit_conversion": "oz/ton → ppm = ×34.286"}
        ],
        "difficulty": "medium",
    },
    {
        "question_set": "schema_mapping",
        "question_text": "Map column 'Cu_pct' to its canonical silver schema target.",
        "context_setup": {
            "raw_column": "Cu_pct",
            "sample_values": [0.05, 0.12, 0.85, 1.4, 3.2],
        },
        "expected_entities": [
            {"raw_column": "Cu_pct",
             "canonical_table": "silver.assays",
             "canonical_column": "cu_ppm",
             "unit_conversion": "% → ppm = ×10000"}
        ],
        "difficulty": "medium",
    },
    {
        "question_set": "schema_mapping",
        "question_text": "Map column 'HoleID' to its canonical silver schema target.",
        "context_setup": {
            "raw_column": "HoleID",
            "sample_values": ["LEB-001", "LEB-002", "LEB-003"],
        },
        "expected_entities": [
            {"raw_column": "HoleID",
             "canonical_table": "silver.collars",
             "canonical_column": "collar_label",
             "unit_conversion": None}
        ],
        "difficulty": "easy",
    },
    {
        "question_set": "schema_mapping",
        "question_text": "Map column 'Easting_UTM' to its canonical silver schema target.",
        "context_setup": {
            "raw_column": "Easting_UTM",
            "sample_values": [503245.1, 503310.5, 503402.0],
        },
        "expected_entities": [
            {"raw_column": "Easting_UTM",
             "canonical_table": "silver.collars",
             "canonical_column": "easting_m",
             "unit_conversion": None,
             "crs_required": True}
        ],
        "difficulty": "easy",
    },
    {
        "question_set": "schema_mapping",
        "question_text": "Map column 'Northing_UTM' to its canonical silver schema target.",
        "context_setup": {
            "raw_column": "Northing_UTM",
            "sample_values": [6452310.0, 6452402.5],
        },
        "expected_entities": [
            {"raw_column": "Northing_UTM",
             "canonical_table": "silver.collars",
             "canonical_column": "northing_m",
             "unit_conversion": None,
             "crs_required": True}
        ],
        "difficulty": "easy",
    },
    {
        "question_set": "schema_mapping",
        "question_text": "Map column 'DepthFrom_m' to its canonical silver schema target.",
        "context_setup": {
            "raw_column": "DepthFrom_m",
            "sample_values": [0.0, 1.5, 3.2, 5.8, 12.4],
        },
        "expected_entities": [
            {"raw_column": "DepthFrom_m",
             "canonical_table": "silver.assays",
             "canonical_column": "depth_from",
             "unit_conversion": None}
        ],
        "difficulty": "easy",
    },
    {
        "question_set": "schema_mapping",
        "question_text": "Map column 'Azimuth' to its canonical silver schema target.",
        "context_setup": {
            "raw_column": "Azimuth",
            "sample_values": [45.0, 90.0, 180.0, 270.0],
        },
        "expected_entities": [
            {"raw_column": "Azimuth",
             "canonical_table": "silver.surveys",
             "canonical_column": "azimuth_deg",
             "unit_conversion": None,
             "domain_check": "0 ≤ azimuth_deg < 360"}
        ],
        "difficulty": "easy",
    },
    {
        "question_set": "schema_mapping",
        "question_text": "Map column 'Lithology' to its canonical silver schema target.",
        "context_setup": {
            "raw_column": "Lithology",
            "sample_values": ["GRANITE", "SCHIST", "GNEISS"],
        },
        "expected_entities": [
            {"raw_column": "Lithology",
             "canonical_table": "silver.lithology_logs",
             "canonical_column": "lithology_code",
             "unit_conversion": None,
             "ontology_resolve": "lithology"}
        ],
        "difficulty": "medium",
    },
    {
        "question_set": "schema_mapping",
        "question_text": "Map column 'Comments' from a drillhole log to its canonical silver schema target.",
        "context_setup": {
            "raw_column": "Comments",
            "sample_values": ["fault zone visible", "core loss 12-14m", "alteration intensifying"],
        },
        "expected_entities": [
            {"raw_column": "Comments",
             "canonical_table": "silver.lithology_logs",
             "canonical_column": "notes",
             "unit_conversion": None,
             "preserve_as_freetext": True}
        ],
        "difficulty": "medium",
    },
]

assert len(QUESTIONS) == 10, f"Expected 10 schema_mapping questions, got {len(QUESTIONS)}"
