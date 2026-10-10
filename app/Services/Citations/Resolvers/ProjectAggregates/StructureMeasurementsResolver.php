<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers\ProjectAggregates;

/**
 * `gold.structure_measurements_visual:project=<uuid>:points=<n>:first=<id>`,
 * from query_stereonet (ADR-0007 PR-2): the oriented structural
 * measurements behind the chat's stereonet.
 */
final class StructureMeasurementsResolver extends AbstractProjectAggregateResolver
{
    public static function prefix(): string
    {
        return 'gold.structure_measurements_visual:';
    }

    protected function sourceType(): string
    {
        return 'structure_measurements';
    }

    protected function describe(object $project, array $fields): array
    {
        $points = $this->count($fields, 'points');

        return [
            'title' => "Structural Measurements: {$project->project_name}",
            'text' => sprintf(
                'Oriented structural measurements for %s plotted on the stereonet: %s points.',
                $project->project_name,
                $points ?? '?',
            ),
            'metadata' => [
                'project_id' => $project->project_id,
                'point_count' => $points,
            ],
        ];
    }
}
