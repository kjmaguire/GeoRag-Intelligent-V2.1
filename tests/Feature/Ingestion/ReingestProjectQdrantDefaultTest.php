<?php

declare(strict_types=1);

namespace Tests\Feature\Ingestion;

use App\Console\Commands\Ingestion\ReingestProject;
use ReflectionClass;
use Tests\TestCase;

/**
 * Locks ADR-0010 default-collection behavior on {@see ReingestProject}.
 *
 * Before 2026-06-02 the command defaulted to the legacy `georag_reports`
 * collection. ADR-0010 makes `georag_chunks` the canonical RAG corpus,
 * and FastAPI's `RETRIEVAL_USE_DOCUMENT_PASSAGES` flag (default true) is
 * what actually decides which collection retrieval reads. A destructive
 * re-ingest must wipe the same collection retrieval reads from, so the
 * artisan default now tracks that flag.
 *
 * Exercises the protected canonicalQdrantCollection() helper via
 * reflection — keeps the artisan command's public surface unchanged.
 */
class ReingestProjectQdrantDefaultTest extends TestCase
{
    private function callCanonical(): string
    {
        $cmd = new ReingestProject;
        $method = (new ReflectionClass(ReingestProject::class))
            ->getMethod('canonicalQdrantCollection');
        $method->setAccessible(true);

        return $method->invoke($cmd);
    }

    /**
     * The .env can leak between tests; capture and restore so we don't
     * pollute the suite. Laravel's env() reads from $_ENV / $_SERVER /
     * getenv() in that order — we wipe all three.
     */
    private function withEnv(?string $value, callable $body): void
    {
        $key = 'RETRIEVAL_USE_DOCUMENT_PASSAGES';
        $prev = [
            'env' => $_ENV[$key] ?? null,
            'server' => $_SERVER[$key] ?? null,
            'getenv' => getenv($key),
        ];
        try {
            if ($value === null) {
                unset($_ENV[$key], $_SERVER[$key]);
                putenv($key);
            } else {
                $_ENV[$key] = $value;
                $_SERVER[$key] = $value;
                putenv("{$key}={$value}");
            }
            $body();
        } finally {
            if ($prev['env'] === null) {
                unset($_ENV[$key]);
            } else {
                $_ENV[$key] = $prev['env'];
            }
            if ($prev['server'] === null) {
                unset($_SERVER[$key]);
            } else {
                $_SERVER[$key] = $prev['server'];
            }
            if ($prev['getenv'] === false) {
                putenv($key);
            } else {
                putenv("{$key}={$prev['getenv']}");
            }
        }
    }

    public function test_unset_env_defaults_to_canonical_chunks(): void
    {
        $this->withEnv(null, function (): void {
            $this->assertSame('georag_chunks', $this->callCanonical());
        });
    }

    public function test_truthy_env_selects_chunks(): void
    {
        foreach (['true', 'TRUE', '1', 'on', 'yes'] as $val) {
            $this->withEnv($val, function () use ($val): void {
                $this->assertSame(
                    'georag_chunks',
                    $this->callCanonical(),
                    "expected chunks for env value {$val}",
                );
            });
        }
    }

    public function test_falsy_env_falls_back_to_legacy_reports(): void
    {
        foreach (['false', '0', 'off', 'no'] as $val) {
            $this->withEnv($val, function () use ($val): void {
                $this->assertSame(
                    'georag_reports',
                    $this->callCanonical(),
                    "expected legacy reports for env value {$val}",
                );
            });
        }
    }
}
