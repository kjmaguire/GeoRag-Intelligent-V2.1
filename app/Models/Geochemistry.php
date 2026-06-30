<?php

declare(strict_types=1);

namespace App\Models;

use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

class Geochemistry extends Model
{
    use HasUuids;

    protected $table = 'silver.geochemistry';

    protected $primaryKey = 'geochem_id';

    public $incrementing = false;

    protected $keyType = 'string';

    protected $fillable = [
        'collar_id',
        'from_depth',
        'to_depth',
        'sio2_wt_pct',
        'al2o3_wt_pct',
        'fe2o3_wt_pct',
        'mgo_wt_pct',
        'cao_wt_pct',
        'na2o_wt_pct',
        'k2o_wt_pct',
        'ree_json',
        'mg_number',
        'cia',
        'eu_anomaly',
    ];

    protected $casts = [
        'from_depth' => 'float',
        'to_depth' => 'float',
        'sio2_wt_pct' => 'float',
        'al2o3_wt_pct' => 'float',
        'fe2o3_wt_pct' => 'float',
        'mgo_wt_pct' => 'float',
        'cao_wt_pct' => 'float',
        'na2o_wt_pct' => 'float',
        'k2o_wt_pct' => 'float',
        'ree_json' => 'array',
        'mg_number' => 'float',
        'cia' => 'float',
        'eu_anomaly' => 'float',
        'created_at' => 'datetime',
        'updated_at' => 'datetime',
    ];

    /**
     * Get the collar this geochemistry record belongs to.
     */
    public function collar(): BelongsTo
    {
        return $this->belongsTo(Collar::class, 'collar_id', 'collar_id');
    }
}
