<?php

declare(strict_types=1);

namespace App\Http\Resources\PublicGeoscience;

use Illuminate\Http\Request;
use Illuminate\Http\Resources\Json\JsonResource;

/**
 * API shape for one Public Geoscience jurisdiction.
 *
 * Contract intent per plan §10. Sources are eager-loaded in the controller
 * and rendered as a nested array — client-side filtering by canonical_type
 * is done in React.
 *
 * `bbox_geojson` is a plain GeoJSON Polygon (converted from PostGIS via
 * ST_AsGeoJSON at query time). Used by MapLibre `fitBounds` on the client.
 */
class JurisdictionResource extends JsonResource
{
    public function toArray(Request $request): array
    {
        $sources = $this->whenLoaded('sources', function () {
            return $this->sources->map(fn ($s) => [
                'source_id'       => $s->source_id,
                'name'            => $s->name,
                'canonical_type'  => $s->canonical_type,
                'service_url'     => $s->service_url,
                'layer_index'     => $s->layer_index,
                'source_crs'      => $s->source_crs,
                'license_summary' => $s->license_summary,
                'license_url'     => $s->license_url,
                'refresh_cadence' => $s->refresh_cadence,
                'last_refreshed_at' => $s->last_refreshed_at?->toIso8601String(),
            ])->values()->all();
        }, []);

        // `bbox_geojson` is attached as a raw JSON string attribute by the
        // controller (via ST_AsGeoJSON). Decode so the client receives an
        // object, not an embedded string.
        $bbox = null;
        if (! empty($this->bbox_geojson)) {
            $decoded = json_decode($this->bbox_geojson, true);
            if (is_array($decoded)) {
                $bbox = $decoded;
            }
        }

        return [
            'jurisdiction_code'  => $this->jurisdiction_code,
            'country_code'       => $this->country_code,
            'display_name'       => $this->display_name,
            'level'              => $this->level,
            'status'             => $this->status,
            'primary_authority'  => $this->primary_authority,
            'license_summary'    => $this->license_summary,
            'license_url'        => $this->license_url,
            'default_source_crs' => $this->default_source_crs,
            'refresh_cadence'    => $this->refresh_cadence,
            'last_refreshed_at'  => $this->last_refreshed_at?->toIso8601String(),
            'teaser'             => $this->teaser,
            'sort_order'         => $this->sort_order,
            'bbox'               => $bbox,
            'sources'            => $sources,
        ];
    }
}
