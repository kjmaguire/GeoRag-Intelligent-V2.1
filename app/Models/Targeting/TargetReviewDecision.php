<?php

declare(strict_types=1);

namespace App\Models\Targeting;

use App\Models\User;
use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

/**
 * Eloquent model for `targeting.target_review_decisions` (master-plan
 * §8.1 / doc-phase 85 schema; doc-phase 110 model layer).
 *
 * R5 sign-off ceremony record with QP credential metadata + hash
 * anchors back to the audit ledger. Decisions: accepted | modified
 * | rejected | signed_off.
 */
class TargetReviewDecision extends Model
{
    use HasUuids;

    protected $table = 'targeting.target_review_decisions';

    protected $primaryKey = 'decision_id';

    public $incrementing = false;

    protected $keyType = 'string';

    public $timestamps = false;

    protected $fillable = [
        'recommendation_id',
        'workspace_id',
        'qp_user_id',
        'qp_credential_id',
        'credential_verified_at',
        'target_recommendations_hash',
        'claim_ledger_hash',
        'decision',
        'rationale',
        'signed_at',
        'qp_signature_method',
        'audit_ledger_id',
    ];

    protected $casts = [
        'credential_verified_at' => 'datetime',
        'signed_at' => 'datetime',
    ];

    public function recommendation(): BelongsTo
    {
        return $this->belongsTo(
            TargetRecommendation::class,
            'recommendation_id',
            'recommendation_id'
        );
    }

    public function qpUser(): BelongsTo
    {
        return $this->belongsTo(User::class, 'qp_user_id', 'id');
    }
}
