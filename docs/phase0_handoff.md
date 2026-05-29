# Phase 0 — Handoff to Phase 1

**Status:** Substantively done. Master acceptance at **15 / 16 checks passing** as of 2026-05-09; the single remaining gap is the `/admin/agent-config/*` Inertia surfaces, which are landing in a parallel worktree and do not block Phase 1 work.

**Final close-out (2026-05-09 evening):**
- `aioboto3` + `boto3` added to FastAPI image, container rebuilt
- `phase0_ops.py` router merged into WSL `main.py` (surgically — preserves WSL-only `outlier_assist` and `pdf` routers)
- All 10 Python Phase 0 agents now invoke + emit audit + write expected output rows: Step 6 verifier 10/10
- Storage Tiering + Support Packet hit a real SeaweedFS S3 `SignatureDoesNotMatch` (R-P0-9) but tolerate gracefully — agents record the failure and continue rather than crash

**Audience:** Phase 1 implementer (Hatchet adoption + first v1.49 → Hatchet workflow migration).

**How to read this:** §1–2 give the headline state. §3 lists what Phase 1 should expect to find already working. §4–5 are the deferral and risks register Phase 1 inherits.

---

## 1. Acceptance result

```
Aggregate: 15 / 16 acceptance checks passed

A) Per-step verifiers
  [PASS] Step 1 (7/7 checks)   — infrastructure foundation
  [PASS] Step 2 (8/8 checks)   — schema deployment + RLS + hash chain
  [PASS] Step 3 (6/6 checks)   — observability foundation + dashboard
  [PASS] Step 4 (7/7 checks)   — audit ledger + outbox + Hatchet workflows
  [FAIL] Step 5 (5/9 checks)   — wrapper green; admin UI surfaces still in-flight
  [PASS] Step 6 (4/4 checks)   — 4 stewardship agents + GPU/VRAM rules

B) Master plan §30 end-to-end
  [PASS] phase0_acceptance_e2e.sh ran (synthetic workflow_run + span tree)
  [PASS] workflow.workflow_runs has the row (status=success)
  [PASS] Tempo serves the trace (full span tree)
  [PASS] audit_ledger has the trace_id-tagged rows
  [PASS] Fresh audit verification over live window is clean

C) Cross-cutting
  [PASS] All 4 implemented agents invoked successfully (live)
  [PASS] All 4 agents emitted agent.invoke.* audit rows
  [PASS] pg_partman tracking 3 partitioned parents
  [PASS] Hatchet /api/ready → 200
  [PASS] OTel collector /health → 200
```

The single failing check is Step 5 sub-deliverable §5.2 — the `/admin/agent-config/*` surfaces. Routes return HTTP 404 because the controllers haven't merged yet. The wrapper, defaults, smoke, and verify-script harness are all complete and the surfaces will land via the active spawned task.

---

## 2. What Phase 0 actually shipped

### Infrastructure (Step 1)
- **PostgreSQL 18.3** custom image (`georag/postgres:18-ext`) built from `postgis/postgis:18-3.6-alpine`. Adds 7 extensions: `auto_explain`, `h3` (h3-pg + h3_postgis), `hypopg`, `pg_stat_kcache`, `pg_partman`, `pg_repack`, `pg_ivm`. Bitcode emission disabled in the build (`with_llvm=no`) since JIT is off at runtime.
- **SeaweedFS** (already deployed pre-Phase 0 per ADR-0001) with three logical tier buckets: `tier-hot`, `tier-warm`, `tier-cold`.
- **Hatchet Lite** (`ghcr.io/hatchet-dev/hatchet/hatchet-lite:latest`) — single-image Postgres-backed deployment. Web/API on host port 8889, gRPC on 7077.
- vLLM, Redis, PgBouncer, MinIO-named SeaweedFS, and the rest of the existing stack untouched.

### Schemas (Step 2)
- 21 net-new tables across 8 namespaces (`audit`, `usage`, `silver`, `gold`, `public_geoscience`, `outbox`, `workflow`, `workspace`).
- Existing `silver.workspaces` and `public.users` left in place per Phase 0 decision #5 (not moved into the `workspace` schema).
- pg_partman parent + 3 monthly partitions for `audit.audit_ledger`, `workflow.workflow_runs`, `usage.usage_events`.
- RLS + `tenant_isolation` policy on the 16 workspace-scoped tables.
- Hash-chain trigger (`audit.compute_audit_hash`) + pure-SQL verifier (`audit.verify_hash_chain`, `audit.run_verification`) — externally re-runnable.

### Observability (Step 3)
- OpenTelemetry collector (`otel/opentelemetry-collector-contrib:0.151.0`) on host ports 4317/4318/13133.
- Tempo (`grafana/tempo:2.6.1`) on host port 3200, single-process local-disk blocks, 7-day retention.
- Prometheus (already in compose) — moved to `dev-monitor` profile + activated. Two new scrape jobs: `vllm` and `otel-collector`.
- Langfuse (pre-existing) reachable.
- Workflow Run Dashboard skeleton at `/admin/workflow-runs` (Laravel + Inertia + React).

### Audit + outbox (Step 4)
- Python audit emitter (`app.audit.emit_audit`, async, transaction-aware, asyncpg).
- PHP audit emitter (`App\Services\Audit\AuditEmitter`).
- Hash recipe documented at `docs/audit_ledger_hash_recipe.md` (canonical recipe + auditor playbook + reference Python implementation).
- Hatchet workflows registered with the engine: `audit_ledger_verify` (cron `0 2 * * *` UTC) and `outbox_dispatcher` (every minute, dispatches to Qdrant / Neo4j / SeaweedFS, dead-letters after 3 transient failures).

### Operational contract wrapper (Step 5)
- `@georag_agent(name, risk_tier, version)` Python decorator at `src/fastapi/app/agents/`. Implements all 12 contract steps (timeout, circuit breaker, idempotency, dry-run, audit emit, usage event). R0 wrapped overhead measured at ~5 ms (audit-insert dominated).
- `App\Services\Agents\AgentInvoker` PHP equivalent for Laravel-side agents.
- Default `agent_timeouts` rows for all 11 Phase 0 agents seeded.
- Two `agent_prompt_pins` rows for the LLM-callers (LLM Incident Diagnosis, Support Packet).

### Phase 0 agents (Step 6)
- 10 of 11 agents implemented as Python modules at `src/fastapi/app/agents/phase0/`. The 11th (GPU/VRAM Health) is implemented as Prometheus alerting rules at `docker/prometheus/rules/gpu-vram-health.yml` per kickoff §Step 7 Finding 3.
- The 4 stewardship agents (Tenant Isolation, Lineage Reporter, Index Health, Store Reconciliation) are tested clean end-to-end. The 6 newer agents (Storage Tiering, Model Upgrade Watch, vLLM Security Check, Model Cost Summary, LLM Incident Diagnosis, Support Packet) landed in the parallel worktree and have rough edges noted in §4.

---

## 3. What Phase 1 inherits

Phase 1 (Hatchet adoption + first v1.49 → Hatchet migration) should expect to find the following already working:

- **Database schema:** all Phase 0 tables exist. Cross-schema FKs to `silver.workspaces(workspace_id)` and `public.users(id)` are the canonical pattern (decision #5). `public_geoscience` is the canonical name (decision #2).
- **Audit ledger:** every state-changing event in Phase 1 should call `app.audit.emit_audit()` (Python) or `App\Services\Audit\AuditEmitter::emit()` (PHP). The hash chain is enforced at the DB layer; callers don't compute hashes.
- **Outbox:** Phase 1's first migrated workflow (the `ingest_pdf` shadowing of v1.49) writes to `outbox.pending_propagations` in the same Postgres transaction as the source-of-truth row. The `outbox_dispatcher` Hatchet workflow handles propagation to Qdrant / Neo4j / SeaweedFS.
- **Operational contract:** any new agents in Phase 1 wrap with `@georag_agent` (or `AgentInvoker::invoke`). Defaults are seeded; Phase 1 should add `agent_timeouts` rows for new agents.
- **Observability:** new services should send OTLP to `otel-collector:4317` (gRPC) or `:4318` (HTTP). The trace_id propagates to `workflow_runs.trace_id` and Langfuse trace metadata.
- **Hatchet:** workflows are registered via the Python SDK from inside `georag-hatchet-worker`. The cron expressions use Hatchet's V1 engine syntax.
- **Index Health Agent + Store Reconciliation Agent** are in place at the Postgres level. Phase 1 extends them with the Qdrant + Neo4j checks once those stores accumulate data.

### What Phase 0 explicitly does NOT ship for Phase 1

- Customer-facing UI (Phase 4+).
- Any answer-graph cognition (Phase 4).
- Document ingestion beyond test fixtures (Phase 3).
- Reporting (Phase 7).
- The proactive-intelligence agents added in registry v1.2/v1.3 (Phases 4–7).

---

## 4. Deferred work (handed off, NOT done)

These items were touched in Phase 0 spawned tasks but did not fully close before sign-off. Phase 1 should pick them up in priority order:

1. **`/admin/agent-config/*` Inertia surfaces** (`/timeouts`, `/prompts`, `/pins`, `/workspaces`). Wrapper, schemas, defaults, audit hooks, and the verify harness are all in place; only the four Laravel controllers + Inertia React pages + PHPUnit feature tests remain. A spawned worktree is in flight. Closes Step 5 from 5/9 → 9/9.

2. **Storage Tiering Agent: SeaweedFS S3 IAM scope quirk.** The Storage Tiering agent code is in place but the SeaweedFS S3 IAM config rejects `mc mb` / `mc ls` calls under the unscoped `Admin / Read / Write / List` actions. The agent picks up the rules but cannot list source-tier buckets without an IAM fix. Workaround: use the `weed shell s3.bucket.*` admin API. Real fix: add wildcard scopes (`Admin:*`, `Read:*`, etc.) to `docker/seaweedfs/entrypoint.sh`'s rendered `s3.json`.

3. **`aioboto3` dependency missing** in the FastAPI container. Required by Storage Tiering Agent and Support Packet Agent for SeaweedFS S3 PUT/GET. Add to `src/fastapi/pyproject.toml` and rebuild the image.

4. **Support Packet Agent end-to-end smoke** is failing on the missing `aioboto3` (above). Once that lands, the agent + the `silver.support_packets` table (already migrated) are ready.

5. **LLM Incident Diagnosis Agent: refusal path is data-dependent** — returns `success` when ambient trace data is rich enough for the LLM to construct a plausible diagnosis, even on workspaces with no real incidents. Step 6 smoke alternates between `success` and `refusal` depending on how much Langfuse data exists at run time. Phase 1 Step 4 ingest_pdf runs add trace data which tips it toward `success`. Real fix: tighten the prompt's refusal threshold or pre-filter ambient traces to only those matching an alert label.

6. **Hatchet workflow scheduling for the Phase 0 agents.** The workflows for `audit_ledger_verify` and `outbox_dispatcher` are registered. The 10 Phase 0 agent functions exist as `@georag_agent`-decorated Python callables but are NOT yet wired into Hatchet step functions with cron schedules. Phase 0 invokes them via `phase0_step6_verify.sh` and the master acceptance; production scheduling lives in a single follow-up task: write one Hatchet workflow per agent, mirror the kickoff §Step 6 cron times.

---

## 5. Risks register additions

These items were surfaced during Phase 0 build that warrant tracking against §32 in the master plan:

| Risk | Description | Where it bites later |
|---|---|---|
| **R-P0-1** | Hash chain uses `payload::text` (Postgres jsonb canonical), not RFC-8785 JCS. Reproducible per-DB but won't satisfy external auditors who require strict JCS. | Phase 11 hardening — `recipe_version` column + JCS implementation. |
| **R-P0-2** | `audit_ledger` lacks an UPDATE/DELETE constraint trigger. A privileged user could mutate rows; the verifier would catch it but only at the next nightly run. | Phase 11 — add constraint trigger + revoke the application role's UPDATE/DELETE grants. |
| **R-P0-3** | SeaweedFS three-tier is *logical* (named buckets on a single physical volume) in dev. Production requires distinct mounts with `-disk=ssd,hdd,archive`. | Phase 11 — multi-mount SeaweedFS deploy. |
| **R-P0-4** | SeaweedFS S3 IAM scope quirk (see deferred §4.2). Storage Tiering Agent + Support Packet Agent are blocked on the dev-stack fix. | Phase 1 unblocker. |
| **R-P0-5** | OTel collector self-telemetry endpoint (`metrics.address`) deprecated in v0.150+; we removed the explicit binding. Self-metrics still exist via the readers-based config but aren't currently scraped. | Phase 11 — wire the readers-based self-telemetry. |
| **R-P0-6** | `audit_ledger.created_at` defaults to `clock_timestamp()` not `now()` to keep the chain order correct under same-transaction batched writes. Any future code that overrides the default explicitly with `now()` will silently break the chain order tiebreaker. | Phase 11 — add a CHECK constraint or trigger guard. |
| **R-P0-7** | `pgcrypto` had to be moved from `silver` (where the database-level search_path put it) to `public`. Fresh DBs install it in `public` via `WITH SCHEMA public`. Existing dev databases that ran the older init may need a manual `ALTER EXTENSION pgcrypto SET SCHEMA public`. | Operator-facing upgrade note. |
| **R-P0-8** | `audit.verify_hash_chain(start, end)` produces false-breaks on the first row in the window when that row's stored `previous_hash` references a row outside the window. Windowed `LAG()` returns NULL but the stored hash is non-NULL. Workaround: verify per-workspace from start of chain (the master acceptance does this directly). | Phase 11 — extend verifier to look up the out-of-window predecessor for the first row in each workspace partition. |
| **R-P0-9** | ~~SeaweedFS S3 returns `SignatureDoesNotMatch` on `aioboto3`...~~ **RESOLVED 2026-05-09 evening.** Root cause was that the `georag-fastapi` container was missing `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD` / `AWS_*` env vars; the storage_tiering / support_packet agents fell back to an empty secret which produced empty SigV4 signatures. Fix: added the seven S3 env vars to the fastapi service block in `docker-compose.yml`. Storage Tiering errors=0 and Support Packet upload_ok=True after the fix. | Closed. |
| **R-P0-10** | ~~The `georag` PostgreSQL role is a SUPERUSER with `rolbypassrls=true`...~~ **FULLY RESOLVED 2026-05-09 evening (Phase 1 Step 1A + 1B).** New `georag_app` role created (NOSUPERUSER, NOBYPASSRLS, LOGIN). Schema USAGE + SELECT/INSERT/UPDATE/DELETE on every Phase 0 + Laravel-managed schema, EXECUTE on `audit.*` functions, ALTER DEFAULT PRIVILEGES for future migrations. **fastapi + laravel-octane + laravel-horizon + laravel-reverb all swapped** to `${GEORAG_APP_USER}`/`${GEORAG_APP_PASSWORD}`. Verified: Laravel `tinker` reports `current_user = georag_app, NOSUPERUSER`. PgBouncer auth_user resolves the new role dynamically — no userlist update needed. Tenant Isolation Auditor: violations 4 → 0. Migration: `database/raw/phase1/10-georag-app-role.sql`. | Closed for app services. Hatchet workers + ofelia + backup-agent + postgres_exporter intentionally STAY on `georag` superuser (need partition maintenance, cross-workspace outbox dispatch, admin introspection respectively) — documented as Phase 1 Step 2 follow-up. |

---

## 6. Spec deviations to fold into a v2.4.3 doc revision

A consolidated list captured during build (master plan + kickoff drift):

1. `public_geo` namespace is canonically `public_geoscience` (Phase 0 decision #2).
2. `silver.workspaces` + `public.users` are the canonical Layer A tables, not `workspace.workspaces` / `workspace.users` (Phase 0 decision #5).
3. SeaweedFS three-tier is logical for dev, physical for prod (deferred to Phase 11).
4. vLLM Prometheus metric prefix is `vllm:` (with colon), not `vllm_` as kickoff Step 1 grep pattern expects.
5. Hatchet readiness endpoint is `/api/ready`, not `/v1/healthz`.
6. SeaweedFS S3 IAM action scope quirk — surface a recommended `s3.json` template for fresh installs.
7. Workflow Run Dashboard split into a sub-task; not folded into Step 3 verify.
8. OTel collector Prometheus-exporter port internal-only (`:8895`), no host mapping.
9. `audit.audit_ledger_verification_runs` has no `workspace_id` — kickoff Step 2 RLS list incorrectly includes it.
10. Hash recipe uses `payload::text`, not RFC-8785 JCS (documented in `audit_ledger_hash_recipe.md`).
11. Phase 0 net-new table count is **21**, not 24 (3 of the kickoff's listed Layer A tables already existed).
12. `audit_ledger.created_at` defaults to `clock_timestamp()` not `now()` — required for in-tx chain ordering.
13. `pgcrypto` lives in `public` (not the database-level search_path's first schema).
14. R0 wrapper overhead is ~5 ms not <5 ms (audit-insert dominated). Revise spec target to `<10 ms`.
15. Wrapper returns `AgentResult(outcome='circuit_open')` instead of raising `AgentCircuitOpenError`. Cleaner contract — caller always gets a result.
16. `SET LOCAL <guc> = $param` is invalid SQL — must use `set_config(name, value, is_local)`. Documented for future agent authors.
17. GPU/VRAM Health Agent ships as Prometheus alerting rules per kickoff §Step 7 Finding 3 — confirmed.
18. `audit.verify_hash_chain` has a windowed-LAG false-break edge case on the first row per workspace partition. Master acceptance works around it with a direct full-chain SQL query.
19. ~~Storage Tiering Agent + Support Packet Agent's S3 calls fail...~~ Resolved 2026-05-09: root cause was missing `AWS_*` env on the fastapi container, not a SeaweedFS issue. See R-P0-9 closure note.
20. **The application role bypasses RLS** (R-P0-10). Phase 0 RLS policies are silently ineffective. Phase 1 must split the role before any production-like multi-workspace data lands.

---

## 7. Operator runbook (Phase 0 dev stack)

Quick commands for anyone picking up this dev workstation:

```bash
# Bring the full Phase 0 stack up.
cd /home/georag/projects/georag
docker compose --profile dev-data --profile dev-monitor up -d

# Re-apply Phase 0 schema (idempotent — safe to re-run).
bash scripts/phase0_step2_apply.sh

# Run all per-step verifiers.
bash scripts/phase0_run_all_verifies.sh

# Run the master acceptance (per-step + §30 e2e + cross-cutting).
bash scripts/phase0_acceptance.sh

# Re-trigger the §30 e2e in isolation; prints trace_id on the last line.
TRACE_ID=$(bash scripts/phase0_acceptance_e2e.sh | tail -1)
echo "Tempo: http://localhost:3200/api/traces/$TRACE_ID"

# Ad-hoc audit chain verification.
docker exec georag-postgresql psql -U georag -d georag -c "
  SELECT audit.run_verification(now() - interval '1 day', now());"

# Sync Windows authoring tree → WSL runtime tree.
bash ops/setup/sync_windows_to_wsl.sh
```

---

## 8. Sign-off

Phase 0 is signed off as substantively complete on **2026-05-09** by the implementing engineer. The handoff is to Phase 1 (Hatchet adoption); the deferred items in §4 are scheduled to land within Phase 1's first sprint and do not block Phase 1's `ingest_pdf` migration prep.

Phase 1 should run `bash scripts/phase0_acceptance.sh` as its first action and confirm 16/16 (post the admin UI landing) before adding any new components.

End of Phase 0 handoff.
