"""OCR triage golden questions (10 mechanical cases).

Exercises §04p / §9.7 `app.ocr.quality_graph.route_page` — given a
synthetic page with known confidence + profile + retry_count, the
system must route it to one of {accept, re_ocr, silver_review, reject}
with the correct reason code.

Each question's `expected_entities` entry has the shape:

    {"page_input": {profile, ocr_confidence, layout_confidence, ...},
     "expected_route": "silver_review",
     "expected_reason": "ocr_confidence_below_threshold"}

Mechanical because `route_page()` is a pure function — same input,
same route + reason. No interpretive output.
"""
from __future__ import annotations

QUESTIONS: list[dict] = [
    {
        "question_set": "ocr_triage",
        "question_text": "Route page with profile=native + ocr_confidence=0.95.",
        "context_setup": {
            "page_input": {
                "profile": "native",
                "ocr_confidence": 0.95,
                "layout_confidence": 0.9,
                "table_confidence": 0.0,
                "retry_count": 0,
                "text_line_count": 240,
            }
        },
        "expected_entities": [{"expected_route": "accept",
                              "expected_reason": None}],
        "difficulty": "easy",
    },
    {
        "question_set": "ocr_triage",
        "question_text": "Route page with profile=scanned + ocr_confidence=0.6 + retry_count=0.",
        "context_setup": {
            "page_input": {
                "profile": "scanned",
                "ocr_confidence": 0.6,
                "layout_confidence": 0.8,
                "table_confidence": 0.0,
                "retry_count": 0,
                "text_line_count": 80,
            }
        },
        "expected_entities": [{"expected_route": "re_ocr",
                              "expected_reason": None,
                              "expected_settings_attempt": 1}],
        "difficulty": "medium",
    },
    {
        "question_set": "ocr_triage",
        "question_text": "Route page with profile=scanned + ocr_confidence=0.5 + retry_count=2 (max retries).",
        "context_setup": {
            "page_input": {
                "profile": "scanned",
                "ocr_confidence": 0.5,
                "layout_confidence": 0.7,
                "table_confidence": 0.0,
                "retry_count": 2,
                "text_line_count": 65,
            }
        },
        "expected_entities": [{"expected_route": "silver_review",
                              "expected_reason": "retry_max_exceeded"}],
        "difficulty": "medium",
    },
    {
        "question_set": "ocr_triage",
        "question_text": "Route page with profile=map_heavy — must defer regardless of confidence.",
        "context_setup": {
            "page_input": {
                "profile": "map_heavy",
                "ocr_confidence": 0.92,
                "layout_confidence": 0.95,
                "table_confidence": 0.0,
                "retry_count": 0,
                "text_line_count": 20,
            }
        },
        "expected_entities": [{"expected_route": "silver_review",
                              "expected_reason": "map_heavy_v1_deferral"}],
        "difficulty": "medium",
    },
    {
        "question_set": "ocr_triage",
        "question_text": "Route page with text_line_count=0 (corrupted / blank page).",
        "context_setup": {
            "page_input": {
                "profile": "scanned",
                "ocr_confidence": 0.0,
                "layout_confidence": 0.0,
                "table_confidence": 0.0,
                "retry_count": 0,
                "text_line_count": 0,
            }
        },
        "expected_entities": [{"expected_route": "reject",
                              "expected_reason": "page_blank_or_corrupted"}],
        "difficulty": "easy",
    },
    {
        "question_set": "ocr_triage",
        "question_text": "Route page with profile=table_heavy + table_confidence=0.4 (below threshold).",
        "context_setup": {
            "page_input": {
                "profile": "table_heavy",
                "ocr_confidence": 0.85,
                "layout_confidence": 0.8,
                "table_confidence": 0.4,
                "retry_count": 0,
                "text_line_count": 60,
            }
        },
        "expected_entities": [{"expected_route": "silver_review",
                              "expected_reason": "table_confidence_below_threshold"}],
        "difficulty": "medium",
    },
    {
        "question_set": "ocr_triage",
        "question_text": "Route page with layout_confidence=0.3 (Docling layout failed).",
        "context_setup": {
            "page_input": {
                "profile": "native",
                "ocr_confidence": 0.9,
                "layout_confidence": 0.3,
                "table_confidence": 0.0,
                "retry_count": 0,
                "text_line_count": 80,
            }
        },
        "expected_entities": [{"expected_route": "silver_review",
                              "expected_reason": "layout_confidence_below_threshold"}],
        "difficulty": "medium",
    },
    {
        "question_set": "ocr_triage",
        "question_text": "Route page with profile=mixed + ocr_confidence=0.75 + retry_count=1.",
        "context_setup": {
            "page_input": {
                "profile": "mixed",
                "ocr_confidence": 0.75,
                "layout_confidence": 0.8,
                "table_confidence": 0.0,
                "retry_count": 1,
                "text_line_count": 110,
            }
        },
        "expected_entities": [{"expected_route": "re_ocr",
                              "expected_reason": None,
                              "expected_settings_attempt": 2}],
        "difficulty": "medium",
    },
    {
        "question_set": "ocr_triage",
        "question_text": "Route page with profile=preflight_invalid (encrypted PDF).",
        "context_setup": {
            "page_input": {
                "profile": "preflight_invalid",
                "ocr_confidence": 0.0,
                "layout_confidence": 0.0,
                "table_confidence": 0.0,
                "retry_count": 0,
                "text_line_count": 0,
                "preflight_error": "encrypted",
            }
        },
        "expected_entities": [{"expected_route": "reject",
                              "expected_reason": "encrypted_section"}],
        "difficulty": "easy",
    },
    {
        "question_set": "ocr_triage",
        "question_text": "Route page with ocr_confidence=0.86 (just above ACCEPT_OCR_CONFIDENCE) — must accept.",
        "context_setup": {
            "page_input": {
                "profile": "scanned",
                "ocr_confidence": 0.86,
                "layout_confidence": 0.8,
                "table_confidence": 0.0,
                "retry_count": 0,
                "text_line_count": 95,
            }
        },
        "expected_entities": [{"expected_route": "accept",
                              "expected_reason": None,
                              "edge_case": "just-above ACCEPT_OCR_CONFIDENCE=0.85"}],
        "difficulty": "hard",
    },
]

assert len(QUESTIONS) == 10, f"Expected 10 ocr_triage questions, got {len(QUESTIONS)}"
