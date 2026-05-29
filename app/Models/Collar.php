<?php

declare(strict_types=1);

namespace App\Models;

use App\Enums\CollarStatus;
use App\Enums\HoleType;
use Database\Factories\CollarFactory;
use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Factories\HasFactory;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;
use Illuminate\Database\Eloquent\Relations\HasMany;

class Collar extends Model
{
    /** @use HasFactory<CollarFactory> */
    use HasFactory;
    use HasUuids;

    protected $table = 'silver.collars';

    protected $primaryKey = 'collar_id';

    public $incrementing = false;

    protected $keyType = 'string';

    protected $fillable = [
        'hole_id',
        'project_id',
        'easting',
        'northing',
        'elevation',
        'total_depth',
        'hole_type',
        'azimuth',
        'dip',
        'drill_date',
        'status',
        // CC-01 Item 2 — spatial uncertainty + CRS provenance.
        'spatial_uncertainty_m',
        'crs_confidence',
        'georef_method',
    ];

    protected $casts = [
        'easting' => 'float',
        'northing' => 'float',
        'elevation' => 'float',
        'total_depth' => 'float',
        'azimuth' => 'float',
        'dip' => 'float',
        'drill_date' => 'date',
        'spatial_uncertainty_m' => 'float',
        'crs_confidence' => 'float',
        'created_at' => 'datetime',
        'updated_at' => 'datetime',
        // §04e Collar — closed-vocabulary string columns cast to backed
        // enums. Per georag-schema-contracts skill: these are STRUCTURAL
        // (industry-standard closed vocab), distinct from SME-managed open
        // vocabularies (commodity / alt_type / lith_code / mineralogy)
        // which stay as plain strings backed by SmeConfig validation.
        'hole_type' => HoleType::class,
        'status' => CollarStatus::class,
    ];

    /**
     * Get the project this collar belongs to.
     */
    public function project(): BelongsTo
    {
        return $this->belongsTo(Project::class, 'project_id', 'project_id');
    }

    /**
     * Get all surveys for this collar.
     */
    public function surveys(): HasMany
    {
        return $this->hasMany(Survey::class, 'collar_id', 'collar_id');
    }

    /**
     * Get all lithology logs for this collar.
     */
    public function lithologyLogs(): HasMany
    {
        return $this->hasMany(LithologyLog::class, 'collar_id', 'collar_id');
    }

    /**
     * Get all alterations for this collar.
     */
    public function alterations(): HasMany
    {
        return $this->hasMany(Alteration::class, 'collar_id', 'collar_id');
    }

    /**
     * Get all structures for this collar.
     */
    public function structures(): HasMany
    {
        return $this->hasMany(Structure::class, 'collar_id', 'collar_id');
    }

    /**
     * Get all samples for this collar.
     */
    public function samples(): HasMany
    {
        return $this->hasMany(Sample::class, 'collar_id', 'collar_id');
    }

    /**
     * Get all geochemistry records for this collar.
     */
    public function geochemistry(): HasMany
    {
        return $this->hasMany(Geochemistry::class, 'collar_id', 'collar_id');
    }

    /**
     * LAS well log curves (GR, RHOB, etc.) — continuous depth-value arrays.
     */
    public function wellLogCurves(): HasMany
    {
        return $this->hasMany(WellLogCurve::class, 'collar_id', 'collar_id');
    }
}
