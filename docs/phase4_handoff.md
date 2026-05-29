# Phase 4 Handoff — Operational maturation

**Document version:** 1.0
**Status:** Phase 4 complete. Phase 5 inheriting.
**Predecessors:** `docs/phase3_handoff.md`,
`docs/phase4_implementation_kickoff.md`.

---

## 1. What Phase 4 delivered

Phase 4 was a "hardening, not new-features" phase. Phase 1–3 stood up
the integration edge end-to-end with a single sender, code-level flow
registry, and an isolated Kestra admin login. Phase 4 closed the four
gaps that mattered for multi-operator / multi-sender real-world use,
then did a chunk of Phase 1 cleanup.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | Per-sender HMAC registry (`usage.external_notification_senders`, pgcrypto-encrypted, rotation-aware); `external_notification` workflow consults the registry, env-var fallback intact | `scripts/phase4_step1_verify.sh` (8/8) |
| 2 | Sanctum-fronted Kestra SSO via reverse proxy at `/admin/integrations/kestra/{path?}` — no second password for operators | `scripts/phase4_step2_verify.sh` (8/8) |
| 3 | `check_fastapi_pydantic_freshness.sh` — catches the "fastapi container has stale Hatchet input model" footgun | `scripts/phase4_step3_verify.sh` (6/6) |
| 4 | DB-driven flow registry (`workflow.flow_registry`) — adding a flow is `INSERT INTO …` not a code deploy | `scripts/phase4_step4_verify.sh` (6/6) |
| 5 | "Senders" panel on `/admin/integrations` — per-sender 24h receive count + Enable/Disable toggle | `scripts/phase4_step5_verify.sh` (5/5) |
| 6 | `silver.shadow_runs` sunset (R-P1-10): archived to S3, table + Phase 1 flags dropped, `shadow_diff` workflow + dashboard removed | `scripts/phase4_step6_verify.sh` (7/7) |
| 7 | `database/raw/phase0-4-rollup.sql` — concatenated migration bundle; reproducible build script; idempotent re-apply | `scripts/phase4_step7_verify.sh` (5/5) |
| 8 | This handoff | — |

**Phase 4 cumulative: 45 / 45 verifier checks** (8+8+6+6+5+7+5).
**Combined Phase 1+3+4 master sweep at Phase 4 close: TBD per re-run
in §6** (Phase 1: 27, Phase 2: 7, Phase 3: 39, Phase 4: 45 — total
**118 / 118 checks** across the surviving verifiers).

---

## 2. Architectural state at end of Phase 4

### 2.1 Orchestration ownership (unchanged from Phase 3)

| Engine | Owns |
|--------|------|
| **Hatchet** | All non-integration workflows; AI pool now 11 workflows (shadow_diff + shadow_diff_scan retired at Step 6) |
| **Kestra** | Integration-edge flows: scheduled imports, inbound webhooks |
| **Dagster** | Bronze + silver factory |
| **Laravel queues (Horizon)** | User-triggered async |

### 2.2 New surfaces

| Surface | Purpose | Phase 5 work |
|---------|---------|--------------|
| `usage.external_notification_senders` | Per-sender HMAC registry, encrypted-at-rest, rotation-aware | Per-sender rate limit + audit trail |
| `workflow.flow_registry` | Single source of truth for flow catalog (FastAPI + Laravel both read it) | Promote to a richer schema (per-flow ACLs, owner tag, retire-at) |
| `/admin/integrations/kestra/...` (proxy) | Sanctum-fronted Kestra UI/API | WebSocket support; nginx-edge variant |
| `scripts/check_fastapi_pydantic_freshness.sh` | CI guard against stale fastapi import cache | wire into composer-test as a pre-commit hook |
| `scripts/phase4_sender_register.sh` | `add | rotate | list | disable` operator helper | tab-completion + bulk import |
| `database/raw/phase0-4-rollup.sql` | Concatenated migration for fresh-install bootstrap | auto-regenerate on PR via CI |

### 2.3 Auth posture (post-Step-2)

| Surface | Auth | Phase 5 work |
|---------|------|--------------|
| Kestra admin UI | **Sanctum-fronted via reverse proxy** (Phase 4 Step 2). Basic-auth is server-side only. | WebSocket support; nginx-edge variant |
| Kestra → FastAPI integrations | Per-flow Bearer JWT (Phase 3 Step 3) | DB-driven JWT signing key per flow (currently global) |
| External sender → `external_notification` | Per-sender HMAC (Phase 4 Step 1), env-var fallback | Per-sender rate limits |
| `/admin/integrations` | `admin` Gate | unchanged |
| `pgsql_kestra` Laravel connection | `kestra` role, SELECT-only via query patterns | promote to explicit read-only role |

---

## 3. Operational state

Same dashboard surfaces as Phase 3 plus:

- "External notification senders" panel on `/admin/integrations` with
  per-sender 24h count + toggle button
- "Open Kestra UI →" link in the page header that routes through the
  Sanctum proxy
- `phase4_sender_register.sh` for sender provisioning + rotation
- `check_fastapi_pydantic_freshness.sh` CI guard

`/admin/shadow-runs` is gone. Deep links 404 cleanly.

---

## 4. Carry-overs for Phase 5

Phase 3's R-P3-* carry-overs that didn't close in Phase 4, plus
Phase 4-discovered items.

| ID | Item | Where | Phase 5 rationale |
|----|------|-------|-------------------|
| **R-P4-1** | Per-sender rate limits | `external_notification` workflow + `usage.external_notification_senders` table | Step 1 only ships auth; rate limits per `source` (token bucket in Redis) protect against a noisy/malicious sender from one tenant DoSing the receive path. |
| **R-P4-2** | Nginx/Caddy edge for Kestra SSO | docker-compose | Phase 4 Step 2 proxies bytes through PHP. Works but adds 1-2ms / request and won't carry WebSocket upgrades (Kestra's flow execution streaming uses WS). Phase 5 moves the proxy to nginx/caddy at the edge. |
| **R-P4-3** | Pre-commit hook for `check_fastapi_pydantic_freshness.sh` | `.git/hooks/pre-commit` template + `composer test` integration | Step 3 ships the check; making it run automatically before commit is the operational completion. |
| **R-P4-4** | Per-flow JWT signing-secret rotation | `KESTRA_FLOW_JWT_SECRET` env | One env-wide key signs every flow's JWT today. Per-flow keys mean rotating one flow's key doesn't invalidate the others. Depends on Kestra's KV store usage in `phase3_jwt_rotate.sh`. |
| **R-P4-5** | `.env` housekeeping (7 orphan vars) | `.env` | Step 7 identified 7 env vars with no in-tree references (LANGFUSE_PROJECT, POSTGRES_MAX_CONNECTIONS, PROMETHEUS_RETENTION, 3 QDRANT_HNSW_*, LANGFUSE_TRACING). Deferred for safety — operators may use these at runtime; phase5 audits + removes. |
| **R-P3-5** | Generalised dual-write harness (carry from Phase 3) | hard-coded workflow_kind | Re-evaluate when the second migration target lands. |
| **R-P3-6** | Hatchet engine HA (carry from Phase 3) | docker-compose | Infrastructure-shaped, big. |
| **R-P3-7** | Per-step OTel spans inside parse_pdf_report | `pdf_report.py` | Ingestion observability gap. |
| **R-P3-8** | SBERT promotion in `shadow_diff` classifier | OBSOLETE — workflow archived at Step 6 |
| **R-P3-9** | Vendor-profile column-mapping for parser | `parse_pdf_report` | Phase 5+ ingestion scope. |
| **R-P3-10** | Drop `silver.shadow_runs` | CLOSED at Phase 4 Step 6. |

### 4.1 Closed since the start of Phase 4

| ID | Resolution |
|----|------------|
| R-P3-1 (per-sender HMAC registry) | CLOSED at Step 1. |
| R-P3-2 (Kestra SSO via Sanctum) | CLOSED at Step 2. |
| R-P3-3 (input-model-staleness CI check) | CLOSED at Step 3. |
| R-P3-4 (DB-driven flow registry) | CLOSED at Step 4. |
| R-P1-10 (drop shadow_runs) | CLOSED at Step 6. |
| R-P3-8 (SBERT) | OBSOLETE — the workflow that used it is gone. |

---

## 5. Files of record

**New in Phase 4:**

```
app/Http/Controllers/Admin/KestraSsoController.php              (Step 2)
config/services.php                                              (mod — Step 2)
database/raw/phase4/10-external-notification-senders.sql        (Step 1)
database/raw/phase4/20-flow-registry-table.sql                  (Step 4)
database/raw/phase4/90-drop-shadow-runs.sql                     (Step 6)
database/raw/phase0-4-rollup.sql                                 (Step 7)
docker-compose.yml                                                (mod — Steps 1, 2)
docker/kestra/application.yaml                                    (mod — Step 6)
docs/phase4_implementation_kickoff.md                            (Step 0)
docs/phase4_handoff.md                                            (this file)
resources/js/Pages/Admin/Integrations.tsx                        (mod — Steps 2, 5, 6)
routes/web.php                                                     (mod — Steps 2, 5, 6)
src/fastapi/app/hatchet_workflows/external_notification.py     (mod — Step 1)
src/fastapi/app/hatchet_workflows/worker.py                       (mod — Step 6)
src/fastapi/app/routers/integrations_trigger.py                  (mod — Step 4)
src/fastapi/app/services/flow_registry.py                       (Step 4)
scripts/check_fastapi_pydantic_freshness.sh                       (Step 3)
scripts/phase4_sender_register.sh                                 (Step 1)
scripts/phase4_step{1..7}_verify.sh                              (each step)
scripts/phase4_step6_archive_shadow_runs.sh                      (Step 6)
scripts/phase4_step7_build_rollup.sh                              (Step 7)
scripts/_phase4_step2_check.php                                   (Step 2 probe)
scripts/_phase4_step2_proxy_probe.php                             (Step 2 probe)
app/Http/Controllers/Admin/IntegrationsController.php           (mod — Steps 4, 5, 6)
config/database.php                                              (mod — Step 6)
.env                                                               (mod — Steps 1, 2)
```

**Archived in Phase 4:**

```
src/fastapi/app/hatchet_workflows/_archived/shadow_diff.py
src/fastapi/app/services/_archived/shadow_diff/
app/Http/Controllers/Admin/_archived/ShadowRunsController.php
resources/js/Pages/Admin/_archived/ShadowRuns/
```

---

## 6. Re-running every Phase 4 verifier

```bash
bash scripts/phase4_step1_verify.sh   # per-sender HMAC registry            (8/8)
bash scripts/phase4_step2_verify.sh   # Kestra SSO via Sanctum              (8/8)
bash scripts/phase4_step3_verify.sh   # fastapi/Pydantic freshness          (6/6)
bash scripts/phase4_step4_verify.sh   # DB-driven flow registry             (6/6)
bash scripts/phase4_step5_verify.sh   # Senders dashboard panel             (5/5)
bash scripts/phase4_step6_verify.sh   # shadow_runs sunset                  (7/7)
bash scripts/phase4_step7_verify.sh   # migration rollup                    (5/5)
```

Combined Phase 1/2/3/4 sweep — 12 verifiers, 118 total checks.

---

## 7. Phase 5 entry checklist

Before Phase 5 work begins:

1. Read this handoff + Phase 1 + 2 + 3 handoffs + Phase 4 kickoff.
2. Re-run every Phase 1/2/3/4 verifier — confirm none have rotted.
3. Decide Phase 5 scope. Best candidates:
   - **R-P4-1** (per-sender rate limits) — natural follow-on to Step 1
   - **R-P4-2** (nginx edge for Kestra SSO) — unblocks WebSocket support
   - **R-P3-6** (Hatchet engine HA) — infrastructure-shaped, big, deferred twice now
   - Or a fresh ingestion-pipeline phase: R-P3-7 + R-P3-9 + outbox propagation parity

End of Phase 4 handoff.
