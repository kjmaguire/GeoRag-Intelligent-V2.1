# `database/raw/_adhoc/` — recovery & smoke SQL (committed, not auto-applied)

This directory holds SQL files that **don't** belong in the phase-numbered raw
sequence (`phase0/`, `phase1/`, `phase13/`, …) but are still worth tracking
under version control.

## What goes here

1. **One-time DDL recovery scripts** — when a Laravel migration is blocked by
   a permission gap or an environment-specific quirk and the operator applies
   the DDL manually via `psql -U georag`. The script captures the exact SQL
   that ran so other clusters can re-apply it deterministically.
2. **Smoke / contract-validation SQL** — short scripts that prove the SQL
   contract between a Dagster asset (or other writer) and the live schema is
   still aligned. Re-run after schema migrations to catch drift fast.

## What does NOT go here

- Anything that should run on every fresh cluster → `phase{N}/`.
- Anything that belongs in a Laravel migration → `database/migrations/`.
- Per-PR scratch SQL the author wrote and threw away → don't commit.

## Naming

- Recovery: `YYYY_MM_DD_<intent>.sql` (mirrors the Laravel migration timestamp
  format so the trail is easy to follow).
- Smoke: `<asset-or-feature>_smoke.sql`.

## Current entries

| File | Origin | Why it's here |
|---|---|---|
| [`2026_05_22_extend_silver_spatial_features.sql`](2026_05_22_extend_silver_spatial_features.sql) | Manual application of Laravel migration `2026_05_22_010000_extend_silver_spatial_features.php` | The Laravel role `georag_app` couldn't ALTER `silver.spatial_features` (owned by `georag` from phase0). Applied via `psql -U georag` then recorded in `migrations` (batch 30). After [[pg-migration-connection-2026-05-22]] landed, future migrations use `--database=pgsql_migrations` and don't need this workaround. Kept for clusters that haven't run the new connection yet. |
| [`2026_05_24_backfill_georef_method.sql`](2026_05_24_backfill_georef_method.sql) | Backfill for CC-01 Item 2 follow-on | Migration `2026_05_23_050000_add_spatial_uncertainty_to_collars_and_spatial_features` left `georef_method` + `crs_confidence` NULL on every legacy row. This script sets `'detected' / 0.7` where geom exists and `'assumed' / 0.3` where only easting/northing exist. Idempotent. Prints a count delta and rolls back if any targeted row is left NULL. |
| [`b6_b7_smoke.sql`](b6_b7_smoke.sql) | Schema-drift check after B6/B7 asset rewrite | Smoke-tests the UPSERT/INSERT paths in `gold_cross_section_panels.py` + `gold_structure_measurements_visual.py` against live PG. Run after any migration that touches `gold.cross_section_panels`, `gold.structure_measurements_visual`, `silver.collars`, or `silver.structure`. |

## How to run

```bash
docker exec -i georag-postgresql psql -U georag -d georag < database/raw/_adhoc/<file>.sql
```

The privileged `georag` role is required for recovery scripts that ALTER
phase0-owned tables; `georag_app` is fine for the smoke scripts.
