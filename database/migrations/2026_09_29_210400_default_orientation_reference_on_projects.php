<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * DEFAULT 'BOH' on silver.projects.orientation_reference (§04e, database
 * audit 2026-09-29 PG-14).
 *
 * The column is NOT NULL with no default (2026_04_09_180000:20), while
 * StoreProjectRequest validated it as nullable — an API client that omitted
 * it got a 500 on INSERT. The request now defaults it; this makes any other
 * writer that omits it land on the same value instead of failing.
 *
 * BOH is what every writer already uses (the New Project form, FastAPI's
 * Project model, and since 2026-09-29 the ingestion project stubs, which
 * wrote 'grid_north'). SET DEFAULT is catalog-only: no rewrite, no scan,
 * and existing rows — including legacy 'grid_north' ones — are untouched.
 * No CHECK is added: the vocabulary is an SME question (BOH/TOH is the
 * core-orientation mark; 'grid_north' is a north reference), and a CHECK
 * would reject those legacy rows.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement("ALTER TABLE silver.projects ALTER COLUMN orientation_reference SET DEFAULT 'BOH'");
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('ALTER TABLE silver.projects ALTER COLUMN orientation_reference DROP DEFAULT');
    }
};
