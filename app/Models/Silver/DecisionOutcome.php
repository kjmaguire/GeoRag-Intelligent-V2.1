<?php

declare(strict_types=1);

namespace App\Models\Silver;

use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

/**
 * Eloquent model for `silver.decision_outcomes` (master-plan §9.9 /
 * doc-phase 92 schema; doc-phase 110 model layer).
 *
 * Post-decision outcome tracking — used for the §20.8 learning loop
 * (drilled targets, eval pass/fail, etc.).
 */
class DecisionOutcome extends Model
{
    use HasUuids;

    protected $table = 'silver.decision_outcomes';

    protected $primaryKey = 'outcome_id';

    public $incrementing = false;

    protected $keyType = 'string';

    public $timestamps = false;

    protected $fillable = ['decision_id', 'outcome_kind', 'outcome_payload', 'observed_at'];

    protected $casts = [
        'outcome_payload' => 'array',
        'observed_at' => 'datetime',
    ];

    public function decision(): BelongsTo
    {
        return $this->belongsTo(DecisionRecord::class, 'decision_id', 'decision_id');
    }
}
