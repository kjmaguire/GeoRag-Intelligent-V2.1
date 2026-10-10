<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers\ProjectAggregates;

/**
 * `silver.coverage_gap:project=<uuid>:indexed=<n>:processed=<n>:attrs=<n>`,
 * from query_coverage_gap: how much of what was uploaded became searchable,
 * and which drill-data attributes the project's holes have.
 */
final class CoverageGapResolver extends AbstractProjectAggregateResolver
{
    public static function prefix(): string
    {
        return 'silver.coverage_gap:';
    }

    protected function sourceType(): string
    {
        return 'coverage_gap';
    }

    protected function describe(object $project, array $fields): array
    {
        $indexed = $this->count($fields, 'indexed');
        $processed = $this->count($fields, 'processed');
        $attributes = $this->count($fields, 'attrs');

        return [
            'title' => "Data Coverage: {$project->project_name}",
            'text' => sprintf(
                'Coverage check the answer drew on: %s files ingested, %s of them produced a report, and %s drill-data attributes measured across the project\'s holes.',
                $indexed ?? '?',
                $processed ?? '?',
                $attributes ?? '?',
            ),
            'metadata' => [
                'project_id' => $project->project_id,
                'files_ingested' => $indexed,
                'files_with_report' => $processed,
                'attributes_measured' => $attributes,
            ],
        ];
    }
}
