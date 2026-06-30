<?php

declare(strict_types=1);

namespace App\Models;

use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

class LithologyLog extends Model
{
    use HasUuids;

    protected $table = 'silver.lithology_logs';

    protected $primaryKey = 'log_id';

    public $incrementing = false;

    protected $keyType = 'string';

    protected $fillable = [
        'collar_id',
        'from_depth',
        'to_depth',
        'lithology_code',
        'lithology_description',
        'grain_size',
        'color',
        'hardness',
        'rqd',
        'recovery',
        'weathering',
    ];

    protected $casts = [
        'from_depth' => 'float',
        'to_depth' => 'float',
        'rqd' => 'float',
        'recovery' => 'float',
        'created_at' => 'datetime',
        'updated_at' => 'datetime',
    ];

    /**
     * Get the collar this lithology log belongs to.
     */
    public function collar(): BelongsTo
    {
        return $this->belongsTo(Collar::class, 'collar_id', 'collar_id');
    }
}
