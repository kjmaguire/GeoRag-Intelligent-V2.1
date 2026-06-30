<?php

declare(strict_types=1);

namespace App\Console\Commands;

use App\Models\QueryAuditLog;
use Illuminate\Console\Command;
use Illuminate\Contracts\Encryption\DecryptException;
use Illuminate\Support\Facades\Crypt;
use Illuminate\Support\Facades\DB;

/**
 * Re-encrypts audit rows from a plaintext JSONL dump, using the CURRENT
 * APP_KEY. Run as the second half of the APP_KEY rotation workflow
 * described in docs/RUNBOOK.md — AFTER `php artisan key:generate`.
 *
 *   php artisan audit:restore-pii --input /secure/audit-pii.jsonl
 *   php artisan audit:restore-pii --input /secure/audit-pii.jsonl --dry-run
 *
 * Pairs with audit:dump-pii. Input format is JSONL emitted by that
 * command. Each line is one row; restore iterates line-by-line so a
 * million-row dump doesn't need to fit in memory.
 *
 * Semantics
 *   - Match each input row by audit_id to an existing DB row.
 *   - Skip rows that don't exist in the DB (logged with a warning so the
 *     operator notices drift — e.g., retention-purged rows between dump
 *     and restore).
 *   - Re-assign query_text and response_text through the model so the
 *     mutator encrypts under the new APP_KEY and refreshes query_text_hash.
 *   - No row-insert path: we assume rotation happens in-place, never
 *     into a fresh empty table. If rows are missing, the operator should
 *     investigate rather than silently resurrect them.
 */
class RestoreAuditPii extends Command
{
    protected $signature = 'audit:restore-pii
                            {--input= : Source JSONL path emitted by audit:dump-pii (required).}
                            {--dry-run : Walk the input and report counts; do not write.}';

    protected $description = 'Re-encrypt audit rows under the current APP_KEY from a plaintext dump (post-rotation).';

    public function handle(): int
    {
        $input = $this->option('input');
        $dryRun = (bool) $this->option('dry-run');

        if (! $input) {
            $this->error('audit:restore-pii requires --input=<path>');

            return self::FAILURE;
        }
        if (! is_readable($input)) {
            $this->error("Cannot read input file: {$input}");

            return self::FAILURE;
        }

        $fh = fopen($input, 'r');
        if ($fh === false) {
            $this->error("Cannot open input file: {$input}");

            return self::FAILURE;
        }

        $restored = 0;
        $skipped = 0;
        $missing = 0;
        $errors = 0;
        // Running SHA-256 of data-row audit_ids, computed in input order.
        // Compared against the trailer's `ids_sha256` for integrity (#2).
        $hashCtx = hash_init('sha256');
        $dataRowCount = 0;
        $trailer = null;

        try {
            while (($line = fgets($fh)) !== false) {
                $line = trim($line);
                if ($line === '') {
                    continue;
                }
                try {
                    $row = json_decode($line, true, flags: JSON_THROW_ON_ERROR);
                } catch (\JsonException $je) {
                    $errors++;
                    $this->warn('Malformed JSONL line (skipping): '.substr($line, 0, 120));

                    continue;
                }

                // Integrity trailer (#2). Always the last line of a dump
                // produced by audit:dump-pii. Capture and validate at EOF.
                if (($row['__meta__'] ?? null) === 'audit-pii-dump') {
                    $trailer = $row;

                    continue;
                }

                $auditId = $row['audit_id'] ?? null;
                if (! $auditId) {
                    $errors++;
                    $this->warn('Row has no audit_id; skipping.');

                    continue;
                }
                hash_update($hashCtx, (string) $auditId);
                $dataRowCount++;

                $model = QueryAuditLog::find($auditId);
                if ($model === null) {
                    $missing++;

                    continue;
                }

                // If both fields match what the model already holds, skip —
                // avoids unnecessary writes on a partial retry. However, a
                // post-rotation row will have UNREADABLE ciphertext; the
                // cast throws DecryptException on read. That's precisely
                // the case where we NEED to write, so catch + force-write.
                $incomingQuery = $row['query_text'] ?? null;
                $incomingResponse = $row['response_text'] ?? null;
                try {
                    $currentQuery = $model->query_text;
                    $currentResponse = $model->response_text;
                    $needsWrite = ($incomingQuery !== $currentQuery)
                        || ($incomingResponse !== $currentResponse);
                } catch (DecryptException) {
                    // Current ciphertext unreadable under this APP_KEY —
                    // this is the post-rotation recovery path. Always write.
                    $needsWrite = true;
                }

                if (! $needsWrite) {
                    $skipped++;

                    continue;
                }

                if ($dryRun) {
                    $restored++;

                    continue;
                }

                // Write via raw DB update rather than $model->save() —
                // Eloquent's save() compares $original through the
                // `encrypted` cast to build dirty state, and on
                // already-corrupt ciphertext that decrypt throws
                // DecryptException. Same trap Phase 1 hit in the
                // audit:encrypt-pii backfill; same fix.
                $update = [
                    'query_text' => $incomingQuery === null ? null : Crypt::encryptString($incomingQuery),
                    'response_text' => $incomingResponse === null ? null : Crypt::encryptString($incomingResponse),
                ];
                if ($incomingQuery !== null) {
                    $update['query_text_hash'] = QueryAuditLog::hashQueryText($incomingQuery);
                }
                DB::table('query_audit_log')
                    ->where('audit_id', $auditId)
                    ->update($update);
                $restored++;
            }
        } finally {
            fclose($fh);
        }

        // Integrity check (#2). Refuse to report success when the dump's
        // trailer disagrees with what we actually read — a truncation or
        // mid-transit corruption would otherwise look like a clean run
        // with a smaller-than-expected row count.
        $idsSha = hash_final($hashCtx);
        if ($trailer === null) {
            $this->warn(
                'Dump has no integrity trailer. Either it was produced by a '
                    .'pre-integrity-check version of audit:dump-pii, or it was '
                    .'truncated in transit. Proceeding, but cannot guarantee completeness.',
            );
        } else {
            $expectedRows = (int) ($trailer['row_count'] ?? -1);
            $expectedSha = (string) ($trailer['ids_sha256'] ?? '');
            if ($expectedRows !== $dataRowCount) {
                $this->error(sprintf(
                    'Dump integrity check FAILED: trailer says %d rows, read %d. '
                        .'Refusing to consider this restore successful.',
                    $expectedRows,
                    $dataRowCount,
                ));

                return self::FAILURE;
            }
            if ($expectedSha !== '' && $expectedSha !== $idsSha) {
                $this->error(
                    'Dump integrity check FAILED: audit_id SHA-256 mismatch. '
                        ."Expected {$expectedSha}, computed {$idsSha}. "
                        .'Restore may have reordered rows or seen a corrupt dump.',
                );

                return self::FAILURE;
            }
            $hintExpected = (string) ($trailer['app_key_hint'] ?? '');
            $hintCurrent = substr(hash_hmac('sha256', 'audit-pii', config('app.key')), 0, 16);
            if ($hintExpected !== '' && $hintExpected === $hintCurrent) {
                // Same key on both sides — unusual for a rotation workflow.
                // Not an error (could be a drill / integrity-check restore
                // onto the same install) but worth surfacing.
                $this->warn(
                    'Dump and current APP_KEY look identical. This is typical for '
                        .'integrity-rehearsal restores; during a real key rotation '
                        .'the hints should differ.',
                );
            }
            $this->info(
                "Integrity check passed: {$dataRowCount} rows match trailer "
                    ."(schema v{$trailer['schema_version']}).",
            );
        }

        $this->info(sprintf(
            '%s %d rows (skipped %d already-current, %d missing in DB, %d malformed).',
            $dryRun ? 'Would restore' : 'Restored',
            $restored,
            $skipped,
            $missing,
            $errors,
        ));

        if (! $dryRun && $missing > 0) {
            $this->warn(
                "{$missing} rows from the dump have no matching DB row. "
                .'Investigate before shredding the dump.',
            );
        }

        return ($errors > 0) ? self::FAILURE : self::SUCCESS;
    }
}
