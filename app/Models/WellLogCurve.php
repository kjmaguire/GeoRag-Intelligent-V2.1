<?php

declare(strict_types=1);

namespace App\Models;

use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

class WellLogCurve extends Model
{
    use HasUuids;

    protected $table = 'silver.well_log_curves';
    protected $primaryKey = 'curve_id';
    public $incrementing = false;
    protected $keyType = 'string';

    protected $fillable = [
        'collar_id',
        'curve_name',
        'curve_unit',
        'curve_description',
        'min_depth',
        'max_depth',
        'step',
        'null_value',
        'sample_count',
        'las_version',
        'source_file',
        'depths',
        'values',
    ];

    protected $casts = [
        'min_depth'    => 'float',
        'max_depth'    => 'float',
        'step'         => 'float',
        'null_value'   => 'float',
        'sample_count' => 'integer',
        'created_at'   => 'datetime',
        'updated_at'   => 'datetime',
    ];

    /**
     * The collar this curve belongs to.
     */
    public function collar(): BelongsTo
    {
        return $this->belongsTo(Collar::class, 'collar_id', 'collar_id');
    }
}
