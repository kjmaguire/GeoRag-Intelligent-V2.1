<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Support\SetsWorkspaceRlsContext;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/LakehouseController — single-page Bronze + Silver + Gold inventory.
 *
 * Phase-22 §B/S/G build-out. Renders one consolidated view of every layer
 * the project has data in, with row counts, the most-recently-ingested rows,
 * and drill-in links to existing detail surfaces (IngestQuality, Sources,
 * Hole Compare, Cross-Section panels, etc.).
 *
 * Scope decisions:
 *
 *   1. Single page rather than three (Bronze viewer, Silver browse, Gold
 *      dashboard). The Foundry left rail already crowds the surface; one
 *      "Lakehouse" entry collapses three surfaces into anchored sections.
 *   2. Reads counts only (cheap aggregate queries) + 10 most-recent rows
 *      per table. Heavy detail lives behind drill-ins.
 *   3. Defensive try/catch around each schema — if a table doesn't exist
 *      in the current DB (mid-migration), the page still renders with the
 *      missing layer marked as "schema not provisioned" rather than 500.
 *   4. Tenancy:
 *        - silver/gold tables — RLS enforces workspace isolation; project
 *          scoping is added via an explicit `project_id` filter.
 *        - bronze.source_files — has `workspace_id` but no RLS yet; we
 *          add the workspace filter explicitly here (matches the
 *          stated assumption and avoids cross-tenant counts).
 *        - bronze.ingest_manifest / bronze.provenance — no scoping
 *          columns and no RLS; counts are inherently global. Surfaced
 *          to the UI with `scope: 'global'` so users see the badge.
 */
class LakehouseController extends Controller
{
    use SetsWorkspaceRlsContext;

    public function show(Request $request, string $slug): Response
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()->projects()
            ->where('silver.projects.project_id', $project->project_id)
            ->firstOrFail();

        // Pin the RLS GUC for the rest of this request's queries.
        // Without this, the silver/gold RLS policies fall through to their
        // permissive `current_setting(..., true) IS NULL` clause and the
        // page would see every workspace's rows.
        $workspaceId = (string) DB::table('silver.projects')
            ->where('project_id', $project->project_id)
            ->value('workspace_id');

        return $this->withWorkspaceRls($workspaceId, function () use ($project, $workspaceId) {
            $bronze = $this->bronzeSummary($project->project_id, $workspaceId);
            $silver = $this->silverSummary($project->project_id);
            $gold = $this->goldSummary($project->project_id);

            return Inertia::render('Foundry/Lakehouse', [
                'project' => [
                    'project_id' => $project->project_id,
                    'project_name' => $project->project_name,
                    'slug' => $project->slug,
                ],
                'bronze' => $bronze,
                'silver' => $silver,
                'gold' => $gold,
            ]);
        });
    }

    /**
     * Bronze layer is heterogeneous re: tenancy:
     *
     *   - bronze.source_files     — has workspace_id, no project_id.
     *     Scope to current workspace so a user can't see other tenants'
     *     uploads. RLS is not enabled on this table (see Option 2 ticket).
     *   - bronze.ingest_manifest  — no workspace_id, no project_id.
     *     Schema-wide audit log; currently global. Surface as scope='global'.
     *   - bronze.provenance       — no workspace_id, no project_id.
     *     Schema-wide lineage; currently global. Surface as scope='global'.
     *
     * The scope label flows through to the UI as a Pill so users know
     * the count's blast radius without reading the schema.
     *
     * @return array<string, mixed>
     */
    private function bronzeSummary(string $projectId, string $workspaceId): array
    {
        $configs = [
            'source_files' => [
                'qualified' => 'bronze.source_files',
                'scopeColumn' => 'workspace_id',
                'scopeValue' => $workspaceId,
                'scope' => 'workspace',
            ],
            'ingest_manifest' => [
                'qualified' => 'bronze.ingest_manifest',
                'scopeColumn' => null,
                'scopeValue' => null,
                'scope' => 'global',
            ],
            'provenance' => [
                'qualified' => 'bronze.provenance',
                'scopeColumn' => null,
                'scopeValue' => null,
                'scope' => 'global',
            ],
        ];

        $summary = [];
        foreach ($configs as $key => $cfg) {
            $row = $this->tableSummary(
                $cfg['qualified'],
                $cfg['scopeValue'] ?? $projectId,
                projectColumn: $cfg['scopeColumn'],
            );
            $row['scope'] = $cfg['scope'];
            $summary[$key] = $row;
        }

        return $summary;
    }

    /** @return array<string, mixed> */
    private function silverSummary(string $projectId): array
    {
        $tables = [
            'collars' => 'silver.collars',
            'lithology_intervals' => 'silver.lithology_intervals',
            'assays_v2' => 'silver.assays_v2',
            'structures' => 'silver.structures',
            'geophysics_surveys' => 'silver.geophysics_surveys',
            'spatial_features' => 'silver.spatial_features',
            'raster_layers' => 'silver.raster_layers',
            'reports' => 'silver.reports',
        ];

        $summary = [];
        foreach ($tables as $key => $qualified) {
            $row = $this->tableSummary($qualified, $projectId, projectColumn: 'project_id');
            $row['scope'] = 'project';
            $summary[$key] = $row;
        }

        return $summary;
    }

    /** @return array<string, mixed> */
    private function goldSummary(string $projectId): array
    {
        $tables = [
            'drillhole_intervals_visual' => 'gold.drillhole_intervals_visual',
            'cross_section_panels' => 'gold.cross_section_panels',
            'structure_measurements_visual' => 'gold.structure_measurements_visual',
            'h3_density' => 'gold.h3_density',
        ];

        $summary = [];
        foreach ($tables as $key => $qualified) {
            $row = $this->tableSummary($qualified, $projectId, projectColumn: 'project_id');
            $row['scope'] = 'project';
            $summary[$key] = $row;
        }

        return $summary;
    }

    /**
     * Run a defensive (table_exists) count + recent-rows query.
     *
     * Different layers use different "recency" columns: silver/gold tables
     * have `created_at`; bronze.source_files uses `ingested_at`; bronze.
     * ingest_manifest uses `indexed_at`; bronze.provenance uses `ingested_at`.
     * We probe information_schema.columns for whichever timestamp-ish column
     * exists and fall back to no ordering if none are present (the page just
     * shows the first 10 rows in physical order).
     *
     * @return array{exists: bool, count: int, recent: array<int, array<string, mixed>>}
     */
    private function tableSummary(string $qualified, string $projectId, ?string $projectColumn): array
    {
        try {
            [$schema, $table] = explode('.', $qualified, 2);
            $exists = DB::table('information_schema.tables')
                ->where('table_schema', $schema)
                ->where('table_name', $table)
                ->exists();
            if (! $exists) {
                return ['exists' => false, 'count' => 0, 'recent' => []];
            }

            $countQuery = DB::table($qualified);
            if ($projectColumn !== null) {
                $countQuery->where($projectColumn, $projectId);
            }
            $count = (int) $countQuery->count();

            $orderColumn = $this->recencyColumn($schema, $table);

            $recentQuery = DB::table($qualified);
            if ($projectColumn !== null) {
                $recentQuery->where($projectColumn, $projectId);
            }
            if ($orderColumn !== null) {
                $recentQuery->orderByDesc($orderColumn);
            }
            $recent = $recentQuery
                ->limit(10)
                ->get()
                ->map(fn ($row) => (array) $row)
                ->all();

            return ['exists' => true, 'count' => $count, 'recent' => $recent];
        } catch (\Throwable $e) {
            return ['exists' => false, 'count' => 0, 'recent' => [], 'error' => $e->getMessage()];
        }
    }

    /** First-match timestamp-ish column on the target table, or null. */
    private function recencyColumn(string $schema, string $table): ?string
    {
        static $candidates = ['created_at', 'computed_at', 'ingested_at', 'indexed_at', 'updated_at'];
        $found = DB::table('information_schema.columns')
            ->where('table_schema', $schema)
            ->where('table_name', $table)
            ->whereIn('column_name', $candidates)
            ->pluck('column_name')
            ->all();
        foreach ($candidates as $name) {
            if (in_array($name, $found, true)) {
                return $name;
            }
        }

        return null;
    }
}
