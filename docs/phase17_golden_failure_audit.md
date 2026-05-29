# Phase 17 Step 1 — Golden-test failure audit

**Document version:** 1.0
**Status:** Snapshot at Phase 17 open (1 / 31 passing in
`test_golden_queries.py`).
**Predecessor:** `docs/phase11_golden_baseline.md`,
`docs/phase14_r-p13-1_scoping.md`.

Captures every Milestone-1 golden-test failure + its category +
the proposed Phase 17+ remediation. Used to decide what Phase 17
Step 2 can unlock vs. what defers to follow-on phases.

---

## 1. Failure classes

### A. Number-mismatch (fixture content doesn't match test's documented ground truth)

| Test | Expected | Got | Fix needed |
|------|----------|-----|------------|
| gq-001-count-holes | "20" | "10 drill holes" | Add 10 XLS-24-* collars |
| gq-006-completed-holes | "9" | (refusal / 0.1 conf) | Agent should compute, may need MV column |
| gq-007-average-depth | "360.8" | "348.0" | Recompute depths to sum 7216m across 20 holes |
| gq-009-holes-in-2022 | "3" | (refusal) | Should match — needs LLM to dispatch year-aware query |
| gq-010-specific-hole-depth | "510" | (refusal) | PLS-22-08 retrieval path needs tool dispatch |

**Phase 17 Step 2 unlocks:** gq-001, gq-007 directly via fixture
extension. Others depend on agent tool dispatch which requires
investigation.

### B. Hole-ID-not-mentioned (agent refused or chose wrong hole)

| Test | Expected hole | Failure |
|------|---------------|---------|
| gq-002-list-hole-ids | "PLS-" | (refusal) |
| gq-004-shallowest-hole | "PLS-21-06" | (refusal — shallowest IS 265m PLS-21-06) |
| gq-008-easternmost-hole | "PLS-22-10" | (refusal) |
| gq-016-cross-section | "PLS-" | (refusal) |
| gq-019-westernmost-hole | "PLS-21-05" | (refusal) |
| gq-027-shallowest-hole | "PLS-21-06" + "265" | (refusal) |

These hit the agent's tool-dispatch path. Either the classifier
isn't routing them to `query_spatial_collars` reliably, or the
retrieval is empty. **Phase 18+ investigation territory.**

### C. Confidence-below-threshold (agent's refusal path)

| Test | Threshold | Got |
|------|-----------|-----|
| gq-003-deepest-hole | 0.800 | 0.100 |
| gq-005-diamond-holes | 0.800 | 0.100 |
| gq-020-in-progress-count | 0.700 | 0.100 |

`0.100` is the agent's "refused, no data" default. Same root
cause as Class B — the agent went refusal-path. Will resolve when
Class B does.

### D. Knowledge-graph entities missing (Neo4j not seeded)

| Test | Expected | Fix |
|------|----------|-----|
| gq-011-graph-deposit | "Triple R" | Seed Neo4j MineralDeposit node |
| gq-012-graph-qp | "Sarah Thompson" | Seed Neo4j QualifiedPerson node |
| gq-013-graph-formations | "CGL", "GPT" | Seed Neo4j Formation nodes |
| gq-018-deposit-type | "unconformity" | Seed Neo4j MineralDeposit.deposit_type |

**Phase 18+ territory** (Neo4j fixture work + needs SME-style
geological context for realistic test data).

### E. Assay / lithology / commodity fixtures missing

| Test | Expected | Fix |
|------|----------|-----|
| gq-014-assay-u3o8 | "U3O8", "52" | Seed `silver.samples` with U3O8 assays |
| gq-015-lithology-narration | "PLS-20-01", "SST", "PGN" | Seed `silver.lithology_logs` for PLS-20-01 |
| gq-017-assay-gold | "Au" | Seed `silver.samples` with Au assays |
| gq-022-primary-commodity | "uranium" | Update project metadata commodity → uranium (Phase 17 Step 2 unlocks) |
| gq-023-fault-count | "fault" | Seed `silver.structures` (geology features) |
| gq-024-host-basin | "Athabasca" | Existing project.region includes "Athabasca" — should already pass; check LLM phrasing |
| gq-025-qp-name | "Sarah Thompson" | Same as gq-012 |
| gq-026-estimation-method | "kriging" | Document fixture / report content |

**Phase 18+ territory** for most. gq-022 unlocks in Phase 17 Step 2.

### F. Trend / aggregate (need tool-dispatch + LLM phrasing)

| Test | Expected | Note |
|------|----------|------|
| gq-021-orientation-reference | "grid" | Project.orientation_reference='grid' set — passes when agent dispatches project-info tool |
| gq-028-total-metres | "metre" | Aggregate sum + the word "metre" — LLM should naturally produce |
| gq-029-drill-programme-trend | "2020", "2022" | Tied to gq-009 — date range |
| gq-030-dominant-azimuth | "azimuth" | Survey table aggregation |

These should mostly work once Class B is fixed.

---

## 2. Phase 17 Step 2 scope (what we can fix now)

Engineering-only fixes — no test changes, no SME-gated work:

1. **Add 10 XLS-24-* collars** → unlocks gq-001 ("20" count).
2. **Recompute 20-hole depths so avg = 360.8m** (3608m total) →
   unlocks gq-007.
3. **Update `silver.projects.commodity` to 'uranium'** + trim
   `region` to start with "Athabasca" → unlocks gq-022 (and
   helps gq-024 pass if it doesn't already).
4. **Refresh MV** → all the above propagate to the agent's
   facts block.

Estimated unlock: **3-4 additional reliable passes** beyond the
1-2 metadata floor. Combined: ~5 reliable passes.

---

## 3. Phase 18+ scope (deferred)

- **Class B (hole-ID-mention)** — investigate agent's
  classifier + tool-dispatch path for hole-specific queries.
  Likely 6-7 unlocks.
- **Class D (Neo4j entities)** — seed knowledge graph fixtures.
- **Class E (assay/lithology)** — schema seed work; pairs with
  Class D for full LLM-context completeness.
- **Class F** mostly resolves with Class B unlocked.

---

## 4. Phase 17 expected end state

Per the Step 2 scope, expect **≥5 reliable passes** after the
fixture extensions land. The current 1-2 floor moves to 5 as
the regression gate for Phase 18+.

End of audit.
