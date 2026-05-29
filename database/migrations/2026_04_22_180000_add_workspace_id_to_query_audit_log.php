<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

/**
 * Module 9 Chunk 9.8 — add workspace_id to query_audit_log.
 *
 * Closes audit finding A6-01 / A3 coverage gap: NI 43-101 compliance trail
 * needs to scope by workspace, not just project. A multi-tenant deployment
 * has to surface "all queries by workspace X" without a project_id JOIN.
 *
 * Backfill strategy: existing rows that have a project_id resolve their
 * workspace via silver.projects. Rows with no project_id (auth-only failures
 * before the project was selected, e.g. the new 403 events from Chunks 9.1
 * and 9.4) keep workspace_id NULL — those will be set inline by the writer
 * going forward.
 */
return new class extends Migration
{
    public function up(): void
    {
        Schema::table('query_audit_log', function (Blueprint $table): void {
            $table->uuid('workspace_id')->nullable()->after('project_id');
            $table->index('workspace_id');
        });

        // Backfill workspace_id from silver.projects via the existing
        // project_id linkage. Rows where the project doesn't (or no longer)
        // exist stay NULL; the application writer is responsible for
        // populating the column on every new insert.
        DB::statement(<<<'SQL'
            UPDATE query_audit_log AS q
            SET workspace_id = p.workspace_id
            FROM silver.projects AS p
            WHERE q.project_id IS NOT NULL
              AND p.project_id = q.project_id
              AND q.workspace_id IS NULL
        SQL);

        // Add a partial index to speed up the common query "what 403s did
        // workspace X get in the last 24h" (Module 9 Chunk 9.8 audit hooks).
        Schema::table('query_audit_log', function (Blueprint $table): void {
            $table->index(['workspace_id', 'created_at'], 'qal_workspace_created_idx');
        });
    }

    public function down(): void
    {
        Schema::table('query_audit_log', function (Blueprint $table): void {
            $table->dropIndex('qal_workspace_created_idx');
            $table->dropIndex(['workspace_id']);
            $table->dropColumn('workspace_id');
        });
    }
};
