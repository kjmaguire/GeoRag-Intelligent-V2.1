# Master-plan §10 (Eval harness + Customer Support Cockpit) — Kickoff

**Doc-phase:** 178 (§10-v1 shipped)
**Status:** SHIPPED — 5-tick batch landed 2026-05-16
**Predecessor:** `docs/master_plan_section10_scope_proposal.md` (doc-phase 95)
**Authored:** 2026-05-16
**Acceptance:** `scripts/section10_acceptance.sh` — 17/17 green
**Test coverage:** `tests/test_promotion_gate.py` (6 tests) + `tests/test_cross_workspace_audit.py` (4 tests) — 10/10 green

---

## TL;DR

§10 is **mostly shipped** through prior phases — Phase H4 added the
Support Cockpit UI, eval_real_rag_nightly + evaluate_workspace +
support_replay are graduated workflows, 5/5 §10 tables exist, 63 of
the original 100 golden questions are seeded. The remaining
autonomous-safe work is a **5-tick polish batch**:

- §10.2  Top-up golden questions to 100 across remaining sets
- §10.6  Promotion-gate enforcer in CI
- §10.12 Cross-workspace access audit emission
- §10.13 LangFuse trace replay link in the Support Cockpit
- §10.14 Acceptance harness mirroring Phase H4 + §11 + §6

---

## Reality-calibrated scope

### Already shipped (verified 2026-05-16)

| Sub-step | Item                                              | Evidence                                       |
|----------|---------------------------------------------------|------------------------------------------------|
| 10.1     | `eval.golden_questions` schema                    | Table exists                                   |
| 10.2     | Golden questions seeded                           | **63 of 100 rows** (6 question_set slots populated) |
| 10.4     | `evaluate_workspace` Hatchet workflow             | `app/hatchet_workflows/evaluate_workspace.py`  |
| 10.5     | `eval.run_results` schema                         | Table exists                                   |
| 10.7     | Eval surfacing                                    | `eval_real_rag_nightly` cron @ 05:15 UTC       |
| 10.8     | `ops.support_tickets` + `_traces` + `_replay_runs`| 3 tables exist (6 sample tickets seeded)       |
| 10.9     | Support agent skeletons                           | (per scope-proposal — to verify)               |
| 10.10    | `support_replay` Hatchet workflow                 | `app/hatchet_workflows/support_replay.py`      |
| 10.11    | Customer Support Cockpit UI                       | Phase H4 §10 surface (`/admin/support/*`)      |
| —        | Real-RAG nightly eval cron                        | Doc-phase 170 wired `0 5 * * *` UTC            |

### Question-set breakdown (current 63 seeded)

| `question_set`         | Seeded | Target | Gap |
|------------------------|-------:|-------:|----:|
| `core_chat`            | 10     | 15     | -5  |
| `numeric_grounding`    | 15     | 20     | -5  |
| `ocr_triage`           | 10     | 10     | 0   |
| `refusal_correctness`  | 8      | 15     | -7  |
| `report_section`       | 10     | 15     | -5  |
| `schema_mapping`       | 10     | 15     | -5  |
| `citation_provenance`  | 0      | 10     | -10 |
| `temporal_reasoning`   | 0      | 10     | -10 |
| **TOTAL**              | **63** | **110**| **-47** |

(Original master-plan target was 100; this proposal bumps to 110 to
hit ≥10 per set including the 2 missing sets.)

### Open — autonomous-safe (this kickoff covers these)

| Sub-step | Item                                              | Estimated effort |
|----------|---------------------------------------------------|------------------|
| 10.2b    | Top up golden questions to ≥10 per set (47 more)  | 60-90 min        |
| 10.6     | Promotion-gate enforcer + CI integration          | 45 min           |
| 10.12    | Cross-workspace access audit emission             | 30 min           |
| 10.13    | LangFuse trace replay link in cockpit             | 30 min           |
| 10.14    | Acceptance harness `section10_acceptance.sh`      | 30 min           |
| **Total**|                                                   | **3-4 hours**    |

### Open — Kyle-gated (deferred to §10-v2)

| Sub-step | Item                                | Why deferred                                        |
|----------|-------------------------------------|-----------------------------------------------------|
| 10.3     | Question authoring UI in admin      | Product-design judgment on the editor UX            |
| 10.7-v2  | Eval Dashboard with diff visualizer | Heavy frontend; needs Kyle to pick chart library    |

---

## Sub-step detail (5-tick batch)

### §10.2b — Golden questions top-up

**What ships:**
- 47 new golden questions across the gaps above (5 in core_chat, 5
  in numeric_grounding, 7 in refusal_correctness, 5 in
  report_section, 5 in schema_mapping, 10 in citation_provenance,
  10 in temporal_reasoning).
- New SQL seed file `database/raw/phase0/106-section10-golden-questions-topup.sql`
- Questions authored to spec — each carries `expected_answer`,
  `expected_citations`, `validators`, `tags`.

**Acceptance:**
- `SELECT count(*) FROM eval.golden_questions` = 110
- Every question_set has ≥10 rows
- `evaluate_workspace.aio_mock_run` against any set passes >0%
  (sanity — real pass rate gated on real LLM)

### §10.6 — Promotion gate enforcer

**What ships:**
- New `app.services.eval.promotion_gate` module:
  - `assess_promotion(workspace_id, candidate_workflow_run_id, baseline_run_id)`
  - Returns `{allow: bool, regressions: [{question_id, baseline_pass, candidate_pass}]}`
  - Threshold: any per-set pass-rate regression >5% blocks promotion
- New admin endpoint `POST /api/v1/admin/eval/assess-promotion`
- Audit row `eval.promotion.{allowed|blocked}` on every assessment
- Documentation in RUNBOOK.md for the override-with-rationale path

**Acceptance:**
- Synthetic candidate-baseline pair with a 10% regression → `allow=false`
- Same with 2% drift → `allow=true`
- Unit + integration tests both green

### §10.12 — Cross-workspace access audit

**What ships:**
- New `app.services.audit.cross_workspace_access` helper
- Hook into `app/services/workspace_resolution.py` so any
  authenticated request that touches a workspace_id NOT in the
  user's project_user pivot emits `security.cross_workspace_access`
  audit row (action_type ends `.alert` so it lands in the inbox)
- Idempotent within a 1-hour window (same user + same target
  workspace = one alert, not many)

**Acceptance:**
- Synthetic request from user A targeting workspace B emits one
  audit row
- Repeat within 1h emits zero additional rows
- After 1h-window-expiry, emits a second row

### §10.13 — LangFuse trace replay link

**What ships:**
- New Inertia page accessor in the Support Cockpit `/admin/support/tickets/{id}`
- Each ticket trace row carries a `langfuse_trace_url` field built
  from `trace_id` + env-configured `LANGFUSE_BASE_URL`
- Click opens LangFuse in a new tab pre-scoped to the trace
- Falls back to a copyable trace_id when LANGFUSE_BASE_URL is unset

**Acceptance:**
- Cockpit page rendering includes the link
- Link target opens to a valid LangFuse trace URL pattern

### §10.14 — Acceptance harness

**What ships:**
- `scripts/section10_acceptance.sh` mirroring `section6_acceptance.sh`
  + `section11_acceptance.sh` patterns
- 10-12 checks: tables present + workflows registered + golden_questions
  count + promotion-gate endpoint + cross-workspace audit + cockpit
  route + LangFuse link presence
- Exit 0 = §10-v1 surface green

**Acceptance:**
- Happy + failure-path exit codes verified

---

## Locked decisions (pre-approved per kickoff pattern)

| Decision                          | Value                                                                |
|-----------------------------------|----------------------------------------------------------------------|
| Promotion regression threshold    | **>5%** per-question_set pass-rate drop                              |
| Cross-workspace audit window      | **1 hour** idempotency (same user + target = one alert per hour)     |
| Golden questions per set          | **≥10** (target 110 total across 8 sets)                             |
| Acceptance harness shell          | Bash + docker exec pattern (mirrors §6 + §11)                        |
| LangFuse URL config               | Env var `LANGFUSE_BASE_URL`, falls back to copyable trace_id        |

---

## Sign-off

If Kyle approves this kickoff as-written:

- [x] §10-v1 = the 5-tick autonomous batch above
- [ ] §10-v2 = authoring UI + dashboard diff viz, deferred to a
      Kyle-paired session
- [x] First wave fires immediately
- [x] `scripts/section10_acceptance.sh` is the done-test

## Shipped (2026-05-16)

| Tick    | What landed                                                                                          |
|---------|------------------------------------------------------------------------------------------------------|
| §10.2b  | 47 new golden questions; total now 110/110, every set ≥10                                            |
| §10.6   | `app.services.eval.promotion_gate.assess_promotion` + `POST /api/v1/admin/eval/assess-promotion` + audit + 6 tests |
| §10.12  | `app.services.cross_workspace_audit.emit_cross_workspace_alert` wired into `workspace_resolution` mismatch path + 4 tests |
| §10.13  | `langfuse_base_url` Inertia prop + `renderTraceLink` helper in Support Cockpit (shipped earlier in wave D) |
| §10.14  | `scripts/section10_acceptance.sh` — 17/17 checks green                                              |

Note: the kickoff originally listed `citation_provenance` + `temporal_reasoning`
as the missing question_sets. The actual CHECK constraint in
`eval.golden_questions` permits `public_private_boundary` +
`target_recommendation` instead — top-up seeded those.
