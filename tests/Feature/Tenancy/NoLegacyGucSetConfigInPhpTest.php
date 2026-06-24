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
 * Scope: `app/` (controllers / commands / services / console) and
 * `database/seeders/`. Migrations are intentionally EXCLUDED — they
 * historically captured the legacy GUC for both legitimate (creating
 * policies that read it back when those policies still existed) and
 * archaeological reasons. The May-29 sweep migration ALSO references
 * the legacy GUC by design (it's deleting policies that named it),
 * and a flat-text scan can't tell intent from accident in DDL. If a
 * migration needs RLS context, that's already a write to `app.*`.
 */
class NoLegacyGucSetConfigInPhpTest extends TestCase
{
    /**
     * @return string[] absolute paths
     */
    private function findPhpFiles(string $absRoot): array
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
            // Skip vendor + test fixtures.
            if (str_contains($path, '/vendor/') || str_contains($path, '/tests/')) {
                continue;
            }
            $files[] = $path;
        }

        return $files;
    }

    public function test_no_php_file_calls_set_config_with_legacy_gucs(): void
    {
        $base = str_replace('\\', '/', base_path());
        $roots = [
            $base.'/app',
            $base.'/database/seeders',
        ];

        // Matches PHP-style set_config calls that target the legacy GUC,
        // e.g. DB::statement("SELECT set_config('georag.workspace_id', ?, true)")
        // or any direct "set_config('georag.project_id'" usage.
        $pattern = "/set_config\\s*\\(\\s*['\"]georag\\.(workspace_id|project_id)['\"]/";

        $violations = [];
        foreach ($roots as $root) {
            foreach ($this->findPhpFiles($root) as $f) {
                $contents = @file_get_contents($f);
                if ($contents === false) {
                    continue;
                }
                if (preg_match($pattern, $contents) === 1) {
                    $violations[] = substr($f, strlen($base) + 1);
                }
            }
        }
        sort($violations);

        $this->assertSame(
            [],
            $violations,
            'The following PHP files still call set_config() with the legacy '.
            "'georag.workspace_id' or 'georag.project_id' GUC. The canonical ".
            "RLS policies read 'app.workspace_id' / 'app.project_id', so the ".
            'legacy GUC has zero effect — the call is a silent fail-closed '.
            "bug (RLS denies the rows). Switch to the canonical GUC:\n".
            "  DB::statement(\"SELECT set_config('app.workspace_id', ?, true)\", [\$wsId]);\n".
            "Violations:\n  ".implode("\n  ", $violations),
        );
    }
}
