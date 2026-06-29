<?php

declare(strict_types=1);

namespace Tests\Feature\Ingestion;

use App\Console\Commands\Ingestion\ReingestProject;
use InvalidArgumentException;
use ReflectionClass;
use Tests\TestCase;

/**
 * Covers the targeted-recovery `--keys-file` option on
 * {@see ReingestProject}. The full command depends on Storage/HTTP/DB
 * which need heavy mocking; this test isolates the JSON-parsing helper
 * via reflection. The recovery branch is short, deterministic, and
 * historically the source of operator mistakes (paths, bad JSON, empty
 * arrays), so it gets its own unit-style coverage.
 */
class ReingestProjectKeysFileTest extends TestCase
{
    /**
     * Invoke the protected loadWantedKeys() helper. ReingestProject's
     * helper is intentionally protected so test code reaches it via
     * reflection — keeping it out of the public surface of an artisan
     * command preserves encapsulation.
     *
     * @return array<string,true>
     */
    private function callLoadWantedKeys(string $path): array
    {
        $cmd = new ReingestProject;
        $method = (new ReflectionClass(ReingestProject::class))
            ->getMethod('loadWantedKeys');
        $method->setAccessible(true);

        return $method->invoke($cmd, $path);
    }

    private function writeTmpJson(string $contents): string
    {
        $tmp = tempnam(sys_get_temp_dir(), 'reingest-test-').'.json';
        file_put_contents($tmp, $contents);

        return $tmp;
    }

    public function test_returns_minio_key_set_from_well_formed_manifest(): void
    {
        $tmp = $this->writeTmpJson(json_encode([
            ['minio_key' => 'reports/proj-a/file-1.pdf', 'noise' => 'ignored'],
            ['minio_key' => 'reports/proj-a/file-2.pdf'],
            ['minio_key' => 'reports/proj-a/file-3.pdf'],
        ]));

        $wanted = $this->callLoadWantedKeys($tmp);
        unlink($tmp);

        $this->assertSame([
            'reports/proj-a/file-1.pdf' => true,
            'reports/proj-a/file-2.pdf' => true,
            'reports/proj-a/file-3.pdf' => true,
        ], $wanted);
    }

    public function test_deduplicates_repeated_keys(): void
    {
        $tmp = $this->writeTmpJson(json_encode([
            ['minio_key' => 'reports/proj-a/dup.pdf'],
            ['minio_key' => 'reports/proj-a/dup.pdf'],
            ['minio_key' => 'reports/proj-a/unique.pdf'],
        ]));

        $wanted = $this->callLoadWantedKeys($tmp);
        unlink($tmp);

        $this->assertCount(2, $wanted);
        $this->assertArrayHasKey('reports/proj-a/dup.pdf', $wanted);
        $this->assertArrayHasKey('reports/proj-a/unique.pdf', $wanted);
    }

    public function test_skips_entries_without_minio_key_field(): void
    {
        $tmp = $this->writeTmpJson(json_encode([
            ['minio_key' => 'reports/proj-a/keeper.pdf'],
            ['other_field' => 'no key here'],
            ['minio_key' => ''],
            ['minio_key' => null],
            'not-an-object',
            ['minio_key' => 'reports/proj-a/other-keeper.pdf'],
        ]));

        $wanted = $this->callLoadWantedKeys($tmp);
        unlink($tmp);

        $this->assertSame([
            'reports/proj-a/keeper.pdf' => true,
            'reports/proj-a/other-keeper.pdf' => true,
        ], $wanted);
    }

    public function test_missing_file_throws_invalid_argument(): void
    {
        $this->expectException(InvalidArgumentException::class);
        $this->expectExceptionMessageMatches('/--keys-file not found/');

        $this->callLoadWantedKeys('/tmp/this-path-does-not-exist-1234567.json');
    }

    public function test_malformed_json_throws_invalid_argument(): void
    {
        $tmp = $this->writeTmpJson('{ this is not json');
        try {
            $this->expectException(InvalidArgumentException::class);
            $this->expectExceptionMessageMatches('/--keys-file is not valid JSON/');
            $this->callLoadWantedKeys($tmp);
        } finally {
            unlink($tmp);
        }
    }

    public function test_non_array_root_throws_invalid_argument(): void
    {
        $tmp = $this->writeTmpJson(json_encode((object) ['minio_key' => 'one.pdf']));
        try {
            $this->expectException(InvalidArgumentException::class);
            $this->expectExceptionMessageMatches('/JSON array of objects/');
            $this->callLoadWantedKeys($tmp);
        } finally {
            unlink($tmp);
        }
    }

    public function test_empty_array_throws_invalid_argument(): void
    {
        $tmp = $this->writeTmpJson('[]');
        try {
            $this->expectException(InvalidArgumentException::class);
            $this->expectExceptionMessageMatches('/no minio_key entries/');
            $this->callLoadWantedKeys($tmp);
        } finally {
            unlink($tmp);
        }
    }

    public function test_array_with_no_usable_entries_throws_invalid_argument(): void
    {
        $tmp = $this->writeTmpJson(json_encode([
            ['other_field' => 'a'],
            ['minio_key' => ''],
            'string-entry',
        ]));
        try {
            $this->expectException(InvalidArgumentException::class);
            $this->expectExceptionMessageMatches('/no minio_key entries/');
            $this->callLoadWantedKeys($tmp);
        } finally {
            unlink($tmp);
        }
    }
}
