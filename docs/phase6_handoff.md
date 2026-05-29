# Phase 6 Handoff — Integration-edge close-out

**Document version:** 1.0
**Status:** Phase 6 complete. Phase 7 inheriting.
**Predecessors:** `docs/phase5_handoff.md`,
`docs/phase6_implementation_kickoff.md`.

---

## 1. What Phase 6 delivered

Phase 6 closed the three carry-overs that kept the integration edge
from being "actually production-ready under load":

1. **R-P5-1 + R-P5-3** — Phase 5 wired parse-stage spans into the
   parser. They were running on the no-op tracer because no
   `TracerProvider` was being installed in the worker process. Phase 6
   moves the bootstrap from `pdf_report.py` module-load into
   `worker.py:main()` so the exporter starts before the first
   workflow run, picks up the pool-suffixed `service.name`, and
   actually ships spans to Tempo.
2. **R-P4-2** — Caddy edge in front of Kestra with native WebSocket
   support. The Phase 4 Step 2 Laravel passthrough remains for
   cookie-only callers; the new path at `:8087` validates Sanctum
   PATs via Caddy's `forward_auth` to `/internal/sanctum/check`,
   then proxies HTTP + WS to Kestra with basic auth injected from
   the auth-check response.
3. **R-P5-2** — Multi-kid JWT rotation. `workflow.flow_jwt_keys`
   holds a row per historical kid with `valid_from` / `valid_until`
   windows; rotation with `overlap_hours > 0` extends the prior
   kid's window so in-flight tokens keep verifying through the
   transition.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | OTel TracerProvider bootstrap moved into `worker.py:main()`; OTel env wired into both Hatchet worker compose blocks; AI worker gets `KESTRA_FLOW_JWT_SECRET` (R-P5-4 fold-in) | `scripts/phase6_step1_verify.sh` (7/7) |
| 2 | `caddy` service + `caddy/Caddyfile`; Laravel `/internal/sanctum/check` route + `KestraSsoCheckController`; WebSocket upgrade preserved through the edge | `scripts/phase6_step2_verify.sh` (8/8) |
| 3 | `workflow.flow_jwt_keys` table; `get_flow_jwt_keys()` SECURITY DEFINER fn; `set_flow_jwt_secret(..., overlap_hours)` overload; `flow_jwt.py` verify path matches inbound `kid` against the full valid set | `scripts/phase6_step3_verify.sh` (7/7) |
| 4 | This handoff | — |

**Phase 6 cumulative: 22 / 22 verifier checks** (7+8+7).
**Master sweep across Phase 0 → Phase 6 at close: 207 / 207 across
30 verifiers** (`scripts/phase6_master_sweep.sh`).

---

## 2. Architectural state at end of Phase 6

### 2.1 Orchestration ownership (unchanged from Phase 5)

| Engine | Owns |
|--------|------|
| **Hatchet** | All non-integration workflows; 11 AI + 5 ingestion |
| **Kestra** | Integration-edge flows: scheduled imports, inbound webhooks |
| **Dagster** | Bronze + silver factory |
| **Laravel queues (Horizon)** | User-triggered async |

### 2.2 New surfaces

| Surface | Purpose | Phase 7 work |
|---------|---------|--------------|
| `workflow.flow_jwt_keys` | Per-flow JWT signing history with overlap-window rotation | Add automated `valid_until` enforcement (sweep + purge) |
| `caddy` service at `:8087` | WebSocket-capable edge proxy for Kestra, Sanctum-gated via `forward_auth` | Generalise to fronting all `/admin/integrations/*` paths; add TLS |
| `/internal/sanctum/check` | Caddy-targeted auth probe that returns Kestra basic-auth header | Same shape for future per-service edge proxies |
| `worker.py` TracerProvider bootstrap | Exports parse spans (+ any future worker spans) to Tempo via OTLP | Wire the same bootstrap into the Dagster daemon entry point |
| `scripts/phase3_jwt_rotate.sh provision-key … <overlap_hours>` | Operator CLI for rotation with overlap | Bulk `provision-all-flows` + cron-driven proactive rotation |

### 2.3 Auth posture (post-Phase-6)

| Surface | Auth | Quota / Rotation |
|---------|------|------------------|
| Kestra admin UI (browser) | Laravel session → KestraSsoController passthrough | unchanged |
| Kestra admin UI (CLI / WS) | Sanctum PAT → Caddy `:8087` → forward_auth → Kestra basic-auth | unchanged |
| Kestra → FastAPI integrations | Per-flow Bearer JWT with `kid` header; overlap-window rotation | `provision-key … 24` extends prior kid for 24h |
| External sender → `external_notification` | Per-sender HMAC | Per-sender token-bucket (Phase 5 Step 1) |

### 2.4 Observability posture

Parse spans now flow end-to-end:

```
hatchet-worker-ingestion → install_tracer_provider(service.name=…ingestion)
                       → parse_pdf_report() yields 7 named stage spans
                       → BatchSpanProcessor → otel-collector:4318
                       → Tempo (queryable by service.name + span name)
```

The Phase 5 Step 4 verifier's "spans wired in source" check still
passes; the new Phase 6 Step 1 verifier adds an end-to-end probe that
parses a fixture PDF and asserts ≥6 spans land in Tempo within 60s.

---

## 3. Operational state

Same dashboard surfaces as Phase 5 plus:

- `http://localhost:8087/` — Caddy edge for Kestra. Operators with a
  Sanctum PAT can hit Kestra's WebSocket endpoints for flow execution
  streaming without going through Laravel passthrough.
- `provision-key <flow> <kid> <overlap_hours>` — rotate per-flow
  signing keys without breaking in-flight tokens.
- Tempo at `:3200` now shows traces under
  `service.name=georag-hatchet-worker-ingestion` (and `-ai` once that
  pool runs a workflow that emits spans).

---

## 4. Carry-overs for Phase 7

Phase 5 + 6 carry-overs that didn't close in Phase 6, plus
Phase 6-discovered items.

| ID | Item | Where | Phase 7 rationale |
|----|------|-------|--------------------|
| **R-P3-5** | Generalised dual-write harness | hard-coded workflow_kind | Re-evaluate when the second migration target lands. |
| **R-P3-6** | Hatchet engine HA | docker-compose | Infrastructure-shaped, big. Deferred four times now — Phase 7 should pick it up. |
| **R-P3-9** | Vendor-profile column-mapping for parser | `parse_pdf_report` | Ingestion scope; pairs naturally with Phase 7 if we go ingestion-focused. |
| **R-P6-1** | Tracer bootstrap on Dagster daemon | `src/dagster/georag_dagster/__init__.py` | Phase 6 ships the bootstrap on Hatchet workers. Dagster daemon also calls parse_pdf_report (silver_reports asset) but bootstraps no TracerProvider, so its spans land on the no-op tracer. |
| **R-P6-2** | Auto-prune expired `flow_jwt_keys` rows | `workflow.flow_jwt_keys` | Currently nothing reaps rows whose `valid_until` is in the past. A nightly Hatchet workflow + a `DELETE … WHERE valid_until < now() - interval '7d'` query closes this. |
| **R-P6-3** | TLS on the Caddy edge | `caddy/Caddyfile` | Step 2 ships HTTP-only at `:8087`. Production should terminate TLS at Caddy. Trivial in Caddyfile, requires a cert source decision. |
| **R-P6-4** | Rollup file renamed | `database/raw/phase0-4-rollup.sql` | The build script picks up every `phase[0-9]*` dir so the file now includes Phase 5 + 6 content despite its name. Rename to `phase0-N-rollup.sql` or update tooling to use a stable `current-rollup.sql` symlink. |
| **R-P5-2** | Multi-kid JWT rotation | CLOSED at Step 3. |
| **R-P5-1** | Tracer bootstrap on worker startup | CLOSED at Step 1. |
| **R-P5-3** | Tempo end-to-end probe | CLOSED at Step 1. |
| **R-P5-4** | Per-flow JWT loader env on AI worker | CLOSED at Step 1. |
| **R-P4-2** | Nginx/Caddy edge for Kestra SSO | CLOSED at Step 2. |

---

## 5. Files of record

**New in Phase 6:**

```
app/Http/Controllers/Internal/KestraSsoCheckController.php        (Step 2)
caddy/Caddyfile                                                    (Step 2)
database/raw/phase6/10-flow-jwt-keys-multikid.sql                 (Step 3)
docker-compose.yml                                                 (mod — Steps 1, 2)
docs/phase6_implementation_kickoff.md                              (Step 0)
docs/phase6_handoff.md                                              (this file)
routes/web.php                                                       (mod — Step 2)
scripts/phase3_jwt_rotate.sh                                       (mod — Step 3; overlap_hours arg)
scripts/phase6_master_sweep.sh                                     (Step 4)
scripts/phase6_step1_verify.sh                                     (Step 1)
scripts/phase6_step2_verify.sh                                     (Step 2)
scripts/phase6_step3_verify.sh                                     (Step 3)
src/dagster/georag_dagster/parsers/pdf_report.py                  (mod — Step 1; no module-load bootstrap)
src/fastapi/app/hatchet_workflows/worker.py                       (mod — Step 1; install at main())
src/fastapi/app/services/flow_jwt.py                              (mod — Step 3; multi-kid verify)
database/raw/phase5/20-per-flow-jwt-keys.sql                      (mod — Step 3; distinct-name count)
scripts/phase5_step2_verify.sh                                     (mod — Step 3; distinct-name count)
```

**Archived in Phase 6:** none — all new code.

---

## 6. Re-running every Phase 6 verifier

```bash
bash scripts/phase6_step1_verify.sh   # tracer bootstrap + Tempo e2e         (7/7)
bash scripts/phase6_step2_verify.sh   # Caddy edge for Kestra SSO           (8/8)
bash scripts/phase6_step3_verify.sh   # multi-kid JWT rotation              (7/7)
```

Combined Phase 0 → Phase 6 sweep — **30 verifiers, 207 total checks**
(`scripts/phase6_master_sweep.sh`).

---

## 7. Phase 7 entry checklist

Before Phase 7 work begins:

1. Read this handoff + Phase 5 handoff + Phase 6 kickoff.
2. Re-run `scripts/phase6_master_sweep.sh` — confirm 207/207 still
   green. If any verifier rotted: investigate before starting new
   work.
3. Decide Phase 7 scope. Best candidates:
   - **R-P3-6** (Hatchet engine HA) — infrastructure-shaped, big,
     deferred four times. Time to close it.
   - **R-P3-9** (Vendor-profile column-mapping) + **R-P6-1** (Dagster
     daemon tracer) — a small ingestion-pipeline phase.
   - **R-P6-2 + R-P6-3 + R-P6-4** — operational close-out for the
     edge work just delivered (auto-prune keys, TLS on Caddy, rollup
     filename). Smaller phase than Phase 6, suitable if appetite is
     low.

End of Phase 6 handoff.
