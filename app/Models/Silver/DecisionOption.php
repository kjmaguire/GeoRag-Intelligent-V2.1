<?php

declare(strict_types=1);

namespace App\Models\Silver;

use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

/**
 * Eloquent model for `silver.decision_options` (master-plan §9.9 /
 * doc-phase 92 schema; doc-phase 110 model layer).
 *
 * One row per option considered for a decision; the chosen option
 * has `was_chosen = true`.
 */
class DecisionOption extends Model
{
    use HasUuids;

    protected $table = 'silver.decision_options';

    protected $primaryKey = 'option_id';

    public $incrementing = false;

    protected $keyType = 'string';

    public $timestamps = false;

    protected $fillable = ['decision_id', 'label', 'description', 'was_chosen', 'payload'];

    protected $casts = [
        'was_chosen' => 'boolean',
        'payload' => 'array',
    ];

    public function decision(): BelongsTo
    {
        return $this->belongsTo(DecisionRecord::class, 'decision_id', 'decision_id');
    }
}
