# Schema `public` — Data Dictionary (skeleton)

Default Postgres schema. Holds Laravel-managed tables + extensions +
the standalone `public.smdi_deposits`.

## Laravel-managed tables

| Table | Purpose | Source |
|---|---|---|
| `public.users` | Auth identity (BIGINT `id`) | `0001_01_01_000000_create_users_table.php` |
| `public.password_reset_tokens` | Sanctum / forgot-password | `0001_01_01_000000_*` |
| `public.sessions` | Sanctum SPA session store (when SESSION_DRIVER=database; in prod we use Redis) | Laravel default |
| `public.cache`, `public.cache_locks` | Laravel cache fallback (Redis is primary) | `0001_01_01_000001_create_cache_table.php` |
| `public.jobs`, `public.job_batches`, `public.failed_jobs` | Laravel queue tables (Redis is primary; these are dead in prod but exist) | `0001_01_01_000002_create_jobs_table.php` |
| `public.migrations` | Laravel migration ledger | Laravel default |
| `public.personal_access_tokens` | Sanctum PATs | `2026_04_09_173743_create_personal_access_tokens_table.php` |
| `public.pulse_*` | Laravel Pulse recorders | `2026_04_09_173734_create_pulse_tables.php` |
| `public.exports` | Generated export ledger | `2026_04_13_000000_create_exports_table.php` |
| `public.project_user` | Per-project user roster (pivot) | `2026_04_11_000000_create_project_user_table.php` |
| `public.query_audit_log` | Historical — moved to `audit.query_audit_log` by [2026_05_07_120000](../../../database/migrations/2026_05_07_120000_move_query_audit_log_to_audit_schema.php) | (migrated) |

## Geoscience reference

| Table | Created by | Status |
|---|---|---|
| `public.smdi_deposits` | [2026_05_25_050000](../../../database/migrations/2026_05_25_050000_create_smdi_deposits.php) | Live — 6,012 SK Mineral Deposit Index points; served via Martin |

## Extensions

`postgis`, `postgis_raster`, `postgis_topology`, `pgcrypto`, `pg_trgm`,
`uuid-ossp`, `pg_stat_statements`, `h3`, `h3_postgis`, `hypopg`,
`pg_stat_kcache`, `pg_repack`, `pg_ivm` all live in `public`. See [Ch 02 §1](../manual/02-data-stores.md).
