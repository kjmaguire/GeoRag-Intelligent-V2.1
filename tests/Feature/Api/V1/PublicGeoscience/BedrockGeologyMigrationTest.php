<?php

namespace Tests\Feature\Api\V1\PublicGeoscience;

use Illuminate\Foundation\Testing\DatabaseTransactions;
use Illuminate\Support\Facades\DB;
use PHPUnit\Framework\Attributes\DataProvider;
use Tests\TestCase;

/**
 * Smoke tests for the 2026_04_18_170000_create_pg_bedrock_geology_tables migration.
 *
 * Validates:
 * - pg_bedrock_geology table exists with expected columns
 * - pg_bedrock_geology_history table exists
 * - v_pg_bedrock_geology_mvt view exists
 * - sources.canonical_type CHECK constraint accepts 'bedrock_geology'
 * - sources.canonical_type CHECK constraint still accepts prior types
 *
 * These tests run against a real Postgres test connection (not SQLite).
 * Run with:
 *     php artisan test --filter BedrockGeologyMigrationTest
 */
class BedrockGeologyMigrationTest extends TestCase
{
    use DatabaseTransactions;

    protected function setUp(): void
    {
        parent::setUp();
        $this->skipIfSqlite();
    }

    // ── pg_bedrock_geology table ─────────────────────────────────────────────

    public function test_pg_bedrock_geology_table_exists(): void
    {
        $exists = DB::selectOne("
            SELECT 1
              FROM information_schema.tables
             WHERE table_schema = 'public_geoscience'
               AND table_name   = 'pg_bedrock_geology'
        ");

        $this->assertNotNull($exists, 'Table public_geoscience.pg_bedrock_geology does not exist.');
    }

    #[DataProvider('bedrockGeologyColumnProvider')]
    public function test_pg_bedrock_geology_has_expected_column(string $column): void
    {
        $row = DB::selectOne("
            SELECT column_name
              FROM information_schema.columns
             WHERE table_schema = 'public_geoscience'
               AND table_name   = 'pg_bedrock_geology'
               AND column_name  = ?
        ", [$column]);

        $this->assertNotNull(
            $row,
            "Column '$column' missing from public_geoscience.pg_bedrock_geology"
        );
    }

    public static function bedrockGeologyColumnProvider(): array
    {
        return [
            'unit_code'         => ['unit_code'],
            'unit_name'         => ['unit_name'],
            'eon'               => ['eon'],
            'era'               => ['era'],
            'period'            => ['period'],
            'group_name'        => ['group_name'],
            'formation'         => ['formation'],
            'member'            => ['member'],
            'structural_domain' => ['structural_domain'],
            'lithology'         => ['lithology'],
            'scale'             => ['scale'],
            'geom'              => ['geom'],
            'source_id'         => ['source_id'],
            'jurisdiction_code' => ['jurisdiction_code'],
            'checksum'          => ['checksum'],
        ];
    }

    // ── pg_bedrock_geology_history table ─────────────────────────────────────

    public function test_pg_bedrock_geology_history_table_exists(): void
    {
        $exists = DB::selectOne("
            SELECT 1
              FROM information_schema.tables
             WHERE table_schema = 'public_geoscience'
               AND table_name   = 'pg_bedrock_geology_history'
        ");

        $this->assertNotNull(
            $exists,
            'Table public_geoscience.pg_bedrock_geology_history does not exist.'
        );
    }

    public function test_history_table_has_superseded_at_column(): void
    {
        $row = DB::selectOne("
            SELECT column_name
              FROM information_schema.columns
             WHERE table_schema = 'public_geoscience'
               AND table_name   = 'pg_bedrock_geology_history'
               AND column_name  = 'superseded_at'
        ");

        $this->assertNotNull(
            $row,
            "Column 'superseded_at' missing from pg_bedrock_geology_history"
        );
    }

    // ── MVT view ─────────────────────────────────────────────────────────────

    public function test_v_pg_bedrock_geology_mvt_view_exists(): void
    {
        $exists = DB::selectOne("
            SELECT 1
              FROM information_schema.views
             WHERE table_schema = 'public_geoscience'
               AND table_name   = 'v_pg_bedrock_geology_mvt'
        ");

        $this->assertNotNull(
            $exists,
            'View public_geoscience.v_pg_bedrock_geology_mvt does not exist.'
        );
    }

    // ── sources.canonical_type CHECK constraint ───────────────────────────────

    public function test_canonical_type_check_accepts_bedrock_geology(): void
    {
        // The CHECK constraint must not reject 'bedrock_geology'. The simplest
        // verification is asking Postgres to evaluate the constraint expression
        // without actually inserting a row (which would require FK satisfaction).
        // We query the constraint definition and assert it contains the value.
        $row = DB::selectOne("
            SELECT pg_get_constraintdef(oid) AS def
              FROM pg_constraint
             WHERE conname      = 'sources_canonical_type_check'
               AND conrelid     = 'public_geoscience.sources'::regclass
        ");

        $this->assertNotNull($row, "Constraint 'sources_canonical_type_check' not found on public_geoscience.sources");
        $this->assertStringContainsString(
            'bedrock_geology',
            $row->def,
            "sources_canonical_type_check does not include 'bedrock_geology'"
        );
    }

    public function test_canonical_type_check_still_includes_prior_types(): void
    {
        $row = DB::selectOne("
            SELECT pg_get_constraintdef(oid) AS def
              FROM pg_constraint
             WHERE conname  = 'sources_canonical_type_check'
               AND conrelid = 'public_geoscience.sources'::regclass
        ");

        $this->assertNotNull($row, "Constraint 'sources_canonical_type_check' not found.");

        foreach (['mine', 'mineral_occurrence', 'drillhole_collar', 'mineral_disposition'] as $type) {
            $this->assertStringContainsString(
                $type,
                $row->def,
                "sources_canonical_type_check dropped previously-valid type '$type'"
            );
        }
    }
}
