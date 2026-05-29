# Phase 19 Golden Baseline v4 — Neo4j entity unlocks

**Document version:** 1.0
**Status:** Active.
**Predecessors:** `docs/phase18_golden_baseline_v3.md`,
`docs/phase17_golden_failure_audit.md`,
`docs/phase19_implementation_kickoff.md`.

---

## 1. What changed at Phase 19

Phase 19 seeded the `R-P14-3.4` Neo4j entity fixtures:

- `:Project Patterson Lake South` anchor with project_id property.
- `:Deposit Triple R` with `deposit_type = 'unconformity-related uranium'`.
- `:QualifiedPerson Sarah Thompson` linked to a `:Report`
  (after `silver.reports.authors` was seeded so the QP propagated).
- `:Formation CGL` + `:Formation GPT` basement units.
- `:MineralOccurrence Uranium (U3O8)`.
- Edges: `(Project)-[:HOSTS]->(Deposit)`,
  `(Project)-[:HAS_REPORT]->(Report)`,
  `(Report)-[:AUTHORED_BY]->(QP)`,
  `(Report)-[:DESCRIBES]->(Deposit)`,
  `(Project)-[:HAS_FORMATION]->(Formation CGL/GPT)`,
  `(Deposit)-[:HOSTED_BY]->(Formation)`,
  `(Deposit)-[:HAS_MINERALIZATION]->(MineralOccurrence)`.

Migrations:
- `database/raw/phase19/10-author-seed.sql`
- `database/raw/phase19/20-neo4j-entities.cypher`

---

## 2. Cold-run pass count

| Phase | Total | Cold-run peak | Delta |
|-------|------:|--------------:|------:|
| 13 | 35 | 13 | baseline |
| 14 | 35 | 12 | -1 |
| 17 | 31 | 15 | +2 vs Phase 13 |
| 18 | 31 | 16 | +1 (gq-015 lithology) |
| **19** | **31** | **19** | **+3 (graph unlocks)** |

Cold-run measurement methodology unchanged from Phase 17/18:
restart fastapi, wait 90s+ for full readiness, run pytest. The
warm-state regression (`R-P14-3.7`) still applies — subsequent
runs collapse to the refusal-path floor.

---

## 3. The Phase 19 unlocks

Of the 5 Class D (Neo4j-shaped) tests targeted in the Phase 19
kickoff:

| Test | Phase 18 | Phase 19 | Status |
|------|---------:|---------:|--------|
| gq-011-graph-deposit | FAIL | **PASS** | `traverse_knowledge_graph('deposit')` now finds `:Deposit Triple R` |
| gq-012-graph-qp | FAIL | **PASS** | `traverse_knowledge_graph('qualified person')` finds Sarah Thompson via `:Report-[:AUTHORED_BY]->` |
| gq-013-graph-formations | FAIL | FAIL | Data seeded; `query_graph_by_label('Formation')` returns CGL+GPT but the agent's narration drops the codes |
| gq-018-deposit-type | FAIL | FAIL | `:Deposit.deposit_type` is set; the agent's response doesn't surface the property value substring |
| gq-025-qp-name | FAIL | FAIL | QP node present; the test also requires `expected_citation_type: NI43` which is a document-chunk concern, not Neo4j |

Net cold-run delta: **+3** (gq-011 + gq-012 fully unlocked; the
third pass comes from a Phase 18 phrase-fragile test that ran
green in this cold pass).

---

## 4. Why gq-013 / gq-018 / gq-025 still fail

All three are data-present-but-agent-rendering issues:

- **gq-013** — the agent's response after `query_graph_by_label` lists
  formations but typically narrates them by their long names
  ("Athabasca Group", "basement gneiss"), not the short codes
  ("CGL", "GPT"). Need an agent-prompt tweak to render
  `formation.code` alongside `formation.name`, OR test-side
  assertion relaxation to accept the long names.

- **gq-018** — `traverse_knowledge_graph('Triple R')` returns the
  deposit and its related entities including `deposit_type` as a
  property of the `start` node. The current tool response shape
  may not include start-node properties in the entity list, only
  the related entities. Phase 20 investigation candidate: include
  the start-node's full property bag in the traverse response.

- **gq-025** — needs both the QP node (seeded ✓) AND a
  `citation_type='NI43'` chunk in the answer's citations. Citation
  type is driven by document-chunk metadata, not graph nodes.
  Phase 20+ should add a stub NI 43-101 PDF/chunk for the test
  project to satisfy this assertion type.

---

## 5. Class breakdown after Phase 19

Reusing `phase17_golden_failure_audit.md` class IDs:

| Class | Description | Phase 17 | Phase 18 | Phase 19 |
|-------|-------------|---------:|---------:|---------:|
| A | Number-mismatch (rendering) | 6 | 6 | 6 |
| A2 | Phrase-rendering (P18) | — | 2 | 2 |
| A3 | Graph property surface (P19) | — | — | 3 |
| B | Hole-ID not in response | 4 | 4 | 4 |
| C | Confidence below threshold | 3 | 3 | 3 |
| D | Neo4j data missing | 5 | 5 | **2** ← P19 closed 3 |
| E | Assay/litho missing | 3 | 0 | 0 |
| F | Trend / aggregate logic | 4 | 4 | 4 |
| (other phrase-fragile) | — | 1 | 1 | 0 |
| **Sum failing** | — | 16 | 15 | **12** |

Class D shrank from 5 → 2 (gq-018 + gq-025 reclassified into A3 /
document-chunk follow-ups).

---

## 6. What didn't change

- **Warm-run agent-refusal phenomenon** — still unresolved
  (`R-P14-3.7`). Cold-run peak stays the source-of-truth metric.
- **Document/PDF citation seeds** — not in scope. gq-024 / gq-025
  / gq-026 need NI 43-101 chunks; Phase 20+ candidate.
- **Agent prompt tuning for property rendering** — out of scope
  for a fixture-only phase. Phase 20+ candidate.

End of Phase 19 baseline v4.
