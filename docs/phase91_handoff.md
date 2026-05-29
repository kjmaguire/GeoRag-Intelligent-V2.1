## Doc-phase 91 handoff — §9.4 + §9.5 hypotheses schema + agent

**Status:** Complete. 2 tables live + agent skeleton imports clean.

## What landed

### §9.4 — hypotheses schema

`database/migrations/2026_05_13_120000_create_silver_hypotheses.php`:
- `silver.hypotheses` — hypothesis_id + workspace_id + parent_question
  + label ("A"/"B"/"C"/etc.) + description + confidence + review_status
  ("ai_suggested"/"reviewed"/"accepted"/"rejected") + rationale.
- `silver.hypothesis_evidence_links` — link_id + hypothesis_id +
  source_chunk_id + role ("supporting"/"contradicting"/"missing"/
  "recommended_test") + weight + payload.

CHECK constraints:
- `review_status ∈ {ai_suggested, reviewed, accepted, rejected}`
- `role ∈ {supporting, contradicting, missing, recommended_test}`
- `confidence ∈ [0, 1]`, `weight ∈ [0, 1]`
- `label ~ '^[A-Z][A-Z0-9]?$'` (A-Z, with optional digit suffix)
- `missing`/`recommended_test` roles MUST have source_chunk_id NULL
  (those rows represent evidence-gaps, not actual chunks).

RLS via `app.workspace_id`; evidence_links scopes through EXISTS on
parent hypotheses (same pattern as §8.1's target_score_factors).

Applied via superuser; migration row recorded.

### §9.5 — Hypothesis Generator Agent

`src/fastapi/app/agents/phase9/`:
- `hypothesis_generator.py` — `@georag_agent`-decorated R2 agent.
  Takes workspace_id + parent_question + candidate_evidence_chunk_ids;
  returns hypotheses[] with per-hypothesis evidence categorization
  (supporting/contradicting/missing) + recommended_tests +
  confidence + confidence_method.
- Skeleton (raises NotImplementedError). Lands as an Answer Graph
  EXTENSION node (not a new graph) — fires only when classifier
  flags the question as interpretive.

## Master-plan §9 progress

| Sub-step | Status |
|---|---|
| 9.0 scope proposal | ✅ |
| 9.1 ontology schema | ✅ |
| 9.2 ontology seed loader | ✅ |
| 9.3 SME ontology population | pending (Kyle/contractor) |
| 9.4 hypotheses schema | ✅ |
| 9.5 hypothesis agent | ✅ skeleton |
| 9.6 spatial relationship engine | pending |
| 9.7 next-best-data recommendations | pending |
| 9.8 analogue finder | pending |
| 9.9 decision intelligence schema | pending |
| 9.10 decision capture hooks | pending |
| 9.11 field_outcome_learning workflow | pending |
| 9.12 data lineage graph UI | pending (frontend) |
| 9.13 What Changed delta detection | pending |
| 9.14 acceptance test | pending |

**5 of 14 §9 sub-steps closed.**

## Recommended next tick

Doc-phase 92 = §9.9 (decision intelligence schema) + §9.10 (capture
facade skeleton). 5-table migration + facade module. Pattern matches
§8.1 (10-table migration).

## Carry-overs

Same blockers as prior §9 ticks:
- Kyle SME ontology population (§9.3)
- Image rebuild (langgraph for Answer Graph extension node)
- Activepieces install status

Plus new for §9:
- Answer Graph extension point still TBD — the chat retrieval
  pipeline's classifier needs an "interpretive question" branch.
