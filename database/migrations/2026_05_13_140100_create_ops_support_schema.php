<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create `ops.*` schema — Customer Support Cockpit tables
 * (doc-phase 97 / §10.8 / §25.2).
 *
 * Three tables per master-plan §25.2:
 *   - support_tickets         — customer-reported issues
 *   - support_ticket_traces   — many-to-many: tickets ↔ correlated traces
 *   - support_replay_runs     — workflow replay attempts for diagnosis
 *
 * ops.* schema is GLOBAL — no workspace RLS; cross-workspace access
 * is logged via `app.audit.emit_audit(action_type='support_access')`
 * per §25.3. Workspace owners can see those audit entries on their
 * own ledger.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('CREATE SCHEMA IF NOT EXISTS ops;');
        DB::statement('SET search_path TO ops, silver, public;');

        // ---------------- support_tickets ----------------
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS ops.support_tickets (
                ticket_id        UUID         NOT NULL DEFAULT gen_random_uuid(),
                workspace_id     UUID         NULL,
                reported_by_user_id BIGINT    NULL,
                reported_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                channel          VARCHAR(20)  NOT NULL,
                category         VARCHAR(40)  NOT NULL,
                description      TEXT         NOT NULL,
                severity         VARCHAR(10)  NOT NULL DEFAULT 'medium',
                assigned_to_user_id BIGINT    NULL,
                status           VARCHAR(20)  NOT NULL DEFAULT 'open',
                resolution_summary TEXT       NULL,
                resolved_at      TIMESTAMPTZ  NULL,
                customer_visible_response TEXT NULL,
                CONSTRAINT support_tickets_pkey PRIMARY KEY (ticket_id),
                CONSTRAINT support_tickets_workspace_id_fkey
                    FOREIGN KEY (workspace_id)
                    REFERENCES silver.workspaces (workspace_id)
                    ON DELETE SET NULL,
                CONSTRAINT support_tickets_reported_by_user_id_fkey
                    FOREIGN KEY (reported_by_user_id)
                    REFERENCES public.users (id)
                    ON DELETE SET NULL,
                CONSTRAINT support_tickets_assigned_to_user_id_fkey
                    FOREIGN KEY (assigned_to_user_id)
                    REFERENCES public.users (id)
                    ON DELETE SET NULL,
                CONSTRAINT support_tickets_channel_valid
                    CHECK (channel IN ('in_app', 'email', 'webhook', 'phone')),
                CONSTRAINT support_tickets_category_valid
                    CHECK (category IN (
                        'wrong_answer', 'failed_ingestion', 'failed_report',
                        'integration_issue', 'performance', 'other'
                    )),
                CONSTRAINT support_tickets_severity_valid
                    CHECK (severity IN ('low', 'medium', 'high', 'critical')),
                CONSTRAINT support_tickets_status_valid
                    CHECK (status IN ('open', 'investigating', 'resolved', 'closed'))
            );
        SQL);

        // ---------------- support_ticket_traces ----------------
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS ops.support_ticket_traces (
                link_id          UUID         NOT NULL DEFAULT gen_random_uuid(),
                ticket_id        UUID         NOT NULL,
                trace_id         VARCHAR(120) NOT NULL,
                trace_summary    TEXT         NOT NULL,
                added_by_user_id BIGINT       NOT NULL,
                added_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT support_ticket_traces_pkey PRIMARY KEY (link_id),
                CONSTRAINT support_ticket_traces_ticket_id_fkey
                    FOREIGN KEY (ticket_id)
                    REFERENCES ops.support_tickets (ticket_id)
                    ON DELETE CASCADE,
                CONSTRAINT support_ticket_traces_added_by_fkey
                    FOREIGN KEY (added_by_user_id)
                    REFERENCES public.users (id)
                    ON DELETE RESTRICT,
                CONSTRAINT support_ticket_traces_unique
                    UNIQUE (ticket_id, trace_id)
            );
        SQL);

        // ---------------- support_replay_runs ----------------
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS ops.support_replay_runs (
                replay_id        UUID         NOT NULL DEFAULT gen_random_uuid(),
                ticket_id        UUID         NOT NULL,
                original_workflow_run_id VARCHAR(120) NOT NULL,
                replay_workflow_run_id VARCHAR(120) NULL,
                diff_summary     TEXT         NULL,
                dry_run          BOOLEAN      NOT NULL DEFAULT true,
                initiated_by_user_id BIGINT   NOT NULL,
                initiated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                completed_at     TIMESTAMPTZ  NULL,
                status           VARCHAR(20)  NOT NULL DEFAULT 'pending',
                CONSTRAINT support_replay_runs_pkey PRIMARY KEY (replay_id),
                CONSTRAINT support_replay_runs_ticket_id_fkey
                    FOREIGN KEY (ticket_id)
                    REFERENCES ops.support_tickets (ticket_id)
                    ON DELETE CASCADE,
                CONSTRAINT support_replay_runs_initiated_by_fkey
                    FOREIGN KEY (initiated_by_user_id)
                    REFERENCES public.users (id)
                    ON DELETE RESTRICT,
                CONSTRAINT support_replay_runs_status_valid
                    CHECK (status IN ('pending', 'running', 'completed', 'failed', 'aborted'))
            );
        SQL);

        // ---------------- Indexes ----------------
        DB::statement('CREATE INDEX IF NOT EXISTS idx_support_tickets_status_severity
                       ON ops.support_tickets (status, severity);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_support_tickets_workspace
                       ON ops.support_tickets (workspace_id) WHERE workspace_id IS NOT NULL;');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_support_tickets_assigned
                       ON ops.support_tickets (assigned_to_user_id) WHERE assigned_to_user_id IS NOT NULL;');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_support_ticket_traces_ticket
                       ON ops.support_ticket_traces (ticket_id);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_support_ticket_traces_trace
                       ON ops.support_ticket_traces (trace_id);');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_support_replay_runs_ticket
                       ON ops.support_replay_runs (ticket_id);');

        // ---------------- Grants ----------------
        // ops.* is global but only ops-role members + georag_app
        // (for cockpit reads) get access. Application-level role
        // enforcement gates the cockpit UI per §25.3.
        DB::statement('GRANT USAGE ON SCHEMA ops TO georag_app;');
        DB::statement('GRANT SELECT, INSERT, UPDATE, DELETE
                       ON ALL TABLES IN SCHEMA ops TO georag_app;');
        DB::statement('GRANT USAGE, SELECT
                       ON ALL SEQUENCES IN SCHEMA ops TO georag_app;');
    }

    public function down(): void
    {
        DB::statement('DROP SCHEMA IF EXISTS ops CASCADE;');
    }
};
