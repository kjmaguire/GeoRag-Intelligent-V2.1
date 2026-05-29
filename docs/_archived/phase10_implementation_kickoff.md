# Phase 10 Implementation Kickoff — Ops close-out + Phase 11 pivot scoping

**Document version:** 1.0
**Status:** Active.
**Predecessors:** `docs/phase9_handoff.md`.

---

## 1. Theme

Phase 7, 8, and 9 were three consecutive tight ops phases. Phase 10
closes the last two carry-overs they generated (audit on JWT rotation,
flaky rate-limit verifier), adds one operator-visibility item that
pairs with the Phase 9 Rotate button (Add Sender form), and **spawns
scoping work for Phase 11** — the natural pivot point after the
integration-edge / auth / observability arc is now mature.

Phase 11 should be either an SME-driven ingestion phase (vendor
profiles + parser quality) OR the first RAG-pipeline-quality phase
(golden queries + hallucination defence). Phase 10 Step 4 inventories
what already exists so Phase 11 starts with eyes open.

---

## 2. Locked decisions

| ID | Item | Phase 10 status |
|----|------|---------------|
| **R-P9-1** | Audit row per JWT rotation | **In scope (Step 1)** |
| **R-P9-3** | Fix `phase5_step1_verify.sh` minute-boundary flake | **In scope (Step 2)** |
| **(new)** | Add-sender form on `/admin/integrations` | **In scope (Step 3)** |
| **(new)** | Phase 11 scoping doc — RAG pipeline + golden queries inventory | **In scope (Step 4)** |
| **R-P3-5** | Generalised dual-write harness | Defer |
| **R-P3-6** | Hatchet HA | Defer |
| **R-P3-9** | Vendor profiles | Defer to Phase 11 (depends on Step 4 outcome) |
| **R-P9-2** | Real ACME activation | Defer — deploy-time, not code-time |

---

## 3. Done definition

Each step ships a verifier. Phase 10 passes when:

- Step 1 verifier proves an audit row lands in `audit.audit_ledger`
  with `action_type='workflow.jwt_key.rotated'` for every Rotate
  button submit, including the requesting user + flow + new kid.
- Step 2 verifier proves `phase5_step1_verify.sh` passes 6/6 on
  three consecutive runs (the prior flake was crossing UTC-minute
  boundaries; the fix sends all 4 probes in a tight burst).
- Step 3 verifier proves admins can register a new sender via the
  Integrations page, the resulting row lands in
  `usage.external_notification_senders`, the generated HMAC secret
  is surfaced in flash once + never persisted to logs.
- Step 4 verifier proves `docs/phase11_scoping.md` exists, lists
  the RAG/ingestion artifacts that already exist in the tree
  (parsers, agent prompts, citations infrastructure), and proposes
  3 candidate Phase 11 scopes ranked by ease + value.
- All prior phase verifiers still green (274 → ~295+).

---

## 4. Step-by-step

### Step 1 — Audit on JWT rotation (R-P9-1)
- Extend `IntegrationsController::rotateFlowKey()` to insert into
  `audit.audit_ledger` after the DB rotation succeeds. Payload:
  flow_name, prior_kid (NULL if first), new_kid, overlap_hours,
  acting user id.
- Verifier: trigger a rotation through the controller, assert one
  new audit row matches the expected shape.

### Step 2 — Rate-limit verifier flake fix (R-P9-3)
- Phase 5 Step 1's "above-limit" check sends 4 requests sequentially,
  waiting for COMPLETED on each. When execution crosses a UTC-minute
  boundary, the bucket resets and the 4th send is allowed.
- Fix: send all 4 requests in a tight burst (skip the `wait_completed`
  between sends), then wait for all 4 to settle, then assert. The
  burst keeps all increments inside one minute window.
- Verifier: run `phase5_step1_verify.sh` three times in a row; all
  must report 6/6.

### Step 3 — Add-sender form (new ops item)
- Add `registerSender` controller action that wraps
  `usage.register_external_notification_sender(...)` with the
  encryption-key GUC pattern from Step 1 (Phase 6) / rotate (Phase 9).
- Add `RegisterSenderForm` Inertia component below the Senders table.
- Flash the generated HMAC secret on success — operator copies it
  out, never stored client-side, never logged.
- Verifier: acting-as-admin probe submits the form, confirms the
  row lands in `usage.external_notification_senders` and the secret
  appears in the flash bag but NOT in
  `audit.audit_ledger.payload`.

### Step 4 — Phase 11 scoping (new)
- Spawn an Explore subagent to inventory: existing parser files,
  Pydantic AI agent prompts, retrieval-pipeline code, citation
  infrastructure, RAG-related tests, golden-query stubs (if any),
  hallucination-defence components per Section 04i.
- Write `docs/phase11_scoping.md` summarising the inventory + 3
  candidate Phase 11 scopes:
    A. Golden-query suite + hallucination-defence wiring
    B. R-P3-9 vendor profiles (gated on SME availability)
    C. R-P3-6 Hatchet HA Path B
- Verifier: file exists, lists ≥5 inventoried artifacts with file
  paths, lists 3 candidates A/B/C with effort + value estimates.

### Step 5 — Phase 10 → Phase 11 handoff
- Same shape as previous handoffs.

---

## 5. Engineering invariants

- `scripts/phase10_master_sweep.sh` extends the Phase 9 sweep.
  Target: 100% green.
- No new database migrations (rotation audit + sender register
  both go through existing tables / functions).
- Step 3 must never surface the HMAC secret in the audit ledger
  payload or in logs.

---

## 6. Files of record (preview)

```
app/Http/Controllers/Admin/IntegrationsController.php             (mod — Steps 1, 3)
docs/phase10_implementation_kickoff.md                             (this file)
docs/phase10_handoff.md                                             (Step 5)
docs/phase11_scoping.md                                            (Step 4)
resources/js/Pages/Admin/Integrations.tsx                          (mod — Step 3)
routes/web.php                                                       (mod — Step 3; sender register POST)
scripts/phase5_step1_verify.sh                                     (mod — Step 2; burst-sends fix)
scripts/phase10_master_sweep.sh                                    (Step 5)
scripts/phase10_step1_verify.sh                                    (Step 1)
scripts/phase10_step2_verify.sh                                    (Step 2)
scripts/phase10_step3_verify.sh                                    (Step 3)
scripts/phase10_step4_verify.sh                                    (Step 4)
```

---

End of Phase 10 kickoff.
