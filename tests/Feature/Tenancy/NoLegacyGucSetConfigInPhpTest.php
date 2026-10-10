<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use FilesystemIterator;
use RecursiveDirectoryIterator;
use RecursiveIteratorIterator;
use Tests\TestCase;

/**
 * PHP-side counterpart to
 * src/fastapi/tests/test_acquire_scoped.py::test_no_production_files_set_legacy_georag_gucs.
 *
 * Background
 * ----------
 * Every active RLS policy reads `app.workspace_id` / `app.project_id`
 * after the May-25 → May-29 sweeps. The legacy `georag.workspace_id`
 * / `georag.project_id` GUCs are retired — setting them has zero
 * effect, and setting them INSTEAD of the canonical names is a
 * silent fail-closed bug (RLS denies the rows, but the caller sees a
 * successful set_config and an INSERT that reports zero rows
 * affected).
 *
 * The Python regression test scans `*.py` under `src/`. It does not
 * cover Laravel seeders / commands / migrations. The 2026-06-02 audit
 * caught CgiVocabSeeder calling `set_config('georag.workspace_id', …)`
 * exactly because no test was watching that surface. This file closes
 * the gap for PHP.
 *
 * Scope: `app/` (controllers / commands / services / console),
 * `database/seeders/` and `tests/`. Migrations are intentionally EXCLUDED — they
 * historically captured the legacy GUC for both legitimate (creating
 * policies that read it back when those policies still existed) and
 * archaeological reasons. The May-29 sweep migration ALSO references
 * the legacy GUC by design (it's deleting policies that named it),
 * and a flat-text scan can't tell intent from accident in DDL. If a
 * migration needs RLS context, that's already a write to `app.*`.
 *
 * `tests/` was added on 2026-08-21 and then scanned nothing: files were
 * skipped when their ABSOLUTE path contained `/tests/`, which every file
 * under `{base}/tests` does. Paths are now judged relative to the project
 * root, each root must scan a minimum number of files, and the detector is
 * itself tested on a throwaway tree, so the next bug of this kind fails here
 * instead of going unseen.
 */
class NoLegacyGucSetConfigInPhpTest extends TestCase
{
    /**
     * Matches PHP-style set_config calls that target the legacy GUC,
     * e.g. DB::statement("SELECT set_config('georag.workspace_id', ?, true)")
     * or any direct "set_config('georag.project_id'" usage.
     */
    private const PATTERN = "/set_config\\s*\\(\\s*['\"]georag\\.(workspace_id|project_id)['\"]/";

    /**
     * Fewer files than this under a root means the walk is broken, not that
     * the codebase shrank. Set well under today's counts (170 / 14 / 200+).
     *
     * @var array<string, int>
     */
    private const MIN_FILES = [
        'app' => 50,
        'database/seeders' => 5,
        'tests' => 50,
    ];

    /**
     * @return string[] absolute paths
     */
    private function findPhpFiles(string $absRoot, string $base): array
    {
        if (! is_dir($absRoot)) {
            return [];
        }

        $files = [];
        $iter = new RecursiveIteratorIterator(
            new RecursiveDirectoryIterator($absRoot, FilesystemIterator::SKIP_DOTS),
        );
        foreach ($iter as $f) {
            $path = str_replace('\\', '/', (string) $f);
            if (! str_ends_with($path, '.php')) {
                continue;
            }
            // Judge the path RELATIVE to the project root. Matching '/tests/'
            // against the absolute path excluded every file under {base}/tests,
            // and would also exclude a checkout that itself lives under a
            // directory called tests.
            $relative = substr($path, strlen($base) + 1);
            if (str_contains('/'.$relative, '/vendor/')) {
                continue;
            }
            $files[] = $path;
        }

        return $files;
    }

    /**
     * @param array<string, string> $roots label => absolute directory
     *
     * @return array{violations: string[], scanned: array<string, int>}
     */
    private function scan(array $roots, string $base, ?string $skip = null): array
    {
        $violations = [];
        $scanned = [];
        foreach ($roots as $label => $root) {
            $scanned[$label] = 0;
            foreach ($this->findPhpFiles($root, $base) as $f) {
                // This file quotes the legacy GUC in its own comment; scanning
                // itself would make the detector its own only violation.
                if ($skip !== null && realpath($f) === realpath($skip)) {
                    continue;
                }
                $scanned[$label]++;
                $contents = @file_get_contents($f);
                if ($contents === false) {
                    continue;
                }
                if (preg_match(self::PATTERN, $contents) === 1) {
                    $violations[] = substr($f, strlen($base) + 1);
                }
            }
        }
        sort($violations);

        return ['violations' => $violations, 'scanned' => $scanned];
    }

    public function test_no_php_file_calls_set_config_with_legacy_gucs(): void
    {
        $base = str_replace('\\', '/', base_path());
        // tests/ added 2026-08-21. It was out of scope, which is how
        // GuardSchemaRlsTest — the workspace-isolation PEN-TEST — came to
        // bind the legacy GUC and report a false cross-tenant leak on four
        // tables for months. A guard that does not cover the tests cannot
        // see the tests lying to it.
        $result = $this->scan([
            'app' => $base.'/app',
            'database/seeders' => $base.'/database/seeders',
            'tests' => $base.'/tests',
        ], $base, __FILE__);

        foreach (self::MIN_FILES as $label => $minimum) {
            $this->assertGreaterThanOrEqual(
                $minimum,
                $result['scanned'][$label],
                "The scan of {$label}/ saw {$result['scanned'][$label]} PHP files (expected at least {$minimum}). "
                .'A scan that finds nothing proves nothing; the file walk is broken.',
            );
        }

        $this->assertSame(
            [],
            $result['violations'],
            'The following PHP files still call set_config() with the legacy '.
            "'georag.workspace_id' or 'georag.project_id' GUC. The canonical ".
            "RLS policies read 'app.workspace_id' / 'app.project_id', so the ".
            'legacy GUC has zero effect — the call is a silent fail-closed '.
            "bug (RLS denies the rows). Switch to the canonical GUC:\n".
            "  DB::statement(\"SELECT set_config('app.workspace_id', ?, true)\", [\$wsId]);\n".
            "Violations:\n  ".implode("\n  ", $result['violations']),
        );
    }

    public function test_the_detector_finds_a_violation_under_tests_and_app(): void
    {
        // A throwaway project whose ABSOLUTE path contains /tests/ — the shape
        // that hid every violation under tests/ before.
        $tmp = str_replace('\\', '/', sys_get_temp_dir()).'/legacy-guc-'.bin2hex(random_bytes(4)).'/tests/project';
        $bad = "<?php\nDB::statement(\"SELECT set_config('georag.workspace_id', ?, true)\", [\$id]);\n";
        $good = "<?php\nDB::statement(\"SELECT set_config('app.workspace_id', ?, true)\", [\$id]);\n";
        $files = [
            'tests/Feature/BadTest.php' => $bad,
            'app/Console/BadCommand.php' => $bad,
            'app/Console/GoodCommand.php' => $good,
            'vendor/some/package/Bad.php' => $bad,
        ];
        foreach ($files as $relative => $contents) {
            if (! is_dir(dirname($tmp.'/'.$relative))) {
                mkdir(dirname($tmp.'/'.$relative), 0777, true);
            }
            file_put_contents($tmp.'/'.$relative, $contents);
        }

        try {
            $result = $this->scan(['app' => $tmp.'/app', 'tests' => $tmp.'/tests', 'vendor' => $tmp.'/vendor'], $tmp);

            $this->assertSame(
                ['app/Console/BadCommand.php', 'tests/Feature/BadTest.php'],
                $result['violations'],
                'the detector must flag the legacy GUC in app/ and in tests/, and ignore vendor/',
            );
            $this->assertSame(['app' => 2, 'tests' => 1, 'vendor' => 0], $result['scanned']);
        } finally {
            foreach (array_keys($files) as $relative) {
                @unlink($tmp.'/'.$relative);
            }
            foreach (['tests/Feature', 'tests', 'app/Console', 'app', 'vendor/some/package', 'vendor/some', 'vendor'] as $dir) {
                @rmdir($tmp.'/'.$dir);
            }
            @rmdir($tmp);
            @rmdir(dirname($tmp));
            @rmdir(dirname($tmp, 2));
        }
    }
}
