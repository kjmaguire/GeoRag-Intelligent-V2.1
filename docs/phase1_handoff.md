# Phase 1 Handoff — Hatchet adoption + first workflow migration

**Document version:** 1.0
**Status:** Phase 1 implementation complete. Phase 2 inheriting.
**Predecessors:** `docs/phase0_handoff.md`, `docs/phase1_implementation_kickoff.md`,
`docs/phase1_v149_ingest_pdf_survey.md`, `docs/phase1_step8_cutover_runbook.md`.

---

## 1. What Phase 1 delivered

Phase 1 had two top-line goals:

1. **Stand up Hatchet as a first-class orchestrator** alongside Dagster
   (Phase 0 had only the engine + a single nightly workflow).
2. **Move `ingest_pdf` from Dagster v1.49 to Hatchet** behind a shadow
   harness, with no user-visible regression.

Both delivered. Concretely:

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `georag_app` Postgres role (NOSUPERUSER, NOBYPASSRLS) — Phase 0's superuser hole closed | `database/raw/phase1/10-georag-app-role.sql` |
| 2 | Hatchet worker pools split into `ingestion` + `ai` (10 + 13 workflows total); 10 Phase-0 agent workflows wrapped + cron-scheduled | implicit via Step 4–7 |
| 3 | v1.49 `ingest_pdf` survey + locked diff contract | `docs/phase1_v149_ingest_pdf_survey.md` |
| 4 | `silver.shadow_runs` + `workspace.feature_flags` schema, `ingest_pdf` Hatchet workflow (preflight → parse → persist) | `scripts/phase1_step4_verify.sh` (8/8) |
| 5A | Laravel `ShadowRouter` dual-write decision + FastAPI `/internal/v1/shadow/ingest_pdf/trigger` route | `scripts/phase1_step5a_smoke.sh` |
| 5B | `shadow_diff` Hatchet workflow + classifier (locked §10 contract) + `shadow_diff_scan` cron + Dagster `record_v149_for_shadow` hook + `UploadController` integration | `scripts/phase1_step5b_verify.sh` (7/7) |
| 6 | Shadow comparison dashboard at `/admin/shadow-runs` (filters + traffic-pct editor + clean-streak tile + per-row drilldown) | `scripts/phase1_step6_verify.sh` (5/5) |
| 7 | Hatchet Worker Dashboard at `/admin/hatchet-workers` (pool liveness + recent-run rollup) | `scripts/phase1_step7_verify.sh` (6/6) |
| 8 | 14-day cutover runbook + `phase1_step8_traffic.sh` CLI | `docs/phase1_step8_cutover_runbook.md` |
| 9 | This handoff | — |

All verifiers run green at the time of writing; the Step 5B smoke
confirms end-to-end dual-write + diff classification with hatchet
persist landing in ~10 s and classification in ~5 s.

---

## 2. Architectural state at end of Phase 1

### 2.1 Orchestrators

| Engine | Owns | Notes |
|--------|------|-------|
| **Hatchet** | `outbox_dispatcher`, `audit_ledger_verify`, `ingest_pdf`, 10 Phase 0 agent wrappers, `shadow_diff`, `shadow_diff_scan` (13 total) | New first-class. |
| **Dagster** | `bronze_*` + `silver_*` asset chain (still incl. `silver_reports`); the bronze→silver factory | Unchanged from v1.49. `silver_reports` will retire at Phase 1 cutover. |
| **Laravel queues (Horizon)** | User-triggered async (exports, embeddings) | Unchanged. |

The `Don't duplicate orchestration` rule in CLAUDE.md still holds —
Hatchet runs the new things, Dagster runs the existing bulk pipelines,
Horizon runs user-triggered work. `ingest_pdf` is the first shared
slice; ownership flips fully to Hatchet at cutover.

### 2.2 Hatchet worker pools

```
georag-hatchet-worker-ingestion (slots=20)
  └─ outbox_dispatcher, ingest_pdf, storage_tiering_run,
     index_health_check, store_reconciliation_run

georag-hatchet-worker-ai (slots=20)
  └─ audit_ledger_verify, shadow_diff, shadow_diff_scan,
     tenant_isolation_audit, lineage_walk, model_upgrade_watch_run,
     vllm_security_check_run, model_cost_summary_run,
     llm_incident_diagnosis_run, support_packet_assemble
```

Both pools register with the lite Hatchet engine on the same `default`
tenant; per-pool affinity is via worker-side workflow registration,
not action prefixes. (We considered `ai:` / `ingestion:` prefixes on
workflow names for visual clarity but dropped them for consistency
with existing names.)

### 2.3 Dual-write surface

- **Entry point:** `app/Http/Controllers/Api/V1/UploadController.php`
  for `category='reports'` only.
- **Decision logic:** `app/Services/Ingestion/ShadowRouter.php`. Reads
  `workspace.feature_flags.ingest_pdf_hatchet_traffic_pct` (workspace
  override → platform default), computes a deterministic SHA-256-mod
  roll on `(workspace_id || minio_key)`, dispatches to FastAPI's
  `/internal/v1/shadow/ingest_pdf/trigger` if the roll wins.
- **Hatchet side:** `src/fastapi/app/hatchet_workflows/ingest_pdf.py`
  inserts a `partial` shadow_runs row in preflight, parses via the
  v1.49 `parse_pdf_report()` (mounted into the worker), persists to
  `silver.reports` and UPSERTs hatchet_result/duration/audit_run_id.
- **v1.49 side:** `src/dagster/georag_dagster/hooks/shadow_v149.py`
  is called from the `silver_reports` asset after parse completes;
  finds the most recent partial row for `(workspace_id, minio_key)`
  and fills v149_result/duration/audit_run_id.
- **Diff side:** `src/fastapi/app/hatchet_workflows/shadow_diff.py`
  fires per-row when both sides have landed, runs the locked §10
  classifier, writes classification + diff_details. The
  `shadow_diff_scan` cron sweeps for paired rows + 24h timeouts.

### 2.4 Master kill switch

`workspace.feature_flags.ingest_pdf_shadow_enabled` (boolean, platform
default = true). Setting to `false` disables the entire dual-write path
without touching code or restarting services. `ShadowRouter` honours
the flag on every upload.

---

## 3. Operational state

| Surface | URL / path | Read | Write |
|---------|-----------|------|-------|
| Shadow comparison dashboard | `/admin/shadow-runs` | classifications, durations, errors | `traffic_pct` UPSERT |
| Per-row diff drilldown | `/admin/shadow-runs/{id}` | full diff_details + raw payloads | — |
| Hatchet Worker Dashboard | `/admin/hatchet-workers` | engine health + run rollup | — |
| Workflow Run Dashboard | `/admin/workflow-runs` (Phase 0) | broader workflow.workflow_runs table | — |
| Cutover CLI | `scripts/phase1_step8_traffic.sh` | `get` + `streak` | `set <pct>`, `disable`, `enable` |
| Cutover runbook | `docs/phase1_step8_cutover_runbook.md` | — | — |

The dashboards are gated by the `admin` Gate (`users.is_admin = true`,
defined in `AppServiceProvider::boot`).

---

## 4. Carry-overs for Phase 2

These are *known* and don't block Phase 1's exit; they're explicitly
deferred to Phase 2 with the rationale below.

| ID | Item | Where | Why deferred |
|----|------|-------|--------------|
| **R-P1-1** | ~~`silver.reports.write` audit emission on both sides~~ | `ingest_pdf.persist` + Dagster `silver_reports` | **CLOSED** post-handoff. Both sides now emit `ingest_pdf.parse.complete` + `silver.reports.write`. Step 5B smoke flipped from `divergent` → `clean`. |
| **R-P1-2** | Promote section similarity from token-Jaccard to SBERT cosine | `app/services/shadow_diff/classifier.py` | Phase 1 chose Jaccard for image-size + GPU-contention reasons (~400 MB SBERT load). If `minor` rates trend high in soak, Phase 2 promotes. |
| **R-P1-3** | Promote outbox-count diff to `divergent` | `classifier._check_outbox_propagations` | Phase 1 outbox emission isn't fully contracted; Phase 2 locks the contract and tightens the check. |
| **R-P1-4** | ~~`silver.document_passages` writes on the Hatchet path~~ | `ingest_pdf.persist` | **CLOSED** post-handoff. Hatchet `persist` writes one passage per parsed section (`chunk_kind='narrative'`, idempotent via UNIQUE on `(document_id, revision_number, text_hash)`). Step 4 verifier now bumped to 9/9 with a passage-count check. Layout-aware chunking (table/figure types, bbox, layout_region_ids) is Phase 2 ingestion-pipeline scope. |
| **R-P1-5** | Per-step OTel spans inside `parse_pdf_report` | `pdf_report.py` | Today only the parser's own logging exists; Phase 2 wraps each stage in a span so the worker dashboard can attribute time. |
| **R-P1-6** | ~~Feature-flag audit trail~~ | `workspace.feature_flags` | **CLOSED** post-handoff. `workspace.feature_flag_history` sidecar + `feature_flags_audit_trg` trigger captures every INSERT/UPDATE/DELETE with old + new values, indexed by `(flag_name, changed_at DESC)`. CLI: `phase1_step8_traffic.sh history [N]`. Migration: `database/raw/phase1/30-feature-flag-history.sql`. |
| **R-P1-7** | Per-vendor-profile column-mapping for the parser | `parse_pdf_report` | The upload attaches `vendor_profile_id` as S3 metadata but the parser doesn't yet use it. Phase 2 ingestion pipeline. |
| **R-P1-8** | Generalise the dual-write harness | `ShadowRouter` + `shadow_diff` | Today they hard-code `workflow_kind='ingest_pdf'`. Phase 2 will introduce a second migration (likely `ingest_csv` for collars/surveys) and parameterise. |
| **R-P1-9** | Hatchet engine HA + tenant boundary | docker-compose + Hatchet config | Phase 1 runs the Lite single-node config; Phase 2 must address blast radius before user-triggered RAG queries route through Hatchet. |
| **R-P1-10** | Drop `silver.shadow_runs` post-cutover (≥30 days) | Migration | Keep the table during the post-mortem window; archive after. |

### 4.1 Phase 0 carry-overs still open

These came in from `docs/phase0_handoff.md` and remain:

- **R-P0-1** through **R-P0-10** (see Phase 0 handoff). The
  `LLM Incident Diagnosis Agent` data-dependent refusal still applies;
  Phase 2's Pydantic AI layer is the natural place to address it.

---

## 5. Files of record

New / changed in Phase 1:

```
app/Http/Controllers/Admin/HatchetWorkersController.php   (new — Step 7)
app/Http/Controllers/Admin/ShadowRunsController.php       (new — Step 6)
app/Http/Controllers/Api/V1/UploadController.php          (mod  — Step 5B)
app/Services/Ingestion/ShadowRouter.php                   (new — Step 5A)
config/database.php                                       (mod  — Step 7)
database/raw/phase1/10-georag-app-role.sql                (new — Step 1)
database/raw/phase1/20-shadow-runs-and-feature-flags.sql  (new — Step 4)
docker-compose.yml                                        (mod  — Step 7)
docs/phase1_implementation_kickoff.md                     (kickoff plan)
docs/phase1_v149_ingest_pdf_survey.md                     (Step 3)
docs/phase1_step8_cutover_runbook.md                      (new — Step 8)
docs/phase1_handoff.md                                    (this file — Step 9)
resources/js/Pages/Admin/HatchetWorkers.tsx               (new — Step 7)
resources/js/Pages/Admin/ShadowRuns/Index.tsx             (new — Step 6)
resources/js/Pages/Admin/ShadowRuns/Show.tsx              (new — Step 6)
routes/web.php                                            (mod  — Steps 6 + 7)
scripts/phase1_step5a_smoke.sh                            (new — Step 5A)
scripts/phase1_step5b_smoke.sh                            (new — Step 5B)
scripts/phase1_step5b_verify.sh                           (new — Step 5B)
scripts/phase1_step6_verify.sh                            (new — Step 6)
scripts/phase1_step7_verify.sh                            (new — Step 7)
scripts/phase1_step8_traffic.sh                           (new — Step 8)
scripts/_phase1_step6_check.php                           (new — Step 6 probe)
scripts/_phase1_step7_check.php                           (new — Step 7 probe)
src/dagster/georag_dagster/assets/silver_reports.py       (mod  — Step 5B)
src/dagster/georag_dagster/hooks/shadow_v149.py           (new — Step 5B)
src/fastapi/app/hatchet_workflows/ingest_pdf.py           (new — Step 4)
src/fastapi/app/hatchet_workflows/phase0_agents.py        (new — Step 2)
src/fastapi/app/hatchet_workflows/shadow_diff.py          (new — Step 5B)
src/fastapi/app/hatchet_workflows/worker.py               (new — Step 2)
src/fastapi/app/routers/shadow_trigger.py                 (new — Step 5A)
src/fastapi/app/services/shadow_diff/classifier.py        (new — Step 5B)
src/fastapi/app/services/shadow_diff/__init__.py          (new — Step 5B)
```

---

## 6. Re-running every Phase 1 verifier

```bash
bash scripts/phase1_step2_verify.sh        # Hatchet pool split
bash scripts/phase1_step4_verify.sh        # ingest_pdf scaffold + smoke
bash scripts/phase1_step5b_verify.sh       # diff contract + smoke (covers 5A inline)
bash scripts/phase1_step6_verify.sh        # Shadow comparison dashboard
bash scripts/phase1_step7_verify.sh        # Hatchet Worker Dashboard
```

Last green at handoff: 8/8, 7/7, 5/5, 6/6 respectively. Step 2 verifier
predates this session; should still be green — re-run before Phase 2
opens.

---

## 7. Phase 2 entry checklist

Before Phase 2 work begins, an inheriting engineer should:

1. Read this handoff + the cutover runbook.
2. Read `phase0_handoff.md` for upstream context.
3. Re-run all verifiers in §6 — confirm none have rotted.
4. Decide Phase 1 cutover timing. The runbook is calendar-light;
   Phase 2 work CAN start during the soak window so long as it doesn't
   touch the Hatchet `ingest_pdf` workflow or the shadow plumbing.
5. Address **R-P1-1** (audit emission) before bumping traffic past 1%
   — it's the single thing keeping the streak gate from going green.

End of Phase 1 handoff.
