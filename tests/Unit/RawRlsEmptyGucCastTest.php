<?php

declare(strict_types=1);

namespace Tests\Unit;

use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\Attributes\Test;
use PHPUnit\Framework\TestCase;

/**
 * No SQL that builds an RLS policy -- the raw files db:apply-raw runs, or a
 * migration -- may cast the workspace GUC to uuid without guarding the empty
 * string.
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
 * login. Migration 2026_08_28_100700 carried a copy of block 3's bare cast
 * for the same reason, which is why migrations are scanned too.
 *
 * NULLIF(..., '')::uuid keeps a strict policy strict (NULL matches no row)
 * without the error; it does not add an `IS NULL OR` escape hatch.
 *
 * The match runs over the whole file with comments removed, case-insensitive
 * and whitespace-tolerant, so `TRUE)::UUID`, a cast split across lines, the
 * no-missing_ok form and `CAST(... AS uuid)` are all caught. The FastAPI half
 * of this guard is src/fastapi/tests/test_workspace_guc_cast_guard.py.
 */
final class RawRlsEmptyGucCastTest extends TestCase
{
    /**
     * `current_setting('app.workspace_id'[, true|false])` followed by a uuid
     * cast with nothing in between. The quotes may be doubled, as they are
     * inside EXECUTE format(...) strings. The NULLIF form never matches: its
     * `, '')` sits between the call and the cast.
     */
    private const BARE_CAST = "/current_setting\\(\\s*'{1,2}app\\.workspace_id'{1,2}\\s*(?:,\\s*(?:true|false)\\s*)?\\)\\s*::\\s*uuid\\b/i";

    private const BARE_CAST_FUNCTION = "/\\bcast\\(\\s*current_setting\\(\\s*'{1,2}app\\.workspace_id'{1,2}\\s*(?:,\\s*(?:true|false)\\s*)?\\)\\s+as\\s+uuid\\b/i";

    #[Test]
    public function applied_raw_files_never_cast_the_workspace_guc_to_uuid_bare(): void
    {
        $root = dirname(__DIR__, 2).'/database/raw';
        $manifest = json_decode((string) file_get_contents($root.'/manifest.json'), true, 512, JSON_THROW_ON_ERROR);

        $offenders = [];
        foreach ($manifest['files'] as $entry) {
            $path = $root.'/'.$entry['path'];
            $this->assertFileIsReadable($path, "manifest lists {$entry['path']} but it cannot be read");

            $offenders = [...$offenders, ...self::offenders($entry['path'], self::stripSqlComments((string) file_get_contents($path)))];
        }

        $this->assertSame([], $offenders, self::message());
    }

    #[Test]
    public function migrations_never_cast_the_workspace_guc_to_uuid_bare(): void
    {
        $paths = glob(dirname(__DIR__, 2).'/database/migrations/*.php') ?: [];
        $this->assertNotEmpty($paths, 'no migrations found -- the glob is wrong, not the code clean');

        $offenders = [];
        foreach ($paths as $path) {
            $offenders = [...$offenders, ...self::offenders(basename($path), self::stripPhpComments((string) file_get_contents($path)))];
        }

        $this->assertSame([], $offenders, self::message());
    }

    /**
     * The patterns themselves, so a narrowed regex fails here rather than
     * quietly passing every file.
     */
    #[Test]
    #[DataProvider('bareCasts')]
    public function the_pattern_catches_every_spelling_of_the_bare_cast(string $sql): void
    {
        $this->assertNotSame([], self::offenders('fixture', $sql));
    }

    #[Test]
    #[DataProvider('guardedCasts')]
    public function the_pattern_accepts_the_guarded_cast(string $sql): void
    {
        $this->assertSame([], self::offenders('fixture', $sql));
    }

    /**
     * @return array<string, array{string}>
     */
    public static function bareCasts(): array
    {
        return [
            'lowercase' => ["USING (workspace_id = current_setting('app.workspace_id', true)::uuid)"],
            'uppercase keywords' => ["USING (workspace_id = current_setting('app.workspace_id', TRUE)::UUID)"],
            'no missing_ok' => ["workspace_id = current_setting('app.workspace_id')::uuid"],
            'spaced cast' => ["current_setting('app.workspace_id', true) :: uuid"],
            'split across lines' => ["workspace_id =\n    current_setting('app.workspace_id', true)\n    ::uuid"],
            'CAST function' => ["CAST(current_setting('app.workspace_id', true) AS uuid)"],
            'doubled quotes in format()' => ["current_setting(''app.workspace_id'', true)::uuid"],
        ];
    }

    /**
     * @return array<string, array{string}>
     */
    public static function guardedCasts(): array
    {
        return [
            'NULLIF' => ["workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid"],
            'NULLIF uppercase' => ["workspace_id = NULLIF(CURRENT_SETTING('app.workspace_id', TRUE), '')::UUID"],
            'NULLIF doubled quotes' => ["NULLIF(current_setting(''app.workspace_id'', true), '''')::uuid"],
            'NULLIF parenthesised' => ["(NULLIF(current_setting('app.workspace_id', true), ''))::uuid"],
            'text comparison' => ["current_setting('app.workspace_id', true) = ''"],
        ];
    }

    /**
     * @return list<string>
     */
    private static function offenders(string $label, string $source): array
    {
        $offenders = [];
        foreach ([self::BARE_CAST, self::BARE_CAST_FUNCTION] as $pattern) {
            preg_match_all($pattern, $source, $matches, PREG_OFFSET_CAPTURE);
            foreach ($matches[0] as [$text, $offset]) {
                $line = substr_count($source, "\n", 0, $offset) + 1;
                $offenders[] = $label.':'.$line.': '.preg_replace('/\s+/', ' ', $text);
            }
        }

        return $offenders;
    }

    /**
     * Drops `--` line comments and `/* *\/` block comments. Line numbers are
     * kept by replacing each comment with its own newlines.
     */
    private static function stripSqlComments(string $sql): string
    {
        return (string) preg_replace_callback(
            '~--[^\n]*|/\*.*?\*/~s',
            static fn (array $match): string => str_repeat("\n", substr_count($match[0], "\n")),
            $sql,
        );
    }

    /**
     * Drops PHP comments and docblocks, keeping strings and heredocs -- the
     * SQL a migration runs lives in those.
     */
    private static function stripPhpComments(string $php): string
    {
        $out = '';
        foreach (token_get_all($php) as $token) {
            if (is_array($token) && in_array($token[0], [T_COMMENT, T_DOC_COMMENT], true)) {
                $out .= str_repeat("\n", substr_count($token[1], "\n"));

                continue;
            }
            $out .= is_array($token) ? $token[1] : $token;
        }

        return $out;
    }

    private static function message(): string
    {
        return "A bare workspace GUC cast raises 22P02 when the GUC is ''. "
            ."Use NULLIF(current_setting('app.workspace_id', true), '')::uuid.";
    }
}
