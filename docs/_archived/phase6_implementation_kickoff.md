# Phase 6 Implementation Kickoff — Integration-edge close-out

**Document version:** 1.0
**Status:** Active.
**Predecessors:** `docs/phase5_handoff.md`.

---

## 1. Theme

Phases 4 + 5 hardened the receive path: per-sender auth, per-sender
rate limits, per-flow JWT keys, freshness CI guard, parse-stage spans.
Phase 6 closes the remaining edge work so the integration plane is
fully production-shaped for a multi-tenant deploy:

1. **R-P5-1 + R-P5-3** — wire the Phase 5 OTel bootstrap into the
   worker entry points so parse spans actually export, and prove it
   with a Tempo query.
2. **R-P4-2** — move the Kestra SSO proxy from PHP-passthrough to an
   edge proxy (Caddy) that handles WebSockets natively.
3. **R-P5-2** — multi-kid JWT rotation overlap window, so a key
   rotation doesn't invalidate in-flight tokens.

Phase 6 is one more "harden the edge" phase. Phase 7 chooses between
the two deferred infrastructure items (Hatchet engine HA) or a fresh
ingestion-pipeline phase (R-P3-9 vendor profiles + outbox propagation
parity).

---

## 2. Locked decisions

| ID | Item | Phase 6 status |
|----|------|---------------|
| **R-P5-1** | Tracer bootstrap on worker startup | **In scope (Step 1)** |
| **R-P5-3** | Tempo end-to-end probe for parse spans | **In scope (Step 1)** — paired with R-P5-1 |
| **R-P4-2** | Nginx/Caddy edge for Kestra SSO | **In scope (Step 2)** |
| **R-P5-2** | Multi-kid JWT rotation overlap | **In scope (Step 3)** |
| **R-P5-4** | Per-flow JWT loader env on AI worker | **In scope (Step 1)** — small, fold in |
| **R-P3-5** | Generalised dual-write harness | Defer |
| **R-P3-6** | Hatchet engine HA | Defer — Phase 7 candidate |
| **R-P3-9** | Vendor-profile column-mapping | Defer — Phase 7 candidate |

---

## 3. Done definition

Every step ships a verifier. Phase 6 passes when:

- Step 1 verifier proves `OTEL_EXPORTER_OTLP_ENDPOINT` populated workers
  emit parse spans visible through Tempo's HTTP search API, AND the
  Step 5 Step 4 verifier still passes after the bootstrap call moves
  from `pdf_report.py` module-load to `worker.py:main()`.
- Step 2 verifier proves a Caddy edge proxy serves the Kestra admin UI
  through Sanctum-validated cookies, the WebSocket upgrade for flow
  execution streaming succeeds, and the old PHP-passthrough path still
  works for callers that prefer it.
- Step 3 verifier proves a two-kid rotation (old kid + new kid both
  present in the registry, both valid for a configurable overlap
  window) and that JWTs minted under either kid verify.
- All prior phase verifiers still green (185 baseline → ~205+ at Phase
  6 close depending on Step 2 + Step 3 check counts).

---

## 4. Step-by-step

### Step 1 — Wire tracer bootstrap + Tempo probe (R-P5-1, R-P5-3, R-P5-4)
- Move `install_tracer_provider("pdf_report")` out of
  `pdf_report.py` module-load and into `worker.py:main()` so it
  fires once per process and gets the right service name
  (`georag-hatchet-worker-ingestion` / `-ai`).
- Add `OTEL_EXPORTER_OTLP_ENDPOINT` + `OTEL_SERVICE_NAME` env to both
  Hatchet worker compose blocks (default
  `http://otel-collector:4318` / pool-named).
- Add `AUDIT_ENCRYPTION_KEY` + `POSTGRES_DIRECT_*` to the AI worker
  block (R-P5-4 fold-in).
- Verifier: run a real `ingest_pdf` smoke against a fixture PDF, then
  poll Tempo's search API for ≥6 child spans tagged
  `service.name=georag-hatchet-worker-ingestion` and parent
  `pdf_report.preflight`.

### Step 2 — Caddy edge for Kestra SSO (R-P4-2)
- Add a `caddy` service to docker-compose that fronts Kestra at
  `/admin/integrations/kestra/*` and proxies WebSockets natively.
- Sanctum check: Caddy calls a Laravel `/internal/sanctum/check`
  endpoint with the inbound session cookie before forwarding.
- Keep the PHP-passthrough path on `/admin/integrations/kestra-fallback/*`
  for environments without Caddy.
- Verifier: probe both the HTTP and WS endpoints with a Sanctum
  cookie; confirm WebSocket upgrade returns 101 and the
  fallback path still works.

### Step 3 — Multi-kid JWT rotation (R-P5-2)
- Schema: split `jwt_secret_kid` + `jwt_secret_ciphertext` into a
  child table `workflow.flow_jwt_keys` with columns `(flow_name, kid,
  ciphertext, valid_from, valid_until)`. Existing `flow_registry`
  columns become a view backed by the most-recent active kid.
- `verify_flow_jwt_token` looks up the kid in the child table and
  accepts any kid whose window includes `now()`.
- `provision-key` CLI gains a `--rotate-with-overlap-hours N` flag
  that writes a new kid and back-dates the old kid's `valid_until`
  to `now() + N hours`.
- Verifier: provision a flow with kid=`a`, rotate to kid=`b` with 2h
  overlap, mint with both, verify both succeed; advance the clock
  past the overlap, kid=`a` rejects.

### Step 4 — Phase 6 → Phase 7 handoff
- Same shape as previous handoffs.

---

## 5. Engineering invariants

- Single sweep target: `scripts/phase6_master_sweep.sh` extends the
  Phase 5 sweep with the new verifiers. Target remains 100% green.
- All new SQL migrations land under `database/raw/phase6/`.
- `database/raw/phase0-6-rollup.sql` regenerated at Step 4 (Phase 4
  Step 7 pattern).
- pgcrypto `app.audit_encryption_key` GUC is the single rotation
  anchor across `flow_jwt_keys` + `external_notification_senders` +
  any future encrypted-at-rest registry.

---

## 6. Files of record (preview)

```
caddy/Caddyfile                                                    (Step 2)
database/raw/phase6/10-flow-jwt-keys.sql                          (Step 3)
database/raw/phase0-6-rollup.sql                                   (Step 4)
docker-compose.yml                                                 (mod — Steps 1, 2)
docs/phase6_implementation_kickoff.md                              (this file)
docs/phase6_handoff.md                                              (Step 4)
scripts/phase3_jwt_rotate.sh                                       (mod — Step 3)
scripts/phase6_master_sweep.sh                                     (Step 4)
scripts/phase6_step1_verify.sh                                     (Step 1)
scripts/phase6_step2_verify.sh                                     (Step 2)
scripts/phase6_step3_verify.sh                                     (Step 3)
src/dagster/georag_dagster/parsers/pdf_report.py                  (mod — Step 1)
src/fastapi/app/hatchet_workflows/worker.py                       (mod — Step 1)
src/fastapi/app/services/flow_jwt.py                              (mod — Step 3)
```

---

End of Phase 6 kickoff.
