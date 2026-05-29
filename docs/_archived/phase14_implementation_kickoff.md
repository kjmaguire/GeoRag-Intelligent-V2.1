# Phase 14 Implementation Kickoff — Third prompt migration + HMAC overlap + R-P13-1 scoping

**Document version:** 1.0
**Status:** Active.
**Predecessors:** `docs/phase13_handoff.md`.

---

## 1. Theme

Phase 13 seeded the golden fixture and observed an intermittent
refusal-path issue (R-P13-1). Phase 14 keeps the prompt-discipline
arc moving (third migration), adds the deferred HMAC overlap-window
rotation, and scopes R-P13-1 with a read-only investigation —
intentionally producing a diagnostic doc rather than risking a
code change to a 5184-line orchestrator without more signal.

---

## 2. Locked decisions

| ID | Item | Phase 14 status |
|----|------|---------------|
| **R-P12-more-prompts** | Third inline prompt migration | **In scope (Step 1)** |
| **R-P12-l6-overlap-hmac** | HMAC overlap-window rotation | **In scope (Step 2)** |
| **R-P13-1** | Intermittent agent refusal investigation | **In scope (Step 3; doc-only)** |
| **R-P11-baseline-2**, **R-P11-l4-fixture**, **R-P11-B**, **R-P12-l6-sme-review** | Defer |
| **R-P3-5**, **R-P3-6**, **R-P3-9** | Defer |

---

## 3. Done definition

- Step 1 verifier: third prompt migrated cleanly (round-trip
  equality + registry entry).
- Step 2 verifier: `rotateSenderHmac` accepts an optional
  `overlap_hours` field; when > 0, the prior sender's
  `disabled_at` is set to `now() + overlap_hours` rather than
  `now()`. The audit payload records the overlap window.
- Step 3 verifier: `docs/phase14_r-p13-1_scoping.md` exists,
  enumerates ≥3 hypotheses for the intermittent refusal, and
  references concrete code paths (file:line) the investigation
  would target.
- All prior verifiers still green (373 → ~390+).

---

## 4. Step-by-step

### Step 1 — Migrate `_AGENT_SYSTEM_PROMPT` from `agentic_escalation.py`
- Same pattern as Phase 12 Step 2 + Phase 13 Step 1.
- Target: `app/agent/prompts/agent_system.py`.
- Update consumer to import from canonical path.
- Verifier: round-trip equality + registry entry + pre-commit
  hook accepts.

### Step 2 — HMAC rotation overlap (R-P12-l6-overlap-hmac)
- `rotateSenderHmac` accepts an optional `overlap_hours` form
  field (default 0 = immediate cut, matches current Phase 12 Step 4
  behaviour). When > 0, sets the prior sender's `disabled_at` to
  `clock_timestamp() + make_interval(hours => N)` instead of
  `clock_timestamp()`.
- Audit emission payload gains an `overlap_hours` field.
- Inertia form (`SenderRowView`) keeps the current single-click
  Rotate button (default overlap=0); a separate optional
  per-form overlap input is deferred to Phase 15+.
- Verifier: probe rotation with `overlap_hours=24` confirms the
  prior `disabled_at` lands ~24h in the future + audit payload
  records the field.

### Step 3 — R-P13-1 scoping (doc-only investigation)
- Read `app/agent/orchestrator.py` + `app/agent/tools.py` to
  enumerate the paths that produce "I don't have that data" /
  "I don't have that number" refusals.
- Cross-reference with the Phase 13 observation (13/35 then 2/35
  on identical fixture).
- Output: `docs/phase14_r-p13-1_scoping.md` listing:
    1. The refusal text patterns + their source file:line
    2. ≥3 hypotheses for the intermittency
    3. Recommended investigation steps for Phase 15+ (probes,
       instrumentation, fixture additions)
- No orchestrator code changes in this phase.

### Step 4 — Phase 14 → Phase 15 handoff

---

## 5. Engineering invariants

- `scripts/phase14_master_sweep.sh` extends the Phase 13 sweep.
- No new DB migrations.
- Step 3 is investigation, not implementation — Phase 15 may
  re-open R-P13-1 with the scoping doc as input.

---

## 6. Files of record (preview)

```
app/Http/Controllers/Admin/IntegrationsController.php             (mod — Step 2)
docs/phase14_implementation_kickoff.md                             (this file)
docs/phase14_handoff.md                                             (Step 4)
docs/phase14_r-p13-1_scoping.md                                    (Step 3)
scripts/phase14_master_sweep.sh                                    (Step 4)
scripts/phase14_step1_verify.sh                                    (Step 1)
scripts/phase14_step2_verify.sh                                    (Step 2)
scripts/phase14_step3_verify.sh                                    (Step 3)
src/fastapi/app/agent/agentic_escalation.py                       (mod — Step 1)
src/fastapi/app/agent/prompts/agent_system.py                     (Step 1)
src/fastapi/app/agent/prompts/_version_registry.py                 (mod — Step 1)
```

---

End of Phase 14 kickoff.
