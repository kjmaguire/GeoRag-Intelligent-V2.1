<?php

declare(strict_types=1);

namespace App\Models;

use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;
use Illuminate\Support\Facades\Crypt;

/**
 * Query audit log — every RAG query is recorded for NI 43-101 compliance.
 *
 * Immutable by design — no update method exposed. Once logged, a record
 * is only readable. The response_text and citations fields are populated
 * asynchronously by the StreamQueryFromFastApi job after the FastAPI
 * response stream completes.
 *
 * PII at rest (A4 fix):
 *   query_text and response_text are encrypted via Laravel's `encrypted`
 *   cast (APP_KEY). Ciphertext is non-deterministic, so a deterministic
 *   SHA-256 hash of the normalised query is written to query_text_hash on
 *   every save. The analytics layer groups by that hash instead of by the
 *   raw query. Plaintext is only ever visible through this model.
 */
class QueryAuditLog extends Model
{
    use HasUuids;

    // Schema-qualified per §05 step 6: audit data lives in its own schema,
    // not in `public`. Migration: 2026_05_07_120000_move_query_audit_log_to_audit_schema.php.
    protected $table = 'audit.query_audit_log';

    protected $primaryKey = 'audit_id';

    public $incrementing = false;

    protected $keyType = 'string';

    protected $fillable = [
        'user_id',
        'project_id',
        // Module 9 Chunk 9.8 — workspace scoping for NI 43-101 compliance trail.
        'workspace_id',
        'query_id',
        'query_text',
        'query_text_hash',
        'response_text',
        'citations',
        'sources_used',
        'confidence',
        'response_time_ms',
        'llm_model',
        'ip_address',
        'dispatched_at',
        'faithfulness_score',
        'context_precision_score',
    ];

    protected $casts = [
        'query_text' => 'encrypted',
        'response_text' => 'encrypted',
        'citations' => 'array',
        'sources_used' => 'array',
        'confidence' => 'float',
        'dispatched_at' => 'datetime',
        'faithfulness_score' => 'float',
        'context_precision_score' => 'float',
    ];

    /**
     * Keep query_text_hash in sync whenever query_text changes. The hash is
     * used by ProjectAnalyticsController::show() to group semantically-equal
     * queries without decrypting the whole reporting window.
     *
     * Normalisation: trim + lowercase so minor typography differences fold
     * into the same bucket. APP_KEY salts the HMAC so hashes are not
     * cross-tenant comparable.
     */
    public function setQueryTextAttribute(?string $value): void
    {
        // Use encryptString (no serialize wrapper) to match what Laravel's
        // `encrypted` cast does on read (decryptString, also no unserialize).
        // The global encrypt() helper serializes the value before encrypting,
        // which would leave `s:N:"..."` in the cast's output — caught by
        // QueryAuditPiiEncryptionTest after R1 unblocked feature tests.
        $this->attributes['query_text'] = $value === null
            ? null
            : Crypt::encryptString($value);

        $this->attributes['query_text_hash'] = $value === null
            ? null
            : self::hashQueryText($value);
    }

    /**
     * Deterministic hash for aggregation. Public so the encrypt-existing
     * backfill command and analytics queries can call it.
     */
    public static function hashQueryText(string $value): string
    {
        $normalised = mb_strtolower(trim($value));

        return hash_hmac('sha256', $normalised, config('app.key'));
    }

    public function user(): BelongsTo
    {
        return $this->belongsTo(User::class);
    }
}
