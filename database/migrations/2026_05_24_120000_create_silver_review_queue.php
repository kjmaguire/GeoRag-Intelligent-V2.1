<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * CC-01 Item 1 Slice 4 — create silver.review_queue + silver.review_audit_log.
 *
 * The schema was documented in src/fastapi/app/models/review_queue.py and
 * referenced by the RLS policies in database/raw/phase0/100-rls-tenant-
 * isolation-block4.sql, but no CREATE TABLE statement actually shipped
 * for it. This migration closes that gap so:
 *
 *   - DrillUploadController + the Dagster SRQ writer can persist rows
 *   - The DrillReview Foundry page can read + decide on them
 *   - Phase 1.A SRQ contract is honoured
 *
 * Schema choices that diverge from review_queue.py:
 *   - assigned_to_user_id / decided_by_user_id are BIGINT (matches the
 *     existing Laravel users.id auto-increment). The Pydantic model has
 *     UUID; that mismatch is a known follow-up — Laravel writes win
 *     because the DB is the canonical source.
 *
 * Test DB: this migration runs on both prod (pgsql) and the dedicated
 * georag_test DB. Skipped on sqlite (silver.* and jsonb don't exist).
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // ── Enums ─────────────────────────────────────────────────────────
        DB::statement(<<<'SQL'
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'review_routing_enum') THEN
                    CREATE TYPE review_routing_enum AS ENUM (
                        'auto_pass', 'review_required', 'auto_reject'
                    );
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'review_lifecycle_enum') THEN
                    CREATE TYPE review_lifecycle_enum AS ENUM (
                        'pending', 'in_review', 'decided', 'committed', 'archived'
                    );
                END IF;
                IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'review_decision_enum') THEN
                    CREATE TYPE review_decision_enum AS ENUM (
                        'approve_as_parsed', 'approve_with_corrections', 'reject', 'defer'
                    );
                END IF;
            END$$;
        SQL);

        // ── silver.review_queue ───────────────────────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.review_queue (
                queue_id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id            uuid NOT NULL,
                project_id              uuid NOT NULL,
                target_table            varchar(128) NOT NULL,
                target_record_kind      varchar(64)  NOT NULL,
                bronze_uri              varchar(1024) NOT NULL,
                bronze_row_offset       bigint CHECK (bronze_row_offset IS NULL OR bronze_row_offset >= 0),
                payload                 jsonb NOT NULL,
                confidence_per_field    jsonb NOT NULL DEFAULT '{}'::jsonb,
                confidence_record       numeric(4,3) NOT NULL
                                          CHECK (confidence_record >= 0 AND confidence_record <= 1),
                parser_version          varchar(128) NOT NULL,
                routing_decision        review_routing_enum NOT NULL,
                routing_reason          varchar(512),
                outlier_flags           jsonb NOT NULL DEFAULT '[]'::jsonb,
                lifecycle               review_lifecycle_enum NOT NULL DEFAULT 'pending',
                assigned_to_user_id     bigint,
                decided_by_user_id      bigint,
                decision_kind           review_decision_enum,
                decision_payload        jsonb,
                decision_rationale      varchar(2048),
                decided_at              timestamptz,
                committed_silver_pk     uuid,
                created_at              timestamptz NOT NULL DEFAULT now(),
                updated_at              timestamptz NOT NULL DEFAULT now(),
                CONSTRAINT review_queue_decision_payload_nonnull_when_corrections
                    CHECK (
                        decision_kind IS DISTINCT FROM 'approve_with_corrections'
                        OR decision_payload IS NOT NULL
                    ),
                CONSTRAINT review_queue_decision_payload_null_for_other_kinds
                    CHECK (
                        decision_kind IN ('approve_with_corrections') OR decision_kind IS NULL
                        OR decision_payload IS NULL
                    )
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS review_queue_workspace_lifecycle_idx ON silver.review_queue (workspace_id, lifecycle)');
        DB::statement('CREATE INDEX IF NOT EXISTS review_queue_project_target_idx ON silver.review_queue (project_id, target_table)');
        DB::statement('CREATE INDEX IF NOT EXISTS review_queue_bronze_uri_idx ON silver.review_queue (bronze_uri)');
        DB::statement('CREATE INDEX IF NOT EXISTS review_queue_assigned_user_idx ON silver.review_queue (assigned_to_user_id) WHERE assigned_to_user_id IS NOT NULL');
        DB::statement('CREATE INDEX IF NOT EXISTS review_queue_outlier_flags_gin ON silver.review_queue USING GIN (outlier_flags jsonb_path_ops)');

        // ── silver.review_audit_log (append-only state transitions) ──────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.review_audit_log (
                audit_id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                queue_id                uuid NOT NULL REFERENCES silver.review_queue(queue_id) ON DELETE CASCADE,
                actor_user_id           bigint,
                from_lifecycle          review_lifecycle_enum,
                to_lifecycle            review_lifecycle_enum NOT NULL,
                decision_kind           review_decision_enum,
                decision_payload_diff   jsonb,
                created_at              timestamptz NOT NULL DEFAULT now()
            )
        SQL);
        DB::statement('CREATE INDEX IF NOT EXISTS review_audit_log_queue_idx ON silver.review_audit_log (queue_id, created_at DESC)');

        DB::statement("COMMENT ON TABLE silver.review_queue IS 'Silver-promotion review queue. Parsers write rows here when confidence is low or outlier flags fired; reviewers approve/correct/reject from the Foundry DrillReview page; a downstream commit job promotes decided rows into silver.*.'");
        DB::statement("COMMENT ON TABLE silver.review_audit_log IS 'Append-only audit trail for review_queue state transitions. One row per lifecycle change.'");

        // ── Grants — application user reads/writes; the audit log is
        // insert-only for the app (committed updates land via dedicated
        // service writes).
        DB::statement('GRANT SELECT, INSERT, UPDATE ON silver.review_queue TO georag_app');
        DB::statement('GRANT SELECT, INSERT ON silver.review_audit_log TO georag_app');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP TABLE IF EXISTS silver.review_audit_log CASCADE');
        DB::statement('DROP TABLE IF EXISTS silver.review_queue CASCADE');
        DB::statement('DROP TYPE IF EXISTS review_decision_enum');
        DB::statement('DROP TYPE IF EXISTS review_lifecycle_enum');
        DB::statement('DROP TYPE IF EXISTS review_routing_enum');
    }
};
