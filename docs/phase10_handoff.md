# Phase 10 Handoff — Ops close-out + Phase 11 pivot scoping

**Document version:** 1.0
**Status:** Phase 10 complete. Phase 11 inheriting.
**Predecessors:** `docs/phase9_handoff.md`,
`docs/phase10_implementation_kickoff.md`.

---

## 1. What Phase 10 delivered

Phase 10 closed the two carry-overs Phase 9 left (audit on rotation,
rate-limit verifier flake), added one operator-visibility item that
pairs with the Phase 9 Rotate button (Add Sender form with one-shot
secret reveal), and — most consequentially — produced the **Phase 11
scoping doc** off an Explore-agent inventory of the actual codebase
state. That doc reframes Phase 11 from "build RAG" to "validate the
RAG framework that's already there."

| Step | Output | Verifier |
|------|--------|----------|
| 1 | Audit ledger emission on JWT rotation (`action_type='workflow.jwt_key.rotated'`, secret NEVER in payload) — closes R-P9-1 | `scripts/phase10_step1_verify.sh` (6/6) |
| 2 | `phase5_step1_verify.sh` burst-send refactor + minute-alignment guard — closes R-P9-3 flake; three consecutive runs now pass | `scripts/phase10_step2_verify.sh` (3/3) |
| 3 | `registerSender` controller action + `POST /admin/integrations/senders` route + `RegisterSenderForm` Inertia component + one-shot secret banner | `scripts/phase10_step3_verify.sh` (7/7) |
| 4 | `docs/phase11_scoping.md` — codebase inventory + 3 Phase 11 candidate paths (golden-query expansion, frontend Search page, prompts/ canonicalisation) with effort estimates | `scripts/phase10_step4_verify.sh` (6/6) |
| 5 | This handoff | — |

**Phase 10 cumulative: 22 / 22 verifier checks** (6+3+7+6).
**Master sweep across Phase 0 → Phase 10 at close: 296 / 296 across
45 verifiers** (`scripts/phase10_master_sweep.sh`).

---

## 2. Architectural state at end of Phase 10

### 2.1 Orchestration ownership (unchanged from Phase 9)

No changes. Hatchet still single-instance, Kestra still single-instance,
Dagster image OTel-ready, Caddy edge TLS-enabled.

### 2.2 New surfaces

| Surface | Purpose | Phase 11 work |
|---------|---------|---------------|
| `audit.audit_ledger` rows tagged `workflow.jwt_key.rotated` | Operator-visible trail of every key rotation | Surface in `/admin/integrations` as a "Rotation history" panel |
| `POST /admin/integrations/senders` + `RegisterSenderForm` | Operators add senders without SSH | Pair with a "rotate this sender's HMAC" button (currently CLI-only) |
| One-shot `sender_secret` flash banner | One-time secret reveal, never persisted | Same pattern reusable for other "secret reveal" UIs |
| `docs/phase11_scoping.md` | Phase 11 scoping reference | Drives Phase 11 kickoff |

### 2.3 Auth + TLS posture (unchanged)

No movement. Sanctum + admin Gate everywhere relevant. Caddy edge TLS
defaults to internal CA, ACME-ready via env swap.

### 2.4 RAG / agent posture (newly inventoried)

The Phase 11 scoping doc surfaced what's already in the tree:

- **12 parsers** (PDF, CSV variants, DOCX, LAS, raster, SEGY, spatial,
  XLSX, XYZ) totalling ~6.4k lines under
  `src/dagster/georag_dagster/parsers/`.
- **30 agent files** under `src/fastapi/app/agent/`. `orchestrator.py`
  is 5184 lines; `tools.py` is 1632 lines.
- **Section 04i hallucination defence is implemented** — 10 files
  covering all six layers + completeness + validators +
  qualitative_detector.
- **134 test files** total (49 fastapi + 29 dagster + 56 Laravel),
  including `test_golden_queries.py` + `test_public_geoscience_golden.py`.
- **Three notable gaps:** no `src/fastapi/app/agent/prompts/` subdirectory,
  no React Search/Query page in `resources/js/Pages/`, no
  frontend citation rendering.

---

## 3. Operational state

Same as Phase 9 plus:

- `/admin/integrations` page has a "Register a new sender" section
  with the secret revealed once on success.
- Every JWT rotation through the Phase 9 Rotate button lands an
  audit row. Query `SELECT * FROM audit.audit_ledger
  WHERE action_type = 'workflow.jwt_key.rotated' ORDER BY created_at DESC`
  for the history.
- `phase5_step1_verify.sh` is no longer flaky on minute boundaries —
  three runs back to back pass.

---

## 4. Carry-overs for Phase 11

| ID | Item | Where | Phase 11 rationale |
|----|------|-------|---------------------|
| **R-P3-5** | Generalised dual-write harness | hard-coded workflow_kind | Re-evaluate when the second migration target lands. |
| **R-P3-6** | Hatchet HA | docker-compose | Path B per `phase8_hatchet_ha_design.md` if a forcing function lands. |
| **R-P3-9** | Vendor-profile column-mapping | parsers | Needs Kyle SME input. |
| **R-P9-2** | Real ACME activation in prod | Caddy + DNS | Deploy-time, not code-time. |
| **R-P10-1** | "Rotate this sender's HMAC" button | `IntegrationsController` + `Integrations.tsx` | Step 3 added register; the parallel rotate is still CLI-only (`scripts/phase4_sender_register.sh rotate`). |
| **R-P10-2** | Rotation history panel | `Integrations.tsx` | The audit-ledger emission lands operators a trail but there's no UI surface for it yet. |
| **R-P11-A** | Golden-query suite expansion + Section 04i audit | tests + agent layers | Path A from `phase11_scoping.md` — the recommended Phase 11. |
| **R-P11-B** | Frontend Search/Query page | `resources/js/Pages/` | Path B — deferred to Phase 12. |
| **R-P11-C** | `prompts/` subdirectory canonicalisation | `src/fastapi/app/agent/prompts/` | Path C — pairs well with Path A. |
| **R-P9-1** | Audit row per JWT rotation | CLOSED at Step 1. |
| **R-P9-3** | Phase 5 Step 1 verifier flake | CLOSED at Step 2. |

---

## 5. Files of record

**New in Phase 10:**

```
app/Http/Controllers/Admin/IntegrationsController.php             (mod — Steps 1, 3)
docs/phase10_implementation_kickoff.md                             (Step 0)
docs/phase10_handoff.md                                             (this file)
docs/phase11_scoping.md                                            (Step 4)
resources/js/Pages/Admin/Integrations.tsx                          (mod — Step 3; RegisterSenderForm + banner)
routes/web.php                                                       (mod — Step 3; sender register POST)
scripts/_phase10_step3_probe.php                                   (Step 3 verifier helper)
scripts/phase5_step1_verify.sh                                     (mod — Step 2; burst-send + alignment)
scripts/phase10_master_sweep.sh                                    (Step 5)
scripts/phase10_step1_verify.sh                                    (Step 1)
scripts/phase10_step2_verify.sh                                    (Step 2)
scripts/phase10_step3_verify.sh                                    (Step 3)
scripts/phase10_step4_verify.sh                                    (Step 4)
```

**Bugs discovered + fixed during the sweep:**

- `IntegrationsController::index()` called `$request->session()->pull(...)`
  to surface the Phase 10 Step 3 sender-secret flash. Test probes
  construct Request objects without a session, breaking the Phase 8
  Step 2 + Phase 9 Step 2 + Phase 10 Step 1 verifiers in the sweep.
  Fix: guard with `$request->hasSession() ?` so probe contexts get
  null gracefully.

**Archived in Phase 10:** none.

---

## 6. Re-running every Phase 10 verifier

```bash
bash scripts/phase10_step1_verify.sh   # JWT rotation audit       (6/6)
bash scripts/phase10_step2_verify.sh   # rate-limit verifier fix  (3/3)
bash scripts/phase10_step3_verify.sh   # sender registration UI   (7/7)
bash scripts/phase10_step4_verify.sh   # Phase 11 scoping doc     (6/6)
```

Combined Phase 0 → Phase 10 sweep — **45 verifiers, 296 total checks**
(`scripts/phase10_master_sweep.sh`). Step 2 is slower than the others
(up to ~60s waiting for the rate-limit minute alignment + three
sequential phase5_step1 runs).

---

## 7. Phase 11 entry checklist

Before Phase 11 work begins:

1. **Read `docs/phase11_scoping.md` first.** It changes the framing.
2. Read this handoff + Phase 9 handoff + the kickoff template.
3. Re-run `scripts/phase10_master_sweep.sh` — confirm 296/296 green.
4. Scope Phase 11 from the scoping doc's recommendation: **Path A
   (golden-query expansion + Section 04i audit) + Path C (prompts/
   canonicalisation)** as a combined ~7-step phase.
5. If Kyle SME input lands before Phase 11 opens, **R-P3-9 vendor
   profiles** is a strong alternative — but defer if Kyle's
   unavailable. Don't fly blind on geological decisions.

Four "tight ops" phases in a row (Phase 7 → 10). The integration-edge
+ auth + observability + admin-UI arc is **mature**. Phase 11 is the
natural pivot to product-shaped work.

End of Phase 10 handoff.
