<?php

declare(strict_types=1);

namespace App\Models;

use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

class Sample extends Model
{
    use HasUuids;

    protected $table = 'silver.samples';
    protected $primaryKey = 'sample_id';
    public $incrementing = false;
    protected $keyType = 'string';

    protected $fillable = [
        'collar_id',
        'from_depth',
        'to_depth',
        'sample_type',
        'lab_id',
        'commodity_assays',
        'qaqc_type',
    ];

    protected $casts = [
        'from_depth' => 'float',
        'to_depth' => 'float',
        'commodity_assays' => 'array',
        'created_at' => 'datetime',
        'updated_at' => 'datetime',
    ];

    /**
     * Get the collar this sample belongs to.
     */
    public function collar(): BelongsTo
    {
        return $this->belongsTo(Collar::class, 'collar_id', 'collar_id');
    }
}
