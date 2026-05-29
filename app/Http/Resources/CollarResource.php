<?php

declare(strict_types=1);

namespace App\Http\Resources;

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
            'hole_type' => $this->hole_type,
            'azimuth' => $this->azimuth,
            'dip' => $this->dip,
            'drill_date' => $this->drill_date?->toDateString(),
            'status' => $this->status,

            // WGS84 lon/lat derived from PostGIS geometry via ST_Transform.
            // These are pre-computed in the controller query via selectRaw so they
            // arrive as attributes; fall back to null if geom is absent.
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
                'survey_method' => $s->survey_method,
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
            'geochemistry' => $this->whenLoaded('geochemistry', fn () => $this->geochemistry->map(fn ($g) => [
                'geochem_id' => $g->geochem_id,
                'from_depth' => $g->from_depth,
                'to_depth' => $g->to_depth,
                'element' => $g->element,
                'value' => $g->value,
                'unit' => $g->unit,
                'method' => $g->method,
            ]),
            ),

            'created_at' => $this->created_at?->toISOString(),
            'updated_at' => $this->updated_at?->toISOString(),
        ];
    }
}
