<?php

declare(strict_types=1);

namespace App\Http\Controllers\Foundry;

use App\Http\Controllers\Controller;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Inertia\Inertia;
use Inertia\Response;

class PublicGeoController extends Controller
{
    public function show(Request $request): Response
    {
        $jurisdictions = collect();
        try {
            // Join with sources to surface per-jurisdiction source_count + the
            // most recent last_refreshed_at across them. The legacy column
            // names (`code`, `last_sync_at`, `source_count`) were never
            // present on `public_geo.jurisdictions`; this query supplies the
            // shape the frontend expects.
            $jurisdictions = DB::table('public_geo.jurisdictions AS j')
                ->leftJoin('public_geo.sources AS s', 's.jurisdiction_code', '=', 'j.jurisdiction_code')
                ->groupBy(
                    'j.jurisdiction_code', 'j.display_name', 'j.status',
                    'j.country_code', 'j.sort_order',
                )
                ->orderBy('j.sort_order')
                ->orderBy('j.display_name')
                ->select([
                    'j.jurisdiction_code',
                    'j.display_name',
                    'j.status',
                    DB::raw('COUNT(s.source_id) AS source_count'),
                    DB::raw('MAX(s.last_refreshed_at) AS last_refreshed_at'),
                ])
                ->get();
        } catch (\Throwable $e) {
            try {
                $jurisdictions = DB::table('silver.public_geoscience_jurisdictions')->orderBy('display_name')->get();
            } catch (\Throwable $e2) { /* */
            }
        }

        $layers = [
            ['id' => 'bedrock_geology', 'label' => 'Bedrock geology', 'tier' => 2, 'group' => 'Geology'],
            ['id' => 'surficial_geology', 'label' => 'Surficial geology', 'tier' => 2, 'group' => 'Geology'],
            ['id' => 'faults', 'label' => 'Faults', 'tier' => 2, 'group' => 'Geology'],
            ['id' => 'dykes', 'label' => 'Dykes', 'tier' => 2, 'group' => 'Geology'],
            ['id' => 'geological_domains', 'label' => 'Geological domains', 'tier' => 2, 'group' => 'Geology'],
            ['id' => 'regional_compilation_pts', 'label' => 'Regional compilation pts', 'tier' => 2, 'group' => 'Geology'],
            ['id' => 'regional_compilation_polys', 'label' => 'Regional compilation polys', 'tier' => 2, 'group' => 'Geology'],
            ['id' => 'geoscience_publications', 'label' => 'Geoscience publications', 'tier' => 2, 'group' => 'Base'],
            ['id' => 'geophys_survey_coverage', 'label' => 'Geophys survey coverage', 'tier' => 2, 'group' => 'Geophysics'],
            ['id' => 'geophys_control_points', 'label' => 'Geophys control points', 'tier' => 2, 'group' => 'Geophysics'],
            ['id' => 'petroleum_wells', 'label' => 'Petroleum wells', 'tier' => 3, 'group' => 'Tenure', 'locked' => true],
            ['id' => 'well_trajectories', 'label' => 'Well trajectories', 'tier' => 3, 'group' => 'Tenure', 'locked' => true],
            ['id' => 'petroleum_pools', 'label' => 'Petroleum pools', 'tier' => 3, 'group' => 'Tenure', 'locked' => true],
            ['id' => 'geochronology_samples', 'label' => 'Geochronology samples', 'tier' => 3, 'group' => 'Geochem', 'locked' => true],
            ['id' => 'geochemistry_samples', 'label' => 'Geochemistry samples', 'tier' => 3, 'group' => 'Geochem', 'locked' => true],
            ['id' => 'feature_points', 'label' => 'Feature points', 'tier' => 3, 'group' => 'Geology', 'locked' => true],
            ['id' => 'feature_lines', 'label' => 'Feature lines', 'tier' => 3, 'group' => 'Geology', 'locked' => true],
        ];

        return Inertia::render('Foundry/PublicGeo', [
            'jurisdictions' => $jurisdictions->map(fn ($j) => [
                // Canonical column on public_geo.jurisdictions is
                // `jurisdiction_code`. Older code paths used `code`; fall
                // back if a legacy sibling row still exposes that.
                'code' => (string) ($j->jurisdiction_code ?? $j->code ?? ''),
                'name' => (string) ($j->display_name ?? $j->name ?? ''),
                'sources' => (int) ($j->source_count ?? 0),
                // `last_refreshed_at` is the canonical column; `last_sync_at`
                // was the legacy alias. Keep both for backward compat.
                'last_sync' => (string) ($j->last_refreshed_at ?? $j->last_sync_at ?? '—'),
            ])->values(),
            'layers' => $layers,
            'empty' => $jurisdictions->isEmpty(),
        ]);
    }
}
