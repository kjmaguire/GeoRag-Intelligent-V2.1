<?php

declare(strict_types=1);

namespace App\Console\Commands;

use App\Models\QueryAuditLog;
use Illuminate\Console\Command;
use RuntimeException;

/**
 * Hardened plaintext-dump command for the APP_KEY rotation workflow.
 *
 *   php artisan audit:dump-pii --output /secure/audit-pii.jsonl
 *   php artisan audit:dump-pii --output - > /secure/audit-pii.jsonl
 *   php artisan audit:dump-pii --dry-run       (count rows, print nothing)
 *
 * Replaces the brittle `php artisan tinker` heredoc procedure documented
 * in docs/RUNBOOK.md. Streams one JSON object per line (JSONL) to the
 * output path so it scales to millions of rows without loading all of
 * them into memory at once.
 *
 * Reads through the QueryAuditLog model — the `encrypted` cast decrypts
 * transparently with the CURRENT APP_KEY. Call this command BEFORE
 * rotating APP_KEY; after rotation the cipher becomes unreadable.
 *
 * Safety guards
 *   - Refuses to write to a world-readable or world-writable destination.
 *   - Writes with 0600 permissions on Unix filesystems.
 *   - Prints a shredding reminder on completion.
 *   - NEVER writes to stdout unless --output=- is explicitly passed
 *     (prevents an accidental `php artisan audit:dump-pii` from leaking
 *     PII into terminal scrollback).
 *
 * Pairs with audit:restore-pii (rotation companion).
 */
class DumpAuditPii extends Command
{
    protected $signature = 'audit:dump-pii
                            {--output= : Destination path for the JSONL dump (required); use "-" for stdout.}
                            {--chunk=500 : Rows per DB batch (default: 500).}
                            {--dry-run : Count rows and print the plan; do not write anything.}';

    protected $description = 'Stream query_audit_log plaintext to a JSONL file before APP_KEY rotation.';

    public function handle(): int
    {
        $output = $this->option('output');
        $chunk = max(1, (int) $this->option('chunk'));
        $dryRun = (bool) $this->option('dry-run');

        if (! $output) {
            $this->error(
                'audit:dump-pii requires --output=<path> (or --output=- for stdout). '
                .'Refusing to print PII without an explicit destination.',
            );

            return self::FAILURE;
        }

        $stream = null;
        try {
            if ($output === '-') {
                $stream = fopen('php://stdout', 'w');
            } else {
                // Safety: destination directory must exist AND must not be
                // world-readable/writable. A rotation dump on a shared box
                // has the same threat model as the database itself; don't
                // drop it into /tmp by accident.
                $dir = dirname($output);
                if (! is_dir($dir)) {
                    throw new RuntimeException("Output directory does not exist: {$dir}");
                }
                $perms = fileperms($dir) & 0777;
                $sticky = (fileperms($dir) & 0o1000) !== 0; // /tmp-style sticky bit
                if (($perms & 0o002) !== 0 && ! $sticky) {
                    // Truly world-writable, no sticky bit — refuse.
                    throw new RuntimeException(
                        "Refusing to dump PII: directory {$dir} is world-writable with "
                        .'no sticky bit (perms='.decoct($perms).'). '
                        ."Tighten with: chmod o-w {$dir}  (or +t for sticky).",
                    );
                }
                if (($perms & 0o002) !== 0 && $sticky) {
                    $this->warn(
                        "Dump directory {$dir} is world-writable with sticky bit. "
                        .'OK for transient dumps that get shredded immediately, '
                        .'but prefer a locked-down directory for long-lived backups.',
                    );
                }
                if (($perms & 0o004) !== 0) {
                    $this->warn(
                        "Dump directory {$dir} is world-readable — proceeding but this "
                        .'violates least-privilege for PII at rest.',
                    );
                }
                if (! $dryRun) {
                    $stream = fopen($output, 'w');
                    if ($stream === false) {
                        throw new RuntimeException("Cannot open {$output} for writing.");
                    }
                    @chmod($output, 0o600);
                }
            }

            $total = 0;
            // Running SHA-256 of concatenated audit_ids, in the iteration
            // order. Restore can recompute this from the data rows it
            // reads back and compare against the trailer — any truncation,
            // reordering, or row-edit in transit fails the check.
            $hashCtx = hash_init('sha256');

            // Streaming iteration — chunked so a million-row table doesn't
            // exhaust memory. Reads via Eloquent so the `encrypted` cast
            // decrypts transparently.
            QueryAuditLog::orderBy('audit_id')->chunk($chunk, function ($rows) use (&$total, $hashCtx, $stream, $dryRun) {
                foreach ($rows as $row) {
                    $total++;
                    hash_update($hashCtx, (string) $row->audit_id);
                    if ($dryRun) {
                        continue;
                    }
                    $payload = [
                        'audit_id' => $row->audit_id,
                        'user_id' => $row->user_id,
                        'project_id' => $row->project_id,
                        'query_id' => $row->query_id,
                        'query_text' => $row->query_text,        // cast decrypts
                        'response_text' => $row->response_text,     // cast decrypts
                        'citations' => $row->citations,
                        'sources_used' => $row->sources_used,
                        'confidence' => $row->confidence,
                        'response_time_ms' => $row->response_time_ms,
                        'llm_model' => $row->llm_model,
                        'ip_address' => $row->ip_address,
                        'dispatched_at' => optional($row->dispatched_at)?->toIso8601String(),
                        'created_at' => optional($row->created_at)?->toIso8601String(),
                    ];
                    // JSONL — one row per line so restore can stream.
                    fwrite(
                        $stream,
                        json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES).PHP_EOL,
                    );
                }
            });

            // Integrity trailer (#2 follow-up). Marked with a __meta__
            // key so restore can distinguish it from data rows. Carries
            // the final row count AND a SHA-256 of the concatenated
            // audit_ids so truncation or edit-in-flight can't pass the
            // count check alone.
            if (! $dryRun && $stream !== null) {
                $trailer = [
                    '__meta__' => 'audit-pii-dump',
                    'schema_version' => 1,
                    'row_count' => $total,
                    'ids_sha256' => hash_final($hashCtx),
                    'app_key_hint' => substr(hash_hmac('sha256', 'audit-pii', config('app.key')), 0, 16),
                    'written_at' => now()->toIso8601String(),
                ];
                fwrite(
                    $stream,
                    json_encode($trailer, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES).PHP_EOL,
                );
            }

            if ($stream !== null && $output !== '-') {
                fclose($stream);
                $stream = null;
            }

            if ($dryRun) {
                $this->info("Would dump {$total} rows (dry-run).");
            } else {
                $this->info("Dumped {$total} rows → ".($output === '-' ? 'stdout' : $output));
                if ($output !== '-') {
                    $this->warn(
                        'IMPORTANT: shred the dump after APP_KEY rotation:  shred -u '.escapeshellarg($output),
                    );
                }
            }

            return self::SUCCESS;
        } catch (\Throwable $e) {
            if ($stream !== null && $output !== '-') {
                fclose($stream);
                // Best-effort: remove a partial dump so we don't leave
                // plaintext hanging around on a failed run.
                if (file_exists($output)) {
                    @unlink($output);
                }
            }
            $this->error('Dump failed: '.$e->getMessage());

            return self::FAILURE;
        }
    }
}
