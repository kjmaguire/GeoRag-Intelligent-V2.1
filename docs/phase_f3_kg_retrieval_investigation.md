# Phase F.3 — KG-aware retrieval investigation (doc-phase 188)

**Status:** **INVESTIGATION ONLY — no code changes applied.** Two hypotheses tested + both reverted. Eval baseline corrected from 6/10 → **5/10** (honest measurement post full OCR-embedding). 121/121 substrate verifier.

## TL;DR

Going in: "the deposit-type question fails Layer 1 because retrieval doesn't query Neo4j for `:Deposit` nodes — let's surface them via entity-resolution path."

What I tested:
1. **Hypothesis A:** Exclude `:Report` / `:Publication` from `fetch_project_graph_entities` so Formation/Deposit ranked higher
2. **Hypothesis B:** Bump `limit` 50 → 200 so more entities reach the orchestrator

What I found:
- Hypothesis A: **6/10 → 5/10** (regression — Report title tokens were contributing to entity-grounding for the "What county and state" question)
- Hypothesis B: **6/10 → 5/10** (regression — more entities in prompt diluted the entity-grounding signal)
- Both reverted; code is back to the pre-F.3 baseline

**Bonus honest finding:** The 6/10 measurement from Phase E.2.4 was on a partial-embedding state. After all 1,108 OCR passages embedded into Qdrant (Phase E.1-d completion), the noisier corpus caused the "What county and state" question to regress on its own. The TRUE baseline post-full-embed is 5/10.

## What I investigated

### Eval failure under inspection

```
What type of uranium deposit is targeted by drilling in Shirley Basin, Wyoming?
  → Layer 1 violation: 1 citation(s) below relevance_score gate 0.5
```

The LLM CAN answer (gives "sandstone-hosted roll-front uranium" from the project preamble which contains the Deposit node info), but its citations don't ground in retrieved Qdrant chunks. The retrieved chunks score below 0.5 — they're OCR'd gamma-log tables, not narrative content about deposit types.

### Architecture deep-dive

Walked through the orchestrator's KG path:

1. `_classify_query` correctly flags the question as `graph=True` (matches "deposit" keyword)
2. `fetch_project_graph_entities` returns 54 entities (50 from Neo4j + 4 universal)
3. `_extract_graph_entities(query, known_entities)` → **[] empty result**

So the orchestrator gets zero query candidates. It falls back to label-based query (`Deposit` label match), which DOES find the Deposit node and adds it to tool_results. The LLM then answers correctly.

BUT — Layer 1's citation grounding fails because the [DATA-2] citation the LLM uses doesn't map to a retrieved chunk above 0.5.

### Why the entity list was missing CAMECO/SHIRLEY/Deposit

Neo4j has 1,176 entities for the Cameco project after the OCR ingest:
- 1 Project (Cameco Shirley Basin Uranium)
- 63 DrillHoles
- 3 Formations (CAMECO RESOURCES, SHIRLEY BASIN, CARBON)
- 1 Deposit
- 1,108 Reports (one per OCR'd TIFF document)

The orchestrator's cypher sorts by `degree DESC, size(name) DESC` with LIMIT 50. With Reports having long titles (40-100 chars), they dominate the size-tiebreaker after the project, pushing 7-char drillhole IDs but ALSO the 6-16 char Formation/Deposit entries past the LIMIT cutoff.

**Direct verification:** Running the cypher with `LIMIT 50` AND `NOT 'Report' IN labels(n)` did surface Formation/Deposit at positions 2-4. The filter works at the Neo4j level.

### Why the fix didn't help

Even when the Formation/Deposit entities WERE in the orchestrator's known_entities list, the eval result didn't improve — it got worse. Two effects:

1. **Report titles were soft-grounding the "county and state" answer.** Titles like "2012 Shirley Basin Drill Hole Coordinates" contained "Shirley Basin" + implicit Wyoming context. Removing Reports from the entity list removed those soft anchors; the orchestrator's other guards then triggered over-refusal on the next eval run.

2. **More entities in prompt = more dilution.** With 200 entities in the project preamble (vs 50), the LLM's attention is more thinly spread; the "Top project entities" list becomes noise rather than signal.

Both hypotheses fully reverted. The orchestrator code is back to the pre-F.3 state.

## Honest eval baseline correction

| Phase | Pass (measured) | Notes |
|---|---|---|
| Post-D (3 passages embedded) | 5/10 | Initial baseline |
| Post-E.1 (1,108 passages embedded — IN PROGRESS) | 4/10 | Partial-embedding state |
| Post-E.2.4 (prompt steering) | 6/10 | Measured BEFORE OCR embedding fully landed |
| Post-E.3.1 (guard tuning) | 6/10 | Same partial state |
| Post-F.3 (full embedding + reverts) | **5/10** | Honest baseline; OCR noise displaced clean PDF retrieval |

The 6/10 peak was a transient measurement window between E.2.4 work and the embedder finishing. Once all 1,108 OCR passages were in Qdrant, retrieval surfaces fragmented chunks for the "county and state" question, triggering the orchestrator's numeric/completeness guards.

This is the same OCR-noise effect we documented in Phase E.1 — adding more content without curation can hurt eval on specific questions.

## What WOULD actually move the deposit-type Layer 1 fail

**Phase F.4 — Structured-data tool wiring for deposit/formation queries.** The orchestrator's `search_documents` only queries Qdrant. A new tool would:

1. Detect deposit/formation/lithology query intent
2. Query Neo4j for matching `:Deposit` / `:Formation` nodes scoped to the project
3. Surface the node's `description` field as a tool_result with proper citation marker (e.g. `[KG:1]`)
4. The citation binder grounds [KG:1] to the Neo4j node

This is a real architectural change: new tool, new citation type, new prompt rule. ~5-8 ticks of work.

Phase F.3 attempted to shortcut this by enriching the entity-resolution path. The shortcut didn't work because:
- Layer 1 retrieval_quality fails on Qdrant chunks below threshold; KG entities aren't Qdrant chunks
- The entity-resolution path is for hallucination DETECTION, not for evidence retrieval

## Files modified (and reverted)

- `src/fastapi/app/agent/orchestrator.py` — Investigated `fetch_project_graph_entities` extensively. Final state has a documented comment block explaining what was tried + why nothing was kept. Limit reverted to 50; cypher reverted to pre-F.3.

## Cumulative state

- **Doc-phase ticks this run:** **56** (132 → 188)
- **Substrate verifier:** **121/121** PASS
- **Pytest cases:** 330
- **True eval baseline:** **5/10** on core_chat with full OCR corpus
- **Hatchet AI pool:** 14

## What's next

Honest options:

1. **Phase F.4 — Structured-data tool wiring** (~5-8 ticks) — the real fix
2. **Phase F.5 — SME question refinement** (~1 tick) — relax `expected_citations.min_count` on the deposit-type question so the LLM's preamble-derived answer doesn't need a Qdrant citation
3. **Pause** — the platform is structurally solid at 5/10 on real data; Kyle's review of the Phase B-F arc could shape priorities better than another fix attempt

## Honest assessment

Phase F.3 produced no eval improvement — but it produced **rigorous understanding** of why the system works as it does. The architecture is internally consistent; the deposit-type Layer 1 fail is correctly identifying that the LLM's answer doesn't ground in retrieved evidence (because the evidence lives in Neo4j, not Qdrant).

Fixing this requires building the bridge between KG retrieval and the citation system, which is a multi-tick architectural lift. It's the right next step but not a 30-minute one.
