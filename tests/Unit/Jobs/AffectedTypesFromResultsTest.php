<?php

declare(strict_types=1);

namespace Tests\Unit\Jobs;

use App\Jobs\DebounceWorkspaceMvRefresh;
use Illuminate\Support\Str;
use PHPUnit\Framework\Attributes\DataProvider;
use ReflectionMethod;
use Tests\TestCase;

/**
 * `affected_types` is what decides whether a page re-fetches.
 *
 * Every partial reload in the Foundry frontend is gated on this list, and
 * the list had no test of its own — so the two data shapes that were
 * missing from it (spatial features and well-log curves) went unnoticed
 * until someone uploaded a LAS file and watched the strip log not change.
 *
 * Pure function, no database: the job's constructor only assigns fields.
 */
final class AffectedTypesFromResultsTest extends TestCase
{
    /**
     * Types emitted on every successful refresh regardless of which views
     * moved. The frontend treats these as "something landed in this
     * project" — see the method's own docblock for why a superset beats
     * per-table accuracy here.
     *
     * @var list<string>
     */
    private const ALWAYS = [
        'reports',
        'quality',
        'review_queue',
        'structures',
        'curves',
        'hypotheses',
        'what_changed',
    ];

    /**
     * @param array<int, array<string, mixed>> $results
     *
     * @return list<string>
     */
    private function affectedTypes(array $results): array
    {
        $job = new DebounceWorkspaceMvRefresh(
            (string) Str::uuid(),
            (string) Str::uuid(),
            (string) Str::uuid(),
            1_755_000_000,
        );

        $method = new ReflectionMethod($job, 'affectedTypesFromResults');

        return $method->invoke($job, $results);
    }

    public function test_an_empty_result_set_still_reports_the_always_types(): void
    {
        // A workspace whose views were all already fresh still had an
        // ingest complete. Emitting nothing here would mean the pages that
        // read straight from Silver never learn the rows arrived.
        $types = $this->affectedTypes([]);

        foreach (self::ALWAYS as $expected) {
            $this->assertContains($expected, $types);
        }
    }

    public function test_a_refreshed_collar_summary_adds_collars_and_assays(): void
    {
        $types = $this->affectedTypes([
            ['view_name' => 'silver.mv_collar_summary', 'status' => 'completed'],
        ]);

        $this->assertContains('collars', $types);
        $this->assertContains('assays', $types);
    }

    public function test_a_failed_view_contributes_nothing(): void
    {
        $types = $this->affectedTypes([
            ['view_name' => 'silver.mv_collar_summary', 'status' => 'failed'],
        ]);

        $this->assertNotContains('collars', $types);
        $this->assertNotContains('assays', $types);
    }

    public function test_types_are_unique(): void
    {
        // Two completed views both mapping to 'collars' must not produce
        // 'collars' twice — the receiving hooks use includes(), but a
        // duplicated list is a sign the dedupe was dropped.
        $types = $this->affectedTypes([
            ['view_name' => 'silver.mv_collar_summary', 'status' => 'completed'],
            ['view_name' => 'silver.mv_collar_summary', 'status' => 'completed'],
        ]);

        $this->assertSame(array_values(array_unique($types)), $types);
    }

    public function test_the_list_is_a_plain_list_not_a_sparse_array(): void
    {
        // It is JSON-encoded into the broadcast payload; a sparse array
        // serialises as an object and `affected_types.includes(...)` on the
        // frontend then throws.
        $types = $this->affectedTypes([
            ['view_name' => 'silver.other_view', 'status' => 'completed'],
        ]);

        $this->assertSame(range(0, count($types) - 1), array_keys($types));
    }

    /**
     * Each of these is filtered on by name in a `useWorkspaceDataUpdated`
     * callback somewhere in resources/js. Dropping one silently stops that
     * page from ever refreshing itself.
     */
    #[DataProvider('subscribedTypes')]
    public function test_every_type_a_page_filters_on_is_emitted(string $type): void
    {
        $this->assertContains($type, $this->affectedTypes([
            ['view_name' => 'silver.mv_collar_summary', 'status' => 'completed'],
        ]));
    }

    /** @return array<string, array{string}> */
    public static function subscribedTypes(): array
    {
        return [
            'Reports + Sources' => ['reports'],
            'Reports quality panel' => ['quality'],
            'DrillholeDetail collars' => ['collars'],
            'DrillholeDetail assays' => ['assays'],
            'DrillholeDetail structures' => ['structures'],
            'DrillholeDetail curves' => ['curves'],
            'DrillReview queue' => ['review_queue'],
            'Reasoning hypotheses' => ['hypotheses'],
            'WhatChangedFeed' => ['what_changed'],
        ];
    }
}
