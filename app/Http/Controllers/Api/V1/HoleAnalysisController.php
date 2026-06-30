<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use App\Models\Collar;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;

/**
 * Per-hole geological analysis payload.
 *
 *   GET /api/v1/projects/{projectId}/holes/{holeIdOrCollarId}/analysis
 *
 * Returns everything the HoleAnalysisPanel needs in one round trip:
 *
 *   {
 *     "collar":      { hole_id, total_depth, azimuth, dip, easting, northing, elevation, ... },
 *     "surveys":     [{ depth, azimuth, dip, survey_method }, ...],     // Azimuth/Dip-vs-Depth + Spiral
 *     "structures":  [{ depth, structure_type, alpha_angle, beta_angle,
 *                       true_dip, dip_direction, description }, ...],  // Stereonet
 *     "geochem":     [{ from_depth, to_depth, sio2_wt_pct, mgo_wt_pct,
 *                       eu_anomaly, cia, mg_number, ree_json }, ...],  // GeochemPlots
 *   }
 *
 * Authorization: the caller must own the parent project (same rule as
 * CollarController — Project::findOrFail falls back to 404 rather than
 * leaking that another user's project exists).
 *
 * The hole identifier in the URL may be either a `hole_id` (human-friendly
 * string, e.g. "PLS-22-08") or a `collar_id` UUID — the controller tries
 * UUID lookup first and falls back to hole_id. This matches how the chat
 * viz payload already references holes.
 */
class HoleAnalysisController extends Controller
{
    public function show(Request $request, string $projectId, string $holeIdOrCollarId): JsonResponse
    {
        // Auth + project scoping: only the caller's own projects.
        $userProjectIds = $request->user()->projects()->pluck('silver.projects.project_id');
        if (! $userProjectIds->contains($projectId)) {
            return response()->json(['error' => 'project_not_found'], 404);
        }

        // Resolve by collar_id UUID first; fall back to hole_id string.
        $isUuid = preg_match('/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i', $holeIdOrCollarId);
        $collar = null;

        if ($isUuid) {
            $collar = Collar::selectRaw(
                '*, ST_X(ST_Transform(geom, 4326)) AS longitude, ST_Y(ST_Transform(geom, 4326)) AS latitude',
            )
                ->where('project_id', $projectId)
                ->where('collar_id', $holeIdOrCollarId)
                ->first();
        }
        if ($collar === null) {
            $collar = Collar::selectRaw(
                '*, ST_X(ST_Transform(geom, 4326)) AS longitude, ST_Y(ST_Transform(geom, 4326)) AS latitude',
            )
                ->where('project_id', $projectId)
                ->where('hole_id', $holeIdOrCollarId)
                ->first();
        }
        if ($collar === null) {
            return response()->json(['error' => 'hole_not_found'], 404);
        }

        $collarId = $collar->collar_id;

        // Surveys: raw DB rows in depth order. The orientation-spiral viz
        // needs contiguous survey points to interpolate a trajectory.
        $surveys = DB::table('silver.surveys')
            ->where('collar_id', $collarId)
            ->orderBy('depth')
            ->get([
                'depth',
                'azimuth',
                'dip',
                'survey_method',
            ]);

        // Structural measurements for the stereonet. Orientation is carried
        // as `true_dip` + `dip_direction` (the field geologists fill in);
        // alpha/beta are the raw core-angle measurements before orientation
        // correction.
        $structures = DB::table('silver.structures')
            ->where('collar_id', $collarId)
            ->orderBy('depth')
            ->get([
                'depth',
                'structure_type',
                'alpha_angle',
                'beta_angle',
                'true_dip',
                'dip_direction',
                'description',
            ]);

        // Geochemistry — major oxides + computed petrology indices for the
        // four scatter plots (Mg# vs SiO₂, Eu/Eu* vs SiO₂, CIA vs Depth,
        // (La/Yb)_N vs Depth). REE blob stays JSONB so the frontend can
        // compute (La/Yb)_N from chondrite-normalised values it knows.
        $geochem = DB::table('silver.geochemistry')
            ->where('collar_id', $collarId)
            ->orderBy('from_depth')
            ->get([
                'from_depth',
                'to_depth',
                'sio2_wt_pct',
                'al2o3_wt_pct',
                'fe2o3_wt_pct',
                'mgo_wt_pct',
                'cao_wt_pct',
                'na2o_wt_pct',
                'k2o_wt_pct',
                'mg_number',
                'cia',
                'eu_anomaly',
                'ree_json',
            ]);

        return response()->json([
            'collar' => [
                'collar_id' => $collar->collar_id,
                'hole_id' => $collar->hole_id,
                'hole_type' => $collar->hole_type,
                'status' => $collar->status,
                'total_depth' => $collar->total_depth,
                'azimuth' => $collar->azimuth,
                'dip' => $collar->dip,
                'elevation' => $collar->elevation,
                'easting' => $collar->easting,
                'northing' => $collar->northing,
                'longitude' => $collar->longitude,
                'latitude' => $collar->latitude,
                'drill_date' => $collar->drill_date,
            ],
            'surveys' => $surveys,
            'structures' => $structures,
            'geochem' => $geochem,
        ]);
    }
}
