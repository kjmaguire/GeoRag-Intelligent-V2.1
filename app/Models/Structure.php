<?php

declare(strict_types=1);

namespace App\Models;

use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

class Structure extends Model
{
    use HasUuids;

    // Migration 2026_05_20_060400 dropped the empty plural `silver.structures`
    // and replaced it with the singular spec table. New shape: `id` (uuid) PK,
    // `workspace_id` (RLS), `true_dip` / `true_dip_dir` replacing the old
    // `true_dip` / `dip_direction` pair, plus `roughness` / `infill` / `notes`.
    protected $table = 'silver.structure';

    protected $primaryKey = 'id';

    public $incrementing = false;

    protected $keyType = 'string';

    public $timestamps = false;

    protected $fillable = [
        'workspace_id',
        'collar_id',
        'depth',
        'structure_type',
        'alpha_angle',
        'beta_angle',
        'true_dip',
        'true_dip_dir',
        'roughness',
        'infill',
        'notes',
    ];

    protected $casts = [
        'depth' => 'float',
        'alpha_angle' => 'float',
        'beta_angle' => 'float',
        'true_dip' => 'float',
        'true_dip_dir' => 'float',
        'created_at' => 'datetime',
    ];

    public function collar(): BelongsTo
    {
        return $this->belongsTo(Collar::class, 'collar_id', 'collar_id');
    }
}
