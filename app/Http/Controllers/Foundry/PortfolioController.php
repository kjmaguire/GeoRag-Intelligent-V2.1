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
            ['label' => 'WORKSPACE', 'value' => (string) ($projects->first()['workspace_id'] ?? 'default'), 'sub' => 'multi-tenant scope'],
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

        // Top-level workspace_id for the Reverb activity subscription.
        // Previously fell back to `$user->workspace_id ?? <hardcoded>`,
        // but User has no workspace_id column — so the hardcoded
        // default tenant fired for every user, and after the
        // 2026-06-03 Theme F fix (workspace.{}.activity scoping) the
        // subscription is denied for any tenant whose projects aren't
        // in the default workspace. Derive from the first project the
        // user actually owns; null when they have none (UI then
        // skips the Reverb subscription rather than firing it at a
        // foreign workspace). See AUDIT_AND_FIX_REPORT.md Theme H.
        $resolvedWorkspaceId = $projects->isEmpty()
            ? null
            : (string) $projects->first()['workspace_id'];

        return Inertia::render('Foundry/Portfolio', [
            'org_name' => 'Workspace',
            'workspace_id' => $resolvedWorkspaceId,
            'projects' => $projects,
            'kpis' => $kpis,
            'activity' => $activity,
            'empty' => $projects->isEmpty(),
        ]);
    }
}
