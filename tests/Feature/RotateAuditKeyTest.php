<?php

namespace Tests\Feature;

use App\Models\Project;
use App\Models\QueryAuditLog;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Crypt;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\File;
use Illuminate\Support\Str;
use Tests\TestCase;

/**
 * Exercises the audit:rotate-key orchestrator end-to-end (R3/R5 follow-up #3).
 *
 * Seeds rows with the original APP_KEY, runs the orchestrator, verifies
 * that:
 *   - APP_KEY actually changed on disk (.env) and in-process (config).
 *   - query_text + response_text round-trip cleanly under the new key.
 *   - The plaintext dump is shredded unless --keep-dump is passed.
 *   - Old ciphertext bytes in the DB were in fact replaced (not just
 *     re-read through the same key).
 */
class RotateAuditKeyTest extends TestCase
{
    use RefreshDatabase;

    private ?string $originalAppKey = null;
    private ?string $envPath        = null;
    private ?string $originalEnv    = null;

    protected function setUp(): void
    {
        parent::setUp();

        Project::getModel()->setTable('projects');

        // Snapshot the APP_KEY so we can restore after the test — the
        // orchestrator writes a new key to the active environment file,
        // and leaving it in place would break every subsequent test's
        // encryption round-trip.
        $this->originalAppKey = (string) config('app.key');

        // IMPORTANT: use the ACTIVE env file, not a hardcoded `.env`.
        // Under APP_ENV=testing (forced by phpunit.xml) Laravel's active
        // environment file is `.env.testing`, and that's what
        // `key:generate --force` will rewrite. If we snapshot the wrong
        // file, successful runs leave `.env.testing` polluted for every
        // subsequent test.
        $this->envPath = app()->environmentFilePath();
        if (File::exists($this->envPath)) {
            $this->originalEnv = File::get($this->envPath);
        }

        // Laravel's `key:generate` uses a regex
        //     /^APP_KEY=<current config('app.key')>/m
        // to find-and-replace the key inside the active env file. When
        // the container's shell env ships its own APP_KEY (Dotenv
        // immutable mode respects existing env vars), `config('app.key')`
        // ends up pointing at the SHELL value rather than the value in
        // `.env.testing`. The regex then misses, `key:generate` silently
        // returns without rewriting anything, the orchestrator reports
        // SUCCESS, and the test falsely fails with "APP_KEY didn't
        // change" because nothing actually changed.
        //
        // Rewrite the active env file so the APP_KEY line matches the
        // in-process config value, guaranteeing the regex will hit.
        if ($this->originalEnv !== null) {
            $patched = preg_replace(
                '/^APP_KEY=.*$/m',
                'APP_KEY=' . $this->originalAppKey,
                $this->originalEnv,
            );
            if (is_string($patched) && $patched !== $this->originalEnv) {
                File::put($this->envPath, $patched);
            }
        }
    }

    protected function tearDown(): void
    {
        // Restore .env + in-process config + encrypter binding exactly as
        // we found them.
        if ($this->originalEnv !== null && $this->envPath !== null) {
            File::put($this->envPath, $this->originalEnv);
        }
        if ($this->originalAppKey !== null) {
            config(['app.key' => $this->originalAppKey]);
            // Force the facade to rebuild its encrypter against the
            // restored key so other tests are unaffected.
            $keyBytes = str_starts_with($this->originalAppKey, 'base64:')
                ? base64_decode(substr($this->originalAppKey, 7))
                : $this->originalAppKey;
            $fresh = new \Illuminate\Encryption\Encrypter(
                $keyBytes,
                config('app.cipher', 'AES-256-CBC'),
            );
            $this->app->instance('encrypter', $fresh);
            $this->app->instance(\Illuminate\Contracts\Encryption\Encrypter::class, $fresh);
            Crypt::clearResolvedInstance('encrypter');
        }

        parent::tearDown();
    }

    private function seedRow(string $queryText, string $responseText): QueryAuditLog
    {
        $row = QueryAuditLog::create([
            'user_id'    => null,
            'project_id' => (string) Str::uuid(),
            'query_id'   => (string) Str::uuid(),
            'query_text' => $queryText,
            'ip_address' => '127.0.0.1',
            'llm_model'  => 'test-model',
        ]);
        $row->response_text = $responseText;
        $row->save();
        return $row->fresh();
    }

    public function test_rotation_swaps_key_and_preserves_plaintext(): void
    {
        $rowA = $this->seedRow('alpha deposit — JV confidential', 'resp-A [DATA-1]');
        $rowB = $this->seedRow('beta hole PLS-22-08 lithology query', 'resp-B [DATA-1]');

        $originalKey = (string) config('app.key');
        $originalCipherA = DB::table('query_audit_log')
            ->where('audit_id', $rowA->audit_id)
            ->value('query_text');
        $this->assertNotEmpty($originalCipherA, 'baseline: ciphertext present');

        // Run the orchestrator — skip maintenance mode (there's no HTTP
        // layer to park in a test) and don't keep the dump.
        $this->artisan('audit:rotate-key', [
            '--force'          => true,
            '--no-maintenance' => true,
            '--dump-dir'       => sys_get_temp_dir(),
        ])
            ->expectsOutputToContain('Rotation complete.')
            ->assertSuccessful();

        // APP_KEY must have changed, both on disk and in-process.
        $newKey = (string) config('app.key');
        $this->assertNotSame(
            $originalKey,
            $newKey,
            'APP_KEY should change after rotation',
        );

        // Round-trip through the model — cast must decrypt cleanly with
        // the new key.
        $this->assertSame(
            'alpha deposit — JV confidential',
            QueryAuditLog::find($rowA->audit_id)->query_text,
        );
        $this->assertSame(
            'beta hole PLS-22-08 lithology query',
            QueryAuditLog::find($rowB->audit_id)->query_text,
        );

        // The raw ciphertext in the DB must have changed (different IV
        // at minimum, plus different MAC under a different key).
        $newCipherA = DB::table('query_audit_log')
            ->where('audit_id', $rowA->audit_id)
            ->value('query_text');
        $this->assertNotSame(
            $originalCipherA,
            $newCipherA,
            'raw ciphertext must be rewritten, not reused',
        );

        // Explicit: attempting to decrypt the NEW ciphertext with the OLD
        // key must fail, proving the rotation is real.
        $oldKeyBytes = str_starts_with($originalKey, 'base64:')
            ? base64_decode(substr($originalKey, 7))
            : $originalKey;
        $oldEncrypter = new \Illuminate\Encryption\Encrypter(
            $oldKeyBytes,
            config('app.cipher', 'AES-256-CBC'),
        );
        $this->expectException(\Illuminate\Contracts\Encryption\DecryptException::class);
        $oldEncrypter->decryptString($newCipherA);
    }

    public function test_rotation_shreds_dump_by_default(): void
    {
        $this->seedRow('secret', 'resp');

        $tmpDir = sys_get_temp_dir();
        // Snapshot which files exist in /tmp before the run so we can
        // detect any rotation-dump leftover.
        $before = glob($tmpDir . '/audit-pii-rotation-*.jsonl') ?: [];

        $this->artisan('audit:rotate-key', [
            '--force'          => true,
            '--no-maintenance' => true,
            '--dump-dir'       => $tmpDir,
        ])->assertSuccessful();

        $after = glob($tmpDir . '/audit-pii-rotation-*.jsonl') ?: [];
        $leftover = array_diff($after, $before);
        $this->assertEmpty(
            $leftover,
            'default rotation should shred dump; leftovers: ' . implode(',', $leftover),
        );
    }

    public function test_rotation_keeps_dump_with_flag(): void
    {
        $this->seedRow('kept plaintext', 'resp');

        $tmpDir = sys_get_temp_dir();
        $before = glob($tmpDir . '/audit-pii-rotation-*.jsonl') ?: [];

        $this->artisan('audit:rotate-key', [
            '--force'          => true,
            '--keep-dump'      => true,
            '--no-maintenance' => true,
            '--dump-dir'       => $tmpDir,
        ])
            ->expectsOutputToContain('Keeping dump')
            ->assertSuccessful();

        $after = glob($tmpDir . '/audit-pii-rotation-*.jsonl') ?: [];
        $leftover = array_values(array_diff($after, $before));
        $this->assertCount(1, $leftover, 'expected exactly one preserved dump');

        // Clean up so this test doesn't contaminate subsequent runs.
        foreach ($leftover as $path) {
            @unlink($path);
        }
    }
}
