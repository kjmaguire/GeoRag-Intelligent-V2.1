<?php

declare(strict_types=1);

namespace App\Models\PublicGeoscience;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

/**
 * One publishable feature feed that flows into the Public Geoscience corpus.
 *
 * Each row identifies a specific ArcGIS REST / GeoJSON endpoint (or similar)
 * that maps to a single canonical entity type (mine / mineral_occurrence /
 * drillhole_collar / resource_potential_zone). Ingestion (Phase 2) reads from
 * this registry; Phase 1 just stores the metadata.
 */
class PublicGeoSource extends Model
{
    protected $table = 'public_geo.sources';

    protected $primaryKey = 'source_id';

    public $incrementing = false;

    protected $keyType = 'string';

    protected $fillable = [
        'source_id',
        'jurisdiction_code',
        'name',
        'canonical_type',
        'service_url',
        'layer_index',
        'source_crs',
        'license_summary',
        'license_url',
        'refresh_cadence',
        'last_refreshed_at',
        'notes',
    ];

    protected $casts = [
        'layer_index' => 'integer',
        'source_crs' => 'integer',
        'last_refreshed_at' => 'datetime',
        'created_at' => 'datetime',
        'updated_at' => 'datetime',
    ];

    public function jurisdiction(): BelongsTo
    {
        return $this->belongsTo(
            Jurisdiction::class,
            'jurisdiction_code',
            'jurisdiction_code',
        );
    }
}
