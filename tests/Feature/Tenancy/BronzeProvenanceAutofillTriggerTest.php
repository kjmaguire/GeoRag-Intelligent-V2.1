<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Tests\TestCase;

/**
 * Verifies the BEFORE INSERT trigger
 * ``bronze.provenance_autofill_workspace_id`` added 2026-05-25 in
 * migration 2026_05_25_175601 actually:
 *
 *   1. Auto-populates workspace_id from the target silver row when
 *      the INSERT omits it (covers all existing Dagster + FastAPI
 *      provenance writers without code changes).
 *   2. Leaves an explicitly-supplied workspace_id alone (no overwrite,
 *      no double-write).
 *   3. Leaves workspace_id NULL when the target table is outside the
 *      lookup set (the RLS IS-NULL exemption still keeps the row
 *      visible).
 *
 * Skipped on SQLite — PL/pgSQL is Postgres-only.
 */
final class BronzeProvenanceAutofillTriggerTest extends TestCase
{
    use RefreshDatabase;

    /**
     * @return array{user: User, project: Project, workspace_id: string}
     */
    private function seedProjectMember(): array
    {
        $user = User::factory()->create();

        $workspaceId = (string) Str::uuid();
        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())
             ON CONFLICT (workspace_id) DO NOTHING',
            [$workspaceId, 'autofill-test', 'autofill-test-'.substr($workspaceId, 0, 8)],
        );

        $project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$workspaceId, $project->project_id],
        );
        $user->projects()->syncWithoutDetaching(
            [$project->project_id => ['role' => 'viewer']],
        );

        return ['user' => $user, 'project' => $project, 'workspace_id' => $workspaceId];
    }

    /**
     * Renamed from seedCollarOrSkip — the skip path is gone now that
     * 2026_05_25_184335_provision_silver_workspace_columns_for_test_db
     * adds the column in the test DB. Kept the defensive
     * `markTestSkipped` for the edge case where the migration didn't
     * run (e.g. someone manually rolled it back) so the test still
     * surfaces the gap clearly instead of TypeError-ing on the
     * INSERT.
     */
    private function seedCollar(string $projectId, string $workspaceId): string
    {
        $hasWorkspaceCol = DB::table('information_schema.columns')
            ->where('table_schema', 'silver')
            ->where('table_name', 'collars')
            ->where('column_name', 'workspace_id')
            ->exists();
        if (! $hasWorkspaceCol) {
            $this->markTestSkipped(
                'silver.collars.workspace_id missing — provision migration '.
                '2026_05_25_184335_provision_silver_workspace_columns_for_test_db '.
                'should have added it.',
            );
        }

        $collarId = (string) Str::uuid();
        DB::statement(
            "INSERT INTO silver.collars (
                collar_id, hole_id, project_id, workspace_id,
                easting, northing, elevation, total_depth, azimuth, dip,
                hole_type, status, geom
             ) VALUES (
                ?::uuid, ?, ?::uuid, ?::uuid,
                500000, 4500000, 1000, 150, 180, -60,
                'DDH', 'completed',
                ST_SetSRID(ST_MakePoint(500000, 4500000), 32613)
             )",
            [$collarId, 'AUTOFILL-TEST-001', $projectId, $workspaceId],
        );

        return $collarId;
    }

    /**
     * @param array<string, mixed> $cols
     */
    private function insertProvenance(array $cols): string
    {
        $colNames = implode(', ', array_keys($cols));
        $placeholders = implode(', ', array_fill(0, count($cols), '?'));
        $sql = "INSERT INTO bronze.provenance ({$colNames}) VALUES ({$placeholders}) "
            .'RETURNING provenance_id::text AS id';
        $row = DB::selectOne($sql, array_values($cols));

        return $row->id;
    }

    public function test_trigger_autofills_workspace_id_from_silver_collars(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            $this->markTestSkipped('PL/pgSQL trigger is Postgres-only.');
        }

        ['project' => $project, 'workspace_id' => $workspaceId] = $this->seedProjectMember();
        $collarId = $this->seedCollar($project->project_id, $workspaceId);

        $provId = $this->insertProvenance([
            'target_schema' => 'silver',
            'target_table' => 'collars',
            'target_id' => $collarId,
            'source_file' => '__autofill_test__.csv',
            'source_file_sha256' => str_repeat('a', 64),
            'parser_name' => 'autofill_test',
            'parser_version' => '0.0.0',
        ]);

        $row = DB::selectOne(
            'SELECT workspace_id::text AS ws FROM bronze.provenance WHERE provenance_id = ?::uuid',
            [$provId],
        );

        $this->assertSame(
            $workspaceId, $row->ws,
            'Trigger must derive workspace_id from silver.collars when not supplied',
        );
    }

    public function test_trigger_respects_explicit_workspace_id(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            $this->markTestSkipped('PL/pgSQL trigger is Postgres-only.');
        }

        ['project' => $project, 'workspace_id' => $silverWs] = $this->seedProjectMember();
        $collarId = $this->seedCollar($project->project_id, $silverWs);

        $explicitWs = (string) Str::uuid();
        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())',
            [$explicitWs, 'explicit-ws', 'explicit-ws-'.substr($explicitWs, 0, 8)],
        );

        $provId = $this->insertProvenance([
            'workspace_id' => $explicitWs,
            'target_schema' => 'silver',
            'target_table' => 'collars',
            'target_id' => $collarId,
            'source_file' => '__explicit_ws_test__.csv',
            'source_file_sha256' => str_repeat('b', 64),
            'parser_name' => 'explicit_ws_test',
            'parser_version' => '0.0.0',
        ]);

        $row = DB::selectOne(
            'SELECT workspace_id::text AS ws FROM bronze.provenance WHERE provenance_id = ?::uuid',
            [$provId],
        );

        $this->assertSame(
            $explicitWs, $row->ws,
            'Trigger must NOT overwrite an explicitly-supplied workspace_id',
        );
    }

    public function test_trigger_falls_back_to_default_workspace_for_unknown_target(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            $this->markTestSkipped('PL/pgSQL trigger is Postgres-only.');
        }

        // Updated semantics (2026_05_25_183115 — NOT NULL tightening):
        // when the target table is outside the trigger's CASE, the
        // trigger now falls back to the Default Workspace UUID instead
        // of leaving NULL. Required because workspace_id is now
        // NOT NULL and the policy no longer exempts NULL rows.
        $provId = $this->insertProvenance([
            'target_schema' => 'gold',
            'target_table' => 'drillhole_intervals_visual',
            'target_id' => (string) Str::uuid(),
            'source_file' => '__unknown_target__.csv',
            'source_file_sha256' => str_repeat('c', 64),
            'parser_name' => 'unknown_target',
            'parser_version' => '0.0.0',
        ]);

        $row = DB::selectOne(
            'SELECT workspace_id::text AS ws FROM bronze.provenance WHERE provenance_id = ?::uuid',
            [$provId],
        );

        $this->assertSame(
            'a0000000-0000-0000-0000-000000000001', $row->ws,
            'Trigger must fall back to Default Workspace for unknown target tables '.
            '(previously left NULL, tightened 2026-05-25 when NOT NULL constraint landed)',
        );
    }

    public function test_bronze_workspace_id_columns_are_not_null(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            $this->markTestSkipped('PL/pgSQL trigger is Postgres-only.');
        }

        // Locks the contract from 2026_05_25_183115_tighten_bronze_tenancy_columns_to_not_null.
        // If a future migration loosens these columns, the policy IS NULL
        // exemption needs to come back too — surface both decisions
        // together.
        foreach ([['bronze', 'provenance'], ['bronze', 'ingest_manifest']] as [$schema, $tbl]) {
            $row = DB::selectOne(
                "SELECT is_nullable FROM information_schema.columns
                 WHERE table_schema = ? AND table_name = ? AND column_name = 'workspace_id'",
                [$schema, $tbl],
            );
            $this->assertNotNull($row, "{$schema}.{$tbl}.workspace_id column must exist");
            $this->assertSame(
                'NO', $row->is_nullable,
                "{$schema}.{$tbl}.workspace_id must be NOT NULL — see migration 2026_05_25_183115",
            );
        }
    }
}
