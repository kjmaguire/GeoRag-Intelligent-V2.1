# Phase 8 Handoff — Phase 7 close-outs + HA scoping

**Document version:** 1.0
**Status:** Phase 8 complete. Phase 9 inheriting.
**Predecessors:** `docs/phase7_handoff.md`,
`docs/phase8_implementation_kickoff.md`.

---

## 1. What Phase 8 delivered

Phase 8 cleared the three R-P7-* carry-overs and put a design doc
against R-P3-6 (Hatchet HA, deferred five times). No new feature
surfaces — the integration edge stays the shape Phase 6 + 7 left
it. The two notable additions: the dagster image now ships
OpenTelemetry deps so Phase 7's bootstrap call actually fires, and
the admin UI surfaces the multi-kid JWT history operators have been
managing only via CLI.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | Rebuilt `georag/dagster:latest` image now contains `opentelemetry-{api,sdk,exporter-otlp-proto-http}` v1.41.1 — closes R-P7-1 runtime gap | `scripts/phase8_step1_verify.sh` (6/6) |
| 2 | `/admin/integrations` page gained a "Per-flow JWT keys" panel showing kid + valid_from/until + active status; admin-gated | `scripts/phase8_step2_verify.sh` (5/5) |
| 3 | Caddy TLS issuer parametrized via `{$CADDY_TLS_ISSUER:internal}`; runbook at `docs/runbooks/caddy_tls.md` covers the internal→acme swap | `scripts/phase8_step3_verify.sh` (6/6) |
| 4 | `docs/phase8_hatchet_ha_design.md` — full scoping of multi-instance Hatchet (current posture, failure modes, multi-engine architecture, worker adaptation, state-loss boundaries, operational ask, three Phase 9 paths with recommendation) | `scripts/phase8_step4_verify.sh` (5/5) |
| 5 | This handoff | — |

**Phase 8 cumulative: 22 / 22 verifier checks** (6+5+6+5).
**Master sweep across Phase 0 → Phase 8 at close: 256 / 256 across
38 verifiers** (`scripts/phase8_master_sweep.sh`).

---

## 2. Architectural state at end of Phase 8

### 2.1 Orchestration ownership (unchanged from Phase 7)

| Engine | Owns |
|--------|------|
| **Hatchet** | All non-integration workflows; AI pool 12 workflows |
| **Kestra** | Integration-edge flows |
| **Dagster** | Bronze + silver factory; OTel-enabled image now ready |
| **Laravel queues** | User-triggered async |

### 2.2 New surfaces

| Surface | Purpose | Phase 9 work |
|---------|---------|--------------|
| `/admin/integrations` "Per-flow JWT keys" panel | Operator visibility of kid + valid windows + active status | Add Rotate-with-overlap button (currently CLI-only) |
| `CADDY_TLS_ISSUER` env | Env-driven swap between Caddy internal CA and external ACME | Wire `acme_email` + DNS validation when ACME is needed |
| `docs/runbooks/caddy_tls.md` | Operator runbook for cert source swap | First entry in `docs/runbooks/`; pattern for future runbooks |
| `docs/phase8_hatchet_ha_design.md` | Scoping for R-P3-6 — recommendation is Path A (accept single-instance for V1) | Re-open if a forcing function lands |

### 2.3 Auth + TLS posture (unchanged from Phase 7)

No changes to auth/TLS shapes. Phase 8 made TLS configuration
swappable but the default behaviour (internal CA) is identical.

### 2.4 Observability posture

Dagster image is now **OTel-ready**. When the daemon starts under
`dev-ingest` profile, `definitions.py`'s `install_tracer_provider()`
call (Phase 7 Step 1) actually installs a real exporter. Phase 9
should run an end-to-end probe (parse a fixture PDF through Dagster's
`silver_reports` asset, query Tempo for `service.name=georag-dagster-daemon`
spans). That probe was the deferred R-P5-3 follow-on for Dagster;
the wiring is now in place.

---

## 3. Operational state

Same as Phase 7 plus:

- The admin Integrations page now shows the full per-flow JWT key
  history.
- `bash docs/runbooks/caddy_tls.md` (read, not executed — it's a
  doc) covers the production TLS swap.
- Dagster daemon, when brought up via `docker compose --profile
  dev-ingest up -d`, will emit spans to the otel-collector under
  `service.name=georag-dagster-daemon`.

---

## 4. Carry-overs for Phase 9

| ID | Item | Where | Phase 9 rationale |
|----|------|-------|--------------------|
| **R-P3-5** | Generalised dual-write harness | hard-coded workflow_kind | Re-evaluate when the second migration target lands. |
| **R-P3-6** | Hatchet engine HA | docker-compose | **Scoped at Phase 8 Step 4.** Recommendation: defer until forcing function. If reopened, see `phase8_hatchet_ha_design.md` Path B (compose + LB) as the minimal step. |
| **R-P3-9** | Vendor-profile column-mapping for parser | `parse_pdf_report` | Needs Kyle SME input on what vendor profiles exist. |
| **R-P5-3** (Dagster variant) | End-to-end Tempo probe for Dagster spans | new `phase9_step*_verify.sh` | Phase 8 made it possible by rebuilding the image; the actual probe needs to start dagster + materialise an asset + query Tempo. |
| **R-P8-1** | Rotate-with-overlap button in the admin UI | `Admin/Integrations.tsx` | Step 2 ships read-only. Adding the action requires a controller route + form. |
| **R-P8-2** | ACME wiring on the Caddy edge | `caddy/Caddyfile` + `.env` | Step 3 made the issuer swappable; running it in production needs `acme_email` + DNS or HTTP-01 validation. |
| **R-P7-1** | Rebuild Dagster image + verify OTel | CLOSED at Step 1 (build only; runtime e2e still deferred via R-P5-3 Dagster variant). |
| **R-P7-2** | Operator UI for `flow_jwt_keys` | CLOSED at Step 2. |
| **R-P7-3** | External CA strategy + parametrized tls | CLOSED at Step 3. |

---

## 5. Files of record

**New in Phase 8:**

```
app/Http/Controllers/Admin/IntegrationsController.php             (mod — Step 2; loadFlowJwtKeys + prop)
caddy/Caddyfile                                                    (mod — Step 3; env-templated issuer)
docker-compose.yml                                                 (mod — Step 3; CADDY_TLS_ISSUER env)
docs/phase8_hatchet_ha_design.md                                   (Step 4)
docs/phase8_implementation_kickoff.md                              (Step 0)
docs/phase8_handoff.md                                              (this file)
docs/runbooks/caddy_tls.md                                         (Step 3 — first runbook entry)
resources/js/Pages/Admin/Integrations.tsx                          (mod — Step 2; FlowJwtKeyRow + panel)
scripts/_phase8_step2_probe.php                                    (Step 2 probe — verifier helper)
scripts/phase8_master_sweep.sh                                     (Step 5)
scripts/phase8_step1_verify.sh                                     (Step 1)
scripts/phase8_step2_verify.sh                                     (Step 2)
scripts/phase8_step3_verify.sh                                     (Step 3)
scripts/phase8_step4_verify.sh                                     (Step 4)
```

**Image rebuilds:**

```
georag/dagster:latest   (Step 1 — added opentelemetry-* deps)
```

**Archived in Phase 8:** none.

---

## 6. Re-running every Phase 8 verifier

```bash
bash scripts/phase8_step1_verify.sh   # Dagster image OTel rebuild   (6/6)
bash scripts/phase8_step2_verify.sh   # flow_jwt_keys admin UI       (5/5)
bash scripts/phase8_step3_verify.sh   # Caddy TLS issuer param       (6/6)
bash scripts/phase8_step4_verify.sh   # Hatchet HA design doc        (5/5)
```

Combined Phase 0 → Phase 8 sweep — **38 verifiers, 256 total checks**
(`scripts/phase8_master_sweep.sh`).

---

## 7. Phase 9 entry checklist

Before Phase 9 work begins:

1. Read this handoff + Phase 7 handoff + Phase 8 kickoff +
   `phase8_hatchet_ha_design.md` (if considering R-P3-6).
2. Re-run `scripts/phase8_master_sweep.sh` — confirm 256/256 green.
3. Decide Phase 9 scope. Candidates ranked by readiness:
   - **R-P5-3 Dagster variant** + **R-P8-1** (Rotate button) +
     **R-P8-2** (ACME wiring) — three small ops items, pairs well.
     Could be a tight Phase 9.
   - **R-P3-9** (vendor profiles) — needs SME input from Kyle; ask
     before scheduling.
   - **R-P3-6** (Hatchet HA) — only reopen if a forcing function
     landed since Phase 8 close. Path B (compose + LB) is ~4 steps.
   - Fresh territory: golden-query test suite, RAG pipeline
     hardening, or one of the deferred ingest formats per the
     CLAUDE.md "untouched" list.

End of Phase 8 handoff.
