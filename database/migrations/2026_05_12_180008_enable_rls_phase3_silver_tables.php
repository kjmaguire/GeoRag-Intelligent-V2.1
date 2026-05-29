<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Enable workspace-scoped RLS on all 8 master-plan §3 silver tables.
 *
 * Master-plan §3 Step 2 / Phase 0 decision #5. Mirrors the policy
 * pattern from database/raw/phase0/95-rls-policies.sql.
 *
 * Policy:
 *   workspace_id = current_setting('app.workspace_id')::uuid
 *
 * Application middleware MUST set the GUC per-connection
 * (or per-transaction) before queries. Asyncpg pattern:
 *   await conn.execute("SET LOCAL app.workspace_id = $1", workspace_id)
 *
 * Migration is idempotent: existing policies are dropped + recreated.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement(<<<'SQL'
            DO $$
            DECLARE
                target_tables text[] := ARRAY[
                    'ocr_page_quality',
                    'document_ingestion_quality',
                    'table_extraction_quality',
                    'parser_run_artifacts',
                    'low_confidence_page_reviews',
                    'ingest_extractions',
                    'ingest_layouts',
                    'ingest_ocr_results'
                ];
                t text;
                qualified text;
            BEGIN
                FOREACH t IN ARRAY target_tables LOOP
                    qualified := format('%I.%I', 'silver', t);

                    EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', qualified);
                    EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY',  qualified);

                    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %s', qualified);

                    EXECUTE format($f$
                        CREATE POLICY tenant_isolation ON %s
                            USING (
                                workspace_id IS NOT DISTINCT FROM
                                    NULLIF(current_setting('app.workspace_id', true), '')::uuid
                                OR current_setting('app.workspace_id', true) IS NULL
                                OR current_setting('app.workspace_id', true) = ''
                            )
                            WITH CHECK (
                                workspace_id IS NOT DISTINCT FROM
                                    NULLIF(current_setting('app.workspace_id', true), '')::uuid
                                OR current_setting('app.workspace_id', true) IS NULL
                                OR current_setting('app.workspace_id', true) = ''
                            )
                    $f$, qualified);

                    RAISE NOTICE 'phase3 RLS applied: %', qualified;
                END LOOP;
            END $$;
        SQL);
    }

    public function down(): void
    {
        DB::statement(<<<'SQL'
            DO $$
            DECLARE
                target_tables text[] := ARRAY[
                    'ocr_page_quality',
                    'document_ingestion_quality',
                    'table_extraction_quality',
                    'parser_run_artifacts',
                    'low_confidence_page_reviews',
                    'ingest_extractions',
                    'ingest_layouts',
                    'ingest_ocr_results'
                ];
                t text;
                qualified text;
            BEGIN
                FOREACH t IN ARRAY target_tables LOOP
                    qualified := format('%I.%I', 'silver', t);
                    EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %s', qualified);
                    EXECUTE format('ALTER TABLE %s DISABLE ROW LEVEL SECURITY', qualified);
                END LOOP;
            END $$;
        SQL);
    }
};
