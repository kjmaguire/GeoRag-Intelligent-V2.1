<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Rename schema `public_geoscience` → `public_geo`.
 *
 * Master plan v2.4.2 §22.1 names this namespace `public_geo`. The original
 * Phase 0 build used `public_geoscience` (a longer, more descriptive name)
 * with a deviation note in `scripts/phase0_step1_verify.sh`. This migration
 * aligns the DB to the canonical spec name so the Phase 0 step-1 verifier
 * passes without a deviation note and Phase 1 schema diff stays clean.
 *
 * `ALTER SCHEMA ... RENAME TO` is an atomic catalog operation: every table,
 * index, view, function, and RLS policy in the schema follows automatically.
 *
 * SQLite (test DB) does not have schemas — skip gracefully under that driver.
 *
 * On a fresh-init Postgres cluster, the schema is created as `public_geo`
 * directly by `2026_04_14_000000_create_public_geo_schema.php` (the original
 * migration was already updated by the 2026-05-17 propagation pass). This
 * migration therefore guards on both:
 *   1. The legacy name still exists  → run the rename.
 *   2. Only the new name exists       → no-op (idempotent on fresh installs).
 *
 * No `down()` — reverting the rename would invalidate every code path that
 * now references `public_geo.*`. If a rollback is truly required, restore
 * from a pre-2026-05-17 snapshot.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        $legacyExists = (bool) DB::selectOne(
            "SELECT 1 FROM pg_namespace WHERE nspname = 'public_geoscience'",
        );
        $newExists = (bool) DB::selectOne(
            "SELECT 1 FROM pg_namespace WHERE nspname = 'public_geo'",
        );

        if ($legacyExists && ! $newExists) {
            DB::statement('ALTER SCHEMA public_geoscience RENAME TO public_geo');
        }
        // else: either already renamed or fresh-init created public_geo
        // directly. No-op.
    }

    public function down(): void
    {
        // Intentional no-op — code references are now all `public_geo.*`,
        // a reverse rename would break the application. Restore from a
        // pre-2026-05-17 snapshot if a hard rollback is required.
    }
};
