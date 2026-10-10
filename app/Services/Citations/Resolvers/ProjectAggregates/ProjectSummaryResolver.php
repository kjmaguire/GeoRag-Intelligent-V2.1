<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers\ProjectAggregates;

/**
 * `silver.project_summary:project=<uuid>:rows=<n>:first_row=<id>`, from
 * query_project_summary (ADR-0007 PR-1): the project's data broken down by
 * source table, year and technique.
 */
final class ProjectSummaryResolver extends AbstractProjectAggregateResolver
{
    public static function prefix(): string
    {
        return 'silver.project_summary:';
    }

    protected function sourceType(): string
    {
        return 'project_summary';
    }

    protected function describe(object $project, array $fields): array
    {
        $rows = $this->count($fields, 'rows');

        return [
            'title' => "Project Data Summary: {$project->project_name}",
            'text' => sprintf(
                'The breakdown of %s\'s data by source, year and technique (%s rows) that the answer drew on.',
                $project->project_name,
                $rows ?? '?',
            ),
            'metadata' => [
                'project_id' => $project->project_id,
                'row_count' => $rows,
            ],
        ];
    }
}
