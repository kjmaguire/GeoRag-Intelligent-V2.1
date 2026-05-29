## Doc-phase 93 handoff — §9.6 + §9.7 + §9.8 remaining reasoning agents

**Status:** Complete. 3 new agent skeletons; all 4 §9 agents now callable.

## What landed

Three new files under `app/agents/phase9/`:

| Agent | File | §20.x role | Risk |
|---|---|---|---|
| Spatial Relationship | `spatial_relationship.py` | §20.4 PostGIS+Neo4j relationship queries | R1 |
| Next-Best-Data | `next_best_data.py` | §20.5 14-kind action menu | R1 |
| Analogue Finder | `analogue_finder.py` | §20.6 Qdrant+Neo4j combined ranker | R1 |

Updated `__init__.py` to re-export all 4 §9 agents
(hypothesis_generator from doc-phase 91 + these 3).

### Notable design notes

- `next_best_data.py` exports a `NEXT_BEST_DATA_KINDS` tuple of 14
  controlled-vocabulary values mapped 1:1 to §20.5. Cost/time
  estimates left as future SME input (placeholders in the output
  contract). Pending Kyle for project-realistic ranges.
- `analogue_finder` depends on §9.3 SME ontology + populated
  `targeting.target_models.analogues_payload` to do anything
  meaningful. Skeleton flags this in docstring.
- `spatial_relationship` returns predicate triples
  ({subject, predicate, object}); predicate vocabulary deliberately
  open ("crosscuts", "hosts", "overprints", "is_in", etc.). Locking
  the vocabulary is a §9.6 v2 concern.

### Smoke test

    docker exec georag-fastapi python -c "
        from app.agents import phase9
        print(len(phase9.__all__))  # 4
    "

## Master-plan §9 progress

| Sub-step | Status |
|---|---|
| 9.0 scope | ✅ |
| 9.1 ontology schema | ✅ |
| 9.2 ontology seeds | ✅ |
| 9.3 SME ontology population | pending (Kyle) |
| 9.4 hypotheses schema | ✅ |
| 9.5 hypothesis agent | ✅ skeleton |
| 9.6 spatial relationship | ✅ skeleton |
| 9.7 next-best-data | ✅ skeleton |
| 9.8 analogue finder | ✅ skeleton |
| 9.9 decision intelligence schema | ✅ |
| 9.10 decision capture facade | ✅ skeleton |
| 9.11 field_outcome_learning workflow | pending |
| 9.12 data lineage graph UI | pending (frontend) |
| 9.13 What Changed delta detection | pending |
| 9.14 acceptance test | pending |

**10 of 14 §9 sub-steps closed** (71%). Remaining ticks: §9.11
Hatchet workflow (autonomous-safe), §9.13 delta detector
(autonomous-safe), §9.12 frontend (Kyle), §9.14 acceptance
(post-graduation).

## Recommended next tick

Doc-phase 94 = §9.11 (`field_outcome_learning` Hatchet workflow
skeleton) + §9.13 (What Changed delta detector skeleton). Two final
autonomous-safe §9 backend ticks. After that, §9 backend is fully
scaffolded.

Then §10 (Eval harness + Customer Support Cockpit) scope proposal.

## Carry-overs

Same as prior §9 ticks. The 4 §9 agent skeletons collectively block
on:
1. §9.3 SME ontology population
2. Answer Graph extension point (Hypothesis Generator wiring)
3. Image rebuild (langgraph for Answer Graph extension)
4. `targeting.target_models.analogues_payload` content
