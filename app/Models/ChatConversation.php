<?php

declare(strict_types=1);

namespace App\Models;

use Illuminate\Database\Eloquent\Concerns\HasUuids;
use Illuminate\Database\Eloquent\Model;
use Illuminate\Database\Eloquent\Relations\BelongsTo;
use Illuminate\Database\Eloquent\Relations\HasMany;

/**
 * A threaded chat conversation owned by a single user.
 *
 * Created when the user first submits a query in a new thread; title is
 * either user-set or derived from the first message (first 60 chars).
 */
class ChatConversation extends Model
{
    use HasUuids;

    protected $table = 'chat_conversations';

    protected $primaryKey = 'conversation_id';

    public $incrementing = false;

    protected $keyType = 'string';

    protected $fillable = [
        'conversation_id',
        'user_id',
        'title',
        'project_id',
    ];

    public function user(): BelongsTo
    {
        return $this->belongsTo(User::class);
    }

    public function messages(): HasMany
    {
        return $this->hasMany(ChatMessage::class, 'conversation_id', 'conversation_id')
            ->orderBy('created_at');
    }
}
