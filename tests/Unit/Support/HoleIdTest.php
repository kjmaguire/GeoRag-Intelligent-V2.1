<?php

declare(strict_types=1);

namespace Tests\Unit\Support;

use App\Support\HoleId;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\TestCase;

/**
 * App\Support\HoleId must agree with georag_geoparsers._hole_id.canonicalize
 * and silver.canonical_hole_id — they are one rule in three languages, and
 * silver.collars is unique on its output (§04e, 2026-09-29).
 */
final class HoleIdTest extends TestCase
{
    /**
     * @return array<string, array{0: ?string, 1: ?string}>
     */
    public static function cases(): array
    {
        return [
            'hyphens' => ['LEB-23-001', 'LEB23001'],
            'underscores and case' => ['leb_23_001', 'LEB23001'],
            'spaces and slash' => ['  LEB 23/001', 'LEB23001'],
            'dots' => ['SRE09.6', 'SRE096'],
            'already canonical' => ['SRE096', 'SRE096'],
            'blank' => ['   ', null],
            'separators only' => ['-_./', null],
            'null' => [null, null],
        ];
    }

    #[DataProvider('cases')]
    public function test_canonicalize(?string $raw, ?string $expected): void
    {
        $this->assertSame($expected, HoleId::canonicalize($raw));
    }
}
