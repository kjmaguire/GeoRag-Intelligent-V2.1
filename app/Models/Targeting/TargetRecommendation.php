<?php

declare(strict_types=1);

namespace App\Models\Targeting;

use App\Models\Project;
use Database\Factories\Targeting\TargetRecommendationFactory;
use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Factories\HasFactory;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;
use Illuminate\Database\Eloquent\Relations\HasMany;

/**
 * Eloquent model for `targeting.target_recommendations` (master-plan
 * §8.1 / doc-phase 85 schema; doc-phase 110 model layer).
 *
 * Final ranked recommendation row. One row per (run_id, rank). Joins
 * to `target_candidate_zones` for geometry + `target_scores` for the
 * aggregate score + factor breakdown.
 *
 * RLS via workspace_id (silver.workspaces).
 */
class TargetRecommendation extends Model
{
    /** @use HasFactory<TargetRecommendationFactory> */
    use HasFactory;
    use HasUuids;

    protected $table = 'targeting.target_recommendations';

    protected $primaryKey = 'recommendation_id';

    public $incrementing = false;

    protected $keyType = 'string';

    public $timestamps = false; // schema uses created_at only, no updated_at

    protected $fillable = [
        'workspace_id',
        'project_id',
        'run_id',
        'zone_id',
        'score_id',
        'rank',
        'explanation_markdown',
        'created_at',
    ];

    protected $casts = [
        'rank' => 'integer',
        'created_at' => 'datetime',
    ];

    /**
     * The project this recommendation lives in.
     */
    public function project(): BelongsTo
    {
        return $this->belongsTo(Project::class, 'project_id', 'project_id');
    }

    /**
     * Review decisions logged against this recommendation (R5 sign-off).
     */
    public function reviewDecisions(): HasMany
    {
        return $this->hasMany(
            TargetReviewDecision::class,
            'recommendation_id',
            'recommendation_id'
        );
    }

    /**
     * Drilled outcomes tied to this recommendation (Phase 12 training
     * input).
     */
    public function outcomes(): HasMany
    {
        return $this->hasMany(
            TargetOutcome::class,
            'recommendation_id',
            'recommendation_id'
        );
    }
}
