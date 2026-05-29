<?php

declare(strict_types=1);

namespace App\Models\Eval;

use App\Models\User;
use Database\Factories\Eval\GoldenQuestionFactory;
use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Factories\HasFactory;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

/**
 * Eloquent model for `eval.golden_questions` (master-plan §10.1 /
 * doc-phase 97 schema; doc-phase 109 model layer).
 *
 * Eight question sets per §24.1: core_chat | public_private_boundary |
 * numeric_grounding | refusal_correctness | target_recommendation |
 * report_section | schema_mapping | ocr_triage.
 *
 * Lifecycle: draft → active → retired. Geologist review gates the
 * draft → active transition (reviewed_by + reviewed_at populated).
 *
 * NO workspace_id — eval is GLOBAL operational data.
 */
class GoldenQuestion extends Model
{
    /** @use HasFactory<GoldenQuestionFactory> */
    use HasFactory;
    use HasUuids;

    protected $table = 'eval.golden_questions';

    protected $primaryKey = 'question_id';

    public $incrementing = false;

    protected $keyType = 'string';

    public $timestamps = false; // schema uses authored_at + reviewed_at, not created_at/updated_at

    protected $fillable = [
        'question_set',
        'question_text',
        'context_setup',
        'expected_intent_class',
        'expected_citations',
        'expected_entities',
        'expected_numeric_values',
        'expected_refusal',
        'expected_refusal_reason',
        'expected_language_compliance',
        'difficulty',
        'authored_by_user_id',
        'authored_at',
        'reviewed_by_user_id',
        'reviewed_at',
        'status',
    ];

    protected $casts = [
        'context_setup' => 'array',
        'expected_citations' => 'array',
        'expected_entities' => 'array',
        'expected_numeric_values' => 'array',
        'expected_refusal' => 'boolean',
        'expected_language_compliance' => 'array',
        'authored_at' => 'datetime',
        'reviewed_at' => 'datetime',
    ];

    /**
     * The user who authored this question.
     */
    public function author(): BelongsTo
    {
        return $this->belongsTo(User::class, 'authored_by_user_id', 'id');
    }

    /**
     * The user who reviewed this question (may be null on drafts).
     */
    public function reviewer(): BelongsTo
    {
        return $this->belongsTo(User::class, 'reviewed_by_user_id', 'id');
    }
}
