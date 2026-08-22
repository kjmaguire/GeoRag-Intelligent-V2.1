"""Hallucination prevention layers for the GeoRAG agent.

Architecture reference: Section 04i — Hallucination Prevention (v1.49).

WHAT ACTUALLY RUNS
------------------
Read this before changing anything in this package.

    orchestrator_validators.py   The canonical implementation and the only
                                 post-assembly validation path. Holds
                                 ``verify_numbers`` (numeric grounding),
                                 ``verify_entities`` (entity resolution),
                                 ``verify_constraints`` (geological limits),
                                 ``verify_completeness`` (per-claim citation
                                 coverage), ``guard_tolerances`` and
                                 ``run_post_assembly_validation``, which
                                 agentic_retrieval/nodes.py calls.

    layer2_typed_output.py       ``validate_and_repair`` — post-assembly
                                 typed-output repair, called from
                                 agentic_retrieval/nodes.py.

    layer5_provenance.py         ``enrich_provenance`` — citation provenance
                                 enrichment, called from
                                 agentic_retrieval/nodes.py. Enrichment, not
                                 a gate.

    layer6_constraints.py        ``_find_violations`` — imported by
                                 orchestrator_validators.verify_constraints.
                                 NOTE: the module's public entry point,
                                 ``check_geological_constraints``, is reached
                                 only by the re-export below and by tests;
                                 the live path goes through
                                 ``_find_violations``.

    citation_markers.py          Shared marker regexes. Widely used.

    qualitative_detector.py      Keyword-driven entity disambiguation.

§04i v1.49 framing — 4 explicit guards
--------------------------------------
The §04i clause was consolidated from a 6-layer to a 4-guard framing in the
v1.10 doc edit. The surviving file names retain the original layer numbering
for git-history continuity. The mapping from current files to the four guards
is:

    Numeric grounding       orchestrator_validators.verify_numbers — every
                            emitted integer / float traces back to a
                            tool-call result.

    Entity grounding        orchestrator_validators.verify_entities —
                            drill-hole IDs and quoted names verified against
                            silver.collars + the Neo4j KG, plus
                            qualitative_detector for disambiguation.

    Citation completeness   layer2_typed_output (every marker has a matching
                            Citation, no empty source_chunk_id) plus
                            orchestrator_validators.verify_completeness
                            (positive coverage — every declarative sentence
                            carries a marker).

    Refusal path            orchestrator_validators.verify_constraints over
                            layer6_constraints._find_violations (geological
                            hard limits), feeding the should_retry /
                            confidence-floor decision in
                            agentic_retrieval/nodes.py.

    layer5_provenance       Chunk provenance enrichment. A sub-component of
                            numeric / entity grounding rather than an
                            independent guard.

When §04i is referenced in code review or docs, prefer the 4-guard
vocabulary; treat the layerN_*.py file names as implementation detail.

Deleted 2026-08-21 — a parallel implementation that never ran
-------------------------------------------------------------
``layer1_retrieval.py``, ``layer3_numerical.py``, ``layer4_entity.py`` and
``layer_completeness.py`` (1,287 lines) were removed, along with
``app/services/refusal_builder.py`` (523 lines) which only they could reach.

They were built for Pydantic AI's ``@agent.output_validator`` decorator
pattern, against a ``geo_agent.py`` that does not exist and never did. The
orchestrator went a different way, so these modules sat beside
orchestrator_validators.py looking like safety controls in force while only
orchestrator_validators ever executed — with a full, green test suite
confirming behaviour nothing depended on. ``evaluate_guards``, the six-layer
guard evaluator CLAUDE.md hard rule 5 names, was among them and had no
production caller.

The only logic unique to them was the completeness guard and the per-query-
class guard-tolerance model; both were ported into orchestrator_validators
(``verify_completeness`` and ``guard_tolerances``). The completeness guard
now runs on the live path for the first time, advisory-only — see the
tolerance note in ``run_post_assembly_validation`` for why it does not yet
trigger retries, and why the numeric/entity tolerance knobs remain
deliberately unapplied.

``geo_agent.py`` DOES NOT EXIST
-------------------------------
Several modules in this package used to say the validators were "registered
in geo_agent.py with ``@geo_agent.output_validator``". There is no
geo_agent.py anywhere under src/fastapi and there is no output_validator
registration. If you find another docstring saying otherwise, it is
describing an architecture that was never built.
"""

from app.agent.hallucination.layer6_constraints import check_geological_constraints

__all__ = [
    "check_geological_constraints",
]
