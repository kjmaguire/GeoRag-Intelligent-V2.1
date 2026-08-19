<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Sibling to 2026_05_14_140100 (workflow schema) / 140400 (usage schema) —
 * provision the Phase 4/6 raw-SQL objects the Laravel test DB was missing.
 *
 * 2026-08-17 CI-gap audit: `tests/Feature/Admin/IntegrationsControllerTest`
 * is (and always was) listed in `phpunit.pgsql.xml`'s allowlist, but CI
 * never actually invoked `phpunit.pgsql.xml` (see that file's own header —
 * `docs/RUNBOOK.md` → "Test environment gotchas" — and `.github/workflows/
 * ci.yml`'s `laravel` job), so this gap went undetected: `workflow.
 * flow_registry`, `workflow.flow_jwt_keys`, and `usage.
 * external_notification_senders` are created by raw SQL apply
 * (database/raw/phase4/10-external-notification-senders.sql,
 * database/raw/phase4/20-flow-registry-table.sql,
 * database/raw/phase6/10-flow-jwt-keys-multikid.sql) which production DBs
 * get but the Laravel test DB (migrate:fresh only) never did.
 *
 * Mirrors the FINAL shape (post phase4→phase5→phase6) directly rather than
 * replaying each historical ALTER — this is a fresh test DB, there's
 * nothing to migrate forward from.
 *
 * `CREATE ... IF NOT EXISTS` / `CREATE OR REPLACE` throughout — no-op on
 * production where the raw SQL already ran.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // pgp_sym_encrypt/decrypt (sender + JWT-key secret storage) need
        // pgcrypto. gen_random_uuid() is core since PG13 and already works
        // without it, but the pgp_sym_* functions do not.
        DB::statement('CREATE EXTENSION IF NOT EXISTS pgcrypto');

        // ───────────────────────── workflow.flow_registry ───────────────
        // database/raw/phase4/20-flow-registry-table.sql, columns extended
        // in-place with database/raw/phase5/20's jwt_secret_kid/ciphertext
        // rather than replaying the ALTER separately.
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS workflow.flow_registry (
                flow_name               text        PRIMARY KEY,
                kind                    text        NOT NULL,
                description             text        NOT NULL,
                hatchet_workflow_module text        NOT NULL,
                hatchet_workflow_attr   text        NOT NULL,
                pydantic_input_attr     text        NOT NULL,
                flag_name               text        NULL,
                enabled                 boolean     NOT NULL DEFAULT true,
                jwt_secret_kid          text        NULL,
                jwt_secret_ciphertext   bytea       NULL,
                created_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
                updated_at              timestamptz NOT NULL DEFAULT clock_timestamp(),
                CONSTRAINT flow_registry_kind_check CHECK (
                    kind IN ('scheduled-import', 'inbound-webhook', 'placeholder', 'agent-trigger')
                ),
                CONSTRAINT flow_registry_flag_name_format CHECK (
                    flag_name IS NULL OR flag_name ~ '^flows\.[a-z0-9_]+\.enabled$'
                )
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS flow_registry_enabled_idx ON workflow.flow_registry (enabled)');

        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION workflow.flow_registry_touch_updated_at()
                RETURNS trigger
                LANGUAGE plpgsql
            AS $body$
            BEGIN
                NEW.updated_at := clock_timestamp();
                RETURN NEW;
            END;
            $body$
        SQL);
        DB::statement('DROP TRIGGER IF EXISTS flow_registry_touch_updated_at ON workflow.flow_registry');
        DB::statement(<<<'SQL'
            CREATE TRIGGER flow_registry_touch_updated_at
                BEFORE UPDATE ON workflow.flow_registry
                FOR EACH ROW EXECUTE FUNCTION workflow.flow_registry_touch_updated_at()
        SQL);

        DB::statement('GRANT SELECT, INSERT, UPDATE ON workflow.flow_registry TO georag_app');

        // Seed: mirror the three currently-hard-coded flows (same as prod).
        DB::table('workflow.flow_registry')->insertOrIgnore([
            [
                'flow_name' => 'phase2_smoke',
                'kind' => 'placeholder',
                'description' => 'Connectivity-debug echo workflow. Triggerable for ops smoke; not driven by any Kestra flow.',
                'hatchet_workflow_module' => 'app.hatchet_workflows.phase2_smoke',
                'hatchet_workflow_attr' => 'phase2_smoke',
                'pydantic_input_attr' => 'Phase2SmokeInput',
                'flag_name' => null,
                'enabled' => true,
            ],
            [
                'flow_name' => 'public_geoscience_pull',
                'kind' => 'scheduled-import',
                'description' => 'Cron pulls a public geoscience feed → S3 → records bronze.provenance.',
                'hatchet_workflow_module' => 'app.hatchet_workflows.public_geoscience_pull',
                'hatchet_workflow_attr' => 'public_geoscience_pull',
                'pydantic_input_attr' => 'PublicGeoSciencePullInput',
                'flag_name' => 'flows.public_geoscience_pull.enabled',
                'enabled' => true,
            ],
            [
                'flow_name' => 'external_notification',
                'kind' => 'inbound-webhook',
                'description' => 'External sender posts to an orchestrator webhook → idempotent record in audit_ledger.',
                'hatchet_workflow_module' => 'app.hatchet_workflows.external_notification',
                'hatchet_workflow_attr' => 'external_notification',
                'pydantic_input_attr' => 'ExternalNotificationInput',
                'flag_name' => 'flows.external_notification.enabled',
                'enabled' => true,
            ],
        ]);

        // ─────────────────────── workflow.flow_jwt_keys ─────────────────
        // database/raw/phase6/10-flow-jwt-keys-multikid.sql — final shape,
        // created directly rather than replaying phase5's single-kid columns.
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS workflow.flow_jwt_keys (
                id           uuid           PRIMARY KEY DEFAULT gen_random_uuid(),
                flow_name    text           NOT NULL REFERENCES workflow.flow_registry(flow_name) ON DELETE CASCADE,
                kid          text           NOT NULL,
                ciphertext   bytea          NOT NULL,
                valid_from   timestamptz    NOT NULL DEFAULT clock_timestamp(),
                valid_until  timestamptz    NULL,
                created_at   timestamptz    NOT NULL DEFAULT clock_timestamp(),
                CONSTRAINT flow_jwt_keys_unique_kid UNIQUE (flow_name, kid),
                CONSTRAINT flow_jwt_keys_window_check
                    CHECK (valid_until IS NULL OR valid_until > valid_from)
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS flow_jwt_keys_flow_active_idx
                       ON workflow.flow_jwt_keys (flow_name, valid_from DESC)
                       WHERE valid_until IS NULL');
        DB::statement('CREATE INDEX IF NOT EXISTS flow_jwt_keys_flow_window_idx
                       ON workflow.flow_jwt_keys (flow_name, valid_from, valid_until)');

        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION workflow.set_flow_jwt_secret(
                p_flow_name     text,
                p_secret_kid    text,
                p_secret_plain  text,
                p_overlap_hours int DEFAULT 0
            ) RETURNS void
                LANGUAGE plpgsql
                SECURITY DEFINER
                SET search_path = workflow, public, pg_catalog
            AS $body$
            DECLARE
                enc_key text := current_setting('app.audit_encryption_key', true);
                n_existing int;
            BEGIN
                IF enc_key IS NULL OR enc_key = '' THEN
                    RAISE EXCEPTION 'app.audit_encryption_key GUC not set';
                END IF;
                IF p_secret_kid = '' OR p_secret_plain = '' THEN
                    RAISE EXCEPTION 'secret_kid + secret_plaintext required';
                END IF;
                IF NOT EXISTS (SELECT 1 FROM workflow.flow_registry WHERE flow_name = p_flow_name) THEN
                    RAISE EXCEPTION 'unknown flow_name: %', p_flow_name;
                END IF;
                IF p_overlap_hours < 0 THEN
                    RAISE EXCEPTION 'overlap_hours must be >= 0, got %', p_overlap_hours;
                END IF;

                UPDATE workflow.flow_jwt_keys
                   SET valid_until = clock_timestamp() + make_interval(hours => p_overlap_hours)
                 WHERE flow_name = p_flow_name
                   AND valid_until IS NULL;

                INSERT INTO workflow.flow_jwt_keys (flow_name, kid, ciphertext, valid_from, valid_until)
                VALUES (
                    p_flow_name,
                    p_secret_kid,
                    pgp_sym_encrypt(p_secret_plain, enc_key)::bytea,
                    clock_timestamp(),
                    NULL
                )
                ON CONFLICT (flow_name, kid)
                DO UPDATE SET
                    ciphertext = EXCLUDED.ciphertext,
                    valid_from = EXCLUDED.valid_from,
                    valid_until = NULL;

                UPDATE workflow.flow_registry
                   SET jwt_secret_kid        = p_secret_kid,
                       jwt_secret_ciphertext = pgp_sym_encrypt(p_secret_plain, enc_key)::bytea,
                       updated_at            = clock_timestamp()
                 WHERE flow_name = p_flow_name;

                GET DIAGNOSTICS n_existing = ROW_COUNT;
                IF n_existing = 0 THEN
                    RAISE EXCEPTION 'flow_registry update unexpectedly affected 0 rows';
                END IF;
            END;
            $body$
        SQL);
        DB::statement('GRANT EXECUTE ON FUNCTION workflow.set_flow_jwt_secret(text, text, text, int) TO georag_app');

        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION workflow.get_flow_jwt_secret(p_flow_name text)
            RETURNS TABLE (kid text, plain text)
                LANGUAGE plpgsql
                SECURITY DEFINER
                SET search_path = workflow, public, pg_catalog
            AS $body$
            DECLARE
                enc_key text := current_setting('app.audit_encryption_key', true);
            BEGIN
                IF enc_key IS NULL OR enc_key = '' THEN
                    RAISE EXCEPTION 'app.audit_encryption_key GUC not set';
                END IF;
                RETURN QUERY
                    SELECT k.kid,
                           pgp_sym_decrypt(k.ciphertext, enc_key)
                      FROM workflow.flow_jwt_keys k
                     WHERE k.flow_name = p_flow_name
                       AND k.valid_from <= clock_timestamp()
                       AND (k.valid_until IS NULL OR k.valid_until > clock_timestamp())
                     ORDER BY k.valid_from DESC
                     LIMIT 1;
            END;
            $body$
        SQL);
        DB::statement('GRANT EXECUTE ON FUNCTION workflow.get_flow_jwt_secret(text) TO georag_app');

        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION workflow.get_flow_jwt_keys(p_flow_name text)
            RETURNS TABLE (kid text, plain text, valid_until timestamptz)
                LANGUAGE plpgsql
                SECURITY DEFINER
                SET search_path = workflow, public, pg_catalog
            AS $body$
            DECLARE
                enc_key text := current_setting('app.audit_encryption_key', true);
            BEGIN
                IF enc_key IS NULL OR enc_key = '' THEN
                    RAISE EXCEPTION 'app.audit_encryption_key GUC not set';
                END IF;
                RETURN QUERY
                    SELECT k.kid,
                           pgp_sym_decrypt(k.ciphertext, enc_key),
                           k.valid_until
                      FROM workflow.flow_jwt_keys k
                     WHERE k.flow_name = p_flow_name
                       AND k.valid_from <= clock_timestamp()
                       AND (k.valid_until IS NULL OR k.valid_until > clock_timestamp())
                     ORDER BY k.valid_from DESC;
            END;
            $body$
        SQL);
        DB::statement('GRANT EXECUTE ON FUNCTION workflow.get_flow_jwt_keys(text) TO georag_app');

        // ─────────────── usage.external_notification_senders ────────────
        // database/raw/phase4/10-external-notification-senders.sql.
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS usage.external_notification_senders (
                id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
                source            text        NOT NULL,
                secret_kid        text        NOT NULL,
                secret_ciphertext bytea       NOT NULL,
                description       text        NULL,
                created_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
                rotated_from_id   uuid        NULL,
                disabled_at       timestamptz NULL,
                last_seen_at      timestamptz NULL,
                CONSTRAINT external_notification_senders_source_kid_unique UNIQUE (source, secret_kid),
                CONSTRAINT external_notification_senders_rotated_from_fkey
                    FOREIGN KEY (rotated_from_id)
                    REFERENCES usage.external_notification_senders(id)
                    ON DELETE SET NULL
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS external_notification_senders_source_idx
                       ON usage.external_notification_senders (source)
                       WHERE disabled_at IS NULL');
        DB::statement('CREATE INDEX IF NOT EXISTS external_notification_senders_disabled_idx
                       ON usage.external_notification_senders (disabled_at)');

        DB::statement('ALTER TABLE usage.external_notification_senders ENABLE ROW LEVEL SECURITY');
        DB::statement('ALTER TABLE usage.external_notification_senders FORCE  ROW LEVEL SECURITY');

        DB::statement('DROP POLICY IF EXISTS senders_admin_read ON usage.external_notification_senders');
        DB::statement(<<<'SQL'
            CREATE POLICY senders_admin_read ON usage.external_notification_senders
                FOR SELECT
                USING (true)
        SQL);

        DB::statement('DROP POLICY IF EXISTS senders_app_insert_update ON usage.external_notification_senders');
        DB::statement(<<<'SQL'
            CREATE POLICY senders_app_insert_update ON usage.external_notification_senders
                FOR ALL
                USING (true)
                WITH CHECK (true)
        SQL);

        DB::statement('GRANT SELECT, INSERT, UPDATE ON usage.external_notification_senders TO georag_app');

        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION usage.register_external_notification_sender(
                p_source       text,
                p_secret_kid   text,
                p_secret_plain text,
                p_description  text DEFAULT NULL,
                p_supersedes   uuid DEFAULT NULL
            ) RETURNS uuid
                LANGUAGE plpgsql
                SECURITY DEFINER
                SET search_path = usage, public, pg_catalog
            AS $body$
            DECLARE
                enc_key text := current_setting('app.audit_encryption_key', true);
                new_id  uuid;
            BEGIN
                IF enc_key IS NULL OR enc_key = '' THEN
                    RAISE EXCEPTION 'app.audit_encryption_key GUC not set — cannot encrypt sender secret';
                END IF;
                IF p_source = '' OR p_secret_kid = '' OR p_secret_plain = '' THEN
                    RAISE EXCEPTION 'source, secret_kid, secret_plaintext are required';
                END IF;

                INSERT INTO usage.external_notification_senders
                    (source, secret_kid, secret_ciphertext, description, rotated_from_id)
                VALUES (
                    p_source,
                    p_secret_kid,
                    pgp_sym_encrypt(p_secret_plain, enc_key)::bytea,
                    p_description,
                    p_supersedes
                )
                RETURNING id INTO new_id;

                RETURN new_id;
            END;
            $body$
        SQL);
        DB::statement('GRANT EXECUTE ON FUNCTION usage.register_external_notification_sender(text, text, text, text, uuid) TO georag_app');

        DB::statement(<<<'SQL'
            CREATE OR REPLACE FUNCTION usage.lookup_external_notification_sender_secrets(
                p_source text
            ) RETURNS TABLE (sender_id uuid, secret_kid text, secret_plain text)
                LANGUAGE plpgsql
                SECURITY DEFINER
                SET search_path = usage, public, pg_catalog
            AS $body$
            DECLARE
                enc_key text := current_setting('app.audit_encryption_key', true);
            BEGIN
                IF enc_key IS NULL OR enc_key = '' THEN
                    RAISE EXCEPTION 'app.audit_encryption_key GUC not set — cannot decrypt sender secret';
                END IF;

                RETURN QUERY
                    SELECT id,
                           s.secret_kid,
                           pgp_sym_decrypt(s.secret_ciphertext, enc_key)
                      FROM usage.external_notification_senders s
                     WHERE s.source = p_source
                       AND s.disabled_at IS NULL
                     ORDER BY s.created_at DESC;
            END;
            $body$
        SQL);
        DB::statement('GRANT EXECUTE ON FUNCTION usage.lookup_external_notification_sender_secrets(text) TO georag_app');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP FUNCTION IF EXISTS usage.lookup_external_notification_sender_secrets(text)');
        DB::statement('DROP FUNCTION IF EXISTS usage.register_external_notification_sender(text, text, text, text, uuid)');
        DB::statement('DROP TABLE IF EXISTS usage.external_notification_senders CASCADE');

        DB::statement('DROP FUNCTION IF EXISTS workflow.get_flow_jwt_keys(text)');
        DB::statement('DROP FUNCTION IF EXISTS workflow.get_flow_jwt_secret(text)');
        DB::statement('DROP FUNCTION IF EXISTS workflow.set_flow_jwt_secret(text, text, text, int)');
        DB::statement('DROP TABLE IF EXISTS workflow.flow_jwt_keys CASCADE');

        DB::statement('DROP TRIGGER IF EXISTS flow_registry_touch_updated_at ON workflow.flow_registry');
        DB::statement('DROP FUNCTION IF EXISTS workflow.flow_registry_touch_updated_at()');
        DB::statement('DROP TABLE IF EXISTS workflow.flow_registry CASCADE');
    }
};
