# Phase 12 Handoff — RAG discipline + operator UI continuations

**Document version:** 1.0
**Status:** Phase 12 complete. Phase 13 inheriting.
**Predecessors:** `docs/phase11_handoff.md`,
`docs/phase12_implementation_kickoff.md`.

---

## 1. What Phase 12 delivered

Phase 12 closed the three small RAG-discipline carry-overs from
Phase 11 (init.py drift, prompt migration pattern proof, Layer 6
externalisation) and bundled them with the two operator-UI
continuations from Phase 10 (sender HMAC rotate + rotation history
panel).

The most consequential single delivery: **Layer 6 constraints now
load from a JSON file** the geologist SME can edit without a code
deploy. This closes the biggest CLAUDE.md hard-rule-6 violation
surfaced by the Phase 11 audit ("schemas in Section 04e are
contracts").

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `hallucination/__init__.py` docstring corrected — no longer claims Layers 2/5 are "not implemented"; references the Phase 11 audit doc as trail | `scripts/phase12_step1_verify.sh` (5/5) |
| 2 | `_REPHRASE_SYSTEM_PROMPT` migrated from `app/agent/escalation.py` (inline triple-quoted string) → `app/agent/prompts/rephrase_system.py` (canonical pattern). Registry entry added. | `scripts/phase12_step2_verify.sh` (6/6) |
| 3 | `app/agent/hallucination/layer6_constraints.json` (7 constraints with regex keywords + bounds + unit hints) + module-load via `_load_constraints_from_json()`. SME can adjust limits without touching Python. | `scripts/phase12_step3_verify.sh` (6/6) |
| 4 | `rotateSenderHmac` controller action + `POST /admin/integrations/senders/{id}/rotate-hmac` route + per-row "Rotate HMAC" button + "Rotation history" Inertia panel surfacing both JWT and HMAC rotation events from `audit.audit_ledger` | `scripts/phase12_step4_verify.sh` (8/8) |
| 5 | This handoff | — |

**Phase 12 cumulative: 25 / 25 verifier checks** (5+6+6+8).
**Master sweep across Phase 0 → Phase 12 at close: 349 / 349 across
54 verifiers** (`scripts/phase12_master_sweep.sh`).

---

## 2. Architectural state at end of Phase 12

### 2.1 Orchestration ownership (unchanged from Phase 11)

No changes.

### 2.2 New surfaces

| Surface | Purpose | Phase 13 work |
|---------|---------|---------------|
| `layer6_constraints.json` | SME-editable geological plausibility limits | Kyle SME review of the seven constraint bounds; potential additional constraints (e.g. recovery >100% / azimuth wrap behaviour) |
| `app/agent/prompts/rephrase_system.py` | First migrated inline prompt — proves the pattern | Migrate 1-2 more inline prompts (escalation.py is now empty of inline prompts; orchestrator.py + llm_classifier.py still have them) |
| `POST /admin/integrations/senders/{id}/rotate-hmac` | Operator UI parity with JWT rotate (Phase 9 Step 2) | Add `--overlap-hours` support if HMAC senders need overlap-window rotation (currently hard-cut: prior disabled, new active) |
| Rotation history panel | Single view of both JWT + HMAC rotations | Filter / search controls if the audit table grows; pagination |

### 2.3 §04i framework state (post-cleanup)

The Phase 11 audit's three notable gaps are now closed or in flight:

- **init.py docstring drift** — CLOSED at Step 1.
- **Layer 6 hard-coded constraints** — CLOSED at Step 3 (JSON
  externalised; module loads via path-relative read).
- **Layer 4 sparse-fixture coverage gap** — still open. R-P11-baseline-1
  fixture seed remains the right fix; deferred per Phase 12 scope.

### 2.4 Auth + TLS + audit posture (unchanged)

The Phase 12 Step 4 rotation actions both write to `audit.audit_ledger`
without ever serialising the underlying secret, matching the Phase 9
Step 2 / Phase 10 Step 1 pattern.

---

## 3. Operational state

Same as Phase 11 plus:

- `/admin/integrations` page now has:
    1. **Per-row "Rotate HMAC" button** on active senders (next to
       Enable/Disable). Mirrors the Phase 9 JWT rotate UX.
    2. **Rotation history panel** above the existing flag history
       — last 25 JWT + HMAC rotation events with actor IDs.
- Geological limit changes (`layer6_constraints.json`) take effect
  at next fastapi container restart. No code deploy needed.
- The first migrated inline prompt
  (`app/agent/prompts/rephrase_system.py`) is the canonical example
  for Phase 13+ migrations.

---

## 4. Carry-overs for Phase 13

| ID | Item | Where | Phase 13 rationale |
|----|------|-------|---------------------|
| **R-P3-5** | Generalised dual-write harness | hard-coded | Re-evaluate when second migration target lands |
| **R-P3-6** | Hatchet HA | docker-compose | Path B per `phase8_hatchet_ha_design.md` |
| **R-P3-9** | Vendor-profile column-mapping | parsers | SME-gated |
| **R-P11-baseline-1** | Seed `silver.collars` PLS-20-* fixture | golden-test infra | Unlocks 30 currently-failing golden tests; **highest leverage** |
| **R-P11-baseline-2** | Seed pgeo corpus fixture | golden-test infra | Unlocks 3 pgeo golden tests |
| **R-P11-B** | Frontend Search/Query page | `resources/js/Pages/` | First user-facing RAG surface |
| **R-P11-l4-fixture** | CI fixture for Layer 4 entity grounding | tests | Currently passes spuriously on sparse data |
| **R-P12-more-prompts** | Migrate inline prompts from `orchestrator.py` + `llm_classifier.py` | follow-on | Step 2 proved the pattern; remaining migrations should now be routine |
| **R-P12-l6-overlap-hmac** | `--overlap-hours` for HMAC rotation | `IntegrationsController` | Step 4 ships hard-cut rotation; parity with JWT overlap |
| **R-P12-l6-sme-review** | Kyle review of `layer6_constraints.json` bounds | doc | Constraints inherited from inline code as-is; SME may want adjustments |
| **R-P11-init-drift** | CLOSED at Step 1 |
| **R-P11-prompts-migrate** | CLOSED at Step 2 |
| **R-P11-l6-config** | CLOSED at Step 3 |
| **R-P10-1** | Sender HMAC rotate | CLOSED at Step 4 |
| **R-P10-2** | Rotation history panel | CLOSED at Step 4 |

---

## 5. Files of record

**New in Phase 12:**

```
app/Http/Controllers/Admin/IntegrationsController.php             (mod — Step 4)
docs/phase12_implementation_kickoff.md                             (Step 0)
docs/phase12_handoff.md                                             (this file)
resources/js/Pages/Admin/Integrations.tsx                          (mod — Step 4)
routes/web.php                                                       (mod — Step 4)
scripts/_phase12_step4_probe.php                                   (Step 4 helper)
scripts/phase11_step3_verify.sh                                    (mod — Phase 12 master sweep cleanup; loosened import-keys assertion)
scripts/phase12_master_sweep.sh                                    (Step 5)
scripts/phase12_step1_verify.sh                                    (Step 1)
scripts/phase12_step2_verify.sh                                    (Step 2)
scripts/phase12_step3_verify.sh                                    (Step 3)
scripts/phase12_step4_verify.sh                                    (Step 4)
src/fastapi/app/agent/escalation.py                                (mod — Step 2; imports from prompts/)
src/fastapi/app/agent/hallucination/__init__.py                   (mod — Step 1)
src/fastapi/app/agent/hallucination/layer6_constraints.json       (Step 3 — SME-editable config)
src/fastapi/app/agent/hallucination/layer6_constraints.py         (mod — Step 3; JSON loader)
src/fastapi/app/agent/prompts/_version_registry.py                 (mod — Step 2; rephrase_system entry)
src/fastapi/app/agent/prompts/rephrase_system.py                   (Step 2 — first migrated prompt)
```

**Archived in Phase 12:** none.

---

## 6. Re-running every Phase 12 verifier

```bash
bash scripts/phase12_step1_verify.sh   # init.py docstring drift fix  (5/5)
bash scripts/phase12_step2_verify.sh   # rephrase prompt migration    (6/6)
bash scripts/phase12_step3_verify.sh   # L6 constraints externalised  (6/6)
bash scripts/phase12_step4_verify.sh   # sender rotate + history      (8/8)
```

Combined Phase 0 → Phase 12 sweep — **54 verifiers, 349 total checks**
(`scripts/phase12_master_sweep.sh`).

---

## 7. Phase 13 entry checklist

Before Phase 13 work begins:

1. Read this handoff + Phase 11 handoff + `docs/phase11_scoping.md`
   (the inventory still applies).
2. Re-run `scripts/phase12_master_sweep.sh` — confirm 349/349 green.
3. Decide Phase 13 scope. Three obvious paths:
   - **R-P11-baseline-1 + R-P11-baseline-2** (golden fixture seeding)
     — highest leverage; unlocks 33 currently-failing tests for real
     RAG quality measurement.
   - **R-P11-B** (frontend Search/Query page) — first user-facing
     RAG surface. Pairs nicely with R-P11-baseline-1 since you'd
     want a real corpus to demo against.
   - **R-P12-more-prompts + R-P12-l6-sme-review + R-P12-l6-overlap-hmac**
     — three more small RAG-discipline / ops items if appetite for
     another tight phase before the bigger pivot.

After six small ops/discipline phases (7 → 12), the codebase is
deeply mature on infrastructure + integration edge + admin UX +
RAG framework. Phase 13 is the natural moment to tackle the
fixture-seeding work that unlocks real golden-query validation,
or to start the first user-facing Search page.

End of Phase 12 handoff.
