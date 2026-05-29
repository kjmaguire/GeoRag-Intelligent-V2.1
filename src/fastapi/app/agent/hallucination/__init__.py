"""Hallucination prevention layers for the GeoRAG Pydantic AI agent.

Architecture reference: Section 04i — Hallucination Prevention (v1.49).

§04i v1.49 framing — 4 explicit guards
--------------------------------------
The §04i clause was consolidated from a 6-layer to a 4-guard framing in
the v1.10 doc edit. The file names below RETAIN the original layer
numbering for git-history continuity and import stability; the modules
themselves haven't moved. The conceptual mapping from current files to
the four guards is:

    Numeric grounding       layer3_numerical (every emitted integer / float
                            traces back to a tool-call result; ModelRetry on
                            ungrounded numbers).

    Entity grounding        layer4_entity (drill-hole IDs + quoted names
                            verified against silver.collars + Neo4j KG)
                            + qualitative_detector (keyword-driven entity
                            disambiguation).

    Citation completeness   layer1_retrieval (drops low-relevance chunks
                            before they reach the LLM)
                            + Pydantic typed-output validation (Layer 2 —
                            non-empty citations + source_chunk_id, enforced
                            by Pydantic AI before our validators)
                            + layer_completeness (positive coverage —
                            every claim has a citation).

    Refusal path            layer6_constraints (geological hard limits;
                            ModelRetry on implausible values)
                            + orchestrator_validators (uncertainty-trigger
                            enforcement; escalates to refuse-with-explanation
                            when guards fail N times in succession).

    layer5_provenance       Chunk provenance similarity check. Acts as a
                            sub-component of Numeric / Entity grounding
                            rather than an independent guard. Active once
                            Qdrant documents stabilise (Milestone 2+).

When §04i is referenced in code review or docs, prefer the 4-guard
vocabulary; treat the layerN_*.py file names as implementation detail.

Layers implemented here
-----------------------
Layer 1  layer1_retrieval.py   Retrieval quality gate — drops low-relevance
                               chunks from tool results before they reach the
                               LLM.  Applied inside tool functions, not as an
                               output validator.

Layer 3  layer3_numerical.py   Numerical claim verification — parses every
                               integer and float from the agent's response text
                               and traces each back to a tool call result.
                               Raises ModelRetry if any number is ungrounded.

Layer 4  layer4_entity.py      Entity resolution — extracts drill-hole IDs and
                               quoted names from the response text and verifies
                               them against PostGIS silver.collars and the Neo4j
                               knowledge graph.  Raises ModelRetry for unknown
                               entities.

Layer 6  layer6_constraints.py Geological constraint rules — applies SME-defined
                               hard limits (max depth, grade, recovery, etc.) to
                               any numerical value that appears near a geological
                               keyword.  Raises ModelRetry for implausible values.

Layers 2 and 5 — drift corrected (Phase 12 Step 1)
---------------------------------------------------
Earlier revisions of this docstring claimed Layers 2 + 5 were
"handled elsewhere" / "not implemented." The Phase 11 §04i audit
(``docs/phase11_section_04i_audit.md``) caught the drift — both
files exist with real implementations:

Layer 2  layer2_typed_output.py (128 lines) — post-assembly validator
         that runs AFTER ``response_assembler`` builds the
         ``GeoRAGResponse``. Beyond Pydantic AI's schema validation:
         (1) every ``[DATA-N]`` / ``[NI43-N]`` marker in LLM text has a
             matching ``Citation`` object,
         (2) no ``Citation`` has an empty ``source_chunk_id``,
         (3) ``text`` is not empty / pure-refusal-without-grounding,
         (4) ``confidence`` ∈ [0.0, 1.0].

Layer 5  layer5_provenance.py (157 lines) — enrichment, not a gate.
         For each ``Citation``, walks the chain::

             Citation.source_chunk_id
               → silver table (collars / lithology_logs / reports / samples)
                 → bronze.source_files (file_path, sha256, bucket)

         The Qdrant-similarity variant of Layer 5 still awaits
         populated Qdrant documents (Milestone 2+); the provenance
         enrichment path is live today.

Import pattern
--------------
geo_agent.py imports the three validator functions and registers them via the
``@geo_agent.output_validator`` decorator:

    from app.agent.hallucination import (
        verify_numerical_claims,
        resolve_entity_references,
        check_geological_constraints,
    )
"""

from app.agent.hallucination.layer3_numerical import verify_numerical_claims
from app.agent.hallucination.layer4_entity import resolve_entity_references
from app.agent.hallucination.layer6_constraints import check_geological_constraints

__all__ = [
    "verify_numerical_claims",
    "resolve_entity_references",
    "check_geological_constraints",
]
