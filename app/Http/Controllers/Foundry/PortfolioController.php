<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Models\QueryAuditLog;
use Carbon\CarbonImmutable;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/PortfolioController — org-level rollup landing for the redesign.
 *
 * Reads real data from silver.projects + silver.collars + audit.query_audit_log.
 * No demo seeders. Empty workspaces render an empty-state CTA.
 *
 * Plan rev 7 — Wave 1.
 */
class PortfolioController extends Controller
{
    public function show(Request $request): Response
    {
        $user = $request->user();
        $projectIds = $user->projects()->pluck('silver.projects.project_id');

        $projects = Project::whereIn('project_id', $projectIds)
            ->orderBy('project_name')
            ->get()
            ->map(fn ($p) => [
                'project_id' => $p->project_id,
                'project_name' => $p->project_name,
                'slug' => $p->slug,
                'region' => $p->region,
                'commodity' => $p->commodity,
                'status' => is_object($p->status) ? $p->status->value : ($p->status ?? 'active'),
                'crs_epsg' => $p->crs_epsg,
                'data_version' => $p->data_version ?? 0,
                'workspace_id' => $p->workspace_id,
                'created_at' => $p->created_at?->toIso8601String(),
                'updated_at' => $p->updated_at?->toIso8601String(),
            ])
            ->values();

        $activeCount = $projects->where('status', 'active')->count();
        $archivedCount = $projects->where('status', 'archived')->count();
        // After mapping the enum to its string value above, the comparison works.

        $collarCount = DB::table('silver.collars')
            ->whereIn('project_id', $projectIds)
            ->count();

        $queries24h = QueryAuditLog::where('user_id', $user->id)
            ->whereIn('project_id', $projectIds)
            ->where('created_at', '>=', CarbonImmutable::now()->subDay())
            ->count();

        $queries30d = QueryAuditLog::where('user_id', $user->id)
            ->whereIn('project_id', $projectIds)
            ->where('created_at', '>=', CarbonImmutable::now()->subDays(30))
            ->count();

        $kpis = [
            ['label' => 'PROJECTS', 'value' => (string) $projects->count(), 'sub' => "{$activeCount} active · {$archivedCount} archived"],
            ['label' => 'HOLES IN GROUND', 'value' => (string) $collarCount, 'sub' => 'across portfolio'],
            ['label' => 'QUERIES · 24H', 'value' => (string) $queries24h, 'sub' => "{$queries30d} in 30d", 'tone' => 'accent'],
            ['label' => 'CRS', 'value' => $projects->isNotEmpty() ? ('EPSG:'.(string) ($projects->first()['crs_epsg'] ?? '—')) : '—', 'sub' => 'primary projection'],
            ['label' => 'WORKSPACE', 'value' => (string) ($user->workspace_id ?? 'default'), 'sub' => 'multi-tenant scope'],
        ];

        $activity = QueryAuditLog::where('user_id', $user->id)
            ->whereIn('project_id', $projectIds)
            ->orderByDesc('created_at')
            ->limit(15)
            ->get()
            ->map(fn ($q) => [
                'id' => (string) $q->id,
                'timestamp' => $q->created_at?->diffForHumans() ?? '—',
                'actor' => 'You',
                'project' => $q->project_id ? (string) $q->project_id : null,
                'kind' => 'query',
                'text' => substr((string) ($q->query_text ?? ''), 0, 140),
            ])
            ->values();

        return Inertia::render('Foundry/Portfolio', [
            'org_name' => 'Workspace',
            // Top-level workspace_id so the Reverb subscription has the
            // channel target. Falls back to the seeded default workspace
            // (matches QueryController's fallback) when the user row
            // doesn't carry one (legacy accounts).
            'workspace_id' => (string) ($user->workspace_id ?? 'a0000000-0000-0000-0000-000000000001'),
            'projects' => $projects,
            'kpis' => $kpis,
            'activity' => $activity,
            'empty' => $projects->isEmpty(),
        ]);
    }
}
