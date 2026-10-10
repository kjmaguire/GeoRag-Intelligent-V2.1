<?php

declare(strict_types=1);

namespace App\Http\Resources;

use App\Models\WellLogCurve;
use Illuminate\Http\Request;
use Illuminate\Http\Resources\Json\JsonResource;

class CollarResource extends JsonResource
{
    public function toArray(Request $request): array
    {
        return [
            'collar_id' => $this->collar_id,
            'project_id' => $this->project_id,
            'hole_id' => $this->hole_id,
            'easting' => $this->easting,
            'northing' => $this->northing,
            'elevation' => $this->elevation,
            'total_depth' => $this->total_depth,
            // hole_type / status as STORED, not the cast enum. The ingestion
            // writes the file's own words ("DDH", "Closed"), which TolerantEnum
            // reads as null; the raw string is what the geologist sees and
            // what an in-vocabulary enum would have serialised to anyway.
            'hole_type' => $this->resource->getRawOriginal('hole_type'),
            'azimuth' => $this->azimuth,
            'dip' => $this->dip,
            'drill_date' => $this->drill_date?->toDateString(),
            'status' => $this->resource->getRawOriginal('status'),

            // WGS84 lon/lat read straight off silver.collars.geom_4326 (the
            // only collar geometry since the SRID-32613 `geom` was retired
            // 2026-09-29). Pre-computed in the controller query via selectRaw
            // so they arrive as attributes; null when geom_4326 is absent.
            'longitude' => isset($this->longitude) ? (float) $this->longitude : null,
            'latitude' => isset($this->latitude) ? (float) $this->latitude : null,

            // CC-01 Item 2 — spatial uncertainty + CRS provenance. Drives the
            // MapView uncertainty-rings layer + the DrillholeDetail badge.
            // See migration 2026_05_23_050000_add_spatial_uncertainty_to_collars_and_spatial_features
            // for the column COMMENTs that define the vocabulary.
            'spatial_uncertainty_m' => $this->spatial_uncertainty_m !== null ? (float) $this->spatial_uncertainty_m : null,
            'crs_confidence' => $this->crs_confidence !== null ? (float) $this->crs_confidence : null,
            'georef_method' => $this->georef_method,

            // Relationship counts — populated when the controller calls withCount()
            'survey_count' => $this->surveys_count ?? 0,
            'sample_count' => $this->samples_count ?? 0,

            // Full relationship payloads — only present on show() where they are
            // explicitly eager-loaded via ->load(). whenLoaded() returns the data
            // if the relation is already loaded, or omits the key entirely so
            // index() responses stay lean.
            'surveys' => $this->whenLoaded('surveys', fn () => $this->surveys->map(fn ($s) => [
                'survey_id' => $s->survey_id,
                'depth' => $s->depth,
                'azimuth' => $s->azimuth,
                'dip' => $s->dip,
                // The STORED string, deliberately, not the cast enum.
                //
                // Most ingested rows are outside the §04e vocabulary — the
                // ingestion writes 'unknown' whenever a sheet names no
                // instrument, and 'desurveyed_trace' for a Discover trace —
                // and TolerantSurveyMethod yields null for those rather than
                // throwing the ValueError that used to 500 this endpoint.
                //
                // Reading the raw value is not a workaround for that; it is
                // the more direct expression of what this field is for. The
                // payload is JSON, so an in-vocabulary enum would serialise
                // to exactly this same string, and taking it raw additionally
                // shows the geologist what the file actually said instead of
                // a blank where 'desurveyed_trace' was.
                'survey_method' => $s->getRawOriginal('survey_method'),
                // The file's declared north for this azimuth, or null.
                'azimuth_reference' => $s->azimuth_reference,
            ]),
            ),
            'lithology_logs' => $this->whenLoaded('lithologyLogs', fn () => $this->lithologyLogs->map(fn ($l) => [
                'log_id' => $l->log_id,
                'from_depth' => $l->from_depth,
                'to_depth' => $l->to_depth,
                'lithology_code' => $l->lithology_code,
                'lithology_description' => $l->lithology_description,
                'grain_size' => $l->grain_size,
                'color' => $l->color,
                'hardness' => $l->hardness,
                'rqd' => $l->rqd,
                'recovery' => $l->recovery,
                'weathering' => $l->weathering,
            ]),
            ),
            // alteration_id / structure_id are JSON-contract aliases for the
            // singular tables' `id` column (post-2026_05_20 rename).
            'alterations' => $this->whenLoaded('alterations', fn () => $this->alterations->map(fn ($a) => [
                'alteration_id' => $a->id,
                'from_depth' => $a->from_depth,
                'to_depth' => $a->to_depth,
                'alteration_type' => $a->alteration_type,
                'intensity' => $a->intensity,
                'minerals' => $a->minerals,
                'notes' => $a->notes,
            ]),
            ),
            'mineralization' => $this->whenLoaded('mineralization', fn () => $this->mineralization->map(fn ($m) => [
                'mineralization_id' => $m->id,
                'from_depth' => $m->from_depth,
                'to_depth' => $m->to_depth,
                'mineral' => $m->mineral,
                'abundance_pct' => $m->abundance_pct,
                'form' => $m->form,
                'grain_size' => $m->grain_size,
                'notes' => $m->notes,
            ]),
            ),
            'structures' => $this->whenLoaded('structures', fn () => $this->structures->map(fn ($s) => [
                'structure_id' => $s->id,
                'depth' => $s->depth,
                'structure_type' => $s->structure_type,
                'dip_direction' => $s->true_dip_dir,
                'dip_angle' => $s->true_dip,
                'alpha_angle' => $s->alpha_angle,
                'beta_angle' => $s->beta_angle,
            ]),
            ),
            'samples' => $this->whenLoaded('samples', fn () => $this->samples->map(fn ($s) => [
                'sample_id' => $s->sample_id,
                'from_depth' => $s->from_depth,
                'to_depth' => $s->to_depth,
                'sample_type' => $s->sample_type,
                'lab_id' => $s->lab_id,
                'commodity_assays' => $s->commodity_assays,
                'qaqc_type' => $s->qaqc_type,
            ]),
            ),
            // The columns silver.geochemistry actually has. This used to emit
            // element / value / unit / method, none of which exist on the table
            // (they are silver.assays_v2's), so all four were null on every row.
            'geochemistry' => $this->whenLoaded('geochemistry', fn () => $this->geochemistry->map(fn ($g) => [
                'geochem_id' => $g->geochem_id,
                'from_depth' => $g->from_depth,
                'to_depth' => $g->to_depth,
                'sample_id' => $g->sample_id,
                'sample_type' => $g->sample_type,
                'sio2_wt_pct' => $g->sio2_wt_pct,
                'al2o3_wt_pct' => $g->al2o3_wt_pct,
                'fe2o3_wt_pct' => $g->fe2o3_wt_pct,
                'mgo_wt_pct' => $g->mgo_wt_pct,
                'cao_wt_pct' => $g->cao_wt_pct,
                'na2o_wt_pct' => $g->na2o_wt_pct,
                'k2o_wt_pct' => $g->k2o_wt_pct,
                'mg_number' => $g->mg_number,
                'cia' => $g->cia,
                'eu_anomaly' => $g->eu_anomaly,
                'ree_json' => $g->ree_json,
                'assay_values_ppm' => $g->assay_values_ppm,
            ]),
            ),
            // The curves StripLogViewer draws beside the lithology column. It
            // read `well_log_curves` from this payload, which never carried
            // one, so the curve track could not render. Depths are metres, on
            // the same axis as the intervals; show() leaves out legacy rows
            // whose depth unit was never recorded, which cannot be placed.
            'well_log_curves' => $this->whenLoaded('wellLogCurves', fn ($curves) => $curves
                ->map(fn (WellLogCurve $curve): array => self::curvePayload($curve))
                ->values(),
            ),

            'created_at' => $this->created_at?->toISOString(),
            'updated_at' => $this->updated_at?->toISOString(),
        ];
    }

    /**
     * Most samples sent per curve. A LAS at 0.1 m over 1,000 m is 10,000 per
     * curve; the viewer draws about 500, so the payload is thinned by stride.
     */
    public const MAX_CURVE_POINTS = 1000;

    /**
     * One curve as the viewer draws it: parallel `depths` (metres) and
     * `values` arrays, thinned to MAX_CURVE_POINTS, with `sample_count` the
     * number stored.
     *
     * @return array<string, mixed>
     */
    private static function curvePayload(WellLogCurve $curve): array
    {
        $depths = WellLogCurve::floatArray($curve->depths);
        $values = WellLogCurve::floatArray($curve->values);
        $count = min(count($depths), count($values));
        $toMetres = $curve->depth_unit === 'ft' ? 0.3048 : 1.0;
        $stride = max(1, (int) ceil($count / self::MAX_CURVE_POINTS));

        $outDepths = [];
        $outValues = [];
        for ($i = 0; $i < $count; $i += $stride) {
            $outDepths[] = $depths[$i] * $toMetres;
            $outValues[] = $values[$i];
        }

        return [
            'curve_id' => $curve->curve_id,
            'curve_name' => $curve->curve_name,
            'curve_unit' => $curve->curve_unit,
            'null_value' => $curve->null_value,
            'sample_count' => $count,
            'depths' => $outDepths,
            'values' => $outValues,
        ];
    }
}
