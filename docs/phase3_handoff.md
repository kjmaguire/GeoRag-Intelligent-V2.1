# Phase 3 Handoff — Kestra migration + Activepieces sunset

**Document version:** 1.0
**Status:** Phase 3 complete. Phase 4 inheriting.
**Predecessors:** `docs/phase2_handoff.md`,
`docs/phase3_implementation_kickoff.md`.
**Archived as part of this phase:** `docs/_archived/phase2_activepieces_flows.md`,
`docs/_archived/phase2_activepieces_upgrade.md`,
`scripts/_archived/phase2_step6_verify.sh`,
`scripts/_archived/phase2_rp28_backups_verify.sh`.

---

## 1. What Phase 3 delivered

The integration edge migrated from Activepieces to Kestra. Both Phase 2
flows (`public_geoscience_pull`, `external_notification`) re-landed as
committed Kestra YAML flows, with security hardened in the process:

- **R-P2-2 (flow-as-code)** — solved natively by Kestra's YAML-flows-by-default
  model. No engineering effort to build an exporter; flows commit as YAML
  to `kestra/flows/georag/*.yaml` and Kestra reads them via API.
- **R-P2-3 (per-flow JWT)** — Kestra → FastAPI bridge auth replaced the
  shared `X-Service-Key` with per-flow `Authorization: Bearer <jwt>`.
  Each flow's JWT carries `scope=flow:<flow_name>`. A leak compromises
  one flow rather than every integration.
- **R-P2-4 (HMAC sender auth)** — `external_notification` now verifies
  upstream sender HMAC-SHA256 over a canonical JSON of the payload
  fields. Tampered, unsigned, or replay payloads are rejected at the
  Hatchet workflow before any side-effects run.

Plus the full sunset:

- Activepieces container removed from compose
- `activepieces` Postgres role + logical DB dropped (cluster-level
  pg_basebackup retained the prior state)
- 11 ACTIVEPIECES_* env vars stripped from `.env`
- `pgsql_activepieces` connection removed from Laravel
- X-Service-Key fallback removed from `integrations_trigger.py`
- 2 Activepieces runbooks archived
- 2 stale verifiers archived

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `kestra` Postgres role + logical DB on existing server | `scripts/phase3_step1_verify.sh` (5/5) |
| 2 | Kestra docker service (`v1.2.18`, port 8086, basic auth, Postgres-backed); tutorial flows disabled | `scripts/phase3_step2_verify.sh` (6/6) |
| 3 | `flow_jwt` mint+verify; `integrations_trigger` accepts JWT-or-X-Service-Key during co-existence; flag namespace renamed `activepieces.*` → `flows.*` | `scripts/phase3_step3_verify.sh` (10/10) |
| 4 | `public_geoscience_pull` Kestra YAML flow (committed); smoke uses JWT auth | `scripts/phase3_step4_verify.sh` (5/5) |
| 5 | `external_notification` Kestra YAML flow + HMAC sender auth (4 cases: valid / tampered / missing / replay) | `scripts/phase3_step5_verify.sh` (7/7) |
| 6 | `pgsql_kestra` Laravel connection; `loadKestraFlows()` reads JSONB-backed `flows` table; dashboard rebases | `scripts/phase3_step6_verify.sh` (6/6) |
| 7 | Activepieces sunset (DB + role + container + env + connection + X-Service-Key fallback); master regression sweep | `scripts/phase3_step7_verify.sh` (8/8) |
| 8 | This handoff | — |

**Phase 3 cumulative: 47 / 47 verifier checks** (5+6+10+5+7+6+8).
**Master regression sweep at Phase 3 close: 73 / 73 across 11 verifiers**
(Phase 1 Steps 4/5B/6/7 = 27, Phase 2 Step 7 = 7, Phase 3 Steps 1–6 = 39).

---

## 2. Architectural state at end of Phase 3

### 2.1 Orchestration ownership

| Engine | Owns | Notes |
|--------|------|-------|
| **Hatchet** | All Phase 0/1/2 workflows + `public_geoscience_pull`, `external_notification`, `phase2_smoke` | 13 workflows on the AI pool. |
| **Kestra** | Integration-edge flows: scheduled imports, inbound webhooks, third-party connectors | New. Flows in `kestra/flows/georag/*.yaml` (committed source-of-truth). Operator deploys via Kestra API or `kestra flow update`. |
| **Dagster** | `bronze_*` + `silver_*` factory; existing public_geoscience asset chain | Unchanged. |
| **Laravel queues (Horizon)** | User-triggered async (exports, embeddings) | Unchanged. |
| ~~Activepieces~~ | — | **Sunset complete at Phase 3 Step 7.** |

### 2.2 Auth posture (post-Step-7)

| Surface | Auth | Phase 4 work |
|---------|------|--------------|
| Kestra admin UI (`:8086`) | Isolated basic auth | SSO via Sanctum (R-P2-1 still open) |
| Kestra → FastAPI (`/internal/v1/integrations/{flow}/trigger`) | **Per-flow Bearer JWT** | Phase 4 may move JWT mint to a DB-driven secret store |
| External sender → Kestra webhook | Activepieces' built-in URL-secret + HMAC sender auth on FastAPI side | Per-sender HMAC secret rotation registry (Phase 4) |
| `/admin/integrations` | Existing `admin` Gate | unchanged |
| `PostgreSQL-Hatchet` Grafana datasource | `grafana_hatchet_readonly` (SELECT only) | unchanged |
| `pgsql_kestra` Laravel connection | `kestra` role (read-only via Laravel-side query patterns) | unchanged |

### 2.3 Trigger flow shapes

**Outbound — `public_geoscience_pull`:**

```
Kestra schedule (cron 0 */6 * * *, disabled by default)
  └─ HTTP GET upstream feature service (Kestra Http piece)
  └─ S3 upload to bronze/public_geoscience/<id>/<ts>.geojson (Kestra S3 piece)
  └─ HTTP POST FastAPI /internal/v1/integrations/public_geoscience_pull/trigger
        Authorization: Bearer {{ kv('flow_jwt_public_geoscience_pull') }}
                                            │
                                            ▼
                Hatchet workflow public_geoscience_pull (unchanged from Phase 2)
                    ├─ feature-flag gate: flows.public_geoscience_pull.enabled
                    ├─ S3 GET → validate GeoJSON → SHA256
                    ├─ idempotent INSERT bronze.provenance
                    └─ audit emit 'public_geoscience.pull.complete'
```

**Inbound — `external_notification`:**

```
External sender ──signed POST──> Kestra webhook URL
                                   │
                                   ▼
              forward_to_fastapi (Kestra Http POST)
                  Authorization: Bearer {{ kv('flow_jwt_external_notification') }}
                  Body: {{ trigger.body | json }}     ← signed envelope passes through
                                   │
                                   ▼
              Hatchet workflow external_notification
                  ├─ verify_hmac_signature  ← R-P2-4 (rejects unsigned/tampered)
                  ├─ feature-flag gate: flows.external_notification.enabled
                  ├─ idempotency check (audit_ledger by notification_id)
                  └─ audit emit 'external_notification.received'
```

### 2.4 Worker pool layout

```
georag-hatchet-worker-ingestion  (slots=20)  — 5 workflows
georag-hatchet-worker-ai         (slots=20)  — 13 workflows
  └─ audit_ledger_verify, shadow_diff, shadow_diff_scan,
     phase2_smoke, public_geoscience_pull, external_notification,
     + 7 Phase 0 agent wrappers
```

`phase2_smoke` stays as a connectivity-debug surface (named for Phase 2
but applicable to any orchestrator). Removable in Phase 4 if it adds noise.

---

## 3. Operational state

| Surface | URL / path | Read | Write |
|---------|-----------|------|-------|
| Integrations dashboard | `/admin/integrations` | flow registry + flag state + last-24h Hatchet rollup + Kestra flow list + flag-flip history | toggle `flows.<flow>.enabled` |
| Kestra UI | `http://localhost:8086` (basic auth from `.env`) | flows / executions / kv store / triggers | flow CRUD via API; YAML-import via `kestra flow update` |
| Integrations Grafana | `GeoRAG / Integrations` (port 3000, requires `dev-monitor`) | run timeseries, success-rate gauge, recent runs | — |
| Cutover CLI (Phase 1) | `scripts/phase1_step8_traffic.sh` | flag get/set/streak/history | flip flags |
| JWT rotation CLI | `scripts/phase3_jwt_rotate.sh` | mint + write per-flow JWT | rotate |

`feature_flag_history` (R-P1-6) auto-captures every flag flip including
the namespace-renamed `flows.*` family.

---

## 4. Carry-overs for Phase 4

Smaller open items inherit from Phase 2; the bigger Phase 3-discovered ones
are R-P3-N.

| ID | Item | Where | Phase 4 rationale |
|----|------|-------|-------------------|
| **R-P3-1** | Per-sender HMAC secret registry for `external_notification` | `EXTERNAL_NOTIFICATION_HMAC_SECRET` env | Today there's a SINGLE shared HMAC secret for all senders. Multi-sender support requires a lookup table indexed by `source` field with rotation policy + per-sender disable. |
| **R-P3-2** | Kestra SSO (replaces D3a basic auth) | Kestra `application.yaml`, Sanctum bridge | Phase 3 keeps Kestra's isolated admin login; Phase 4 fronts it with the Laravel session. |
| **R-P3-3** | Restart-on-input-model-change discipline | dev workflow | Caught during Step 5 — fastapi caches Pydantic input models at import time, so adding a field to an existing workflow's model requires `docker compose restart fastapi` (not just file save). Documented as a watch item; may build a CI check. |
| **R-P3-4** | DB-driven flow registry (carries from R-P2-5) | `integrations_trigger.py` + IntegrationsController | Code-level `FLOW_REGISTRY` dict in two places. With Kestra's YAML flows, Phase 4 could discover the FastAPI registry from the YAML side and auto-sync the Laravel-side enumeration. |
| **R-P3-5** | Generalised dual-write harness (carries from R-P2-7) | `silver.shadow_runs.workflow_kind` | Phase 1 + 2 + 3 hard-code `workflow_kind='ingest_pdf'` in shadow logic; Phase 4 introduces a third migration target (likely `ingest_csv`) and parameterises. |
| **R-P3-6** | Hatchet engine HA (carries from R-P1-9 + R-P2-10) | docker-compose | Lite single-node remains. Worth re-evaluating before user-triggered RAG queries route through Hatchet. |
| **R-P3-7** | Per-step OTel spans inside `parse_pdf_report` (carries from R-P1-5) | `pdf_report.py` | Today only the parser's own logging exists. |
| **R-P3-8** | SBERT promotion in `shadow_diff` (carries from R-P1-2) | `shadow_diff/classifier.py` | Token Jaccard works; promote if `minor` rates trend high. |
| **R-P3-9** | Vendor-profile column-mapping for the parser (carries from R-P1-7) | `parse_pdf_report` | Phase 2 ingestion-pipeline scope item still open. |
| **R-P3-10** | Drop `silver.shadow_runs` post-cutover (carries from R-P1-10) | migration | Wait ≥30 days post-Phase-1-cutover. |

### 4.1 Closed since the start of Phase 3

| ID | Resolution |
|----|------------|
| R-P2-1 (Activepieces SSO) | OBSOLETE — Activepieces sunset; Kestra has its own auth path (R-P3-2). |
| R-P2-2 (flow-as-code) | CLOSED by Kestra adoption (YAML native). |
| R-P2-3 (per-flow JWT) | CLOSED at Step 3. |
| R-P2-4 (HMAC inbound) | CLOSED at Step 5. |
| R-P2-5 (DB-driven registry) | DEFERRED to R-P3-4 — Kestra YAML reduces the urgency. |
| R-P2-9 (image upgrade runbook) | OBSOLETE — Activepieces sunset; Kestra has its own upgrade path documented inline in `docker-compose.yml`. |

---

## 5. Files of record

**New in Phase 3:**

```
database/raw/phase3/10-kestra-role-and-db.sql                   (Step 1)
database/raw/phase3/20-rename-flow-flags.sql                    (Step 3)
database/raw/phase3/90-activepieces-sunset.sql                  (Step 7)
docker-compose.yml                                                (mod — Steps 2, 6, 7)
kestra/flows/georag/public_geoscience_pull.yaml                 (Step 4)
kestra/flows/georag/external_notification.yaml                  (Step 5)
src/fastapi/app/services/flow_jwt.py                            (Step 3)
src/fastapi/app/config.py                                        (mod — Step 3)
src/fastapi/app/routers/integrations_trigger.py                  (mod — Steps 3, 7)
src/fastapi/app/hatchet_workflows/external_notification.py      (mod — Step 3, 5)
src/fastapi/app/hatchet_workflows/public_geoscience_pull.py     (mod — Step 3)
app/Http/Controllers/Admin/IntegrationsController.php           (mod — Steps 3, 6, 7)
config/database.php                                              (mod — Steps 6, 7)
resources/js/Pages/Admin/Integrations.tsx                        (mod — Steps 6, 7)
docs/phase3_implementation_kickoff.md                            (Step 0)
docs/phase3_handoff.md                                            (this file)
scripts/phase3_step{1..7}_verify.sh                              (each step)
scripts/phase3_step{4,5}_smoke.sh                                (per-flow)
scripts/phase3_jwt_rotate.sh                                     (Step 3)
scripts/_phase3_step6_check.php                                  (Step 6)
.env                                                               (mod — Steps 1, 2, 3, 5, 7)
```

**Archived in Phase 3:**

```
docs/_archived/phase2_activepieces_flows.md
docs/_archived/phase2_activepieces_upgrade.md
scripts/_archived/phase2_step6_verify.sh
scripts/_archived/_phase2_step6_check.php
scripts/_archived/phase2_rp28_backups_verify.sh
```

---

## 6. Re-running every Phase 3 verifier

```bash
bash scripts/phase3_step1_verify.sh   # kestra role + DB                    (5/5)
bash scripts/phase3_step2_verify.sh   # kestra docker service               (6/6)
bash scripts/phase3_step3_verify.sh   # per-flow JWT + flag namespace       (10/10)
bash scripts/phase3_step4_verify.sh   # public_geoscience_pull on Kestra    (5/5)
bash scripts/phase3_step5_verify.sh   # external_notification + HMAC        (7/7)
bash scripts/phase3_step6_verify.sh   # /admin/integrations dashboard       (6/6)
bash scripts/phase3_step7_verify.sh   # Activepieces sunset + regression    (8/8)
```

Combined with surviving Phase 1 + 2 verifiers, the project's done-definition
surface is **73 / 73 across 11 verifiers** at Phase 3 close.

---

## 7. Phase 4 entry checklist

Before Phase 4 work begins, an inheriting engineer should:

1. Read this handoff + Phase 1 + Phase 2 handoffs + Phase 3 kickoff.
2. Re-run every Phase 1/2/3 verifier — confirm none have rotted.
3. Open the Kestra UI (`http://localhost:8086`, basic auth from `.env`)
   and confirm:
   - Both `kestra/flows/georag/*.yaml` are loadable via API
   - The `flow_jwt_public_geoscience_pull` + `flow_jwt_external_notification`
     KV entries exist (mint via `scripts/phase3_jwt_rotate.sh write`)
   - The `external_notification` webhook URL is provisioned + shared with
     senders
4. Pick a Phase 4 first carry-over. **R-P3-1 (per-sender HMAC registry)**
   is the highest-leverage operationally — it unblocks multi-sender
   inbound integration, which is the natural next step now that the
   single-sender slice works end-to-end.

End of Phase 3 handoff.
