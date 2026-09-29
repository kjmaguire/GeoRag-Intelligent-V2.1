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

    /**
     * orientation_reference when nobody says otherwise — what the New
     * Project form, FastAPI's Project model and the ingestion stubs all
     * write, and the column's DB default. Vocabulary: BOH | TOH (core
     * orientation mark); see StoreProjectRequest::prepareForValidation().
     */
    public const DEFAULT_ORIENTATION_REFERENCE = 'BOH';

    protected $table = 'silver.projects';

    protected $primaryKey = 'project_id';

    public $incrementing = false;

    protected $keyType = 'string';

    protected $fillable = [
        'project_name',
        'crs_datum',
        // Added 2026-08-25. The column has existed since the schema was
        // written and FOUR readers consult it (Overview, Workspace, Chat's
        // context envelope, the projects index) — but nothing had ever been
        // able to WRITE it, so it was NULL on every project ever created and
        // every reader fell through to its default.
        //
        // The cost was not cosmetic. ingest_tabular now resolves a CSV's
        // coordinate system as: the file's own override, else THIS, else
        // EPSG:32613. With this permanently NULL the middle rung did not
        // exist, so a project in Alaska read its collar CSVs as UTM zone 13N
        // and wrote the holes ~2,500 km east of where they were drilled.
        'crs_epsg',
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

    /** `silver.projects.slug` is VARCHAR(255). */
    public const SLUG_MAX_LENGTH = 255;

    public const SLUG_SUFFIX_LENGTH = 8;

    protected static function booted(): void
    {
        static::creating(function (self $project): void {
            if (empty($project->slug) && ! empty($project->project_name)) {
                $project->slug = self::makeSlug((string) $project->project_name);
            }
        });
    }

    /**
     * `{name-slug}-{8 random chars}`, never longer than the column.
     *
     * LAR-14 (2026-09-29): the suffix was `substr(project_id, 0, 8)`, and
     * HasUuids mints UUIDv7, whose first 8 hex digits are the top of the
     * millisecond timestamp — they change only every ~65 s. Two projects with
     * the same name inside that window (a double-submit, a retry after a
     * timeout, two tenants' "Demo") collided on `projects_slug_unique` and
     * store() returned 500. A 255-char name plus the suffix also overflowed
     * VARCHAR(255) (22001). The suffix is now random (36^8 ≈ 2.8e12) and the
     * base is truncated to leave room for it.
     */
    public static function makeSlug(string $projectName): string
    {
        $maxBase = self::SLUG_MAX_LENGTH - 1 - self::SLUG_SUFFIX_LENGTH;
        $base = rtrim(substr(Str::slug($projectName), 0, $maxBase), '-');

        return ($base !== '' ? $base : 'project').'-'.Str::lower(Str::random(self::SLUG_SUFFIX_LENGTH));
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
