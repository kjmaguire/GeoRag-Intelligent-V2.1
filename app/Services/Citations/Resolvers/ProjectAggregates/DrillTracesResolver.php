<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers\ProjectAggregates;

/**
 * `silver.drill_traces:project=<uuid>:holes=<n>:first_collar=<uuid>:hole_filter=<hole|all>`,
 * from query_drill_traces_3d (ADR-0007 PR-4): the desurveyed traces behind
 * the chat's 3D drill card.
 */
final class DrillTracesResolver extends AbstractProjectAggregateResolver
{
    public static function prefix(): string
    {
        return 'silver.drill_traces:';
    }

    protected function sourceType(): string
    {
        return 'drill_traces';
    }

    protected function describe(object $project, array $fields): array
    {
        $holes = $this->count($fields, 'holes');
        $filter = $fields['hole_filter'] ?? 'all';
        // A hole id, or 'all'. Anything else is not echoed.
        $holeFilter = $filter !== 'all' && preg_match('/^[A-Za-z0-9 _.\/\-]{1,64}$/', $filter) === 1 ? $filter : null;

        return [
            'title' => "Drill Traces: {$project->project_name}",
            'text' => sprintf(
                '3D drill traces for %s: %s holes%s.',
                $project->project_name,
                $holes ?? '?',
                $holeFilter !== null ? ", filtered to {$holeFilter}" : '',
            ),
            'metadata' => [
                'project_id' => $project->project_id,
                'hole_count' => $holes,
                'hole_id' => $holeFilter,
            ],
        ];
    }
}
