<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    public function up(): void
    {
        DB::statement("ALTER TABLE silver.projects ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'active'");
        DB::statement("ALTER TABLE silver.projects ADD COLUMN IF NOT EXISTS slug VARCHAR(255)");

        DB::statement("ALTER TABLE silver.projects ADD CONSTRAINT projects_status_check CHECK (status IN ('active', 'indexing', 'degraded', 'archived'))");

        // Backfill slugs from project_name for existing rows.
        DB::statement("UPDATE silver.projects SET slug = LOWER(REGEXP_REPLACE(TRIM(project_name), '[^a-zA-Z0-9]+', '-', 'g')) WHERE slug IS NULL");

        // Remove trailing hyphens from generated slugs.
        DB::statement("UPDATE silver.projects SET slug = RTRIM(slug, '-') WHERE slug LIKE '%-'");

        DB::statement("ALTER TABLE silver.projects ALTER COLUMN slug SET NOT NULL");
        DB::statement("CREATE UNIQUE INDEX IF NOT EXISTS projects_slug_unique ON silver.projects (slug)");
    }

    public function down(): void
    {
        DB::statement('DROP INDEX IF EXISTS silver.projects_slug_unique');
        DB::statement('ALTER TABLE silver.projects DROP CONSTRAINT IF EXISTS projects_status_check');
        DB::statement('ALTER TABLE silver.projects DROP COLUMN IF EXISTS slug');
        DB::statement('ALTER TABLE silver.projects DROP COLUMN IF EXISTS status');
    }
};
