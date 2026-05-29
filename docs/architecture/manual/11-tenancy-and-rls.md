# Chapter 11 — Tenancy and Row-Level Security

GeoRAG is multi-tenant at the row level. Every workspace’s data lives in
the same tables as every other workspace’s — segregation is enforced by
PostgreSQL RLS, not separate schemas/databases.

## 1. The tenancy spine

| Object | Where | Role |
|---|---|---|
| `silver.workspaces` | [03-schemas.md §2](03-schemas.md) | The tenant root. UUID PK, slug, monotonic `data_version`. RLS exempt. |
| `silver.projects` | same | Project belongs to one workspace; has its own `data_version`. RLS enforced. |
| `workspace.users`, `workspace.memberships` | `workspace.*` schema | Per-tenant user roster and role assignments. |
| `workspace.workspace_roles` | same | System-global roles (workspace_id NULL) visible to all; per-workspace roles fenced by tenant rule ([phase0/95-rls-policies.sql:101-114](../../../database/raw/phase0/95-rls-policies.sql)). |
| `public.users` | `public` (Laravel) | The authentication identity. One user can belong to many workspaces. |

## 2. The GUC contract

The session GUC `app.workspace_id` is **the** workspace fence.

- Set by Laravel middleware on every PgBouncer-pooled connection per request.
- Set by FastAPI middleware on every asyncpg connection per request.
- Set by Hatchet worker handlers via `SET LOCAL app.workspace_id = ?` inside
  the transaction. Hatchet workers bypass PgBouncer (use
  `POSTGRES_DIRECT_HOST`) so `SET LOCAL` actually persists for the duration
  of the txn.

Every RLS policy reads it via `current_setting('app.workspace_id', true)`.

## 3. The role split

[ch 02 §1](02-data-stores.md) listed the roles. The load-bearing facts for
RLS:

- `georag` (the owner role) is **`LOGIN SUPERUSER` with `rolbypassrls=true`**.
  Postgres superusers always bypass RLS — this is a *latent* tenant
  isolation risk anywhere `georag` is used for runtime traffic. The Phase 0
  finding **R-P0-10** flagged this; the fix is operational:
  - `georag` is restricted to migrations + admin via the dedicated
    `pgsql_migrations` Laravel connection only ([docker-compose.yml:538-547](../../../docker-compose.yml)).
  - The new runtime role `georag_app` was introduced with
    `NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE INHERIT`
    ([phase1/10-georag-app-role.sql:23-32](../../../database/raw/phase1/10-georag-app-role.sql))
    so RLS policies actually apply at runtime.
  - A self-check at [phase1/10-georag-app-role.sql:97-108](../../../database/raw/phase1/10-georag-app-role.sql)
    refuses startup if `georag_app` ever acquires SUPERUSER/BYPASSRLS.
  - Structural fix (proposed): split `georag` into `georag_owner` (table
    owner, `NOSUPERUSER`) + `georag_migrator` (DDL grants only,
    `NOSUPERUSER`). Tracked in [Ch 02 §1.1](02-data-stores.md#11-known-security-issue--georag-role-is-superuser).

- `pgbouncer` connects to Postgres as `georag_app` for the application
  connection pool. The owner `georag` is reachable only via the dedicated
  `pgsql_migrations` connection.

## 4. RLS policy patterns

### Strict pattern (the canonical shape)

[phase0/96-rls-tenant-isolation-block1.sql:79-84](../../../database/raw/phase0/96-rls-tenant-isolation-block1.sql) — `silver.collars`:

```sql
ALTER TABLE silver.collars ENABLE ROW LEVEL SECURITY;
ALTER TABLE silver.collars FORCE  ROW LEVEL SECURITY;
CREATE POLICY collars_workspace_isolation ON silver.collars
    USING (workspace_id = current_setting('app.workspace_id', true)::uuid)
    WITH CHECK (workspace_id = current_setting('app.workspace_id', true)::uuid);
```

`FORCE` is critical — without it, the table owner (`georag` during ALTERs)
would bypass.

### NULL-exempt pattern (cross-schema sweep)

[phase0/95-rls-policies.sql:72-86](../../../database/raw/phase0/95-rls-policies.sql) —
the DO-block loop applied to dozens of tables at Phase 0:

```sql
CREATE POLICY tenant_isolation ON %s
    USING (
        workspace_id IS NOT DISTINCT FROM
            NULLIF(current_setting('app.workspace_id', true), '')::uuid
        OR current_setting('app.workspace_id', true) IS NULL
        OR current_setting('app.workspace_id', true) = ''
    )
    WITH CHECK ( … same … )
```

Allows admin paths (no GUC) to read system-wide events. The asymmetric
`WITH CHECK` was tightened in
[2026_05_25_184857_normalize_layered_workspace_isolation_policies.php](../../../database/migrations/2026_05_25_184857_normalize_layered_workspace_isolation_policies.php) +
phase-2 follow-up
[185013](../../../database/migrations/2026_05_25_185013_normalize_layered_workspace_isolation_policies_phase2.php).

### Special case — workspace_roles

[phase0/95-rls-policies.sql:101-114](../../../database/raw/phase0/95-rls-policies.sql).
Global roles (`workspace_id IS NULL`) are visible to all sessions;
per-workspace roles follow the tenant rule.

### Tightened audit policy

[phase0/99-rls-block3-policy-tighten.sql:14](../../../database/raw/phase0/99-rls-block3-policy-tighten.sql).
`audit.audit_ledger` keeps a stricter shape so a query without the GUC
can't see other workspaces’ audit trails.

## 5. The coverage chain

Listed in [03-schemas.md §5](03-schemas.md). In order (with closure dates):

- `phase0/95..100` — initial sweep (2026-04, Phase 0).
- `phase0/111`, `112` — guard fixes.
- `2026_04_22_170000_extend_rls_workspace_coverage.php` — Phase 1 extension.
- `2026_05_12_180008_enable_rls_phase3_silver_tables.php` — §04p quality
  track.
- `2026_05_19_180100_enable_rls_on_uncovered_workspace_tables.php` — audit
  gap closure.
- `2026_05_20_010000_close_deferred_rls_gaps.php`,
  `2026_05_20_060800_enable_rls_on_drillhole_tables.php`.
- `2026_05_25_170825_enable_rls_on_bronze_tenancy_tables.php` — bronze
  cross-workspace leak closed
  ([project_bronze_tenancy_rls_2026_05_25](../notes/INDEX.md#project_bronze_tenancy_rls_2026_05_25)).
- `2026_05_25_173814_enable_rls_on_post_phase0_workspace_tables.php`,
  `175214`, `180924`, `182857`, `184630`, `184857`, `185013` — the May 25
  cleanup wave
  ([project_rls_coverage_audit_2026_05_25](../notes/INDEX.md#project_rls_coverage_audit_2026_05_25)).
- `2026_05_25_183115_tighten_bronze_tenancy_columns_to_not_null.php`.

## 6. The regression backstop

[tests/Feature/Tenancy/WorkspaceRlsCoverageTest.php:41](../../../tests/Feature/Tenancy/WorkspaceRlsCoverageTest.php).
PHPUnit, `RefreshDatabase`. For every live test-DB table with a
`workspace_id` column:

- Asserts `pg_class.relrowsecurity = true`
- Asserts at least one policy in `pg_policies`
- Exempts only `silver.workspaces` (self-referential) and partition
  children via `pg_inherits`

`EXEMPT_TEST_DB_ONLY_TABLES` was emptied on 2026-05-25 when test-DB parity
caught up to production
([project_test_db_parity_and_policy_normalization_2026_05_25](../notes/INDEX.md#project_test_db_parity_and_policy_normalization_2026_05_25)).

## 7. Service-to-service authentication

| Hop | Auth | Where |
|---|---|---|
| Browser → laravel-octane | Sanctum cookie (SPA) OR Sanctum PAT | `config/sanctum.php`, `EnsureFrontendRequestsAreStateful` middleware |
| laravel-octane → fastapi | `X-Service-Key: ${FASTAPI_SERVICE_KEY}` (HMAC) | All `Http::client()` calls into FastAPI; verified by [src/fastapi/app/services/auth.py](../../../src/fastapi/app/services/auth.py) |
| laravel-octane → fastapi (per-flow) | `Authorization: Bearer <jwt>` minted by `App\Services\FastApiJwtMinter` | For Kestra flows that hop through Laravel |
| Hatchet worker → laravel-octane | `X-Service-Key: ${FASTAPI_SERVICE_KEY}` | `services/laravel_bridge.py::post_*` |
| Kestra → fastapi | Per-flow JWT (`KESTRA_FLOW_JWT_SECRET` + per-flow private key from encrypted `workflow.flow_registry`) | `services/flow_jwt.py` |
| External webhook senders → kestra | HMAC-SHA256 over canonical JSON; `EXTERNAL_NOTIFICATION_HMAC_SECRET` | Hatchet `external_notification` workflow verifies on the receiving side |
| Hatchet worker → hatchet-lite (engine) | `HATCHET_CLIENT_TOKEN` (JWT) over gRPC | `HATCHET_CLIENT_HOST_PORT=hatchet-lite:7077` |
| caddy → laravel-octane (forward_auth) | Re-uses Sanctum cookie | `caddy/Caddyfile` |
| caddy → kestra (after forward_auth) | Basic auth (`KESTRA_BASIC_AUTH_USER` / `_PASSWORD`) | Injected by Caddy on behalf of the user |
| backup-agent → seaweedfs | S3 credentials | `S3_ACCESS_KEY` / `S3_SECRET_KEY` |

## 8. Per-flow JWT machinery

Kestra → FastAPI uses per-flow JWTs (not a single shared secret) so a leak
of one flow's key doesn't compromise the rest.

- Storage: `workflow.flow_registry` (each row holds an encrypted private
  key column).
- Encryption key: `AUDIT_ENCRYPTION_KEY` env var (32-byte minimum) — feeds
  pgcrypto.
- Loader: [services/flow_jwt.py](../../../src/fastapi/app/services/flow_jwt.py)
  `_load_per_flow_key_sync()` runs through the direct PG connection
  (`POSTGRES_DIRECT_HOST=postgresql`) because it uses `set_config()`.
- Key reaper: `flow_jwt_key_reaper` Hatchet workflow (weekly cron) rotates
  keys past TTL.

## 9. RLS-aware tests

Tests that need to exercise RLS use the **state-machine-tests** workspace
under the live tenancy fence. Convention noted in
[project_ingest_completion_terminal_2026_05_25](../notes/INDEX.md#project_ingest_completion_terminal_2026_05_25):
the `RefreshDatabase` PHPUnit trait + `actingAs()` doesn’t fence at the
RLS layer by default — tests must `SET app.workspace_id` explicitly.

## 10. `BYPASSRLS` is forbidden

No production role should have `BYPASSRLS`. The runtime app role
(`georag_app`) cannot have it; `georag` (owner) had it removed in Phase 1.
Migrations using `pgsql_migrations` run as `georag` — they CAN see across
workspaces during DDL, but never serve user requests.

## 11. Cross-workspace audit

`silver.cross_workspace_audit` (per
[project_rls_coverage_audit_2026_05_25](../notes/INDEX.md#project_rls_coverage_audit_2026_05_25))
catches stray FKs that point across workspace boundaries — invariants like
`silver.evidence_items.workspace_id == silver.evidence_items.document_passage_id.workspace_id`.

## 12. Provenance auto-fill

[03-schemas.md §10](03-schemas.md) — `bronze.provenance_autofill_workspace_id`
trigger fills in `workspace_id` from the target silver row at INSERT time.
Covers all 10+ existing provenance writers with zero code change.

## 13. The 12 always-fail-open policies that were broken

[project_parked_items_2026_05_25](../notes/INDEX.md#project_parked_items_2026_05_25):
12 RLS policies had a broken GUC read pattern that always permitted access
(the `current_setting()` returned `''` which `IS NOT DISTINCT FROM NULL` →
true for every row). Replaced with the canonical pattern by the
2026-05-25 normalisation migrations.

## 14. Common pitfalls

- **Forgetting `FORCE ROW LEVEL SECURITY`** — without it, the table owner
  bypasses. The `WorkspaceRlsCoverageTest` test catches this.
- **`SET` instead of `SET LOCAL`** — `SET` persists past the transaction
  on a pooled connection; never use it in handlers.
- **PgBouncer transaction mode + GUCs** — `SET` on a transaction-pooled
  connection leaks across users. Only `SET LOCAL` inside an explicit
  transaction is safe. Hatchet workers + Dagster + the FastAPI per-flow
  loader all use `POSTGRES_DIRECT_HOST=postgresql` for this reason.
- **Migrations running as `georag_app`** — they’d be blocked by RLS on
  every UPDATE. Use the `pgsql_migrations` connection.
