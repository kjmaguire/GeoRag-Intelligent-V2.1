# Master-plan §9 (Geological Reasoning + Decision Intelligence) — Scope Proposal

**Doc-phase 89** — fifth scope proposal in the §5→§6→§7→§8→§9 sequence.

---

## What §9 ships

"The differentiating intelligence layers." Per §20 intro, this is "the
most original idea in the architecture and the most defensible
differentiator against generic-RAG competitors."

Master-plan Phase 9 deliverables (verbatim):
1. Geological ontology populated (§20.1)
2. Competing hypothesis engine (§20.3) integrated into Answer Graph
3. Spatial geological relationship engine (§20.4) live
4. Next-best-data recommendations (§20.5) on Workspace Health Dashboard
5. Analogue finder (§20.6)
6. Decision Intelligence Layer schema (§21.1, §21.2) populated
7. All eight tracked decision types (§21.3) capturing decisions
8. Field feedback loop (§21.4) wired
9. Data lineage graph UI (§21.6)
10. "What Changed" intelligence (§21.7)

**Done test:** a chat session with a geologist surfaces competing
hypotheses, the geologist accepts one, the decision is recorded in
`decision_records`, and a "What Changed" report a week later
includes that decision in the workspace narrative.

---

## Why §9 is fundamentally different from §5-§8

§5-§8 are mostly **engineering** phases — schemas, agents,
graphs, renderers, dashboards. The scaffolding pattern (skeleton +
contract) maps cleanly.

§9 is **content + science** at the core:
- Geological ontology = curated taxonomy (§20.1 lists 11 ontology
  classes; each needs canonical terms + synonyms — typically 50-200
  entries each).
- Deposit model attributes (the §8.3 SME pass) feed §9's deposit-model
  intelligence directly.
- Competing hypothesis engine = LLM prompts + claim-ledger integration
  + uncertainty math.
- Decision Intelligence Layer is closer to engineering, BUT the eight
  decision types span the whole product — each requires a hook into
  an existing flow (target sign-off, CRS resolution, schema mapping,
  etc.).

So §9 is part **autonomous-safe engineering** (decision-records schema,
"What Changed" delta workflow, hypothesis schema, lineage UI scaffold),
part **Kyle SME** (ontology population, deposit-model attributes,
analogue list curation).

---

## Sub-step breakdown estimate

| # | What | Backend | Frontend | SME | Ticks |
|---|---|---|---|---|---|
| 9.1 | `geological_ontology_terms` + `_synonyms` schema | small | none | none | 1 |
| 9.2 | Ontology seed loader (skeletons for 11 classes) | small | none | none | 1 |
| 9.3 | Ontology SME population — 11 classes × 50-200 terms | none | none | **heavy** | (Kyle, async) |
| 9.4 | Hypotheses schema (`hypotheses` + evidence linkers) | small | none | none | 1 |
| 9.5 | Hypothesis generation agent + integration into Answer Graph | medium | none | none | 2-3 |
| 9.6 | Spatial geological relationship engine — Cypher + PostGIS queries | medium | none | small | 2 |
| 9.7 | Next-best-data recommendations agent + JSON menu | medium | small | medium | 2 |
| 9.8 | Analogue finder — Qdrant + Neo4j combined ranker | medium | small | small | 2 |
| 9.9 | `decision_records` + 4 related tables | small | none | none | 1-2 |
| 9.10 | Decision-capture hooks into 8 decision types | medium | none | none | 2-3 |
| 9.11 | `field_outcome_learning` Hatchet workflow | small | none | none | 1 |
| 9.12 | Data lineage graph UI (React Flow component) | small (API) | medium | none | 2 |
| 9.13 | "What Changed" report (already templated in §7.2) — wire the delta detection | medium | none | none | 2 |
| 9.14 | Acceptance test: chat → hypothesis → decision → What Changed | mixed | mixed | small | 2 |

**Total: 21-26 ticks** (excluding §9.3 ontology population which Kyle
owns and runs async).

Frontend skew: ~25% frontend (lineage graph UI, hypothesis surface in
chat, next-best-data on Workspace Health Dashboard). §9 is mostly
backend + content.

---

## V1.49 / current baseline overlap

What exists:
- **Answer Graph** — already runs at every chat retrieval. Adding a
  hypothesis-generation node is an extension, not a new graph.
- **Audit ledger + hash chain** — §21.6 lineage UI reads from
  `audit_ledger`; the chain already covers most of the graph.
- **Citation + claim ledger** — every chat answer already tags
  passages by chunk_id. Hypotheses can hang off this.
- **Qdrant** — analogue finder embedding similarity infrastructure.
- **Neo4j** — analogue finder graph traversal infrastructure.
- **`workflow_runs` table** — `field_outcome_learning` follows
  established Hatchet pattern.
- **§7 What Changed template** — already in `templates.py`
  (doc-phase 82). §9.13 wires the delta detection that feeds it.

What's new:
- **Geological ontology tables** + content (the big SME ask).
- **Hypotheses tables** + competing-hypothesis logic.
- **Decision Intelligence Layer** — 5 new tables + 8 capture hooks.
- **Next-best-data menu + cost/time/uncertainty estimates** — 14
  recommendation types per §20.5.
- **Data lineage graph UI** — frontend.

---

## Risks

1. **Ontology population (§9.3) is heavy SME work.** Estimate: 11
   classes × 50-200 terms = 550-2200 ontology entries. Each needs
   canonical term + synonyms + BGS-style mapping where applicable.
   Realistically a multi-week external contractor pass. Tracks
   independently from autonomous run.
2. **Hypothesis quality.** LLM-generated competing hypotheses can
   hallucinate; the supporting/contradicting evidence ledger MUST
   trace to real passages via existing citation contract.
3. **Decision capture sprawl.** 8 decision types × hooks into existing
   flows = 8 separate integration points. Risk of "scattered code"
   anti-pattern. Mitigation: single `app.services.decision_intelligence`
   facade with one `record_decision()` function called from each hook.
4. **Lineage UI complexity.** React Flow graphs can be heavy. Need
   pagination + lazy-load for workspaces with >10k audit_ledger rows.
5. **Activepieces dependency for "What Changed" cadence** — same
   gate as §7.11 + §8.11.

---

## Dependencies

- **Existing Answer Graph** — extension only; no new langgraph
  installation needed.
- **`react-flow`** for §9.12 lineage UI. Already on the frontend?
  Verify in `package.json` at §9.12 start.
- **`shap`** — Phase 12 only. Skip.

---

## Open questions for Kyle

1. **§9.3 ontology population — who owns it?** Master plan calls for
   11 classes × 50-200 terms. Realistic v1 = a 100-term-per-class
   seed (1100 entries total). External contractor or internal SME?
2. **Hypothesis surface in chat** — show always, or only on request?
   §20.3 implies always when relevant; UX may want toggle.
3. **Decision capture UX** — modal at decision time, or background
   capture (no friction)? Master plan doesn't say. Suggest background
   for `target_recommendation` + `report_signoff`; modal for
   `crs_decision` + `schema_mapping` where ambiguity matters.
4. **§9.13 "What Changed" cadence** — daily, weekly, on-demand only?
   Suggest weekly default + on-demand override.

---

## Recommendation

§9 autonomous-safe slice (backend skeleton + content-free schemas):
- **§9.1** ontology schema (2 tables)
- **§9.2** ontology seed loader (empty templates for 11 classes)
- **§9.4** hypotheses schema
- **§9.5** hypothesis agent skeleton
- **§9.6** spatial relationship engine — Cypher templates with
  parameter slots
- **§9.7** next-best-data menu (14 types per §20.5 with placeholder
  cost/time estimates)
- **§9.8** analogue finder skeleton
- **§9.9** decision intelligence schema (5 tables)
- **§9.10** decision-capture facade skeleton
- **§9.11** `field_outcome_learning` Hatchet workflow skeleton
- **§9.13** "What Changed" delta detector skeleton (reads existing
  templates from doc-phase 82)

That gets §9 to roughly the same skeleton-scaffold state §7 + §8
reached. The full §9 phase needs:
- Kyle SME ontology population (§9.3 — async, multi-week)
- Frontend pass for §9.12 lineage UI (waits for Kyle)
- Image rebuild (langgraph for hypothesis agent wiring)

---

## TL;DR

§9 = differentiating intelligence layers. 21-26 ticks (excluding
ontology population, which is multi-week Kyle/contractor work).
Backend skeleton work is autonomous-safe; ontology content + lineage
UI need Kyle. Reuses §7 What Changed template + §8 target outcomes
table + existing Answer Graph + audit ledger.

Autonomous run next ticks: doc-phase 90 = §9.1 + §9.2 (ontology
schema + seed loader). Doc-phase 91 = §9.4 + §9.5 (hypothesis schema
+ agent skeleton). Doc-phase 92 = §9.9 + §9.10 (decision intelligence
schema + facade). Doc-phase 93 = §9.11 + §9.13 (Hatchet workflows).
Doc-phase 94 = §9.6 + §9.7 + §9.8 (spatial + next-best-data + analogue
finder skeletons).

After §9: §10 (eval + cockpit), §11 (DR + perf), §12 (XGBoost).
