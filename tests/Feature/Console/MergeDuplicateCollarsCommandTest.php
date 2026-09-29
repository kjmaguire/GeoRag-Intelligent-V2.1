<?php

declare(strict_types=1);

namespace Tests\Feature\Console;

use App\Services\Collars\CanonicalHoleIdIndex;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * UNIQUE (project_id, hole_id_canonical) on silver.collars and the ghost
 * merge that makes it buildable (§04e, SME-approved, Kyle, 2026-09-29).
 *
 * Ghosts are reproduced the way production got them: the old writers stored
 * a raw hole id (cameco_log_ingester) or NULL (the collar API) in
 * hole_id_canonical. The fixture drops the full index and disables the
 * deriving trigger for its INSERTs — all inside the test's transaction.
 */
final class MergeDuplicateCollarsCommandTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private const OLDEST = 'aaaaaaaa-0000-0000-0000-000000000001';

    private const NEWER = 'aaaaaaaa-0000-0000-0000-000000000002';

    private string $workspaceId;

    private string $projectId;

    protected function setUp(): void
    {
        parent::setUp();

        $this->workspaceId = (string) Str::uuid();
        $this->projectId = (string) Str::uuid();
        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug) VALUES (?::uuid, ?, ?)',
            [$this->workspaceId, 'merge-ws', 'merge-'.substr($this->workspaceId, 0, 8)],
        );
        DB::statement(
            'INSERT INTO silver.projects (project_id, project_name, slug, workspace_id) VALUES (?::uuid, ?, ?, ?::uuid)',
            [$this->projectId, 'merge-project', 'merge-'.substr($this->projectId, 0, 8), $this->workspaceId],
        );
    }

    private function seedGhosts(): void
    {
        DB::statement('DROP INDEX IF EXISTS silver.'.CanonicalHoleIdIndex::INDEX);
        DB::statement(
            'CREATE UNIQUE INDEX IF NOT EXISTS '.CanonicalHoleIdIndex::LEGACY_PARTIAL_INDEX
            .' ON silver.collars (project_id, hole_id_canonical) WHERE hole_id_canonical IS NOT NULL',
        );
        DB::statement('ALTER TABLE silver.collars DISABLE TRIGGER trg_collars_hole_id_canonical');
        // OLDEST: a .log collar — raw hole id as "canonical", no total depth.
        // NEWER:  the collar-table spelling — real canonical, depth + dip.
        DB::statement(
            "INSERT INTO silver.collars (collar_id, hole_id, hole_id_canonical, project_id, workspace_id,
                    easting, northing, total_depth, dip, hole_type, status, created_at, updated_at)
             VALUES (?::uuid, 'SRE09-6', 'SRE09-6', ?::uuid, ?::uuid, 1, 2, NULL, NULL, 'x', 'y', now() - interval '2 days', now()),
                    (?::uuid, 'SRE09_6', 'SRE096', ?::uuid, ?::uuid, 1, 2, 150, -60, 'x', 'y', now() - interval '1 day', now())",
            [self::OLDEST, $this->projectId, $this->workspaceId, self::NEWER, $this->projectId, $this->workspaceId],
        );
        DB::statement('ALTER TABLE silver.collars ENABLE TRIGGER trg_collars_hole_id_canonical');

        foreach ([self::OLDEST => '{1,2}', self::NEWER => '{3,4}'] as $collarId => $values) {
            DB::statement(
                "INSERT INTO silver.well_log_curves (curve_id, collar_id, workspace_id, curve_name, min_depth, max_depth,
                        sample_count, depths, \"values\")
                 VALUES (?::uuid, ?::uuid, ?::uuid, 'GR', 0, 10, 2, '{0,10}', ?::float8[])",
                [(string) Str::uuid(), $collarId, $this->workspaceId, $values],
            );
        }
        DB::statement(
            "INSERT INTO silver.surveys (survey_id, collar_id, workspace_id, depth, azimuth, dip, survey_method)
             VALUES (?::uuid, ?::uuid, ?::uuid, 100, 90, -61, 'Gyro')",
            [(string) Str::uuid(), self::NEWER, $this->workspaceId],
        );
    }

    private function collarCount(): int
    {
        return (int) DB::table('silver.collars')->where('project_id', $this->projectId)->count();
    }

    private function indexExists(): bool
    {
        return (bool) DB::selectOne(
            'SELECT to_regclass(?) IS NOT NULL AS present',
            ['silver.'.CanonicalHoleIdIndex::INDEX],
        )->present;
    }

    public function test_a_fresh_database_gets_the_unique_index(): void
    {
        $this->assertTrue($this->indexExists());
        $this->assertSame(
            CanonicalHoleIdIndex::STATUS_EXISTS,
            app(CanonicalHoleIdIndex::class)->ensure()['status'],
        );
    }

    public function test_the_trigger_derives_the_canonical_key_whatever_the_writer_sends(): void
    {
        DB::statement(
            "INSERT INTO silver.collars (collar_id, hole_id, hole_id_canonical, project_id, workspace_id,
                    easting, northing, hole_type, status)
             VALUES (gen_random_uuid(), ' leb-23/001 ', 'garbage', ?::uuid, ?::uuid, 1, 2, 'x', 'y')",
            [$this->projectId, $this->workspaceId],
        );

        $this->assertSame(
            'LEB23001',
            DB::table('silver.collars')->where('project_id', $this->projectId)->value('hole_id_canonical'),
        );
    }

    public function test_with_ghosts_present_the_index_is_skipped_not_failed(): void
    {
        $this->seedGhosts();

        $state = app(CanonicalHoleIdIndex::class)->ensure();

        $this->assertSame(CanonicalHoleIdIndex::STATUS_SKIPPED_DUPLICATES, $state['status']);
        $this->assertSame(1, $state['duplicate_groups']);
        $this->assertSame(2, $state['duplicate_collars']);
        $this->assertStringContainsString('collars:merge-duplicates', $state['message']);
        $this->assertFalse($this->indexExists());
    }

    public function test_the_default_is_a_dry_run_that_changes_nothing(): void
    {
        $this->seedGhosts();

        $this->artisan('collars:merge-duplicates')
            ->expectsOutputToContain('DRY RUN')
            ->expectsOutputToContain('1 group(s); 1 ghost collar(s) would be deleted')
            ->assertSuccessful();

        $this->assertSame(2, $this->collarCount());
        $this->assertFalse($this->indexExists());
    }

    public function test_execute_merges_into_the_oldest_collar_and_builds_the_index(): void
    {
        $this->seedGhosts();

        $this->artisan('collars:merge-duplicates', ['--execute' => true])
            ->expectsOutputToContain('EXECUTED')
            ->assertSuccessful();

        $survivor = DB::table('silver.collars')->where('project_id', $this->projectId)->get();
        $this->assertCount(1, $survivor);
        $this->assertSame(self::OLDEST, $survivor[0]->collar_id, 'the oldest collar survives');
        $this->assertSame('SRE09-6', $survivor[0]->hole_id, 'its spelling is kept');
        $this->assertSame('SRE096', $survivor[0]->hole_id_canonical);
        $this->assertEquals(150.0, $survivor[0]->total_depth, 'a NULL on the survivor is filled from the ghost');
        $this->assertEquals(-60.0, $survivor[0]->dip);

        // Children re-pointed; the ghost's GR curve collided with the
        // survivor's own (collar_id, curve_name) and was dropped.
        $this->assertSame(1, DB::table('silver.surveys')->where('collar_id', self::OLDEST)->count());
        $curves = DB::table('silver.well_log_curves')->where('collar_id', self::OLDEST)->pluck('values')->all();
        $this->assertSame(['{1,2}'], $curves);

        $this->assertTrue($this->indexExists());
    }

    public function test_dry_run_and_execute_together_are_refused(): void
    {
        $this->artisan('collars:merge-duplicates', ['--dry-run' => true, '--execute' => true])
            ->assertExitCode(2);
    }
}
