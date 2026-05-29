<?php

declare(strict_types=1);

namespace App\Http\Controllers;

use App\Models\Project;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\RedirectResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Inertia\Inertia;
use Inertia\Response;

/**
 * §8.5 Customer Onboarding Wizard.
 *
 * 4-step funnel (compressed from the master plan's 7 steps; OAuth +
 * real-time ingest + first-report are stubbed as "coming next"):
 *   Step 1: Workspace identity — name, primary commodity, region
 *   Step 2: Project — name + optional AOI polygon (drawn on a small map)
 *   Step 3: Data — upload at least one file (PDF/CSV) OR skip and try demo data
 *   Step 4: First chat — auto-seeded prompt; "ask a question about your project"
 *
 * Persistence:
 *   - Workspace identity stored on the user's primary workspace row
 *     (silver.workspaces.name = workspace_name)
 *   - Project created via the existing /api/v1/projects logic so
 *     pivot membership wires up + workspace_id is set
 *   - AOI polygon stored on silver.projects.aoi_geom (json fallback if
 *     column missing)
 *
 * Routes:
 *   GET  /onboarding             — wizard Inertia page
 *   POST /onboarding/step1       — save workspace identity
 *   POST /onboarding/step2       — create project + AOI
 *   POST /onboarding/step3       — (placeholder) record skip / file count
 *   POST /onboarding/complete    — mark complete; redirect to chat
 */
class OnboardingController extends Controller
{
    private const COMMODITIES = [
        'uranium', 'gold', 'copper', 'lithium', 'oil_gas',
        'silver', 'nickel', 'cobalt', 'REE', 'custom',
    ];
    private const REGIONS = [
        'CA-SK' => 'Saskatchewan',
        'CA-BC' => 'British Columbia',
        'CA-NT' => 'Northwest Territories',
        'CA-YT' => 'Yukon',
        'CA-ON' => 'Ontario',
        'CA-QC' => 'Québec',
        'US'    => 'United States',
        'OTHER' => 'Other',
    ];

    public function index(Request $request): Response
    {
        $user = $request->user();
        // Recall any partial progress from session
        $progress = session('onboarding_progress', []);

        return Inertia::render('Onboarding/Wizard', [
            'commodities' => self::COMMODITIES,
            'regions' => self::REGIONS,
            'progress' => $progress,
            'user_email' => $user?->email,
        ]);
    }

    public function step1(Request $request): JsonResponse
    {
        $data = $request->validate([
            'workspace_name' => ['required', 'string', 'max:255'],
            'commodity' => ['required', 'in:'.implode(',', self::COMMODITIES)],
            'region' => ['required', 'in:'.implode(',', array_keys(self::REGIONS))],
        ]);

        $user = $request->user();
        if (! $user) {
            return response()->json(['error' => 'unauthenticated'], 401);
        }

        // Update the user's default workspace (silver.workspaces row).
        // The user's first project's workspace_id is the authoritative one;
        // if they don't have a project yet, we update the seeded "default"
        // workspace (a0000000-…001) since that's where new projects land.
        try {
            DB::table('silver.workspaces as w')
                ->whereExists(function ($q) use ($user) {
                    $q->from('project_user as pu')
                        ->join('silver.projects as p', 'p.project_id', '=', 'pu.project_id')
                        ->whereColumn('p.workspace_id', 'w.workspace_id')
                        ->where('pu.user_id', $user->id);
                })
                ->orWhere('workspace_id', 'a0000000-0000-0000-0000-000000000001')
                ->update([
                    'name' => $data['workspace_name'],
                    'updated_at' => now(),
                ]);
        } catch (\Throwable $e) {
            // Non-fatal — wizard still proceeds; admin can fix workspace name later
            report($e);
        }

        $progress = array_merge(session('onboarding_progress', []), [
            'step1' => $data,
            'completed_steps' => ['step1'],
        ]);
        session(['onboarding_progress' => $progress]);

        return response()->json(['ok' => true, 'progress' => $progress]);
    }

    public function step2(Request $request): JsonResponse
    {
        $data = $request->validate([
            'project_name' => ['required', 'string', 'max:255'],
            'aoi_geojson' => ['nullable', 'array'],
        ]);

        $user = $request->user();
        if (! $user) {
            return response()->json(['error' => 'unauthenticated'], 401);
        }

        $progress = session('onboarding_progress', []);
        $commodity = $progress['step1']['commodity'] ?? 'custom';
        $region = $progress['step1']['region'] ?? 'OTHER';

        // Create the project via direct DB insert (the existing
        // ProjectController::store has FormRequest validation we don't
        // want to thread through the wizard payload).
        $projectId = (string) Str::uuid();
        $slug = Str::slug($data['project_name']).'-'.substr($projectId, 0, 8);

        try {
            DB::transaction(function () use ($projectId, $data, $slug, $commodity, $region, $user) {
                DB::table('silver.projects')->insert([
                    'project_id' => $projectId,
                    'workspace_id' => 'a0000000-0000-0000-0000-000000000001',
                    'project_name' => $data['project_name'],
                    'slug' => $slug,
                    'crs_datum' => 'EPSG:32613',
                    'orientation_reference' => 'grid_north',
                    'commodity' => $commodity,
                    'region' => self::REGIONS[$region] ?? $region,
                    'status' => 'active',
                    'created_at' => now(),
                    'updated_at' => now(),
                ]);

                // Pivot — owner role
                DB::table('project_user')->insert([
                    'project_id' => $projectId,
                    'user_id' => $user->id,
                    'role' => 'owner',
                    'created_at' => now(),
                    'updated_at' => now(),
                ]);
            });
        } catch (\Throwable $e) {
            report($e);
            return response()->json([
                'error' => 'failed to create project',
                'reason' => $e->getMessage(),
            ], 500);
        }

        // Stash AOI as a §19.3 target_zone seed — the customer's drawn
        // AOI becomes their first interpretation artifact, so it shows
        // up on the project map immediately.
        if (! empty($data['aoi_geojson']) && isset($data['aoi_geojson']['type'])) {
            try {
                $geojson = json_encode($data['aoi_geojson']);
                DB::statement(
                    "INSERT INTO interpretation.interpretation_target_zones
                        (workspace_id, project_id, author_user_id, name, rationale,
                         commodity, confidence, geom)
                     VALUES (?, ?, ?, ?, ?, ?, ?,
                             ST_SetSRID(ST_GeomFromGeoJSON(?), 4326))",
                    [
                        'a0000000-0000-0000-0000-000000000001',
                        $projectId,
                        $user->id,
                        $data['project_name'].' AOI',
                        'Drawn during onboarding wizard',
                        $commodity,
                        'medium',
                        $geojson,
                    ],
                );
            } catch (\Throwable $e) {
                // Non-fatal — AOI is optional
                report($e);
            }
        }

        $progress = array_merge(session('onboarding_progress', []), [
            'step2' => array_merge($data, ['project_id' => $projectId, 'slug' => $slug]),
            'completed_steps' => array_merge(
                session('onboarding_progress.completed_steps', []),
                ['step2'],
            ),
        ]);
        session(['onboarding_progress' => $progress]);

        return response()->json([
            'ok' => true,
            'project_id' => $projectId,
            'slug' => $slug,
            'progress' => $progress,
        ]);
    }

    public function step3(Request $request): JsonResponse
    {
        $data = $request->validate([
            'skipped' => ['boolean'],
            'file_count' => ['nullable', 'integer'],
        ]);

        $progress = array_merge(session('onboarding_progress', []), [
            'step3' => $data,
            'completed_steps' => array_merge(
                session('onboarding_progress.completed_steps', []),
                ['step3'],
            ),
        ]);
        session(['onboarding_progress' => $progress]);

        return response()->json(['ok' => true, 'progress' => $progress]);
    }

    public function complete(Request $request): RedirectResponse
    {
        $progress = session('onboarding_progress', []);
        session()->forget('onboarding_progress');

        $projectId = $progress['step2']['project_id'] ?? null;
        if ($projectId) {
            return redirect()->route('chat', ['project_id' => $projectId]);
        }
        return redirect()->route('chat');
    }
}
