# GeoRAG Phase 1 Implementation Kickoff

**Document version:** 1.0
**Status:** Locked. Phase 1 build plan against `GeoRAG_master_plan_v2.4.2.md` §30 (Phase 1) + `georag-agent-registry-v1.3.md` + `docs/phase0_handoff.md`.
**Audience:** Claude Code in agent mode (primary), Kyle as product owner (oversight).
**Date:** 2026-05-09

---

## What this document is

The Phase 0 kickoff translated the master plan's §30 Phase 0 entry into a 9-step sequence with explicit acceptance per step. This document does the same for Phase 1.

Phase 1 is **Hatchet adoption + first workflow migration**. Per master plan §30:

> Stand up Hatchet; migrate first workflow.
>
> Deliverables:
> - Hatchet engine + Postgres-backed durability
> - Two worker pools deployed (`python-worker-ingestion`, `python-worker-ai`)
> - `ingest_pdf` workflow implemented, runs in shadow alongside v1.49 path
> - Comparison dashboard for shadow runs (output diffs)
> - 14-day parallel run window (start at 1% traffic, scale to 100%)
> - Hatchet Worker Dashboard
>
> **Done when:** 100% of new PDF ingestion runs through Hatchet for 7 consecutive days with zero divergence, and v1.49 ingestion path retired.

Phase 0 already shipped the Hatchet engine + Postgres durability + a single worker (`georag-hatchet-worker`) that runs `audit_ledger_verify` + `outbox_dispatcher`. So deliverable #1 is partially done; Phase 1 picks up at the worker pool split.

When the master plan or registry conflict with this document on Phase 1 specifics, **this document wins** for Phase 1 sequencing — those documents describe the destination, this document describes the path.

---

## Phase 1 done definition (from master plan §30)

> 100% of new PDF ingestion runs through Hatchet for 7 consecutive days with zero divergence, and v1.49 ingestion path retired.

Translated into concrete acceptance tests (see § Acceptance tests at end):

1. Two named worker pools running (`python-worker-ingestion`, `python-worker-ai`) — each subscribes to its own Hatchet queue (action) and worker registration is visible in the engine.
2. The Hatchet `ingest_pdf` workflow is registered, schema-validated, and triggerable both on-demand and via the dispatch path that v1.49 uses.
3. A shadow harness routes a configurable % of `ingest_pdf` requests to BOTH paths (v1.49 + Hatchet), captures both outputs, computes a structured diff, and stores the result in `silver.shadow_runs` (new table — Phase 1 schema).
4. The shadow comparison dashboard renders the last 100 shadow runs with diff classification (clean / minor / divergent / fatal).
5. Traffic ramp gate: starts at 1%, walks 1%→10%→50%→100% based on a daily zero-divergence check; the ramp control lives in `workspace.feature_flags` (new table) read by the request router.
6. Hatchet Worker Dashboard renders worker pool health (workers connected per pool, recent step latencies, recent failures).
7. `phase1_acceptance.sh` mirrors `phase0_acceptance.sh` shape — runs all per-step verifies + an e2e shadow-run check + cross-cutting health.
8. After the 14-day window with 7 consecutive zero-divergence days, v1.49 `ingest_pdf` code path is removed (PR with explicit deletion list, archived `_deprecated/` mirror per Phase 0 convention).

Phase 1 does NOT ship: any new agents, any UI beyond the dashboards, the rest of the v1.49 workflows (those are Phase 1+ continuation, scoped per workflow), Activepieces (Phase 2).

---

## Pre-Phase 1 carry-over from Phase 0 handoff

Per `docs/phase0_handoff.md` §4 + §5, the following items were deferred from Phase 0. Phase 1 picks them up in this priority order:

| # | Carry-over | Status entering Phase 1 | Phase 1 disposition |
|---|---|---|---|
| 1 | **R-P0-10** — `georag` PostgreSQL role is a SUPERUSER with `rolbypassrls=true`; RLS silently ineffective | New finding 2026-05-09 | **MUST FIX before Step 4** of Phase 1 (before shadow ingest writes silver rows). Step 1.A below. |
| 2 | `/admin/agent-config/*` Inertia surfaces | Spawned worktree, mid-flight | Land naturally via the existing spawned task; don't block Phase 1 on it. Verify-script will go to 9/9 once it merges. |
| 3 | Hatchet scheduling for the 10 Phase 0 agents | Agents are `@georag_agent`-decorated callables; no cron yet | **Step 2** of Phase 1 (mirror the audit_ledger_verify / outbox_dispatcher worker pattern; one workflow per agent). |
| 4 | R-P0-1 (JCS for hash recipe), R-P0-2 (audit UPDATE/DELETE guard), R-P0-3 (physical SeaweedFS tiers), R-P0-5 (OTel self-telemetry), R-P0-6 (clock_timestamp guard), R-P0-8 (windowed-LAG verifier fix) | Phase 11 hardening items | Not in Phase 1 scope. Documented in handoff §5; track in §32 risks register. |

---

## How Claude Code should read this document

Same conventions as the Phase 0 kickoff:

- Each step has **Prerequisites**, **Deliverables**, **Implementation notes**, **Definition of done**, **Cross-references**.
- If a step's done definition can't be met because of an upstream gap, **halt and surface** rather than invent.
- Authorized to decide: implementation language within the established stack, internal naming, file organisation within established package boundaries, test fixture content.

---

## Document structure

| Step | Section | Audience builds |
|---|---|---|
| 1 | § Pre-Phase 1 (R-P0-10 role split) | Non-superuser app role + grants + cred swap |
| 2 | § Worker pool split + Phase 0 agent scheduling | Two worker containers, register Phase 0 agents on cron |
| 3 | § v1.49 `ingest_pdf` survey | Read-only mapping of the existing path |
| 4 | § Hatchet `ingest_pdf` workflow | Python implementation + schema |
| 5 | § Shadow harness + diff store | Dual-write, diff, traffic-flag table |
| 6 | § Shadow comparison dashboard | Laravel + Inertia route |
| 7 | § Hatchet Worker Dashboard | Laravel + Inertia route |
| 8 | § Acceptance + 14-day shadow + retire v1.49 | Calendar + final cutover |
| 9 | § Handoff to Phase 2 | What Phase 2 reads from Phase 1 |

Build Steps 1–7 in order; Step 8 is the calendar window plus the final retirement.

---

## Step 1 — Pre-Phase 1: split the application Postgres role (R-P0-10)

**Goal:** Stop relying on a superuser for application traffic. Introduce a non-superuser role that has the grants the app needs but no `BYPASSRLS`, so the Phase 0 RLS policies actually apply.

### Prerequisites

- Phase 0 closed out per acceptance.

### Deliverables

- New role `georag_app` (LOGIN, NOSUPERUSER, NOBYPASSRLS, password from env).
- Grants: USAGE on every Phase 0 schema; INSERT/UPDATE/SELECT/DELETE on every workspace-scoped table; SELECT on shared catalogs; EXECUTE on `audit.*` functions.
- `georag` role kept SUPERUSER for migrations + admin only. Update `init-roles.sql` so future fresh deploys provision both roles correctly.
- `docker-compose.yml` updated: `laravel-octane`, `laravel-horizon`, `laravel-reverb`, `fastapi`, `georag-hatchet-worker` all switch their `POSTGRES_USER` / `AWS_*` (no — only DB) to `georag_app`. PgBouncer routing updated.
- `docs/RUNBOOK.md` section: "When to use georag vs georag_app".

### Implementation notes

- Use `pg_dump` of the running DB's grants as a starting point so we don't miss permissions Laravel migrations have introduced.
- `pg_partman` operations (run by the partition maintenance job) need `georag` not `georag_app` because partition creation requires elevated rights. Keep the partition maintenance Hatchet workflow on the superuser role.
- `pgbouncer` user mapping: route `georag` and `georag_app` to two different pools to avoid GUC bleed between them.
- Verify with the Tenant Isolation Auditor: a fresh run after the role split should report `violations=0` (was 4 pre-split).

### Definition of done

```bash
# georag_app exists with the right attributes
docker exec georag-postgresql psql -U georag -d georag -c "
  SELECT rolname, rolsuper, rolbypassrls, rolcanlogin
  FROM pg_roles WHERE rolname = 'georag_app';"
# → georag_app | f | f | t

# Tenant Isolation Auditor reports zero violations
WS_ID=00000000-acc6-eea4-cccc-111111110001 \
    docker exec georag-fastapi python3 -c "..." # run agent
# violations=0

# Application services are connected as georag_app, not georag
docker exec georag-fastapi sh -c 'echo "$POSTGRES_USER"'   # → georag_app
docker exec georag-laravel-octane sh -c 'echo "$DB_USERNAME"' # → georag_app

# All existing Phase 0 acceptance still green after the cutover
bash scripts/phase0_acceptance.sh   # → 16/16 (15 was without admin UI; this should be 15+1 once admin UI lands)
```

---

## Step 2 — Worker pool split + Phase 0 agent scheduling

**Goal:** Split the single `georag-hatchet-worker` into two named worker pools and register the 10 Phase 0 agent callables as Hatchet cron workflows on the appropriate pool.

### Prerequisites

- Step 1 complete (`georag_app` role active).

### Deliverables

| Component | Notes |
|---|---|
| `georag-hatchet-worker-ingestion` container | Subscribes to action prefix `ingestion:*`. Runs `outbox_dispatcher`, `ingest_pdf` (Step 4+), `storage_tiering_run`, `index_health_check`, `store_reconciliation_run`. |
| `georag-hatchet-worker-ai` container | Subscribes to action prefix `ai:*`. Runs `audit_ledger_verify`, `tenant_isolation_audit`, `lineage_walk` (when invoked async), `model_upgrade_watch_run`, `vllm_security_check_run`, `model_cost_summary_run`, `llm_incident_diagnosis_run`, `support_packet_assemble`. |
| Cron schedules | Per kickoff §Step 6 (Phase 0): tenant isolation 02:00 UTC, storage tiering 03:00 UTC, index health every 6h, store reconciliation 04:00 UTC, model upgrade watch + vLLM security check daily, model cost summary 06:00 UTC. |
| `phase1_step2_verify.sh` | Confirms both worker containers healthy, Hatchet engine sees workers in two pools, all 12 workflows registered (10 agents + audit_verify + outbox_dispatcher). |

### Implementation notes

- Worker affinity: Hatchet supports per-worker action filters via the SDK — `worker.start(actions=["ingestion:*"])`. Use that.
- The two workers share the same FastAPI image; only the entry command differs. Add a small Python entrypoint that picks the action filter from `WORKER_POOL` env var.
- Resource shaping: ingestion pool gets more CPU + memory (PDF parsing is heavy); AI pool needs vLLM connectivity, less CPU.
- Don't break the existing `audit_ledger_verify` / `outbox_dispatcher` cron schedules — they should keep running through the cutover.

### Definition of done

```bash
# Two worker containers healthy
docker ps --filter name=georag-hatchet-worker --format '{{.Names}}|{{.Status}}'
# → georag-hatchet-worker-ingestion | Up ... (healthy)
# → georag-hatchet-worker-ai        | Up ... (healthy)

# Hatchet engine knows about both pools
docker exec georag-postgresql psql -U hatchet -d hatchet -c "
  SELECT name, count(*) FROM \"Worker\" WHERE last_heartbeat_at > now() - interval '60 seconds' GROUP BY name;"
# → 2 rows

# All 12 workflows registered
docker exec georag-hatchet-worker-ingestion python -m app.hatchet_workflows.worker --list \
  | sort | uniq | wc -l
# → 12
```

---

## Step 3 — v1.49 `ingest_pdf` survey

**Goal:** Map the existing v1.49 ingestion path so the Hatchet replacement has a precise contract to satisfy.

### Prerequisites

- None — read-only.

### Deliverables

- `docs/phase1_v149_ingest_pdf_survey.md` capturing:
  - Entry points (Laravel route, Dagster sensor, FastAPI background, queue dispatch?)
  - Inputs (PDF source: SeaweedFS bronze key? upload? URL?), outputs (silver tables touched, audit_ledger entries, outbox propagations)
  - Stages (download → preflight → text extract → layout → OCR → tables → embed → index)
  - Failure modes + retry behaviour
  - Per-stage timing measured against 5 representative PDFs (small / medium / large / scanned / complex layout)
- A diff contract: what counts as "same output" vs "divergent" — exact match on silver row IDs + counts, fuzzy match on extracted text (>= 0.95 cosine), exact match on audit_ledger action_type set per run.

### Implementation notes

- This step is mostly reading + grep-ing. Use the `Explore` subagent.
- The diff contract is the single most important deliverable — it determines what passes/fails the 14-day shadow.

### Definition of done

```bash
test -f docs/phase1_v149_ingest_pdf_survey.md
grep -q '## Diff contract' docs/phase1_v149_ingest_pdf_survey.md
```

Phase 1 implementer reads this doc + signs off ("contract is what I'm replicating"). No automated check beyond doc existence.

---

## Step 4 — Hatchet `ingest_pdf` workflow

**Goal:** Implement `ingest_pdf` as a Hatchet workflow that satisfies the v1.49 contract from Step 3.

### Prerequisites

- Steps 1–3 complete.
- New table `silver.shadow_runs` and `workspace.feature_flags` provisioned.

### Deliverables

- `src/fastapi/app/hatchet_workflows/ingest_pdf.py` — workflow with one `@workflow.step` per stage in the v1.49 contract.
- New migration `database/raw/phase1/10-shadow-runs.sql` — `silver.shadow_runs`, `workspace.feature_flags`.
- Registered on the `python-worker-ingestion` pool with action prefix `ingestion:ingest_pdf`.
- Triggerable via `hatchet workflow trigger ingestion:ingest_pdf --inputs '{...}'`.
- Smoke: a known-good PDF runs end-to-end through Hatchet only and produces the same silver rows as v1.49.

### Implementation notes

- Each step uses `@georag_agent` if it contains agent invocations; pure plumbing (download, archive, queue) does not need the wrapper.
- Step retries: piggyback on Hatchet's built-in retry policy (declare `retries=3, retry_backoff=exponential` per step).
- Resource limits: pin `max_concurrent_runs` per worker so a flood of large PDFs doesn't OOM the pool.

### Definition of done

```bash
# Workflow registered
hatchet workflow list | grep ingestion:ingest_pdf

# Smoke against a known-good PDF
TEST_PDF=tests/fixtures/phase1_smoke_drillhole.pdf
RUN_ID=$(hatchet workflow trigger ingestion:ingest_pdf \
  --inputs "{\"pdf_uri\": \"s3://bronze/$TEST_PDF\", \"workspace_id\": \"...\"}" --json | jq -r .run_id)
sleep 60
docker exec georag-postgresql psql -U georag_app -d georag -c "
  SELECT status, ended_at IS NOT NULL FROM workflow.workflow_runs WHERE run_id = '$RUN_ID';"
# → success | t
```

---

## Step 5 — Shadow harness + diff store + traffic flag

**Goal:** Wire the request router so a configurable % of `ingest_pdf` requests are sent to BOTH the v1.49 path AND the Hatchet workflow, the outputs are captured, and a structured diff is stored in `silver.shadow_runs`.

### Prerequisites

- Step 4 complete (Hatchet `ingest_pdf` works in isolation).

### Deliverables

- `app/Services/Ingestion/ShadowRouter.php` (Laravel side — most ingestion entry points are Laravel) that reads `workspace.feature_flags.ingest_pdf_hatchet_traffic_pct`, decides per request whether to dual-write, and emits a `shadow_run_id` correlation token to both paths.
- A diff worker (Hatchet workflow `ai:shadow_diff`) that picks up completed shadow runs and writes one `silver.shadow_runs` row per pair with `classification` ∈ `clean | minor | divergent | fatal`.
- The traffic % starts at **0** and is operator-managed (Step 6 dashboard exposes the toggle in Phase 1; Phase 8's calendar phase rolls it forward).

### Implementation notes

- The diff contract from Step 3 §"Diff contract" determines `classification`. Keep the diff logic in one Python module so changes are reviewable.
- Both paths must complete for a row to be classified — partial failures get `classification='partial'` and don't count toward the 7-day-zero-divergence gate.
- Idempotency: dual-write must be R3-safe; use the wrapper's R3 idempotency keyed on `(workspace_id, shadow_run_id, path)`.

### Definition of done

```bash
# Synthetic dual-write produces matching silver rows + classification='clean'
bash scripts/phase1_shadow_smoke.sh
# → asserts: 1 shadow_runs row with classification='clean' for the test PDF
```

---

## Step 6 — Shadow comparison dashboard

**Goal:** Operator surface for shadow runs — last 100 by `created_at desc`, filterable by classification + workspace.

### Prerequisites

- Step 5 complete.

### Deliverables

- `App\Http\Controllers\Admin\ShadowRunController` + `resources/js/Pages/Admin/ShadowRuns.tsx` (mirrors `WorkflowRuns.tsx` pattern from Phase 0 Step 3).
- Detail view per shadow run: side-by-side v1.49 vs Hatchet outputs, diff highlights, audit_ledger trace_id link to Tempo.
- Operator action: bump traffic % (writes `workspace.feature_flags`, audited).

### Implementation notes

- Mirror the Workflow Run Dashboard skeleton authored after Phase 0 Step 3. Same admin auth, same DataTable.
- Spawnable as a separate task (Laravel + Inertia work) — the kickoff anticipates this is its own ~one-session lift.

### Definition of done

```bash
curl -s -o /dev/null -w '%{http_code}' http://laravel-octane:80/admin/shadow-runs
# → 200/302/401/403
```

---

## Step 7 — Hatchet Worker Dashboard

**Goal:** Operator surface for Hatchet worker health — workers per pool, recent step latencies, recent failures, queue depth.

### Prerequisites

- Step 2 complete (workers are running).

### Deliverables

- `App\Http\Controllers\Admin\HatchetWorkerController` + `resources/js/Pages/Admin/HatchetWorkers.tsx`.
- Backed by Hatchet's REST API (`http://hatchet-lite:8888/api/v1/...`) + the worker heartbeat table in the Hatchet Postgres DB.
- Read-only — no worker control actions in Phase 1; that's Phase 10 Customer Support Cockpit.

### Definition of done

```bash
curl -s -o /dev/null -w '%{http_code}' http://laravel-octane:80/admin/hatchet-workers
# → 200/302/401/403
```

---

## Step 8 — Acceptance + 14-day shadow + retire v1.49

**Goal:** Run the shadow window, watch for zero-divergence days, ramp traffic, retire the v1.49 path.

### Prerequisites

- Steps 1–7 all green.
- `phase1_acceptance.sh` exists (mirrors `phase0_acceptance.sh` structure).

### Deliverables

- Daily shadow report (existing Hatchet workflow `ai:shadow_diff` rolls up daily into `silver.shadow_runs_daily`).
- Operator decision matrix:
  - Day 0: traffic = 1%
  - Day +1 zero-div: 10%
  - +1 more zero-div: 50%
  - +1 more zero-div: 100%
  - +7 more zero-div at 100%: cutover ready
- Cutover PR: deletes the v1.49 `ingest_pdf` code path, archives the deleted files under `_deprecated/v149_ingest_pdf/`, removes the `ShadowRouter` dual-write (kept for future migrations as a library).

### Definition of done

```bash
# Final acceptance — full Phase 1
bash scripts/phase1_acceptance.sh
# → 100% pass

# Hatchet alone owns ingest_pdf
grep -r 'old_v149_ingest_pdf' src/ app/ 2>&1 | wc -l
# → 0

# Calendar gate (operator-confirmed via the shadow dashboard, not a script):
# 7 consecutive days at 100% with classification='clean' for every shadow run
```

---

## Step 9 — Handoff to Phase 2

Phase 2 (Activepieces adoption + integration boundary migration) builds on Phase 1's foundation. Phase 1 ends with `docs/phase1_handoff.md` covering:

- Acceptance result (full per-step + master)
- Worker pool topology + how to add a new pool
- Lessons from the shadow window (any divergence reasons, even if reconciled)
- Carry-overs to Phase 2 (anything noted but not fixed)
- Updated risks register additions
- Spec deviations for v2.4.4 doc revision (running list grows)

Phase 2 reads `docs/phase1_handoff.md` first, then proceeds.

---

## Closing

Phase 1 is the first **migration** phase, not just a build phase. The 14-day shadow window is the heart of it — the engineering ramp (Steps 1–7) is two-three sessions of work, then the calendar carries it. Discipline matters: don't accelerate the ramp because the dashboard "looks clean"; the master plan §30 done-definition is explicit (7 consecutive days at 100% with zero divergence).

When Step 8's calendar gate is hit, sign off, write the Phase 1 handoff, move to Phase 2 (Activepieces).

End of Phase 1 implementation kickoff document.
