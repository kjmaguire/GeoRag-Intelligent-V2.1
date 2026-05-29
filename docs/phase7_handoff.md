# Phase 7 Handoff — Operational close-out

**Document version:** 1.0
**Status:** Phase 7 complete. Phase 8 inheriting.
**Predecessors:** `docs/phase6_handoff.md`,
`docs/phase7_implementation_kickoff.md`.

---

## 1. What Phase 7 delivered

Phase 7 was a focused operational close-out — four of the carry-overs
Phase 6 left in its wake, all small and autonomously doable without
SME input. No new feature surfaces; the integration edge stays the
shape Phase 6 left it. The point was to clear the housekeeping
backlog so Phase 8 has a clean baseline to either tackle the
deferred big-infrastructure item (Hatchet HA) or pivot to ingestion.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | Dagster `definitions.py` calls `install_tracer_provider("georag-dagster-daemon")` at module load; both Dagster compose blocks gain `OTEL_*` env; `opentelemetry-{api,sdk,exporter-otlp-proto-http}` added to `src/dagster/pyproject.toml` so the next image rebuild ships span export | `scripts/phase7_step1_verify.sh` (6/6) |
| 2 | `workflow.reap_expired_flow_jwt_keys()` SECURITY DEFINER fn + `flow_jwt_key_reaper` Hatchet workflow on cron `0 4 * * *`; deletes rows where `valid_until < now() - retention_days` | `scripts/phase7_step2_verify.sh` (7/7) |
| 3 | Caddy HTTPS listener at `:8443` using internal CA (`tls internal`); `caddy_data` named volume persists the CA across container life; HTTP `:8087` listener kept for back-compat | `scripts/phase7_step3_verify.sh` (8/8) |
| 4 | Rollup canonical filename `database/raw/current-rollup.sql`; `phase0-4-rollup.sql` retained as byte-for-byte back-compat copy; builder auto-detects latest phase for the banner | `scripts/phase7_step4_verify.sh` (6/6) |
| 5 | This handoff | — |

**Phase 7 cumulative: 27 / 27 verifier checks** (6+7+8+6).
**Master sweep across Phase 0 → Phase 7 at close: 234 / 234 across
34 verifiers** (`scripts/phase7_master_sweep.sh`).

---

## 2. Architectural state at end of Phase 7

### 2.1 Orchestration ownership (unchanged from Phase 6)

| Engine | Owns |
|--------|------|
| **Hatchet** | All non-integration workflows; AI pool now 12 workflows (added `flow_jwt_key_reaper`) |
| **Kestra** | Integration-edge flows: scheduled imports, inbound webhooks |
| **Dagster** | Bronze + silver factory; now wired for OTel export once image is rebuilt |
| **Laravel queues (Horizon)** | User-triggered async |

### 2.2 New surfaces

| Surface | Purpose | Phase 8 work |
|---------|---------|--------------|
| `flow_jwt_key_reaper` Hatchet workflow | Nightly auto-prune of expired per-flow JWT keys | Add operator-visible delete-history audit row |
| Caddy `:8443` HTTPS listener | TLS-terminated edge for Kestra (Caddy internal CA) | External CA cert source for prod (`tls` directive swap) |
| `database/raw/current-rollup.sql` | Canonical bootstrap rollup, name no longer tied to Phase 4 | None — terminal name |
| `dagster-daemon` + `-webserver` OTel env | Spans ready to export once image rebuild lands the SDK | Rebuild + run an ingest_pdf-equivalent through Dagster to prove e2e |

### 2.3 Auth + TLS posture

| Surface | Auth | TLS |
|---------|------|-----|
| Kestra admin (browser) | Laravel session → KestraSsoController passthrough | terminated upstream |
| Kestra admin (CLI / WS, plaintext) | Sanctum PAT → Caddy `:8087` → forward_auth | HTTP — internal docker net only |
| Kestra admin (CLI / WS, TLS) | Same as above on `:8443` | Caddy internal CA leaf |
| External sender → external_notification | HMAC per-sender | unchanged |

### 2.4 Observability posture

Worker spans (Phase 6 Step 1) still flow as before. Dagster spans
wired but not yet flowing — the dagster image needs a rebuild for
the new `opentelemetry-*` deps to land. The bootstrap call is in
place at module-load of `definitions.py`, so rebuild + restart is
the only operator action needed. Carry-over R-P7-1.

---

## 3. Operational state

Same as Phase 6 plus:

- The `flow_jwt_key_reaper` workflow fires nightly at 04:00 UTC on
  the AI pool. Operators can also invoke it ad-hoc via Hatchet's
  trigger API for emergency cleanups.
- `https://localhost:8443/` serves the Kestra edge with a Caddy-issued
  cert. Browsers will show a "not trusted" warning until the root
  certificate (extractable from `/data/caddy/pki/authorities/local/root.crt`
  inside the container) is imported.
- `bash scripts/phase4_step7_build_rollup.sh` now writes both
  `current-rollup.sql` and the legacy `phase0-4-rollup.sql`.

---

## 4. Carry-overs for Phase 8

| ID | Item | Where | Phase 8 rationale |
|----|------|-------|--------------------|
| **R-P3-5** | Generalised dual-write harness | hard-coded workflow_kind | Re-evaluate when the second migration target lands. |
| **R-P3-6** | Hatchet engine HA | docker-compose | Infrastructure-shaped, big, deferred five times now. Phase 8 should pick it up unless ingestion goes first. |
| **R-P3-9** | Vendor-profile column-mapping for parser | `parse_pdf_report` | Ingestion scope; needs Kyle's SME input on what vendor profiles exist. |
| **R-P7-1** | Rebuild + verify Dagster image with OTel deps | `docker/dagster.Dockerfile` | Phase 7 added the deps to pyproject + wired the bootstrap. The image needs `docker compose build dagster-daemon` to land the SDK. Verifier should query Tempo for `service.name=georag-dagster-daemon` after a sample materialisation. |
| **R-P7-2** | Operator UI surface for `flow_jwt_keys` history | `/admin/integrations` Inertia page | Dashboard view of currently-active kids per flow + manual rotate-with-overlap action. |
| **R-P7-3** | External CA / ACME for Caddy edge | `caddy/Caddyfile` | Internal CA is dev-only. Production needs a real cert source — typically Let's Encrypt via ACME or a corp-issued cert. |
| **R-P6-2** | Auto-prune flow_jwt_keys | CLOSED at Step 2. |
| **R-P6-3** | TLS on Caddy edge | CLOSED at Step 3. |
| **R-P6-4** | Rollup filename rationalisation | CLOSED at Step 4. |
| **R-P6-1** | Tracer bootstrap on Dagster daemon | CLOSED at Step 1 (wiring); runtime confirmation deferred via R-P7-1. |

---

## 5. Files of record

**New in Phase 7:**

```
database/raw/current-rollup.sql                                    (Step 4 — new canonical)
database/raw/phase7/10-flow-jwt-key-reaper.sql                    (Step 2)
docker-compose.yml                                                 (mod — Steps 1, 3)
docs/phase7_implementation_kickoff.md                              (Step 0)
docs/phase7_handoff.md                                              (this file)
scripts/phase4_step7_build_rollup.sh                              (mod — Step 4)
scripts/phase7_master_sweep.sh                                     (Step 5)
scripts/phase7_step1_verify.sh                                     (Step 1)
scripts/phase7_step2_verify.sh                                     (Step 2)
scripts/phase7_step3_verify.sh                                     (Step 3)
scripts/phase7_step4_verify.sh                                     (Step 4)
src/dagster/georag_dagster/definitions.py                         (mod — Step 1)
src/dagster/pyproject.toml                                         (mod — Step 1; OTel deps)
src/fastapi/app/hatchet_workflows/flow_jwt_key_reaper.py          (Step 2)
src/fastapi/app/hatchet_workflows/worker.py                       (mod — Step 2; pool registration)
caddy/Caddyfile                                                    (mod — Step 3; :8443 HTTPS + snippet refactor)
```

**Archived in Phase 7:** none.

---

## 6. Re-running every Phase 7 verifier

```bash
bash scripts/phase7_step1_verify.sh   # Dagster tracer bootstrap   (6/6)
bash scripts/phase7_step2_verify.sh   # flow_jwt_keys reaper       (7/7)
bash scripts/phase7_step3_verify.sh   # Caddy TLS at :8443         (8/8)
bash scripts/phase7_step4_verify.sh   # rollup rename              (6/6)
```

Combined Phase 0 → Phase 7 sweep — **34 verifiers, 234 total checks**
(`scripts/phase7_master_sweep.sh`).

---

## 7. Phase 8 entry checklist

Before Phase 8 work begins:

1. Read this handoff + Phase 6 handoff + Phase 7 kickoff.
2. Re-run `scripts/phase7_master_sweep.sh` — confirm 234/234 still
   green.
3. Decide Phase 8 scope. Best candidates:
   - **R-P3-6** (Hatchet engine HA) — infrastructure-shaped, big,
     deferred FIVE times now. This is the natural Phase 8 if the
     appetite is there.
   - **R-P3-9** (vendor-profile column-mapping) + **R-P7-1**
     (rebuild Dagster image + verify Tempo e2e) — a pragmatic
     ingestion-focused phase. Vendor profiles need Kyle SME input.
   - **R-P7-2 + R-P7-3** — keep going on operational maturation
     (admin UI for JWT keys + external CA on Caddy). Smaller than
     R-P3-6, still meaningful.

End of Phase 7 handoff.
