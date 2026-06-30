<?php

declare(strict_types=1);

namespace App\Console\Commands;

use App\Models\QueryAuditLog;
use Illuminate\Console\Command;
use Illuminate\Contracts\Encryption\DecryptException;
use Illuminate\Support\Facades\Crypt;
use Illuminate\Support\Facades\DB;

/**
 * Backfill command for the A4 PII-at-rest migration.
 *
 *   php artisan audit:encrypt-pii --dry-run
 *   php artisan audit:encrypt-pii
 *
 * Re-saves every QueryAuditLog row so the new `encrypted` cast writes
 * ciphertext to query_text / response_text and populates query_text_hash
 * via the model's mutator. Idempotent — rows that already contain
 * ciphertext are skipped (Laravel's encrypted payloads decode as JSON with
 * an "iv" / "value" / "mac" envelope; we probe with Crypt::decryptString()
 * and skip rows that decrypt cleanly).
 *
 * Safe to run under Octane — uses chunked iteration so memory stays flat
 * on large tables. Does not dispatch jobs, broadcast events, or touch
 * timestamps.
 */
class EncryptExistingAuditPii extends Command
{
    protected $signature = 'audit:encrypt-pii
                            {--dry-run : Report what would change without writing}
                            {--chunk=500 : Rows per batch}';

    protected $description = 'Encrypt plaintext query_text/response_text in query_audit_log and backfill query_text_hash';

    public function handle(): int
    {
        $dryRun = (bool) $this->option('dry-run');
        $chunk = max(1, (int) $this->option('chunk'));

        if (! $dryRun && ! $this->confirm('This will rewrite every query_audit_log row. Continue?', false)) {
            $this->warn('Aborted.');

            return self::SUCCESS;
        }

        $total = 0;
        $rewrote = 0;
        $skipped = 0;

        // Query the raw columns directly — we must NOT read through the
        // encrypted cast, or rows that are already plaintext will throw
        // during the cast's decrypt step and we'd never see them.
        DB::table('query_audit_log')
            ->select(['audit_id', 'query_text', 'response_text', 'query_text_hash'])
            ->orderBy('audit_id')
            ->chunk($chunk, function ($rows) use (&$total, &$rewrote, &$skipped, $dryRun) {
                foreach ($rows as $row) {
                    $total++;

                    $queryAlreadyEncrypted = $this->looksEncrypted($row->query_text);
                    $responseAlreadyEncrypted = $row->response_text === null
                        || $this->looksEncrypted($row->response_text);
                    $hashAlreadyPresent = $row->query_text_hash !== null
                        && $row->query_text_hash !== '';

                    // Detect legacy ciphertext that contains a serialize
                    // wrapper (written by the A4 mutator's original
                    // `encrypt()` call before R1 fix). Those rows need
                    // to be re-rewritten so reads via the `encrypted` cast
                    // return canonical plaintext. We force re-rewrite by
                    // flipping the "already encrypted" flags to false.
                    if ($queryAlreadyEncrypted && $this->isLegacySerialized($row->query_text)) {
                        $queryAlreadyEncrypted = false;
                    }
                    if ($responseAlreadyEncrypted && $row->response_text !== null
                        && $this->isLegacySerialized($row->response_text)) {
                        $responseAlreadyEncrypted = false;
                    }

                    if ($queryAlreadyEncrypted && $responseAlreadyEncrypted && $hashAlreadyPresent) {
                        $skipped++;

                        continue;
                    }

                    if ($dryRun) {
                        $rewrote++;

                        continue;
                    }

                    // Resolve plaintext versions (decrypt if already encrypted).
                    $queryPlain = $queryAlreadyEncrypted
                        ? $this->safeDecrypt($row->query_text)
                        : $row->query_text;

                    $responsePlain = $row->response_text === null
                        ? null
                        : ($responseAlreadyEncrypted
                            ? $this->safeDecrypt($row->response_text)
                            : $row->response_text);

                    // Write via raw DB update. We deliberately bypass the
                    // Eloquent model here: on legacy plaintext rows, saving
                    // through QueryAuditLog::save() triggers Laravel's
                    // dirty-tracking `originalIsEquivalent()` check, which
                    // runs the `encrypted` cast over the ORIGINAL plaintext
                    // column value and throws DecryptException. Writing raw
                    // with pre-computed ciphertext + hash sidesteps that
                    // entirely and matches the outputs the mutator would
                    // have produced.
                    $update = [];
                    if (! $queryAlreadyEncrypted) {
                        // Match the model mutator: use encryptString (no
                        // serialize wrapper) so the `encrypted` cast's
                        // decryptString can round-trip cleanly.
                        $update['query_text'] = $queryPlain === null ? null : Crypt::encryptString($queryPlain);
                    }
                    if (! $responseAlreadyEncrypted) {
                        $update['response_text'] = $responsePlain === null ? null : Crypt::encryptString($responsePlain);
                    }
                    if (! $hashAlreadyPresent && $queryPlain !== null) {
                        $update['query_text_hash'] = QueryAuditLog::hashQueryText($queryPlain);
                    }

                    if ($update === []) {
                        $skipped++;

                        continue;
                    }

                    DB::table('query_audit_log')
                        ->where('audit_id', $row->audit_id)
                        ->update($update);

                    $rewrote++;
                }
            });

        $this->info(sprintf(
            '%s %d/%d rows (%d skipped as already-encrypted).',
            $dryRun ? 'Would rewrite' : 'Rewrote',
            $rewrote,
            $total,
            $skipped,
        ));

        return self::SUCCESS;
    }

    /**
     * Probe: does this value look like a Laravel-encrypted payload?
     *
     * Laravel encryption emits base64 of a JSON envelope with keys
     * iv/value/mac. Base64 of `{"iv"…` starts with `eyJpdiI6`. Cheap
     * prefix check first, then an actual decrypt attempt as the source
     * of truth so we don't false-positive on legitimate plaintext.
     */
    private function looksEncrypted(?string $value): bool
    {
        if ($value === null || $value === '') {
            return false;
        }

        if (! str_starts_with($value, 'eyJ')) {
            return false;
        }

        try {
            Crypt::decryptString($value);

            return true;
        } catch (DecryptException) {
            return false;
        }
    }

    /**
     * True when `$value` is a ciphertext that decrypts into a PHP
     * serialize() wrapper (legacy A4 shape). Used to force re-rewrite so
     * the new `encryptString`-only pipeline replaces the mixed-encoding
     * payload.
     */
    private function isLegacySerialized(?string $value): bool
    {
        if ($value === null || $value === '') {
            return false;
        }
        try {
            $plain = Crypt::decryptString($value);
        } catch (DecryptException) {
            return false;
        }

        return is_string($plain) && preg_match('/^s:\d+:"/', $plain) === 1;
    }

    private function safeDecrypt(string $value): string
    {
        try {
            $decrypted = Crypt::decryptString($value);
            // Legacy A4 rows were written with the global encrypt() helper
            // which serialize-wraps values before encrypting. Detect the
            // `s:N:"...";` serialize form and unwrap so the re-encrypt
            // path re-stores canonical plaintext.
            if (is_string($decrypted) && preg_match('/^s:\d+:"/', $decrypted)) {
                $unserialized = @unserialize($decrypted);
                if (is_string($unserialized)) {
                    return $unserialized;
                }
            }

            return $decrypted;
        } catch (DecryptException) {
            // Shouldn't reach here because looksEncrypted() verified first,
            // but if APP_KEY has rotated we return the original string so
            // operator can see the damage rather than losing the row.
            return $value;
        }
    }
}
