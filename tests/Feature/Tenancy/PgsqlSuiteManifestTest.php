<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Tests\TestCase;

/**
 * phpunit.pgsql.xml's file list is hand-maintained. Stop it drifting.
 *
 * The config documents its own audit signal:
 *
 *     grep -rl "RequiresPostgres\|skipIfSqlite" tests/Feature
 *
 * That grep has a hole: a file can gate itself with an INLINE
 * `DB::connection()->getDriverName() !== 'pgsql'` check instead of the
 * trait, and the grep will not see it. Three files did exactly that and
 * were never added — including GuardSchemaRlsTest, the pen-test for the
 * five guard-arm tables (silver.query_traces, data_quality_flags,
 * document_versions, entity_aliases, alias_gaps), none of which the pgTAP
 * file covers.
 *
 * The consequence is the quiet kind. Those files DO run — in the SQLite
 * suite, where they immediately skip. So a dropped FORCE ROW LEVEL
 * SECURITY produces one more "skipped" line among the 225 the CI log
 * already carries, and nothing goes red.
 *
 * This test repairs the detector rather than the instance: it recognises
 * BOTH forms of gate, and fails when a Postgres-gated file under
 * tests/Feature is absent from the config.
 */
final class PgsqlSuiteManifestTest extends TestCase
{
    /** Patterns that mean "this file needs a real PostgreSQL driver". */
    private const POSTGRES_GATES = [
        'RequiresPostgres',
        'skipIfSqlite',
        "getDriverName() !== 'pgsql'",
        'getDriverName() !== "pgsql"',
    ];

    public function test_every_postgres_gated_feature_test_is_in_the_pgsql_suite(): void
    {
        $config = base_path('phpunit.pgsql.xml');
        $this->assertFileExists($config);
        $xml = file_get_contents($config);

        $missing = [];
        foreach ($this->featureTestFiles() as $path) {
            // This file itself contains every gate pattern as a literal in
            // POSTGRES_GATES, so a naive scan flags the detector as one of
            // the things it detects.
            if (realpath($path) === realpath(__FILE__)) {
                continue;
            }
            $source = file_get_contents($path);
            if ($source === false || ! $this->isPostgresGated($source)) {
                continue;
            }
            $relative = str_replace(
                [base_path().DIRECTORY_SEPARATOR, DIRECTORY_SEPARATOR],
                ['', '/'],
                $path,
            );
            if (! str_contains($xml, '<file>'.$relative.'</file>')) {
                $missing[] = $relative;
            }
        }

        $this->assertSame([], $missing, sprintf(
            "These tests gate themselves on PostgreSQL but are not listed in\n".
            "phpunit.pgsql.xml, so they run ONLY in the SQLite suite — where\n".
            "they skip on the very first line. They are not covering anything:\n  %s\n\n".
            'Add a <file> entry for each, or drop the Postgres gate.',
            implode("\n  ", $missing),
        ));
    }

    public function test_the_pgsql_suite_lists_no_files_that_have_been_deleted(): void
    {
        $xml = file_get_contents(base_path('phpunit.pgsql.xml'));
        preg_match_all('#<file>(.+?)</file>#', $xml, $matches);
        $this->assertNotEmpty($matches[1], 'no <file> entries found — parse broke');

        $gone = [];
        foreach ($matches[1] as $relative) {
            if (! file_exists(base_path($relative))) {
                $gone[] = $relative;
            }
        }
        // A stale entry is not merely untidy: PHPUnit does not error on a
        // missing <file>, so the suite silently runs fewer tests than the
        // config claims.
        $this->assertSame([], $gone, sprintf(
            "phpunit.pgsql.xml lists files that no longer exist:\n  %s",
            implode("\n  ", $gone),
        ));
    }

    public function test_the_detector_recognises_an_inline_driver_check(): void
    {
        // The hole this test exists to close: a gate the documented grep
        // cannot see.
        $inline = "if (DB::connection()->getDriverName() !== 'pgsql') {";
        $this->assertTrue($this->isPostgresGated($inline));
        $this->assertTrue($this->isPostgresGated('use Tests\Concerns\RequiresPostgres;'));
        $this->assertTrue($this->isPostgresGated('$this->skipIfSqlite();'));
        $this->assertFalse($this->isPostgresGated('$this->markTestSkipped("No projects in DB.");'));
    }

    private function isPostgresGated(string $source): bool
    {
        foreach (self::POSTGRES_GATES as $needle) {
            if (str_contains($source, $needle)) {
                return true;
            }
        }

        return false;
    }

    /** @return list<string> */
    private function featureTestFiles(): array
    {
        $root = base_path('tests/Feature');
        $files = [];
        $iterator = new \RecursiveIteratorIterator(
            new \RecursiveDirectoryIterator($root, \FilesystemIterator::SKIP_DOTS),
        );
        foreach ($iterator as $entry) {
            if ($entry->isFile() && str_ends_with($entry->getFilename(), 'Test.php')) {
                $files[] = $entry->getPathname();
            }
        }
        sort($files);

        return $files;
    }
}
