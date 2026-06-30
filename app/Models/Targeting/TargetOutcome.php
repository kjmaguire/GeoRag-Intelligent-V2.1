<?php

declare(strict_types=1);

namespace App\Models\Targeting;

use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

/**
 * Eloquent model for `targeting.target_outcomes` (master-plan §8.1 /
 * doc-phase 85 schema; doc-phase 110 model layer).
 *
 * Post-drilling outcome record — the training signal for Phase 12
 * XGBoost retraining. Values: hit | miss | partial | pending |
 * unresolvable.
 */
class TargetOutcome extends Model
{
    use HasUuids;

    protected $table = 'targeting.target_outcomes';

    protected $primaryKey = 'outcome_id';

    public $incrementing = false;

    protected $keyType = 'string';

    public $timestamps = false;

    protected $fillable = [
        'recommendation_id',
        'workspace_id',
        'drillhole_collar_id',
        'hit_or_miss',
        'outcome_payload',
        'recorded_at',
    ];

    protected $casts = [
        'outcome_payload' => 'array',
        'recorded_at' => 'datetime',
    ];

    public function recommendation(): BelongsTo
    {
        return $this->belongsTo(
            TargetRecommendation::class,
            'recommendation_id',
            'recommendation_id',
        );
    }
}
