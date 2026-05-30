<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            // SQLite test DB — audit.query_audit_log is a flat name
            DB::statement('ALTER TABLE "audit.query_audit_log" ADD COLUMN faithfulness_score REAL NULL');
            DB::statement('ALTER TABLE "audit.query_audit_log" ADD COLUMN context_precision_score REAL NULL');

            return;
        }

        DB::statement('ALTER TABLE audit.query_audit_log ADD COLUMN IF NOT EXISTS faithfulness_score REAL NULL');
        DB::statement('ALTER TABLE audit.query_audit_log ADD COLUMN IF NOT EXISTS context_precision_score REAL NULL');
        DB::statement("COMMENT ON COLUMN audit.query_audit_log.faithfulness_score IS 'Qwen3-as-judge: fraction of answer claims supported by retrieved passages (0.0-1.0). NULL = not yet scored.'");
        DB::statement("COMMENT ON COLUMN audit.query_audit_log.context_precision_score IS 'Qwen3-as-judge: fraction of retrieved passages that were relevant (0.0-1.0). NULL = not yet scored.'");
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement('ALTER TABLE audit.query_audit_log DROP COLUMN IF EXISTS faithfulness_score');
        DB::statement('ALTER TABLE audit.query_audit_log DROP COLUMN IF EXISTS context_precision_score');
    }
};
