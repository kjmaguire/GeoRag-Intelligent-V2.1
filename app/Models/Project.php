<?php

declare(strict_types=1);

namespace App\Models;

use App\Enums\ProjectStatus;
use Database\Factories\ProjectFactory;
use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Factories\HasFactory;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\HasMany;
use Illuminate\Support\Str;

/**
 * A project row in silver.projects.
 *
 * The @property block is not decoration. Eloquent resolves columns
 * dynamically, so larastan cannot see them, and every access to one of
 * these was an "Access to an undefined property" entry in
 * phpstan-baseline.neon — 20 of them across the controllers, which meant
 * a genuine typo in a column name looked exactly like the existing noise.
 * Declared here 2026-08-21; the corresponding baseline entries went with
 * it.
 *
 * Only the plain columns are declared. `status` is deliberately left out:
 * it is cast to ProjectStatus but read as both an enum and a string
 * (see OverviewController's `is_object($project->status)` branch), so
 * declaring one type would trade twenty honest baseline entries for a
 * dishonest annotation.
 *
 * @property string $project_id
 * @property string $workspace_id
 * @property string $slug
 * @property string $project_name
 * @property int|null $crs_epsg
 */
class Project extends Model
{
    /** @use HasFactory<ProjectFactory> */
    use HasFactory;
    use HasUuids;

    protected $table = 'silver.projects';

    protected $primaryKey = 'project_id';

    public $incrementing = false;

    protected $keyType = 'string';

    protected $fillable = [
        'project_name',
        'crs_datum',
        'company',
        'magnetic_declination',
        'orientation_reference',
        'commodity',
        'region',
        'status',
        'slug',
    ];

    protected $casts = [
        'magnetic_declination' => 'float',
        'status' => ProjectStatus::class,
        'created_at' => 'datetime',
        'updated_at' => 'datetime',
    ];

    protected $attributes = [
        'crs_datum' => 'EPSG:32613',
    ];

    protected static function booted(): void
    {
        static::creating(function (self $project): void {
            if (empty($project->slug) && ! empty($project->project_name)) {
                $suffix = $project->project_id ? substr((string) $project->project_id, 0, 8) : Str::random(8);
                $project->slug = Str::slug($project->project_name).'-'.$suffix;
            }
        });
    }

    /**
     * Get all collars for this project.
     */
    public function collars(): HasMany
    {
        return $this->hasMany(Collar::class, 'project_id', 'project_id');
    }

    /**
     * Get all reports for this project (matched by project_name).
     */
    public function reports(): HasMany
    {
        return $this->hasMany(Report::class, 'project_name', 'project_name');
    }
}
