<?php

declare(strict_types=1);

namespace App\Models;

use Database\Factories\SavedMapViewFactory;
use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Factories\HasFactory;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

/**
 * Eloquent model for `silver.saved_map_views` (master-plan §6.5
 * doc-phase 76 schema; doc-phase 105 model layer).
 *
 * Per-user, per-project, workspace-scoped MapLibre saved-view state.
 * The `view_state` JSONB column holds the MapLibre camera + active
 * layer pack + filter + draw-layer state; the column is deliberately
 * unstructured so frontend evolution doesn't require schema
 * migrations.
 */
class SavedMapView extends Model
{
    /** @use HasFactory<SavedMapViewFactory> */
    use HasFactory;
    use HasUuids;

    protected $table = 'silver.saved_map_views';

    protected $primaryKey = 'view_id';

    public $incrementing = false;

    protected $keyType = 'string';

    protected $fillable = [
        'workspace_id',
        'project_id',
        'user_id',
        'name',
        'description',
        'view_state',
        'aoi_geom',
        'is_shared',
    ];

    protected $casts = [
        'view_state' => 'array',
        'is_shared' => 'boolean',
        'created_at' => 'datetime',
        'updated_at' => 'datetime',
    ];

    /**
     * The project this saved view belongs to.
     */
    public function project(): BelongsTo
    {
        return $this->belongsTo(Project::class, 'project_id', 'project_id');
    }

    /**
     * The user who owns this saved view.
     */
    public function user(): BelongsTo
    {
        return $this->belongsTo(User::class, 'user_id', 'id');
    }
}
