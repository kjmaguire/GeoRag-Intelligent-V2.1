<?php

declare(strict_types=1);

namespace App\Console\Commands;

use Illuminate\Console\Command;
use Illuminate\Contracts\Encryption\Encrypter as EncrypterContract;
use Illuminate\Encryption\Encrypter;
use Illuminate\Support\Facades\Artisan;
use Illuminate\Support\Facades\Crypt;
use Illuminate\Support\Facades\File;
use Illuminate\Support\Str;
use RuntimeException;
use Symfony\Component\Process\ExecutableFinder;
use Symfony\Component\Process\Process;

/**
 * Single-command APP_KEY rotation orchestrator (R3/R5 follow-up).
 *
 *   php artisan audit:rotate-key                      # guided, with confirmations
 *   php artisan audit:rotate-key --force              # skip confirmations
 *   php artisan audit:rotate-key --keep-dump          # don't shred the plaintext
 *   php artisan audit:rotate-key --dump-dir=/secure   # custom dump location
 *
 * Wraps the dump → key:generate → restore flow documented in
 * docs/RUNBOOK.md so operators run one command instead of four (plus a
 * maintenance-mode wrapper). The underlying commands remain separately
 * invocable for partial-recovery scenarios.
 *
 * In-process key swap
 * -------------------
 * `php artisan key:generate` writes the new key to .env AND updates the
 * in-process `config('app.key')`, but it does NOT rebind the cached
 * `encrypter` singleton that Crypt/HasAttributes/etc. are holding. This
 * command explicitly:
 *   1. Forgets the container binding for 'encrypter'.
 *   2. Clears the Crypt facade's resolved instance.
 * so the SAME php process that dumped with the old key can restore with
 * the new key. No container restart required.
 *
 * Failure handling
 * ----------------
 * - Dump fails: maintenance mode lifted; nothing changed; non-zero exit.
 * - key:generate fails: dump is on disk but key is stale. Easy recovery —
 *   re-run with --force.
 * - Restore fails: dump is on disk AND new key is live. Rows unreadable.
 *   Operator must rerun `audit:restore-pii --input <dump>` manually or
 *   rotate back (manually copy OLD key into .env and re-run).
 * - We NEVER shred the dump on failure — it's the only recovery asset.
 */
class RotateAuditKey extends Command
{
    protected $signature = 'audit:rotate-key
                            {--force : Skip all interactive confirmations.}
                            {--keep-dump : Do not shred the plaintext dump on success.}
                            {--dump-dir=/tmp : Directory for the transient plaintext dump.}
                            {--no-maintenance : Do NOT put the app into maintenance mode (advanced).}';

    protected $description = 'Rotate APP_KEY with dump-and-reseed: maintenance mode → dump → key:generate → restore → shred → up.';

    public function handle(): int
    {
        $keepDump = (bool) $this->option('keep-dump');
        $force = (bool) $this->option('force');
        $skipMaintenance = (bool) $this->option('no-maintenance');
        $dumpDir = rtrim((string) $this->option('dump-dir'), DIRECTORY_SEPARATOR);

        if (! is_dir($dumpDir)) {
            $this->error("Dump directory does not exist: {$dumpDir}");

            return self::FAILURE;
        }

        // Build a unique dump path so repeated runs don't collide and we
        // can't accidentally shred an unrelated file.
        $dumpPath = $dumpDir.'/audit-pii-rotation-'.date('Ymd-His').'-'
            .Str::random(6).'.jsonl';

        $this->line('');
        $this->line('<fg=yellow>APP_KEY rotation plan:</>');
        $this->line('  1. '.($skipMaintenance ? '[SKIPPED]' : 'Enter maintenance mode'));
        $this->line('  2. Dump audit PII with OLD APP_KEY     → '.$dumpPath);
        $this->line('  3. Generate + install new APP_KEY');
        $this->line('  4. Rebind in-process encrypter');
        $this->line('  5. Restore audit PII with NEW APP_KEY (integrity-checked)');
        $this->line('  6. '.($keepDump ? '[SKIPPED — --keep-dump] Dump preserved at '.$dumpPath : 'Shred the plaintext dump'));
        $this->line('  7. '.($skipMaintenance ? '[SKIPPED]' : 'Lift maintenance mode'));
        $this->line('');

        if (! $force && ! $this->confirm('Proceed?', false)) {
            $this->warn('Aborted by operator.');

            return self::SUCCESS;
        }

        $maintenanceEngaged = false;

        try {
            // ── 1. Maintenance mode ─────────────────────────────────────
            if (! $skipMaintenance) {
                $this->line('[1/7] Entering maintenance mode…');
                Artisan::call('down');
                $maintenanceEngaged = true;
            }

            // ── 2. Dump with OLD key ────────────────────────────────────
            $this->line('[2/7] Dumping audit PII with OLD APP_KEY → '.$dumpPath);
            $dumpExit = Artisan::call('audit:dump-pii', [
                '--output' => $dumpPath,
            ]);
            $this->line(trim(Artisan::output()));
            if ($dumpExit !== self::SUCCESS) {
                throw new RuntimeException('audit:dump-pii failed');
            }
            if (! is_file($dumpPath) || filesize($dumpPath) === 0) {
                throw new RuntimeException("Dump file missing or empty: {$dumpPath}");
            }

            // ── 3. Generate + install new APP_KEY ──────────────────────
            $this->line('[3/7] Generating + installing new APP_KEY…');
            $keyExit = Artisan::call('key:generate', ['--force' => true]);
            $this->line(trim(Artisan::output()));
            if ($keyExit !== self::SUCCESS) {
                throw new RuntimeException('key:generate failed');
            }

            // ── 4. Rebind in-process encrypter ─────────────────────────
            $this->line('[4/7] Rebinding in-process encrypter…');
            $this->rebindEncrypter();

            // ── 5. Restore with NEW key (integrity check enforced) ─────
            $this->line('[5/7] Restoring audit PII with NEW APP_KEY…');
            $restoreExit = Artisan::call('audit:restore-pii', [
                '--input' => $dumpPath,
            ]);
            $this->line(trim(Artisan::output()));
            if ($restoreExit !== self::SUCCESS) {
                throw new RuntimeException(
                    'audit:restore-pii failed. Dump preserved at '.$dumpPath
                        .' — rerun audit:restore-pii manually to finish the rotation.',
                );
            }

            // ── 6. Shred the plaintext dump ────────────────────────────
            if ($keepDump) {
                $this->warn('[6/7] Keeping dump at '.$dumpPath.' (--keep-dump). '
                    .'SHRED IT MANUALLY as soon as you\'re done.');
            } else {
                $this->line('[6/7] Shredding the plaintext dump…');
                $this->shredFile($dumpPath);
            }
            $this->info('Rotation complete.');

            return self::SUCCESS;
        } catch (\Throwable $e) {
            $this->error('Rotation failed: '.$e->getMessage());
            if (is_file($dumpPath)) {
                $this->warn(
                    'The plaintext dump is preserved at '.$dumpPath.' for recovery. '
                        .'Do NOT delete it until you have either successfully completed '
                        .'the rotation OR reverted APP_KEY to the original value.',
                );
            }

            return self::FAILURE;
        } finally {
            if ($maintenanceEngaged) {
                $this->line('[7/7] Lifting maintenance mode…');
                Artisan::call('up');
            }
        }
    }

    /**
     * Replace the cached 'encrypter' singleton with a fresh one built from
     * the (now-updated) config('app.key'). Without this, the SAME php
     * process that loaded the OLD Encrypter at boot would keep using it
     * for the restore pass, and the new cipher writes would be unreadable
     * until the next container restart.
     */
    private function rebindEncrypter(): void
    {
        $appKey = (string) config('app.key');
        if ($appKey === '') {
            throw new RuntimeException(
                'config(app.key) is empty after key:generate — cannot rebind encrypter.',
            );
        }
        // Decode `base64:` prefix — matches Laravel's parseKey behaviour.
        if (str_starts_with($appKey, 'base64:')) {
            $keyBytes = base64_decode(substr($appKey, 7));
        } else {
            $keyBytes = $appKey;
        }
        $cipher = (string) config('app.cipher', 'AES-256-CBC');
        $fresh = new Encrypter($keyBytes, $cipher);

        // Rebind the container binding + the facade's resolved instance.
        $this->laravel->instance('encrypter', $fresh);
        $this->laravel->instance(EncrypterContract::class, $fresh);
        Crypt::clearResolvedInstance('encrypter');
    }

    /**
     * Remove the dump file using `shred` when available, falling back to
     * a zero-overwrite-then-unlink when it's not. Either way the plaintext
     * is gone from the filesystem before the command exits.
     *
     * Uses Symfony Process (no shell invocation) so the path argument
     * cannot be misinterpreted as shell metacharacters.
     */
    private function shredFile(string $path): void
    {
        if (! is_file($path)) {
            return;
        }
        $shred = (new ExecutableFinder)->find('shred');
        if ($shred !== null) {
            $process = new Process([$shred, '-u', $path]);
            $process->setTimeout(30);
            $process->run();
            if ($process->isSuccessful() && ! file_exists($path)) {
                return;
            }
            // Fall through to best-effort overwrite on shred failure.
        }
        // Fallback: overwrite with zeros, then unlink. Best-effort —
        // on copy-on-write filesystems (btrfs, ZFS) this may not
        // actually overwrite the original blocks. `shred` is preferred.
        $size = filesize($path) ?: 0;
        if ($size > 0) {
            $fh = @fopen($path, 'w');
            if ($fh !== false) {
                fwrite($fh, str_repeat("\0", min($size, 1024 * 1024)));
                fclose($fh);
            }
        }
        if (File::exists($path)) {
            @unlink($path);
        }
    }
}
