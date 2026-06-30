<?php

declare(strict_types=1);

namespace App\Models;

use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;

/**
 * One message in a chat conversation. `metadata` stores everything on the
 * GeoRAGResponse that isn't the plain text (citations, confidence,
 * map_payload, viz_payload) so the UI can fully rehydrate past answers.
 */
class ChatMessage extends Model
{
    use HasUuids;

    protected $table = 'chat_messages';

    protected $primaryKey = 'message_id';

    public $incrementing = false;

    protected $keyType = 'string';

    // The chat_messages table only has created_at — messages are append-
    // only. Suppress Eloquent's default updated_at column so it isn't
    // added to INSERT payloads.
    const UPDATED_AT = null;

    protected $fillable = [
        'message_id',
        'conversation_id',
        'role',
        'content',
        'metadata',
    ];

    protected $casts = [
        'metadata' => 'array',
    ];

    public function conversation(): BelongsTo
    {
        return $this->belongsTo(ChatConversation::class, 'conversation_id', 'conversation_id');
    }
}
