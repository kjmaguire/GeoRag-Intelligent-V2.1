# Phase 8 Implementation Kickoff — Phase 7 close-outs + HA scoping

**Document version:** 1.0
**Status:** Active.
**Predecessors:** `docs/phase7_handoff.md`.

---

## 1. Theme

Phase 7 closed four R-P6-* operational gaps but left three of its own
behind (R-P7-1, R-P7-2, R-P7-3). Phase 8 closes all three, plus
finally puts a design doc against R-P3-6 (Hatchet engine HA, deferred
five times now). Actual Hatchet clustering on docker-compose is too
much of an infrastructure rabbit hole for one phase; the design doc
unblocks Phase 9 to decide between full HA, an HA-via-k8s pivot, or
deciding the single-instance posture is acceptable for V1.

Phase 8 is **closure-shaped**, not feature-shaped. Phase 9 chooses
between Hatchet HA implementation (per the new design doc), a fresh
ingestion phase (R-P3-9 vendor profiles + parser quality), or RAG
pipeline / golden-query work.

---

## 2. Locked decisions

| ID | Item | Phase 8 status |
|----|------|---------------|
| **R-P7-1** | Rebuild Dagster image + verify Tempo e2e | **In scope (Step 1)** |
| **R-P7-2** | Operator UI for `flow_jwt_keys` | **In scope (Step 2)** |
| **R-P7-3** | External CA strategy + parametrized `tls` directive | **In scope (Step 3)** |
| **R-P3-6** | Hatchet engine HA | **Design doc only (Step 4)** — Phase 9+ decision |
| **R-P3-5** | Generalised dual-write harness | Defer |
| **R-P3-9** | Vendor-profile column-mapping | Defer — needs SME input |

---

## 3. Done definition

Each step ships a verifier. Phase 8 passes when:

- Step 1 verifier proves the dagster image contains
  `opentelemetry-sdk` after a rebuild and (if Dagster can be brought
  up) emits spans to Tempo under
  `service.name=georag-dagster-daemon`.
- Step 2 verifier proves the `/admin/integrations` page exposes a
  "Per-flow JWT keys" panel listing currently-active kids + a
  Rotate-with-overlap action, gated by the admin Gate.
- Step 3 verifier proves the Caddyfile's `tls` directive is driven
  by an env var (`CADDY_TLS_ISSUER`) defaulting to `internal` but
  honouring `acme` (production target) when overridden.
- Step 4 verifier proves `docs/phase8_hatchet_ha_design.md` exists,
  documents the trade-offs in enough detail (4+ key sections), and
  references the live `docker-compose.yml` hatchet-lite block.
- All prior phase verifiers still green (234 → ~260+ at Phase 8
  close).

---

## 4. Step-by-step

### Step 1 — Dagster image rebuild + Tempo e2e (R-P7-1)
- Run `docker compose build dagster-daemon` to pull in the
  `opentelemetry-*` deps added to `pyproject.toml` at Phase 7
  Step 1.
- Verifier: probe the running daemon container for the SDK + (if up)
  trigger a brief asset materialisation and query Tempo for the
  resulting spans.
- If the dagster daemon can't be brought up cleanly in CI (depends
  on `dev-ingest` profile), the verifier accepts a "deps present
  in image" pass.

### Step 2 — Admin UI for `flow_jwt_keys` (R-P7-2)
- Extend `IntegrationsController` with a `jwtKeys()` method that
  joins `workflow.flow_registry` to `workflow.flow_jwt_keys`,
  grouping by flow + listing kid, valid_from, valid_until.
- Pass the list as an Inertia prop to the existing
  `Admin/Integrations.tsx` page; add a collapsed panel beneath
  the existing Senders panel.
- Verifier: page-controller probe (mirrors the Phase 4 Step 5
  pattern) confirms admin sees the panel + non-admin gets 403.

### Step 3 — Parametrized Caddy TLS directive (R-P7-3)
- Replace `tls internal` in the HTTPS site block with
  `tls {$CADDY_TLS_ISSUER:internal}`. Caddy reads the env at boot
  and substitutes; `internal` keeps the dev experience, `acme` (or
  email-bearing variants) enables external CA in prod.
- Document the swap in the Caddyfile header + add a short runbook
  under `docs/_archived/` (or a new `docs/runbooks/caddy_tls.md`).
- Verifier: env-override probe — set `CADDY_TLS_ISSUER=internal`,
  cert chain still issued by Caddy local; confirm the Caddyfile
  references the env var.

### Step 4 — Hatchet HA design doc (R-P3-6 scoping)
- Write `docs/phase8_hatchet_ha_design.md` covering:
  1. Current single-instance posture + failure modes
  2. Multi-instance Hatchet engine — gRPC LB, shared Postgres
     repository, ticker contention
  3. Worker-side adaptation — DNS round-robin vs explicit
     `HATCHET_CLIENT_HOST_PORT` list
  4. State-loss boundaries — in-flight jobs, cron triggers,
     long-running workflow check-pointing
  5. Operational ask — TLS for inter-engine comms, separate
     Postgres replica or stay shared
  6. Recommendation: Phase 9 candidates (full implementation vs
     "decision: accept single-instance for V1")
- Verifier: file exists, ≥4 of the 6 sections present, references
  the live compose service name.

### Step 5 — Phase 8 → Phase 9 handoff
- Same shape as previous handoffs.

---

## 5. Engineering invariants

- `scripts/phase8_master_sweep.sh` extends the Phase 7 sweep with
  the four new verifiers. Target: 100% green.
- No new database/raw/phase8 migration unless Step 2's admin UI
  needs a backing view (decide during implementation).
- All shell verifiers stay self-contained — no Composer / npm
  dependencies new to Phase 8.

---

## 6. Files of record (preview)

```
app/Http/Controllers/Admin/IntegrationsController.php             (mod — Step 2)
caddy/Caddyfile                                                    (mod — Step 3)
docker-compose.yml                                                 (mod — Step 3; CADDY_TLS_ISSUER env)
docs/phase8_hatchet_ha_design.md                                   (Step 4)
docs/phase8_implementation_kickoff.md                              (this file)
docs/phase8_handoff.md                                              (Step 5)
docs/runbooks/caddy_tls.md                                         (Step 3)
resources/js/Pages/Admin/Integrations.tsx                          (mod — Step 2)
scripts/phase8_master_sweep.sh                                     (Step 5)
scripts/phase8_step1_verify.sh                                     (Step 1)
scripts/phase8_step2_verify.sh                                     (Step 2)
scripts/phase8_step3_verify.sh                                     (Step 3)
scripts/phase8_step4_verify.sh                                     (Step 4)
```

---

End of Phase 8 kickoff.
