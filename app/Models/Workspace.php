<?php

declare(strict_types=1);

namespace App\Models;

use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsToMany;
use Illuminate\Database\Eloquent\Relations\HasMany;

/**
 * Eloquent model for `silver.workspaces`.
 *
 * Added 2026-06-03 as part of the user→workspace association
 * work (audit item A). The workspaces table has existed since
 * 2026-04-20 (`2026_04_20_100000_create_workspaces_and_data_version`)
 * but no Eloquent model was created — every read went through
 * raw DB::table() queries. The new `workspace_user` pivot needs
 * a Workspace model for the `User::workspaces()` belongsToMany
 * relationship; this is that model.
 *
 * Read-mostly: workspaces are managed via the OnboardingController
 * and admin surfaces; no general-purpose `create()` here. The model
 * stays intentionally thin so future fields land here naturally
 * rather than via raw DB::table edits scattered around the app.
 */
class Workspace extends Model
{
    use HasUuids;

    protected $table = 'silver.workspaces';

    protected $primaryKey = 'workspace_id';

    public $incrementing = false;

    protected $keyType = 'string';

    /**
     * Mass-assignment allowlist intentionally minimal. Workspace
     * provisioning happens in OnboardingController + admin paths;
     * neither relies on $request->all() so the conservative list
     * keeps the audit surface tight.
     */
    protected $fillable = [
        'name',
        'slug',
    ];

    protected $casts = [
        'created_at' => 'datetime',
        'updated_at' => 'datetime',
        'data_version' => 'integer',
    ];

    /**
     * Members of this workspace via the `workspace_user` pivot.
     * Pivot carries role: owner | admin | member | viewer.
     */
    public function members(): BelongsToMany
    {
        return $this->belongsToMany(
            User::class,
            'workspace_user',
            'workspace_id',
            'user_id',
        )->withPivot('role')->withTimestamps();
    }

    /**
     * Projects belonging to this workspace.
     */
    public function projects(): HasMany
    {
        return $this->hasMany(Project::class, 'workspace_id', 'workspace_id');
    }
}
