<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Tighten the `workflow.workflow_runs.engine` CHECK constraint to drop the
 * sunset `'activepieces'` value and add the live `'kestra'` value.
 *
 * Background — Activepieces was the Phase 2 integration orchestrator. At
 * Phase 3 Step 7 it was sunset wholesale and replaced by Kestra (see
 * `database/raw/phase3/90-activepieces-sunset.sql`, which drops the
 * logical DB, role, and feature flags). The CHECK constraint on
 * `workflow.workflow_runs.engine` was never updated at the time, so:
 *   - Fresh DB initialisations (via `database/raw/phase0/30-layer-c-...`)
 *     still admitted `'activepieces'` despite the service being gone.
 *   - Kestra writes to `engine='kestra'` would have failed the constraint.
 *
 * This migration reconciles legacy rows (remaps any `'activepieces'` rows
 * to `'kestra'`, since Kestra now owns those flows) and re-installs the
 * constraint with the corrected vocabulary.
 *
 * SQLite (test DB) does not have a `workflow` schema so the migration
 * is gated on the Postgres driver.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // Reconcile legacy rows first so the tightened constraint can land.
        // Activepieces flows were rehomed onto Kestra at Phase 3 Step 7;
        // historical rows belong to flows now owned by Kestra.
        DB::statement(
            "UPDATE workflow.workflow_runs SET engine = 'kestra' WHERE engine = 'activepieces'",
        );

        DB::statement(
            'ALTER TABLE workflow.workflow_runs DROP CONSTRAINT IF EXISTS workflow_runs_engine_check',
        );
        DB::statement(
            'ALTER TABLE workflow.workflow_runs ADD CONSTRAINT workflow_runs_engine_check '
            ."CHECK (engine IN ('hatchet','kestra','langgraph','dagster','horizon','reverb'))",
        );
    }

    public function down(): void
    {
        // No-op. The Activepieces sunset is irreversible (the logical DB,
        // role, and feature flags were already dropped at Phase 3 Step 7);
        // re-admitting `'activepieces'` to the constraint would mask the
        // fact that the orchestrator no longer exists.
    }
};
