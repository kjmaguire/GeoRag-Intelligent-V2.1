<?php

declare(strict_types=1);

namespace App\Models;

use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;
use Illuminate\Support\Carbon;
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
 *
 * The @property block below is not decoration. Larastan infers Eloquent
 * attributes from the schema when it can reach one, and neither this
 * workstation nor the phpstan CI job has a database -- so every read of
 * `$row->query_text` was an "undefined property" living in
 * phpstan-baseline.neon instead. Twenty-eight such entries across six
 * files, and they disagreed between local and CI depending on what each
 * could introspect. Declaring the shape once here is what the baseline
 * was standing in for.
 *
 * Types follow $casts: encrypted values read back as strings, the three
 * array casts as arrays, confidence and the two deprecated scores as
 * floats, the timestamps as Carbon.
 *
 * @property string $audit_id
 * @property int|null $user_id
 * @property string|null $project_id
 * @property string|null $workspace_id
 * @property string|null $query_id
 * @property string|null $query_text
 * @property string|null $query_text_hash
 * @property string|null $response_text
 * @property array<int, mixed>|null $citations
 * @property array<int, mixed>|null $sources_used
 * @property float|null $confidence
 * @property int|null $response_time_ms
 * @property string|null $llm_model
 * @property string|null $ip_address
 * @property array<string, mixed>|null $metadata
 * @property Carbon|null $dispatched_at
 * @property Carbon|null $created_at
 * @property Carbon|null $updated_at
 * @property float|null $faithfulness_score
 * @property float|null $context_precision_score
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
        // DEPRECATED 2026-07-27. Both are kept fillable and cast so the
        // values written between 2026-05-30 and 2026-07-27 still read
        // back correctly, but NOTHING WRITES THEM ANY MORE:
        // score_answer_quality.py was removed in 09d1d35. A filter like
        // `where('faithfulness_score', '<', 0.5)` returns zero rows and
        // reads as "no low-faithfulness answers" when it means "nothing
        // has been scored since July". Live answer quality lives in
        // silver.answer_runs (see the answer_quality_watch workflow).
        'faithfulness_score',
        'context_precision_score',
    ];

    public function getTable()
    {
        $table = parent::getTable();

        if ($this->getConnection()->getDriverName() === 'sqlite') {
            return str_contains($table, '.')
                ? substr($table, (int) strrpos($table, '.') + 1)
                : $table;
        }

        return $table;
    }

    protected $casts = [
        'query_text' => 'encrypted',
        'response_text' => 'encrypted',
        'citations' => 'array',
        'sources_used' => 'array',
        'confidence' => 'float',
        // Guard-code durability (StreamQueryFromFastApi finalisation) —
        // without the cast, reads come back as a JSON string and the
        // merge in the job would clobber instead of extend.
        'metadata' => 'array',
        'dispatched_at' => 'datetime',
        // See the note on $fillable: deprecated, no writer since
        // 2026-07-27, NULL on every row created since.
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
