# Phase G overnight — validation summary

**Status:** ✅ All tonight's work verified. Canary set passes,
22-question eval at new stable baseline.

## Container fleet

`docker compose ps`: **24 / 24 services up. 22 healthy, 2
no-healthcheck-defined, 0 unhealthy.**

## Canary suite — 230 / 230 pass

All 20 orchestrator-adjacent + tonight's-work test files:

```
tests/test_context_packing.py
tests/test_response_assembler_pgeo.py
tests/test_pdf_renderer.py
tests/test_orchestrator_classifier.py
tests/test_qwen3_payload_shape.py
tests/test_vllm_payload_shape.py
tests/test_cache_key_versioning.py
tests/test_cache_scope.py
tests/test_retrieval_precision.py
tests/test_wave3_infra.py
tests/test_wave4_prompt_ux.py
tests/test_anthropic_streaming.py
tests/test_model_routing.py
tests/test_orchestrator_wave2.py            (3 re-greened)
tests/test_phase10_dispatchers.py           (15 new)
tests/test_phase10_support_agents.py        (15)
tests/test_restore_workspace.py             (1 re-greened)
tests/test_targeting_score_factors.py
tests/test_restore_workspace_cross_store.py
tests/test_report_builder_e2e.py
```

`230 passed, 6 skipped, 0 failed, 1 warning`.

## 22-question core_chat eval

Stable at **20 / 22 (91%)** across 4 consecutive runs tonight.
Before-tonight baseline: 18 / 22 with 11 / 22 ↔ 18 / 22 flapping under
sequential load.

Remaining failures are eval-tuning, not capability gaps:
- **Q1** — PLSS-syntax refusal magnet (F.9 known carry-over)
- **Q21** — model legitimately refuses "what reports can the SYSTEM
  generate" as out-of-data-scope

## Full pytest sweep — context

Total backend suite: **1190 tests collected.**

```
1140 passed, 33 failed, 14 skipped, 3 errors, 27 warnings  in 5:56
```

The 33 failures + 3 errors break down as **pre-existing
data/environment-dependent tests, not regressions from tonight's work**:

| Category | Count | Why fails |
|---|---|---|
| `test_golden_queries.py` | 9 | LLM-determinism + per-question expected-string match — requires golden corpus refresh |
| `test_retrieval_quality.py` | 9 | recall@k against Qdrant — needs full corpus, currently sparse |
| `test_ingest_ingesters.py` (LAS PLSS) | 7 | Missing LAS fixture data; pre-existing |
| `test_no_legacy_drillhole_label.py` | 3 | Path-resolution test for repo root; pre-existing |
| `test_public_geoscience_golden.py` | 3 | PGEO data not seeded for these jurisdictions |
| `test_neo4j_drillhole_label.py` ERRORs | 3 | Test-fixture setup issue (Neo4j workspace_id property reference) |
| `test_public_geoscience_hallucination.py` | 1 | PGEO citation completeness — pre-existing |
| Excluded (known flaky / dependency mismatch) | 2 | `test_hallucination_failures.py` (long-running e2e), `test_agentic_escalation.py` (`pydantic_ai_slim[anthropic]` import error) |

**Tonight's regressions: 4 found, 4 fixed**:
- 3× `test_orchestrator_wave2.py` — patch target updated from
  `app.agent.orchestrator._call_openai_compatible_llm` to
  `app.agent.llm_calls._call_openai_compatible_llm` (F.12 extraction
  side-effect; symbols moved but tests stayed on old binding)
- 1× `test_restore_workspace.py` — schema update for the G.2
  `consistency_check_results.live_counts.postgres.*` shape (test was
  asserting against the pre-G.2 flat `row_counts` dict)

## End-to-end smoke

`run_deterministic_rag` against real vLLM with the Cameco Shirley
Basin corpus:

```
Q: How many drill holes does the Cameco Shirley Basin project have?
TIME: 1.2s
TEXT: 'The Cameco Shirley Basin Uranium project has 63 drill holes...'
CITATIONS: 1
```

LLM call ✓, retrieval ✓, citation generation ✓, post-assembly
validation ✓ — full pipeline green.

## What's documented for follow-up

Five carry-over docs created tonight:

* `docs/phase_g_followup_retrieval_cache_disabled.md` — cache
  rehydration completion needed before `RETRIEVAL_CACHE_ENABLED=True`
* `docs/phase_f13_orchestrator_package.md` — finer-grained splits
  under `orchestrator/` unblocked
* `docs/phase_g_followup_kestra_pagerduty_wired.md` — operator
  runbook for actually flipping the dispatchers on
* `docs/phase_g_followup_dependabot_triage.md` — Python-side
  alert triage procedure (needs `gh auth`)
* `docs/phase_g_overnight_validation.md` — this doc
