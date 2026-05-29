# Phase 2 Handoff — Activepieces adoption + integration boundary

**Document version:** 1.0
**Status:** Phase 2 implementation complete. Phase 3 inheriting.
**Predecessors:** `docs/phase1_handoff.md`, `docs/phase2_scope_proposal.md`,
`docs/phase2_implementation_kickoff.md`, `docs/phase2_activepieces_flows.md`.

---

## 1. What Phase 2 delivered

Phase 2 stood up Activepieces as the integration edge. Two flows
landed end-to-end (one outbound scheduled-import, one inbound webhook),
both behind feature flags, both visible on a new `/admin/integrations`
dashboard, both backed by a Grafana panel reading from the Hatchet
engine's run table.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `activepieces` Postgres role (NOSUPERUSER, NOBYPASSRLS) + logical DB on the existing `postgresql` server | `scripts/phase2_step1_verify.sh` (5/5) |
| 2 | Activepieces docker service (image `activepieces/activepieces:0.83.0`, profile `dev-data`); admin reachable on `:8090` | `scripts/phase2_step2_verify.sh` (5/5) |
| 3 | FastAPI `/internal/v1/integrations/{flow_name}/trigger` route + flow registry (service-key gated); `phase2_smoke` placeholder workflow | `scripts/phase2_step3_verify.sh` (6/6) |
| 4 | First flow — `public_geoscience_pull`: outbound scheduled-import bridge writing `bronze.provenance` + audit | `scripts/phase2_step4_verify.sh` (7/7) |
| 5a | Second flow — `external_notification`: inbound-webhook receiver, idempotent on `notification_id` | `scripts/phase2_step5_verify.sh` (7/7) |
| 6 | `/admin/integrations` Inertia dashboard joining 4 sources (registry + flags + Activepieces flows + Hatchet runs) | `scripts/phase2_step6_verify.sh` (7/7) |
| 7 | Grafana provisioned `PostgreSQL-Hatchet` datasource + `GeoRAG / Integrations` dashboard reading `v1_runs_olap` | `scripts/phase2_step7_verify.sh` (7/7) |
| 8 | This handoff | — |

All seven verifiers green at handoff time — Phase 2 cumulative **46 / 46**.

---

## 2. Architectural state at end of Phase 2

### 2.1 Orchestration ownership

| Engine | Owns | Notes |
|--------|------|-------|
| **Hatchet** | All Phase 0/1 workflows + 3 new Phase 2 ones (`phase2_smoke`, `public_geoscience_pull`, `external_notification`) | AI worker pool now hosts 13 workflows. |
| **Activepieces** | Integration-edge flows only — cron-driven imports, inbound webhooks, third-party connectors | New. Flows live in Activepieces' own DB; recreation runbook is `docs/phase2_activepieces_flows.md`. |
| **Dagster** | `bronze_*` + `silver_*` factory; the `silver_reports` Dagster path retired at Phase 1 cutover | Unchanged for Phase 2. |
| **Laravel queues (Horizon)** | User-triggered async (exports, embeddings) | Unchanged. |

The **integration edge** model (D2 Model A from the scope proposal) is
the rule: external world ⇄ Activepieces ⇄ Hatchet/Dagster/Laravel.
Activepieces is the only thing that talks to third-party SaaS or
accepts inbound webhooks; internal workflows stay on Hatchet.

### 2.2 Data flow — outbound scheduled import

```
Activepieces cron (6h)
  └─ HTTP GET upstream feed
  └─ S3 PUT bronze/public_geoscience/<source_id>/<ts>.geojson
  └─ HTTP POST /internal/v1/integrations/public_geoscience_pull/trigger
                                    │
                                    ▼
              Hatchet workflow public_geoscience_pull
                  ├─ feature-flag gate
                  ├─ S3 GET → validate GeoJSON → SHA256
                  ├─ idempotent INSERT bronze.provenance
                  └─ audit emit 'public_geoscience.pull.complete'
```

### 2.3 Data flow — inbound webhook

```
External sender ──POST──> Activepieces webhook URL
                                   │
                                   ▼
              Code-piece envelope shaping
                  {notification_id, source, kind, payload, received_at}
                                   │
                                   ▼
              HTTP POST /internal/v1/integrations/external_notification/trigger
                                   │
                                   ▼
              Hatchet workflow external_notification
                  ├─ feature-flag gate
                  ├─ idempotency check (audit_ledger by notification_id)
                  └─ audit emit 'external_notification.received'
```

### 2.4 Auth posture

| Surface | Auth | Phase 3 work |
|---------|------|--------------|
| Activepieces admin UI | Isolated Activepieces login (D3a) | SSO via Sanctum (D3b deferred) |
| Activepieces → FastAPI (`/internal/v1/integrations/...`) | `X-Service-Key` (shared with Phase 1 shadow_trigger) | Per-flow JWT (least-privilege) |
| External sender → Activepieces webhook | Activepieces' built-in URL secret + header auth | HMAC-with-shared-secret (per-sender) |
| `/admin/integrations` | Existing `admin` Gate | unchanged |
| `PostgreSQL-Hatchet` Grafana datasource | `grafana_hatchet_readonly` (SELECT only) | unchanged |

### 2.5 Worker-pool layout

```
georag-hatchet-worker-ingestion (slots=20)  — 5 workflows
georag-hatchet-worker-ai        (slots=20)  — 13 workflows
  └─ audit_ledger_verify, shadow_diff, shadow_diff_scan,
     phase2_smoke, public_geoscience_pull, external_notification,
     + 7 Phase 0 agent wrappers
```

`phase2_smoke` stays in the AI pool as a connectivity-debug surface for
ops. Removable at Phase 3 if it adds noise.

---

## 3. Operational state

| Surface | URL / path | Read | Write |
|---------|-----------|------|-------|
| Integrations dashboard | `/admin/integrations` | flow registry + flag state + last-24h Hatchet rollup + Activepieces flow list + flag-flip history | toggle `activepieces.<flow>.enabled` |
| Integrations Grafana | `GeoRAG / Integrations` (port 3000, requires `dev-monitor` profile) | run timeseries by flow + status, success-rate gauge, recent-100 runs table | — |
| Activepieces UI | `http://localhost:8090` | flows / runs / connections (operator-owned) | flow CRUD |
| Activepieces flow runbook | `docs/phase2_activepieces_flows.md` | how to (re-)create the canonical flows in a fresh environment | — |
| Cutover CLI (Phase 1) | `scripts/phase1_step8_traffic.sh` | `get | set | streak | history` for traffic-pct + flag history | yes |

`feature_flag_history` (R-P1-6) auto-captures every flag flip, including
Phase 2's `activepieces.*.enabled` flips — visible at `/admin/integrations`'s
"Recent flag flips" section + `phase1_step8_traffic.sh history`.

---

## 4. Carry-overs for Phase 3

Phase 2 deliberately deferred the items below to keep scope tight. None
of these block production use of the Phase 2 surface; they're items
where Phase 2's "minimum viable" choice has a known better answer.

| ID | Item | Where | Phase 3 rationale |
|----|------|-------|-------------------|
| **R-P2-1** | Activepieces SSO via Sanctum (D3b) | docker-compose env, Activepieces admin UI | Phase 2 chose isolated auth (D3a) to ship faster. SSO is a hardening item once the integration estate grows. |
| **R-P2-2** | Flow definitions as code (export → commit → CI gate) | `docs/phase2_activepieces_flows.md` | Phase 2 keeps flows authoritative in Activepieces' DB; edits go through its UI without code review. Phase 3 builds export-to-JSON + a CI check. |
| **R-P2-3** | Per-flow JWT instead of shared service key | `src/fastapi/app/routers/integrations_trigger.py` | Today every flow uses `FASTAPI_SERVICE_KEY`; a leak compromises all flows. Phase 3 mints a per-flow JWT with `flow:<name>` scope. |
| **R-P2-4** | HMAC-signed webhooks for external senders | `src/fastapi/app/hatchet_workflows/external_notification.py` | Phase 2 trusts the Activepieces-managed webhook URL. Phase 3 adds shared-secret signature verification on a per-sender basis, with rotation. |
| **R-P2-5** | DB-driven flow registry (replaces `FLOW_REGISTRY` dict) | `integrations_trigger.py` + `IntegrationsController` | Phase 2 hard-codes the registry in two places (FastAPI + Laravel). Phase 3 stores flow definitions in Postgres so adding a flow is data-only, no deploy. |
| **R-P2-6** | ~~Per-run duration p50/p95 in `/admin/integrations` rollup~~ | `IntegrationsController::loadHatchetRunRollups` | **CLOSED** post-handoff. Added `loadHatchetDurations()` reading `v1_task_events_olap` (pairing `STARTED` + `FINISHED` events per task, `percentile_disc` aggregated by workflow_name). Real numbers in current soak: `phase2_smoke` p50 3ms, `external_notification` p50 41ms, `public_geoscience_pull` p50 157ms p95 503ms. |
| **R-P2-7** | Generalised dual-write harness (extend Phase 1's `shadow_runs` to non-`ingest_pdf` flows) | `silver.shadow_runs.workflow_kind` | Phase 1 + 2 both hard-code workflow_kind. Phase 3 introduces a third migration target — likely `ingest_csv` for collars/surveys — and parameterises the shadow harness. |
| **R-P2-8** | ~~Activepieces backups in `pg_dump` scope~~ | `docker/postgresql/backup.sh` | **CLOSED** post-handoff. Discovered structurally already covered: the existing schedule uses `pg_basebackup` which is **cluster-level** — it captures every logical DB on the server in one consistent snapshot, including `activepieces`. Updated `backup.sh` header to document this; verifier `phase2_rp28_backups_verify.sh` (4/4) asserts the activepieces DB + DRY_RUN reaches `pg_basebackup` invocation. |
| **R-P2-9** | ~~Activepieces image pin upgrade discipline~~ | `docker-compose.yml` | **CLOSED** post-handoff. Runbook landed at `docs/phase2_activepieces_upgrade.md` — pre-flight (snapshot DB + export flows), upgrade steps, verification gate (Step 2 + 3 verifiers + both flow smokes), rollback procedure, known gotchas (image size, webhook URL rotation across major versions). |
| **R-P2-10** | Hatchet engine HA (Phase 1 R-P1-9 still open) | docker-compose | Lite single-node remains. Worth re-evaluating before user-triggered RAG queries route through Hatchet. |

### 4.1 Earlier-phase carry-overs still open

- **R-P1-2** — SBERT promotion in `shadow_diff` classifier (token Jaccard works fine in current soak).
- **R-P1-3** — outbox-count diff promote to `divergent` (Phase 2 didn't touch outbox).
- **R-P1-5** — per-step OTel spans inside `parse_pdf_report`.
- **R-P1-7** — vendor-profile column-mapping for the parser.
- **R-P1-9** — Hatchet engine HA (see R-P2-10 above; consolidated).
- **R-P1-10** — drop `silver.shadow_runs` ≥ 30 days post-Phase-1-cutover.

Phase 0 carry-overs (R-P0-1 through R-P0-10 from `docs/phase0_handoff.md`)
remain untouched by Phase 2 and are still candidates for Phase 3.

---

## 5. Files of record

```
app/Http/Controllers/Admin/IntegrationsController.php           (new — Step 6)
config/database.php                                              (mod — Step 6)
database/raw/phase2/10-activepieces-role-and-db.sql              (new — Step 1)
database/raw/phase2/20-activepieces-flow-flags.sql               (new — Step 4 + 5a)
database/raw/phase2/30-grafana-hatchet-readonly.sql              (new — Step 7)
docker-compose.yml                                                (mod — Step 2 + 6)
docker/grafana/provisioning/datasources/hatchet.yml              (new — Step 7)
docker/grafana/dashboards/georag-integrations.json               (new — Step 7)
docs/phase2_implementation_kickoff.md                             (Step 0)
docs/phase2_scope_proposal.md                                     (Step 0)
docs/phase2_activepieces_flows.md                                 (Step 4 + 5a)
docs/phase2_handoff.md                                            (this file — Step 8)
resources/js/Pages/Admin/Integrations.tsx                         (new — Step 6)
routes/web.php                                                     (mod — Step 6)
scripts/_phase2_step6_check.php                                   (new — Step 6 probe)
scripts/phase2_step{1..7}_verify.sh                               (each step)
scripts/phase2_step{4,5}_smoke.sh                                 (per-flow)
src/fastapi/app/hatchet_workflows/external_notification.py       (new — Step 5a)
src/fastapi/app/hatchet_workflows/phase2_smoke.py                 (new — Step 3)
src/fastapi/app/hatchet_workflows/public_geoscience_pull.py      (new — Step 4)
src/fastapi/app/hatchet_workflows/worker.py                       (mod — Steps 3/4/5a)
src/fastapi/app/main.py                                            (mod — Step 3)
src/fastapi/app/routers/integrations_trigger.py                  (new — Step 3)
.env                                                               (mod — Steps 2 + 7 secrets)
```

---

## 6. Re-running every Phase 2 verifier

```bash
bash scripts/phase2_step1_verify.sh   # role + DB         (5/5)
bash scripts/phase2_step2_verify.sh   # docker service    (5/5)
bash scripts/phase2_step3_verify.sh   # trigger route     (6/6)
bash scripts/phase2_step4_verify.sh   # public_geoscience_pull (7/7)
bash scripts/phase2_step5_verify.sh   # external_notification  (7/7)
bash scripts/phase2_step6_verify.sh   # /admin/integrations    (7/7)
bash scripts/phase2_step7_verify.sh   # OTel + Grafana         (7/7)
```

Combined with Phase 1's verifiers (Step 4 + 5B + 6 + 7 = 27/27), the
project's done-definition surface is **73 / 73** at Phase 2 close.

---

## 7. Phase 3 entry checklist

Before Phase 3 work begins, an inheriting engineer should:

1. Read this handoff + the Phase 1 handoff + the Phase 2 kickoff +
   `docs/phase2_activepieces_flows.md`.
2. Re-run all Phase 1 + Phase 2 verifiers — confirm none have rotted.
3. Open the Activepieces UI (`http://localhost:8090`) and confirm the
   isolated admin login works with the env-provided credentials.
4. Create the canonical flows from the runbook so the
   `/admin/integrations` dashboard shows real Activepieces-side rows
   (today's verifier ran with zero defined flows — that's expected
   for a fresh environment but uninformative for soak testing).
5. Pick a Phase 3 first carry-over. **R-P2-2 (flow-as-code)** is the
   highest-leverage: until it lands, every fresh environment requires
   manual flow re-creation, which is the runbook's biggest pain point.

End of Phase 2 handoff.
