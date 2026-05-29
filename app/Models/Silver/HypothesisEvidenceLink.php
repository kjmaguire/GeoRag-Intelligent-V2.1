<?php

declare(strict_types=1);

namespace App\Models\Silver;

use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

/**
 * Eloquent model for `silver.hypothesis_evidence_links` (master-plan
 * §9.4 / doc-phase 91 schema; doc-phase 110 model layer).
 *
 * Roles: supporting | contradicting | missing | recommended_test.
 * Missing + recommended_test rows have NULL source_chunk_id (they
 * represent evidence-gaps, not actual chunks).
 *
 * RLS scoped via EXISTS on parent hypothesis (doc-phase 91 policy).
 */
class HypothesisEvidenceLink extends Model
{
    use HasUuids;

    protected $table = 'silver.hypothesis_evidence_links';

    protected $primaryKey = 'link_id';

    public $incrementing = false;

    protected $keyType = 'string';

    public $timestamps = false;

    protected $fillable = [
        'hypothesis_id',
        'source_chunk_id',
        'role',
        'weight',
        'payload',
    ];

    protected $casts = [
        'weight' => 'float',
        'payload' => 'array',
    ];

    public function hypothesis(): BelongsTo
    {
        return $this->belongsTo(Hypothesis::class, 'hypothesis_id', 'hypothesis_id');
    }
}
