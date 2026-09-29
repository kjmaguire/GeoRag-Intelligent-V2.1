<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Api\V1\UploadController;
use App\Http\Controllers\Controller;
use App\Models\Project;
use App\Services\IngestionSnapshot;
use Carbon\CarbonImmutable;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Inertia\Inertia;
use Inertia\Response;

/**
 * Foundry/IngestionRunsController — per-project ingestion progress surface.
 *
 * The snapshot itself — silver.ingest_progress rows, the bronze listing, and
 * which uploaded object produced which report — is built by
 * App\Services\IngestionSnapshot, which the Overview's "X files ingesting"
 * tile reads too, so the two can never disagree.
 *
 * Two endpoints:
 *   GET /projects/{slug}/ingestion-runs        → Inertia view
 *   GET /projects/{slug}/ingestion-runs.json   → JSON for the 5s poll
 */
class IngestionRunsController extends Controller
{
    public function __construct(
        private readonly IngestionSnapshot $snapshot,
    ) {}

    public function show(Request $request, string $slug): Response
    {
        $project = $this->loadProject($request, $slug);

        return Inertia::render('Foundry/IngestionRuns', [
            'project' => [
                'project_id' => $project->project_id,
                'project_name' => $project->project_name,
                'slug' => $project->slug,
            ],
            'runs' => $this->snapshot->build($project->project_id, (string) $project->workspace_id),
        ]);
    }

    public function progress(Request $request, string $slug): JsonResponse
    {
        $project = $this->loadProject($request, $slug);

        return response()->json([
            // The poll DOES include the bronze upload listing — see
            // IngestionSnapshot::listUploads()'s cache. Skipping it (as this
            // endpoint did between 2f332f2 and 2026-08-19) made a freshly-
            // uploaded file appear on page load and then vanish on the very
            // next poll.
            'runs' => $this->snapshot->build(
                $project->project_id,
                (string) $project->workspace_id,
            ),
            'fetched_at' => CarbonImmutable::now()->toIso8601String(),
        ]);
    }

    /**
     * Re-run a tabular ingest with a column mapping the user confirmed.
     *
     * The gap this closes: when alias matching misses a REQUIRED column the
     * whole file is refused, and the only remedy the app offered was
     * "rename the key columns and re-upload" — which asks a geologist to
     * edit their source data to suit our vocabulary, and which they cannot
     * do at all for a file they received from someone else.
     *
     * The bytes are already in bronze, so nothing is re-uploaded. This
     * re-triggers the SAME workflow against the SAME object with the
     * mapping attached, and ingest_tabular applies it ahead of the built-in
     * spellings.
     *
     * `minio_key` is validated against this project's own prefix rather
     * than trusted. It arrives from the browser, and a key is the only
     * thing standing between "re-run my file" and "ingest an object from
     * another project into mine" — RLS scopes the WRITES, but the read
     * would already have happened.
     */
    public function remap(Request $request, string $slug): JsonResponse
    {
        $project = $this->loadProject($request, $slug);

        $validated = $request->validate([
            'minio_key' => ['required', 'string', 'max:1024'],
            'sheet_type' => ['required', 'string', 'in:collar,survey,lithology,sample'],
            'column_map' => ['required', 'array', 'min:1'],
            // Canonical field names are ours, so they are constrained to a
            // conservative shape rather than accepted as arbitrary keys.
            'column_map.*' => ['required', 'string', 'max:255'],
        ]);

        $expectedPrefix = '/'.$project->project_id.'/';
        if (! str_contains($validated['minio_key'], $expectedPrefix)) {
            return response()->json([
                'message' => 'That file does not belong to this project.',
            ], 422);
        }

        foreach (array_keys($validated['column_map']) as $field) {
            if (preg_match('/^[a-z][a-z0-9_]{0,63}$/', (string) $field) !== 1) {
                return response()->json([
                    'message' => "Unrecognised field name: {$field}",
                ], 422);
            }
        }

        $result = app(UploadController::class)->dispatchTabularRemap(
            // loadProject() has already dereferenced the user, and the route
            // is behind auth — the fallback is for the type, not a real case.
            userId: (string) ($request->user()?->getAuthIdentifier() ?? 'unknown'),
            workspaceId: (string) $project->workspace_id,
            projectId: (string) $project->project_id,
            minioKey: $validated['minio_key'],
            sheetType: $validated['sheet_type'],
            columnMap: [$validated['sheet_type'] => $validated['column_map']],
        );

        return response()->json($result, $result['dispatched'] ? 202 : 502);
    }

    private function loadProject(Request $request, string $slug): Project
    {
        $project = Project::where('slug', $slug)->firstOrFail();
        $request->user()
            ->projects()
            ->where('silver.projects.project_id', $project->project_id)
            ->firstOrFail();

        return $project;
    }
}
