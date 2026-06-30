<?php

declare(strict_types=1);

namespace App\Models\Silver;

use App\Models\User;
use Database\Factories\Silver\DecisionRecordFactory;
use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Factories\HasFactory;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;
use Illuminate\Database\Eloquent\Relations\HasMany;

/**
 * Eloquent model for `silver.decision_records` (master-plan §9.9 /
 * doc-phase 92 schema; doc-phase 110 model layer).
 *
 * Eight decision types per §21.3:
 *   target_recommendation | crs_decision | schema_mapping |
 *   public_data_import | export_approval | workflow_enablement |
 *   conflict_resolution | report_signoff
 *
 * RLS via workspace_id; hash + audit_ledger_id link to the chain
 * anchor in `audit.audit_ledger`.
 */
class DecisionRecord extends Model
{
    /** @use HasFactory<DecisionRecordFactory> */
    use HasFactory;
    use HasUuids;

    protected $table = 'silver.decision_records';

    protected $primaryKey = 'decision_id';

    public $incrementing = false;

    protected $keyType = 'string';

    public $timestamps = false;

    protected $fillable = [
        'workspace_id',
        'decision_type',
        'recommendation',
        'human_decision',
        'reason',
        'uncertainty',
        'decided_by_user_id',
        'decided_at',
        'hash',
        'audit_ledger_id',
    ];

    protected $casts = [
        'uncertainty' => 'float',
        'decided_at' => 'datetime',
    ];

    public function decidedBy(): BelongsTo
    {
        return $this->belongsTo(User::class, 'decided_by_user_id', 'id');
    }

    /**
     * Evidence chunks linked to this decision.
     */
    public function evidenceLinks(): HasMany
    {
        return $this->hasMany(
            DecisionEvidenceLink::class,
            'decision_id',
            'decision_id',
        );
    }

    /**
     * Options considered for this decision.
     */
    public function options(): HasMany
    {
        return $this->hasMany(DecisionOption::class, 'decision_id', 'decision_id');
    }

    /**
     * Post-decision outcomes.
     */
    public function outcomes(): HasMany
    {
        return $this->hasMany(DecisionOutcome::class, 'decision_id', 'decision_id');
    }

    /**
     * Retrospective lessons captured.
     */
    public function lessonsLearned(): HasMany
    {
        return $this->hasMany(
            DecisionLessonLearned::class,
            'decision_id',
            'decision_id',
        );
    }
}
