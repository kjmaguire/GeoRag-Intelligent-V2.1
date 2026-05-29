<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Phase-2 of layered-policy cleanup — extends 2026_05_25_184857 with
 * the application schemas it missed (only filtered for silver/gold/
 * bronze/audit/public_geo/index). 44 policies remained in:
 *
 *   targeting     — target_candidate_zones, target_outcomes,
 *                   target_recommendations, target_review_decisions,
 *                   target_scores, target_backtests, target_score_factors,
 *                   target_uncertainties
 *   interpretation — interpretation_comments, interpretation_notes,
 *                    interpretation_section_lines, interpretation_target_zones
 *   workspace     — agent_permissions, approval_requirements,
 *                    tool_invocations, dry_run_outputs, feature_flag_history,
 *                    feature_flags, idempotency_keys, workspace_agent_config,
 *                    workspace_memberships, workspace_roles
 *   ops           — support_replay_runs, support_ticket_traces,
 *                    support_tickets
 *   outbox        — pending_propagations, propagation_attempts
 *   usage         — usage_aggregates_daily, usage_events,
 *                    workspace_cost_ceilings
 *   workflow      — workflow_run_events, workflow_runs
 *   audit         — audit_ledger, audit_ledger_verification_runs,
 *                    integration_credentials_audit
 *   silver        — kg_*_aliases (4), spatial_features, storage_tier_policy,
 *                    assays, assay_samples, claim_ledger
 *
 * The behavior + safety analysis is identical to phase 1 (see that
 * migration's class doc) — pure catalog hygiene, zero behavior change.
 *
 * SQLite (test DB) does not support RLS — gated on Postgres.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::unprepared(<<<'SQL'
            DO $$
            DECLARE
                rec RECORD;
                is_nullable text;
            BEGIN
                FOR rec IN
                    SELECT p.schemaname, p.tablename, p.policyname
                      FROM pg_policies p
                     WHERE p.schemaname NOT IN ('pg_catalog', 'information_schema', 'public')
                       AND p.qual::text LIKE '%NULLIF%'
                       AND (LENGTH(p.qual::text) - LENGTH(REPLACE(p.qual::text, 'NULLIF', '')))
                           / LENGTH('NULLIF') >= 3
                LOOP
                    -- Skip nullable-workspace_id tables — those policies
                    -- carry the `OR workspace_id IS NULL` exemption,
                    -- which is a legitimate different shape.
                    SELECT c.is_nullable INTO is_nullable
                      FROM information_schema.columns c
                     WHERE c.table_schema = rec.schemaname
                       AND c.table_name = rec.tablename
                       AND c.column_name = 'workspace_id';

                    IF is_nullable IS NULL OR is_nullable = 'YES' THEN
                        CONTINUE;
                    END IF;

                    EXECUTE format(
                        'DROP POLICY IF EXISTS %I ON %I.%I',
                        rec.policyname, rec.schemaname, rec.tablename
                    );
                    EXECUTE format(
                        $f$
                        CREATE POLICY %I ON %I.%I
                          USING (
                            NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                            OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                          )
                        $f$,
                        rec.policyname, rec.schemaname, rec.tablename
                    );
                END LOOP;
            END
            $$;
        SQL);
    }

    public function down(): void
    {
        // No-op — see phase-1 migration.
    }
};
