# Hatchet review — 2026-08-21

Scope: the Hatchet engine, the worker, all 52 registered workflows, what
dispatches them, and what they touch. Every claim is backed by live Azure
state or Log Analytics over the retained window (2026-08-03 → 2026-08-21),
not by reading the code alone.

Environment at time of review:

- engine `hatchet-cc` — `hatchet-lite:v0.89.7`, TCP ingress 7077 (+8888),
  1 replica, `DATABASE_URL` → the `hatchet` database on **`georag-pg-cc`**
- worker `hatchet-worker-cc` — image `georag/fastapi:47d77b1`,
  `WORKER_POOL=all`, 4 vCPU / 8 GiB, **maxReplicas 1**, 20 slots,
  **no liveness or readiness probe**
- 52 workflows registered (13 ingestion + 39 ai, merged into `all`)

Method note: "did it run" is `run: start step:` in the worker log; "did it
succeed" is a matching `finished step run:`. Counting only starts —
which is what most of the earlier passes did — makes a workflow that fails
every night look like a workflow that runs every night.

---

## The census

31 of the 52 registered workflows executed at least once in the retained
window. Two more executed that **no longer exist in the tree**
(`backup_neo4j`, last run 08-05; `vllm_security_check_run`, last run 08-15)
— the engine still holds their cron rows.

Runs 2026-08-03 → 2026-08-21, expected vs started vs finished:

| workflow | cron | expected | started | finished |
|---|---|---|---|---|
| outbox_dispatcher | `* * * * *` | ~25,900 | 20,367 | ~20,300 |
| reliability_metrics_publisher | `* * * * *` | ~25,900 | 20,239 | ~20,200 |
| cost_burn_watcher | `*/5` | 5,184 | 4,051 | 4,029 |
| stale_run_detector | `*/15` | 1,728 | 1,387 | ok |
| qdrant_payload_audit | `0 * * * *` | 432 | 341 | 341 |
| cold_tier_archive | `0 4` | 18 | 13 | 13 |
| pg_partman_maintenance | `15 4` | 18 | 14 | 12 |
| idempotency_keys_cleanup | `15 4` | 18 | 13 | 12 |
| **audit_ledger_verify** | `0 2` | 18 | 10 | **0** |
| **backup_postgres** | `0 2` | 18 | 10 | **0** |
| **repair_shadow_aggregate** | `15 2` | 18 | 11 | **0** |
| **retention_sweep** | `45 4` | 18 | 5 | **0** |
| **embed_pending_passages** | `*/10` + `45 5` | 2,610 | **93** | — |
| **enrich_passage_context** | `30 4` | 18 | **0** | — |
| **verbalize_page_images** | `20 * * * *` | ~60 | **0** | — |

Two separate problems are visible here and they have nothing to do with
each other:

- workflows that **start and then fail 100% of the time** (Finding 1)
- workflows whose **cron never fires at all** (Finding 2)

Attendance across all healthy crons sits at **78–79% of expected**; that
shortfall is the nightly database shutdown (Finding 3).

---

## Finding 1 — four nightly workflows failed every night for a month — RESOLVED, verified 2026-08-21

**Status: already fixed and confirmed on the live database.** The fixes
shipped in `47d77b1` at 2026-08-20 22:12 UTC, several hours after the last
log line this review drew on, so the review's original "still live" reading
was wrong. Verified directly against `georag-pg-cc` on 2026-08-21 as
`georag_app`:

| check | result |
|---|---|
| `audit_ledger_verification_runs.workflow_run_id` exists | yes |
| status CHECK admits `in_progress` | yes — the CheckViolation trap behind it is closed too |
| `audit.run_verification` / `verify_hash_chain` / `recompute_hash` | all present, `EXECUTE` granted |
| `has_database_privilege(georag_app,'georag','CONNECT'/'TEMP')` | true / true |
| `silver.ingest_progress` SELECT / DELETE | true / true |
| `gold.mv_refresh_log_id_seq` USAGE / SELECT | true / true |
| `silver.mv_collar_summary` SELECT / **MAINTAIN** | true / true |

All four are declared in the migration chain, not patched out of band, so a
fresh cluster gets them too: `2026_08_20_030000` (table shape + the three
verification functions), `2026_08_19_060000` (declares
`gold.repair_shadow_daily`, so `repair_shadow_aggregate` no longer needs
`CREATE` on the database at runtime), `2026_08_19_050000` (per-object table
and sequence grants) and `2026_08_20_040000` (matview SELECT + MAINTAIN —
`REFRESH` is gated on MAINTAIN in PG17+, which SELECT alone would not have
fixed).

One latent defect in this family was still open and is fixed in the working
tree: `2026_08_20_030000` ran `COMMENT ON COLUMN ...error_text` outside the
`columnExists('error_text')` guard. On any cluster built from the canonical
definition — which has `error_message` and no `error_text` — that throws
SQLSTATE 42703 and takes the migration down, and since CD runs migrations
before images, a failure there ships no code at all. Azure survived it only
because Azure happened to have the mirror column. Both `error_text`
statements now sit behind the one guard.

The original finding follows, since the diagnosis is what matters for next
time. It corrected an earlier read that blamed these on the Postgres
shutdown window: on 2026-08-20 the database was up at 02:00 —
`stale_run_detector` and `cost_burn_watcher` both completed in the same
second — and these still failed. Exceptions captured live:

| workflow | schedule | exception |
|---|---|---|
| `audit_ledger_verify` | 02:00 | `UndefinedColumnError: column "workflow_run_id" of relation "audit_ledger_verification_runs" does not exist` |
| `repair_shadow_aggregate` | 02:15 | `InsufficientPrivilegeError: permission denied for database georag` |
| `retention_sweep` | 04:45 | `InsufficientPrivilegeError: permission denied for table ingest_progress` |
| `nightly_ingestion_integrity` tier 3 | 02:00, 04:00 | `permission denied for sequence mv_refresh_log_id_seq` |

All four are the schema-and-grant drift already recorded in
`project_raw_sql_layer_never_applied_to_azure` and
`project_azure_grant_gaps_2026_08_20` — they land here because Hatchet is
where the damage shows up. Latest occurrence of each is 2026-08-20 02:00–04:45,
the last nightly window before the fixes deployed at 22:12.

The consequences were specific, and the backlog they left is still real even
though the causes are fixed:

- **`retention_sweep` had never once succeeded.** Its whole job is purging
  `audit.query_audit_log` (a row per RAG query) and
  `silver.ingest_progress` (a row per ingest attempt), both documented in
  its own docstring as growing unbounded. The 180d/90d retention defaults
  have never been applied, so **the first successful run has a month or more
  of accumulated rows to delete** — worth watching that it finishes inside
  its 55-minute `execution_timeout` rather than assuming it will.
- **`audit_ledger_verify` had never once succeeded** since 08-05, so the
  audit hash chain still has no verification record for that window. It
  verifies a 24 h window per run, so the gap does not backfill itself.
- `repair_shadow_aggregate` needed `CREATE` on the database only because it
  created `gold.repair_shadow_daily` at runtime; declaring the table in
  `2026_08_19_060000` removed the requirement instead of widening the grant.

Two that *were* on this list have since recovered and should not be
re-reported as broken: `mv_refresh_silver` and `flow_jwt_key_reaper` both
completed cleanly on 2026-08-20. Their poor 30-day ratios are historical,
from the pre-08-03 era when `outbox.pending_propagations` and friends did
not exist on Azure at all (1,014 `UndefinedTableError` hits, all before
08-03).

---

## Finding 2 — three cron workflows have never fired, and the cause is one CEL expression — FIXED in the working tree

`embed_pending_passages`, `enrich_passage_context` and
`verbalize_page_images` are the only three cron workflows that declare
`concurrency=ConcurrencyExpression(...)`. They are also the only three
whose crons produce no runs.

The minute-of-hour histogram for `embed_pending_passages` is flat and
scattered — not clustered on `:00/:10/:20/…` as a `*/10` cron would be.
Its 93 runs are all inline dispatches from `ingest_pdf.persist`
(`ingest_pdf.py:1826`, `:1997`). **The cron itself has never fired.**

Cause, confirmed against the v0.89 engine source rather than inferred:

1. A declarative `on_crons` trigger sends **no input at all**. The Python
   SDK hardcodes `cron_input=None` (`hatchet_sdk/runnables/workflow.py:257`),
   so `input` is `{}` at the engine.
2. The engine evaluates the CEL concurrency key against that raw payload,
   *before* Pydantic applies field defaults on the worker. `input` is
   declared as `map(string, dyn)` (`internal/cel/cel.go:54`), so
   `input.workspace_id` is a select on a missing map key and cel-go raises
   `no such key: workspace_id`.
3. The engine does **not** queue or retry that. `pkg/repository/task.go:2103`
   turns a concurrency-expression error into an initial task state of
   `FAILED`, with the concurrency key recorded literally as `"FAILED"`.

So each tick produced a run that was born failed, before any worker was
involved — which is exactly why the worker log had nothing to find, and why
this looked like "the cron isn't registered". (My first pass said the run
was "never scheduled". It is scheduled; it is failed on arrival. Same
observable, different fix.)

The `'cron'` fallback added on 2026-06-01 does not help:

```python
# embed_pending_passages.py:120, verbalize_page_images.py:71
expression="input.workspace_id != '' ? input.workspace_id : 'cron'"
```

It guards against the key being **empty**. The key is **absent**.
`enrich_passage_context.py:61` has no fallback at all
(`expression="input.workspace_id"`) *and* declares
`workspace_id: str = Field(...)` — required, no default — so its cron
payload could not have validated on the worker either.

**Fix applied.** The CEL `has()` macro is absence-safe, and Hatchet's own
test suite covers this exact shape against this exact environment
(`internal/cel/cel_test.go:40` asserts
`has(input.custom) ? input.custom.value : "default"` returns the default on
`input = {}`), so it compiles at registration. That last part matters:
`pkg/repository/workflow.go:69` validates the expression on `PutWorkflow`,
so an expression that fails to compile would fail the worker's whole
registration — all 52 workflows — rather than just this one.

```python
expression=(
    "has(input.workspace_id) && string(input.workspace_id) != '' "
    "? string(input.workspace_id) : 'cron'"
)
```

`string()` is belt-and-braces: `task.go:2108` fails the run outright if the
concurrency key is not a string, so coerce rather than fail.

Three further changes were needed to make the crons actually work, not just
get past the engine:

- **`workspace_id` is no longer a required Pydantic field** on
  `embed_pending_passages` and `enrich_passage_context`. Both declared it
  required, so once the CEL expression let the run through, it would have
  died on the worker with a ValidationError instead — the same outage one
  step later.

  The first attempt simply defaulted it to `""`, which broke two REC#1
  tenancy guards in `test_workspace_dependency.py` — correctly, because it
  moved enforcement out of the model and into the task body, turning a
  dispatcher's mistake from an immediate `ValidationError` at the call site
  into a workflow that starts and then fails. The requirement instead moved
  to a `model_validator` keyed on `project_id`:

  ```python
  if self.project_id != "*" and not self.workspace_id:
      raise ValueError(...)
  ```

  A dispatcher naming a specific project still cannot omit `workspace_id`
  and still fails at construction time, so REC#1's guarantee is intact. The
  only payload that may omit it is the `project_id="*"` fan-out — which
  resolves the workspace per project and is the only shape a cron can send.
  `verbalize_page_images` got the same validator: it had no such guard, and
  a workspace-less single-project call was accepted and then silently
  skipped by an `if not workspace_id: continue`, reporting success for a
  no-op.
- **The `"*"` fan-out resolves the workspace per project** in both
  workflows, instead of applying one `input.workspace_id` to every project.
  These sweeps span every workspace, and `embed_pending_passages(...)`
  binds RLS to the workspace it is handed — so the old code would have
  bound the wrong scope, seen no rows, and reported a cheerful zero. Latent
  only because the cron never fired; it would have shipped as a silent
  under-embed the moment it did. `verbalize_page_images` already did this
  correctly and was the model for both.
- **`embed_pending_passages`' Qdrant-drift recovery path** re-resolves the
  work list after wiping `embedding_id`; it now rebuilds the
  `(workspace, project)` pairs rather than the project list the loop no
  longer reads.

What this had cost:

- the every-10-minute **embed safety net** — the thing that catches
  passages whose inline embed dispatch lost the race with a Hatchet retry
  (the BattleNorth bug it was written for) — has never run once
- **contextual retrieval headers** (`contextualized_content`) are never
  generated, so `passage_embedder` has been embedding un-enriched text
- **page-image verbalization** has never produced a description, despite
  `IMAGE_VERBALIZATION_ENABLED=true` on the worker

Two tests passed throughout all of it, and both are worth understanding:
`test_embed_pending_passages_cron_schedule` asserts the cron string is
present in the declaration, and
`test_embed_pending_passages_has_per_workspace_singleton_concurrency`
asserted the expression **as an exact string literal** — so it locked the
broken expression in place and would have failed had anyone fixed it.

`tests/test_embed_pending_passages_concurrency.py` is rewritten to assert
the *property* instead, across all three workflows:

- every `input.X` in a cron workflow's concurrency expression sits behind a
  `has(input.X)`
- the input model validates `{}` — the actual cron payload — and yields
  `project_id="*"` with an empty `workspace_id`
- a source-level `ast` scan over `app/hatchet_workflows/*.py` that fails for
  **any** workflow declaring both `on_crons` and an absence-unsafe
  `concurrency`, so this cannot be reintroduced by a workflow added later

Both guards were confirmed to fail when the old expression is put back, then
pass once restored — a guard that has never been seen to fail is not a guard.

**Post-deploy verification.** The engine only registers the corrected
expression when the worker next connects, so after deploy confirm the cron
actually produces runs rather than assuming it:

```bash
az monitor log-analytics query -w f0ac10a8-45b9-4ea7-bef8-d0ab10c371c9 --analytics-query "ContainerAppConsoleLogs_CL | where ContainerAppName_s == 'hatchet-worker-cc' | where TimeGenerated > ago(2h) | where Log_s contains 'run: start step: embed_pending_passages' | extend mn=datetime_part('minute', TimeGenerated) | summarize n=count() by mn | order by mn asc"
```

Minutes should cluster on `:00/:10/:20/…`. A flat scatter means the cron
still is not firing and only inline dispatches are landing — the exact
signature this bug had.

---

## Finding 3 — the engine's own database is stopped 10 hours a day, and that is when most crons are scheduled

`hatchet-cc` stores its state in the `hatchet` database on `georag-pg-cc`,
which `shutdown-scheduler-cc` stops at 00:00 UTC and
`startup-scheduler-cc` restarts at 10:00 UTC. Confirmed live at 01:50 UTC
on 2026-08-21: server state `Stopped`, engine logging

```
ERR could not poll cron schedules   error="context deadline exceeded" service=ticker
ERR error acquiring concurrency leases
ERR could not get workflow version
```

Every one of these engine error classes is confined to hours 0, 1, 2 and 4
— zero occurrences during the day. The engine is healthy while the
database is up and completely broken while it is down. **When Hatchet's
own control plane is down, missed crons are not backfilled.** That is the
78–79% attendance, and 24 of the 32 cron declarations sit inside the
window.

On the worker side the same window produces a heartbeat that fails and
retries forever:

```
[ERROR] failed heartbeat (719)
grpc.aio._call.AioRpcError: DEADLINE_EXCEEDED
```

719 consecutive failures by 01:50. The worker never exits, never restarts,
and burns 4 vCPU emitting ~1,900 error lines an hour.

Three separable issues:

1. **The worker has no probe** (`probes: null`), so a wedged worker is
   never restarted by the platform — exactly what the 719-heartbeat loop
   demonstrates. The local compose healthcheck,
   `grep -q app.hatchet_workflows.worker /proc/1/cmdline`, can only fail
   once the process is already gone, so it detects nothing either.
2. **`hatchet-worker-cc` is `maxReplicas: 1`** with 20 slots and execution
   timeouts up to 24h (`generate_report`, `score_targets`). One stuck run
   parks a slot for a day and there is no second replica.
3. The already-recorded idea of splitting `WORKER_POOL` so the
   every-minute crons live on a small always-on pool is orthogonal. It
   saves money; it does not fix the engine being down.

The cheapest real fix is to move the nightly block past 10:00 UTC rather
than keep scheduling work into hours when the orchestrator cannot
orchestrate. Note that this is a *scheduling* decision and it does **not**
fix Finding 1 — those four fail at any hour.

---

## Finding 4 — all four nightly backups fail every night, and none of them has a destination that exists

| workflow | schedule | exception |
|---|---|---|
| `backup_postgres` | 02:00 | `FileNotFoundError: 'pg_dump'` — not installed in the FastAPI image |
| `backup_redis` | 02:45 | `FileNotFoundError: 'docker'` — shells out to `docker exec`, impossible in Container Apps |
| `backup_qdrant` | 02:30 | `ValueError: one of these env vars must be set: AWS_ACCESS_KEY_ID, S3_ACCESS_KEY, MINIO_ROOT_USER, …` |
| `backup_seaweedfs` | 03:00 | same `ValueError` |

50 `pg_dump` failures and 28 `docker` failures logged nightly from 08-14 to
08-20 inclusive. All four upload to a SeaweedFS S3 endpoint — **there is no
SeaweedFS container app in the resource group**; object storage is Azure
Blob via `georag_object_storage`, and no S3 credentials are set anywhere.

The failure path is instrumented correctly — `backup_postgres` writes
`backups.snapshot_runs` and emits a `backup.postgres.snapshot.failed`
audit row. Nothing reads either. See Finding 6.

---

## Finding 5 — the nightly self-healing agent calls itself over HTTP, at the hour its own API is scaled to zero

`nightly_ingestion_integrity` Tier 1 finds bronze objects with no silver
row and re-dispatches them — by POSTing to
`/internal/v1/shadow/ingest_pdf/trigger` on `fastapi-cc`
(`nightly_ingestion_integrity.py:227`, `_dispatch_ingest_pdf`), an HTTP
round-trip out of the worker and back into the app.

It runs at 02:00 and 04:00 UTC, when `fastapi-cc` is at **0 replicas**.
Result, every night since at least 08-18:

```
WARNING tier1.bronze.dispatch_failed key=reports/019fda61-…/…corporate-presentation.pdf err=
```

Seven orphaned files, retried and failed nightly. Note `err=` is **empty**
— the exception stringifies to nothing, so the log line carries no
diagnosis at all.

`stale_run_detector.py:237` already does this the right way, in-process:

```python
ref = await ingest_pdf.aio_run_no_wait(payload)
```

Tier 1 should do the same. That removes the HTTP hop, the JWT self-signing
inside `_dispatch_ingest_pdf`, and the dependency on another container app
being awake.

---

## Finding 6 — nothing alerts on a Hatchet failure, and the busiest workflow writes to a scraper that does not exist

`az monitor scheduled-query list -g georag` returns **empty**. There are no
log-based alerts at all. The 11 metric alerts that exist cover container
restarts, CPU, memory and Postgres availability — none of which move when a
workflow fails. The worker does not restart when it wedges (Finding 3), so
`hatchet-worker-cc-restarts` is silent by construction.

That is why four dead backups, four 100%-failing nightly jobs and three
never-firing crons went unnoticed for weeks: the signal is written and
nobody subscribes.

Compounding it: `reliability_metrics_publisher` runs **every minute**
(20,239 runs, joint-busiest in the fleet) to refresh Prometheus gauges in
the worker's own in-process registry. There is **no Prometheus and no
Grafana in the resource group**, and the worker container app has **no
ingress**, so nothing can scrape it even in principle. The busiest workflow
in the system is a no-op.

Minimum viable alerting, three log-based rules:

```kql
// 1. any workflow raising
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "hatchet-worker-cc"
| where Log_s has "Traceback (most recent call last)"

// 2. worker lost the engine
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "hatchet-worker-cc"
| where Log_s matches regex @"failed heartbeat \((\d{2,})\)"

// 3. started-but-never-finished, the check that would have caught Finding 1
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "hatchet-worker-cc" and TimeGenerated > ago(26h)
| extend wf = coalesce(extract(@"run: start step: ([a-zA-Z0-9_]+):", 1, Log_s),
                       extract(@"finished step run: ([a-zA-Z0-9_]+):", 1, Log_s)),
         k  = iff(Log_s has "finished step run:", "fin", "start")
| where isnotempty(wf)
| summarize started = countif(k == "start"), finished = countif(k == "fin") by wf
| where started > 0 and finished == 0
```

---

## Finding 7 — three production paths invoke workflows through the SDK's test helper

`aio_mock_run()` runs a task body inline as a plain coroutine: no engine,
no durability, no retry, no run record, nothing in the dashboard. It
appears in three non-test call sites:

- `what_changed_weekly.py:167` → `what_changed_detector`
- `routers/ml_training.py:160,173` → `train_target_model`, `train_source_trust`
- `services/report_builder/whatchanged_integration.py:71` → `what_changed_detector`

That is why `what_changed_detector`, `train_target_model` and
`train_source_trust` show zero runs while being registered on the worker —
their registration is decorative.

It also hides a live failure. On 2026-08-17:

```
INFO  what_changed_weekly.start   run_id=c1e74d42… window=2026-08-10..2026-08-17
INFO  what_changed_detector.task_started workspace=a0000000-…-000000000001
ERROR what_changed_weekly: detector failed for ws=a0000000-…: invalid input syntax for type uuid: ""
INFO  what_changed_weekly.completed run_id=c1e74d42… ws=1 ingest=0 audits=0
```

The wrapper catches the detector's exception and still reports
`completed … ingest=0 audits=0`. A weekly digest that errored on every
workspace is indistinguishable from a genuinely quiet week.

The `""`-cast error surfaces at `what_changed_detector.py:173`, the
`ops.support_tickets` count. `workspace_str` is a valid UUID at that point,
so the empty string is almost certainly coming from an RLS policy on
`ops.support_tickets` reading an unset GUC and casting `''::uuid` — the
legacy-`georag.workspace_id` family. Check `pg_policies` for that table.

---

## Finding 8 — smaller things, in descending order of consequence

1. **`outbox_dispatcher` long-polls for ~55 s of every 60 s minute**
   (`01:55:00.095` → `01:55:55.525`, every minute, all day). Each tick
   builds and tears down a fresh 2–10 connection asyncpg pool: ~1,440 pool
   lifecycles a day against PgBouncer for a queue that is almost always
   empty. A short drain on the existing `*/1` cron, or one durable
   long-lived task, does the same work for a fraction of the connections.

2. **`qdrant_payload_audit` is fail-open.** 43 of its 341 runs logged
   `Qdrant unreachable / scroll failed: . Retry next hour.` and were
   counted as **finished**. The Guard-2 payload-shape audit reports clean
   whenever `qdrant-cc` happens to be asleep — 13% of runs. It should fail
   the run, or at minimum emit a distinct audit row for "could not check".

3. **`_dispatch_neo4j` returns `transient_failure` forever**
   (`outbox_dispatcher.py:160`) for a store removed on 2026-07-28. Any
   lingering `target_store='neo4j'` row burns its full retry budget before
   dead-lettering. It should return `permanent_failure`; the condition is
   not transient and never will be.

4. **Stale crons on the engine.** `vllm_security_check_run` ran daily at
   01:00 until 08-15 and does not exist anywhere in the tree;
   `backup_neo4j` ran until 08-05. Hatchet does not remove a cron when a
   workflow stops being registered — the same leak the `bc_minfile_pull` /
   `nrcan_geo_pull` comment in `worker.py` documents. Both need an explicit
   de-registration sweep.

5. **Dead code carrying live cron declarations.** `bc_minfile_pull.py`
   (1,007 lines, `0 6 1 * *`) and `nrcan_geo_pull.py` (167 lines,
   `0 7 1 * *`) are retired and unregistered, but the `on_crons` are still
   in the files — anyone re-adding them to `POOLS` silently re-arms a
   monthly job against a retired pipeline. Delete them.

6. **`generate_report` and `score_targets` declare
   `execution_timeout="24h"`** on a single-replica, 20-slot worker, and
   `worker.py` describes both as skeletons. A stuck run parks a slot for a
   day.

7. **Version drift.** `docker-compose.yml:1581` pins
   `hatchet-lite:v0.86.12`; Azure runs `v0.89.7`. An engine-side bug is not
   guaranteed to reproduce locally.

8. **`HatchetDispatchThrottle` doc drift** — its docstring says
   `ingest_pdf` is `max_runs=1`; it was raised to 2 on 2026-08-07
   (`ingest_pdf.py:529`). The throttle window was sized against the old
   value.

9. **`charts/georag/templates/hatchet.yaml`** describes a Kubernetes
   StatefulSet that is not how anything runs — same class as the known
   `deploy/azure/containerapps/*.yaml` drift. It documents; it does not
   drive.

10. **`.claude/skills/hatchet-workflow/` contains only `NOTES.md`** — no
    `SKILL.md`, so the skill cannot be invoked.

---

## Verified healthy

- **`ingest_pdf`** — 366 step-starts, real parses landing (`sections=1297
  tables=625`), `on_failure` hook wired and firing, `mark_failed_by_run`
  writing terminal states. The `error_text=` kwarg crash that broke the
  failure handler on 08-20 is fixed in `47d77b1` and has not recurred since
  the 22:12 deploy.
- **`stale_run_detector`** — 1,387 runs, 94% attendance, dispatches
  in-process via the SDK, has `on_failure` hooks.
- **`cost_burn_watcher`** (4,051/4,029), **`cold_tier_archive`** (13/13),
  **`idempotency_keys_cleanup`** (13/12), **`pg_partman_maintenance`**
  (14/12), **`mv_refresh_silver`** and **`flow_jwt_key_reaper`** — all
  completing on schedule as of 2026-08-20.
- **The upload → workflow routing** in `UploadController::CATEGORIES` is
  accurate and well-documented: every accepted category has a live Hatchet
  consumer, and `RETIRED_CATEGORIES` correctly 422s the four that do not.
- `ingest_pdf`, `ingest_zip_archive`, `tiff_normalize` and
  `stale_run_detector` are the only workflows with `on_failure` hooks — but
  they are also the four that most need them.

## Status and remaining work

Done as of 2026-08-21:

- ~~The four grant/schema fixes (Finding 1)~~ — shipped in `47d77b1`
  2026-08-20 22:12 UTC, all four verified directly against the live
  database. Plus the unguarded `COMMENT ON COLUMN` in
  `2026_08_20_030000`, fixed in the working tree — that one would break a
  fresh canonical cluster and, because CD migrates before it deploys, ship
  no code at all.
- ~~CEL `has()` fix (Finding 2)~~ — applied to all three workflows, with
  the cron-payload defaults, the per-project workspace resolution the
  fan-outs needed, and property-based regression guards that were confirmed
  to fail on the old expression.

  Both the old and new expressions were evaluated against real payloads in a
  CEL implementation rather than reasoned about: the two old forms raise
  `no such member in mapping: 'workspace_id'` on `{}` (matching cel-go's
  `no such key`), and the new form returns `'cron'` for `{}`, `'cron'` for
  `{"workspace_id": ""}`, and the workspace id itself when one is supplied —
  so per-workspace grouping is preserved for the dispatch path.

  Full FastAPI suite: **2803 passed, 18 skipped, 0 failed** (`PYTEST_EXIT=0`),
  ruff clean. Two caveats on the harness, both pre-existing and neither a
  product defect: `test_backend_enum_contract.py` resolves `parents[3]` for
  the repo root and cannot collect when only `src/fastapi` is mounted, and
  `georag_object_storage` must be mounted over site-packages or
  `test_tiff_normalize_workflow` fails on a `metadata` module that is in the
  deployed image but not in the older local one. See
  `reference_fastapi_test_container_recipe`.

Remaining, in order:

1. **Three log-based alert rules**, especially the
   started-but-never-finished query (Finding 6). Everything above ran
   broken for weeks purely because nothing was watching; the alerting gap
   is the reason this was a review finding rather than a page.
2. **Move the remaining nightly crons past 10:00 UTC** (Finding 3), and
   give `hatchet-worker-cc` a liveness probe so a wedged worker restarts
   instead of retrying a heartbeat 719 times.
3. **Tier 1 in-process dispatch** (Finding 5).
4. **Backups** — rewrite against Azure Blob, or delete the four workflows
   rather than keep a nightly green-looking failure (Finding 4).
5. **Replace the three `aio_mock_run` call sites** (Finding 7).
6. The Finding 8 list.

Two things to watch on the next deploy, both consequences of fixes rather
than new problems:

- `retention_sweep`'s first successful run has a month or more of backlog
  to delete, against a 55-minute `execution_timeout`.
- The three revived crons will find a large first batch — `enrich` and
  `verbalize` have never processed anything at all, and `enrich` runs a
  Qwen3 call per passage. Consider a `max_passages` cap on the first run.
