# Phase 14 Handoff — Third prompt + HMAC overlap + R-P13-1 root-caused

**Document version:** 1.0
**Status:** Phase 14 complete. Phase 15 inheriting.
**Predecessors:** `docs/phase13_handoff.md`,
`docs/phase14_implementation_kickoff.md`,
`docs/phase14_r-p13-1_scoping.md`.

---

## 1. What Phase 14 delivered

Phase 14 was a "discipline + investigation" phase that
unexpectedly resolved Phase 13's highest-priority carry-over.
What started as a doc-only scoping exercise for R-P13-1 turned
up the root cause (stale `silver.mv_collar_summary`) and proved
the fix in flight — the Phase 13 fixture migration now refreshes
the MV automatically.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `_AGENT_SYSTEM_PROMPT` (67 lines, 4133 chars) migrated from `agentic_escalation.py` → `app/agent/prompts/agent_system.py` + registry entry (4 entries total) | `scripts/phase14_step1_verify.sh` (6/6) |
| 2 | HMAC rotation now accepts `overlap_hours` (0-168, default 0). Prior sender's `disabled_at = now() + overlap_hours`. Audit payload records the field. Phase 12 Step 4 regression preserved. | `scripts/phase14_step2_verify.sh` (6/6) |
| 3 | R-P13-1 scoping doc + in-flight fix: `silver.mv_collar_summary` identified as the agent's fact source; missing-row → empty SUMMARIES block → "I don't have that number". Phase 13 fixture migration now runs `REFRESH MATERIALIZED VIEW` after the seed. Three R-P14-* carry-overs documented. | `scripts/phase14_step3_verify.sh` (7/7) |
| 4 | This handoff | — |

**Phase 14 cumulative: 19 / 19 verifier checks** (6+6+7).
**Master sweep across Phase 0 → Phase 14 at close: 392 / 392 across
61 verifiers** (`scripts/phase14_master_sweep.sh`).

---

## 2. R-P13-1 — diagnosed + partially fixed

See `docs/phase14_r-p13-1_scoping.md` for the full diagnosis. Short version:

- The agent's NUMERIC system-prompt variant (`orchestrator.py:843+`)
  instructs the LLM to say *"I don't have that number in this
  project"* when the **HIGH-CONFIDENCE SUMMARIES** block is absent
  from the prompt context.
- The block comes from `_build_project_facts()`
  (`orchestrator.py:1244`), which reads `silver.mv_collar_summary`.
- Phase 13 seeded `silver.collars` but didn't refresh the MV.
  Result: cold runs (first one after a chance refresh) saw the
  populated MV → 12-13 passes; warm runs saw the empty MV →
  2 passes.
- Phase 14 adds `REFRESH MATERIALIZED VIEW silver.mv_collar_summary;`
  to the end of the fixture migration. Re-applying the migration
  leaves the MV in a state the agent can find.

**Caveat:** even with the MV populated, the live golden run still
fluctuates (12 passes one moment, 1 the next). The MV's row count
stays at 10 across these fluctuations — so a second factor is in
play. Logged as R-P14 hypothesis #2 in the scoping doc; deferred.

---

## 3. Architectural state at end of Phase 14

### 3.1 Prompts registry

4 entries now: `example_system`, `rephrase_system`, `classifier_system`,
`agent_system`. The 67-line agent prompt was the largest migration
to date.

### 3.2 Sender rotation posture

JWT rotation (Phase 6 Step 3) and HMAC rotation (Phase 14 Step 2)
now both support overlap-window rotation with identical 0-168h
bounds. Audit emissions on both paths carry the `overlap_hours`
field for forensics.

### 3.3 Fixture migration

`database/raw/phase13/10-golden-collars-fixture.sql` is now a
three-stage operation: seed parent project + seed 10 collars +
REFRESH MATERIALIZED VIEW. Re-applying is idempotent end-to-end.

---

## 4. Carry-overs for Phase 15

| ID | Item | Where | Priority |
|----|------|-------|----------|
| **R-P14-2** | Nightly Hatchet `mv_refresh_silver` workflow | `src/fastapi/app/hatchet_workflows/` | **High** — Dagster pipeline is dev-paused; without a cron the MV will drift again |
| **R-P14-3** | Golden-test pass-rate improvement | tests + agent | Medium — investigate remaining 19-23 failures with MV populated |
| **R-P14-1** | MV refresh in fixture migration | CLOSED at Phase 14 Step 3 |
| **R-P13-1** | Intermittent agent refusal | Root-caused at Phase 14 Step 3; downstream effects deferred |
| **R-P11-baseline-2** | Public-geoscience fixture | tests | Medium |
| **R-P11-l4-fixture** | Layer 4 entity fixture | tests | Medium |
| **R-P11-B** | Frontend Search/Query page | `resources/js/Pages/` | Medium |
| **R-P12-more-prompts** | Continue inline-prompt migrations | orchestrator + others | Low — pattern proven |
| **R-P12-l6-sme-review** | SME review of L6 constraints | doc | SME-gated |
| **R-P3-5**, **R-P3-6**, **R-P3-9** | Dual-write / HA / vendor profiles | various | Deferred |

---

## 5. Files of record

**New in Phase 14:**

```
app/Http/Controllers/Admin/IntegrationsController.php             (mod — Step 2; overlap_hours)
database/raw/phase13/10-golden-collars-fixture.sql                 (mod — Step 3; REFRESH MV)
docs/phase14_implementation_kickoff.md                             (Step 0)
docs/phase14_r-p13-1_scoping.md                                    (Step 3)
docs/phase14_handoff.md                                             (this file)
scripts/_phase14_step2_probe.php                                   (Step 2 helper)
scripts/phase14_master_sweep.sh                                    (Step 4)
scripts/phase14_step1_verify.sh                                    (Step 1)
scripts/phase14_step2_verify.sh                                    (Step 2)
scripts/phase14_step3_verify.sh                                    (Step 3)
src/fastapi/app/agent/agentic_escalation.py                       (mod — Step 1; imports from prompts/)
src/fastapi/app/agent/prompts/_version_registry.py                 (mod — Step 1; agent_system entry)
src/fastapi/app/agent/prompts/agent_system.py                      (Step 1)
```

---

## 6. Re-running every Phase 14 verifier

```bash
bash scripts/phase14_step1_verify.sh   # agent_system migration       (6/6)
bash scripts/phase14_step2_verify.sh   # HMAC rotation overlap        (6/6)
bash scripts/phase14_step3_verify.sh   # R-P13-1 scoping + MV fix     (7/7)
```

Combined Phase 0 → Phase 14 sweep — **61 verifiers, 392 total checks**
(`scripts/phase14_master_sweep.sh`).

---

## 7. Phase 15 entry checklist

1. Read this handoff + `docs/phase14_r-p13-1_scoping.md`.
2. Re-run `scripts/phase14_master_sweep.sh` — confirm 392/392.
3. Highest-leverage Phase 15 scope: **R-P14-2** (nightly MV
   refresh workflow). Without it, the MV will drift back to
   empty between fixture re-applies, undoing Phase 14's R-P13-1
   fix at the next deploy. Pattern is well-established —
   `flow_jwt_key_reaper` from Phase 7 Step 2 is the template.

End of Phase 14 handoff.
