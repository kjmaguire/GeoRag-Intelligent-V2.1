<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

/**
 * StreamQueryFromFastApi has persisted guard_error_codes into
 * query_audit_log.metadata since the Plan §4b durability block, but no
 * migration ever created the column — the write only fires when a guard
 * code trips (e.g. NUMERIC_GROUNDING_FAILED), so every guarded answer
 * crashed the job mid-finalisation and the chat UI never received its
 * terminal event (observed live on Azure 2026-08-11). IF NOT EXISTS keeps
 * this idempotent with the emergency ALTER already applied to the live DB.
 */
return new class extends Migration
{
    public function up(): void
    {
        if ($this->connectionSupportsSchemas()) {
            DB::statement('ALTER TABLE audit.query_audit_log ADD COLUMN IF NOT EXISTS metadata jsonb');

            return;
        }

        // sqlite test DB: table lives unqualified (see QueryAuditLog::getTable()).
        if (! Schema::hasColumn('query_audit_log', 'metadata')) {
            Schema::table('query_audit_log', function ($table) {
                $table->json('metadata')->nullable();
            });
        }
    }

    public function down(): void
    {
        if ($this->connectionSupportsSchemas()) {
            DB::statement('ALTER TABLE audit.query_audit_log DROP COLUMN IF EXISTS metadata');

            return;
        }

        if (Schema::hasColumn('query_audit_log', 'metadata')) {
            Schema::table('query_audit_log', function ($table) {
                $table->dropColumn('metadata');
            });
        }
    }

    private function connectionSupportsSchemas(): bool
    {
        return DB::connection()->getDriverName() === 'pgsql';
    }
};
