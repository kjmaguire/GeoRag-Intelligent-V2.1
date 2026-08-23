# Fail-open RLS posture — census, decision, and sequencing

**Measured** 2026-08-21 against `postgis/postgis:18-3.6` with the full
`php artisan migrate` chain applied (264 migrations, 138 tenant-schema tables,
126 policies).
**Status of this document:** Tier 0 is implemented and merged-ready. Tiers 1–4
are a recommendation and need a human call on sequencing.

---

## 1. The census

The parked-item note from 2026-05-25 records "12 fail-open RLS policies".
**That number is wrong and always was low.** The verified count:

| | Policies | Distinct tables |
|---|---|---|
| Fail-open | **93** | **92** |
| Fail-closed | 33 | 32 |
| **Total** | 126 | 124 |

`silver.geochemistry` carries two fail-open policies, which is why 93 policies
cover 92 tables. There are **no RESTRICTIVE policies** anywhere in the cluster
and **no table mixes** a fail-open with a fail-closed policy — permissive
policies OR together, so a single fail-open policy would defeat any
fail-closed companion. Every policy applies to `PUBLIC`.

Reproduce with:

```bash
docker exec georag-integ-pg psql -U georag -d georag_test -c "select n.nspname||'.'||c.relname, p.polname, pg_get_expr(p.polqual, p.polrelid) from pg_policy p join pg_class c on c.oid = p.polrelid join pg_namespace n on n.oid = c.relnamespace where pg_get_expr(p.polqual, p.polrelid) ilike '%is null%' order by 1;"
```

Note the query in the original brief (`like '%IS NULL) OR%'`) happens to return
the same 93 rows, but only by luck — it matches the `current_setting(...) IS
NULL) OR` substring in the variant shapes below. Prefer the `ilike '%is null%'`
form, which is shape-independent.

### 1.1 Five distinct fail-open shapes

The brief describes one shape. There are five, and three of them would survive
a naive find-and-replace on the canonical text.

| # | Shape | Count | Notes |
|---|---|---|---|
| A | `GUC IS NULL OR workspace_id = GUC` | 76 | the canonical one |
| B | `GUC IS NULL OR workspace_id IS NULL OR workspace_id = GUC` | 7 | **doubly open** — NULL-workspace rows are visible to every tenant *even when the GUC is correctly bound* |
| C | `NOT (workspace_id IS DISTINCT FROM GUC) OR GUC IS NULL OR GUC = ''` | 6 | policy name `tenant_isolation`; also carries an explicit `WITH CHECK` |
| D | `GUC IS NULL OR EXISTS (parent scoped by GUC)` | 2 | `silver.collab_comments`, `silver.target_rationales` |
| E | `workspace_id::text = GUC OR GUC IS NULL OR GUC = ''` | 2 | `silver.source_trust_scores`; `silver.qp_credentials` scopes on `app.current_user_id`, a different axis entirely |

**Shape B is worse than the brief suggests.** On
`audit.audit_ledger`, `audit.query_audit_log`, `gold.mv_refresh_log`,
`targeting.target_backtests`, `workspace.workspace_roles` and two audit
verification tables, a row with `workspace_id IS NULL` is readable by every
tenant no matter what the GUC says. Fixing the unbound-GUC branch does not fix
that; it needs a separate decision about whether NULL-workspace rows are
legitimate system-wide records (they are, on the audit tables) or data leaks.

### 1.2 FORCE ROW LEVEL SECURITY is missing on 50 of the 92

`ENABLE ROW LEVEL SECURITY` does not apply to the table owner. 50 of the 92
fail-open tables are `ENABLE` without `FORCE`, so the owner — `georag`, which
`MIGRATE_DB_USERNAME` connects as — bypasses the policy entirely regardless of
its text.

This cuts **in favour** of flipping, and it is the single most reassuring
finding in this document: on those 50 tables, migrations, seeders, and ad-hoc
`psql -U georag` sessions are unaffected by any policy change, because the
policy never applied to them in the first place. The breakage surface is
`georag_app` (the application role, not the owner), plus the owner only on the
42 tables that *are* forced.

Locally `georag` is additionally `rolsuper` + `rolbypassrls`, so nothing
applies to it at all. **Do not rely on that for Azure** — Flexible Server's
admin role is not a true superuser, and this has not been verified there.

---

## 2. Code paths that read these tables with no GUC bound

Three trees were swept: `app/` (Laravel), `src/fastapi/app/` (Python), and
`database/seeders/`, plus `database/raw/`, `src/dagster/` and `tests/` for
completeness.

### 2.1 The Python readers — 86 unbound connection-owning functions

An AST sweep (same ownership rules as
`src/fastapi/tests/test_bind_workspace_scope_sweep.py`: a function only counts
if it calls `.acquire()`/`.connect()` itself rather than receiving `conn`)
found **86 functions that own a connection, query a fail-open table, and never
bind `app.workspace_id`**. They split two ways, and *both halves break under a
naive flip*:

- **21 functions carry an explicit `workspace_id = $n` predicate.** They are
  correct today *only because RLS lets them through*. Under fail-closed they
  return zero rows, because the predicate does not help when the policy has
  already filtered everything away.
- **65 functions carry no workspace predicate at all.** These are the ones
  currently doing unscoped cross-tenant reads. Under fail-closed they also
  return zero rows — which for the `SELECT`s is a safe failure, but for the
  `UPDATE`s is a silent no-op.

The `UPDATE` case is the dangerous one. Every progress and heartbeat write in
`hatchet_workflows/_progress.py` and `_archive_progress.py` updates by
`run_id` with no GUC: `mark_heartbeat()`, `mark_stage_progress()`,
`mark_completed_by_run()`, `mark_failed_by_run()`, `mark_timed_out()`,
`mark_cancelled()`. Fail-closed turns each into a zero-row `UPDATE` that
raises nothing, and ingestion silently stops reporting progress.

Concentration of unbound readers:

| Table | Unbound reader functions |
|---|---|
| `silver.ingest_progress` | 17 |
| `silver.reports` | 14 |
| `silver.collars` | 12 |
| `silver.projects` | 11 |
| `silver.answer_runs` | 9 |
| `audit.audit_ledger` | 7 |
| `silver.archive_ingest_runs` | 6 |

### 2.2 The bootstrap problem — the reason a blanket flip cannot work

**`silver.projects` and `silver.workspaces` are read by the code that decides
what the workspace is.** They cannot bind the GUC first, because discovering
the GUC's value is the entire purpose of the query.

`src/fastapi/app/services/workspace_resolution.py:43`
`_lookup_workspace_for_project()` runs
`SELECT workspace_id FROM silver.projects WHERE project_id = $1` on a bare
`pg_pool.acquire()`. Under fail-closed it returns `None` for every project, and
the documented resolution order falls through to **HTTP 403 on every request**.

`app/Http/Middleware/BindWorkspaceRlsContext.php` has the same loop and it is
subtler. `handle()` calls `resolveWorkspaceId()` *before* `bind()`, and
`resolveWorkspaceId()` reads `silver.projects` (via the `Project` model and via
the `project_user` pivot). At that moment the connection is carrying the empty
string that the **previous** request's `finally { $this->bind(null); }` left
behind. So the middleware that arms RLS depends on RLS being unarmed to do its
job. Flip `silver.projects` and every project-scoped request resolves to no
workspace, binds `''`, and then sees zero rows everywhere.

`WorkspaceRlsCoverageTest` already documents this for `silver.workspaces`,
exempting it as "self-referential — RLS would block reading the very rows used
to evaluate workspace membership". The same is true of `silver.projects` and
was never written down.

### 2.3 The empty-string fallback is a live fail-open dependency

`BindWorkspaceRlsContext::resolveWorkspaceId()` deliberately returns `null` —
and `bind()` then writes `''` — in four cases, one of which is routine:

> a user who belongs to **more than one workspace**, on any route that does not
> name a project.

Today that user sees every workspace's rows (including other tenants'). Under
fail-closed they see nothing at all. Neither is right; the correct fix is for
the middleware to resolve a workspace explicitly rather than to encode "I don't
know" as a value that the database interprets as "show everything".

### 2.4 Laravel non-HTTP paths (no middleware, therefore never bound)

| Path | Fail-open tables touched |
|---|---|
| `database/seeders/CgiVocabSeeder.php` | `silver.workspaces`, `silver.entity_aliases` |
| `database/seeders/DemoHoleAnalysisSeeder.php` | `silver.collars`, `silver.structure`, `silver.geochemistry` |
| `app/Console/Commands/Ingestion/ReingestProject.php` | `silver.projects`, `silver.reports` |
| `app/Jobs/DebounceWorkspaceMvRefresh.php` | `silver.projects`, `silver.reports`, `gold.mv_refresh_log`, `silver.review_queue`, `silver.document_ingestion_quality` |
| `app/Jobs/StreamQueryFromFastApi.php` | `silver.projects` |

Queue jobs and artisan commands do not run middleware, so none of these bind.
All of them run as the owner today, so the 50 non-FORCE tables are safe; the
forced ones are not.

### 2.5 Tables with no reader at all

**28 of the 92 have zero references** in `app/`, `src/`, or
`database/seeders/`. They are schema that migrations create and nothing
queries.

---

## 3. Recommendation

**Flip to fail-closed — but in tiers, not in one migration, and not before the
bootstrap path has a replacement.**

Keeping fail-open and compensating with mandatory explicit predicates is not a
viable alternative. It is what the codebase already nominally does, and the
measurement says it does not hold: 65 of 86 unbound readers have no predicate
at all. A convention that is violated in 76% of its call sites is not a
control. The `cluster_runner.py` incident is what that failure mode costs —
and the reason it was a cross-tenant *write* rather than an empty result set is
the policy shape, which is still in place on 80 tables.

The sequencing that makes this safe:

| Tier | Tables | Blocked on | Risk |
|---|---|---|---|
| **0 — done** | **12** | nothing | none |
| 1 — bootstrap | `silver.projects`, `silver.workspaces` | a `SECURITY DEFINER` resolver (below) | high until then |
| 2 — bind-first | 38 tables with unbound Python readers | binding the 86 functions | mechanical, high volume |
| 3 — Laravel-only | 42 tables whose only readers are middleware-bound | fixing the `''` fallback (§2.3) | changes behaviour for multi-workspace users |
| 4 — audit | shape-B tables | a decision on NULL-workspace rows | policy question, not a code one |

### 3.1 The bootstrap fix (unblocks Tier 1, and therefore everything)

Do not keep `silver.projects` fail-open as a special case. Make the deliberate
unbound read possible and the accidental one impossible:

```sql
CREATE FUNCTION silver.resolve_workspace_for_project(p_project_id uuid)
RETURNS uuid
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, silver
AS $$ SELECT workspace_id FROM silver.projects WHERE project_id = p_project_id $$;

GRANT EXECUTE ON FUNCTION silver.resolve_workspace_for_project(uuid) TO georag_app;
```

Owned by `georag`, so it bypasses RLS; it returns exactly one column for one
project and cannot be used to enumerate. Point
`_lookup_workspace_for_project()` and `BindWorkspaceRlsContext` at it, then
flip `silver.projects`. Every *other* unbound read of `silver.projects` — the
`cluster_runner.py` slug lookup class — then returns zero rows instead of
another tenant's project.

That is the change that would actually have prevented the incident, and it is
the one I recommend doing next.

---

## 4. What was implemented

`database/migrations/2026_08_21_030000_close_fail_open_rls_on_unreferenced_tables.php`
flips **12 tables** — every table where the flip provably cannot break
anything: no unbound reader in `src/fastapi/app`, and no reference at all in
`app/`, `database/seeders/`, `database/raw/`, `src/dagster/` or `tests/`.

```
bronze.raw_collar_entries          silver.ocr_page_quality
bronze.raw_geophysical_runs        silver.parser_run_artifacts
bronze.raw_surveys                 silver.table_extraction_quality
silver.control_points              silver.collab_comments
silver.historic_workings           silver.project_boundaries
silver.sample_intervals            audit.audit_ledger_chain_fork_quarantine
```

Two carve-outs, both deliberate:

- `silver.collab_comments` has no `workspace_id` column; it keeps its
  `EXISTS` scope through `silver.collab_anchors`, which is already
  fail-closed. Only the unbound-GUC branch was removed.
- `audit.audit_ledger_chain_fork_quarantine` has a **nullable**
  `workspace_id`, where NULL means a system-wide sweep belonging to no tenant.
  Dropping that branch would hide those rows from every reader, so it is
  retained (shape B, §1.1) and only the unbound-GUC branch removed. This table
  still needs the Tier-4 decision.

The migration is reversible; `down()` restores the fail-open branch, and the
round-trip was exercised against `georag_test`.

### Verification

`tests/Feature/Tenancy/FailClosedRlsPolicyTest.php`, registered in
`phpunit.pgsql.xml` (required — `PgsqlSuiteManifestTest` fails the build
otherwise). 13 tests, 28 assertions, all passing.

It asserts both halves. The catalog half checks all 12 policies no longer
contain an unbound-GUC branch. The behavioural half inserts two rows in two
workspaces as the owner, drops to `georag_app` via `SET LOCAL ROLE` — necessary
because the suite connects as the table owner, which policies do not apply to —
and asserts:

| GUC state | Rows visible | Before the flip |
|---|---|---|
| never set | **0** | 2 |
| set to `''` | **0** | 2 |
| set to workspace A | **1** | 1 |

The empty-string case is not hypothetical; it is what
`BindWorkspaceRlsContext` writes on every request whose workspace it cannot
resolve (§2.3).

The test was confirmed non-vacuous by reverting
`bronze_raw_surveys_workspace_isolation` to its fail-open form, at which point
both the catalog and behavioural assertions fail with the offending expression
in the message.
