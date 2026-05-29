# Phase 2 Implementation Kickoff — Activepieces adoption + integration boundary

**Document version:** 1.0
**Status:** Active. Scope locked per `docs/phase2_scope_proposal.md` sign-off.
**Predecessors:** `docs/phase1_handoff.md`, `docs/phase2_scope_proposal.md`.

---

## 1. Locked decisions (recap from scope proposal)

- **D1 — First flow:** scheduled public-geoscience import.
- **D2 — Topology:** Model A (Activepieces is the integration edge —
  external world ⇄ Activepieces ⇄ Hatchet/Dagster/Laravel).
- **D3 — Auth:** isolated Activepieces admin login. Sanctum SSO is
  Phase 3+.
- **D4 — Persistence:** new `activepieces` logical DB on the existing
  `postgresql` server, separate `activepieces` role.
- **D5 — Cutover model:** feature-flag gating per flow
  (`activepieces.<flow>.enabled`); reuse `workspace.feature_flag_history`
  (R-P1-6) for the audit trail. No shadow harness — these are
  greenfield flows.
- **D6 — Ceiling:** 8 steps, ~Phase 1's footprint.

---

## 2. Done definition for Phase 2

Phase 2 is **done** when all of:

1. Activepieces is healthy in the dev stack (`docker compose up -d` brings
   it up; healthcheck passes; admin UI reachable).
2. Schema + role isolation matches the Phase 0 + Phase 1 pattern
   (`activepieces` role, `activepieces` logical DB, no superuser).
3. The first scheduled-import flow runs end-to-end: Activepieces cron →
   HTTP GET upstream → S3 drop → triggers a Hatchet ingestion workflow
   → row lands in the existing `bronze_*` table. Behind a feature flag.
4. `/admin/integrations` Inertia page shows: registered flows, enabled
   state, last 24h run rollup (success/failure/duration), recent
   failures with the upstream HTTP status.
5. OTel collector ingests Activepieces flow runs (one span per flow run,
   tagged with `flow.id` + `flow.version`); a Grafana panel exists.
6. Every flow gates on `activepieces.<flow_name>.enabled`; bumps go
   through `workspace.feature_flag_history`.
7. Per-flow + per-step verifiers green.
8. Phase 2 → Phase 3 handoff written.

Phase 2 does **not** ship: replacing the existing `bronze_public_geoscience`
Dagster asset wholesale (it stays on Dagster); Slack notifications
(deferred to Phase 3); user-buildable flows (operators authoring new
pieces — Phase 3+); SSO (Phase 3+).

---

## 3. In-scope vs out-of-scope, by area

| Area | In Phase 2 | Out (Phase 3+) |
|------|-----------|----------------|
| Activepieces service | docker compose, healthcheck, profile gating | HA / multi-replica |
| Auth | isolated admin login | Sanctum/OAuth SSO |
| Postgres | new logical DB on shared server | dedicated instance |
| Flow #1 — scheduled import | one upstream feed → S3 → Hatchet | full BCGS/NRCan migration off Dagster |
| Flow #2 (TBD pre-Step 5) | one more flow per scope ceiling | rest of the integration estate |
| Admin UI | `/admin/integrations` (read-only list + run rollup) | flow editor embed; flow CRUD |
| Observability | OTel spans + 1 Grafana panel | per-piece metric drilldowns |
| Feature flag gating | reuse Phase 1 sidecar | per-piece role-based gating |
| Audit | reuse `audit.audit_ledger` for ops actions | flow-edit lineage |
| Cutover | feature-flag ramp 0 → on | shadow harness (greenfield, none needed) |

---

## 4. Step-by-step

Each step has its own verify/smoke shape, mirroring Phase 1 (Step 4
verifier, Step 5B smoke, etc.). Per-step done-definitions are written
before code; verifiers are written alongside.

### Step 1 — Postgres role + logical DB
- New role: `activepieces` (LOGIN, NOSUPERUSER, NOBYPASSRLS, password
  from `.env`). Mirrors the `georag_app` and `hatchet` patterns.
- New database: `activepieces` (owner `activepieces`).
- Migration file: `database/raw/phase2/10-activepieces-role-and-db.sql`.
- Verifier: connection works as `activepieces`; role properties match
  the Phase 1 pattern; no superuser inheritance.

**Deliverable:** `scripts/phase2_step1_verify.sh`.

### Step 2 — Activepieces docker service
- New service in `docker-compose.yml` under `dev-data` + `dev-full`
  profiles. Image: `activepieces/activepieces:latest` (pin a specific
  tag — TBD when implementing).
- Wired to: the new `activepieces` logical DB; Redis (Activepieces uses
  it for queues); persistent volume for the file store.
- Healthcheck: HTTP 200 from `/api/v1/flags`.
- Port: `${ACTIVEPIECES_PORT:-8090}` (avoiding clash with Hatchet 8889
  + FastAPI 8000 + Octane 80).
- Verifier: container starts + healthchecks pass; admin UI reachable
  on the configured port; first-time admin user created via env var.

**Deliverable:** `scripts/phase2_step2_verify.sh`.

### Step 3 — `phase2_internal_trigger` FastAPI route + service-key
- Activepieces flows call into our stack via FastAPI's existing
  service-key gate (matching the Phase 1 `/internal/v1/shadow/...`
  pattern).
- New route: `POST /internal/v1/integrations/{flow_name}/trigger` —
  validates payload, dispatches a Hatchet workflow, returns
  `workflow_run_id`.
- Activepieces holds the service key as a connection secret; flows use
  the HTTP piece + the connection.
- Verifier: route returns 401 without key; returns 202 with workflow_run_id
  for a known flow_name; rejects unknown flow_name with 404.

**Deliverable:** `scripts/phase2_step3_verify.sh`.

### Step 4 — Flow #1 — scheduled public-geoscience import (small slice)
- Pick **one** upstream feed (smallest realistic — TBD when
  implementing; recommend a single ArcGIS FeatureService layer that's
  already in the Phase 0 manifest).
- Activepieces flow: cron → HTTP GET upstream → drop the GeoJSON in
  bronze under a versioned key → POST to
  `/internal/v1/integrations/public_geoscience_pull/trigger` with the
  S3 key.
- Hatchet side: a thin `public_geoscience_pull` workflow that registers
  the bronze object so the existing `bronze_public_geoscience`
  Dagster asset can pick it up (or — if simpler — directly lifts it
  into bronze without the Dagster handoff for the smoke).
- Behind `activepieces.public_geoscience_pull.enabled` flag (default
  false; flip on after manual review).
- Verifier: flow runs once on demand → S3 object lands → Hatchet
  workflow fires → bronze row written within 60s.

**Deliverable:** `scripts/phase2_step4_verify.sh` + smoke.

### Step 5 — Flow #2 (per scope ceiling)
- One additional flow exercising a different shape. Strong candidates
  (pick during Step 4 retrospective):
  - **5a** — webhook *receiver* (external sender posts → Activepieces
    routes → Hatchet ingest). Tests inbound traffic vs Step 4's
    outbound pull.
  - **5b** — second scheduled feed (different upstream, validates the
    pattern reuses cleanly).
- Same feature-flag + verifier pattern as Step 4.

**Deliverable:** `scripts/phase2_step5_verify.sh` + smoke.

### Step 6 — `/admin/integrations` Inertia dashboard
- Mirrors `/admin/hatchet-workers` shape:
  - Registered flows (read from Activepieces' Postgres DB through a new
    `pgsql_activepieces` Laravel connection).
  - Enabled state (read from `workspace.feature_flags`).
  - Last 24h run rollup (succeeded/failed/running, p50/p95 duration).
  - Recent failures table with upstream status code.
- Bonus: link out to the Activepieces native UI for flow drilldown.
- Same admin Gate as the other Phase 1 dashboards.
- Verifier: route registered; controller class loads; `pgsql_activepieces`
  connection reachable; runs query returns shape.

**Deliverable:** `scripts/phase2_step6_verify.sh`.

### Step 7 — OTel + Grafana
- Activepieces emits flow-run events via either webhook → OTel
  collector or piece-level instrumentation. Whichever the upstream
  supports cleanest in the chosen image version.
- Grafana panel: `Activepieces flow runs` showing succeeded vs failed
  vs duration p95, grouped by flow_name.
- Provisioned via the existing `docker/grafana/provisioning/dashboards/`
  shape.
- Verifier: at least one span ingested with the right tags after a Step
  4 smoke run.

**Deliverable:** `scripts/phase2_step7_verify.sh`.

### Step 8 — Phase 2 → Phase 3 handoff
- `docs/phase2_handoff.md` — what shipped, exit-state architecture diff
  vs Phase 1, carry-overs, files of record. Same shape as
  `docs/phase1_handoff.md`.

**Deliverable:** the doc.

---

## 5. Engineering invariants for Phase 2

These ride along with every step:

1. **Don't reimplement — bridge.** The existing `bronze_public_geoscience`
   Dagster asset stays. Activepieces becomes the *trigger* for it (or
   a thin shim around it), not a replacement.
2. **One flow at a time, behind a flag.** Every Activepieces flow
   gates on `activepieces.<flow_name>.enabled`. Default false. Bumps
   are auditable via R-P1-6.
3. **No new persistence patterns.** Reuse `audit.audit_ledger`,
   `workspace.feature_flags`, `workflow.workflow_runs`. Activepieces'
   own internal storage stays in its own Postgres DB; we don't ETL it
   into ours.
4. **Service-key for inbound from Activepieces.** Reuse FastAPI's
   `X-Service-Key` gate; no new auth surface in this phase.
5. **OTel-only observability.** No Activepieces-specific Prometheus
   exporter — flows must surface through the OTel collector like
   everything else.
6. **Octane-safe.** New `pgsql_activepieces` connection follows the
   same pattern as `pgsql_hatchet` (resolver closure, no static state
   leak between requests).

---

## 6. Risks + mitigations

| Risk | Mitigation |
|------|-----------|
| Activepieces' upstream API surface drifts (image tags) | Pin to a specific version in compose; doc the upgrade procedure in the Phase 2 handoff. |
| Flow-state lives outside our Postgres backup window | Step 1 verifier asserts the `activepieces` DB is in the existing pg_dump scope. |
| Flow logic gets edited via Activepieces UI without code review | Phase 2 keeps the admin UI **read-only** for the first cut; flow definitions are managed by code (export → commit) until Phase 3 builds an editor-aware review flow. |
| Self-hosted Activepieces auth differs from internal SSO expectations | Documented as Phase 3+ (D3a in the scope proposal). Operators get a separate login during Phase 2; that's the explicit trade-off. |
| Step 4's "smallest realistic feed" choice is guesswork | Make the upstream-feed pick a Step 4 sub-task. Pick it concretely after reading the Phase 0 manifest, not in this kickoff. |

---

## 7. Files of record (preview)

```
database/raw/phase2/10-activepieces-role-and-db.sql       (Step 1)
docker-compose.yml                                         (mod — Step 2)
src/fastapi/app/routers/integrations_trigger.py            (Step 3)
src/fastapi/app/main.py                                    (mod — Step 3)
src/fastapi/app/hatchet_workflows/public_geoscience_pull.py (Step 4)
src/fastapi/app/hatchet_workflows/worker.py                (mod — Step 4)
app/Http/Controllers/Admin/IntegrationsController.php      (Step 6)
config/database.php                                        (mod — Step 6)
resources/js/Pages/Admin/Integrations.tsx                  (Step 6)
routes/web.php                                             (mod — Step 6)
docker/grafana/provisioning/dashboards/activepieces.json   (Step 7)
docs/phase2_handoff.md                                     (Step 8)
scripts/phase2_step{1..7}_verify.sh                        (each step)
scripts/phase2_step{4,5}_smoke.sh                          (per-flow)
```

Activepieces' own flow definitions live in its DB; their export-to-code
+ review flow is Phase 3. Until then we treat Activepieces' UI as
authoritative and have engineers re-create flows in fresh environments
from a one-pager runbook.

---

## 8. Sign-offs + start order

When Kyle says **"go"**:

1. Step 1 first (role + DB — fully reversible).
2. Step 2 next (service comes up — also reversible by removing the
   profile entry).
3. Step 3 unblocks Step 4 (the trigger route is what the flow calls
   into).
4. Step 4 is the first end-to-end. Demo before opening Step 5.
5. Steps 5–7 are mostly parallel-safe; do them in the order matching
   the team's bandwidth.
6. Step 8 closes Phase 2.

End of Phase 2 implementation kickoff.
