## Doc-phase 174 handoff — audit.audit_ledger test-DB provisioning — full Track3 dashboard suite green

**Status:** Live + 14/14 Track3 dashboard tests pass under `phpunit.pgsql.xml` + 112/112 substrate verifier + 56/56 FastAPI eval regression preserved.

## What landed

A Laravel migration that mirrors the minimal `audit.audit_ledger` +
`audit.audit_ledger_verification_runs` schema needed by the dashboard
controllers. Production DBs still use the raw-SQL partitioned table
(`database/raw/phase0/20-layer-b-audit-ledger.sql` + the hash-chain
trigger from `90-audit-hash-chain-trigger.sql`); the new migration is
a `CREATE TABLE IF NOT EXISTS` no-op against production but creates a
plain non-partitioned table on the test DB.

### Production vs test-DB shape

| Aspect | Production | Test DB |
|---|---|---|
| Partitioning | Monthly via pg_partman | None — single table |
| Hash trigger | `BEFORE INSERT` computes sha256 chain | None |
| Schema columns | Identical (uuid, workspace_id, actor_id, action_type, payload, hash, ...) | Identical |
| Insert path | `AuditEmitter` writes with hash chain | Test fixtures insert directly |
| Read path | `EvalDashboardController`, `DecisionHistoryController`, `SupportCockpitController` SELECTs | Same controllers, see empty table → empty dashboards (correct behavior) |

The migration uses `CREATE TABLE IF NOT EXISTS` so when applied
against a production DB where the partitioned parent already exists,
it no-ops cleanly. Where the schema is fresh (test DB), it creates
the simple version.

### Closes 3 Track3 dashboard test failures

Pre-doc-phase 174 state under `phpunit.pgsql.xml`:

```
test_eval_dashboard_*                        — 4/4 PASS (closed doc-phase 172)
test_decision_history_admin_renders_with_*   — FAIL (audit.audit_ledger missing)
test_support_cockpit_admin_renders_with_*    — FAIL (audit.audit_ledger missing)
test_support_cockpit_status_filter_passes_*  — FAIL (audit.audit_ledger missing)
test_hypothesis_workspace_*                  — 3/3 PASS
```

Post-doc-phase 174:

```bash
docker exec georag-laravel-octane php artisan test --compact \
    -c phpunit.pgsql.xml tests/Feature/Admin/Track3DashboardsTest.php
# → 14 passed (111 assertions) in 4.71s
```

### Down-migration safety

The `down()` method gates `DROP TABLE` on `pg_class.relkind = 'r'`
(ordinary table) — when the production partitioned parent (`relkind
= 'p'`) is present, the drop is skipped. Prevents accidentally
destroying production audit data if someone runs `migrate:rollback`
against a real DB.

## Smoke verification

```bash
# Full Track3 suite
docker exec georag-laravel-octane php artisan test --compact \
    -c phpunit.pgsql.xml tests/Feature/Admin/Track3DashboardsTest.php
# → 14 passed in 4.71s (was 11 passed / 3 failed)

# Substrate verifier
bash scripts/autonomous_run_substrate_verify.sh
# → 112/112 checks passed
```

## Cumulative session state — 42 ticks closed

- **Doc-phase ticks this run:** **42** (132 → 174)
- **Substrate verifier:** **112/112 PASS**
- **Laravel Track3 dashboard tests (pgsql):** **14 of 14 — COMPLETE**
- **Live pytest cases:** 284
- **Sections closed:** §25.4 + §6 + §04i validators
- **Eval pipeline:** real_rag_v1 nightly cron live with full 6-layer chain
- **Phase A ingestion:** staging 200GB Uranium zip to container-local (in progress)

## Carry-overs

- The test-DB `audit.audit_ledger` is non-partitioned. If a future
  test exercises partition management or hash-chain verification,
  the test fixture should mirror the production setup more closely
  (mount pg_partman or hand-author partitions in the fixture).
  Today no test does this.
- The Laravel migration role `georag_app` lacks `CREATE` on database.
  Migrations relying on `CREATE SCHEMA` need to either run as
  superuser via raw-SQL apply or assume the schema already exists.
  Bronze and audit schemas are now both established via raw SQL +
  recorded in the migrations table — pattern documented for future
  schema-creating migrations.
- Full Track3 suite is now green under pgsql phpunit. Any new admin
  dashboard test landing in this suite should hold the line —
  consider adding a substrate-verifier check that runs the Track3
  suite end-to-end under pgsql once the audit migration timestamp
  no longer needs special handling.
