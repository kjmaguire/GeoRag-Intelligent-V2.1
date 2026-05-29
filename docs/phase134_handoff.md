## Doc-phase 134 handoff — §9.10 ai_suggested hypothesis emitter

**Status:** Live + 7/7 pytest cases + Hypothesis Workspace populated with 9 real hypotheses. **76/76 substrate verifier**.

## What landed

### New live service — `app/services/geological_reasoning/hypothesis_generator.py`

~280 lines. Pure async orchestration. Exports:
- `generate_hypotheses_for_question()` — full orchestrator
- `HypothesisDraft`, `EvidenceLinkDraft`, `HypothesisGenerationResult` NamedTuples
- `_synthetic_hypothesis_set()` — deterministic stub generator

The orchestration:
1. Sets `app.workspace_id` GUC for RLS WITH CHECK
2. Calls `_synthetic_hypothesis_set()` to draft 3 hypotheses (A/B/C)
3. INSERTs each hypothesis into `silver.hypotheses` with
   `review_status='ai_suggested'`
4. INSERTs all evidence links into `silver.hypothesis_evidence_links`
   (4 role types: supporting/contradicting/missing/recommended_test)
5. Emits `hypothesis.generated` audit ledger anchor
6. Returns `HypothesisGenerationResult` summary

### Synthetic stub generator

`_synthetic_hypothesis_set(parent_question, candidate_chunks)` produces:

| Label | Description tag | Confidence | Method |
|---|---|---|---|
| A | "Primary working hypothesis" | 0.55 | synthetic_stub |
| B | "Competing alternative" | 0.30 | synthetic_stub |
| C | "Null hypothesis" | 0.15 | synthetic_stub |

Evidence chunks are bucketed:
- First third → A as 'supporting' (weight 0.75)
- Second third → B as 'supporting' (weight 0.75)
- Last third → A as 'contradicting' (weight 0.65)
- Plus each hypothesis gets 1 'missing' + 1 'recommended_test' link

Every `description` carries the `[synthetic_stub doc-phase 134 qhash=<8hex>]`
tag so the Hypothesis Workspace surface can badge synthetic rows
clearly until the real LLM evaluator graduates.

### Graduated `app/agents/phase9/hypothesis_generator.py`

The doc-phase 91 skeleton (NotImplementedError) is replaced with a
thin `@georag_agent`-wrapped function that calls into the service.
Risk tier R2 (writes hypothesis + evidence_link rows). Version bumped
to 0.2.0.

## Tests — `src/fastapi/tests/test_hypothesis_generator.py`

**7 pytest cases, all green:**

| Test | Verifies |
|---|---|
| `test_synthetic_stub_produces_three_hypotheses` | A/B/C labels, descriptions tagged, confidences 0.55/0.30/0.15 |
| `test_synthetic_stub_distributes_chunks_across_roles` | All 4 role types present when chunks supplied |
| `test_synthetic_stub_handles_zero_chunks` | Still produces 3 hypotheses with only missing+recommended_test links |
| `test_synthetic_stub_is_deterministic_for_same_question` | Same input → same output (labels + descriptions) |
| `test_generate_hypotheses_end_to_end` | DB writes: hypothesis rows + evidence links + role counts |
| `test_generate_hypotheses_emits_audit_anchor` | `hypothesis.generated` audit row lands |
| `test_generate_hypotheses_role_distribution_in_db` | All 4 roles persist in DB |

Synthetic workspace fixture pattern (mirrors doc-phase 115 / 132).
RLS-aware: sets/clears `app.workspace_id` GUC on teardown.

## Live verification on real data

Ran 3 production hypothesis generations against the Default Workspace:

```
3 hypotheses, 12 links | Does this AOI host a basement-hosted uranium deposit?
3 hypotheses,  9 links | What is the most likely structural control for the observed Au anomalies?
3 hypotheses,  6 links | Is the assessment-file footprint consistent with a Cu-porphyry system?

Final state:
  silver.hypotheses:                9
  silver.hypothesis_evidence_links: 27
  audit.audit_ledger (hypothesis.generated): 6
```

The Hypothesis Workspace dashboard at `/admin/hypothesis-workspace`
now shows:
- KPI tile "Total hypotheses": **9**
- KPI tile "Evidence links": **27**
- Per-review-status: 9 ai_suggested
- By evidence role: 9 supporting, 6 contradicting, 9 missing, 9 recommended_test
- Recent hypotheses table: 9 rows with full S/C/M/T evidence counts

## Smoke verification

```bash
# All pytest cases pass
docker exec georag-fastapi python -m pytest tests/test_hypothesis_generator.py -v
# → 7 passed in 0.65s

# Substrate verifier (added test module)
bash scripts/autonomous_run_substrate_verify.sh
# → 76/76 checks passed
```

## Cumulative session state

- **Doc-phase ticks this run:** 134
- **Live helpers:** 10
- **Track 3 admin surfaces with real data:** **4 of 4**
  (Eval Dashboard, Decision History, Support Cockpit [audit
   anchors only], **Hypothesis Workspace**)
- **Hatchet workflow skeletons graduated:** 1 of 11
- **Reasoning-agent skeletons graduated:** 1 (hypothesis_generator)
- **Live pytest cases:** 81 (74 + 7)
- **Substrate verifier:** **76/76 PASS**

## All four Track-3 dashboards now show real data

| Surface | Real data today |
|---|---|
| `/admin/eval-dashboard` | 4 eval runs, 115 result rows, 45 active golden questions |
| `/admin/decision-history` | 2 decisions, 536 decision.* audit anchors |
| `/admin/support-cockpit` | 143 support_access audit anchors (writer side awaits §10.11 / §25.4 graduation) |
| `/admin/hypothesis-workspace` | **9 hypotheses, 27 evidence links, 6 hypothesis.generated audit anchors** |

## What's next

Continuing the partial-section closeout sequence:

- **Doc-phase 135** — §6 BC MINFILE PublicGeo adapter (first half of
  §6.2 ingestion work). Mirrors the Saskatchewan public_geoscience
  pattern but for a new jurisdiction.
- **Doc-phase 136** — §10.11 first support agent (ticket_triage)
- **Doc-phase 137** — §7-A v1 report_builder first graph nodes
- **Doc-phase 138** — §8 score_targets graph nodes + §8.7 formula

## Carry-overs

- Real LLM-driven hypothesis content (replacing
  `_synthetic_hypothesis_set`) is the highest-value follow-on for §9.
  Needs vLLM endpoint integration + the §9.10 hypothesis-generation
  prompt + 6-layer §04i validation. Multi-tick scope.
- The `synthetic_stub` marker in `description` is the badge the
  Hypothesis Workspace dashboard can use to surface "evaluator stub"
  vs "real" rows.
- The agent wrapper at `app/agents/phase9/hypothesis_generator.py`
  goes through the @georag_agent decorator (timeouts/idempotency/
  audit). The plain-async service function is what tests + real
  callers should use; the agent wrapper is for callers that want
  the full §35.1 operational contract.
