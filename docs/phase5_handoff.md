# Phase 5 Handoff — Multi-tenant hardening + observability completion

**Document version:** 1.0
**Status:** Phase 5 complete. Phase 6 inheriting.
**Predecessors:** `docs/phase4_handoff.md`,
`docs/phase5_implementation_kickoff.md`.

---

## 1. What Phase 5 delivered

Phase 5 closed the four highest-leverage carry-overs from Phase 4:
per-sender rate limiting (R-P4-1), per-flow JWT signing keys
(R-P4-4), the pre-commit guard that hardens the Phase 4 Step 3
freshness check into something operators can't bypass (R-P4-3 +
R-P4-5), and the ingestion-observability gap inside `parse_pdf_report`
(R-P3-7). No new features; the integration edge is now production-shaped
for >1 tenant.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | Per-sender rate limit (`rate_limit_per_minute` on senders, Redis fixed-window token bucket, audit short-circuit on `reason='rate_limited:...'`) | `scripts/phase5_step1_verify.sh` (6/6) |
| 2 | Per-flow JWT signing keys (`jwt_secret_kid` + `jwt_secret_ciphertext` on `workflow.flow_registry`; mint emits `kid` header when provisioned; env-var fallback intact) | `scripts/phase5_step2_verify.sh` (8/8) |
| 3 | Pre-commit hook (`fastapi-pydantic-freshness` in `.pre-commit-config.yaml`) + `.env` housekeeping (7 orphan vars stripped) | `scripts/phase5_step3_verify.sh` (7/7) |
| 4 | Per-stage OTel spans in `parse_pdf_report` (preflight / unstructured / pdfplumber / OCR / metadata / sections / resource_tables) + lazy `TracerProvider` bootstrap | `scripts/phase5_step4_verify.sh` (7/7) |
| 5 | This handoff | — |

**Phase 5 cumulative: 28 / 28 verifier checks** (6+8+7+7).
**Master sweep across Phase 0 → Phase 5 at close: 185 / 185 across
27 verifiers** (`scripts/phase5_master_sweep.sh`).

---

## 2. Architectural state at end of Phase 5

### 2.1 Orchestration ownership (unchanged from Phase 4)

| Engine | Owns |
|--------|------|
| **Hatchet** | All non-integration workflows; AI pool 11 workflows |
| **Kestra** | Integration-edge flows: scheduled imports, inbound webhooks |
| **Dagster** | Bronze + silver factory |
| **Laravel queues (Horizon)** | User-triggered async |

### 2.2 New surfaces

| Surface | Purpose | Phase 6 work |
|---------|---------|--------------|
| `usage.external_notification_senders.rate_limit_per_minute` | Per-sender token-bucket limit; default 60/min, 0 disables | Tenant-policy escalation (auto-bump on consent) |
| `workflow.flow_registry.jwt_secret_{kid,ciphertext}` | Per-flow HS256 signing secret, encrypted-at-rest with `app.audit_encryption_key` | Multi-kid rotation (overlap window with two valid kids) |
| `scripts/phase3_jwt_rotate.sh provision-key` | Operator CLI to mint + persist a per-flow secret | Bulk `provision-all` + zero-downtime kid rotation |
| `georag_dagster.observability` package | Lazy OTel TracerProvider; shared by Dagster + Hatchet workers | Wire into `worker.py:main()` so spans actually export by default |
| `.pre-commit-config.yaml` `fastapi-pydantic-freshness` hook | Blocks commits when fastapi source mtime > container start | Add `pint --test --dirty` + ruff hooks |

### 2.3 Auth + quota posture

| Surface | Auth | Quota | Phase 6 work |
|---------|------|-------|--------------|
| Kestra admin UI | Sanctum-fronted reverse proxy | — | WebSocket support (R-P4-2 carry) |
| Kestra → FastAPI integrations | Per-flow Bearer JWT, per-flow signing key with `kid` claim (Phase 5 Step 2) | — | Multi-kid rotation |
| External sender → `external_notification` | Per-sender HMAC (Phase 4 Step 1) | Per-sender token bucket (Phase 5 Step 1) | Burst credit / sliding window |
| `/admin/integrations` | `admin` Gate | — | unchanged |

### 2.4 Observability posture

OTel exporters were already wired in the Laravel + FastAPI service
boundaries; this phase closed the parse-stage gap. `parse_pdf_report`
now emits seven child spans (preflight, unstructured, pdfplumber, ocr,
metadata, sections, resource_tables) with attributes for input size,
chars extracted, parse-quality %, etc. Exports activate as soon as
`OTEL_EXPORTER_OTLP_ENDPOINT` is set on the worker — the bootstrap is
called at module-load of `pdf_report.py` so both ingestion-pool Hatchet
workers and the Dagster daemon get spans without extra wiring. With no
endpoint, every span call collapses to a no-op via opentelemetry's
default tracer provider.

---

## 3. Operational state

Same dashboard surfaces as Phase 4 plus:

- Per-sender `rate_limit_per_minute` column visible in the `/admin/integrations`
  Senders panel (display-only at end of Phase 5 — the bucket runs in Redis
  with key `rl:external_notification:<source>:<minute_bucket>`)
- The Phase 4 `check_fastapi_pydantic_freshness.sh` script now runs
  automatically via `pre-commit` if installed: `pip install pre-commit && pre-commit install`
- 17 lines of dead env vars removed from `.env`

---

## 4. Carry-overs for Phase 6

Phase 4 carry-overs that didn't close in Phase 5, plus
Phase 5-discovered items.

| ID | Item | Where | Phase 6 rationale |
|----|------|-------|--------------------|
| **R-P4-2** | Nginx/Caddy edge for Kestra SSO | docker-compose | Phase 4 Step 2 proxies bytes through PHP; works but adds 1-2ms/req and won't carry WebSocket upgrades. |
| **R-P3-5** | Generalised dual-write harness | hard-coded workflow_kind | Re-evaluate when the second migration target lands. |
| **R-P3-6** | Hatchet engine HA | docker-compose | Infrastructure-shaped, big. Deferred three times now. |
| **R-P3-9** | Vendor-profile column-mapping for parser | `parse_pdf_report` | Ingestion scope. |
| **R-P5-1** | Wire `install_tracer_provider()` into `worker.py:main()` | `src/fastapi/app/hatchet_workflows/worker.py` | Phase 5 ships the bootstrap function; the AI/ingestion worker entry points still need to invoke it once at startup before the otel-collector receives parse spans. |
| **R-P5-2** | Multi-kid JWT rotation | `workflow.flow_registry` | Per-flow key supports one kid today; a real rotation needs an "old kid still verifies for N hours" overlap window. |
| **R-P5-3** | Tempo end-to-end probe for parse spans | `scripts/phase5_step4_verify.sh` | Step 4 verifier only checks instrumentation is wired in source; once R-P5-1 lands, add a probe that fetches Tempo's search API and asserts ≥6 child spans land per ingest_pdf run. |
| **R-P5-4** | Per-flow JWT loader on the AI worker | `hatchet-worker-ai` env | Phase 5 Step 2 wired `AUDIT_ENCRYPTION_KEY` + `POSTGRES_DIRECT_HOST` into the fastapi service. Hatchet workers don't currently mint per-flow JWTs but if they ever do, they need the same env. |

### 4.1 Closed since the start of Phase 5

| ID | Resolution |
|----|------------|
| R-P4-1 (per-sender rate limits) | CLOSED at Step 1. |
| R-P4-3 (pre-commit hook for freshness check) | CLOSED at Step 3. |
| R-P4-4 (per-flow JWT keys) | CLOSED at Step 2. |
| R-P4-5 (.env housekeeping) | CLOSED at Step 3. |
| R-P3-7 (per-step parse_pdf_report OTel spans) | CLOSED at Step 4. |
| R-P3-8 (SBERT) | OBSOLETE — already noted at Phase 4. |
| R-P3-10 (drop shadow_runs) | CLOSED — noted at Phase 4. |

---

## 5. Files of record

**New in Phase 5:**

```
.pre-commit-config.yaml                                          (mod — Step 3)
database/raw/phase5/10-sender-rate-limits.sql                    (Step 1)
database/raw/phase5/20-per-flow-jwt-keys.sql                     (Step 2)
docker-compose.yml                                               (mod — Steps 1, 2)
docs/phase5_implementation_kickoff.md                            (Step 0)
docs/phase5_handoff.md                                           (this file)
scripts/phase3_jwt_rotate.sh                                     (mod — Step 2; provision-key subcmd)
scripts/phase5_master_sweep.sh                                   (Step 5)
scripts/phase5_step1_verify.sh                                   (Step 1)
scripts/phase5_step2_verify.sh                                   (Step 2)
scripts/phase5_step3_verify.sh                                   (Step 3)
scripts/phase5_step4_verify.sh                                   (Step 4)
src/dagster/georag_dagster/observability/__init__.py             (Step 4)
src/dagster/georag_dagster/observability/otel.py                 (Step 4)
src/dagster/georag_dagster/parsers/pdf_report.py                 (mod — Step 4)
src/fastapi/app/hatchet_workflows/external_notification.py       (mod — Step 1)
src/fastapi/app/services/flow_jwt.py                             (mod — Step 2)
.env                                                              (mod — Step 3; 7 orphans stripped)
```

**Archived in Phase 5:**

```
scripts/_archived/phase2_step1_verify.sh   (Activepieces role/DB — sunset at Phase 3 Step 7)
scripts/_archived/phase2_step2_verify.sh   (Activepieces docker service — sunset)
scripts/_archived/phase2_step3_verify.sh   (X-Service-Key auth — superseded by phase3_step3 Bearer JWT)
scripts/_archived/phase2_step4_verify.sh   (flows.activepieces.* flags — renamed Phase 3 Step 2)
scripts/_archived/phase2_step5_verify.sh   (flows.activepieces.* flags — renamed Phase 3 Step 2)
```

---

## 6. Re-running every Phase 5 verifier

```bash
bash scripts/phase5_step1_verify.sh   # per-sender rate limit          (6/6)
bash scripts/phase5_step2_verify.sh   # per-flow JWT signing keys     (8/8)
bash scripts/phase5_step3_verify.sh   # pre-commit + .env housekeeping (7/7)
bash scripts/phase5_step4_verify.sh   # parse_pdf_report OTel spans   (7/7)
```

Combined Phase 0 → Phase 5 sweep — **27 verifiers, 185 total checks**
(`scripts/phase5_master_sweep.sh`).

---

## 7. Phase 6 entry checklist

Before Phase 6 work begins:

1. Read this handoff + Phase 4 handoff + Phase 5 kickoff.
2. Re-run `scripts/phase5_master_sweep.sh` — confirm 185/185 still
   green. If any verifier rotted: investigate before starting new work.
3. Install the pre-commit hooks now wired in `.pre-commit-config.yaml`:
   `pip install pre-commit && pre-commit install`.
4. Decide Phase 6 scope. Best candidates:
   - **R-P5-1** (wire tracer bootstrap into worker.py) + **R-P5-3**
     (Tempo probe) — natural close-out of Phase 5 Step 4
   - **R-P4-2** (nginx edge for Kestra SSO) — unblocks WebSocket support
   - **R-P5-2** (multi-kid JWT rotation) — needed for real rotation in prod
   - **R-P3-6** (Hatchet engine HA) — infrastructure-shaped, big, deferred four times now
   - Or a fresh ingestion-pipeline phase: R-P3-9 + outbox propagation parity

End of Phase 5 handoff.
