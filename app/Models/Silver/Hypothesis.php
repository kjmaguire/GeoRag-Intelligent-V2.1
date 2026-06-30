<?php

declare(strict_types=1);

namespace App\Models\Silver;

use App\Models\User;
use Database\Factories\Silver\HypothesisFactory;
use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Factories\HasFactory;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;
use Illuminate\Database\Eloquent\Relations\HasMany;

/**
 * Eloquent model for `silver.hypotheses` (master-plan §9.4 /
 * doc-phase 91 schema; doc-phase 110 model layer).
 *
 * Competing hypothesis for a parent question. Labels A/B/C/D etc.
 * Review states: ai_suggested | reviewed | accepted | rejected.
 *
 * RLS via workspace_id (silver.workspaces).
 */
class Hypothesis extends Model
{
    /** @use HasFactory<HypothesisFactory> */
    use HasFactory;
    use HasUuids;

    protected $table = 'silver.hypotheses';

    protected $primaryKey = 'hypothesis_id';

    public $incrementing = false;

    protected $keyType = 'string';

    public $timestamps = false; // schema uses created_at + reviewed_at only

    protected $fillable = [
        'workspace_id',
        'parent_question',
        'label',
        'description',
        'confidence',
        'confidence_method',
        'review_status',
        'reviewed_by_user_id',
        'reviewed_at',
        'rationale',
        'created_at',
    ];

    protected $casts = [
        'confidence' => 'float',
        'reviewed_at' => 'datetime',
        'created_at' => 'datetime',
    ];

    public function reviewer(): BelongsTo
    {
        return $this->belongsTo(User::class, 'reviewed_by_user_id', 'id');
    }

    /**
     * All evidence links (supporting / contradicting / missing /
     * recommended_test) attached to this hypothesis.
     */
    public function evidenceLinks(): HasMany
    {
        return $this->hasMany(
            HypothesisEvidenceLink::class,
            'hypothesis_id',
            'hypothesis_id',
        );
    }
}
