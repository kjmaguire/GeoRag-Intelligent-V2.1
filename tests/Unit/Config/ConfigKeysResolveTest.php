<?php

declare(strict_types=1);

namespace Tests\Unit\Config;

use Illuminate\Support\Facades\Config;
use PHPUnit\Framework\Attributes\Test;
use RecursiveDirectoryIterator;
use RecursiveIteratorIterator;
use Tests\TestCase;

/**
 * Every config key the application reads must exist.
 *
 * `config('a.b.c')` on a key that was never defined does not fail. It
 * returns null, the `?:` or `??` beside it takes over, and the feature runs
 * on its fallback forever. Four of those were live simultaneously on
 * 2026-08-22:
 *
 *   - `services.horizon.admin_emails` -- the Horizon dashboard gate read a
 *     key that did not exist, took the `[]` default, and denied every user
 *     in every non-local environment. Setting HORIZON_ADMIN_EMAILS did
 *     nothing, because nothing read it.
 *   - `services.fastapi.url` -- four call sites, including the job that
 *     triggers the materialised-view refresh after an ingest. The real keys
 *     are `internal_url` and `base_url`, so all four fell through to a bare
 *     env() read of a variable that was itself undeclared, defaulting to a
 *     docker-compose hostname that does not resolve in production.
 *   - `services.basemap.styles` -- shared to the SPA on every Inertia
 *     response as `basemap_styles`, always null, so every map used
 *     hard-coded public-CDN URLs and the air-gapped on-prem swap that
 *     indirection exists for (CLAUDE.md hard rule #8) was impossible.
 *   - `horizon.defaults.queue` -- `defaults` is keyed by supervisor name,
 *     so the queue-depth metric silently watched one queue and not the
 *     `llm` one its own comment says it exists to watch.
 *
 * None of the four raised anything. This test is the thing that does.
 */
final class ConfigKeysResolveTest extends TestCase
{
    /**
     * Keys that are legitimately absent from config/*.php.
     *
     * Only PostgreSQL GUC names belong here: they read `set_config('app.x')`
     * in a SQL string, which is not a Laravel config lookup at all. The
     * lookbehind in configKeysIn() already excludes `set_config(`; these
     * entries cover any other spelling that reaches the same GUC.
     *
     * @var list<string>
     */
    private const NOT_LARAVEL_CONFIG = [
        'app.workspace_id',
        'app.audit_encryption_key',
    ];

    #[Test]
    public function every_config_key_read_by_the_application_resolves(): void
    {
        $unresolved = [];

        foreach ($this->phpSources() as $file) {
            $source = file_get_contents($file);
            if ($source === false) {
                continue;
            }

            foreach ($this->configKeysIn($source) as $key) {
                if (in_array($key, self::NOT_LARAVEL_CONFIG, true)) {
                    continue;
                }
                if (! Config::has($key)) {
                    $unresolved[$key][] = $this->relative($file);
                }
            }
        }

        $this->assertSame([], $unresolved, $this->describe($unresolved));
    }

    /**
     * Config keys read with a literal dotted string.
     *
     * The negative lookbehind matters: `set_config('app.workspace_id', ...)`
     * is a Postgres call inside a SQL string and matches a naive
     * config-open-paren pattern perfectly.
     *
     * @return list<string>
     */
    private function configKeysIn(string $source): array
    {
        preg_match_all(
            '/(?<![A-Za-z0-9_])config\(\s*\'([a-z0-9_]+(?:\.[A-Za-z0-9_]+)+)\'/',
            $source,
            $matches,
        );

        return array_values(array_unique($matches[1] ?? []));
    }

    /** @return list<string> */
    private function phpSources(): array
    {
        $files = [];

        /** @var iterable<\SplFileInfo> $iterator */
        $iterator = new RecursiveIteratorIterator(new RecursiveDirectoryIterator(base_path('app')));
        foreach ($iterator as $file) {
            if ($file->isFile() && $file->getExtension() === 'php') {
                $files[] = $file->getPathname();
            }
        }

        foreach ((array) glob(base_path('routes/*.php')) as $file) {
            $files[] = (string) $file;
        }

        return $files;
    }

    private function relative(string $path): string
    {
        return str_replace([base_path().DIRECTORY_SEPARATOR, DIRECTORY_SEPARATOR], ['', '/'], $path);
    }

    /** @param array<string, list<string>> $unresolved */
    private function describe(array $unresolved): string
    {
        if ($unresolved === []) {
            return '';
        }

        $lines = ['config() keys that do not resolve (they return null silently):'];
        foreach ($unresolved as $key => $files) {
            $lines[] = "  {$key}";
            foreach (array_unique($files) as $file) {
                $lines[] = "      {$file}";
            }
        }

        return implode(PHP_EOL, $lines);
    }
}
