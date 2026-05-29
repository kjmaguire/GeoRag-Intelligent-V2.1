<?php

declare(strict_types=1);

namespace App\Models\PublicGeoscience;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\HasMany;

/**
 * Public Geoscience jurisdiction registry row.
 *
 * Each row is one queryable (or roadmap) geological-survey jurisdiction —
 * e.g. Saskatchewan, federal Canada. Only rows with status = 'active' are
 * actually ingested; 'coming_soon' rows exist to render the roadmap tiles
 * in the UI (plan §02a, §09c).
 */
class Jurisdiction extends Model
{
    protected $table = 'public_geo.jurisdictions';
    protected $primaryKey = 'jurisdiction_code';
    public $incrementing = false;
    protected $keyType = 'string';

    protected $fillable = [
        'jurisdiction_code',
        'country_code',
        'display_name',
        'level',
        'status',
        'primary_authority',
        'license_summary',
        'license_url',
        'default_source_crs',
        'refresh_cadence',
        'last_refreshed_at',
        'teaser',
        'sort_order',
    ];

    protected $casts = [
        'default_source_crs' => 'integer',
        'sort_order'         => 'integer',
        'last_refreshed_at'  => 'datetime',
        'created_at'         => 'datetime',
        'updated_at'         => 'datetime',
    ];

    /**
     * Sources (feature feeds) published under this jurisdiction.
     */
    public function sources(): HasMany
    {
        return $this->hasMany(
            PublicGeoSource::class,
            'jurisdiction_code',
            'jurisdiction_code'
        );
    }
}
