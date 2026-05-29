# Phase 15 Implementation Kickoff — Nightly MV refresh + one more prompt

**Document version:** 1.0
**Status:** Active.
**Predecessors:** `docs/phase14_handoff.md`,
`docs/phase14_r-p13-1_scoping.md`.

---

## 1. Theme

Phase 14 root-caused R-P13-1 and added a one-shot MV refresh to
the Phase 13 fixture migration. Phase 15 makes the fix durable
across normal operation: a nightly Hatchet workflow refreshes
`silver.mv_collar_summary` so the agent's fact-source MV doesn't
drift back to stale-empty between fixture re-applies.

Pattern is well-established — `flow_jwt_key_reaper` (Phase 7
Step 2) is the template: AI-pool workflow + SECURITY DEFINER
SQL function + Hatchet cron registration + Step verifier.

Bundled with one more inline-prompt migration to keep that
discipline arc moving (now four prompts in canonical form;
target the next isolated module-level constant).

---

## 2. Locked decisions

| ID | Item | Phase 15 status |
|----|------|---------------|
| **R-P14-2** | Nightly `mv_refresh_silver` Hatchet workflow | **In scope (Step 1)** |
| **R-P12-more-prompts** | Fifth inline-prompt migration | **In scope (Step 2)** |
| **R-P14-3** | Golden-test pass-rate investigation | Defer — needs longer iterations |
| **R-P11-baseline-2** | Public-geoscience fixture | Defer |
| **R-P11-l4-fixture** | Layer 4 fixture | Defer |
| **R-P11-B** | Frontend Search page | Defer |
| **R-P12-l6-sme-review** | SME review | SME-gated |
| **R-P3-5**, **R-P3-6**, **R-P3-9** | Deferred per prior phases |

---

## 3. Done definition

- Step 1 verifier: `mv_refresh_silver` registered with Hatchet
  on cron `0 3 * * *`; AI-pool worker lists it; a synthetic
  trigger refreshes the MV.
- Step 2 verifier: a fifth inline prompt migrated to canonical
  `prompts/` form.
- All prior verifiers still green (392 → ~410+).

---

## 4. Step-by-step

### Step 1 — Nightly MV refresh workflow (R-P14-2)
- SQL fn: `workflow.refresh_silver_agent_mvs()` —
  SECURITY DEFINER, refreshes `silver.mv_collar_summary` (and
  any sibling agent-prompt MVs). Returns the number of MVs
  refreshed.
- Hatchet workflow: `mv_refresh_silver` on `0 3 * * *` UTC
  (chosen to land between `flow_jwt_key_reaper` at 04:00 and
  `audit_ledger_verify` at 02:00).
- AI-pool registration in `worker.py` POOLS dict.
- Verifier:
  1. SQL fn present + executable as `georag_app`
  2. Workflow module loads
  3. AI worker `--list` includes the new workflow
  4. Hatchet engine has the cron trigger
  5. Direct fn invocation refreshes the MV
  6. After invocation, MV has 10 collars for the test project

### Step 2 — Fifth inline-prompt migration
- Pick a remaining module-level prompt constant from the agent
  code (likely a system prompt variant inside `orchestrator.py`
  or a smaller helper file).
- Same Phase 11 Step 3 pattern.

### Step 3 — Handoff

---

## 5. Files of record (preview)

```
database/raw/phase15/10-mv-refresh-fn.sql                          (Step 1)
docs/phase15_implementation_kickoff.md                             (this file)
docs/phase15_handoff.md                                             (Step 3)
scripts/phase15_master_sweep.sh                                    (Step 3)
scripts/phase15_step1_verify.sh                                    (Step 1)
scripts/phase15_step2_verify.sh                                    (Step 2)
src/fastapi/app/agent/prompts/<new_name>.py                       (Step 2)
src/fastapi/app/agent/prompts/_version_registry.py                 (mod — Step 2)
src/fastapi/app/hatchet_workflows/mv_refresh_silver.py             (Step 1)
src/fastapi/app/hatchet_workflows/worker.py                       (mod — Step 1; register)
```

---

End of Phase 15 kickoff.
