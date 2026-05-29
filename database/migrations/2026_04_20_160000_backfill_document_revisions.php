<?php

/**
 * B8.4 — EVID: Backfill silver.document_revisions from existing silver.reports rows.
 *
 * Module 3 Phase B 2026-04-20.  DRAFT — senior-reviewer (Opus) must approve
 * before php artisan migrate is run.
 *
 * Context
 * -------
 * silver.reports contains 1 row (confirmed by audit query 2026-04-20):
 *   report_id: 44a67709-b846-42ec-a361-9faa6e224170
 *   title:     "NI 43-101 Technical Report"
 *   created_at: NULL (not set at ingest time — legacy gap)
 *
 * This migration seeds one document_revisions row for that existing report.
 * Because no bronze.provenance row exists that links to this report_id (the
 * bronze_provenance table was created after this report was ingested), the
 * source_uri and source_sha256 cannot be recovered from provenance metadata.
 * Legacy sentinel values are used instead, matching the same convention that
 * bronze.provenance uses for pre-hardening rows: parser_name = the string
 * 'legacy-pre-2026-04-20', parser_version = 'unknown'.
 *
 * source_sha256 is set to the all-zeros sentinel (64 × '0') rather than a
 * computed hash, because the original Bronze object path cannot be determined
 * from the current silver row alone.  The sentinel is detectable and filterable
 * in any query that checks for genuine content hashes.
 *
 * The default workspace (a0000000-0000-0000-0000-000000000001) is used for
 * workspace_id.  All existing projects were backfilled to this workspace in
 * migration 2026_04_20_100000.
 *
 * Rollback
 * --------
 * DELETE the seeded rows.  Because document_revisions.source_sha256 uses the
 * all-zeros sentinel, the down() can identify and remove exactly the backfilled
 * rows without risk of deleting legitimately-ingested revision rows.
 *
 * NOT in this migration
 * ---------------------
 * - answer_citation_items backfill (Module 6 scope — 0 rows exist)
 * - evidence_items population for these reports (B8.5 / B8.6 scope)
 * - Any changes to silver.reports itself
 */

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

return new class extends Migration
{
    /**
     * Default workspace UUID — seeded in migration 2026_04_20_100000.
     * Must not change once seeded (recorded in workspaces migration comment).
     */
    private string $defaultWorkspaceId = 'a0000000-0000-0000-0000-000000000001';

    /**
     * Sentinel sha256 used when the original Bronze object hash is not
     * recoverable.  64 lowercase hex zeros — visually distinct from any real
     * SHA-256 value.
     */
    private string $legacySentinelSha256 = '0000000000000000000000000000000000000000000000000000000000000000';

    public function up(): void
    {
        // Count rows to decide whether backfill is needed.
        // Uses a raw query so the migration is not coupled to any Eloquent model.
        $reportCount = DB::selectOne('SELECT COUNT(*) AS cnt FROM silver.reports')->cnt;

        if ((int) $reportCount === 0) {
            // No documents exist — backfill is a no-op.  Note in migration log.
            DB::statement("DO $$ BEGIN RAISE NOTICE 'B8.4 backfill: silver.reports is empty — no document_revisions rows seeded.'; END $$");
            return;
        }

        // Insert one document_revisions row per silver.reports row that does
        // not already have a revision row.  ON CONFLICT on (document_id, revision_number)
        // makes this idempotent — safe to re-run if the migration partially failed.
        //
        // source_uri uses the legacy sentinel path pattern so downstream tooling
        // can distinguish pre-provenance rows from real Bronze URIs.
        //
        // ingested_at: COALESCE(r.created_at AT TIME ZONE 'UTC', NOW()).
        // The NI 43-101 row has created_at = NULL so NOW() is used (migration
        // run timestamp — best available approximation).  AT TIME ZONE 'UTC'
        // interprets the TIMESTAMP WITHOUT TIME ZONE column value as UTC
        // deterministically, regardless of the session's TIMEZONE setting.
        // (::timestamptz would use the session TZ, which can vary across envs.)
        //
        // Named placeholders are used for the sentinel constants — consistent
        // with Laravel convention even though these are class-private constants
        // and not user-supplied input (not an injection risk either way).
        DB::statement(
            "INSERT INTO silver.document_revisions (
                document_revision_id,
                document_id,
                workspace_id,
                revision_number,
                source_uri,
                source_sha256,
                ingested_at,
                parser_name,
                parser_version,
                superseded_by_revision_id,
                created_at
            )
            SELECT
                gen_random_uuid()                                AS document_revision_id,
                r.report_id                                      AS document_id,
                :ws::uuid                                        AS workspace_id,
                1                                                AS revision_number,
                'bronze://legacy-pre-2026-04-20/' || r.report_id::text
                                                                 AS source_uri,
                :sha                                             AS source_sha256,
                COALESCE(r.created_at AT TIME ZONE 'UTC', NOW()) AS ingested_at,
                'legacy-pre-2026-04-20'                          AS parser_name,
                'unknown'                                        AS parser_version,
                NULL                                             AS superseded_by_revision_id,
                NOW()                                            AS created_at
            FROM silver.reports r
            WHERE NOT EXISTS (
                SELECT 1
                FROM silver.document_revisions dr
                WHERE dr.document_id = r.report_id
                  AND dr.revision_number = 1
            )
            ON CONFLICT (document_id, revision_number) DO NOTHING",
            [
                ':ws'  => $this->defaultWorkspaceId,
                ':sha' => $this->legacySentinelSha256,
            ]
        );

        $seededCount = DB::selectOne(
            'SELECT COUNT(*) AS cnt
             FROM silver.document_revisions
             WHERE source_sha256 = :sha',
            [':sha' => $this->legacySentinelSha256]
        )->cnt;

        DB::statement("DO \$\$ BEGIN RAISE NOTICE 'B8.4 backfill: seeded % document_revisions rows from silver.reports.', {$seededCount}; END \$\$");
    }

    public function down(): void
    {
        // Remove only the legacy-sentinel rows inserted by this migration.
        // Real revision rows (source_sha256 != all-zeros) are untouched.
        DB::statement(
            'DELETE FROM silver.document_revisions
              WHERE source_sha256 = :sha
                AND parser_name   = :name',
            [
                ':sha'  => $this->legacySentinelSha256,
                ':name' => 'legacy-pre-2026-04-20',
            ]
        );
    }
};
