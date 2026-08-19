<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

/**
 * 2026-08-17 CI-gap audit — `bronze.source_files.ingested_by` was created
 * as `uuid` (2026_05_20_060000_create_bronze_drillhole_tables.php), but
 * `App\Http\Controllers\Api\V1\DrillUploadController::store()` writes
 * `(string) $user->id` into it, and every user-reference column
 * everywhere else in the schema (audit.audit_ledger.actor_id,
 * ops.support_tickets.reported_by_user_id, silver.collab_anchors.
 * created_by, workspace.workspace_memberships.user_id, etc.) is `bigint`
 * matching Laravel's default `public.users.id` — `ingested_by` is the
 * one outlier.
 *
 * Because a bigint id (e.g. "6") is never valid `uuid` input, this made
 * EVERY drill-upload INSERT fail with `invalid input syntax for type
 * uuid` — caught by the controller's own try/catch and surfaced as a
 * generic 500 `persist_failed`, so the endpoint has silently 500'd for
 * every real authenticated upload since the column was created. Verified
 * against the live dev DB: all 11 existing bronze.source_files rows are
 * `source_type='xlsx'` from an unrelated ingestion path that leaves
 * `ingested_by` NULL — zero rows have ever come from `source_type=
 * 'drill_upload'`, confirming the INSERT has never once succeeded.
 *
 * This went undetected because `DrillUploadControllerTest` (Postgres-
 * gated, RequiresPostgres) never actually ran in CI (see
 * `.github/workflows/ci.yml`'s laravel job / phpunit.pgsql.xml header),
 * and SQLite — where the fast default suite runs — has no real column
 * typing, so the SQLite mirror
 * (2026_05_20_060001_provision_bronze_source_files_for_test_db.php,
 * itself typed as a loose `string`) silently accepted the bigint-shaped
 * string and masked the bug there too.
 *
 * Safe to ALTER in place: the column has never held a non-NULL value
 * (see above), so there is no real data to migrate/cast.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'pgsql') {
            DB::statement('ALTER TABLE bronze.source_files ALTER COLUMN ingested_by DROP DEFAULT');
            DB::statement('ALTER TABLE bronze.source_files ALTER COLUMN ingested_by TYPE bigint USING NULL');
            DB::statement(<<<'SQL'
                ALTER TABLE bronze.source_files
                    ADD CONSTRAINT source_files_ingested_by_fkey
                    FOREIGN KEY (ingested_by) REFERENCES public.users(id)
                    ON DELETE SET NULL
            SQL);

            return;
        }

        if (DB::connection()->getDriverName() === 'sqlite') {
            Schema::table('source_files', function (Blueprint $table): void {
                $table->unsignedBigInteger('ingested_by')->nullable()->change();
            });
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'pgsql') {
            DB::statement('ALTER TABLE bronze.source_files DROP CONSTRAINT IF EXISTS source_files_ingested_by_fkey');
            DB::statement('ALTER TABLE bronze.source_files ALTER COLUMN ingested_by TYPE uuid USING NULL');

            return;
        }

        if (DB::connection()->getDriverName() === 'sqlite') {
            Schema::table('source_files', function (Blueprint $table): void {
                $table->string('ingested_by')->nullable()->change();
            });
        }
    }
};
