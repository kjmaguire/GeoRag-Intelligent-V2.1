<?php

declare(strict_types=1);

namespace Tests\Feature\Console;

use App\Models\QueryAuditLog;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Regression for database audit 2026-09-29 PG-4.
 *
 * RestoreAuditPii, EncryptExistingAuditPii and GoldenSetReport used
 * `DB::table('query_audit_log')`. The table is audit.query_audit_log, and
 * production's pgsql connection runs with search_path=public (nothing in
 * terraform or compose sets DB_SEARCH_PATH), so every one of them died with
 * `relation "query_audit_log" does not exist`. The APP_KEY rotation script
 * runs audit:restore-pii, so a rotation could never complete.
 *
 * The rest of the suite cannot see this: SQLite strips schema prefixes and
 * phpunit.pgsql.xml forces a DB_SEARCH_PATH that includes `audit`. So this
 * test narrows search_path back to production's `public` for its own
 * transaction before running the commands.
 */
final class AuditPiiCommandsUnderPublicSearchPathTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    protected function setUp(): void
    {
        parent::setUp();

        // RefreshDatabase has opened the test transaction; SET LOCAL lasts
        // until its rollback and does not leak into other tests.
        DB::statement('SET LOCAL search_path TO public');
        $this->assertSame('public', DB::selectOne('SHOW search_path')->search_path);
    }

    public function test_dump_then_restore_round_trips_with_production_search_path(): void
    {
        $first = $this->seedRow('Alpha-7 grade?', 'Alpha-7 runs 3.2 g/t Au [DATA-1]');
        $second = $this->seedRow('Beta-2 depth?', 'Beta-2 is 410 m deep [DATA-2]');

        $dumpPath = sys_get_temp_dir().'/audit-pii-public-'.Str::uuid().'.jsonl';
        register_shutdown_function(fn () => @unlink($dumpPath));

        $this->artisan('audit:dump-pii', ['--output' => $dumpPath])
            ->expectsOutputToContain('Dumped 2 rows')
            ->assertSuccessful();

        DB::table((new QueryAuditLog)->getTable())
            ->whereIn('audit_id', [$first->audit_id, $second->audit_id])
            ->update([
                'query_text' => 'CORRUPTED_CIPHERTEXT_SIMULATION',
                'response_text' => 'CORRUPTED_CIPHERTEXT_SIMULATION',
                'query_text_hash' => null,
            ]);

        $this->artisan('audit:restore-pii', ['--input' => $dumpPath])
            ->expectsOutputToContain('Restored 2 rows')
            ->assertSuccessful();

        $this->assertSame('Alpha-7 grade?', QueryAuditLog::find($first->audit_id)->query_text);
        $this->assertSame('Beta-2 is 410 m deep [DATA-2]', QueryAuditLog::find($second->audit_id)->response_text);
        $this->assertNotNull(QueryAuditLog::find($first->audit_id)->query_text_hash);
    }

    public function test_encrypt_existing_dry_run_reads_the_audit_table(): void
    {
        $this->seedRow('Gamma-3 tonnage?', 'Gamma-3 has 1.2 Mt [DATA-3]');

        $this->artisan('audit:encrypt-pii', ['--dry-run' => true])
            ->assertSuccessful();
    }

    public function test_golden_set_report_reads_the_audit_table(): void
    {
        for ($i = 0; $i < 3; $i++) {
            $row = $this->seedRow('which hole is deepest?', 'H2 [DATA-4]');
            $row->confidence = 0.2;
            $row->save();
        }

        $outputPath = sys_get_temp_dir().'/golden-public-'.Str::uuid().'.md';
        register_shutdown_function(fn () => @unlink($outputPath));

        $this->artisan('audit:golden-set-report', [
            '--since' => '1d',
            '--min-count' => 2,
            '--output' => $outputPath,
        ])->assertSuccessful();

        $this->assertStringContainsString('which hole is deepest?', (string) file_get_contents($outputPath));
    }

    private function seedRow(string $queryText, string $responseText): QueryAuditLog
    {
        $row = QueryAuditLog::create([
            'user_id' => null,
            'project_id' => (string) Str::uuid(),
            'query_id' => (string) Str::uuid(),
            'query_text' => $queryText,
            'ip_address' => '127.0.0.1',
            'llm_model' => 'test-model',
        ]);
        $row->response_text = $responseText;
        $row->save();

        return $row->fresh();
    }
}
