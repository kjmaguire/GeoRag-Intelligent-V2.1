<?php

declare(strict_types=1);

use App\Services\Collars\CanonicalHoleIdIndex;
use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * §04e: UNIQUE (project_id, hole_id_canonical) on silver.collars — part 2 of
 * 2 (SME-approved, Kyle, 2026-09-29).
 *
 * Backfills hole_id_canonical through silver.canonical_hole_id (part 1:
 * 2026_09_29_230200_derive_hole_id_canonical_on_collars) and builds
 * `collars_project_id_hole_id_canonical_unique` CONCURRENTLY — which is why
 * this migration runs outside a transaction.
 *
 * THIS MIGRATION NEVER FAILS A DEPLOY. Production may already hold ghost
 * collars (two spellings of one hole in one project). When it does, the
 * index is NOT built: a warning names the number of duplicate groups and
 * collars, the migration completes, and
 *
 *     php artisan collars:merge-duplicates --database=pgsql_migrations            (dry run)
 *     php artisan collars:merge-duplicates --database=pgsql_migrations --execute
 *
 * merges them and builds the index. A later `php artisan migrate` builds it
 * too: App\Providers\AppServiceProvider re-runs CanonicalHoleIdIndex::ensure()
 * after every `migrate` while the index is missing. All of the logic lives in
 * App\Services\Collars\CanonicalHoleIdIndex so the three paths cannot drift.
 *
 * down() restores the partial `uq_collars_project_hole_canonical` it
 * superseded before dropping the full index; the backfilled values stay (they
 * are the values the trigger would write anyway).
 */
return new class extends Migration
{
    /**
     * CREATE / DROP INDEX CONCURRENTLY cannot run inside a transaction block.
     */
    public $withinTransaction = false;

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        app(CanonicalHoleIdIndex::class)->ensure(DB::getDefaultConnection());
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        app(CanonicalHoleIdIndex::class)->drop(DB::getDefaultConnection());
    }
};
