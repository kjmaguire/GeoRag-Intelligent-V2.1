# Phase 9 Implementation Kickoff — Ops follow-throughs

**Document version:** 1.0
**Status:** Active.
**Predecessors:** `docs/phase8_handoff.md`.

---

## 1. Theme

Phase 8 closed Phase 7's R-P7-* gaps and put a design doc against
R-P3-6 (Hatchet HA). Three small R-P8-* / R-P5-3 items remained.
Phase 9 closes all of them. This is the third successive "tight ops"
phase (Phase 7 + 8 + 9); after this, Phase 10 likely pivots either
to fresh territory (golden-query suite, RAG pipeline hardening, an
untouched ingest format) or to R-P3-6 if a forcing function lands.

Phase 9 is small + low-risk by design. No new infrastructure, no
new database tables, just close-out of three flagged items.

---

## 2. Locked decisions

| ID | Item | Phase 9 status |
|----|------|---------------|
| **R-P5-3 (Dagster variant)** | Tempo e2e probe for Dagster-emitted parse spans | **In scope (Step 1)** |
| **R-P8-1** | Rotate-with-overlap button in admin UI | **In scope (Step 2)** |
| **R-P8-2** | ACME wiring scaffold on Caddy edge | **In scope (Step 3)** |
| **R-P3-5** | Generalised dual-write harness | Defer |
| **R-P3-6** | Hatchet engine HA | Defer per Phase 8 design doc |
| **R-P3-9** | Vendor-profile column-mapping | Defer — needs SME input |

---

## 3. Done definition

Each step ships a verifier. Phase 9 passes when:

- Step 1 verifier brings up the dagster image briefly, calls
  `parse_pdf_report` against the fixture under
  `service.name=georag-dagster-daemon`, forces a span flush, then
  confirms ≥6 spans appear in Tempo's search API for that service.
- Step 2 verifier proves the admin UI has a Rotate button per
  per-flow row, that submitting it (admin-acting probe) calls the
  Phase 6 Step 3 multi-kid set function with a non-zero
  overlap_hours, and that the resulting row appears in
  `workflow.flow_jwt_keys`.
- Step 3 verifier proves the Caddyfile carries an `acme_email`
  global directive driven by `CADDY_ACME_EMAIL` env, the default
  (internal CA) path still works, and switching the env produces
  a different config that `caddy validate`s cleanly (even without
  actually issuing through ACME).
- All prior phase verifiers still green (256 → ~275+).

---

## 4. Step-by-step

### Step 1 — Dagster Tempo e2e probe (R-P5-3 Dagster variant)
- Verifier brings up a one-shot dagster container (overriding the
  default `dagster-daemon run` command) that runs an ad-hoc Python
  script: bootstrap tracer with
  `OTEL_SERVICE_NAME=phase9-dagster-probe`, call `parse_pdf_report`
  on the fixture, force_flush.
- Then probe Tempo `/api/search?tags=service.name=phase9-dagster-probe`
  for ≥6 spans.
- Doesn't require running the full Dagster daemon — just a fresh
  Python process inside the rebuilt image confirming the bootstrap
  path fires end-to-end.

### Step 2 — Rotate button (R-P8-1)
- Add a `rotateFlowKey` controller action on
  `IntegrationsController` taking `flow_name` + `overlap_hours`
  (default 24). Wraps `workflow.set_flow_jwt_secret(...)`.
- Add a small form per row in the JWT keys panel: kid + overlap
  hours + submit button. Inertia POST → controller → DB.
- Verifier: acting-as-admin probe submits the form, confirms a new
  row in `flow_jwt_keys` with valid_from=now + the previous row's
  `valid_until` set to ~now+overlap_hours.

### Step 3 — ACME wiring scaffold (R-P8-2)
- Add `acme_email {$CADDY_ACME_EMAIL:ops@example.invalid}` to the
  Caddyfile global block. Always emitted; only consulted when an
  ACME issuer is active.
- Compose: add `CADDY_ACME_EMAIL` env var to the caddy service
  block.
- Update the runbook to remove the "edit Caddyfile" step — now
  just an env-var swap.
- Verifier: env-override probe — set `CADDY_ACME_EMAIL=test@local`,
  confirm Caddy boots + healthz still returns 200; restore.

### Step 4 — Phase 9 → Phase 10 handoff
- Same shape as previous handoffs.

---

## 5. Engineering invariants

- `scripts/phase9_master_sweep.sh` extends the Phase 8 sweep with
  the three new verifiers. Target: 100% green.
- No new database migrations (Step 2 reuses Phase 6 Step 3's
  `set_flow_jwt_secret` function).
- Step 1's Tempo probe MUST cleanup after itself (no orphan
  containers; no Tempo state pollution beyond the probe's
  service.name namespace).

---

## 6. Files of record (preview)

```
app/Http/Controllers/Admin/IntegrationsController.php             (mod — Step 2; rotateFlowKey action)
caddy/Caddyfile                                                    (mod — Step 3; acme_email global)
docker-compose.yml                                                 (mod — Step 3; CADDY_ACME_EMAIL env)
docs/phase9_implementation_kickoff.md                              (this file)
docs/phase9_handoff.md                                              (Step 4)
docs/runbooks/caddy_tls.md                                         (mod — Step 3; simplified swap)
resources/js/Pages/Admin/Integrations.tsx                          (mod — Step 2; rotate form)
routes/web.php                                                       (mod — Step 2; PATCH /admin/integrations/jwt-keys/...)
scripts/phase9_master_sweep.sh                                     (Step 4)
scripts/phase9_step1_verify.sh                                     (Step 1)
scripts/phase9_step2_verify.sh                                     (Step 2)
scripts/phase9_step3_verify.sh                                     (Step 3)
```

---

End of Phase 9 kickoff.
