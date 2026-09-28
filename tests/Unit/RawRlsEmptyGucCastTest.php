<?php

declare(strict_types=1);

namespace Tests\Unit;

use PHPUnit\Framework\Attributes\Test;
use PHPUnit\Framework\TestCase;

/**
 * No raw SQL file that db:apply-raw runs may cast the workspace GUC to uuid
 * without guarding the empty string.
 *
 * BindWorkspaceRlsContext binds `app.workspace_id` to '' whenever it cannot
 * resolve a workspace -- every request from a user with no project, and every
 * pooled connection after the request's finally-block unbinds. A policy that
 * says `current_setting('app.workspace_id', true)::uuid` then evaluates
 * `''::uuid` and raises SQLSTATE 22P02 inside the row filter, so the query
 * 500s instead of returning no rows.
 *
 * That shipped on the first AWS deploy (2026-09-28): it was the first to run
 * db:apply-raw, and 97-rls-tenant-isolation-block2.sql DROPs and re-CREATEs
 * `projects_workspace_isolation` with the bare cast on every deploy, so a
 * freshly seeded admin with no project got a 500 on /projects straight after
 * login. A migration cannot fix this -- the raw pass runs after `migrate` and
 * overwrites it -- which is why the guard lives here, over the raw files.
 *
 * NULLIF(..., '')::uuid keeps a strict policy strict (NULL matches no row)
 * without the error; it does not add an `IS NULL OR` escape hatch.
 */
final class RawRlsEmptyGucCastTest extends TestCase
{
    #[Test]
    public function applied_raw_files_never_cast_the_workspace_guc_to_uuid_bare(): void
    {
        $root = dirname(__DIR__, 2).'/database/raw';
        $manifest = json_decode((string) file_get_contents($root.'/manifest.json'), true, 512, JSON_THROW_ON_ERROR);

        $offenders = [];
        foreach ($manifest['files'] as $entry) {
            $lines = file($root.'/'.$entry['path'], FILE_IGNORE_NEW_LINES) ?: [];
            foreach ($lines as $number => $line) {
                if (str_starts_with(ltrim($line), '--')) {
                    continue;
                }
                // Matches both the literal form and the doubled-quote form
                // used inside EXECUTE format(...) strings.
                if (preg_match("/(?<!NULLIF\\()current_setting\\('{1,2}app\\.workspace_id'{1,2},\\s*true\\)::uuid/", $line) === 1) {
                    $offenders[] = $entry['path'].':'.($number + 1).': '.trim($line);
                }
            }
        }

        $this->assertSame(
            [],
            $offenders,
            "Bare current_setting('app.workspace_id', true)::uuid raises 22P02 when the GUC is ''. "
                ."Use NULLIF(current_setting('app.workspace_id', true), '')::uuid.",
        );
    }
}
