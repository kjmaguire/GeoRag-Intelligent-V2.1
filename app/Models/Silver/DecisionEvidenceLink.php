<?php

declare(strict_types=1);

namespace App\Models\Silver;

use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

/**
 * Eloquent model for `silver.decision_evidence_links` (master-plan
 * §9.9 / doc-phase 92 schema; doc-phase 110 model layer).
 *
 * Roles: supporting | contradicting | context.
 */
class DecisionEvidenceLink extends Model
{
    use HasUuids;

    protected $table = 'silver.decision_evidence_links';

    protected $primaryKey = 'link_id';

    public $incrementing = false;

    protected $keyType = 'string';

    public $timestamps = false;

    protected $fillable = ['decision_id', 'source_chunk_id', 'role', 'payload'];

    protected $casts = ['payload' => 'array'];

    public function decision(): BelongsTo
    {
        return $this->belongsTo(DecisionRecord::class, 'decision_id', 'decision_id');
    }
}
