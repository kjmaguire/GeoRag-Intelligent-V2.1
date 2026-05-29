# Phase 12 Implementation Kickoff — RAG discipline + operator UI continuations

**Document version:** 1.0
**Status:** Active.
**Predecessors:** `docs/phase11_handoff.md`, `docs/phase11_section_04i_audit.md`.

---

## 1. Theme

Phase 11 audited the §04i hallucination framework + bootstrapped the
`prompts/` subdirectory. Phase 12 closes the three small RAG-discipline
items that Phase 11 surfaced (init.py docstring drift, prove the
prompt-migration pattern, externalise Layer 6 constraints) and pairs
them with the two operator-UI continuations Phase 10 deferred (sender
HMAC rotate, rotation history panel).

Deferring R-P11-baseline-{1,2} (golden fixture seeding) and R-P11-B
(frontend Search page) to Phase 13+ — both are larger investments
than the items below, and the audit work makes them easier to scope
later.

---

## 2. Locked decisions

| ID | Item | Phase 12 status |
|----|------|---------------|
| **R-P11-init-drift** | Fix `hallucination/__init__.py` docstring | **In scope (Step 1)** |
| **R-P11-prompts-migrate** | Migrate one inline prompt to `prompts/` | **In scope (Step 2)** |
| **R-P11-l6-config** | Externalise Layer 6 constraints | **In scope (Step 3)** |
| **R-P10-1** | Sender HMAC rotate button | **In scope (Step 4)** |
| **R-P10-2** | Rotation history panel | **In scope (Step 4)** |
| **R-P11-baseline-1** | Seed `silver.collars` PLS-20-* fixture | Defer — Phase 13+ |
| **R-P11-baseline-2** | Seed pgeo corpus fixture | Defer — Phase 13+ |
| **R-P11-B** | Frontend Search/Query page | Defer — Phase 13+ |
| **R-P3-5** | Dual-write harness | Defer |
| **R-P3-6** | Hatchet HA | Defer |
| **R-P3-9** | Vendor profiles | Defer — SME-gated |

---

## 3. Done definition

Each step ships a verifier. Phase 12 passes when:

- Step 1 verifier proves the `hallucination/__init__.py` docstring
  accurately describes the actual state of Layer 2 and Layer 5
  (which are implemented, not "handled elsewhere").
- Step 2 verifier proves at least one inline prompt has been moved
  from `orchestrator.py` (or any agent file) into
  `src/fastapi/app/agent/prompts/<name>.py`, the registry has an
  entry for it, and the caller imports from `prompts/`.
- Step 3 verifier proves Layer 6 constraints load from a config
  file (JSON or YAML under `src/fastapi/app/agent/hallucination/`
  or a new `config/` location), not from inline Python literals.
- Step 4 verifier proves the admin UI has:
    1. A "Rotate HMAC" button per sender row (parallel to the
       Phase 9 JWT rotate pattern) that calls a new controller
       action.
    2. A "Rotation history" panel showing recent
       `workflow.jwt_key.rotated` + new
       `usage.external_notification_sender.hmac_rotated` audit
       rows.
- All prior phase verifiers still green (324 → ~360+).

---

## 4. Step-by-step

### Step 1 — Hallucination init.py docstring fix (R-P11-init-drift)
- Re-read `src/fastapi/app/agent/hallucination/__init__.py`.
- Update the "Layers 2 and 5 are handled elsewhere" block to
  reflect Phase 11 audit findings: Layer 2 has 128-line
  implementation, Layer 5 has 157-line implementation.
- Don't change any code logic — purely the docstring.
- Verifier: docstring no longer claims Layers 2/5 are "not
  implemented here"; references the actual file paths + line
  counts.

### Step 2 — Inline prompt migration (R-P11-prompts-migrate)
- Find one short, isolated inline prompt string (≤500 chars,
  module-level constant or clearly delimited f-string) in the
  agent code.
- Move it to `src/fastapi/app/agent/prompts/<name>.py` following
  the Phase 11 `example_system.py` pattern.
- Register in `_version_registry.py`.
- Update the caller to import from the new location.
- Verifier: import works in-container, callers updated, registry
  has the new entry.

### Step 3 — Layer 6 constraint externalisation (R-P11-l6-config)
- Audit `layer6_constraints.py` for inline numeric limits (max
  depth, max grade, etc.).
- Move limits to a JSON or YAML config file under
  `src/fastapi/app/agent/hallucination/layer6_constraints.json`
  (or similar).
- Update `layer6_constraints.py` to load on module import (cached
  thereafter).
- Verifier: config file exists with at least 3 documented
  constraints; module loads the config; round-trip a value
  through `check_geological_constraints` still works.

### Step 4 — Sender HMAC rotate + rotation history panel (R-P10-1 + R-P10-2)
- Two-in-one step. Add:
    1. `IntegrationsController::rotateSenderHmac()` action +
       `POST /admin/integrations/senders/{id}/rotate-hmac` route
       + form in `Integrations.tsx` (one button per sender row,
       not a footer form).
    2. `loadRotationHistory()` method on the controller that
       reads audit rows tagged `workflow.jwt_key.rotated` +
       `usage.external_notification_sender.hmac_rotated`. Surfaces
       as a "Rotation history" Inertia panel.
- Verifier: acting-as-admin probe rotates a sender HMAC (new kid
  lands in DB + audit row emits), and the rotation history prop
  reflects the new row.

### Step 5 — Phase 12 → Phase 13 handoff
- Same shape as previous handoffs.

---

## 5. Engineering invariants

- `scripts/phase12_master_sweep.sh` extends the Phase 11 sweep.
  Target: 100% green.
- No new database migrations (Step 4 reuses
  `usage.register_external_notification_sender` for the rotate path
  — same pattern as the Phase 10 Step 3 registration).
- Step 3's config file MUST NOT introduce new SME-side surface
  area without Kyle's input. Migrate existing hard-coded limits as-is;
  enable his future edits without code deploys.

---

## 6. Files of record (preview)

```
app/Http/Controllers/Admin/IntegrationsController.php             (mod — Step 4)
docs/phase12_implementation_kickoff.md                             (this file)
docs/phase12_handoff.md                                             (Step 5)
resources/js/Pages/Admin/Integrations.tsx                          (mod — Step 4)
routes/web.php                                                       (mod — Step 4)
scripts/phase12_master_sweep.sh                                    (Step 5)
scripts/phase12_step1_verify.sh                                    (Step 1)
scripts/phase12_step2_verify.sh                                    (Step 2)
scripts/phase12_step3_verify.sh                                    (Step 3)
scripts/phase12_step4_verify.sh                                    (Step 4)
src/fastapi/app/agent/hallucination/__init__.py                   (mod — Step 1)
src/fastapi/app/agent/hallucination/layer6_constraints.json       (Step 3 — config externalisation)
src/fastapi/app/agent/hallucination/layer6_constraints.py         (mod — Step 3; load from JSON)
src/fastapi/app/agent/prompts/<migrated_name>.py                  (Step 2)
src/fastapi/app/agent/prompts/_version_registry.py                 (mod — Step 2; new entry)
```

---

End of Phase 12 kickoff.
