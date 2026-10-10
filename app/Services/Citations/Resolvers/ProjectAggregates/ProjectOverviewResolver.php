<?php

declare(strict_types=1);

namespace App\Services\Citations\Resolvers\ProjectAggregates;

use Illuminate\Support\Facades\DB;

/**
 * `silver.projects:slug=<slug>:company=<company>:curves=<n>:reports=<n>`,
 * from query_project_overview. The only aggregate citation keyed by slug
 * rather than project id.
 */
final class ProjectOverviewResolver extends AbstractProjectAggregateResolver
{
    public static function prefix(): string
    {
        return 'silver.projects:';
    }

    protected function sourceType(): string
    {
        return 'project_overview';
    }

    /**
     * @param array<string, string> $fields
     * @param list<string> $projectIds
     */
    protected function project(array $fields, string $workspaceId, array $projectIds): ?object
    {
        $slug = $fields['slug'] ?? '';

        // 'unknown' is what FastAPI writes when the project had no slug.
        if ($slug === '' || $slug === 'unknown' || strlen($slug) > 255) {
            return null;
        }

        return DB::table('silver.projects')
            ->where('slug', $slug)
            ->where('workspace_id', $workspaceId)
            ->whereIn('project_id', $projectIds)
            ->first(['project_id', 'project_name', 'company']);
    }

    protected function describe(object $project, array $fields): array
    {
        $reports = $this->count($fields, 'reports');
        $curves = $this->count($fields, 'curves');
        $company = is_string($project->company ?? null) && $project->company !== '' ? " ({$project->company})" : '';

        return [
            'title' => "Project Overview: {$project->project_name}",
            'text' => sprintf(
                'Project metadata for %s%s. When the answer was written the project had %s reports and %s distinct well-log curves.',
                $project->project_name,
                $company,
                $reports ?? '?',
                $curves ?? '?',
            ),
            'metadata' => [
                'project_id' => $project->project_id,
                'report_count' => $reports,
                'curve_count' => $curves,
            ],
        ];
    }
}
