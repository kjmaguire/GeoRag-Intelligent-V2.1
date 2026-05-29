# Phase 9 Handoff — Ops follow-throughs

**Document version:** 1.0
**Status:** Phase 9 complete. Phase 10 inheriting.
**Predecessors:** `docs/phase8_handoff.md`,
`docs/phase9_implementation_kickoff.md`.

---

## 1. What Phase 9 delivered

Phase 9 closed three small carry-overs Phase 8 left behind:
end-to-end Tempo verification for Dagster-emitted parse spans, an
operator-visible Rotate button on the JWT keys admin panel, and the
ACME email scaffolding that pairs with the Phase 8 issuer env switch.
Third "tight ops" phase in a row; no new infrastructure, no new
database tables.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | Dagster Tempo e2e probe: one-shot container parses fixture under `service.name=phase9-dagster-probe-*`, ≥6 spans land in Tempo within 60s — closes R-P5-3 (Dagster variant) | `scripts/phase9_step1_verify.sh` (5/5) |
| 2 | `IntegrationsController::rotateFlowKey()` + `POST /admin/integrations/jwt-keys/rotate` route + Inertia `RotateFlowKeyForm` component; runs `workflow.set_flow_jwt_secret(flow, kid, secret, overlap_hours)` inside a transaction with `app.audit_encryption_key` set | `scripts/phase9_step2_verify.sh` (6/6) |
| 3 | Caddy `email {$CADDY_ACME_EMAIL:...}` global directive + `CADDY_ACME_EMAIL` compose env; runbook simplified to env-only swap | `scripts/phase9_step3_verify.sh` (7/7) |
| 4 | This handoff | — |

**Phase 9 cumulative: 18 / 18 verifier checks** (5+6+7).
**Master sweep across Phase 0 → Phase 9 at close: 274 / 274 across
41 verifiers** (`scripts/phase9_master_sweep.sh`).

---

## 2. Architectural state at end of Phase 9

### 2.1 Orchestration ownership (unchanged from Phase 8)

No changes — Hatchet still single-instance, Kestra single-instance,
Dagster single-instance + image now confirmed exporting spans.

### 2.2 New surfaces

| Surface | Purpose | Phase 10 work |
|---------|---------|---------------|
| `POST /admin/integrations/jwt-keys/rotate` + `RotateFlowKeyForm` | Operators rotate JWT signing keys without SSH'ing to the host | Audit log entry per rotation (who/when/which flow) |
| `CADDY_ACME_EMAIL` env | ACME account registration email, env-driven | Actual production ACME activation with a real hostname + DNS / HTTP-01 |
| `phase9-dagster-probe-*` Tempo service-name pattern | Ad-hoc e2e probe for parse spans from the Dagster image | Wire the same probe into Phase 10's CI smoke if a CI pipeline lands |

### 2.3 Auth + TLS posture (unchanged)

The Phase 9 rotation surface uses the same Sanctum-gated `/admin/`
prefix as the rest of the Integrations page. Auth model didn't move.

### 2.4 Observability posture

**Closed the Dagster gap.** Spans from `parse_pdf_report` run inside
the Dagster image now reach Tempo end-to-end. Verifier 5/5 in
under a minute on a warm cluster.

Confirmed bottom-line OTel topology:

```
Hatchet workers (Phase 6 Step 1)  →┐
Dagster image (Phase 8 Step 1)    →┤→ otel-collector:4318 → Tempo:3200
phase9-dagster-probe (Step 1)     →┘
```

---

## 3. Operational state

Same as Phase 8 plus:

- The `/admin/integrations` page now has a Rotate button at the
  bottom of the "Per-flow JWT keys" panel. Pick a flow + overlap
  hours, click Rotate — the prior kid stays valid for the overlap
  window, the new one signs going forward.
- `CADDY_ACME_EMAIL=ops@example.com` + `CADDY_TLS_ISSUER=acme` in
  `.env` is the complete production-TLS swap (no Caddyfile edit).
- Dagster image will emit parse spans whenever a `silver_reports`
  materialization runs against the otel-collector — operationally
  this fires on the daily 02:00 UTC schedule (currently `STOPPED`
  default; flip via Dagster UI when an asset run is expected).

---

## 4. Carry-overs for Phase 10

| ID | Item | Where | Phase 10 rationale |
|----|------|-------|---------------------|
| **R-P3-5** | Generalised dual-write harness | hard-coded workflow_kind | Re-evaluate when the second migration target lands. |
| **R-P3-6** | Hatchet engine HA | docker-compose | Path B per `phase8_hatchet_ha_design.md` if reopened. |
| **R-P3-9** | Vendor-profile column-mapping | `parse_pdf_report` | Needs Kyle SME input. |
| **R-P9-1** | Audit log for JWT rotations | `audit.audit_ledger` | Step 2 ships the rotation action but doesn't write to the audit ledger. Add `action_type='workflow.jwt_key.rotated'` emission. |
| **R-P9-2** | Real ACME activation in a prod env | `caddy/Caddyfile` + DNS | Step 3 wired the scaffolding; setting `CADDY_TLS_ISSUER=acme` against a real hostname is a deploy-time, not code-time, follow-on. |
| **R-P9-3** | Phase 5 Step 1 verifier — minute-boundary flake | `phase5_step1_verify.sh` | The over-limit check intermittently fails when test execution crosses a UTC-minute boundary (rate limiter is a fixed minute window). Two retries usually pass. Fix: send all 4 in a tighter burst OR switch to a sliding window. |
| **R-P5-3** | End-to-end Tempo probe — Dagster variant | CLOSED at Step 1. |
| **R-P8-1** | Rotate-with-overlap button | CLOSED at Step 2. |
| **R-P8-2** | ACME wiring scaffold | CLOSED at Step 3. |

---

## 5. Files of record

**New in Phase 9:**

```
app/Http/Controllers/Admin/IntegrationsController.php             (mod — Step 2; rotateFlowKey action)
caddy/Caddyfile                                                    (mod — Step 3; email directive)
docker-compose.yml                                                 (mod — Step 3; CADDY_ACME_EMAIL env)
docs/phase9_implementation_kickoff.md                              (Step 0)
docs/phase9_handoff.md                                              (this file)
docs/runbooks/caddy_tls.md                                         (mod — Step 3; env-only swap)
resources/js/Pages/Admin/Integrations.tsx                          (mod — Step 2; RotateFlowKeyForm)
routes/web.php                                                       (mod — Step 2; rotate POST route)
scripts/_phase9_step2_probe.php                                    (Step 2 verifier helper)
scripts/phase9_master_sweep.sh                                     (Step 4)
scripts/phase9_step1_verify.sh                                     (Step 1)
scripts/phase9_step2_verify.sh                                     (Step 2)
scripts/phase9_step3_verify.sh                                     (Step 3)
```

**Archived in Phase 9:** none.

---

## 6. Re-running every Phase 9 verifier

```bash
bash scripts/phase9_step1_verify.sh   # Dagster Tempo e2e        (5/5)
bash scripts/phase9_step2_verify.sh   # Rotate button + DB       (6/6)
bash scripts/phase9_step3_verify.sh   # ACME wiring scaffold     (7/7)
```

Step 1 needs `COMPOSE_NETWORK=georag` (default in the master sweep)
to attach the probe container to the otel-collector network.

Combined Phase 0 → Phase 9 sweep — **41 verifiers, 274 total checks**
(`scripts/phase9_master_sweep.sh`).

---

## 7. Phase 10 entry checklist

Before Phase 10 work begins:

1. Read this handoff + Phase 8 handoff + Phase 9 kickoff.
2. Re-run `scripts/phase9_master_sweep.sh` — confirm 274/274 green.
   Phase 5 Step 1 flakes occasionally on minute boundaries (R-P9-3);
   a second run usually clears it.
3. Decide Phase 10 scope. Candidates:
   - **R-P9-1** (audit row per rotation) + **R-P9-3** (fix the
     rate-limit verifier flake) — small ops items, pairs well with
     a third item.
   - **R-P3-9** (vendor profiles) — finally tackle the ingestion-side
     work if Kyle's available for SME input.
   - **R-P3-6** (Hatchet HA) — Path B per the design doc.
   - Fresh territory: golden-query suite, RAG pipeline hardening,
     or an untouched ingest format.

Three small ops phases in a row (Phase 7, 8, 9) means Phase 10 is
the right point to either pivot to a meatier item or call the
integration-edge + auth + observability work "done" and move to RAG
quality / ingestion breadth.

End of Phase 9 handoff.
