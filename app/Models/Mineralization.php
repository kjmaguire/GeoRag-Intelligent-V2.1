<?php

declare(strict_types=1);

namespace App\Models;

use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

/**
 * One mineral over one drilled interval (`silver.mineralization`, created by
 * 2026_05_20_060400). Read-only in the application: rows are written by the
 * ingest_tabular Hatchet workflow from a geology log's mineral columns.
 *
 * This is the raw record the collar API serves. The strip log does not read it:
 * promote_silver_to_gold folds it into gold `mineralization` intervals (§04e),
 * which HoleStripTracks flattens back to one band per mineral.
 */
class Mineralization extends Model
{
    use HasUuids;

    protected $table = 'silver.mineralization';

    protected $primaryKey = 'id';

    public $incrementing = false;

    protected $keyType = 'string';

    public $timestamps = false;

    protected $fillable = [
        'workspace_id',
        'collar_id',
        'from_depth',
        'to_depth',
        'mineral',
        'abundance_pct',
        'form',
        'grain_size',
        'notes',
    ];

    protected $casts = [
        'from_depth' => 'float',
        'to_depth' => 'float',
        'abundance_pct' => 'float',
        'created_at' => 'datetime',
    ];

    /**
     * @return BelongsTo<Collar, $this>
     */
    public function collar(): BelongsTo
    {
        return $this->belongsTo(Collar::class, 'collar_id', 'collar_id');
    }
}
