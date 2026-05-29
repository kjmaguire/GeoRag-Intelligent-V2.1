# Phase 7 Implementation Kickoff — Operational close-out

**Document version:** 1.0
**Status:** Active.
**Predecessors:** `docs/phase6_handoff.md`.

---

## 1. Theme

Phase 6 delivered the integration-edge close-out (worker tracer
bootstrap, Caddy edge with WS, multi-kid JWT rotation). It opened
four small operational gaps in its wake (R-P6-1 through R-P6-4).
Phase 7 closes them all in one tight phase, no new feature surface.
That keeps Phase 8 free for either Hatchet HA (R-P3-6, deferred four
times now) or a fresh ingestion-pipeline phase (R-P3-9 vendor
profiles + parser quality).

Phase 7 is deliberately **small and operational**, not a continuation
of the multi-step receive-path arc that ran from Phases 4 → 6.

---

## 2. Locked decisions

| ID | Item | Phase 7 status |
|----|------|---------------|
| **R-P6-1** | Tracer bootstrap on Dagster daemon | **In scope (Step 1)** |
| **R-P6-2** | Auto-prune expired `flow_jwt_keys` rows | **In scope (Step 2)** |
| **R-P6-3** | TLS on the Caddy edge | **In scope (Step 3)** |
| **R-P6-4** | Rollup filename rationalisation | **In scope (Step 4)** |
| **R-P3-5** | Generalised dual-write harness | Defer |
| **R-P3-6** | Hatchet engine HA | Defer — Phase 8 candidate |
| **R-P3-9** | Vendor-profile column-mapping | Defer — needs SME input |

---

## 3. Done definition

Each step ships a verifier. Phase 7 passes when:

- Step 1 verifier proves the Dagster daemon installs a TracerProvider
  on startup and the `silver_reports` asset's parse spans land in
  Tempo under `service.name=georag-dagster-daemon`.
- Step 2 verifier proves a daily Hatchet workflow (or equivalent
  scheduled job) deletes `flow_jwt_keys` rows whose `valid_until` is
  more than 7 days in the past, AND that recently-retired rows
  (within the retention window) are preserved.
- Step 3 verifier proves Caddy serves HTTPS at `:8443` using its
  internal-issuer cert, the cert is self-trustable for dev, and the
  HTTP `:8087` listener still works for back-compat.
- Step 4 verifier proves the rollup is reachable through a stable
  filename (`current-rollup.sql`) that always points at the latest
  per-phase concatenation, and the old `phase0-4-rollup.sql` either
  still works or is cleanly removed.
- All prior phase verifiers still green (207 → ~230+ at Phase 7
  close).

---

## 4. Step-by-step

### Step 1 — Dagster daemon tracer bootstrap (R-P6-1)
- Call `install_tracer_provider(default_service_name="georag-dagster-daemon")`
  from the Dagster definitions module load (or the daemon entry).
- Add `OTEL_EXPORTER_OTLP_ENDPOINT` + `OTEL_SERVICE_NAME` to the
  `dagster-daemon` + `dagster-webserver` compose blocks.
- Verifier: probe ingestion of the fixture PDF via the silver_reports
  asset → ≥6 spans under `service.name=georag-dagster-daemon` in
  Tempo.

### Step 2 — Auto-prune `flow_jwt_keys` (R-P6-2)
- Add a daily-cron Hatchet workflow `flow_jwt_key_reaper` that
  deletes rows from `workflow.flow_jwt_keys` where
  `valid_until < now() - interval '7 days'`.
- Wire it into the AI pool (no ingestion side-effects).
- Verifier: insert two rows with `valid_until = now() - 10d` and
  `valid_until = now() - 1d`; trigger the workflow; confirm only the
  10-day-old row got deleted.

### Step 3 — TLS on the Caddy edge (R-P6-3)
- Use Caddy's internal CA (`local_certs` directive) so dev clusters
  get auto-generated self-signed certs without external dependencies.
- Add a `:8443` HTTPS listener alongside `:8087`. Reverse-proxy
  behaviour is identical; only the listen address changes.
- Verifier: `curl -k` to `https://localhost:8443/healthz` returns
  200, and the cert chain includes Caddy's internal CA root.

### Step 4 — Rollup filename rationalisation (R-P6-4)
- Rename the rollup file from `phase0-4-rollup.sql` to a
  phase-agnostic `current-rollup.sql`. Add a symlink (or copy)
  from the old name for back-compat.
- Update `phase4_step7_build_rollup.sh` to write the new name and
  update `phase4_step7_verify.sh` to check both names.
- Verifier: both names exist; both have identical content; rebuild
  is idempotent; the live DB re-apply still succeeds.

### Step 5 — Phase 7 → Phase 8 handoff
- Same shape as previous handoffs.

---

## 5. Engineering invariants

- Single sweep target: `scripts/phase7_master_sweep.sh` extends the
  Phase 6 sweep with the four new verifiers. Target: 100% green.
- All new SQL migrations land under `database/raw/phase7/` (only
  Step 2 needs one — the reaper-workflow registration row in
  `workflow.flow_registry` if any).
- The Dagster daemon bootstrap call must be idempotent — re-invocation
  is a no-op once a real provider is installed.

---

## 6. Files of record (preview)

```
caddy/Caddyfile                                                    (mod — Step 3)
database/raw/phase7/10-flow-jwt-key-reaper.sql                    (Step 2)
database/raw/current-rollup.sql                                    (mod — Step 4)
docker-compose.yml                                                 (mod — Steps 1, 3)
docs/phase7_implementation_kickoff.md                              (this file)
docs/phase7_handoff.md                                              (Step 5)
scripts/phase4_step7_build_rollup.sh                              (mod — Step 4)
scripts/phase4_step7_verify.sh                                    (mod — Step 4)
scripts/phase7_master_sweep.sh                                     (Step 5)
scripts/phase7_step1_verify.sh                                     (Step 1)
scripts/phase7_step2_verify.sh                                     (Step 2)
scripts/phase7_step3_verify.sh                                     (Step 3)
scripts/phase7_step4_verify.sh                                     (Step 4)
src/dagster/georag_dagster/definitions.py                         (mod — Step 1)
src/fastapi/app/hatchet_workflows/flow_jwt_key_reaper.py          (Step 2)
src/fastapi/app/hatchet_workflows/worker.py                       (mod — Step 2; register reaper)
```

---

End of Phase 7 kickoff.
