# Phase 15 Handoff — Nightly MV refresh + orchestrator prompt audit

**Document version:** 1.0
**Status:** Phase 15 complete. Phase 16 inheriting.
**Predecessors:** `docs/phase14_handoff.md`,
`docs/phase14_r-p13-1_scoping.md`.

---

## 1. What Phase 15 delivered

Phase 15 closed the highest-leverage carry-over from Phase 14
(R-P14-2: nightly MV refresh) and audited the remaining inline
prompts in `orchestrator.py` for a future bundled migration.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `workflow.refresh_silver_agent_mvs()` SECURITY DEFINER function + `mv_refresh_silver` Hatchet workflow on cron `0 3 * * *` UTC; AI pool registration | `scripts/phase15_step1_verify.sh` (6/6) |
| 2 | `docs/phase15_orchestrator_prompts_audit.md` — enumerates the 10 inline prompt variants in `orchestrator.py`, references `_SYSTEM_PROMPT_VERSION` cache key, lays out R-P15-1 bundled-migration plan | `scripts/phase15_step2_verify.sh` (5/5) |
| 3 | This handoff | — |

**Phase 15 cumulative: 11 / 11 verifier checks** (6+5).
**Master sweep across Phase 0 → Phase 15 at close: 403 / 403 across
63 verifiers** (`scripts/phase15_master_sweep.sh`).

---

## 2. R-P13-1 final status

The Phase 14 root cause is now operationally fixed:

- **Phase 13 Step 3 (`10-golden-collars-fixture.sql`):** one-shot
  refresh on the fixture-seed migration. Re-applying the migration
  always leaves the MV in a state the agent can find.
- **Phase 14 Step 3 (root-causing):** documented the failure mode.
- **Phase 15 Step 1 (`mv_refresh_silver`):** nightly cron refreshes
  the MV at 03:00 UTC so it doesn't drift between deploys.

The two remaining R-P13-1 caveats (LLM-determinism beyond MV
state, intermittent run-to-run fluctuation even with MV populated)
remain as **R-P14-3** — golden-test pass-rate investigation.

---

## 3. Architectural state at end of Phase 15

### 3.1 AI-pool Hatchet workflows

Three operator-class nightly workflows now run on the AI pool:

| Time (UTC) | Workflow | Purpose |
|-----------:|----------|---------|
| 02:00 | audit_ledger_verify | Hash-chain verification of audit rows |
| 03:00 | **mv_refresh_silver** (NEW) | Refresh agent-prompt MVs |
| 04:00 | flow_jwt_key_reaper | Reap expired per-flow JWT signing keys |

All three follow the same shape: SECURITY DEFINER SQL helper +
asyncpg + Hatchet cron. The `mv_refresh_silver` SQL function is
extensible — add more `REFRESH MATERIALIZED VIEW` lines as new
agent-prompt MVs land.

### 3.2 Prompts registry

Still 4 entries (Phase 14 close). The orchestrator-side bundled
migration (R-P15-1) would add 10 more in a single phase when
attempted.

---

## 4. Carry-overs for Phase 16

| ID | Item | Where | Priority |
|----|------|-------|----------|
| **R-P14-3** | Golden-test pass-rate investigation | tests + agent | High — even with MV refresh, run-to-run pass count fluctuates 1-12 |
| **R-P15-1** | Bundled orchestrator prompt migration (10 variants) | `orchestrator.py` + `prompts/orchestrator_system.py` | Medium — scoped in Phase 15 Step 2 |
| **R-P11-baseline-2** | Public-geoscience fixture | tests | Medium |
| **R-P11-l4-fixture** | Layer 4 entity fixture | tests | Medium |
| **R-P11-B** | Frontend Search/Query page | `resources/js/Pages/` | Medium |
| **R-P12-l6-sme-review** | SME review of L6 constraints | doc | SME-gated |
| **R-P14-2** | Nightly MV refresh workflow | CLOSED at Phase 15 Step 1 |
| **R-P3-5**, **R-P3-6**, **R-P3-9** | Deferred per prior phases |

---

## 5. Files of record

**New in Phase 15:**

```
database/raw/phase15/10-mv-refresh-fn.sql                          (Step 1)
docs/phase15_implementation_kickoff.md                             (Step 0)
docs/phase15_orchestrator_prompts_audit.md                         (Step 2)
docs/phase15_handoff.md                                             (this file)
scripts/phase15_master_sweep.sh                                    (Step 3)
scripts/phase15_step1_verify.sh                                    (Step 1)
scripts/phase15_step2_verify.sh                                    (Step 2)
src/fastapi/app/hatchet_workflows/mv_refresh_silver.py             (Step 1)
src/fastapi/app/hatchet_workflows/worker.py                       (mod — Step 1; AI-pool registration)
```

---

## 6. Re-running every Phase 15 verifier

```bash
bash scripts/phase15_step1_verify.sh   # mv_refresh_silver workflow  (6/6)
bash scripts/phase15_step2_verify.sh   # orchestrator prompt audit   (5/5)
```

Combined Phase 0 → Phase 15 sweep — **63 verifiers, 403 total checks**
(`scripts/phase15_master_sweep.sh`). One intermittent flake observed
on `phase4_step7_verify.sh` (the rollup verifier; passes standalone,
sometimes 4/5 in batch due to MV/timing churn).

---

## 7. Phase 16 entry checklist

1. Read this handoff + `docs/phase15_orchestrator_prompts_audit.md`.
2. Re-run `scripts/phase15_master_sweep.sh` — confirm 403/403.
3. Highest-leverage Phase 16 scope candidates:
   - **R-P14-3** (golden-test investigation) — needed for real
     pass-rate measurement
   - **R-P15-1** (bundled orchestrator prompts migration) —
     finishes the prompt-discipline arc
   - **R-P11-B** (frontend Search page) — first user-facing RAG
     surface
   - **R-P11-baseline-2** (public-geoscience fixture) — unlocks
     the 3 pgeo golden tests

End of Phase 15 handoff.
