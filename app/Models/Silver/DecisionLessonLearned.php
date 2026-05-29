<?php

declare(strict_types=1);

namespace App\Models\Silver;

use App\Models\User;
use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

/**
 * Eloquent model for `silver.decision_lessons_learned` (master-plan
 * §9.9 / doc-phase 92 schema; doc-phase 110 model layer).
 *
 * Retrospective captures — what we learned after a decision played
 * out. Written by `field_outcome_learning` workflow (§9.11) when
 * drilling outcomes arrive.
 */
class DecisionLessonLearned extends Model
{
    use HasUuids;

    protected $table = 'silver.decision_lessons_learned';

    protected $primaryKey = 'lesson_id';

    public $incrementing = false;

    protected $keyType = 'string';

    public $timestamps = false;

    protected $fillable = [
        'decision_id',
        'captured_by_user_id',
        'lesson_markdown',
        'captured_at',
    ];

    protected $casts = ['captured_at' => 'datetime'];

    public function decision(): BelongsTo
    {
        return $this->belongsTo(DecisionRecord::class, 'decision_id', 'decision_id');
    }

    public function capturedBy(): BelongsTo
    {
        return $this->belongsTo(User::class, 'captured_by_user_id', 'id');
    }
}
