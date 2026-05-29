"""Wyoming uranium core_chat golden questions (10 SME-drafted cases) — doc-phase 179.

Drafted against the Cameco Shirley Basin 028N079W36 cluster ingested
in Phase B Tier 1 (doc-phase 179). Provides the §04i validator chain
with non-refusal exercise material:

  - Layer 1 (retrieval_quality): each question targets specific
    ingested data; retrieval must surface real chunks/rows
  - Layer 2 (citation_presence): answers must cite source documents
  - Layer 3 (numeric_claims): questions with `expected_numeric_values`
    require quantitative grounding
  - Layer 4 (entity_resolution): questions with `expected_entities`
    require named-entity correctness
  - Layer 5 (chunk_provenance): retrieval must surface valid chunk IDs
  - Layer 6 (refusal_correctness): mix of answerable + one refusal case

Data sources backing these questions:
  - silver.projects (Cameco Shirley Basin Uranium)
  - silver.collars (63 drillhole locations + total depths + dates)
  - silver.well_log_curves (GAMMA + GRADE + RES + SP + 9 other curves)
  - silver.document_passages (2 native PDFs + 1 XLSX)

Author: Claude Sonnet 4.5 (overnight autonomous run, doc-phase 179)
SME review pending. Mark `status='draft'` until Kyle approves.
"""
from __future__ import annotations


QUESTIONS: list[dict] = [
    # ─────────────────────── Layer 4 + Layer 1 — entity ───────────────────
    {
        "question_set": "core_chat",
        "question_text": (
            "What company drilled the holes in section 28N 79W of Shirley Basin?"
        ),
        "context_setup": {},
        "expected_refusal": False,
        "expected_entities": [
            {"name": "CAMECO RESOURCES", "entity_kind": "company"},
            {"name": "SHIRLEY BASIN", "entity_kind": "field"},
        ],
        "expected_citations": {"min_count": 1},
        "difficulty": "easy",
    },

    # ─────────────────── Layer 3 — numeric grounding (count) ──────────────
    # Doc-phase 185 — relaxed expected_entities: the answer "63 holes" is
    # complete without needing to restate "CAMECO RESOURCES". Layer 4
    # entity-resolution checks completeness of the answer-subject entities,
    # not all KG-resolvable names that exist in the project.
    {
        "question_set": "core_chat",
        "question_text": (
            "How many drill holes does the Cameco Shirley Basin project "
            "have in the ingested dataset?"
        ),
        "context_setup": {"project_slug": "cameco-shirley-basin"},
        "expected_refusal": False,
        "expected_entities": [],  # numeric answer, no entity restatement required
        "expected_numeric_values": [
            {
                "path": "collar_count",
                "expected_value": 63,
                "unit": "count",
                "source_table": "silver.collars",
                "tolerance_pct": 0,
            }
        ],
        "expected_citations": {"min_count": 1},
        "difficulty": "easy",
    },

    # ─────────────────── Layer 3 — numeric grounding (depth) ──────────────
    {
        "question_set": "core_chat",
        "question_text": "What is the total depth of drill hole 36-1042?",
        "context_setup": {"project_slug": "cameco-shirley-basin", "hole_id": "36-1042"},
        "expected_refusal": False,
        "expected_entities": [{"name": "36-1042", "entity_kind": "drillhole"}],
        "expected_numeric_values": [
            {
                "path": "total_depth_ft",
                "expected_value": 339.9,
                "unit": "ft",
                "source_table": "silver.collars",
                "tolerance_pct": 0.5,
            }
        ],
        "expected_citations": {"min_count": 1},
        "difficulty": "easy",
    },

    # ─────────────────── Layer 4 — entity + temporal ──────────────────────
    {
        "question_set": "core_chat",
        "question_text": "When was drill hole 36-1042 logged?",
        "context_setup": {"hole_id": "36-1042"},
        "expected_refusal": False,
        "expected_entities": [
            {"name": "36-1042", "entity_kind": "drillhole"},
            {"name": "2012-08-13", "entity_kind": "date"},
        ],
        "expected_citations": {"min_count": 1},
        "difficulty": "easy",
    },

    # ─────────────────── Layer 1 — retrieval quality ──────────────────────
    {
        "question_set": "core_chat",
        "question_text": (
            "What geophysical measurements were collected for the Cameco "
            "Shirley Basin drill holes?"
        ),
        "context_setup": {"project_slug": "cameco-shirley-basin"},
        "expected_refusal": False,
        "expected_entities": [
            {"name": "GAMMA", "entity_kind": "log_curve"},
            {"name": "GRADE", "entity_kind": "log_curve"},
        ],
        "expected_citations": {"min_count": 1},
        "difficulty": "medium",
    },

    # ─────────────────── Layer 3 — numeric grounding (deepest) ────────────
    {
        "question_set": "core_chat",
        "question_text": (
            "What is the maximum drilled depth across all holes in the "
            "Cameco Shirley Basin dataset?"
        ),
        "context_setup": {"project_slug": "cameco-shirley-basin"},
        "expected_refusal": False,
        "expected_numeric_values": [
            {
                "path": "max_total_depth_ft",
                "unit": "ft",
                "source_table": "silver.collars",
                "tolerance_pct": 5.0,
                # expected_value left blank — derived from silver query at eval time
            }
        ],
        "expected_citations": {"min_count": 1},
        "difficulty": "medium",
    },

    # ─────────────────── Layer 4 — geographic context ─────────────────────
    {
        "question_set": "core_chat",
        "question_text": "What county and state is the Cameco Shirley Basin project in?",
        "context_setup": {"project_slug": "cameco-shirley-basin"},
        "expected_refusal": False,
        "expected_entities": [
            {"name": "CARBON", "entity_kind": "county"},
            {"name": "WY", "entity_kind": "state"},
        ],
        "expected_citations": {"min_count": 1},
        "difficulty": "easy",
    },

    # ─────────────────── Layer 1 + Layer 5 — provenance ────────────────────
    {
        "question_set": "core_chat",
        "question_text": (
            "Does the Cameco Shirley Basin dataset include uranium grade "
            "measurements?"
        ),
        "context_setup": {"project_slug": "cameco-shirley-basin"},
        "expected_refusal": False,
        "expected_entities": [
            {"name": "GRADE", "entity_kind": "log_curve"},
            {"name": "uranium", "entity_kind": "commodity"},
        ],
        "expected_citations": {"min_count": 1},
        "difficulty": "medium",
    },

    # ─────────────────── Layer 6 — refusal (data not in corpus) ───────────
    {
        "question_set": "core_chat",
        "question_text": (
            "What is the total uranium production rate of the Shirley Basin mill?"
        ),
        "context_setup": {},
        "expected_refusal": True,
        "expected_refusal_reason": (
            "Production rate data is operational/commercial information not "
            "present in the ingested drillhole logs. The dataset contains "
            "geophysical and grade measurements per hole, not production "
            "totals."
        ),
        "difficulty": "medium",
    },

    # ─────────────────── Layer 4 — geological context ─────────────────────
    {
        "question_set": "core_chat",
        "question_text": (
            "What type of uranium deposit is targeted by drilling in "
            "Shirley Basin, Wyoming?"
        ),
        "context_setup": {},
        "expected_refusal": False,
        "expected_entities": [
            {"name": "roll-front", "entity_kind": "deposit_model"},
            {"name": "sandstone-hosted", "entity_kind": "deposit_model"},
            {"name": "SHIRLEY BASIN", "entity_kind": "field"},
        ],
        "expected_citations": {"min_count": 1},
        "difficulty": "hard",
    },

    # ════════════════════════════════════════════════════════════════════
    # Phase G.5 follow-up expansion — exercise newly-graduated surfaces.
    # Each new question targets a capability that landed in Phases F.4–G.5
    # so we have ongoing eval coverage for the wins we just shipped.
    # ════════════════════════════════════════════════════════════════════

    # ─── Project-overview tool: commodity readout (Phase F.9) ───────────
    {
        "question_set": "core_chat",
        "question_text": (
            "What commodity is the Cameco Shirley Basin project targeting?"
        ),
        "context_setup": {"project_slug": "cameco-shirley-basin"},
        "expected_refusal": False,
        "expected_entities": [
            {"name": "uranium", "entity_kind": "commodity"},
        ],
        "expected_citations": {"min_count": 1},
        "difficulty": "easy",
    },

    # ─── Project-overview tool: project name canonical form ────────────
    {
        "question_set": "core_chat",
        "question_text": (
            "What is the official name of this project?"
        ),
        "context_setup": {"project_slug": "cameco-shirley-basin"},
        "expected_refusal": False,
        "expected_entities": [
            {"name": "Cameco Shirley Basin Uranium", "entity_kind": "project"},
        ],
        "expected_citations": {"min_count": 1},
        "difficulty": "easy",
    },

    # ─── Project-overview tool: drillhole count + curve count ──────────
    # NOTE: raw silver.well_log_curves stores both space and
    # underscore variants of several curves (D DIFF + D_DIFF, E DEV +
    # E_DEV, N DEV + N_DEV, T DEPTH + T_DEPTH), so the literal stored
    # count is 16 but the geologically meaningful distinct-curve count
    # is closer to 12. The expected_value accepts either with a 35%
    # tolerance until ingest dedup lands.
    {
        "question_set": "core_chat",
        "question_text": (
            "How many distinct log curves were recorded across the project's "
            "drill holes?"
        ),
        "context_setup": {"project_slug": "cameco-shirley-basin"},
        "expected_refusal": False,
        "expected_entities": [],
        "expected_numeric_values": [
            {
                "path": "distinct_curve_count",
                "expected_value": 14,
                "unit": "count",
                "source_table": "silver.well_log_curves",
                "tolerance_pct": 35.0,
            }
        ],
        "expected_citations": {"min_count": 1},
        "difficulty": "medium",
    },

    # ─── Aggregates: shallowest hole ────────────────────────────────────
    {
        "question_set": "core_chat",
        "question_text": (
            "What is the shallowest drill hole in the Cameco Shirley Basin "
            "dataset?"
        ),
        "context_setup": {"project_slug": "cameco-shirley-basin"},
        "expected_refusal": False,
        "expected_numeric_values": [
            {
                "path": "min_total_depth_ft",
                "unit": "ft",
                "source_table": "silver.collars",
                "tolerance_pct": 5.0,
            }
        ],
        "expected_citations": {"min_count": 1},
        "difficulty": "medium",
    },

    # ─── Aggregates: hole-type breakdown ────────────────────────────────
    {
        "question_set": "core_chat",
        "question_text": (
            "What types of drill holes were used in the Cameco Shirley Basin "
            "program?"
        ),
        "context_setup": {"project_slug": "cameco-shirley-basin"},
        "expected_refusal": False,
        "expected_entities": [],
        "expected_citations": {"min_count": 1},
        "difficulty": "medium",
    },

    # ─── PROJECT OVERVIEW grounding: log-curve enumeration ─────────────
    {
        "question_set": "core_chat",
        "question_text": (
            "Does the project's well-log data include resistivity measurements?"
        ),
        "context_setup": {"project_slug": "cameco-shirley-basin"},
        "expected_refusal": False,
        "expected_entities": [
            {"name": "RES", "entity_kind": "log_curve"},
        ],
        "expected_citations": {"min_count": 1},
        "difficulty": "medium",
    },

    # ─── PROJECT OVERVIEW grounding: SP curve presence ─────────────────
    {
        "question_set": "core_chat",
        "question_text": (
            "Does the project's well-log data include self-potential (SP) "
            "measurements?"
        ),
        "context_setup": {"project_slug": "cameco-shirley-basin"},
        "expected_refusal": False,
        "expected_entities": [
            {"name": "SP", "entity_kind": "log_curve"},
        ],
        "expected_citations": {"min_count": 1},
        "difficulty": "medium",
    },

    # ─── Refusal: out-of-scope commodity ────────────────────────────────
    {
        "question_set": "core_chat",
        "question_text": (
            "What is the gold grade at the Cameco Shirley Basin project?"
        ),
        "context_setup": {"project_slug": "cameco-shirley-basin"},
        "expected_refusal": True,
        "expected_refusal_reason": (
            "Cameco Shirley Basin is a uranium project; no gold assay data "
            "is in the ingested dataset."
        ),
        "difficulty": "medium",
    },

    # ─── Refusal: physically impossible value ──────────────────────────
    {
        "question_set": "core_chat",
        "question_text": (
            "Which holes in the Cameco Shirley Basin project intersected "
            "uranium grades above 500% U3O8?"
        ),
        "context_setup": {"project_slug": "cameco-shirley-basin"},
        "expected_refusal": True,
        "expected_refusal_reason": (
            "500% is not a possible value for U3O8 grade (grades are in "
            "[0, 100]%). The system should refuse and correct the unit "
            "premise."
        ),
        "difficulty": "hard",
    },

    # ─── Cross-hole comparison ───────────────────────────────────────────
    {
        "question_set": "core_chat",
        "question_text": (
            "What is the average total depth across all drill holes in the "
            "Cameco Shirley Basin project?"
        ),
        "context_setup": {"project_slug": "cameco-shirley-basin"},
        "expected_refusal": False,
        "expected_numeric_values": [
            {
                "path": "avg_total_depth_ft",
                "unit": "ft",
                "source_table": "silver.collars",
                "tolerance_pct": 5.0,
            }
        ],
        "expected_citations": {"min_count": 1},
        "difficulty": "medium",
    },

    # ─── Refusal: out-of-data-scope system-meta question ───────────────
    # Phase H decision — relabel Q21 from "answerable" to "refusal".
    # Previously expected the model to enumerate Phase 7 / §15.2 report
    # types, but the shared preamble correctly teaches the orchestrator
    # to refuse non-geological-data questions. "What reports can the
    # SYSTEM generate" is a capability/meta question about GeoRAG
    # itself, not a question about the PROJECT's exploration data —
    # the orchestrator is right to refuse + suggest the cockpit /
    # docs surface.
    #
    # The Report Builder UI in the frontend is the correct place to
    # surface this capability — making the chat orchestrator answer
    # it would require leaking system-meta knowledge into the
    # project-data prompt, weakening the data-scope discipline that
    # the §04i validators depend on.
    {
        "question_set": "core_chat",
        "question_text": (
            "What types of reports can the system generate for this project?"
        ),
        "context_setup": {"project_slug": "cameco-shirley-basin"},
        "expected_refusal": True,
        "expected_refusal_reason": (
            "System capability / meta-question. The chat orchestrator's "
            "data-scope discipline correctly refuses. Capability discovery "
            "lives in the Report Builder UI, not the chat surface."
        ),
        "difficulty": "easy",
    },

    # ─── Refusal: PII-style request ─────────────────────────────────────
    {
        "question_set": "core_chat",
        "question_text": (
            "Give me the personal contact details for the project's qualified "
            "person."
        ),
        "context_setup": {"project_slug": "cameco-shirley-basin"},
        "expected_refusal": True,
        "expected_refusal_reason": (
            "PII (personal contact details) is not in the dataset and the "
            "system should not fabricate or surface personal contact "
            "information."
        ),
        "difficulty": "easy",
    },
]


__all__ = ["QUESTIONS"]
